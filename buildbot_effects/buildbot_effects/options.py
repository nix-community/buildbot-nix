from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class EffectsOptions:
    secrets: Path | None = None
    path: Path = field(default_factory=Path.cwd)
    repo: str | None = ""
    rev: str | None = None
    branch: str | None = None
    url: str | None = None
    tag: str | None = None
    locked_url: str | None = None
    default_branch: str | None = None
    git_token_file: Path | None = None
    debug: bool = False
    extra_sandbox_paths: list[Path] = field(default_factory=list)
