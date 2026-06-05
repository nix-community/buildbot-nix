"""Web frontend tests: HTML views over a seeded
ephemeral Postgres via the httpx ASGI test client."""

# ruff: noqa: PLR2004 (literal values in test assertions are fine)

from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import asyncpg
import httpx
import pytest
import zstandard

from buildbot_nix.engine.executor import LogWriter
from buildbot_nix.engine.web.app import create_app, timeago
from buildbot_nix.engine.web.logs import ansi_to_html, render_log_lines

from .e2e.support import ephemeral_postgres, seed

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from fastapi import FastAPI

pytestmark = pytest.mark.skipif(
    shutil.which("initdb") is None, reason="postgresql not available"
)


@pytest.fixture(scope="module")
def postgres_dsn(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    with ephemeral_postgres(tmp_path_factory, "web") as dsn:
        asyncio.run(seed(dsn))
        yield dsn


@pytest.fixture(scope="module")
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(event_loop)
    yield event_loop
    asyncio.set_event_loop(None)
    event_loop.close()


@pytest.fixture(scope="module")
def client(postgres_dsn: str, loop: asyncio.AbstractEventLoop) -> Iterator[WebClient]:
    async def make_pool() -> asyncpg.Pool:
        return await asyncpg.create_pool(postgres_dsn)

    pool = loop.run_until_complete(make_pool())
    app = create_app(pool)
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    yield WebClient(loop, http, app)
    loop.run_until_complete(http.aclose())
    loop.run_until_complete(pool.close())


class WebClient:
    def __init__(
        self,
        event_loop: asyncio.AbstractEventLoop,
        http: httpx.AsyncClient,
        app: FastAPI,
    ) -> None:
        self.loop = event_loop
        self.http = http
        self.app = app


def get(client: WebClient, url: str) -> httpx.Response:
    return client.loop.run_until_complete(client.http.get(url))


def test_homepage(client: WebClient) -> None:
    response = get(client, "/")
    assert response.status_code == 200
    assert "acme/widget" in response.text  # sidebar
    assert "#3" in response.text  # recent feed


def test_project_page_with_filters(client: WebClient) -> None:
    response = get(client, "/projects/acme/widget")
    assert response.status_code == 200
    assert "#1" in response.text
    assert "#2" in response.text

    filtered = get(client, "/projects/acme/widget?status=failed")
    assert "#2" in filtered.text
    assert ">#1<" not in filtered.text

    by_branch = get(client, "/projects/acme/widget?branch=feature")
    assert "#3" in by_branch.text
    assert ">#2<" not in by_branch.text


def test_build_page(client: WebClient) -> None:
    response = get(client, "/projects/acme/widget/builds/2")
    assert response.status_code == 200
    text = response.text
    # Attributes grouped by system, failed first.
    assert text.index("x86_64-linux.bad") < text.index("x86_64-linux.ok")
    assert "aarch64-linux" in text
    # Inline error excerpt.
    assert "builder failed loudly" in text
    # Prev/next navigation.
    assert "/builds/1" in text
    assert "/builds/3" in text
    # Forge links.
    assert "https://github.com/acme/widget/commit/sha-2" in text


def test_build_page_live_poll_marker(client: WebClient) -> None:
    running = get(client, "/projects/acme/widget/builds/3")
    assert 'data-poll-url="' in running.text
    finished = get(client, "/projects/acme/widget/builds/1")
    assert 'data-poll-url="' not in finished.text
    # PR link on the PR build.
    assert "/pull/5" in running.text

    fragment = get(client, "/projects/acme/widget/builds/3/attributes")
    assert fragment.status_code == 200
    assert "x86_64-linux.ok" in fragment.text


def test_attribute_history(client: WebClient) -> None:
    response = get(client, "/projects/acme/widget/attrs/x86_64-linux.bad")
    assert response.status_code == 200
    # Appears in all three builds.
    for number in (1, 2, 3):
        assert f"/builds/{number}" in response.text


def test_search(client: WebClient) -> None:
    response = get(client, "/search?q=widg")
    assert "acme/widget" in response.text
    attrs = get(client, "/search?q=bad")
    assert "x86_64-linux.bad" in attrs.text


def test_search_escapes_like_wildcards(client: WebClient) -> None:
    # `%` and `_` must match literally, not as ILIKE wildcards.
    everything = get(client, "/search?q=%25")  # a literal "%"
    assert "acme/widget" not in everything.text
    assert "x86_64-linux" not in everything.text
    underscore = get(client, "/search?q=widge_")  # "_" must not match "t"
    assert "acme/widget" not in underscore.text


def test_page_out_of_range_does_not_error(client: WebClient) -> None:
    # page <= 0 must not turn into a negative SQL OFFSET (500).
    assert get(client, "/projects/acme/widget?page=0").status_code == 200
    assert get(client, "/projects/acme/widget?page=-5").status_code == 200
    api = get(client, "/api/projects/acme/widget/builds?page=0")
    assert api.status_code == 200
    assert api.json()["items"]


def test_queue_page(client: WebClient) -> None:
    response = get(client, "/queue")
    assert response.status_code == 200
    assert "#3" in response.text  # the building build
    assert ">#1<" not in response.text


def test_metrics_are_gauges(client: WebClient) -> None:
    # Table-derived series shrink, so they must not be counters.
    text = get(client, "/metrics").text
    assert "# TYPE buildbot_nix_builds_total gauge" in text
    assert "counter" not in text


def test_health_and_static(client: WebClient) -> None:
    assert get(client, "/health").text == "ok"
    css = get(client, "/static/style.css")
    assert "prefers-color-scheme: dark" in css.text


def test_404s(client: WebClient) -> None:
    assert get(client, "/projects/acme/nope").status_code == 404
    assert get(client, "/projects/acme/widget/builds/999").status_code == 404


def test_timeago() -> None:
    now = datetime.now(tz=UTC)
    assert timeago(None) == "—"
    assert timeago(now - timedelta(days=2)) == "2d ago"
    assert timeago(now - timedelta(minutes=5)).endswith("m ago")


# --- log viewer / streaming / raw download ---------------------------


def test_ansi_to_html() -> None:
    html_out = ansi_to_html("\x1b[31merror\x1b[0m plain <tag>")
    assert '<span class="ansi-red">error</span>' in html_out
    assert "&lt;tag&gt;" in html_out
    # Cursor sequences stripped.
    assert ansi_to_html("\x1b[2Khello") == "hello"

    lines = render_log_lines("one\ntwo")
    assert 'id="L1"' in lines
    assert 'href="#L2"' in lines


def seed_log(client: WebClient, tmp_path: Path) -> None:
    async def run() -> None:
        ctx = client.app.state.web_context
        ctx.state_dir = tmp_path
        attr_id = await ctx.pool.fetchval(
            """
            SELECT a.id FROM build_attributes a
            JOIN builds b ON b.id = a.build_id
            WHERE b.number = 2 AND a.attr = 'x86_64-linux.bad'
            """
        )
        await ctx.pool.execute("DELETE FROM logs WHERE attribute_id = $1", attr_id)
        await ctx.pool.execute(
            "INSERT INTO logs (attribute_id, path, size_bytes) VALUES ($1, $2, $3)",
            attr_id,
            "logs/2/x86_64-linux.bad.zst",
            42,
        )
        log_file = tmp_path / "logs" / "2" / "x86_64-linux.bad.zst"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_bytes(
            zstandard.ZstdCompressor().compress(b"\x1b[31mbuild exploded\x1b[0m\n")
        )

    client.loop.run_until_complete(run())


def test_log_viewer_and_raw(client: WebClient, tmp_path: Path) -> None:
    seed_log(client, tmp_path)
    viewer = get(client, "/projects/acme/widget/builds/2/logs/x86_64-linux.bad")
    assert viewer.status_code == 200
    assert "ansi-red" in viewer.text
    assert 'id="L1"' in viewer.text

    raw = get(client, "/projects/acme/widget/builds/2/logs/x86_64-linux.bad.txt")
    assert "build exploded" in raw.text

    zst = get(client, "/projects/acme/widget/builds/2/logs/x86_64-linux.bad.zst")
    assert zst.headers["content-type"] == "application/zstd"

    missing = get(client, "/projects/acme/widget/builds/2/logs/nope")
    assert missing.status_code == 404


def test_log_sse_stream_finished(client: WebClient, tmp_path: Path) -> None:
    seed_log(client, tmp_path)
    response = get(
        client, "/projects/acme/widget/builds/2/logs/x86_64-linux.bad/stream"
    )
    assert response.status_code == 200
    assert "build exploded" in response.text
    assert "event: done" in response.text


# --- JSON API ----------------------------------------------------------


def test_api_projects_and_builds(client: WebClient) -> None:
    projects = get(client, "/api/projects").json()
    assert any(p["name"] == "widget" for p in projects)

    builds = get(client, "/api/projects/acme/widget/builds").json()
    assert builds["page"] == 1
    assert len(builds["items"]) == 3

    filtered = get(client, "/api/projects/acme/widget/builds?status=failed").json()
    assert [b["number"] for b in filtered["items"]] == [2]

    detail = get(client, "/api/projects/acme/widget/builds/2").json()
    assert detail["build"]["status"] == "failed"
    statuses = {a["attr"]: a["status"] for a in detail["attributes"]}
    assert statuses["x86_64-linux.bad"] == "failed"

    history = get(client, "/api/projects/acme/widget/attrs/x86_64-linux.bad").json()
    assert len(history) == 3

    queue = get(client, "/api/queue").json()
    assert [b["number"] for b in queue] == [3]

    assert get(client, "/api/projects/acme/nope").status_code == 404


def test_openapi_docs(client: WebClient) -> None:
    spec = get(client, "/api/openapi.json").json()
    assert "/api/projects" in spec["paths"]
    assert get(client, "/docs").status_code == 200


# --- live logs ---------------------------------------------------------


def test_live_log_history_before_completion(client: WebClient, tmp_path: Path) -> None:
    """Running attributes have no logs DB row yet: viewer/raw/stream
    must fall back to the registered LogWriter's on-disk file."""
    ctx = client.app.state.web_context
    ctx.state_dir = tmp_path
    registry = client.app.state.log_registry

    async def run() -> tuple[str, str, str]:
        build_id = await ctx.pool.fetchval("SELECT id FROM builds WHERE number = 3")
        writer = LogWriter(path=tmp_path / "logs" / "live" / "x86_64-linux.ok.zst")
        await writer.write(b"early output\n")
        registry.register(build_id, "x86_64-linux.ok", writer)
        try:
            base = "/projects/acme/widget/builds/3/logs/x86_64-linux.ok"
            raw = (await client.http.get(f"{base}.txt")).text
            viewer = (await client.http.get(base)).text
            # The stream must replay history; close the writer so the
            # SSE generator terminates.
            stream_task = asyncio.ensure_future(client.http.get(f"{base}/stream"))
            await asyncio.sleep(0.1)
            await writer.write(b"late output\n")
            await writer.close()
            stream = (await stream_task).text
        finally:
            registry.unregister(build_id, "x86_64-linux.ok")
        return raw, viewer, stream

    raw, viewer, stream = client.loop.run_until_complete(run())
    assert "early output" in raw
    assert "early output" in viewer
    assert "early output" in stream
    assert "late output" in stream
    assert "event: done" in stream
