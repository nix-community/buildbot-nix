"""Tests for effects secret resolution, scope rules, and CLI driving
(with a fake buildbot-effects on PATH)."""

# ruff: noqa: ARG001, S106 (fixtures used for side effects; test secret names)

from __future__ import annotations

import asyncio
import os
import stat
from typing import TYPE_CHECKING

import pytest

from buildbot_nix.engine.effects import (
    EffectsContext,
    EffectsError,
    list_effects,
    resolve_effects_secret,
    run_effect,
    should_run_effects,
)
from buildbot_nix.engine.repo_config import BranchConfig

if TYPE_CHECKING:
    from pathlib import Path

SECRETS = {
    "github:acme/widget": "widget-secret",
    "gitea:acme/*": "acme-org-secret",
}


def test_resolve_exact_match() -> None:
    assert (
        resolve_effects_secret(SECRETS, "github", "acme", "widget") == "widget-secret"
    )


def test_resolve_org_wildcard() -> None:
    assert (
        resolve_effects_secret(SECRETS, "gitea", "acme", "anything")
        == "acme-org-secret"
    )


def test_resolve_no_match() -> None:
    assert resolve_effects_secret(SECRETS, "github", "other", "repo") is None
    # Wildcard is per-forge: github:acme/* is not configured.
    assert resolve_effects_secret(SECRETS, "github", "acme", "other") is None


def test_should_run_effects_default_branch() -> None:
    assert should_run_effects(BranchConfig(), "main", "main", is_pull_request=False)


def test_should_run_effects_pr_gated() -> None:
    assert not should_run_effects(
        BranchConfig(), "main", "pr-branch", is_pull_request=True
    )
    assert should_run_effects(
        BranchConfig(effects_on_pull_requests=True),
        "main",
        "pr-branch",
        is_pull_request=True,
    )


def test_should_run_effects_branch_globs() -> None:
    config = BranchConfig(effects_branches=["release-*", "staging"])
    assert should_run_effects(config, "main", "release-1.0", is_pull_request=False)
    assert should_run_effects(config, "main", "staging", is_pull_request=False)
    assert not should_run_effects(config, "main", "feature", is_pull_request=False)


@pytest.fixture
def fake_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "buildbot-effects"
    script.write_text(
        f"""#!/bin/sh
echo "$@" >> {tmp_path}/calls
case "$1" in
  list)
    # nix routinely logs to stderr while evaluating; must not corrupt JSON.
    echo 'warning: Git tree is dirty' >&2
    echo '["deploy", "notify"]'
    ;;
  run)
    # Echo whether a secrets file was passed, its content, and its mode.
    prev=""
    for arg in "$@"; do
      if [ "$prev" = "--secrets" ]; then cat "$arg"; stat -c 'mode=%a' "$arg"; fi
      prev="$arg"
    done
    echo "HOME=$HOME"
    echo "running effect"
    ;;
esac
"""
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return tmp_path


def make_ctx(tmp_path: Path, secret_name: str | None = None) -> EffectsContext:
    worktree = tmp_path / "wt"
    worktree.mkdir(exist_ok=True)
    return EffectsContext(
        worktree_path=worktree,
        rev="abc123",
        branch="main",
        repo="acme/widget",
        secret_name=secret_name,
    )


def test_list_effects(tmp_path: Path, fake_effects: Path) -> None:
    effects = asyncio.run(list_effects(make_ctx(tmp_path)))
    assert effects == ["deploy", "notify"]
    calls = (fake_effects / "calls").read_text()
    assert "--rev abc123" in calls
    assert "--branch main" in calls
    assert "--repo acme/widget" in calls


def test_run_effect_with_secrets(
    tmp_path: Path, fake_effects: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "widget-secret").write_text('{"token": "s3"}')
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))

    chunks: list[bytes] = []

    async def log_write(data: bytes) -> None:
        chunks.append(data)

    ctx = make_ctx(tmp_path, secret_name="widget-secret")
    ok = asyncio.run(run_effect(ctx, "deploy", log_write))
    assert ok
    output = b"".join(chunks).decode()
    assert '{"token": "s3"}' in output  # secrets file content reached the CLI
    # Deploy credentials must not be world/group readable while on disk.
    assert "mode=600" in output
    assert "running effect" in output
    # Secrets file cleaned up afterwards.
    assert not list(tmp_path.glob("*-secrets.json"))


def test_run_effect_inherits_environment(
    tmp_path: Path, fake_effects: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Remote builders need $HOME for ~/.ssh; the service environment
    # must be inherited by buildbot-effects.
    monkeypatch.setenv("HOME", "/var/lib/buildbot-nix-test")
    chunks: list[bytes] = []

    async def log_write(data: bytes) -> None:
        chunks.append(data)

    ok = asyncio.run(run_effect(make_ctx(tmp_path), "deploy", log_write))
    assert ok
    assert b"HOME=/var/lib/buildbot-nix-test\n" in b"".join(chunks)


def test_long_output_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A single output line larger than asyncio's 64 KiB StreamReader
    # default must not abort the run.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "buildbot-effects"
    script.write_text('#!/bin/sh\nhead -c 200000 /dev/zero | tr "\\0" x\necho\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

    ok = asyncio.run(run_effect(make_ctx(tmp_path), "deploy"))
    assert ok


def test_timeout_kills_effect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "buildbot-effects"
    script.write_text("#!/bin/sh\nsleep 60\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

    ctx = make_ctx(tmp_path)
    ctx.timeout = 0.2
    with pytest.raises(EffectsError, match="timed out"):
        asyncio.run(run_effect(ctx, "deploy"))


def test_run_effect_extra_sandbox_paths(tmp_path: Path, fake_effects: Path) -> None:
    ctx = make_ctx(tmp_path)
    ctx.extra_sandbox_paths = [tmp_path / "extra"]
    asyncio.run(run_effect(ctx, "deploy"))
    calls = (fake_effects / "calls").read_text()
    assert f"--extra-sandbox-path {tmp_path / 'extra'}" in calls
