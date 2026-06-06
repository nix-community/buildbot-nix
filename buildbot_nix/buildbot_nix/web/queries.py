"""Read-side queries for the web frontend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg

PAGE_SIZE = 50


def _like_escape(query: str) -> str:
    """Escape LIKE/ILIKE metacharacters so user input matches literally."""
    return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Display order: failures first, then running, then the rest.
FAILED_FIRST_ORDER = """
    CASE
        WHEN a.status IN ('failed', 'failed_eval', 'dependency_failed',
                          'cached_failure') THEN 0
        WHEN a.status IN ('pending', 'building') THEN 1
        WHEN a.status = 'cancelled' THEN 2
        ELSE 3
    END
"""


@dataclass(frozen=True)
class Page:
    items: list[dict[str, Any]]
    page: int
    has_next: bool


def _rows(records: list[asyncpg.Record]) -> list[dict[str, Any]]:
    return [dict(r) for r in records]


class WebQueries:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def projects(self, *, enabled_only: bool = True) -> list[dict[str, Any]]:
        where = "WHERE enabled" if enabled_only else ""
        return _rows(
            await self.pool.fetch(
                f"SELECT * FROM projects {where} ORDER BY owner, name"  # noqa: S608
            )
        )

    async def project_by_name(self, owner: str, name: str) -> dict[str, Any] | None:
        # (owner, name) is only unique per forge; until URLs carry the
        # forge, pick deterministically rather than by planner row order.
        row = await self.pool.fetchrow(
            "SELECT * FROM projects WHERE owner = $1 AND name = $2 "
            "ORDER BY forge, id LIMIT 1",
            owner,
            name,
        )
        return dict(row) if row else None

    async def recent_builds(
        self, limit: int = 30, project_ids: list[int] | None = None
    ) -> list[dict[str, Any]]:
        """Homepage feed; optionally restricted to visible projects."""
        if project_ids is None:
            return _rows(
                await self.pool.fetch(
                    """
                    SELECT b.*, p.owner, p.name AS project_name
                    FROM builds b JOIN projects p ON p.id = b.project_id
                    ORDER BY b.id DESC LIMIT $1
                    """,
                    limit,
                )
            )
        return _rows(
            await self.pool.fetch(
                """
                SELECT b.*, p.owner, p.name AS project_name
                FROM builds b JOIN projects p ON p.id = b.project_id
                WHERE b.project_id = ANY($2::bigint[])
                ORDER BY b.id DESC LIMIT $1
                """,
                limit,
                project_ids,
            )
        )

    async def builds_for_project(
        self,
        project_id: int,
        *,
        page: int = 1,
        status: str | None = None,
        branch: str | None = None,
        commit: str | None = None,
    ) -> Page:
        page = max(page, 1)
        conditions = ["project_id = $1"]
        args: list[Any] = [project_id]
        if status:
            args.append(status)
            conditions.append(f"status = ${len(args)}")
        if branch:
            args.append(branch)
            conditions.append(f"branch = ${len(args)}")
        if commit:
            # Prefix match so agents can pass short revs.
            args.append(commit)
            conditions.append(f"starts_with(commit_sha, ${len(args)})")
        args.append(PAGE_SIZE + 1)
        args.append((page - 1) * PAGE_SIZE)
        rows = await self.pool.fetch(
            f"""
            SELECT * FROM builds WHERE {" AND ".join(conditions)}
            ORDER BY number DESC LIMIT ${len(args) - 1} OFFSET ${len(args)}
            """,  # noqa: S608
            *args,
        )
        return Page(
            items=_rows(rows[:PAGE_SIZE]), page=page, has_next=len(rows) > PAGE_SIZE
        )

    async def build_by_number(
        self, project_id: int, number: int
    ) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM builds WHERE project_id = $1 AND number = $2",
            project_id,
            number,
        )
        return dict(row) if row else None

    async def neighbor_numbers(
        self, project_id: int, number: int
    ) -> tuple[int | None, int | None]:
        """Prev/next build numbers within the project."""
        prev_number = await self.pool.fetchval(
            "SELECT max(number) FROM builds WHERE project_id = $1 AND number < $2",
            project_id,
            number,
        )
        next_number = await self.pool.fetchval(
            "SELECT min(number) FROM builds WHERE project_id = $1 AND number > $2",
            project_id,
            number,
        )
        return prev_number, next_number

    async def attributes(self, build_id: int) -> list[dict[str, Any]]:
        """Attributes grouped client-side by system, failed first."""
        return _rows(
            await self.pool.fetch(
                f"""
                SELECT a.*, l.path AS log_path, l.size_bytes AS log_size
                FROM build_attributes a
                LEFT JOIN logs l ON l.attribute_id = a.id
                WHERE a.build_id = $1
                ORDER BY {FAILED_FIRST_ORDER}, a.system, a.attr
                """,  # noqa: S608
                build_id,
            )
        )

    async def attribute_history(
        self, project_id: int, attr: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Results of the same attribute across a project's builds."""
        return _rows(
            await self.pool.fetch(
                """
                SELECT a.*, b.number AS build_number, b.branch, b.commit_sha,
                       b.created_at AS build_created_at
                FROM build_attributes a
                JOIN builds b ON b.id = a.build_id
                WHERE b.project_id = $1 AND a.attr = $2
                ORDER BY b.number DESC LIMIT $3
                """,
                project_id,
                attr,
                limit,
            )
        )

    async def attribute_neighbors(
        self, project_id: int, attr: str, build_number: int
    ) -> tuple[int | None, int | None]:
        """Prev/next build numbers containing the same attribute."""
        prev_number = await self.pool.fetchval(
            """
            SELECT max(b.number) FROM build_attributes a
            JOIN builds b ON b.id = a.build_id
            WHERE b.project_id = $1 AND a.attr = $2 AND b.number < $3
            """,
            project_id,
            attr,
            build_number,
        )
        next_number = await self.pool.fetchval(
            """
            SELECT min(b.number) FROM build_attributes a
            JOIN builds b ON b.id = a.build_id
            WHERE b.project_id = $1 AND a.attr = $2 AND b.number > $3
            """,
            project_id,
            attr,
            build_number,
        )
        return prev_number, next_number

    async def search(
        self, query: str, limit: int = 50, project_ids: list[int] | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        """Substring search over projects and attributes."""
        pattern = f"%{_like_escape(query)}%"
        project_filter = "" if project_ids is None else "AND p.id = ANY($3::bigint[])"
        args: list[Any] = [pattern, limit]
        if project_ids is not None:
            args.append(project_ids)
        projects = await self.pool.fetch(
            f"""
            SELECT p.* FROM projects p
            WHERE (p.owner || '/' || p.name) ILIKE $1 {project_filter}
            ORDER BY p.owner, p.name LIMIT $2
            """,  # noqa: S608
            *args,
        )
        attributes = await self.pool.fetch(
            f"""
            SELECT DISTINCT ON (b.project_id, a.attr)
                   a.attr, a.status, b.number AS build_number,
                   p.owner, p.name AS project_name
            FROM build_attributes a
            JOIN builds b ON b.id = a.build_id
            JOIN projects p ON p.id = b.project_id
            WHERE a.attr ILIKE $1 {project_filter}
            ORDER BY b.project_id, a.attr, b.number DESC
            LIMIT $2
            """,  # noqa: S608
            *args,
        )
        return {"projects": _rows(projects), "attributes": _rows(attributes)}

    async def queue(self, project_ids: list[int] | None = None) -> list[dict[str, Any]]:
        """Pending (FIFO position by id) and running builds."""
        project_filter = "" if project_ids is None else "AND p.id = ANY($1::bigint[])"
        args = [] if project_ids is None else [project_ids]
        return _rows(
            await self.pool.fetch(
                f"""
                SELECT b.*, p.owner, p.name AS project_name,
                       row_number() OVER (ORDER BY b.id) AS queue_position
                FROM builds b JOIN projects p ON p.id = b.project_id
                WHERE b.status IN ('pending', 'evaluating', 'building')
                {project_filter}
                ORDER BY b.id
                """,  # noqa: S608
                *args,
            )
        )
