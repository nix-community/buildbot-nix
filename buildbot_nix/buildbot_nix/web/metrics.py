"""Prometheus /metrics endpoint.

Unauthenticated by design, therefore free of private repository names:
metrics are aggregated by status/state only, never labeled by project.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

if TYPE_CHECKING:
    import asyncpg


async def render_metrics(pool: asyncpg.Pool) -> str:
    # Everything here is a gauge: status transitions and retention
    # cleanup shrink the table-derived values, so they are not counters.
    lines: list[str] = []

    lines.append("# HELP buildbot_nix_builds Builds by final status.")
    lines.append("# TYPE buildbot_nix_builds gauge")
    lines.extend(
        f'buildbot_nix_builds{{status="{row["status"]}"}} {row["count"]}'
        for row in await pool.fetch(
            "SELECT status, count(*) AS count FROM builds GROUP BY status"
        )
    )

    lines.append("# HELP buildbot_nix_attributes Attribute results by status.")
    lines.append("# TYPE buildbot_nix_attributes gauge")
    lines.extend(
        f'buildbot_nix_attributes{{status="{row["status"]}"}} {row["count"]}'
        for row in await pool.fetch(
            "SELECT status, count(*) AS count FROM build_attributes GROUP BY status"
        )
    )

    queue_depth = await pool.fetchval(
        "SELECT count(*) FROM builds "
        "WHERE status IN ('pending', 'evaluating', 'building')"
    )
    lines.append("# HELP buildbot_nix_queue_depth Builds pending or running.")
    lines.append("# TYPE buildbot_nix_queue_depth gauge")
    lines.append(f"buildbot_nix_queue_depth {queue_depth}")

    duration = await pool.fetchrow(
        """
        SELECT
            coalesce(sum(extract(epoch FROM finished_at - started_at)), 0) AS total,
            count(*) AS count
        FROM builds
        WHERE started_at IS NOT NULL AND finished_at IS NOT NULL
        """
    )
    lines.append(
        "# HELP buildbot_nix_build_duration_seconds_sum "
        "Total wall time of finished builds."
    )
    lines.append("# TYPE buildbot_nix_build_duration_seconds_sum gauge")
    lines.append(f"buildbot_nix_build_duration_seconds_sum {duration['total']}")
    lines.append("# TYPE buildbot_nix_build_duration_seconds_count gauge")
    lines.append(f"buildbot_nix_build_duration_seconds_count {duration['count']}")

    projects = await pool.fetchrow(
        "SELECT count(*) FILTER (WHERE enabled) AS enabled, count(*) AS total "
        "FROM projects"
    )
    lines.append("# HELP buildbot_nix_projects Projects known/enabled.")
    lines.append("# TYPE buildbot_nix_projects gauge")
    lines.append(f'buildbot_nix_projects{{state="enabled"}} {projects["enabled"]}')
    lines.append(f'buildbot_nix_projects{{state="total"}} {projects["total"]}')

    return "\n".join(lines) + "\n"


# /metrics is unauthenticated: without a cache anyone could run the
# full-table aggregations in a loop.
CACHE_TTL = 15.0


def create_metrics_router(pool: asyncpg.Pool) -> APIRouter:
    router = APIRouter()
    cached: tuple[float, str] | None = None
    lock = asyncio.Lock()

    @router.get("/metrics", response_class=PlainTextResponse)
    async def metrics() -> PlainTextResponse:
        nonlocal cached
        async with lock:  # one query burst even under concurrent scrapes
            if cached is None or time.monotonic() - cached[0] > CACHE_TTL:
                cached = (time.monotonic(), await render_metrics(pool))
        return PlainTextResponse(
            cached[1],
            media_type="text/plain; version=0.0.4",
        )

    return router
