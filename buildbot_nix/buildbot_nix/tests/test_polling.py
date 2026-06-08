"""Tests for the pull-based polling change source."""

# ruff: noqa: PLR2004 (literal values in test assertions are fine)
from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from buildbot_nix.gitrepo import FetchCredentials
from buildbot_nix.polling import (
    PolledRepository,
    PollingService,
    poll_head,
)

from .support import git, init_upstream

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    # "f" is committed so later `git commit -am` picks up edits to it.
    return init_upstream(tmp_path / "upstream", {"f": "1"})


@dataclass
class RecordingSink:
    changes: list[tuple[str, str, str]] = field(default_factory=list)

    async def head_changed(
        self, repo: PolledRepository, branch: str, commit_sha: str
    ) -> None:
        self.changes.append((repo.name, branch, commit_sha))


def test_poll_head(upstream: Path) -> None:
    repo = PolledRepository(name="r", url=str(upstream), default_branch="main")
    sha = asyncio.run(poll_head(repo))
    assert sha == git(upstream, "rev-parse", "HEAD")
    # Unknown branch: None.
    missing = PolledRepository(name="r", url=str(upstream), default_branch="nope")
    assert asyncio.run(poll_head(missing)) is None


def test_polling_detects_changes(upstream: Path) -> None:
    repo = PolledRepository(name="r", url=str(upstream), default_branch="main")
    sink = RecordingSink()
    service = PollingService([repo], sink)

    async def run() -> None:
        assert await service.poll_repo_once(repo)  # initial head
        assert not await service.poll_repo_once(repo)  # unchanged
        (upstream / "f").write_text("2")
        git(upstream, "commit", "-am", "c2")
        assert await service.poll_repo_once(repo)

    asyncio.run(run())
    assert len(sink.changes) == 2
    assert sink.changes[1][2] == git(upstream, "rev-parse", "HEAD")


def test_poll_loop_survives_sink_errors(upstream: Path) -> None:
    """A sink failure must not kill the loop; the head is retried."""

    @dataclass
    class FlakySink(RecordingSink):
        failures: int = 1

        async def head_changed(
            self, repo: PolledRepository, branch: str, commit_sha: str
        ) -> None:
            if self.failures > 0:
                self.failures -= 1
                msg = "db down"
                raise ConnectionError(msg)
            await super().head_changed(repo, branch, commit_sha)

    repo = PolledRepository(
        name="r", url=str(upstream), default_branch="main", poll_interval=0
    )
    sink = FlakySink()
    service = PollingService([repo], sink)

    async def run() -> None:
        service.start()
        try:
            for _ in range(200):
                if sink.changes:
                    break
                await asyncio.sleep(0.05)
        finally:
            await service.stop()

    asyncio.run(run())
    assert sink.failures == 0  # first submit attempt failed
    assert sink.changes == [("r", "main", git(upstream, "rev-parse", "HEAD"))]


def test_ssh_command_assembly(tmp_path: Path) -> None:
    creds = FetchCredentials(
        ssh_private_key_file=tmp_path / "key",
        ssh_known_hosts_file=tmp_path / "known_hosts",
    )
    command = creds.git_ssh_command()
    assert command is not None
    assert f"-i {tmp_path / 'key'}" in command
    assert f"UserKnownHostsFile={tmp_path / 'known_hosts'}" in command
    assert FetchCredentials().git_ssh_command() is None


def test_systemd_credential_name_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The NixOS module passes the bare LoadCredential name."""
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    repo = PolledRepository(
        name="r",
        url="git@example.com:r",
        default_branch="main",
        ssh_private_key_file=Path("pull-based__owner_slash_repo"),
    )
    creds = repo.credentials()
    assert creds.ssh_private_key_file == tmp_path / "pull-based__owner_slash_repo"
    # Absolute paths and unset CREDENTIALS_DIRECTORY pass through.
    monkeypatch.delenv("CREDENTIALS_DIRECTORY")
    assert repo.credentials().ssh_private_key_file == Path(
        "pull-based__owner_slash_repo"
    )
