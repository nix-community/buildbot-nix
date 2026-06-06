"""Tests for engine post-build steps (interpolation + execution)."""

# ruff: noqa: PLR2004 (literal values in test assertions are fine)
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from buildbot_nix.config import Interpolate, PostBuildStep
from buildbot_nix.post_build import (
    InterpolationError,
    interpolate,
    run_post_build_steps,
)

if TYPE_CHECKING:
    from pathlib import Path

PROPS = {"attr": "x86_64-linux.foo", "out_path": "/nix/store/abc-foo"}


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
