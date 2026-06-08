"""Dependency-aware attribute scheduling.

Port of the behavioral core of nixbot/build_trigger.py
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
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
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
    by_drv: dict[str, list[NixEvalJobSuccess]] = {}
    for job in jobs:
        by_drv.setdefault(job.drv_path, []).append(job)
    sorter = graphlib.TopologicalSorter(job_closures)
    return [job for item in sorter.static_order() for job in by_drv.get(item, ())]


def compute_reverse_deps(job_closures: dict[str, set[str]]) -> dict[str, set[str]]:
    """drv_path -> drv_paths of jobs whose closure contains it."""
    reverse: dict[str, set[str]] = {}
    for drv, deps in job_closures.items():
        for dep in deps:
            reverse.setdefault(dep, set()).add(drv)
    return reverse


def get_failed_dependents(
    job: NixEvalJobSuccess,
    jobs: list[NixEvalJobSuccess],
    job_closures: dict[str, set[str]],
) -> list[NixEvalJobSuccess]:
    """Transitive dependents of a failed job among `jobs`.

    MUST be called before the failed job is pruned from job_closures —
    the closure sets are how the dependency edges are found. The BFS
    expands only through jobs in `jobs`: a dependent reachable solely
    via an already-finished job is not failed.
    """
    reverse_deps = compute_reverse_deps(job_closures)
    by_drv: dict[str, list[NixEvalJobSuccess]] = {}
    for build in jobs:
        by_drv.setdefault(build.drv_path, []).append(build)
    removed: list[NixEvalJobSuccess] = []
    seen: set[str] = set()
    stack = [job.drv_path]
    while stack:
        drv = stack.pop()
        for dependent in reverse_deps.get(drv, ()):
            if dependent in by_drv and dependent not in seen:
                seen.add(dependent)
                stack.append(dependent)
                removed.extend(by_drv[dependent])
    return removed


def compute_job_closures(
    jobs: list[NixEvalJobSuccess],
) -> dict[str, set[str]]:
    job_set = {job.drv_path for job in jobs}
    return {
        job.drv_path: (job_set & {*job.needed_builds, *job.needed_substitutes})
        - {job.drv_path}
        for job in jobs
    }


class _ResultEmitter:
    """Reports each new ScheduleResult entry to on_result exactly once."""

    def __init__(
        self,
        result: ScheduleResult,
        on_result: Callable[[AttributeResult], Awaitable[None]] | None,
    ) -> None:
        self.result = result
        self.on_result = on_result
        self.emitted = 0

    async def flush(self) -> None:
        while self.emitted < len(self.result.results):
            pending_result = self.result.results[self.emitted]
            self.emitted += 1
            if self.on_result is not None:
                await self.on_result(pending_result)


def _fail_unresolvable(state: _State) -> None:
    """Dependency cycle or external dep: should not happen; fail the
    remainder instead of spinning."""
    for job in state.pending:
        state.result.results.append(
            _success_result(
                job, AttributeStatus.failed, error="unresolvable dependencies"
            )
        )
    state.pending.clear()


def _success_result(
    job: NixEvalJobSuccess,
    status: AttributeStatus,
    *,
    error: str | None = None,
    dependency_attr: str | None = None,
    first_failure_url: str | None = None,
) -> AttributeResult:
    return AttributeResult(
        attr=job.attr,
        status=status,
        job=job,
        out_path=job.outputs.get("out"),
        drv_path=job.drv_path,
        system=job.system,
        error=error,
        dependency_attr=dependency_attr,
        first_failure_url=first_failure_url,
    )


@dataclass
class _State:
    """Mutable scheduling state shared by the loop helpers."""

    result: ScheduleResult
    pending: list[NixEvalJobSuccess]
    job_closures: dict[str, set[str]]
    # Static reverse edges from the initial closures: which jobs'
    # closures contain a given drv. Lets prune touch only actual
    # dependents instead of every closure (O(deps) vs O(jobs)).
    reverse_deps: dict[str, set[str]] = field(default_factory=dict)
    # Drvs that failed (or were cancelled / dependency-failed): jobs
    # arriving in later batches that depend on one of these must not
    # build. post_build_failure drvs are absent on purpose - their
    # store paths exist.
    failed_drvs: set[str] = field(default_factory=set)
    # Subset of failed_drvs that were cancelled rather than failed:
    # their late-arriving dependents settle as cancelled, not
    # dependency_failed (batch timing must not flip a superseded build
    # from cancelled to failed).
    cancelled_drvs: set[str] = field(default_factory=set)
    # Delivery is at-least-once (final re-send): re-ingesting a seen
    # attr must be a no-op or settled jobs build twice.
    seen_attrs: set[str] = field(default_factory=set)

    def prune(self, drv_path: str) -> None:
        for dependent in self.reverse_deps.get(drv_path, ()):
            self.job_closures[dependent].discard(drv_path)

    def fail_dependents(self, job: NixEvalJobSuccess, status: AttributeStatus) -> None:
        """Invariant: dependents are computed BEFORE the finished job is
        pruned from the closures."""
        dependents = get_failed_dependents(job, self.pending, self.job_closures)
        settled = {job.drv_path} | {d.drv_path for d in dependents}
        self.failed_drvs |= settled
        if status == AttributeStatus.cancelled:
            self.cancelled_drvs |= settled
        self.pending = [j for j in self.pending if j.drv_path not in settled]
        for dependent in dependents:
            self.result.results.append(
                _success_result(dependent, status, dependency_attr=job.attr)
            )
            self.prune(dependent.drv_path)


class JobScheduler:
    """Schedules evaluated attributes in dependency order."""

    def __init__(
        self,
        executor: BuildExecutor,
        supported_systems: list[str],
        failed_build_cache: FailedBuildCache | None = None,
        *,
        build_url: str = "",
        on_result: Callable[[AttributeResult], Awaitable[None]] | None = None,
    ) -> None:
        self.executor = executor
        self.supported_systems = supported_systems
        self.failed_build_cache = failed_build_cache
        self.build_url = build_url
        # Called for each result as soon as it is known.
        self.on_result = on_result

    def _partition_jobs(
        self, jobs: list[NixEvalJob], result: ScheduleResult
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
            elif job.system in self.supported_systems:
                successful_jobs.append(job)
            # Unsupported systems are dropped: the orchestrator never
            # records attribute rows for them, so they neither fail the
            # build nor block aggregation.
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
                job for job in state.pending if not state.job_closures.get(job.drv_path)
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
                    state.prune(job.drv_path)

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
                await self.failed_build_cache.add(job.drv_path, self.build_url)
            # Cancelled jobs propagate cancellation, never
            # dependency_failed, and are not cached as failures.
            state.fail_dependents(
                job,
                AttributeStatus.cancelled
                if cancelled
                else AttributeStatus.dependency_failed,
            )
        state.prune(job.drv_path)

    async def _wait_for_progress(
        self,
        state: _State,
        running: dict[asyncio.Task[BuildOutcome], NixEvalJobSuccess],
        queue: asyncio.Queue[list[NixEvalJob] | None],
        get_task: asyncio.Task[list[NixEvalJob] | None] | None,
        flush: Callable[[], Awaitable[None]],
    ) -> asyncio.Task[list[NixEvalJob] | None] | None:
        """Block until a new eval batch arrives or a running build
        finishes, then fold the outcome into the state. Returns the
        (possibly replaced) queue reader task; None after the
        end-of-input sentinel."""
        waits: set[asyncio.Task[object]] = set(running)
        if get_task is not None:
            waits.add(get_task)
        done_tasks, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        if get_task is not None and get_task in done_tasks:
            done_tasks.discard(get_task)
            get_task = self._on_queue_item(state, running, queue, get_task.result())
        for done in done_tasks:
            # Only build tasks remain after the queue reader was discarded.
            task = cast("asyncio.Task[BuildOutcome]", done)
            await self._finish_job(state, running.pop(task), task.result())
            await flush()
        return get_task

    def _on_queue_item(
        self,
        state: _State,
        running: dict[asyncio.Task[BuildOutcome], NixEvalJobSuccess],
        queue: asyncio.Queue[list[NixEvalJob] | None],
        batch: list[NixEvalJob] | None,
    ) -> asyncio.Task[list[NixEvalJob] | None] | None:
        """Ingest one queue item; returns the next reader task, or None
        on the end-of-input sentinel."""
        if batch is None:
            return None
        self._ingest(state, running, batch)
        return asyncio.create_task(queue.get())

    def _ingest(
        self,
        state: _State,
        running: dict[asyncio.Task[BuildOutcome], NixEvalJobSuccess],
        batch: list[NixEvalJob],
    ) -> None:
        """Merge a batch of eval results into the live scheduling state.

        Closures are recomputed over pending + running jobs only:
        dependencies on drvs that already succeeded drop out naturally,
        dependencies on failed drvs settle the new job immediately."""
        fresh = [job for job in batch if job.attr not in state.seen_attrs]
        state.seen_attrs.update(job.attr for job in fresh)
        new_jobs = self._partition_jobs(fresh, state.result)
        # Settle jobs depending on failed drvs. Iterate new and already
        # pending jobs to a fixpoint: neither batches nor batch arrival
        # order is topological, so a dependent may precede the job that
        # turns out dependency-failed.
        candidates = state.pending + new_jobs
        settled_any = True
        while settled_any:
            settled_any = False
            remaining: list[NixEvalJobSuccess] = []
            for job in candidates:
                deps = {*job.needed_builds, *job.needed_substitutes}
                blocking = deps & state.failed_drvs
                if blocking:
                    state.failed_drvs.add(job.drv_path)
                    # A genuine failure among the deps wins; only purely
                    # cancelled deps propagate cancellation.
                    if blocking <= state.cancelled_drvs:
                        state.cancelled_drvs.add(job.drv_path)
                        status = AttributeStatus.cancelled
                    else:
                        status = AttributeStatus.dependency_failed
                    state.result.results.append(_success_result(job, status))
                    settled_any = True
                else:
                    remaining.append(job)
            candidates = remaining
        state.pending = candidates
        unfinished = state.pending + list(running.values())
        state.job_closures = compute_job_closures(unfinished)
        state.reverse_deps = compute_reverse_deps(state.job_closures)
        state.pending = sort_jobs_by_closures(state.pending, state.job_closures)

    async def run(self, jobs: list[NixEvalJob]) -> ScheduleResult:
        queue: asyncio.Queue[list[NixEvalJob] | None] = asyncio.Queue()
        queue.put_nowait(list(jobs))
        queue.put_nowait(None)
        return await self.run_incremental(queue)

    async def run_incremental(
        self, queue: asyncio.Queue[list[NixEvalJob] | None]
    ) -> ScheduleResult:
        """Schedule batches of eval results as they arrive on `queue`,
        starting builds while the evaluation is still running. A None
        sentinel marks the end of input."""
        result = ScheduleResult()
        state = _State(result=result, pending=[], job_closures={})
        running: dict[asyncio.Task[BuildOutcome], NixEvalJobSuccess] = {}
        flush = _ResultEmitter(result, self.on_result).flush
        # get_task is None once the end-of-input sentinel arrived.
        get_task: asyncio.Task[list[NixEvalJob] | None] | None = asyncio.create_task(
            queue.get()
        )
        try:
            while get_task is not None or state.pending or running:
                any_ready = await self._dispatch_ready(state, running)
                await flush()

                if not running and get_task is None:
                    if state.pending and not any_ready:
                        _fail_unresolvable(state)
                    continue

                get_task = await self._wait_for_progress(
                    state, running, queue, get_task, flush
                )
        finally:
            # On cancellation (eval failure, superseded build) stop the
            # in-flight executor tasks instead of orphaning them.
            if get_task is not None:
                get_task.cancel()
            for task in running:
                task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)

        await flush()
        return result

    async def _classify(self, job: NixEvalJobSuccess, result: ScheduleResult) -> str:
        """Decide whether to run or skip a ready job, recording skip results."""
        # An output already in the local store is not a failure: the
        # local-store skip must win over a stale failed-build cache
        # entry for the same drv.
        if job.cache_status == CacheStatus.local:
            result.results.append(_success_result(job, AttributeStatus.skipped_local))
            return "skip"

        if self.failed_build_cache is not None:
            cached = await self.failed_build_cache.check(job.drv_path)
            if cached is not None:
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

        # Everything else builds: a missing/unknown cacheStatus must
        # not pass as "already built", and a forced local job runs so
        # the completion flow flips its forge status.
        return "run"
