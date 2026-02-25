from __future__ import annotations

import os
import signal
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from buildbot.plugins import util
from buildbot.process.properties import Interpolate, Properties
from buildbot.util.twisted import async_to_deferred
from buildbot_gitea.auth import GiteaAuth  # type: ignore[import]
from buildbot_gitea.reporter import GiteaStatusPush  # type: ignore[import]
from pydantic import BaseModel
from twisted.logger import Logger
from twisted.python import log

from .common import (
    RepoAccessors,
    ThreadDeferredBuildStep,
    atomic_write_file,
    filter_repos,
    http_request,
    model_dump_project_cache,
    model_validate_project_cache,
    paginated_github_request,
    slugify_project_name,
)
from .nix_status_generator import BuildNixEvalStatusGenerator
from .projects import GitBackend, GitProject

if TYPE_CHECKING:
    from pathlib import Path

    from buildbot.changes.base import ChangeSource
    from buildbot.config.builder import BuilderConfig
    from buildbot.reporters.base import ReporterBase
    from buildbot.www.auth import AuthBase
    from buildbot.www.avatar import AvatarBase

    from .models import GiteaConfig

tlog = Logger()


class FilteredGiteaStatusPush(GiteaStatusPush):
    """GiteaStatusPush that only reports on Gitea projects."""

    def __init__(self, backend: GiteaBackend, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.backend = backend

    @async_to_deferred
    async def sendMessage(self, reports: list[dict[str, Any]]) -> None:
        """Filter out non-Gitea projects before sending."""
        if not reports:
            return

        # Check if this is a Gitea project
        build = reports[0]["builds"][0]
        props = Properties.fromDict(build["properties"])
        projectname = props.getProperty("projectname")

        # Handle missing or empty projectname
        if not projectname:
            log.msg("Skipping Gitea status update: projectname is missing or empty")
            return

        # Skip if projectname is not in our Gitea project set
        if projectname not in self.backend.gitea_projects:
            log.msg(
                f"Skipping Gitea status update for non-Gitea project: {projectname}"
            )
            return

        await super().sendMessage(reports)


class RepoOwnerData(BaseModel):
    login: str


class RepoData(BaseModel):
    name: str
    owner: RepoOwnerData
    full_name: str
    ssh_url: str
    default_branch: str
    topics: list[str]


class GiteaProject(GitProject):
    config: GiteaConfig
    webhook_secret: str
    data: RepoData

    def __init__(
        self, config: GiteaConfig, webhook_secret: str, data: RepoData
    ) -> None:
        self.config = config
        self.webhook_secret = webhook_secret
        self.data = data

    def get_project_url(self) -> str:
        url = urlparse(self.config.instance_url)
        if self.config.ssh_private_key_file:
            return self.data.ssh_url
        host = url.hostname
        if url.port:
            host = f"{host}:{url.port}"
        return (
            f"{url.scheme}://git:%(secret:{self.config.token_file})s@{host}/{self.name}"
        )

    def create_change_source(self) -> ChangeSource | None:
        return None

    @property
    def pretty_type(self) -> str:
        return "Gitea"

    @property
    def type(self) -> str:
        return "gitea"

    @property
    def nix_ref_type(self) -> str:
        return "gitea"

    @property
    def repo(self) -> str:
        return self.data.name

    @property
    def owner(self) -> str:
        return self.data.owner.login

    @property
    def name(self) -> str:
        return self.data.full_name

    @property
    def url(self) -> str:
        # not `html_url` because https://github.com/lab132/buildbot-gitea/blob/f569a2294ea8501ef3bcc5d5b8c777dfdbf26dcc/buildbot_gitea/webhook.py#L34
        return self.data.ssh_url

    @property
    def project_id(self) -> str:
        return slugify_project_name(self.data.full_name)

    @property
    def default_branch(self) -> str:
        return self.data.default_branch

    @property
    def topics(self) -> list[str]:
        # note that Gitea doesn't by default put this data here, we splice it in, in `refresh_projects`
        return self.data.topics

    @property
    def belongs_to_org(self) -> bool:
        # TODO Gitea doesn't include this information
        return False  # self.data["owner"]["type"] == "Organization"

    @property
    def private_key_path(self) -> Path | None:
        return self.config.ssh_private_key_file

    @property
    def known_hosts_path(self) -> Path | None:
        return self.config.ssh_known_hosts_file


class GiteaBackend(GitBackend):
    config: GiteaConfig
    webhook_secret: str
    instance_url: str
    gitea_projects: set[str]

    def __init__(self, config: GiteaConfig, instance_url: str) -> None:
        self.config = config
        self.instance_url = instance_url
        self.gitea_projects = set()  # Will be populated by load_projects

    def create_reload_builder(self, worker_names: list[str]) -> BuilderConfig:
        """Updates the flake an opens a PR for it."""
        factory = util.BuildFactory()
        factory.addStep(
            ReloadGiteaProjects(
                self.config, self.config.project_cache_file, backend=self
            ),
        )
        factory.addStep(
            CreateGiteaProjectHooks(
                self.config,
                self.instance_url,
            ),
        )
        return util.BuilderConfig(
            name=self.reload_builder_name,
            workernames=worker_names,
            factory=factory,
        )

    def create_reporter(self) -> ReporterBase:
        return FilteredGiteaStatusPush(
            backend=self,
            baseURL=self.config.instance_url,
            token=Interpolate(self.config.token),
            context=Interpolate("buildbot/%(prop:status_name)s"),
            context_pr=Interpolate("buildbot/%(prop:status_name)s"),
            generators=[
                BuildNixEvalStatusGenerator(),
            ],
        )

    def create_change_hook(self) -> dict[str, Any]:
        return {
            "secret": self.config.webhook_secret,
            # The "mergeable" field is a bit buggy,
            # we already do the merge locally anyway.
            "onlyMergeablePullRequest": False,
        }

    def create_avatar_method(self) -> AvatarBase | None:
        return None

    def create_auth(self) -> AuthBase:
        if self.config.oauth_id is None:
            msg = "Gitea requires an OAuth ID to be set"
            raise ValueError(msg)
        return GiteaAuth(
            self.config.instance_url,
            self.config.oauth_id,
            self.config.oauth_secret,
        )

    def load_projects(self) -> list[GitProject]:
        if not self.config.project_cache_file.exists():
            return []

        repos: list[RepoData] = filter_repos(
            self.config.filters,
            sorted(
                model_validate_project_cache(RepoData, self.config.project_cache_file),
                key=lambda repo: repo.full_name,
            ),
            RepoAccessors(
                repo_name=lambda repo: repo.full_name,
                user=lambda repo: repo.owner.login,
                topics=lambda repo: repo.topics,
            ),
        )
        repo_names: list[str] = [repo.owner.login + "/" + repo.name for repo in repos]
        tlog.info(
            f"Loading {len(repos)} cached repositories: [{', '.join(repo_names)}]"
        )

        # Populate the set of Gitea projects
        self.gitea_projects = {repo.full_name for repo in repos}

        return [
            GiteaProject(
                self.config, self.config.webhook_secret, RepoData.model_validate(repo)
            )
            for repo in repos
        ]

    def update_projects(self, projects: set[str]) -> None:
        # Replace atomically to avoid mutating a set read elsewhere
        self.gitea_projects = projects

    def are_projects_cached(self) -> bool:
        return self.config.project_cache_file.exists()

    @property
    def type(self) -> str:
        return "gitea"

    @property
    def pretty_type(self) -> str:
        return "Gitea"

    @property
    def reload_builder_name(self) -> str:
        return "reload-gitea-projects"

    @property
    def change_hook_name(self) -> str:
        return "gitea"


@dataclass
class RepoHookConfig:
    token: str
    webhook_secret: str
    owner: str
    repo: str
    gitea_url: str
    instance_url: str


def create_repo_hook(config: RepoHookConfig) -> None:
    hooks = paginated_github_request(
        f"{config.gitea_url}/api/v1/repos/{config.owner}/{config.repo}/hooks?limit=100",
        config.token,
    )
    hook_config = {
        "url": config.instance_url + "change_hook/gitea",
        "content_type": "json",
        "insecure_ssl": "0",
        "secret": config.webhook_secret,
    }
    data = {
        "name": "web",
        "active": True,
        "events": ["push", "pull_request"],
        "config": hook_config,
        "type": "gitea",
    }
    headers = {
        "Authorization": f"token {config.token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    for hook in hooks:
        if hook["config"]["url"] == config.instance_url + "change_hook/gitea":
            log.msg(f"hook for {config.owner}/{config.repo} already exists")
            return

    log.msg(f"creating hook for {config.owner}/{config.repo}")
    http_request(
        f"{config.gitea_url}/api/v1/repos/{config.owner}/{config.repo}/hooks",
        method="POST",
        headers=headers,
        data=data,
    )


class CreateGiteaProjectHooks(ThreadDeferredBuildStep):
    name = "create_gitea_project_hooks"

    config: GiteaConfig
    instance_url: str

    def __init__(
        self,
        config: GiteaConfig,
        instance_url: str,
        **kwargs: Any,
    ) -> None:
        self.config = config
        self.instance_url = instance_url
        super().__init__(**kwargs)

    def run_deferred(self) -> None:
        repos = model_validate_project_cache(RepoData, self.config.project_cache_file)

        for repo in repos:
            hook_config = RepoHookConfig(
                token=self.config.token,
                webhook_secret=self.config.webhook_secret,
                owner=repo.owner.login,
                repo=repo.name,
                gitea_url=self.config.instance_url,
                instance_url=self.instance_url,
            )
            create_repo_hook(hook_config)

    def run_post(self) -> Any:
        os.kill(os.getpid(), signal.SIGHUP)
        return util.SUCCESS


class ReloadGiteaProjects(ThreadDeferredBuildStep):
    name = "reload_gitea_projects"

    config: GiteaConfig
    project_cache_file: Path

    def __init__(
        self,
        config: GiteaConfig,
        project_cache_file: Path,
        backend: GiteaBackend | None = None,
        **kwargs: Any,
    ) -> None:
        self.config = config
        self.project_cache_file = project_cache_file
        self.backend = backend
        super().__init__(**kwargs)

    def run_deferred(self) -> None:
        repos: list[RepoData] = filter_repos(
            self.config.filters,
            refresh_projects(self.config),
            RepoAccessors(
                repo_name=lambda repo: repo.full_name,
                user=lambda repo: repo.owner.login,
                topics=lambda repo: repo.topics,
            ),
        )

        atomic_write_file(self.project_cache_file, model_dump_project_cache(repos))

        # Update the backend's in-memory gitea_projects set directly
        if self.backend:
            self.backend.update_projects({repo.full_name for repo in repos})
            tlog.info(f"Updated backend gitea_projects with {len(repos)} projects")

    def run_post(self) -> Any:
        return util.SUCCESS


def refresh_projects(config: GiteaConfig) -> list[RepoData]:
    repos = []

    for repo in paginated_github_request(
        f"{config.instance_url}/api/v1/user/repos?limit=100",
        config.token,
    ):
        if not repo["permissions"]["admin"]:
            name = repo["full_name"]
            log.msg(
                f"skipping {name} because we do not have admin privileges, needed for hook management",
            )
        else:
            try:
                # Gitea doesn't include topics in the default repo listing, unlike GitHub
                topics: list[str] = http_request(
                    f"{config.instance_url}/api/v1/repos/{repo['owner']['login']}/{repo['name']}/topics",
                    headers={"Authorization": f"token {config.token}"},
                ).json()["topics"]
                repo["topics"] = topics
                repos.append(RepoData.model_validate(repo))
            except OSError:
                pass

    return repos
