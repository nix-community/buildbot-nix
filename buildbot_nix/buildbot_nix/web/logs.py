"""Log viewer, SSE streaming, and raw downloads.

Logs live as frame-chunked zstd files on disk. Finished logs are
decompressed for the viewer/raw text endpoints. Running attributes stream through the LogRegistry: the
executor's LogWriter fans out to any number of SSE subscribers.
"""

from __future__ import annotations

import asyncio
import html
from typing import TYPE_CHECKING, NamedTuple

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    StreamingResponse,
)

from ..ansi import (  # noqa: TID252
    ANSI_PARTIAL_RE,
    ANSI_TOKEN_RE,
    CTRL_RE,
    strip_ansi,
)
from ..executor import read_log  # noqa: TID252
from ..scheduler import TERMINAL_FAILURES  # noqa: TID252
from .api_routes import FailureSummary, clean_row

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from ..executor import LogWriter  # noqa: TID252
    from .app import WebContext


class LogRegistry:
    """Live LogWriters of currently running attributes."""

    def __init__(self) -> None:
        self._writers: dict[tuple[int, str], LogWriter] = {}

    def register(self, build_id: int, attr: str, writer: LogWriter) -> None:
        self._writers[(build_id, attr)] = writer

    def unregister(self, build_id: int, attr: str) -> None:
        self._writers.pop((build_id, attr), None)

    def get(self, build_id: int, attr: str) -> LogWriter | None:
        return self._writers.get((build_id, attr))


_COLOR_CLASSES = {}
for _i, _name in enumerate(
    ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"]
):
    _COLOR_CLASSES[str(30 + _i)] = f"ansi-{_name}"
    _COLOR_CLASSES[str(90 + _i)] = f"ansi-bright-{_name}"


class _Style(NamedTuple):
    fg: str | None = None  # color class
    bold: bool = False
    href: str | None = None  # OSC 8 link target


_RESET = _Style()


def _apply_sgr(params: str, style: _Style) -> _Style:
    """SGR is stateful: codes modify the current style, they don't
    replace it. Unknown codes are ignored, which also covers colon
    syntax (38:5:185 stays one unknown code); semicolon-separated
    extended colors consume their arguments so e.g. 38;5;31 is not
    read as red."""
    fg, bold = style.fg, style.bold
    codes = (params or "0").split(";")
    i = 0
    while i < len(codes):
        code = codes[i] or "0"
        if code == "0":
            fg, bold = None, False
        elif code == "1":
            bold = True
        elif code == "22":
            bold = False
        elif code == "39":
            fg = None
        elif code in _COLOR_CLASSES:
            fg = _COLOR_CLASSES[code]
        elif code in ("38", "48"):
            # Extended color (unsupported): 38;5;n or 38;2;r;g;b.
            is_rgb = i + 1 < len(codes) and codes[i + 1] == "2"
            i += 4 if is_rgb else 2
        i += 1
    return style._replace(fg=fg, bold=bold)


def _apply_osc(payload: str, style: _Style) -> _Style:
    """OSC 8 opens/closes a hyperlink; every other OSC (window title
    etc.) is dropped without touching the style."""
    if not payload.startswith("8;"):
        return style
    uri = payload.split(";", 2)[-1]
    # Logs are untrusted: http(s) targets only.
    if not uri.startswith(("http://", "https://")):
        uri = ""
    return style._replace(href=uri or None)


def _render_segment(segment: str, style: _Style) -> str:
    if not segment:
        return ""
    # Stripping C0 before tokenizing would eat OSC's BEL terminator.
    out = html.escape(CTRL_RE.sub("", segment))
    classes = " ".join(c for c in (style.fg, "ansi-bold" if style.bold else None) if c)
    if classes:
        out = f'<span class="{classes}">{out}</span>'
    if style.href:
        out = (
            f'<a href="{html.escape(style.href, quote=True)}" rel="nofollow">{out}</a>'
        )
    return out


def _ansi_convert(text: str, style: _Style) -> tuple[str, _Style]:
    """Convert SGR colors to spans and OSC 8 to links; strip every
    other sequence. `style` carries in from the previous chunk/line;
    the style left open at the end is returned for the next one."""
    out: list[str] = []
    pos = 0
    for match in ANSI_TOKEN_RE.finditer(text):
        out.append(_render_segment(text[pos : match.start()], style))
        pos = match.end()
        if match.group("sgr") is not None:
            style = _apply_sgr(match.group("sgr"), style)
        elif match.group("osc") is not None:
            style = _apply_osc(match.group("osc"), style)
    out.append(_render_segment(text[pos:], style))
    return "".join(out), style


def ansi_to_html(text: str) -> str:
    return _ansi_convert(text, _RESET)[0]


# Real escape sequences are tiny; a held-back "partial" larger than
# this is an unterminated OSC that would buffer the live stream
# forever.
_TAIL_MAX = 4096


class AnsiHtmlStream:
    """Chunked variant for live streams: SGR state and escape
    sequences split across chunk boundaries survive."""

    def __init__(self) -> None:
        self._style = _RESET
        self._tail = ""

    def feed(self, text: str) -> str:
        text = self._tail + text
        self._tail = ""
        partial = ANSI_PARTIAL_RE.search(text)
        if partial:
            self._tail = text[partial.start() :]
            text = text[: partial.start()]
        rendered, self._style = _ansi_convert(text, self._style)
        if len(self._tail) > _TAIL_MAX:
            # Give up on the broken sequence: flush it as plain text
            # (minus the ESC bytes) so the stream keeps moving.
            rendered += _render_segment(self._tail.replace("\x1b", ""), self._style)
            self._tail = ""
        return rendered


def render_log_lines(text: str) -> str:
    """Lines with id anchors for permalinks. Style carries across
    lines, matching what the live stream rendered."""
    lines = []
    style = _RESET
    for i, line in enumerate(text.splitlines(), 1):
        rendered, style = _ansi_convert(line, style)
        lines.append(
            f'<span class="logline" id="L{i}">'
            f'<a class="lineno" href="#L{i}">{i}</a>{rendered}</span>'
        )
    # .logline is display:block; a joining "\n" inside <pre> would
    # render as an extra blank line.
    return "".join(lines)


async def _log_path(
    ctx: WebContext, registry: LogRegistry, build: dict, attr: str
) -> Path | None:
    if attr.startswith("effect:"):
        # Effects store log metadata inline (no attribute row).
        row = await ctx.pool.fetchrow(
            "SELECT log_path AS path FROM build_effects "
            "WHERE build_id = $1 AND name = $2 AND log_path IS NOT NULL",
            build["id"],
            attr.removeprefix("effect:"),
        )
    else:
        row = await ctx.pool.fetchrow(
            """
            SELECT l.path FROM logs l
            JOIN build_attributes a ON a.id = l.attribute_id
            WHERE a.build_id = $1 AND a.attr = $2
            """,
            build["id"],
            attr,
        )
    path = ctx.state_dir / row["path"] if row else None
    if path is None:
        # No logs row until completion; running attributes use the
        # live writer's on-disk file.
        writer = registry.get(build["id"], attr)
        if writer is not None:
            path = writer.path
    return path


async def _log_text(
    registry: LogRegistry, build: dict, attr: str, path: Path | None
) -> str | None:
    writer = registry.get(build["id"], attr)
    if writer is not None:
        # Running attribute: part of the log is still buffered in
        # the writer, not yet on disk.
        data = await writer.snapshot()
    elif path is None or not await asyncio.to_thread(path.exists):
        return None
    else:
        # Decompression off the event loop: logs are up to 64 MB.
        data = await asyncio.to_thread(read_log, path)
    return data.decode(errors="replace")


async def _failure_summary(
    ctx: WebContext, registry: LogRegistry, build: dict, tail: int
) -> dict:
    failures = []
    for a in await ctx.queries.attributes(build["id"]):
        if a["status"] not in _FAILURE_STATUSES:
            continue
        path = await _log_path(ctx, registry, build, a["attr"])
        text = await _log_text(registry, build, a["attr"], path)
        if text is not None:
            text = strip_ansi(text)
        failures.append(
            {
                "attr": a["attr"],
                "status": a["status"],
                "error": strip_ansi(a["error"]) if a["error"] else None,
                "log_tail": "\n".join(text.splitlines()[-tail:]) if text else None,
            }
        )
    return {
        "status": build["status"],
        "error": build["error"],
        # clean_row decodes the JSONB column to a list.
        "eval_warnings": clean_row(build)["eval_warnings"],
        "failures": failures,
    }


_FAILURE_STATUSES = {s.value for s in TERMINAL_FAILURES} | {"cancelled"}


class _LogRoutes:
    def __init__(self, ctx: WebContext, registry: LogRegistry) -> None:
        self.ctx = ctx
        self.registry = registry

    async def _build_or_404(
        self, request: Request, forge: str, owner: str, name: str, number: int
    ) -> tuple[dict, dict]:
        project = await self.ctx.repo_or_404(forge, owner, name, request)
        build = await self.ctx.queries.build_by_number(project["id"], number)
        if build is None:
            raise HTTPException(status_code=404)
        return project, build

    async def _resolve(  # noqa: PLR0913
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        number: int,
        attr: str,
    ) -> tuple[dict, dict, Path | None]:
        project, build = await self._build_or_404(request, forge, owner, name, number)
        return project, build, await _log_path(self.ctx, self.registry, build, attr)

    async def log_raw_text(  # noqa: PLR0913
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        number: int,
        attr: str,
        tail: int | None = Query(None, ge=1),
    ) -> PlainTextResponse:
        """Full log as plain text; ?tail=N returns only the last N lines."""
        _, build, path = await self._resolve(request, forge, owner, name, number, attr)
        text = await _log_text(self.registry, build, attr, path)
        if text is None:
            raise HTTPException(status_code=404)
        if tail:
            text = "\n".join(text.splitlines()[-tail:]) + "\n"
        return PlainTextResponse(strip_ansi(text))

    async def scheduled_run_log(
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        run_id: int,
    ) -> PlainTextResponse:
        """Log of one scheduled-effect run as plain text."""
        project = await self.ctx.repo_or_404(forge, owner, name, request)
        row = await self.ctx.pool.fetchrow(
            "SELECT id FROM scheduled_effect_runs WHERE id = $1 AND project_id = $2",
            run_id,
            project["id"],
        )
        path = self.ctx.state_dir / "logs" / "scheduled" / f"{run_id}.zst"
        if row is None or not await asyncio.to_thread(path.exists):
            raise HTTPException(status_code=404)
        data = await asyncio.to_thread(read_log, path)
        return PlainTextResponse(strip_ansi(data.decode(errors="replace")))

    async def build_failures(  # noqa: PLR0913
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        number: int,
        # ge=1: splitlines()[-0:] would be the whole list, dumping the
        # full log of every failed attribute.
        tail: int = Query(50, ge=1),
    ) -> dict:
        """One-shot failure summary: failed attributes with log tails.

        Saves API consumers (CI scripts, LLM agents) a request per
        attribute when answering "why did this build fail?".
        """
        _, build = await self._build_or_404(request, forge, owner, name, number)
        return await _failure_summary(self.ctx, self.registry, build, tail)

    async def log_stream(  # noqa: PLR0913
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        number: int,
        attr: str,
    ) -> StreamingResponse:
        """SSE: history from disk, then live chunks until completion."""
        _, build, path = await self._resolve(request, forge, owner, name, number, attr)
        writer = self.registry.get(build["id"], attr)
        return StreamingResponse(
            _stream_events(writer, path), media_type="text/event-stream"
        )

    async def log_viewer(  # noqa: PLR0913
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        number: int,
        attr: str,
    ) -> HTMLResponse:
        project, build, path = await self._resolve(
            request, forge, owner, name, number, attr
        )
        attr_status = await self.ctx.pool.fetchval(
            "SELECT status FROM build_attributes WHERE build_id = $1 AND attr = $2",
            build["id"],
            attr,
        )
        # Live pages render no snapshot: the stream replays full
        # history on connect, the client would throw it away.
        live = self.registry.get(build["id"], attr) is not None
        content = ""
        waiting = False
        if not live:
            if path is not None and path.exists():
                data = await asyncio.to_thread(read_log, path)
                content = await asyncio.to_thread(
                    render_log_lines, data.decode(errors="replace")
                )
            elif attr_status in ("pending", "building"):
                # The build page links queued attributes before any
                # log exists; show a waiting page instead of a 404.
                waiting = True
            else:
                raise HTTPException(status_code=404)
        prev_number, next_number = await self.ctx.queries.attribute_neighbors(
            project["id"], attr, number
        )
        return await self.ctx.render(
            "log.html",
            request=request,
            project=project,
            build=build,
            attr_status=attr_status,
            attr=attr,
            content=content,
            live=live,
            waiting=waiting,
            prev_number=prev_number,
            next_number=next_number,
            can_control=await self.ctx.can_control(request, build),
        )


_BASE = "/repos/{forge}/{owner}/{name}/builds/{number}"


def create_log_router(ctx: WebContext, registry: LogRegistry) -> APIRouter:
    router = APIRouter()
    routes = _LogRoutes(ctx, registry)
    router.get(f"{_BASE}/logs/{{attr}}.txt")(routes.log_raw_text)
    router.get(f"{_BASE}/logs/{{attr}}/stream")(routes.log_stream)
    router.get(f"{_BASE}/logs/{{attr}}", response_class=HTMLResponse)(routes.log_viewer)
    router.get("/repos/{forge}/{owner}/{name}/schedules/runs/{run_id}.txt")(
        routes.scheduled_run_log
    )
    return router


def create_log_api_router(ctx: WebContext, registry: LogRegistry) -> APIRouter:
    """The failure summary belongs to the documented /api surface,
    unlike the HTML/plain-text log routes."""
    router = APIRouter(tags=["api"])
    routes = _LogRoutes(ctx, registry)
    router.get(f"/api{_BASE}/failures", response_model=FailureSummary)(
        routes.build_failures
    )
    return router


async def _stream_events(
    writer: LogWriter | None, path: Path | None
) -> AsyncGenerator[str, None]:
    """History first, then live chunks (if a writer is running);
    everything rendered to HTML server-side so the client just
    appends."""
    ansi = AnsiHtmlStream()
    if writer is not None:
        history_bytes, queue = await writer.subscribe_with_history()
        history = history_bytes.decode(errors="replace")
    else:
        queue = None
        history = ""
        if path is not None and await asyncio.to_thread(path.exists):
            data = await asyncio.to_thread(read_log, path)
            history = data.decode(errors="replace")
    if history:
        yield _sse(ansi.feed(history))
    if queue is None:
        yield "event: done\ndata: \n\n"
        return
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=30)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            if chunk is None:
                yield "event: done\ndata: \n\n"
                return
            yield _sse(ansi.feed(chunk.decode(errors="replace")))
    finally:
        if writer is not None:
            writer.unsubscribe(queue)


def _sse(text: str) -> str:
    # EventSource accepts \r, \r\n and \n as line terminators: a bare
    # CR inside a data: payload would end the line early and let log
    # content forge SSE fields (e.g. a premature "event: done").
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    data = "\n".join(f"data: {line}" for line in lines)
    return f"{data}\n\n"
