"""Pull-based polling change source.

Port of pull_based/: repositories without webhooks are polled with
`git ls-remote` for their default-branch head. Each repository polls at
its configured interval, with a random initial delay bounded by
`poll_spread` to avoid synchronized fetch storms. Per-repo SSH keys and
known-hosts files are honored via FetchCredentials.git_ssh_command()
(shared with clone/fetch, which also covers gitea.sshPrivateKeyFile
for the webhook backend).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .config import resolve_credential_path
from .gitrepo import FetchCredentials, GitError, run_git

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PolledRepository:
    name: str
    url: str
    default_branch: str
    poll_interval: int = 60
    ssh_private_key_file: Path | None = None
    ssh_known_hosts_file: Path | None = None

    def credentials(self) -> FetchCredentials:
        return FetchCredentials(
            ssh_private_key_file=resolve_credential_path(self.ssh_private_key_file),
            ssh_known_hosts_file=resolve_credential_path(self.ssh_known_hosts_file),
        )


class PollSink(Protocol):
    async def head_changed(
        self, repo: PolledRepository, branch: str, commit_sha: str
    ) -> None: ...


async def poll_head(repo: PolledRepository) -> str | None:
    """Current head SHA of the repo's default branch, or None on error."""
    try:
        output = await run_git(
            ["ls-remote", repo.url, f"refs/heads/{repo.default_branch}"],
            credentials=repo.credentials(),
        )
    except GitError as e:
        logger.warning("polling failed", extra={"repo": repo.name, "stderr": e.stderr})
        return None
    for line in output.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == f"refs/heads/{repo.default_branch}":
            return sha.strip()
    return None


class PollingService:
    def __init__(
        self,
        repositories: list[PolledRepository],
        sink: PollSink,
        poll_spread: int | None = None,
    ) -> None:
        self.repositories = repositories
        self.sink = sink
        self.poll_spread = poll_spread
        self._last_seen: dict[str, str] = {}
        self._tasks: list[asyncio.Task[None]] = []

    async def poll_repo_once(self, repo: PolledRepository) -> bool:
        """Poll one repository; returns True when a new head was seen."""
        sha = await poll_head(repo)
        if sha is None or self._last_seen.get(repo.name) == sha:
            return False
        # The sink dedupes against existing builds (tree-hash reuse), so
        # the initial observation after startup is safe to submit.
        await self.sink.head_changed(repo, repo.default_branch, sha)
        # Updated only after a successful submit so failures retry.
        self._last_seen[repo.name] = sha
        return True

    async def _poll_loop(self, repo: PolledRepository) -> None:
        if self.poll_spread:
            await asyncio.sleep(random.uniform(0, self.poll_spread))  # noqa: S311
        while True:
            try:
                await self.poll_repo_once(repo)
            except Exception:
                # A transient sink/DB error must not kill the poll loop.
                logger.exception("poll iteration failed", extra={"repo": repo.name})
            await asyncio.sleep(repo.poll_interval)

    def start(self) -> None:
        for repo in self.repositories:
            self._tasks.append(asyncio.create_task(self._poll_loop(repo)))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
