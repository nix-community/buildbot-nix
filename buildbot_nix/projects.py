from abc import ABC, abstractmethod
from typing import Any

from buildbot.config.builder import BuilderConfig
from buildbot.reporters.base import ReporterBase
from buildbot.www.avatar import AvatarBase
from buildbot.www.auth import AuthBase

class GitBackend(ABC):
    @abstractmethod
    def create_reload_builder(
            self,
            worker_names: list[str]
    ) -> BuilderConfig:
        pass

    @abstractmethod
    def create_reporter(self) -> ReporterBase:
        pass

    @abstractmethod
    def create_change_hook(self, webhook_secret: str) -> dict[str, Any]:
        pass

    @abstractmethod
    def create_avatar_method(self) -> AvatarBase:
        pass

    @abstractmethod
    def create_auth(self) -> AuthBase:
        pass

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
        webhook_secret: str,
    ) -> None:
        pass

    @abstractmethod
    def get_project_url() -> str:
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
