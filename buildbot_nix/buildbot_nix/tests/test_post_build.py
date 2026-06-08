"""Post-build step tests (interpolation + execution)."""

# ruff: noqa: PLR2004 (literal values in test assertions are fine)
from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

import pytest

from buildbot_nix.config import Interpolate, PostBuildStep
from buildbot_nix.events import ChangeEvent, RepoInfo
from buildbot_nix.post_build import (
    InterpolationError,
    build_props,
    interpolate,
    run_post_build_step,
    run_post_build_steps,
)

from .support import mk_job

if TYPE_CHECKING:
    from pathlib import Path

PROPS = {"attr": "x86_64-linux.foo", "out_path": "/nix/store/abc-foo"}


def test_build_props_exposes_change_context() -> None:
    event = ChangeEvent(
        repo=RepoInfo(
            id=1,
            key="github/acme/proj",
            name="acme/proj",
            owner="acme",
            repo="proj",
            forge="github",
            clone_url="https://example.com/acme/proj.git",
            default_branch="main",
        ),
        branch="feature",
        commit_sha="deadbeef",
        pr_number=42,
    )
    job = mk_job()
    props = build_props(event, job)
    assert props["project"] == "acme/proj"
    assert props["branch"] == "feature"
    assert props["revision"] == "deadbeef"
    assert props["pr_number"] == "42"
    assert props["default_branch"] == "main"
    assert props["out_link"] == "result-foo"
    # The executor percent-encodes the attr in the out-link name;
    # out_link must match it or upload steps reference a missing path.
    assert (
        build_props(event, mk_job('with"quotes'))["out_link"] == "result-with%22quotes"
    )
    assert (
        build_props(ChangeEvent(repo=event.repo, branch="main", commit_sha="x"), job)[
            "pr_number"
        ]
        == ""
    )


def test_interpolate_plain_string() -> None:
    assert interpolate("cachix", PROPS) == "cachix"


def test_interpolate_prop() -> None:
    assert (
        interpolate(Interpolate("result-%(prop:attr)s"), PROPS)
        == "result-x86_64-linux.foo"
    )


def test_interpolate_unknown_prop() -> None:
    with pytest.raises(InterpolationError, match="unknown build property"):
        interpolate(Interpolate("%(prop:nope)s"), PROPS)


def test_interpolate_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "cachix-auth-token").write_text("sekrit\n")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    assert interpolate(Interpolate("%(secret:cachix-auth-token)s"), PROPS) == "sekrit"


def test_interpolate_secret_without_credentials_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    with pytest.raises(InterpolationError, match="CREDENTIALS_DIRECTORY"):
        interpolate(Interpolate("%(secret:x)s"), PROPS)


def test_run_steps_success_and_env(tmp_path: Path) -> None:
    step = PostBuildStep(
        name="upload",
        environment={"TARGET": Interpolate("%(prop:out_path)s")},
        command=["sh", "-c", "echo uploading $TARGET"],
    )
    results = asyncio.run(run_post_build_steps([step], PROPS, tmp_path))
    assert len(results) == 1
    assert results[0].success
    assert "uploading /nix/store/abc-foo" in results[0].output


def test_run_steps_warn_only_failure_continues(tmp_path: Path) -> None:
    steps = [
        PostBuildStep(
            name="cachix push",
            environment={},
            command=["sh", "-c", "echo cachix broken >&2; exit 1"],
            warn_only=True,
        ),
        PostBuildStep(name="after", environment={}, command=["true"]),
    ]
    results = asyncio.run(run_post_build_steps(steps, PROPS, tmp_path))
    assert len(results) == 2
    assert not results[0].success
    assert not results[0].failed  # warn-only: does not fail the attribute
    assert "cachix broken" in results[0].output
    assert results[1].success


def test_run_steps_hard_failure_continues(tmp_path: Path) -> None:
    # flunkOnFailure semantics: a hard failure fails the attribute but
    # the remaining steps still run (buildbot never set haltOnFailure).
    steps = [
        PostBuildStep(name="boom", environment={}, command=["false"]),
        PostBuildStep(name="after", environment={}, command=["true"]),
    ]
    results = asyncio.run(run_post_build_steps(steps, PROPS, tmp_path))
    assert len(results) == 2
    assert results[0].failed
    assert results[1].success


def test_run_steps_inherit_service_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Tools like cachix need HOME/XDG_*; the service environment must
    # be inherited, with step.environment overlaid.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("INHERITED_VAR", "from-service")
    step = PostBuildStep(
        name="env",
        environment={"EXTRA": "x"},
        command=["sh", "-c", "echo home=$HOME inherited=$INHERITED_VAR extra=$EXTRA"],
    )
    results = asyncio.run(run_post_build_steps([step], PROPS, tmp_path))
    assert results[0].success
    assert f"home={tmp_path}" in results[0].output
    assert "inherited=from-service" in results[0].output
    assert "extra=x" in results[0].output


def test_run_step_timeout_kills_process(tmp_path: Path) -> None:
    # A hung upload step must not block the attribute forever: the
    # step is killed (whole process group) and reported as a failure.
    pidfile = tmp_path / "pid"
    step = PostBuildStep(
        name="hang",
        environment={},
        command=["sh", "-c", f"echo $$ > {pidfile}; sleep 60"],
    )
    result = asyncio.run(run_post_build_step(step, PROPS, tmp_path, step_timeout=0.2))
    assert not result.success
    assert "timed out" in result.output
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_run_step_cancel_kills_process(tmp_path: Path) -> None:
    # Build cancellation must kill the running step's process group.
    pidfile = tmp_path / "pid"
    step = PostBuildStep(
        name="hang",
        environment={},
        command=["sh", "-c", f"echo $$ > {pidfile}; sleep 60"],
    )

    async def run() -> None:
        task = asyncio.create_task(run_post_build_step(step, PROPS, tmp_path))
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_run_step_missing_binary_is_failure(tmp_path: Path) -> None:
    # A nonexistent step binary is a post-build failure result, not an
    # unhandled internal error.
    step = PostBuildStep(
        name="missing",
        environment={},
        command=["/nonexistent/upload-tool"],
    )
    result = asyncio.run(run_post_build_step(step, PROPS, tmp_path))
    assert not result.success
    assert "upload-tool" in result.output
