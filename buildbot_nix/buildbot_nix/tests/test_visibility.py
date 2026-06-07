"""Private-project visibility tests: anonymous and
unauthorized access on HTML, fragment, log, and SSE endpoints."""

# ruff: noqa: PLR2004 (literal values in test assertions are fine)

from __future__ import annotations

import asyncio
import secrets
import shutil
from typing import TYPE_CHECKING

import asyncpg
import httpx
import pytest

from buildbot_nix.api_tokens import ApiTokenStore
from buildbot_nix.auth import AuthzConfig, SessionSigner, User
from buildbot_nix.forge_tokens import ForgeTokenStore
from buildbot_nix.visibility import AccessCache, RepoAccess, VisibilityService
from buildbot_nix.web.app import create_app

from .support import cookie_header, ephemeral_postgres

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.skipif(
    shutil.which("initdb") is None, reason="postgresql not available"
)


class FakeFetcher:
    def __init__(
        self,
        grants: dict[str, frozenset[str]],
        admin_grants: dict[str, frozenset[str]] | None = None,
    ) -> None:
        self.grants = grants
        self.admin_grants = admin_grants or {}
        self.calls = 0

    async def repo_access(self, user: User, token: str) -> RepoAccess:
        self.calls += 1
        key = f"{user.qualified}:{token}"
        return RepoAccess(
            accessible=self.grants.get(key, frozenset()),
            admin=self.admin_grants.get(key, frozenset()),
        )


@pytest.fixture(scope="module")
def postgres_dsn(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    with ephemeral_postgres(tmp_path_factory, "vis") as dsn:
        asyncio.run(seed(dsn))
        yield dsn


async def seed(dsn: str) -> None:
    pool = await asyncpg.create_pool(dsn)
    try:
        for repo_id, name, private in [
            ("pub-1", "public", False),
            ("priv-1", "secret", True),
        ]:
            project_id = await pool.fetchval(
                """
                INSERT INTO projects (forge, forge_repo_id, owner, name,
                                      default_branch, url, private, enabled)
                VALUES ('github', $1, 'acme', $2, 'main', 'u', $3, TRUE)
                RETURNING id
                """,
                repo_id,
                name,
                private,
            )
            build_id = await pool.fetchval(
                """
                INSERT INTO builds (project_id, number, commit_sha, branch, status)
                VALUES ($1, 1, 'sha', 'main', 'succeeded') RETURNING id
                """,
                project_id,
            )
            await pool.execute(
                "INSERT INTO build_attributes (build_id, attr, system, status) "
                "VALUES ($1, 'a.x', 'x86_64-linux', 'succeeded')",
                build_id,
            )
    finally:
        await pool.close()


SIGNER = SessionSigner([b"test-key"])
FETCHER = FakeFetcher({"github:carol:tok-carol": frozenset({"github:priv-1"})})


@pytest.fixture(scope="module")
def harness(postgres_dsn: str) -> Iterator[tuple]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def setup() -> tuple:
        pool = await asyncpg.create_pool(postgres_dsn)
        visibility = VisibilityService(
            pool,
            AuthzConfig(admins=["github:root"]),
            fetcher=FETCHER,
            cache=AccessCache(ttl=3600),
        )
        app = create_app(pool)
        ctx = app.state.web_context
        ctx.signer = SIGNER
        ctx.visibility = visibility
        ctx.forge_tokens = ForgeTokenStore(pool)
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


def get(
    harness: tuple, url: str, user: User | None = None, token: str = ""
) -> httpx.Response:
    loop, client = harness
    cookies = {}
    if user is not None:
        session_id = None
        if token:
            # Forge tokens are stored server-side, keyed by session id.
            vault = client._transport.app.state.web_context.forge_tokens  # type: ignore[attr-defined]  # noqa: SLF001
            session_id = secrets.token_urlsafe(8)
            loop.run_until_complete(vault.save(session_id, token, 3600))
        cookies["buildbot_nix_session"] = SIGNER.session_for(user, session_id)
    return loop.run_until_complete(client.get(url, headers=cookie_header(cookies)))


CAROL = User(provider="github", username="carol")
MALLORY = User(provider="github", username="mallory")
ROOT = User(provider="github", username="root")


def test_anonymous_sees_public_only(harness: tuple) -> None:
    home = get(harness, "/")
    assert "acme/public" in home.text
    assert "secret" not in home.text  # name leak check
    assert get(harness, "/repos/github/acme/public").status_code == 200
    assert get(harness, "/repos/github/acme/secret").status_code == 404
    assert get(harness, "/repos/github/acme/secret/builds/1").status_code == 404
    # Log + SSE endpoints hidden too.
    assert (
        get(harness, "/repos/github/acme/secret/builds/1/logs/a.x").status_code == 404
    )
    assert (
        get(harness, "/repos/github/acme/secret/builds/1/logs/a.x/stream").status_code
        == 404
    )
    assert (
        get(harness, "/repos/github/acme/secret/builds/1/attributes").status_code == 404
    )


def test_unauthorized_user_sees_public_only(harness: tuple) -> None:
    assert (
        get(harness, "/repos/github/acme/secret", MALLORY, "tok-mallory").status_code
        == 404
    )
    home = get(harness, "/", MALLORY, "tok-mallory")
    assert "secret" not in home.text


def test_authorized_user_sees_private(harness: tuple) -> None:
    assert (
        get(harness, "/repos/github/acme/secret", CAROL, "tok-carol").status_code == 200
    )
    home = get(harness, "/", CAROL, "tok-carol")
    assert "acme/secret" in home.text


def test_admin_sees_everything(harness: tuple) -> None:
    assert get(harness, "/repos/github/acme/secret", ROOT).status_code == 200


def test_admin_api_token_sees_private(harness: tuple) -> None:
    # Bearer tokens carry the owner's identity: an admin token may
    # read private projects.
    loop, client = harness
    ctx = client._transport.app.state.web_context  # noqa: SLF001
    ctx.token_store = ApiTokenStore(ctx.pool)
    token = loop.run_until_complete(ctx.token_store.create(ROOT, "admin-script"))
    response = loop.run_until_complete(
        client.get(
            "/repos/github/acme/secret", headers={"Authorization": f"Bearer {token}"}
        )
    )
    assert response.status_code == 200


class FailingFetcher:
    def __init__(self) -> None:
        self.fail = True
        self.calls = 0

    async def repo_access(
        self,
        user: User,  # noqa: ARG002
        token: str,  # noqa: ARG002
    ) -> RepoAccess:
        self.calls += 1
        if self.fail:
            msg = "forge down"
            raise httpx.ConnectError(msg)
        return RepoAccess(frozenset({"github:priv-1"}), frozenset())


def test_forge_repo_admins_can_toggle_their_repos(harness: tuple) -> None:
    loop, client = harness
    ctx = client._transport.app.state.web_context  # noqa: SLF001
    fetcher = FakeFetcher(
        grants={"github:carol:tok-carol": frozenset({"github:priv-1"})},
        admin_grants={"github:carol:tok-carol": frozenset({"github:priv-1"})},
    )
    service = VisibilityService(
        ctx.pool,
        AuthzConfig(admins=["github:root"]),
        fetcher=fetcher,
        cache=AccessCache(ttl=3600),
    )

    async def run() -> None:
        # Instance admin: everything (None).
        assert await service.toggleable_repo_ids(ROOT) is None
        # Repo admin: exactly their repo.
        ids = await service.toggleable_repo_ids(CAROL, "tok-carol")
        assert ids is not None
        assert len(ids) == 1
        # Access without forge-admin permission: nothing.
        assert await service.toggleable_repo_ids(MALLORY, "tok-mallory") == []
        # Anonymous: nothing.
        assert await service.toggleable_repo_ids(None) == []

    loop.run_until_complete(run())


def test_fetch_errors_are_not_cached(harness: tuple) -> None:
    """A transient forge failure must not poison the access cache:
    the next request retries and sees the private project again."""
    loop, client = harness
    ctx = client._transport.app.state.web_context  # noqa: SLF001
    fetcher = FailingFetcher()
    service = VisibilityService(
        ctx.pool,
        AuthzConfig(admins=[]),
        fetcher=fetcher,
        cache=AccessCache(ttl=3600),
    )

    async def run() -> None:
        # While the forge errors: public-only, nothing cached.
        first = await service.visible_repo_ids(CAROL, "tok-carol")
        assert first is not None
        assert len(first) == 1
        fetcher.fail = False
        second = await service.visible_repo_ids(CAROL, "tok-carol")
        assert second is not None
        assert len(second) == 2
        assert fetcher.calls == 2

    loop.run_until_complete(run())


def test_access_cache_used(harness: tuple) -> None:
    calls_before = FETCHER.calls
    get(harness, "/repos/github/acme/secret", CAROL, "tok-carol")
    get(harness, "/repos/github/acme/secret", CAROL, "tok-carol")
    # TTL cache: at most one fetch for repeated requests.
    assert FETCHER.calls <= calls_before + 1


def test_cache_negative_results() -> None:
    cache = AccessCache(ttl=60)
    empty = RepoAccess(frozenset(), frozenset())
    assert cache.get("u") is None
    cache.set("u", empty)
    assert cache.get("u") == empty
    cache.invalidate("u")
    assert cache.get("u") is None


def test_metrics_unauthenticated_no_private_names(harness: tuple) -> None:
    response = get(harness, "/metrics")
    assert response.status_code == 200
    assert "buildbot_nix_builds_total" in response.text
    assert "buildbot_nix_queue_depth" in response.text
    assert "buildbot_nix_projects" in response.text
    # No private repo names leak into metrics.
    assert "secret" not in response.text
