"""Dependency-aware attribute scheduling.

Port of the behavioral core of buildbot_nix/build_trigger.py
(BuildTrigger) without buildbot schedulers/steps:

- topological order by derivation closures,
- dependency-failure propagation, preserving the invariant that failed
  dependents are computed BEFORE the finished job is pruned from the
  closures,
- supported-systems filter,
- CacheStatus handling: `local` jobs are skipped but their out paths
  recorded for gcroots/outputs (unless forced to run to flip a
  previously failed status); `cached` jobs are scheduled so nix
  substitutes them,
- per-attribute failed-eval records,
- opt-in failed-build cache skip (storage interface; wired in 2.3).

Build execution and the global concurrency cap live behind the
BuildExecutor protocol; status reporting is layered on top
by forge integration.
"""

from __future__ import annotations

import asyncio
import graphlib
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime

from .models import (
    CacheStatus,
    NixEvalJob,
    NixEvalJobError,
    NixEvalJobSuccess,
)

logger = logging.getLogger(__name__)


class BuildOutcome(StrEnum):
    success = "success"
    failure = "failure"
    # Failure that must not enter the failed-build cache (internal
    # errors): the derivation itself is not known to be broken, and
    # dependents are still failed.
    failure_no_cache = "failure_no_cache"
    # The derivation built fine but a post-build step failed: fails the
    # attribute without poisoning the failed-build cache or failing
    # dependents (the store path exists).
    post_build_failure = "post_build_failure"
    cancelled = "cancelled"


class BuildExecutor(Protocol):
    """Builds one attribute; implements the global concurrency cap."""

    async def build(self, job: NixEvalJobSuccess) -> BuildOutcome: ...


@dataclass(frozen=True)
class CachedFailure:
    drv_path: str
    time: datetime
    url: str


class FailedBuildCache(Protocol):
    """Storage for the opt-in failed-build cache."""

    async def check(self, drv_path: str) -> CachedFailure | None: ...

    async def add(self, drv_path: str, url: str) -> None: ...

    async def remove(self, drv_path: str) -> None: ...


class AttributeStatus(StrEnum):
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    failed_eval = "failed_eval"
    dependency_failed = "dependency_failed"
    cached_failure = "cached_failure"
    # Already present in the local store: not rebuilt, out path recorded
    # for gcroot registration and outputs updates.
    skipped_local = "skipped_local"


TERMINAL_FAILURES = {
    AttributeStatus.failed,
    AttributeStatus.failed_eval,
    AttributeStatus.dependency_failed,
    AttributeStatus.cached_failure,
}


@dataclass
class AttributeResult:
    attr: str
    status: AttributeStatus
    job: NixEvalJob
    error: str | None = None
    # Attr of the dependency whose failure propagated here.
    dependency_attr: str | None = None
    # Link to the first failing build for cached failures.
    first_failure_url: str | None = None
    out_path: str | None = None
    drv_path: str | None = None
    system: str | None = None


@dataclass
class ScheduleResult:
    results: list[AttributeResult] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return not any(
            r.status in TERMINAL_FAILURES or r.status == AttributeStatus.cancelled
            for r in self.results
        )

    @property
    def skipped_out_paths(self) -> list[tuple[str, str]]:
        """(attr, out_path) of locally-present jobs that were skipped;
        these still get gcroots/outputs updates."""
        return [
            (r.attr, r.out_path)
            for r in self.results
            if r.status == AttributeStatus.skipped_local and r.out_path is not None
        ]


def sort_jobs_by_closures(
    jobs: list[NixEvalJobSuccess], job_closures: dict[str, set[str]]
) -> list[NixEvalJobSuccess]:
    """Topological order by derivation closure (dependencies first)."""
    jobs = list(jobs)
    sorted_jobs = []
    sorter = graphlib.TopologicalSorter(job_closures)
    for item in sorter.static_order():
        i = 0
        while i < len(jobs):
            if item == jobs[i].drvPath:
                sorted_jobs.append(jobs[i])
                del jobs[i]
            else:
                i += 1
    return sorted_jobs


def get_failed_dependents(
    job: NixEvalJobSuccess,
    jobs: list[NixEvalJobSuccess],
    job_closures: dict[str, set[str]],
) -> list[NixEvalJobSuccess]:
    """Transitive dependents of a failed job.

    MUST be called before the failed job is pruned from job_closures —
    the closure sets are how the dependency edges are found.
    """
    jobs = list(jobs)
    failed_paths: list[str] = [job.drvPath]
    removed = []
    while True:
        old_paths = list(failed_paths)
        for build in list(jobs):
            deps: set[str] = job_closures.get(build.drvPath, set())
            for path in old_paths:
                if path in deps:
                    failed_paths.append(build.drvPath)
                    jobs.remove(build)
                    removed.append(build)
                    break
        if old_paths == failed_paths:
            break
    return removed


def compute_job_closures(
    jobs: list[NixEvalJobSuccess],
) -> dict[str, set[str]]:
    job_set = {job.drvPath for job in jobs}
    return {
        k.drvPath: set(k.neededSubstitutes)
        .union(set(k.neededBuilds))
        .intersection(job_set)
        .difference({k.drvPath})
        for k in jobs
    }


def _success_result(
    job: NixEvalJobSuccess, status: AttributeStatus, **kw: str | None
) -> AttributeResult:
    return AttributeResult(
        attr=job.attr,
        status=status,
        job=job,
        out_path=job.outputs.get("out"),
        drv_path=job.drvPath,
        system=job.system,
        **kw,  # type: ignore[arg-type]
    )


@dataclass
class _State:
    """Mutable scheduling state shared by the loop helpers."""

    result: ScheduleResult
    pending: list[NixEvalJobSuccess]
    job_closures: dict[str, set[str]]

    def prune(self, drv_path: str) -> None:
        for closure in self.job_closures.values():
            closure.discard(drv_path)

    def fail_dependents(self, job: NixEvalJobSuccess, status: AttributeStatus) -> None:
        """Invariant: dependents are computed BEFORE the finished job is
        pruned from the closures."""
        for dependent in get_failed_dependents(job, self.pending, self.job_closures):
            self.pending.remove(dependent)
            self.result.results.append(
                _success_result(dependent, status, dependency_attr=job.attr)
            )
            self.prune(dependent.drvPath)


class JobScheduler:
    """Schedules evaluated attributes in dependency order."""

    def __init__(  # noqa: PLR0913
        self,
        executor: BuildExecutor,
        supported_systems: list[str],
        failed_build_cache: FailedBuildCache | None = None,
        *,
        is_rebuild: bool = False,
        force_attrs: set[str] | None = None,
        build_url: str = "",
    ) -> None:
        self.executor = executor
        self.supported_systems = supported_systems
        self.failed_build_cache = failed_build_cache
        self.is_rebuild = is_rebuild
        # Attrs that would be skipped as already built but must run
        # anyway to flip a previously failed forge status.
        self.force_attrs = force_attrs or set()
        self.build_url = build_url

    @staticmethod
    def _partition_jobs(
        jobs: list[NixEvalJob],
        supported_systems: list[str],
        result: ScheduleResult,
    ) -> list[NixEvalJobSuccess]:
        """Split eval output: per-attribute failed-eval records, plus the
        buildable jobs filtered by supported systems."""
        successful_jobs: list[NixEvalJobSuccess] = []
        for job in jobs:
            if isinstance(job, NixEvalJobError):
                result.results.append(
                    AttributeResult(
                        attr=job.attr,
                        status=AttributeStatus.failed_eval,
                        job=job,
                        error=job.error,
                    )
                )
            elif isinstance(job, NixEvalJobSuccess) and job.system in supported_systems:
                successful_jobs.append(job)
        return successful_jobs

    async def _dispatch_ready(
        self,
        state: _State,
        running: dict[asyncio.Task[BuildOutcome], NixEvalJobSuccess],
    ) -> bool:
        """Start or skip all dependency-free jobs. Returns whether any
        job was ready. Loops until a fixpoint: skipping a job prunes it
        from the closures, which can free its dependents immediately."""
        any_ready = False
        while True:
            ready = [
                job for job in state.pending if not state.job_closures.get(job.drvPath)
            ]
            if not ready:
                return any_ready
            any_ready = True
            for job in ready:
                state.pending.remove(job)
                action = await self._classify(job, state.result)
                if action == "run":
                    task = asyncio.create_task(self.executor.build(job))
                    running[task] = job
                else:
                    if action == "skip-failed":
                        # Cached failure counts as a failure: propagate to
                        # dependents before pruning.
                        state.fail_dependents(job, AttributeStatus.dependency_failed)
                    state.prune(job.drvPath)

    async def _finish_job(
        self, state: _State, job: NixEvalJobSuccess, outcome: BuildOutcome
    ) -> None:
        if outcome == BuildOutcome.success:
            state.result.results.append(_success_result(job, AttributeStatus.succeeded))
        elif outcome == BuildOutcome.post_build_failure:
            # The derivation itself built: do not cache it as a failed
            # build and do not fail dependents (their dependency exists
            # in the store), but the attribute is failed.
            state.result.results.append(_success_result(job, AttributeStatus.failed))
        else:
            cancelled = outcome == BuildOutcome.cancelled
            state.result.results.append(
                _success_result(
                    job,
                    AttributeStatus.cancelled if cancelled else AttributeStatus.failed,
                )
            )
            if outcome == BuildOutcome.failure and self.failed_build_cache is not None:
                await self.failed_build_cache.add(job.drvPath, self.build_url)
            # Cancelled jobs propagate cancellation, never
            # dependency_failed, and are not cached as failures.
            state.fail_dependents(
                job,
                AttributeStatus.cancelled
                if cancelled
                else AttributeStatus.dependency_failed,
            )
        state.prune(job.drvPath)

    async def run(self, jobs: list[NixEvalJob]) -> ScheduleResult:
        result = ScheduleResult()
        successful_jobs = self._partition_jobs(jobs, self.supported_systems, result)
        job_closures = compute_job_closures(successful_jobs)
        state = _State(
            result=result,
            pending=sort_jobs_by_closures(successful_jobs, job_closures),
            job_closures=job_closures,
        )
        running: dict[asyncio.Task[BuildOutcome], NixEvalJobSuccess] = {}

        while state.pending or running:
            any_ready = await self._dispatch_ready(state, running)

            if not running:
                if state.pending and not any_ready:
                    # Dependency cycle or external dep: should not happen;
                    # fail the remainder instead of spinning.
                    for job in state.pending:
                        result.results.append(
                            _success_result(
                                job,
                                AttributeStatus.failed,
                                error="unresolvable dependencies",
                            )
                        )
                    state.pending.clear()
                continue

            done_tasks, _ = await asyncio.wait(
                running, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done_tasks:
                job = running.pop(task)
                await self._finish_job(state, job, task.result())

        return result

    async def _classify(self, job: NixEvalJobSuccess, result: ScheduleResult) -> str:
        """Decide whether to run or skip a ready job, recording skip results."""
        if self.failed_build_cache is not None:
            cached = await self.failed_build_cache.check(job.drvPath)
            if cached is not None:
                if self.is_rebuild:
                    # Rebuild requested: drop the cache entry and build.
                    await self.failed_build_cache.remove(job.drvPath)
                    return "run"
                result.results.append(
                    _success_result(
                        job,
                        AttributeStatus.cached_failure,
                        first_failure_url=cached.url,
                        error=(
                            f"skipped due to cached failure, first failed at "
                            f"{cached.time}" + (f": {cached.url}" if cached.url else "")
                        ),
                    )
                )
                return "skip-failed"

        if job.cacheStatus in {CacheStatus.notBuilt, CacheStatus.cached}:
            # cached: schedule so nix substitutes it.
            return "run"

        # Already in the local store.
        if job.attr in self.force_attrs:
            # Previously reported failed: run anyway so the normal
            # completion flow flips the forge status to success.
            return "run"
        result.results.append(_success_result(job, AttributeStatus.skipped_local))
        return "skip"
