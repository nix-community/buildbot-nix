"""Startup reconciliation: after downtime, build the
default-branch head and open-PR heads that have no build record yet.

Runs after crash recovery; duplicates are resolved by the
supersede rules. "Built" means any build record exists for the head
commit in that project, regardless of result — exact merge-tree
checking happens once the orchestrator computes the tree.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from .webhooks import ChangeRequest

if TYPE_CHECKING:
    import asyncpg

    from .forge import GiteaClient, GitHubAppClient, GitlabClient
    from .repos import RepoRecord
    from .webhooks import ChangeSink

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RemoteHead:
    branch: str
    commit_sha: str
    pr_number: int | None = None
    pr_author: str | None = None
    base_sha: str | None = None


async def github_heads(
    client: GitHubAppClient, project: RepoRecord
) -> list[RemoteHead]:
    installation_id = client.repo_installations.get(
        f"{project.owner}/{project.name}".lower()
    )
    if installation_id is None:
        return []
    token = await client.installation_token(installation_id)
    repo_url = f"{client.api_url}/repos/{project.owner}/{project.name}"
    heads = []
    branch: dict[str, Any] | None = None
    response = await client.http.get(
        f"{repo_url}/branches/{project.default_branch}",
        headers={"Authorization": f"Bearer {token}"},
    )
    if response.status_code < 400:  # noqa: PLR2004
        branch = response.json()
    if branch:
        heads.append(
            RemoteHead(
                branch=project.default_branch,
                commit_sha=branch["commit"]["sha"],
            )
        )
    pulls = await client.paginated(f"{repo_url}/pulls?state=open&per_page=100", token)
    heads.extend(
        RemoteHead(
            branch=pull["base"]["ref"],
            commit_sha=pull["head"]["sha"],
            pr_number=pull["number"],
            pr_author=f"github:{pull['user']['login']}",
            base_sha=pull["base"]["sha"],
        )
        for pull in pulls
    )
    return heads


async def gitea_heads(client: GiteaClient, project: RepoRecord) -> list[RemoteHead]:
    repo_url = f"{client.instance_url}/api/v1/repos/{project.owner}/{project.name}"
    heads = []
    response = await client.http.get(
        f"{repo_url}/branches/{project.default_branch}",
        headers=client.auth_headers(),
    )
    if response.status_code < 400:  # noqa: PLR2004
        heads.append(
            RemoteHead(
                branch=project.default_branch,
                commit_sha=response.json()["commit"]["id"],
            )
        )
    pulls = await client.paginated(f"{repo_url}/pulls?state=open&limit=50")
    heads.extend(
        RemoteHead(
            branch=pull["base"]["ref"],
            commit_sha=pull["head"]["sha"],
            pr_number=pull["number"],
            pr_author=f"gitea:{pull['user']['login']}",
            base_sha=pull["base"]["sha"],
        )
        for pull in pulls
    )
    return heads


async def gitlab_heads(client: GitlabClient, project: RepoRecord) -> list[RemoteHead]:
    repo_url = client.project_api_url(project.owner, project.name)
    heads = []
    response = await client.http.get(
        f"{repo_url}/repository/branches/{quote(project.default_branch, safe='')}",
        headers=client.auth_headers(),
    )
    if response.status_code < 400:  # noqa: PLR2004
        heads.append(
            RemoteHead(
                branch=project.default_branch,
                commit_sha=response.json()["commit"]["id"],
            )
        )
    pulls = await client.paginated(
        f"{repo_url}/merge_requests?state=opened&per_page=100"
    )
    heads.extend(
        RemoteHead(
            branch=pull["target_branch"],
            commit_sha=pull["sha"],
            pr_number=pull["iid"],
            pr_author=f"gitlab:{pull['author']['username']}",
            # The MR API exposes no base sha; merge against the
            # target branch ref instead.
            base_sha=f"refs/heads/{pull['target_branch']}",
        )
        for pull in pulls
    )
    return heads


async def _has_builds(pool: asyncpg.Pool, project_id: int) -> bool:
    """Cancelled builds don't count: an operator cancelling the storm
    after enabling must not turn the project non-fresh."""
    return (
        await pool.fetchval(
            "SELECT 1 FROM builds WHERE project_id = $1 "
            "AND status != 'cancelled' LIMIT 1",
            project_id,
        )
        is not None
    )


async def is_built(pool: asyncpg.Pool, project_id: int, commit_sha: str) -> bool:
    return (
        await pool.fetchval(
            "SELECT 1 FROM builds WHERE project_id = $1 AND commit_sha = $2 LIMIT 1",
            project_id,
            commit_sha,
        )
        is not None
    )


async def reconcile_repo(
    pool: asyncpg.Pool,
    project: RepoRecord,
    heads: list[RemoteHead],
    sink: ChangeSink,
) -> int:
    """Submit change events for unbuilt heads. Returns submit count.

    A project without any non-cancelled build record is fresh, not
    recovering from downtime: build the default branch only, the
    open-PR backlog would be stale work. PRs build on their next
    push."""
    first_contact = not await _has_builds(pool, project.id)
    submitted = 0
    for head in heads:
        if first_contact and head.pr_number is not None:
            continue
        if await is_built(pool, project.id, head.commit_sha):
            continue
        logger.info(
            "reconciliation: building unbuilt head",
            extra={
                "project": f"{project.owner}/{project.name}",
                "branch": head.branch,
                "pr": head.pr_number,
            },
        )
        await sink.submit(
            ChangeRequest(
                forge=project.forge,
                forge_repo_id=project.forge_repo_id,
                branch=head.branch,
                commit_sha=head.commit_sha,
                pr_number=head.pr_number,
                pr_author=head.pr_author,
                base_sha=head.base_sha,
            )
        )
        submitted += 1
    return submitted
