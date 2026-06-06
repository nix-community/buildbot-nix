"""Web frontend tests: HTML views over a seeded
ephemeral Postgres via the httpx ASGI test client."""

# ruff: noqa: PLR2004 (literal values in test assertions are fine)

from __future__ import annotations

import asyncio
import json
import re
import shutil
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import asyncpg
import httpx
import pytest
import zstandard

from buildbot_nix.executor import LogWriter
from buildbot_nix.web.app import create_app, timeago
from buildbot_nix.web.events import EventBroker
from buildbot_nix.web.logs import AnsiHtmlStream, ansi_to_html, render_log_lines

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


def test_build_page_shows_eval_warnings_as_text(client: WebClient) -> None:
    async def seed() -> None:
        ctx = client.app.state.web_context
        await ctx.pool.execute(
            "UPDATE builds SET eval_warnings = $1::jsonb WHERE number = 2",
            json.dumps(["evaluation warning: foo is deprecated", "trace: bar"]),
        )

    client.loop.run_until_complete(seed())
    text = get(client, "/projects/acme/widget/builds/2").text
    assert "evaluation warning: foo is deprecated" in text
    assert "trace: bar" in text
    # Decoded, not the raw jsonb string.
    assert "[&#34;" not in text
    assert '["' not in text


def test_build_page_live_markers(client: WebClient) -> None:
    running = get(client, "/projects/acme/widget/builds/3")
    # htmx SSE wiring: event stream scoped to the build, main region
    # refetched and morphed on events.
    assert 'sse-connect="/events?build=' in running.text
    assert 'hx-trigger="sse:message' in running.text
    # PR link on the PR build.
    assert "/pull/5" in running.text


def test_attribute_history(client: WebClient) -> None:
    response = get(client, "/projects/acme/widget/attrs/x86_64-linux.bad")
    assert response.status_code == 200
    # Appears in all three builds.
    for number in (1, 2, 3):
        assert f"/builds/{number}" in response.text


def test_project_filter_escapes_like_wildcards(client: WebClient) -> None:
    # `%` and `_` must match literally, not as ILIKE wildcards.
    assert "acme/widget" in get(client, "/?q=widg").text
    assert "acme/widget" not in get(client, "/?q=%25").text
    assert "acme/widget" not in get(client, "/?q=widge_").text


def test_page_out_of_range_does_not_error(client: WebClient) -> None:
    # page <= 0 must not turn into a negative SQL OFFSET (500).
    assert get(client, "/projects/acme/widget?page=0").status_code == 200
    assert get(client, "/projects/acme/widget?page=-5").status_code == 200
    api = get(client, "/api/projects/acme/widget/builds?page=0")
    assert api.status_code == 200
    assert api.json()["items"]


def test_activity_page(client: WebClient) -> None:
    response = get(client, "/builds")
    assert response.status_code == 200
    # Queue section shows the building build with its position.
    assert "queue-pos" in response.text
    assert "#3" in response.text
    # Activity feed lists finished builds too.
    assert ">#1<" in response.text


def test_activity_rows_fragment_pagination(client: WebClient) -> None:
    rows = get(client, "/builds/rows?before=1")
    assert rows.status_code == 200
    assert "<tr" not in rows.text  # nothing older than build id 1

    # Walk the cursor like the frontend does: each fragment's last
    # data-id feeds the next request until the response is empty.
    cursor = 999999
    seen: list[int] = []
    while True:
        text = get(client, f"/builds/rows?before={cursor}").text
        ids = re.findall(r'data-id="(\d+)"', text)
        if not ids:
            break
        seen.extend(int(i) for i in ids)
        cursor = int(ids[-1])
    assert len(seen) == 3
    assert seen == sorted(seen, reverse=True)  # newest first, no overlap


def test_project_rows_fragment_filters(client: WebClient) -> None:
    all_rows = get(client, "/projects/acme/widget/rows?before=999999")
    assert all_rows.text.count("data-id=") == 3

    failed = get(client, "/projects/acme/widget/rows?before=999999&status=failed")
    assert failed.text.count("data-id=") == 1
    assert ">#2<" in failed.text

    branch = get(client, "/projects/acme/widget/rows?before=999999&branch=feature")
    assert branch.text.count("data-id=") == 1
    assert ">#3<" in branch.text


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
    # Newlines between block-level spans double-space the log in <pre>.
    assert "\n" not in lines


def test_ansi_stream_state_across_chunks() -> None:
    stream = AnsiHtmlStream()
    # Escape sequence split between chunks.
    assert stream.feed("a\x1b[") == "a"
    assert stream.feed("31mred") == '<span class="ansi-red">red</span>'
    # The open color carries into the next chunk.
    assert stream.feed("still red") == '<span class="ansi-red">still red</span>'
    assert stream.feed("\x1b[0mplain") == "plain"


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

    # Prev/next navigation to the same attribute in neighboring builds.
    assert 'rel="prev"' in viewer.text
    assert "/builds/1/logs/x86_64-linux.bad" in viewer.text
    assert 'rel="next"' in viewer.text
    assert "/builds/3/logs/x86_64-linux.bad" in viewer.text

    raw = get(client, "/projects/acme/widget/builds/2/logs/x86_64-linux.bad.txt")
    assert "build exploded" in raw.text

    missing = get(client, "/projects/acme/widget/builds/2/logs/nope")
    assert missing.status_code == 404


def test_log_viewer_waits_for_queued_attribute(client: WebClient) -> None:
    # The build page links queued attributes before any log exists.
    async def make_pending() -> None:
        ctx = client.app.state.web_context
        await ctx.pool.execute(
            """
            UPDATE build_attributes a SET status = 'pending'
            FROM builds b
            WHERE b.id = a.build_id AND b.number = 3
              AND a.attr = 'x86_64-linux.ok'
            """
        )

    async def restore() -> None:
        ctx = client.app.state.web_context
        await ctx.pool.execute(
            """
            UPDATE build_attributes a SET status = 'succeeded'
            FROM builds b
            WHERE b.id = a.build_id AND b.number = 3
              AND a.attr = 'x86_64-linux.ok'
            """
        )

    client.loop.run_until_complete(make_pending())
    try:
        response = get(client, "/projects/acme/widget/builds/3/logs/x86_64-linux.ok")
        assert response.status_code == 200
        assert "waiting for the build to start" in response.text
    finally:
        client.loop.run_until_complete(restore())


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
    # The live viewer renders no snapshot; the stream replays history.
    assert "const LIVE = true" in viewer
    assert "early output" in stream
    assert "late output" in stream
    assert "event: done" in stream


def test_api_builds_commit_filter(client: WebClient) -> None:
    hits = get(client, "/api/projects/acme/widget/builds?commit=sha-2").json()
    assert [b["number"] for b in hits["items"]] == [2]
    misses = get(client, "/api/projects/acme/widget/builds?commit=ffff").json()
    assert misses["items"] == []


def test_log_tail_param(client: WebClient, tmp_path: Path) -> None:
    seed_log(client, tmp_path)
    full = get(client, "/projects/acme/widget/builds/2/logs/x86_64-linux.bad.txt")
    assert "build exploded" in full.text
    tail = get(
        client, "/projects/acme/widget/builds/2/logs/x86_64-linux.bad.txt?tail=1"
    )
    # ANSI escapes are stripped from the plain-text view.
    assert tail.text == "build exploded\n"


def test_api_build_failures(client: WebClient, tmp_path: Path) -> None:
    seed_log(client, tmp_path)
    summary = get(client, "/api/projects/acme/widget/builds/2/failures").json()
    assert summary["status"] == "failed"
    failed = {f["attr"]: f for f in summary["failures"]}
    assert "x86_64-linux.bad" in failed
    assert "build exploded" in failed["x86_64-linux.bad"]["log_tail"]
    # Succeeded attributes are not in the failure summary.
    assert "x86_64-linux.ok" not in failed


def test_llms_txt(client: WebClient) -> None:
    response = get(client, "/llms.txt")
    assert response.status_code == 200
    assert "/api/openapi.json" in response.text
    assert "failures" in response.text


def test_event_broker_pushes_status_changes(
    client: WebClient, postgres_dsn: str
) -> None:
    """Trigger -> NOTIFY -> broker -> subscriber queue, end to end."""

    async def run() -> tuple[dict, dict]:
        pool = await asyncpg.create_pool(postgres_dsn)
        broker = EventBroker(pool)
        await broker.start()
        build_id = await pool.fetchval("SELECT id FROM builds WHERE number = 3")
        ours = broker.subscribe(build_id=build_id)
        other_build = broker.subscribe(build_id=build_id + 1000)
        await pool.execute(
            "UPDATE build_attributes SET status = 'failed' "
            "WHERE build_id = $1 AND attr = 'x86_64-linux.ok'",
            build_id,
        )
        attr_event = json.loads(await asyncio.wait_for(ours.queue.get(), 5))
        await pool.execute(
            "UPDATE builds SET status = 'failed' WHERE id = $1", build_id
        )
        build_event = json.loads(await asyncio.wait_for(ours.queue.get(), 5))
        assert other_build.queue.empty()
        await broker.stop()
        await pool.close()
        return attr_event, build_event

    attr_event, build_event = client.loop.run_until_complete(run())
    assert attr_event["attr"] == "x86_64-linux.ok"
    assert attr_event["status"] == "failed"
    assert "attr" not in build_event
    assert build_event["status"] == "failed"
