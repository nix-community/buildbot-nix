"""Work queue (migration 0010): producers enqueue intent, one
dispatcher claims and executes. Pending items dedupe per (kind, key);
claims serialize per key and survive restarts via requeue. Dedup
includes the payload: same key with different payloads (e.g. restarts
of different attributes) are distinct intents."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import asyncpg


@dataclass(frozen=True)
class WorkItem:
    id: int
    kind: str
    dedup_key: str
    payload: dict[str, Any]


class WorkQueue:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def enqueue(
        self, kind: str, dedup_key: str, payload: dict[str, Any] | None = None
    ) -> bool:
        """Returns False when an identical pending item already exists."""
        return (
            await self.pool.fetchval(
                """
                INSERT INTO work_queue (kind, dedup_key, payload)
                VALUES ($1, $2, $3)
                ON CONFLICT (kind, dedup_key, md5(payload::text))
                WHERE status = 'pending'
                DO NOTHING
                RETURNING id
                """,
                kind,
                dedup_key,
                json.dumps(payload or {}),
            )
            is not None
        )

    async def claim_next(self) -> WorkItem | None:
        """Claim the oldest pending item whose dedup key is idle."""
        try:
            row = await self._claim_row()
        except asyncpg.UniqueViolationError:
            # Lost the running slot (see work_queue_running_uniq);
            # the item stays pending for a later pass.
            return None
        if row is None:
            return None
        return WorkItem(
            id=row["id"],
            kind=row["kind"],
            dedup_key=row["dedup_key"],
            payload=json.loads(row["payload"]),
        )

    async def _claim_row(self) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            """
            UPDATE work_queue SET status = 'running', claimed_at = now()
            WHERE id = (
                SELECT id FROM work_queue w
                WHERE w.status = 'pending'
                  AND NOT EXISTS (
                    SELECT 1 FROM work_queue r
                    WHERE r.dedup_key = w.dedup_key AND r.status = 'running'
                  )
                ORDER BY w.created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id, kind, dedup_key, payload
            """
        )

    async def finish(self, item_id: int, *, error: str | None = None) -> None:
        await self.pool.execute(
            """
            UPDATE work_queue
            SET status = $2, error = $3, finished_at = now()
            WHERE id = $1
            """,
            item_id,
            "done" if error is None else "failed",
            error,
        )

    async def settle_interrupted(self) -> None:
        """Startup: requeue work the previous process died holding.
        Executors are idempotent against completed state (existing
        builds are found by tree hash, effects by the started flag),
        so re-dispatching is safe. Assumes a single dispatcher process;
        with several, this would steal live work (a claimed_at lease
        would be needed instead)."""
        await self.pool.execute(
            """
            UPDATE work_queue w SET status = 'pending', claimed_at = NULL
            WHERE status = 'running' AND NOT EXISTS (
                SELECT 1 FROM work_queue p
                WHERE p.kind = w.kind AND p.dedup_key = w.dedup_key
                  AND p.status = 'pending'
            )
            """
        )
        # An identical intent was re-enqueued before the crash; the
        # pending row carries it, requeueing would hit pending_uniq.
        await self.pool.execute(
            """
            UPDATE work_queue SET status = 'failed', finished_at = now(),
                error = 'interrupted; superseded by a newer request'
            WHERE status = 'running'
            """
        )

    async def cleanup(self, retention_days: int) -> None:
        await self.pool.execute(
            """
            DELETE FROM work_queue
            WHERE finished_at IS NOT NULL
              AND finished_at < now() - make_interval(days => $1)
            """,
            retention_days,
        )
