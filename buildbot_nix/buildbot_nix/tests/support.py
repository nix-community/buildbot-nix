"""Shared test helpers."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import subprocess
import time
from typing import TYPE_CHECKING

from buildbot_nix.migrations import apply_migrations

if TYPE_CHECKING:
    from collections.abc import Coroutine, Iterator

    import pytest

from buildbot_nix.models import CacheStatus, NixEvalJobSuccess


def mk_job(
    attr: str = "foo",
    *,
    deps: list[str] | None = None,
    cache_status: CacheStatus = CacheStatus.not_built,
    system: str = "x86_64-linux",
    out: str | None = None,
) -> NixEvalJobSuccess:
    """An eval result like nix-eval-jobs emits: needed_builds covers
    the job's own drv plus its dependencies."""
    drv = f"/nix/store/{attr}.drv"
    return NixEvalJobSuccess(
        attr=attr,
        attr_path=attr.split("."),
        cache_status=cache_status,
        needed_builds=[drv, *(f"/nix/store/{d}.drv" for d in (deps or []))],
        needed_substitutes=[],
        drv_path=drv,
        name=attr,
        outputs={"out": out or f"/nix/store/{attr}-out"},
        system=system,
    )


def cookie_header(cookies: dict[str, str]) -> dict[str, str]:
    """Cookies as a request header (per-request cookies= is
    deprecated in httpx)."""
    return {"cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}


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
