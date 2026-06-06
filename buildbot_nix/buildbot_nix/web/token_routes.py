"""Personal API token management UI routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..auth import same_origin  # noqa: TID252

if TYPE_CHECKING:
    from ..api_tokens import ApiTokenStore  # noqa: TID252
    from .app import WebContext


def _parse_expiry(expires_days: str) -> datetime | None:
    if not expires_days:
        return None
    try:
        days = int(expires_days)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="expires_days must be a number"
        ) from None
    if days <= 0:
        raise HTTPException(status_code=400, detail="expires_days must be positive")
    try:
        return datetime.now(tz=UTC) + timedelta(days=days)
    except OverflowError:
        raise HTTPException(
            status_code=400, detail="expires_days is too large"
        ) from None


def create_token_router(
    ctx: WebContext, store: ApiTokenStore, own_url: str
) -> APIRouter:
    router = APIRouter()

    @router.get("/tokens", response_class=HTMLResponse)
    async def tokens_page(request: Request) -> HTMLResponse:
        user = ctx.current_user(request)
        if user is None:
            raise HTTPException(status_code=403, detail="login required")
        return ctx.render(
            "tokens.html",
            user=user,
            tokens=await store.list_for(user),
            new_token=None,
        )

    @router.post("/tokens", response_class=HTMLResponse)
    async def create_token(
        request: Request,
        name: Annotated[str, Form()] = "",
        expires_days: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        if not same_origin(request, own_url):
            raise HTTPException(status_code=403, detail="cross-origin request")
        user = ctx.current_user(request)
        if user is None:
            raise HTTPException(status_code=403, detail="login required")
        token = await store.create(user, name or "unnamed", _parse_expiry(expires_days))
        # Shown exactly once.
        return ctx.render(
            "tokens.html",
            user=user,
            tokens=await store.list_for(user),
            new_token=token,
        )

    @router.post("/tokens/{token_id}/revoke")
    async def revoke_token(request: Request, token_id: int) -> RedirectResponse:
        if not same_origin(request, own_url):
            raise HTTPException(status_code=403, detail="cross-origin request")
        user = ctx.current_user(request)
        if user is None:
            raise HTTPException(status_code=403, detail="login required")
        if not await store.revoke(user, token_id):
            raise HTTPException(status_code=404)
        return RedirectResponse("/tokens", status_code=303)

    return router
