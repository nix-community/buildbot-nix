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


@dataclass(frozen=True)
class RepoAccess:
    """Repo keys ("forge:forge_repo_id") a user can see, and the subset
    where the forge grants them admin permission."""

    accessible: frozenset[str]
    admin: frozenset[str]


@dataclass
class _CacheEntry:
    access: RepoAccess
    expires: float


class AccessCache:
    """Per-user repo-access cache (negatives cached)."""

    def __init__(self, ttl: int = DEFAULT_TTL) -> None:
        self.ttl = ttl
        self._entries: dict[str, _CacheEntry] = {}

    def get(self, user_key: str) -> RepoAccess | None:
        entry = self._entries.get(user_key)
        if entry is None or entry.expires < time.monotonic():
            return None
        return entry.access

    def set(self, user_key: str, access: RepoAccess) -> None:
        self._entries[user_key] = _CacheEntry(
            access=access, expires=time.monotonic() + self.ttl
        )

    def invalidate(self, user_key: str) -> None:
        self._entries.pop(user_key, None)


class RepoAccessFetcher(Protocol):
    """Fetches the repos a user can access on their forge."""

    async def repo_access(self, user: User, token: str) -> RepoAccess: ...


class ForgeRepoAccessFetcher:
    """Lists the user's accessible repos with their own OAuth token."""

    def __init__(self, http: httpx.AsyncClient, gitea_url: str | None = None) -> None:
        self.http = http
        self.gitea_url = gitea_url.rstrip("/") if gitea_url else None

    async def repo_access(self, user: User, token: str) -> RepoAccess:
        if user.provider == "github":
            return await self._github(token)
        if user.provider == "gitea" and self.gitea_url:
            return await self._gitea(token)
        return RepoAccess(frozenset(), frozenset())

    async def _github(self, token: str) -> RepoAccess:
        repos: list[dict] = []
        url: str | None = "https://api.github.com/user/repos?per_page=100"
        while url:
            response = await self.http.get(
                url, headers={"Authorization": f"Bearer {token}"}
            )
            # Raise instead of truncating: a partial/empty set cached by
            # the caller would hide private projects for the whole TTL.
            response.raise_for_status()
            repos += response.json()
            url = response.links.get("next", {}).get("url")
        return _collect("github", repos)

    async def _gitea(self, token: str) -> RepoAccess:
        repos: list[dict] = []
        page = 1
        while True:
            response = await self.http.get(
                f"{self.gitea_url}/api/v1/user/repos?limit=100&page={page}",
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            if not response.json():
                return _collect("gitea", repos)
            repos += response.json()
            page += 1


def _collect(forge: str, repos: list[dict]) -> RepoAccess:
    """Both forges report per-repo permissions in the listing itself."""
    keys = [(f"{forge}:{repo['id']}", repo) for repo in repos]
    return RepoAccess(
        accessible=frozenset(key for key, _ in keys),
        admin=frozenset(
            key for key, repo in keys if repo.get("permissions", {}).get("admin")
        ),
    )


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

    async def visible_repo_ids(
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
        access = await self._repo_access(user, token)
        if access is None:
            return visible
        visible.extend(
            row["id"]
            for row in rows
            if row["private"]
            and f"{row['forge']}:{row['forge_repo_id']}" in access.accessible
        )
        return visible

    async def toggleable_repo_ids(
        self, user: User | None, token: str | None = None
    ) -> list[int] | None:
        """Projects the requester may enable/disable. None = all
        (instance admins); forge-side repo admins get their own."""
        if is_admin(user, self.authz):
            return None
        access = await self._repo_access(user, token)
        if access is None:
            return []
        rows = await self.pool.fetch("SELECT id, forge, forge_repo_id FROM projects")
        return [
            row["id"]
            for row in rows
            if f"{row['forge']}:{row['forge_repo_id']}" in access.admin
        ]

    async def _repo_access(
        self, user: User | None, token: str | None
    ) -> RepoAccess | None:
        """None: no usable access info (anonymous, no fetcher, or the
        forge failed) — callers fall back to their public behavior."""
        if user is None or self.fetcher is None or token is None:
            return None
        access = self.cache.get(user.qualified)
        if access is None:
            try:
                access = await self.fetcher.repo_access(user, token)
            except httpx.HTTPError:
                # Transient forge failure: uncached so the next request
                # retries.
                logger.warning(
                    "failed to fetch accessible repos",
                    extra={"user": user.qualified},
                )
                return None
            self.cache.set(user.qualified, access)
        return access
