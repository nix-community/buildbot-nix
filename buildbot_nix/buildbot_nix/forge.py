"""Forge clients and project discovery.

httpx-based async port of github/, github_projects.py and
gitea_projects.py discovery:

- GitHub: App auth only (token mode dropped). App JWTs are signed via
  openssl with the operator-supplied private key; short-lived
  installation tokens are minted per installation and cached until
  shortly before expiry. Private-repo fetches get per-fetch credentials
  through the gitrepo CredentialsProvider interface (netrc with
  x-access-token).
- Gitea: personal token; repos listed via /api/v1/user/repos with
  topics fetched per repo (as before).
- Discovery filters: repoAllowlist/userAllowlist are a security
  boundary applied at discovery time; the topic filter is only a legacy
  import aid (see projects.py for the one-shot enablement import).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from .gitrepo import FetchCredentials

if TYPE_CHECKING:
    from .config import RepoFilters

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


class ForgeError(Exception):
    pass


@dataclass(frozen=True)
class DiscoveredRepo:
    forge: str  # "github" | "gitea"
    forge_repo_id: str  # stable numeric id as string
    owner: str
    repo: str
    default_branch: str
    clone_url: str
    private: bool
    topics: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return f"{self.owner}/{self.repo}"


def filter_repos(
    filters: RepoFilters, repos: list[DiscoveredRepo]
) -> list[DiscoveredRepo]:
    """Port of common.filter_repos: allow everything when both
    allowlists are unset; otherwise a repo passes if its owner is in
    the user allowlist or its full name in the repo allowlist.

    The topic is deliberately not a discovery filter: it is only the
    one-shot legacy enablement import aid (projects.py); filtering on
    it here would keep non-topic repos out of the projects table and
    thus impossible to enable in the web UI."""
    no_allowlists = filters.user_allowlist is None and filters.repo_allowlist is None
    return [
        repo
        for repo in repos
        if (
            no_allowlists
            or (
                filters.user_allowlist is not None
                and repo.owner in filters.user_allowlist
            )
            or (
                filters.repo_allowlist is not None
                and repo.name in filters.repo_allowlist
            )
        )
    ]


# --- GitHub App auth -----------------------------------------------------------


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


async def generate_app_jwt(
    app_id: int, private_key_file: Path, lifetime: timedelta = timedelta(minutes=10)
) -> tuple[str, datetime]:
    """RS256-signed GitHub App JWT, signed via openssl (port of
    github/jwt_token.py)."""
    now = datetime.now(tz=UTC)
    iat = now - timedelta(seconds=60)
    exp = iat + lifetime
    payload = {
        "iat": int(iat.timestamp()),
        "exp": int(exp.timestamp()),
        "iss": str(app_id),
    }
    header = {"alg": "RS256", "typ": "JWT"}
    signing_input = (
        f"{_base64url(json.dumps(header).encode())}."
        f"{_base64url(json.dumps(payload).encode())}"
    )
    proc = await asyncio.create_subprocess_exec(
        "openssl",
        "dgst",
        "-binary",
        "-sha256",
        "-sign",
        str(private_key_file),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=os.environ.get("CREDENTIALS_DIRECTORY"),
    )
    stdout, stderr = await proc.communicate(signing_input.encode())
    if proc.returncode != 0:
        msg = f"failed to sign GitHub App JWT: {stderr.decode(errors='replace')}"
        raise ForgeError(msg)
    return f"{signing_input}.{_base64url(stdout)}", exp


@dataclass
class _CachedToken:
    token: str
    expires: datetime


class GitHubAppClient:
    """GitHub App API client with per-installation token cache."""

    def __init__(
        self,
        app_id: int,
        private_key_file: Path,
        http: httpx.AsyncClient | None = None,
        api_url: str = GITHUB_API,
    ) -> None:
        self.app_id = app_id
        self.private_key_file = private_key_file
        self.api_url = api_url
        self.http = http or httpx.AsyncClient()
        self._jwt: _CachedToken | None = None
        self._installation_tokens: dict[int, _CachedToken] = {}
        # owner/repo (lowercase) -> installation id; filled by discovery.
        self.repo_installations: dict[str, int] = {}

    async def _app_jwt(self) -> str:
        if self._jwt is None or self._jwt.expires - datetime.now(tz=UTC) < timedelta(
            minutes=2
        ):
            token, expires = await generate_app_jwt(self.app_id, self.private_key_file)
            self._jwt = _CachedToken(token, expires)
        return self._jwt.token

    async def installation_token(self, installation_id: int) -> str:
        cached = self._installation_tokens.get(installation_id)
        if cached is not None and cached.expires > datetime.now(tz=UTC):
            return cached.token
        response = await self.http.post(
            f"{self.api_url}/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {await self._app_jwt()}"},
        )
        if response.status_code >= 400:  # noqa: PLR2004
            msg = f"failed to mint installation token: {response.status_code} {response.text}"
            raise ForgeError(msg)
        token = response.json()["token"]
        # GitHub installation tokens last 60 minutes; refresh at 80%.
        self._installation_tokens[installation_id] = _CachedToken(
            token, datetime.now(tz=UTC) + timedelta(minutes=48)
        )
        return token

    async def _paginated(
        self, url: str, token: str, subkey: str | None = None
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        next_url: str | None = url
        while next_url:
            response = await self.http.get(
                next_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            if response.status_code >= 400:  # noqa: PLR2004
                msg = f"GitHub request failed: {response.status_code} {response.text}"
                raise ForgeError(msg)
            data = response.json()
            results.extend(data[subkey] if subkey else data)
            next_url = response.links.get("next", {}).get("url")
        return results

    async def list_installations(self) -> list[int]:
        installations = await self._paginated(
            f"{self.api_url}/app/installations?per_page=100", await self._app_jwt()
        )
        return [installation["id"] for installation in installations]

    async def discover_repos(self) -> list[DiscoveredRepo]:
        repos: list[DiscoveredRepo] = []
        for installation_id in await self.list_installations():
            token = await self.installation_token(installation_id)
            for repo in await self._paginated(
                f"{self.api_url}/installation/repositories?per_page=100",
                token,
                subkey="repositories",
            ):
                discovered = DiscoveredRepo(
                    forge="github",
                    forge_repo_id=str(repo["id"]),
                    owner=repo["owner"]["login"],
                    repo=repo["name"],
                    default_branch=repo.get("default_branch") or "main",
                    clone_url=repo["clone_url"],
                    private=repo.get("private", False),
                    topics=tuple(repo.get("topics", [])),
                )
                repos.append(discovered)
                self.repo_installations[discovered.name.lower()] = installation_id
        return repos


class GitHubFetchCredentialsProvider:
    """Per-fetch short-lived installation tokens for private repos
    (plugs into the gitrepo CredentialsProvider interface)."""

    def __init__(self, client: GitHubAppClient, host: str = "github.com") -> None:
        self.client = client
        self.host = host
        self._netrc_dir = Path(tempfile.mkdtemp(prefix="github-netrc-"))

    async def get(self, repo_url: str) -> FetchCredentials:
        name = _repo_name_from_url(repo_url)
        installation_id = (
            self.client.repo_installations.get(name.lower()) if name else None
        )
        if installation_id is None:
            return FetchCredentials()
        token = await self.client.installation_token(installation_id)
        # GitHub Enterprise: match the host git actually fetches from.
        host = urlparse(repo_url).hostname or self.host
        netrc = self._netrc_dir / f"{installation_id}.netrc"
        _write_netrc_atomic(
            netrc, f"machine {host} login x-access-token password {token}\n"
        )
        return FetchCredentials(netrc_file=netrc)


def _write_netrc_atomic(netrc: Path, content: str) -> None:
    """Atomic replace: git/curl re-reads the netrc mid-fetch, so an
    in-place rewrite would expose a truncated file to concurrent
    fetches of the same installation."""
    fd, tmp_name = tempfile.mkstemp(dir=netrc.parent)  # 0600 by default
    os.close(fd)
    tmp = Path(tmp_name)
    tmp.write_text(content)
    tmp.replace(netrc)


class GiteaFetchCredentialsProvider:
    """Credentials for fetching Gitea repositories: the API token as a
    netrc entry for HTTPS clone URLs (Gitea accepts the token as basic
    auth password), plus the optional per-instance SSH key for SSH
    remotes (gitea.sshPrivateKeyFile)."""

    def __init__(
        self,
        instance_url: str,
        token: str,
        ssh_private_key_file: Path | None = None,
        ssh_known_hosts_file: Path | None = None,
    ) -> None:
        host = httpx.URL(instance_url).host
        self._netrc = Path(tempfile.mkdtemp(prefix="gitea-netrc-")) / "netrc"
        self._netrc.touch(mode=0o600)
        self._netrc.write_text(f"machine {host} login oauth2 password {token}\n")
        self.ssh_private_key_file = ssh_private_key_file
        self.ssh_known_hosts_file = ssh_known_hosts_file

    async def get(self, repo_url: str) -> FetchCredentials:  # noqa: ARG002
        return FetchCredentials(
            netrc_file=self._netrc,
            ssh_private_key_file=self.ssh_private_key_file,
            ssh_known_hosts_file=self.ssh_known_hosts_file,
        )


def _repo_name_from_url(repo_url: str) -> str | None:
    # https://github.com/owner/repo(.git)
    parts = repo_url.rstrip("/").removesuffix(".git").split("/")
    if len(parts) < 2:  # noqa: PLR2004
        return None
    return f"{parts[-2]}/{parts[-1]}"


# --- Gitea ---------------------------------------------------------------------


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
            # Webhook management needs admin permission; without it an
            # enabled project could never receive events.
            if not (repo.get("permissions") or {}).get("admin", True):
                logger.info(
                    "skipping repo without admin permission",
                    extra={"repo": f"{repo['owner']['login']}/{repo['name']}"},
                )
                continue
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
