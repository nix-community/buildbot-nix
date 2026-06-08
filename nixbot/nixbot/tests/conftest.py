"""Shared fixtures: one ephemeral Postgres per test module, a fresh
upstream git repo, and work-queue isolation."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

import pytest

from .support import ephemeral_postgres, init_upstream, truncate_work_queue

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(scope="module")
def postgres_dsn(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[str]:
    if shutil.which("initdb") is None:
        pytest.skip("postgresql not available")
    dbname = request.module.__name__.rsplit(".", 1)[-1].removeprefix("test_")
    with ephemeral_postgres(tmp_path_factory, dbname) as dsn:
        yield dsn


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    return init_upstream(tmp_path / "upstream")


@pytest.fixture
def fresh_work_queue(postgres_dsn: str) -> None:
    """Per-test isolation for modules sharing one database."""
    truncate_work_queue(postgres_dsn)
