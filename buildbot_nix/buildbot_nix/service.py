"""Service composition: wires every component into
one running process — database, orchestrator, forge clients, webhook
ingestion, web frontend, pollers, and background maintenance loops.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import re
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .config import ScheduleWhen
from .db import BuildStatus
from .effects import EffectsContext, effects_context
from .events import ChangeEvent, RepoInfo, StatusReporter
from .executor import LogWriter
from .forge import (
    GiteaClient,
    GitHubAppClient,
    GitlabClient,
    filter_repos,
)
from .gitea_hooks import register_repo_hook
from .gitlab_hooks import register_repo_hook as register_gitlab_repo_hook
from .gitrepo import (
    CredentialsProvider,
    FetchCredentials,
    StaticCredentialsProvider,
)
from .hook_secrets import WebhookSecrets
from .orchestrator import pr_refspec
from .reconcile import gitea_heads, github_heads, gitlab_heads, reconcile_repo
from .recovery import (
    check_store_paths,
    cleanup_old_builds,
    cleanup_orphan_log_dirs,
    fail_interrupted_effects,
    find_unfinished_builds,
    settle_already_built,
)
from .scheduled import (
    DueEffect,
    ScheduledEffectsStore,
    discover_schedules,
    run_scheduled_effect,
)
from .webhooks import (
    ChangeRequest,
    PrClosed,
    WebhookEvent,
    should_build_branch,
)
from .work_queue import WorkItem, WorkQueue

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    import asyncpg

    from .config import Config
    from .db import BuildRecord
    from .orchestrator import Orchestrator
    from .polling import PolledRepository
    from .recovery import ResumableBuild
    from .repos import RepoRecord, RepoStore
    from .scheduler import AttributeResult

logger = logging.getLogger(__name__)

_STATIC_CREDENTIALS = StaticCredentialsProvider()

# Repo metadata rarely changes; the UI refresh button covers the
# "I just created a repo" case without waiting for the next tick.
DISCOVERY_INTERVAL = 60 * 60
REFRESH_COOLDOWN = 60
MAINTENANCE_INTERVAL = 60 * 60


def scheduled_worktree_id(due: DueEffect, run_id: int) -> str:
    """Per-run worktree id: concurrent effects (or a run outlasting the
    next due fire) must not share a checkout. Schedule/effect names are
    repo-controlled, so sanitize against path traversal."""
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", f"{due.schedule_name}-{due.effect}")
    return f"scheduled-{due.project_id}-{safe}-{run_id}"


class PullBasedCredentialsProvider:
    """Per-repo SSH credentials for pull-based repositories."""

    def __init__(self, repos: list[PolledRepository]) -> None:
        self._by_url = {repo.url: repo for repo in repos}

    async def get(self, repo_url: str) -> FetchCredentials:
        repo = self._by_url.get(repo_url)
        if repo is None:
            return FetchCredentials()
        return FetchCredentials(
            ssh_private_key_file=repo.ssh_private_key_file,
            ssh_known_hosts_file=repo.ssh_known_hosts_file,
        )


def repo_info(record: RepoRecord) -> RepoInfo:
    return RepoInfo(
        id=record.id,
        key=f"{record.forge}/{record.owner}/{record.name}",
        name=f"{record.owner}/{record.name}",
        owner=record.owner,
        repo=record.name,
        forge=record.forge,
        clone_url=record.url,
        default_branch=record.default_branch,
    )


MAX_REPORT_ATTEMPTS = 5
REPORT_BACKOFF_SECONDS = 30
MAX_REPORT_DELAY_SECONDS = 3600


def _report_payload(build_id: int, attempt: int, error: Exception) -> dict[str, Any]:
    payload: dict[str, Any] = {"build_id": build_id, "attempt": attempt}
    # Forges send Retry-After on rate limits; honoring it is required
    # (e.g. GitHub secondary limits escalate when ignored).
    retry_after = getattr(error, "retry_after", None)
    if retry_after is not None:
        payload["retry_at"] = time.time() + min(retry_after, MAX_REPORT_DELAY_SECONDS)
    return payload


def _report_delay(attempt: int, retry_at: float | None) -> float:
    backoff = min(REPORT_BACKOFF_SECONDS * (attempt - 1), 300)
    hinted = max(0.0, retry_at - time.time()) if retry_at is not None else 0.0
    return min(max(backoff, hinted), MAX_REPORT_DELAY_SECONDS)


@dataclass
class RetryingReporter:
    """Wraps the forge reporter: a failed terminal status post becomes
    a queued retry instead of a stale pending commit status."""

    inner: StatusReporter
    service: CIService

    async def build_started(self, event: ChangeEvent, build: BuildRecord) -> None:
        await self.inner.build_started(event, build)

    async def eval_finished(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        *,
        success: bool,
        warnings: list[str],
    ) -> None:
        await self.inner.eval_finished(event, build, success=success, warnings=warnings)

    async def eval_cancelled(self, event: ChangeEvent, build: BuildRecord) -> None:
        await self.inner.eval_cancelled(event, build)

    async def build_finished(  # noqa: PLR0913
        self,
        event: ChangeEvent,
        build: BuildRecord,
        status: str,
        generation: int,
        results: list[AttributeResult],
        *,
        attr_statuses: dict[str, str] | None = None,
        attr_prefix: str = "checks",
    ) -> None:
        try:
            await self.inner.build_finished(
                event,
                build,
                status,
                generation,
                results,
                attr_statuses=attr_statuses,
                attr_prefix=attr_prefix,
            )
        except Exception as e:
            logger.exception(
                "status post failed; queueing a retry", extra={"build_id": build.id}
            )
            await self.service.enqueue_work(
                "report", f"report-{build.id}", _report_payload(build.id, 1, e)
            )


@dataclass
class CIService:
    config: Config
    pool: asyncpg.Pool
    orchestrator: Orchestrator
    repo_store: RepoStore
    github: GitHubAppClient | None = None
    gitea: GiteaClient | None = None
    gitlab: GitlabClient | None = None
    credentials_providers: dict[str, CredentialsProvider] = field(default_factory=dict)
    # Strong references to fire-and-forget tasks: the event loop only
    # keeps weak references, so an unreferenced running build could be
    # garbage-collected mid-flight.
    _tasks: set[asyncio.Task] = field(default_factory=set)
    # Discovery must not run concurrently (upserts, webhook
    # registration); the timestamp debounces the UI refresh button,
    # which any logged-in user can press.
    _discovery_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _last_discovery: float = 0.0
    # Wakes the dispatcher early on local enqueues and completions.
    _work_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Process start (DB clock when constructed via bootstrap): effect
    # rows started after this are live deploys, not crash leftovers.
    _started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def _credentials_provider(self, forge: str) -> CredentialsProvider:
        return self.credentials_providers.get(forge, _STATIC_CREDENTIALS)

    def _spawn(self, coro: Coroutine[None, None, object]) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("background task failed", exc_info=task.exception())

    # -- change ingestion (ChangeSink for webhooks/reconciliation) -------

    async def submit(self, event: WebhookEvent) -> None:
        if isinstance(event, PrClosed):
            project = await self.repo_store.by_forge_id(
                event.forge, event.forge_repo_id
            )
            if project is not None:
                self.orchestrator.canceller.cancel_pr(project.id, event.pr_number)
            # Queued events for the PR would build it after the close.
            await self.pool.execute(
                """
                UPDATE work_queue SET status = 'done', finished_at = now()
                WHERE kind = 'change' AND status = 'pending'
                  AND payload->>'forge' = $1
                  AND payload->>'forge_repo_id' = $2
                  AND (payload->>'pr_number')::int = $3
                """,
                event.forge,
                event.forge_repo_id,
                event.pr_number,
            )
            return
        await self._submit_change(event)

    async def _submit_change(self, change: ChangeRequest) -> None:
        """Enqueue only; the dispatcher runs _process_change. The key
        serializes deliveries of one commit, not of one branch:
        supersede needs newer commits to run concurrently."""
        await self.enqueue_work(
            "change",
            f"change-{change.forge}-{change.forge_repo_id}-{change.commit_sha}",
            dataclasses.asdict(change),
        )

    async def _process_change(self, change: ChangeRequest) -> None:
        project = await self.repo_store.by_forge_id(change.forge, change.forge_repo_id)
        if project is None or not project.enabled:
            return
        if change.pr_number is None and not should_build_branch(
            self.config.branches, project.default_branch, change.branch
        ):
            return
        info = repo_info(project)
        credentials = await self._credentials_provider(info.forge).get(info.clone_url)
        event = ChangeEvent(
            repo=info,
            branch=change.branch,
            commit_sha=change.commit_sha,
            pr_number=change.pr_number,
            pr_author=change.pr_author,
            base_sha=change.base_sha,
            commit_message=change.commit_message,
        )
        await self.orchestrator.handle_change_event(event, credentials)

    # -- ControlBackend ---------------------------------------------------

    async def refresh_projects(self) -> None:
        async with self._discovery_lock:
            if time.monotonic() - self._last_discovery < REFRESH_COOLDOWN:
                return
            await self.discover_once()
            self._last_discovery = time.monotonic()

    async def restart_build(self, build_id: int) -> None:
        await self.enqueue_work("restart", f"build-{build_id}", {"build_id": build_id})

    async def restart_attribute(self, build_id: int, attr: str) -> None:
        await self.enqueue_work(
            "restart", f"build-{build_id}", {"build_id": build_id, "attr": attr}
        )

    async def restart_effects(self, build_id: int) -> None:
        await self.enqueue_work("effects", f"build-{build_id}", {"build_id": build_id})

    async def enqueue_work(
        self, kind: str, dedup_key: str, payload: dict[str, Any]
    ) -> None:
        await WorkQueue(self.pool).enqueue(kind, dedup_key, payload)
        self._work_event.set()

    async def work_loop(self) -> None:
        """Single dispatcher: claims queued intent and executes it."""
        queue = WorkQueue(self.pool)
        while True:
            try:
                item = await queue.claim_next()
            except Exception:
                logger.exception("work claim failed")
                item = None
            if item is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._work_event.wait(), timeout=5)
                self._work_event.clear()
                continue
            self._spawn(self._execute_work(queue, item))

    async def drain_work(self) -> None:
        """Execute claimable work to completion (tests)."""
        queue = WorkQueue(self.pool)
        while (item := await queue.claim_next()) is not None:
            await self._execute_work(queue, item)

    async def _execute_work(self, queue: WorkQueue, item: WorkItem) -> None:
        try:
            await self._dispatch_work(item)
        except Exception as e:
            logger.exception("work item failed", extra={"work_id": item.id})
            await queue.finish(item.id, error=str(e) or type(e).__name__)
        else:
            await queue.finish(item.id)
        finally:
            # A deferred same-key item may be claimable now.
            self._work_event.set()

    async def _dispatch_work(self, item: WorkItem) -> None:
        payload = item.payload
        if item.kind == "change":
            await self._process_change(ChangeRequest(**payload))
        elif item.kind == "restart":
            await self._restart(payload["build_id"], payload.get("attr"))
        elif item.kind == "rerun":
            # Crash recovery: resume pending attributes, no reset.
            await self._rerun(payload["build_id"])
        elif item.kind == "effects":
            await self._restart_effects(payload["build_id"])
        elif item.kind == "effect":
            await self._run_effect_item(payload["build_id"], payload["name"])
        elif item.kind == "report":
            await self._re_report(
                payload["build_id"],
                payload.get("attempt", 1),
                payload.get("retry_at"),
            )
        elif item.kind == "refresh-schedules":
            await self._refresh_schedules_item(payload["project_id"], payload["rev"])
        elif item.kind == "scheduled":
            await self._run_scheduled(
                DueEffect(
                    project_id=payload["project_id"],
                    schedule_name=payload["schedule_name"],
                    effect=payload["effect"],
                    when=ScheduleWhen.model_validate(payload["when"]),
                )
            )
        else:
            msg = f"unknown work kind {item.kind!r}"
            raise ValueError(msg)

    async def _re_report(
        self, build_id: int, attempt: int, retry_at: float | None = None
    ) -> None:
        """Re-post the build summary from database state. Waits for the
        larger of the attempt backoff and the forge's Retry-After."""
        delay = _report_delay(attempt, retry_at)
        if delay > 0:
            await asyncio.sleep(delay)
        build = await self.orchestrator.db.get_build(build_id)
        if build is None:
            return
        project = await self.repo_store.by_id(build.project_id)
        if project is None:
            return
        event = ChangeEvent(
            repo=repo_info(project),
            branch=build.branch,
            commit_sha=build.commit_sha,
            pr_number=build.pr_number,
        )
        rows = await self.pool.fetch(
            "SELECT attr, status FROM build_attributes WHERE build_id = $1", build_id
        )
        reporter = self.orchestrator.reporter
        if isinstance(reporter, RetryingReporter):
            # Post via the inner reporter: the wrapper would enqueue a
            # competing attempt-1 item on failure.
            reporter = reporter.inner
        try:
            # Empty results: only the summary is re-posted; per-attribute
            # statuses were already posted (or cached) inline.
            await reporter.build_finished(
                event,
                build,
                build.status,
                build.status_generation,
                [],
                attr_statuses={row["attr"]: row["status"] for row in rows},
            )
        except Exception as e:
            if attempt < MAX_REPORT_ATTEMPTS:
                await self.enqueue_work(
                    "report",
                    f"report-{build_id}",
                    _report_payload(build_id, attempt + 1, e),
                )
            raise

    async def _run_effect_item(self, build_id: int, name: str) -> None:
        build = await self.orchestrator.db.get_build(build_id)
        if build is None:
            return
        project = await self.repo_store.by_id(build.project_id)
        if project is None:
            return
        info = repo_info(project)
        credentials = await self._credentials_provider(info.forge).get(info.clone_url)
        try:
            await self.orchestrator.run_effect_item(info, build, name, credentials)
        except Exception as e:
            # Setup failures (fetch/checkout) happen before the
            # runner settles the row.
            await self.orchestrator.db.finish_effect(
                build_id, name, success=False, error=str(e) or type(e).__name__
            )
            raise

    async def _refresh_schedules_item(self, project_id: int, rev: str) -> None:
        """Discover and store onSchedule definitions at the commit."""
        project = await self.repo_store.by_id(project_id)
        if project is None or not project.enabled:
            return
        info = repo_info(project)
        credentials = await self._credentials_provider(info.forge).get(info.clone_url)
        await self.orchestrator.repos.fetch(
            info.key, info.clone_url, ["+refs/heads/*:refs/heads/*"], credentials
        )
        worktree = await self.orchestrator.repos.checkout_for_build(
            info.key,
            f"schedules-{project_id}",
            base_commit=rev,
            credentials=credentials,
        )
        try:
            ctx = EffectsContext(
                worktree_path=worktree.path,
                rev=rev,
                branch=info.default_branch,
                repo=info.name,
                extra_sandbox_paths=self.config.effects_extra_sandbox_paths,
            )
            schedules = await discover_schedules(ctx)
            await ScheduledEffectsStore(self.pool).replace_schedules(
                project_id, schedules
            )
        finally:
            await self.orchestrator.repos.remove_worktree(worktree)

    async def _restart_effects(self, build_id: int) -> None:
        if build_id in self.orchestrator.cancel_events:
            return  # build (or an effects rerun) still running
        build = await self.orchestrator.db.get_build(build_id)
        if build is None or build.status != "succeeded":
            return  # effects only ever run after a successful build
        project = await self.repo_store.by_id(build.project_id)
        if project is None:
            return
        info = repo_info(project)
        credentials = await self._credentials_provider(info.forge).get(info.clone_url)
        await self.orchestrator.rerun_effects(info, build, credentials)

    async def _restart(self, build_id: int, attr: str | None) -> None:
        """Reset attributes (one or all) and re-run only the pending jobs
        from the stored eval results — no re-eval."""
        if build_id in self.orchestrator.cancel_events:
            return  # still running; a restart would double-build
        if await self.orchestrator.db.get_build(build_id) is None:
            return
        if attr is not None:
            # A stale attr (e.g. after a re-eval renamed it) must not
            # reset the build row and spawn an empty rerun.
            known = await self.pool.fetchval(
                "SELECT 1 FROM build_attributes WHERE build_id = $1 AND attr = $2",
                build_id,
                attr,
            )
            if known is None:
                logger.warning(
                    "restart of unknown attribute ignored",
                    extra={"build_id": build_id, "attr": attr},
                )
                return
        # An explicit rebuild clears cached failures so the attributes
        # actually build again instead of re-skipping.
        await self.pool.execute(
            "DELETE FROM failed_builds WHERE derivation IN "
            "(SELECT drv_path FROM build_attributes "
            "WHERE build_id = $1 AND ($2::text IS NULL OR attr = $2))",
            build_id,
            attr,
        )
        await self.pool.execute(
            "UPDATE build_attributes SET status = 'pending', error = NULL, "
            "started_at = NULL, finished_at = NULL "
            "WHERE build_id = $1 AND ($2::text IS NULL OR attr = $2)",
            build_id,
            attr,
        )
        if attr is None:
            # A full restart re-runs effects; a partial rebuild must
            # not re-deploy.
            await self.pool.execute(
                "UPDATE builds SET effects_started = FALSE WHERE id = $1", build_id
            )
            await self.pool.execute(
                "UPDATE build_effects SET status = 'pending', error = NULL, "
                "finished_at = NULL, log_path = NULL, log_size = 0, "
                "log_truncated = FALSE WHERE build_id = $1",
                build_id,
            )
        # Queued, not started: the rerun decides whether this becomes
        # a re-eval (evaluating) or an attribute rerun (building).
        # Clearing finished_at keeps retention cleanup off a build
        # that is about to rerun; clearing error/eval_warnings keeps
        # a stale failure banner off a restart that succeeds.
        await self.pool.execute(
            "UPDATE builds SET status = 'pending', error = NULL, "
            "eval_warnings = NULL, started_at = NULL, finished_at = NULL "
            "WHERE id = $1",
            build_id,
        )
        await self._rerun(build_id)

    async def _rerun(self, build_id: int) -> None:
        # Serialized by the work queue's per-build dedup key.
        build = await self.orchestrator.db.get_build(build_id)
        if build is None:
            return
        project = await self.repo_store.by_id(build.project_id)
        if project is None:
            return
        info = repo_info(project)
        credentials = await self._credentials_provider(info.forge).get(info.clone_url)
        results = await find_unfinished_builds(self.pool, build_id=build_id)
        resumable = results[0] if results else None
        if resumable is None:
            return
        unfinished_count = await self.pool.fetchval(
            "SELECT count(*) FROM build_attributes "
            "WHERE build_id = $1 AND status IN ('pending', 'building')",
            build_id,
        )
        if (
            # A crash mid-eval leaves a partial attribute set; resuming
            # it would report success for an incomplete build.
            build.status != "evaluating"
            and resumable.has_attributes
            and len(resumable.pending_jobs) == unfinished_count
        ):
            # Stored drv paths may have been garbage-collected since
            # the eval; rerunning them would fail with "path does not
            # exist" instead of rebuilding.
            drvs = [job.drv_path for job in resumable.pending_jobs]
            valid = await check_store_paths(drvs)
            if all(drv in valid for drv in drvs):
                await self.orchestrator.rerun_pending_attributes(
                    info, build, resumable.pending_jobs, credentials
                )
                return
            logger.info(
                "stored derivations missing from the store; re-evaluating",
                extra={"build_id": build_id},
            )
        # No resumable eval results (no attribute rows, or unfinished
        # rows without drv_path): an empty rerun would aggregate to
        # "succeeded" without building anything; re-evaluate instead.
        try:
            await self._reeval(info, build, credentials)
        except Exception:
            logger.exception("re-evaluation failed", extra={"build_id": build_id})
            await self.orchestrator.db.set_build_status(
                build_id,
                BuildStatus.FAILED,
                error="re-evaluation failed; see service logs",
            )
            await self._report_interrupted(resumable)

    async def _reeval(
        self,
        info: RepoInfo,
        build: BuildRecord,
        credentials: FetchCredentials | None,
    ) -> None:
        event = ChangeEvent(
            repo=info,
            branch=build.branch,
            commit_sha=build.commit_sha,
            pr_number=build.pr_number,
        )
        refspecs = ["+refs/heads/*:refs/heads/*"]
        if build.pr_number is not None:
            refspecs.append(pr_refspec(info.forge, build.pr_number))
        await self.orchestrator.repos.fetch(
            info.key, info.clone_url, refspecs, credentials
        )
        # Credentials matter here too: submodule init in the checkout
        # fails for private submodules without them.
        worktree = await self.orchestrator.repos.checkout_for_build(
            info.key,
            f"rerun-{build.id}",
            base_commit=build.commit_sha,
            credentials=credentials,
        )
        try:
            # Stale rows (e.g. failed_eval with NULL drv_path) would
            # wedge the aggregate; the re-eval rewrites them. Finished
            # rows with a drv_path are kept: their results are valid
            # and the re-eval skips already-built attributes.
            await self.pool.execute(
                "DELETE FROM build_attributes WHERE build_id = $1 "
                "AND (status IN ('pending', 'building') OR drv_path IS NULL)",
                build.id,
            )
            await self.orchestrator.run_build(event, build, worktree.path)
        finally:
            await self.orchestrator.repos.remove_worktree(worktree)
            self.orchestrator.cancel_events.pop(build.id, None)

    async def recover_unfinished_builds(self) -> None:
        """Crash recovery: settle already-built attributes, then queue
        reruns for the rest. Builds interrupted mid-eval (no attribute
        rows) re-evaluate via the rerun path."""
        await fail_interrupted_effects(self.pool, self._started_at)
        for resumable in await find_unfinished_builds(self.pool):
            remaining, settled = await settle_already_built(
                self.orchestrator.db, resumable
            )
            if settled:
                # Recovered results still need gcroots/outputs updates.
                event = await self._change_event_for(resumable)
                if event is not None:
                    await self.orchestrator.post_process_skipped(event, settled)
            logger.info(
                "recovering build",
                extra={"build_id": resumable.build_id, "remaining": len(remaining)},
            )
            await self.enqueue_work(
                "rerun",
                f"build-{resumable.build_id}",
                {"build_id": resumable.build_id},
            )

    async def _change_event_for(self, resumable: ResumableBuild) -> ChangeEvent | None:
        project = await self.repo_store.by_id(resumable.project_id)
        if project is None:
            return None
        return ChangeEvent(
            repo=repo_info(project),
            branch=resumable.branch,
            commit_sha=resumable.commit_sha,
            pr_number=resumable.pr_number,
        )

    async def _report_interrupted(self, resumable: ResumableBuild) -> None:
        """Post the failure to the forge; otherwise the commit status
        stays pending forever after an interrupted evaluation."""
        build = await self.orchestrator.db.get_build(resumable.build_id)
        event = await self._change_event_for(resumable)
        if build is None or event is None:
            return
        await self.orchestrator.reporter.eval_finished(
            event, build, success=False, warnings=[]
        )
        await self.orchestrator.reporter.build_finished(
            event, build, BuildStatus.FAILED, build.status_generation, []
        )

    async def cancel_attribute(self, build_id: int, attr: str) -> None:
        event = self.orchestrator.attr_cancel_events.get((build_id, attr))
        if event is not None:
            event.set()
            return
        # Not queued or running (e.g. leftover from an interrupted
        # build): mark it cancelled directly.
        result = await self.pool.execute(
            "UPDATE build_attributes SET status = 'cancelled', "
            "finished_at = now() "
            "WHERE build_id = $1 AND attr = $2 "
            "AND status IN ('pending', 'building')",
            build_id,
            attr,
        )
        if result != "UPDATE 1":
            return
        # No running pipeline re-aggregates for us; without this the
        # build stays non-terminal forever once all rows are settled.
        status, generation = await self.orchestrator.db.aggregate_build(build_id)
        if status in BuildStatus.TERMINAL:
            await self._report_direct_finish(build_id, status, generation)

    async def cancel_build(self, build_id: int) -> None:
        event = self.orchestrator.cancel_events.get(build_id)
        if event is not None:
            event.set()
            return
        # Not running: mark cancelled directly.
        generation = await self.pool.fetchval(
            "UPDATE builds SET status = 'cancelled', finished_at = now(), "
            "status_generation = status_generation + 1 "
            "WHERE id = $1 AND status IN ('pending', 'evaluating', 'building') "
            "RETURNING status_generation",
            build_id,
        )
        if generation is None:
            return
        # Leftover pending/building rows would look running forever.
        await self.orchestrator.db.settle_unfinished_attributes(build_id)
        await self._report_direct_finish(build_id, BuildStatus.CANCELLED, generation)

    async def _report_direct_finish(
        self, build_id: int, status: str, generation: int
    ) -> None:
        """Post the terminal forge status for a build settled outside a
        running pipeline; otherwise the commit status stays pending
        forever."""
        build = await self.orchestrator.db.get_build(build_id)
        if build is None:
            return
        project = await self.repo_store.by_id(build.project_id)
        if project is None:
            return
        change = ChangeEvent(
            repo=repo_info(project),
            branch=build.branch,
            commit_sha=build.commit_sha,
            pr_number=build.pr_number,
        )
        await self.orchestrator.reporter.build_finished(
            change, build, status, generation, []
        )

    # -- background loops ---------------------------------------------------

    async def discovery_loop(self) -> None:
        reconciled = False
        while True:
            try:
                async with self._discovery_lock:
                    await self.discover_once()
                    self._last_discovery = time.monotonic()
                if not reconciled:
                    # Startup reconciliation needs discovery first
                    # (GitHub installation tokens are learned during
                    # discovery); retried until one pass succeeds so a
                    # forge outage at startup does not skip it.
                    await self.reconcile_once()
                    reconciled = True
            except Exception:
                logger.exception("project discovery failed")
            await asyncio.sleep(DISCOVERY_INTERVAL)

    async def reconcile_once(self) -> None:
        """Build default-branch and open-PR heads that got no build
        record while the service was down (missed webhooks)."""
        for project in await self.repo_store.enabled_repos():
            try:
                if project.forge == "github" and self.github is not None:
                    heads = await github_heads(self.github, project)
                elif project.forge == "gitea" and self.gitea is not None:
                    heads = await gitea_heads(self.gitea, project)
                elif project.forge == "gitlab" and self.gitlab is not None:
                    heads = await gitlab_heads(self.gitlab, project)
                else:
                    continue
                await reconcile_repo(self.pool, project, heads, self)
            except Exception:
                logger.exception(
                    "reconciliation failed",
                    extra={"project": f"{project.owner}/{project.name}"},
                )

    async def _warn_github_webhook_misconfig(self, github: GitHubAppClient) -> None:
        try:
            base = self.config.webhook_base_url or self.config.url
            for problem in await github.check_app_webhook(base):
                logger.warning("github app misconfigured: %s", problem)
        except Exception:
            logger.exception("github app webhook check failed")

    async def discover_once(self) -> None:
        if self.config.pull_based is not None:
            await self.repo_store.sync_pull_based(
                [
                    (repo.name, repo.url, repo.default_branch)
                    for repo in self.config.pull_based.repositories.values()
                ]
            )
        repos = []
        # The topic is only a legacy import aid (one-shot enablement in
        # sync_discovered); it must not hard-filter discovery, otherwise
        # untagged repos never appear in the admin UI.
        if self.github is not None and self.config.github is not None:
            await self._warn_github_webhook_misconfig(self.github)
            repos += filter_repos(
                replace(self.config.github.filters, topic=None),
                await self.github.discover_repos(),
            )
        if self.gitea is not None and self.config.gitea is not None:
            repos += filter_repos(
                replace(self.config.gitea.filters, topic=None),
                await self.gitea.discover_repos(),
            )
        if self.gitlab is not None and self.config.gitlab is not None:
            repos += filter_repos(
                replace(self.config.gitlab.filters, topic=None),
                await self.gitlab.discover_repos(),
            )
        topic = None
        for forge_config in (self.config.github, self.config.gitea, self.config.gitlab):
            if topic is None and forge_config is not None:
                topic = forge_config.filters.topic
        await self.repo_store.sync_discovered(repos, legacy_import_topic=topic)
        # Auto-register Gitea/GitLab webhooks for enabled projects.
        await self._register_hooks()

    async def _register_hooks(self) -> None:
        registrars: dict[str, tuple[Any, Callable[..., Awaitable[None]]]] = {}
        if self.gitea is not None:
            registrars["gitea"] = (self.gitea, register_repo_hook)
        if self.gitlab is not None:
            registrars["gitlab"] = (self.gitlab, register_gitlab_repo_hook)
        base = self.config.webhook_base_url or self.config.url
        for project in await self.repo_store.enabled_repos():
            if project.forge not in registrars:
                continue
            client, register = registrars[project.forge]
            try:
                await register(
                    client,
                    WebhookSecrets(self.pool, project.forge),
                    project.id,
                    project.owner,
                    project.name,
                    base,
                )
            except Exception:
                logger.exception(
                    "%s hook registration failed",
                    project.forge,
                    extra={"project": f"{project.owner}/{project.name}"},
                )

    async def maintenance_loop(self) -> None:
        while True:
            try:
                await cleanup_old_builds(
                    self.pool, self.config.state_dir, self.config.retention_days
                )
                build_ids = {
                    row["id"] for row in await self.pool.fetch("SELECT id FROM builds")
                }
                cleanup_orphan_log_dirs(build_ids, self.config.state_dir)
                await WorkQueue(self.pool).cleanup(self.config.retention_days)
                await self.orchestrator.repos.cleanup()
                await self.orchestrator.repos.gc()
            except Exception:
                logger.exception("maintenance run failed")
            await asyncio.sleep(MAINTENANCE_INTERVAL)

    async def scheduled_effects_loop(self) -> None:
        store = ScheduledEffectsStore(self.pool)
        while True:
            try:
                for due in await store.due_effects():
                    logger.info(
                        "scheduled effect due",
                        extra={
                            "schedule": due.schedule_name,
                            "effect": due.effect,
                        },
                    )
                    # Enqueue before mark_run: the reverse order loses
                    # the occurrence when we crash in between.
                    await self.enqueue_work(
                        "scheduled",
                        f"scheduled-{due.project_id}-{due.schedule_name}-{due.effect}",
                        {
                            "project_id": due.project_id,
                            "schedule_name": due.schedule_name,
                            "effect": due.effect,
                            "when": due.when.model_dump(exclude_none=True),
                        },
                    )
                    await store.mark_run(due)
            except Exception:
                logger.exception("scheduled-effects sweep failed")
            # Sleep to the next minute boundary: is_due matches exactly
            # one wall-clock minute, so a fixed 60s sleep after the sweep
            # work would drift and silently skip minutes.
            await asyncio.sleep(60 - (time.time() % 60))

    async def _run_scheduled(self, due: DueEffect) -> None:
        project = await self.repo_store.by_id(due.project_id)
        if project is None or not project.enabled:
            return
        info = repo_info(project)
        store = ScheduledEffectsStore(self.pool)
        run_id = await store.start_run(due)
        try:
            success = await self._run_scheduled_inner(due, info, run_id)
            await store.finish_run(run_id, success=success)
        except Exception as e:
            # Spawned task: an exception would only surface as "Task
            # exception was never retrieved" and leave the row running.
            logger.exception("scheduled effect crashed", extra={"run_id": run_id})
            await store.finish_run(run_id, success=False, error=str(e))

    async def _run_scheduled_inner(
        self, due: DueEffect, info: RepoInfo, run_id: int
    ) -> bool:
        credentials = await self._credentials_provider(info.forge).get(info.clone_url)
        await self.orchestrator.repos.fetch(
            info.key, info.clone_url, ["+refs/heads/*:refs/heads/*"], credentials
        )
        worktree = await self.orchestrator.repos.checkout_for_build(
            info.key,
            scheduled_worktree_id(due, run_id),
            base_commit=info.default_branch,
            credentials=credentials,
        )
        task_token = self.orchestrator.task_tokens.issue(due.project_id)
        log_dir = self.config.state_dir / "logs" / "scheduled"
        log_dir.mkdir(parents=True, exist_ok=True)
        log = LogWriter(
            path=log_dir / f"{run_id}.zst",
            size_limit=self.config.log_size_limit,
        )
        try:
            ctx = effects_context(
                self.config,
                info,
                worktree_path=worktree.path,
                rev=await worktree.rev_parse("HEAD"),
                branch=info.default_branch,
                git_token=credentials.token if credentials is not None else None,
                task_token=task_token,
            )
            return await run_scheduled_effect(
                ctx, due.schedule_name, due.effect, log.write
            )
        finally:
            self.orchestrator.task_tokens.revoke(task_token)
            await log.close()
            await self.orchestrator.repos.remove_worktree(worktree)
