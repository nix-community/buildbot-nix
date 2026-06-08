"""Shared fixtures: one ephemeral Postgres per test module."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

import pytest

from .support import ephemeral_postgres

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(scope="module")
def postgres_dsn(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[str]:
    if shutil.which("initdb") is None:
        pytest.skip("postgresql not available")
    dbname = request.module.__name__.rsplit(".", 1)[-1].removeprefix("test_")
    with ephemeral_postgres(tmp_path_factory, dbname) as dsn:
        yield dsn
