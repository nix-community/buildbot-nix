"""Tests for sessions, key rotation, CSRF, authz rules, OAuth flow."""

# ruff: noqa: PLR2004 (literal values in test assertions are fine)

from __future__ import annotations

import asyncio
import base64
from typing import TYPE_CHECKING

import httpx
from fastapi import FastAPI

from buildbot_nix.auth import (
    AuthzConfig,
    OAuthProvider,
    SessionSigner,
    User,
    can_control_build,
    gitea_oauth,
    github_oauth,
    is_admin,
    load_signing_keys,
    oidc_provider,
    rotate_signing_key,
)
from buildbot_nix.web.auth_routes import (
    SESSION_COOKIE,
    create_auth_router,
)

from .support import cookie_header

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

ALICE = User(provider="github", username="alice")
BOB = User(provider="gitea", username="alice")  # same name, other forge


class DictVault:
    """In-memory TokenVault for tests without a database."""

    def __init__(self) -> None:
        self.tokens: dict[str, str] = {}

    async def save(self, session_id: str, token: str, lifetime: int) -> None:  # noqa: ARG002
        self.tokens[session_id] = token

    async def get(self, session_id: str) -> str | None:
        return self.tokens.get(session_id)

    async def delete(self, session_id: str) -> None:
        self.tokens.pop(session_id, None)


def test_session_roundtrip() -> None:
    signer = SessionSigner([b"k1"])
    token = signer.session_for(ALICE)
    assert signer.user_from(token) == ALICE
    assert signer.user_from(None) is None
    assert signer.user_from("garbage") is None
    assert signer.user_from(token + "x") is None


def test_session_carries_avatar() -> None:
    signer = SessionSigner([b"k1"])
    user = User(
        provider="gitea",
        username="bob",
        avatar_url="https://gitea.test/avatars/123",
    )
    roundtripped = signer.user_from(signer.session_for(user))
    assert roundtripped is not None
    assert roundtripped.avatar_url == "https://gitea.test/avatars/123"


def test_session_expiry() -> None:
    signer = SessionSigner([b"k1"], lifetime=-1)
    token = signer.session_for(ALICE)
    assert SessionSigner([b"k1"]).user_from(token) is None


def test_two_key_rotation() -> None:
    old = SessionSigner([b"old"])
    token = old.session_for(ALICE)
    rotated = SessionSigner([b"new", b"old"])
    assert rotated.user_from(token) == ALICE
    # Fully dropped key: invalid.
    assert SessionSigner([b"new"]).user_from(token) is None


def test_signing_key_files(tmp_path: Path) -> None:
    keys1 = load_signing_keys(tmp_path)
    assert len(keys1) == 1
    keys2 = load_signing_keys(tmp_path)
    assert keys1 == keys2  # stable across restarts
    rotate_signing_key(tmp_path)
    keys3 = load_signing_keys(tmp_path)
    assert len(keys3) == 2
    assert keys3[1] == keys1[0]  # previous key still verifies
    # LoadCredential override wins.
    override = tmp_path / "override-key"
    override.write_bytes(b"operator-key\n")
    assert load_signing_keys(tmp_path, override) == [b"operator-key"]


def test_signing_key_file_permissions(tmp_path: Path) -> None:
    load_signing_keys(tmp_path)
    assert (tmp_path / "session-key").stat().st_mode & 0o777 == 0o600
    rotate_signing_key(tmp_path)
    # The previous key still verifies sessions; must not be world-readable.
    assert (tmp_path / "session-key.previous").stat().st_mode & 0o777 == 0o600


def test_unqualified_admin_entry_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="buildbot_nix.auth"):
        AuthzConfig(admins=["alice"])
    assert "not provider-qualified" in caplog.text
    caplog.clear()
    with caplog.at_level("WARNING", logger="buildbot_nix.auth"):
        AuthzConfig(admins=["github:alice"])
    assert caplog.text == ""


def test_provider_qualified_admins() -> None:
    config = AuthzConfig(admins=["github:alice"])
    assert is_admin(ALICE, config)
    assert not is_admin(BOB, config)  # same username, different provider
    assert not is_admin(None, config)


def test_can_control_build() -> None:
    config = AuthzConfig(admins=["github:admin"])
    admin = User(provider="github", username="admin")
    assert can_control_build(admin, config)
    # PR author rule: exact forge identity match only.
    assert can_control_build(ALICE, config, build_pr_author="github:alice")
    assert not can_control_build(BOB, config, build_pr_author="github:alice")
    assert not can_control_build(ALICE, config, build_pr_author=None)
    assert not can_control_build(None, config)
    # allowUnauthenticatedControl opens everything.
    open_config = AuthzConfig(admins=[], allow_unauthenticated_control=True)
    assert can_control_build(None, open_config)


def test_oauth_login_flow() -> None:
    provider = github_oauth("cid", "csecret")
    vault = DictVault()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/oauth/access_token":
            assert b"code=thecode" in request.content
            return httpx.Response(200, json={"access_token": "at"})
        if request.url.path == "/user":
            assert request.headers["Authorization"] == "Bearer at"
            return httpx.Response(
                200,
                json={
                    "login": "alice",
                    "avatar_url": "https://avatars.test/alice",
                },
            )
        return httpx.Response(404)

    app = FastAPI()
    signer = SessionSigner([b"k"])
    app.include_router(
        create_auth_router(
            {"github": provider},
            signer,
            "https://ci.test",
            vault,
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
    )

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://ci.test"
        ) as client:
            login = await client.get("/login/github")
            assert login.status_code == 307
            assert "github.com/login/oauth/authorize" in login.headers["location"]
            assert "client_id=cid" in login.headers["location"]
            state = login.cookies["buildbot_nix_oauth_state"]

            callback = await client.get(
                f"/auth/github/callback?code=thecode&state={state}",
                headers=cookie_header({"buildbot_nix_oauth_state": state}),
            )
            assert callback.status_code == 307
            session = callback.cookies[SESSION_COOKIE]
            session_user = signer.user_from(session)
            assert session_user is not None
            assert session_user.qualified == ALICE.qualified
            assert session_user.avatar_url == "https://avatars.test/alice"
            # The forge token lives server-side; the cookie only carries
            # an opaque session id.
            payload = signer.verify(session)
            assert payload is not None
            assert "token" not in payload
            session_id = signer.session_id_from(session)
            assert session_id is not None
            assert vault.tokens[session_id] == "at"

            # Bad state rejected.
            bad = await client.get(
                "/auth/github/callback?code=x&state=wrong",
                headers=cookie_header({"buildbot_nix_oauth_state": state}),
            )
            assert bad.status_code == 403

            # User cancelled on the authorize page: no code, error param.
            cancelled = await client.get(
                f"/auth/github/callback?error=access_denied&state={state}",
                headers=cookie_header({"buildbot_nix_oauth_state": state}),
            )
            assert cancelled.status_code == 403

            # Logout requires same-origin (CSRF).
            cross = await client.post(
                "/logout", headers={"Origin": "https://evil.example.com"}
            )
            assert cross.status_code == 403
            ok = await client.post(
                "/logout",
                headers={"Origin": "https://ci.test"}
                | cookie_header({SESSION_COOKIE: session}),
            )
            assert ok.status_code == 303
            # Logout invalidates the server-side forge token.
            assert vault.tokens == {}

    asyncio.run(run())


def test_oauth_callback_handles_token_exchange_errors() -> None:
    """GitHub returns expired/reused-code errors as HTTP 200 with an
    error body; the callback must answer 403, not crash with a 500."""
    provider = github_oauth("cid", "csecret")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/oauth/access_token":
            if b"code=stale" in request.content:
                return httpx.Response(200, json={"error": "bad_verification_code"})
            return httpx.Response(200, json={"access_token": "at"})
        if request.url.path == "/user":
            # Userinfo body without the username field.
            return httpx.Response(200, json={})
        return httpx.Response(404)

    app = FastAPI()
    app.include_router(
        create_auth_router(
            {"github": provider},
            SessionSigner([b"k"]),
            "https://ci.test",
            DictVault(),
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
    )

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://ci.test"
        ) as client:
            response = await client.get(
                "/auth/github/callback?code=stale&state=s",
                headers=cookie_header({"buildbot_nix_oauth_state": "s"}),
            )
            assert response.status_code == 403
            assert "bad_verification_code" in response.text

            # Userinfo body missing the username field: also 403, not 500.
            response = await client.get(
                "/auth/github/callback?code=ok&state=s",
                headers=cookie_header({"buildbot_nix_oauth_state": "s"}),
            )
            assert response.status_code == 403
            assert "userinfo" in response.json()["detail"]

    asyncio.run(run())


def test_github_oauth_scope_and_enterprise_urls() -> None:
    default = github_oauth("cid", "cs")
    # "repo" is needed so /user/repos lists private repositories.
    assert "repo" in default.scope.split()
    assert default.authorize_url == "https://github.com/login/oauth/authorize"
    assert default.userinfo_url == "https://api.github.com/user"

    ghe = github_oauth("cid", "cs", "https://ghe.corp.example/api/v3")
    assert ghe.authorize_url == "https://ghe.corp.example/login/oauth/authorize"
    assert ghe.token_url == "https://ghe.corp.example/login/oauth/access_token"  # noqa: S105
    assert ghe.userinfo_url == "https://ghe.corp.example/api/v3/user"


def test_oidc_discovery() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": "https://id.example.com",
                    "authorization_endpoint": "https://id.example.com/auth",
                    "token_endpoint": "https://id.example.com/token",
                    "userinfo_endpoint": "https://id.example.com/userinfo",
                },
            )
        return httpx.Response(404)

    provider = asyncio.run(
        oidc_provider(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            "https://id.example.com/.well-known/openid-configuration",
            "cid",
            "cs",
            ["openid", "profile"],
        )
    )
    assert provider.provider_id == "oidc:id.example.com"
    assert provider.authorize_url == "https://id.example.com/auth"
    assert provider.scope == "openid profile"


def test_gitea_oauth_urls() -> None:
    provider = gitea_oauth("https://gitea.example.com/", "cid", "cs")
    assert provider.authorize_url == "https://gitea.example.com/login/oauth/authorize"
    assert provider.userinfo_url == "https://gitea.example.com/api/v1/user"
    # read:repository is needed so /api/v1/user/repos works with scoped tokens.
    assert "read:repository" in provider.scope.split()


def test_oidc_exchange_uses_basic_auth() -> None:
    """OIDC servers only have to support client_secret_basic (RFC
    6749 section 2.3.1); authelia rejects body credentials with 401."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            seen["authorization"] = request.headers.get("authorization", "")
            seen["body"] = request.content.decode()
            return httpx.Response(200, json={"access_token": "tok"})
        if request.url.path == "/userinfo":
            return httpx.Response(
                200, json={"preferred_username": "alice", "picture": None}
            )
        return httpx.Response(404)

    provider = OAuthProvider(
        name="oidc",
        authorize_url="https://id.example.com/auth",
        token_url="https://id.example.com/token",  # noqa: S106
        userinfo_url="https://id.example.com/userinfo",
        client_id="cid",
        client_secret="cs",  # noqa: S106
        scope="openid",
        username_field="preferred_username",
        provider_id="oidc:id.example.com",
        client_auth="basic",
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    user, _token = asyncio.run(
        provider.exchange_code(client, "code123", "https://ci/auth/oidc/callback")
    )
    assert user.username == "alice"
    expected = base64.b64encode(b"cid:cs").decode()
    assert seen["authorization"] == f"Basic {expected}"
    assert "client_secret" not in seen["body"]
