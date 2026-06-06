"""Live status events for the web frontend.

Postgres triggers (migration 0005) NOTIFY on every build/attribute
status change; the broker holds one LISTEN connection and fans the
payloads out to SSE subscribers. Pages refetch fragments or patch
rows when an event arrives instead of polling on a timer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import asyncpg

    from .app import WebContext

logger = logging.getLogger(__name__)

CHANNEL = "build_events"
KEEPALIVE_SECONDS = 30.0
RECONNECT_SECONDS = 5.0
# A slow client just loses events; each one only triggers a refetch
# of current state.
QUEUE_SIZE = 64


@dataclass(eq=False)
class Subscription:
    queue: asyncio.Queue[str]
    build_id: int | None = None
    project_id: int | None = None
    # None means all projects are visible to this subscriber.
    visible: set[int] | None = None

    def wants(self, event: dict[str, Any]) -> bool:
        if self.build_id is not None and event.get("build_id") != self.build_id:
            return False
        if self.project_id is not None and event.get("project_id") != self.project_id:
            return False
        return self.visible is None or event.get("project_id") in self.visible


class EventBroker:
    """One LISTEN connection, many SSE subscribers."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool
        self._subscriptions: set[Subscription] = set()
        self._conn: asyncpg.pool.PoolConnectionProxy | None = None
        self._stopped = False
        self._reconnect_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._conn = await self.pool.acquire()
        await self._conn.add_listener(CHANNEL, self._on_notify)
        # A killed connection (postgres restart) would silence events
        # forever; reconnect in the background.
        self._conn.add_termination_listener(self._on_termination)

    async def stop(self) -> None:
        self._stopped = True
        if self._conn is None:
            return
        conn, self._conn = self._conn, None
        with contextlib.suppress(Exception):
            await conn.remove_listener(CHANNEL, self._on_notify)
        await self.pool.release(conn)

    def _on_termination(self, _conn: object) -> None:
        if self._stopped:
            return
        self._conn = None
        # Keep a reference so the task is not garbage-collected.
        self._reconnect_task = asyncio.get_running_loop().create_task(self._reconnect())

    async def _reconnect(self) -> None:
        while not self._stopped:
            try:
                await self.start()
            except Exception:
                logger.exception("event listener reconnect failed; retrying")
                await asyncio.sleep(RECONNECT_SECONDS)
            else:
                logger.info("event listener reconnected")
                return

    def _on_notify(
        self,
        _conn: object,
        _pid: object,
        _channel: object,
        payload: object,
    ) -> None:
        try:
            event = json.loads(str(payload))
        except ValueError:
            logger.warning("malformed notify payload", extra={"payload": payload})
            return
        for sub in self._subscriptions:
            if sub.wants(event):
                with contextlib.suppress(asyncio.QueueFull):
                    sub.queue.put_nowait(str(payload))

    def subscribe(
        self,
        build_id: int | None = None,
        project_id: int | None = None,
        visible: set[int] | None = None,
    ) -> Subscription:
        sub = Subscription(
            queue=asyncio.Queue(QUEUE_SIZE),
            build_id=build_id,
            project_id=project_id,
            visible=visible,
        )
        self._subscriptions.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subscriptions.discard(sub)


def create_events_router(ctx: WebContext, broker: EventBroker) -> APIRouter:
    router = APIRouter()

    @router.get("/events")
    async def events(
        request: Request,
        build: int | None = None,
        project: int | None = None,
    ) -> StreamingResponse:
        visible = await ctx.visible_project_ids(request)
        sub = broker.subscribe(
            build_id=build,
            project_id=project,
            visible=set(visible) if visible is not None else None,
        )

        async def gen() -> AsyncIterator[str]:
            try:
                yield "retry: 3000\n\n"
                while True:
                    try:
                        payload = await asyncio.wait_for(
                            sub.queue.get(), KEEPALIVE_SECONDS
                        )
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield f"data: {payload}\n\n"
            finally:
                broker.unsubscribe(sub)

        return StreamingResponse(gen(), media_type="text/event-stream")

    return router
