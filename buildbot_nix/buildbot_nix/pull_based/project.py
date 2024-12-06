from typing import Any
from urllib.parse import ParseResult, urlparse

from buildbot.changes.base import ChangeSource
from buildbot.changes.gitpoller import GitPoller

from buildbot_nix.common import slugify_project_name
from buildbot_nix.projects import GitProject


class PullBasedProject(GitProject):
    parsed_url: ParseResult
    _url: str
    _name: str
    _default_branch: str
    poll_interval: int
    poll_spread: int
    ssh_private_key: str | None
    ssh_known_hosts: str | None

    def __init__(
        self,
        url: str,
        name: str,
        default_branch: str,
        poll_interval: int,
        poll_spread: int,
        ssh_private_key: str | None,
        ssh_known_hosts: str | None,
        **kwargs: Any,
    ) -> None:
        self.parsed_url = urlparse(url)
        self._url = url
        self._name = name
        self._default_branch = default_branch
        self.poll_interval = poll_interval
        self.poll_spread = poll_spread
        self.ssh_private_key = ssh_private_key
        self.ssh_known_hosts = ssh_known_hosts
        super().__init__(**kwargs)

    def get_project_url(self) -> str:
        return self.url

    def create_change_source(self) -> ChangeSource | None:
        return GitPoller(
            repourl=self.url,
            branches=True,
            pollInterval=self.poll_interval,
            pollRandomDelayMin=0,
            pollRandomDelayMax=self.poll_spread,
            pollAtLaunch=True,
            category="push",
            project=self.name,
            sshPrivateKey=self.ssh_private_key,
            sshKnownHosts=self.ssh_known_hosts,
        )

    @property
    def pretty_type(self) -> str:
        return "Pull Based"

    @property
    def type(self) -> str:
        return "pull-based"

    @property
    def repo(self) -> str:
        return self.name

    @property
    def nix_ref_type(self) -> str:
        return f"git+{self.parsed_url.scheme}"

    @property
    def owner(self) -> str:
        return "unknown"

    @property
    def name(self) -> str:
        return self._name

    @property
    def url(self) -> str:
        return self._url

    @property
    def project_id(self) -> str:
        return slugify_project_name(self._name)

    @property
    def default_branch(self) -> str:
        return self._default_branch

    @property
    def topics(self) -> list[str]:
        return []

    @property
    def belongs_to_org(self) -> bool:
        return False
