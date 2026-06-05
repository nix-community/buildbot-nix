"""Build cancellation and supersede tracking.

Ports build_canceller.py semantics onto the engine: a new change event
for the same branch or PR supersedes the in-flight build, which is
cancelled via its cancel event (the executor kills the process group,
suppresses pending retries, and the scheduler propagates "skipped
(superseded)" — never "dependency failed" — to dependents).

Supersede decisions compare commits/merge-trees, not webhook arrival
order: an event whose commit is an ancestor of the running build's
commit is stale and ignored (delivery-GUID dedupe lives in webhook
ingestion). A build shared by multiple contexts (branch
push + PR with identical tree) is only cancelled when superseded in
all contexts referencing it. PR close/merge cancels that PR's builds.

`[skip ci]` / `[ci skip]` commit message markers are honored here too.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio

logger = logging.getLogger(__name__)

_SKIP_CI_RE = re.compile(r"\[(?:skip ci|ci skip)\]", re.IGNORECASE)


def has_skip_ci_marker(commit_message: str) -> bool:
    return bool(_SKIP_CI_RE.search(commit_message))


def branch_key(branch: str, pr_number: int | None = None) -> str:
    """Context key for supersede tracking; one in-flight build per key.

    Ports branch_key_for_pr: PRs collapse to one key regardless of
    merge/head ref form.
    """
    if pr_number is not None:
        return f"pr/{pr_number}"
    if branch.startswith(("refs/pull/", "refs/merge-requests/")):
        # refs/pull/123/merge -> refs/pull/123
        return branch.rsplit("/", 1)[0]
    return branch


@dataclass
class InFlightBuild:
    build_id: int
    project_id: int
    tree_hash: str
    commit_sha: str
    cancel_event: asyncio.Event
    contexts: set[str] = field(default_factory=set)


class RegisterOutcome:
    NEW = "new"
    DUPLICATE = "duplicate"  # same content, build shared/reused
    STALE = "stale"  # incoming commit is older than the running one


class CancellationManager:
    def __init__(self) -> None:
        self._builds: dict[int, InFlightBuild] = {}
        # (project_id, context_key) -> build_id
        self._by_context: dict[tuple[int, str], int] = {}

    def register(  # noqa: PLR0913
        self,
        project_id: int,
        context_key: str,
        build_id: int,
        tree_hash: str,
        commit_sha: str,
        cancel_event: asyncio.Event,
        *,
        incoming_is_ancestor_of_running: bool = False,
    ) -> str:
        """Track a build for a context, superseding any previous one.

        `incoming_is_ancestor_of_running` should be the result of a
        `git merge-base --is-ancestor <incoming> <running>` check by the
        caller; when true the event is out-of-order and ignored.
        """
        context = (project_id, context_key)
        old_build_id = self._by_context.get(context)
        if old_build_id is not None:
            old = self._builds.get(old_build_id)
            if old is not None:
                # Identity is the post-merge tree hash only: the same
                # head commit yields a different tree once the base
                # branch advances.
                if old.tree_hash == tree_hash:
                    # Same content: shared build, nothing to cancel.
                    old.contexts.add(context_key)
                    return RegisterOutcome.DUPLICATE
                if incoming_is_ancestor_of_running:
                    logger.info(
                        "ignoring out-of-order change event",
                        extra={
                            "project_id": project_id,
                            "context": context_key,
                            "stale_commit": commit_sha,
                        },
                    )
                    return RegisterOutcome.STALE
                self._release_context(old, context_key, reason="superseded")

        build = self._builds.get(build_id)
        if build is None:
            build = InFlightBuild(
                build_id=build_id,
                project_id=project_id,
                tree_hash=tree_hash,
                commit_sha=commit_sha,
                cancel_event=cancel_event,
            )
            self._builds[build_id] = build
        build.contexts.add(context_key)
        self._by_context[context] = build_id
        return RegisterOutcome.NEW

    def _release_context(
        self, build: InFlightBuild, context_key: str, reason: str
    ) -> None:
        build.contexts.discard(context_key)
        self._by_context.pop((build.project_id, context_key), None)
        if not build.contexts:
            # Shared builds are only cancelled when superseded in ALL
            # contexts referencing them.
            logger.info(
                "cancelling build",
                extra={"build_id": build.build_id, "reason": reason},
            )
            build.cancel_event.set()
            self._builds.pop(build.build_id, None)

    def complete(self, build_id: int) -> None:
        """Build reached a terminal state: stop tracking it."""
        build = self._builds.pop(build_id, None)
        if build is None:
            return
        for context_key in build.contexts:
            self._by_context.pop((build.project_id, context_key), None)

    def cancel_pr(self, project_id: int, pr_number: int) -> bool:
        """PR closed/merged: cancel its in-flight build (in that context)."""
        context_key = branch_key("", pr_number)
        build_id = self._by_context.get((project_id, context_key))
        if build_id is None:
            return False
        build = self._builds.get(build_id)
        if build is None:
            return False
        self._release_context(build, context_key, reason="pr closed")
        return True

    def running_commit_for(self, project_id: int, context_key: str) -> str | None:
        """Commit of the in-flight build for a context, for the caller's
        out-of-order (`git merge-base --is-ancestor`) check."""
        build_id = self._by_context.get((project_id, context_key))
        if build_id is None:
            return None
        build = self._builds.get(build_id)
        return build.commit_sha if build else None

    def cancel_event_for(self, build_id: int) -> asyncio.Event | None:
        build = self._builds.get(build_id)
        return build.cancel_event if build else None
