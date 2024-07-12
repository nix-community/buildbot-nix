import json
import os
import signal
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from itertools import starmap
from pathlib import Path
from typing import Any

from buildbot.config.builder import BuilderConfig
from buildbot.plugins import util
from buildbot.process.buildstep import BuildStep
from buildbot.process.properties import Interpolate
from buildbot.reporters.base import ReporterBase
from buildbot.reporters.github import GitHubStatusPush
from buildbot.secrets.providers.base import SecretProviderBase
from buildbot.www.auth import AuthBase
from buildbot.www.avatar import AvatarBase, AvatarGitHub
from buildbot.www.oauth2 import GitHubAuth
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
from .github.auth._type import AuthType, AuthTypeApp, AuthTypeLegacy
from .github.installation_token import InstallationToken
from .github.jwt_token import JWTToken
from .github.legacy_token import (
    LegacyToken,
)
from .github.repo_token import (
    RepoToken,
)
from .projects import GitBackend, GitProject
from .secrets import read_secret_file

tlog = Logger()


def get_installations(jwt_token: JWTToken) -> list[int]:
    installations = paginated_github_request(
        "https://api.github.com/app/installations?per_page=100", jwt_token.get()
    )

    return [installation["id"] for installation in installations]


class CreateGitHubInstallationHooks(ThreadDeferredBuildStep):
    name = "create_github_installation_hooks"

    jwt_token: JWTToken
    project_cache_file: Path
    installation_token_map_name: Path
    webhook_secret: str
    webhook_url: str
    topic: str | None

    def __init__(
        self,
        jwt_token: JWTToken,
        project_cache_file: Path,
        installation_token_map_name: Path,
        webhook_secret: str,
        webhook_url: str,
        topic: str | None,
        **kwargs: Any,
    ) -> None:
        self.jwt_token = jwt_token
        self.project_cache_file = project_cache_file
        self.installation_token_map_name = installation_token_map_name
        self.webhook_secret = webhook_secret
        self.webhook_url = webhook_url
        self.topic = topic
        super().__init__(**kwargs)

    def run_deferred(self) -> None:
        repos = json.loads(self.project_cache_file.read_text())
        installation_token_map: dict[int, InstallationToken] = dict(
            starmap(
                lambda k, v: (
                    int(k),
                    InstallationToken.from_json(
                        self.jwt_token, int(k), self.installation_token_map_name, v
                    ),
                ),
                json.loads(self.installation_token_map_name.read_text()).items(),
            )
        )

        for repo in repos:
            create_project_hook(
                installation_token_map[repo["installation_id"]],
                self.webhook_secret,
                repo["owner"]["login"],
                repo["name"],
                self.webhook_url,
            )

    def run_post(self) -> Any:
        # reload the buildbot config
        os.kill(os.getpid(), signal.SIGHUP)
        return util.SUCCESS


class ReloadGithubInstallations(ThreadDeferredBuildStep):
    name = "reload_github_projects"

    jwt_token: JWTToken
    project_cache_file: Path
    installation_token_map_name: Path
    project_id_map_name: Path
    topic: str | None

    def __init__(
        self,
        jwt_token: JWTToken,
        project_cache_file: Path,
        installation_token_map_name: Path,
        project_id_map_name: Path,
        topic: str | None,
        **kwargs: Any,
    ) -> None:
        self.jwt_token = jwt_token
        self.installation_token_map_name = installation_token_map_name
        self.project_id_map_name = project_id_map_name
        self.project_cache_file = project_cache_file
        self.topic = topic
        super().__init__(**kwargs)

    def run_deferred(self) -> None:
        installation_token_map = GithubBackend.create_missing_installations(
            self.jwt_token,
            self.installation_token_map_name,
            GithubBackend.load_installations(
                self.jwt_token,
                self.installation_token_map_name,
            ),
            get_installations(self.jwt_token),
        )

        repos: list[Any] = []
        project_id_map: dict[str, int] = {}

        repos = []

        for k, v in installation_token_map.items():
            new_repos = filter_repos_by_topic(
                self.topic,
                refresh_projects(
                    v.get(),
                    self.project_cache_file,
                    clear=True,
                    api_endpoint="/installation/repositories",
                    subkey="repositories",
                    require_admin=False,
                ),
                lambda repo: repo["topics"],
            )

            for repo in new_repos:
                repo["installation_id"] = k

                project_id_map[repo["full_name"]] = k

            repos.extend(new_repos)

        atomic_write_file(self.project_cache_file, json.dumps(repos))
        atomic_write_file(self.project_id_map_name, json.dumps(project_id_map))

        tlog.info(
            f"Fetched {len(repos)} repositories from {len(installation_token_map.items())} installation tokens."
        )

    def run_post(self) -> Any:
        return util.SUCCESS


class CreateGitHubProjectHooks(ThreadDeferredBuildStep):
    name = "create_github_project_hooks"

    token: RepoToken
    project_cache_file: Path
    webhook_secret: str
    webhook_url: str
    topic: str | None

    def __init__(
        self,
        token: RepoToken,
        project_cache_file: Path,
        webhook_secret: str,
        webhook_url: str,
        topic: str | None,
        **kwargs: Any,
    ) -> None:
        self.token = token
        self.project_cache_file = project_cache_file
        self.webhook_secret = webhook_secret
        self.webhook_url = webhook_url
        self.topic = topic
        super().__init__(**kwargs)

    def run_deferred(self) -> None:
        repos = json.loads(self.project_cache_file.read_text())

        for repo in repos:
            create_project_hook(
                self.token,
                self.webhook_secret,
                repo["owner"]["login"],
                repo["name"],
                self.webhook_url,
            )

    def run_post(self) -> Any:
        # reload the buildbot config
        os.kill(os.getpid(), signal.SIGHUP)
        return util.SUCCESS


class ReloadGithubProjects(ThreadDeferredBuildStep):
    name = "reload_github_projects"

    token: RepoToken
    project_cache_file: Path
    topic: str | None

    def __init__(
        self,
        token: RepoToken,
        project_cache_file: Path,
        topic: str | None,
        **kwargs: Any,
    ) -> None:
        self.token = token
        self.project_cache_file = project_cache_file
        self.topic = topic
        super().__init__(**kwargs)

    def run_deferred(self) -> None:
        repos: list[Any] = filter_repos_by_topic(
            self.topic,
            refresh_projects(self.token.get(), self.project_cache_file),
            lambda repo: repo["topics"],
        )

        atomic_write_file(self.project_cache_file, json.dumps(repos))

    def run_post(self) -> Any:
        return util.SUCCESS


class GitHubAppStatusPush(GitHubStatusPush):
    token_source: Callable[[int], RepoToken]
    project_id_source: Callable[[str], int]
    saved_args: dict[str, Any]
    saved_kwargs: dict[str, Any]

    def checkConfig(
        self,
        token_source: Callable[[int], RepoToken],
        project_id_source: Callable[[str], int],
        context: Any = None,
        baseURL: Any = None,
        verbose: Any = False,
        debug: Any = None,
        verify: Any = None,
        generators: Any = None,
        **kwargs: dict[str, Any],
    ) -> Any:
        if generators is None:
            generators = self._create_default_generators()

        if "token" in kwargs:
            del kwargs["token"]
        super().checkConfig(
            token="",
            context=context,
            baseURL=baseURL,
            verbose=verbose,
            debug=debug,
            verify=verify,
            generators=generators,
            **kwargs,
        )

    def reconfigService(
        self,
        token_source: Callable[[int], RepoToken],
        project_id_source: Callable[[str], int],
        context: Any = None,
        baseURL: Any = None,
        verbose: Any = False,
        debug: Any = None,
        verify: Any = None,
        generators: Any = None,
        **kwargs: dict[str, Any],
    ) -> Any:
        if "saved_args" not in self or self.saved_args is None:
            self.saved_args = {}
        self.token_source = token_source
        self.project_id_source = project_id_source
        self.saved_kwargs = kwargs
        self.saved_args["context"] = context
        self.saved_args["baseURL"] = baseURL
        self.saved_args["verbose"] = verbose
        self.saved_args["debug"] = debug
        self.saved_args["verify"] = verify
        self.saved_args["generators"] = generators

        if generators is None:
            generators = self._create_default_generators()

        if "token" in kwargs:
            del kwargs["token"]
        super().reconfigService(
            token="",
            context=context,
            baseURL=baseURL,
            verbose=verbose,
            debug=debug,
            verify=verify,
            generators=generators,
            **kwargs,
        )

    def sendMessage(self, reports: Any) -> Any:
        build = reports[0]["builds"][0]
        sourcestamps = build["buildset"].get("sourcestamps")
        if not sourcestamps:
            return None

        for sourcestamp in sourcestamps:
            build["buildset"]["sourcestamps"] = [sourcestamp]

            token: str

            if "project" in sourcestamp and sourcestamp["project"] != "":
                token = self.token_source(
                    self.project_id_source(sourcestamp["project"])
                ).get()
            else:
                token = ""

            super().reconfigService(
                token,
                context=self.saved_args["context"],
                baseURL=self.saved_args["baseURL"],
                verbose=self.saved_args["verbose"],
                debug=self.saved_args["debug"],
                verify=self.saved_args["verify"],
                generators=self.saved_args["generators"],
                **self.saved_kwargs,
            )

            return super().sendMessage(reports)

        return None


class GithubAuthBackend(ABC):
    @abstractmethod
    def get_general_token(self) -> RepoToken:
        pass

    @abstractmethod
    def get_repo_token(self, repo: dict[str, Any]) -> RepoToken:
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
        project_cache_file: Path,
        webhook_secret: str,
        webhook_url: str,
        topic: str | None,
    ) -> list[BuildStep]:
        pass


class GithubLegacyAuthBackend(GithubAuthBackend):
    auth_type: AuthTypeLegacy

    token: LegacyToken

    def __init__(self, auth_type: AuthTypeLegacy) -> None:
        self.auth_type = auth_type
        self.token = LegacyToken(read_secret_file(auth_type.token_secret_name))

    def get_general_token(self) -> RepoToken:
        return self.token

    def get_repo_token(self, repo: dict[str, Any]) -> RepoToken:
        return self.token

    def create_secret_providers(self) -> list[SecretProviderBase]:
        return [GitHubLegacySecretService(self.token)]

    def create_reporter(self) -> ReporterBase:
        return GitHubStatusPush(
            token=self.token.get(),
            # Since we dynamically create build steps,
            # we use `virtual_builder_name` in the webinterface
            # so that we distinguish what has beeing build
            context=Interpolate("buildbot/%(prop:status_name)s"),
        )

    def create_reload_builder_steps(
        self,
        project_cache_file: Path,
        webhook_secret: str,
        webhook_url: str,
        topic: str | None,
    ) -> list[BuildStep]:
        return [
            ReloadGithubProjects(
                token=self.token,
                project_cache_file=project_cache_file,
                topic=topic,
            ),
            CreateGitHubProjectHooks(
                token=self.token,
                project_cache_file=project_cache_file,
                webhook_secret=webhook_secret,
                webhook_url=webhook_url,
                topic=topic,
            ),
        ]


class GitHubLegacySecretService(SecretProviderBase):
    name = "GitHubLegacySecretService"
    token: LegacyToken

    def reconfigService(self, token: LegacyToken) -> None:
        self.token = token

    def get(self, entry: str) -> str | None:
        """
        get the value from the file identified by 'entry'
        """
        if entry.startswith("github-token"):
            return self.token.get()
        return None


class GithubAppAuthBackend(GithubAuthBackend):
    auth_type: AuthTypeApp

    jwt_token: JWTToken
    installation_tokens: dict[int, InstallationToken]
    project_id_map: dict[str, int]

    def __init__(self, auth_type: AuthTypeApp) -> None:
        self.auth_type = auth_type
        self.jwt_token = JWTToken(
            self.auth_type.app_id, self.auth_type.app_secret_key_name
        )
        self.installation_tokens = GithubBackend.load_installations(
            self.jwt_token,
            self.auth_type.app_installation_token_map_name,
        )
        if self.auth_type.app_project_id_map_name.exists():
            self.project_id_map = json.loads(
                self.auth_type.app_project_id_map_name.read_text()
            )
        else:
            tlog.info(
                "~project-id-map~ is not present, GitHub project reload will follow."
            )
            self.project_id_map = {}

    def get_general_token(self) -> RepoToken:
        return self.jwt_token

    def get_repo_token(self, repo: dict[str, Any]) -> RepoToken:
        assert "installation_id" in repo, f"Missing installation_id in {repo}"
        return self.installation_tokens[repo["installation_id"]]

    def create_secret_providers(self) -> list[SecretProviderBase]:
        return [GitHubAppSecretService(self.installation_tokens, self.jwt_token)]

    def create_reporter(self) -> ReporterBase:
        return GitHubAppStatusPush(
            token_source=lambda iid: self.installation_tokens[iid],
            project_id_source=lambda project: self.project_id_map[project],
            # Since we dynamically create build steps,
            # we use `virtual_builder_name` in the webinterface
            # so that we distinguish what has beeing build
            context=Interpolate("buildbot/%(prop:status_name)s"),
        )

    def create_reload_builder_steps(
        self,
        project_cache_file: Path,
        webhook_secret: str,
        webhook_url: str,
        topic: str | None,
    ) -> list[BuildStep]:
        return [
            ReloadGithubInstallations(
                self.jwt_token,
                project_cache_file,
                self.auth_type.app_installation_token_map_name,
                self.auth_type.app_project_id_map_name,
                topic,
            ),
            CreateGitHubInstallationHooks(
                self.jwt_token,
                project_cache_file,
                self.auth_type.app_installation_token_map_name,
                webhook_secret=webhook_secret,
                webhook_url=webhook_url,
                topic=topic,
            ),
        ]


class GitHubAppSecretService(SecretProviderBase):
    name = "GitHubAppSecretService"
    installation_tokens: dict[int, InstallationToken]
    jwt_token: JWTToken

    def reconfigService(
        self, installation_tokens: dict[int, InstallationToken], jwt_token: JWTToken
    ) -> None:
        self.installation_tokens = installation_tokens
        self.jwt_token = jwt_token

    def get(self, entry: str) -> str | None:
        """
        get the value from the file identified by 'entry'
        """
        if entry.startswith("github-token-"):
            return self.installation_tokens[
                int(entry.removeprefix("github-token-"))
            ].get()
        if entry == "github-jwt-token":
            return self.jwt_token.get()
        return None


@dataclass
class GithubConfig:
    oauth_id: str | None
    auth_type: AuthType
    oauth_secret_name: str = "github-oauth-secret"
    webhook_secret_name: str = "github-webhook-secret"
    project_cache_file: Path = Path("github-project-cache-v1.json")
    topic: str | None = "build-with-buildbot"


@dataclass
class GithubBackend(GitBackend):
    config: GithubConfig
    webhook_secret: str
    webhook_url: str

    auth_backend: GithubAuthBackend

    def __init__(self, config: GithubConfig, webhook_url: str) -> None:
        self.config = config
        self.webhook_secret = read_secret_file(self.config.webhook_secret_name)
        self.webhook_url = webhook_url

        if isinstance(self.config.auth_type, AuthTypeLegacy):
            self.auth_backend = GithubLegacyAuthBackend(self.config.auth_type)
        elif isinstance(self.config.auth_type, AuthTypeApp):
            self.auth_backend = GithubAppAuthBackend(self.config.auth_type)

    @staticmethod
    def load_installations(
        jwt_token: JWTToken, installations_token_map_name: Path
    ) -> dict[int, InstallationToken]:
        initial_installations_map: dict[str, Any]
        if installations_token_map_name.exists():
            initial_installations_map = json.loads(
                installations_token_map_name.read_text()
            )
        else:
            initial_installations_map = {}

        installations_map: dict[int, InstallationToken] = {}

        for iid, installation in initial_installations_map.items():
            installations_map[int(iid)] = InstallationToken.from_json(
                jwt_token, int(iid), installations_token_map_name, installation
            )

        return installations_map

    @staticmethod
    def create_missing_installations(
        jwt_token: JWTToken,
        installations_token_map_name: Path,
        installations_map: dict[int, InstallationToken],
        installations: list[int],
    ) -> dict[int, InstallationToken]:
        for installation in set(installations) - installations_map.keys():
            installations_map[installation] = InstallationToken.new(
                jwt_token,
                installation,
                installations_token_map_name,
            )

        return installations_map

    def create_reload_builder(self, worker_names: list[str]) -> BuilderConfig:
        """Updates the flake an opens a PR for it."""
        factory = util.BuildFactory()
        steps = self.auth_backend.create_reload_builder_steps(
            self.config.project_cache_file,
            self.webhook_secret,
            self.webhook_url,
            self.config.topic,
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
        return {
            "secret": self.webhook_secret,
            "strict": True,
            "token": self.auth_backend.get_general_token().get(),
            "github_property_whitelist": ["github.base.sha", "github.head.sha"],
        }

    def create_avatar_method(self) -> AvatarBase | None:
        return AvatarGitHub()

    def create_auth(self) -> AuthBase:
        assert self.config.oauth_id is not None, "GitHub OAuth ID is required"
        return GitHubAuth(
            self.config.oauth_id,
            read_secret_file(self.config.oauth_secret_name),
            apiVersion=4,
        )

    def create_secret_providers(self) -> list[SecretProviderBase]:
        return self.auth_backend.create_secret_providers()

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

        if isinstance(self.auth_backend, GithubAppAuthBackend):
            dropped_repos = list(
                filter(lambda repo: "installation_id" not in repo, repos)
            )
            if dropped_repos:
                tlog.info(
                    "Dropped projects follow, refresh will follow after initialisation:"
                )
                for dropped_repo in dropped_repos:
                    tlog.info(f"\tDropping repo {dropped_repo['full_name']}")
            repos = list(filter(lambda repo: "installation_id" in repo, repos))

        repo_names: list[str] = [
            repo["owner"]["login"] + "/" + repo["name"] for repo in repos
        ]

        tlog.info(
            f"Loading {len(repos)} cached repositories: [{', '.join(repo_names)}]"
        )
        return [
            GithubProject(
                self.auth_backend.get_repo_token(repo),
                self.config,
                self.webhook_secret,
                repo,
            )
            for repo in repos
        ]

    def are_projects_cached(self) -> bool:
        if not self.config.project_cache_file.exists():
            return False

        if (
            isinstance(self.config.auth_type, AuthTypeApp)
            and not self.config.auth_type.app_project_id_map_name.exists()
        ):
            return False

        all_have_installation_id = True
        for project in json.loads(self.config.project_cache_file.read_text()):
            if "installation_id" not in project:
                all_have_installation_id = False
                break

        return all_have_installation_id

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
    config = dict(
        url=webhook_url + "change_hook/github",
        content_type="json",
        insecure_ssl="0",
        secret=webhook_secret,
    )
    data = dict(name="web", active=True, events=["push", "pull_request"], config=config)
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
    config: GithubConfig
    webhook_secret: str
    data: dict[str, Any]
    token: RepoToken

    def __init__(
        self,
        token: RepoToken,
        config: GithubConfig,
        webhook_secret: str,
        data: dict[str, Any],
    ) -> None:
        self.token = token
        self.config = config
        self.webhook_secret = webhook_secret
        self.data = data

    def get_project_url(self) -> str:
        return f"https://git:{self.token.get_as_secret()}s@github.com/{self.name}"

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


def refresh_projects(
    github_token: str,
    repo_cache_file: Path,
    repos: list[Any] | None = None,
    clear: bool = True,
    api_endpoint: str = "/user/repos",
    subkey: None | str = None,
    require_admin: bool = True,
) -> list[Any]:
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
            repos.append(repo)

    return repos
