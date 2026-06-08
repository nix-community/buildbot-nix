"""Authentication and authorization.

Login: GitHub OAuth, Gitea OAuth, and generic OIDC. Sessions are
signed cookies (HMAC-SHA256 over a JSON payload with expiry) with a
two-key rotation window; the signing key is auto-generated in the
state directory and can be overridden via LoadCredential. CSRF: all
state-changing endpoints require a same-origin check
(Origin/Sec-Fetch-Site) on top of SameSite=Lax cookies.

Authorization (port of the authz.py semantics): admin and allowlist
entries are provider-qualified (`github:alice`, `gitea:bob`,
`oidc:<issuer>:<sub>` — the OIDC identity claim defaults to the
stable `sub` and is configurable via `mapping.username`) — the same
username on a different provider never matches. Admins control everything; a PR author (matched by
forge identity on the same forge) may restart/cancel builds of their
own PR; `allowUnauthenticatedControl` opens control for VPN/local-dev
instances.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlencode

import httpx

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import Request

logger = logging.getLogger(__name__)

DEFAULT_SESSION_LIFETIME = 30 * 24 * 60 * 60


@dataclass(frozen=True)
class User:
    provider: str  # "github" | "gitea" | "oidc:<issuer>"
    username: str
    avatar_url: str | None = None
    # OIDC groups claim; drives "provider:group:<name>" viewer rules.
    groups: tuple[str, ...] = ()

    @property
    def qualified(self) -> str:
        return f"{self.provider}:{self.username}"


# --- signed sessions -------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class SessionSigner:
    """Signs with the first key, verifies against all (rotation window)."""

    def __init__(
        self, keys: list[bytes], lifetime: int = DEFAULT_SESSION_LIFETIME
    ) -> None:
        if not keys:
            msg = "at least one signing key required"
            raise ValueError(msg)
        self.keys = keys
        self.lifetime = lifetime

    def sign(self, payload: dict[str, Any]) -> str:
        payload = {**payload, "exp": int(time.time()) + self.lifetime}
        body = _b64(json.dumps(payload).encode())
        mac = hmac.new(self.keys[0], body.encode(), hashlib.sha256).digest()
        return f"{body}.{_b64(mac)}"

    def verify(self, token: str) -> dict[str, Any] | None:
        try:
            body, mac_b64 = token.split(".", 1)
            mac = _unb64(mac_b64)
        except ValueError:
            return None
        if not any(
            hmac.compare_digest(
                hmac.new(key, body.encode(), hashlib.sha256).digest(), mac
            )
            for key in self.keys
        ):
            return None
        try:
            payload = json.loads(_unb64(body))
        except (ValueError, json.JSONDecodeError):
            return None
        if payload.get("exp", 0) < time.time():
            return None
        return payload

    def session_for(self, user: User, session_id: str | None = None) -> str:
        payload: dict[str, Any] = {
            "provider": user.provider,
            "username": user.username,
        }
        if user.avatar_url is not None:
            payload["avatar"] = user.avatar_url
        if user.groups:
            payload["groups"] = list(user.groups)
        if session_id is not None:
            # References the server-side forge-token row; the cookie is
            # signed, not encrypted, so the token itself never goes here.
            payload["sid"] = session_id
        return self.sign(payload)

    def user_from(self, token: str | None) -> User | None:
        if not token:
            return None
        payload = self.verify(token)
        if payload is None or "provider" not in payload or "username" not in payload:
            return None
        return User(
            provider=payload["provider"],
            username=payload["username"],
            avatar_url=payload.get("avatar"),
            groups=tuple(payload.get("groups", ())),
        )

    def session_id_from(self, token: str | None) -> str | None:
        if not token:
            return None
        payload = self.verify(token)
        return payload.get("sid") if payload else None


MIN_SIGNING_KEY_LEN = 32


def _read_key(path: Path) -> bytes | None:
    """None for missing, empty or too-short files: a truncated key
    (e.g. from a crash mid-write) would be trivially forgeable."""
    if not path.exists():
        return None
    key = path.read_bytes()
    if len(key) < MIN_SIGNING_KEY_LEN:
        logger.warning("signing key %s is too short; regenerating", path)
        return None
    return key


def _write_key(path: Path, key: bytes) -> None:
    """Atomic (temp file + rename): a crash mid-write must never leave
    a partial key behind."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.touch(mode=0o600)
    tmp.write_bytes(key)
    tmp.replace(path)


def load_signing_keys(state_dir: Path, override: Path | None = None) -> list[bytes]:
    """Current + previous key. Auto-generated in StateDirectory unless
    overridden via LoadCredential."""
    if override is not None:
        override_key = override.read_bytes().strip()
        if not override_key:
            msg = f"session signing key override {override} is empty"
            raise ValueError(msg)
        return [override_key]
    key_file = state_dir / "session-key"
    previous_file = state_dir / "session-key.previous"
    keys = []
    key = _read_key(key_file)
    if key is None:
        state_dir.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(MIN_SIGNING_KEY_LEN)
        _write_key(key_file, key)
    keys.append(key)
    previous = _read_key(previous_file)
    if previous is not None:
        keys.append(previous)
    return keys


def rotate_signing_key(state_dir: Path) -> None:
    key_file = state_dir / "session-key"
    previous_file = state_dir / "session-key.previous"
    current = _read_key(key_file)
    if current is not None:
        # Still a valid verification key during the rotation window.
        _write_key(previous_file, current)
    _write_key(key_file, secrets.token_bytes(MIN_SIGNING_KEY_LEN))


# --- CSRF -----------------------------------------------------------------------


def same_origin(request: Request, own_url: str) -> bool:
    """Origin/Sec-Fetch-Site check for state-changing endpoints."""
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site in ("same-origin", "none")
    origin = request.headers.get("origin")
    if origin is None:
        # Non-browser client (API token usage); cookies don't apply.
        return True
    return origin.rstrip("/") == own_url.rstrip("/")


# --- authorization ----------------------------------------------------------------


@dataclass
class AuthzConfig:
    admins: list[str]  # provider-qualified
    allow_unauthenticated_control: bool = False
    # Per-repo private visibility rules; see can_view_private.
    private_repo_viewers: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # is_admin matches User.qualified exactly, so unqualified entries
        # (e.g. legacy "alice" from the old buildbot config) never match.
        for entry in self.admins:
            if ":" not in entry:
                logger.warning(
                    "admin entry %r is not provider-qualified "
                    "(expected e.g. 'github:%s') and will never match",
                    entry,
                    entry,
                )


def matches_rule(user: User, rule: str) -> bool:
    """Provider-qualified viewer rules: exact "provider:username",
    any-user "provider:*", or groups-claim "provider:group:<name>".
    Unqualified entries never match (same as admins)."""
    provider, sep, rest = rule.rpartition(":")
    if not sep:
        return False
    if provider == user.provider:
        return rest in ("*", user.username)
    group_provider, group_sep, group_marker = provider.rpartition(":")
    if group_sep and group_marker == "group":
        # provider:group:<name>; rest is the group name
        return group_provider == user.provider and rest in user.groups
    return False


def relevant_groups(
    groups: tuple[str, ...], viewers: dict[str, list[str]]
) -> tuple[str, ...]:
    """Groups any viewer rule can match. The session cookie carries
    these; the full OIDC claim can be dozens of LDAP groups and a
    signed cookie past ~4KB hits header limits."""
    referenced = {
        rule.rpartition(":")[2]
        for rules in viewers.values()
        for rule in rules
        if ":group:" in rule
    }
    return tuple(group for group in groups if group in referenced)


def can_view_private(
    user: User | None,
    viewers: dict[str, list[str]],
    forge: str,
    owner: str,
    name: str,
) -> bool:
    """Per-repo private visibility; the most specific key wins
    ("forge:owner/repo" > "forge:owner/*" > "*"), like the per-repo
    effects secrets."""
    if user is None:
        return False
    rules = None
    for key in (f"{forge}:{owner}/{name}", f"{forge}:{owner}/*", "*"):
        rules = viewers.get(key)
        if rules is not None:
            break
    return any(matches_rule(user, rule) for rule in rules or [])


def is_admin(user: User | None, config: AuthzConfig) -> bool:
    return user is not None and user.qualified in config.admins


def can_control_build(
    user: User | None,
    config: AuthzConfig,
    *,
    build_pr_author: str | None = None,
) -> bool:
    """Restart/cancel permission for one build."""
    if config.allow_unauthenticated_control:
        return True
    if user is None:
        return False
    if is_admin(user, config):
        return True
    # PR-author rule: forge identity must match exactly (provider-
    # qualified), and only for that PR's builds.
    return build_pr_author is not None and user.qualified == build_pr_author


# --- OAuth/OIDC flows ---------------------------------------------------------------


@dataclass(frozen=True)
class ForgeToken:
    """Access token plus its advertised lifetime (None = the provider
    did not say, e.g. classic GitHub OAuth tokens that never expire)."""

    access_token: str
    expires_in: int | None = None


class OAuthError(Exception):
    """Token exchange or userinfo fetch failed (expired/reused code,
    provider error response, malformed body)."""


@dataclass
class OAuthProvider:
    name: str  # "github" | "gitea" | "oidc"
    authorize_url: str
    token_url: str
    userinfo_url: str
    client_id: str
    client_secret: str
    scope: str
    # Field of the userinfo response carrying the username.
    username_field: str = "login"
    # Userinfo field carrying the avatar URL.
    avatar_field: str = "avatar_url"
    # Userinfo field with group names (None = no group capture).
    groups_field: str | None = None
    provider_id: str = ""  # User.provider value
    # GitHub/Gitea take credentials in the body; OIDC servers only
    # have to support client_secret_basic (RFC 6749 section 2.3.1).
    client_auth: Literal["body", "basic"] = "body"

    def authorize_redirect(self, redirect_uri: str, state: str) -> str:
        return (
            self.authorize_url
            + "?"
            + urlencode(
                {
                    "client_id": self.client_id,
                    "redirect_uri": redirect_uri,
                    "response_type": "code",
                    "scope": self.scope,
                    "state": state,
                }
            )
        )

    async def exchange_code(
        self, http: httpx.AsyncClient, code: str, redirect_uri: str
    ) -> tuple[User, ForgeToken]:
        data = {
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        auth = (
            httpx.BasicAuth(self.client_id, self.client_secret)
            if self.client_auth == "basic"
            else httpx.USE_CLIENT_DEFAULT
        )
        if self.client_auth == "body":
            data["client_id"] = self.client_id
            data["client_secret"] = self.client_secret
        response = await http.post(
            self.token_url,
            data=data,
            auth=auth,
            headers={"Accept": "application/json"},
        )
        # GitHub returns token-exchange errors (expired/reused code) as
        # HTTP 200 with an {"error": ...} body; other providers use 4xx.
        if response.is_error:
            msg = f"token endpoint returned HTTP {response.status_code}"
            raise OAuthError(msg)
        try:
            token_body = response.json()
        except ValueError as exc:
            msg = "token endpoint returned a non-JSON body"
            raise OAuthError(msg) from exc
        access_token = token_body.get("access_token")
        if not access_token:
            msg = f"token exchange failed: {token_body.get('error', 'no access_token')}"
            raise OAuthError(msg)
        # Gitea/OIDC tokens expire (typically 1h); record the lifetime
        # so the stored forge token is not trusted past it.
        raw_expires = token_body.get("expires_in")
        expires_in = int(raw_expires) if isinstance(raw_expires, (int, float)) else None
        userinfo = await http.get(
            self.userinfo_url,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if userinfo.is_error:
            msg = f"userinfo endpoint returned HTTP {userinfo.status_code}"
            raise OAuthError(msg)
        try:
            info = userinfo.json()
        except ValueError as exc:
            msg = "userinfo endpoint returned a non-JSON body"
            raise OAuthError(msg) from exc
        username = info.get(self.username_field) if isinstance(info, dict) else None
        # A null/empty/non-string claim must not collapse every such
        # user into the shared identity "None" or "".
        if not isinstance(username, str) or not username:
            msg = f"userinfo response has no usable username in {self.username_field!r}"
            raise OAuthError(msg)
        avatar = info.get(self.avatar_field)
        groups: tuple[str, ...] = ()
        if self.groups_field is not None:
            raw_groups = info.get(self.groups_field)
            if isinstance(raw_groups, list):
                groups = tuple(str(g) for g in raw_groups)
        return User(
            provider=self.provider_id or self.name,
            username=username,
            avatar_url=str(avatar) if avatar else None,
            groups=groups,
        ), ForgeToken(access_token, expires_in)


def github_oauth(
    client_id: str,
    client_secret: str,
    api_url: str = "https://api.github.com",
    *,
    private_repo_scope: bool = False,
) -> OAuthProvider:
    api = api_url.rstrip("/")
    if api == "https://api.github.com":
        web = "https://github.com"
    else:
        # GitHub Enterprise: API lives at https://ghe.example.com/api/v3.
        web = api.removesuffix("/api/v3")
    return OAuthProvider(
        name="github",
        authorize_url=f"{web}/login/oauth/authorize",
        token_url=f"{web}/login/oauth/access_token",
        userinfo_url=f"{api}/user",
        client_id=client_id,
        client_secret=client_secret,
        # Private-repo visibility (GET /user/repos listing private
        # repositories) requires "repo", which GitHub only offers with
        # write access; instances without private repos should not hold
        # write-capable tokens, so it is opt-in.
        scope="read:user repo" if private_repo_scope else "read:user",
        username_field="login",
        provider_id="github",
    )


def gitea_oauth(instance_url: str, client_id: str, client_secret: str) -> OAuthProvider:
    base = instance_url.rstrip("/")
    return OAuthProvider(
        name="gitea",
        authorize_url=f"{base}/login/oauth/authorize",
        token_url=f"{base}/login/oauth/access_token",
        userinfo_url=f"{base}/api/v1/user",
        client_id=client_id,
        client_secret=client_secret,
        # read:repository is required so the visibility repo-set fetch
        # (GET /api/v1/user/repos) works with scoped tokens (Gitea >= 1.19).
        scope="read:user read:repository",
        username_field="login",
        provider_id="gitea",
    )


def gitlab_oauth(
    instance_url: str, client_id: str, client_secret: str
) -> OAuthProvider:
    base = instance_url.rstrip("/")
    return OAuthProvider(
        name="gitlab",
        authorize_url=f"{base}/oauth/authorize",
        token_url=f"{base}/oauth/token",
        userinfo_url=f"{base}/api/v4/user",
        client_id=client_id,
        client_secret=client_secret,
        # read_api is required so the visibility repo-set fetch
        # (GET /api/v4/projects?membership=true) works.
        scope="read_user read_api",
        username_field="username",
        avatar_field="avatar_url",
        provider_id="gitlab",
    )


async def oidc_provider(  # noqa: PLR0913
    http: httpx.AsyncClient,
    discovery_url: str,
    client_id: str,
    client_secret: str,
    scope: list[str],
    username_claim: str = "sub",
    groups_claim: str | None = None,
) -> OAuthProvider:
    """Generic OIDC via the discovery document. Identities are
    qualified as oidc:<issuer>:<claim> where the claim defaults to the
    stable "sub": a mutable claim like preferred_username would let a
    user who can edit it hijack someone else's admin/viewer entry."""
    response = await http.get(discovery_url)
    response.raise_for_status()
    doc = response.json()
    issuer = doc["issuer"].removeprefix("https://").rstrip("/")
    # Absent means client_secret_basic per OIDC discovery spec; only
    # fall back to body credentials if basic is explicitly not offered.
    methods = doc.get("token_endpoint_auth_methods_supported")
    client_auth: Literal["body", "basic"] = "basic"
    if methods is not None and "client_secret_basic" not in methods:
        client_auth = "body"
    return OAuthProvider(
        name="oidc",
        authorize_url=doc["authorization_endpoint"],
        token_url=doc["token_endpoint"],
        userinfo_url=doc["userinfo_endpoint"],
        client_id=client_id,
        client_secret=client_secret,
        scope=" ".join(scope),
        client_auth=client_auth,
        username_field=username_claim,
        groups_field=groups_claim,
        # Standard OIDC claim for the profile picture.
        avatar_field="picture",
        provider_id=f"oidc:{issuer}",
    )
