"""Log viewer, SSE streaming, and raw downloads.

Logs live as frame-chunked zstd files on disk. Finished logs are
decompressed for the viewer/raw text endpoints. Running attributes stream through the LogRegistry: the
executor's LogWriter fans out to any number of SSE subscribers.
"""

from __future__ import annotations

import asyncio
import html
import re
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    StreamingResponse,
)

from ..executor import read_log  # noqa: TID252
from ..scheduler import TERMINAL_FAILURES  # noqa: TID252

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


_ANSI_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")
_ANSI_OTHER_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-ln-z]")
# An escape sequence cut off at the end of a chunk.
_ANSI_PARTIAL_RE = re.compile(r"\x1b(\[[0-9;?]*)?\Z")


def strip_ansi(text: str) -> str:
    """Plain-text consumers (curl, scripts, agents) want clean text;
    the HTML viewer renders the colored original."""
    return _ANSI_OTHER_RE.sub("", _ANSI_SGR_RE.sub("", text))


_COLOR_CLASSES = {
    **{
        str(30 + i): f"ansi-{name}"
        for i, name in enumerate(
            ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"]
        )
    },
    **{
        str(90 + i): f"ansi-bright-{name}"
        for i, name in enumerate(
            ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"]
        )
    },
    "1": "ansi-bold",
}


def _ansi_convert(text: str, classes: list[str]) -> tuple[str, list[str]]:
    """Convert SGR color/bold codes to spans; strip other sequences.
    `classes` is the style carried in from the previous chunk; the
    style left open at the end is returned for the next one."""
    text = _ANSI_OTHER_RE.sub("", text)
    segments: list[tuple[str, list[str]]] = []
    pos = 0
    for match in _ANSI_SGR_RE.finditer(text):
        segments.append((text[pos : match.start()], classes))
        pos = match.end()
        classes = [
            _COLOR_CLASSES[code]
            for code in (match.group(1) or "0").split(";")
            if code in _COLOR_CLASSES
        ]
    segments.append((text[pos:], classes))
    out = []
    for segment, style in segments:
        if not segment:
            continue
        if style:
            out.append(f'<span class="{" ".join(style)}">{html.escape(segment)}</span>')
        else:
            out.append(html.escape(segment))
    return "".join(out), classes


def ansi_to_html(text: str) -> str:
    return _ansi_convert(text, [])[0]


class AnsiHtmlStream:
    """Chunked variant for live streams: SGR state and escape
    sequences split across chunk boundaries survive."""

    def __init__(self) -> None:
        self._classes: list[str] = []
        self._tail = ""

    def feed(self, text: str) -> str:
        text = self._tail + text
        self._tail = ""
        partial = _ANSI_PARTIAL_RE.search(text)
        if partial:
            self._tail = text[partial.start() :]
            text = text[: partial.start()]
        rendered, self._classes = _ansi_convert(text, self._classes)
        return rendered


def render_log_lines(text: str) -> str:
    """Lines with id anchors for permalinks."""
    lines = []
    for i, line in enumerate(text.splitlines(), 1):
        lines.append(
            f'<span class="logline" id="L{i}">'
            f'<a class="lineno" href="#L{i}">{i}</a>{ansi_to_html(line)}</span>'
        )
    # .logline is display:block; a joining "\n" inside <pre> would
    # render as an extra blank line.
    return "".join(lines)


async def _log_path(
    ctx: WebContext, registry: LogRegistry, build: dict, attr: str
) -> Path | None:
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
                "error": a["error"],
                "log_tail": "\n".join(text.splitlines()[-tail:]) if text else None,
            }
        )
    return {
        "status": build["status"],
        "error": build["error"],
        "eval_warnings": build["eval_warnings"],
        "failures": failures,
    }


_FAILURE_STATUSES = {s.value for s in TERMINAL_FAILURES} | {"cancelled"}


def create_log_router(ctx: WebContext, registry: LogRegistry) -> APIRouter:  # noqa: C901
    router = APIRouter()

    async def _resolve(
        request: Request, owner: str, name: str, number: int, attr: str
    ) -> tuple[dict, dict, Path | None]:
        project = await ctx.project_or_404(owner, name, request)
        build = await ctx.queries.build_by_number(project["id"], number)
        if build is None:
            raise HTTPException(status_code=404)
        return project, build, await _log_path(ctx, registry, build, attr)

    @router.get("/projects/{owner}/{name}/builds/{number}/logs/{attr}.txt")
    async def log_raw_text(  # noqa: PLR0913
        request: Request,
        owner: str,
        name: str,
        number: int,
        attr: str,
        tail: int | None = None,
    ) -> PlainTextResponse:
        """Full log as plain text; ?tail=N returns only the last N lines."""
        _, build, path = await _resolve(request, owner, name, number, attr)
        text = await _log_text(registry, build, attr, path)
        if text is None:
            raise HTTPException(status_code=404)
        if tail is not None and tail > 0:
            text = "\n".join(text.splitlines()[-tail:]) + "\n"
        return PlainTextResponse(strip_ansi(text))

    @router.get("/api/projects/{owner}/{name}/builds/{number}/failures")
    async def build_failures(
        request: Request, owner: str, name: str, number: int, tail: int = 50
    ) -> dict:
        """One-shot failure summary: failed attributes with log tails.

        Saves API consumers (CI scripts, LLM agents) a request per
        attribute when answering "why did this build fail?".
        """
        project = await ctx.project_or_404(owner, name, request)
        build = await ctx.queries.build_by_number(project["id"], number)
        if build is None:
            raise HTTPException(status_code=404)
        return await _failure_summary(ctx, registry, build, tail)

    @router.get("/projects/{owner}/{name}/builds/{number}/logs/{attr}/stream")
    async def log_stream(
        request: Request, owner: str, name: str, number: int, attr: str
    ) -> StreamingResponse:
        """SSE: history from disk, then live chunks until completion."""
        _, build, path = await _resolve(request, owner, name, number, attr)
        writer = registry.get(build["id"], attr)
        return StreamingResponse(
            _stream_events(writer, path), media_type="text/event-stream"
        )

    @router.get(
        "/projects/{owner}/{name}/builds/{number}/logs/{attr}",
        response_class=HTMLResponse,
    )
    async def log_viewer(
        request: Request, owner: str, name: str, number: int, attr: str
    ) -> HTMLResponse:
        project, build, path = await _resolve(request, owner, name, number, attr)
        attr_status = await ctx.pool.fetchval(
            "SELECT status FROM build_attributes WHERE build_id = $1 AND attr = $2",
            build["id"],
            attr,
        )
        # Live pages render no snapshot: the stream replays full
        # history on connect, the client would throw it away.
        live = registry.get(build["id"], attr) is not None
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
        prev_number, next_number = await ctx.queries.attribute_neighbors(
            project["id"], attr, number
        )
        return ctx.render(
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
    data = "\n".join(f"data: {line}" for line in text.split("\n"))
    return f"{data}\n\n"
