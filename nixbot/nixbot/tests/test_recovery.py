"""Tests for crash recovery and retention cleanup."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import asyncpg

from nixbot.db import BuildDB
from nixbot.recovery import (
    check_store_paths,
    cleanup_old_builds,
    cleanup_orphan_log_dirs,
    fail_interrupted_effects,
    find_unfinished_builds,
    settle_already_built,
)

from .support import insert_build, insert_project

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


async def make_build(pool: asyncpg.Pool, name: str) -> int:
    project_id = await insert_project(pool, name)
    return await insert_build(
        pool, project_id, tree_hash=f"tree-{name}", status="building"
    )


async def add_attr(
    pool: asyncpg.Pool, build_id: int, attr: str, status: str, out: str | None = None
) -> None:
    await pool.execute(
        """
        INSERT INTO build_attributes (build_id, attr, system, drv_path, outputs,
                                      status)
        VALUES ($1, $2, 'x86_64-linux', $3, $4::jsonb, $5)
        """,
        build_id,
        attr,
        f"/nix/store/{attr}.drv",
        f'{{"out": "/nix/store/{attr}-out"}}' if out is None else out,
        status,
    )


def test_resume_skips_terminal_attributes(postgres_dsn: str) -> None:
    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            build_id = await make_build(pool, "resume")
            await add_attr(pool, build_id, "done", "succeeded")
            await add_attr(pool, build_id, "failed", "failed")
            await add_attr(pool, build_id, "todo", "pending")

            builds = await find_unfinished_builds(pool)
            target = next(b for b in builds if b.build_id == build_id)
            # Terminal attributes never resumed.
            assert [j.attr for j in target.pending_jobs] == ["todo"]
            assert not target.effects_started
        finally:
            await pool.close()

    asyncio.run(run())


def test_settle_already_built(postgres_dsn: str) -> None:
    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            build_id = await make_build(pool, "settle")
            await add_attr(pool, build_id, "present", "pending")
            await add_attr(pool, build_id, "missing", "pending")
            builds = await find_unfinished_builds(pool)
            target = next(b for b in builds if b.build_id == build_id)

            async def checker(paths: list[str]) -> set[str]:
                return {p for p in paths if "present" in p}

            remaining, settled = await settle_already_built(
                BuildDB(pool), target, checker
            )
            assert [j.attr for j in remaining] == ["missing"]
            assert settled == [("present", "/nix/store/present-out")]
            statuses = {
                r["attr"]: r["status"]
                for r in await pool.fetch(
                    "SELECT attr, status FROM build_attributes WHERE build_id = $1",
                    build_id,
                )
            }
            # Store-valid path completed without rebuild.
            assert statuses["present"] == "succeeded"
            assert statuses["missing"] == "pending"
        finally:
            await pool.close()

    asyncio.run(run())


def test_retention_cleanup(postgres_dsn: str, tmp_path: Path) -> None:
    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            build_id = await make_build(pool, "old")
            await pool.execute(
                "UPDATE builds SET status='succeeded', "
                "finished_at = now() - interval '100 days' WHERE id = $1",
                build_id,
            )
            recent_id = await make_build(pool, "recent")
            await pool.execute(
                "UPDATE builds SET status='succeeded', finished_at = now() "
                "WHERE id = $1",
                recent_id,
            )
            log_dir = tmp_path / "logs" / str(build_id)
            log_dir.mkdir(parents=True)
            (log_dir / "a.zst").write_bytes(b"x")

            deleted = await cleanup_old_builds(pool, tmp_path, 90)
            assert deleted == 1
            assert not log_dir.exists()
            assert (
                await pool.fetchval(
                    "SELECT count(*) FROM builds WHERE id = $1", recent_id
                )
                == 1
            )
        finally:
            await pool.close()

    asyncio.run(run())


class _FakePool:
    """Returns the given build ids; optionally runs a callback first."""

    def __init__(
        self, ids: set[int], on_fetch: Callable[[], None] | None = None
    ) -> None:
        self.ids = ids
        self.on_fetch = on_fetch

    async def fetch(self, _query: str) -> list[dict]:
        if self.on_fetch is not None:
            self.on_fetch()
        return [{"id": build_id} for build_id in self.ids]


def test_orphan_log_dirs(tmp_path: Path) -> None:
    (tmp_path / "logs" / "42").mkdir(parents=True)
    (tmp_path / "logs" / "43").mkdir(parents=True)
    (tmp_path / "logs" / "not-a-build").mkdir(parents=True)
    asyncio.run(cleanup_orphan_log_dirs(_FakePool({42}), tmp_path, grace_seconds=0))
    assert (tmp_path / "logs" / "42").exists()
    assert not (tmp_path / "logs" / "43").exists()
    assert (tmp_path / "logs" / "not-a-build").exists()  # ignored


def test_orphan_log_dirs_grace_period(tmp_path: Path) -> None:
    """A freshly created log dir is kept even without a build row: its
    build may have been inserted after the id snapshot."""
    (tmp_path / "logs" / "77").mkdir(parents=True)
    asyncio.run(cleanup_orphan_log_dirs(_FakePool(set()), tmp_path))
    assert (tmp_path / "logs" / "77").exists()


def test_orphan_log_dirs_scan_before_id_snapshot(tmp_path: Path) -> None:
    """Dirs created after the directory scan must survive even when the
    id snapshot does not contain them (creation race)."""

    (tmp_path / "logs" / "1").mkdir(parents=True)  # triggers the snapshot

    def create_late_dir() -> None:
        (tmp_path / "logs" / "99").mkdir(parents=True)

    asyncio.run(
        cleanup_orphan_log_dirs(
            _FakePool(set(), on_fetch=create_late_dir), tmp_path, grace_seconds=0
        )
    )
    assert (tmp_path / "logs" / "99").exists()
    assert not (tmp_path / "logs" / "1").exists()


def test_resume_includes_interrupted_building_attributes(postgres_dsn: str) -> None:
    # An attribute that was 'building' when the service died must
    # resume like a pending one (it is not terminal).
    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            build_id = await make_build(pool, "resume-building")
            await add_attr(pool, build_id, "inflight", "building")
            await add_attr(pool, build_id, "done", "succeeded")
            builds = await find_unfinished_builds(pool)
            target = next(b for b in builds if b.build_id == build_id)
            assert [j.attr for j in target.pending_jobs] == ["inflight"]
        finally:
            await pool.close()

    asyncio.run(run())


def test_retention_skips_restarted_builds(postgres_dsn: str, tmp_path: Path) -> None:
    # A restarted build briefly keeps its old finished_at while its
    # status is 'building' again; the hourly sweep must not delete it
    # mid-rerun.
    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            build_id = await make_build(pool, "restarted-old")
            await pool.execute(
                "UPDATE builds SET status = 'building', "
                "finished_at = now() - interval '100 days' WHERE id = $1",
                build_id,
            )
            await cleanup_old_builds(pool, tmp_path, 90)
            assert (
                await pool.fetchval(
                    "SELECT count(*) FROM builds WHERE id = $1", build_id
                )
                == 1
            )
        finally:
            await pool.close()

    asyncio.run(run())


def test_retention_ages_out_failed_caches(postgres_dsn: str, tmp_path: Path) -> None:
    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            old_ts = time.time() - 100 * 86400
            await pool.execute(
                "INSERT INTO failed_statuses (revision, status_name, timestamp) "
                "VALUES ('old-rev', 'ctx', $1), ('new-rev', 'ctx', $2)",
                old_ts,
                time.time(),
            )
            cache_project = await insert_project(
                pool, "cache-age", forge_repo_id="cache-age"
            )
            await pool.execute(
                "INSERT INTO failed_builds (project_id, derivation, timestamp, url) "
                "VALUES ($3, '/nix/store/old.drv', $1, 'u'), "
                "($3, '/nix/store/new.drv', $2, 'u')",
                old_ts,
                time.time(),
                cache_project,
            )
            await cleanup_old_builds(pool, tmp_path, 90)
            revisions = {
                r["revision"]
                for r in await pool.fetch("SELECT revision FROM failed_statuses")
            }
            assert "old-rev" not in revisions
            assert "new-rev" in revisions
            drvs = {
                r["derivation"]
                for r in await pool.fetch("SELECT derivation FROM failed_builds")
            }
            assert "/nix/store/old.drv" not in drvs
            assert "/nix/store/new.drv" in drvs
        finally:
            await pool.close()

    asyncio.run(run())


def test_check_store_paths_reads_the_nix_db(tmp_path: Path) -> None:
    """Validity comes from the store database, not from subprocesses."""
    db = tmp_path / "db.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE ValidPaths (path TEXT)")
    con.execute("INSERT INTO ValidPaths (path) VALUES ('/nix/store/aaa-built')")
    con.commit()
    con.close()

    result = asyncio.run(
        check_store_paths(
            ["/nix/store/aaa-built", "/nix/store/bbb-missing", ""], nix_db=db
        )
    )
    assert result == {"/nix/store/aaa-built"}

    # Unreadable database: fail open as "nothing built yet".
    missing = asyncio.run(
        check_store_paths(["/nix/store/aaa-built"], nix_db=tmp_path / "nope")
    )
    assert missing == set()


def test_failed_rebuild_settles_pending_effects(postgres_dsn: str) -> None:
    """A failed rebuild must settle its pending effect rows."""

    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            build_id = await make_build(pool, "fx-failed-rebuild")
            await pool.execute(
                "INSERT INTO build_effects (build_id, name, status) "
                "VALUES ($1, 'deploy', 'pending')",
                build_id,
            )
            await BuildDB(pool).set_build_status(build_id, "failed")
            row = await pool.fetchrow(
                "SELECT status, error FROM build_effects WHERE build_id = $1",
                build_id,
            )
            assert row["status"] == "failed"
            assert "did not succeed" in row["error"]
        finally:
            await pool.close()

    asyncio.run(run())


def test_eval_start_clears_stale_eval_warnings(postgres_dsn: str) -> None:
    """A re-run build must not show the previous attempt's warnings."""

    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            build_id = await make_build(pool, "lw-rerun")
            db = BuildDB(pool)
            await db.set_eval_warnings(
                build_id, '[{"level": "warning", "message": "old", "count": 3}]'
            )
            await db.set_build_status(build_id, "failed")
            assert await pool.fetchval(
                "SELECT eval_warnings FROM builds WHERE id = $1", build_id
            )
            await db.set_build_status(build_id, "evaluating")
            assert (
                await pool.fetchval(
                    "SELECT eval_warnings FROM builds WHERE id = $1", build_id
                )
                is None
            )
        finally:
            await pool.close()

    asyncio.run(run())


def test_interrupted_effects_fail_on_recovery(postgres_dsn: str) -> None:
    """Effects never auto-re-run, so rows left running by a crash
    would spin forever without the startup sweep."""

    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            build_id = await make_build(pool, "fx-sweep")
            await pool.execute(
                "INSERT INTO build_effects (build_id, name) VALUES ($1, 'deploy')",
                build_id,
            )
            project_id = await pool.fetchval(
                "SELECT project_id FROM builds WHERE id = $1", build_id
            )
            await pool.execute(
                "INSERT INTO scheduled_effect_runs (project_id, schedule_name, "
                "effect) VALUES ($1, 's', 'beat')",
                project_id,
            )
            await fail_interrupted_effects(
                pool, datetime.now(UTC) + timedelta(minutes=1)
            )
            for table, column in [
                ("build_effects", "build_id"),
                ("scheduled_effect_runs", "project_id"),
            ]:
                row = await pool.fetchrow(
                    f"SELECT status, error, finished_at FROM {table} "  # noqa: S608
                    f"WHERE {column} = $1",
                    build_id if column == "build_id" else project_id,
                )
                assert row["status"] == "failed"
                assert "interrupted" in row["error"]
                assert row["finished_at"] is not None
        finally:
            await pool.close()

    asyncio.run(run())


def test_interrupted_effects_sweep_spares_live_effects(postgres_dsn: str) -> None:
    """The startup sweep runs concurrently with the work loop: effect
    rows created after process start are live deploys and must not be
    flipped to failed."""

    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            build_id = await make_build(pool, "fx-live")
            process_start = datetime.now(UTC)
            await pool.execute(
                "INSERT INTO build_effects (build_id, name) VALUES ($1, 'deploy')",
                build_id,
            )
            await fail_interrupted_effects(pool, process_start)
            status = await pool.fetchval(
                "SELECT status FROM build_effects WHERE build_id = $1", build_id
            )
            assert status == "running"
        finally:
            await pool.close()

    asyncio.run(run())
