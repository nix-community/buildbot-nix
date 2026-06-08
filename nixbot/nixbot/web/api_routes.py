"""JSON API mirroring the HTML views.

Unversioned under /api; OpenAPI documentation comes from FastAPI
(/docs, /openapi.json). Visibility rules are identical to the HTML
views; API tokens authenticate via Authorization: Bearer.
"""

from __future__ import annotations

import json
from datetime import datetime  # noqa: TC003 (pydantic needs the runtime type)
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..ansi import strip_ansi  # noqa: TID252
from .queries import BuildFilters

if TYPE_CHECKING:
    from .app import WebContext


# Typed views of the DB rows for the OpenAPI spec. Internal columns
# (next_build_number, status_generation) are deliberately absent:
# response_model filtering strips them from responses.


class Repo(BaseModel):
    """Enabled project."""

    id: int
    forge: str
    forge_repo_id: str
    owner: str
    name: str
    default_branch: str
    url: str
    private: bool
    enabled: bool
    created_at: datetime
    updated_at: datetime


class EvalWarningGroup(BaseModel):
    """Deduplicated evaluator warning streamed during the eval."""

    level: str  # warning | error
    message: str
    count: int


class Build(BaseModel):
    """One CI run of a commit."""

    id: int
    project_id: int
    number: int
    tree_hash: str | None
    commit_sha: str
    branch: str
    pr_number: int | None
    pr_author: str | None
    status: str  # pending | evaluating | building | succeeded | failed | cancelled
    eval_warnings: list[EvalWarningGroup] | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class BuildPage(BaseModel):
    items: list[Build]
    page: int
    has_next: bool


class Attribute(BaseModel):
    """One flake attribute of a build."""

    id: int
    build_id: int
    attr: str
    system: str | None
    drv_path: str | None
    outputs: dict[str, str | None] | None
    # pending | building | succeeded | failed | cancelled | skipped_local
    # | dependency_failed | cached_failure | failed_eval
    status: str
    cached: bool
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    log_path: str | None = None
    log_size: int | None = None


class BuildDetail(BaseModel):
    build: Build
    attributes: list[Attribute]


class AttributeHistoryEntry(Attribute):
    """Attribute result plus the build it belongs to."""

    build_number: int
    branch: str
    commit_sha: str
    build_created_at: datetime


class AttributeFailure(BaseModel):
    attr: str
    status: str
    error: str | None
    log_tail: str | None


class FailureSummary(BaseModel):
    """Why a build failed: failed attributes with log tails."""

    status: str
    error: str | None
    eval_warnings: list[EvalWarningGroup] | None
    failures: list[AttributeFailure]


class QueueEntry(Build):
    """Pending/running build with its FIFO position."""

    owner: str
    project_name: str
    forge: str
    url: str
    queue_position: int | None  # None while already running


def clean_row(row: dict[str, Any]) -> dict[str, Any]:
    """JSON-encodable copy of a DB row."""
    out: dict[str, Any] = {}
    for key, value in row.items():
        if hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        elif key == "error" and isinstance(value, str):
            out[key] = strip_ansi(value)
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

    @router.get("/repos", response_model=list[Repo])
    async def list_repos(request: Request) -> list[dict[str, Any]]:
        """Enabled projects visible to the requester."""
        visible = await ctx.visible_repo_ids(request)
        projects = await ctx.queries.projects()
        if visible is not None:
            projects = [p for p in projects if p["id"] in visible]
        return [clean_row(p) for p in projects]

    @router.get("/repos/{forge}/{owner:owner}/{name:segment}", response_model=Repo)
    async def get_repo(
        request: Request, forge: str, owner: str, name: str
    ) -> dict[str, Any]:
        """Single project; 404 when unknown or not visible."""
        return clean_row(await ctx.repo_or_404(forge, owner, name, request))

    @router.get(
        "/repos/{forge}/{owner:owner}/{name:segment}/builds", response_model=BuildPage
    )
    async def list_builds(  # noqa: PLR0913
        request: Request,
        forge: str,
        owner: str,
        name: str,
        page: int = 1,
        status: str | None = None,
        branch: str | None = None,
        pr_number: int | None = None,
        commit: str | None = None,
    ) -> dict[str, Any]:
        """Paginated builds with status/branch/PR/commit-prefix filters."""
        project = await ctx.repo_or_404(forge, owner, name, request)
        builds = await ctx.queries.builds_for_repo(
            project["id"],
            page=page,
            filters=BuildFilters(
                status=status, branch=branch, pr_number=pr_number, commit=commit
            ),
        )
        return {
            "items": [clean_row(b) for b in builds.items],
            "page": builds.page,
            "has_next": builds.has_next,
        }

    @router.get(
        "/repos/{forge}/{owner:owner}/{name:segment}/builds/{number}",
        response_model=BuildDetail,
    )
    async def get_build(
        request: Request, forge: str, owner: str, name: str, number: int
    ) -> dict[str, Any]:
        """Build plus all of its attributes."""
        project = await ctx.repo_or_404(forge, owner, name, request)
        build = await ctx.queries.build_by_number(project["id"], number)
        if build is None:
            raise HTTPException(status_code=404)
        attributes = await ctx.queries.attributes(build["id"])
        return {
            "build": clean_row(build),
            "attributes": [clean_row(a) for a in attributes],
        }

    @router.get(
        # :path — attribute names may contain slashes.
        "/repos/{forge}/{owner:owner}/{name:segment}/attrs/{attr:path}",
        response_model=list[AttributeHistoryEntry],
    )
    async def get_attribute_history(
        request: Request, forge: str, owner: str, name: str, attr: str
    ) -> list[dict[str, Any]]:
        """History of one attribute across a project's builds (newest first)."""
        project = await ctx.repo_or_404(forge, owner, name, request)
        return [
            clean_row(h)
            for h in await ctx.queries.attribute_history(project["id"], attr)
        ]

    @router.get("/queue", response_model=list[QueueEntry])
    async def get_queue(request: Request) -> list[dict[str, Any]]:
        """Pending/running builds with FIFO positions."""
        visible = await ctx.visible_repo_ids(request)
        return [clean_row(b) for b in await ctx.queries.queue(visible)]

    return router
