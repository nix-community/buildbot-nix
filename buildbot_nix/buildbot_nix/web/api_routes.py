"""JSON API mirroring the HTML views.

Unversioned under /api; OpenAPI documentation comes from FastAPI
(/docs, /openapi.json). Visibility rules are identical to the HTML
views; API tokens authenticate via Authorization: Bearer.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request

from .queries import BuildFilters

if TYPE_CHECKING:
    from .app import WebContext


def _clean(row: dict[str, Any]) -> dict[str, Any]:
    """JSON-encodable copy of a DB row."""
    out: dict[str, Any] = {}
    for key, value in row.items():
        if hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        elif key in ("outputs", "eval_warnings") and isinstance(value, str):
            try:
                out[key] = json.loads(value)
            except json.JSONDecodeError:
                out[key] = value
        else:
            out[key] = value
    return out


def create_api_router(ctx: WebContext) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["api"])

    @router.get("/projects")
    async def list_projects(request: Request) -> list[dict[str, Any]]:
        """Enabled projects visible to the requester."""
        visible = await ctx.visible_project_ids(request)
        projects = await ctx.queries.projects()
        if visible is not None:
            projects = [p for p in projects if p["id"] in visible]
        return [_clean(p) for p in projects]

    @router.get("/projects/{owner}/{name}")
    async def get_project(request: Request, owner: str, name: str) -> dict[str, Any]:
        return _clean(await ctx.project_or_404(owner, name, request))

    @router.get("/projects/{owner}/{name}/builds")
    async def list_builds(  # noqa: PLR0913
        request: Request,
        owner: str,
        name: str,
        page: int = 1,
        status: str | None = None,
        branch: str | None = None,
        commit: str | None = None,
    ) -> dict[str, Any]:
        """Paginated builds with status/branch/commit-prefix filters."""
        project = await ctx.project_or_404(owner, name, request)
        builds = await ctx.queries.builds_for_project(
            project["id"],
            page=page,
            filters=BuildFilters(status=status, branch=branch, commit=commit),
        )
        return {
            "items": [_clean(b) for b in builds.items],
            "page": builds.page,
            "has_next": builds.has_next,
        }

    @router.get("/projects/{owner}/{name}/builds/{number}")
    async def get_build(
        request: Request, owner: str, name: str, number: int
    ) -> dict[str, Any]:
        project = await ctx.project_or_404(owner, name, request)
        build = await ctx.queries.build_by_number(project["id"], number)
        if build is None:
            raise HTTPException(status_code=404)
        attributes = await ctx.queries.attributes(build["id"])
        return {
            "build": _clean(build),
            "attributes": [_clean(a) for a in attributes],
        }

    @router.get("/projects/{owner}/{name}/attrs/{attr}")
    async def get_attribute_history(
        request: Request, owner: str, name: str, attr: str
    ) -> list[dict[str, Any]]:
        project = await ctx.project_or_404(owner, name, request)
        return [
            _clean(h) for h in await ctx.queries.attribute_history(project["id"], attr)
        ]

    @router.get("/queue")
    async def get_queue(request: Request) -> list[dict[str, Any]]:
        """Pending/running builds with FIFO positions."""
        visible = await ctx.visible_project_ids(request)
        return [_clean(b) for b in await ctx.queries.queue(visible)]

    return router
