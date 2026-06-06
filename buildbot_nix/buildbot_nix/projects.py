"""Project store: DB-backed enablement keyed by stable forge repo ID.

Discovery (engine/forge.py) feeds repos in; rows are upserted on
(forge, forge_repo_id) so renames/transfers keep history and the
enablement flag. Enablement is toggled by admins in the web UI; the
legacy topic filter is imported once, on the first startup with an
empty projects table.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg

    from .forge import DiscoveredRepo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProjectRecord:
    id: int
    forge: str
    forge_repo_id: str
    owner: str
    name: str
    default_branch: str
    url: str
    private: bool
    enabled: bool


def _record(row: asyncpg.Record) -> ProjectRecord:
    return ProjectRecord(
        id=row["id"],
        forge=row["forge"],
        forge_repo_id=row["forge_repo_id"],
        owner=row["owner"],
        name=row["name"],
        default_branch=row["default_branch"],
        url=row["url"],
        private=row["private"],
        enabled=row["enabled"],
    )


class ProjectStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def is_empty(self) -> bool:
        return await self.pool.fetchval("SELECT count(*) FROM projects") == 0

    async def sync_discovered(
        self,
        repos: list[DiscoveredRepo],
        *,
        legacy_import_topic: str | None = None,
    ) -> None:
        """Upsert discovered repos. When `legacy_import_topic` is given
        (only on first startup with an empty table), repos carrying that
        topic are enabled — a one-shot import of the old topic-based
        project selection."""
        do_import = legacy_import_topic is not None and await self.is_empty()
        async with self.pool.acquire() as conn, conn.transaction():
            for repo in repos:
                enabled = bool(
                    do_import
                    and legacy_import_topic is not None
                    and legacy_import_topic in repo.topics
                )
                await conn.execute(
                    """
                    INSERT INTO projects
                        (forge, forge_repo_id, owner, name, default_branch,
                         url, private, enabled)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (forge, forge_repo_id) DO UPDATE SET
                        owner = EXCLUDED.owner,
                        name = EXCLUDED.name,
                        default_branch = EXCLUDED.default_branch,
                        url = EXCLUDED.url,
                        private = EXCLUDED.private,
                        updated_at = now()
                    """,
                    repo.forge,
                    repo.forge_repo_id,
                    repo.owner,
                    repo.repo,
                    repo.default_branch,
                    repo.clone_url,
                    repo.private,
                    enabled,
                )
        if do_import:
            count = await self.pool.fetchval(
                "SELECT count(*) FROM projects WHERE enabled"
            )
            logger.info(
                "legacy topic import complete",
                extra={"topic": legacy_import_topic, "enabled": count},
            )

    async def sync_pull_based(self, repos: list[tuple[str, str, str]]) -> None:
        """Upsert pull-based repositories as (name, url, default_branch).

        Listing a repository in the static config is the enablement
        decision, so new rows start enabled; the admin toggle is
        preserved on conflict."""
        for name, url, default_branch in repos:
            owner, _, repo = name.rpartition("/")
            await self.pool.execute(
                """
                INSERT INTO projects
                    (forge, forge_repo_id, owner, name, default_branch,
                     url, private, enabled)
                VALUES ('pull_based', $1, $2, $3, $4, $5, FALSE, TRUE)
                ON CONFLICT (forge, forge_repo_id) DO UPDATE SET
                    default_branch = EXCLUDED.default_branch,
                    url = EXCLUDED.url,
                    updated_at = now()
                """,
                name,
                owner or "pull_based",
                repo,
                default_branch,
                url,
            )

    async def set_enabled(self, project_id: int, *, enabled: bool) -> None:
        await self.pool.execute(
            "UPDATE projects SET enabled = $2, updated_at = now() WHERE id = $1",
            project_id,
            enabled,
        )

    async def enabled_projects(self) -> list[ProjectRecord]:
        rows = await self.pool.fetch(
            "SELECT * FROM projects WHERE enabled ORDER BY owner, name"
        )
        return [_record(row) for row in rows]

    async def all_projects(self) -> list[ProjectRecord]:
        rows = await self.pool.fetch("SELECT * FROM projects ORDER BY owner, name")
        return [_record(row) for row in rows]

    async def by_id(self, project_id: int) -> ProjectRecord | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM projects WHERE id = $1", project_id
        )
        return _record(row) if row else None

    async def by_forge_id(self, forge: str, forge_repo_id: str) -> ProjectRecord | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM projects WHERE forge = $1 AND forge_repo_id = $2",
            forge,
            forge_repo_id,
        )
        return _record(row) if row else None
