"""Live behavior: attribute auto-refresh polling and SSE log streaming.

These exercise the JavaScript in base.html/log.html against a real
browser; the httpx web tests can only assert the markers exist.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from buildbot_nix.engine.executor import LogWriter

if TYPE_CHECKING:
    from playwright.sync_api import Page

    from .support import EngineServer

ATTR = "x86_64-linux.ok"


async def _build_id(server: EngineServer, number: int) -> int:
    return await server.pool.fetchval("SELECT id FROM builds WHERE number = $1", number)


def test_attribute_table_refreshes_while_building(
    page: Page, server: EngineServer
) -> None:
    page.goto("/projects/acme/widget/builds/3")
    row = page.locator('tr[data-attr="aarch64-linux.other"]')
    assert "succeeded" in row.inner_text()

    async def fail_attribute() -> None:
        build_id = await _build_id(server, 3)
        await server.pool.execute(
            """
            UPDATE build_attributes SET status = 'failed',
                   error = 'error: flipped by e2e test'
            WHERE build_id = $1 AND attr = 'aarch64-linux.other'
            """,
            build_id,
        )

    server.run(fail_attribute())
    # The 5s poll tick swaps in the updated attribute fragment.
    row.locator(".status.failed").wait_for(timeout=15_000)
    assert "flipped by e2e test" in page.content()


def test_log_page_streams_live_output(page: Page, server: EngineServer) -> None:
    build_id = server.run(_build_id(server, 3))
    writer = LogWriter(path=server.state_dir / "live" / f"{ATTR}.zst")
    server.registry.register(build_id, ATTR, writer)
    try:
        page.goto(f"/projects/acme/widget/builds/3/logs/{ATTR}")
        log = page.locator("#log")

        # Live-only logs have no history replay: lines sent before the
        # EventSource subscribes would be lost, so wait for the stream.
        deadline = time.monotonic() + 15
        while not writer._subscribers:  # noqa: SLF001
            if time.monotonic() > deadline:
                msg = "browser never connected to the SSE stream"
                raise TimeoutError(msg)
            time.sleep(0.1)

        server.run(writer.write(b"hello from the build\n"))
        log.get_by_text("hello from the build").wait_for(timeout=15_000)

        server.run(writer.write(b"second line arrives later\n"))
        log.get_by_text("second line arrives later").wait_for(timeout=15_000)

        # close() ends the SSE stream; streamed content stays visible.
        server.run(writer.close())
        assert "hello from the build" in log.inner_text()
    finally:
        server.registry.unregister(build_id, ATTR)
