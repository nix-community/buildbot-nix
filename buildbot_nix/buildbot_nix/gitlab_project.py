import os
import signal
from collections.abc import Generator
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

from buildbot.changes.base import ChangeSource
from buildbot.config.builder import BuilderConfig
from buildbot.plugins import util
from buildbot.process.properties import Interpolate
from buildbot.reporters.base import ReporterBase
from buildbot.reporters.gitlab import GitLabStatusPush
from buildbot.util import httpclientservice
from buildbot.www import resource
from buildbot.www.auth import AuthBase
from buildbot.www.avatar import AvatarBase
from pydantic import BaseModel
from twisted.internet import defer
from twisted.logger import Logger
from twisted.python import log

from buildbot_nix.common import (
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
from buildbot_nix.models import GitlabConfig
from buildbot_nix.nix_status_generator import BuildNixEvalStatusGenerator
from buildbot_nix.projects import GitBackend, GitProject

tlog = Logger()


class NamespaceData(BaseModel):
    path: str
    kind: str


class RepoData(BaseModel):
    id: int
    name: str
    name_with_namespace: str
    path: str
    path_with_namespace: str
    ssh_url_to_repo: str
    web_url: str
    namespace: NamespaceData
    default_branch: str
    topics: list[str]


class GitlabProject(GitProject):
    config: GitlabConfig
    data: RepoData

    def __init__(self, config: GitlabConfig, data: RepoData) -> None:
        self.config = config
        self.data = data

    def get_project_url(self) -> str:
        url = urlparse(self.config.instance_url)
        return f"{url.scheme}://git:%(secret:{self.config.token_file})s@{url.hostname}/{self.data.path_with_namespace}"

    def create_change_source(self) -> ChangeSource | None:
        return None

    @property
    def pretty_type(self) -> str:
        return "Gitlab"

    @property
    def type(self) -> str:
        return "gitlab"

    @property
    def nix_ref_type(self) -> str:
        return "gitlab"

    @property
    def repo(self) -> str:
        return self.data.path

    @property
    def owner(self) -> str:
        return self.data.namespace.path

    @property
    def name(self) -> str:
        # This needs to match what buildbot uses in change hooks to map an incoming change
        # to a project: https://github.com/buildbot/buildbot/blob/master/master/buildbot/www/hooks/gitlab.py#L45
        # I suspect this will result in clashes if you have identically-named projects in
        # different namespaces, as is totally valid in gitlab.
        # Using self.data.name_with_namespace would be more robust.
        return self.data.name

    @property
    def url(self) -> str:
        # Not `web_url`: the buildbot gitlab hook dialect uses repository.url which seems
        # to be the ssh url in practice.
        # See: https://github.com/buildbot/buildbot/blob/master/master/buildbot/www/hooks/gitlab.py#L271
        return self.data.ssh_url_to_repo

    @property
    def project_id(self) -> str:
        return slugify_project_name(self.data.path_with_namespace)

    @property
    def default_branch(self) -> str:
        return self.data.default_branch

    @property
    def topics(self) -> list[str]:
        return self.data.topics

    @property
    def belongs_to_org(self) -> bool:
        return self.data.namespace.kind == "group"

    @property
    def private_key_path(self) -> Path | None:
        return None

    @property
    def known_hosts_path(self) -> Path | None:
        return None


class GitlabBackend(GitBackend):
    config: GitlabConfig
    instance_url: str

    def __init__(self, config: GitlabConfig, instance_url: str) -> None:
        self.config = config
        self.instance_url = instance_url

    def create_reload_builder(self, worker_names: list[str]) -> BuilderConfig:
        factory = util.BuildFactory()
        factory.addStep(
            ReloadGitlabProjects(self.config, self.config.project_cache_file),
        )
        factory.addStep(
            CreateGitlabProjectHooks(
                self.config,
                self.instance_url,
            )
        )
        return util.BuilderConfig(
            name=self.reload_builder_name,
            workernames=worker_names,
            factory=factory,
        )

    def create_reporter(self) -> ReporterBase:
        return GitLabStatusPush(
            token=self.config.token,
            context=Interpolate("buildbot/%(prop:status_name)s"),
            baseURL=self.config.instance_url,
            generators=[
                BuildNixEvalStatusGenerator(),
            ],
        )

    def create_change_hook(self) -> dict[str, Any]:
        return {"secret": self.config.webhook_secret}

    def load_projects(self) -> list["GitProject"]:
        if not self.config.project_cache_file.exists():
            return []

        repos: list[RepoData] = filter_repos(
            self.config.filters,
            sorted(
                model_validate_project_cache(RepoData, self.config.project_cache_file),
                key=lambda repo: repo.path_with_namespace,
            ),
            RepoAccessors(
                repo_name=lambda repo: repo.name,
                user=lambda repo: repo.namespace.path,
                topics=lambda repo: repo.topics,
            ),
        )
        tlog.info(f"Loading {len(repos)} cached repos.")

        return [
            GitlabProject(self.config, RepoData.model_validate(repo)) for repo in repos
        ]

    def are_projects_cached(self) -> bool:
        return self.config.project_cache_file.exists()

    def create_auth(self) -> AuthBase:
        raise NotImplementedError

    def create_avatar_method(self) -> AvatarBase | None:
        return AvatarGitlab(config=self.config)

    @property
    def reload_builder_name(self) -> str:
        return "reload-gitlab-projects"

    @property
    def type(self) -> str:
        return "gitlab"

    @property
    def pretty_type(self) -> str:
        return "Gitlab"

    @property
    def change_hook_name(self) -> str:
        return "gitlab"


class ReloadGitlabProjects(ThreadDeferredBuildStep):
    name = "reload_gitlab_projects"

    config: GitlabConfig
    project_cache_file: Path

    def __init__(
        self,
        config: GitlabConfig,
        project_cache_file: Path,
        **kwargs: Any,
    ) -> None:
        self.config = config
        self.project_cache_file = project_cache_file
        super().__init__(**kwargs)

    def run_deferred(self) -> None:
        repos: list[RepoData] = filter_repos(
            self.config.filters,
            refresh_projects(self.config),
            RepoAccessors(
                repo_name=lambda repo: repo.name,
                user=lambda repo: repo.namespace.path,
                topics=lambda repo: repo.topics,
            ),
        )
        atomic_write_file(self.project_cache_file, model_dump_project_cache(repos))

    def run_post(self) -> Any:
        return util.SUCCESS


class CreateGitlabProjectHooks(ThreadDeferredBuildStep):
    name = "create_gitlab_project_hooks"

    config: GitlabConfig
    instance_url: str

    def __init__(self, config: GitlabConfig, instance_url: str, **kwargs: Any) -> None:
        self.config = config
        self.instance_url = instance_url
        super().__init__(**kwargs)

    def run_deferred(self) -> None:
        repos = model_validate_project_cache(RepoData, self.config.project_cache_file)
        for repo in repos:
            create_project_hook(
                token=self.config.token,
                webhook_secret=self.config.webhook_secret,
                project_id=repo.id,
                gitlab_url=self.config.instance_url,
                instance_url=self.instance_url,
            )

    def run_post(self) -> Any:
        os.kill(os.getpid(), signal.SIGHUP)
        return util.SUCCESS


class AvatarGitlab(AvatarBase):
    name = "gitlab"

    config: GitlabConfig

    def __init__(
        self,
        config: GitlabConfig,
        debug: bool = False,  # noqa: FBT002
        verify: bool = True,  # noqa: FBT002
    ) -> None:
        self.config = config
        self.debug = debug
        self.verify = verify

        self.master = None
        self.client: httpclientservice.HTTPSession | None = None

    def _get_http_client(
        self,
    ) -> httpclientservice.HTTPSession:
        assert self.master is not None  # noqa: S101
        if self.client is not None:
            return self.client

        headers = {
            "User-Agent": "Buildbot",
            "Authorization": f"Bearer {self.config.token}",
        }

        self.client = httpclientservice.HTTPSession(
            self.master.httpservice,
            self.config.instance_url,
            headers=headers,
            debug=self.debug,
            verify=self.verify,
        )

        return self.client

    @defer.inlineCallbacks
    def getUserAvatar(  # noqa: N802
        self,
        email: bytes,
        username: bytes | None,
        size: str | int,
        defaultAvatarUrl: str,  # noqa: N803
    ) -> Generator[defer.Deferred, str | None, None]:
        if isinstance(size, int):
            size = str(size)
        username_str = username.decode("utf-8") if username else None
        email_str = email.decode("utf-8")
        avatar_url = None
        if username_str is not None:
            avatar_url = yield self._get_avatar_by_username(username_str)
        if avatar_url is None:
            avatar_url = yield self._get_avatar_by_email(email_str, size)
        if not avatar_url:
            avatar_url = defaultAvatarUrl
        raise resource.Redirect(avatar_url)

    @defer.inlineCallbacks
    def _get_avatar_by_username(
        self, username: str
    ) -> Generator[defer.Deferred, Any, str | None]:
        qs = urlencode({"username": username})
        http = self._get_http_client()
        users = yield http.get(f"/api/v4/users?{qs}")
        users = yield users.json()
        if len(users) == 1:
            return users[0]["avatar_url"]
        if len(users) > 1:
            # TODO: log warning
            ...
        return None

    @defer.inlineCallbacks
    def _get_avatar_by_email(
        self, email: str, size: str | None
    ) -> Generator[defer.Deferred, Any, str | None]:
        http = self._get_http_client()
        q = {"email": email}
        if size is not None:
            q["size"] = size
        qs = urlencode(q)
        res = yield http.get(f"/api/v4/avatar?{qs}")
        data = yield res.json()
        # N.B: Gitlab's public avatar endpoint returns a gravatar url if there isn't an
        # account with a matching public email - so it should always return *something*.
        # See: https://docs.gitlab.com/api/avatar/#get-details-on-an-account-avatar
        if "avatar_url" in data:
            return data["avatar_url"]
        else:
            return None


def refresh_projects(config: GitlabConfig) -> list[RepoData]:
    # access level 40 == Maintainer. See https://docs.gitlab.com/api/members/#roles
    return [
        RepoData.model_validate(repo)
        for repo in paginated_github_request(
            f"{config.instance_url}/api/v4/projects?min_access_level=40&pagination=keyset&per_page=100&order_by=id&sort=asc",
            config.token,
        )
    ]


def create_project_hook(
    token: str,
    webhook_secret: str,
    project_id: int,
    gitlab_url: str,
    instance_url: str,
) -> None:
    hook_url = instance_url + "change_hook/gitlab"
    for hook in paginated_github_request(
        f"{gitlab_url}/api/v4/projects/{project_id}/hooks",
        token,
    ):
        if hook["url"] == hook_url:
            log.msg(f"hook for gitlab project {project_id} already exists")
            return
    log.msg(f"creating hook for gitlab project {project_id}")
    http_request(
        f"{gitlab_url}/api/v4/projects/{project_id}/hooks",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        data={
            "name": "buildbot hook",
            "url": hook_url,
            "enable_ssl_verification": True,
            "token": webhook_secret,
            # We don't need to be informed of most events
            "confidential_issues_events": False,
            "confidential_note_events": False,
            "deployment_events": False,
            "feature_flag_events": False,
            "issues_events": False,
            "job_events": False,
            "merge_requests_events": False,
            "note_events": False,
            "pipeline_events": False,
            "releases_events": False,
            "wiki_page_events": False,
            "resource_access_token_events": False,
        },
    )
