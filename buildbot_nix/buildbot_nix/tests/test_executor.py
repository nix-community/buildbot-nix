"""Tests for the attribute build executor: fair queue, log capture,
build subprocess handling (with a fake `nix` on PATH)."""

# ruff: noqa: PLR2004, ARG001 (test literals; fixtures used for side effects)

from __future__ import annotations

import asyncio
import contextlib
import os
import stat
from typing import TYPE_CHECKING

import pytest

from buildbot_nix.executor import (
    SUBSCRIBER_QUEUE_MAXSIZE,
    BuildSettings,
    FairScheduler,
    LogWriter,
    NixBuildExecutor,
    build_nix_command,
    failure_excerpt,
    is_transient_error,
    iter_lines,
    read_log,
    render_log_event,
)
from buildbot_nix.scheduler import BuildOutcome

from .support import mk_job

if TYPE_CHECKING:
    from pathlib import Path


# --- FairScheduler ---------------------------------------------------------


def test_fair_round_robin_across_builds() -> None:
    async def run() -> list[str]:
        queue = FairScheduler(1)
        order: list[str] = []
        await queue.acquire("seed")  # occupy the only slot

        async def worker(name: str, key: str) -> None:
            await queue.acquire(key)
            order.append(name)
            queue.release()

        # Build A enqueues three attrs first, build B two: fairness must
        # interleave instead of draining A first; FIFO within a build.
        tasks = [
            asyncio.create_task(worker("a1", "A")),
            asyncio.create_task(worker("a2", "A")),
            asyncio.create_task(worker("a3", "A")),
            asyncio.create_task(worker("b1", "B")),
            asyncio.create_task(worker("b2", "B")),
        ]
        await asyncio.sleep(0)  # let all enqueue
        queue.release()  # free the seed slot
        await asyncio.gather(*tasks)
        return order

    order = asyncio.run(run())
    assert order == ["a1", "b1", "a2", "b2", "a3"]


def test_fair_scheduler_capacity() -> None:
    async def run() -> int:
        queue = FairScheduler(2)
        active = 0
        peak = 0

        async def worker() -> None:
            nonlocal active, peak
            await queue.acquire("k")
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            queue.release()

        await asyncio.gather(*[worker() for _ in range(6)])
        return peak

    assert asyncio.run(run()) == 2


def test_fair_scheduler_cancelled_waiter() -> None:
    async def run() -> None:
        queue = FairScheduler(1)
        await queue.acquire("A")
        waiter = asyncio.create_task(queue.acquire("B"))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        queue.release()
        # Slot must be available again.
        await asyncio.wait_for(queue.acquire("C"), timeout=1)

    asyncio.run(run())


# --- LogWriter --------------------------------------------------------------


def test_log_writer_roundtrip(tmp_path: Path) -> None:
    async def run() -> None:
        writer = LogWriter(path=tmp_path / "log.zst")
        await writer.write(b"hello ")
        await writer.write(b"world\n")
        await writer.close()

    asyncio.run(run())
    assert read_log(tmp_path / "log.zst") == b"hello world\n"


def test_log_writer_fan_out(tmp_path: Path) -> None:
    async def run() -> list[bytes]:
        writer = LogWriter(path=tmp_path / "log.zst")
        sub1 = writer.subscribe()
        sub2 = writer.subscribe()
        await writer.write(b"line1\n")
        await writer.close()
        chunks = []
        while (chunk := await sub1.get()) is not None:
            chunks.append(chunk)
        assert await sub2.get() == b"line1\n"
        assert await sub2.get() is None
        return chunks

    assert asyncio.run(run()) == [b"line1\n"]


def test_log_writer_subscribe_with_history(tmp_path: Path) -> None:
    async def run() -> None:
        writer = LogWriter(path=tmp_path / "log.zst")
        await writer.write(b"early\n")
        history, queue = await writer.subscribe_with_history()
        assert history == b"early\n"
        await writer.write(b"late\n")
        assert await queue.get() == b"late\n"
        await writer.close()
        assert await queue.get() is None
        # Subscribing after close must terminate, not hang.
        history, queue = await writer.subscribe_with_history()
        assert history == b"early\nlate\n"
        assert await queue.get() is None

    asyncio.run(run())


def test_log_writer_truncation_keeps_head_and_tail(tmp_path: Path) -> None:
    async def run() -> LogWriter:
        writer = LogWriter(path=tmp_path / "log.zst", size_limit=1000)
        await writer.write(b"H" * 600)  # head budget is 500
        for i in range(100):
            await writer.write(f"tail-{i:03d}\n".encode())
        await writer.close()
        return writer

    writer = asyncio.run(run())
    content = read_log(tmp_path / "log.zst")
    assert writer.truncated
    assert content.startswith(b"H" * 500)
    assert b"log truncated" in content
    assert b"tail-099" in content  # newest tail kept
    assert b"tail-000" not in content  # oldest tail dropped
    # Stored content respects the cap (plus the marker line).
    assert len(content) < 1200


# --- command assembly & transient detection ---------------------------------


def test_build_nix_command(tmp_path: Path) -> None:
    settings = BuildSettings(log_dir=tmp_path, max_silent_time=77, show_trace=True)
    cmd = build_nix_command(mk_job(), settings, tmp_path / "result-foo")
    assert cmd[:4] == ["nix", "build", "--log-format", "internal-json"]
    assert "--show-trace" in cmd
    assert cmd[cmd.index("--max-silent-time") + 1] == "77"
    assert cmd[-1] == "/nix/store/foo.drv^*"
    assert cmd[cmd.index("--out-link") + 1] == str(tmp_path / "result-foo")


def test_is_transient_error() -> None:
    assert is_transient_error("error: unable to download 'https://x': 500")
    assert is_transient_error("Connection reset by peer")
    assert not is_transient_error("error: builder failed with exit code 1")


# --- NixBuildExecutor with fake nix -----------------------------------------


@pytest.fixture
def fake_nix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake `nix` on PATH controlled by environment-ish marker files."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "nix"
    script.write_text(
        f"""#!/bin/sh
control={tmp_path}/control
echo "building $@"
case "$(cat "$control" 2>/dev/null)" in
  fail) echo "error: builder failed with exit code 1"; exit 1 ;;
  transient-once)
    echo transient > "$control"
    echo "error: unable to download 'https://cache': Connection reset by peer"
    exit 1 ;;
  transient) echo ok; exit 0 ;;
  hang) sleep 60 ;;
  longline) head -c 200000 /dev/zero | tr '\\0' 'A'; echo ;;
  racepid) echo $$ > {tmp_path}/pid; echo ok ;;
  *) echo ok; exit 0 ;;
esac
"""
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return tmp_path / "control"


def run_build(
    tmp_path: Path,
    settings: BuildSettings | None = None,
    cancel_event: asyncio.Event | None = None,
) -> tuple[BuildOutcome, bytes]:
    async def run() -> tuple[BuildOutcome, bytes]:
        executor = NixBuildExecutor(
            FairScheduler(2), settings or BuildSettings(log_dir=tmp_path)
        )
        writer = LogWriter(path=tmp_path / "log.zst")
        outcome = await executor.build_attribute(
            "build-1", mk_job(), writer, tmp_path, cancel_event
        )
        await writer.close()
        return outcome, read_log(tmp_path / "log.zst")

    return asyncio.run(run())


def test_executor_success(tmp_path: Path, fake_nix: Path) -> None:
    outcome, log = run_build(tmp_path)
    assert outcome == BuildOutcome.success
    assert b"building" in log


def test_executor_failure(tmp_path: Path, fake_nix: Path) -> None:
    fake_nix.write_text("fail")
    outcome, log = run_build(tmp_path)
    assert outcome == BuildOutcome.failure
    assert b"builder failed" in log


def test_executor_transient_retry_succeeds(tmp_path: Path, fake_nix: Path) -> None:
    fake_nix.write_text("transient-once")
    outcome, log = run_build(tmp_path)
    assert outcome == BuildOutcome.success
    assert b"retrying once" in log


def test_executor_timeout(tmp_path: Path, fake_nix: Path) -> None:
    fake_nix.write_text("hang")
    outcome, log = run_build(tmp_path, BuildSettings(log_dir=tmp_path, timeout=1))
    assert outcome == BuildOutcome.failure
    assert b"timed out" in log


def test_executor_cancel(tmp_path: Path, fake_nix: Path) -> None:
    fake_nix.write_text("hang")

    async def run() -> BuildOutcome:
        executor = NixBuildExecutor(FairScheduler(2), BuildSettings(log_dir=tmp_path))
        writer = LogWriter(path=tmp_path / "log.zst")
        cancel_event = asyncio.Event()
        task = asyncio.create_task(
            executor.build_attribute("b", mk_job(), writer, tmp_path, cancel_event)
        )
        await asyncio.sleep(0.2)
        cancel_event.set()
        outcome = await asyncio.wait_for(task, timeout=5)
        await writer.close()
        return outcome

    assert asyncio.run(run()) == BuildOutcome.cancelled


def test_executor_cancel_suppresses_retry(tmp_path: Path, fake_nix: Path) -> None:
    # Cancel before start: no retry, no build.
    fake_nix.write_text("transient-once")
    cancel_event = asyncio.Event()
    cancel_event.set()
    outcome, log = run_build(tmp_path, cancel_event=cancel_event)
    assert outcome == BuildOutcome.cancelled
    assert b"retrying" not in log


def test_log_writer_not_truncated_between_head_and_limit(tmp_path: Path) -> None:
    # Total output fits the cap (head budget + tail budget): nothing is
    # dropped, so the log must not be reported or marked as truncated.
    async def run() -> LogWriter:
        writer = LogWriter(path=tmp_path / "log.zst", size_limit=1000)
        await writer.write(b"H" * 600)  # head budget is 500
        await writer.close()
        return writer

    writer = asyncio.run(run())
    content = read_log(tmp_path / "log.zst")
    assert not writer.truncated
    assert b"log truncated" not in content
    assert content == b"H" * 600


def test_log_writer_subscriber_queue_bounded(tmp_path: Path) -> None:
    async def run() -> tuple[int, bytes | None]:
        writer = LogWriter(path=tmp_path / "log.zst")
        queue = writer.subscribe()
        # A stalled client: nothing consumes while the build streams.
        for i in range(SUBSCRIBER_QUEUE_MAXSIZE + 100):
            await writer.write(f"chunk-{i}\n".encode())
        size = queue.qsize()
        await writer.close()
        # Drain: the newest data and the close sentinel must be present.
        last = None
        while (chunk := await queue.get()) is not None:
            last = chunk
        return size, last

    size, last = asyncio.run(run())
    assert size <= SUBSCRIBER_QUEUE_MAXSIZE
    assert last == f"chunk-{SUBSCRIBER_QUEUE_MAXSIZE + 99}\n".encode()


def test_log_writer_batches_frames(tmp_path: Path) -> None:
    # Many tiny writes must not produce one zstd frame each (frame
    # overhead would make the "compressed" log larger than plaintext).
    async def run() -> None:
        writer = LogWriter(path=tmp_path / "log.zst")
        for i in range(10000):
            await writer.write(f"line {i}\n".encode())
        await writer.close()

    asyncio.run(run())
    raw = (tmp_path / "log.zst").stat().st_size
    plain = read_log(tmp_path / "log.zst")
    assert plain == b"".join(f"line {i}\n".encode() for i in range(10000))
    assert raw < len(plain)


def test_executor_handles_long_lines(tmp_path: Path, fake_nix: Path) -> None:
    # Lines over asyncio's 64 KiB default StreamReader limit must not
    # kill the log pump.
    fake_nix.write_text("longline")
    outcome, log = run_build(tmp_path)
    assert outcome == BuildOutcome.success
    assert b"A" * 200000 in log


def test_executor_sanitizes_out_link(tmp_path: Path, fake_nix: Path) -> None:
    # Repository-controlled attribute names must not traverse out of
    # the worktree via --out-link.
    async def run() -> bytes:
        executor = NixBuildExecutor(FairScheduler(2), BuildSettings(log_dir=tmp_path))
        writer = LogWriter(path=tmp_path / "log.zst")
        outcome = await executor.build_attribute(
            "b", mk_job('checks."../../evil"'), writer, tmp_path
        )
        assert outcome == BuildOutcome.success
        await writer.close()
        return read_log(tmp_path / "log.zst")

    log = asyncio.run(run())
    assert b"result-.." not in log
    assert b"--out-link " + str(tmp_path).encode() + b"/result-checks." in log


def test_executor_cancel_while_queued(tmp_path: Path, fake_nix: Path) -> None:
    # A queued build whose cancel event fires must not wait for a slot.
    fake_nix.write_text("hang")

    async def run() -> BuildOutcome:
        queue = FairScheduler(1)
        executor = NixBuildExecutor(queue, BuildSettings(log_dir=tmp_path))
        blocker_writer = LogWriter(path=tmp_path / "blocker.zst")
        blocker = asyncio.create_task(
            executor.build_attribute("a", mk_job("blocker"), blocker_writer, tmp_path)
        )
        await asyncio.sleep(0.2)  # blocker holds the only slot
        cancel_event = asyncio.Event()
        writer = LogWriter(path=tmp_path / "log.zst")
        queued = asyncio.create_task(
            executor.build_attribute("b", mk_job(), writer, tmp_path, cancel_event)
        )
        await asyncio.sleep(0.1)
        cancel_event.set()
        outcome = await asyncio.wait_for(queued, timeout=2)
        blocker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await blocker
        return outcome

    assert asyncio.run(run()) == BuildOutcome.cancelled


def test_executor_finished_build_wins_cancel_race(
    tmp_path: Path, fake_nix: Path
) -> None:
    # The process exits successfully and the cancel event fires before
    # the executor observes the exit: the completed build must be
    # recorded as success, not cancelled.
    fake_nix.write_text("racepid")
    pidfile = tmp_path / "pid"

    async def run() -> BuildOutcome:
        executor = NixBuildExecutor(FairScheduler(2), BuildSettings(log_dir=tmp_path))
        writer = LogWriter(path=tmp_path / "log.zst")
        cancel_event = asyncio.Event()

        async def cancel_after_exit() -> None:
            while (  # noqa: ASYNC110 — polling an external file is the point
                not pidfile.exists() or not pidfile.read_text().strip()
            ):
                await asyncio.sleep(0.005)
            pid = int(pidfile.read_text())
            # Wait until the child has been reaped (exit observed by the
            # event loop), then request cancellation. Polling is the only
            # way to observe another process's exit from outside.
            while True:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.005)
            cancel_event.set()

        canceller = asyncio.create_task(cancel_after_exit())
        outcome = await executor.build_attribute(
            "b", mk_job(), writer, tmp_path, cancel_event
        )
        canceller.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await canceller
        await writer.close()
        return outcome

    assert asyncio.run(run()) == BuildOutcome.success


def test_executor_task_cancelled_while_queued_releases_waiter(
    tmp_path: Path, fake_nix: Path
) -> None:
    # Cancelling the coroutine itself while it waits for a slot must
    # withdraw the waiter: a later grant must not leak a slot.
    fake_nix.write_text("hang")

    async def run() -> None:
        queue = FairScheduler(1)
        executor = NixBuildExecutor(queue, BuildSettings(log_dir=tmp_path))
        await queue.acquire("seed")  # occupy the only slot
        writer = LogWriter(path=tmp_path / "log.zst")
        queued = asyncio.create_task(
            executor.build_attribute("b", mk_job(), writer, tmp_path)
        )
        await asyncio.sleep(0.05)  # let it enqueue
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        queue.release()
        # The slot must be available again, not granted to the dead waiter.
        await asyncio.wait_for(queue.acquire("c"), timeout=1)

    asyncio.run(run())


def test_render_log_event_attributes_and_colors() -> None:
    activities: dict[int, str] = {}
    start = (
        b'@nix {"action":"start","id":7,"type":105,'
        b'"text":"building \'/nix/store/abc123-hello-2.12.drv\'",'
        b'"fields":["/nix/store/abc123-hello-2.12.drv"]}'
    )
    assert render_log_event(start, activities) == (
        b"building '/nix/store/abc123-hello-2.12.drv'\n"
    )
    log_line = (
        b'@nix {"action":"result","id":7,"type":101,'
        b'"fields":["checking for gcc... yes"]}'
    )
    assert render_log_event(log_line, activities) == (
        b"hello-2.12> checking for gcc... yes\n"
    )
    # nix's own messages keep their ANSI colors.
    msg = b'@nix {"action":"msg","level":0,"msg":"\\u001b[31;1merror:\\u001b[0m boom"}'
    assert render_log_event(msg, activities) == b"\x1b[31;1merror:\x1b[0m boom\n"
    # Progress events produce no log output.
    assert render_log_event(b'@nix {"action":"stop","id":7}', activities) is None
    # Non-event output passes through.
    assert render_log_event(b"plain line\n", activities) == b"plain line\n"


# As written by render_log_event: the builder's own lines arrive as
# internal-json events and get a name> prefix; nix's prose error then
# re-quotes the same lines with a bare "> ".
NIX_FAILURE_TAIL = """\
building '/nix/store/mxpvhwgqn3q6sl1lykymcj77z7a1iifi-fail-1.drv'
fail-1> this build is supposed to fail
error: build of '/nix/store/mxpvhwgqn3q6sl1lykymcj77z7a1iifi-fail-1.drv' failed: Cannot build '/nix/store/mxpvhwgqn3q6sl1lykymcj77z7a1iifi-fail-1.drv'.
       Reason: builder failed with exit code 1.
       Output paths:
         /nix/store/wxpxl5abpxrr4ljnwj100nhijnbn2riz-fail-1
       Last 1 log lines:
       > this build is supposed to fail
       For full logs, run:
         nix log /nix/store/mxpvhwgqn3q6sl1lykymcj77z7a1iifi-fail-1.drv
error: Cannot build '/nix/store/mxpvhwgqn3q6sl1lykymcj77z7a1iifi-fail-1.drv'.
       Reason: builder failed with exit code 1.
       Output paths:
         /nix/store/wxpxl5abpxrr4ljnwj100nhijnbn2riz-fail-1
"""


def test_failure_excerpt_extracts_log_and_reason() -> None:
    excerpt = failure_excerpt(NIX_FAILURE_TAIL)
    lines = excerpt.splitlines()
    # The structured (name-prefixed) line wins over nix's prose
    # re-quote of the same text; the failure reason follows.
    assert lines[0] == "fail-1> this build is supposed to fail"
    assert lines[1] == "Reason: builder failed with exit code 1."
    assert len([x for x in lines if "supposed to fail" in x]) == 1
    # nix boilerplate is gone.
    assert "Output paths" not in excerpt
    assert "For full logs" not in excerpt
    assert "Last 1 log lines" not in excerpt


def test_failure_excerpt_without_log_lines_keeps_filtered_tail() -> None:
    tail = (
        "error: a 'aarch64-linux' with features {} is required to build x.drv\n"
        "required (system, features): (aarch64-linux, [])\n"
        "3 available machines:\n"
    )
    excerpt = failure_excerpt(tail)
    assert "required to build" in excerpt
    assert "3 available machines" in excerpt


def test_iter_lines_survives_line_over_stream_limit() -> None:
    # A single output line larger than the StreamReader limit used to
    # raise LimitOverrunError in the pump, leaving nix blocked on the
    # full pipe until the build timeout.
    async def run() -> list[bytes]:
        reader = asyncio.StreamReader(limit=64)
        reader.feed_data(b"A" * 1000 + b"\nnext\n")
        reader.feed_eof()
        return [chunk async for chunk in iter_lines(reader)]

    chunks = asyncio.run(run())
    assert b"".join(chunks) == b"A" * 1000 + b"\nnext\n"
    # Line-oriented behavior preserved for lines within bounds.
    assert chunks[-1] == b"next\n"


def test_iter_lines_caps_buffered_line_length() -> None:
    # An endless line must not buffer unboundedly: the buffer is
    # flushed whenever it exceeds max_line, so memory stays bounded by
    # max_line plus one read chunk.
    async def run() -> list[bytes]:
        reader = asyncio.StreamReader(limit=64)
        reader.feed_data(b"B" * 300)
        reader.feed_eof()
        return [chunk async for chunk in iter_lines(reader, max_line=100)]

    chunks = asyncio.run(run())
    assert b"".join(chunks) == b"B" * 300
    assert all(len(c) <= 100 + 64 * 1024 for c in chunks)
