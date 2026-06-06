"""Log viewer, SSE streaming, and raw downloads.

Logs live as frame-chunked zstd files on disk. Finished logs are
decompressed for the viewer/raw text endpoints (or served as .zst
as-is). Running attributes stream through the LogRegistry: the
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
    Response,
    StreamingResponse,
)

from ..executor import read_log  # noqa: TID252

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


def ansi_to_html(text: str) -> str:
    """Convert SGR color/bold codes to spans; strip other sequences."""
    text = _ANSI_OTHER_RE.sub("", text)
    out: list[str] = []
    open_span = False
    pos = 0
    for match in _ANSI_SGR_RE.finditer(text):
        out.append(html.escape(text[pos : match.start()]))
        pos = match.end()
        if open_span:
            out.append("</span>")
            open_span = False
        classes = [
            _COLOR_CLASSES[code]
            for code in (match.group(1) or "0").split(";")
            if code in _COLOR_CLASSES
        ]
        if classes:
            out.append(f'<span class="{" ".join(classes)}">')
            open_span = True
    out.append(html.escape(text[pos:]))
    if open_span:
        out.append("</span>")
    return "".join(out)


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


def create_log_router(ctx: WebContext, registry: LogRegistry) -> APIRouter:  # noqa: C901
    router = APIRouter()

    async def _resolve(
        request: Request, owner: str, name: str, number: int, attr: str
    ) -> tuple[dict, dict, Path | None]:
        project = await ctx.project_or_404(owner, name, request)
        build = await ctx.queries.build_by_number(project["id"], number)
        if build is None:
            raise HTTPException(status_code=404)
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
        return project, build, path

    @router.get("/projects/{owner}/{name}/builds/{number}/logs/{attr}.txt")
    async def log_raw_text(
        request: Request, owner: str, name: str, number: int, attr: str
    ) -> PlainTextResponse:
        _, build, path = await _resolve(request, owner, name, number, attr)
        writer = registry.get(build["id"], attr)
        if writer is not None:
            # Running attribute: part of the log is still buffered in
            # the writer, not yet on disk.
            data = await writer.snapshot()
        elif path is None or not path.exists():
            raise HTTPException(status_code=404)
        else:
            # Decompression off the event loop: logs are up to 64 MB.
            data = await asyncio.to_thread(read_log, path)
        return PlainTextResponse(data.decode(errors="replace"))

    @router.get("/projects/{owner}/{name}/builds/{number}/logs/{attr}.zst")
    async def log_raw_zst(
        request: Request, owner: str, name: str, number: int, attr: str
    ) -> Response:
        _, _, path = await _resolve(request, owner, name, number, attr)
        if path is None or not path.exists():
            raise HTTPException(status_code=404)
        data = await asyncio.to_thread(path.read_bytes)
        return Response(data, media_type="application/zstd")

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
        writer = registry.get(build["id"], attr)
        live = writer is not None
        content = ""
        if writer is not None:
            data = await writer.snapshot()
            content = await asyncio.to_thread(
                render_log_lines, data.decode(errors="replace")
            )
        elif path is not None and path.exists():
            data = await asyncio.to_thread(read_log, path)
            content = await asyncio.to_thread(
                render_log_lines, data.decode(errors="replace")
            )
        else:
            raise HTTPException(status_code=404)
        return ctx.render(
            "log.html",
            request=request,
            project=project,
            build=build,
            attr=attr,
            content=content,
            live=live,
        )

    return router


async def _stream_events(
    writer: LogWriter | None, path: Path | None
) -> AsyncGenerator[str, None]:
    """History first, then live chunks (if a writer is running)."""
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
        yield _sse(history)
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
            yield _sse(chunk.decode(errors="replace"))
    finally:
        if writer is not None:
            writer.unsubscribe(queue)


def _sse(text: str) -> str:
    data = "\n".join(f"data: {line}" for line in text.split("\n"))
    return f"{data}\n\n"
