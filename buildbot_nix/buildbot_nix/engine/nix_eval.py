"""Async nix-eval-jobs runner.

Ported from the buildbot step in buildbot_nix/nix_eval.py: streams
nix-eval-jobs JSON lines, honors the repo's `buildbot-nix.toml`
(flake_dir/lock_file/attribute), extracts evaluation warnings from
stderr, and sizes workers/memory dynamically (engine/memory.py).

The evaluator runs inside a bubblewrap sandbox as defense in depth:
only the worktree, the nix store, the gc-roots dir, the nix daemon
socket, and an optional per-build repo-scoped credential file are
mounted — never $CREDENTIALS_DIRECTORY. Network stays available for
flake input fetching. The process is launched in a transient systemd
scope with a cgroup memory limit; eval OOM is a permanent failure.
A semaphore caps concurrent evaluations (default 1).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .models import NixEvalJob, NixEvalJobModel

if TYPE_CHECKING:
    from collections.abc import Callable

    from .repo_config import BranchConfig

logger = logging.getLogger(__name__)

OOM_RETURN_CODES = (-9, 137)

# StreamReader buffer limit: nix-eval-jobs emits one JSON object per
# line including the full neededBuilds/neededSubstitutes closures,
# which easily exceeds asyncio's 64 KiB default readline limit.
STREAM_LIMIT = 64 * 1024 * 1024


class EvalError(Exception):
    """nix-eval-jobs failed."""


class EvalOOMError(EvalError):
    """Evaluation ran out of memory: permanent failure, no retry."""


@dataclass
class EvalSettings:
    gc_roots_dir: Path
    worker_count: int = 1
    max_memory_size_mib: int = 4096
    show_trace: bool = False
    # bwrap sandbox; disable only in tests.
    sandbox: bool = True
    # Transient systemd scope with MemoryMax; needs a systemd session.
    systemd_scope: bool = True
    # Repo-scoped credential file mounted as $HOME/.netrc in the sandbox.
    netrc_file: Path | None = None
    nix_daemon_socket: Path = Path("/nix/var/nix/daemon-socket/socket")
    extra_ro_paths: list[Path] = field(default_factory=list)


@dataclass
class EvalResult:
    jobs: list[NixEvalJob]
    warnings: list[str]


def build_eval_command(
    branch_config: BranchConfig, settings: EvalSettings
) -> list[str]:
    """The plain nix-eval-jobs invocation (without sandbox wrapping)."""
    flake = f"{branch_config.flake_dir}#{branch_config.attribute}"
    return [
        "nix-eval-jobs",
        "--option",
        "eval-cache",
        "false",
        "--workers",
        str(settings.worker_count),
        "--max-memory-size",
        str(settings.max_memory_size_mib),
        "--option",
        "accept-flake-config",
        "true",
        "--gc-roots-dir",
        str(settings.gc_roots_dir),
        "--force-recurse",
        "--check-cache-status",
        *(["--show-trace"] if settings.show_trace else []),
        "--flake",
        flake,
        *(
            ["--reference-lock-file", branch_config.lock_file]
            if branch_config.lock_file != "flake.lock"
            else []
        ),
    ]


# Host configuration the evaluator needs inside the sandbox: nix.conf
# (experimental features, substituters), TLS certificates and name
# resolution for flake-input fetching. /etc/static covers NixOS's
# symlinked /etc entries.
SANDBOX_ETC_PATHS = (
    "/etc/nix",
    "/etc/static",
    "/etc/ssl",
    "/etc/resolv.conf",
    "/etc/hosts",
    "/etc/passwd",
    "/etc/group",
    "/etc/nsswitch.conf",
)


def build_sandbox_command(worktree_path: Path, settings: EvalSettings) -> list[str]:
    """bwrap wrapper: worktree + nix store + gc-roots dir + daemon socket
    + per-build credential file only; never $CREDENTIALS_DIRECTORY."""
    cmd = [
        "bwrap",
        "--unshare-all",
        "--share-net",
        "--die-with-parent",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",  # noqa: S108
        "--ro-bind",
        "/nix/store",
        "/nix/store",
        "--ro-bind",
        "/nix/var/nix/db",
        "/nix/var/nix/db",
        "--ro-bind",
        str(settings.nix_daemon_socket),
        str(settings.nix_daemon_socket),
        "--bind",
        str(settings.gc_roots_dir),
        str(settings.gc_roots_dir),
        "--bind",
        str(worktree_path),
        str(worktree_path),
        "--setenv",
        "HOME",
        "/tmp",  # noqa: S108
        # The db bind is read-only; always go through the daemon.
        "--setenv",
        "NIX_REMOTE",
        "daemon",
        "--chdir",
        str(worktree_path),
    ]
    for path in (*map(Path, SANDBOX_ETC_PATHS), *settings.extra_ro_paths):
        if path.exists():
            cmd += ["--ro-bind", str(path), str(path)]
    if settings.netrc_file is not None:
        cmd += ["--ro-bind", str(settings.netrc_file), "/tmp/.netrc"]  # noqa: S108
    return cmd


async def systemd_scope_available() -> bool:
    """Whether transient scopes can be created (running as root or with a
    user session); system users without one fall back to no scope."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemd-run",
            "--scope",
            "--quiet",
            "--collect",
            "true",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await proc.wait() == 0
    except OSError:
        return False


class CgroupLimiter:
    """Per-eval memory caps via a delegated cgroup v2 subtree.

    With Delegate=memory on the service unit the engine owns its cgroup
    subtree: it moves itself into a "main" leaf (the no-internal-processes
    rule forbids processes next to child cgroups with controllers), enables
    the memory controller for the subtree, and runs each evaluation in its
    own leaf with memory.max set. Unlike systemd-run this needs no polkit
    authorization, so it works for an unprivileged system user.
    """

    def __init__(self, root: Path, *, subtree_enabled: bool = True) -> None:
        self.root = root
        self._counter = itertools.count()
        self._subtree_enabled = subtree_enabled

    @classmethod
    def create(cls) -> CgroupLimiter | None:
        """Set up the delegated subtree; None when not delegated (no
        cgroup v2, read-only /sys/fs/cgroup, or memory controller absent)."""
        try:
            cgroup_line = Path("/proc/self/cgroup").read_text()
        except OSError:
            return None
        own = next(
            (
                line.removeprefix("0::")
                for line in cgroup_line.splitlines()
                if line.startswith("0::")
            ),
            None,
        )
        if own is None:
            return None
        root = Path("/sys/fs/cgroup") / own.lstrip("/")
        try:
            controllers = (root / "cgroup.controllers").read_text().split()
            if "memory" not in controllers:
                return None
            main = root / "main"
            main.mkdir(exist_ok=True)
            (main / "cgroup.procs").write_text(str(os.getpid()))
        except OSError:
            return None
        subtree_enabled = True
        try:
            (root / "cgroup.subtree_control").write_text("+memory")
        except OSError:
            # EBUSY while another process (e.g. an ExecStartPost health
            # check) still sits in the unit cgroup: retry lazily on the
            # first eval, by which time it has usually exited.
            subtree_enabled = False
        return cls(root, subtree_enabled=subtree_enabled)

    def new_eval_cgroup(self, limit_mib: int) -> Path:
        if not self._subtree_enabled:
            (self.root / "cgroup.subtree_control").write_text("+memory")
            self._subtree_enabled = True
        path = self.root / f"eval-{next(self._counter)}"
        path.mkdir()
        (path / "memory.max").write_text(str(limit_mib * 1024 * 1024))
        return path

    @staticmethod
    def _kill(path: Path) -> None:
        kill = path / "cgroup.kill"
        if kill.exists():
            with contextlib.suppress(OSError):
                kill.write_text("1")

    @staticmethod
    def _try_rmdir(path: Path) -> bool:
        try:
            path.rmdir()
        except OSError:
            return False
        return True

    async def cleanup(self, path: Path) -> None:
        await asyncio.to_thread(self._kill, path)
        for _ in range(50):
            if await asyncio.to_thread(self._try_rmdir, path):
                return
            await asyncio.sleep(0.1)
        logger.warning("failed to remove eval cgroup", extra={"path": str(path)})


def build_systemd_scope_command(settings: EvalSettings) -> list[str]:
    """Transient scope so the kernel enforces a cgroup memory limit on
    the whole eval process tree."""
    # Head-room above the per-worker limit: workers are restarted by
    # nix-eval-jobs when they exceed max-memory-size, the scope limit is
    # the hard backstop for the whole tree.
    scope_limit = settings.max_memory_size_mib * (settings.worker_count + 1)
    return [
        "systemd-run",
        "--scope",
        "--collect",
        "--quiet",
        f"--property=MemoryMax={scope_limit}M",
    ]


def build_full_command(
    worktree_path: Path, branch_config: BranchConfig, settings: EvalSettings
) -> list[str]:
    cmd: list[str] = []
    if settings.systemd_scope:
        cmd += build_systemd_scope_command(settings)
    if settings.sandbox:
        cmd += build_sandbox_command(worktree_path, settings)
    cmd += build_eval_command(branch_config, settings)
    return cmd


def extract_eval_warnings(stderr_output: str) -> list[str]:
    """Extract `evaluation warning:` blocks from nix-eval-jobs stderr,
    filtering out Nix configuration warnings. Ported from
    NixEvalCommand._format_warnings."""
    warning_lines = stderr_output.strip().split("\n") if stderr_output else []
    eval_warnings: list[str] = []
    i = 0
    while i < len(warning_lines):
        line = warning_lines[i]
        if "evaluation warning:" in line:
            first_line = line.replace("evaluation warning:", "").strip()
            warning_block = [first_line] if first_line else []
            i += 1
            # Collect continuation lines (indented, or empty lines
            # followed by an indented line).
            while i < len(warning_lines):
                next_line = warning_lines[i]
                if next_line.startswith((" ", "\t")):
                    warning_block.append(next_line.strip())
                    i += 1
                elif not next_line.strip():
                    if i + 1 < len(warning_lines) and warning_lines[i + 1].startswith(
                        (" ", "\t")
                    ):
                        warning_block.append("")
                        i += 1
                    else:
                        break
                else:
                    break
            if warning_block:
                eval_warnings.append("\n".join(warning_block))
        else:
            i += 1
    return eval_warnings


class EvalRunner:
    """Runs nix-eval-jobs with a global concurrency cap."""

    def __init__(
        self, concurrency: int = 1, limiter: CgroupLimiter | None = None
    ) -> None:
        self._semaphore = asyncio.Semaphore(concurrency)
        self.limiter = limiter
        # Probed on first use; system users without a session can't
        # create transient scopes.
        self._systemd_scope_ok: bool | None = None

    async def run(
        self,
        worktree_path: Path,
        branch_config: BranchConfig,
        settings: EvalSettings,
    ) -> EvalResult:
        async with self._semaphore:
            return await self._run(worktree_path, branch_config, settings)

    async def _run(
        self,
        worktree_path: Path,
        branch_config: BranchConfig,
        settings: EvalSettings,
    ) -> EvalResult:
        settings.gc_roots_dir.mkdir(parents=True, exist_ok=True)

        # Prefer the delegated cgroup over a systemd-run scope: same
        # kernel-enforced tree-wide limit without needing polkit.
        eval_cgroup: Path | None = None
        if self.limiter is not None and settings.systemd_scope:
            limit = settings.max_memory_size_mib * (settings.worker_count + 1)
            try:
                eval_cgroup = self.limiter.new_eval_cgroup(limit)
            except OSError:
                logger.exception("failed to create eval cgroup")
            else:
                settings = replace(settings, systemd_scope=False)
        if settings.systemd_scope:
            if self._systemd_scope_ok is None:
                self._systemd_scope_ok = await systemd_scope_available()
            if not self._systemd_scope_ok:
                settings = replace(settings, systemd_scope=False)

        def join_eval_cgroup() -> None:
            # Runs in the child between fork and exec; "0" means self.
            assert eval_cgroup is not None  # noqa: S101
            (eval_cgroup / "cgroup.procs").write_text("0")

        try:
            return await self._spawn(
                worktree_path,
                branch_config,
                settings,
                preexec_fn=join_eval_cgroup if eval_cgroup is not None else None,
            )
        finally:
            if self.limiter is not None and eval_cgroup is not None:
                await self.limiter.cleanup(eval_cgroup)

    async def _spawn(
        self,
        worktree_path: Path,
        branch_config: BranchConfig,
        settings: EvalSettings,
        preexec_fn: Callable[[], None] | None = None,
    ) -> EvalResult:
        cmd = build_full_command(worktree_path, branch_config, settings)
        logger.info(
            "starting evaluation",
            extra={"worktree": str(worktree_path), "argv0": cmd[0]},
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=preexec_fn,
            limit=STREAM_LIMIT,
            env={
                "CLICOLOR_FORCE": "1",
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            },
        )
        assert proc.stdout is not None  # noqa: S101
        assert proc.stderr is not None  # noqa: S101

        jobs: list[NixEvalJob] = []
        parse_errors: list[str] = []

        async def read_stdout() -> None:
            assert proc.stdout is not None  # noqa: S101
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    jobs.append(NixEvalJobModel.validate_python(json.loads(line)))
                except (json.JSONDecodeError, ValueError) as e:
                    parse_errors.append(f"failed to parse line: {line}: {e}")

        async def read_stderr() -> bytes:
            assert proc.stderr is not None  # noqa: S101
            return await proc.stderr.read()

        try:
            _, stderr_bytes = await asyncio.gather(read_stdout(), read_stderr())
            returncode = await proc.wait()
        except asyncio.CancelledError:
            # Build superseded/cancelled: kill the evaluator instead
            # of letting it run to completion.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise
        stderr_text = stderr_bytes.decode(errors="replace")
        warnings = extract_eval_warnings(stderr_text)

        if returncode != 0:
            tail = "\n".join(stderr_text.splitlines()[-50:])
            if returncode in OOM_RETURN_CODES or "out of memory" in stderr_text:
                msg = (
                    "evaluation ran out of memory (cgroup limit "
                    f"{settings.max_memory_size_mib} MiB/worker); this is a "
                    f"permanent failure, reduce evaluation memory usage:\n{tail}"
                )
                raise EvalOOMError(msg)
            msg = f"nix-eval-jobs failed with exit code {returncode}:\n{tail}"
            raise EvalError(msg)
        if parse_errors:
            msg = "; ".join(parse_errors)
            raise EvalError(msg)
        return EvalResult(jobs=jobs, warnings=warnings)
