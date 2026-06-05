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

import asyncpg
import httpx
import uvicorn

from .api_tokens import ApiTokenStore
from .auth import (
    AuthzConfig,
    OAuthProvider,
    SessionSigner,
    gitea_oauth,
    github_oauth,
    load_signing_keys,
    oidc_provider,
)
from .config import ConfigError, read_secret_file
from .db import BuildDB, BuildStatus
from .effects import EffectsContext, resolve_effects_secret
from .executor import BuildSettings, FairScheduler, NixBuildExecutor
from .failed_builds import PostgresFailedBuildCache
from .forge import (
    GiteaClient,
    GiteaFetchCredentialsProvider,
    GitHubAppClient,
    GitHubFetchCredentialsProvider,
    filter_repos,
)
from .forge_tokens import ForgeTokenStore
from .gitea_hooks import GiteaWebhookSecrets, register_repo_hook
from .gitrepo import (
    CredentialsProvider,
    FetchCredentials,
    RepoManager,
    StaticCredentialsProvider,
)
from .migrations import apply_migrations
from .nix_eval import CgroupLimiter, EvalRunner
from .orchestrator import ChangeEvent, Orchestrator, ProjectInfo
from .polling import PolledRepository, PollingService
from .projects import ProjectStore
from .reconcile import gitea_heads, github_heads, reconcile_project
from .recovery import (
    cleanup_old_builds,
    cleanup_orphan_log_dirs,
    fail_interrupted_eval,
    find_unfinished_builds,
    settle_already_built,
)
from .scheduled import DueEffect, ScheduledEffectsStore, run_scheduled_effect
from .status import (
    CommitStatusPoster,
    FailedStatusStore,
    ForgeStatusReporter,
    GiteaStatusPoster,
    GitHubStatusPoster,
)
from .visibility import AccessCache, ForgeRepoAccessFetcher, VisibilityService
from .web.app import create_app
from .web.auth_routes import create_auth_router
from .web.control_routes import create_control_router
from .web.token_routes import create_token_router
from .webhooks import (
    ChangeRequest,
    PrClosed,
    WebhookEvent,
    create_webhook_router,
    should_build_branch,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from fastapi import FastAPI

    from .config import EngineConfig
    from .db import BuildRecord
    from .projects import ProjectRecord
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
            resumable = next(
                (
                    r
                    for r in await find_unfinished_builds(self.pool)
                    if r.build_id == build_id
                ),
                None,
            )
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


def _resolve_dsn(config: EngineConfig) -> str:
    if config.db_url_file is not None:
        return read_secret_file(config.db_url_file)
    if config.db_url is not None:
        return config.db_url
    msg = "either db_url or db_url_file must be configured"
    raise ConfigError(msg)


def _polled_repositories(config: EngineConfig) -> list[PolledRepository]:
    if config.pull_based is None:
        return []
    return [
        PolledRepository(
            name=repo.name,
            url=repo.url,
            default_branch=repo.default_branch,
            poll_interval=repo.poll_interval,
            ssh_private_key_file=resolve_credential_path(repo.ssh_private_key_file),
            ssh_known_hosts_file=resolve_credential_path(repo.ssh_known_hosts_file),
        )
        for repo in config.pull_based.repositories.values()
    ]


async def _login_providers(config: EngineConfig) -> dict[str, OAuthProvider]:
    providers: dict[str, OAuthProvider] = {}
    if config.github is not None and config.github.oauth_id:
        providers["github"] = github_oauth(
            config.github.oauth_id,
            config.github.oauth_secret,
            config.github.api_url,
        )
    if config.gitea is not None and config.gitea.oauth_id:
        providers["gitea"] = gitea_oauth(
            config.gitea.instance_url,
            config.gitea.oauth_id,
            config.gitea.oauth_secret,
        )
    if config.oidc is not None:
        try:
            providers["oidc"] = await oidc_provider(
                httpx.AsyncClient(),
                config.oidc.discovery_url,
                config.oidc.client_id,
                config.oidc.client_secret,
                config.oidc.scope,
                username_claim=config.oidc.mapping.username,
            )
        except Exception:
            logger.exception("OIDC discovery failed; OIDC login disabled")
    return providers


async def build_service(config: EngineConfig) -> tuple[EngineService, FastAPI]:
    """Create the composed service and its ASGI app."""
    # Normalize SQLAlchemy/asyncpg-style URLs before *both* consumers;
    # apply_migrations chokes on the +asyncpg scheme just like the pool.
    dsn = _resolve_dsn(config).replace("postgresql+asyncpg://", "postgresql://")
    await apply_migrations(dsn)
    pool = await asyncpg.create_pool(dsn)

    github = None
    gitea = None
    credentials_providers: dict[str, CredentialsProvider] = {}
    posters: dict[str, CommitStatusPoster] = {}
    if config.github is not None:
        github = GitHubAppClient(
            config.github.id,
            config.github.secret_key_file,
            api_url=config.github.api_url,
        )
        credentials_providers["github"] = GitHubFetchCredentialsProvider(github)
        posters["github"] = GitHubStatusPoster(github)
    if config.gitea is not None:
        gitea = GiteaClient(config.gitea.instance_url, config.gitea.token)
        credentials_providers["gitea"] = GiteaFetchCredentialsProvider(
            config.gitea.instance_url,
            config.gitea.token,
            ssh_private_key_file=resolve_credential_path(
                config.gitea.ssh_private_key_file
            ),
            ssh_known_hosts_file=resolve_credential_path(
                config.gitea.ssh_known_hosts_file
            ),
        )
        posters["gitea"] = GiteaStatusPoster(gitea)
    if config.pull_based is not None:
        credentials_providers["pull_based"] = PullBasedCredentialsProvider(
            _polled_repositories(config)
        )

    reporter = None
    if posters:
        reporter = ForgeStatusReporter(
            posters,
            FailedStatusStore(pool),
            config.url,
            config.failed_build_report_limit,
        )

    executor = NixBuildExecutor(
        FairScheduler(config.build_concurrency or os.cpu_count() or 4),
        BuildSettings(
            log_dir=config.state_dir / "logs",
            timeout=config.build_timeout,
            max_silent_time=config.build_max_silent_time,
            show_trace=config.show_trace_on_failure,
            log_size_limit=config.log_size_limit,
        ),
    )
    orchestrator = Orchestrator(
        config=config,
        db=BuildDB(pool),
        repos=RepoManager(config.state_dir),
        eval_runner=EvalRunner(config.eval_concurrency, limiter=CgroupLimiter.create()),
        executor=executor,
        failed_build_cache=PostgresFailedBuildCache(pool),
    )
    if reporter is not None:
        orchestrator.reporter = reporter

    service = EngineService(
        config=config,
        pool=pool,
        orchestrator=orchestrator,
        project_store=ProjectStore(pool),
        github=github,
        gitea=gitea,
        credentials_providers=credentials_providers,
    )

    # Web application.
    app = create_app(pool, config.state_dir, orchestrator.log_registry)
    ctx = app.state.web_context
    authz = AuthzConfig(
        admins=config.admins,
        allow_unauthenticated_control=config.allow_unauthenticated_control,
    )
    signer = SessionSigner(
        load_signing_keys(config.state_dir), lifetime=config.session_lifetime
    )
    ctx.signer = signer
    ctx.token_store = ApiTokenStore(pool)
    ctx.forge_tokens = ForgeTokenStore(pool)
    ctx.visibility = VisibilityService(
        pool,
        authz,
        fetcher=ForgeRepoAccessFetcher(
            httpx.AsyncClient(),
            gitea_url=config.gitea.instance_url if config.gitea else None,
        ),
        cache=AccessCache(config.repo_acl_cache_ttl),
    )

    providers = await _login_providers(config)
    if providers:
        app.include_router(
            create_auth_router(providers, signer, config.url, ctx.forge_tokens)
        )
        ctx.env.globals["login_providers"] = sorted(providers)
    app.include_router(create_control_router(ctx, service, authz, config.url))
    app.include_router(create_token_router(ctx, ctx.token_store, config.url))
    app.include_router(
        create_webhook_router(
            service,
            config.github.webhook_secret if config.github is not None else None,
            GiteaWebhookSecrets(pool) if config.gitea is not None else None,
        )
    )
    return service, app


async def _startup(service: EngineService) -> None:
    """One-shot startup work, ordered: discovery (the GitHub
    installation map must exist before any fetch/status), crash
    recovery, then reconciliation of heads missed during downtime."""
    try:
        await service.discover_once()
    except Exception:
        logger.exception("initial project discovery failed")

    # Crash recovery before serving: settle already-built attributes,
    # then resume the rest without re-eval.
    for resumable in await find_unfinished_builds(service.pool):
        if await fail_interrupted_eval(service.orchestrator.db, resumable):
            await service._report_interrupted(resumable)  # noqa: SLF001
            continue
        remaining, settled = await settle_already_built(
            service.orchestrator.db, resumable
        )
        if settled:
            # Recovered results still need gcroots/outputs updates.
            event = await service._change_event_for(resumable)  # noqa: SLF001
            if event is not None:
                await service.orchestrator._post_process_skipped(  # noqa: SLF001
                    event, settled
                )
        logger.info(
            "recovering build",
            extra={"build_id": resumable.build_id, "remaining": len(remaining)},
        )
        await service._rerun_pending(resumable.build_id)  # noqa: SLF001


async def run_service(config: EngineConfig) -> None:
    service, app = await build_service(config)

    await _startup(service)

    tasks = [
        asyncio.create_task(service.discovery_loop()),
        asyncio.create_task(service.maintenance_loop()),
        asyncio.create_task(service.scheduled_effects_loop()),
    ]

    poller: PollingService | None = None
    if config.pull_based is not None:
        polled = _polled_repositories(config)

        class PollSink:
            async def head_changed(
                self, repo: PolledRepository, branch: str, commit_sha: str
            ) -> None:
                await service._submit_change(  # noqa: SLF001
                    ChangeRequest(
                        forge="pull_based",
                        forge_repo_id=repo.name,
                        branch=branch,
                        commit_sha=commit_sha,
                    )
                )

        poller = PollingService(polled, PollSink(), config.pull_based.poll_spread)
        poller.start()

    # uvicorn binds only one of host/port and uds per server, so a
    # deployment with both gets two servers over the same app.
    servers = [
        uvicorn.Server(
            uvicorn.Config(
                app,
                host="0.0.0.0",  # noqa: S104
                port=config.http_port,
                log_level="info",
            )
        )
    ]
    if config.http_unix_socket:
        servers.append(
            uvicorn.Server(
                uvicorn.Config(app, uds=str(config.http_unix_socket), log_level="info")
            )
        )
    server_tasks = [asyncio.create_task(server.serve()) for server in servers]
    try:
        # Each uvicorn server installs its own signal handlers; with two
        # servers only the last one wins, so the other would never stop.
        # Treat the first exit as shutdown and cancel the rest.
        done, pending = await asyncio.wait(
            server_tasks, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()
    finally:
        if poller is not None:
            await poller.stop()
        for task in tasks:
            task.cancel()
