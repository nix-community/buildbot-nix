"""Nix daemon proxy for the effect sandbox.

hercules-ci-agent gives effects a private daemon socket instead of
the host's: each connection is served by its own untrusted daemon
process, so effects cannot use trusted-user privileges and the
configured extra nix options apply. `nix-daemon --stdio` provides
exactly that per-connection behavior.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        # Propagate EOF: the daemon only exits when its stdin closes,
        # and the client only sees the end of the reply this way.
        with contextlib.suppress(OSError):
            writer.close()


async def _handle(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    extra_options: list[tuple[str, str]],
) -> None:
    args = [arg for k, v in extra_options for arg in ("--option", k, v)]
    try:
        proc = await asyncio.create_subprocess_exec(
            "nix-daemon",
            "--stdio",
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        # A hanging connection would stall the nix client in the
        # effect; a closed one fails it loudly.
        writer.close()
        raise
    assert proc.stdin is not None  # noqa: S101
    assert proc.stdout is not None  # noqa: S101
    try:
        await asyncio.gather(_pump(reader, proc.stdin), _pump(proc.stdout, writer))
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


@contextmanager
def nix_daemon_proxy(
    socket_path: Path, extra_options: list[tuple[str, str]]
) -> Iterator[None]:
    """Serve a unix socket while the effect runs; stops on exit."""
    loop = asyncio.new_event_loop()

    def runner() -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            # Drain cancelled handlers so their daemon subprocesses
            # are killed and awaited.
            tasks = asyncio.all_tasks(loop)
            for task in tasks:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            loop.close()

    async def start() -> asyncio.Server:
        return await asyncio.start_unix_server(
            lambda r, w: _handle(r, w, extra_options), path=str(socket_path)
        )

    thread = threading.Thread(target=runner)
    thread.start()
    try:
        asyncio.run_coroutine_threadsafe(start(), loop).result(timeout=10)
        yield
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
