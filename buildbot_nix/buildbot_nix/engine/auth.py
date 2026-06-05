"""Authentication and authorization.

Login: GitHub OAuth, Gitea OAuth, and generic OIDC. Sessions are
signed cookies (HMAC-SHA256 over a JSON payload with expiry) with a
two-key rotation window; the signing key is auto-generated in the
state directory and can be overridden via LoadCredential. CSRF: all
state-changing endpoints require a same-origin check
(Origin/Sec-Fetch-Site) on top of SameSite=Lax cookies.

Authorization (port of the authz.py semantics): admin and allowlist
entries are provider-qualified (`github:alice`, `gitea:bob`,
`oidc:<issuer>:<sub>`) — the same username on a different provider
never matches. Admins control everything; a PR author (matched by
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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

if TYPE_CHECKING:
    from pathlib import Path

    import httpx
    from fastapi import Request

logger = logging.getLogger(__name__)

DEFAULT_SESSION_LIFETIME = 30 * 24 * 60 * 60


@dataclass(frozen=True)
class User:
    provider: str  # "github" | "gitea" | "oidc:<issuer>"
    username: str

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
        payload = {"provider": user.provider, "username": user.username}
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
        return User(provider=payload["provider"], username=payload["username"])

    def session_id_from(self, token: str | None) -> str | None:
        if not token:
            return None
        payload = self.verify(token)
        return payload.get("sid") if payload else None


def load_signing_keys(state_dir: Path, override: Path | None = None) -> list[bytes]:
    """Current + previous key. Auto-generated in StateDirectory unless
    overridden via LoadCredential."""
    if override is not None:
        return [override.read_bytes().strip()]
    key_file = state_dir / "session-key"
    previous_file = state_dir / "session-key.previous"
    keys = []
    if key_file.exists():
        keys.append(key_file.read_bytes())
    else:
        state_dir.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        key_file.touch(mode=0o600)
        key_file.write_bytes(key)
        keys.append(key)
    if previous_file.exists():
        keys.append(previous_file.read_bytes())
    return keys


def rotate_signing_key(state_dir: Path) -> None:
    key_file = state_dir / "session-key"
    previous_file = state_dir / "session-key.previous"
    if key_file.exists():
        # Still a valid verification key during the rotation window.
        previous_file.touch(mode=0o600)
        previous_file.write_bytes(key_file.read_bytes())
    new_key = secrets.token_bytes(32)
    key_file.touch(mode=0o600)
    key_file.write_bytes(new_key)


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
    provider_id: str = ""  # User.provider value

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
    ) -> tuple[User, str]:
        response = await http.post(
            self.token_url,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
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
        userinfo = await http.get(
            self.userinfo_url,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if userinfo.is_error:
            msg = f"userinfo endpoint returned HTTP {userinfo.status_code}"
            raise OAuthError(msg)
        try:
            username = str(userinfo.json()[self.username_field])
        except (ValueError, KeyError) as exc:
            msg = f"userinfo response is missing {self.username_field!r}"
            raise OAuthError(msg) from exc
        return User(
            provider=self.provider_id or self.name, username=username
        ), access_token


def github_oauth(
    client_id: str,
    client_secret: str,
    api_url: str = "https://api.github.com",
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
        # "repo" is required so the visibility repo-set fetch
        # (GET /user/repos) can see private repositories.
        scope="read:user repo",
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


async def oidc_provider(  # noqa: PLR0913
    http: httpx.AsyncClient,
    discovery_url: str,
    client_id: str,
    client_secret: str,
    scope: list[str],
    username_claim: str = "preferred_username",
) -> OAuthProvider:
    """Generic OIDC via the discovery document. Identities are
    qualified as oidc:<issuer>:<username>."""
    response = await http.get(discovery_url)
    response.raise_for_status()
    doc = response.json()
    issuer = doc["issuer"].removeprefix("https://").rstrip("/")
    return OAuthProvider(
        name="oidc",
        authorize_url=doc["authorization_endpoint"],
        token_url=doc["token_endpoint"],
        userinfo_url=doc["userinfo_endpoint"],
        client_id=client_id,
        client_secret=client_secret,
        scope=" ".join(scope),
        username_field=username_claim,
        provider_id=f"oidc:{issuer}",
    )
