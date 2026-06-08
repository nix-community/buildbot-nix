"""Private-project visibility tests: anonymous and
unauthorized access on HTML, fragment, log, and SSE endpoints."""

# ruff: noqa: PLR2004 (literal values in test assertions are fine)

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import asyncpg
import httpx
import pytest

from buildbot_nix.api_tokens import ApiTokenStore
from buildbot_nix.auth import AuthzConfig, User
from buildbot_nix.forge_tokens import ForgeTokenStore
from buildbot_nix.visibility import AccessCache, RepoAccess, VisibilityService

from .support import WebHarness, insert_build, insert_project, web_harness

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI


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
def postgres_dsn(postgres_dsn: str) -> str:
    asyncio.run(seed(postgres_dsn))
    return postgres_dsn


async def seed(dsn: str) -> None:
    pool = await asyncpg.create_pool(dsn)
    try:
        for repo_id, name, private in [
            ("pub-1", "public", False),
            ("priv-1", "secret", True),
        ]:
            project_id = await insert_project(
                pool, name, forge_repo_id=repo_id, private=private
            )
            build_id = await insert_build(pool, project_id, status="succeeded")
            await pool.execute(
                "INSERT INTO build_attributes (build_id, attr, system, status) "
                "VALUES ($1, 'a.x', 'x86_64-linux', 'succeeded')",
                build_id,
            )
    finally:
        await pool.close()


FETCHER = FakeFetcher({"github:carol:tok-carol": frozenset({"github:priv-1"})})


@pytest.fixture(scope="module")
def harness(postgres_dsn: str) -> Iterator[WebHarness]:
    def configure(app: FastAPI, pool: asyncpg.Pool) -> None:
        ctx = app.state.web_context
        ctx.visibility = VisibilityService(
            pool,
            AuthzConfig(admins=["github:root"]),
            fetcher=FETCHER,
            cache=AccessCache(ttl=3600),
        )
        ctx.forge_tokens = ForgeTokenStore(pool)

    with web_harness(postgres_dsn, configure=configure) as h:
        yield h


CAROL = User(provider="github", username="carol")
MALLORY = User(provider="github", username="mallory")
ROOT = User(provider="github", username="root")


def test_anonymous_sees_public_only(harness: WebHarness) -> None:
    home = harness.get("/")
    assert "acme/public" in home.text
    assert "secret" not in home.text  # name leak check
    assert harness.get("/repos/github/acme/public").status_code == 200
    assert harness.get("/repos/github/acme/secret").status_code == 404
    assert harness.get("/repos/github/acme/secret/builds/1").status_code == 404
    # Log + SSE endpoints hidden too.
    assert harness.get("/repos/github/acme/secret/builds/1/logs/a.x").status_code == 404
    assert (
        harness.get("/repos/github/acme/secret/builds/1/logs/a.x/stream").status_code
        == 404
    )
    assert (
        harness.get("/repos/github/acme/secret/builds/1/attributes").status_code == 404
    )


def test_unauthorized_user_sees_public_only(harness: WebHarness) -> None:
    assert (
        harness.get("/repos/github/acme/secret", MALLORY, "tok-mallory").status_code
        == 404
    )
    home = harness.get("/", MALLORY, "tok-mallory")
    assert "secret" not in home.text


def test_authorized_user_sees_private(harness: WebHarness) -> None:
    assert (
        harness.get("/repos/github/acme/secret", CAROL, "tok-carol").status_code == 200
    )
    home = harness.get("/", CAROL, "tok-carol")
    assert "acme/secret" in home.text


def test_admin_sees_everything(harness: WebHarness) -> None:
    assert harness.get("/repos/github/acme/secret", ROOT).status_code == 200


def test_admin_api_token_sees_private(harness: WebHarness) -> None:
    # Bearer tokens carry the owner's identity: an admin token may
    # read private projects.
    ctx = harness.ctx
    ctx.token_store = ApiTokenStore(ctx.pool)
    token = harness.run(ctx.token_store.create(ROOT, "admin-script"))
    response = harness.run(
        harness.http.get(
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


def test_forge_repo_admins_can_toggle_their_repos(harness: WebHarness) -> None:
    ctx = harness.ctx
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

    harness.run(run())


def test_fetch_errors_are_not_cached(harness: WebHarness) -> None:
    """A transient forge failure must not poison the access cache:
    the next request retries and sees the private project again."""
    ctx = harness.ctx
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

    harness.run(run())


def test_access_cache_used(harness: WebHarness) -> None:
    calls_before = FETCHER.calls
    harness.get("/repos/github/acme/secret", CAROL, "tok-carol")
    harness.get("/repos/github/acme/secret", CAROL, "tok-carol")
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


def test_metrics_unauthenticated_no_private_names(harness: WebHarness) -> None:
    response = harness.get("/metrics")
    assert response.status_code == 200
    assert "buildbot_nix_builds_total" in response.text
    assert "buildbot_nix_queue_depth" in response.text
    assert "buildbot_nix_projects" in response.text
    # No private repo names leak into metrics.
    assert "secret" not in response.text


def test_configured_viewers_see_private(harness: WebHarness) -> None:
    """privateRepoViewers grants visibility to users without forge
    tokens (e.g. OIDC logins)."""
    ctx = harness.ctx
    assert ctx.visibility is not None
    saved = ctx.visibility.authz
    ctx.visibility.authz = AuthzConfig(
        admins=["github:root"],
        private_repo_viewers={
            "github:acme/secret": [
                "oidc:idp:*",
                "oidc:idp:group:auditors",
            ]
        },
    )
    try:
        idp_user = User(provider="oidc:idp", username="dora")
        assert harness.get("/repos/github/acme/secret", idp_user).status_code == 200
        auditor = User(provider="oidc:other", username="erik", groups=("auditors",))
        # Different provider: neither rule matches.
        assert harness.get("/repos/github/acme/secret", auditor).status_code == 404
        # Anonymous stays out.
        assert harness.get("/repos/github/acme/secret").status_code == 404
    finally:
        ctx.visibility.authz = saved


def test_api_token_inherits_login_groups(harness: WebHarness) -> None:
    """Tokens snapshot the creator's groups, so group-granted viewers
    keep their visibility over the API."""
    ctx = harness.ctx
    assert ctx.visibility is not None
    ctx.token_store = ApiTokenStore(ctx.pool)
    saved = ctx.visibility.authz
    ctx.visibility.authz = AuthzConfig(
        admins=["github:root"],
        private_repo_viewers={"*": ["oidc:idp:group:auditors"]},
    )
    try:
        creator = User(provider="oidc:idp", username="erika", groups=("auditors",))
        token = harness.run(ctx.token_store.create(creator, "t1"))
        restored = harness.run(ctx.token_store.authenticate(token))
        assert restored is not None
        assert restored.groups == ("auditors",)

        response = harness.run(
            harness.http.get(
                "/api/repos/github/acme/secret/builds",
                headers={"Authorization": f"Bearer {token}"},
            )
        )
        assert response.status_code == 200
    finally:
        ctx.visibility.authz = saved
