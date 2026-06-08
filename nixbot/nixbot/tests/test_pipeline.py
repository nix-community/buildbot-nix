"""End-to-end pipeline tests against a real local nix store:
eval fan-out, cached skip, failure aggregation."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from nixbot.executor import (
    BuildSettings,
    FairScheduler,
    LogWriter,
    NixBuildExecutor,
    read_log,
)
from nixbot.nix_eval import EvalRunner, EvalSettings
from nixbot.repo_config import BranchConfig
from nixbot.scheduler import (
    AttributeStatus,
    BuildOutcome,
    JobScheduler,
    ScheduleResult,
)

if TYPE_CHECKING:
    from pathlib import Path

    from nixbot.models import NixEvalJobSuccess

pytestmark = pytest.mark.skipif(
    shutil.which("nix-eval-jobs") is None or shutil.which("nix") is None,
    reason="nix tooling not available",
)


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
      mk = name: script: derivation {{
        inherit name system;
        builder = "/bin/sh";
        args = [ "-c" script ];
      }};
      base = mk "base-{salt}" "echo base > $out";
    in {{
      checks.${{system}} = {{
        inherit base;
        dependent = mk "dependent-{salt}" "cat ${{base}} > $out; echo dep >> $out";
        broken = mk "broken-{salt}" "echo build error details >&2; exit 1";
        brokendep = mk "brokendep-{salt}" "cat ${{mk "broken2-{salt}" "exit 1"}} > $out";
        evalfail = throw "deliberate eval failure";
      }};
    }};
}}
"""


class PipelineExecutor:
    """Adapts NixBuildExecutor to the scheduler's BuildExecutor protocol."""

    def __init__(self, cwd: Path, log_dir: Path) -> None:
        self.inner = NixBuildExecutor(
            FairScheduler(2), BuildSettings(log_dir=log_dir, timeout=300)
        )
        self.cwd = cwd
        self.log_dir = log_dir
        self.built: list[str] = []

    async def build(self, job: NixEvalJobSuccess) -> BuildOutcome:
        self.built.append(job.attr)
        writer = LogWriter(path=self.log_dir / f"{job.attr}.zst")
        outcome = await self.inner.build_attribute("test", job, writer, self.cwd)
        await writer.close()
        return outcome


@pytest.fixture(scope="module")
def flake(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("flake")
    salt = path.name  # unique store paths per test session
    (path / "flake.nix").write_text(
        FLAKE_TEMPLATE.format(system=current_system(), salt=salt)
    )
    return path


def run_pipeline(flake: Path, log_dir: Path) -> tuple[ScheduleResult, PipelineExecutor]:
    async def run() -> tuple[ScheduleResult, PipelineExecutor]:
        settings = EvalSettings(
            gc_roots_dir=log_dir / "gcroots", sandbox=False, systemd_scope=False
        )
        eval_result = await EvalRunner().run(flake, BranchConfig(), settings)
        executor = PipelineExecutor(flake, log_dir)
        scheduler = JobScheduler(executor, [current_system()])
        return await scheduler.run(eval_result.jobs), executor

    return asyncio.run(run())


def test_full_pipeline(flake: Path, tmp_path: Path) -> None:
    result, executor = run_pipeline(flake, tmp_path)
    system = current_system()
    statuses = {r.attr: r.status for r in result.results}

    # Eval fan-out: every attribute got its own record.
    assert set(statuses) == {
        f"{system}.base",
        f"{system}.dependent",
        f"{system}.broken",
        f"{system}.brokendep",
        f"{system}.evalfail",
    }

    # Failure aggregation: eval failure, build failure, dependency failure.
    assert statuses[f"{system}.evalfail"] == AttributeStatus.failed_eval
    assert statuses[f"{system}.broken"] == AttributeStatus.failed
    assert statuses[f"{system}.brokendep"] in {
        AttributeStatus.failed,
        AttributeStatus.dependency_failed,
    }
    assert not result.success

    # Successful chain really built in the local store.
    assert statuses[f"{system}.base"] in {
        AttributeStatus.succeeded,
        AttributeStatus.skipped_local,
    }
    assert statuses[f"{system}.dependent"] in {
        AttributeStatus.succeeded,
        AttributeStatus.skipped_local,
    }

    # Real build log captured for the broken attribute.
    if f"{system}.broken" in executor.built:
        log = read_log(tmp_path / f"{system}.broken.zst")
        assert b"build error details" in log

    # Error text preserved for the eval failure.
    errors = {r.attr: r.error for r in result.results}
    assert "deliberate eval failure" in (errors[f"{system}.evalfail"] or "")


def test_cached_skip_on_second_run(flake: Path, tmp_path: Path) -> None:
    # First run built base/dependent (test ordering within module);
    # a second eval marks them local and the scheduler skips them.
    result, executor = run_pipeline(flake, tmp_path)
    system = current_system()
    statuses = {r.attr: r.status for r in result.results}
    assert statuses[f"{system}.base"] == AttributeStatus.skipped_local
    assert statuses[f"{system}.dependent"] == AttributeStatus.skipped_local
    assert f"{system}.base" not in executor.built
    assert f"{system}.dependent" not in executor.built
    # Skipped jobs still expose out paths for gcroots/outputs updates.
    skipped = dict(result.skipped_out_paths)
    assert skipped[f"{system}.base"].startswith("/nix/store/")
