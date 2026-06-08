"""Build control endpoints: restart build, restart a single
attribute without re-eval, rebuild-all-failed, cancel build or a
single attribute — gated by authz
(admins, PR authors for their own PR, allowUnauthenticatedControl) and
CSRF same-origin checks. Admin-only project enable/disable toggle.

The actual restart/cancel work happens behind the ControlBackend
protocol, implemented by the service service composition
where the orchestrator lives.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Protocol
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from ..auth import can_control_build, is_admin, same_origin  # noqa: TID252
from ..hook_secrets import WebhookSecrets  # noqa: TID252

if TYPE_CHECKING:
    from ..auth import AuthzConfig  # noqa: TID252
    from .app import WebContext


class ControlBackend(Protocol):
    async def restart_build(self, build_id: int) -> None: ...

    async def restart_attribute(self, build_id: int, attr: str) -> None: ...

    async def restart_effects(self, build_id: int) -> None: ...

    async def cancel_build(self, build_id: int) -> None: ...

    async def cancel_attribute(self, build_id: int, attr: str) -> None: ...

    async def refresh_projects(self) -> None: ...


FAILED_STATUSES = ("failed", "failed_eval", "dependency_failed", "cached_failure")


class ControlAction(BaseModel):
    """Acknowledgement of a restart/cancel request."""

    number: int
    action: str  # restart | cancel


class EnableResult(BaseModel):
    owner: str
    name: str
    enabled: bool


def _back(forge: str, owner: str, name: str, number: int) -> RedirectResponse:
    return RedirectResponse(
        f"/repos/{forge}/{owner}/{name}/builds/{number}", status_code=303
    )


def _back_to_attr(
    forge: str, owner: str, name: str, number: int, attr: str
) -> RedirectResponse:
    return RedirectResponse(
        f"/repos/{forge}/{owner}/{name}/builds/{number}/logs/{attr}",
        status_code=303,
    )


class _ControlRoutes:
    def __init__(
        self,
        ctx: WebContext,
        backend: ControlBackend,
        authz: AuthzConfig,
        own_url: str,
    ) -> None:
        self.ctx = ctx
        self.backend = backend
        self.authz = authz
        self.own_url = own_url

    async def _authorize(
        self, request: Request, forge: str, owner: str, name: str, number: int
    ) -> dict:
        if not same_origin(request, self.own_url):
            raise HTTPException(status_code=403, detail="cross-origin request")
        project = await self.ctx.repo_or_404(forge, owner, name, request)
        build = await self.ctx.queries.build_by_number(project["id"], number)
        if build is None:
            raise HTTPException(status_code=404)
        user = await self.ctx.request_user(request)
        if not can_control_build(
            user, self.authz, build_pr_author=build.get("pr_author")
        ):
            raise HTTPException(status_code=403, detail="not authorized")
        return build

    async def restart(
        self, request: Request, forge: str, owner: str, name: str, number: int
    ) -> RedirectResponse:
        build = await self._authorize(request, forge, owner, name, number)
        await self.backend.restart_build(build["id"])
        return _back(forge, owner, name, number)

    async def restart_attribute(  # noqa: PLR0913
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        number: int,
        attr: str,
    ) -> RedirectResponse:
        build = await self._authorize(request, forge, owner, name, number)
        known = await self.ctx.pool.fetchval(
            "SELECT 1 FROM build_attributes WHERE build_id = $1 AND attr = $2",
            build["id"],
            attr,
        )
        if known is None:
            raise HTTPException(status_code=404, detail="unknown attribute")
        # Reuses the stored eval results (drv_path); no re-eval.
        await self.backend.restart_attribute(build["id"], attr)
        return _back_to_attr(forge, owner, name, number, attr)

    async def restart_effects(
        self, request: Request, forge: str, owner: str, name: str, number: int
    ) -> RedirectResponse:
        build = await self._authorize(request, forge, owner, name, number)
        await self.backend.restart_effects(build["id"])
        return _back(forge, owner, name, number)

    async def rebuild_failed(
        self, request: Request, forge: str, owner: str, name: str, number: int
    ) -> RedirectResponse:
        build = await self._authorize(request, forge, owner, name, number)
        rows = await self.ctx.pool.fetch(
            "SELECT attr FROM build_attributes "
            "WHERE build_id = $1 AND status = ANY($2::text[])",
            build["id"],
            list(FAILED_STATUSES),
        )
        for row in rows:
            await self.backend.restart_attribute(build["id"], row["attr"])
        return _back(forge, owner, name, number)

    async def cancel_attribute(  # noqa: PLR0913
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        number: int,
        attr: str,
    ) -> RedirectResponse:
        build = await self._authorize(request, forge, owner, name, number)
        await self.backend.cancel_attribute(build["id"], attr)
        return _back_to_attr(forge, owner, name, number, attr)

    async def cancel(
        self, request: Request, forge: str, owner: str, name: str, number: int
    ) -> RedirectResponse:
        build = await self._authorize(request, forge, owner, name, number)
        await self.backend.cancel_build(build["id"])
        return _back(forge, owner, name, number)

    async def _require_repo_admin(self, request: Request, project_id: int) -> None:
        """Instance admins, or forge-side admins of this repo."""
        if not same_origin(request, self.own_url):
            raise HTTPException(status_code=403, detail="cross-origin request")
        if not is_admin(await self.ctx.request_user(request), self.authz):
            toggleable = await self.ctx.toggleable_repo_ids(request) or []
            if project_id not in toggleable:
                raise HTTPException(status_code=403, detail="not a project admin")

    async def api_restart(
        self, request: Request, forge: str, owner: str, name: str, number: int
    ) -> dict:
        """Re-run the whole build. Authz: admins or the PR author."""
        build = await self._authorize(request, forge, owner, name, number)
        await self.backend.restart_build(build["id"])
        return {"number": number, "action": "restart"}

    async def api_cancel(
        self, request: Request, forge: str, owner: str, name: str, number: int
    ) -> dict:
        """Cancel a pending/running build. Authz: admins or the PR author."""
        build = await self._authorize(request, forge, owner, name, number)
        await self.backend.cancel_build(build["id"])
        return {"number": number, "action": "cancel"}

    async def api_set_enabled(
        self, request: Request, forge: str, owner: str, name: str
    ) -> dict:
        """Idempotent enable/disable for scripts and agents."""
        project = await self.ctx.repo_or_404(forge, owner, name, request)
        await self._require_repo_admin(request, project["id"])
        enabled = request.url.path.endswith("/enable")
        await self.ctx.pool.execute(
            "UPDATE projects SET enabled = $2, updated_at = now() WHERE id = $1",
            project["id"],
            enabled,
        )
        return {"owner": owner, "name": name, "enabled": enabled}

    async def refresh_repos(
        self, request: Request, q: Annotated[str, Form()] = ""
    ) -> RedirectResponse:
        """Re-run forge discovery on demand (background sync is hourly).
        Any logged-in user: the typical case is surfacing a freshly
        created repo in the enable search. Anonymous requests are
        rejected and the backend debounces, so outsiders cannot hammer
        the forge APIs."""
        if not same_origin(request, self.own_url):
            raise HTTPException(status_code=403, detail="cross-origin request")
        if await self.ctx.request_user(request) is None:
            raise HTTPException(status_code=403, detail="login required")
        await self.backend.refresh_projects()
        return RedirectResponse(f"/?q={quote(q)}" if q else "/", status_code=303)

    async def toggle_repo(
        self, request: Request, project_id: int, q: Annotated[str, Form()] = ""
    ) -> RedirectResponse:
        await self._require_repo_admin(request, project_id)
        await self.ctx.pool.execute(
            "UPDATE projects SET enabled = NOT enabled, updated_at = now() "
            "WHERE id = $1",
            project_id,
        )
        # Back to the dashboard with the project filter intact.
        return RedirectResponse(f"/?q={quote(q)}" if q else "/", status_code=303)

    async def regenerate_webhook_secret(
        self, request: Request, project_id: int
    ) -> HTMLResponse:
        """Rotate and show the webhook secret once; it is never
        readable afterwards."""
        await self._require_repo_admin(request, project_id)
        secret = await WebhookSecrets(self.ctx.pool).rotate(project_id)
        return await self.ctx.render(
            "_webhook_secret.html", request=request, secret=secret
        )


def create_control_router(
    ctx: WebContext,
    backend: ControlBackend,
    authz: AuthzConfig,
    own_url: str,
) -> APIRouter:
    router = APIRouter()
    routes = _ControlRoutes(ctx, backend, authz, own_url)
    base = "/repos/{forge}/{owner}/{name}/builds/{number}"
    router.post(f"{base}/restart")(routes.restart)
    router.post(f"{base}/attrs/{{attr}}/restart")(routes.restart_attribute)
    router.post(f"{base}/rebuild-failed")(routes.rebuild_failed)
    router.post(f"{base}/effects/restart")(routes.restart_effects)
    router.post(f"{base}/attrs/{{attr}}/cancel")(routes.cancel_attribute)
    router.post(f"{base}/cancel")(routes.cancel)
    router.post("/admin/repos/refresh")(routes.refresh_repos)
    router.post("/admin/repos/{project_id}/toggle")(routes.toggle_repo)
    router.post("/admin/repos/{project_id}/webhook-secret")(
        routes.regenerate_webhook_secret
    )
    return router


def create_control_api_router(
    ctx: WebContext,
    backend: ControlBackend,
    authz: AuthzConfig,
    own_url: str,
) -> APIRouter:
    """JSON control endpoints; part of the documented /api surface.
    API tokens authenticate via Authorization: Bearer."""
    router = APIRouter(prefix="/api", tags=["api"])
    routes = _ControlRoutes(ctx, backend, authz, own_url)
    base = "/repos/{forge}/{owner}/{name}"
    router.post(f"{base}/builds/{{number}}/restart", response_model=ControlAction)(
        routes.api_restart
    )
    router.post(f"{base}/builds/{{number}}/cancel", response_model=ControlAction)(
        routes.api_cancel
    )
    router.post(f"{base}/enable", response_model=EnableResult)(routes.api_set_enabled)
    router.post(f"{base}/disable", response_model=EnableResult)(routes.api_set_enabled)
    return router
