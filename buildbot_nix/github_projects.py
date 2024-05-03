import json
import os
import signal
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Any

from buildbot.config.builder import BuilderConfig
from buildbot.plugins import util
from buildbot.process.buildstep import BuildStep
from buildbot.process.properties import Interpolate
from buildbot.reporters.base import ReporterBase
from buildbot.reporters.github import GitHubStatusPush
from buildbot.www.auth import AuthBase
from buildbot.www.avatar import AvatarBase, AvatarGitHub
from buildbot.www.oauth2 import GitHubAuth
from twisted.internet import defer, threads
from twisted.internet.defer import ensureDeferred
from twisted.python import log
from twisted.python.failure import Failure

if TYPE_CHECKING:
    from buildbot.process.log import StreamLog

from .common import (http_request, paginated_github_request,
                     slugify_project_name)
from .projects import GitBackend, GitProject
from .secrets import read_secret_file


class ReloadGithubProjects(BuildStep):
    name = "reload_github_projects"

    def __init__(self, token: str, project_cache_file: Path, **kwargs: Any) -> None:
        self.token = token
        self.project_cache_file = project_cache_file
        super().__init__(**kwargs)

    def reload_projects(self) -> None:
        refresh_projects(self.token, self.project_cache_file)

    @defer.inlineCallbacks
    def run(self) -> Generator[Any, object, Any]:
        d = threads.deferToThread(self.reload_projects)  # type: ignore[no-untyped-call]

        self.error_msg = ""

        def error_cb(failure: Failure) -> int:
            self.error_msg += failure.getTraceback()
            return util.FAILURE

        d.addCallbacks(lambda _: util.SUCCESS, error_cb)
        res = yield d
        if res == util.SUCCESS:
            # reload the buildbot config
            os.kill(os.getpid(), signal.SIGHUP)
            return util.SUCCESS
        else:
            log: StreamLog = yield self.addLog("log")
            log.addStderr(f"Failed to reload project list: {self.error_msg}")
            return util.FAILURE


@dataclass
class GithubConfig:
    oauth_id: str | None

    # TODO unused
    buildbot_user: str
    oauth_secret_name: str = "github-oauth-secret"
    token_secret_name: str = "github-token"
    webhook_secret_name: str = "github-webhook-secret"
    project_cache_file: Path = Path("github-project-cache.json")
    topic: str | None = "build-with-buildbot"

    def token(self) -> str:
        return read_secret_file(self.token_secret_name)


class AsyncGitHubStatusPush(GitHubStatusPush):
    @defer.inlineCallbacks
    def sendMessage(  # noqa: N802
        self, reports: list[dict[str, Any]]
    ) -> Generator[Any, object, Any]:
        # Don't block the CI on the GitHub status updates
        yield ensureDeferred(super().sendMessage(reports))


@dataclass
class GithubBackend(GitBackend):
    config: GithubConfig
    webhook_secret: str

    def __init__(self, config: GithubConfig) -> None:
        self.config = config
        self.webhook_secret = read_secret_file(self.config.webhook_secret_name)

    def create_reload_builder(self, worker_names: list[str]) -> BuilderConfig:
        """Updates the flake an opens a PR for it."""
        factory = util.BuildFactory()
        factory.addStep(
            ReloadGithubProjects(self.config.token(), self.config.project_cache_file),
        )
        return util.BuilderConfig(
            name=self.reload_builder_name,
            workernames=worker_names,
            factory=factory,
        )

    def create_reporter(self) -> ReporterBase:
        return AsyncGitHubStatusPush(
            token=self.config.token(),
            # Since we dynamically create build steps,
            # we use `virtual_builder_name` in the webinterface
            # so that we distinguish what has beeing build
            context=Interpolate("buildbot/%(prop:status_name)s"),
        )

    def create_change_hook(self) -> dict[str, Any]:
        return {
            "secret": self.webhook_secret,
            "strict": True,
            "token": self.config.token(),
            "github_property_whitelist": ["github.base.sha", "github.head.sha"],
        }

    def create_avatar_method(self) -> AvatarBase | None:
        return AvatarGitHub(token=self.config.token())

    def create_auth(self) -> AuthBase:
        assert self.config.oauth_id is not None, "GitHub OAuth ID is required"
        return GitHubAuth(
            self.config.oauth_id,
            read_secret_file(self.config.oauth_secret_name),
            apiVersion=4,
        )

    def load_projects(self) -> list["GitProject"]:
        if not self.config.project_cache_file.exists():
            return []

        repos: list[dict[str, Any]] = sorted(
            json.loads(self.config.project_cache_file.read_text()),
            key=lambda x: x["full_name"],
        )
        return list(
            filter(
                lambda project: self.config.topic is not None
                and self.config.topic in project.topics,
                [
                    GithubProject(self.config, self.webhook_secret, repo)
                    for repo in repos
                ],
            )
        )

    def are_projects_cached(self) -> bool:
        return self.config.project_cache_file.exists()

    @property
    def type(self) -> str:
        return "github"

    @property
    def pretty_type(self) -> str:
        return "GitHub"

    @property
    def reload_builder_name(self) -> str:
        return "reload-github-projects"

    @property
    def change_hook_name(self) -> str:
        return "github"


class GithubProject(GitProject):
    config: GithubConfig

    def __init__(
        self, config: GithubConfig, webhook_secret: str, data: dict[str, Any]
    ) -> None:
        self.config = config
        self.webhook_secret = webhook_secret
        self.data = data

    def create_project_hook(
        self,
        owner: str,
        repo: str,
        webhook_url: str,
    ) -> None:
        hooks = paginated_github_request(
            f"https://api.github.com/repos/{owner}/{repo}/hooks?per_page=100",
            self.config.token(),
        )
        config = dict(
            url=webhook_url + "change_hook/github",
            content_type="json",
            insecure_ssl="0",
            secret=self.webhook_secret,
        )
        data = dict(
            name="web", active=True, events=["push", "pull_request"], config=config
        )
        headers = {
            "Authorization": f"Bearer {self.config.token()}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        for hook in hooks:
            if hook["config"]["url"] == webhook_url + "change_hook/github":
                log.msg(f"hook for {owner}/{repo} already exists")
                return

        http_request(
            f"https://api.github.com/repos/{owner}/{repo}/hooks",
            method="POST",
            headers=headers,
            data=data,
        )

    def get_project_url(self) -> str:
        return f"https://git:%(secret:{self.config.token_secret_name})s@github.com/{self.name}"

    @property
    def pretty_type(self) -> str:
        return "GitHub"

    @property
    def type(self) -> str:
        return "github"

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
        return self.data["html_url"]

    @property
    def project_id(self) -> str:
        return slugify_project_name(self.data["full_name"])

    @property
    def default_branch(self) -> str:
        return self.data["default_branch"]

    @property
    def topics(self) -> list[str]:
        return self.data["topics"]

    @property
    def belongs_to_org(self) -> bool:
        return self.data["owner"]["type"] == "Organization"


def refresh_projects(github_token: str, repo_cache_file: Path) -> None:
    repos = []

    for repo in paginated_github_request(
        "https://api.github.com/user/repos?per_page=100",
        github_token,
    ):
        if not repo["permissions"]["admin"]:
            name = repo["full_name"]
            log.msg(
                f"skipping {name} because we do not have admin privileges, needed for hook management",
            )
        else:
            repos.append(repo)

    with NamedTemporaryFile("w", delete=False, dir=repo_cache_file.parent) as f:
        path = Path(f.name)
        try:
            f.write(json.dumps(repos))
            f.flush()
            path.rename(repo_cache_file)
        except OSError:
            path.unlink()
            raise
