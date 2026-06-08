"""Service configuration models.

Ported from buildbot_nix.models with all buildbot couplings removed:
no `Interpolate.to_buildbot`, no buildbot step construction, no
master/worker options. buildbot-nix is a single service, so options that
only made sense for the buildbot master/worker split are gone.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping  # noqa: TC003
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, GetCoreSchemaHandler, field_validator
from pydantic_core import CoreSchema, core_schema


class ConfigError(Exception):
    """Raised on invalid or inconsistent configuration."""


@dataclass
class RepoFilters:
    repo_allowlist: list[str] | None = None
    user_allowlist: list[str] | None = None
    topic: str | None = None


def read_secret_file(secret_file: Path) -> str:
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if directory is None:
        return secret_file.read_text().rstrip()
    return Path(directory).joinpath(secret_file).read_text().rstrip()


# Note that serialization isn't correct, as there is no way to *rename* the
# field `nix_type` to `_type`; one must always specify `by_alias=True`, such
# as `model_dump(by_alias=True)`. Relevant issue:
# https://github.com/pydantic/pydantic/issues/8379
class Interpolate(BaseModel):
    """A string with placeholder interpolation, set from repo config.

    Kept for config-format compatibility with the buildbot-era
    `Interpolate` (`{"_type": "interpolate", "value": ...}`); the service
    substitutes placeholders itself when running post-build steps.
    """

    model_config = ConfigDict(populate_by_name=True)

    nix_type: str = Field(alias="_type")
    value: str

    def __init__(self, value: str, **kwargs: Any) -> None:  # noqa: ARG002
        super().__init__(nix_type="interpolate", value=value)


class GiteaConfig(BaseModel):
    instance_url: str
    filters: RepoFilters = Field(default_factory=RepoFilters)

    token_file: Path = Field(default=Path("gitea-token"))

    oauth_id: str | None = None
    oauth_secret_file: Path | None = None

    ssh_private_key_file: Path | None = None
    ssh_known_hosts_file: Path | None = None

    @property
    def token(self) -> str:
        return read_secret_file(self.token_file)

    @property
    def oauth_secret(self) -> str:
        if self.oauth_secret_file is None:
            msg = "gitea.oauth_id is set but gitea.oauth_secret_file is missing"
            raise ConfigError(msg)
        return read_secret_file(self.oauth_secret_file)

    model_config = ConfigDict(
        extra="forbid",
        ignored_types=(property,),
    )


class GitlabConfig(BaseModel):
    instance_url: str = "https://gitlab.com"
    filters: RepoFilters = Field(default_factory=RepoFilters)

    token_file: Path = Field(default=Path("gitlab-token"))

    ssh_private_key_file: Path | None = None
    ssh_known_hosts_file: Path | None = None

    @property
    def token(self) -> str:
        return read_secret_file(self.token_file)

    model_config = ConfigDict(
        extra="forbid",
        ignored_types=(property,),
    )


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


class GitHubConfig(BaseModel):
    """GitHub App configuration (App auth only; token mode was dropped)."""

    id: int
    secret_key_file: Path
    # Overridable for GitHub Enterprise and integration tests.
    api_url: str = "https://api.github.com"
    webhook_secret_file: Path = Field(default=Path("github-webhook-secret"))

    filters: RepoFilters = Field(default_factory=RepoFilters)

    oauth_id: str | None = None
    oauth_secret_file: Path | None = None
    # Request the write-capable "repo" OAuth scope at login so private
    # repositories show up in the visibility check. GitHub has no
    # read-only repo scope, so public-only instances should leave this
    # off and not hold write-capable user tokens.
    oauth_private_repo_scope: bool = False

    @property
    def secret_key(self) -> str:
        return read_secret_file(self.secret_key_file)

    @property
    def webhook_secret(self) -> str:
        return read_secret_file(self.webhook_secret_file)

    @property
    def oauth_secret(self) -> str:
        if self.oauth_secret_file is None:
            msg = "github.oauth_id is set but github.oauth_secret_file is missing"
            raise ConfigError(msg)
        return read_secret_file(self.oauth_secret_file)

    model_config = ConfigDict(
        extra="forbid",
        ignored_types=(property,),
    )


class OIDCMappingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = "email"
    # Identity claim for admin/viewer matching. Defaults to the stable
    # "sub": a mutable claim like preferred_username would let a user
    # who can edit it hijack someone else's admin entry.
    username: str = "sub"
    full_name: str = "name"
    groups: str | None = None


class OIDCConfig(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        ignored_types=(property,),
    )

    name: str
    discovery_url: str
    client_id: str
    scope: list[str]
    mapping: OIDCMappingConfig = Field(default_factory=OIDCMappingConfig)

    client_secret_file: Path = Field(default=Path("oidc-client-secret"))

    @property
    def client_secret(self) -> str:
        return read_secret_file(self.client_secret_file)


class PostBuildStep(BaseModel):
    name: str
    environment: Mapping[str, str | Interpolate]
    command: list[str | Interpolate]
    warn_only: bool = False


def glob_to_regex(glob: str) -> re.Pattern:
    return re.compile(glob.replace("*", ".*").replace("?", "."))


class BranchConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    match_glob: str = Field(
        validation_alias="matchGlob", serialization_alias="matchGlob"
    )
    register_gcroots: bool = Field(
        validation_alias="registerGCRoots", serialization_alias="registerGCRoots"
    )
    update_outputs: bool = Field(
        validation_alias="updateOutputs", serialization_alias="updateOutputs"
    )

    match_regex: re.Pattern | None = Field(default=None, exclude=True)

    def model_post_init(self, __context: dict | None = None, /) -> None:
        self.match_regex = glob_to_regex(self.match_glob or "")

    def __or__(self, other: BranchConfig) -> BranchConfig:
        # A branch may match several globs; OR the boolean settings.
        # The first config's match_glob is kept (lookups already happened).
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

    def lookup_branch_config(self, branch: str | None) -> BranchConfig | None:
        if branch is None:
            return None
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
        self,
        default_branch: str,
        branch: str | None,
        fn: Callable[[BranchConfig], bool],
    ) -> bool:
        branch_config = self.lookup_branch_config(branch)
        return branch == default_branch or (
            branch_config is not None and fn(branch_config)
        )

    def do_run(self, default_branch: str, branch: str | None) -> bool:
        return self.check_lookup(default_branch, branch, lambda _: True)

    def do_register_gcroot(self, default_branch: str, branch: str | None) -> bool:
        return self.check_lookup(default_branch, branch, lambda bc: bc.register_gcroots)

    def do_update_outputs(self, default_branch: str, branch: str | None) -> bool:
        return self.check_lookup(default_branch, branch, lambda bc: bc.update_outputs)


class Config(BaseModel):
    """Top-level configuration for the buildbot-nix service."""

    model_config = ConfigDict(extra="forbid", ignored_types=(property,))

    # Either an inline DSN or a file containing it (for remote databases
    # whose password must not land in the world-readable nix store).
    db_url: str | None = None
    db_url_file: Path | None = None
    build_systems: list[str]
    domain: str
    url: str

    # Webhook URL may differ from the UI URL (e.g. split ingress).
    webhook_base_url: str | None = None

    @field_validator("url", "webhook_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str | None) -> str | None:
        # Joined with absolute paths everywhere; a trailing slash
        # produces double-slash URLs (the state API 404s on them).
        return value.rstrip("/") if value is not None else None

    # Directory for persistent clones, worktrees, logs, gc-roots, keys.
    state_dir: Path = Path("/var/lib/buildbot-nix")

    use_https: bool = False
    eval_max_memory_size: int = 4096
    admins: list[str] = []
    # Private visibility for non-forge logins; key/rule syntax in
    # docs/OIDC.md, semantics in auth.can_view_private.
    private_repo_viewers: dict[str, list[str]] = {}
    eval_worker_count: int | None = None
    # Concurrent evaluations (global); default matches today's global eval lock.
    eval_concurrency: int = 1
    # Global cap on concurrent attribute builds; None = derive from CPU count.
    build_concurrency: int | None = None

    gitea: GiteaConfig | None = None
    gitlab: GitlabConfig | None = None
    github: GitHubConfig | None = None
    pull_based: PullBasedConfig | None = None
    oidc: OIDCConfig | None = None
    outputs_path: Path | None = None
    post_build_steps: list[PostBuildStep] = []
    failed_build_report_limit: int = (
        47  # Default: 50 total - 3 reserved for eval/build/effects
    )
    branches: BranchConfigDict = BranchConfigDict({})
    # Outside /nix/var/nix/gcroots (e.g. dev setups) nix-store
    # --add-root registers indirect roots, so any writable dir works.
    gcroots_dir: Path = Path("/nix/var/nix/gcroots/per-user/buildbot-nix")

    effects_per_repo_secrets: dict[str, str] = {}
    effects_extra_sandbox_paths: list[Path] = []
    # JSON file with named mountables effects may request via
    # __hci_effect_mounts: {name: {source, readOnly, condition}}.
    effects_mountables_file: Path | None = None
    # nix options for the effect's private daemon, e.g. allowed-uris.
    effects_extra_nix_options: dict[str, str] = {}
    show_trace_on_failure: bool = False
    cache_failed_builds: bool = False
    allow_unauthenticated_control: bool = False
    build_max_silent_time: int = 60 * 20  # stop stuck builds after 20 minutes
    build_timeout: int = 60 * 60 * 3  # 3 hours default for nix build
    # Wall-clock limit for one nix-eval-jobs run; a hung evaluation
    # would otherwise hold the global eval slot forever.
    eval_timeout: int = 60 * 60

    # Per-attribute log size cap in bytes; head and tail kept on truncation.
    log_size_limit: int = 64 * 1024 * 1024
    # Build/log retention horizon in days.
    retention_days: int = 90
    # Session cookie lifetime in seconds (default 30 days).
    session_lifetime: int = 30 * 24 * 60 * 60
    # TTL for the per-user accessible-repo-set cache in seconds.
    repo_acl_cache_ttl: int = 60 * 60

    http_port: int = 8010
    http_unix_socket: Path | None = None

    @classmethod
    def load(cls, path: Path) -> Config:
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            msg = f"Failed to load config from {path}: {e}"
            raise ConfigError(msg) from e
        return cls.model_validate(data)


class ScheduleWhen(BaseModel):
    """Hercules CI schedule specification.

    Defines when a scheduled effect should run, similar to cron.
    All times are in UTC.
    """

    minute: int | None = None
    hour: int | list[int] | None = None
    dayOfWeek: list[str] | None = Field(default=None)  # noqa: N815
    dayOfMonth: list[int] | None = Field(default=None)  # noqa: N815

    def resolved(self, schedule_name: str = "") -> dict[str, Any]:
        """Resolve to concrete cron-like fields.

        An unset minute gets a deterministic pseudo-random default
        seeded by the schedule name (thundering-herd avoidance); an
        unset hour means every hour, like Hercules.
        Day-of-week names are normalized to integers (Mon=0 .. Sun=6).
        """
        fields: dict[str, Any] = {}
        seed = int(hashlib.sha256(schedule_name.encode()).hexdigest(), 16)

        fields["minute"] = self.minute if self.minute is not None else seed % 60
        fields["hour"] = self.hour if self.hour is not None else list(range(24))

        if self.dayOfWeek is not None:
            day_map = {
                "Mon": 0,
                "Tue": 1,
                "Wed": 2,
                "Thu": 3,
                "Fri": 4,
                "Sat": 5,
                "Sun": 6,
            }
            fields["dayOfWeek"] = [day_map[d] for d in self.dayOfWeek]

        if self.dayOfMonth is not None:
            fields["dayOfMonth"] = self.dayOfMonth

        return fields


class ScheduledEffectConfig(BaseModel):
    """A scheduled effect definition from the flake."""

    name: str
    when: ScheduleWhen
    effects: list[str]
