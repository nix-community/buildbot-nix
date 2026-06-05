"""Login/logout routes wiring the auth module into the web app."""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from ..auth import OAuthError, same_origin  # noqa: TID252

if TYPE_CHECKING:
    from ..auth import OAuthProvider, SessionSigner  # noqa: TID252
    from ..forge_tokens import TokenVault  # noqa: TID252

SESSION_COOKIE = "buildbot_nix_session"
STATE_COOKIE = "buildbot_nix_oauth_state"


def _check_callback_params(request: Request, code: str, state: str, error: str) -> None:
    if not state or request.cookies.get(STATE_COOKIE) != state:
        raise HTTPException(status_code=403, detail="invalid oauth state")
    if error or not code:
        # e.g. ?error=access_denied when the user cancels authorization.
        raise HTTPException(
            status_code=403, detail=f"login failed: {error or 'missing code'}"
        )


def create_auth_router(
    providers: dict[str, OAuthProvider],
    signer: SessionSigner,
    base_url: str,
    forge_tokens: TokenVault,
    http: httpx.AsyncClient | None = None,
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
            STATE_COOKIE,
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
            user, access_token = await provider.exchange_code(http, code, redirect_uri)
        except (OAuthError, httpx.HTTPError) as exc:
            # Expired/reused codes are routine (refresh, stale tab).
            raise HTTPException(
                status_code=403,
                detail=f"login failed ({exc}); please log in again",
            ) from exc
        session_id = secrets.token_urlsafe(32)
        await forge_tokens.save(session_id, access_token, signer.lifetime)
        response = RedirectResponse("/")
        response.delete_cookie(STATE_COOKIE)
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
        # Captured cookie copies must not stay usable after logout.
        session_id = signer.session_id_from(request.cookies.get(SESSION_COOKIE))
        if session_id is not None:
            await forge_tokens.delete(session_id)
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    return router
