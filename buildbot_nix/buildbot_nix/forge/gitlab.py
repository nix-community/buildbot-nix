"""GitLab client: personal/group/project access token (api scope),
discovery via /api/v4/projects?membership=true. No GitHub-App
equivalent exists, so auth follows the Gitea model."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from .base import DiscoveredRepo, ForgeError


class GitlabClient:
    def __init__(
        self,
        instance_url: str,
        token: str,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.instance_url = instance_url.rstrip("/")
        self.token = token
        self.http = http or httpx.AsyncClient()

    def auth_headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self.token}

    def project_api_url(self, owner: str, repo: str) -> str:
        # GitLab accepts the URL-encoded full path wherever it takes a
        # numeric project id; namespaces may be nested (a/b/c).
        return (
            f"{self.instance_url}/api/v4/projects/{quote(f'{owner}/{repo}', safe='')}"
        )

    async def paginated(self, url: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        next_url: str | None = url
        while next_url:
            response = await self.http.get(next_url, headers=self.auth_headers())
            if response.status_code >= 400:  # noqa: PLR2004
                msg = f"GitLab request failed: {response.status_code} {response.text}"
                raise ForgeError(msg)
            results.extend(response.json())
            next_url = response.links.get("next", {}).get("url")
        return results

    async def discover_repos(self) -> list[DiscoveredRepo]:
        repos = []
        for repo in await self.paginated(
            f"{self.instance_url}/api/v4/projects"
            "?membership=true&archived=false&per_page=100"
        ):
            owner, _, name = repo["path_with_namespace"].rpartition("/")
            repos.append(
                DiscoveredRepo(
                    forge="gitlab",
                    forge_repo_id=str(repo["id"]),
                    owner=owner,
                    repo=name,
                    default_branch=repo.get("default_branch") or "main",
                    clone_url=repo["http_url_to_repo"],
                    private=repo.get("visibility") != "public",
                    topics=tuple(repo.get("topics") or ()),
                )
            )
        return repos
