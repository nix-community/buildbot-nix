"""Tests for the service's nix-eval-jobs runner (pure parts plus an
optional integration test against a real nix-eval-jobs)."""

# ruff: noqa: PLR2004 (literal values in test assertions are fine)

from __future__ import annotations

import asyncio
import json
import os
import shutil
from typing import TYPE_CHECKING

import pytest

from nixbot import nix_eval
from nixbot.memory import (
    MAX_EVAL_WORKERS,
    EvalWorkerConfig,
    MemoryInfo,
    calculate_eval_workers,
    get_memory_info,
)
from nixbot.nix_eval import (
    EvalError,
    EvalRunner,
    EvalSettings,
    build_eval_command,
    build_full_command,
    build_sandbox_command,
)
from nixbot.repo_config import BranchConfig

if TYPE_CHECKING:
    from pathlib import Path

    from nixbot.models import NixEvalJob


def test_branch_config_defaults(tmp_path: Path) -> None:
    config = BranchConfig.load(tmp_path)
    assert config.flake_dir == "."
    assert config.lock_file == "flake.lock"
    assert config.attribute == "checks"
    assert not config.effects_on_pull_requests


def test_branch_config_from_toml(tmp_path: Path) -> None:
    (tmp_path / "nixbot.toml").write_text(
        'flake_dir = "subdir"\nlock_file = "dev.lock"\nattribute = "hydraJobs"\n'
        "effects_on_pull_requests = true\n"
    )
    config = BranchConfig.load(tmp_path)
    assert config.flake_dir == "subdir"
    assert config.lock_file == "dev.lock"
    assert config.attribute == "hydraJobs"
    assert config.effects_on_pull_requests


def test_branch_config_rejects_traversal(tmp_path: Path) -> None:
    (tmp_path / "nixbot.toml").write_text('flake_dir = "../../etc"\n')
    # Falls back to defaults on invalid config.
    assert BranchConfig.load(tmp_path).flake_dir == "."


def test_branch_config_invalid_toml(tmp_path: Path) -> None:
    (tmp_path / "nixbot.toml").write_text("not toml :::")
    assert BranchConfig.load(tmp_path).flake_dir == "."


def test_eval_command(tmp_path: Path) -> None:
    settings = EvalSettings(
        gc_roots_dir=tmp_path / "gcroots", worker_count=4, max_memory_size_mib=1024
    )
    cmd = build_eval_command(BranchConfig(), settings)
    assert cmd[0] == "nix-eval-jobs"
    assert "--workers" in cmd
    assert cmd[cmd.index("--workers") + 1] == "4"
    assert cmd[cmd.index("--max-memory-size") + 1] == "1024"
    assert cmd[cmd.index("--flake") + 1] == ".#checks"
    # Flakes and nix-command must be opted-in for hosts where the
    # system nix.conf hasn't enabled them yet.
    assert "nix-command flakes" in cmd
    assert "--show-trace" not in cmd
    assert "--reference-lock-file" not in cmd

    settings.show_trace = True
    cmd = build_eval_command(
        BranchConfig(flake_dir="sub", lock_file="alt.lock", attribute="hydraJobs"),
        settings,
    )
    assert "--show-trace" in cmd
    assert cmd[cmd.index("--flake") + 1] == "sub#hydraJobs"
    assert cmd[cmd.index("--reference-lock-file") + 1] == "alt.lock"


def test_sandbox_command_mounts(tmp_path: Path) -> None:
    netrc = tmp_path / "netrc"
    netrc.write_text("machine x login y password z\n")
    socket = tmp_path / "daemon-socket"
    socket.touch()
    settings = EvalSettings(
        gc_roots_dir=tmp_path / "gcroots", netrc_file=netrc, nix_daemon_socket=socket
    )
    worktree = tmp_path / "wt"
    cmd = build_sandbox_command(worktree, settings)
    assert cmd[0] == "bwrap"
    joined = " ".join(cmd)
    assert "--ro-bind /nix/store /nix/store" in joined
    assert f"--bind {worktree} {worktree}" in joined
    assert f"--bind {tmp_path / 'gcroots'} {tmp_path / 'gcroots'}" in joined
    assert f"--ro-bind {netrc} /tmp/.netrc" in joined
    # The credentials directory must never be mounted.
    assert "CREDENTIALS_DIRECTORY" not in joined


def test_sandbox_skips_missing_daemon_socket(tmp_path: Path) -> None:
    # Single-user nix installs have no daemon socket; the sandbox must
    # not hard-bind it (bwrap aborts on missing source paths) and the
    # evaluator must not be forced through the absent daemon.
    settings = EvalSettings(
        gc_roots_dir=tmp_path / "gcroots",
        nix_daemon_socket=tmp_path / "no-such-socket",
    )
    cmd = build_sandbox_command(tmp_path / "wt", settings)
    joined = " ".join(cmd)
    assert "no-such-socket" not in joined
    assert "NIX_REMOTE" not in joined


def test_full_command_composition(tmp_path: Path) -> None:
    settings = EvalSettings(gc_roots_dir=tmp_path, sandbox=True, systemd_scope=True)
    cmd = build_full_command(tmp_path / "wt", BranchConfig(), settings)
    assert cmd[0] == "systemd-run"
    assert "bwrap" in cmd
    assert "nix-eval-jobs" in cmd

    settings = EvalSettings(gc_roots_dir=tmp_path, sandbox=False, systemd_scope=False)
    cmd = build_full_command(tmp_path / "wt", BranchConfig(), settings)
    assert cmd[0] == "nix-eval-jobs"


def test_memory_info_zfs_arc(tmp_path: Path) -> None:
    arcstats = tmp_path / "arcstats"
    arcstats.write_text("name type data\nsize 4 2147483648\n")
    info = get_memory_info(arcstats_path=arcstats)
    assert info.zfs_arc_used == 2048
    assert info.total_memory_mib > 0
    # Missing arcstats: no ZFS.
    assert get_memory_info(arcstats_path=tmp_path / "absent").zfs_arc_used == 0


def test_calculate_eval_workers() -> None:
    # Plenty of memory: CPU-bound worker count.
    config = calculate_eval_workers(
        MemoryInfo(total_memory_mib=65536, available_memory_mib=60000), cpu_count=8
    )
    assert config == EvalWorkerConfig(count=4, max_memory_mib=2048)

    # Memory-tight host: fewer/smaller workers, at least one.
    config = calculate_eval_workers(
        MemoryInfo(total_memory_mib=4096, available_memory_mib=3000), cpu_count=16
    )
    assert config.count >= 1
    assert config.max_memory_mib >= 1024

    # ZFS ARC counts as reclaimable.
    with_arc = calculate_eval_workers(
        MemoryInfo(
            total_memory_mib=16384, available_memory_mib=4096, zfs_arc_used=8192
        ),
        cpu_count=16,
    )
    without_arc = calculate_eval_workers(
        MemoryInfo(total_memory_mib=16384, available_memory_mib=4096), cpu_count=16
    )
    assert with_arc.count >= without_arc.count

    # The memory-refit branch must not exceed the global worker cap on
    # many-core, memory-limited hosts.
    config = calculate_eval_workers(
        MemoryInfo(total_memory_mib=49152, available_memory_mib=44000), cpu_count=64
    )
    assert config.count <= MAX_EVAL_WORKERS


@pytest.mark.skipif(
    shutil.which("nix-eval-jobs") is None or shutil.which("nix") is None,
    reason="nix-eval-jobs not available",
)
def test_eval_runner_integration(tmp_path: Path) -> None:
    flake = tmp_path / "repo"
    flake.mkdir()
    (flake / "flake.nix").write_text(
        """
        {
          outputs = { self }: {
            checks.x86_64-linux.ok = builtins.warn "eval warning here" (
              derivation {
                name = "ok";
                system = "x86_64-linux";
                builder = "/bin/sh";
                args = [ "-c" "echo ok > $out" ];
              }
            );
          };
        }
        """
    )
    settings = EvalSettings(
        gc_roots_dir=tmp_path / "gcroots",
        sandbox=False,
        systemd_scope=False,
    )
    seen: list[str] = []

    async def on_line(line: str) -> None:
        seen.append(line)

    result = asyncio.run(
        EvalRunner().run(flake, BranchConfig(), settings, on_stderr_line=on_line)
    )
    assert len(result.jobs) == 1
    job = result.jobs[0]
    assert job.attr == "x86_64-linux.ok"  # type: ignore[union-attr]
    assert any("eval warning here" in line for line in seen)


def test_stderr_noise_filtered_and_warnings_reach_callback(tmp_path: Path) -> None:
    """Noise stays out of both the tail and the callback; warning and
    error lines reach the callback ANSI-stripped, progress output does
    not."""
    script = tmp_path / "warn.sh"
    script.write_text(
        "#!/bin/sh\n"
        "printf '\\033[35;1mwarning:\\033[0m unable to download foo\\n' >&2\n"
        "echo 'plain progress output' >&2\n"
        "echo \"warning: unknown setting 'allowed-users'\" >&2\n"
        "echo \"warning: SQLite database '/nix/var/nix/db/db.sqlite' is busy\" >&2\n"
        "echo 'error: something broke' >&2\n"
    )
    script.chmod(0o755)
    seen: list[str] = []

    async def on_line(line: str) -> None:
        seen.append(line)

    async def run() -> bytes:
        proc = await asyncio.create_subprocess_exec(
            str(script), stderr=asyncio.subprocess.PIPE
        )
        assert proc.stderr is not None
        tail = await nix_eval._read_stderr_tail(proc.stderr, on_line=on_line)  # noqa: SLF001
        await proc.wait()
        return tail

    tail = asyncio.run(run())
    assert seen == [
        "warning: unable to download foo",
        "error: something broke",
    ]
    assert b"unknown setting" not in tail
    assert b"is busy" not in tail
    assert b"plain progress output" in tail
    assert b"error: something broke" in tail


def test_stderr_tail_is_bounded(tmp_path: Path) -> None:
    script = tmp_path / "noisy.sh"
    script.write_text(
        "#!/bin/sh\n"
        "yes FILLER | head -c 3145728 >&2\n"
        "echo 'final error marker' >&2\n"
        "exit 1\n"
    )
    script.chmod(0o755)

    async def run() -> None:
        proc = await asyncio.create_subprocess_exec(
            str(script), stderr=asyncio.subprocess.PIPE
        )
        assert proc.stderr is not None
        tail = await nix_eval._read_stderr_tail(proc.stderr)  # noqa: SLF001
        await proc.wait()
        assert len(tail) <= nix_eval.STDERR_TAIL_LIMIT
        assert tail.endswith(b"final error marker\n")

    asyncio.run(run())


def test_eval_runner_times_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(nix_eval, "build_full_command", lambda *_args: ["sleep", "60"])
    settings = EvalSettings(
        gc_roots_dir=tmp_path / "gcroots",
        sandbox=False,
        systemd_scope=False,
        timeout=0.2,
    )
    with pytest.raises(EvalError, match="timed out"):
        asyncio.run(EvalRunner().run(tmp_path, BranchConfig(), settings))


def test_cgroup_limiter_create_handles_missing_delegation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No cgroup.controllers under the fake root: create() must return None
    # instead of raising.
    content = "0::/system.slice/test.service\n"

    def fake_read_text(self: Path, *_args: object, **_kwargs: object) -> str:
        if str(self) == "/proc/self/cgroup":
            return content
        msg = "no delegation"
        raise OSError(msg)

    monkeypatch.setattr(nix_eval.Path, "read_text", fake_read_text)
    assert nix_eval.CgroupLimiter.create() is None


def test_cgroup_limiter_eval_cgroup_lifecycle(tmp_path: Path) -> None:
    # Filesystem-level behavior with a plain directory standing in for the
    # delegated subtree; cgroup.kill writes fail and are tolerated.
    limiter = nix_eval.CgroupLimiter(tmp_path)
    path = limiter.new_eval_cgroup(123)
    assert (path / "memory.max").read_text() == str(123 * 1024 * 1024)
    (path / "memory.max").unlink()
    asyncio.run(limiter.cleanup(path))
    assert not path.exists()


def test_eval_runner_handles_long_json_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # nix-eval-jobs emits one JSON object per line including the full
    # neededBuilds closure; such lines easily exceed asyncio's 64 KiB
    # default StreamReader limit and must not crash the evaluation.
    job = {
        "attr": "big",
        "attrPath": ["big"],
        "cacheStatus": "notBuilt",
        "neededBuilds": [f"/nix/store/{i:056d}-dep.drv" for i in range(2000)],
        "neededSubstitutes": [],
        "drvPath": "/nix/store/big.drv",
        "name": "big",
        "outputs": {"out": "/nix/store/big-out"},
        "system": "x86_64-linux",
    }
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps(job) + "\n")
    assert payload.stat().st_size > 64 * 1024

    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "nix-eval-jobs"
    fake.write_text(f'#!/bin/sh\ncat "{payload}"\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

    settings = EvalSettings(
        gc_roots_dir=tmp_path / "gcroots", sandbox=False, systemd_scope=False
    )
    result = asyncio.run(EvalRunner().run(tmp_path, BranchConfig(), settings))
    assert len(result.jobs) == 1
    assert result.jobs[0].attr == "big"


def test_eval_runner_streams_job_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Large evals (thousands of attrs) must reach the database in
    # batches while nix-eval-jobs is still running, not as one huge
    # insert at the end.
    lines = [
        json.dumps(
            {
                "attr": f"job{i}",
                "attrPath": [f"job{i}"],
                "cacheStatus": "notBuilt",
                "neededBuilds": [],
                "neededSubstitutes": [],
                "drvPath": f"/nix/store/job{i}.drv",
                "name": f"job{i}",
                "outputs": {"out": f"/nix/store/job{i}-out"},
                "system": "x86_64-linux",
            }
        )
        for i in range(250)
    ]
    payload = tmp_path / "payload.json"
    payload.write_text("\n".join(lines) + "\n")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "nix-eval-jobs"
    fake.write_text(f'#!/bin/sh\ncat "{payload}"\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

    batches: list[int] = []

    async def on_jobs(jobs: list[NixEvalJob]) -> None:
        batches.append(len(jobs))

    settings = EvalSettings(
        gc_roots_dir=tmp_path / "gcroots", sandbox=False, systemd_scope=False
    )
    result = asyncio.run(
        EvalRunner().run(tmp_path, BranchConfig(), settings, on_jobs=on_jobs)
    )
    assert len(result.jobs) == 250
    assert sum(batches) == 250
    assert len(batches) > 1  # streamed in multiple batches
    assert max(batches) <= 100


def test_eval_runner_flushes_partial_batch_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A slow eval that trickles jobs must not sit on a partial batch
    # until the size threshold; the flush interval bounds the latency.
    job = json.dumps(
        {
            "attr": "first",
            "attrPath": ["first"],
            "cacheStatus": "notBuilt",
            "neededBuilds": [],
            "neededSubstitutes": [],
            "drvPath": "/nix/store/first.drv",
            "name": "first",
            "outputs": {"out": "/nix/store/first-out"},
            "system": "x86_64-linux",
        }
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "nix-eval-jobs"
    second = job.replace("first", "second")
    fake.write_text(f"#!/bin/sh\necho '{job}'\nsleep 3\necho '{second}'\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

    batches: list[int] = []

    async def on_jobs(jobs: list[NixEvalJob]) -> None:
        batches.append(len(jobs))

    settings = EvalSettings(
        gc_roots_dir=tmp_path / "gcroots", sandbox=False, systemd_scope=False
    )
    result = asyncio.run(
        EvalRunner().run(tmp_path, BranchConfig(), settings, on_jobs=on_jobs)
    )
    assert len(result.jobs) == 2
    # First job flushed alone during the sleep, second after EOF.
    assert batches == [1, 1]


def test_cgroup_limiter_retries_subtree_enable(tmp_path: Path) -> None:
    # +memory could not be enabled at startup (e.g. an ExecStartPost
    # health check still occupied the unit cgroup): the first eval
    # retries instead of permanently losing the memory cap.
    limiter = nix_eval.CgroupLimiter(tmp_path, subtree_enabled=False)
    path = limiter.new_eval_cgroup(64)
    assert (tmp_path / "cgroup.subtree_control").read_text() == "+memory"
    assert (path / "memory.max").read_text() == str(64 * 1024 * 1024)


def test_eval_failure_with_oom_text_in_stderr_is_not_oom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failed eval whose trace merely mentions "out of memory" (e.g. a
    # package description or nested builder output) must not be
    # classified as a permanent cgroup OOM.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "nix-eval-jobs"
    fake.write_text(
        "#!/bin/sh\n"
        'echo "error: package foo: never run out of memory again" >&2\n'
        'echo "error: assertion failed" >&2\n'
        "exit 1\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

    settings = EvalSettings(
        gc_roots_dir=tmp_path / "gcroots", sandbox=False, systemd_scope=False
    )
    with pytest.raises(EvalError) as excinfo:
        asyncio.run(EvalRunner().run(tmp_path, BranchConfig(), settings))
    assert not isinstance(excinfo.value, nix_eval.EvalOOMError)


def test_cgroup_oom_killed_reads_memory_events(tmp_path: Path) -> None:
    assert not nix_eval.cgroup_oom_killed(None)
    assert not nix_eval.cgroup_oom_killed(tmp_path)  # no memory.events
    (tmp_path / "memory.events").write_text("low 0\nhigh 2\nmax 5\noom 0\noom_kill 0\n")
    assert not nix_eval.cgroup_oom_killed(tmp_path)
    (tmp_path / "memory.events").write_text("low 0\noom 1\noom_kill 3\n")
    assert nix_eval.cgroup_oom_killed(tmp_path)


def test_eval_runner_proxy_env_reaches_evaluator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Proxy/TLS settings must reach nix-eval-jobs or evaluation breaks
    # in proxy/custom-CA deployments. The fake echoes its environment
    # into stderr, which the EvalError carries.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "nix-eval-jobs"
    fake.write_text(
        '#!/bin/sh\necho "proxy=$https_proxy ca=$NIX_SSL_CERT_FILE" >&2\nexit 1\n'
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("https_proxy", "http://proxy:3128")
    monkeypatch.setenv("NIX_SSL_CERT_FILE", "/etc/ssl/ca.pem")

    settings = EvalSettings(
        gc_roots_dir=tmp_path / "gcroots", sandbox=False, systemd_scope=False
    )
    with pytest.raises(EvalError) as excinfo:
        asyncio.run(EvalRunner().run(tmp_path, BranchConfig(), settings))
    assert "proxy=http://proxy:3128 ca=/etc/ssl/ca.pem" in str(excinfo.value)


def test_eval_fails_cleanly_on_line_over_stream_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A JSON line over the StreamReader limit used to raise ValueError
    # out of the reader, leaking a running nix-eval-jobs blocked on the
    # full pipe; it must surface as a clean EvalError instead.
    monkeypatch.setattr(nix_eval, "STREAM_LIMIT", 64 * 1024)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "nix-eval-jobs"
    fake.write_text("#!/bin/sh\nhead -c 200000 /dev/zero | tr '\\0' 'x'\necho\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

    settings = EvalSettings(
        gc_roots_dir=tmp_path / "gcroots", sandbox=False, systemd_scope=False
    )
    with pytest.raises(EvalError):
        asyncio.run(EvalRunner().run(tmp_path, BranchConfig(), settings))
