"""Shared build-event value types and the status-reporting protocol.

Kept separate from the orchestrator so forge integration, webhooks,
and the web frontend can depend on these without importing the whole
build pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .db import BuildRecord
    from .scheduler import AttributeResult


@dataclass(frozen=True)
class RepoInfo:
    """The service-side view of an enabled project."""

    id: int  # database id
    key: str  # e.g. "github/owner/repo" (clone directory key)
    name: str  # "owner/repo"
    owner: str
    repo: str
    forge: str  # "github" | "gitea"
    clone_url: str
    default_branch: str


@dataclass(frozen=True)
class ChangeEvent:
    """A push or pull-request event from a forge or poller."""

    repo: RepoInfo
    branch: str
    commit_sha: str
    # PR-only fields; base_sha is the base branch head to merge into.
    pr_number: int | None = None
    pr_author: str | None = None
    base_sha: str | None = None
    commit_message: str = ""


class StatusReporter(Protocol):
    """Receives lifecycle events; forge integration implements this."""

    async def build_started(self, event: ChangeEvent, build: BuildRecord) -> None: ...

    async def eval_finished(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        *,
        success: bool,
        warnings: list[str],
    ) -> None: ...

    async def eval_cancelled(self, event: ChangeEvent, build: BuildRecord) -> None: ...

    async def build_finished(  # noqa: PLR0913
        self,
        event: ChangeEvent,
        build: BuildRecord,
        status: str,
        generation: int,
        results: list[AttributeResult],
        *,
        attr_statuses: dict[str, str] | None = None,
        attr_prefix: str = "checks",
    ) -> None: ...


class NullStatusReporter:
    async def build_started(self, event: ChangeEvent, build: BuildRecord) -> None:
        pass

    async def eval_finished(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        *,
        success: bool,
        warnings: list[str],
    ) -> None:
        pass

    async def eval_cancelled(self, event: ChangeEvent, build: BuildRecord) -> None:
        pass

    async def build_finished(  # noqa: PLR0913
        self,
        event: ChangeEvent,
        build: BuildRecord,
        status: str,
        generation: int,
        results: list[AttributeResult],
        *,
        attr_statuses: dict[str, str] | None = None,
        attr_prefix: str = "checks",
    ) -> None:
        pass
