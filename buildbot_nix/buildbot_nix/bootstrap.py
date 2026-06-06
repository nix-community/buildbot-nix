"""Process bootstrap: resolve configuration into a wired EngineService
and run it — database pool, migrations, forge clients, auth providers,
web app, and the uvicorn server.
"""

from __future__ import annotations

import asyncio
import logging
import os
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
from .db import BuildDB
from .executor import BuildSettings, FairScheduler, NixBuildExecutor
from .failed_builds import PostgresFailedBuildCache
from .forge import (
    GiteaClient,
    GiteaFetchCredentialsProvider,
    GitHubAppClient,
    GitHubFetchCredentialsProvider,
)
from .forge_tokens import ForgeTokenStore
from .gitea_hooks import GiteaWebhookSecrets
from .gitrepo import (
    CredentialsProvider,
    RepoManager,
)
from .migrations import apply_migrations
from .nix_eval import CgroupLimiter, EvalRunner
from .orchestrator import Orchestrator
from .polling import PolledRepository, PollingService
from .projects import ProjectStore
from .recovery import (
    fail_interrupted_eval,
    find_unfinished_builds,
    settle_already_built,
)
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
    create_webhook_router,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from .config import EngineConfig

from .service import (
    EngineService,
    PullBasedCredentialsProvider,
    resolve_credential_path,
)

logger = logging.getLogger(__name__)


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
