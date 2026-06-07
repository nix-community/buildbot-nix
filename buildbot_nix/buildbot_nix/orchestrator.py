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
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
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
    list_effects,
    resolve_effects_secret,
    run_effect,
    should_run_effects,
)
from .events import ChangeEvent, NullStatusReporter, RepoInfo, StatusReporter
from .executor import LogWriter
from .gitrepo import GitError, MergeConflictError, run_git
from .memory import calculate_eval_workers
from .models import NixEvalJobSuccess
from .nix_eval import EvalError, EvalSettings
from .post_build import build_props, run_post_build_steps
from .repo_config import BranchConfig
from .scheduled import ScheduledEffectsStore, discover_schedules
from .scheduler import (
    AttributeResult,
    AttributeStatus,
    BuildOutcome,
    JobScheduler,
)
from .web.logs import LogRegistry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from pathlib import Path

    from .config import EngineConfig
    from .db import BuildDB, BuildRecord
    from .executor import NixBuildExecutor
    from .gitrepo import FetchCredentials, RepoManager
    from .models import NixEvalJob
    from .nix_eval import EvalRunner
    from .scheduler import FailedBuildCache

    GcrootRegistrar = Callable[[Path, str, str, str], Awaitable[None]]
    OutputWriter = Callable[[Path, str, str, str, str, str], Path]

logger = logging.getLogger(__name__)


@dataclass
class Orchestrator:
    config: EngineConfig
    db: BuildDB
    repos: RepoManager
    eval_runner: EvalRunner
    executor: NixBuildExecutor
    reporter: StatusReporter = field(default_factory=NullStatusReporter)
    failed_build_cache: FailedBuildCache | None = None
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
    # Second contexts attached to an in-flight build, for status fan-out.
    linked_events: dict[int, list[ChangeEvent]] = field(default_factory=dict)

    def _log_dir(self, build: BuildRecord) -> Path:
        return self.config.state_dir / "logs" / str(build.id)

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
        # fetching all of refs/pull/* is unbounded on PR-heavy repos.
        refspecs = ["+refs/heads/*:refs/heads/*"]
        if event.pr_number is not None:
            refspecs.append(
                f"+refs/pull/{event.pr_number}/*:refs/pull/{event.pr_number}/*"
            )
        await self.repos.fetch(repo.key, repo.clone_url, refspecs, credentials)
        try:
            # Unique token: concurrent events for the same commit must
            # not share (and destroy) one checkout.
            worktree = await self.repos.checkout_for_build(
                repo.key,
                f"{repo.id}-{event.commit_sha[:12]}-{uuid.uuid4().hex[:8]}",
                base_commit=event.base_sha or event.commit_sha,
                head_commit=event.commit_sha if event.base_sha else None,
                credentials=credentials,
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
        if not created and build.status in (
            BuildStatus.SUCCEEDED,
            BuildStatus.FAILED,
        ):
            await self._reuse_terminal_build(event, build, key, tree_hash)
            return

        # Out-of-order delivery check: an event whose commit is an
        # ancestor of the context's running build is stale.
        incoming_stale = False
        running_commit = self.canceller.running_commit_for(repo.id, key)
        if running_commit is not None and running_commit != event.commit_sha:
            incoming_stale = await self._is_ancestor(
                repo.key, event.commit_sha, running_commit
            )

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
            # In-flight (or recovering) build shared with another
            # context: attach for the final status fan-out.
            self.linked_events.setdefault(build.id, []).append(event)
            await self.reporter.build_started(event, build)
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
        finally:
            # Eval gc-roots only need to outlive the build; without
            # cleanup the nix store grows unboundedly.
            shutil.rmtree(self._gcroots_dir(build), ignore_errors=True)

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
        eval_settings = EvalSettings(
            gc_roots_dir=self._gcroots_dir(build),
            worker_count=self.config.eval_worker_count
            or calculate_eval_workers().count,
            max_memory_size_mib=self.config.eval_max_memory_size,
            show_trace=self.config.show_trace_on_failure,
            netrc_file=credentials.netrc_file if credentials is not None else None,
            # The worktree's .git points into the central clone; the
            # sandboxed evaluator needs to read it.
            extra_ro_paths=[self.repos.clone_path(event.repo.key)],
        )
        # Race the evaluation against the cancel event: a superseded
        # build must not hold the eval slot to completion.
        cancel_event = self.cancel_events.setdefault(build.id, asyncio.Event())
        eval_task = asyncio.ensure_future(
            self.eval_runner.run(worktree_path, branch_config, eval_settings)
        )
        cancel_wait = asyncio.ensure_future(cancel_event.wait())
        try:
            await asyncio.wait(
                {eval_task, cancel_wait}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            cancel_wait.cancel()
        if cancel_event.is_set() and not eval_task.done():
            eval_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, EvalError):
                await eval_task
            await self.db.set_build_status(build.id, BuildStatus.CANCELLED)
            await self.reporter.build_finished(
                event, build, BuildStatus.CANCELLED, build.status_generation, []
            )
            await self._finish_linked(
                build, BuildStatus.CANCELLED, build.status_generation, []
            )
            return
        try:
            eval_result = await eval_task
        except EvalError as e:
            logger.warning(
                "evaluation failed",
                extra={"build_id": build.id, "error": str(e)},
            )
            await self.db.set_build_status(build.id, BuildStatus.FAILED, error=str(e))
            await self.reporter.eval_finished(event, build, success=False, warnings=[])
            await self.reporter.build_finished(
                event, build, BuildStatus.FAILED, build.status_generation, []
            )
            await self._finish_linked(
                build,
                BuildStatus.FAILED,
                build.status_generation,
                [],
                eval_success=False,
            )
            return

        await self.db.set_build_status(
            build.id,
            BuildStatus.BUILDING,
            eval_warnings=(
                json.dumps(eval_result.warnings) if eval_result.warnings else None
            ),
        )
        # Pending rows are what crash recovery resumes from. The
        # scheduler drops unsupported systems; their pending rows would
        # never turn terminal, so don't record them.
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
            event, build, success=True, warnings=eval_result.warnings
        )

        status = await self._build_attributes(
            event, build, worktree_path, eval_result.jobs
        )
        if status == BuildStatus.SUCCEEDED:
            await self._maybe_run_effects(event, build, worktree_path, branch_config)

    async def _build_attributes(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        worktree_path: Path,
        jobs: Sequence[NixEvalJob],
    ) -> str:
        """Schedule the attribute builds, persist their results, and
        re-aggregate the build (shared by fresh builds and reruns).
        Returns the aggregated build status."""
        cancel_event = self.cancel_events.setdefault(build.id, asyncio.Event())
        scheduler = JobScheduler(
            _OrchestratorExecutor(self, event, build, worktree_path, cancel_event),
            self.config.build_systems,
            failed_build_cache=(
                self.failed_build_cache if self.config.cache_failed_builds else None
            ),
            build_url=f"{self.config.url}/repos/{event.repo.forge}/{event.repo.name}/builds/{build.number}",
        )
        schedule_result = await scheduler.run(list(jobs))

        # Persist results the executor adapter didn't already write
        # (failed_eval, dependency_failed, cached_failure, skips).
        recorded = await self.db.get_attribute_statuses(build.id)
        for result in schedule_result.results:
            if recorded.get(result.attr) in (None, "pending", "building"):
                await self.db.complete_attribute(build.id, result)

        # Skipped-as-local attributes still get gcroots/outputs updates.
        await self._post_process_skipped(event, schedule_result.skipped_out_paths)

        status, generation = await self.db.aggregate_build(build.id)
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

        if (
            status == BuildStatus.SUCCEEDED
            and event.pr_number is None
            and event.branch == event.repo.default_branch
        ):
            await self._refresh_schedules(event, worktree_path)
        return status

    async def _refresh_schedules(self, event: ChangeEvent, worktree_path: Path) -> None:
        """Persist `onSchedule` definitions after a successful
        default-branch build; the service's scheduled-effects loop only
        sweeps what is stored here."""
        try:
            ctx = EffectsContext(
                worktree_path=worktree_path,
                rev=event.commit_sha,
                branch=event.branch,
                repo=event.repo.name,
                extra_sandbox_paths=self.config.effects_extra_sandbox_paths,
            )
            schedules = await discover_schedules(ctx)
            await ScheduledEffectsStore(self.db.pool).replace_schedules(
                event.repo.id, schedules
            )
        except Exception:
            logger.exception(
                "schedule discovery failed",
                extra={"project": event.repo.name},
            )

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
            # No re-eval happens on this path; go straight to building.
            await self.db.set_build_status(build.id, BuildStatus.BUILDING)
            event = ChangeEvent(
                repo=info,
                branch=build.branch,
                commit_sha=build.commit_sha,
                pr_number=build.pr_number,
            )
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
            # PR head commits are only reachable via refs/pull/*.
            refspecs = ["+refs/heads/*:refs/heads/*"]
            if build.pr_number is not None:
                refspecs.append(
                    f"+refs/pull/{build.pr_number}/*:refs/pull/{build.pr_number}/*"
                )
            await self.repos.fetch(info.key, info.clone_url, refspecs, credentials)
            worktree = await self.repos.checkout_for_build(
                info.key,
                f"rerun-{build.id}",
                base_commit=build.commit_sha,
                credentials=credentials,
            )
            try:
                await self._build_attributes(event, build, worktree.path, pending_jobs)
            finally:
                await self.repos.remove_worktree(worktree)
        finally:
            self.canceller.complete(build.id)
            self.cancel_events.pop(build.id, None)

    async def _maybe_run_effects(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        worktree_path: Path,
        branch_config: BranchConfig,
    ) -> None:
        repo = event.repo
        if not should_run_effects(
            branch_config,
            repo.default_branch,
            event.branch,
            is_pull_request=event.pr_number is not None,
        ):
            return
        # The started-flag guards against auto-re-running effects on
        # crash recovery (deploys are not idempotent).
        if not await self.db.mark_effects_started(build.id):
            return
        ctx = EffectsContext(
            worktree_path=worktree_path,
            rev=event.commit_sha,
            branch=event.branch,
            repo=repo.name,
            secret_name=resolve_effects_secret(
                self.config.effects_per_repo_secrets,
                repo.forge,
                repo.owner,
                repo.repo,
            ),
            extra_sandbox_paths=self.config.effects_extra_sandbox_paths,
        )
        try:
            for name in await list_effects(ctx):
                if not await run_effect(ctx, name):
                    logger.error(
                        "effect failed",
                        extra={"build_id": build.id, "effect": name},
                    )
        except (EffectsError, OSError):
            # OSError: buildbot-effects not installed; effects are
            # best-effort and must not fail the (already reported) build.
            logger.exception("effects execution failed", extra={"build_id": build.id})

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

    async def _reuse_terminal_build(
        self, event: ChangeEvent, build: BuildRecord, key: str, tree_hash: str
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
        self.canceller.register(
            event.repo.id,
            key,
            build.id,
            tree_hash,
            event.commit_sha,
            asyncio.Event(),
        )
        self.canceller.complete(build.id)
        if build.status == BuildStatus.SUCCEEDED:
            await self._post_process_existing(event, build)
        # Post the eval context too, or a required nix-eval check on
        # this commit stays "Expected" forever.
        has_attrs = bool(await self.db.get_attribute_statuses(build.id))
        await self.reporter.eval_finished(event, build, success=has_attrs, warnings=[])
        await self.reporter.build_finished(
            event, build, build.status, build.status_generation, []
        )

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
        await self._post_process_skipped(event, pairs)

    async def _post_process_skipped(
        self, event: ChangeEvent, skipped: list[tuple[str, str]]
    ) -> None:
        branches = self.config.branches
        repo = event.repo
        if event.pr_number is not None:
            return  # push events only, matching current behavior
        for attr, out_path in skipped:
            if not out_path:
                continue
            if branches.do_register_gcroot(repo.default_branch, event.branch):
                await self.register_gcroot(
                    self.config.gcroots_dir, repo.name, attr, out_path
                )
            if self.config.outputs_path is not None and branches.do_update_outputs(
                repo.default_branch, event.branch
            ):
                self.write_output_path(
                    self.config.outputs_path,
                    repo.owner,
                    repo.repo,
                    event.branch,
                    attr,
                    out_path,
                )


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
                drv_path=job.drvPath,
                system=job.system,
            )
            await self.o.db.complete_attribute(self.build_record.id, result)
            # Internal errors are not derivation failures: don't cache.
            return BuildOutcome.failure_no_cache

    async def _build_inner(self, job: NixEvalJobSuccess) -> BuildOutcome:
        log_dir = self.o._log_dir(self.build_record)  # noqa: SLF001
        # Attribute names come from untrusted flakes; percent-encode
        # so the log file cannot escape the log directory.
        log_path = log_dir / f"{quote(job.attr, safe='')}.zst"
        writer = LogWriter(path=log_path, size_limit=self.o.config.log_size_limit)
        self.o.log_registry.register(self.build_record.id, job.attr, writer)
        await self.o.db.mark_attribute_building(
            self.build_record.id, job.attr, job.system, job.drvPath
        )
        # Per-attribute cancellation: the executor watches one event, so
        # mirror the build-level cancel into the attribute's own event.
        attr_cancel = asyncio.Event()
        self.o.attr_cancel_events[(self.build_record.id, job.attr)] = attr_cancel

        async def _mirror_build_cancel() -> None:
            await self.cancel_event.wait()
            attr_cancel.set()

        mirror = asyncio.create_task(_mirror_build_cancel())
        try:
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
            await writer.close()
            self.o.log_registry.unregister(self.build_record.id, job.attr)

        status = {
            BuildOutcome.success: AttributeStatus.succeeded,
            BuildOutcome.failure: AttributeStatus.failed,
            BuildOutcome.failure_no_cache: AttributeStatus.failed,
            BuildOutcome.post_build_failure: AttributeStatus.failed,
            BuildOutcome.cancelled: AttributeStatus.cancelled,
        }[outcome]
        result = AttributeResult(
            attr=job.attr,
            status=status,
            job=job,
            out_path=job.outputs.get("out"),
            drv_path=job.drvPath,
            system=job.system,
        )
        await self.o.db.complete_attribute(
            self.build_record.id,
            result,
            log_path=str(log_path.relative_to(self.o.config.state_dir)),
            log_size=writer.bytes_seen,
            log_truncated=writer.truncated,
        )
        if outcome == BuildOutcome.success:
            try:
                await self.o._post_process_skipped(  # noqa: SLF001
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
