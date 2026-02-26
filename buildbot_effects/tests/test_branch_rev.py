"""Regression test for https://github.com/nix-community/buildbot-nix/issues/583

When --branch is specified without --rev, effects_args() should resolve
the rev from the tip of that branch, not from HEAD of the current checkout.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from buildbot_effects import effects_args
from buildbot_effects.options import EffectsOptions

if TYPE_CHECKING:
    from pathlib import Path


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def test_branch_resolves_to_branch_tip(tmp_path: Path) -> None:
    """--branch should resolve rev to the tip of that branch, not HEAD."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@test")

    # Initial commit on main
    (repo / "file.txt").write_text("main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    main_rev = _git(repo, "rev-parse", "HEAD")

    # Create a feature branch with a new commit
    _git(repo, "checkout", "-b", "feature")
    (repo / "file.txt").write_text("feature")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feature commit")
    feature_rev = _git(repo, "rev-parse", "HEAD")

    # Go back to main — HEAD is now main_rev
    _git(repo, "checkout", "main")
    assert _git(repo, "rev-parse", "HEAD") == main_rev

    # Ask for --branch=feature without --rev: should get feature_rev, not main_rev
    opts = EffectsOptions(path=repo, branch="feature")
    result = effects_args(opts)

    assert result["rev"] == feature_rev, (
        f"Expected rev from 'feature' branch ({feature_rev[:7]}), "
        f"got HEAD ({result['rev'][:7]})"
    )
    assert result["branch"] == "feature"
