"""Shared forge types: errors, the discovery model, allowlist
filtering and netrc-based fetch credentials."""

from __future__ import annotations

import atexit
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from buildbot_nix.gitrepo import FetchCredentials

if TYPE_CHECKING:
    from buildbot_nix.config import RepoFilters


class ForgeError(Exception):
    """A forge API call failed. `status_code` carries the HTTP status
    so callers can classify (e.g. 403 = missing permission) without
    substring-matching the message, which may contain ids/bodies."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class DiscoveredRepo:
    forge: str  # "github" | "gitea" | "gitlab"
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


class NetrcFetchCredentialsProvider:
    """Credentials for fetching from token-auth forges (Gitea, GitLab):
    the API token as a netrc entry for HTTPS clone URLs (both accept it
    as basic auth password for user oauth2), plus the optional
    per-instance SSH key for SSH remotes.

    SECURITY: neither Gitea nor GitLab can mint short-lived per-repo
    fetch tokens the way GitHub Apps can, so the operator's API token
    is exposed to every fetch — including PR-controlled paths such as
    submodule fetches — for every repo on the instance. Use a dedicated
    machine account with minimal scopes (Gitea: read:repository;
    GitLab: a read_repository-scoped token) rather than a personal
    admin token."""

    def __init__(
        self,
        instance_url: str,
        token: str,
        ssh_private_key_file: Path | None = None,
        ssh_known_hosts_file: Path | None = None,
    ) -> None:
        host = httpx.URL(instance_url).host
        self._netrc_dir = Path(tempfile.mkdtemp(prefix="forge-netrc-"))
        atexit.register(self.cleanup)
        self._netrc = self._netrc_dir / "netrc"
        self._netrc.touch(mode=0o600)
        self._netrc.write_text(f"machine {host} login oauth2 password {token}\n")
        self._token = token
        self.ssh_private_key_file = ssh_private_key_file
        self.ssh_known_hosts_file = ssh_known_hosts_file

    def cleanup(self) -> None:
        shutil.rmtree(self._netrc_dir, ignore_errors=True)

    async def get(self, repo_url: str) -> FetchCredentials:  # noqa: ARG002
        return FetchCredentials(
            netrc_file=self._netrc,
            token=self._token,
            ssh_private_key_file=self.ssh_private_key_file,
            ssh_known_hosts_file=self.ssh_known_hosts_file,
        )
