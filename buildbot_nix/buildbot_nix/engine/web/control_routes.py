"""Build control endpoints: restart build, restart a single
attribute without re-eval, rebuild-all-failed, cancel — gated by authz
(admins, PR authors for their own PR, allowUnauthenticatedControl) and
CSRF same-origin checks. Admin-only project enable/disable toggle.

The actual restart/cancel work happens behind the ControlBackend
protocol, implemented by the engine service composition
where the orchestrator lives.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from ..auth import can_control_build, is_admin, same_origin  # noqa: TID252

if TYPE_CHECKING:
    from ..auth import AuthzConfig  # noqa: TID252
    from .app import WebContext


class ControlBackend(Protocol):
    async def restart_build(self, build_id: int) -> None: ...

    async def restart_attribute(self, build_id: int, attr: str) -> None: ...

    async def cancel_build(self, build_id: int) -> None: ...


FAILED_STATUSES = ("failed", "failed_eval", "dependency_failed", "cached_failure")


def create_control_router(  # noqa: C901
    ctx: WebContext,
    backend: ControlBackend,
    authz: AuthzConfig,
    own_url: str,
) -> APIRouter:
    router = APIRouter()

    async def _authorize(request: Request, owner: str, name: str, number: int) -> dict:
        if not same_origin(request, own_url):
            raise HTTPException(status_code=403, detail="cross-origin request")
        project = await ctx.project_or_404(owner, name, request)
        build = await ctx.queries.build_by_number(project["id"], number)
        if build is None:
            raise HTTPException(status_code=404)
        user = await ctx.request_user(request)
        if not can_control_build(user, authz, build_pr_author=build.get("pr_author")):
            raise HTTPException(status_code=403, detail="not authorized")
        return build

    def _back(owner: str, name: str, number: int) -> RedirectResponse:
        return RedirectResponse(
            f"/projects/{owner}/{name}/builds/{number}", status_code=303
        )

    @router.post("/projects/{owner}/{name}/builds/{number}/restart")
    async def restart(
        request: Request, owner: str, name: str, number: int
    ) -> RedirectResponse:
        build = await _authorize(request, owner, name, number)
        await backend.restart_build(build["id"])
        return _back(owner, name, number)

    @router.post("/projects/{owner}/{name}/builds/{number}/attrs/{attr}/restart")
    async def restart_attribute(
        request: Request, owner: str, name: str, number: int, attr: str
    ) -> RedirectResponse:
        build = await _authorize(request, owner, name, number)
        # Reuses the stored eval results (drv_path); no re-eval.
        await backend.restart_attribute(build["id"], attr)
        return _back(owner, name, number)

    @router.post("/projects/{owner}/{name}/builds/{number}/rebuild-failed")
    async def rebuild_failed(
        request: Request, owner: str, name: str, number: int
    ) -> RedirectResponse:
        build = await _authorize(request, owner, name, number)
        rows = await ctx.pool.fetch(
            "SELECT attr FROM build_attributes "
            "WHERE build_id = $1 AND status = ANY($2::text[])",
            build["id"],
            list(FAILED_STATUSES),
        )
        for row in rows:
            await backend.restart_attribute(build["id"], row["attr"])
        return _back(owner, name, number)

    @router.post("/projects/{owner}/{name}/builds/{number}/cancel")
    async def cancel(
        request: Request, owner: str, name: str, number: int
    ) -> RedirectResponse:
        build = await _authorize(request, owner, name, number)
        await backend.cancel_build(build["id"])
        return _back(owner, name, number)

    @router.post("/admin/projects/{project_id}/toggle")
    async def toggle_project(request: Request, project_id: int) -> RedirectResponse:
        if not same_origin(request, own_url):
            raise HTTPException(status_code=403, detail="cross-origin request")
        if not is_admin(await ctx.request_user(request), authz):
            raise HTTPException(status_code=403, detail="admin only")
        await ctx.pool.execute(
            "UPDATE projects SET enabled = NOT enabled, updated_at = now() "
            "WHERE id = $1",
            project_id,
        )
        return RedirectResponse("/", status_code=303)

    return router
