"""Async nix-eval-jobs runner.

Ported from the buildbot step in nixbot/nix_eval.py: streams
nix-eval-jobs JSON lines, honors the repo's `nixbot.toml`
(flake_dir/lock_file/attribute), streams warnings/errors from stderr,
and sizes workers/memory dynamically (memory.py).

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
import signal
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .ansi import strip_ansi
from .environ import passthrough_env
from .models import NixEvalJob, NixEvalJobModel

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .repo_config import BranchConfig

    JobBatchCallback = Callable[[list[NixEvalJob]], Awaitable[None]]
    StderrLineCallback = Callable[[str], Awaitable[None]]

logger = logging.getLogger(__name__)

OOM_RETURN_CODES = (-9, 137)

# StreamReader buffer limit: nix-eval-jobs emits one JSON object per
# line including the full neededBuilds/neededSubstitutes closures,
# which easily exceeds asyncio's 64 KiB default readline limit.
STREAM_LIMIT = 64 * 1024 * 1024

# Retain only the tail of evaluator stderr: huge eval traces (deep
# --show-trace output) would otherwise be buffered unbounded in memory.
STDERR_TAIL_LIMIT = 1024 * 1024

JOB_BATCH_SIZE = 100
# Flush a partial batch after this long without new output so trickling
# evals still surface jobs promptly.
JOB_FLUSH_INTERVAL = 1.0


class EvalError(Exception):
    """nix-eval-jobs failed."""


class EvalOOMError(EvalError):
    """Evaluation ran out of memory: permanent failure, no retry."""


@dataclass
class EvalSettings:
    gc_roots_dir: Path
    # Overall wall-clock limit for one evaluation. A hung evaluator
    # would otherwise hold the global eval semaphore forever and wedge
    # CI for every project.
    timeout: float = 60 * 60

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
    # Extra nix-eval-jobs arguments, e.g. --option overrides.
    extra_args: list[str] = field(default_factory=list)


@dataclass
class EvalResult:
    jobs: list[NixEvalJob]


def build_eval_command(
    branch_config: BranchConfig, settings: EvalSettings
) -> list[str]:
    """The plain nix-eval-jobs invocation (without sandbox wrapping)."""
    flake = f"{branch_config.flake_dir}#{branch_config.attribute}"
    return [
        "nix-eval-jobs",
        # The service drives nix entirely through flakes; on hosts where
        # the system nix.conf hasn't enabled them (single-user installs,
        # containers) the eval would crash on the first --flake.
        "--option",
        "extra-experimental-features",
        "nix-command flakes",
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
        *settings.extra_args,
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
    ]
    if settings.nix_daemon_socket.exists():
        cmd += [
            "--ro-bind",
            "/nix/store",
            "/nix/store",
            "--ro-bind",
            "/nix/var/nix/db",
            "/nix/var/nix/db",
            "--ro-bind",
            str(settings.nix_daemon_socket),
            str(settings.nix_daemon_socket),
            # The db bind is read-only; always go through the daemon.
            "--setenv",
            "NIX_REMOTE",
            "daemon",
        ]
    else:
        # Single-user nix (no daemon): the evaluator writes the store
        # directly, so /nix must be writable inside the sandbox.
        cmd += [
            "--bind",
            "/nix",
            "/nix",
        ]
    cmd += [
        "--bind",
        str(settings.gc_roots_dir),
        str(settings.gc_roots_dir),
        "--bind",
        str(worktree_path),
        str(worktree_path),
        "--setenv",
        "HOME",
        "/tmp",  # noqa: S108
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

    With Delegate=memory on the service unit the service owns its cgroup
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


async def _drain(stream: asyncio.StreamReader) -> None:
    while await stream.read(64 * 1024):
        pass


def _is_stderr_noise(line: str) -> bool:
    """Known-harmless nix stderr noise: the sandboxed evaluator parses
    the host nix.conf with a libstore that lacks daemon-only settings,
    and concurrent evals contend on the local sqlite caches (matching
    "SQLite database" alone would also swallow corruption errors)."""
    return "warning: unknown setting" in line or (
        "SQLite database" in line and "is busy" in line
    )


async def _read_stderr_tail(
    stream: asyncio.StreamReader,
    limit: int = STDERR_TAIL_LIMIT,
    on_line: StderrLineCallback | None = None,
) -> bytes:
    """Read stderr to EOF keeping only the last `limit` bytes, dropping
    known-noise lines and live-streaming warnings/errors to the
    optional `on_line` callback."""
    buf = bytearray()
    pending = bytearray()

    async def keep(raw_line: bytes) -> bool:
        text = strip_ansi(raw_line.decode(errors="replace")).strip()
        if _is_stderr_noise(text):
            return False
        if on_line is not None and ("warning:" in text or "error:" in text):
            await on_line(text)
        return True

    while chunk := await stream.read(64 * 1024):
        pending += chunk
        *lines, rest = pending.split(b"\n")
        pending = bytearray(rest)
        # A single line larger than the tail limit (huge eval trace):
        # keep it without waiting for its newline so memory stays bounded.
        if len(pending) > 2 * limit:
            buf += pending
            pending.clear()
        for line in lines:
            if await keep(line):
                buf += line + b"\n"
        if len(buf) > 2 * limit:
            del buf[:-limit]
    if pending and await keep(bytes(pending)):
        buf += pending
    return bytes(buf[-limit:])


async def _read_job_stream(
    stdout: asyncio.StreamReader,
    jobs: list[NixEvalJob],
    parse_errors: list[str],
    on_jobs: JobBatchCallback | None,
) -> None:
    """Parse nix-eval-jobs JSON lines, flushing batches of
    JOB_BATCH_SIZE jobs to on_jobs so consumers see progress during
    large evals."""
    batch: list[NixEvalJob] = []

    async def flush() -> None:
        if on_jobs is not None and batch:
            await on_jobs(batch.copy())
            batch.clear()

    while True:
        try:
            raw = await asyncio.wait_for(stdout.readline(), timeout=JOB_FLUSH_INTERVAL)
        except TimeoutError:
            await flush()
            continue
        except ValueError:
            # One JSON line over the StreamReader limit. Drain stdout
            # so nix-eval-jobs doesn't block on the full pipe, and fail
            # the eval via parse_errors instead of crashing the reader.
            parse_errors.append(f"output line exceeds {STREAM_LIMIT} bytes")
            await _drain(stdout)
            break
        if not raw:
            break
        line = raw.decode(errors="replace").strip()
        if not line:
            continue
        try:
            job = NixEvalJobModel.validate_python(json.loads(line))
        except (json.JSONDecodeError, ValueError) as e:
            parse_errors.append(f"failed to parse line: {line}: {e}")
            continue
        jobs.append(job)
        batch.append(job)
        if len(batch) >= JOB_BATCH_SIZE:
            await flush()
    await flush()


def cgroup_oom_killed(eval_cgroup: Path | None) -> bool:
    """Whether the kernel OOM-killed anything in the eval cgroup,
    per its memory.events oom_kill counter."""
    if eval_cgroup is None:
        return False
    try:
        events = (eval_cgroup / "memory.events").read_text()
    except OSError:
        return False
    for line in events.splitlines():
        key, _, value = line.partition(" ")
        if key == "oom_kill":
            return int(value) > 0
    return False


def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the evaluator and its children (systemd-run/bwrap wrap the
    real evaluator); the process group exists because the subprocess is
    started with start_new_session=True."""
    with contextlib.suppress(ProcessLookupError):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()


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
        on_jobs: JobBatchCallback | None = None,
        on_stderr_line: StderrLineCallback | None = None,
    ) -> EvalResult:
        async with self._semaphore:
            return await self._run(
                worktree_path, branch_config, settings, on_jobs, on_stderr_line
            )

    async def _run(
        self,
        worktree_path: Path,
        branch_config: BranchConfig,
        settings: EvalSettings,
        on_jobs: JobBatchCallback | None = None,
        on_stderr_line: StderrLineCallback | None = None,
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
                on_jobs=on_jobs,
                on_stderr_line=on_stderr_line,
                eval_cgroup=eval_cgroup,
            )
        finally:
            if self.limiter is not None and eval_cgroup is not None:
                await self.limiter.cleanup(eval_cgroup)

    async def _spawn(  # noqa: PLR0913
        self,
        worktree_path: Path,
        branch_config: BranchConfig,
        settings: EvalSettings,
        preexec_fn: Callable[[], None] | None = None,
        on_jobs: JobBatchCallback | None = None,
        on_stderr_line: StderrLineCallback | None = None,
        eval_cgroup: Path | None = None,
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
            # Own process group so a timeout can kill the whole tree.
            start_new_session=True,
            limit=STREAM_LIMIT,
            # Proxy/TLS/NIX_* must reach the evaluator: flake input
            # fetching fails in proxy/custom-CA deployments otherwise.
            env={
                **passthrough_env(),
                "CLICOLOR_FORCE": "1",
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            },
        )
        assert proc.stdout is not None  # noqa: S101
        assert proc.stderr is not None  # noqa: S101

        jobs: list[NixEvalJob] = []
        parse_errors: list[str] = []

        try:
            async with asyncio.timeout(settings.timeout):
                _, stderr_bytes = await asyncio.gather(
                    _read_job_stream(proc.stdout, jobs, parse_errors, on_jobs),
                    _read_stderr_tail(proc.stderr, on_line=on_stderr_line),
                )
                returncode = await proc.wait()
        except BaseException as e:
            # Timeout, cancellation, or a reader bug: kill the evaluator
            # instead of leaking it (possibly blocked on a full pipe).
            _kill_process_tree(proc)
            await proc.wait()
            if isinstance(e, TimeoutError):
                msg = f"evaluation timed out after {settings.timeout:.0f} seconds"
                raise EvalError(msg) from None
            raise
        stderr_text = stderr_bytes.decode(errors="replace")

        if returncode != 0:
            tail = "\n".join(stderr_text.splitlines()[-50:])
            # No stderr substring match: that would misclassify evals
            # whose trace merely mentions "out of memory".
            if returncode in OOM_RETURN_CODES or cgroup_oom_killed(eval_cgroup):
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
        return EvalResult(jobs=jobs)
