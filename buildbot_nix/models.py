from collections.abc import Mapping
from enum import Enum
from pathlib import Path

from buildbot.plugins import steps, util
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .secrets import read_secret_file


class InternalError(Exception):
    pass


def exclude_fields(fields: list[str]) -> dict[str, dict[str, bool]]:
    return {k: {"exclude": True} for k in fields}


class AuthBackendConfig(str, Enum):
    github = "github"
    gitea = "gitea"
    httpbasicauth = "httpbasicauth"
    none = "none"


# note that serialization isn't correct, as there is no way to *rename* the field `nix_type` to `_type`,
# one must always specify `by_alias = True`, such as `model_dump(by_alias = True)`, relevant issue:
# https://github.com/pydantic/pydantic/issues/8379
class Interpolate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    nix_type: str = Field(alias="_type")
    value: str

    def __init__(self, value: str) -> None:
        super().__init__(nix_type="interpolate", value=value)


class CachixConfig(BaseModel):
    name: str

    signing_key_file: Path | None
    auth_token_file: Path | None

    @property
    def signing_key(self) -> str:
        if self.signing_key_file is None:
            raise InternalError
        return read_secret_file(self.signing_key_file)

    @property
    def auth_token(self) -> str:
        if self.auth_token_file is None:
            raise InternalError
        return read_secret_file(self.auth_token_file)

    # TODO why did the original implementation return an empty env if both files were missing?
    @property
    def environment(self) -> Mapping[str, str | Interpolate]:
        environment = {}
        if self.signing_key_file is not None:
            environment["CACHIX_SIGNING_KEY"] = Interpolate(
                f"%(secret:{self.signing_key_file})s"
            )
        if self.auth_token_file is not None:
            environment["CACHIX_AUTH_TOKEN"] = Interpolate(
                f"%(secret:{self.auth_token_file})s"
            )
        return environment

    class Config:
        fields = exclude_fields(["signing_key", "auth_token"])


class GiteaConfig(BaseModel):
    instance_url: str
    topic: str | None

    token_file: Path = Field(default=Path("gitea-token"))
    webhook_secret_file: Path = Field(default=Path("gitea-webhook-secret"))
    project_cache_file: Path = Field(default=Path("gitea-project-cache.json"))

    oauth_id: str | None
    oauth_secret_file: Path | None

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
        def maybe_interpolate(value: str | Interpolate) -> str | util.Interpolate:
            if isinstance(value, str):
                return value
            return util.Interpolate(value.value)

        return steps.ShellCommand(
            name=self.name,
            env={k: maybe_interpolate(self.environment[k]) for k in self.environment},
            command=[maybe_interpolate(x) for x in self.command],
        )


class BuildbotNixConfig(BaseModel):
    db_url: str
    auth_backend: AuthBackendConfig
    cachix: CachixConfig | None
    gitea: GiteaConfig | None
    github: GitHubConfig | None
    admins: list[str]
    workers_file: Path
    build_systems: list[str]
    eval_max_memory_size: int
    eval_worker_count: int | None
    nix_workers_secret_file: Path = Field(default=Path("buildbot-nix-workers"))
    domain: str
    webhook_base_url: str
    use_https: bool
    outputs_path: Path | None
    url: str
    post_build_steps: list[PostBuildStep]
    job_report_limit: int | None
    http_basic_auth_password_file: Path | None

    @property
    def nix_workers_secret(self) -> str:
        return read_secret_file(self.nix_workers_secret_file)

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
    drvPath: str  # noqa: N815
    inputDrvs: dict[str, list[str]]  # noqa: N815
    name: str
    outputs: dict[str, str]
    system: str


NixEvalJob = NixEvalJobError | NixEvalJobSuccess
NixEvalJobModel = TypeAdapter(NixEvalJob)


class NixDerivation(BaseModel):
    class InputDerivation(BaseModel):
        dynamicOutputs: dict[str, str]  # noqa: N815
        outputs: list[str]

    inputDrvs: dict[str, InputDerivation]  # noqa: N815
    # TODO parse out more information, if needed
