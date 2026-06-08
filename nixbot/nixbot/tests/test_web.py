"""Web frontend tests: HTML views over a seeded
ephemeral Postgres via the httpx ASGI test client."""

# ruff: noqa: PLR2004 (literal values in test assertions are fine)

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

import asyncpg
import pytest
import zstandard
from fastapi import Request
from fastapi.responses import JSONResponse

from nixbot.api_tokens import ApiTokenStore
from nixbot.auth import AuthzConfig, User
from nixbot.db import BuildStatus
from nixbot.effects_state import TaskTokens
from nixbot.executor import LogWriter
from nixbot.forge_tokens import ForgeTokenStore
from nixbot.scheduler import AttributeStatus
from nixbot.visibility import VisibilityService
from nixbot.web import events as events_module
from nixbot.web.auth_routes import SESSION_COOKIE, create_auth_router
from nixbot.web.events import EventBroker
from nixbot.web.logs import (
    AnsiHtmlStream,
    ansi_to_html,
    render_log_lines,
    strip_ansi,
)
from nixbot.web.state_routes import create_state_router
from nixbot.web.templating import (
    branch_url,
    commit_url,
    make_env,
    pr_url,
    timeago,
)

from .e2e.support import seed
from .support import (
    WebHarness,
    cookie_header,
    insert_build,
    insert_project,
    web_harness,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI


@pytest.fixture(scope="module")
def postgres_dsn(postgres_dsn: str) -> str:
    asyncio.run(seed(postgres_dsn))
    return postgres_dsn


@pytest.fixture(scope="module")
def client(postgres_dsn: str) -> Iterator[WebHarness]:
    with web_harness(postgres_dsn) as harness:
        yield harness


def test_html_error_pages(client: WebHarness) -> None:
    missing = client.loop.run_until_complete(
        client.http.get("/repos/github/nope/nope", headers={"accept": "text/html"})
    )
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("text/html")
    assert 'href="/"' in missing.text

    api = client.get("/api/repos/github/nope/nope")
    assert api.status_code == 404
    assert api.json() == {"detail": "Not Found"}


def test_api_strips_ansi_from_errors(client: WebHarness) -> None:
    build = client.get("/api/repos/github/acme/widget/builds/2").json()
    errors = [a["error"] for a in build["attributes"] if a["error"]]
    assert errors
    assert all("\x1b" not in e for e in errors)


def test_homepage(client: WebHarness) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "acme/widget" in response.text  # sidebar
    assert "#3" in response.text  # recent feed


def test_project_page_with_filters(client: WebHarness) -> None:
    response = client.get("/repos/github/acme/widget")
    assert response.status_code == 200
    assert "#1" in response.text
    assert "#2" in response.text

    filtered = client.get("/repos/github/acme/widget?status=failed")
    assert "#2" in filtered.text
    assert ">#1<" not in filtered.text

    by_branch = client.get("/repos/github/acme/widget?ref=feature")
    assert "#3" in by_branch.text
    assert ">#2<" not in by_branch.text

    # A numeric ref means a PR; build 3 is PR #5.
    by_pr = client.get("/repos/github/acme/widget?ref=%235")
    assert "#3" in by_pr.text
    assert ">#2<" not in by_pr.text


def test_all_digit_branch_is_filterable(client: WebHarness) -> None:
    """A branch named "2024" must not be hijacked as PR number 2024;
    PRs use the explicit #N or pr/N forms."""
    ctx = client.ctx

    async def setup() -> None:
        project_id = await ctx.pool.fetchval(
            "SELECT id FROM projects WHERE name = 'widget'"
        )
        await ctx.pool.execute(
            "INSERT INTO builds (project_id, number, commit_sha, branch, status)"
            " VALUES ($1, 80, 'y1', '2024', 'succeeded')",
            project_id,
        )

    client.loop.run_until_complete(setup())
    try:
        by_branch = client.get("/repos/github/acme/widget?ref=2024")
        assert ">#80<" in by_branch.text
        assert ">#3<" not in by_branch.text
        for ref in ("%235", "pr/5"):
            by_pr = client.get(f"/repos/github/acme/widget?ref={ref}")
            assert ">#3<" in by_pr.text
            assert ">#80<" not in by_pr.text
    finally:
        client.loop.run_until_complete(
            ctx.pool.execute("DELETE FROM builds WHERE number = 80")
        )


def test_build_page(client: WebHarness) -> None:
    response = client.get("/repos/github/acme/widget/builds/2")
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


def test_attribute_rows_fragment_paginates(client: WebHarness) -> None:
    base = "/repos/github/acme/widget/builds/2/attrs"
    rows = client.get(f"{base}?group=succeeded")
    assert rows.status_code == 200
    assert "x86_64-linux.ok" in rows.text
    assert "aarch64-linux.other" in rows.text

    first = client.get(f"{base}?group=succeeded&limit=1").text
    assert "aarch64-linux.other" in first  # alphabetical within the group
    assert "x86_64-linux.ok" not in first
    assert "page=2" in first  # load-more sentinel

    second = client.get(f"{base}?group=succeeded&limit=1&page=2").text
    assert "x86_64-linux.ok" in second
    assert "page=3" not in second

    assert client.get(f"{base}?group=nonsense").status_code == 404


def test_build_page_shows_eval_warning_groups(client: WebHarness) -> None:
    async def seed() -> None:
        ctx = client.ctx
        await ctx.pool.execute(
            "UPDATE builds SET eval_warnings = $1::jsonb WHERE number = 2",
            json.dumps(
                [
                    {"level": "error", "message": "download failed", "count": 24},
                    {"level": "warning", "message": "foo is deprecated", "count": 1},
                ]
            ),
        )

    client.loop.run_until_complete(seed())
    text = client.get("/repos/github/acme/widget/builds/2").text
    assert "foo is deprecated" in text
    assert "download failed" in text
    assert "\u00d724" in text
    assert "2 kinds" in text
    assert "25 occurrences" in text
    # Decoded, not the raw jsonb string.
    assert "[&#34;" not in text


def test_build_page_renders_ansi_in_error(client: WebHarness) -> None:
    """Eval failures carry nix's colored output; raw escape codes must
    not reach the HTML (the API already strips them)."""

    async def seed() -> None:
        await client.ctx.pool.execute(
            "UPDATE builds SET error = $1 WHERE number = 2",
            "evaluation failed: \x1b[31mred error\x1b[0m",
        )

    client.loop.run_until_complete(seed())
    text = client.get("/repos/github/acme/widget/builds/2").text
    assert "\x1b" not in text
    assert "red error" in text


def test_running_build_shows_progress(client: WebHarness) -> None:
    running = client.get("/repos/github/acme/widget/builds/3").text
    assert 'class="progress-track"' in running
    assert 'class="spinner"' in running
    assert "seg ok" in running  # settled attrs render as a green segment
    assert "3 of 3" in running  # seeded attrs are all settled
    finished = client.get("/repos/github/acme/widget/builds/2").text
    assert 'class="progress-track"' not in finished


def test_build_page_live_markers(client: WebHarness) -> None:
    running = client.get("/repos/github/acme/widget/builds/3")
    # htmx SSE wiring: event stream scoped to the build, main region
    # refetched and morphed on events.
    assert 'sse-connect="/events?build=' in running.text
    assert 'hx-trigger="sse:message' in running.text
    # Finished pages re-check on connect too (rebuild-failed).
    finished = client.get("/repos/github/acme/widget/builds/2")
    assert "htmx:sseOpen" in finished.text
    # PR link on the PR build.
    assert "/pull/5" in running.text


def test_attribute_history(client: WebHarness) -> None:
    response = client.get("/repos/github/acme/widget/attrs/x86_64-linux.bad")
    assert response.status_code == 200
    # Appears in all three builds.
    for number in (1, 2, 3):
        assert f"/builds/{number}" in response.text


def test_project_filter_escapes_like_wildcards(client: WebHarness) -> None:
    # `%` and `_` must match literally, not as ILIKE wildcards.
    assert "acme/widget" in client.get("/?q=widg").text
    assert "acme/widget" not in client.get("/?q=%25").text
    assert "acme/widget" not in client.get("/?q=widge_").text


def test_page_out_of_range_does_not_error(client: WebHarness) -> None:
    # page <= 0 must not turn into a negative SQL OFFSET (500).
    assert client.get("/repos/github/acme/widget?page=0").status_code == 200
    assert client.get("/repos/github/acme/widget?page=-5").status_code == 200
    api = client.get("/api/repos/github/acme/widget/builds?page=0")
    assert api.status_code == 200
    assert api.json()["items"]


def test_activity_page(client: WebHarness) -> None:
    response = client.get("/builds")
    assert response.status_code == 200
    # Queue section shows the building build with its position.
    assert "queue-pos" in response.text
    assert "#3" in response.text
    # Activity feed lists finished builds too.
    assert ">#1<" in response.text


def test_activity_no_sentinel_on_exact_page(
    client: WebHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exactly PAGE_SIZE builds must not render a load-more sentinel:
    the follow-up fetch would come back empty."""
    monkeypatch.setattr("nixbot.web.app.PAGE_SIZE", 3)  # 3 seeded builds
    response = client.get("/builds")
    assert response.status_code == 200
    assert response.text.count("data-id=") == 3
    assert "scroll-sentinel" not in response.text


def test_activity_rows_fragment_pagination(client: WebHarness) -> None:
    rows = client.get("/builds/rows?before=1")
    assert rows.status_code == 200
    assert "<tr" not in rows.text  # nothing older than build id 1

    # Walk the cursor like the frontend does: each fragment's last
    # data-id feeds the next request until the response is empty.
    cursor = 999999
    seen: list[int] = []
    while True:
        text = client.get(f"/builds/rows?before={cursor}").text
        ids = re.findall(r'data-id="(\d+)"', text)
        if not ids:
            break
        seen.extend(int(i) for i in ids)
        cursor = int(ids[-1])
    assert len(seen) == 3
    assert seen == sorted(seen, reverse=True)  # newest first, no overlap


def test_project_rows_fragment_filters(client: WebHarness) -> None:
    all_rows = client.get("/repos/github/acme/widget/rows?before=999999")
    assert all_rows.text.count("data-id=") == 3

    failed = client.get("/repos/github/acme/widget/rows?before=999999&status=failed")
    assert failed.text.count("data-id=") == 1
    assert ">#2<" in failed.text

    branch = client.get("/repos/github/acme/widget/rows?before=999999&ref=feature")
    assert branch.text.count("data-id=") == 1
    assert ">#3<" in branch.text


def test_metrics_are_gauges(client: WebHarness) -> None:
    # Table-derived series shrink, so they must not be counters — and
    # per Prometheus conventions non-monotonic series must not be
    # named _total either.
    text = client.get("/metrics").text
    assert "# TYPE nixbot_builds gauge" in text
    assert "# TYPE nixbot_attributes gauge" in text
    assert "_total" not in text
    assert "counter" not in text


def test_metrics_are_cached(client: WebHarness) -> None:
    """/metrics is unauthenticated: a scrape (or a curl loop) must not
    run full-table aggregations every time."""
    ctx = client.ctx
    first = client.get("/metrics").text

    async def insert() -> int:
        project_id = await ctx.pool.fetchval(
            "SELECT id FROM projects WHERE name = 'widget'"
        )
        return await ctx.pool.fetchval(
            "INSERT INTO builds (project_id, number, commit_sha, branch, status)"
            " VALUES ($1, 70, 'm1', 'main', 'pending') RETURNING id",
            project_id,
        )

    build_id = client.loop.run_until_complete(insert())
    try:
        assert client.get("/metrics").text == first
    finally:
        client.loop.run_until_complete(
            ctx.pool.execute("DELETE FROM builds WHERE id = $1", build_id)
        )


def test_static_urls_carry_content_version(client: WebHarness) -> None:
    page = client.get("/").text
    m = re.search(r"/static/style\.css\?v=([0-9a-f]{12})", page)
    assert m
    response = client.get(f"/static/style.css?v={m.group(1)}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_legacy_project_urls_redirect(client: WebHarness) -> None:
    r = client.get("/projects/acme/widget/builds/2?x=1")
    assert r.status_code == 307
    assert r.headers["location"] == "/repos/github/acme/widget/builds/2?x=1"
    r = client.get("/api/projects/acme/widget")
    assert r.headers["location"] == "/api/repos/github/acme/widget"
    assert client.get("/projects/acme/nope").status_code == 404


class _StubVisibility:
    def __init__(self, visible: list[int]) -> None:
        self.visible = visible
        self.seen_users: list[object] = []

    async def visible_repo_ids(
        self,
        user: object,
        token: object = None,  # noqa: ARG002
    ) -> list[int]:
        self.seen_users.append(user)
        return self.visible


def test_legacy_redirect_hides_invisible_projects(client: WebHarness) -> None:
    """Redirect-vs-404 must not let anonymous users probe private
    repo existence."""
    ctx = client.ctx

    async def setup() -> int:
        return await insert_project(
            ctx.pool, "secret", forge_repo_id="web-secret", private=True
        )

    secret_id = client.loop.run_until_complete(setup())
    ctx.visibility = cast("VisibilityService", _StubVisibility([]))
    try:
        assert client.get("/projects/acme/secret").status_code == 404
        ctx.visibility = cast("VisibilityService", _StubVisibility([secret_id]))
        assert client.get("/projects/acme/secret").status_code == 307
    finally:
        ctx.visibility = None
        client.loop.run_until_complete(
            ctx.pool.execute("DELETE FROM projects WHERE id = $1", secret_id)
        )


def test_api_token_grants_private_repo_visibility(client: WebHarness) -> None:
    """Bearer-authenticated requests must go through the same
    visibility path as session users: the token carries the user's
    groups, which the private_repo_viewers rules match."""
    ctx = client.ctx
    authz = AuthzConfig(admins=[], private_repo_viewers={"*": ["github:group:dev"]})

    async def setup() -> tuple[int, str]:
        secret_id = await insert_project(
            ctx.pool, "vault", forge_repo_id="web-vault", private=True
        )
        store = ApiTokenStore(ctx.pool)
        token = await store.create(
            User(provider="github", username="alice", groups=("dev",)), "t"
        )
        return secret_id, token

    secret_id, token = client.loop.run_until_complete(setup())
    ctx.token_store = ApiTokenStore(ctx.pool)
    ctx.visibility = VisibilityService(ctx.pool, authz)
    try:
        url = "/api/repos/github/acme/vault"
        assert client.get(url).status_code == 404  # anonymous
        ok = client.get(url, headers={"Authorization": f"Bearer {token}"})
        assert ok.status_code == 200
    finally:
        ctx.visibility = None
        ctx.token_store = None
        client.loop.run_until_complete(
            ctx.pool.execute("DELETE FROM projects WHERE id = $1", secret_id)
        )


def test_legacy_redirect_404s_on_ambiguous_owner_name(client: WebHarness) -> None:
    """The same owner/name on two forges must not resolve to an
    arbitrary one."""
    ctx = client.ctx

    async def setup() -> int:
        return await insert_project(
            ctx.pool, "widget", forge="gitea", forge_repo_id="web-gitea-1"
        )

    other_id = client.loop.run_until_complete(setup())
    try:
        assert client.get("/projects/acme/widget").status_code == 404
    finally:
        client.loop.run_until_complete(
            ctx.pool.execute("DELETE FROM projects WHERE id = $1", other_id)
        )


def test_health_and_static(client: WebHarness) -> None:
    assert client.get("/health").text == "ok"
    css = client.get("/static/style.css")
    assert "prefers-color-scheme: dark" in css.text


def test_404s(client: WebHarness) -> None:
    assert client.get("/repos/github/acme/nope").status_code == 404
    assert client.get("/repos/github/acme/widget/builds/999").status_code == 404


def test_timeago() -> None:
    now = datetime.now(tz=UTC)
    assert timeago(None) == "—"
    assert timeago(now - timedelta(days=2)) == "2d ago"
    assert timeago(now - timedelta(minutes=5)).endswith("m ago")


def test_forge_urls() -> None:
    """Each forge has its own web URL scheme; GitLab notably nests
    everything under /-/."""
    github = {"forge": "github", "url": "https://github.com/acme/widget.git"}
    gitea = {"forge": "gitea", "url": "https://gitea.example.com/acme/widget.git"}
    gitlab = {"forge": "gitlab", "url": "https://gitlab.example.com/acme/widget.git"}

    assert pr_url(github, 7) == "https://github.com/acme/widget/pull/7"
    assert pr_url(gitea, 7) == "https://gitea.example.com/acme/widget/pulls/7"
    assert (
        pr_url(gitlab, 7) == "https://gitlab.example.com/acme/widget/-/merge_requests/7"
    )

    assert branch_url(github, "main") == "https://github.com/acme/widget/tree/main"
    assert (
        branch_url(gitea, "main")
        == "https://gitea.example.com/acme/widget/src/branch/main"
    )
    assert (
        branch_url(gitlab, "main")
        == "https://gitlab.example.com/acme/widget/-/tree/main"
    )

    assert commit_url(github, "abc") == "https://github.com/acme/widget/commit/abc"
    assert (
        commit_url(gitea, "abc") == "https://gitea.example.com/acme/widget/commit/abc"
    )
    assert (
        commit_url(gitlab, "abc")
        == "https://gitlab.example.com/acme/widget/-/commit/abc"
    )

    # pull_based with an SSH clone URL: no valid href can be built,
    # the helpers return "" and templates fall back to plain text.
    ssh = {"forge": "pull_based", "url": "git@host.example.com:acme/widget.git"}
    assert commit_url(ssh, "abc") == ""
    assert branch_url(ssh, "main") == ""
    assert pr_url(ssh, 7) == ""
    # ... but http(s) pull_based URLs keep their links.
    http = {"forge": "pull_based", "url": "https://host.example.com/acme/widget.git"}
    assert commit_url(http, "abc") == "https://host.example.com/acme/widget/commit/abc"


def test_link_macros_plain_for_ssh_pull_based() -> None:
    """SSH clone URLs of pull_based projects must not leak into hrefs;
    the macros fall back to plain text."""
    env = make_env()
    project = {"forge": "pull_based", "url": "git@host.example.com:acme/machine.git"}
    out = env.from_string(
        '{% from "_macros.html" import commit_link, ref_link %}'
        '{{ commit_link(p, "c" * 40) }} {{ ref_link(p, b) }}'
    ).render(p=project, b={"pr_number": None, "branch": "main"})
    assert "git@" not in out
    assert "<a" not in out
    assert "cccccccccc" in out
    assert "main" in out


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
        "\x1b[0 qhello": "hello",  # CSI with intermediate byte (DECSCUSR)
        "\x1b[?25lhi\x1b[?25h": "hi",  # private mode set/reset
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
    # A two-character escape split across chunks must not leak its
    # final byte as text.
    stream = AnsiHtmlStream()
    assert stream.feed("a\x1b(") == "a"
    assert stream.feed("Bhello") == "hello"
    # A CSI sequence with an intermediate byte split across chunks.
    assert stream.feed("b\x1b[0 ") == "b"
    assert stream.feed("qworld") == "world"


def test_ansi_stream_flushes_oversized_unterminated_osc() -> None:
    """An unterminated OSC must not buffer the rest of the stream
    forever: past a sanity bound it is flushed as plain text."""
    stream = AnsiHtmlStream()
    # Small partial sequences stay buffered awaiting their terminator.
    assert stream.feed("\x1b]8;;https://e.x") == ""
    payload = "x" * 5000
    out = stream.feed(payload)
    assert payload in out
    assert "after" in stream.feed("after")


def seed_log(client: WebHarness, tmp_path: Path) -> None:
    async def run() -> None:
        ctx = client.ctx
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


def test_log_viewer_and_raw(client: WebHarness, tmp_path: Path) -> None:
    seed_log(client, tmp_path)
    viewer = client.get("/repos/github/acme/widget/builds/2/logs/x86_64-linux.bad")
    assert viewer.status_code == 200
    assert "ansi-red" in viewer.text
    assert 'id="L1"' in viewer.text

    # Prev/next navigation to the same attribute in neighboring builds.
    assert 'rel="prev"' in viewer.text
    assert "/builds/1/logs/x86_64-linux.bad" in viewer.text
    assert 'rel="next"' in viewer.text
    assert "/builds/3/logs/x86_64-linux.bad" in viewer.text

    raw = client.get("/repos/github/acme/widget/builds/2/logs/x86_64-linux.bad.txt")
    assert "build exploded" in raw.text

    missing = client.get("/repos/github/acme/widget/builds/2/logs/nope")
    assert missing.status_code == 404


def test_attr_named_dot_txt_not_shadowed_by_raw_route(
    client: WebHarness, tmp_path: Path
) -> None:
    """An attribute literally named "foo.txt" used to be unreachable:
    /logs/foo.txt matched the raw route and served attr "foo"'s log."""
    ctx = client.ctx
    ctx.state_dir = tmp_path

    async def setup() -> int:
        build_id = await ctx.pool.fetchval("SELECT id FROM builds WHERE number = 2")
        for attr, content in (
            ("foo", b"plain foo log\n"),
            ("foo.txt", b"dot txt log\n"),
        ):
            attr_id = await ctx.pool.fetchval(
                "INSERT INTO build_attributes (build_id, attr, system, status)"
                " VALUES ($1, $2, 'x86_64-linux', 'failed') RETURNING id",
                build_id,
                attr,
            )
            rel = f"logs/2/{attr}.zst"
            f = tmp_path / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(zstandard.ZstdCompressor().compress(content))
            await ctx.pool.execute(
                "INSERT INTO logs (attribute_id, path, size_bytes) VALUES ($1, $2, $3)",
                attr_id,
                rel,
                f.stat().st_size,
            )
        return build_id

    build_id = client.loop.run_until_complete(setup())
    try:
        base = "/repos/github/acme/widget/builds/2/logs"
        viewer = client.get(f"{base}/foo.txt")
        assert viewer.headers["content-type"].startswith("text/html")
        assert "dot txt log" in viewer.text
        assert client.get(f"{base}/raw/foo").text == "plain foo log\n"
        assert client.get(f"{base}/raw/foo.txt").text == "dot txt log\n"
        # The legacy suffix route still serves raw when nothing collides.
        assert client.get(f"{base}/foo.txt.txt").text == "dot txt log\n"
        assert client.get(f"{base}/raw/nope").status_code == 404
    finally:
        client.loop.run_until_complete(
            ctx.pool.execute(
                "DELETE FROM build_attributes WHERE build_id = $1"
                " AND attr IN ('foo', 'foo.txt')",
                build_id,
            )
        )


def test_attr_names_with_special_characters(client: WebHarness, tmp_path: Path) -> None:
    """Attrs containing / % ? # must round-trip through generated
    links and route matching."""
    ctx = client.ctx
    ctx.state_dir = tmp_path
    attr = "pkgs/o k?#100%"
    encoded = "pkgs/o%20k%3F%23100%25"

    async def setup() -> int:
        build_id = await ctx.pool.fetchval("SELECT id FROM builds WHERE number = 2")
        attr_id = await ctx.pool.fetchval(
            "INSERT INTO build_attributes (build_id, attr, system, status)"
            " VALUES ($1, $2, 'x86_64-linux', 'failed') RETURNING id",
            build_id,
            attr,
        )
        rel = "logs/2/weird.zst"
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(zstandard.ZstdCompressor().compress(b"weird log\n"))
        await ctx.pool.execute(
            "INSERT INTO logs (attribute_id, path, size_bytes) VALUES ($1, $2, $3)",
            attr_id,
            rel,
            f.stat().st_size,
        )
        return build_id

    build_id = client.loop.run_until_complete(setup())
    try:
        base = "/repos/github/acme/widget/builds/2"
        page = client.get(f"{base}/attrs?group=failed")
        assert f"logs/{encoded}" in page.text
        viewer = client.get(f"{base}/logs/{encoded}")
        assert viewer.status_code == 200
        assert "weird log" in viewer.text
        assert client.get(f"{base}/logs/raw/{encoded}").text == "weird log\n"
        history = client.get(f"/api/repos/github/acme/widget/attrs/{encoded}")
        assert history.status_code == 200
        assert history.json()[0]["attr"] == attr
    finally:
        client.loop.run_until_complete(
            ctx.pool.execute(
                "DELETE FROM build_attributes WHERE build_id = $1 AND attr = $2",
                build_id,
                attr,
            )
        )


def test_log_viewer_waits_for_queued_attribute(client: WebHarness) -> None:
    # The build page links queued attributes before any log exists.
    async def make_pending() -> None:
        ctx = client.ctx
        await ctx.pool.execute(
            """
            UPDATE build_attributes a SET status = 'pending'
            FROM builds b
            WHERE b.id = a.build_id AND b.number = 3
              AND a.attr = 'x86_64-linux.ok'
            """
        )

    async def restore() -> None:
        ctx = client.ctx
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
        response = client.get("/repos/github/acme/widget/builds/3/logs/x86_64-linux.ok")
        assert response.status_code == 200
        assert "waiting for the build to start" in response.text
    finally:
        client.loop.run_until_complete(restore())


def test_log_sse_stream_finished(client: WebHarness, tmp_path: Path) -> None:
    seed_log(client, tmp_path)
    response = client.get(
        "/repos/github/acme/widget/builds/2/logs/x86_64-linux.bad/stream"
    )
    assert response.status_code == 200
    assert "build exploded" in response.text
    assert "event: done" in response.text


# --- JSON API ----------------------------------------------------------


def test_api_projects_and_builds(client: WebHarness) -> None:
    projects = client.get("/api/repos").json()
    assert any(p["name"] == "widget" for p in projects)

    repo = client.get("/api/repos/github/acme/widget").json()
    assert repo["name"] == "widget"
    assert repo["default_branch"] == "main"

    builds = client.get("/api/repos/github/acme/widget/builds").json()
    assert builds["page"] == 1
    assert len(builds["items"]) == 3

    filtered = client.get("/api/repos/github/acme/widget/builds?status=failed").json()
    assert [b["number"] for b in filtered["items"]] == [2]

    detail = client.get("/api/repos/github/acme/widget/builds/2").json()
    assert detail["build"]["status"] == "failed"
    statuses = {a["attr"]: a["status"] for a in detail["attributes"]}
    assert statuses["x86_64-linux.bad"] == "failed"

    history = client.get("/api/repos/github/acme/widget/attrs/x86_64-linux.bad").json()
    assert len(history) == 3

    queue = client.get("/api/queue").json()
    assert [b["number"] for b in queue] == [3]


def test_build_rows_link_refs(client: WebHarness) -> None:
    # Branch builds link to the branch, PR builds to the pull request.
    page = client.get("/repos/github/acme/widget").text
    assert "https://github.com/acme/widget/tree/main" in page
    assert "https://github.com/acme/widget/pull/5" in page
    # Cross-project rows on the activity feed carry their own forge URL.
    activity = client.get("/builds").text
    assert "https://github.com/acme/widget/pull/5" in activity


def test_attributes_sort_building_before_pending(client: WebHarness) -> None:
    seeded = ("a-pending", "b-building", "c-failed")

    async def run() -> list[str]:
        ctx = client.ctx
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


def test_queue_sorts_active_before_pending(client: WebHarness) -> None:
    async def run() -> list[int]:
        ctx = client.ctx
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

    assert client.get("/api/repos/github/acme/nope").status_code == 404


def test_queue_position_is_global_and_skips_running(client: WebHarness) -> None:
    """Positions number the global FIFO of queued builds: they must
    not be renumbered per viewer over the visibility-filtered subset,
    and running builds have no queue position."""
    ctx = client.ctx

    async def run() -> tuple[list, list]:
        widget_id = await ctx.pool.fetchval(
            "SELECT id FROM projects WHERE name = 'widget'"
        )
        gadget_id = await insert_project(ctx.pool, "gadget", forge_repo_id="web-g1")
        await ctx.pool.execute(
            """
            INSERT INTO builds (project_id, number, commit_sha, branch, status)
            VALUES ($1, 95, 'g1', 'main', 'pending'),
                   ($2, 1, 'g2', 'main', 'pending'),
                   ($1, 96, 'g3', 'main', 'pending')
            """,
            widget_id,
            gadget_id,
        )
        try:
            full = await ctx.queries.queue()
            filtered = await ctx.queries.queue([widget_id])
        finally:
            await ctx.pool.execute(
                "DELETE FROM builds WHERE project_id = $1", gadget_id
            )
            await ctx.pool.execute("DELETE FROM projects WHERE id = $1", gadget_id)
            await ctx.pool.execute("DELETE FROM builds WHERE number IN (95, 96)")
        return full, filtered

    full, filtered = client.loop.run_until_complete(run())
    # Running builds (build 3 is building) carry no queue position.
    assert [b["queue_position"] for b in full if b["status"] != "pending"] == [None]
    assert [b["queue_position"] for b in full if b["status"] == "pending"] == [1, 2, 3]
    # The filtered view keeps the GLOBAL positions: gadget's build
    # still occupies slot 2 even though this viewer cannot see it.
    assert [b["queue_position"] for b in filtered if b["status"] == "pending"] == [1, 3]


def test_openapi_docs(client: WebHarness) -> None:
    spec = client.get("/api/openapi.json").json()
    assert "/api/repos" in spec["paths"]
    # HTML pages and other non-API routes stay out of the spec.
    assert all(path.startswith("/api/") for path in spec["paths"])
    assert client.get("/docs").status_code == 200
    # Responses are typed: every operation documents a response schema.
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            schema = op["responses"]["200"]["content"]["application/json"]["schema"]
            assert schema not in ({}, None), f"{method} {path}"
    assert "Build" in spec["components"]["schemas"]


# --- live logs ---------------------------------------------------------


def test_live_log_history_before_completion(client: WebHarness, tmp_path: Path) -> None:
    """Running attributes have no logs DB row yet: viewer/raw/stream
    must fall back to the registered LogWriter's on-disk file."""
    ctx = client.ctx
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


def test_log_sse_stream_caps_history_backlog(
    client: WebHarness, tmp_path: Path
) -> None:
    """Subscribing late to a huge log must not render the entire
    history through the ANSI pipeline; only a bounded tail replays."""
    ctx = client.ctx
    ctx.state_dir = tmp_path
    registry = client.app.state.log_registry

    async def run() -> str:
        build_id = await ctx.pool.fetchval("SELECT id FROM builds WHERE number = 3")
        writer = LogWriter(path=tmp_path / "logs" / "live" / "big.zst")
        await writer.write("".join(f"line{i:05d}\n" for i in range(3000)).encode())
        registry.register(build_id, "x86_64-linux.ok", writer)
        try:
            stream_task = asyncio.ensure_future(
                client.http.get(
                    "/repos/github/acme/widget/builds/3/logs/x86_64-linux.ok/stream"
                )
            )
            await asyncio.sleep(0.1)
            await writer.close()
            return (await stream_task).text
        finally:
            registry.unregister(build_id, "x86_64-linux.ok")

    stream = client.loop.run_until_complete(run())
    assert "line02999" in stream
    assert "line00000" not in stream
    assert "truncated" in stream


def test_log_sse_stream_neutralizes_carriage_returns(
    client: WebHarness, tmp_path: Path
) -> None:
    """EventSource treats a bare CR as a line terminator: log content
    after \r would escape the data: framing and could forge SSE
    fields like a premature "event: done"."""
    ctx = client.ctx
    ctx.state_dir = tmp_path
    registry = client.app.state.log_registry

    async def run() -> str:
        build_id = await ctx.pool.fetchval("SELECT id FROM builds WHERE number = 3")
        writer = LogWriter(path=tmp_path / "logs" / "live" / "cr.zst")
        await writer.write(b"progress 1%\rprogress 2%\r\nevent: done\rtail\n")
        registry.register(build_id, "x86_64-linux.ok", writer)
        try:
            stream_task = asyncio.ensure_future(
                client.http.get(
                    "/repos/github/acme/widget/builds/3/logs/x86_64-linux.ok/stream"
                )
            )
            await asyncio.sleep(0.1)
            await writer.close()
            return (await stream_task).text
        finally:
            registry.unregister(build_id, "x86_64-linux.ok")

    stream = client.loop.run_until_complete(run())
    body_lines = stream.split("\n")
    # The only event: line is the terminal done marker.
    assert [li for li in body_lines if li.startswith("event:")] == ["event: done"]
    # No raw CR may reach the wire inside a data: payload.
    assert "\r" not in stream
    assert "progress 2%" in stream


def test_api_builds_commit_filter(client: WebHarness) -> None:
    hits = client.get("/api/repos/github/acme/widget/builds?commit=sha-2").json()
    assert [b["number"] for b in hits["items"]] == [2]
    misses = client.get("/api/repos/github/acme/widget/builds?commit=ffff").json()
    assert misses["items"] == []


def test_log_tail_param(client: WebHarness, tmp_path: Path) -> None:
    seed_log(client, tmp_path)
    full = client.get("/repos/github/acme/widget/builds/2/logs/x86_64-linux.bad.txt")
    assert "build exploded" in full.text
    tail = client.get(
        "/repos/github/acme/widget/builds/2/logs/x86_64-linux.bad.txt?tail=1"
    )
    # ANSI escapes are stripped from the plain-text view.
    assert tail.text == "build exploded\n"


def test_api_build_failures(client: WebHarness, tmp_path: Path) -> None:
    seed_log(client, tmp_path)
    summary = client.get("/api/repos/github/acme/widget/builds/2/failures").json()
    assert summary["status"] == "failed"
    failed = {f["attr"]: f for f in summary["failures"]}
    assert "x86_64-linux.bad" in failed
    assert "build exploded" in failed["x86_64-linux.bad"]["log_tail"]
    # Succeeded attributes are not in the failure summary.
    assert "x86_64-linux.ok" not in failed


def test_api_build_failures_rejects_non_positive_tail(
    client: WebHarness, tmp_path: Path
) -> None:
    """splitlines()[-0:] is the WHOLE list: tail=0 must not dump the
    full log of every failed attribute."""
    seed_log(client, tmp_path)
    log_file = tmp_path / "logs" / "2" / "x86_64-linux.bad.zst"
    lines = "".join(f"line {i}\n" for i in range(60))
    log_file.write_bytes(zstandard.ZstdCompressor().compress(lines.encode()))

    base = "/api/repos/github/acme/widget/builds/2/failures"
    assert client.get(f"{base}?tail=0").status_code == 422
    one = client.get(f"{base}?tail=1").json()
    tails = [f["log_tail"] for f in one["failures"] if f["attr"] == "x86_64-linux.bad"]
    assert tails == ["line 59"]


def test_llms_txt(client: WebHarness) -> None:
    response = client.get("/llms.txt")
    assert response.status_code == 200
    assert "/api/openapi.json" in response.text
    assert "failures" in response.text


def test_event_broker_pushes_status_changes(
    client: WebHarness, postgres_dsn: str
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


def test_events_stream_refreshes_visibility(
    client: WebHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long-lived /events stream must re-resolve the viewer's
    visibility set instead of keeping the subscribe-time snapshot:
    revoking access must stop private events mid-stream (and granting
    it must start them, which is what this test observes)."""
    monkeypatch.setattr(events_module, "KEEPALIVE_SECONDS", 0.05)
    monkeypatch.setattr(events_module, "VISIBILITY_REFRESH_SECONDS", 0.0)
    ctx = client.ctx
    broker = client.app.state.event_broker
    stub = _StubVisibility([])
    ctx.visibility = cast("VisibilityService", stub)

    async def run() -> list[str]:
        widget_id = await ctx.pool.fetchval(
            "SELECT id FROM projects WHERE name = 'widget'"
        )
        cookie = client.signer.session_for(
            User(provider="github", username="alice"), "sse-session"
        )
        request = Request(
            {
                "type": "http",
                "headers": [(b"cookie", f"{SESSION_COOKIE}={cookie}".encode())],
                "query_string": b"",
            }
        )
        sub = broker.subscribe(visible=set())
        gen = events_module.event_stream(ctx, request, broker, sub)
        assert (await anext(gen)).startswith("retry:")
        # Invisible at notify time: filtered out before the queue.
        broker._on_notify(  # noqa: SLF001
            None, None, None, json.dumps({"project_id": widget_id, "mark": "one"})
        )
        stub.visible = [widget_id]
        # The next iteration re-resolves visibility before keepalive.
        async with asyncio.timeout(5):
            assert "keepalive" in await anext(gen)
        broker._on_notify(  # noqa: SLF001
            None, None, None, json.dumps({"project_id": widget_id, "mark": "two"})
        )
        received = []
        async with asyncio.timeout(5):
            async for line in gen:
                if line.startswith("data:"):
                    received.append(line)
                    break
        # Refreshes re-authenticate instead of reusing the request's
        # cached user: a logout mid-stream must be picked up.
        assert stub.seen_users[-1].qualified == "github:alice"  # type: ignore[attr-defined]
        assert ctx.revoked_sessions is not None
        await ctx.revoked_sessions.revoke("sse-session", 60)
        async with asyncio.timeout(5):
            assert "keepalive" in await anext(gen)
        assert stub.seen_users[-1] is None
        await gen.aclose()
        return received

    try:
        received = client.loop.run_until_complete(run())
    finally:
        ctx.visibility = None
    assert len(received) == 1
    assert "two" in received[0]
    assert "one" not in received[0]


def test_attribute_search(client: WebHarness) -> None:
    # Results keep the group structure, opened and filtered.
    found = client.get("/repos/github/acme/widget/builds/2/attrs?q=bad")
    assert "x86_64-linux.bad" in found.text
    assert "x86_64-linux.ok" not in found.text
    assert "1 failed" in found.text
    assert "succeeded" not in found.text
    by_name = client.get("/repos/github/acme/widget/builds/2/attrs?q=other")
    assert "1 succeeded" in by_name.text
    assert "open>" in by_name.text
    # Clearing the query restores the unfiltered view.
    empty = client.get("/repos/github/acme/widget/builds/2/attrs?q=")
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


def test_build_page_shows_effects(client: WebHarness) -> None:
    async def seed_effects() -> None:
        ctx = client.ctx
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
    text = client.get("/repos/github/acme/widget/builds/2").text
    assert "Effects" in text
    assert "deploy" in text
    assert "ssh: connection refused" in text
    # Both finished-with-log and running effects link to the viewer.
    assert "logs/effect:deploy" in text
    assert "logs/effect:notify" in text
    # Builds without effects don't render the section.
    assert "Effects" not in client.get("/repos/github/acme/widget/builds/1").text


def test_effect_log_raw_text(client: WebHarness, tmp_path: Path) -> None:
    async def seed_effect_log() -> None:
        ctx = client.ctx
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
    response = client.get("/repos/github/acme/widget/builds/2/logs/effect:deploy2.txt")
    assert response.status_code == 200
    assert "activating system" in response.text


def test_effect_changes_notify_build_events(client: WebHarness) -> None:
    """The page's SSE refresh rides on build_events; without a trigger
    on build_effects the Effects section never updates live."""

    async def run() -> list[str]:
        ctx = client.ctx
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


def test_effects_state_api(client: WebHarness, tmp_path: Path) -> None:
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


def test_logout_revokes_session_cookie(postgres_dsn: str) -> None:
    """A captured copy of the session cookie must stop authenticating
    once the user logs out, even though the cookie itself is still
    validly signed and unexpired."""

    def configure(app: FastAPI, pool: asyncpg.Pool) -> None:
        ctx = app.state.web_context
        app.include_router(
            create_auth_router(
                {},
                ctx.signer,
                "http://test",
                ForgeTokenStore(pool),
                revoked_sessions=ctx.revoked_sessions,
            )
        )

        @app.get("/whoami")
        async def whoami(request: Request) -> JSONResponse:
            user = await ctx.request_user(request)
            return JSONResponse({"user": user.qualified if user else None})

    with web_harness(postgres_dsn, configure=configure) as harness:
        cookie = harness.signer.session_for(
            User(provider="github", username="alice"), "sid-logout-test"
        )
        headers = cookie_header({SESSION_COOKIE: cookie})

        before = harness.run(harness.http.get("/whoami", headers=headers))
        assert before.json() == {"user": "github:alice"}

        logout = harness.run(
            harness.http.post("/logout", headers=headers | {"Origin": "http://test"})
        )
        assert logout.status_code == 303

        after = harness.run(harness.http.get("/whoami", headers=headers))
        assert after.json() == {"user": None}


def test_gitlab_nested_group_routes(client: WebHarness) -> None:
    """GitLab nested-group projects have "/" in the owner; the web and
    API routes must still resolve them."""

    async def setup() -> None:
        pool = client.ctx.pool
        project_id = await insert_project(
            pool,
            "proj",
            forge="gitlab",
            forge_repo_id="nested-1",
            owner="group/sub",
            url="https://gitlab.example.com/group/sub/proj.git",
        )
        await insert_build(pool, project_id, number=1, status="succeeded")

    client.run(setup())
    page = client.get("/repos/gitlab/group/sub/proj")
    assert page.status_code == 200
    api = client.get("/api/repos/gitlab/group/sub/proj")
    assert api.status_code == 200
    assert api.json()["owner"] == "group/sub"
    build_page = client.get("/repos/gitlab/group/sub/proj/builds/1")
    assert build_page.status_code == 200
    build_api = client.get("/api/repos/gitlab/group/sub/proj/builds/1")
    assert build_api.status_code == 200
