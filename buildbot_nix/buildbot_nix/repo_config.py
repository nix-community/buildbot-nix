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
    def loads(cls, text: str | None) -> Self:
        """Parse `buildbot-nix.toml` content (e.g. read from a git ref);
        defaults on absence or invalid content."""
        if text is None:
            return cls()
        try:
            return cls.model_validate(tomllib.loads(text))
        except (tomllib.TOMLDecodeError, ValidationError):
            return cls()

    @classmethod
    def load(cls, repo_root: Path) -> Self:
        """Read `buildbot-nix.toml` from a checkout; defaults on absence
        or invalid content (matching the buildbot-era behavior)."""
        try:
            text = (repo_root / "buildbot-nix.toml").read_text()
        except OSError:
            return cls()
        config = cls.loads(text)
        try:
            _validate_flake_dir(config.flake_dir, repo_root)
        except RepoConfigError:
            return cls()
        return config
