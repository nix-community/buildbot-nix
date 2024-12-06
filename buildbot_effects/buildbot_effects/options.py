from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class EffectsOptions:
    secrets: Path | None = None
    path: Path = field(default_factory=lambda: Path.cwd())
    repo: str | None = ""
    rev: str | None = None
    branch: str | None = None
    url: str | None = None
    tag: str | None = None
