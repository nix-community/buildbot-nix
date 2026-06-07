"""Web frontend tests: HTML views over a seeded
ephemeral Postgres via the httpx ASGI test client."""

# ruff: noqa: PLR2004 (literal values in test assertions are fine)

from __future__ import annotations

import asyncio
import json
import re
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg
import httpx
import pytest
import zstandard

from buildbot_nix.db import BuildStatus
from buildbot_nix.effects_state import TaskTokens
from buildbot_nix.executor import LogWriter
from buildbot_nix.scheduler import AttributeStatus
from buildbot_nix.web.app import create_app
from buildbot_nix.web.events import EventBroker
from buildbot_nix.web.logs import (
    AnsiHtmlStream,
    ansi_to_html,
    render_log_lines,
    strip_ansi,
)
from buildbot_nix.web.state_routes import create_state_router
from buildbot_nix.web.templating import timeago

from .e2e.support import seed
from .support import ephemeral_postgres

if TYPE_CHECKING:
    from collections.abc import Iterator

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


def test_html_error_pages(client: WebClient) -> None:
    missing = client.loop.run_until_complete(
        client.http.get("/repos/github/nope/nope", headers={"accept": "text/html"})
    )
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("text/html")
    assert 'href="/"' in missing.text

    api = get(client, "/api/repos/github/nope/nope")
    assert api.status_code == 404
    assert api.json() == {"detail": "Not Found"}


def test_api_strips_ansi_from_errors(client: WebClient) -> None:
    build = get(client, "/api/repos/github/acme/widget/builds/2").json()
    errors = [a["error"] for a in build["attributes"] if a["error"]]
    assert errors
    assert all("\x1b" not in e for e in errors)


def test_homepage(client: WebClient) -> None:
    response = get(client, "/")
    assert response.status_code == 200
    assert "acme/widget" in response.text  # sidebar
    assert "#3" in response.text  # recent feed


def test_project_page_with_filters(client: WebClient) -> None:
    response = get(client, "/repos/github/acme/widget")
    assert response.status_code == 200
    assert "#1" in response.text
    assert "#2" in response.text

    filtered = get(client, "/repos/github/acme/widget?status=failed")
    assert "#2" in filtered.text
    assert ">#1<" not in filtered.text

    by_branch = get(client, "/repos/github/acme/widget?ref=feature")
    assert "#3" in by_branch.text
    assert ">#2<" not in by_branch.text

    # A numeric ref means a PR; build 3 is PR #5.
    by_pr = get(client, "/repos/github/acme/widget?ref=%235")
    assert "#3" in by_pr.text
    assert ">#2<" not in by_pr.text


def test_build_page(client: WebClient) -> None:
    response = get(client, "/repos/github/acme/widget/builds/2")
    assert response.status_code == 200
    text = response.text
    # Failures render eagerly in an open group; the succeeded bulk is
    # collapsed to a count.
    assert "x86_64-linux.bad" in text
    failed_group = re.search(r'<details[^>]*data-group="failed"[^>]*>', text)
    assert failed_group is not None
    assert "open" in failed_group.group(0)
    assert "x86_64-linux.ok" not in text
    assert "2 succeeded" in text
    assert "attrs?group=succeeded" in text
    # Groups scroll internally so the summaries below stay reachable.
    assert text.count('class="attr-scroll"') >= 2
    # Search queries the server, not just the loaded rows.
    assert 'name="q"' in text
    # Inline error excerpt.
    # ANSI in the stored excerpt renders as color, not as escapes.
    assert "builder failed loudly" in text
    assert '<span class="ansi-red' in text
    assert "\x1b" not in text
    # Prev/next navigation.
    assert "/builds/1" in text
    assert "/builds/3" in text
    # Forge links.
    assert "https://github.com/acme/widget/commit/sha-2" in text


def test_attribute_rows_fragment_paginates(client: WebClient) -> None:
    base = "/repos/github/acme/widget/builds/2/attrs"
    rows = get(client, f"{base}?group=succeeded")
    assert rows.status_code == 200
    assert "x86_64-linux.ok" in rows.text
    assert "aarch64-linux.other" in rows.text

    first = get(client, f"{base}?group=succeeded&limit=1").text
    assert "aarch64-linux.other" in first  # alphabetical within the group
    assert "x86_64-linux.ok" not in first
    assert "page=2" in first  # load-more sentinel

    second = get(client, f"{base}?group=succeeded&limit=1&page=2").text
    assert "x86_64-linux.ok" in second
    assert "page=3" not in second

    assert get(client, f"{base}?group=nonsense").status_code == 404


def test_build_page_shows_eval_warnings_as_text(client: WebClient) -> None:
    async def seed() -> None:
        ctx = client.app.state.web_context
        await ctx.pool.execute(
            "UPDATE builds SET eval_warnings = $1::jsonb WHERE number = 2",
            json.dumps(["evaluation warning: foo is deprecated", "trace: bar"]),
        )

    client.loop.run_until_complete(seed())
    text = get(client, "/repos/github/acme/widget/builds/2").text
    assert "evaluation warning: foo is deprecated" in text
    assert "trace: bar" in text
    # Decoded, not the raw jsonb string.
    assert "[&#34;" not in text
    assert '["' not in text


def test_running_build_shows_progress(client: WebClient) -> None:
    running = get(client, "/repos/github/acme/widget/builds/3").text
    assert 'class="progress-track"' in running
    assert 'class="spinner"' in running
    assert "seg ok" in running  # settled attrs render as a green segment
    assert "3 of 3" in running  # seeded attrs are all settled
    finished = get(client, "/repos/github/acme/widget/builds/2").text
    assert 'class="progress-track"' not in finished


def test_build_page_live_markers(client: WebClient) -> None:
    running = get(client, "/repos/github/acme/widget/builds/3")
    # htmx SSE wiring: event stream scoped to the build, main region
    # refetched and morphed on events.
    assert 'sse-connect="/events?build=' in running.text
    assert 'hx-trigger="sse:message' in running.text
    # Finished pages re-check on connect too (rebuild-failed).
    finished = get(client, "/repos/github/acme/widget/builds/2")
    assert "htmx:sseOpen" in finished.text
    # PR link on the PR build.
    assert "/pull/5" in running.text


def test_attribute_history(client: WebClient) -> None:
    response = get(client, "/repos/github/acme/widget/attrs/x86_64-linux.bad")
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
    assert get(client, "/repos/github/acme/widget?page=0").status_code == 200
    assert get(client, "/repos/github/acme/widget?page=-5").status_code == 200
    api = get(client, "/api/repos/github/acme/widget/builds?page=0")
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
    all_rows = get(client, "/repos/github/acme/widget/rows?before=999999")
    assert all_rows.text.count("data-id=") == 3

    failed = get(client, "/repos/github/acme/widget/rows?before=999999&status=failed")
    assert failed.text.count("data-id=") == 1
    assert ">#2<" in failed.text

    branch = get(client, "/repos/github/acme/widget/rows?before=999999&ref=feature")
    assert branch.text.count("data-id=") == 1
    assert ">#3<" in branch.text


def test_metrics_are_gauges(client: WebClient) -> None:
    # Table-derived series shrink, so they must not be counters.
    text = get(client, "/metrics").text
    assert "# TYPE buildbot_nix_builds_total gauge" in text
    assert "counter" not in text


def test_static_urls_carry_content_version(client: WebClient) -> None:
    page = get(client, "/").text
    m = re.search(r"/static/style\.css\?v=([0-9a-f]{12})", page)
    assert m
    response = get(client, f"/static/style.css?v={m.group(1)}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_legacy_project_urls_redirect(client: WebClient) -> None:
    r = get(client, "/projects/acme/widget/builds/2?x=1")
    assert r.status_code == 307
    assert r.headers["location"] == "/repos/github/acme/widget/builds/2?x=1"
    r = get(client, "/api/projects/acme/widget")
    assert r.headers["location"] == "/api/repos/github/acme/widget"
    assert get(client, "/projects/acme/nope").status_code == 404


def test_health_and_static(client: WebClient) -> None:
    assert get(client, "/health").text == "ok"
    css = get(client, "/static/style.css")
    assert "prefers-color-scheme: dark" in css.text


def test_404s(client: WebClient) -> None:
    assert get(client, "/repos/github/acme/nope").status_code == 404
    assert get(client, "/repos/github/acme/widget/builds/999").status_code == 404


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


def test_ansi_sgr_is_stateful() -> None:
    # Codes accumulate: bold must not drop the color set earlier.
    assert ansi_to_html("\x1b[31mr\x1b[1mb") == (
        '<span class="ansi-red">r</span><span class="ansi-red ansi-bold">b</span>'
    )
    # Unknown codes (dim) are ignored, not a reset.
    assert ansi_to_html("\x1b[31ma\x1b[2mb") == (
        '<span class="ansi-red">a</span><span class="ansi-red">b</span>'
    )
    # 5;31 are arguments of the (unsupported) 256-color code 38, not
    # a basic red.
    assert ansi_to_html("\x1b[38;5;31mx") == "x"
    assert ansi_to_html("\x1b[38;2;1;2;3mx") == "x"
    # systemd uses ITU colon syntax: arguments ride along inside one
    # parameter; the sequence must not leak into the output.
    out = ansi_to_html("\x1b[0;1;38:5:185meth1")
    assert out == '<span class="ansi-bold">eth1</span>'
    assert strip_ansi("\x1b[0;1;38:5:185meth1") == "eth1"


def test_osc8_hyperlinks() -> None:
    raw = "\x1b]8;;https://example.com\x1b\\click\x1b]8;;\x1b\\ plain"
    assert ansi_to_html(raw) == (
        '<a href="https://example.com" rel="nofollow">click</a> plain'
    )
    # Colored link text nests; the link survives an SGR reset.
    raw = "\x1b]8;;https://e.x\x07\x1b[31mred\x1b[0mplain\x1b]8;;\x07"
    assert ansi_to_html(raw) == (
        '<a href="https://e.x" rel="nofollow"><span class="ansi-red">red</span></a>'
        '<a href="https://e.x" rel="nofollow">plain</a>'
    )
    # Untrusted logs: only http(s) targets become links.
    assert ansi_to_html("\x1b]8;;javascript:alert(1)\x1b\\x\x1b]8;;\x1b\\") == "x"
    assert strip_ansi("\x1b]8;;https://e.x\x07click\x1b]8;;\x07") == "click"


def test_non_sgr_sequences_are_stripped() -> None:
    cases = {
        "\x1b]0;make all\x07hello": "hello",  # window title
        "\x1b(Bhello": "hello",  # charset select
        "\x1b7hello": "hello",  # DECSC
        "\x1b[>4;2mhello": "hello",  # xterm private CSI
        "ding\x07dong": "dingdong",  # BEL
        "a\x0bb\x0cc": "abc",  # C0 controls
        "tab\there": "tab\there",  # tab survives
    }
    for raw, expected in cases.items():
        assert ansi_to_html(raw) == expected, raw
        assert strip_ansi(raw) == expected, raw


def test_render_log_lines_carries_color_across_lines() -> None:
    # nix error blocks are colored over several lines; the reset
    # arrives lines later.
    out = render_log_lines("\x1b[31mone\ntwo\x1b[0m\nthree")
    assert '<span class="ansi-red">one</span>' in out
    assert '<span class="ansi-red">two</span>' in out
    assert ">three</span>" in out


def test_ansi_stream_state_across_chunks() -> None:
    stream = AnsiHtmlStream()
    # Escape sequence split between chunks.
    assert stream.feed("a\x1b[") == "a"
    assert stream.feed("31mred") == '<span class="ansi-red">red</span>'
    # The open color carries into the next chunk.
    assert stream.feed("still red") == '<span class="ansi-red">still red</span>'
    assert stream.feed("\x1b[0mplain") == "plain"
    # An OSC split across chunks is held until its terminator.
    assert stream.feed("a\x1b]8;;https://e") == "a"
    assert stream.feed(".x\x1b") == ""
    link = '<a href="https://e.x" rel="nofollow">link</a>'
    assert stream.feed("\\link") == link


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
    viewer = get(client, "/repos/github/acme/widget/builds/2/logs/x86_64-linux.bad")
    assert viewer.status_code == 200
    assert "ansi-red" in viewer.text
    assert 'id="L1"' in viewer.text

    # Prev/next navigation to the same attribute in neighboring builds.
    assert 'rel="prev"' in viewer.text
    assert "/builds/1/logs/x86_64-linux.bad" in viewer.text
    assert 'rel="next"' in viewer.text
    assert "/builds/3/logs/x86_64-linux.bad" in viewer.text

    raw = get(client, "/repos/github/acme/widget/builds/2/logs/x86_64-linux.bad.txt")
    assert "build exploded" in raw.text

    missing = get(client, "/repos/github/acme/widget/builds/2/logs/nope")
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
        response = get(
            client, "/repos/github/acme/widget/builds/3/logs/x86_64-linux.ok"
        )
        assert response.status_code == 200
        assert "waiting for the build to start" in response.text
    finally:
        client.loop.run_until_complete(restore())


def test_log_sse_stream_finished(client: WebClient, tmp_path: Path) -> None:
    seed_log(client, tmp_path)
    response = get(
        client, "/repos/github/acme/widget/builds/2/logs/x86_64-linux.bad/stream"
    )
    assert response.status_code == 200
    assert "build exploded" in response.text
    assert "event: done" in response.text


# --- JSON API ----------------------------------------------------------


def test_api_projects_and_builds(client: WebClient) -> None:
    projects = get(client, "/api/repos").json()
    assert any(p["name"] == "widget" for p in projects)

    repo = get(client, "/api/repos/github/acme/widget").json()
    assert repo["name"] == "widget"
    assert repo["default_branch"] == "main"

    builds = get(client, "/api/repos/github/acme/widget/builds").json()
    assert builds["page"] == 1
    assert len(builds["items"]) == 3

    filtered = get(client, "/api/repos/github/acme/widget/builds?status=failed").json()
    assert [b["number"] for b in filtered["items"]] == [2]

    detail = get(client, "/api/repos/github/acme/widget/builds/2").json()
    assert detail["build"]["status"] == "failed"
    statuses = {a["attr"]: a["status"] for a in detail["attributes"]}
    assert statuses["x86_64-linux.bad"] == "failed"

    history = get(client, "/api/repos/github/acme/widget/attrs/x86_64-linux.bad").json()
    assert len(history) == 3

    queue = get(client, "/api/queue").json()
    assert [b["number"] for b in queue] == [3]


def test_build_rows_link_refs(client: WebClient) -> None:
    # Branch builds link to the branch, PR builds to the pull request.
    page = get(client, "/repos/github/acme/widget").text
    assert "https://github.com/acme/widget/tree/main" in page
    assert "https://github.com/acme/widget/pull/5" in page
    # Cross-project rows on the activity feed carry their own forge URL.
    activity = get(client, "/builds").text
    assert "https://github.com/acme/widget/pull/5" in activity


def test_attributes_sort_building_before_pending(client: WebClient) -> None:
    seeded = ("a-pending", "b-building", "c-failed")

    async def run() -> list[str]:
        ctx = client.app.state.web_context
        build_id = await ctx.pool.fetchval("SELECT id FROM builds WHERE number = 3")
        await ctx.pool.execute(
            """
            INSERT INTO build_attributes (build_id, attr, system, status)
            VALUES ($1, 'a-pending', 'x86_64-linux', 'pending'),
                   ($1, 'b-building', 'x86_64-linux', 'building'),
                   ($1, 'c-failed', 'x86_64-linux', 'failed')
            """,
            build_id,
        )
        try:
            rows = await ctx.queries.attributes(build_id)
            return [r["attr"] for r in rows if r["attr"] in seeded]
        finally:
            await ctx.pool.execute(
                "DELETE FROM build_attributes WHERE build_id = $1 AND attr = ANY($2)",
                build_id,
                seeded,
            )

    order = client.loop.run_until_complete(run())
    assert order == ["c-failed", "b-building", "a-pending"]


def test_queue_sorts_active_before_pending(client: WebClient) -> None:
    async def run() -> list[int]:
        ctx = client.app.state.web_context
        project_id = await ctx.pool.fetchval(
            "SELECT id FROM projects WHERE name = 'widget'"
        )
        await ctx.pool.execute(
            """
            INSERT INTO builds (project_id, number, commit_sha, branch, status)
            VALUES ($1, 90, 'q1', 'main', 'pending'),
                   ($1, 91, 'q2', 'main', 'building')
            """,
            project_id,
        )
        try:
            return [b["number"] for b in await ctx.queries.queue()]
        finally:
            await ctx.pool.execute("DELETE FROM builds WHERE number IN (90, 91)")

    # Build 3 is evaluating; both active builds come before the
    # earlier-submitted pending one.
    assert client.loop.run_until_complete(run()) == [3, 91, 90]

    assert get(client, "/api/repos/github/acme/nope").status_code == 404


def test_openapi_docs(client: WebClient) -> None:
    spec = get(client, "/api/openapi.json").json()
    assert "/api/repos" in spec["paths"]
    # HTML pages and other non-API routes stay out of the spec.
    assert all(path.startswith("/api/") for path in spec["paths"])
    assert get(client, "/docs").status_code == 200
    # Responses are typed: every operation documents a response schema.
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            schema = op["responses"]["200"]["content"]["application/json"]["schema"]
            assert schema not in ({}, None), f"{method} {path}"
    assert "Build" in spec["components"]["schemas"]


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
            base = "/repos/github/acme/widget/builds/3/logs/x86_64-linux.ok"
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
    hits = get(client, "/api/repos/github/acme/widget/builds?commit=sha-2").json()
    assert [b["number"] for b in hits["items"]] == [2]
    misses = get(client, "/api/repos/github/acme/widget/builds?commit=ffff").json()
    assert misses["items"] == []


def test_log_tail_param(client: WebClient, tmp_path: Path) -> None:
    seed_log(client, tmp_path)
    full = get(client, "/repos/github/acme/widget/builds/2/logs/x86_64-linux.bad.txt")
    assert "build exploded" in full.text
    tail = get(
        client, "/repos/github/acme/widget/builds/2/logs/x86_64-linux.bad.txt?tail=1"
    )
    # ANSI escapes are stripped from the plain-text view.
    assert tail.text == "build exploded\n"


def test_api_build_failures(client: WebClient, tmp_path: Path) -> None:
    seed_log(client, tmp_path)
    summary = get(client, "/api/repos/github/acme/widget/builds/2/failures").json()
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
        # Restore the seeded state for later tests.
        await pool.execute(
            "UPDATE build_attributes SET status = 'succeeded' "
            "WHERE build_id = $1 AND attr = 'x86_64-linux.ok'",
            build_id,
        )
        await pool.execute(
            "UPDATE builds SET status = 'building' WHERE id = $1", build_id
        )
        await broker.stop()
        await pool.close()
        return attr_event, build_event

    attr_event, build_event = client.loop.run_until_complete(run())
    assert attr_event["attr"] == "x86_64-linux.ok"
    assert attr_event["status"] == "failed"
    assert "attr" not in build_event
    assert build_event["status"] == "failed"


def test_attribute_search(client: WebClient) -> None:
    # Results keep the group structure, opened and filtered.
    found = get(client, "/repos/github/acme/widget/builds/2/attrs?q=bad")
    assert "x86_64-linux.bad" in found.text
    assert "x86_64-linux.ok" not in found.text
    assert "1 failed" in found.text
    assert "succeeded" not in found.text
    by_name = get(client, "/repos/github/acme/widget/builds/2/attrs?q=other")
    assert "1 succeeded" in by_name.text
    assert "open>" in by_name.text
    # Clearing the query restores the unfiltered view.
    empty = get(client, "/repos/github/acme/widget/builds/2/attrs?q=")
    assert "2 succeeded" in empty.text
    assert "attrs?group=succeeded" in empty.text


def test_css_covers_every_status() -> None:
    """Status values double as CSS classes; a missing rule silently
    renders a grey icon."""
    css = (Path(__file__).parent.parent / "web" / "static" / "style.css").read_text()
    statuses = (
        {s.value for s in AttributeStatus}
        | set(BuildStatus.TERMINAL)
        | {
            BuildStatus.PENDING,
            BuildStatus.EVALUATING,
            BuildStatus.BUILDING,
        }
    )
    missing = [s for s in statuses if f".{s}" not in css]
    assert not missing


def test_build_page_shows_effects(client: WebClient) -> None:
    async def seed_effects() -> None:
        ctx = client.app.state.web_context
        build_id = await ctx.pool.fetchval("SELECT id FROM builds WHERE number = 2")
        await ctx.pool.execute(
            """
            INSERT INTO build_effects (build_id, name, status, error, log_path,
                                       finished_at)
            VALUES ($1, 'deploy', 'failed', 'ssh: connection refused',
                    'logs/effect-deploy.zst', now()),
                   ($1, 'notify', 'running', NULL, NULL, NULL)
            """,
            build_id,
        )

    client.loop.run_until_complete(seed_effects())
    text = get(client, "/repos/github/acme/widget/builds/2").text
    assert "Effects" in text
    assert "deploy" in text
    assert "ssh: connection refused" in text
    # Both finished-with-log and running effects link to the viewer.
    assert "logs/effect:deploy" in text
    assert "logs/effect:notify" in text
    # Builds without effects don't render the section.
    assert "Effects" not in get(client, "/repos/github/acme/widget/builds/1").text


def test_effect_log_raw_text(client: WebClient, tmp_path: Path) -> None:
    async def seed_effect_log() -> None:
        ctx = client.app.state.web_context
        ctx.state_dir = tmp_path
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "effect-deploy2.zst").write_bytes(
            zstandard.ZstdCompressor().compress(b"activating system...\n")
        )
        build_id = await ctx.pool.fetchval("SELECT id FROM builds WHERE number = 2")
        await ctx.pool.execute(
            "INSERT INTO build_effects (build_id, name, status, log_path, "
            "finished_at) VALUES ($1, 'deploy2', 'succeeded', "
            "'logs/effect-deploy2.zst', now())",
            build_id,
        )

    client.loop.run_until_complete(seed_effect_log())
    response = get(client, "/repos/github/acme/widget/builds/2/logs/effect:deploy2.txt")
    assert response.status_code == 200
    assert "activating system" in response.text


def test_effect_changes_notify_build_events(client: WebClient) -> None:
    """The page's SSE refresh rides on build_events; without a trigger
    on build_effects the Effects section never updates live."""

    async def run() -> list[str]:
        ctx = client.app.state.web_context
        build_id = await ctx.pool.fetchval("SELECT id FROM builds WHERE number = 3")
        received: list[str] = []
        conn = await ctx.pool.acquire()
        listener = lambda *args: received.append(args[3])  # noqa: E731
        try:
            await conn.add_listener("build_events", listener)
            await ctx.pool.execute(
                "INSERT INTO build_effects (build_id, name) VALUES ($1, 'fxlive')",
                build_id,
            )
            await ctx.pool.execute(
                "UPDATE build_effects SET status = 'succeeded', "
                "finished_at = now() WHERE build_id = $1 AND name = 'fxlive'",
                build_id,
            )
            await asyncio.sleep(0.2)
        finally:
            await conn.remove_listener("build_events", listener)
            await ctx.pool.release(conn)
        return received

    received = client.loop.run_until_complete(run())
    payloads = [json.loads(r) for r in received]
    assert [p["status"] for p in payloads] == ["running", "succeeded"]
    assert all(p["effect"] == "fxlive" for p in payloads)


def test_effects_state_api(client: WebClient, tmp_path: Path) -> None:
    tokens = TaskTokens()
    client.app.include_router(create_state_router(tmp_path, tokens))
    token = tokens.issue(7)

    auth = {"Authorization": f"Bearer {token}"}
    url = "/api/v1/current-task/state/known-hosts/data"

    async def run() -> None:
        assert (await client.http.get(url)).status_code == 401
        assert (await client.http.get(url, headers=auth)).status_code == 404
        put = await client.http.put(url, headers=auth, content=b"host key\n")
        assert put.status_code == 200
        got = await client.http.get(url, headers=auth)
        assert got.status_code == 200
        assert got.content == b"host key\n"
        # quote() keeps dots: "." and ".." would resolve to the
        # project directory and its parent (reachable via %2E%2E).
        for dots in ("%2E%2E", "%2E"):
            dot_url = f"/api/v1/current-task/state/{dots}/data"
            put = await client.http.put(dot_url, headers=auth, content=b"x")
            assert put.status_code == 400
        # Revoked token: effects cannot reach the API after their run.
        tokens.revoke(token)
        assert (await client.http.get(url, headers=auth)).status_code == 401

    client.loop.run_until_complete(run())
    # Scoped under the project directory, name percent-encoded.
    assert (tmp_path / "effects-state" / "7" / "known-hosts").exists()
