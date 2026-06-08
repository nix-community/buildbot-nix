"""GitHub App auth and discovery. App JWTs are signed via openssl
with the operator-supplied private key; short-lived installation
tokens are minted per installation and cached until shortly before
expiry. Private-repo fetches get per-fetch credentials through the
gitrepo CredentialsProvider interface (netrc with x-access-token)."""

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
from typing import Any
from urllib.parse import urlparse

import httpx

from buildbot_nix.gitrepo import FetchCredentials

from .base import DiscoveredRepo, ForgeError

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


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
        # Keyed by (installation id, repo scope); None = installation-wide.
        self._installation_tokens: dict[
            tuple[int, tuple[str, ...] | None], _CachedToken
        ] = {}
        # owner/repo (lowercase) -> installation id; filled by discovery.
        self.repo_installations: dict[str, int] = {}

    async def _app_jwt(self) -> str:
        if self._jwt is None or self._jwt.expires - datetime.now(tz=UTC) < timedelta(
            minutes=2
        ):
            token, expires = await generate_app_jwt(self.app_id, self.private_key_file)
            self._jwt = _CachedToken(token, expires)
        return self._jwt.token

    async def installation_token(
        self, installation_id: int, repositories: tuple[str, ...] | None = None
    ) -> str:
        """Mint (or reuse) an installation token. `repositories` limits
        the token to those repo names; pass it for any token that can
        leak into PR-controlled paths (e.g. fetch credentials)."""
        cache_key = (installation_id, repositories)
        cached = self._installation_tokens.get(cache_key)
        if cached is not None and cached.expires > datetime.now(tz=UTC):
            return cached.token
        response = await self.http.post(
            f"{self.api_url}/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {await self._app_jwt()}"},
            json={"repositories": list(repositories)} if repositories else None,
        )
        if response.status_code >= 400:  # noqa: PLR2004
            msg = f"failed to mint installation token: {response.status_code} {response.text}"
            raise ForgeError(msg, status_code=response.status_code)
        token = response.json()["token"]
        # GitHub installation tokens last 60 minutes; refresh at 80%.
        self._installation_tokens[cache_key] = _CachedToken(
            token, datetime.now(tz=UTC) + timedelta(minutes=48)
        )
        return token

    async def paginated(
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
                raise ForgeError(msg, status_code=response.status_code)
            data = response.json()
            results.extend(data[subkey] if subkey else data)
            next_url = response.links.get("next", {}).get("url")
        return results

    async def check_app_webhook(self, base_url: str) -> list[str]:
        """Return problems with the GitHub App's webhook configuration.

        All events arrive via the App-level webhook (no per-repo hooks),
        so a missing URL or event subscription silently disables CI.
        """
        expected = f"{base_url.rstrip('/')}/webhooks/github"
        legacy = f"{base_url.rstrip('/')}/change_hook/github"
        jwt = await self._app_jwt()
        headers = {
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/vnd.github+json",
        }
        problems: list[str] = []
        response = await self.http.get(
            f"{self.api_url}/app/hook/config", headers=headers
        )
        if response.status_code >= 400:  # noqa: PLR2004
            msg = f"failed to fetch app webhook config: {response.status_code} {response.text}"
            raise ForgeError(msg, status_code=response.status_code)
        url = response.json().get("url") or ""
        if url not in (expected, legacy):
            problems.append(
                f"GitHub App webhook URL is {url!r}, expected {expected!r}; "
                "set it in the app settings and mark the webhook Active"
            )
        response = await self.http.get(f"{self.api_url}/app", headers=headers)
        if response.status_code >= 400:  # noqa: PLR2004
            msg = (
                f"failed to fetch app metadata: {response.status_code} {response.text}"
            )
            raise ForgeError(msg, status_code=response.status_code)
        events = set(response.json().get("events") or [])
        problems.extend(
            f"GitHub App is not subscribed to the {required!r} event; "
            "enable it under the app's 'Permissions & events' settings"
            for required in ("push", "pull_request")
            if required not in events
        )
        return problems

    async def list_installations(self) -> list[int]:
        installations = await self.paginated(
            f"{self.api_url}/app/installations?per_page=100", await self._app_jwt()
        )
        return [installation["id"] for installation in installations]

    async def discover_repos(self) -> list[DiscoveredRepo]:
        repos: list[DiscoveredRepo] = []
        for installation_id in await self.list_installations():
            token = await self.installation_token(installation_id)
            for repo in await self.paginated(
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
        if name is None:
            return FetchCredentials()
        installation_id = self.client.repo_installations.get(name.lower())
        if installation_id is None:
            return FetchCredentials()
        # Fetch credentials reach PR-controlled paths (submodule
        # fetches), so the token must not cover the whole installation.
        repo_name = name.split("/")[-1]
        token = await self.client.installation_token(
            installation_id, repositories=(repo_name,)
        )
        # GitHub Enterprise: match the host git actually fetches from.
        host = urlparse(repo_url).hostname or self.host
        netrc = self._netrc_dir / f"{installation_id}-{repo_name}.netrc"
        _write_netrc_atomic(
            netrc, f"machine {host} login x-access-token password {token}\n"
        )
        return FetchCredentials(netrc_file=netrc, token=token)


def _write_netrc_atomic(netrc: Path, content: str) -> None:
    """Atomic replace: git/curl re-reads the netrc mid-fetch, so an
    in-place rewrite would expose a truncated file to concurrent
    fetches of the same installation."""
    fd, tmp_name = tempfile.mkstemp(dir=netrc.parent)  # 0600 by default
    os.close(fd)
    tmp = Path(tmp_name)
    tmp.write_text(content)
    tmp.replace(netrc)


def _repo_name_from_url(repo_url: str) -> str | None:
    # https://github.com/owner/repo(.git)
    parts = repo_url.rstrip("/").removesuffix(".git").split("/")
    if len(parts) < 2:  # noqa: PLR2004
        return None
    return f"{parts[-2]}/{parts[-1]}"
