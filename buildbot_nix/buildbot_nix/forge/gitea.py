"""Gitea client: personal-token auth, discovery via
/api/v1/user/repos with topics fetched per repo."""

from __future__ import annotations

from typing import Any

import httpx

from .base import DiscoveredRepo, ForgeError


class GiteaClient:
    def __init__(
        self,
        instance_url: str,
        token: str,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.instance_url = instance_url.rstrip("/")
        self.token = token
        self.http = http or httpx.AsyncClient()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"token {self.token}"}

    async def _paginated(self, url: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            response = await self.http.get(
                f"{url}&page={page}", headers=self._headers()
            )
            if response.status_code >= 400:  # noqa: PLR2004
                msg = f"Gitea request failed: {response.status_code} {response.text}"
                raise ForgeError(msg)
            data = response.json()
            if not data:
                return results
            results.extend(data)
            page += 1

    async def discover_repos(self) -> list[DiscoveredRepo]:
        repos = []
        for repo in await self._paginated(
            f"{self.instance_url}/api/v1/user/repos?limit=100"
        ):
            topics_response = await self.http.get(
                f"{self.instance_url}/api/v1/repos/"
                f"{repo['owner']['login']}/{repo['name']}/topics",
                headers=self._headers(),
            )
            topics = (
                topics_response.json().get("topics", [])
                if topics_response.status_code < 400  # noqa: PLR2004
                else []
            )
            repos.append(
                DiscoveredRepo(
                    forge="gitea",
                    forge_repo_id=str(repo["id"]),
                    owner=repo["owner"]["login"],
                    repo=repo["name"],
                    default_branch=repo.get("default_branch") or "main",
                    clone_url=repo["clone_url"],
                    private=repo.get("private", False),
                    topics=tuple(topics),
                )
            )
        return repos
