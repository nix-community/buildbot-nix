"""Outputs-path symlink/file updates, ported from nix_build.py.

For push events on branches with `updateOutputs`, the store path of
each attribute result is written to
<outputs_path>/<forge>/<owner>/<repo>/<branch>/<attr> (URL-quoted,
traversal-safe; forge-scoped so the same owner/repo on two forges
cannot overwrite each other) so external tooling (e.g. deploys, nginx autoindex) can find the
latest outputs. Per-branch gating (`match_glob`/`update_outputs`/
`register_gcroots`) lives in config.BranchConfigDict.
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path

from .gcroots import safe_attr_filename


class OutputsPathError(Exception):
    pass


def join_traversalsafe(root: Path, joined: Path) -> Path:
    """Join paths safely, preventing directory traversal attacks."""
    root = root.resolve()
    for part in joined.parts:
        new_root = (root / part).resolve()
        if not new_root.is_relative_to(root):
            msg = (
                f"Joined path attempted to traverse upwards when processing "
                f"{root} against {part} (gave {new_root})"
            )
            raise OutputsPathError(msg)
        root = new_root
    return root


def join_all_traversalsafe(root: Path, *paths: str) -> Path:
    for path in paths:
        root = join_traversalsafe(root, Path(path))
    return root


def write_output_path(  # noqa: PLR0913
    outputs_path: Path,
    forge: str,
    owner: str,
    repo: str,
    branch: str,
    attr: str,
    out_path: str,
) -> Path:
    """Write an attribute's store path to the outputs directory.

    Returns the file path that was written to.
    """
    file = join_all_traversalsafe(
        outputs_path,
        urllib.parse.quote_plus(forge),
        urllib.parse.quote_plus(owner),
        urllib.parse.quote_plus(repo),
        urllib.parse.quote_plus(branch),
        # Shared with gcroots: handles ""/"."/".." attribute names,
        # which quote_plus leaves as path hazards.
        safe_attr_filename(attr),
    )
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(out_path)
    return file
