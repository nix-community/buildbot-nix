"""Tests for clone/worktree management using real git repositories."""

from __future__ import annotations

import asyncio
import shutil
from typing import TYPE_CHECKING

import pytest

from buildbot_nix.gitrepo import (
    GitError,
    MergeConflictError,
    RepoManager,
    StaticCredentialsProvider,
    Worktree,
    run_git,
)

from .support import git, init_upstream

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

KEY = "github/acme/widget"


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    return init_upstream(tmp_path / "upstream", {"file.txt": "hello\n"})


@pytest.fixture
def manager(tmp_path: Path) -> RepoManager:
    return RepoManager(tmp_path / "state")


def fetch(manager: RepoManager, upstream: Path) -> None:
    asyncio.run(manager.fetch(KEY, str(upstream), ["+refs/heads/*:refs/heads/*"]))


def test_fetch_and_worktree(manager: RepoManager, upstream: Path) -> None:
    fetch(manager, upstream)
    sha = git(upstream, "rev-parse", "HEAD")

    async def run() -> None:
        wt = await manager.checkout_for_build(KEY, "build-1", base_commit=sha)
        assert (wt.path / "file.txt").read_text() == "hello\n"
        assert await wt.rev_parse("HEAD") == sha
        await manager.remove_worktree(wt)
        assert not wt.path.exists()

    asyncio.run(run())


def test_fetch_updates_existing_clone(manager: RepoManager, upstream: Path) -> None:
    fetch(manager, upstream)
    (upstream / "file.txt").write_text("v2\n")
    git(upstream, "commit", "-am", "update")
    fetch(manager, upstream)
    sha = git(upstream, "rev-parse", "HEAD")

    async def run() -> None:
        wt = await manager.checkout_for_build(KEY, "build-2", base_commit=sha)
        assert (wt.path / "file.txt").read_text() == "v2\n"
        await manager.remove_worktree(wt)

    asyncio.run(run())


def test_pr_merge_and_tree_hash_dedup(manager: RepoManager, upstream: Path) -> None:
    base = git(upstream, "rev-parse", "HEAD")
    git(upstream, "checkout", "-b", "pr")
    (upstream / "feature.txt").write_text("feature\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "feature")
    head = git(upstream, "rev-parse", "HEAD")
    git(upstream, "checkout", "main")
    fetch(manager, upstream)

    async def run() -> tuple[str, str]:
        wt1 = await manager.checkout_for_build(
            KEY, "b1", base_commit=base, head_commit=head
        )
        tree1 = await wt1.tree_hash()
        await manager.remove_worktree(wt1)
        # Same content again (re-push scenario): tree hash identical
        # even though the merge commits differ.
        wt2 = await manager.checkout_for_build(
            KEY, "b2", base_commit=base, head_commit=head
        )
        tree2 = await wt2.tree_hash()
        await manager.remove_worktree(wt2)
        return tree1, tree2

    tree1, tree2 = asyncio.run(run())
    assert tree1 == tree2


def test_merge_conflict_raises(manager: RepoManager, upstream: Path) -> None:
    git(upstream, "checkout", "-b", "pr")
    (upstream / "file.txt").write_text("pr version\n")
    git(upstream, "commit", "-am", "pr change")
    head = git(upstream, "rev-parse", "HEAD")
    git(upstream, "checkout", "main")
    (upstream / "file.txt").write_text("main version\n")
    git(upstream, "commit", "-am", "main change")
    base = git(upstream, "rev-parse", "HEAD")
    fetch(manager, upstream)

    async def run() -> None:
        with pytest.raises(MergeConflictError):
            await manager.checkout_for_build(
                KEY, "b3", base_commit=base, head_commit=head
            )

    asyncio.run(run())
    # Worktree cleaned up after conflict.
    assert not (manager.worktrees_dir / "b3").exists()


def test_reclone_on_corruption(manager: RepoManager, upstream: Path) -> None:
    fetch(manager, upstream)
    clone = manager.clone_path(KEY)
    # Destroy the object store; next fetch must transparently re-clone.
    shutil.rmtree(clone / "objects")
    fetch(manager, upstream)
    sha = git(upstream, "rev-parse", "HEAD")

    async def run() -> None:
        wt = await manager.checkout_for_build(KEY, "b4", base_commit=sha)
        await manager.remove_worktree(wt)

    asyncio.run(run())


def test_transient_fetch_error_keeps_clone(
    manager: RepoManager, upstream: Path
) -> None:
    fetch(manager, upstream)
    clone = manager.clone_path(KEY)
    # Unreachable remote but healthy clone: the clone (which backs
    # in-flight builds' worktrees) must survive.
    with pytest.raises(GitError):
        asyncio.run(
            manager.fetch(
                KEY, str(upstream / "does-not-exist"), ["+refs/heads/*:refs/heads/*"]
            )
        )
    assert (clone / "HEAD").exists()
    assert any((clone / "objects").rglob("*"))


def test_cleanup_resolves_symlinked_paths(tmp_path: Path, upstream: Path) -> None:
    # git reports symlink-resolved worktree paths; cleanup must compare
    # resolved paths or it deletes live worktrees.
    real_state = tmp_path / "real-state"
    real_state.mkdir()
    link = tmp_path / "state-link"
    link.symlink_to(real_state)
    manager = RepoManager(link)
    fetch(manager, upstream)
    sha = git(upstream, "rev-parse", "HEAD")
    live = asyncio.run(manager.checkout_for_build(KEY, "live", base_commit=sha))
    asyncio.run(manager.cleanup())
    assert live.path.exists()


@pytest.fixture
def submodule(upstream: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Add a file:// submodule to upstream; allows the file protocol
    (blocked by default since CVE-2022-39253) via environment-based
    git config passed through by run_git."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")
    sub = upstream.parent / "submodule"
    sub.mkdir()
    git(sub, "init", "-b", "main")
    (sub / "inner.txt").write_text("inner\n")
    git(sub, "add", ".")
    git(sub, "commit", "-m", "inner")
    git(
        upstream,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(sub),
        "vendored",
    )
    git(upstream, "commit", "-m", "add submodule")
    return sub


@pytest.mark.usefixtures("submodule")
def test_submodules_checked_out(manager: RepoManager, upstream: Path) -> None:
    fetch(manager, upstream)
    sha = git(upstream, "rev-parse", "HEAD")

    async def run() -> None:
        wt = await manager.checkout_for_build(KEY, "sub-build", base_commit=sha)
        assert (wt.path / "vendored" / "inner.txt").read_text() == "inner\n"
        await manager.remove_worktree(wt)

    asyncio.run(run())


def test_failed_submodule_checkout_removes_worktree(
    manager: RepoManager, upstream: Path, submodule: Path
) -> None:
    fetch(manager, upstream)
    sha = git(upstream, "rev-parse", "HEAD")
    # Submodule source gone: the update fails and the half-initialized
    # worktree must not leak (it would stay registered forever).
    shutil.rmtree(submodule)

    async def run() -> None:
        with pytest.raises(GitError):
            await manager.checkout_for_build(KEY, "sub-fail", base_commit=sha)

    asyncio.run(run())
    assert not (manager.worktrees_dir / "sub-fail").exists()


def test_cleanup_sweeps_orphans(manager: RepoManager, upstream: Path) -> None:
    fetch(manager, upstream)
    sha = git(upstream, "rev-parse", "HEAD")

    async def run() -> Worktree:
        return await manager.checkout_for_build(KEY, "live", base_commit=sha)

    live = asyncio.run(run())
    orphan = manager.worktrees_dir / "orphan"
    orphan.mkdir()
    (orphan / "junk").write_text("x")

    asyncio.run(manager.cleanup())
    assert not orphan.exists()
    assert live.path.exists()
    asyncio.run(manager.gc())


def test_static_credentials_provider(tmp_path: Path) -> None:
    netrc = tmp_path / "netrc"
    netrc.write_text("machine example.com login x password y\n")
    provider = StaticCredentialsProvider(netrc)
    creds = asyncio.run(provider.get("https://example.com/r.git"))
    assert creds.netrc_file == netrc
    assert (
        asyncio.run(StaticCredentialsProvider().get("https://x/y.git")).netrc_file
        is None
    )


def test_run_git_error_includes_stderr(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="failed"):
        asyncio.run(run_git(["rev-parse", "HEAD"], cwd=tmp_path))
