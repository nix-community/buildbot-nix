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

from .webhooks import ChangeRequest

if TYPE_CHECKING:
    import asyncpg

    from .forge import GiteaClient, GitHubAppClient
    from .projects import ProjectRecord
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
    client: GitHubAppClient, project: ProjectRecord
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
    pulls = await client._paginated(  # noqa: SLF001
        f"{repo_url}/pulls?state=open&per_page=100", token
    )
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


async def gitea_heads(client: GiteaClient, project: ProjectRecord) -> list[RemoteHead]:
    repo_url = f"{client.instance_url}/api/v1/repos/{project.owner}/{project.name}"
    heads = []
    response = await client.http.get(
        f"{repo_url}/branches/{project.default_branch}",
        headers=client._headers(),  # noqa: SLF001
    )
    if response.status_code < 400:  # noqa: PLR2004
        heads.append(
            RemoteHead(
                branch=project.default_branch,
                commit_sha=response.json()["commit"]["id"],
            )
        )
    pulls = await client._paginated(f"{repo_url}/pulls?state=open&limit=50")  # noqa: SLF001
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


async def is_built(pool: asyncpg.Pool, project_id: int, commit_sha: str) -> bool:
    return (
        await pool.fetchval(
            "SELECT 1 FROM builds WHERE project_id = $1 AND commit_sha = $2 LIMIT 1",
            project_id,
            commit_sha,
        )
        is not None
    )


async def reconcile_project(
    pool: asyncpg.Pool,
    project: ProjectRecord,
    heads: list[RemoteHead],
    sink: ChangeSink,
) -> int:
    """Submit change events for unbuilt heads. Returns submit count."""
    submitted = 0
    for head in heads:
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
