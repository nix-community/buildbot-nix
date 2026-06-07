"""Read-side queries for the web frontend."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg

PAGE_SIZE = 50


@dataclass
class BuildFilters:
    status: str | None = None
    branch: str | None = None
    pr_number: int | None = None
    commit: str | None = None
    before: int | None = None  # cursor: only builds with a smaller id

    @classmethod
    def for_ref(cls, ref: str | None, **kwargs: Any) -> BuildFilters:
        """Parse a ref filter: "#123" or "123" means a PR, anything
        else a branch name."""
        if ref and re.fullmatch(r"#?\d+", ref):
            return cls(pr_number=int(ref.lstrip("#")), **kwargs)
        return cls(branch=ref or None, **kwargs)


def _like_escape(query: str) -> str:
    """Escape LIKE/ILIKE metacharacters so user input matches literally."""
    return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Display order: failures first, then running, then the rest.
FAILED_FIRST_ORDER = """
    CASE
        WHEN a.status IN ('failed', 'failed_eval', 'dependency_failed',
                          'cached_failure') THEN 0
        WHEN a.status = 'building' THEN 1
        WHEN a.status = 'pending' THEN 2
        WHEN a.status = 'cancelled' THEN 3
        ELSE 4
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

    async def projects(
        self, *, enabled: bool | None = True, q: str | None = None
    ) -> list[dict[str, Any]]:
        return _rows(
            await self.pool.fetch(
                """
                SELECT * FROM projects
                WHERE ($1::boolean IS NULL OR enabled = $1)
                  AND ($2::text IS NULL OR owner || '/' || name ILIKE $2)
                ORDER BY owner, name
                """,
                enabled,
                f"%{_like_escape(q)}%" if q else None,
            )
        )

    async def repo_by_name(
        self, forge: str, owner: str, name: str
    ) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM projects WHERE forge = $1 AND owner = $2 AND name = $3",
            forge,
            owner,
            name,
        )
        return dict(row) if row else None

    async def repo_candidates(self, owner: str, name: str) -> list[dict[str, Any]]:
        """All forges' rows for an unqualified owner/name; used by the
        legacy-URL redirect to pick a target."""
        return _rows(
            await self.pool.fetch(
                "SELECT * FROM projects WHERE owner = $1 AND name = $2 "
                "ORDER BY forge, id",
                owner,
                name,
            )
        )

    async def repo_overview(
        self, project_ids: list[int] | None = None, q: str | None = None
    ) -> list[dict[str, Any]]:
        """Homepage pipeline rows: each project with its latest build,
        the last ten builds (status + duration) for the bar chart, and
        speed/reliability over the last thirty builds."""
        rows = await self.pool.fetch(
            """
            SELECT p.*,
                   lb.number AS last_number, lb.status AS last_status,
                   lb.branch AS last_branch, lb.created_at AS last_created_at,
                   lb.started_at, lb.finished_at,
                   h.history, m.median_secs, m.pass_rate
            FROM projects p
            LEFT JOIN LATERAL (
                SELECT * FROM builds b WHERE b.project_id = p.id
                ORDER BY b.number DESC LIMIT 1
            ) lb ON true
            LEFT JOIN LATERAL (
                SELECT json_agg(
                    json_build_object(
                        'number', t.number, 'status', t.status,
                        'secs', EXTRACT(EPOCH FROM (t.finished_at - t.started_at))
                    )
                    ORDER BY t.number
                ) AS history
                FROM (
                    SELECT number, status, started_at, finished_at FROM builds b
                    WHERE b.project_id = p.id
                    ORDER BY number DESC LIMIT 10
                ) t
            ) h ON true
            LEFT JOIN LATERAL (
                -- Median, not mean: one build stuck behind a busy nix
                -- daemon must not dominate the typical duration.
                SELECT percentile_cont(0.5) WITHIN GROUP (
                           ORDER BY EXTRACT(EPOCH FROM (t.finished_at - t.started_at))
                       ) FILTER (WHERE t.status = 'succeeded') AS median_secs,
                       -- Cancelled builds say nothing about the code:
                       -- they must not drag reliability toward zero.
                       count(*) FILTER (WHERE t.status = 'succeeded')::float
                           / NULLIF(
                               count(*) FILTER (
                                   WHERE t.status IN ('succeeded', 'failed')
                               ), 0) AS pass_rate
                FROM (
                    SELECT status, started_at, finished_at FROM builds b
                    WHERE b.project_id = p.id
                      AND b.status IN ('succeeded', 'failed', 'cancelled')
                    ORDER BY b.number DESC LIMIT 30
                ) t
            ) m ON true
            WHERE p.enabled AND ($1::bigint[] IS NULL OR p.id = ANY($1))
              AND ($2::text IS NULL OR p.owner || '/' || p.name ILIKE $2)
            ORDER BY p.owner, p.name
            """,
            project_ids,
            f"%{_like_escape(q)}%" if q else None,
        )
        overview = _rows(rows)
        for row in overview:
            row["history"] = json.loads(row["history"]) if row["history"] else []
        return overview

    async def repo_count(self, project_ids: list[int] | None = None) -> int:
        return await self.pool.fetchval(
            """
            SELECT count(*) FROM projects
            WHERE enabled AND ($1::bigint[] IS NULL OR id = ANY($1))
            """,
            project_ids,
        )

    async def status_counts(
        self, project_ids: list[int] | None = None
    ) -> dict[str, int]:
        """Running/queued counts for the homepage summary strip."""
        rows = await self.pool.fetch(
            """
            SELECT status, count(*) AS n FROM builds
            WHERE status IN ('pending', 'evaluating', 'building')
              AND ($1::bigint[] IS NULL OR project_id = ANY($1))
            GROUP BY status
            """,
            project_ids,
        )
        return {r["status"]: r["n"] for r in rows}

    async def recent_builds(
        self,
        limit: int = 50,
        project_ids: list[int] | None = None,
        before: int | None = None,
    ) -> list[dict[str, Any]]:
        """Activity feed; cursor on build id for infinite scroll."""
        return _rows(
            await self.pool.fetch(
                """
                SELECT b.*, p.owner, p.name AS project_name, p.forge, p.url
                FROM builds b JOIN projects p ON p.id = b.project_id
                WHERE ($2::bigint[] IS NULL OR b.project_id = ANY($2))
                  AND ($3::bigint IS NULL OR b.id < $3)
                ORDER BY b.id DESC LIMIT $1
                """,
                limit,
                project_ids,
                before,
            )
        )

    async def builds_for_repo(
        self,
        project_id: int,
        *,
        page: int = 1,
        limit: int = PAGE_SIZE,
        filters: BuildFilters | None = None,
    ) -> Page:
        f = filters or BuildFilters()
        page = max(page, 1)
        conditions = ["project_id = $1"]
        args: list[Any] = [project_id]
        if f.status:
            args.append(f.status)
            conditions.append(f"status = ${len(args)}")
        if f.branch:
            args.append(f.branch)
            conditions.append(f"branch = ${len(args)}")
        if f.pr_number is not None:
            args.append(f.pr_number)
            conditions.append(f"pr_number = ${len(args)}")
        if f.commit:
            # Prefix match so agents can pass short revs.
            args.append(f.commit)
            conditions.append(f"starts_with(commit_sha, ${len(args)})")
        if f.before is not None:
            args.append(f.before)
            conditions.append(f"id < ${len(args)}")
        args.append(limit + 1)
        args.append((page - 1) * limit)
        rows = await self.pool.fetch(
            f"""
            SELECT * FROM builds WHERE {" AND ".join(conditions)}
            ORDER BY number DESC LIMIT ${len(args) - 1} OFFSET ${len(args)}
            """,  # noqa: S608
            *args,
        )
        return Page(items=_rows(rows[:limit]), page=page, has_next=len(rows) > limit)

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
        """Attributes, failed first, then by name."""
        return _rows(
            await self.pool.fetch(
                f"""
                SELECT a.*, l.path AS log_path, l.size_bytes AS log_size
                FROM build_attributes a
                LEFT JOIN logs l ON l.attribute_id = a.id
                WHERE a.build_id = $1
                ORDER BY {FAILED_FIRST_ORDER}, a.attr
                """,  # noqa: S608
                build_id,
            )
        )

    async def attribute_counts(self, build_id: int) -> dict[str, int]:
        """Attribute counts per status."""
        rows = await self.pool.fetch(
            "SELECT status, count(*) AS count FROM build_attributes"
            " WHERE build_id = $1 GROUP BY status",
            build_id,
        )
        return {r["status"]: r["count"] for r in rows}

    async def attribute_page(
        self, build_id: int, statuses: tuple[str, ...], limit: int, page: int
    ) -> Page:
        """One page of attributes with the given statuses, by name."""
        rows = await self.pool.fetch(
            """
            SELECT a.*, l.path AS log_path, l.size_bytes AS log_size
            FROM build_attributes a
            LEFT JOIN logs l ON l.attribute_id = a.id
            WHERE a.build_id = $1 AND a.status = any($2::text[])
            ORDER BY a.attr LIMIT $3 OFFSET $4
            """,
            build_id,
            list(statuses),
            limit + 1,
            (page - 1) * limit,
        )
        return Page(items=_rows(rows[:limit]), page=page, has_next=len(rows) > limit)

    async def attribute_search(
        self, build_id: int, q: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Attributes matching a substring, across all statuses."""
        pattern = "%" + _like_escape(q) + "%"
        return _rows(
            await self.pool.fetch(
                """
                SELECT a.*, l.path AS log_path, l.size_bytes AS log_size
                FROM build_attributes a
                LEFT JOIN logs l ON l.attribute_id = a.id
                WHERE a.build_id = $1 AND a.attr ILIKE $2
                ORDER BY a.attr LIMIT $3
                """,
                build_id,
                pattern,
                limit,
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

    async def queue(self, project_ids: list[int] | None = None) -> list[dict[str, Any]]:
        """Pending (FIFO position by id) and running builds."""
        project_filter = "" if project_ids is None else "AND p.id = ANY($1::bigint[])"
        args = [] if project_ids is None else [project_ids]
        return _rows(
            await self.pool.fetch(
                f"""
                SELECT b.*, p.owner, p.name AS project_name, p.forge, p.url,
                       row_number() OVER (ORDER BY b.id) AS queue_position
                FROM builds b JOIN projects p ON p.id = b.project_id
                WHERE b.status IN ('pending', 'evaluating', 'building')
                {project_filter}
                -- Active builds first; queue_position stays FIFO.
                ORDER BY b.status = 'pending', b.id
                """,  # noqa: S608
                *args,
            )
        )
