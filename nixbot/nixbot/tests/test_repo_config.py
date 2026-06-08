"""Per-repository config file loading.

Repositories migrating from buildbot-nix still carry a
`buildbot-nix.toml`; it must keep working until renamed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nixbot.repo_config import CONFIG_FILENAMES, BranchConfig

if TYPE_CHECKING:
    from pathlib import Path


def test_load_nixbot_toml(tmp_path: Path) -> None:
    (tmp_path / "nixbot.toml").write_text('attribute = "hydraJobs"')
    assert BranchConfig.load(tmp_path).attribute == "hydraJobs"


def test_load_legacy_buildbot_nix_toml(tmp_path: Path) -> None:
    (tmp_path / "buildbot-nix.toml").write_text('attribute = "hydraJobs"')
    assert BranchConfig.load(tmp_path).attribute == "hydraJobs"


def test_nixbot_toml_wins_over_legacy(tmp_path: Path) -> None:
    (tmp_path / "nixbot.toml").write_text('attribute = "new"')
    (tmp_path / "buildbot-nix.toml").write_text('attribute = "old"')
    assert BranchConfig.load(tmp_path).attribute == "new"


def test_missing_files_default(tmp_path: Path) -> None:
    assert BranchConfig.load(tmp_path).attribute == "checks"


def test_config_filenames_order() -> None:
    assert CONFIG_FILENAMES == ("nixbot.toml", "buildbot-nix.toml")
