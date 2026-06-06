"""Private-project visibility.

Public projects are readable without login. Private projects (the
`private` flag tracked on project records during discovery) are
visible only to logged-in users whose forge account can access the
repository: the user's accessible-repo set is fetched once per user
from the forge API using the OAuth token captured at login, cached
with a configurable TTL (default 1h, negative results cached too) and
dropped on logout/session expiry. Admins see everything.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import httpx

from .auth import is_admin

if TYPE_CHECKING:
    import asyncpg

    from .auth import AuthzConfig, User

logger = logging.getLogger(__name__)

DEFAULT_TTL = 60 * 60


@dataclass
class _CacheEntry:
    repo_ids: frozenset[str]
    expires: float


class AccessCache:
    """Per-user accessible-repo-set cache (negatives cached)."""

    def __init__(self, ttl: int = DEFAULT_TTL) -> None:
        self.ttl = ttl
        self._entries: dict[str, _CacheEntry] = {}

    def get(self, user_key: str) -> frozenset[str] | None:
        entry = self._entries.get(user_key)
        if entry is None or entry.expires < time.monotonic():
            return None
        return entry.repo_ids

    def set(self, user_key: str, repo_ids: frozenset[str]) -> None:
        self._entries[user_key] = _CacheEntry(
            repo_ids=repo_ids, expires=time.monotonic() + self.ttl
        )

    def invalidate(self, user_key: str) -> None:
        self._entries.pop(user_key, None)


class RepoAccessFetcher(Protocol):
    """Fetches the set of (forge, forge_repo_id) a user can access."""

    async def accessible_repo_ids(self, user: User, token: str) -> frozenset[str]: ...


class ForgeRepoAccessFetcher:
    """Lists the user's accessible repos with their own OAuth token."""

    def __init__(self, http: httpx.AsyncClient, gitea_url: str | None = None) -> None:
        self.http = http
        self.gitea_url = gitea_url.rstrip("/") if gitea_url else None

    async def accessible_repo_ids(self, user: User, token: str) -> frozenset[str]:
        if user.provider == "github":
            return await self._github(token)
        if user.provider == "gitea" and self.gitea_url:
            return await self._gitea(token)
        return frozenset()

    async def _github(self, token: str) -> frozenset[str]:
        ids: set[str] = set()
        url: str | None = "https://api.github.com/user/repos?per_page=100"
        while url:
            response = await self.http.get(
                url, headers={"Authorization": f"Bearer {token}"}
            )
            # Raise instead of truncating: a partial/empty set cached by
            # the caller would hide private projects for the whole TTL.
            response.raise_for_status()
            ids.update(f"github:{repo['id']}" for repo in response.json())
            url = response.links.get("next", {}).get("url")
        return frozenset(ids)

    async def _gitea(self, token: str) -> frozenset[str]:
        ids: set[str] = set()
        page = 1
        while True:
            response = await self.http.get(
                f"{self.gitea_url}/api/v1/user/repos?limit=100&page={page}",
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            if not response.json():
                return frozenset(ids)
            ids.update(f"gitea:{repo['id']}" for repo in response.json())
            page += 1


class VisibilityService:
    def __init__(
        self,
        pool: asyncpg.Pool,
        authz: AuthzConfig,
        fetcher: RepoAccessFetcher | None = None,
        cache: AccessCache | None = None,
    ) -> None:
        self.pool = pool
        self.authz = authz
        self.fetcher = fetcher
        self.cache = cache or AccessCache()

    async def visible_project_ids(
        self, user: User | None, token: str | None = None
    ) -> list[int] | None:
        """None = everything visible (admins). Otherwise the project ids
        the requester may see (public + accessible private)."""
        if is_admin(user, self.authz):
            return None
        rows = await self.pool.fetch(
            "SELECT id, forge, forge_repo_id, private FROM projects"
        )
        visible = [row["id"] for row in rows if not row["private"]]
        if user is None or self.fetcher is None or token is None:
            return visible
        accessible = self.cache.get(user.qualified)
        if accessible is None:
            try:
                accessible = await self.fetcher.accessible_repo_ids(user, token)
            except httpx.HTTPError:
                # Transient forge failure: public-only for this request,
                # uncached so the next one retries.
                logger.warning(
                    "failed to fetch accessible repos",
                    extra={"user": user.qualified},
                )
                return visible
            self.cache.set(user.qualified, accessible)
        visible.extend(
            row["id"]
            for row in rows
            if row["private"] and f"{row['forge']}:{row['forge_repo_id']}" in accessible
        )
        return visible
