"""Login/logout routes wiring the auth module into the web app."""

from __future__ import annotations

import re
import secrets
from dataclasses import replace
from typing import TYPE_CHECKING

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from ..auth import OAuthError, relevant_groups, same_origin  # noqa: TID252

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..auth import OAuthProvider, SessionSigner, User  # noqa: TID252
    from ..forge_tokens import SessionRevocations, TokenVault  # noqa: TID252

SESSION_COOKIE = "nixbot_session"
STATE_COOKIE = "nixbot_oauth_state"
# Server-generated states are token_urlsafe; the callback echoes the
# state into a cookie name, so reject anything else outright.
_STATE_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _state_cookie(state: str) -> str:
    """Per-attempt cookie: concurrent logins (multiple tabs) must not
    clobber each other's state."""
    return f"{STATE_COOKIE}_{state}"


def _check_callback_params(request: Request, code: str, state: str, error: str) -> None:
    if (
        not _STATE_RE.fullmatch(state)
        or request.cookies.get(_state_cookie(state)) != state
    ):
        raise HTTPException(status_code=403, detail="invalid oauth state")
    if error or not code:
        # e.g. ?error=access_denied when the user cancels authorization.
        raise HTTPException(
            status_code=403, detail=f"login failed: {error or 'missing code'}"
        )


async def _invalidate_session(
    signer: SessionSigner,
    forge_tokens: TokenVault,
    revoked_sessions: SessionRevocations | None,
    cookie: str | None,
) -> None:
    """Captured cookie copies must not stay usable after logout: the
    cookie is stateless and stays validly signed until expiry, so the
    session id goes on a server-side denylist and the forge token is
    dropped."""
    session_id = signer.session_id_from(cookie)
    if session_id is None:
        return
    # Revoke first: if we crash in between, a still-present forge token
    # is useless without an accepted session, but the reverse order
    # would leave a cookie that keeps authenticating.
    if revoked_sessions is not None:
        await revoked_sessions.revoke(session_id, signer.lifetime)
    await forge_tokens.delete(session_id)


def create_auth_router(  # noqa: C901, PLR0913
    providers: dict[str, OAuthProvider],
    signer: SessionSigner,
    base_url: str,
    forge_tokens: TokenVault,
    http: httpx.AsyncClient | None = None,
    private_repo_viewers: dict[str, list[str]] | None = None,
    revoked_sessions: SessionRevocations | None = None,
    on_logout: Callable[[User], None] | None = None,
) -> APIRouter:
    router = APIRouter()
    http = http or httpx.AsyncClient()
    base_url = base_url.rstrip("/")

    @router.get("/login/{provider_name}")
    async def login(provider_name: str) -> RedirectResponse:
        provider = providers.get(provider_name)
        if provider is None:
            raise HTTPException(status_code=404, detail="unknown login provider")
        state = secrets.token_urlsafe(24)
        redirect_uri = f"{base_url}/auth/{provider_name}/callback"
        response = RedirectResponse(provider.authorize_redirect(redirect_uri, state))
        response.set_cookie(
            _state_cookie(state),
            state,
            max_age=600,
            httponly=True,
            samesite="lax",
            secure=base_url.startswith("https"),
        )
        return response

    @router.get("/auth/{provider_name}/callback")
    async def callback(
        provider_name: str,
        request: Request,
        code: str = "",
        state: str = "",
        error: str = "",
    ) -> RedirectResponse:
        provider = providers.get(provider_name)
        if provider is None:
            raise HTTPException(status_code=404)
        _check_callback_params(request, code, state, error)
        redirect_uri = f"{base_url}/auth/{provider_name}/callback"
        try:
            user, token = await provider.exchange_code(http, code, redirect_uri)
        except (OAuthError, httpx.HTTPError) as exc:
            # Expired/reused codes are routine (refresh, stale tab).
            raise HTTPException(
                status_code=403,
                detail=f"login failed ({exc}); please log in again",
            ) from exc
        if user.groups:
            user = replace(
                user, groups=relevant_groups(user.groups, private_repo_viewers or {})
            )
        session_id = secrets.token_urlsafe(32)
        # Cap the stored forge token at its own expiry: a Gitea token
        # dies ~1h into a 30-day session, and an expired-but-stored
        # token would fire a failing forge call on every request.
        token_lifetime = signer.lifetime
        if token.expires_in is not None:
            token_lifetime = min(token_lifetime, token.expires_in)
        await forge_tokens.save(session_id, token.access_token, token_lifetime)
        response = RedirectResponse("/")
        response.delete_cookie(_state_cookie(state))
        response.set_cookie(
            SESSION_COOKIE,
            signer.session_for(user, session_id),
            max_age=signer.lifetime,
            httponly=True,
            samesite="lax",
            secure=base_url.startswith("https"),
        )
        return response

    @router.post("/logout")
    async def logout(request: Request) -> RedirectResponse:
        if not same_origin(request, base_url):
            raise HTTPException(status_code=403, detail="cross-origin request")
        cookie = request.cookies.get(SESSION_COOKIE)
        await _invalidate_session(signer, forge_tokens, revoked_sessions, cookie)
        user = signer.user_from(cookie)
        if user is not None and on_logout is not None:
            on_logout(user)
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    return router
