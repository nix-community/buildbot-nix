"""Jinja environment, template filters/globals, and static-asset
serving shared by the web routes."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from ..ansi import strip_ansi  # noqa: TID252
from .logs import ansi_to_html

if TYPE_CHECKING:
    from fastapi.responses import Response

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

RUNNING_STATUSES = ("pending", "evaluating", "building")


class CachedStaticFiles(StaticFiles):
    """Static assets are immutable: their URLs carry a content hash
    (static_v), so caches may keep them forever."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def _static_version() -> str:
    """Content hash over the static assets; bound into asset URLs so
    browser caches roll over on deploy."""
    digest = hashlib.sha256()
    for f in sorted(STATIC_DIR.rglob("*")):
        if f.is_file():
            digest.update(f.read_bytes())
    return digest.hexdigest()[:12]


def timeago(value: datetime | None) -> str:
    if value is None:
        return "—"
    delta = datetime.now(tz=UTC) - value
    seconds = int(delta.total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return f"{max(seconds, 0)}s ago"


def timeuntil(value: datetime | None) -> str:
    if value is None:
        return "—"
    seconds = int((value - datetime.now(tz=UTC)).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"in {seconds // size}{unit}"
    return f"in {max(seconds, 0)}s"


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


def _forge_base(project: dict[str, Any]) -> str | None:
    """Web URL base for forge links, or None for pull_based projects
    with a non-http clone URL (an SSH remote makes no valid href)."""
    url: str = project["url"]
    if project["forge"] == "pull_based" and not url.startswith(("http://", "https://")):
        return None
    return url.removesuffix(".git")


def commit_url(project: dict[str, Any], sha: str) -> str:
    base = _forge_base(project)
    if base is None:
        return ""
    if project["forge"] == "gitlab":
        return f"{base}/-/commit/{sha}"
    # GitHub and Gitea share the commit URL scheme.
    return f"{base}/commit/{sha}"


def repo_path(project: dict[str, Any]) -> str:
    """Internal page path; accepts project rows and build rows joined
    with the project (which carry the name as project_name)."""
    name = project.get("name") or project["project_name"]
    return f"/repos/{project['forge']}/{project['owner']}/{name}"


def build_path(project: dict[str, Any], number: int) -> str:
    return f"{repo_path(project)}/builds/{number}"


def pr_url(project: dict[str, Any], pr_number: int) -> str:
    base = _forge_base(project)
    if base is None:
        return ""
    if project["forge"] == "github":
        return f"{base}/pull/{pr_number}"
    if project["forge"] == "gitlab":
        return f"{base}/-/merge_requests/{pr_number}"
    return f"{base}/pulls/{pr_number}"


def branch_url(project: dict[str, Any], branch: str) -> str:
    base = _forge_base(project)
    if base is None:
        return ""
    if project["forge"] == "github":
        return f"{base}/tree/{quote(branch)}"
    if project["forge"] == "gitlab":
        return f"{base}/-/tree/{quote(branch)}"
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
    env.globals["static_v"] = _static_version()
    env.filters["timeago"] = timeago
    env.filters["timeuntil"] = timeuntil
    env.filters["duration"] = duration
    env.filters["duration_secs"] = duration_secs
    env.filters["excerpt"] = excerpt
    env.filters["plain"] = strip_ansi
    # ansi_to_html escapes its input before adding span tags.
    env.filters["ansi"] = lambda text: Markup(ansi_to_html(text or ""))  # noqa: S704
    env.globals["commit_url"] = commit_url
    env.globals["repo_name"] = repo_name
    env.globals["pr_url"] = pr_url
    env.globals["branch_url"] = branch_url
    env.globals["repo_path"] = repo_path
    env.globals["build_path"] = build_path
    env.globals["RUNNING_STATUSES"] = RUNNING_STATUSES
    # Header login links; the service composition fills this in.
    env.globals["login_providers"] = []
    return env
