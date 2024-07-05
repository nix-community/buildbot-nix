import json
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from buildbot.config.builder import BuilderConfig
from buildbot.plugins import util
from buildbot.process.properties import Interpolate
from buildbot.reporters.base import ReporterBase
from buildbot.www.auth import AuthBase
from buildbot.www.avatar import AvatarBase
from buildbot_gitea.auth import GiteaAuth  # type: ignore[import]
from buildbot_gitea.reporter import GiteaStatusPush  # type: ignore[import]
from twisted.logger import Logger
from twisted.python import log

from .common import (
    ThreadDeferredBuildStep,
    atomic_write_file,
    filter_repos_by_topic,
    http_request,
    paginated_github_request,
    slugify_project_name,
)
from .projects import GitBackend, GitProject
from .secrets import read_secret_file

tlog = Logger()


@dataclass
class GiteaConfig:
    instance_url: str
    oauth_id: str | None

    oauth_secret_name: str = "gitea-oauth-secret"
    token_secret_name: str = "gitea-token"
    webhook_secret_name: str = "gitea-webhook-secret"
    project_cache_file: Path = Path("gitea-project-cache.json")
    topic: str | None = "build-with-buildbot"

    def oauth_secret(self) -> str:
        return read_secret_file(self.oauth_secret_name)

    def token(self) -> str:
        return read_secret_file(self.token_secret_name)


class GiteaProject(GitProject):
    config: GiteaConfig
    webhook_secret: str
    data: dict[str, Any]

    def __init__(
        self, config: GiteaConfig, webhook_secret: str, data: dict[str, Any]
    ) -> None:
        self.config = config
        self.webhook_secret = webhook_secret
        self.data = data

    def get_project_url(self) -> str:
        url = urlparse(self.config.instance_url)
        return f"{url.scheme}://git:%(secret:{self.config.token_secret_name})s@{url.hostname}/{self.name}"

    @property
    def pretty_type(self) -> str:
        return "Gitea"

    @property
    def type(self) -> str:
        return "gitea"

    @property
    def repo(self) -> str:
        return self.data["name"]

    @property
    def owner(self) -> str:
        return self.data["owner"]["login"]

    @property
    def name(self) -> str:
        return self.data["full_name"]

    @property
    def url(self) -> str:
        # not `html_url` because https://github.com/lab132/buildbot-gitea/blob/f569a2294ea8501ef3bcc5d5b8c777dfdbf26dcc/buildbot_gitea/webhook.py#L34
        return self.data["ssh_url"]

    @property
    def project_id(self) -> str:
        return slugify_project_name(self.data["full_name"])

    @property
    def default_branch(self) -> str:
        return self.data["default_branch"]

    @property
    def topics(self) -> list[str]:
        # note that Gitea doesn't by default put this data here, we splice it in, in `refresh_projects`
        return self.data["topics"]

    @property
    def belongs_to_org(self) -> bool:
        # TODO Gitea doesn't include this information
        return False  # self.data["owner"]["type"] == "Organization"


class GiteaBackend(GitBackend):
    config: GiteaConfig
    webhook_secret: str

    def __init__(self, config: GiteaConfig) -> None:
        self.config = config
        self.webhook_secret = read_secret_file(self.config.webhook_secret_name)

    def create_reload_builder(self, worker_names: list[str]) -> BuilderConfig:
        """Updates the flake an opens a PR for it."""
        factory = util.BuildFactory()
        factory.addStep(
            ReloadGiteaProjects(self.config, self.config.project_cache_file),
        )
        factory.addStep(
            CreateGiteaProjectHooks(
                self.config, self.config.project_cache_file, self.webhook_secret
            ),
        )
        return util.BuilderConfig(
            name=self.reload_builder_name,
            workernames=worker_names,
            factory=factory,
        )

    def create_reporter(self) -> ReporterBase:
        return GiteaStatusPush(
            self.config.instance_url,
            Interpolate(self.config.token()),
            context=Interpolate("buildbot/%(prop:status_name)s"),
            context_pr=Interpolate("buildbot/%(prop:status_name)s"),
        )

    def create_change_hook(self) -> dict[str, Any]:
        return {
            "secret": self.webhook_secret,
            # The "mergable" field is a bit buggy,
            # we already do the merge locally anyway.
            "onlyMergeablePullRequest": False,
        }

    def create_avatar_method(self) -> AvatarBase | None:
        return None

    def create_auth(self) -> AuthBase:
        assert self.config.oauth_id is not None, "Gitea requires an OAuth ID to be set"
        return GiteaAuth(
            self.config.instance_url,
            self.config.oauth_id,
            self.config.oauth_secret(),
        )

    def load_projects(self) -> list["GitProject"]:
        if not self.config.project_cache_file.exists():
            return []

        repos: list[dict[str, Any]] = filter_repos_by_topic(
            self.config.topic,
            sorted(
                json.loads(self.config.project_cache_file.read_text()),
                key=lambda x: x["full_name"],
            ),
            lambda repo: repo["topics"],
        )
        repo_names: list[str] = [
            repo["owner"]["login"] + "/" + repo["name"] for repo in repos
        ]
        tlog.info(
            f"Loading {len(repos)} cached repositories: [{', '.join(repo_names)}]"
        )

        return [GiteaProject(self.config, self.webhook_secret, repo) for repo in repos]

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


def create_repo_hook(
    token: str, webhook_secret: str, owner: str, repo: str, webhook_url: str
) -> None:
    hooks = paginated_github_request(
        f"{webhook_url}/api/v1/repos/{owner}/{repo}/hooks?limit=100",
        token,
    )
    config = dict(
        url=webhook_url + "change_hook/gitea",
        content_type="json",
        insecure_ssl="0",
        secret=webhook_secret,
    )
    data = dict(
        name="web",
        active=True,
        events=["push", "pull_request"],
        config=config,
        type="gitea",
    )
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    for hook in hooks:
        if hook["config"]["url"] == webhook_url + "change_hook/gitea":
            log.msg(f"hook for {owner}/{repo} already exists")
            return

    log.msg(f"creating hook for {owner}/{repo}")
    http_request(
        f"{webhook_url}/api/v1/repos/{owner}/{repo}/hooks",
        method="POST",
        headers=headers,
        data=data,
    )


class CreateGiteaProjectHooks(ThreadDeferredBuildStep):
    name = "create_gitea_project_hooks"

    config: GiteaConfig
    project_cache_file: Path
    webhook_secret: str

    def __init__(
        self,
        config: GiteaConfig,
        project_cache_file: Path,
        webhook_secret: str,
        **kwargs: Any,
    ) -> None:
        self.config = config
        self.project_cache_file = project_cache_file
        self.webhook_secret = webhook_secret
        super().__init__(**kwargs)

    def run_deferred(self) -> None:
        repos = json.loads(self.project_cache_file.read_text())

        for repo in repos:
            create_repo_hook(
                self.config.token(),
                self.webhook_secret,
                repo["owner"]["login"],
                repo["name"],
                self.config.instance_url,
            )

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
        **kwargs: Any,
    ) -> None:
        self.config = config
        self.project_cache_file = project_cache_file
        super().__init__(**kwargs)

    def run_deferred(self) -> None:
        repos = filter_repos_by_topic(
            self.config.topic,
            refresh_projects(self.config, self.project_cache_file),
            lambda repo: repo["topics"],
        )

        atomic_write_file(self.project_cache_file, json.dumps(repos))

    def run_post(self) -> Any:
        return util.SUCCESS


def refresh_projects(config: GiteaConfig, repo_cache_file: Path) -> list[Any]:
    repos = []

    for repo in paginated_github_request(
        f"{config.instance_url}/api/v1/user/repos?limit=100",
        config.token(),
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
                    headers={"Authorization": f"token {config.token()}"},
                ).json()["topics"]
                repo["topics"] = topics
                repos.append(repo)
            except OSError:
                pass

    return repos
