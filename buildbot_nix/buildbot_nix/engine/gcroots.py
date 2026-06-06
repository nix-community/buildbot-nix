"""GC-root registration, ported from nix_gcroot.py.

The latest successful result of each attribute is protected from
garbage collection under <gcroots_dir>/<project>/<attr> (in
production /nix/var/nix/gcroots/per-user/buildbot-nix). Skipped-as-
already-built attributes are registered too. Re-registration is
skipped when the root already points at the same store path.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class GcrootRegistrationError(Exception):
    pass


def safe_attr_filename(attr: str) -> str:
    """Encode a flake attribute name as a single safe path component.

    Attribute names are repository-controlled and may contain `/` and
    `..` via quoted Nix attributes; used verbatim in paths they allow
    traversal outside the intended directory. Typical attribute names
    (letters, digits, `.`, `-`, `_`) are preserved unchanged.
    """
    encoded = quote(attr, safe="")
    if encoded in {"", ".", ".."}:
        # Not usable as a path component: "" would vanish, "."/".."
        # resolve to other directories.
        encoded = {"": "%empty", ".": "%2E", "..": "%2E%2E"}[encoded]
    return encoded


def gcroot_path(gcroots_dir: Path, project: str, attr: str) -> Path:
    return gcroots_dir / project / safe_attr_filename(attr)


def gcroot_already_points_to(gc_root: Path, out_path: str) -> bool:
    try:
        return str(gc_root.readlink()) == out_path
    except OSError:
        return False


async def register_gcroot(
    gcroots_dir: Path, project: str, attr: str, out_path: str
) -> None:
    """Register (or refresh) the gc-root for one attribute result."""
    root = gcroot_path(gcroots_dir, project, attr)
    if await asyncio.to_thread(gcroot_already_points_to, root, out_path):
        # Refresh the symlink's timestamp: age-based tmpfiles cleanup
        # must not expire roots of attributes that are still rebuilt
        # with an unchanged output.
        await asyncio.to_thread(_touch, root)
        return
    root.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "nix-store",
        "--add-root",
        str(root),
        "-r",
        out_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        msg = (
            f"failed to register gcroot {root} -> {out_path}: "
            f"{stderr.decode(errors='replace')}"
        )
        raise GcrootRegistrationError(msg)


def _touch(path: Path) -> None:
    try:
        os.utime(path, follow_symlinks=False)
    except OSError:
        logger.warning("failed to refresh gcroot mtime", extra={"path": str(path)})
