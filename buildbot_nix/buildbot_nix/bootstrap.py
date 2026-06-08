"""Process bootstrap: resolve configuration into a wired CIService
and run it — database pool, migrations, forge clients, auth providers,
web app, and the uvicorn server.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
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
    gitlab_oauth,
    load_signing_keys,
    oidc_provider,
)
from .config import (
    ConfigError,
    GiteaConfig,
    GitlabConfig,
    OIDCConfig,
    read_secret_file,
    resolve_credential_path,
)
from .db import BuildDB
from .executor import BuildSettings, FairScheduler, NixBuildExecutor
from .failed_builds import PostgresFailedBuildCache
from .forge import (
    GiteaClient,
    GitHubAppClient,
    GitHubFetchCredentialsProvider,
    GitlabClient,
    NetrcFetchCredentialsProvider,
)
from .forge_tokens import ForgeTokenStore
from .gitrepo import (
    CredentialsProvider,
    RepoManager,
)
from .hook_secrets import WebhookSecrets
from .migrations import apply_migrations
from .nix_eval import CgroupLimiter, EvalRunner
from .orchestrator import Orchestrator
from .polling import PolledRepository, PollingService
from .repos import RepoStore
from .status import (
    CommitStatusPoster,
    FailedStatusStore,
    ForgeStatusReporter,
    GiteaStatusPoster,
    GitHubStatusPoster,
    GitlabStatusPoster,
)
from .visibility import AccessCache, ForgeRepoAccessFetcher, VisibilityService
from .web.app import create_app
from .web.auth_routes import create_auth_router
from .web.control_routes import create_control_api_router, create_control_router
from .web.token_routes import create_token_router
from .webhooks import (
    ChangeRequest,
    create_webhook_router,
)
from .work_queue import WorkQueue

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI
    from uvicorn._types import ASGIApplication

    from .config import Config

from .service import (
    CIService,
    PullBasedCredentialsProvider,
    RetryingReporter,
)

logger = logging.getLogger(__name__)


def _resolve_dsn(config: Config) -> str:
    if config.db_url_file is not None:
        return read_secret_file(config.db_url_file)
    if config.db_url is not None:
        return config.db_url
    msg = "either db_url or db_url_file must be configured"
    raise ConfigError(msg)


def _polled_repositories(config: Config) -> list[PolledRepository]:
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


async def _login_providers(config: Config) -> dict[str, OAuthProvider]:
    providers: dict[str, OAuthProvider] = {}
    if config.github is not None and config.github.oauth_id:
        providers["github"] = github_oauth(
            config.github.oauth_id,
            config.github.oauth_secret,
            config.github.api_url,
            private_repo_scope=config.github.oauth_private_repo_scope,
        )
    if config.gitea is not None and config.gitea.oauth_id:
        providers["gitea"] = gitea_oauth(
            config.gitea.instance_url,
            config.gitea.oauth_id,
            config.gitea.oauth_secret,
        )
    if config.gitlab is not None and config.gitlab.oauth_id:
        providers["gitlab"] = gitlab_oauth(
            config.gitlab.instance_url,
            config.gitlab.oauth_id,
            config.gitlab.oauth_secret,
        )
    return providers


async def _oidc_login_provider(
    oidc: OIDCConfig, http_factory: Callable[[], httpx.AsyncClient]
) -> OAuthProvider:
    async with http_factory() as http:
        return await oidc_provider(
            http,
            oidc.discovery_url,
            oidc.client_id,
            oidc.client_secret,
            oidc.scope,
            username_claim=oidc.mapping.username,
            groups_claim=oidc.mapping.groups,
        )


async def register_oidc_with_retry(
    oidc: OIDCConfig,
    providers: dict[str, OAuthProvider],
    on_registered: Callable[[], None] | None = None,
    retry_delay: float = 60,
    http_factory: Callable[[], httpx.AsyncClient] = httpx.AsyncClient,
) -> None:
    """Register the OIDC provider, retrying with backoff: a transient
    IdP outage at startup must not permanently disable OIDC login. The
    auth router looks providers up per request, so late registration
    takes effect immediately."""
    delay = retry_delay
    while True:
        try:
            providers["oidc"] = await _oidc_login_provider(oidc, http_factory)
        except Exception:
            logger.exception("OIDC discovery failed; retrying in %ss", delay)
            await asyncio.sleep(delay)
            if delay:
                delay = min(delay * 2, 600)
            continue
        if on_registered is not None:
            on_registered()
        return


@dataclass
class _ForgeClients:
    github: GitHubAppClient | None = None
    gitea: GiteaClient | None = None
    gitlab: GitlabClient | None = None
    credentials_providers: dict[str, CredentialsProvider] = field(default_factory=dict)
    posters: dict[str, CommitStatusPoster] = field(default_factory=dict)


def _netrc_credentials(
    forge_config: GiteaConfig | GitlabConfig,
) -> NetrcFetchCredentialsProvider:
    return NetrcFetchCredentialsProvider(
        forge_config.instance_url,
        forge_config.token,
        ssh_private_key_file=resolve_credential_path(forge_config.ssh_private_key_file),
        ssh_known_hosts_file=resolve_credential_path(forge_config.ssh_known_hosts_file),
    )


def _forge_clients(config: Config) -> _ForgeClients:
    clients = _ForgeClients()
    if config.github is not None:
        clients.github = GitHubAppClient(
            config.github.id,
            config.github.secret_key_file,
            api_url=config.github.api_url,
        )
        clients.credentials_providers["github"] = GitHubFetchCredentialsProvider(
            clients.github
        )
        clients.posters["github"] = GitHubStatusPoster(clients.github)
    if config.gitea is not None:
        clients.gitea = GiteaClient(config.gitea.instance_url, config.gitea.token)
        clients.credentials_providers["gitea"] = _netrc_credentials(config.gitea)
        clients.posters["gitea"] = GiteaStatusPoster(clients.gitea)
    if config.gitlab is not None:
        clients.gitlab = GitlabClient(config.gitlab.instance_url, config.gitlab.token)
        clients.credentials_providers["gitlab"] = _netrc_credentials(config.gitlab)
        clients.posters["gitlab"] = GitlabStatusPoster(clients.gitlab)
    return clients


async def build_service(config: Config) -> tuple[CIService, FastAPI]:
    """Create the composed service and its ASGI app."""
    # Normalize SQLAlchemy/asyncpg-style URLs before *both* consumers;
    # apply_migrations chokes on the +asyncpg scheme just like the pool.
    dsn = _resolve_dsn(config).replace("postgresql+asyncpg://", "postgresql://")
    await apply_migrations(dsn)
    pool = await asyncpg.create_pool(dsn)

    forges = _forge_clients(config)
    github, gitea, gitlab = forges.github, forges.gitea, forges.gitlab
    credentials_providers = forges.credentials_providers
    posters = forges.posters
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
        failed_build_cache=lambda project_id: PostgresFailedBuildCache(
            pool, project_id
        ),
    )
    service = CIService(
        config=config,
        pool=pool,
        orchestrator=orchestrator,
        repo_store=RepoStore(pool),
        github=github,
        gitea=gitea,
        gitlab=gitlab,
        credentials_providers=credentials_providers,
        # DB clock, not app clock: the crash sweep compares it against
        # started_at columns set by now() in SQL.
        _started_at=await pool.fetchval("SELECT now()"),
    )
    if reporter is not None:
        orchestrator.reporter = RetryingReporter(reporter, service)

    # Web application.
    app = create_app(
        pool, config.state_dir, orchestrator.log_registry, orchestrator.task_tokens
    )
    ctx = app.state.web_context
    authz = AuthzConfig(
        admins=config.admins,
        allow_unauthenticated_control=config.allow_unauthenticated_control,
        private_repo_viewers=config.private_repo_viewers,
    )
    signer = SessionSigner(
        load_signing_keys(config.state_dir), lifetime=config.session_lifetime
    )
    ctx.signer = signer
    ctx.authz = authz
    ctx.webhook_base_url = config.webhook_base_url or config.url
    ctx.token_store = ApiTokenStore(pool)
    ctx.forge_tokens = ForgeTokenStore(pool)
    ctx.visibility = VisibilityService(
        pool,
        authz,
        fetcher=ForgeRepoAccessFetcher(
            httpx.AsyncClient(),
            gitea_url=config.gitea.instance_url if config.gitea else None,
            gitlab_url=config.gitlab.instance_url if config.gitlab else None,
            github_api_url=config.github.api_url
            if config.github
            else "https://api.github.com",
        ),
        cache=AccessCache(config.repo_acl_cache_ttl),
    )

    providers = await _login_providers(config)
    oidc_pending = False
    if config.oidc is not None:
        try:
            providers["oidc"] = await _oidc_login_provider(
                config.oidc, httpx.AsyncClient
            )
        except Exception:
            logger.exception("OIDC discovery failed; retrying in background")
            oidc_pending = True
    if providers or oidc_pending:
        app.include_router(
            create_auth_router(
                providers,
                signer,
                config.url,
                ctx.forge_tokens,
                private_repo_viewers=config.private_repo_viewers,
                revoked_sessions=ctx.revoked_sessions,
                on_logout=ctx.visibility.invalidate_user,
            ),
            include_in_schema=False,
        )
        labels = {"github": "GitHub", "gitea": "Gitea", "gitlab": "GitLab"}
        if config.oidc is not None:
            labels["oidc"] = config.oidc.name

        def refresh_login_buttons() -> None:
            ctx.env.globals["login_providers"] = [
                {"key": key, "label": labels.get(key, key)} for key in sorted(providers)
            ]

        refresh_login_buttons()
        if oidc_pending and config.oidc is not None:
            # Keep a reference so the task is not garbage collected.
            app.state.oidc_retry_task = asyncio.create_task(
                register_oidc_with_retry(
                    config.oidc, providers, on_registered=refresh_login_buttons
                )
            )
    app.include_router(
        create_control_router(ctx, service, authz, config.url), include_in_schema=False
    )
    app.include_router(create_control_api_router(ctx, service, authz, config.url))
    app.include_router(
        create_token_router(ctx, ctx.token_store, config.url), include_in_schema=False
    )
    app.include_router(
        create_webhook_router(
            service,
            config.github.webhook_secret if config.github is not None else None,
            WebhookSecrets(pool, "gitea") if config.gitea is not None else None,
            WebhookSecrets(pool, "gitlab") if config.gitlab is not None else None,
        ),
        include_in_schema=False,
    )
    return service, app


async def _startup(service: CIService) -> None:
    """One-shot startup work, ordered: discovery (the GitHub
    installation map must exist before any fetch/status), crash
    recovery, then reconciliation of heads missed during downtime."""
    try:
        await service.discover_once()
    except Exception:
        logger.exception("initial project discovery failed")

    # Crash recovery before serving.
    await service.recover_unfinished_builds()


async def _run_startup(service: CIService) -> None:
    """One-shot startup, then the periodic discovery loop (which must
    not run concurrently with the initial discovery)."""
    try:
        await _startup(service)
    except Exception:
        logger.exception("startup work failed")
    await service.discovery_loop()


def _uvicorn_configs(
    config: Config, app: ASGIApplication | Callable[..., object]
) -> list[uvicorn.Config]:
    """uvicorn binds only one of host/port and uds per server, so each
    listener gets its own server over the same app. With a unix socket
    (TLS proxy deployment), the TCP listener would be a plaintext
    bypass of the proxy, so it is only kept on explicit request
    (http_listen)."""
    configs = []
    if config.http_unix_socket is None or config.http_listen:
        configs.append(
            uvicorn.Config(
                app,
                host="0.0.0.0",  # noqa: S104
                port=config.http_port,
                log_level="info",
            )
        )
    if config.http_unix_socket:
        configs.append(
            uvicorn.Config(app, uds=str(config.http_unix_socket), log_level="info")
        )
    return configs


async def run_service(config: Config) -> None:
    service, app = await build_service(config)

    # Before the dispatcher starts: settling later would requeue work
    # that a running dispatcher legitimately claimed in the meantime.
    await WorkQueue(service.pool).settle_interrupted()

    # Startup can take minutes; it must not keep uvicorn from binding.
    tasks = [
        asyncio.create_task(_run_startup(service)),
        asyncio.create_task(service.maintenance_loop()),
        asyncio.create_task(service.scheduled_effects_loop()),
        asyncio.create_task(service.work_loop()),
    ]

    poller: PollingService | None = None
    if config.pull_based is not None:
        polled = _polled_repositories(config)

        class PollSink:
            async def head_changed(
                self, repo: PolledRepository, branch: str, commit_sha: str
            ) -> None:
                await service.submit(
                    ChangeRequest(
                        forge="pull_based",
                        forge_repo_id=repo.name,
                        branch=branch,
                        commit_sha=commit_sha,
                    )
                )

        poller = PollingService(polled, PollSink(), config.pull_based.poll_spread)
        poller.start()

    servers = [uvicorn.Server(cfg) for cfg in _uvicorn_configs(config, app)]
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
        # Drop connections without waiting: a clean close would block
        # shutdown on whatever the cancelled tasks still hold.
        service.pool.terminate()
