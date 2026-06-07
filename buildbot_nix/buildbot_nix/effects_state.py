"""Hercules state API for effects.

Effects persist small files between runs (`getStateFile`/
`putStateFile` in hercules-ci-effects: nixops state, ssh known
hosts). The agent proxies these to hercules-ci; we serve them from
the state directory, scoped per project, authorized by a per-run
bearer token that the engine mints for each effect invocation.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from pathlib import Path


class TaskTokens:
    """In-memory per-run tokens; an effect only runs while the engine
    is up, so restart-safety is not needed."""

    def __init__(self) -> None:
        self._tokens: dict[str, int] = {}

    def issue(self, project_id: int) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens[token] = project_id
        return token

    def revoke(self, token: str) -> None:
        self._tokens.pop(token, None)

    def project_for(self, token: str) -> int | None:
        return self._tokens.get(token)


def state_file_path(state_dir: Path, project_id: int, name: str) -> Path:
    """State names come from untrusted flakes; percent-encode so they
    cannot escape the per-project directory. quote() keeps dots, so
    "." and ".." (the directory and its parent) need rejecting."""
    if name in {".", ".."}:
        msg = f"invalid state name: {name!r}"
        raise ValueError(msg)
    return state_dir / "effects-state" / str(project_id) / quote(name, safe="")
