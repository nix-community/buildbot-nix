from __future__ import annotations

import json
import os
import signal
from abc import ABC, abstractmethod
from dataclasses import dataclass
from itertools import starmap
from typing import TYPE_CHECKING, Any

from buildbot.plugins import util
from buildbot.process.properties import Interpolate, Properties, WithProperties
from buildbot.reporters.github import GitHubStatusPush
from buildbot.secrets.providers.base import SecretProviderBase
from buildbot.util.twisted import async_to_deferred
from buildbot.www.avatar import AvatarBase, AvatarGitHub
from buildbot.www.oauth2 import GitHubAuth
from pydantic import BaseModel, ConfigDict, Field
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
from .errors import BuildbotNixError
from .github.installation_token import InstallationToken
from .github.jwt_token import JWTToken
from .nix_status_generator import BuildNixEvalStatusGenerator
from .projects import GitBackend, GitProject

if TYPE_CHECKING:
    from pathlib import Path

    from buildbot.changes.base import ChangeSource
    from buildbot.config.builder import BuilderConfig
    from buildbot.process.buildstep import BuildStep
    from buildbot.reporters.base import ReporterBase
    from buildbot.www.auth import AuthBase

    from .github.repo_token import (
        RepoToken,
    )
    from .models import (
        GitHubConfig,
        RepoFilters,
    )

tlog = Logger()


class FilteredGitHubStatusPush(GitHubStatusPush):
    """GitHubStatusPush that only reports on GitHub projects."""

    def __init__(self, backend: GithubAppAuthBackend, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.backend = backend

    @async_to_deferred
    async def sendMessage(self, reports: list[dict[str, Any]]) -> None:
        """Filter out non-GitHub projects before sending."""
        if not reports:
            return

        # Check if this is a GitHub project
        build = reports[0]["builds"][0]
        props = Properties.fromDict(build["properties"])
        projectname = props.getProperty("projectname")

        # Handle missing or empty projectname
        if not projectname:
            log.msg("Skipping GitHub status update: projectname is missing or empty")
            return

        # Skip if projectname is not in our GitHub project map
        if projectname not in self.backend.project_id_map:
            log.msg(
                f"Skipping GitHub status update for non-GitHub project: {projectname}"
            )
            return

        await super().sendMessage(reports)


@dataclass
class GitHubProjectConfig:
    """Core configuration for GitHub projects."""

    project_cache_file: Path
    webhook_secret: str
    webhook_url: str
    filters: RepoFilters


@dataclass
class GitHubAppInstallationConfig:
    """Configuration specific to GitHub App installations."""

    jwt_token: JWTToken
    installation_token_map_name: Path
    project_id_map_name: Path


class RepoOwnerData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    login: str
    ttype: str = Field(alias="type")


class RepoData(BaseModel):
    name: str
    owner: RepoOwnerData
    full_name: str
    html_url: str
    default_branch: str
    topics: list[str]
    installation_id: int | None


def get_installations(jwt_token: JWTToken) -> list[int]:
    installations = paginated_github_request(
        "https://api.github.com/app/installations?per_page=100", jwt_token.get()
    )

    return [installation["id"] for installation in installations]


class CreateGitHubInstallationHooks(ThreadDeferredBuildStep):
    name = "create_github_installation_hooks"

    def __init__(
        self,
        project_config: GitHubProjectConfig,
        app_config: GitHubAppInstallationConfig,
        **kwargs: Any,
    ) -> None:
        self.project_config = project_config
        self.app_config = app_config
        super().__init__(**kwargs)

    def run_deferred(self) -> None:
        repos = model_validate_project_cache(
            RepoData, self.project_config.project_cache_file
        )
        installation_token_map: dict[int, InstallationToken] = dict(
            starmap(
                lambda k, v: (
                    int(k),
                    InstallationToken.from_json(
                        self.app_config.jwt_token,
                        int(k),
                        self.app_config.installation_token_map_name,
                        v,
                    ),
                ),
                json.loads(
                    self.app_config.installation_token_map_name.read_text()
                ).items(),
            )
        )

        for repo in repos:
            if repo.installation_id is None:
                continue

            create_project_hook(
                installation_token_map[repo.installation_id],
                self.project_config.webhook_secret,
                repo.owner.login,
                repo.name,
                self.project_config.webhook_url,
            )

    def run_post(self) -> Any:
        # reload the buildbot config
        os.kill(os.getpid(), signal.SIGHUP)
        return util.SUCCESS


class ReloadGithubInstallations(ThreadDeferredBuildStep):
    name = "reload_github_projects"

    def __init__(
        self,
        project_config: GitHubProjectConfig,
        app_config: GitHubAppInstallationConfig,
        backend: GithubAppAuthBackend | None = None,
        **kwargs: Any,
    ) -> None:
        self.project_config = project_config
        self.app_config = app_config
        self.backend = backend
        super().__init__(**kwargs)

    def run_deferred(self) -> None:
        installation_token_map = GithubBackend.create_missing_installations(
            self.app_config,
            GithubBackend.load_installations(self.app_config),
            get_installations(self.app_config.jwt_token),
        )

        repos: list[RepoData] = []
        project_id_map: dict[str, int] = {}

        repos = []

        for k, v in installation_token_map.items():
            new_repos = filter_repos(
                self.project_config.filters,
                refresh_projects(
                    v.get(),
                    api_endpoint="/installation/repositories",
                    subkey="repositories",
                    require_admin=False,
                ),
                RepoAccessors(
                    repo_name=lambda repo: repo.full_name,
                    user=lambda repo: repo.owner.login,
                    topics=lambda repo: repo.topics,
                ),
            )

            for repo in new_repos:
                repo.installation_id = k

                project_id_map[repo.full_name] = k

            repos.extend(new_repos)

        atomic_write_file(
            self.project_config.project_cache_file, model_dump_project_cache(repos)
        )
        atomic_write_file(
            self.app_config.project_id_map_name, json.dumps(project_id_map)
        )

        # Update the backend's in-memory data directly
        if self.backend:
            self.backend.update_reload_data(installation_token_map, project_id_map)
            tlog.info(
                f"Updated backend with {len(installation_token_map)} installations and {len(project_id_map)} projects"
            )

        tlog.info(
            f"Fetched {len(repos)} repositories from {len(installation_token_map.items())} installation tokens."
        )

    def run_post(self) -> Any:
        return util.SUCCESS


class CreateGitHubProjectHooks(ThreadDeferredBuildStep):
    name = "create_github_project_hooks"

    def __init__(
        self,
        token: RepoToken,
        config: GitHubProjectConfig,
        **kwargs: Any,
    ) -> None:
        self.token = token
        self.config = config
        super().__init__(**kwargs)

    def run_deferred(self) -> None:
        repos = model_validate_project_cache(RepoData, self.config.project_cache_file)

        for repo in repos:
            create_project_hook(
                self.token,
                self.config.webhook_secret,
                repo.owner.login,
                repo.name,
                self.config.webhook_url,
            )

    def run_post(self) -> Any:
        # reload the buildbot config
        os.kill(os.getpid(), signal.SIGHUP)
        return util.SUCCESS


class ReloadGithubProjects(ThreadDeferredBuildStep):
    name = "reload_github_projects"

    token: RepoToken
    project_cache_file: Path
    filters: RepoFilters

    def __init__(
        self,
        token: RepoToken,
        project_cache_file: Path,
        filters: RepoFilters,
        **kwargs: Any,
    ) -> None:
        self.token = token
        self.project_cache_file = project_cache_file
        self.filters = filters
        super().__init__(**kwargs)

    def run_deferred(self) -> None:
        repos: list[RepoData] = filter_repos(
            self.filters,
            refresh_projects(self.token.get()),
            RepoAccessors(
                repo_name=lambda repo: repo.full_name,
                user=lambda repo: repo.owner.login,
                topics=lambda repo: repo.topics,
            ),
        )

        atomic_write_file(self.project_cache_file, model_dump_project_cache(repos))

    def run_post(self) -> Any:
        return util.SUCCESS


class GithubAuthBackend(ABC):
    @abstractmethod
    def get_general_token(self) -> RepoToken:
        pass

    @abstractmethod
    def get_repo_token(self, repo_full_name: str) -> RepoToken:
        pass

    @abstractmethod
    def create_secret_providers(self) -> list[SecretProviderBase]:
        pass

    @abstractmethod
    def create_reporter(self) -> ReporterBase:
        pass

    @abstractmethod
    def create_reload_builder_steps(
        self,
        config: GitHubProjectConfig,
    ) -> list[BuildStep]:
        pass


class GithubAppAuthBackend(GithubAuthBackend):
    config: GitHubConfig

    jwt_token: JWTToken
    installation_tokens: dict[int, InstallationToken]
    project_id_map: dict[str, int]

    def __init__(self, config: GitHubConfig) -> None:
        self.config = config
        self.jwt_token = JWTToken(self.config.id, self.config.secret_key_file)
        self.installation_tokens = GithubBackend.load_installations(
            GitHubAppInstallationConfig(
                jwt_token=self.jwt_token,
                installation_token_map_name=self.config.installation_token_map_file,
                project_id_map_name=self.config.project_id_map_file,
            ),
        )
        if self.config.project_id_map_file.exists():
            try:
                self.project_id_map = json.loads(
                    self.config.project_id_map_file.read_text()
                )
            except json.JSONDecodeError as e:
                tlog.error(
                    f"Invalid JSON in {self.config.project_id_map_file}: {e}; starting with empty map."
                )
                self.project_id_map = {}
        else:
            tlog.info(
                "~project-id-map~ is not present, GitHub project reload will follow."
            )
            self.project_id_map = {}

    def update_reload_data(
        self,
        installation_tokens: dict[int, InstallationToken],
        project_map: dict[str, int],
    ) -> None:
        self.installation_tokens = installation_tokens
        self.project_id_map = project_map

    def get_general_token(self) -> RepoToken:
        return self.jwt_token

    def get_repo_token(self, repo_full_name: str) -> RepoToken:
        maybe_project = self.project_id_map.get(repo_full_name)
        if maybe_project is None:
            msg = f"BUG: Project {repo_full_name} not found in project_id_map at {self.config.project_id_map_file}"
            raise BuildbotNixError(msg)
        installation_id = maybe_project
        return self.installation_tokens[installation_id]

    def create_secret_providers(self) -> list[SecretProviderBase]:
        return [
            GitHubAppSecretService(
                self.project_id_map, self.installation_tokens, self.jwt_token
            )
        ]

    def create_reporter(self) -> ReporterBase:
        def get_github_token(props: Properties) -> str:
            return self.installation_tokens[
                self.project_id_map[props["projectname"]]
            ].get()

        return FilteredGitHubStatusPush(
            backend=self,
            token=WithProperties("%(github_token)s", github_token=get_github_token),
            # Since we dynamically create build steps,
            # we use `virtual_builder_name` in the webinterface
            # so that we distinguish what has being build
            context=Interpolate("buildbot/%(prop:status_name)s"),
            generators=[
                BuildNixEvalStatusGenerator(),
            ],
        )

    def create_reload_builder_steps(
        self,
        config: GitHubProjectConfig,
    ) -> list[BuildStep]:
        app_config = GitHubAppInstallationConfig(
            jwt_token=self.jwt_token,
            installation_token_map_name=self.config.installation_token_map_file,
            project_id_map_name=self.config.project_id_map_file,
        )
        return [
            ReloadGithubInstallations(
                project_config=config,
                app_config=app_config,
                backend=self,
            ),
            CreateGitHubInstallationHooks(
                project_config=config,
                app_config=app_config,
            ),
        ]


class GitHubAppSecretService(SecretProviderBase):
    name: str | None = "GitHubAppSecretService"  # type: ignore[assignment]
    project_id_map: dict[str, int]
    installation_tokens: dict[int, InstallationToken]
    jwt_token: JWTToken

    def reconfigService(  # type: ignore[override]
        self,
        project_id_map: dict[str, int],
        installation_tokens: dict[int, InstallationToken],
        jwt_token: JWTToken,
    ) -> None:
        self.project_id_map = project_id_map
        self.installation_tokens = installation_tokens
        self.jwt_token = jwt_token

    def get(self, entry: str) -> str | None:
        """
        get the value from the file identified by 'entry'
        """
        if entry.startswith("github-token-"):
            try:
                return self.installation_tokens[
                    int(entry.removeprefix("github-token-"))
                ].get()
            except ValueError:
                return self.installation_tokens[
                    self.project_id_map[entry.removeprefix("github-token-")]
                ].get()
        if entry == "github-jwt-token":
            return self.jwt_token.get()
        return None


@dataclass
class GithubBackend(GitBackend):
    config: GitHubConfig
    webhook_secret: str
    webhook_url: str

    auth_backend: GithubAuthBackend

    def __init__(self, config: GitHubConfig, webhook_url: str) -> None:
        self.config = config
        self.webhook_secret = self.config.webhook_secret
        self.webhook_url = webhook_url

        self.auth_backend = GithubAppAuthBackend(self.config)

    @staticmethod
    def load_installations(
        config: GitHubAppInstallationConfig,
    ) -> dict[int, InstallationToken]:
        initial_installations_map: dict[str, Any]
        if config.installation_token_map_name.exists():
            initial_installations_map = json.loads(
                config.installation_token_map_name.read_text()
            )
        else:
            initial_installations_map = {}

        installations_map: dict[int, InstallationToken] = {}

        for iid, installation in initial_installations_map.items():
            installations_map[int(iid)] = InstallationToken.from_json(
                config.jwt_token,
                int(iid),
                config.installation_token_map_name,
                installation,
            )

        return installations_map

    @staticmethod
    def create_missing_installations(
        config: GitHubAppInstallationConfig,
        installations_map: dict[int, InstallationToken],
        installations: list[int],
    ) -> dict[int, InstallationToken]:
        for installation in set(installations) - installations_map.keys():
            installations_map[installation] = InstallationToken.new(
                config.jwt_token,
                installation,
                config.installation_token_map_name,
            )

        return installations_map

    def create_reload_builder(self, worker_names: list[str]) -> BuilderConfig:
        """Updates the flake an opens a PR for it."""
        factory = util.BuildFactory()
        steps = self.auth_backend.create_reload_builder_steps(
            GitHubProjectConfig(
                project_cache_file=self.config.project_cache_file,
                webhook_secret=self.webhook_secret,
                webhook_url=self.webhook_url,
                filters=self.config.filters,
            )
        )
        for step in steps:
            factory.addStep(step)

        return util.BuilderConfig(
            name=self.reload_builder_name,
            workernames=worker_names,
            factory=factory,
        )

    def create_reporter(self) -> ReporterBase:
        return self.auth_backend.create_reporter()

    def create_change_hook(self) -> dict[str, Any]:
        def get_github_token(props: Properties) -> str:
            full_name = props.getProperty("full_name")
            if full_name is None:
                msg = f"full_name not found in properties: {props}"
                raise BuildbotNixError(msg)
            return self.auth_backend.get_repo_token(full_name).get()

        return {
            "secret": self.webhook_secret,
            "strict": True,
            "token": WithProperties("%(github_token)s", github_token=get_github_token),
            "github_property_whitelist": ["github.base.sha", "github.head.sha"],
        }

    def create_avatar_method(self) -> AvatarBase | None:
        return AvatarGitHub()

    def create_auth(self) -> AuthBase:
        if self.config.oauth_id is None:
            msg = "GitHub OAuth ID is required"
            raise ValueError(msg)
        return GitHubAuth(
            self.config.oauth_id,
            self.config.oauth_secret,
            apiVersion=4,
        )

    def create_secret_providers(self) -> list[SecretProviderBase]:
        return self.auth_backend.create_secret_providers()

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

        if isinstance(self.auth_backend, GithubAppAuthBackend):
            dropped_repos = list(
                filter(lambda repo: repo.installation_id is None, repos)
            )
            if dropped_repos:
                tlog.info(
                    "Dropped projects follow, refresh will follow after initialisation:"
                )
                for dropped_repo in dropped_repos:
                    tlog.info(f"\tDropping repo {dropped_repo.full_name}")
            repos = list(filter(lambda repo: repo.installation_id is not None, repos))

        repo_names: list[str] = [repo.owner.login + "/" + repo.name for repo in repos]

        tlog.info(
            f"Loading {len(repos)} cached repositories: [{', '.join(repo_names)}]"
        )
        return [
            GithubProject(
                self.auth_backend.get_repo_token(repo.full_name),
                self.config,
                self.webhook_secret,
                RepoData.model_validate(repo),
            )
            for repo in repos
        ]

    def are_projects_cached(self) -> bool:
        if not self.config.project_cache_file.exists():
            return False

        if not self.config.project_id_map_file.exists():
            return False

        projects = model_validate_project_cache(
            RepoData, self.config.project_cache_file
        )
        return all(project.installation_id is not None for project in projects)

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


def create_project_hook(
    token: RepoToken, webhook_secret: str, owner: str, repo: str, webhook_url: str
) -> None:
    hooks = paginated_github_request(
        f"https://api.github.com/repos/{owner}/{repo}/hooks?per_page=100",
        token.get(),
    )
    config = {
        "url": webhook_url + "change_hook/github",
        "content_type": "json",
        "insecure_ssl": "0",
        "secret": webhook_secret,
    }
    data = {
        "name": "web",
        "active": True,
        "events": ["push", "pull_request"],
        "config": config,
    }
    headers = {
        "Authorization": f"Bearer {token.get()}",
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


class GithubProject(GitProject):
    config: GitHubConfig
    webhook_secret: str
    data: RepoData
    token: RepoToken

    def __init__(
        self,
        token: RepoToken,
        config: GitHubConfig,
        webhook_secret: str,
        data: RepoData,
    ) -> None:
        self.token = token
        self.config = config
        self.webhook_secret = webhook_secret
        self.data = data

    def get_project_url(self) -> str:
        return f"https://git:{self.token.get_as_secret()}s@github.com/{self.name}"

    def create_change_source(self) -> ChangeSource | None:
        return None

    @property
    def pretty_type(self) -> str:
        return "GitHub"

    @property
    def type(self) -> str:
        return "github"

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
        return self.data.html_url

    @property
    def project_id(self) -> str:
        return slugify_project_name(self.data.full_name)

    @property
    def nix_ref_type(self) -> str:
        return "github"

    @property
    def default_branch(self) -> str:
        return self.data.default_branch

    @property
    def topics(self) -> list[str]:
        return self.data.topics

    @property
    def belongs_to_org(self) -> bool:
        return self.data.owner.ttype == "Organization"

    @property
    def private_key_path(self) -> Path | None:
        return None

    @property
    def known_hosts_path(self) -> Path | None:
        return None


def refresh_projects(
    github_token: str,
    repos: list[Any] | None = None,
    api_endpoint: str = "/user/repos",
    subkey: None | str = None,
    *,
    require_admin: bool = True,
) -> list[RepoData]:
    if repos is None:
        repos = []

    for repo in paginated_github_request(
        f"https://api.github.com{api_endpoint}?per_page=100",
        github_token,
        subkey=subkey,
    ):
        # TODO actually check for this properly
        if not repo["permissions"]["admin"] and require_admin:
            name = repo["full_name"]
            log.msg(
                f"skipping {name} because we do not have admin privileges, needed for hook management",
            )
        else:
            repo["installation_id"] = None
            repos.append(RepoData.model_validate(repo))

    return repos
