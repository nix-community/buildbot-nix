import json
import os
import re
from collections.abc import Callable, Mapping
from enum import Enum
from pathlib import Path
from typing import Any, Self

from buildbot.plugins import steps, util
from pydantic import BaseModel, ConfigDict, Field, GetCoreSchemaHandler, TypeAdapter
from pydantic_core import CoreSchema, core_schema

from .errors import BuildbotNixError


class InternalError(Exception):
    pass


def exclude_fields(fields: list[str]) -> dict[str, dict[str, bool]]:
    return {k: {"exclude": True} for k in fields}


class AuthBackendConfig(str, Enum):
    github = "github"
    gitea = "gitea"
    httpbasicauth = "httpbasicauth"
    none = "none"


def read_secret_file(secret_file: Path) -> str:
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if directory is None:
        return secret_file.read_text().rstrip()
    return Path(directory).joinpath(secret_file).read_text().rstrip()


# note that serialization isn't correct, as there is no way to *rename* the field `nix_type` to `_type`,
# one must always specify `by_alias = True`, such as `model_dump(by_alias = True)`, relevant issue:
# https://github.com/pydantic/pydantic/issues/8379
class Interpolate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    nix_type: str = Field(alias="_type")
    value: str

    @classmethod
    def to_buildbot(cls, value: str | Self) -> str | util.Interpolate:
        if isinstance(value, str):
            return value
        return util.Interpolate(value.value)

    def __init__(self, value: str, **kwargs: Any) -> None:
        super().__init__(nix_type="interpolate", value=value)


class GiteaConfig(BaseModel):
    instance_url: str
    topic: str | None

    token_file: Path = Field(default=Path("gitea-token"))
    webhook_secret_file: Path = Field(default=Path("gitea-webhook-secret"))
    project_cache_file: Path = Field(default=Path("gitea-project-cache.json"))

    oauth_id: str | None
    oauth_secret_file: Path | None

    ssh_private_key_file: Path | None
    ssh_known_hosts_file: Path | None

    @property
    def token(self) -> str:
        return read_secret_file(self.token_file)

    @property
    def webhook_secret(self) -> str:
        return read_secret_file(self.webhook_secret_file)

    @property
    def oauth_secret(self) -> str:
        if self.oauth_secret_file is None:
            raise InternalError
        return read_secret_file(self.oauth_secret_file)

    class Config:
        fields = exclude_fields(["token", "webhook_secret", "oauth_secret"])


class PullBasedRepository(BaseModel):
    name: str
    default_branch: str
    url: str
    poll_interval: int = 60

    ssh_private_key_file: Path | None = None
    ssh_known_hosts_file: Path | None = None

    @property
    def ssh_private_key(self) -> str | None:
        if self.ssh_private_key_file is not None:
            return read_secret_file(self.ssh_private_key_file)
        return None

    @property
    def ssh_known_hosts(self) -> str | None:
        if self.ssh_known_hosts_file is not None:
            return read_secret_file(self.ssh_known_hosts_file)
        return None


class PullBasedConfig(BaseModel):
    repositories: dict[str, PullBasedRepository]
    poll_spread: int | None = None


class GitHubLegacyConfig(BaseModel):
    token_file: Path

    @property
    def token(self) -> str:
        return read_secret_file(self.token_file)

    class Config:
        fields = exclude_fields(["token"])


class GitHubAppConfig(BaseModel):
    id: int

    secret_key_file: Path
    installation_token_map_file: Path = Field(
        default=Path("github-app-installation-token-map.json")
    )
    project_id_map_file: Path = Field(
        default=Path("github-app-project-id-map-name.json")
    )
    jwt_token_map: Path = Field(default=Path("github-app-jwt-token"))

    @property
    def secret_key(self) -> str:
        return read_secret_file(self.secret_key_file)

    class Config:
        fields = exclude_fields(["secret_key"])


class GitHubConfig(BaseModel):
    auth_type: GitHubLegacyConfig | GitHubAppConfig
    topic: str | None

    project_cache_file: Path = Field(default=Path("github-project-cache-v1.json"))
    webhook_secret_file: Path = Field(default=Path("github-webhook-secret"))

    oauth_id: str | None
    oauth_secret_file: Path | None

    @property
    def webhook_secret(self) -> str:
        return read_secret_file(self.webhook_secret_file)

    @property
    def oauth_secret(self) -> str:
        if self.oauth_secret_file is None:
            raise InternalError
        return read_secret_file(self.oauth_secret_file)


class PostBuildStep(BaseModel):
    name: str
    environment: Mapping[str, str | Interpolate]
    command: list[str | Interpolate]

    def to_buildstep(self) -> steps.BuildStep:
        return steps.ShellCommand(
            name=self.name,
            env={
                k: Interpolate.to_buildbot(self.environment[k])
                for k in self.environment
            },
            command=[Interpolate.to_buildbot(x) for x in self.command],
        )


def glob_to_regex(glob: str) -> re.Pattern:
    return re.compile(glob.replace("*", ".*").replace("?", "."))


class BranchConfig(BaseModel):
    match_glob: str = Field(validation_alias="matchGlob")
    register_gcroots: bool = Field(validation_alias="registerGCRoots")
    update_outputs: bool = Field(validation_alias="updateOutputs")

    match_regex: re.Pattern | None = Field(default=None, exclude=True)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

        match_glob = kwargs.get("match_glob") or kwargs["matchGlob"]
        self.match_regex = glob_to_regex(match_glob)

    def __or__(self, other: "BranchConfig") -> "BranchConfig":
        assert self.match_glob == other.match_glob
        assert self.match_regex == other.match_regex

        return BranchConfig(
            match_glob=self.match_glob,
            register_gcroots=self.register_gcroots or other.register_gcroots,
            update_outputs=self.update_outputs or other.update_outputs,
        )


class BranchConfigDict(dict[str, BranchConfig]):
    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, handler(dict[str, BranchConfig])
        )

    def lookup_branch_config(self, branch: str) -> BranchConfig | None:
        ret = None
        for branch_config in self.values():
            if (
                branch_config.match_regex is not None
                and branch_config.match_regex.fullmatch(branch)
            ):
                if ret is None:
                    ret = branch_config
                else:
                    ret |= branch_config
        return ret

    def check_lookup(
        self, default_branch: str, branch: str, fn: Callable[[BranchConfig], bool]
    ) -> bool:
        branch_config = self.lookup_branch_config(branch)
        return branch == default_branch or (
            branch_config is not None and fn(branch_config)
        )

    def do_run(self, default_branch: str, branch: str) -> bool:
        return self.check_lookup(default_branch, branch, lambda _: True)

    def do_register_gcroot(self, default_branch: str, branch: str) -> bool:
        return self.check_lookup(default_branch, branch, lambda bc: bc.register_gcroots)

    def do_update_outputs(self, default_branch: str, branch: str) -> bool:
        return self.check_lookup(default_branch, branch, lambda bc: bc.update_outputs)


class Worker(BaseModel):
    name: str
    cores: int
    password: str = Field(alias="pass")


class WorkerConfig(BaseModel):
    workers: list[Worker]


class BuildbotNixConfig(BaseModel):
    db_url: str
    build_systems: list[str]
    domain: str
    url: str

    use_https: bool = False
    auth_backend: AuthBackendConfig = AuthBackendConfig.none
    eval_max_memory_size: int = 4096
    admins: list[str] = []
    local_workers: int = 0
    eval_worker_count: int | None = None
    gitea: GiteaConfig | None = None
    github: GitHubConfig | None = None
    pull_based: PullBasedConfig | None
    outputs_path: Path | None = None
    post_build_steps: list[PostBuildStep] = []
    job_report_limit: int | None = None
    http_basic_auth_password_file: Path | None = None
    branches: BranchConfigDict = BranchConfigDict({})

    nix_workers_secret_file: Path | None = None
    effects_per_repo_secrets: dict[str, str] = {}
    show_trace_on_failure: bool = False

    def nix_worker_secrets(self) -> WorkerConfig:
        if self.nix_workers_secret_file is None:
            return WorkerConfig(workers=[])
        try:
            data = json.loads(read_secret_file(self.nix_workers_secret_file))
        except json.JSONDecodeError as e:
            msg = f"Failed to decode JSON from {self.nix_workers_secret_file}"
            raise BuildbotNixError(msg) from e
        return WorkerConfig(workers=data)

    @property
    def http_basic_auth_password(self) -> str:
        if self.http_basic_auth_password_file is None:
            raise InternalError
        return read_secret_file(self.http_basic_auth_password_file)


class CacheStatus(str, Enum):
    cached = "cached"
    local = "local"
    notBuilt = "notBuilt"  # noqa: N815


class NixEvalJobError(BaseModel):
    error: str
    attr: str
    attrPath: list[str]  # noqa: N815


class NixEvalJobSuccess(BaseModel):
    attr: str
    attrPath: list[str]  # noqa: N815
    cacheStatus: CacheStatus | None = None  # noqa: N815
    neededBuilds: list[str]  # noqa: N815
    neededSubstitutes: list[str]  # noqa: N815
    drvPath: str  # noqa: N815
    inputDrvs: dict[str, list[str]] | None = None  # noqa: N815
    name: str
    outputs: dict[str, str]
    system: str


NixEvalJob = NixEvalJobError | NixEvalJobSuccess
NixEvalJobModel: TypeAdapter[NixEvalJob] = TypeAdapter(NixEvalJob)


class NixDerivation(BaseModel):
    class InputDerivation(BaseModel):
        dynamicOutputs: dict[str, str]  # noqa: N815
        outputs: list[str]

    inputDrvs: dict[str, InputDerivation]  # noqa: N815
    # TODO parse out more information, if needed
