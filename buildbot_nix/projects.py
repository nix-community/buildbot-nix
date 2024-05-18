from abc import ABC, abstractmethod
from typing import Any

from buildbot.config.builder import BuilderConfig
from buildbot.reporters.base import ReporterBase
from buildbot.secrets.providers.base import SecretProviderBase
from buildbot.www.auth import AuthBase
from buildbot.www.avatar import AvatarBase


class GitBackend(ABC):
    @abstractmethod
    def create_reload_builder(self, worker_names: list[str]) -> BuilderConfig:
        pass

    @abstractmethod
    def create_reporter(self) -> ReporterBase:
        pass

    @abstractmethod
    def create_change_hook(self) -> dict[str, Any]:
        pass

    @abstractmethod
    def create_avatar_method(self) -> AvatarBase | None:
        pass

    @abstractmethod
    def create_auth(self) -> AuthBase:
        pass

    def create_secret_providers(self) -> list[SecretProviderBase]:
        return []

    @abstractmethod
    def load_projects(self) -> list["GitProject"]:
        pass

    @abstractmethod
    def are_projects_cached(self) -> bool:
        pass

    @property
    @abstractmethod
    def pretty_type(self) -> str:
        pass

    @property
    @abstractmethod
    def type(self) -> str:
        pass

    @property
    @abstractmethod
    def reload_builder_name(self) -> str:
        pass

    @property
    @abstractmethod
    def change_hook_name(self) -> str:
        pass


class GitProject(ABC):
    @abstractmethod
    def create_project_hook(
        self,
        owner: str,
        repo: str,
        webhook_url: str,
    ) -> None:
        pass

    @abstractmethod
    def get_project_url(self) -> str:
        pass

    @property
    @abstractmethod
    def pretty_type(self) -> str:
        pass

    @property
    @abstractmethod
    def type(self) -> str:
        pass

    @property
    @abstractmethod
    def repo(self) -> str:
        pass

    @property
    @abstractmethod
    def owner(self) -> str:
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def url(self) -> str:
        pass

    @property
    @abstractmethod
    def project_id(self) -> str:
        pass

    @property
    @abstractmethod
    def default_branch(self) -> str:
        pass

    @property
    @abstractmethod
    def topics(self) -> list[str]:
        pass

    @property
    @abstractmethod
    def belongs_to_org(self) -> bool:
        pass
