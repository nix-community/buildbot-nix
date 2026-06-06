"""Service composition: wires every engine component into
one running process — database, orchestrator, forge clients, webhook
ingestion, web frontend, pollers, and background maintenance loops.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .db import BuildStatus
from .effects import EffectsContext, resolve_effects_secret
from .events import ChangeEvent, ProjectInfo
from .forge import (
    GiteaClient,
    GitHubAppClient,
    filter_repos,
)
from .gitea_hooks import GiteaWebhookSecrets, register_repo_hook
from .gitrepo import (
    CredentialsProvider,
    FetchCredentials,
    StaticCredentialsProvider,
)
from .reconcile import gitea_heads, github_heads, reconcile_project
from .recovery import (
    cleanup_old_builds,
    cleanup_orphan_log_dirs,
    find_unfinished_builds,
)
from .scheduled import DueEffect, ScheduledEffectsStore, run_scheduled_effect
from .webhooks import (
    ChangeRequest,
    PrClosed,
    WebhookEvent,
    should_build_branch,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine

    import asyncpg

    from .config import EngineConfig
    from .db import BuildRecord
    from .orchestrator import Orchestrator
    from .polling import PolledRepository
    from .projects import ProjectRecord, ProjectStore
    from .recovery import ResumableBuild

logger = logging.getLogger(__name__)

_STATIC_CREDENTIALS = StaticCredentialsProvider()

DISCOVERY_INTERVAL = 15 * 60
MAINTENANCE_INTERVAL = 60 * 60


def resolve_credential_path(path: Path | None) -> Path | None:
    """Relative secret paths are systemd LoadCredential names; resolve
    them against $CREDENTIALS_DIRECTORY (mirrors read_secret_file)."""
    if path is None or path.is_absolute():
        return path
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if directory is None:
        return path
    return Path(directory) / path


def scheduled_worktree_id(due: DueEffect) -> str:
    """Per-effect worktree id: effects of one schedule run concurrently
    and must not share a checkout. Schedule/effect names are
    repo-controlled, so sanitize against path traversal."""
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", f"{due.schedule_name}-{due.effect}")
    return f"scheduled-{due.project_id}-{safe}"


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


def project_info(record: ProjectRecord) -> ProjectInfo:
    return ProjectInfo(
        id=record.id,
        key=f"{record.forge}/{record.owner}/{record.name}",
        name=f"{record.owner}/{record.name}",
        owner=record.owner,
        repo=record.name,
        forge=record.forge,
        clone_url=record.url,
        default_branch=record.default_branch,
    )


@dataclass
class EngineService:
    config: EngineConfig
    pool: asyncpg.Pool
    orchestrator: Orchestrator
    project_store: ProjectStore
    github: GitHubAppClient | None = None
    gitea: GiteaClient | None = None
    credentials_providers: dict[str, CredentialsProvider] = field(default_factory=dict)
    # Strong references to fire-and-forget tasks: the event loop only
    # keeps weak references, so an unreferenced running build could be
    # garbage-collected mid-flight.
    _tasks: set[asyncio.Task] = field(default_factory=set)
    # Per-build serialization of reruns; the cancel_events guard alone
    # is racy (it is only set once a rerun reaches _build_attributes).
    _rerun_locks: dict[int, asyncio.Lock] = field(default_factory=dict)

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
            project = await self.project_store.by_forge_id(
                event.forge, event.forge_repo_id
            )
            if project is not None:
                self.orchestrator.canceller.cancel_pr(project.id, event.pr_number)
            return
        await self._submit_change(event)

    async def _submit_change(self, change: ChangeRequest) -> None:
        project = await self.project_store.by_forge_id(
            change.forge, change.forge_repo_id
        )
        if project is None or not project.enabled:
            return
        if change.pr_number is None and not should_build_branch(
            self.config.branches, project.default_branch, change.branch
        ):
            return
        info = project_info(project)
        credentials = await self._credentials_provider(info.forge).get(info.clone_url)
        event = ChangeEvent(
            project=info,
            branch=change.branch,
            commit_sha=change.commit_sha,
            pr_number=change.pr_number,
            pr_author=change.pr_author,
            base_sha=change.base_sha,
            commit_message=change.commit_message,
        )
        self._spawn(self.orchestrator.handle_change_event(event, credentials))

    # -- ControlBackend ---------------------------------------------------

    async def restart_build(self, build_id: int) -> None:
        await self._restart(build_id, attr=None)

    async def restart_attribute(self, build_id: int, attr: str) -> None:
        await self._restart(build_id, attr=attr)

    async def _restart(self, build_id: int, attr: str | None) -> None:
        """Reset attributes (one or all) and re-run only the pending jobs
        from the stored eval results — no re-eval."""
        if build_id in self.orchestrator.cancel_events:
            return  # still running; a restart would double-build
        if await self.orchestrator.db.get_build(build_id) is None:
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
            "UPDATE build_attributes SET status = 'pending', error = NULL "
            "WHERE build_id = $1 AND ($2::text IS NULL OR attr = $2)",
            build_id,
            attr,
        )
        # Clear the stale completion timestamp: retention cleanup keys
        # on finished_at and must not delete a build mid-rerun.
        await self.pool.execute(
            "UPDATE builds SET status = 'building', finished_at = NULL WHERE id = $1",
            build_id,
        )
        await self._rerun_pending(build_id)

    async def _rerun_pending(self, build_id: int) -> None:
        self._spawn(self._locked_rerun(build_id))

    async def _locked_rerun(self, build_id: int) -> None:
        lock = self._rerun_locks.setdefault(build_id, asyncio.Lock())
        async with lock:
            build = await self.orchestrator.db.get_build(build_id)
            if build is None:
                return
            project = await self.project_store.by_id(build.project_id)
            if project is None:
                return
            info = project_info(project)
            credentials = await self._credentials_provider(info.forge).get(
                info.clone_url
            )
            results = await find_unfinished_builds(self.pool, build_id=build_id)
            resumable = results[0] if results else None
            if resumable is None:
                return
            pending_count = await self.pool.fetchval(
                "SELECT count(*) FROM build_attributes "
                "WHERE build_id = $1 AND status = 'pending'",
                build_id,
            )
            if (
                resumable.has_attributes
                and len(resumable.pending_jobs) == pending_count
            ):
                await self.orchestrator.rerun_pending_attributes(
                    info, build, resumable.pending_jobs, credentials
                )
                return
            # No resumable eval results (no attribute rows, or pending
            # rows without drv_path): an empty rerun would aggregate to
            # "succeeded" without building anything; re-evaluate instead.
            await self._reeval(info, build, credentials)

    async def _reeval(
        self,
        info: ProjectInfo,
        build: BuildRecord,
        credentials: FetchCredentials | None,
    ) -> None:
        event = ChangeEvent(
            project=info,
            branch=build.branch,
            commit_sha=build.commit_sha,
            pr_number=build.pr_number,
        )
        await self.orchestrator.repos.fetch(
            info.key,
            info.clone_url,
            ["+refs/heads/*:refs/heads/*", "+refs/pull/*:refs/pull/*"],
            credentials,
        )
        worktree = await self.orchestrator.repos.checkout_for_build(
            info.key, f"rerun-{build.id}", base_commit=build.commit_sha
        )
        try:
            # Stale rows (e.g. failed_eval with NULL drv_path) would
            # wedge the aggregate; the re-eval rewrites the full set.
            await self.pool.execute(
                "DELETE FROM build_attributes WHERE build_id = $1", build.id
            )
            await self.orchestrator.run_build(event, build, worktree.path)
        finally:
            await self.orchestrator.repos.remove_worktree(worktree)
            self.orchestrator.cancel_events.pop(build.id, None)

    async def _change_event_for(self, resumable: ResumableBuild) -> ChangeEvent | None:
        project = await self.project_store.by_id(resumable.project_id)
        if project is None:
            return None
        return ChangeEvent(
            project=project_info(project),
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

    async def cancel_build(self, build_id: int) -> None:
        event = self.orchestrator.cancel_events.get(build_id)
        if event is not None:
            event.set()
            return
        # Not running: mark cancelled directly.
        result = await self.pool.execute(
            "UPDATE builds SET status = 'cancelled', finished_at = now(), "
            "status_generation = status_generation + 1 "
            "WHERE id = $1 AND status IN ('pending', 'evaluating', 'building')",
            build_id,
        )
        if result != "UPDATE 1":
            return
        # Without a running pipeline nothing else reports the final
        # state; post the forge status here or the commit stays pending.
        build = await self.orchestrator.db.get_build(build_id)
        if build is None:
            return
        project = await self.project_store.by_id(build.project_id)
        if project is None:
            return
        change = ChangeEvent(
            project=project_info(project),
            branch=build.branch,
            commit_sha=build.commit_sha,
            pr_number=build.pr_number,
        )
        await self.orchestrator.reporter.build_finished(
            change, build, BuildStatus.CANCELLED, build.status_generation, []
        )

    # -- background loops ---------------------------------------------------

    async def discovery_loop(self) -> None:
        reconciled = False
        while True:
            try:
                await self.discover_once()
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
        for project in await self.project_store.enabled_projects():
            try:
                if project.forge == "github" and self.github is not None:
                    heads = await github_heads(self.github, project)
                elif project.forge == "gitea" and self.gitea is not None:
                    heads = await gitea_heads(self.gitea, project)
                else:
                    continue
                await reconcile_project(self.pool, project, heads, self)
            except Exception:
                logger.exception(
                    "reconciliation failed",
                    extra={"project": f"{project.owner}/{project.name}"},
                )

    async def discover_once(self) -> None:
        if self.config.pull_based is not None:
            await self.project_store.sync_pull_based(
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
            repos += filter_repos(
                replace(self.config.github.filters, topic=None),
                await self.github.discover_repos(),
            )
        if self.gitea is not None and self.config.gitea is not None:
            repos += filter_repos(
                replace(self.config.gitea.filters, topic=None),
                await self.gitea.discover_repos(),
            )
        topic = None
        if self.config.github is not None:
            topic = self.config.github.filters.topic
        if topic is None and self.config.gitea is not None:
            topic = self.config.gitea.filters.topic
        await self.project_store.sync_discovered(repos, legacy_import_topic=topic)
        # Auto-register Gitea webhooks for enabled projects.
        if self.gitea is not None:
            secrets_store = GiteaWebhookSecrets(self.pool)
            base = self.config.webhook_base_url or self.config.url
            for project in await self.project_store.enabled_projects():
                if project.forge == "gitea":
                    try:
                        await register_repo_hook(
                            self.gitea,
                            secrets_store,
                            project.id,
                            project.owner,
                            project.name,
                            base,
                        )
                    except Exception:
                        logger.exception(
                            "gitea hook registration failed",
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
                    await store.mark_run(due)
                    logger.info(
                        "scheduled effect due",
                        extra={
                            "schedule": due.schedule_name,
                            "effect": due.effect,
                        },
                    )
                    # Execution runs a default-branch checkout via
                    # buildbot-effects run-scheduled.
                    self._spawn(self._run_scheduled(due))
            except Exception:
                logger.exception("scheduled-effects sweep failed")
            # Sleep to the next minute boundary: is_due matches exactly
            # one wall-clock minute, so a fixed 60s sleep after the sweep
            # work would drift and silently skip minutes.
            await asyncio.sleep(60 - (time.time() % 60))

    async def _run_scheduled(self, due: DueEffect) -> None:
        project = await self.project_store.by_id(due.project_id)
        if project is None or not project.enabled:
            return
        info = project_info(project)
        credentials = await self._credentials_provider(info.forge).get(info.clone_url)
        await self.orchestrator.repos.fetch(
            info.key, info.clone_url, ["+refs/heads/*:refs/heads/*"], credentials
        )
        worktree = await self.orchestrator.repos.checkout_for_build(
            info.key,
            scheduled_worktree_id(due),
            base_commit=info.default_branch,
            credentials=credentials,
        )
        try:
            ctx = EffectsContext(
                worktree_path=worktree.path,
                rev=await worktree.rev_parse("HEAD"),
                branch=info.default_branch,
                repo=info.name,
                secret_name=resolve_effects_secret(
                    self.config.effects_per_repo_secrets,
                    info.forge,
                    info.owner,
                    info.repo,
                ),
                extra_sandbox_paths=self.config.effects_extra_sandbox_paths,
            )
            await run_scheduled_effect(ctx, due.schedule_name, due.effect)
        finally:
            await self.orchestrator.repos.remove_worktree(worktree)
