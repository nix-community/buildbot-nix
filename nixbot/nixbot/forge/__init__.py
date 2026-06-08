"""Forge clients and project discovery (one module per forge)."""

from .base import (
    DiscoveredRepo,
    ForgeError,
    NetrcFetchCredentialsProvider,
    filter_repos,
)
from .gitea import GiteaClient
from .github import GitHubAppClient, GitHubFetchCredentialsProvider
from .gitlab import GitlabClient

__all__ = [
    "DiscoveredRepo",
    "ForgeError",
    "GitHubAppClient",
    "GitHubFetchCredentialsProvider",
    "GiteaClient",
    "GitlabClient",
    "NetrcFetchCredentialsProvider",
    "filter_repos",
]
