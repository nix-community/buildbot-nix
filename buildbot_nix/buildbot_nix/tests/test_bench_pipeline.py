"""Full-pipeline stress benchmark: a generated flake with many
attributes goes through real nix-eval-jobs, the dependency scheduler,
and real nix builds, to find where large flakes break the system
(eval streaming, closure computation, build fan-out, log writing).

Opt-in via BENCH_ATTRS (skipped otherwise): builds really run, which
is far too slow for the regular suite. xdist disables pytest-benchmark
timing, so disable it for real numbers:

    BENCH_ATTRS=5000 pytest -p no:xdist \\
        buildbot_nix/tests/test_bench_pipeline.py
"""

from __future__ import annotations

import asyncio
import os
import resource
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from buildbot_nix.executor import (
    BuildSettings,
    FairScheduler,
    LogWriter,
    NixBuildExecutor,
)
from buildbot_nix.nix_eval import EvalRunner, EvalSettings
from buildbot_nix.repo_config import BranchConfig
from buildbot_nix.scheduler import AttributeStatus, JobScheduler, ScheduleResult

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_benchmark.fixture import BenchmarkFixture

    from buildbot_nix.models import NixEvalJobSuccess
    from buildbot_nix.scheduler import BuildOutcome

pytestmark = [
    pytest.mark.skipif(
        "BENCH_ATTRS" not in os.environ,
        reason="benchmark is opt-in: set BENCH_ATTRS",
    ),
    pytest.mark.skipif(
        shutil.which("nix-eval-jobs") is None or shutil.which("nix") is None,
        reason="nix tooling not available",
    ),
]

NUM_ATTRS = int(os.environ.get("BENCH_ATTRS", "200"))
BUILD_CONCURRENCY = int(os.environ.get("BENCH_CONCURRENCY", "16"))
# Every CHAIN_STRIDE-th attribute depends on its predecessor so the
# scheduler exercises dependency ordering, not just flat fan-out.
CHAIN_STRIDE = 10


# The benchmark measures the engine, not the network: salted drvs
# always miss binary caches (--check-cache-status would issue
# thousands of sequential narinfo lookups), and remote builders would
# add ssh round-trips per trivial build.
LOCAL_ONLY = [
    "--option",
    "substituters",
    "",
    "--option",
    "builders",
    "",
    "--option",
    "max-jobs",
    "auto",
]


def current_system() -> str:
    return subprocess.run(
        ["nix", "eval", "--raw", "--impure", "--expr", "builtins.currentSystem"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


FLAKE_TEMPLATE = """
{{
  outputs = {{ self }}:
    let
      system = "{system}";
      mk = name: deps: derivation {{
        inherit name system;
        builder = "/bin/sh";
        args = [ "-c" "echo ${{toString deps}} $name > $out" ];
      }};
      base = mk "base-{salt}" [ ];
      # No modulo in nix builtins: i is a multiple of n iff i / n * n == i.
      pkg = i:
        let
          deps =
            if i > 0 && i / {chain_stride} * {chain_stride} == i
            then [ (pkg (i - 1)) ]
            else [ base ];
        in mk "pkg-{salt}-${{toString i}}" deps;
    in {{
      checks.${{system}} = builtins.listToAttrs (builtins.genList (i: {{
        name = "pkg-${{toString i}}";
        value = pkg i;
      }}) {num_attrs});
    }};
}}
"""


class BenchExecutor:
    """Adapts NixBuildExecutor to the scheduler's BuildExecutor protocol."""

    def __init__(self, cwd: Path, log_dir: Path) -> None:
        self.inner = NixBuildExecutor(
            FairScheduler(BUILD_CONCURRENCY),
            BuildSettings(log_dir=log_dir, timeout=300, extra_args=LOCAL_ONLY),
        )
        self.cwd = cwd
        self.log_dir = log_dir

    async def build(self, job: NixEvalJobSuccess) -> BuildOutcome:
        writer = LogWriter(path=self.log_dir / f"{job.attr}.zst")
        outcome = await self.inner.build_attribute("bench", job, writer, self.cwd)
        await writer.close()
        return outcome


@pytest.fixture
def flake(tmp_path: Path) -> Path:
    (tmp_path / "flake.nix").write_text(
        FLAKE_TEMPLATE.format(
            system=current_system(),
            salt=tmp_path.name,  # unique store paths: builds really run
            num_attrs=NUM_ATTRS,
            chain_stride=CHAIN_STRIDE,
        )
    )
    return tmp_path


@pytest.mark.timeout(1800)
def test_bench_full_pipeline(
    benchmark: BenchmarkFixture, flake: Path, tmp_path: Path
) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    async def pipeline() -> ScheduleResult:
        settings = EvalSettings(
            gc_roots_dir=tmp_path / "gcroots",
            sandbox=False,
            systemd_scope=False,
            extra_args=LOCAL_ONLY,
        )
        eval_result = await EvalRunner().run(flake, BranchConfig(), settings)
        scheduler = JobScheduler(BenchExecutor(flake, log_dir), [current_system()])
        return await scheduler.run(eval_result.jobs)

    # One round: derivations are salted per run and end up in the nix
    # store, so repeated rounds would only measure the cached-skip path.
    result = benchmark.pedantic(lambda: asyncio.run(pipeline()), rounds=1, iterations=1)
    benchmark.extra_info["attrs"] = NUM_ATTRS
    benchmark.extra_info["max_rss_mib"] = (
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    )

    assert result.success
    statuses = {r.attr: r.status for r in result.results}
    assert len(statuses) == NUM_ATTRS
    assert set(statuses.values()) <= {
        AttributeStatus.succeeded,
        AttributeStatus.skipped_local,
    }
