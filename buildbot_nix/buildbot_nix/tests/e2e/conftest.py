"""Fixtures wiring support.py to Playwright.

Skips cleanly when postgresql, playwright, or the browsers
(PLAYWRIGHT_BROWSERS_PATH) are unavailable.
"""

from __future__ import annotations

import os
import shutil
from typing import TYPE_CHECKING

import pytest

from buildbot_nix.tests.support import ephemeral_postgres, run_sync

from .support import EngineServer, seed

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from playwright.sync_api import Browser, Page


@pytest.fixture(scope="module")
def state_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("state")


@pytest.fixture(scope="module")
def postgres_dsn(
    tmp_path_factory: pytest.TempPathFactory, state_dir: Path
) -> Iterator[str]:
    if shutil.which("initdb") is None:
        pytest.skip("postgresql not available")
    with ephemeral_postgres(tmp_path_factory, "e2e") as dsn:
        run_sync(seed(dsn, state_dir))
        yield dsn


@pytest.fixture(scope="module")
def server(postgres_dsn: str, state_dir: Path) -> Iterator[EngineServer]:
    engine_server = EngineServer(postgres_dsn, state_dir)
    engine_server.start()
    yield engine_server
    engine_server.stop()


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        pytest.skip("PLAYWRIGHT_BROWSERS_PATH not set")
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        # The nix build sandbox forbids Chromium's own sandbox (no user
        # namespaces) and has a tiny /dev/shm.
        chromium = playwright.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        yield chromium
        chromium.close()


@pytest.fixture(scope="module")
def page(browser: Browser, server: EngineServer) -> Iterator[Page]:
    browser_page = browser.new_page(base_url=server.base_url)
    yield browser_page
    browser_page.close()
