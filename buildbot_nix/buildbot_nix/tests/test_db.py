"""Schema CRUD tests against an ephemeral Postgres instance."""


# ruff: noqa: PLR2004 (literal values in test assertions are fine)

from __future__ import annotations

import asyncio
import json

import asyncpg
import pytest

from buildbot_nix import migrations as migrations_mod
from buildbot_nix.db import BuildDB
from buildbot_nix.failed_builds import PostgresFailedBuildCache
from buildbot_nix.migrations import apply_migrations, load_migrations
from buildbot_nix.models import CacheStatus
from buildbot_nix.scheduler import AttributeResult, AttributeStatus

from .support import insert_project, mk_job


async def _connect(dsn: str) -> asyncpg.Connection:
    return await asyncpg.connect(dsn)


def test_load_migrations() -> None:
    migrations = load_migrations()
    assert migrations
    assert migrations[0].version == 1
    assert migrations[0].name == "initial"
    versions = [m.version for m in migrations]
    assert versions == sorted(versions)


def test_migrations_idempotent(postgres_dsn: str) -> None:
    # Second run must be a no-op, not a failure.
    asyncio.run(apply_migrations(postgres_dsn))

    async def check() -> int:
        conn = await _connect(postgres_dsn)
        try:
            return await conn.fetchval("SELECT count(*) FROM schema_migrations")
        finally:
            await conn.close()

    assert asyncio.run(check()) == len(load_migrations())


def test_failed_migration_error_not_masked(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a migration kills the connection, the transaction rollback
    and the advisory unlock fail too; the original error must still
    propagate."""
    bad = migrations_mod.Migration(
        version=9999,
        name="kill_connection",
        sql="SELECT pg_terminate_backend(pg_backend_pid())",
    )
    monkeypatch.setattr(migrations_mod, "load_migrations", lambda: [bad])
    # The migration's own connection error, not the InterfaceError that
    # rollback/unlock raise on the now-dead connection.
    with pytest.raises(asyncpg.PostgresConnectionError):
        asyncio.run(migrations_mod.apply_migrations(postgres_dsn))


def test_project_build_attribute_crud(postgres_dsn: str) -> None:
    async def run() -> None:
        conn = await _connect(postgres_dsn)
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


def test_failed_builds_and_statuses_upsert(postgres_dsn: str) -> None:
    async def run() -> None:
        conn = await _connect(postgres_dsn)
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


def test_failed_build_cache_component(postgres_dsn: str) -> None:
    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
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


def test_get_or_create_build_concurrent_no_duplicates(postgres_dsn: str) -> None:
    # SELECT-then-INSERT must be serialized: there is no unique
    # constraint on (project_id, tree_hash), so without locking two
    # concurrent change events for the same tree create two builds.
    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn, min_size=5, max_size=5)
        try:
            project_id = await insert_project(pool, "race")
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


def test_cancelled_build_not_reused(postgres_dsn: str) -> None:
    # A cancelled build carries no verdict; re-pushing the same tree
    # must build again instead of re-reporting "cancelled".
    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            project_id = await insert_project(pool, "cancelled-reuse")
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


def test_reuse_backfills_pr_fields(postgres_dsn: str) -> None:
    # Branch push creates the build first; the PR event for the same
    # tree must backfill pr_number/pr_author so the PR author keeps
    # restart/cancel rights.
    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            project_id = await insert_project(pool, "pr-backfill")
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


def test_reuse_by_other_pr_clears_both_identity_fields(postgres_dsn: str) -> None:
    # Two PRs producing the same tree share a build; the second PR's
    # event must not leave a row mixing PR A's number with no author,
    # nor attach PR B's author to PR A's number.
    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            project_id = await insert_project(pool, "pr-cross")
            db = BuildDB(pool)
            first, _ = await db.get_or_create_build(
                project_id,
                "tree-x",
                "sha",
                "feature-a",
                pr_number=1,
                pr_author="github:alice",
            )
            second, created = await db.get_or_create_build(
                project_id,
                "tree-x",
                "sha2",
                "feature-b",
                pr_number=2,
                pr_author="github:bob",
            )
            assert not created
            assert second.id == first.id
            row = await pool.fetchrow(
                "SELECT pr_number, pr_author, branch FROM builds WHERE id = $1",
                first.id,
            )
            # Never mix two PRs' identities: clear both together. The
            # branch stays: only a plain branch push takes it over.
            assert row["pr_number"] is None
            assert row["pr_author"] is None
            assert row["branch"] == "feature-a"
        finally:
            await pool.close()

    asyncio.run(run())


def test_branch_push_takes_over_reused_pr_build(postgres_dsn: str) -> None:
    # A default-branch push reusing a PR build (identical tree after
    # the merge) sheds the PR identity and takes over the branch field,
    # else the UI and the pr_number guards keep pointing at the PR.
    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            project_id = await insert_project(pool, "pr-shed")
            db = BuildDB(pool)
            first, _ = await db.get_or_create_build(
                project_id,
                "tree-d",
                "sha",
                "feature",
                pr_number=4,
                pr_author="github:alice",
            )
            second, created = await db.get_or_create_build(
                project_id, "tree-d", "sha", "main"
            )
            assert not created
            assert second.id == first.id
            assert second.pr_number is None
            assert second.branch == "main"
            row = await pool.fetchrow(
                "SELECT pr_number, pr_author, branch FROM builds WHERE id = $1",
                first.id,
            )
            assert dict(row) == {"pr_number": None, "pr_author": None, "branch": "main"}
        finally:
            await pool.close()

    asyncio.run(run())


def test_pr_does_not_capture_branch_push_build(postgres_dsn: str) -> None:
    # A default-branch push created the build; a PR for a different
    # branch sharing the tree hash must not gain pr_author authz
    # (restart/cancel control) over it.
    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            project_id = await insert_project(pool, "pr-capture")
            db = BuildDB(pool)
            first, _ = await db.get_or_create_build(project_id, "tree-m", "sha", "main")
            second, created = await db.get_or_create_build(
                project_id,
                "tree-m",
                "sha",
                "feature",
                pr_number=9,
                pr_author="github:mallory",
            )
            assert not created
            assert second.id == first.id
            row = await pool.fetchrow(
                "SELECT pr_number, pr_author FROM builds WHERE id = $1", first.id
            )
            assert row["pr_number"] is None
            assert row["pr_author"] is None
        finally:
            await pool.close()

    asyncio.run(run())


def test_complete_attribute_preserves_eval_outputs(postgres_dsn: str) -> None:
    # Eval records the full outputs map (multi-output drvs); completion
    # must merge the freshly-known "out" path into it, not replace the
    # map, and must never NULL an existing map when no out path is
    # known (restarted/cancelled attributes).
    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            project_id = await insert_project(pool, "multi-out")
            db = BuildDB(pool)
            build, _ = await db.get_or_create_build(project_id, "tree-o", "sha", "main")
            job = mk_job("foo")
            job = job.model_copy(
                update={
                    "outputs": {
                        "out": "/nix/store/foo-out",
                        "dev": "/nix/store/foo-dev",
                    }
                }
            )
            await db.record_attributes(build.id, [job])

            async def outputs() -> dict:
                return json.loads(
                    await pool.fetchval(
                        "SELECT outputs FROM build_attributes "
                        "WHERE build_id = $1 AND attr = 'foo'",
                        build.id,
                    )
                )

            # Completion without an out path (e.g. cancelled) keeps the map.
            await db.complete_attribute(
                build.id,
                AttributeResult(
                    attr="foo",
                    status=AttributeStatus.cancelled,
                    job=job,
                    drv_path=job.drv_path,
                    system=job.system,
                ),
            )
            assert await outputs() == {
                "out": "/nix/store/foo-out",
                "dev": "/nix/store/foo-dev",
            }

            # Successful completion updates "out" but keeps "dev".
            await db.complete_attribute(
                build.id,
                AttributeResult(
                    attr="foo",
                    status=AttributeStatus.succeeded,
                    job=job,
                    out_path="/nix/store/foo-out-new",
                    drv_path=job.drv_path,
                    system=job.system,
                ),
            )
            assert await outputs() == {
                "out": "/nix/store/foo-out-new",
                "dev": "/nix/store/foo-dev",
            }
        finally:
            await pool.close()

    asyncio.run(run())


def test_complete_attribute_marks_substituted_as_cached(postgres_dsn: str) -> None:
    # The API documents `cached` as "came from cache": attributes
    # substituted from a binary cache must set it, not only ones
    # skipped because they were already in the local store.
    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            project_id = await insert_project(pool, "cached-flag")
            db = BuildDB(pool)
            build, _ = await db.get_or_create_build(project_id, "tree-s", "sha", "main")

            async def cached(attr: str) -> bool:
                return await pool.fetchval(
                    "SELECT cached FROM build_attributes "
                    "WHERE build_id = $1 AND attr = $2",
                    build.id,
                    attr,
                )

            for attr, cache_status, status in (
                ("sub", CacheStatus.cached, AttributeStatus.succeeded),
                ("local", CacheStatus.local, AttributeStatus.skipped_local),
                ("built", CacheStatus.not_built, AttributeStatus.succeeded),
            ):
                job = mk_job(attr, cache_status=cache_status)
                await db.complete_attribute(
                    build.id,
                    AttributeResult(
                        attr=attr,
                        status=status,
                        job=job,
                        out_path=f"/nix/store/{attr}-out",
                        drv_path=job.drv_path,
                        system=job.system,
                    ),
                )
            assert await cached("sub") is True
            assert await cached("local") is True
            assert await cached("built") is False
        finally:
            await pool.close()

    asyncio.run(run())


def test_complete_attribute_replaces_log_row(postgres_dsn: str) -> None:
    # Attribute restarts rewrite the same log file; the metadata row
    # must be replaced, not duplicated with stale sizes.
    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            project_id = await insert_project(pool, "log-dup")
            db = BuildDB(pool)
            build, _ = await db.get_or_create_build(project_id, "tree-l", "sha", "main")
            job = mk_job("foo")
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
    postgres_dsn: str,
) -> None:
    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            project_id = await insert_project(pool, "record-attrs")
            db = BuildDB(pool)
            build, _ = await db.get_or_create_build(
                project_id, "tree-ra", "sha", "main"
            )
            await db.record_attributes(build.id, [mk_job("a"), mk_job("b")])
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
                    job=mk_job("a"),
                    out_path="/nix/store/a-out",
                ),
            )
            await db.record_attributes(build.id, [mk_job("a")])
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
    postgres_dsn: str,
) -> None:
    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            project_id = await insert_project(pool, "building-status")
            db = BuildDB(pool)
            build, _ = await db.get_or_create_build(project_id, "tree-b", "sha", "main")
            job = mk_job("foo")
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
