"""Work-queue claim semantics: dedup, per-key serialization, requeue."""

from __future__ import annotations

import asyncio
import shutil
from typing import TYPE_CHECKING

import asyncpg
import pytest

from buildbot_nix.work_queue import WorkQueue

from .support import ephemeral_postgres, truncate_work_queue

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.skipif(
    shutil.which("initdb") is None, reason="postgresql not available"
)


@pytest.fixture(scope="module")
def postgres_dsn(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    with ephemeral_postgres(tmp_path_factory, "work_queue") as dsn:
        yield dsn


@pytest.fixture(autouse=True)
def _fresh_work_queue(postgres_dsn: str) -> None:
    truncate_work_queue(postgres_dsn)


def test_enqueue_dedupes_pending(postgres_dsn: str) -> None:
    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            queue = WorkQueue(pool)
            assert await queue.enqueue("restart", "build-1", {"build_id": 1})
            # Double click: identical pending intent collapses.
            assert not await queue.enqueue("restart", "build-1", {"build_id": 1})
            # A different payload is a distinct intent.
            assert await queue.enqueue("restart", "build-1", {"attr": "a"})
            item = await queue.claim_next()
            assert item is not None
            assert item.kind == "restart"
            assert item.payload == {"build_id": 1}
            # Claimed (running) no longer blocks new intent.
            assert await queue.enqueue("restart", "build-1", {"build_id": 1})
            await queue.finish(item.id)
        finally:
            await pool.close()

    asyncio.run(run())


def test_claim_serializes_per_key(postgres_dsn: str) -> None:
    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            queue = WorkQueue(pool)
            await queue.enqueue("restart", "build-2")
            first = await queue.claim_next()
            assert first is not None
            await queue.enqueue("effects", "build-2")
            # Same key still running: must not run concurrently.
            assert await queue.claim_next() is None
            # work_queue_running_uniq enforces this in the database.
            with pytest.raises(asyncpg.UniqueViolationError):
                await pool.execute(
                    "UPDATE work_queue SET status = 'running' "
                    "WHERE kind = 'effects' AND dedup_key = 'build-2'"
                )
            # A different key is unaffected.
            await queue.enqueue("restart", "build-3")
            other = await queue.claim_next()
            assert other is not None
            assert other.dedup_key == "build-3"
            # An exception with an empty message must still fail the item.
            await queue.finish(first.id, error=str(Exception()))
            row = await pool.fetchrow(
                "SELECT status, error FROM work_queue WHERE id = $1", first.id
            )
            assert (row["status"], row["error"]) == ("failed", "")
            blocked = await queue.claim_next()
            assert blocked is not None
            assert blocked.kind == "effects"
        finally:
            await pool.close()

    asyncio.run(run())


def test_settle_interrupted_requeues(postgres_dsn: str) -> None:
    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            queue = WorkQueue(pool)
            await queue.enqueue("change", "proj-1")
            assert await queue.claim_next() is not None
            # Same intent re-enqueued before the crash.
            await queue.enqueue("change", "proj-1")
            await queue.settle_interrupted()
            item = await queue.claim_next()
            assert item is not None
            assert item.dedup_key == "proj-1"
            await queue.finish(item.id)
            assert await queue.claim_next() is None
            superseded = await pool.fetchval(
                "SELECT count(*) FROM work_queue WHERE status = 'failed'"
            )
            assert superseded == 1
        finally:
            await pool.close()

    asyncio.run(run())
