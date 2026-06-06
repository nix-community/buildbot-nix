"""FastAPI web frontend.

Server-rendered Jinja2, classless CSS with system-preference dark
mode. Live updates are pushed: Postgres triggers NOTIFY on status
changes, /events fans them out over SSE, and a few lines of inline
JS refetch fragments or patch rows. Per-project sequential build
numbers in URLs; prev/next navigation between builds and
per-attribute history; attributes grouped by system with
failed-first ordering and inline error excerpts.

Visibility filtering hooks (`visible_project_ids`) are wired by task
5.3b; None means everything is visible.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..recovery import check_db_health  # noqa: TID252
from .api_routes import create_api_router
from .auth_routes import SESSION_COOKIE
from .events import EventBroker, create_events_router
from .logs import LogRegistry, create_log_router
from .metrics import create_metrics_router
from .queries import PAGE_SIZE, BuildFilters, WebQueries

if TYPE_CHECKING:
    from ..api_tokens import ApiTokenStore  # noqa: TID252
    from ..auth import SessionSigner, User  # noqa: TID252
    from ..forge_tokens import TokenVault  # noqa: TID252
    from ..visibility import VisibilityService  # noqa: TID252

if TYPE_CHECKING:
    import asyncpg

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

RUNNING_STATUSES = ("pending", "evaluating", "building")
# Upper bound for live row refreshes of deep-scrolled tables.
MAX_ROWS = 500


def timeago(value: datetime | None) -> str:
    if value is None:
        return "—"
    delta = datetime.now(tz=UTC) - value
    seconds = int(delta.total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return f"{max(seconds, 0)}s ago"


def duration(row: dict[str, Any]) -> str:
    started, finished = row.get("started_at"), row.get("finished_at")
    if not started:
        return "—"
    end = finished or datetime.now(tz=UTC)
    return duration_secs((end - started).total_seconds())


def duration_secs(value: float | None) -> str:
    if value is None:
        return "—"
    seconds = int(value)
    if seconds >= 3600:  # noqa: PLR2004
        return f"{seconds // 3600}h {seconds % 3600 // 60}m"
    if seconds >= 60:  # noqa: PLR2004
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds}s"


def commit_url(project: dict[str, Any], sha: str) -> str:
    base = project["url"].removesuffix(".git")
    if project["forge"] == "github":
        return f"{base}/commit/{sha}"
    return f"{base}/commit/{sha}"


def pr_url(project: dict[str, Any], pr_number: int) -> str:
    base = project["url"].removesuffix(".git")
    if project["forge"] == "github":
        return f"{base}/pull/{pr_number}"
    return f"{base}/pulls/{pr_number}"


def branch_url(project: dict[str, Any], branch: str) -> str:
    base = project["url"].removesuffix(".git")
    if project["forge"] == "github":
        return f"{base}/tree/{quote(branch)}"
    return f"{base}/src/branch/{quote(branch)}"


def excerpt(text: str | None, limit: int = 600) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit] + " …"


def repo_name(owner: str, name: str) -> str:
    """Display name: hide the synthetic pull_based owner segment."""
    return name if owner == "pull_based" else f"{owner}/{name}"


def make_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["timeago"] = timeago
    env.filters["duration"] = duration
    env.filters["duration_secs"] = duration_secs
    env.filters["excerpt"] = excerpt
    env.globals["commit_url"] = commit_url
    env.globals["repo_name"] = repo_name
    env.globals["pr_url"] = pr_url
    env.globals["branch_url"] = branch_url
    env.globals["RUNNING_STATUSES"] = RUNNING_STATUSES
    # Header login links; the service composition fills this in.
    env.globals["login_providers"] = []
    return env


class WebContext:
    """Shared state for the routes; auth (5.3) extends this."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        state_dir: Path | None = None,
        signer: SessionSigner | None = None,
        visibility: VisibilityService | None = None,
    ) -> None:
        self.pool = pool
        self.state_dir = state_dir or Path("/var/lib/buildbot-nix")
        self.queries = WebQueries(pool)
        self.env = make_env()
        self.signer = signer
        self.visibility = visibility
        self.token_store: ApiTokenStore | None = None
        self.forge_tokens: TokenVault | None = None

    def current_user(self, request: Request) -> User | None:
        if self.signer is None:
            return None
        return self.signer.user_from(request.cookies.get(SESSION_COOKIE))

    async def request_user(self, request: Request) -> User | None:
        """Session cookie or personal API token (Authorization: Bearer).
        Cached per request: routes ask several times (authz checks,
        visibility, toggleability) and token auth hits the database."""
        if not hasattr(request.state, "bn_user"):
            request.state.bn_user = await self._request_user(request)
        return request.state.bn_user

    async def _request_user(self, request: Request) -> User | None:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer ") and self.token_store is not None:
            return await self.token_store.authenticate(
                auth_header.removeprefix("Bearer ")
            )
        return self.current_user(request)

    def render(
        self, template: str, request: Request | None = None, **context: Any
    ) -> HTMLResponse:
        if request is not None:
            context.setdefault("user", self.current_user(request))
        return HTMLResponse(self.env.get_template(template).render(**context))

    async def visible_project_ids(self, request: Request) -> list[int] | None:
        """None = all projects visible."""
        if self.visibility is None:
            return None
        return await self.visibility.visible_project_ids(
            await self.request_user(request), await self._forge_token(request)
        )

    async def toggleable_project_ids(self, request: Request) -> list[int] | None:
        """Projects the requester may enable/disable; None = all."""
        if self.visibility is None:
            return []
        return await self.visibility.toggleable_project_ids(
            await self.request_user(request), await self._forge_token(request)
        )

    async def _forge_token(self, request: Request) -> str | None:
        if not hasattr(request.state, "bn_forge_token"):
            request.state.bn_forge_token = await self._fetch_forge_token(request)
        return request.state.bn_forge_token

    async def _fetch_forge_token(self, request: Request) -> str | None:
        if self.signer is None or self.forge_tokens is None:
            return None
        session_id = self.signer.session_id_from(request.cookies.get(SESSION_COOKIE))
        if session_id is None:
            return None
        return await self.forge_tokens.get(session_id)

    async def project_or_404(
        self, owner: str, name: str, request: Request | None = None
    ) -> dict[str, Any]:
        """404 for unknown projects AND for private projects the
        requester cannot see (their existence stays hidden)."""
        project = await self.queries.project_by_name(owner, name)
        if project is None:
            raise HTTPException(status_code=404)
        if request is not None:
            visible = await self.visible_project_ids(request)
            if visible is not None and project["id"] not in visible:
                raise HTTPException(status_code=404)
        return project


def create_router(ctx: WebContext) -> APIRouter:  # noqa: C901
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    async def index(request: Request, q: str = "") -> HTMLResponse:
        visible = await ctx.visible_project_ids(request)
        # Discovery inserts repos disabled; admins enable them via
        # search, which is the only place disabled projects appear.
        toggleable = await ctx.toggleable_project_ids(request)
        disabled = [
            p
            for p in (await ctx.queries.projects(enabled=False, q=q) if q else [])
            if toggleable is None or p["id"] in toggleable
        ]
        return ctx.render(
            "index.html",
            request=request,
            q=q,
            toggleable=toggleable,
            projects=await ctx.queries.project_overview(
                project_ids=visible, q=q or None
            ),
            counts=await ctx.queries.status_counts(project_ids=visible),
            project_count=await ctx.queries.project_count(project_ids=visible),
            disabled_projects=disabled,
        )

    @router.get("/builds", response_class=HTMLResponse)
    async def activity(request: Request) -> HTMLResponse:
        visible = await ctx.visible_project_ids(request)
        return ctx.render(
            "activity.html",
            request=request,
            queue=await ctx.queries.queue(visible),
            builds=(builds := await ctx.queries.recent_builds(project_ids=visible)),
            has_more=len(builds) == PAGE_SIZE,
            more_url="/builds/rows",
        )

    @router.get("/builds/rows", response_class=HTMLResponse)
    async def activity_rows(
        request: Request, before: int | None = None, limit: int = PAGE_SIZE
    ) -> HTMLResponse:
        """Row fragments: infinite scroll (before=) and live refresh of
        the loaded rows (limit=)."""
        limit = min(max(limit, 1), MAX_ROWS)
        visible = await ctx.visible_project_ids(request)
        builds = await ctx.queries.recent_builds(
            limit=limit + 1, project_ids=visible, before=before
        )
        return ctx.render(
            "_build_rows.html",
            request=request,
            builds=builds[:limit],
            has_more=len(builds) > limit,
            more_url="/builds/rows",
        )

    @router.get("/builds/queue", response_class=HTMLResponse)
    async def queue_fragment(request: Request) -> HTMLResponse:
        visible = await ctx.visible_project_ids(request)
        return ctx.render(
            "_queue.html", request=request, queue=await ctx.queries.queue(visible)
        )

    @router.get("/projects/{owner}/{name}", response_class=HTMLResponse)
    async def project_page(  # noqa: PLR0913
        request: Request,
        owner: str,
        name: str,
        page: int = 1,
        status: str | None = None,
        ref: str | None = None,
    ) -> HTMLResponse:
        project = await ctx.project_or_404(owner, name, request)
        builds = await ctx.queries.builds_for_project(
            project["id"],
            page=page,
            filters=BuildFilters.for_ref(ref, status=status),
        )
        return ctx.render(
            "project.html",
            request=request,
            project=project,
            builds=builds,
            status=status or "",
            ref=ref or "",
        )

    @router.get("/projects/{owner}/{name}/rows", response_class=HTMLResponse)
    async def project_rows(  # noqa: PLR0913
        request: Request,
        owner: str,
        name: str,
        before: int | None = None,
        limit: int = PAGE_SIZE,
        status: str | None = None,
        ref: str | None = None,
    ) -> HTMLResponse:
        """Row fragments: infinite scroll (before=) and live refresh of
        the loaded rows (limit=)."""
        limit = min(max(limit, 1), MAX_ROWS)
        project = await ctx.project_or_404(owner, name, request)
        builds = await ctx.queries.builds_for_project(
            project["id"],
            limit=limit,
            filters=BuildFilters.for_ref(ref, status=status or None, before=before),
        )
        return ctx.render(
            "_build_rows.html",
            request=request,
            builds=builds.items,
            has_more=builds.has_next,
            more_url=f"/projects/{owner}/{name}/rows?status={status or ''}"
            f"&ref={quote(ref or '')}",
            project=project,
        )

    @router.get("/projects/{owner}/{name}/builds/{number}", response_class=HTMLResponse)
    async def build_page(
        request: Request, owner: str, name: str, number: int
    ) -> HTMLResponse:
        project = await ctx.project_or_404(owner, name, request)
        build = await ctx.queries.build_by_number(project["id"], number)
        if build is None:
            raise HTTPException(status_code=404)
        if isinstance(build.get("eval_warnings"), str):
            # asyncpg returns jsonb as a string; render plain text.
            build["eval_warnings"] = "\n\n".join(json.loads(build["eval_warnings"]))
        prev_number, next_number = await ctx.queries.neighbor_numbers(
            project["id"], number
        )
        return ctx.render(
            "build.html",
            request=request,
            project=project,
            build=build,
            attributes=await ctx.queries.attributes(build["id"]),
            prev_number=prev_number,
            next_number=next_number,
        )

    @router.get("/projects/{owner}/{name}/attrs/{attr}", response_class=HTMLResponse)
    async def attribute_history(
        request: Request, owner: str, name: str, attr: str
    ) -> HTMLResponse:
        project = await ctx.project_or_404(owner, name, request)
        return ctx.render(
            "attribute_history.html",
            request=request,
            project=project,
            attr=attr,
            history=await ctx.queries.attribute_history(project["id"], attr),
        )

    @router.get("/health", response_class=PlainTextResponse)
    async def health() -> PlainTextResponse:
        if not await check_db_health(ctx.pool):
            return PlainTextResponse("database unavailable", status_code=503)
        return PlainTextResponse("ok")

    @router.get("/llms.txt", response_class=PlainTextResponse)
    async def llms_txt() -> PlainTextResponse:
        return PlainTextResponse(LLMS_TXT)

    return router


LLMS_TXT = """\
# buildbot-nix

Nix CI. Every build evaluates a flake and builds each derivation
("attribute") of `.#checks`. JSON API under /api (OpenAPI spec:
/api/openapi.json). All endpoints support unauthenticated GET unless
the instance restricts project visibility.

## Answering "why did build X / commit Y fail?"

- GET /api/projects -> [{owner, name, ...}]
- GET /api/projects/{owner}/{name}/builds?commit={sha-prefix} -> find the build number
  (other filters: status, branch, pr_number, page)
- GET /api/projects/{owner}/{name}/builds/{number}/failures?tail=50
  -> {status, error, eval_warnings, failures: [{attr, status, error, log_tail}]}

## Other endpoints

- GET /api/projects/{owner}/{name}/builds/{number} -> build + all attributes
- GET /api/projects/{owner}/{name}/attrs/{attr} -> per-attribute history
- GET /api/queue -> global build queue
- GET /projects/{owner}/{name}/builds/{number}/logs/{attr}.txt?tail=N
  -> plain-text log (full when tail is omitted)
"""


def create_app(
    pool: asyncpg.Pool,
    state_dir: Path | None = None,
    log_registry: LogRegistry | None = None,
) -> FastAPI:
    broker = EventBroker(pool)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await broker.start()
        try:
            yield
        finally:
            await broker.stop()

    app = FastAPI(
        title="buildbot-nix", openapi_url="/api/openapi.json", lifespan=lifespan
    )
    ctx = WebContext(pool, state_dir)
    app.state.web_context = ctx
    app.include_router(create_events_router(ctx, broker))
    app.include_router(create_router(ctx))
    registry = log_registry or LogRegistry()
    app.state.log_registry = registry
    app.include_router(create_log_router(ctx, registry))
    app.include_router(create_metrics_router(pool))
    app.include_router(create_api_router(ctx))
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
