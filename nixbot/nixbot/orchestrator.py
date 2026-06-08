"""Build orchestrator: change event → build record → eval → attribute
builds → aggregate result.

Wires together the repo manager (clone/worktree + PR merge), the eval
runner, the dependency-aware scheduler, the build executor, gcroots/
outputs updates, post-build steps, and effects. Status reporting is a
callback so forge integration (4.4) and the web frontend can subscribe
without coupling.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote

from . import gcroots, outputs
from .canceller import (
    CancellationManager,
    RegisterOutcome,
    branch_key,
    has_skip_ci_marker,
)
from .db import BuildStatus
from .effects import (
    EffectsContext,
    EffectsError,
    effects_context,
    list_effects,
    run_effect,
    should_run_effects,
)
from .effects_state import TaskTokens
from .events import ChangeEvent, NullStatusReporter, RepoInfo, StatusReporter
from .executor import LogWriter, failure_excerpt
from .gitrepo import GitError, MergeConflictError, run_git
from .live_warnings import LiveWarningAggregator
from .memory import calculate_eval_workers
from .models import NixEvalJobSuccess
from .nix_eval import EvalError, EvalSettings
from .post_build import build_props, run_post_build_steps
from .repo_config import CONFIG_FILENAMES, BranchConfig
from .scheduler import (
    AttributeResult,
    AttributeStatus,
    BuildOutcome,
    JobScheduler,
)
from .web.logs import LogRegistry
from .work_queue import WorkQueue

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
    from pathlib import Path

    from .config import Config
    from .db import BuildDB, BuildRecord
    from .gitrepo import FetchCredentials, RepoManager
    from .models import NixEvalJob
    from .nix_eval import EvalResult, JobBatchCallback, StderrLineCallback
    from .scheduler import CachedFailure, FailedBuildCache

    GcrootRegistrar = Callable[[Path, str, str, str], Awaitable[None]]
    OutputWriter = Callable[[Path, str, str, str, str, str, str], Path]

logger = logging.getLogger(__name__)

LIVE_WARNINGS_FLUSH_INTERVAL = 2.0


def pr_refspec(forge: str, pr_number: int) -> str:
    """GitLab serves MR heads under refs/merge-requests/<iid>/*;
    GitHub and Gitea use refs/pull/<number>/*."""
    ref = (
        f"refs/merge-requests/{pr_number}"
        if forge == "gitlab"
        else f"refs/pull/{pr_number}"
    )
    return f"+{ref}/*:{ref}/*"


class EvalRunnerLike(Protocol):
    """What the orchestrator needs from nix_eval.EvalRunner."""

    async def run(
        self,
        worktree_path: Path,
        branch_config: BranchConfig,
        settings: EvalSettings,
        on_jobs: JobBatchCallback | None = None,
        on_stderr_line: StderrLineCallback | None = None,
    ) -> EvalResult: ...


class AttributeExecutor(Protocol):
    """What the orchestrator needs from executor.NixBuildExecutor."""

    async def build_attribute(
        self,
        build_key: object,
        job: NixEvalJobSuccess,
        log_writer: LogWriter,
        cwd: Path,
        cancel_event: asyncio.Event | None = None,
    ) -> BuildOutcome: ...


@dataclass
class Orchestrator:
    config: Config
    db: BuildDB
    repos: RepoManager
    eval_runner: EvalRunnerLike
    executor: AttributeExecutor
    reporter: StatusReporter = field(default_factory=NullStatusReporter)
    # Project id -> cache; scoped so one project's failures cannot
    # affect another's builds.
    failed_build_cache: Callable[[int], FailedBuildCache] | None = None
    # build id -> cancel event, set by the cancellation manager.
    cancel_events: dict[int, asyncio.Event] = field(default_factory=dict)
    # (build id, attr) -> cancel event for a single queued/running
    # attribute; registered for the lifetime of the executor job.
    attr_cancel_events: dict[tuple[int, str], asyncio.Event] = field(
        default_factory=dict
    )
    # Injectable for tests; defaults to the real implementations.
    register_gcroot: GcrootRegistrar = gcroots.register_gcroot
    write_output_path: OutputWriter = outputs.write_output_path
    canceller: CancellationManager = field(default_factory=CancellationManager)
    # Live log fan-out for the web frontend's SSE endpoints.
    log_registry: LogRegistry = field(default_factory=LogRegistry)
    # Per-run bearer tokens for the hercules state API.
    task_tokens: TaskTokens = field(default_factory=TaskTokens)
    # Second contexts attached to an in-flight build, for status fan-out.
    linked_events: dict[int, list[ChangeEvent]] = field(default_factory=dict)

    def _log_dir(self, build_id: int) -> Path:
        return self.config.state_dir / "logs" / str(build_id)

    def _gcroots_dir(self, build: BuildRecord) -> Path:
        return self.config.state_dir / "eval-gcroots" / str(build.id)

    async def handle_change_event(
        self, event: ChangeEvent, credentials: FetchCredentials | None = None
    ) -> BuildRecord | None:
        """Full lifecycle for one change event. Returns the build record
        (None when the checkout failed before a build existed)."""
        repo = event.repo

        if has_skip_ci_marker(event.commit_message):
            logger.info(
                "skipping build due to [skip ci] marker",
                extra={"repo": repo.name, "commit": event.commit_sha},
            )
            return None

        # Fetch and create the per-build worktree; PR head is merged
        # into the base branch locally. PR refs are fetched per PR:
        # fetching all of them is unbounded on PR-heavy repos.
        refspecs = ["+refs/heads/*:refs/heads/*"]
        if event.pr_number is not None:
            refspecs.append(pr_refspec(repo.forge, event.pr_number))
        await self.repos.fetch(repo.key, repo.clone_url, refspecs, credentials)
        try:
            # Unique token: concurrent events for the same commit must
            # not share (and destroy) one checkout.
            worktree = await self.repos.checkout_for_build(
                repo.key,
                f"{repo.id}-{event.commit_sha[:12]}-{uuid.uuid4().hex[:8]}",
                base_commit=event.base_sha or event.commit_sha,
                head_commit=event.commit_sha if event.base_sha else None,
            )
        except MergeConflictError as e:
            # Merge conflict: failed build, status on the head SHA.
            build = await self.db.create_failed_build(
                repo.id,
                event.commit_sha,
                event.branch,
                str(e),
                pr_number=event.pr_number,
                pr_author=event.pr_author,
            )
            await self.reporter.eval_finished(event, build, success=False, warnings=[])
            await self.reporter.build_finished(event, build, BuildStatus.FAILED, 0, [])
            return build

        try:
            tree_hash = await worktree.tree_hash()
            build, created = await self.db.get_or_create_build(
                repo.id,
                tree_hash,
                event.commit_sha,
                event.branch,
                pr_number=event.pr_number,
                pr_author=event.pr_author,
            )
            await self._dispatch_build(
                event,
                build,
                created=created,
                tree_hash=tree_hash,
                worktree_path=worktree.path,
                credentials=credentials,
            )
            return build
        finally:
            await self.repos.remove_worktree(worktree)

    async def _dispatch_build(  # noqa: PLR0913
        self,
        event: ChangeEvent,
        build: BuildRecord,
        *,
        created: bool,
        tree_hash: str,
        worktree_path: Path,
        credentials: FetchCredentials | None,
    ) -> None:
        """Decide what this event means for the (possibly shared) build:
        reuse a terminal result, drop a stale event, attach to an
        in-flight build, or run it."""
        repo = event.repo
        key = branch_key(event.branch, event.pr_number)
        # A branch push reusing a PR build already shed the PR identity
        # (and took over the branch field) in get_or_create_build.
        # Out-of-order delivery check: an event whose commit is an
        # ancestor of the context's running build is stale. Checked
        # before the reuse branch too: a stale redelivery matching an
        # old terminal build must not supersede the in-flight build.
        incoming_stale = False
        running_commit = self.canceller.running_commit_for(repo.id, key)
        if running_commit is not None and running_commit != event.commit_sha:
            incoming_stale = await self._is_ancestor(
                repo.key, event.commit_sha, running_commit
            )

        if not created and build.status in (
            BuildStatus.SUCCEEDED,
            BuildStatus.FAILED,
        ):
            await self._reuse_terminal_build(
                event,
                build,
                key,
                tree_hash,
                worktree_path=worktree_path,
                credentials=credentials,
                incoming_stale=incoming_stale,
            )
            return

        in_flight = build.id in self.cancel_events
        rebuild = created or (not in_flight and build.status == BuildStatus.CANCELLED)
        if rebuild or in_flight:
            cancel_event = self.cancel_events.setdefault(build.id, asyncio.Event())
        else:
            # Not running here (e.g. crashed build awaiting recovery):
            # a stored entry would block its rerun.
            cancel_event = asyncio.Event()
        outcome = self.canceller.register(
            repo.id,
            key,
            build.id,
            tree_hash,
            event.commit_sha,
            cancel_event,
            incoming_is_ancestor_of_running=incoming_stale,
        )
        if outcome == RegisterOutcome.STALE:
            if rebuild:
                self.cancel_events.pop(build.id, None)
            if created:
                await self.db.set_build_status(build.id, BuildStatus.CANCELLED)
                await self.reporter.build_finished(
                    event, build, BuildStatus.CANCELLED, build.status_generation, []
                )
            return
        if not rebuild:
            await self._attach_linked_event(event, build)
            return
        try:
            await self.run_build(event, build, worktree_path, credentials)
        finally:
            self.canceller.complete(build.id)
            self.cancel_events.pop(build.id, None)
            self.linked_events.pop(build.id, None)

    async def run_build(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        worktree_path: Path,
        credentials: FetchCredentials | None = None,
    ) -> None:
        """Evaluate and build; every attribute completion is one
        transactional DB write, then the result is re-aggregated."""
        try:
            await self._run_build_inner(event, build, worktree_path, credentials)
        except Exception as e:
            # Catch-all: a DB outage or GitError mid-eval would
            # otherwise wedge the build in 'evaluating' with no
            # terminal forge status.
            if isinstance(e, EvalError):
                logger.warning(
                    "evaluation failed",
                    extra={"build_id": build.id, "error": str(e)},
                )
            else:
                logger.exception(
                    "build failed with unexpected error", extra={"build_id": build.id}
                )
            # Skip settling when the final fan-out already happened
            # (e.g. the effects phase failed): the build's aggregated
            # result must not be overwritten with a failure.
            current = await self.db.get_build(build.id)
            if current is None or current.status not in BuildStatus.TERMINAL:
                await self._settle_aborted(
                    event, build, BuildStatus.FAILED, error=str(e)
                )
        finally:
            # Eval gc-roots only need to outlive the build; without
            # cleanup the nix store grows unboundedly.
            shutil.rmtree(self._gcroots_dir(build), ignore_errors=True)

    def _eval_settings(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        credentials: FetchCredentials | None,
    ) -> EvalSettings:
        # Auto-sized workers come with a matching per-worker memory
        # limit; the configured limit acts as a ceiling. An explicit
        # worker count keeps the configured limit as-is.
        if self.config.eval_worker_count:
            worker_count = self.config.eval_worker_count
            eval_max_memory = self.config.eval_max_memory_size
        else:
            worker_config = calculate_eval_workers()
            worker_count = worker_config.count
            eval_max_memory = min(
                self.config.eval_max_memory_size, worker_config.max_memory_mib
            )
        # PR-controlled eval can fetch arbitrary flake inputs with the
        # netrc; an instance-wide token (Gitea/GitLab) would let a
        # malicious PR read any private repo on the forge. Only
        # repo-scoped tokens (GitHub) reach PR evals.
        netrc_file = None
        if credentials is not None and (
            event.pr_number is None or credentials.repo_scoped
        ):
            netrc_file = credentials.netrc_file
        return EvalSettings(
            gc_roots_dir=self._gcroots_dir(build),
            timeout=self.config.eval_timeout,
            worker_count=worker_count,
            max_memory_size_mib=eval_max_memory,
            show_trace=self.config.show_trace_on_failure,
            netrc_file=netrc_file,
            # The worktree's .git points into the central clone; the
            # sandboxed evaluator needs to read it.
            extra_ro_paths=[self.repos.clone_path(event.repo.key)],
        )

    async def _settle_aborted(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        status: str,
        *,
        error: str | None = None,
    ) -> None:
        """Terminal bookkeeping and status fan-out for a build that
        ended without a normal aggregation (failure or cancellation)."""
        await self.db.settle_unfinished_attributes(build.id)
        await self.db.set_build_status(build.id, status, error=error)
        if status == BuildStatus.CANCELLED:
            await self.reporter.eval_cancelled(event, build)
        else:
            await self.reporter.eval_finished(event, build, success=False, warnings=[])
        await self.reporter.build_finished(
            event, build, status, build.status_generation, []
        )
        await self._finish_linked(
            build,
            status,
            build.status_generation,
            [],
            eval_success=None if status == BuildStatus.CANCELLED else False,
        )

    @staticmethod
    async def _reap(*tasks: asyncio.Future[Any]) -> None:
        """Cancel and await tasks, swallowing their errors."""
        for task in tasks:
            if not task.done():
                task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task

    async def _run_build_inner(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        worktree_path: Path,
        credentials: FetchCredentials | None,
    ) -> None:
        await self.reporter.build_started(event, build)
        await self.db.set_build_status(build.id, BuildStatus.EVALUATING)

        branch_config = BranchConfig.load(worktree_path)
        eval_settings = self._eval_settings(event, build, credentials)
        # Race the evaluation against the cancel event: a superseded
        # build must not hold the eval slot to completion.
        cancel_event = self.cancel_events.setdefault(build.id, asyncio.Event())

        jobs_queue: asyncio.Queue[list[NixEvalJob] | None] = asyncio.Queue()

        async def record_job_batch(jobs: list[NixEvalJob]) -> None:
            # Pending rows appear in the UI while the eval is running.
            await self.db.record_attributes(
                build.id,
                [
                    job
                    for job in jobs
                    if isinstance(job, NixEvalJobSuccess)
                    and job.system in self.config.build_systems
                ],
            )
            await jobs_queue.put(jobs)

        # Deduplicated warnings appear on the build page while the eval
        # runs; DB writes are throttled since retry storms emit one
        # line per narinfo.
        live_warnings = LiveWarningAggregator()
        last_flush = 0.0

        async def record_stderr_line(line: str) -> None:
            nonlocal last_flush
            if not live_warnings.add(line):
                return
            now = time.monotonic()
            if now - last_flush >= LIVE_WARNINGS_FLUSH_INTERVAL:
                last_flush = now
                await self.db.set_eval_warnings(
                    build.id, json.dumps(live_warnings.snapshot())
                )

        eval_task = asyncio.ensure_future(
            self.eval_runner.run(
                worktree_path,
                branch_config,
                eval_settings,
                on_jobs=record_job_batch,
                on_stderr_line=record_stderr_line,
            )
        )
        # Builds start as soon as the first eval batch arrives.
        build_task = asyncio.create_task(
            self._build_attributes(event, build, worktree_path, jobs_queue)
        )
        cancel_wait = asyncio.ensure_future(cancel_event.wait())
        try:
            try:
                await asyncio.wait(
                    {eval_task, cancel_wait}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                cancel_wait.cancel()
            if cancel_event.is_set() and not eval_task.done():
                await self._reap(eval_task, build_task)
                await self._settle_aborted(event, build, BuildStatus.CANCELLED)
                return
            # EvalError and anything else propagate to run_build,
            # which settles the build as failed. Flush warnings dropped
            # by the throttle either way (run_build settles failures).
            try:
                eval_result = await eval_task
            finally:
                if live_warnings:
                    await self.db.set_eval_warnings(
                        build.id, json.dumps(live_warnings.snapshot())
                    )
            await self.db.set_build_status(build.id, BuildStatus.BUILDING)
            # Idempotent backstop for the streaming inserts above;
            # pending rows are what crash recovery resumes from. The
            # scheduler drops unsupported systems; their pending rows
            # would never turn terminal, so don't record them.
            await self.db.record_attributes(
                build.id,
                [
                    job
                    for job in eval_result.jobs
                    if isinstance(job, NixEvalJobSuccess)
                    and job.system in self.config.build_systems
                ],
            )
            await self.reporter.eval_finished(
                event,
                build,
                success=True,
                warnings=[str(g["message"]) for g in live_warnings.snapshot()],
            )

            # Re-send the complete eval result: the scheduler dedupes
            # by attr, so this only schedules jobs a streamed batch
            # missed (e.g. eval runners without on_jobs support).
            await jobs_queue.put(list(eval_result.jobs))
            await jobs_queue.put(None)
            status = await build_task
        except BaseException:
            # Reap both tasks or the build task leaks forever blocked
            # on the jobs queue and the evaluator process outlives the
            # build (nix_eval kills the evaluator on cancellation).
            await self._reap(eval_task, build_task)
            raise

        if status == BuildStatus.SUCCEEDED:
            await self._maybe_run_effects(event, build, worktree_path, credentials)

    async def _build_attributes(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        worktree_path: Path,
        jobs: Sequence[NixEvalJob] | asyncio.Queue[list[NixEvalJob] | None],
        *,
        cache_failures: bool = True,
    ) -> str:
        """Schedule the attribute builds, persist their results, and
        re-aggregate the build (shared by fresh builds and reruns).
        Accepts either a complete job list or a queue fed during an
        ongoing evaluation. Returns the aggregated build status."""
        cancel_event = self.cancel_events.setdefault(build.id, asyncio.Event())

        async def record_early(result: AttributeResult) -> None:
            """Persist skips and dependency failures as they happen;
            otherwise they stay pending until the whole build ends."""
            await self.db.complete_attribute(build.id, result, if_unfinished=True)

        failed_build_cache: FailedBuildCache | None = (
            self.failed_build_cache(build.project_id)
            if self.failed_build_cache is not None and self.config.cache_failed_builds
            else None
        )
        if failed_build_cache is not None and not cache_failures:
            failed_build_cache = _ReadOnlyFailedBuildCache(failed_build_cache)
        scheduler = JobScheduler(
            _OrchestratorExecutor(self, event, build, worktree_path, cancel_event),
            self.config.build_systems,
            failed_build_cache=failed_build_cache,
            build_url=f"{self.config.url}/repos/{event.repo.forge}/{event.repo.name}/builds/{build.number}",
            on_result=record_early,
        )
        if isinstance(jobs, asyncio.Queue):
            schedule_result = await scheduler.run_incremental(jobs)
        else:
            schedule_result = await scheduler.run(list(jobs))

        # Persist results the executor adapter didn't already write
        # (failed_eval, dependency_failed, cached_failure, skips).
        for result in schedule_result.results:
            await self.db.complete_attribute(build.id, result, if_unfinished=True)

        # Skipped-as-local attributes still get gcroots/outputs
        # updates. A filesystem error here must not skip the final
        # aggregation and status fan-out below.
        post_process_error: str | None = None
        try:
            await self.post_process_skipped(event, schedule_result.skipped_out_paths)
        except Exception as e:
            logger.exception(
                "post-processing skipped attributes failed",
                extra={"build_id": build.id},
            )
            post_process_error = str(e)

        status, generation = await self.db.aggregate_build(build.id)
        if post_process_error is not None:
            status = BuildStatus.FAILED
            await self.db.set_build_status(
                build.id, BuildStatus.FAILED, error=post_process_error
            )
        await self.reporter.build_finished(
            event,
            build,
            status,
            generation,
            schedule_result.results,
            attr_statuses=await self.db.get_attribute_statuses(build.id),
            attr_prefix=BranchConfig.load(worktree_path).attribute,
        )
        await self._finish_linked(
            build, status, generation, schedule_result.results, eval_success=True
        )
        self.cancel_events.pop(build.id, None)

        if status == BuildStatus.SUCCEEDED:
            await self._refresh_schedules(event)
        return status

    async def _refresh_schedules(self, event: ChangeEvent) -> None:
        """Queue `onSchedule` re-discovery after a successful
        default-branch build; the service's scheduled-effects loop only
        sweeps what the executor stores."""
        if event.pr_number is not None or event.branch != event.repo.default_branch:
            return
        await WorkQueue(self.db.pool).enqueue(
            "refresh-schedules",
            f"schedules-{event.repo.id}",
            {"project_id": event.repo.id, "rev": event.commit_sha},
        )

    @asynccontextmanager
    async def rerun_worktree(
        self,
        info: RepoInfo,
        build: BuildRecord,
        prefix: str,
        credentials: FetchCredentials | None,
    ) -> AsyncIterator[tuple[ChangeEvent, Path]]:
        """Event reconstruction plus a fresh worktree at the recorded
        commit; shared by the rerun paths."""
        event = ChangeEvent(
            repo=info,
            branch=build.branch,
            commit_sha=build.commit_sha,
            pr_number=build.pr_number,
        )
        # PR head commits are only reachable via the PR refs.
        refspecs = ["+refs/heads/*:refs/heads/*"]
        if build.pr_number is not None:
            refspecs.append(pr_refspec(info.forge, build.pr_number))
        await self.repos.fetch(info.key, info.clone_url, refspecs, credentials)
        worktree = await self.repos.checkout_for_build(
            info.key,
            f"{prefix}-{build.id}",
            base_commit=build.commit_sha,
        )
        try:
            yield event, worktree.path
        finally:
            await self.repos.remove_worktree(worktree)

    async def rerun_pending_attributes(
        self,
        info: RepoInfo,
        build: BuildRecord,
        pending_jobs: list[NixEvalJobSuccess],
        credentials: FetchCredentials | None = None,
    ) -> None:
        """Re-run only the pending attributes of an existing build using
        the stored eval results — no re-evaluation (attribute restarts
        and crash recovery)."""
        if build.id in self.cancel_events:
            # Already running; a concurrent rerun would double-write
            # attribute completions.
            return
        # Claim the slot before the first await; concurrent reruns
        # must not pass the guard together.
        cancel_event = self.cancel_events[build.id] = asyncio.Event()
        try:
            current = await self.db.get_build(build.id)
            if current is not None and current.status == "cancelled":
                # Cancelled between scheduling the rerun and getting here.
                return
            # Pending rows for systems no longer in build_systems would
            # stay non-terminal forever: the scheduler drops their jobs.
            # Drop the rows too (same as never recording them).
            unsupported = [
                job
                for job in pending_jobs
                if job.system not in self.config.build_systems
            ]
            if unsupported:
                await self.db.pool.execute(
                    "DELETE FROM build_attributes WHERE build_id = $1 "
                    "AND attr = ANY($2::text[])",
                    build.id,
                    [job.attr for job in unsupported],
                )
                pending_jobs = [
                    job
                    for job in pending_jobs
                    if job.system in self.config.build_systems
                ]
            # No re-eval happens on this path; go straight to building.
            await self.db.set_build_status(build.id, BuildStatus.BUILDING)
            # Register so supersede/PR-close cancellation also covers
            # recovered and restarted builds.
            self.canceller.register(
                info.id,
                branch_key(build.branch, build.pr_number),
                build.id,
                build.tree_hash or "",
                build.commit_sha,
                cancel_event,
            )
            async with self.rerun_worktree(info, build, "rerun", credentials) as (
                event,
                worktree_path,
            ):
                # cache_failures=False: see _ReadOnlyFailedBuildCache.
                status = await self._build_attributes(
                    event, build, worktree_path, pending_jobs, cache_failures=False
                )
                if status == BuildStatus.SUCCEEDED:
                    # Crash recovery before effects started; the
                    # started-flag keeps already-deployed builds from
                    # re-deploying.
                    await self._maybe_run_effects(
                        event, build, worktree_path, credentials
                    )
                    await self._refresh_schedules(event)
        finally:
            self.canceller.complete(build.id)
            self.cancel_events.pop(build.id, None)

    async def rerun_effects(
        self,
        info: RepoInfo,
        build: BuildRecord,
        credentials: FetchCredentials | None = None,
    ) -> None:
        """Effects-only restart: fresh worktree at the recorded commit,
        attributes untouched."""
        if build.id in self.cancel_events:
            # A concurrent rerun (or double click) would deploy twice.
            return
        self.cancel_events[build.id] = asyncio.Event()
        try:
            # Reset under the claim: resetting earlier (e.g. in the
            # service) could clobber a rerun already in flight.
            await self.db.pool.execute(
                "UPDATE builds SET effects_started = FALSE WHERE id = $1", build.id
            )
            await self.db.pool.execute(
                "UPDATE build_effects SET status = 'pending', error = NULL, "
                "finished_at = NULL, log_path = NULL, log_size = 0, "
                "log_truncated = FALSE WHERE build_id = $1",
                build.id,
            )
            async with self.rerun_worktree(info, build, "effects", credentials) as (
                event,
                worktree_path,
            ):
                await self._maybe_run_effects(event, build, worktree_path, credentials)
                await self._refresh_schedules(event)
            # The enqueued effect items share this build's key and only
            # become claimable once this item finishes.
        finally:
            self.cancel_events.pop(build.id, None)

    async def _maybe_run_effects(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        worktree_path: Path,
        credentials: FetchCredentials | None = None,
    ) -> None:
        repo = event.repo
        # Gating config comes from the default branch of the central
        # clone: the worktree is PR-controlled, so its nixbot.toml
        # could grant the PR effects (and deploy secrets).
        # refs/heads/ prefix: a bare branch name would resolve a tag
        # of the same name first (tags auto-follow into the clone).
        config_text = None
        for filename in CONFIG_FILENAMES:
            config_text = await self.repos.show_file(
                repo.key, f"refs/heads/{repo.default_branch}", filename
            )
            if config_text is not None:
                break
        default_branch_config = BranchConfig.loads(config_text)
        if not should_run_effects(
            default_branch_config,
            repo.default_branch,
            event.branch,
            is_pull_request=event.pr_number is not None,
        ):
            return
        # The started-flag guards against auto-re-running effects on
        # crash recovery (deploys are not idempotent).
        if not await self.db.mark_effects_started(build.id):
            return
        task_token = self.task_tokens.issue(build.project_id)
        ctx = effects_context(
            self.config,
            repo,
            worktree_path=worktree_path,
            rev=event.commit_sha,
            branch=event.branch,
            git_token=credentials.token if credentials is not None else None,
            task_token=task_token,
        )
        try:
            names = await list_effects(ctx)
        except (EffectsError, OSError):
            # OSError: nixbot-effects not installed; effects are
            # best-effort and must not fail the (already reported) build.
            logger.exception("effects discovery failed", extra={"build_id": build.id})
            return
        finally:
            self.task_tokens.revoke(task_token)
        # Effects removed from the flake since the last run would
        # otherwise linger as stale pending rows.
        await self.db.pool.execute(
            "DELETE FROM build_effects WHERE build_id = $1 "
            "AND NOT (name = ANY($2::text[]))",
            build.id,
            names,
        )
        await self._enqueue_effects(build, names)

    async def _enqueue_effects(self, build: BuildRecord, names: list[str]) -> None:
        """One queue item per effect, on the build's dedup key."""
        queue = WorkQueue(self.db.pool)
        for name in names:
            await self.db.start_effect(build.id, name, status="pending")
            await queue.enqueue(
                "effect", f"build-{build.id}", {"build_id": build.id, "name": name}
            )

    async def run_effect_item(
        self,
        info: RepoInfo,
        build: BuildRecord,
        name: str,
        credentials: FetchCredentials | None = None,
    ) -> None:
        """Dispatcher entry for one queued effect."""
        row = await self.db.pool.fetchval(
            "SELECT status FROM build_effects WHERE build_id = $1 AND name = $2",
            build.id,
            name,
        )
        if row != "pending":
            # Swept after a crash mid-run, or already terminal; started
            # effects never auto-re-run (deploys are not idempotent).
            return
        async with self.rerun_worktree(info, build, "effect", credentials) as (
            event,
            worktree_path,
        ):
            task_token = self.task_tokens.issue(build.project_id)
            try:
                ctx = effects_context(
                    self.config,
                    info,
                    worktree_path=worktree_path,
                    rev=event.commit_sha,
                    branch=event.branch,
                    git_token=credentials.token if credentials is not None else None,
                    task_token=task_token,
                )
                await self._run_one_effect(ctx, build, name)
            finally:
                self.task_tokens.revoke(task_token)

    @asynccontextmanager
    async def open_log(
        self, build_id: int, key: str, filename: str
    ) -> AsyncIterator[LogWriter]:
        """LogWriter registered for live streaming; closed and
        unregistered on exit. Shared by attribute and effect runs."""
        log_path = self._log_dir(build_id) / filename
        writer = LogWriter(path=log_path, size_limit=self.config.log_size_limit)
        self.log_registry.register(build_id, key, writer)
        try:
            yield writer
        finally:
            await writer.close()
            self.log_registry.unregister(build_id, key)

    async def _run_one_effect(
        self, ctx: EffectsContext, build: BuildRecord, name: str
    ) -> None:
        """One effect with its own row and log."""
        await self.db.start_effect(build.id, name)
        # Effect names come from untrusted flakes; percent-encode so
        # the log file cannot escape the log directory. The "effects/"
        # subdirectory keeps them apart from attribute logs (a flat
        # prefix would collide with an attribute named "effect-X").
        async with self.open_log(
            build.id, f"effect:{name}", f"effects/{quote(name, safe='')}.zst"
        ) as writer:
            try:
                success = await run_effect(ctx, name, writer.write)
            except Exception as e:
                # Any escape would leave the row running forever
                # (nothing re-runs effects) and kill the loop for the
                # remaining effects.
                logger.exception(
                    "effect crashed",
                    extra={"build_id": build.id, "effect": name},
                )
                await writer.write(f"\n{e}\n".encode())
                success = False
        error = None
        if not success:
            logger.error("effect failed", extra={"build_id": build.id, "effect": name})
            error = failure_excerpt(writer.tail_lines()) or None
        await self.db.finish_effect(
            build.id,
            name,
            success=success,
            error=error,
            log_path=str(writer.path.relative_to(self.config.state_dir)),
            log_size=writer.bytes_seen,
            log_truncated=writer.truncated,
        )

    async def _attach_linked_event(
        self, event: ChangeEvent, build: BuildRecord
    ) -> None:
        """In-flight (or recovering) build shared with another context:
        attach for the final status fan-out."""
        self.linked_events.setdefault(build.id, []).append(event)
        await self.reporter.build_started(event, build)
        # The build may have turned terminal between the record fetch
        # and the attach: the final fan-out already happened and would
        # never cover this event. Replay the final status instead.
        current = await self.db.get_build(build.id)
        if current is not None and current.status in BuildStatus.TERMINAL:
            with contextlib.suppress(KeyError, ValueError):
                self.linked_events[build.id].remove(event)
            await self._replay_terminal_status(event, current)

    async def _replay_terminal_status(
        self, event: ChangeEvent, build: BuildRecord
    ) -> None:
        """Re-post the final eval and build statuses of an already
        terminal build for a new context; without this the context's
        nix-eval/nix-build checks stay pending forever. A succeeded
        build with zero attributes is a genuine empty-but-green eval,
        not an eval failure."""
        if build.status == BuildStatus.CANCELLED:
            await self.reporter.eval_cancelled(event, build)
        else:
            eval_success = build.status == BuildStatus.SUCCEEDED or bool(
                await self.db.get_attribute_statuses(build.id)
            )
            await self.reporter.eval_finished(
                event, build, success=eval_success, warnings=[]
            )
        await self.reporter.build_finished(
            event, build, build.status, build.status_generation, []
        )

    async def _finish_linked(
        self,
        build: BuildRecord,
        status: str,
        generation: int,
        results: list[AttributeResult],
        *,
        eval_success: bool | None = None,
    ) -> None:
        """Final status fan-out for second contexts attached to this
        build; eval_success is None when no eval result exists."""
        for linked in self.linked_events.pop(build.id, []):
            if eval_success is not None:
                await self.reporter.eval_finished(
                    linked, build, success=eval_success, warnings=[]
                )
            elif status == BuildStatus.CANCELLED:
                # Cancel during eval: the linked contexts' nix-eval
                # status would otherwise stay pending forever.
                await self.reporter.eval_cancelled(linked, build)
            await self.reporter.build_finished(
                linked, build, status, generation, results
            )

    async def _is_ancestor(
        self, project_key: str, ancestor: str, descendant: str
    ) -> bool:
        try:
            await run_git(
                ["merge-base", "--is-ancestor", ancestor, descendant],
                cwd=self.repos.clone_path(project_key),
            )
        except GitError:
            return False
        return True

    async def _reuse_terminal_build(  # noqa: PLR0913
        self,
        event: ChangeEvent,
        build: BuildRecord,
        key: str,
        tree_hash: str,
        *,
        worktree_path: Path,
        credentials: FetchCredentials | None,
        incoming_stale: bool,
    ) -> None:
        """Same content already built in another context: only report
        the existing result for this context. Still register so an
        in-flight build of this context's previous content is
        superseded, and a push reusing a PR build gets its
        gcroots/outputs updates."""
        logger.info(
            "reusing build for tree hash",
            extra={"build_id": build.id, "tree_hash": tree_hash},
        )
        outcome = self.canceller.register(
            event.repo.id,
            key,
            build.id,
            tree_hash,
            event.commit_sha,
            asyncio.Event(),
            incoming_is_ancestor_of_running=incoming_stale,
        )
        if outcome == RegisterOutcome.STALE:
            # Redelivered out-of-order event: superseding the in-flight
            # newer build with this old result would cancel it.
            return
        self.canceller.complete(build.id)
        if build.status == BuildStatus.SUCCEEDED:
            # Guarded like in-build post-processing: a gcroots/outputs
            # failure must not strand this context without a status.
            try:
                await self._post_process_existing(event, build)
                # A build that ran as a PR never started effects, so a
                # default-branch push reusing it must still deploy; the
                # effects_started flag prevents re-deploys.
                await self._maybe_run_effects(event, build, worktree_path, credentials)
                await self._refresh_schedules(event)
            except Exception:
                logger.exception(
                    "post-processing reused build failed",
                    extra={"build_id": build.id},
                )
        await self._replay_terminal_status(event, build)

    async def _post_process_existing(
        self, event: ChangeEvent, build: BuildRecord
    ) -> None:
        """Gcroots/outputs updates for a context reusing an already
        succeeded build (e.g. default-branch push reusing a PR build)."""
        rows = await self.db.pool.fetch(
            "SELECT attr, outputs FROM build_attributes "
            "WHERE build_id = $1 AND status IN ('succeeded', 'skipped_local')",
            build.id,
        )
        pairs = []
        for row in rows:
            out = (json.loads(row["outputs"]) if row["outputs"] else {}).get("out")
            if out:
                pairs.append((row["attr"], out))
        await self.post_process_skipped(event, pairs)

    async def post_process_skipped(
        self, event: ChangeEvent, skipped: list[tuple[str, str]]
    ) -> None:
        branches = self.config.branches
        repo = event.repo
        if event.pr_number is not None:
            return  # push events only, matching current behavior
        for attr, out_path in skipped:
            if not out_path:
                continue
            # Forge-scoped paths: the same owner/repo on two forges
            # must not share gc-roots or outputs files.
            if branches.do_register_gcroot(repo.default_branch, event.branch):
                await self.register_gcroot(
                    self.config.gcroots_dir, repo.key, attr, out_path
                )
            if self.config.outputs_path is not None and branches.do_update_outputs(
                repo.default_branch, event.branch
            ):
                self.write_output_path(
                    self.config.outputs_path,
                    repo.forge,
                    repo.owner,
                    repo.repo,
                    event.branch,
                    attr,
                    out_path,
                )


class _ReadOnlyFailedBuildCache:
    """Failed-build cache that skips known failures but records none.

    Recovery/restart reruns rebuild jobs from DB rows without dependency
    closures, so dependents of one broken drv fail with their own build
    error; recording those would poison the cache."""

    def __init__(self, inner: FailedBuildCache) -> None:
        self._inner = inner

    async def check(self, drv_path: str) -> CachedFailure | None:
        return await self._inner.check(drv_path)

    async def add(self, drv_path: str, url: str) -> None:
        pass


class _OrchestratorExecutor:
    """Scheduler executor adapter: runs the build, then post-build
    steps, gcroots, outputs, and writes the attribute completion as one
    transactional write."""

    def __init__(
        self,
        orchestrator: Orchestrator,
        event: ChangeEvent,
        build: BuildRecord,
        worktree_path: Path,
        cancel_event: asyncio.Event,
    ) -> None:
        self.o = orchestrator
        self.event = event
        self.build_record = build
        self.worktree_path = worktree_path
        self.cancel_event = cancel_event

    async def build(self, job: NixEvalJobSuccess) -> BuildOutcome:
        try:
            return await self._build_inner(job)
        except Exception:
            logger.exception(
                "unexpected error building attribute",
                extra={"build_id": self.build_record.id, "attr": job.attr},
            )
            result = AttributeResult(
                attr=job.attr,
                status=AttributeStatus.failed,
                job=job,
                error="internal error, see service logs",
                drv_path=job.drv_path,
                system=job.system,
            )
            await self.o.db.complete_attribute(self.build_record.id, result)
            # Internal errors are not derivation failures: don't cache.
            return BuildOutcome.failure_no_cache

    async def _build_inner(self, job: NixEvalJobSuccess) -> BuildOutcome:
        if not await self.o.db.mark_attribute_building(
            self.build_record.id, job.attr, job.system, job.drv_path
        ):
            # Cancelled externally while waiting on dependencies: do
            # not resurrect the row by building it anyway.
            return BuildOutcome.cancelled
        # Per-attribute cancellation: the executor watches one event, so
        # mirror the build-level cancel into the attribute's own event.
        attr_cancel = asyncio.Event()
        self.o.attr_cancel_events[(self.build_record.id, job.attr)] = attr_cancel

        async def _mirror_build_cancel() -> None:
            await self.cancel_event.wait()
            attr_cancel.set()

        mirror = asyncio.create_task(_mirror_build_cancel())
        # Attribute names come from untrusted flakes; percent-encode
        # so the log file cannot escape the log directory.
        try:
            async with self.o.open_log(
                self.build_record.id, job.attr, f"{quote(job.attr, safe='')}.zst"
            ) as writer:
                outcome = await self.o.executor.build_attribute(
                    self.build_record.id,
                    job,
                    writer,
                    self.worktree_path,
                    attr_cancel,
                )
                if outcome == BuildOutcome.success and self.o.config.post_build_steps:
                    props = build_props(self.event, job)
                    step_results = await run_post_build_steps(
                        self.o.config.post_build_steps, props, self.worktree_path
                    )
                    for step in step_results:
                        await writer.write(
                            f"\npost-build step {step.name}: "
                            f"{'ok' if step.success else 'failed'}\n".encode()
                        )
                        await writer.write(step.output.encode())
                    if any(step.failed for step in step_results):
                        # The derivation built: fail the attribute without
                        # poisoning the failed-build cache.
                        outcome = BuildOutcome.post_build_failure
        finally:
            mirror.cancel()
            self.o.attr_cancel_events.pop((self.build_record.id, job.attr), None)

        status = {
            BuildOutcome.success: AttributeStatus.succeeded,
            BuildOutcome.failure: AttributeStatus.failed,
            BuildOutcome.failure_no_cache: AttributeStatus.failed,
            BuildOutcome.post_build_failure: AttributeStatus.failed,
            BuildOutcome.cancelled: AttributeStatus.cancelled,
        }[outcome]
        # Failed attributes carry a log-tail excerpt so the build page
        # answers "why" without a click into the log. ANSI stays: the
        # web layer renders it, the API strips it.
        error = None
        if status == AttributeStatus.failed:
            error = failure_excerpt(writer.tail_lines()) or None
        result = AttributeResult(
            attr=job.attr,
            status=status,
            job=job,
            error=error,
            out_path=job.outputs.get("out"),
            drv_path=job.drv_path,
            system=job.system,
        )
        await self.o.db.complete_attribute(
            self.build_record.id,
            result,
            log_path=str(writer.path.relative_to(self.o.config.state_dir)),
            log_size=writer.bytes_seen,
            log_truncated=writer.truncated,
        )
        if outcome == BuildOutcome.success:
            try:
                await self.o.post_process_skipped(
                    self.event, [(job.attr, job.outputs.get("out") or "")]
                )
            except Exception:
                # Must not overwrite the recorded success or poison
                # the failed-build cache.
                logger.exception(
                    "post-processing failed",
                    extra={"build_id": self.build_record.id, "attr": job.attr},
                )
        return outcome
