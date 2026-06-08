"""Attribute build executor.

Runs `nix build` per derivation with:

- a global concurrency cap, dequeued fairly round-robin across builds
  (FIFO within one build) so huge matrices cannot head-of-line block
  other projects,
- per-attribute timeout (default 3h) plus nix's --max-silent-time
  (default 20min),
- one automatic retry on transient infrastructure errors, suppressed
  once cancellation is requested,
- process-group kill on cancel/timeout,
- frame-chunked zstd log capture: one zstd frame per flush so live
  tailing only decompresses new frames; a single reader fans out to all
  subscribers; compression runs off the event loop; logs are capped
  (default 64 MB) keeping head and tail.

The eval gc-roots directory is owned by the orchestrator and held for
the duration of the build, so derivations cannot be garbage-collected
while queued here.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import re
import signal
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import zstandard

from .ansi import strip_ansi
from .gcroots import safe_attr_filename
from .scheduler import BuildOutcome

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from .models import NixEvalJobSuccess

logger = logging.getLogger(__name__)

# StreamReader buffer limit: asyncio's 64 KiB default makes readline()
# raise on longer lines, which nix build logs routinely contain.
STREAM_LIMIT = 16 * 1024 * 1024

# Cap per-subscriber backlog so a stalled SSE client cannot buffer the
# whole build output in memory; the oldest chunks are dropped.
SUBSCRIBER_QUEUE_MAXSIZE = 256
RECENT_BUFFER_SIZE = 4096

# Batch log output into zstd frames of at least this size; one frame
# per output line would make "compressed" logs larger than plaintext.
FRAME_FLUSH_THRESHOLD = 64 * 1024


async def iter_lines(
    stream: asyncio.StreamReader, max_line: int = STREAM_LIMIT
) -> AsyncIterator[bytes]:
    """Line-split a stream via read() chunks.

    readline() raises LimitOverrunError on lines over the StreamReader
    limit, killing the pump while nix blocks on the full pipe. Reading
    chunks never raises; lines beyond max_line are flushed in pieces so
    one pathological line cannot buffer unboundedly.
    """
    buffer = bytearray()
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            break
        buffer += chunk
        while (newline := buffer.find(b"\n")) != -1:
            yield bytes(buffer[: newline + 1])
            del buffer[: newline + 1]
        if len(buffer) > max_line:
            yield bytes(buffer)
            buffer.clear()
    if buffer:
        yield bytes(buffer)


TRANSIENT_ERROR_MARKERS = (
    "unexpected end-of-file",
    "error: unable to download",
    "Connection reset by peer",
    "Connection refused",
    "Connection timed out",
    "Temporary failure in name resolution",
    "SSL connection",
    "writing to file: Broken pipe",
    "substituter",
)


class FairScheduler:
    """Global slot pool with round-robin dequeue across keys (builds)
    and FIFO order within a key."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            msg = f"capacity must be >= 1, got {capacity}"
            raise ValueError(msg)
        self.capacity = capacity
        self._active = 0
        # key -> FIFO of waiter futures; OrderedDict gives stable rotation.
        self._waiters: OrderedDict[object, deque[asyncio.Future[None]]] = OrderedDict()
        self._rotation: deque[object] = deque()

    async def acquire(self, key: object) -> None:
        if self._active < self.capacity and not self._waiters:
            self._active += 1
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        if key not in self._waiters:
            self._waiters[key] = deque()
            self._rotation.append(key)
        self._waiters[key].append(future)
        try:
            await future
        except asyncio.CancelledError:
            queue = self._waiters.get(key)
            if queue is not None and future in queue:
                queue.remove(future)
                if not queue:
                    del self._waiters[key]
                    self._rotation.remove(key)
            if future.cancelled() or not future.done():
                raise
            # Granted concurrently with cancellation: give the slot back.
            self.release()
            raise

    def release(self) -> None:
        self._active -= 1
        self._dispatch()

    def _dispatch(self) -> None:
        while self._active < self.capacity and self._rotation:
            key = self._rotation[0]
            queue = self._waiters.get(key)
            if not queue:
                self._rotation.popleft()
                self._waiters.pop(key, None)
                continue
            future = queue.popleft()
            # Rotate: next grant goes to the next build.
            self._rotation.rotate(-1)
            if not queue:
                del self._waiters[key]
                self._rotation.remove(key)
            if not future.done():
                self._active += 1
                future.set_result(None)


@dataclass
class LogWriter:
    """Frame-chunked zstd log writer with subscriber fan-out and a size
    cap keeping head and tail."""

    path: Path
    size_limit: int = 64 * 1024 * 1024
    bytes_seen: int = 0
    truncated: bool = False
    closed: bool = False
    _head_budget: int = field(init=False)
    _tail: deque[bytes] = field(default_factory=deque)
    _tail_size: int = 0
    _recent: deque[bytes] = field(default_factory=deque)
    _recent_size: int = 0
    _dropped: int = 0
    _subscribers: list[asyncio.Queue[bytes | None]] = field(default_factory=list)
    _frame_buffer: bytearray = field(default_factory=bytearray)
    _compressor: zstandard.ZstdCompressor = field(
        default_factory=zstandard.ZstdCompressor
    )

    def __post_init__(self) -> None:
        self._head_budget = self.size_limit // 2
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"")

    def subscribe(self) -> asyncio.Queue[bytes | None]:
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=SUBSCRIBER_QUEUE_MAXSIZE
        )
        self._subscribers.append(queue)
        return queue

    @staticmethod
    def _offer(queue: asyncio.Queue[bytes | None], item: bytes | None) -> None:
        """Enqueue without blocking; drop the oldest chunk when the
        subscriber has stalled (no backpressure toward the writer)."""
        while True:
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            else:
                return

    def unsubscribe(self, queue: asyncio.Queue[bytes | None]) -> None:
        with contextlib.suppress(ValueError):
            self._subscribers.remove(queue)

    async def snapshot(self) -> bytes:
        """Full log content so far: flushed frames plus buffered tail."""
        await self._flush_frame()
        history = await asyncio.to_thread(read_log, self.path)
        return history + b"".join(self._tail)

    async def subscribe_with_history(self) -> tuple[bytes, asyncio.Queue[bytes | None]]:
        """Snapshot of the log plus a live subscription: no chunk is
        lost or duplicated between the two."""
        history = await self.snapshot()
        queue = self.subscribe()
        if self.closed:
            # Closed but not yet unregistered: terminate immediately
            # instead of leaving the subscriber waiting forever.
            self._offer(queue, None)
        return history, queue

    async def write(self, data: bytes) -> None:
        if not data:
            return
        self.bytes_seen += len(data)
        self._recent.append(data)
        self._recent_size += len(data)
        while self._recent_size > RECENT_BUFFER_SIZE and len(self._recent) > 1:
            self._recent_size -= len(self._recent.popleft())
        for queue in self._subscribers:
            self._offer(queue, data)
        if self._head_budget > 0:
            chunk = data[: self._head_budget]
            self._head_budget -= len(chunk)
            await self._append_frame(chunk)
            data = data[len(chunk) :]
        if data:
            # Past the head budget: keep only the tail in memory.
            self._tail.append(data)
            self._tail_size += len(data)
            tail_limit = self.size_limit - self.size_limit // 2
            while self._tail_size > tail_limit and self._tail:
                dropped = self._tail.popleft()
                self._tail_size -= len(dropped)
                self._dropped += len(dropped)
                # Only an actual drop truncates: anything still in the
                # tail buffer reaches the disk in full.
                self.truncated = True

    async def _append_frame(self, data: bytes) -> None:
        # Batch into frames so tiny writes (one per output line) don't
        # blow up storage with per-frame overhead; compression runs off
        # the event loop.
        self._frame_buffer += data
        if len(self._frame_buffer) >= FRAME_FLUSH_THRESHOLD:
            await self._flush_frame()

    async def _flush_frame(self) -> None:
        if not self._frame_buffer:
            return
        chunk = bytes(self._frame_buffer)
        self._frame_buffer.clear()
        frame = await asyncio.to_thread(self._compressor.compress, chunk)
        with self.path.open("ab") as f:
            f.write(frame)

    def tail_lines(self, max_lines: int = 30, max_chars: int = 4000) -> str:
        """The last lines of output, for failure excerpts."""
        text = b"".join(self._recent).decode("utf-8", errors="replace")
        lines = text.splitlines()[-max_lines:]
        return "\n".join(lines)[-max_chars:]

    async def close(self) -> None:
        self.closed = True
        if self._dropped:
            marker = (
                f"\n\n... log truncated ({self._dropped} bytes omitted) ...\n\n"
            ).encode()
            await self._append_frame(marker)
        while self._tail:
            await self._append_frame(self._tail.popleft())
        self._tail_size = 0
        await self._flush_frame()
        for queue in self._subscribers:
            self._offer(queue, None)
        self._subscribers.clear()


@dataclass
class BuildSettings:
    log_dir: Path
    timeout: int = 60 * 60 * 3
    max_silent_time: int = 60 * 20
    show_trace: bool = False
    log_size_limit: int = 64 * 1024 * 1024
    # Extra `nix build` arguments, e.g. --option overrides.
    extra_args: list[str] = field(default_factory=list)


def build_nix_command(
    job: NixEvalJobSuccess, settings: BuildSettings, out_link: Path
) -> list[str]:
    return [
        "nix",
        "build",
        # internal-json keeps the ANSI colors that the terminal loggers
        # strip from non-tty output and tags every build-log line with
        # its derivation; render_log_event turns that back into
        # attributed, colored text.
        "--log-format",
        "internal-json",
        *(["--show-trace"] if settings.show_trace else []),
        "--option",
        "keep-going",
        "true",
        "--max-silent-time",
        str(settings.max_silent_time),
        "--accept-flake-config",
        *settings.extra_args,
        "--out-link",
        str(out_link),
        f"{job.drv_path}^*",
    ]


_QUOTED_LINE = re.compile(r"[^\s>]*> +(.*\S)")
_EXCERPT_NOISE = re.compile(
    r"Output paths:|/nix/store/\S+$|Last \d+ log lines:"
    r"|For full logs, run:|nix log /nix/store/\S+$|building '/nix/store/"
)


def failure_excerpt(tail: str, max_lines: int = 8) -> str:
    """The interesting part of a failed build's output: the builder's
    log lines (name>-prefixed from internal-json, deduped against
    nix's bare-'>' prose re-quote) plus one Reason line; without any,
    the tail minus nix's boilerplate. Lines are matched ANSI-stripped
    but emitted raw, so colors survive."""
    quoted: dict[str, str] = {}
    other: dict[str, str] = {}
    reason = None
    for line in tail.splitlines():
        raw, plain = line.strip(), strip_ansi(line).strip()
        if m := _QUOTED_LINE.match(plain):
            quoted.setdefault(m.group(1), raw)
        elif plain.startswith("Reason:"):
            reason = raw
        elif plain and not _EXCERPT_NOISE.match(plain):
            other.setdefault(plain, raw)
    lines = list((quoted or other).values())[-max_lines:]
    if reason:
        lines.append(reason)
    return "\n".join(lines)


# nix activity/result types (nix/util/logging.hh).
ACT_BUILD = 105
RES_BUILD_LOG_LINE = 101


def _drv_display_name(drv_path: str) -> str:
    name = drv_path.rsplit("/", 1)[-1].removesuffix(".drv")
    _, _, name = name.partition("-")  # drop the store hash
    return name or drv_path


def render_log_event(line: bytes, activities: dict[int, str]) -> bytes | None:
    """One line of `nix build --log-format internal-json` to log text.

    Build-log lines get a `name> ` prefix from their build activity;
    nix's own messages pass through with their ANSI colors. Returns
    None for events with no log output (progress, stops, ...).
    """
    if not line.startswith(b"@nix "):
        return line  # not an event: pass through (e.g. daemon chatter)
    try:
        event = json.loads(line[len(b"@nix ") :])
    except ValueError:
        return line
    action = event.get("action")
    if action == "start" and event.get("type") == ACT_BUILD:
        fields = event.get("fields") or []
        if fields:
            activities[event["id"]] = _drv_display_name(str(fields[0]))
        text = event.get("text", "")
        return f"{text}\n".encode() if text else None
    if action == "result" and event.get("type") == RES_BUILD_LOG_LINE:
        fields = event.get("fields") or [""]
        name = activities.get(event.get("id"), "")
        prefix = f"{name}> " if name else ""
        return f"{prefix}{fields[0]}\n".encode()
    if action == "msg":
        msg = event.get("msg", "")
        return f"{msg}\n".encode() if msg else None
    return None


def is_transient_error(output_tail: str) -> bool:
    return any(marker in output_tail for marker in TRANSIENT_ERROR_MARKERS)


class NixBuildExecutor:
    """Builds attributes through the fair global queue."""

    def __init__(self, queue: FairScheduler, settings: BuildSettings) -> None:
        self.queue = queue
        self.settings = settings

    async def build_attribute(
        self,
        build_key: object,
        job: NixEvalJobSuccess,
        log_writer: LogWriter,
        cwd: Path,
        cancel_event: asyncio.Event | None = None,
    ) -> BuildOutcome:
        """Run `nix build` for one attribute, with one automatic retry
        on transient errors (suppressed when cancellation is requested)."""
        cancel_event = cancel_event or asyncio.Event()
        if cancel_event.is_set():
            return BuildOutcome.cancelled
        # A superseded build must not sit "building" until an unrelated
        # build frees a slot: race the acquisition against the cancel.
        acquire_task = asyncio.create_task(self.queue.acquire(build_key))
        cancel_task = asyncio.create_task(cancel_event.wait())
        try:
            await asyncio.wait(
                {acquire_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            # Cancelled in the same turn the slot was granted: give it
            # back, nobody will run the build.
            if acquire_task.done() and not acquire_task.cancelled():
                self.queue.release()
            raise
        finally:
            # Also runs on cancellation of this coroutine: a pending
            # waiter (and its eventual slot) must not leak.
            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_task
            if not acquire_task.done():
                acquire_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await acquire_task
        if acquire_task.cancelled():
            return BuildOutcome.cancelled
        await acquire_task
        try:
            outcome, transient = await self._run_once(
                job, log_writer, cwd, cancel_event
            )
            if outcome == BuildOutcome.failure and transient:
                if cancel_event.is_set():
                    # Cancel arrived while the retry was pending: the
                    # retry must not fire and the attribute ends
                    # cancelled (never cached as a failure).
                    return BuildOutcome.cancelled
                await log_writer.write(
                    b"\n\nbuildbot-nix: transient error detected, retrying once\n\n"
                )
                outcome, _ = await self._run_once(job, log_writer, cwd, cancel_event)
            return outcome
        finally:
            self.queue.release()

    async def _run_once(
        self,
        job: NixEvalJobSuccess,
        log_writer: LogWriter,
        cwd: Path,
        cancel_event: asyncio.Event,
    ) -> tuple[BuildOutcome, bool]:
        if cancel_event.is_set():
            return BuildOutcome.cancelled, False
        out_link = cwd / f"result-{safe_attr_filename(job.attr)}"
        cmd = build_nix_command(job, self.settings, out_link)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,  # own process group for clean kill
            limit=STREAM_LIMIT,
        )
        assert proc.stdout is not None  # noqa: S101

        output_tail: deque[bytes] = deque(maxlen=100)

        async def pump() -> None:
            assert proc.stdout is not None  # noqa: S101
            activities: dict[int, str] = {}
            async for raw in iter_lines(proc.stdout):
                line = render_log_event(raw, activities)
                if line is None:
                    continue
                output_tail.append(line)
                await log_writer.write(line)

        def kill() -> None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)

        pump_task = asyncio.create_task(pump())
        cancel_task = asyncio.create_task(cancel_event.wait())
        try:
            wait_task = asyncio.ensure_future(proc.wait())
            done, _ = await asyncio.wait(
                {wait_task, cancel_task},
                timeout=self.settings.timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            timed_out = not done
            if wait_task not in done:  # cancel requested or timeout
                kill()
                await proc.wait()
            await pump_task
            if timed_out:
                await log_writer.write(
                    f"\n\nbuildbot-nix: build timed out after "
                    f"{self.settings.timeout}s\n\n".encode()
                )
                # Timeouts are genuine failures (cached when enabled).
                return BuildOutcome.failure, False
            if proc.returncode == 0:
                # Even when the kill raced a clean exit: the build
                # finished, the result is real.
                return BuildOutcome.success, False
            if cancel_event.is_set():
                return BuildOutcome.cancelled, False
            tail_text = b"".join(output_tail).decode(errors="replace")
            return BuildOutcome.failure, is_transient_error(tail_text)
        finally:
            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_task


def read_log(path: Path) -> bytes:
    """Decompress a frame-chunked zstd log file (one frame per flush)."""
    data = path.read_bytes()
    dctx = zstandard.ZstdDecompressor()
    # One streaming pass; re-slicing unused_data per frame is quadratic.
    with dctx.stream_reader(io.BytesIO(data), read_across_frames=True) as reader:
        return reader.read()
