"""Build control endpoints: restart build, restart a single
attribute without re-eval, rebuild-all-failed, cancel build or a
single attribute — gated by authz
(admins, PR authors for their own PR, allowUnauthenticatedControl) and
CSRF same-origin checks. Admin-only project enable/disable toggle.

The actual restart/cancel work happens behind the ControlBackend
protocol, implemented by the engine service composition
where the orchestrator lives.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Protocol
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from ..auth import can_control_build, is_admin, same_origin  # noqa: TID252

if TYPE_CHECKING:
    from ..auth import AuthzConfig  # noqa: TID252
    from .app import WebContext


class ControlBackend(Protocol):
    async def restart_build(self, build_id: int) -> None: ...

    async def restart_attribute(self, build_id: int, attr: str) -> None: ...

    async def cancel_build(self, build_id: int) -> None: ...

    async def cancel_attribute(self, build_id: int, attr: str) -> None: ...


FAILED_STATUSES = ("failed", "failed_eval", "dependency_failed", "cached_failure")


def create_control_router(  # noqa: C901
    ctx: WebContext,
    backend: ControlBackend,
    authz: AuthzConfig,
    own_url: str,
) -> APIRouter:
    router = APIRouter()

    async def _authorize(
        request: Request, forge: str, owner: str, name: str, number: int
    ) -> dict:
        if not same_origin(request, own_url):
            raise HTTPException(status_code=403, detail="cross-origin request")
        project = await ctx.repo_or_404(forge, owner, name, request)
        build = await ctx.queries.build_by_number(project["id"], number)
        if build is None:
            raise HTTPException(status_code=404)
        user = await ctx.request_user(request)
        if not can_control_build(user, authz, build_pr_author=build.get("pr_author")):
            raise HTTPException(status_code=403, detail="not authorized")
        return build

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

    @router.post("/repos/{forge}/{owner}/{name}/builds/{number}/restart")
    async def restart(
        request: Request, forge: str, owner: str, name: str, number: int
    ) -> RedirectResponse:
        build = await _authorize(request, forge, owner, name, number)
        await backend.restart_build(build["id"])
        return _back(forge, owner, name, number)

    @router.post("/repos/{forge}/{owner}/{name}/builds/{number}/attrs/{attr}/restart")
    async def restart_attribute(  # noqa: PLR0913
        request: Request, forge: str, owner: str, name: str, number: int, attr: str
    ) -> RedirectResponse:
        build = await _authorize(request, forge, owner, name, number)
        # Reuses the stored eval results (drv_path); no re-eval.
        await backend.restart_attribute(build["id"], attr)
        return _back_to_attr(forge, owner, name, number, attr)

    @router.post("/repos/{forge}/{owner}/{name}/builds/{number}/rebuild-failed")
    async def rebuild_failed(
        request: Request, forge: str, owner: str, name: str, number: int
    ) -> RedirectResponse:
        build = await _authorize(request, forge, owner, name, number)
        rows = await ctx.pool.fetch(
            "SELECT attr FROM build_attributes "
            "WHERE build_id = $1 AND status = ANY($2::text[])",
            build["id"],
            list(FAILED_STATUSES),
        )
        for row in rows:
            await backend.restart_attribute(build["id"], row["attr"])
        return _back(forge, owner, name, number)

    @router.post("/repos/{forge}/{owner}/{name}/builds/{number}/attrs/{attr}/cancel")
    async def cancel_attribute(  # noqa: PLR0913
        request: Request, forge: str, owner: str, name: str, number: int, attr: str
    ) -> RedirectResponse:
        build = await _authorize(request, forge, owner, name, number)
        await backend.cancel_attribute(build["id"], attr)
        return _back_to_attr(forge, owner, name, number, attr)

    @router.post("/repos/{forge}/{owner}/{name}/builds/{number}/cancel")
    async def cancel(
        request: Request, forge: str, owner: str, name: str, number: int
    ) -> RedirectResponse:
        build = await _authorize(request, forge, owner, name, number)
        await backend.cancel_build(build["id"])
        return _back(forge, owner, name, number)

    async def _require_repo_admin(request: Request, project_id: int) -> None:
        """Instance admins, or forge-side admins of this repo."""
        if not same_origin(request, own_url):
            raise HTTPException(status_code=403, detail="cross-origin request")
        if not is_admin(await ctx.request_user(request), authz):
            toggleable = await ctx.toggleable_repo_ids(request) or []
            if project_id not in toggleable:
                raise HTTPException(status_code=403, detail="not a project admin")

    @router.post("/api/repos/{forge}/{owner}/{name}/enable")
    @router.post("/api/repos/{forge}/{owner}/{name}/disable")
    async def api_set_enabled(
        request: Request, forge: str, owner: str, name: str
    ) -> dict:
        """Idempotent enable/disable for scripts and agents."""
        project = await ctx.repo_or_404(forge, owner, name, request)
        await _require_repo_admin(request, project["id"])
        enabled = request.url.path.endswith("/enable")
        await ctx.pool.execute(
            "UPDATE projects SET enabled = $2, updated_at = now() WHERE id = $1",
            project["id"],
            enabled,
        )
        return {"owner": owner, "name": name, "enabled": enabled}

    @router.post("/admin/repos/{project_id}/toggle")
    async def toggle_repo(
        request: Request, project_id: int, q: Annotated[str, Form()] = ""
    ) -> RedirectResponse:
        await _require_repo_admin(request, project_id)
        await ctx.pool.execute(
            "UPDATE projects SET enabled = NOT enabled, updated_at = now() "
            "WHERE id = $1",
            project_id,
        )
        # Back to the dashboard with the project filter intact.
        return RedirectResponse(f"/?q={quote(q)}" if q else "/", status_code=303)

    return router
