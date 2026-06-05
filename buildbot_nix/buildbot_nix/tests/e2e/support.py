"""Shared test infrastructure: ephemeral Postgres, seed data, and a
real uvicorn server hosting the engine web app.

Used by the browser e2e tests and the httpx-based web tests.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import socket
import subprocess
import threading
import time
from typing import TYPE_CHECKING

import asyncpg
import uvicorn

from buildbot_nix.engine.migrations import apply_migrations
from buildbot_nix.engine.web.app import create_app
from buildbot_nix.engine.web.logs import LogRegistry

if TYPE_CHECKING:
    from collections.abc import Coroutine, Iterator
    from pathlib import Path

    import pytest


def run_sync[T](coro: Coroutine[None, None, T]) -> T:
    """asyncio.run in a fresh thread.

    Playwright's sync API keeps an asyncio loop running (greenlet-
    suspended) in the calling thread, so asyncio.run would fail there.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result(timeout=120)


@contextlib.contextmanager
def ephemeral_postgres(
    tmp_path_factory: pytest.TempPathFactory, dbname: str
) -> Iterator[str]:
    """Throwaway Postgres on a unix socket with migrations applied."""
    datadir = tmp_path_factory.mktemp("pgdata")
    sockdir = tmp_path_factory.mktemp("pgsock")
    subprocess.run(  # noqa: S603
        ["initdb", "-D", str(datadir), "-U", "test", "--auth=trust"],
        check=True,
        capture_output=True,
    )
    proc = subprocess.Popen(  # noqa: S603
        ["postgres", "-D", str(datadir), "-k", str(sockdir), "-c", "listen_addresses="],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while not (sockdir / ".s.PGSQL.5432").exists():
            if time.monotonic() > deadline:
                msg = "postgres did not start"
                raise RuntimeError(msg)
            time.sleep(0.1)
        subprocess.run(  # noqa: S603
            ["createdb", "-h", str(sockdir), "-U", "test", dbname],
            check=True,
            capture_output=True,
        )
        dsn = f"postgresql://test@/{dbname}?host={sockdir}"
        run_sync(apply_migrations(dsn))
        yield dsn
    finally:
        proc.terminate()
        proc.wait()


async def seed(dsn: str) -> None:
    """One project with a succeeded, a failed, and a running build."""
    pool = await asyncpg.create_pool(dsn)
    try:
        project_id = await pool.fetchval(
            """
            INSERT INTO projects (forge, forge_repo_id, owner, name,
                                  default_branch, url, enabled)
            VALUES ('github', 'web-1', 'acme', 'widget', 'main',
                    'https://github.com/acme/widget', TRUE)
            RETURNING id
            """
        )
        for number, status, branch in [
            (1, "succeeded", "main"),
            (2, "failed", "main"),
            (3, "building", "feature"),
        ]:
            build_id = await pool.fetchval(
                """
                INSERT INTO builds (project_id, number, tree_hash, commit_sha,
                                    branch, status, pr_number, created_at,
                                    started_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, now(), now())
                RETURNING id
                """,
                project_id,
                number,
                f"tree-{number}",
                f"sha-{number}{'0' * 30}",
                branch,
                status,
                5 if branch == "feature" else None,
            )
            await pool.execute(
                """
                INSERT INTO build_attributes (build_id, attr, system, status, error)
                VALUES
                  ($1, 'x86_64-linux.ok', 'x86_64-linux', 'succeeded', NULL),
                  ($1, 'x86_64-linux.bad', 'x86_64-linux', $2, $3),
                  ($1, 'aarch64-linux.other', 'aarch64-linux', 'succeeded', NULL)
                """,
                build_id,
                "failed" if status == "failed" else "succeeded",
                "error: builder failed loudly" if status == "failed" else None,
            )
    finally:
        await pool.close()


class EngineServer:
    """Engine web app on a real TCP port, in a background event loop.

    Playwright drives a real browser, so an ASGI test client is not
    enough. `run()` executes coroutines (DB updates, log writes) on the
    server's loop from the synchronous test thread.
    """

    def __init__(self, dsn: str, state_dir: Path) -> None:
        self.dsn = dsn
        self.state_dir = state_dir
        self.registry = LogRegistry()
        # Assigned by the server thread before the TCP port opens.
        self.pool: asyncpg.Pool
        self.loop: asyncio.AbstractEventLoop
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        def main() -> None:
            async def serve() -> None:
                self.loop = asyncio.get_running_loop()
                self.pool = await asyncpg.create_pool(self.dsn)
                try:
                    app = create_app(self.pool, self.state_dir, self.registry)
                    config = uvicorn.Config(
                        app, host="127.0.0.1", port=self.port, log_level="warning"
                    )
                    self._server = uvicorn.Server(config)
                    await self._server.serve()
                finally:
                    await self.pool.close()

            asyncio.run(serve())

        self._thread = threading.Thread(target=main, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 30
        while True:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                    return
            except OSError:
                if not self._thread.is_alive():
                    msg = "server thread died during startup"
                    raise RuntimeError(msg) from None
                if time.monotonic() > deadline:
                    msg = "uvicorn did not start"
                    raise RuntimeError(msg) from None
                time.sleep(0.1)

    def run[T](self, coro: Coroutine[None, None, T]) -> T:
        """Run a coroutine on the server's event loop."""
        future: concurrent.futures.Future[T] = asyncio.run_coroutine_threadsafe(
            coro, self.loop
        )
        return future.result(timeout=30)

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)
