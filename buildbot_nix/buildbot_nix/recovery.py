"""Crash recovery, DB-outage policy, and retention cleanup.

Recovery resumes rather than restarts: attributes already terminal in
the database are skipped (attribute completion is one transactional
write, so the database is trustworthy); pending attributes are first
re-checked against the nix store via `nix path-info` — already-valid
out paths complete without a rebuild — and only the rest re-run.
Effects are guarded by the build's started-flag and never re-run
automatically.

DB outage policy: state writes from running builds go through
db_retry() (bounded retries with backoff); when the database stays
unreachable the service raises so systemd restarts it. Webhook
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
from dataclasses import dataclass
from typing import TYPE_CHECKING

import asyncpg

from .models import CacheStatus, NixEvalJobSuccess
from .scheduler import AttributeResult, AttributeStatus

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from .db import BuildDB

logger = logging.getLogger(__name__)

UNFINISHED_STATUSES = ("pending", "evaluating", "building")


class DatabaseUnavailableError(Exception):
    """Raised after bounded retries; the service exits for systemd."""


async def db_retry[T](
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 5,
    base_delay: float = 0.5,
) -> T:
    """Run a DB operation with bounded exponential-backoff retries."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return await fn()
        except (asyncpg.PostgresConnectionError, OSError) as e:
            last_error = e
            delay = base_delay * 2**attempt
            logger.warning(
                "database operation failed, retrying",
                extra={"attempt": attempt + 1, "delay": delay},
            )
            await asyncio.sleep(delay)
    msg = f"database unavailable after {attempts} attempts: {last_error}"
    raise DatabaseUnavailableError(msg) from last_error


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


async def check_store_paths(paths: list[str]) -> set[str]:
    """Valid store paths among `paths` (batched `nix path-info`)."""
    valid: set[str] = set()
    batch_size = 1000
    for i in range(0, len(paths), batch_size):
        batch = [p for p in paths[i : i + batch_size] if p]
        if not batch:
            continue
        proc = await asyncio.create_subprocess_exec(
            "nix",
            "path-info",
            *batch,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        if proc.returncode == 0:
            valid.update(batch)
        elif len(batch) > 1:
            # Mixed validity: check individually.
            for path in batch:
                single = await asyncio.create_subprocess_exec(
                    "nix",
                    "path-info",
                    path,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await single.wait()
                if single.returncode == 0:
                    valid.add(path)
    return valid


async def settle_already_built(
    db: BuildDB,
    build: ResumableBuild,
    path_checker: Callable[[list[str]], Awaitable[set[str]]] = check_store_paths,
) -> tuple[list[NixEvalJobSuccess], list[tuple[str, str]]]:
    """Complete pending attributes whose out paths already exist in the
    store; return (jobs that still need building, settled (attr,
    out_path) pairs for gcroots/outputs post-processing)."""
    out_paths = {
        job.attr: job.outputs.get("out")
        for job in build.pending_jobs
        if job.outputs.get("out")
    }
    valid = await path_checker([p for p in out_paths.values() if p])
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
    for row in rows:
        log_dir = state_dir / "logs" / str(row["id"])
        with contextlib.suppress(OSError):
            shutil.rmtree(log_dir)
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
