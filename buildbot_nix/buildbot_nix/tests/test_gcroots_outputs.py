"""Tests for gcroot helpers, outputs-path updates, and per-branch
config gating."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from buildbot_nix import gcroots as gcroots_mod
from buildbot_nix.config import BranchConfig, BranchConfigDict
from buildbot_nix.gcroots import (
    gcroot_already_points_to,
    gcroot_path,
    register_gcroot,
    safe_attr_filename,
)
from buildbot_nix.outputs import (
    OutputsPathError,
    join_all_traversalsafe,
    write_output_path,
)


def test_gcroot_path_layout() -> None:
    assert (
        str(
            gcroot_path(
                Path("/nix/var/nix/gcroots/per-user/ci-user"),
                "owner/repo",
                "checks.x.foo",
            )
        )
        == "/nix/var/nix/gcroots/per-user/ci-user/owner/repo/checks.x.foo"
    )


def test_gcroot_path_custom_dir(tmp_path: Path) -> None:
    # Dev setups point gcroots_dir at a user-writable directory.
    assert gcroot_path(tmp_path, "o/r", "a") == tmp_path / "o" / "r" / "a"


def test_safe_attr_filename_preserves_normal_attrs() -> None:
    assert safe_attr_filename("checks.x86_64-linux.foo_bar-1") == (
        "checks.x86_64-linux.foo_bar-1"
    )


def test_safe_attr_filename_blocks_traversal() -> None:
    # Quoted Nix attribute names may contain `/` and `..`.
    base = gcroot_path(Path("/g/u"), "o/r", "x").parent
    for attr in ('checks."../../../../tmp/evil"', "..", ".", "", "a/b"):
        encoded = safe_attr_filename(attr)
        assert "/" not in encoded
        assert encoded not in {"", ".", ".."}
        root = gcroot_path(Path("/g/u"), "o/r", attr)
        assert root.parent == base


def test_register_gcroot_refreshes_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Unchanged store path: no nix-store call, but the symlink mtime is
    # refreshed so age-based tmpfiles cleanup does not expire it.
    root = tmp_path / "proj" / "attr"
    root.parent.mkdir(parents=True)
    root.symlink_to("/nix/store/abc")
    old = 1_000_000
    os.utime(root, (old, old), follow_symlinks=False)
    monkeypatch.setattr(gcroots_mod, "gcroot_path", lambda *_a: root)
    # No fake nix-store on PATH: re-registration would fail loudly.
    asyncio.run(register_gcroot(tmp_path, "proj", "attr", "/nix/store/abc"))
    assert root.lstat().st_mtime > old


def test_gcroot_already_points_to(tmp_path: Path) -> None:
    link = tmp_path / "root"
    assert not gcroot_already_points_to(link, "/nix/store/abc")
    link.symlink_to("/nix/store/abc")
    assert gcroot_already_points_to(link, "/nix/store/abc")
    assert not gcroot_already_points_to(link, "/nix/store/other")


def test_write_output_path(tmp_path: Path) -> None:
    file = write_output_path(
        tmp_path,
        "github",
        "acme",
        "widget",
        "main",
        "checks.x86_64-linux.foo",
        "/nix/store/o",
    )
    assert file.read_text() == "/nix/store/o"
    assert file == (
        tmp_path / "github" / "acme" / "widget" / "main" / "checks.x86_64-linux.foo"
    )


def test_write_output_path_quotes_separators(tmp_path: Path) -> None:
    file = write_output_path(
        tmp_path, "github", "acme", "widget", "feature/x", "attr", "/nix/store/o"
    )
    # Branch slashes are quoted, not directories.
    assert file.parent.name == "feature%2Fx"


def test_traversal_rejected(tmp_path: Path) -> None:
    with pytest.raises(OutputsPathError):
        join_all_traversalsafe(tmp_path, "..", "etc")


def branches() -> BranchConfigDict:
    return BranchConfigDict(
        {
            "release": BranchConfig(
                match_glob="release-*",
                register_gcroots=True,
                update_outputs=False,
            ),
            "staging": BranchConfig(
                match_glob="staging",
                register_gcroots=False,
                update_outputs=True,
            ),
        }
    )


def test_branch_config_gating() -> None:
    cfg = branches()
    # Default branch always runs, registers, updates.
    assert cfg.do_run("main", "main")
    assert cfg.do_register_gcroot("main", "main")
    assert cfg.do_update_outputs("main", "main")
    # Matching glob with registerGCRoots.
    assert cfg.do_register_gcroot("main", "release-1.0")
    assert not cfg.do_update_outputs("main", "release-1.0")
    # staging: outputs but no gcroots.
    assert not cfg.do_register_gcroot("main", "staging")
    assert cfg.do_update_outputs("main", "staging")
    # Unmatched branch: nothing.
    assert not cfg.do_register_gcroot("main", "random")
    assert not cfg.do_update_outputs("main", "random")


def test_branch_config_merge_or() -> None:
    cfg = BranchConfigDict(
        {
            "a": BranchConfig(
                match_glob="x*", register_gcroots=True, update_outputs=False
            ),
            "b": BranchConfig(
                match_glob="x*", register_gcroots=False, update_outputs=True
            ),
        }
    )
    # Overlapping globs merge with OR semantics.
    assert cfg.do_register_gcroot("main", "x1")
    assert cfg.do_update_outputs("main", "x1")


def test_write_output_path_is_forge_scoped(tmp_path: Path) -> None:
    # Same owner/repo on two forges must not overwrite each other.
    f1 = write_output_path(
        tmp_path, "github", "acme", "widget", "main", "a", "/nix/store/1"
    )
    f2 = write_output_path(
        tmp_path, "gitea", "acme", "widget", "main", "a", "/nix/store/2"
    )
    assert f1 != f2
    assert f1.read_text() == "/nix/store/1"
    assert f2.read_text() == "/nix/store/2"
