"""Tests for crash recovery, DB-retry policy, and retention cleanup."""

# ruff: noqa: PLR2004 (literal values in test assertions are fine)

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg
import pytest

from buildbot_nix.engine.db import BuildDB
from buildbot_nix.engine.migrations import apply_migrations
from buildbot_nix.engine.recovery import (
    DatabaseUnavailableError,
    cleanup_old_builds,
    cleanup_orphan_log_dirs,
    db_retry,
    fail_interrupted_eval,
    find_unfinished_builds,
    settle_already_built,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.skipif(
    shutil.which("initdb") is None, reason="postgresql not available"
)


@pytest.fixture(scope="module")
def postgres_dsn(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    datadir = tmp_path_factory.mktemp("pgdata")
    sockdir = tmp_path_factory.mktemp("pgsock")
    subprocess.run(  # noqa: S603
        ["initdb", "-D", str(datadir), "-U", "test", "--auth=trust"],
        check=True,
        capture_output=True,
    )
    proc = subprocess.Popen(  # noqa: S603
        ["postgres", "-D", str(datadir), "-k", str(sockdir), "-c", "listen_addresses="],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while not Path(sockdir, ".s.PGSQL.5432").exists():
            if time.monotonic() > deadline:
                msg = "postgres did not start"
                raise RuntimeError(msg)
            time.sleep(0.1)
        subprocess.run(  # noqa: S603
            ["createdb", "-h", str(sockdir), "-U", "test", "recovery"],
            check=True,
            capture_output=True,
        )
        dsn = f"postgresql://test@/recovery?host={sockdir}"
        asyncio.run(apply_migrations(dsn))
        yield dsn
    finally:
        proc.terminate()
        proc.wait()


async def make_build(pool: asyncpg.Pool, name: str) -> int:
    project_id = await pool.fetchval(
        """
        INSERT INTO projects (forge, forge_repo_id, owner, name, default_branch, url)
        VALUES ('github', $1, 'acme', $1, 'main', 'u') RETURNING id
        """,
        name,
    )
    return await pool.fetchval(
        """
        INSERT INTO builds (project_id, number, tree_hash, commit_sha, branch,
                            status)
        VALUES ($1, 1, $2, 'sha', 'main', 'building') RETURNING id
        """,
        project_id,
        f"tree-{name}",
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


def test_db_retry_gives_up() -> None:
    async def run() -> None:
        calls = 0

        async def failing() -> None:
            nonlocal calls
            calls += 1
            raise ConnectionRefusedError

        with pytest.raises(DatabaseUnavailableError):
            await db_retry(failing, attempts=3, base_delay=0.01)
        assert calls == 3

    asyncio.run(run())


def test_db_retry_recovers() -> None:
    async def run() -> str:
        calls = 0

        async def flaky() -> str:
            nonlocal calls
            calls += 1
            if calls < 2:
                raise ConnectionResetError
            return "ok"

        return await db_retry(flaky, attempts=3, base_delay=0.01)

    assert asyncio.run(run()) == "ok"


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


def test_orphan_log_dirs(tmp_path: Path) -> None:
    (tmp_path / "logs" / "42").mkdir(parents=True)
    (tmp_path / "logs" / "43").mkdir(parents=True)
    (tmp_path / "logs" / "not-a-build").mkdir(parents=True)
    cleanup_orphan_log_dirs({42}, tmp_path)
    assert (tmp_path / "logs" / "42").exists()
    assert not (tmp_path / "logs" / "43").exists()
    assert (tmp_path / "logs" / "not-a-build").exists()  # ignored


def test_eval_interrupted_build_fails_instead_of_resuming(postgres_dsn: str) -> None:
    # Killed during evaluation: no attribute rows exist, so the build
    # cannot resume without a re-eval and must not aggregate to success.
    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            db = BuildDB(pool)
            build_id = await make_build(pool, "interrupted-eval")
            resumable = next(
                r for r in await find_unfinished_builds(pool) if r.build_id == build_id
            )
            assert not resumable.has_attributes
            assert await fail_interrupted_eval(db, resumable)
            row = await pool.fetchrow(
                "SELECT status, error FROM builds WHERE id = $1", build_id
            )
            assert row["status"] == "failed"
            assert "interrupted" in row["error"]
        finally:
            await pool.close()

    asyncio.run(run())


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
            await pool.execute(
                "INSERT INTO failed_builds (derivation, timestamp, url) "
                "VALUES ('/nix/store/old.drv', $1, 'u'), "
                "('/nix/store/new.drv', $2, 'u')",
                old_ts,
                time.time(),
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
