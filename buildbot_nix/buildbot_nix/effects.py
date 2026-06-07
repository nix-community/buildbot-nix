"""Hercules-style effects execution (reusing the buildbot_effects CLI).

Ported from buildbot_effects.py + the effects parts of nix_eval.py:

- effects run per the DEFAULT BRANCH's repo config: default branch
  always; PRs when `effects_on_pull_requests`; branches matching
  `effects_branches` globs,
- effects are listed via `buildbot-effects list` and each effect run
  via `buildbot-effects run <name>`,
- per-repo secret resolution supports exact `forge:owner/repo` and org
  wildcard `forge:owner/*` entries; the secret JSON is written next to
  the checkout as ../secrets.json for the duration of the run,
- `effects.extraSandboxPaths` are forwarded.

The orchestrator sets the build's effects started-flag
before invoking run_effect and never auto-re-runs effects on crash
recovery (deploys are not idempotent).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .repo_config import BranchConfig

    LogWrite = Callable[[bytes], Awaitable[None]]

logger = logging.getLogger(__name__)

# Deploy tooling emits arbitrarily long lines; asyncio's 64 KiB
# StreamReader default would abort the read loop on them.
STREAM_LIMIT = 16 * 1024 * 1024
# Deploys can hang on the network; same cap as attribute builds.
DEFAULT_TIMEOUT = 60 * 60 * 3


class EffectsError(Exception):
    pass


def resolve_effects_secret(
    per_repo_effects_secrets: dict[str, str],
    forge_type: str,
    owner: str,
    repo: str,
) -> str | None:
    """Resolve effects secret, either repo-specific or org wildcard."""
    secret_name = per_repo_effects_secrets.get(f"{forge_type}:{owner}/{repo}")
    if secret_name is None:
        secret_name = per_repo_effects_secrets.get(f"{forge_type}:{owner}/*")
    return secret_name


def should_run_effects(
    default_branch_config: BranchConfig,
    default_branch: str,
    branch: str,
    *,
    is_pull_request: bool,
) -> bool:
    """Effects scope follows the default branch's repo config."""
    if branch == default_branch:
        return True
    if is_pull_request:
        return default_branch_config.effects_on_pull_requests
    return any(
        fnmatch(branch, pattern) for pattern in default_branch_config.effects_branches
    )


@dataclass
class EffectsContext:
    worktree_path: Path
    rev: str
    branch: str
    repo: str
    secret_name: str | None = None
    extra_sandbox_paths: list[Path] = field(default_factory=list)
    timeout: float = DEFAULT_TIMEOUT
    default_branch: str | None = None
    # Forge token resolving hercules GitToken secret references.
    git_token: str | None = None
    # Hercules state API (served by the service) + project metadata.
    api_base_url: str | None = None
    task_token: str | None = None
    project_id: str | None = None
    mountables_file: Path | None = None
    extra_nix_options: dict[str, str] = field(default_factory=dict)


def _effects_args(ctx: EffectsContext, secrets_file: Path | None) -> list[str]:
    optional = []
    if ctx.default_branch is not None:
        optional += ["--default-branch", ctx.default_branch]
    if ctx.mountables_file is not None:
        optional += ["--mountables-file", str(ctx.mountables_file)]
    for key, value in ctx.extra_nix_options.items():
        optional += ["--extra-nix-option", f"{key}={value}"]
    return [
        "--rev",
        ctx.rev,
        "--branch",
        ctx.branch,
        "--repo",
        ctx.repo,
        *optional,
        *[
            arg
            for path in ctx.extra_sandbox_paths
            for arg in ("--extra-sandbox-path", str(path))
        ],
        *(["--secrets", str(secrets_file)] if secrets_file is not None else []),
    ]


def _read_secret_file(secret_name: str) -> str:
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if directory is None:
        msg = (
            f"effects secret {secret_name!r} requested but "
            "$CREDENTIALS_DIRECTORY is not set"
        )
        raise EffectsError(msg)
    return (Path(directory) / secret_name).read_text()


async def _pump(
    stream: asyncio.StreamReader, chunks: list[bytes], log_write: LogWrite | None
) -> None:
    async for line in stream:
        chunks.append(line)
        if log_write is not None:
            await log_write(line)


async def _run(
    cmd: list[str],
    cwd: Path,
    log_write: LogWrite | None = None,
    time_limit: float = DEFAULT_TIMEOUT,
) -> tuple[int, str, str]:
    """Run buildbot-effects, returning (returncode, stdout, stderr).

    stderr is kept separate so nix logging cannot corrupt the JSON on
    stdout. The service environment is inherited (remote builders need
    $HOME for ~/.ssh). On timeout or read errors the process group is
    killed so no effect keeps running detached.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=STREAM_LIMIT,
        start_new_session=True,  # own process group for clean kill
    )
    assert proc.stdout is not None  # noqa: S101
    assert proc.stderr is not None  # noqa: S101
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    try:
        async with asyncio.timeout(time_limit):
            await asyncio.gather(
                _pump(proc.stdout, stdout_chunks, log_write),
                _pump(proc.stderr, stderr_chunks, log_write),
                proc.wait(),
            )
    except BaseException as e:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        if isinstance(e, TimeoutError):
            msg = f"{cmd[0]} {cmd[1]} timed out after {time_limit}s"
            raise EffectsError(msg) from e
        raise
    return (
        proc.returncode or 0,
        b"".join(stdout_chunks).decode(errors="replace"),
        b"".join(stderr_chunks).decode(errors="replace"),
    )


async def list_effects(ctx: EffectsContext) -> list[str]:
    """`buildbot-effects list`: the effects defined by the flake."""
    returncode, output, errors = await _run(
        ["buildbot-effects", "list", *_effects_args(ctx, None)],
        ctx.worktree_path,
        time_limit=ctx.timeout,
    )
    if returncode != 0:
        msg = f"buildbot-effects list failed ({returncode}): {errors[-2000:]}"
        raise EffectsError(msg)
    if not output.strip():
        return []
    try:
        effects = json.loads(output)
    except json.JSONDecodeError as e:
        msg = f"failed to parse buildbot-effects list output: {e}"
        raise EffectsError(msg) from e
    return list(effects)


async def run_effect(
    ctx: EffectsContext,
    effect: str,
    log_write: LogWrite | None = None,
) -> bool:
    """Run one effect; returns success. The secrets file is written
    outside the checkout (parent directory, like the buildbot setup)
    and removed afterwards."""
    side_files: list[Path] = []

    def _side_file(suffix: str, content: str) -> Path:
        # Deploy credentials: 0600 only. touch() applies the mode just
        # on creation, so drop any leftover file first.
        path = ctx.worktree_path.parent / f"{ctx.worktree_path.name}-{suffix}"
        path.unlink(missing_ok=True)
        path.touch(mode=0o600)
        path.write_text(content)
        side_files.append(path)
        return path

    secrets_file: Path | None = None
    if ctx.secret_name is not None:
        secrets_file = _side_file("secrets.json", _read_secret_file(ctx.secret_name))
    extra: list[str] = []
    if ctx.git_token is not None:
        extra += ["--git-token-file", str(_side_file("git-token", ctx.git_token))]
    if ctx.task_token is not None:
        extra += ["--task-token-file", str(_side_file("task-token", ctx.task_token))]
    if ctx.api_base_url is not None:
        extra += ["--api-base-url", ctx.api_base_url]
    if ctx.project_id is not None:
        extra += ["--project-id", ctx.project_id]
    extra += ["--project-path", ctx.repo]
    try:
        returncode, _, _ = await _run(
            [
                "buildbot-effects",
                "run",
                *_effects_args(ctx, secrets_file),
                *extra,
                effect,
            ],
            ctx.worktree_path,
            log_write,
            time_limit=ctx.timeout,
        )
        return returncode == 0
    finally:
        for path in side_files:
            path.unlink(missing_ok=True)
