"""Per-repository configuration (`buildbot-nix.toml`), read from the
build worktree instead of via a buildbot remote command."""

from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING, Self

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from pathlib import Path


class RepoConfigError(Exception):
    pass


def _validate_flake_dir(flake_dir: str, repo_root: Path) -> None:
    """Validate that flake_dir is a safe relative path within the repo root."""
    resolved = (repo_root / flake_dir).resolve()
    if ":" in flake_dir or not resolved.is_relative_to(repo_root.resolve()):
        msg = f"Invalid flake_dir {flake_dir}"
        raise RepoConfigError(msg)


class BranchConfig(BaseModel):
    flake_dir: str = "."
    lock_file: str = "flake.lock"
    attribute: str = "checks"
    effects_on_pull_requests: bool = False
    effects_branches: list[str] = []

    @classmethod
    def load(cls, repo_root: Path) -> Self:
        """Read `buildbot-nix.toml` from a checkout; defaults on absence
        or invalid content (matching the buildbot-era behavior)."""
        path = repo_root / "buildbot-nix.toml"
        try:
            data = tomllib.loads(path.read_text())
        except FileNotFoundError:
            return cls()
        except (OSError, tomllib.TOMLDecodeError):
            return cls()
        try:
            config = cls.model_validate(data)
            _validate_flake_dir(config.flake_dir, repo_root)
        except (ValidationError, RepoConfigError):
            return cls()
        return config
