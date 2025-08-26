from typing import Any

from buildbot.config.builder import BuilderConfig
from buildbot.reporters.base import ReporterBase
from buildbot.secrets.providers.base import SecretProviderBase
from buildbot.www.auth import AuthBase
from buildbot.www.avatar import AvatarBase

from buildbot_nix.models import PullBasedConfig
from buildbot_nix.projects import GitBackend, GitProject
from buildbot_nix.pull_based.null_reporter import NullReporter
from buildbot_nix.pull_based.project import PullBasedProject


class PullBasedBacked(GitBackend):
    config: PullBasedConfig

    def __init__(self, pull_based_config: PullBasedConfig, **kwargs: Any) -> None:
        self.config = pull_based_config
        super().__init__(**kwargs)

    def create_reload_builder(self, worker_names: list[str]) -> BuilderConfig | None:
        pass

    def create_reporter(self) -> ReporterBase:
        return NullReporter()

    def create_change_hook(self) -> dict[str, Any] | None:
        pass

    def create_avatar_method(self) -> AvatarBase | None:
        pass

    def create_auth(self) -> AuthBase | None:
        pass

    def create_secret_providers(self) -> list[SecretProviderBase]:
        return []

    def load_projects(self) -> list["GitProject"]:
        ret: list[GitProject] = []
        for name, repo in self.config.repositories.items():
            ret.append(
                PullBasedProject(
                    url=repo.url,
                    default_branch=repo.default_branch,
                    name=name,
                    poll_interval=repo.poll_interval,
                    poll_spread=self.config.poll_spread or 0,
                    ssh_known_hosts=repo.ssh_known_hosts,
                    ssh_private_key=repo.ssh_private_key,
                    ssh_private_key_file=repo.ssh_private_key_file,
                )
            )
        return ret

    def are_projects_cached(self) -> bool:
        return True

    @property
    def pretty_type(self) -> str:
        return "Pull Based"

    @property
    def type(self) -> str:
        return "pull-based"

    @property
    def reload_builder_name(self) -> str:
        return "reload-pull-based-projects"

    @property
    def change_hook_name(self) -> str:
        return "pull-based"
