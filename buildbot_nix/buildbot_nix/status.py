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

Target URLs point at the engine's own URL scheme
(/repos/<forge>/<owner>/<name>/builds/<number>), independent of the frontend tasks.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

import httpx

from .forge import ForgeError

if TYPE_CHECKING:
    import asyncpg

    from .db import BuildRecord
    from .events import ChangeEvent
    from .forge import GiteaClient, GitHubAppClient
    from .scheduler import AttributeResult

logger = logging.getLogger(__name__)

FAILED_STATUS_STATES = frozenset(
    {"failed", "failed_eval", "dependency_failed", "cached_failure", "cancelled"}
)


class StatusState(StrEnum):
    pending = "pending"
    success = "success"
    failure = "failure"
    error = "error"


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
        if response.status_code >= 400:  # noqa: PLR2004
            logger.error(
                "failed to post GitHub status",
                extra={"repo": f"{owner}/{repo}", "status": response.status_code},
            )


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
            headers=self.client._headers(),  # noqa: SLF001
            json={
                "state": state.value,
                "context": context,
                "description": description[:255],
                "target_url": target_url,
            },
        )
        if response.status_code >= 400:  # noqa: PLR2004
            logger.error(
                "failed to post Gitea status",
                extra={"repo": f"{owner}/{repo}", "status": response.status_code},
            )


class FailedStatusStorage(Protocol):
    async def mark_failed(self, revision: str, status_name: str) -> None: ...

    async def get_failed(self, revision: str) -> set[str]: ...

    async def clear(self, revision: str, status_name: str) -> None: ...


class FailedStatusStore:
    """Port of db/failed_status.py onto the engine schema."""

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
        self._posted_generations: dict[int, int] = {}

    def build_url(self, event: ChangeEvent, build: BuildRecord) -> str:
        return f"{self.base_url}/repos/{event.repo.forge}/{event.repo.name}/builds/{build.number}"

    async def _post(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        context: str,
        state: StatusState,
        description: str,
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
                description,
                self.build_url(event, build),
            )
        except (httpx.HTTPError, ForgeError):
            # A transient forge/network failure must not propagate into
            # the orchestrator task and leave builds stuck.
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

        counts = await self._post_attribute_statuses(event, build, results, attr_prefix)
        if attr_statuses is not None:
            # Reruns pass only the re-run subset as `results`: the
            # summary description must still cover the whole build.
            counts = {"failed": 0, "succeeded": 0}
            for attr_status in attr_statuses.values():
                key = "failed" if attr_status in FAILED_STATUS_STATES else "succeeded"
                counts[key] += 1
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

        counts = {"failed": 0, "succeeded": 0}
        reported = 0
        for result in results:
            context = attr_status_context(
                event.repo.forge, event.repo.name, result.attr, attr_prefix
            )
            if result.status.value in FAILED_STATUS_STATES:
                counts["failed"] += 1
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
            description = "build cancelled (superseded)"
        else:
            state = StatusState.failure
            description = (
                f"{counts['failed']} of {counts['failed'] + counts['succeeded']} "
                "attributes failed"
                if counts["failed"]
                else (build.tree_hash and "build failed") or "merge conflict"
            )
        await self._post(
            event, build, "buildbot/nix-build", state, description or "failed"
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
