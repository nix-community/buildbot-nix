"""Tests for cancellation/supersede tracking and [skip ci] markers."""

from __future__ import annotations

import asyncio

from nixbot.canceller import (
    CancellationManager,
    RegisterOutcome,
    branch_key,
    has_skip_ci_marker,
)


def test_skip_ci_markers() -> None:
    assert has_skip_ci_marker("fix typo [skip ci]")
    assert has_skip_ci_marker("[CI SKIP] wip")
    assert has_skip_ci_marker("multi\nline\n[ci skip]")
    assert not has_skip_ci_marker("regular commit about ci skip behavior")
    assert not has_skip_ci_marker("")


def test_branch_key() -> None:
    assert branch_key("main") == "branch/main"
    assert branch_key("refs/pull/123/merge") == "refs/pull/123"
    assert branch_key("refs/merge-requests/9/head") == "refs/merge-requests/9"
    assert branch_key("anything", pr_number=7) == "pr/7"
    # A branch literally named pr/7 must not collide with PR 7.
    assert branch_key("pr/7") != branch_key("x", pr_number=7)


def reg(  # noqa: PLR0913
    mgr: CancellationManager,
    build_id: int,
    tree: str,
    sha: str,
    context: str = "main",
    project: int = 1,
    *,
    stale: bool = False,
) -> tuple[str, asyncio.Event]:
    event = asyncio.Event()
    outcome = mgr.register(
        project,
        context,
        build_id,
        tree,
        sha,
        event,
        incoming_is_ancestor_of_running=stale,
    )
    return outcome, event


def test_supersede_cancels_old_build() -> None:
    mgr = CancellationManager()
    _, old_event = reg(mgr, 1, "tree1", "sha1")
    outcome, new_event = reg(mgr, 2, "tree2", "sha2")
    assert outcome == RegisterOutcome.NEW
    assert old_event.is_set()
    assert not new_event.is_set()


def test_duplicate_same_tree_not_cancelled() -> None:
    mgr = CancellationManager()
    _, old_event = reg(mgr, 1, "tree1", "sha1")
    outcome, _ = reg(mgr, 1, "tree1", "sha-other")
    assert outcome == RegisterOutcome.DUPLICATE
    assert not old_event.is_set()


def test_same_commit_different_tree_supersedes() -> None:
    # Same PR head merged against an advanced base yields a new tree:
    # a new build that must be tracked, not a duplicate.
    mgr = CancellationManager()
    _, old_event = reg(mgr, 1, "tree1", "sha1", context="pr/5")
    outcome, _ = reg(mgr, 2, "tree2", "sha1", context="pr/5")
    assert outcome == RegisterOutcome.NEW
    assert old_event.is_set()
    assert mgr.cancel_pr(1, 5)


def test_out_of_order_event_ignored() -> None:
    mgr = CancellationManager()
    _, running_event = reg(mgr, 1, "tree2", "sha2")
    # Older commit arrives late: must not supersede the newer build.
    outcome, _ = reg(mgr, 2, "tree1", "sha1", stale=True)
    assert outcome == RegisterOutcome.STALE
    assert not running_event.is_set()


def test_shared_build_cancelled_only_when_all_contexts_superseded() -> None:
    mgr = CancellationManager()
    event = asyncio.Event()
    # One build (same tree) referenced by a branch push and a PR.
    mgr.register(1, "main", 1, "tree1", "sha1", event)
    mgr.register(1, "pr/5", 1, "tree1", "sha1", event)

    # Superseded on the branch only: still alive via the PR context.
    reg(mgr, 2, "tree2", "sha2", context="main")
    assert not event.is_set()

    # Superseded in the PR context too: now cancelled.
    reg(mgr, 3, "tree3", "sha3", context="pr/5")
    assert event.is_set()


def test_pr_close_cancels() -> None:
    mgr = CancellationManager()
    _, event = reg(mgr, 1, "tree1", "sha1", context="pr/9")
    assert mgr.cancel_pr(1, 9)
    assert event.is_set()
    # Unknown PR: no-op.
    assert not mgr.cancel_pr(1, 1234)


def test_completed_build_not_cancelled_later() -> None:
    mgr = CancellationManager()
    _, event = reg(mgr, 1, "tree1", "sha1")
    mgr.complete(1)
    outcome, _ = reg(mgr, 2, "tree2", "sha2")
    assert outcome == RegisterOutcome.NEW
    assert not event.is_set()


def test_contexts_isolated_per_project() -> None:
    mgr = CancellationManager()
    _, event1 = reg(mgr, 1, "tree1", "sha1", project=1)
    _, event2 = reg(mgr, 2, "tree2", "sha2", project=2)
    assert not event1.is_set()
    assert not event2.is_set()


def test_reregister_updates_cancel_event() -> None:
    """Crash recovery re-registers a build with a fresh cancel event;
    the manager must cancel via the new event, not the pre-crash one."""
    mgr = CancellationManager()
    reg(mgr, 1, "tree1", "sha1")  # pre-crash registration
    _, fresh_event = reg(mgr, 1, "tree1", "sha1")  # recovered, new event
    reg(mgr, 2, "tree2", "sha2")  # supersedes build 1
    assert fresh_event.is_set()
