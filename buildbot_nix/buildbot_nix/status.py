"""Commit status reporting.

Combined per-phase contexts keep their buildbot-era names —
`buildbot/nix-eval` (warning count appended to the description) and
`buildbot/nix-build` — so existing branch protection rules keep
working. Per-attribute failure statuses (`buildbot/nix-build
<forge>:<owner>/<repo>#checks.<attr>`) cover failing/cancelled
attributes, capped by failedBuildReportLimit (default 47).

Failed per-attribute statuses are persisted per revision
(failed_statuses table, port of db/failed_status.py) so a later
rebuild flips them to success — including force-running already-built
attributes (the orchestrator feeds them to the scheduler as
force_attrs). Status posts carry the build's monotonic generation;
stale posts (lower generation than the last one sent for that build)
are dropped.

Target URLs point at the service's own URL scheme
(/repos/<forge>/<owner>/<name>/builds/<number>), independent of the frontend tasks.
"""

from __future__ import annotations

import contextlib
import logging
import math
import time
from collections import OrderedDict
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar, Protocol

import httpx

from .ansi import strip_ansi
from .forge import ForgeError

if TYPE_CHECKING:
    import asyncpg

    from .db import BuildRecord
    from .events import ChangeEvent
    from .forge import GiteaClient, GitHubAppClient, GitlabClient
    from .scheduler import AttributeResult

logger = logging.getLogger(__name__)

# Cap on remembered (build id -> posted generation) entries; one entry
# per build forever would be a slow leak in a long-lived process.
POSTED_GENERATIONS_MAX = 1024

FAILED_STATUS_STATES = frozenset(
    {"failed", "failed_eval", "dependency_failed", "cached_failure", "cancelled"}
)


class StatusState(StrEnum):
    pending = "pending"
    success = "success"
    failure = "failure"
    error = "error"


class StatusPostError(Exception):
    """HTTP-level status post failure; retry_after carries the forge's
    Retry-After / rate-limit-reset hint in seconds, if any."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is not None:
        try:
            seconds = float(value)
            # float() accepts nan/inf; nan would poison every later
            # min/max comparison down to asyncio.sleep.
            if math.isfinite(seconds):
                return max(0.0, seconds)
        except ValueError:
            with contextlib.suppress(TypeError, ValueError):
                dt = parsedate_to_datetime(value)
                return max(0.0, dt.timestamp() - time.time())
    # GitHub primary rate limits: no Retry-After, only a reset epoch.
    if response.headers.get("X-RateLimit-Remaining") == "0":
        with contextlib.suppress(TypeError, ValueError):
            reset = int(response.headers["X-RateLimit-Reset"])
            return max(0.0, reset - time.time())
    return None


def _raise_for_status(response: httpx.Response, repo: str) -> None:
    if response.status_code >= httpx.codes.BAD_REQUEST:
        msg = f"status post for {repo} failed: HTTP {response.status_code}"
        raise StatusPostError(msg, retry_after=_retry_after_seconds(response))


class CommitStatusPoster(Protocol):
    async def post(  # noqa: PLR0913
        self,
        owner: str,
        repo: str,
        sha: str,
        context: str,
        state: StatusState,
        description: str,
        target_url: str,
    ) -> None: ...


class GitHubStatusPoster:
    def __init__(self, client: GitHubAppClient) -> None:
        self.client = client

    async def post(  # noqa: PLR0913
        self,
        owner: str,
        repo: str,
        sha: str,
        context: str,
        state: StatusState,
        description: str,
        target_url: str,
    ) -> None:
        installation_id = self.client.repo_installations.get(f"{owner}/{repo}".lower())
        if installation_id is None:
            logger.warning(
                "no installation for repo, dropping status",
                extra={"repo": f"{owner}/{repo}"},
            )
            return
        token = await self.client.installation_token(installation_id)
        response = await self.client.http.post(
            f"{self.client.api_url}/repos/{owner}/{repo}/statuses/{sha}",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "state": state.value,
                "context": context,
                "description": description[:140],
                "target_url": target_url,
            },
        )
        _raise_for_status(response, f"{owner}/{repo}")


class GiteaStatusPoster:
    def __init__(self, client: GiteaClient) -> None:
        self.client = client

    async def post(  # noqa: PLR0913
        self,
        owner: str,
        repo: str,
        sha: str,
        context: str,
        state: StatusState,
        description: str,
        target_url: str,
    ) -> None:
        response = await self.client.http.post(
            f"{self.client.instance_url}/api/v1/repos/{owner}/{repo}/statuses/{sha}",
            headers=self.client.auth_headers(),
            json={
                "state": state.value,
                "context": context,
                "description": description[:255],
                "target_url": target_url,
            },
        )
        _raise_for_status(response, f"{owner}/{repo}")


class GitlabStatusPoster:
    # GitLab has no "error" state; both map to failed.
    _STATES: ClassVar[dict[StatusState, str]] = {
        StatusState.pending: "pending",
        StatusState.success: "success",
        StatusState.failure: "failed",
        StatusState.error: "failed",
    }

    def __init__(self, client: GitlabClient) -> None:
        self.client = client

    async def post(  # noqa: PLR0913
        self,
        owner: str,
        repo: str,
        sha: str,
        context: str,
        state: StatusState,
        description: str,
        target_url: str,
    ) -> None:
        response = await self.client.http.post(
            f"{self.client.project_api_url(owner, repo)}/statuses/{sha}",
            headers=self.client.auth_headers(),
            json={
                "state": self._STATES[state],
                "context": context,
                "description": description[:255],
                "target_url": target_url,
            },
        )
        _raise_for_status(response, f"{owner}/{repo}")


class FailedStatusStorage(Protocol):
    async def mark_failed(self, revision: str, status_name: str) -> None: ...

    async def get_failed(self, revision: str) -> set[str]: ...

    async def clear(self, revision: str, status_name: str) -> None: ...


class FailedStatusStore:
    """Port of db/failed_status.py onto the service schema."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def mark_failed(self, revision: str, status_name: str) -> None:
        await self.pool.execute(
            """
            INSERT INTO failed_statuses (revision, status_name, timestamp)
            VALUES ($1, $2, $3)
            ON CONFLICT (revision, status_name)
            DO UPDATE SET timestamp = EXCLUDED.timestamp
            """,
            revision,
            status_name,
            datetime.now(tz=UTC).timestamp(),
        )

    async def get_failed(self, revision: str) -> set[str]:
        rows = await self.pool.fetch(
            "SELECT status_name FROM failed_statuses WHERE revision = $1", revision
        )
        return {row["status_name"] for row in rows}

    async def clear(self, revision: str, status_name: str) -> None:
        await self.pool.execute(
            "DELETE FROM failed_statuses WHERE revision = $1 AND status_name = $2",
            revision,
            status_name,
        )


def _count_key(status: str) -> str:
    """Summary bucket for one attribute status."""
    if status == "cancelled":
        return "cancelled"
    return "failed" if status in FAILED_STATUS_STATES else "succeeded"


def attr_status_context(
    forge: str, project_name: str, attr: str, prefix: str = "checks"
) -> str:
    return f"buildbot/nix-build {forge}:{project_name}#{prefix}.{attr}"


def eval_description(success: bool, warnings: list[str]) -> str:
    base = "evaluation succeeded" if success else "evaluation failed"
    if warnings:
        count = len(warnings)
        return f"{base} ({count} warning{'s' if count != 1 else ''})"
    return base


class ForgeStatusReporter:
    """Implements the orchestrator's StatusReporter protocol."""

    def __init__(
        self,
        posters: dict[str, CommitStatusPoster],
        failed_statuses: FailedStatusStorage,
        base_url: str,
        failed_build_report_limit: int = 47,
    ) -> None:
        # Keyed by forge so mixed GitHub+Gitea deployments post to the
        # right API.
        self.posters = posters
        self.failed_statuses = failed_statuses
        self.base_url = base_url.rstrip("/")
        self.failed_build_report_limit = failed_build_report_limit
        # build id -> highest generation posted (drop stale posts).
        # Bounded LRU: stale-post races only matter around a build's
        # final re-aggregation, so old entries are safe to evict.
        self._posted_generations: OrderedDict[int, int] = OrderedDict()

    def build_url(self, event: ChangeEvent, build: BuildRecord) -> str:
        return f"{self.base_url}/repos/{event.repo.forge}/{event.repo.name}/builds/{build.number}"

    async def _post(  # noqa: PLR0913
        self,
        event: ChangeEvent,
        build: BuildRecord,
        context: str,
        state: StatusState,
        description: str,
        *,
        propagate: bool = False,
    ) -> None:
        poster = self.posters.get(event.repo.forge)
        if poster is None:
            return
        try:
            await poster.post(
                event.repo.owner,
                event.repo.repo,
                event.commit_sha,
                context,
                state,
                # Descriptions may carry failure excerpts with raw ANSI
                # colors (kept for the web UI); forges show them verbatim.
                strip_ansi(description),
                self.build_url(event, build),
            )
        except (httpx.HTTPError, ForgeError, StatusPostError):
            # Transient failures must not propagate into the
            # orchestrator task and leave builds stuck — except the
            # terminal summary, whose failure drives the queued retry.
            if propagate:
                raise
            logger.exception(
                "failed to post commit status",
                extra={"build_id": build.id, "context": context},
            )

    async def build_started(self, event: ChangeEvent, build: BuildRecord) -> None:
        await self._post(
            event, build, "buildbot/nix-eval", StatusState.pending, "evaluating flake"
        )

    async def eval_finished(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        *,
        success: bool,
        warnings: list[str],
    ) -> None:
        await self._post(
            event,
            build,
            "buildbot/nix-eval",
            StatusState.success if success else StatusState.failure,
            eval_description(success, warnings),
        )
        if success:
            await self._post(
                event,
                build,
                "buildbot/nix-build",
                StatusState.pending,
                "building attributes",
            )

    async def eval_cancelled(self, event: ChangeEvent, build: BuildRecord) -> None:
        """Resolve the pending eval context; see the orchestrator's
        cancel path."""
        await self._post(
            event,
            build,
            "buildbot/nix-eval",
            StatusState.error,
            "build cancelled",
        )

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
        # Monotonic generation: drop stale posts after re-aggregation.
        if generation < self._posted_generations.get(build.id, 0):
            logger.info(
                "dropping stale status post",
                extra={"build_id": build.id, "generation": generation},
            )
            return
        self._posted_generations[build.id] = generation
        self._posted_generations.move_to_end(build.id)
        while len(self._posted_generations) > POSTED_GENERATIONS_MAX:
            self._posted_generations.popitem(last=False)

        counts = await self._post_attribute_statuses(event, build, results, attr_prefix)
        if attr_statuses is not None:
            # Reruns pass only the re-run subset as `results`: the
            # summary description must still cover the whole build.
            counts = {"failed": 0, "succeeded": 0, "cancelled": 0}
            for attr_status in attr_statuses.values():
                counts[_count_key(attr_status)] += 1
        await self._post_summary(event, build, status, counts)

    async def _post_attribute_statuses(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        results: list[AttributeResult],
        attr_prefix: str,
    ) -> dict[str, int]:
        """Per-attribute failure statuses and success flips; returns
        failed/succeeded counts over `results`."""
        revision = event.commit_sha
        previously_failed = await self.failed_statuses.get_failed(revision)

        counts = {"failed": 0, "succeeded": 0, "cancelled": 0}
        reported = 0
        for result in results:
            context = attr_status_context(
                event.repo.forge, event.repo.name, result.attr, attr_prefix
            )
            if result.status.value in FAILED_STATUS_STATES:
                counts[_count_key(result.status.value)] += 1
                if context not in previously_failed:
                    # Only new failures consume the report budget;
                    # previously-failed contexts always re-post so they
                    # can later flip to success.
                    if reported >= self.failed_build_report_limit:
                        continue
                    reported += 1
                await self.failed_statuses.mark_failed(revision, context)
                description = result.error or result.status.value
                await self._post(
                    event, build, context, StatusState.failure, description
                )
            else:
                counts["succeeded"] += 1
                if context in previously_failed:
                    # Success-flip for a previously failed status.
                    await self.failed_statuses.clear(revision, context)
                    await self._post(
                        event, build, context, StatusState.success, "succeeded"
                    )
        return counts

    async def _post_summary(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        status: str,
        counts: dict[str, int],
    ) -> None:
        if status == "succeeded":
            state = StatusState.success
            description = f"{counts['succeeded']} attributes built"
        elif status == "cancelled":
            state = StatusState.error
            # Attribute-level cancels aggregate like failures; only a
            # build-level cancel (no attribute info at all) was
            # superseded by a newer build.
            parts = [
                f"{counts[key]} {key}"
                for key in ("cancelled", "failed", "succeeded")
                if counts[key]
            ]
            description = ", ".join(parts) if parts else "build cancelled (superseded)"
        else:
            state = StatusState.failure
            description = (
                f"{counts['failed']} of {sum(counts.values())} attributes failed"
                if counts["failed"]
                else (build.tree_hash and "build failed") or "merge conflict"
            )
        await self._post(
            event,
            build,
            "buildbot/nix-build",
            state,
            description or "failed",
            propagate=True,
        )

    async def force_attrs_for(
        self, event: ChangeEvent, attr_prefix: str = "checks"
    ) -> set[str]:
        """Attrs whose failed status must be flipped: feed to the
        scheduler as force_attrs so already-built attrs still run."""
        prefix = (
            f"buildbot/nix-build {event.repo.forge}:{event.repo.name}#{attr_prefix}."
        )
        return {
            name.removeprefix(prefix)
            for name in await self.failed_statuses.get_failed(event.commit_sha)
            if name.startswith(prefix)
        }
