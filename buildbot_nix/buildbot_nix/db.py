"""Engine data access layer (asyncpg).

Key invariants:

- build identity is the post-merge tree hash: a second change event
  producing the same tree for the same project reuses the existing
  build instead of creating a new one,
- attribute completion is one transactional write (status + log
  metadata together),
- re-aggregation of a build's result is serialized per build via a row
  lock (SELECT ... FOR UPDATE) and bumps a monotonic status generation
  so stale forge status posts can be dropped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .models import NixEvalJobSuccess

if TYPE_CHECKING:
    from collections.abc import Sequence

    import asyncpg

    from .models import NixEvalJob
    from .scheduler import AttributeResult


class BuildStatus:
    PENDING = "pending"
    EVALUATING = "evaluating"
    BUILDING = "building"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    TERMINAL = frozenset({SUCCEEDED, FAILED, CANCELLED})


# Attribute statuses that count as failures when aggregating.
FAILED_ATTRIBUTE_STATUSES = frozenset(
    {"failed", "failed_eval", "dependency_failed", "cached_failure"}
)
TERMINAL_ATTRIBUTE_STATUSES = FAILED_ATTRIBUTE_STATUSES | frozenset(
    {"succeeded", "cancelled", "skipped_local", "skipped"}
)


@dataclass(frozen=True)
class BuildRecord:
    id: int
    project_id: int
    number: int
    tree_hash: str | None
    commit_sha: str
    branch: str
    pr_number: int | None
    status: str
    status_generation: int
    effects_started: bool


def _build_record(row: asyncpg.Record) -> BuildRecord:
    return BuildRecord(
        id=row["id"],
        project_id=row["project_id"],
        number=row["number"],
        tree_hash=row["tree_hash"],
        commit_sha=row["commit_sha"],
        branch=row["branch"],
        pr_number=row["pr_number"],
        status=row["status"],
        status_generation=row["status_generation"],
        effects_started=row["effects_started"],
    )


class BuildDB:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    # -- builds ---------------------------------------------------------

    async def get_or_create_build(  # noqa: PLR0913
        self,
        project_id: int,
        tree_hash: str,
        commit_sha: str,
        branch: str,
        pr_number: int | None = None,
        pr_author: str | None = None,
    ) -> tuple[BuildRecord, bool]:
        """Reuse keyed on post-merge tree hash across contexts."""
        async with self.pool.acquire() as conn, conn.transaction():
            # No unique constraint exists on (project_id, tree_hash);
            # serialize creators or concurrent events insert duplicates.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"{project_id}:{tree_hash}",
            )
            row = await conn.fetchrow(
                # A cancelled build carries no verdict; never reuse it.
                "SELECT * FROM builds WHERE project_id = $1 AND tree_hash = $2 "
                "AND status <> 'cancelled' ORDER BY id DESC LIMIT 1",
                project_id,
                tree_hash,
            )
            if row is not None:
                if row["pr_author"] is not None and pr_number != row["pr_number"]:
                    # Reused in another context (e.g. the default branch
                    # after the PR merged); PR-author control is scoped
                    # to the author's own PR.
                    row = await conn.fetchrow(
                        "UPDATE builds SET pr_author = NULL WHERE id = $1 RETURNING *",
                        row["id"],
                    )
                elif (pr_number is not None and row["pr_number"] is None) or (
                    pr_author is not None and row["pr_author"] is None
                ):
                    # Backfill PR identity for the pr_author authz rule
                    # when the branch push created the build first.
                    row = await conn.fetchrow(
                        "UPDATE builds SET "
                        "pr_number = COALESCE(pr_number, $2), "
                        "pr_author = COALESCE(pr_author, $3) "
                        "WHERE id = $1 RETURNING *",
                        row["id"],
                        pr_number,
                        pr_author,
                    )
                return _build_record(row), False
            number = await conn.fetchval(
                "UPDATE projects SET next_build_number = next_build_number + 1 "
                "WHERE id = $1 RETURNING next_build_number - 1",
                project_id,
            )
            row = await conn.fetchrow(
                """
                INSERT INTO builds (project_id, number, tree_hash, commit_sha,
                                    branch, pr_number, pr_author)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING *
                """,
                project_id,
                number,
                tree_hash,
                commit_sha,
                branch,
                pr_number,
                pr_author,
            )
            return _build_record(row), True

    async def create_failed_build(  # noqa: PLR0913
        self,
        project_id: int,
        commit_sha: str,
        branch: str,
        error: str,
        pr_number: int | None = None,
        pr_author: str | None = None,
    ) -> BuildRecord:
        """A build that failed before evaluation (e.g. merge conflict);
        no tree hash exists, the status is reported on the head SHA."""
        async with self.pool.acquire() as conn, conn.transaction():
            number = await conn.fetchval(
                "UPDATE projects SET next_build_number = next_build_number + 1 "
                "WHERE id = $1 RETURNING next_build_number - 1",
                project_id,
            )
            row = await conn.fetchrow(
                """
                INSERT INTO builds (project_id, number, commit_sha, branch,
                                    pr_number, pr_author, status, error,
                                    finished_at)
                VALUES ($1, $2, $3, $4, $5, $6, 'failed', $7, now())
                RETURNING *
                """,
                project_id,
                number,
                commit_sha,
                branch,
                pr_number,
                pr_author,
                error,
            )
            return _build_record(row)

    async def set_build_status(
        self,
        build_id: int,
        status: str,
        *,
        error: str | None = None,
        eval_warnings: str | None = None,
    ) -> None:
        await self.pool.execute(
            """
            UPDATE builds
            SET status = $2,
                error = COALESCE($3, error),
                eval_warnings = COALESCE($4::jsonb, eval_warnings),
                started_at = CASE
                    WHEN started_at IS NULL AND $2 <> 'pending' THEN now()
                    ELSE started_at
                END,
                -- Invariant: non-terminal states never carry finished_at,
                -- else reruns show negative durations.
                finished_at = CASE
                    WHEN $2 IN ('succeeded', 'failed', 'cancelled') THEN now()
                    ELSE NULL
                END
            WHERE id = $1
            """,
            build_id,
            status,
            error,
            eval_warnings,
        )

    async def get_build(self, build_id: int) -> BuildRecord | None:
        row = await self.pool.fetchrow("SELECT * FROM builds WHERE id = $1", build_id)
        return _build_record(row) if row else None

    async def mark_effects_started(self, build_id: int) -> bool:
        """Set the started-flag; returns False when it was already set
        (effects must never auto-re-run)."""
        result = await self.pool.fetchval(
            "UPDATE builds SET effects_started = TRUE "
            "WHERE id = $1 AND effects_started = FALSE RETURNING id",
            build_id,
        )
        return result is not None

    # -- attributes -----------------------------------------------------

    async def record_attributes(
        self, build_id: int, jobs: Sequence[NixEvalJob]
    ) -> None:
        """Persist eval results as pending rows (with statically-known
        outputs) so crash recovery can resume without a re-eval; eval
        failures are settled by the scheduler."""
        params = [
            (build_id, job.attr, job.system, job.drv_path, json.dumps(job.outputs))
            for job in jobs
            if isinstance(job, NixEvalJobSuccess)
        ]
        if not params:
            return
        # executemany pipelines the inserts in one implicit transaction;
        # large evals produce thousands of attributes.
        await self.pool.executemany(
            """
            INSERT INTO build_attributes
                (build_id, attr, system, drv_path, outputs, status)
            VALUES ($1, $2, $3, $4, $5::jsonb, 'pending')
            ON CONFLICT (build_id, attr) DO NOTHING
            """,
            params,
        )

    async def settle_unfinished_attributes(self, build_id: int) -> None:
        """Mark pending/building rows cancelled. Builds that end without
        a normal aggregation (eval failure, supersedure) must not leave
        attributes that look like they are still running."""
        await self.pool.execute(
            """
            UPDATE build_attributes
            SET status = 'cancelled', finished_at = now()
            WHERE build_id = $1 AND status IN ('pending', 'building')
            """,
            build_id,
        )

    async def mark_attribute_building(
        self, build_id: int, attr: str, system: str | None, drv_path: str | None
    ) -> None:
        """Flip an attribute to 'building' and stamp started_at so the
        web UI can distinguish running attributes from queued ones."""
        await self.pool.execute(
            """
            INSERT INTO build_attributes
                (build_id, attr, system, drv_path, status, started_at)
            VALUES ($1, $2, $3, $4, 'building', now())
            ON CONFLICT (build_id, attr) DO UPDATE SET
                status = 'building',
                started_at = now(),
                finished_at = NULL
            """,
            build_id,
            attr,
            system,
            drv_path,
        )

    async def complete_attribute(
        self,
        build_id: int,
        result: AttributeResult,
        *,
        log_path: str | None = None,
        log_size: int = 0,
        log_truncated: bool = False,
    ) -> None:
        """Single transactional write: status, outputs, error and log
        metadata together (crash-recovery invariant)."""
        async with self.pool.acquire() as conn, conn.transaction():
            attr_id = await conn.fetchval(
                """
                INSERT INTO build_attributes
                    (build_id, attr, system, drv_path, outputs, status, error,
                     cached, finished_at)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, now())
                ON CONFLICT (build_id, attr) DO UPDATE SET
                    status = EXCLUDED.status,
                    outputs = EXCLUDED.outputs,
                    error = EXCLUDED.error,
                    cached = EXCLUDED.cached,
                    finished_at = now()
                RETURNING id
                """,
                build_id,
                result.attr,
                result.system,
                result.drv_path,
                json.dumps({"out": result.out_path}) if result.out_path else None,
                result.status.value,
                result.error,
                result.status.value == "skipped_local",
            )
            if log_path is not None:
                # Reruns rewrite the same log file; replace the
                # metadata row instead of accumulating duplicates.
                await conn.execute("DELETE FROM logs WHERE attribute_id = $1", attr_id)
                await conn.execute(
                    "INSERT INTO logs (attribute_id, path, size_bytes, truncated) "
                    "VALUES ($1, $2, $3, $4)",
                    attr_id,
                    log_path,
                    log_size,
                    log_truncated,
                )

    async def get_attribute_statuses(self, build_id: int) -> dict[str, str]:
        rows = await self.pool.fetch(
            "SELECT attr, status FROM build_attributes WHERE build_id = $1",
            build_id,
        )
        return {row["attr"]: row["status"] for row in rows}

    # -- aggregation ------------------------------------------------------

    async def aggregate_build(self, build_id: int) -> tuple[str, int]:
        """Recompute the build's aggregate result from its attributes.

        Serialized per build via a row lock; bumps the monotonic status
        generation. Returns (status, generation).
        """
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, status FROM builds WHERE id = $1 FOR UPDATE", build_id
            )
            if row is None:
                msg = f"build {build_id} not found"
                raise LookupError(msg)
            statuses = [
                r["status"]
                for r in await conn.fetch(
                    "SELECT status FROM build_attributes WHERE build_id = $1",
                    build_id,
                )
            ]
            if any(s not in TERMINAL_ATTRIBUTE_STATUSES for s in statuses):
                # Not all attributes terminal yet: keep current status.
                generation = await conn.fetchval(
                    "SELECT status_generation FROM builds WHERE id = $1", build_id
                )
                return row["status"], generation
            if any(s in FAILED_ATTRIBUTE_STATUSES for s in statuses):
                status = BuildStatus.FAILED
            elif any(s == "cancelled" for s in statuses):
                status = BuildStatus.CANCELLED
            else:
                status = BuildStatus.SUCCEEDED
            generation = await conn.fetchval(
                """
                UPDATE builds
                SET status = $2,
                    status_generation = status_generation + 1,
                    finished_at = COALESCE(finished_at, now())
                WHERE id = $1
                RETURNING status_generation
                """,
                build_id,
                status,
            )
            return status, generation
