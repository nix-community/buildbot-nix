"""Control endpoint tests: authz gating, CSRF, admin toggle."""

# ruff: noqa: PLR2004 (literal values in test assertions are fine)

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg
import httpx
import pytest

from buildbot_nix.api_tokens import ApiTokenStore
from buildbot_nix.auth import AuthzConfig, SessionSigner, User
from buildbot_nix.bootstrap import build_service
from buildbot_nix.config import EngineConfig
from buildbot_nix.forge_tokens import ForgeTokenStore
from buildbot_nix.migrations import apply_migrations
from buildbot_nix.web.app import create_app
from buildbot_nix.web.control_routes import (
    create_control_api_router,
    create_control_router,
)
from buildbot_nix.web.token_routes import create_token_router

from .support import cookie_header

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.skipif(
    shutil.which("initdb") is None, reason="postgresql not available"
)

SIGNER = SessionSigner([b"ctl-key"])
AUTHZ = AuthzConfig(admins=["github:root"])

ROOT = User(provider="github", username="root")
ALICE = User(provider="github", username="alice")  # PR author
MALLORY = User(provider="gitea", username="alice")  # same name, wrong forge


@dataclass
class FakeBackend:
    restarted: list[int] = field(default_factory=list)
    attr_restarts: list[tuple[int, str]] = field(default_factory=list)
    cancelled: list[int] = field(default_factory=list)
    attr_cancels: list[tuple[int, str]] = field(default_factory=list)
    refreshes: int = 0

    async def restart_build(self, build_id: int) -> None:
        self.restarted.append(build_id)

    async def restart_attribute(self, build_id: int, attr: str) -> None:
        self.attr_restarts.append((build_id, attr))

    async def cancel_build(self, build_id: int) -> None:
        self.cancelled.append(build_id)

    async def cancel_attribute(self, build_id: int, attr: str) -> None:
        self.attr_cancels.append((build_id, attr))

    async def refresh_projects(self) -> None:
        self.refreshes += 1


@pytest.fixture(scope="module")
def postgres_dsn(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    datadir = tmp_path_factory.mktemp("pgdata")
    sockdir = tmp_path_factory.mktemp("pgsock")
    subprocess.run(  # noqa: S603
        ["initdb", "-D", str(datadir), "-U", "test", "--auth=trust"],
        check=True,
        capture_output=True,
    )
    proc = subprocess.Popen(  # noqa: S603
        ["postgres", "-D", str(datadir), "-k", str(sockdir), "-c", "listen_addresses="],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while not Path(sockdir, ".s.PGSQL.5432").exists():
            if time.monotonic() > deadline:
                msg = "postgres did not start"
                raise RuntimeError(msg)
            time.sleep(0.1)
        subprocess.run(  # noqa: S603
            ["createdb", "-h", str(sockdir), "-U", "test", "ctl"],
            check=True,
            capture_output=True,
        )
        dsn = f"postgresql://test@/ctl?host={sockdir}"
        asyncio.run(apply_migrations(dsn))
        asyncio.run(seed(dsn))
        yield dsn
    finally:
        proc.terminate()
        proc.wait()


async def seed(dsn: str) -> None:
    pool = await asyncpg.create_pool(dsn)
    try:
        project_id = await pool.fetchval(
            """
            INSERT INTO projects (forge, forge_repo_id, owner, name,
                                  default_branch, url, enabled)
            VALUES ('github', 'ctl-1', 'acme', 'widget', 'main', 'u', TRUE)
            RETURNING id
            """
        )
        build_id = await pool.fetchval(
            """
            INSERT INTO builds (project_id, number, commit_sha, branch, status,
                                pr_number, pr_author)
            VALUES ($1, 1, 'sha', 'main', 'failed', 7, 'github:alice')
            RETURNING id
            """,
            project_id,
        )
        await pool.execute(
            """
            INSERT INTO build_attributes (build_id, attr, system, status) VALUES
              ($1, 'good', 'x', 'succeeded'),
              ($1, 'bad1', 'x', 'failed'),
              ($1, 'bad2', 'x', 'dependency_failed')
            """,
            build_id,
        )
    finally:
        await pool.close()


BACKEND = FakeBackend()


@pytest.fixture(scope="module")
def harness(postgres_dsn: str) -> Iterator[tuple]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def setup() -> tuple:
        pool = await asyncpg.create_pool(postgres_dsn)
        app = create_app(pool)
        ctx = app.state.web_context
        ctx.signer = SIGNER
        app.include_router(create_control_router(ctx, BACKEND, AUTHZ, "http://test"))
        app.include_router(
            create_control_api_router(ctx, BACKEND, AUTHZ, "http://test")
        )
        ctx.token_store = ApiTokenStore(pool)
        app.include_router(create_token_router(ctx, ctx.token_store, "http://test"))
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        return pool, client

    pool, client = loop.run_until_complete(setup())
    yield loop, client
    loop.run_until_complete(client.aclose())
    loop.run_until_complete(pool.close())
    asyncio.set_event_loop(None)
    loop.close()


def post(
    harness: tuple,
    url: str,
    user: User | None = None,
    origin: str = "http://test",
    data: dict[str, str] | None = None,
) -> httpx.Response:
    loop, client = harness
    cookies = {}
    if user is not None:
        cookies["buildbot_nix_session"] = SIGNER.session_for(user)
    headers = {"Origin": origin} if origin else {}
    return loop.run_until_complete(
        client.post(url, headers=headers | cookie_header(cookies), data=data)
    )


def test_anonymous_cannot_control(harness: tuple) -> None:
    response = post(harness, "/repos/github/acme/widget/builds/1/restart")
    assert response.status_code == 403
    assert BACKEND.restarted == []


def test_admin_can_restart_and_cancel(harness: tuple) -> None:
    assert (
        post(harness, "/repos/github/acme/widget/builds/1/restart", ROOT).status_code
        == 303
    )
    assert len(BACKEND.restarted) == 1
    assert (
        post(harness, "/repos/github/acme/widget/builds/1/cancel", ROOT).status_code
        == 303
    )
    assert len(BACKEND.cancelled) == 1
    assert (
        post(
            harness,
            "/repos/github/acme/widget/builds/1/attrs/x86_64-linux.ok/cancel",
            ROOT,
        ).status_code
        == 303
    )
    assert BACKEND.attr_cancels == [(1, "x86_64-linux.ok")]


def test_pr_author_can_control_own_pr(harness: tuple) -> None:
    assert (
        post(harness, "/repos/github/acme/widget/builds/1/restart", ALICE).status_code
        == 303
    )
    # Same username on another forge: rejected.
    assert (
        post(harness, "/repos/github/acme/widget/builds/1/restart", MALLORY).status_code
        == 403
    )


def test_csrf_cross_origin_rejected(harness: tuple) -> None:
    response = post(
        harness,
        "/repos/github/acme/widget/builds/1/restart",
        ROOT,
        origin="https://evil.example.com",
    )
    assert response.status_code == 403


def test_restart_single_attribute(harness: tuple) -> None:
    BACKEND.attr_restarts.clear()
    assert (
        post(
            harness, "/repos/github/acme/widget/builds/1/attrs/bad1/restart", ROOT
        ).status_code
        == 303
    )
    assert [a for _, a in BACKEND.attr_restarts] == ["bad1"]


def test_rebuild_all_failed(harness: tuple) -> None:
    BACKEND.attr_restarts.clear()
    assert (
        post(
            harness, "/repos/github/acme/widget/builds/1/rebuild-failed", ROOT
        ).status_code
        == 303
    )
    assert sorted(a for _, a in BACKEND.attr_restarts) == ["bad1", "bad2"]


def project_enabled(harness: tuple) -> bool:
    loop, client = harness
    pool = client._transport.app.state.web_context.pool  # type: ignore[attr-defined]  # noqa: SLF001
    return loop.run_until_complete(
        pool.fetchval("SELECT enabled FROM projects WHERE forge_repo_id = 'ctl-1'")
    )


def test_admin_project_toggle(harness: tuple) -> None:
    # Non-admin rejected.
    assert post(harness, "/admin/repos/1/toggle", ALICE).status_code == 403

    before = project_enabled(harness)
    assert post(harness, "/admin/repos/1/toggle", ROOT).status_code == 303
    assert project_enabled(harness) != before

    # The dashboard filter survives a toggle.
    response = post(harness, "/admin/repos/1/toggle", ROOT, data={"q": "wid get"})
    assert response.status_code == 303
    assert response.headers["location"] == "/?q=wid%20get"


def test_repo_refresh(harness: tuple) -> None:
    # Anonymous rejected: discovery hits forge APIs.
    assert post(harness, "/admin/repos/refresh").status_code == 403
    assert BACKEND.refreshes == 0

    # Any logged-in user may refresh, not only admins.
    response = post(harness, "/admin/repos/refresh", ALICE, data={"q": "wid get"})
    assert response.status_code == 303
    assert response.headers["location"] == "/?q=wid%20get"
    assert BACKEND.refreshes == 1

    # Cross-origin rejected.
    assert (
        post(harness, "/admin/repos/refresh", ROOT, origin="http://evil").status_code
        == 403
    )
    assert BACKEND.refreshes == 1


def test_api_enable_disable(harness: tuple) -> None:
    assert (
        post(harness, "/api/repos/github/acme/widget/disable", ALICE).status_code == 403
    )

    response = post(harness, "/api/repos/github/acme/widget/disable", ROOT)
    assert response.status_code == 200
    assert response.json() == {"owner": "acme", "name": "widget", "enabled": False}
    assert project_enabled(harness) is False
    # Idempotent: repeating is fine.
    assert (
        post(harness, "/api/repos/github/acme/widget/disable", ROOT).status_code == 200
    )
    assert (
        post(harness, "/api/repos/github/acme/widget/enable", ROOT).status_code == 200
    )
    assert project_enabled(harness) is True
    assert post(harness, "/api/repos/github/acme/nope/enable", ROOT).status_code == 404


def test_api_restart_and_cancel(harness: tuple) -> None:
    BACKEND.restarted.clear()
    BACKEND.cancelled.clear()

    assert (
        post(harness, "/api/repos/github/acme/widget/builds/1/restart").status_code
        == 403
    )
    assert BACKEND.restarted == []

    response = post(harness, "/api/repos/github/acme/widget/builds/1/restart", ROOT)
    assert response.status_code == 200
    assert response.json() == {"number": 1, "action": "restart"}
    assert len(BACKEND.restarted) == 1

    response = post(harness, "/api/repos/github/acme/widget/builds/1/cancel", ROOT)
    assert response.status_code == 200
    assert response.json() == {"number": 1, "action": "cancel"}
    assert len(BACKEND.cancelled) == 1

    assert (
        post(
            harness, "/api/repos/github/acme/widget/builds/99/restart", ROOT
        ).status_code
        == 404
    )


# --- personal API tokens ---------------------------------------------


def test_api_token_lifecycle(harness: tuple) -> None:
    loop, client = harness
    pool = client._transport.app.state.web_context.pool  # type: ignore[attr-defined]  # noqa: SLF001
    store = ApiTokenStore(pool)

    async def run() -> None:
        token = await store.create(ALICE, "laptop")
        assert token.startswith("bnix_")
        # Stored only as hash.
        stored = await pool.fetchval(
            "SELECT token_hash FROM api_tokens WHERE name = 'laptop'"
        )
        assert token not in stored
        # Authenticates as owner.
        assert await store.authenticate(token) == ALICE
        assert await store.authenticate("bnix_wrong") is None
        assert await store.authenticate("malformed") is None
        # Expired token rejected.
        expired = await store.create(
            ALICE, "old", expires_at=datetime.now(tz=UTC) - timedelta(days=1)
        )
        assert await store.authenticate(expired) is None
        # Revocation: immediate, owner-only.
        tokens = await store.list_for(ALICE)
        laptop = next(t for t in tokens if t.name == "laptop")
        assert not await store.revoke(ROOT, laptop.id)  # not the owner
        assert await store.revoke(ALICE, laptop.id)
        assert await store.authenticate(token) is None

    loop.run_until_complete(run())


def test_token_creation_with_expiry(harness: tuple) -> None:
    loop, client = harness

    def create(data: dict) -> httpx.Response:
        return loop.run_until_complete(
            client.post(
                "/settings/tokens",
                data=data,
                headers={"Origin": "http://test"}
                | cookie_header({"buildbot_nix_session": SIGNER.session_for(ALICE)}),
            )
        )

    assert create({"name": "ci", "expires_days": "7"}).status_code == 200
    store = client._transport.app.state.web_context.token_store  # type: ignore[attr-defined]  # noqa: SLF001
    tokens = loop.run_until_complete(store.list_for(ALICE))
    ci = next(t for t in tokens if t.name == "ci")
    delta = ci.expires_at - datetime.now(tz=UTC)
    assert timedelta(days=6) < delta <= timedelta(days=7)

    # Expiry stays optional.
    assert create({"name": "forever"}).status_code == 200
    tokens = loop.run_until_complete(store.list_for(ALICE))
    assert next(t for t in tokens if t.name == "forever").expires_at is None

    # Invalid values rejected.
    assert create({"name": "bad", "expires_days": "x"}).status_code == 400
    assert create({"name": "bad", "expires_days": "-1"}).status_code == 400
    assert create({"name": "bad", "expires_days": "999999999999"}).status_code == 400


def test_forge_token_store(harness: tuple) -> None:
    loop, client = harness
    pool = client._transport.app.state.web_context.pool  # type: ignore[attr-defined]  # noqa: SLF001
    store = ForgeTokenStore(pool)
    loop.run_until_complete(store.save("sid-1", "tok", 3600))
    assert loop.run_until_complete(store.get("sid-1")) == "tok"
    loop.run_until_complete(store.delete("sid-1"))
    assert loop.run_until_complete(store.get("sid-1")) is None
    # Expired tokens are not returned.
    loop.run_until_complete(store.save("sid-2", "tok2", -1))
    assert loop.run_until_complete(store.get("sid-2")) is None


def test_api_token_controls_build(harness: tuple) -> None:
    loop, client = harness
    ctx = client._transport.app.state.web_context  # type: ignore[attr-defined]  # noqa: SLF001
    store = ApiTokenStore(ctx.pool)
    ctx.token_store = store

    token = loop.run_until_complete(store.create(ALICE, "api"))
    BACKEND.restarted.clear()
    response = loop.run_until_complete(
        client.post(
            "/repos/github/acme/widget/builds/1/restart",
            headers={"Authorization": f"Bearer {token}"},
        )
    )
    assert response.status_code == 303  # PR author via token
    assert len(BACKEND.restarted) == 1

    bad = loop.run_until_complete(
        client.post(
            "/repos/github/acme/widget/builds/1/restart",
            headers={"Authorization": "Bearer bnix_invalid"},
        )
    )
    assert bad.status_code == 403


# --- service composition smoke test -----------------------------------


def test_build_service_composition(postgres_dsn: str, tmp_path: Path) -> None:
    async def run() -> None:
        config = EngineConfig(
            db_url=postgres_dsn,
            build_systems=["x86_64-linux"],
            domain="ci.test",
            url="http://ci.test",
            state_dir=tmp_path / "state",
        )
        service, app = await build_service(config)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://ci.test"
            ) as client:
                assert (await client.get("/health")).text == "ok"
                assert (await client.get("/")).status_code == 200
                assert (await client.get("/metrics")).status_code == 200
                # Webhooks 404 without forge configuration.
                assert (
                    await client.post("/webhooks/github", content=b"{}")
                ).status_code == 404
                # Control endpoints present and gated.
                assert (
                    await client.post("/repos/github/acme/widget/builds/1/cancel")
                ).status_code == 403
        finally:
            await service.pool.close()

    asyncio.run(run())


def test_restart_clears_failed_cache_and_guards_running(
    postgres_dsn: str, tmp_path: Path
) -> None:
    async def run() -> None:
        config = EngineConfig(
            db_url=postgres_dsn,
            build_systems=["x86_64-linux"],
            domain="ci.test",
            url="http://ci.test",
            state_dir=tmp_path / "state",
        )
        service, _app = await build_service(config)
        pool = service.pool
        try:
            project_id = await pool.fetchval(
                "INSERT INTO projects (forge, forge_repo_id, owner, name, url, "
                "default_branch, enabled) VALUES "
                "('github', '77', 'acme', 'widget', 'http://x', 'main', TRUE) "
                "RETURNING id"
            )
            build_id = await pool.fetchval(
                "INSERT INTO builds (project_id, number, branch, commit_sha, "
                "tree_hash, status) VALUES ($1, 1, 'main', 'c1', 't1', 'failed') "
                "RETURNING id",
                project_id,
            )
            await pool.execute(
                "INSERT INTO build_attributes (build_id, attr, system, drv_path, "
                "status) VALUES ($1, 'x86_64-linux.a', 'x86_64-linux', "
                "'/nix/store/a.drv', 'failed')",
                build_id,
            )
            await pool.execute(
                "INSERT INTO failed_builds (derivation, timestamp, url) "
                "VALUES ('/nix/store/a.drv', 1, 'http://old')"
            )

            # Running build: restart refuses, cache row stays.
            service.orchestrator.cancel_events[build_id] = asyncio.Event()
            await service.restart_build(build_id)
            assert await pool.fetchval("SELECT count(*) FROM failed_builds") == 1
            assert (
                await pool.fetchval(
                    "SELECT status FROM build_attributes WHERE build_id = $1",
                    build_id,
                )
                == "failed"
            )

            # Not running: cache cleared, attributes reset.
            del service.orchestrator.cancel_events[build_id]
            await service.restart_build(build_id)
            assert await pool.fetchval("SELECT count(*) FROM failed_builds") == 0
            assert (
                await pool.fetchval(
                    "SELECT status FROM build_attributes WHERE build_id = $1",
                    build_id,
                )
                == "pending"
            )
        finally:
            await pool.close()

    asyncio.run(run())
