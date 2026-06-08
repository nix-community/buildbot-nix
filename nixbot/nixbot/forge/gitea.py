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

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"token {self.token}"}

    async def paginated(self, url: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            response = await self.http.get(
                f"{url}&page={page}", headers=self.auth_headers()
            )
            if response.status_code >= 400:  # noqa: PLR2004
                msg = f"Gitea request failed: {response.status_code} {response.text}"
                raise ForgeError(msg, status_code=response.status_code)
            data = response.json()
            if not data:
                return results
            results.extend(data)
            page += 1

    async def discover_repos(
        self, *, fetch_topics: bool = False
    ) -> list[DiscoveredRepo]:
        """List repos. Topics cost one extra request per repo and are
        only needed by the one-shot legacy topic import, so they are
        skipped unless `fetch_topics` is set."""
        repos = []
        for repo in await self.paginated(
            f"{self.instance_url}/api/v1/user/repos?limit=100"
        ):
            topics: list[str] = []
            if fetch_topics:
                topics_response = await self.http.get(
                    f"{self.instance_url}/api/v1/repos/"
                    f"{repo['owner']['login']}/{repo['name']}/topics",
                    headers=self.auth_headers(),
                )
                if topics_response.status_code < 400:  # noqa: PLR2004
                    # Gitea reports repos without topics as null.
                    topics = topics_response.json().get("topics") or []
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
