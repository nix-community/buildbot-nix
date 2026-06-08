"""Crash recovery, DB-outage policy, and retention cleanup.

Recovery resumes rather than restarts: attributes already terminal in
the database are skipped (attribute completion is one transactional
write, so the database is trustworthy); pending attributes are first
re-checked against the nix store by reading its sqlite database
directly (see check_store_paths) — already-valid out paths complete
without a rebuild — and only the rest re-run.
Effects are guarded by the build's started-flag and never re-run
automatically.

DB outage policy: when the database is unreachable the affected
operation raises and the service restarts via systemd. Webhook
fail-fast lives in ingestion; the health check backs the
HTTP health endpoint.

Retention: builds older than the configured horizon are deleted along
with their log files; stale workdirs are swept via RepoManager.cleanup.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg

from .models import CacheStatus, NixEvalJobSuccess
from .scheduler import AttributeResult, AttributeStatus

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from .db import BuildDB

logger = logging.getLogger(__name__)

UNFINISHED_STATUSES = ("pending", "evaluating", "building")


async def check_db_health(pool: asyncpg.Pool) -> bool:
    """Backs the HTTP health endpoint."""
    try:
        await pool.fetchval("SELECT 1")
    except (TimeoutError, asyncpg.PostgresError, OSError):
        return False
    return True


# --- crash recovery ----------------------------------------------------------


@dataclass(frozen=True)
class ResumableBuild:
    build_id: int
    project_id: int
    branch: str
    commit_sha: str
    pr_number: int | None
    status: str
    effects_started: bool
    # False when the build died during evaluation (no attribute rows).
    has_attributes: bool
    # Jobs reconstructed from non-terminal attribute rows.
    pending_jobs: list[NixEvalJobSuccess]


async def find_unfinished_builds(
    pool: asyncpg.Pool, build_id: int | None = None
) -> list[ResumableBuild]:
    rows = await pool.fetch(
        "SELECT * FROM builds WHERE status = ANY($1::text[]) "
        "AND ($2::bigint IS NULL OR id = $2) ORDER BY id",
        list(UNFINISHED_STATUSES),
        build_id,
    )
    if not rows:
        return []
    # One round-trip for all attribute rows, not one per build.
    attr_rows = await pool.fetch(
        "SELECT build_id, attr, system, drv_path, outputs, status "
        "FROM build_attributes WHERE build_id = ANY($1::bigint[])",
        [row["id"] for row in rows],
    )
    attrs_by_build: dict[int, list[asyncpg.Record]] = {}
    for attr in attr_rows:
        attrs_by_build.setdefault(attr["build_id"], []).append(attr)
    builds = []
    for row in rows:
        attrs = attrs_by_build.get(row["id"], [])
        pending_jobs = []
        for attr in attrs:
            # 'building' rows are attributes that were running when the
            # service died; they must resume like pending ones.
            if attr["status"] not in ("pending", "building"):
                continue  # terminal in DB: never redone
            if not attr["drv_path"]:
                continue
            outputs = json.loads(attr["outputs"]) if attr["outputs"] else {}
            pending_jobs.append(
                NixEvalJobSuccess(
                    attr=attr["attr"],
                    attr_path=attr["attr"].split("."),
                    cache_status=CacheStatus.not_built,
                    needed_builds=[],
                    needed_substitutes=[],
                    drv_path=attr["drv_path"],
                    name=attr["attr"],
                    outputs=outputs or {"out": None},
                    system=attr["system"] or "",
                )
            )
        builds.append(
            ResumableBuild(
                build_id=row["id"],
                project_id=row["project_id"],
                branch=row["branch"],
                commit_sha=row["commit_sha"],
                pr_number=row["pr_number"],
                status=row["status"],
                effects_started=row["effects_started"],
                has_attributes=len(attrs) > 0,
                pending_jobs=pending_jobs,
            )
        )
    return builds


NIX_DB = Path("/nix/var/nix/db/db.sqlite")


def _valid_paths_from_db(paths: list[str], nix_db: Path) -> set[str]:
    con = sqlite3.connect(f"file:{nix_db}?mode=ro", uri=True)
    try:
        valid: set[str] = set()
        batch_size = 500  # sqlite parameter limit
        for i in range(0, len(paths), batch_size):
            batch = paths[i : i + batch_size]
            placeholders = ",".join("?" * len(batch))
            valid.update(
                row[0]
                for row in con.execute(
                    f"SELECT path FROM ValidPaths WHERE path IN ({placeholders})",  # noqa: S608
                    batch,
                )
            )
        return valid
    finally:
        con.close()


async def fail_interrupted_effects(
    pool: asyncpg.Pool, started_before: datetime
) -> None:
    """Settle effect rows left running by a crash: started effects
    never auto-re-run. Pending rows keep their queued items and run
    after the requeue. Only rows started before `started_before`
    (process start) are swept: the sweep runs concurrently with the
    work loop, and newer rows are live effects."""
    await pool.execute(
        """
        UPDATE build_effects SET status = 'failed',
            error = 'interrupted by a service restart', finished_at = now()
        WHERE status = 'running' AND started_at < $1
        """,
        started_before,
    )
    await pool.execute(
        """
        UPDATE scheduled_effect_runs SET status = 'failed',
            error = 'interrupted by a service restart', finished_at = now()
        WHERE status = 'running' AND started_at < $1
        """,
        started_before,
    )


async def check_store_paths(paths: list[str], nix_db: Path = NIX_DB) -> set[str]:
    """Valid store paths among `paths`, read straight from the nix
    store database: no subprocess startup, no daemon round-trips."""
    paths = [p for p in paths if p]
    if not paths:
        return set()
    try:
        return await asyncio.to_thread(_valid_paths_from_db, paths, nix_db)
    except sqlite3.Error:
        logger.exception("reading the nix database failed; assuming unbuilt")
        return set()


async def settle_already_built(
    db: BuildDB,
    build: ResumableBuild,
    path_checker: Callable[[list[str]], Awaitable[set[str]]] = check_store_paths,
) -> tuple[list[NixEvalJobSuccess], list[tuple[str, str]]]:
    """Complete pending attributes whose out paths already exist in the
    store; return (jobs that still need building, settled (attr,
    out_path) pairs for gcroots/outputs post-processing)."""
    valid = await path_checker(
        [p for job in build.pending_jobs if (p := job.outputs.get("out"))]
    )
    remaining = []
    settled: list[tuple[str, str]] = []
    for job in build.pending_jobs:
        out_path = job.outputs.get("out")
        if out_path and out_path in valid:
            await db.complete_attribute(
                build.build_id,
                AttributeResult(
                    attr=job.attr,
                    status=AttributeStatus.succeeded,
                    job=job,
                    out_path=out_path,
                    drv_path=job.drv_path,
                    system=job.system,
                ),
            )
            settled.append((job.attr, out_path))
        else:
            remaining.append(job)
    return remaining, settled


# --- retention cleanup ---------------------------------------------------------


async def cleanup_old_builds(
    pool: asyncpg.Pool, state_dir: Path, retention_days: int
) -> int:
    """Delete builds (cascading to attributes/log rows) and their log
    directories past the retention horizon. Returns deleted count."""
    rows = await pool.fetch(
        """
        DELETE FROM builds
        WHERE finished_at IS NOT NULL
          AND finished_at < now() - make_interval(days => $1)
          -- A restarted build keeps its old finished_at until it
          -- re-aggregates; never delete a build that is running again.
          AND status IN ('succeeded', 'failed', 'cancelled')
        RETURNING id
        """,
        retention_days,
    )
    # Age out the per-revision caches; rows are otherwise only removed
    # on a success flip or explicit rebuild and accumulate forever.
    for table in ("failed_statuses", "failed_builds"):
        await pool.execute(
            f"DELETE FROM {table} "  # noqa: S608 (fixed table names)
            "WHERE to_timestamp(timestamp) < now() - make_interval(days => $1)",
            retention_days,
        )
    old_runs = await pool.fetch(
        """
        DELETE FROM scheduled_effect_runs
        WHERE finished_at IS NOT NULL
          AND finished_at < now() - make_interval(days => $1)
        RETURNING id
        """,
        retention_days,
    )
    for row in rows:
        log_dir = state_dir / "logs" / str(row["id"])
        with contextlib.suppress(OSError):
            shutil.rmtree(log_dir)
    for row in old_runs:
        with contextlib.suppress(OSError):
            (state_dir / "logs" / "scheduled" / f"{row['id']}.zst").unlink()
    if rows:
        logger.info("retention cleanup", extra={"deleted_builds": len(rows)})
    return len(rows)


def cleanup_orphan_log_dirs(pool_build_ids: set[int], state_dir: Path) -> None:
    """Remove log directories whose build row no longer exists."""
    logs_root = state_dir / "logs"
    if not logs_root.is_dir():
        return
    for entry in os.scandir(logs_root):
        try:
            build_id = int(entry.name)
        except ValueError:
            continue
        if build_id not in pool_build_ids:
            with contextlib.suppress(OSError):
                shutil.rmtree(entry.path)
