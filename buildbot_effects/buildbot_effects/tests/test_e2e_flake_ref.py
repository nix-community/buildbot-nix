"""End-to-end test: buildbot-effects list on a git+file:// flake reference.

Verifies that the CLI can resolve a flake ref, fetch metadata, and
evaluate effects without a local checkout — the store path has no .git.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

# Minimal flake that defines a herculesCI output with named effects.
# No external dependencies — uses builtins only.
FLAKE_NIX = """\
{
  description = "Test flake for buildbot-effects";
  outputs = { self, ... }: {
    herculesCI = args: {
      onPush.default.outputs.effects = {
        deploy = {
          effectScript = "echo deploying";
        };
        notify = {
          effectScript = "echo notifying";
        };
      };
    };
  };
}
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


@pytest.fixture
def flake_repo(tmp_path: Path) -> Path:
    """Create a git repo with a minimal flake that has effects."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@test")

    (repo / "flake.nix").write_text(FLAKE_NIX)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


def test_list_via_flake_ref(flake_repo: Path) -> None:
    """buildbot-effects list <git+file://repo> should work end-to-end."""
    flake_ref = f"git+file://{flake_repo}"

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.argv = ['buildbot-effects'] + sys.argv[1:]; "
            "from buildbot_effects.cli import main; main()",
            "list",
            flake_ref,
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    effects = json.loads(result.stdout)
    assert sorted(effects) == ["deploy", "notify"]
