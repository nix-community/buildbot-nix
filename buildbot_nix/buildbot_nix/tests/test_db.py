"""Schema CRUD tests against an ephemeral Postgres instance."""

# ruff: noqa: S603 (subprocess calls use fixed argument lists)
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

from buildbot_nix.db import BuildDB
from buildbot_nix.failed_builds import PostgresFailedBuildCache
from buildbot_nix.migrations import apply_migrations, load_migrations
from buildbot_nix.models import CacheStatus, NixEvalJobSuccess
from buildbot_nix.scheduler import AttributeResult, AttributeStatus

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.skipif(
    shutil.which("initdb") is None, reason="postgresql not available"
)


@pytest.fixture(scope="module")
def postgres_dsn(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    datadir = tmp_path_factory.mktemp("pgdata")
    sockdir = tmp_path_factory.mktemp("pgsock")
    subprocess.run(
        ["initdb", "-D", str(datadir), "-U", "test", "--auth=trust"],
        check=True,
        capture_output=True,
    )
    proc = subprocess.Popen(
        [
            "postgres",
            "-D",
            str(datadir),
            "-k",
            str(sockdir),
            "-c",
            "listen_addresses=",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            if Path(sockdir, ".s.PGSQL.5432").exists():
                break
            if time.monotonic() > deadline:
                msg = "postgres did not start"
                raise RuntimeError(msg)
            time.sleep(0.1)
        subprocess.run(
            ["createdb", "-h", str(sockdir), "-U", "test", "engine"],
            check=True,
            capture_output=True,
        )
        yield f"postgresql://test@/engine?host={sockdir}"
    finally:
        proc.terminate()
        proc.wait()


@pytest.fixture
def migrated_dsn(postgres_dsn: str) -> str:
    asyncio.run(apply_migrations(postgres_dsn))
    return postgres_dsn


async def _connect(dsn: str) -> asyncpg.Connection:
    return await asyncpg.connect(dsn)


def test_load_migrations() -> None:
    migrations = load_migrations()
    assert migrations
    assert migrations[0].version == 1
    assert migrations[0].name == "initial"
    versions = [m.version for m in migrations]
    assert versions == sorted(versions)


def test_migrations_idempotent(migrated_dsn: str) -> None:
    # Second run must be a no-op, not a failure.
    asyncio.run(apply_migrations(migrated_dsn))

    async def check() -> int:
        conn = await _connect(migrated_dsn)
        try:
            return await conn.fetchval("SELECT count(*) FROM schema_migrations")
        finally:
            await conn.close()

    assert asyncio.run(check()) == len(load_migrations())


def test_project_build_attribute_crud(migrated_dsn: str) -> None:
    async def run() -> None:
        conn = await _connect(migrated_dsn)
        try:
            project_id = await conn.fetchval(
                """
                INSERT INTO projects (forge, forge_repo_id, owner, name,
                                      default_branch, url, enabled)
                VALUES ('github', '12345', 'acme', 'widget', 'main',
                        'https://github.com/acme/widget', TRUE)
                RETURNING id
                """
            )
            # Duplicate stable forge ID must be rejected.
            with pytest.raises(Exception, match="duplicate key"):
                await conn.execute(
                    """
                    INSERT INTO projects (forge, forge_repo_id, owner, name,
                                          default_branch, url)
                    VALUES ('github', '12345', 'acme', 'renamed', 'main', 'x')
                    """
                )

            # Allocate a per-project build number atomically.
            number = await conn.fetchval(
                """
                UPDATE projects
                SET next_build_number = next_build_number + 1
                WHERE id = $1
                RETURNING next_build_number - 1
                """,
                project_id,
            )
            assert number == 1
            build_id = await conn.fetchval(
                """
                INSERT INTO builds (project_id, number, tree_hash, commit_sha,
                                    branch, pr_number, pr_author)
                VALUES ($1, $2, 'tree123', 'abc', 'main', 7, 'github:alice')
                RETURNING id
                """,
                project_id,
                number,
            )

            attr_id = await conn.fetchval(
                """
                INSERT INTO build_attributes (build_id, attr, system, drv_path)
                VALUES ($1, 'checks.x86_64-linux.foo', 'x86_64-linux',
                        '/nix/store/foo.drv')
                RETURNING id
                """,
                build_id,
            )
            await conn.execute(
                """
                UPDATE build_attributes
                SET status = 'succeeded',
                    outputs = '{"out": "/nix/store/foo"}',
                    finished_at = now()
                WHERE id = $1
                """,
                attr_id,
            )
            await conn.execute(
                "INSERT INTO logs (attribute_id, path, size_bytes) "
                "VALUES ($1, 'a/b.zst', 42)",
                attr_id,
            )

            row = await conn.fetchrow(
                """
                SELECT b.status, a.status AS attr_status, l.size_bytes
                FROM builds b
                JOIN build_attributes a ON a.build_id = b.id
                JOIN logs l ON l.attribute_id = a.id
                WHERE b.id = $1
                """,
                build_id,
            )
            assert row is not None
            assert row["status"] == "pending"
            assert row["attr_status"] == "succeeded"
            assert row["size_bytes"] == 42

            # Cascade delete: removing the project removes everything.
            await conn.execute("DELETE FROM projects WHERE id = $1", project_id)
            assert await conn.fetchval("SELECT count(*) FROM builds") == 0
            assert await conn.fetchval("SELECT count(*) FROM logs") == 0
        finally:
            await conn.close()

    asyncio.run(run())


def test_failed_builds_and_statuses_upsert(migrated_dsn: str) -> None:
    async def run() -> None:
        conn = await _connect(migrated_dsn)
        try:
            for ts in (1.0, 2.0):
                await conn.execute(
                    """
                    INSERT INTO failed_builds (derivation, timestamp, url)
                    VALUES ('/nix/store/x.drv', $1, 'http://ci/1')
                    ON CONFLICT (derivation)
                    DO UPDATE SET timestamp = EXCLUDED.timestamp
                    """,
                    ts,
                )
            assert (
                await conn.fetchval(
                    "SELECT timestamp FROM failed_builds "
                    "WHERE derivation = '/nix/store/x.drv'"
                )
                == 2.0
            )

            await conn.execute(
                """
                INSERT INTO failed_statuses (revision, status_name, timestamp)
                VALUES ('abc', 'nix-build', 1.0)
                ON CONFLICT (revision, status_name)
                DO UPDATE SET timestamp = EXCLUDED.timestamp
                """
            )
            assert await conn.fetchval("SELECT count(*) FROM failed_statuses") == 1
            await conn.execute("DELETE FROM failed_statuses WHERE revision = 'abc'")
        finally:
            await conn.close()

    asyncio.run(run())


def test_failed_build_cache_component(migrated_dsn: str) -> None:
    async def run() -> None:
        pool = await asyncpg.create_pool(migrated_dsn)
        try:
            cache = PostgresFailedBuildCache(pool)
            drv = "/nix/store/cache-test.drv"
            assert await cache.check(drv) is None
            await cache.add(drv, "http://ci/b/1")
            entry = await cache.check(drv)
            assert entry is not None
            assert entry.url == "http://ci/b/1"
            first_time = entry.time
            # Upsert refreshes timestamp and url.
            await cache.add(drv, "http://ci/b/2")
            entry = await cache.check(drv)
            assert entry is not None
            assert entry.url == "http://ci/b/2"
            assert entry.time >= first_time
            await cache.remove(drv)
            assert await cache.check(drv) is None
        finally:
            await pool.close()

    asyncio.run(run())


# --- BuildDB regression tests --------------------------------------------------


async def _mk_project(pool: asyncpg.Pool, name: str) -> int:
    return await pool.fetchval(
        """
        INSERT INTO projects (forge, forge_repo_id, owner, name,
                              default_branch, url, enabled)
        VALUES ('github', $1, 'acme', $1, 'main', 'u', TRUE)
        RETURNING id
        """,
        name,
    )


def _job(attr: str, out: str | None = None) -> NixEvalJobSuccess:
    return NixEvalJobSuccess(
        attr=attr,
        attr_path=[attr],
        cache_status=CacheStatus.not_built,
        needed_builds=[],
        needed_substitutes=[],
        drv_path=f"/nix/store/{attr}.drv",
        name=attr,
        outputs={"out": out or f"/nix/store/{attr}-out"},
        system="x86_64-linux",
    )


def test_get_or_create_build_concurrent_no_duplicates(migrated_dsn: str) -> None:
    # SELECT-then-INSERT must be serialized: there is no unique
    # constraint on (project_id, tree_hash), so without locking two
    # concurrent change events for the same tree create two builds.
    async def run() -> None:
        pool = await asyncpg.create_pool(migrated_dsn, min_size=5, max_size=5)
        try:
            project_id = await _mk_project(pool, "race")
            db = BuildDB(pool)
            results = await asyncio.gather(
                *(
                    db.get_or_create_build(project_id, "tree-r", "sha", "main")
                    for _ in range(5)
                )
            )
            assert len({build.id for build, _ in results}) == 1
            assert sum(created for _, created in results) == 1
            count = await pool.fetchval(
                "SELECT count(*) FROM builds WHERE project_id = $1", project_id
            )
            assert count == 1
        finally:
            await pool.close()

    asyncio.run(run())


def test_cancelled_build_not_reused(migrated_dsn: str) -> None:
    # A cancelled build carries no verdict; re-pushing the same tree
    # must build again instead of re-reporting "cancelled".
    async def run() -> None:
        pool = await asyncpg.create_pool(migrated_dsn)
        try:
            project_id = await _mk_project(pool, "cancelled-reuse")
            db = BuildDB(pool)
            first, created = await db.get_or_create_build(
                project_id, "tree-c", "sha", "main"
            )
            assert created
            await pool.execute(
                "UPDATE builds SET status = 'cancelled' WHERE id = $1", first.id
            )
            second, created = await db.get_or_create_build(
                project_id, "tree-c", "sha2", "main"
            )
            assert created
            assert second.id != first.id
        finally:
            await pool.close()

    asyncio.run(run())


def test_reuse_backfills_pr_fields(migrated_dsn: str) -> None:
    # Branch push creates the build first; the PR event for the same
    # tree must backfill pr_number/pr_author so the PR author keeps
    # restart/cancel rights.
    async def run() -> None:
        pool = await asyncpg.create_pool(migrated_dsn)
        try:
            project_id = await _mk_project(pool, "pr-backfill")
            db = BuildDB(pool)
            first, _ = await db.get_or_create_build(
                project_id, "tree-p", "sha", "feature"
            )
            assert first.pr_number is None
            second, created = await db.get_or_create_build(
                project_id,
                "tree-p",
                "sha",
                "feature",
                pr_number=7,
                pr_author="github:alice",
            )
            assert not created
            assert second.id == first.id
            assert second.pr_number == 7
            row = await pool.fetchrow(
                "SELECT pr_number, pr_author FROM builds WHERE id = $1", first.id
            )
            assert row["pr_number"] == 7
            assert row["pr_author"] == "github:alice"
        finally:
            await pool.close()

    asyncio.run(run())


def test_complete_attribute_replaces_log_row(migrated_dsn: str) -> None:
    # Attribute restarts rewrite the same log file; the metadata row
    # must be replaced, not duplicated with stale sizes.
    async def run() -> None:
        pool = await asyncpg.create_pool(migrated_dsn)
        try:
            project_id = await _mk_project(pool, "log-dup")
            db = BuildDB(pool)
            build, _ = await db.get_or_create_build(project_id, "tree-l", "sha", "main")
            job = _job("foo")
            for size in (10, 20):
                await db.complete_attribute(
                    build.id,
                    AttributeResult(
                        attr="foo",
                        status=AttributeStatus.succeeded,
                        job=job,
                        out_path="/nix/store/foo-out",
                        drv_path=job.drv_path,
                        system=job.system,
                    ),
                    log_path="logs/x/foo.zst",
                    log_size=size,
                )
            rows = await pool.fetch(
                """
                SELECT l.size_bytes FROM logs l
                JOIN build_attributes a ON a.id = l.attribute_id
                WHERE a.build_id = $1 AND a.attr = 'foo'
                """,
                build.id,
            )
            assert len(rows) == 1
            assert rows[0]["size_bytes"] == 20
        finally:
            await pool.close()

    asyncio.run(run())


def test_record_attributes_persists_pending_rows_with_outputs(
    migrated_dsn: str,
) -> None:
    async def run() -> None:
        pool = await asyncpg.create_pool(migrated_dsn)
        try:
            project_id = await _mk_project(pool, "record-attrs")
            db = BuildDB(pool)
            build, _ = await db.get_or_create_build(
                project_id, "tree-ra", "sha", "main"
            )
            await db.record_attributes(build.id, [_job("a"), _job("b")])
            rows = {
                r["attr"]: r
                for r in await pool.fetch(
                    "SELECT attr, status, outputs FROM build_attributes "
                    "WHERE build_id = $1",
                    build.id,
                )
            }
            assert set(rows) == {"a", "b"}
            assert rows["a"]["status"] == "pending"
            assert '"/nix/store/a-out"' in rows["a"]["outputs"]
            # Re-recording (rerun) must not reset completed rows.
            await db.complete_attribute(
                build.id,
                AttributeResult(
                    attr="a",
                    status=AttributeStatus.succeeded,
                    job=_job("a"),
                    out_path="/nix/store/a-out",
                ),
            )
            await db.record_attributes(build.id, [_job("a")])
            status = await pool.fetchval(
                "SELECT status FROM build_attributes "
                "WHERE build_id = $1 AND attr = 'a'",
                build.id,
            )
            assert status == "succeeded"
        finally:
            await pool.close()

    asyncio.run(run())


def test_mark_attribute_building_sets_status_and_started_at(
    migrated_dsn: str,
) -> None:
    async def run() -> None:
        pool = await asyncpg.create_pool(migrated_dsn)
        try:
            project_id = await _mk_project(pool, "building-status")
            db = BuildDB(pool)
            build, _ = await db.get_or_create_build(project_id, "tree-b", "sha", "main")
            job = _job("foo")
            await db.record_attributes(build.id, [job])
            await db.mark_attribute_building(build.id, "foo", job.system, job.drv_path)
            row = await pool.fetchrow(
                "SELECT status, started_at FROM build_attributes "
                "WHERE build_id = $1 AND attr = 'foo'",
                build.id,
            )
            assert row["status"] == "building"
            assert row["started_at"] is not None
            await db.complete_attribute(
                build.id,
                AttributeResult(
                    attr="foo",
                    status=AttributeStatus.succeeded,
                    job=job,
                    out_path="/nix/store/foo-out",
                ),
            )
            row = await pool.fetchrow(
                "SELECT status, started_at, finished_at FROM build_attributes "
                "WHERE build_id = $1 AND attr = 'foo'",
                build.id,
            )
            assert row["status"] == "succeeded"
            assert row["started_at"] is not None  # preserved by completion
            assert row["finished_at"] is not None
        finally:
            await pool.close()

    asyncio.run(run())
