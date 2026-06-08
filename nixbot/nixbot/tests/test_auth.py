"""Tests for sessions, key rotation, CSRF, authz rules, OAuth flow."""

# ruff: noqa: PLR2004 (literal values in test assertions are fine)

from __future__ import annotations

import asyncio
import base64
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI

from nixbot.auth import (
    AuthzConfig,
    OAuthError,
    OAuthProvider,
    SessionSigner,
    User,
    can_control_build,
    can_view_private,
    gitea_oauth,
    github_oauth,
    is_admin,
    load_signing_keys,
    matches_rule,
    oidc_provider,
    relevant_groups,
    rotate_signing_key,
)
from nixbot.web.auth_routes import (
    SESSION_COOKIE,
    STATE_COOKIE,
    create_auth_router,
)

from .support import cookie_header

if TYPE_CHECKING:
    from pathlib import Path

ALICE = User(provider="github", username="alice")
BOB = User(provider="gitea", username="alice")  # same name, other forge


class DictVault:
    """In-memory TokenVault for tests without a database."""

    def __init__(self) -> None:
        self.tokens: dict[str, str] = {}
        self.lifetimes: dict[str, int] = {}

    async def save(self, session_id: str, token: str, lifetime: int) -> None:
        self.tokens[session_id] = token
        self.lifetimes[session_id] = lifetime

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


def test_empty_or_short_signing_key_regenerated(tmp_path: Path) -> None:
    """A crash between touch() and write_bytes() used to leave a
    zero-byte (trivially forgeable) HMAC key that was loaded forever."""
    (tmp_path / "session-key").touch(mode=0o600)
    keys = load_signing_keys(tmp_path)
    assert len(keys) == 1
    assert len(keys[0]) >= 32
    assert load_signing_keys(tmp_path) == keys  # persisted, stable

    # Too-short keys are also replaced.
    (tmp_path / "session-key").write_bytes(b"short")
    assert len(load_signing_keys(tmp_path)[0]) >= 32

    # An empty previous key must not become a valid verification key.
    (tmp_path / "session-key.previous").touch(mode=0o600)
    assert len(load_signing_keys(tmp_path)) == 1

    # An empty operator-provided override is a hard error, not silence.
    override = tmp_path / "override"
    override.write_bytes(b"\n")
    with pytest.raises(ValueError, match="empty"):
        load_signing_keys(tmp_path, override)


def test_signing_key_file_permissions(tmp_path: Path) -> None:
    load_signing_keys(tmp_path)
    assert (tmp_path / "session-key").stat().st_mode & 0o777 == 0o600
    rotate_signing_key(tmp_path)
    # The previous key still verifies sessions; must not be world-readable.
    assert (tmp_path / "session-key.previous").stat().st_mode & 0o777 == 0o600


def test_unqualified_admin_entry_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="nixbot.auth"):
        AuthzConfig(admins=["alice"])
    assert "not provider-qualified" in caplog.text
    caplog.clear()
    with caplog.at_level("WARNING", logger="nixbot.auth"):
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
            state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]

            callback = await client.get(
                f"/auth/github/callback?code=thecode&state={state}",
                headers=cookie_header({f"nixbot_oauth_state_{state}": state}),
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
                headers=cookie_header({f"nixbot_oauth_state_{state}": state}),
            )
            assert bad.status_code == 403

            # User cancelled on the authorize page: no code, error param.
            cancelled = await client.get(
                f"/auth/github/callback?error=access_denied&state={state}",
                headers=cookie_header({f"nixbot_oauth_state_{state}": state}),
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


def test_forge_token_lifetime_capped_by_expires_in() -> None:
    """Gitea access tokens expire after ~1h while the session lives
    30 days; the stored forge token must expire with the token, not
    the session, so visibility falls back to public instead of
    re-firing failing forge calls."""
    provider = github_oauth("cid", "csecret")
    vault = DictVault()
    signer = SessionSigner([b"k"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "alice"})
        return httpx.Response(404)

    app = FastAPI()
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
            state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
            callback = await client.get(
                f"/auth/github/callback?code=c&state={state}",
                headers=cookie_header({f"nixbot_oauth_state_{state}": state}),
            )
            assert callback.status_code == 307
            session_id = signer.session_id_from(callback.cookies[SESSION_COOKIE])
            assert session_id is not None
            assert vault.lifetimes[session_id] == 3600

    asyncio.run(run())


def test_concurrent_logins_do_not_clobber_oauth_state() -> None:
    """Two login attempts in parallel tabs: a single shared state
    cookie let the second login overwrite the first attempt's state,
    403ing whichever callback completed first."""
    provider = github_oauth("cid", "csecret")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "at"})
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "alice"})
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
            first = await client.get("/login/github")
            second = await client.get("/login/github")
            cookies = dict(first.cookies) | dict(second.cookies)
            states = [
                parse_qs(urlparse(r.headers["location"]).query)["state"][0]
                for r in (first, second)
            ]
            assert states[0] != states[1]
            # Both callbacks succeed, in either completion order.
            for state in states:
                callback = await client.get(
                    f"/auth/github/callback?code=thecode&state={state}",
                    headers=cookie_header(cookies),
                )
                assert callback.status_code == 307, state

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
                headers=cookie_header({"nixbot_oauth_state_s": "s"}),
            )
            assert response.status_code == 403
            assert "bad_verification_code" in response.text

            # Userinfo body missing the username field: also 403, not 500.
            response = await client.get(
                "/auth/github/callback?code=ok&state=s",
                headers=cookie_header({"nixbot_oauth_state_s": "s"}),
            )
            assert response.status_code == 403
            assert "userinfo" in response.json()["detail"]

    asyncio.run(run())


def test_userinfo_rejects_null_or_empty_username() -> None:
    """A userinfo body with "login": null (or "") must not authenticate
    as the literal user "None"/"" shared by everyone."""
    provider = github_oauth("cid", "cs")

    def handler_for(login: object) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/login/oauth/access_token":
                return httpx.Response(200, json={"access_token": "at"})
            if request.url.path == "/user":
                return httpx.Response(200, json={"login": login})
            return httpx.Response(404)

        return httpx.MockTransport(handler)

    for bad_login in (None, "", 42):
        client = httpx.AsyncClient(transport=handler_for(bad_login))
        with pytest.raises(OAuthError, match="username"):
            asyncio.run(provider.exchange_code(client, "c", "https://ci/cb"))


def test_github_oauth_scope_and_enterprise_urls() -> None:
    # Default is the minimal read-only scope: "repo" grants full write
    # access and the token is held server-side for the session lifetime.
    default = github_oauth("cid", "cs")
    assert default.scope.split() == ["read:user"]
    # Private-repo visibility needs "repo" (GitHub has no read-only
    # repo scope); explicit opt-in.
    private = github_oauth("cid", "cs", private_repo_scope=True)
    assert private.scope.split() == ["read:user", "repo"]
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
    # Identity defaults to the stable, provider-unique "sub" claim as
    # documented (oidc:<issuer>:<sub>): a mutable claim like
    # preferred_username would let users hijack admin entries.
    assert provider.username_field == "sub"


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


def test_oidc_provider_honors_advertised_auth_methods() -> None:
    """A provider that only offers client_secret_post (e.g. older
    PocketID) gets body credentials; everyone else gets basic."""

    def discovery(methods: list[str] | None) -> httpx.MockTransport:
        doc: dict[str, object] = {
            "issuer": "https://id.example.com",
            "authorization_endpoint": "https://id.example.com/auth",
            "token_endpoint": "https://id.example.com/token",
            "userinfo_endpoint": "https://id.example.com/userinfo",
        }
        if methods is not None:
            doc["token_endpoint_auth_methods_supported"] = methods
        return httpx.MockTransport(lambda _request: httpx.Response(200, json=doc))

    def provider_for(methods: list[str] | None) -> OAuthProvider:
        return asyncio.run(
            oidc_provider(
                httpx.AsyncClient(transport=discovery(methods)),
                "https://id.example.com/.well-known/openid-configuration",
                "cid",
                "cs",
                ["openid"],
            )
        )

    assert provider_for(["client_secret_post"]).client_auth == "body"
    assert (
        provider_for(["client_secret_basic", "client_secret_post"]).client_auth
        == "basic"
    )
    # Absent means client_secret_basic per OIDC discovery spec.
    assert provider_for(None).client_auth == "basic"


def test_viewer_rule_matching() -> None:
    alice = User(provider="oidc:auth.example.com", username="alice", groups=("ci",))
    assert matches_rule(alice, "oidc:auth.example.com:alice")
    assert matches_rule(alice, "oidc:auth.example.com:*")
    assert matches_rule(alice, "oidc:auth.example.com:group:ci")
    assert not matches_rule(alice, "oidc:auth.example.com:group:ops")
    assert not matches_rule(alice, "oidc:other.example.com:*")
    assert not matches_rule(alice, "github:alice")
    assert not matches_rule(alice, "alice")
    assert not matches_rule(alice, "*")


def test_can_view_private_per_repo_precedence() -> None:
    viewers = {
        "*": ["oidc:idp:group:everyone-private"],
        "gitlab:acme/*": ["oidc:idp:*"],
        "gitlab:acme/secret": ["github:audit-bot"],
    }
    org_user = User(provider="oidc:idp", username="bob")
    everyone = User(provider="oidc:idp", username="eve", groups=("everyone-private",))
    bot = User(provider="github", username="audit-bot")

    assert not can_view_private(org_user, viewers, "gitlab", "acme", "secret")
    assert can_view_private(bot, viewers, "gitlab", "acme", "secret")
    assert can_view_private(org_user, viewers, "gitlab", "acme", "widget")
    # Most specific key only, no union with "*": the global group rule
    # does not leak into repos that define their own rules.
    assert not can_view_private(bot, viewers, "gitlab", "acme", "widget")
    assert can_view_private(everyone, viewers, "github", "other", "repo")
    assert not can_view_private(None, viewers, "github", "other", "repo")
    assert not can_view_private(org_user, {}, "gitlab", "acme", "widget")


def test_session_carries_groups() -> None:
    signer = SessionSigner([b"k" * 32])
    user = User(provider="oidc:idp", username="alice", groups=("ci", "ops"))
    token = signer.session_for(user)
    restored = signer.user_from(token)
    assert restored is not None
    assert restored.groups == ("ci", "ops")


def test_relevant_groups_keeps_only_rule_groups() -> None:
    """LDAP users can be in dozens of groups; a signed cookie past
    ~4KB hits header limits, so the session only carries groups that
    a viewer rule can actually match."""
    viewers = {
        "*": ["oidc:idp:group:ci", "oidc:idp:alice"],
        "gitlab:acme/*": ["oidc:idp:group:auditors"],
    }
    groups = ("ci", "auditors", *[f"unused-{i}" for i in range(100)])
    assert relevant_groups(groups, viewers) == ("ci", "auditors")
    assert relevant_groups(groups, {}) == ()


def test_oauth_callback_filters_session_groups() -> None:
    """The session cookie only carries groups a viewer rule can match."""
    provider = OAuthProvider(
        name="oidc",
        authorize_url="https://id.test/auth",
        token_url="https://id.test/token",  # noqa: S106
        userinfo_url="https://id.test/userinfo",
        client_id="cid",
        client_secret="cs",  # noqa: S106
        scope="openid groups",
        username_field="preferred_username",
        provider_id="oidc:id.test",
        groups_field="groups",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "at"})
        if request.url.path == "/userinfo":
            return httpx.Response(
                200,
                json={
                    "preferred_username": "alice",
                    "groups": ["ci"] + [f"noise-{i}" for i in range(40)],
                },
            )
        return httpx.Response(404)

    signer = SessionSigner([b"k"])
    app = FastAPI()
    app.include_router(
        create_auth_router(
            {"oidc": provider},
            signer,
            "https://ci.test",
            DictVault(),
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            private_repo_viewers={"*": ["oidc:id.test:group:ci"]},
        )
    )

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://ci.test"
        ) as client:
            response = await client.get(
                "/auth/oidc/callback?code=c&state=s",
                headers=cookie_header({f"{STATE_COOKIE}_s": "s"}),
            )
            assert response.status_code in (302, 307)
            session = response.cookies.get(SESSION_COOKIE)
            user = signer.user_from(session)
            assert user is not None
            assert user.groups == ("ci",)

    asyncio.run(run())
