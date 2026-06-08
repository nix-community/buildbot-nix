"""Postgres-backed failed-build cache (opt-in via cacheFailedBuilds).

Storage port of db/failed_builds.py onto the service schema; the skip
semantics live in scheduler.py (cached failures skip the build,
report with a link to the first failure, and propagate to dependents;
an explicit rebuild removes the entry and builds again).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .scheduler import CachedFailure

if TYPE_CHECKING:
    import asyncpg


class PostgresFailedBuildCache:
    """Implements the scheduler's FailedBuildCache protocol, scoped
    per project: a global key would leak one project's build URL into
    another's status descriptions."""

    def __init__(self, pool: asyncpg.Pool, project_id: int) -> None:
        self.pool = pool
        self.project_id = project_id

    async def check(self, drv_path: str) -> CachedFailure | None:
        row = await self.pool.fetchrow(
            "SELECT derivation, timestamp, url FROM failed_builds "
            "WHERE project_id = $1 AND derivation = $2",
            self.project_id,
            drv_path,
        )
        if row is None:
            return None
        return CachedFailure(
            drv_path=row["derivation"],
            time=datetime.fromtimestamp(row["timestamp"], tz=UTC),
            url=row["url"],
        )

    async def add(self, drv_path: str, url: str) -> None:
        await self.pool.execute(
            """
            INSERT INTO failed_builds (project_id, derivation, timestamp, url)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (project_id, derivation)
            DO UPDATE SET timestamp = EXCLUDED.timestamp, url = EXCLUDED.url
            """,
            self.project_id,
            drv_path,
            datetime.now(tz=UTC).timestamp(),
            url,
        )

    async def remove(self, drv_path: str) -> None:
        await self.pool.execute(
            "DELETE FROM failed_builds WHERE project_id = $1 AND derivation = $2",
            self.project_id,
            drv_path,
        )
