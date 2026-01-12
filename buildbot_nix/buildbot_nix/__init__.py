"""Buildbot-nix main module."""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from buildbot.configurators import ConfiguratorBase
from buildbot.plugins import schedulers, util, worker
from buildbot.secrets.providers.file import SecretInAFile
from twisted.logger import Logger

from buildbot_nix.oidc import OIDCAuth

from .authz import setup_authz
from .build_canceller import create_build_canceller
from .db_setup import DatabaseSetupService
from .errors import BuildbotNixError
from .gitea_projects import GiteaBackend
from .github_projects import GithubBackend
from .gitlab_project import GitlabBackend
from .local_worker import NixLocalWorker
from .models import AuthBackendConfig, BuildbotNixConfig
from .nix_build import BuildConfig
from .nix_eval import NixEvalConfig
from .oauth2_proxy_auth import OAuth2ProxyAuth
from .project_config import ProjectConfig, config_for_project
from .pull_based.backend import PullBasedBacked

if TYPE_CHECKING:
    from buildbot.www.auth import AuthBase

    from .projects import GitBackend, GitProject

log = Logger()


class PeriodicWithStartup(schedulers.Periodic):
    def __init__(self, *args: Any, run_on_startup: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.run_on_startup = run_on_startup

    async def activate(self) -> None:
        if self.run_on_startup:
            await self.setState("last_build", None)
        await super().activate()


class NixConfigurator(ConfiguratorBase):
    """Janitor is a configurator which create a Janitor Builder with all needed Janitor steps"""

    def __init__(self, config: BuildbotNixConfig) -> None:
        super().__init__()

        self.config = config

    def _initialize_backends(self) -> dict[str, GitBackend]:
        """Initialize git backends based on configuration."""
        backends: dict[str, GitBackend] = {}

        if self.config.github is not None:
            backends["github"] = GithubBackend(self.config.github, self.config.url)

        if self.config.gitea is not None:
            backends["gitea"] = GiteaBackend(self.config.gitea, self.config.url)

        if self.config.gitlab is not None:
            backends["gitlab"] = GitlabBackend(self.config.gitlab, self.config.url)

        if self.config.pull_based is not None:
            backends["pull_based"] = PullBasedBacked(self.config.pull_based)

        return backends

    def _setup_auth(self, backends: dict[str, GitBackend]) -> AuthBase | None:
        """Setup authentication based on configuration."""
        if self.config.auth_backend == AuthBackendConfig.httpbasicauth:
            return OAuth2ProxyAuth(self.config.http_basic_auth_password)
        if (
            self.config.auth_backend == AuthBackendConfig.oidc
            and self.config.oidc is not None
        ):
            return OIDCAuth(self.config.oidc)
        if self.config.auth_backend == AuthBackendConfig.none:
            return None
        if backends[self.config.auth_backend] is not None:
            return backends[self.config.auth_backend].create_auth()
        return None

    def _setup_workers(self, config: dict[str, Any]) -> list[str]:
        """Configure workers and return worker names."""
        worker_names = []

        # Add nix workers
        for w in self.config.nix_worker_secrets().workers:
            for i in range(w.cores):
                worker_name = f"{w.name}-{i:03}"
                config["workers"].append(worker.Worker(worker_name, w.password))
                worker_names.append(worker_name)

        # Add local workers
        for i in range(self.config.local_workers):
            worker_name = f"local-{i}"
            local_worker = NixLocalWorker(worker_name)
            config["workers"].append(local_worker)
            worker_names.append(worker_name)

        if not worker_names:
            msg = f"No workers configured in {self.config.nix_workers_secret_file} and {self.config.local_workers} local workers."
            raise BuildbotNixError(msg)

        return worker_names

    def _configure_projects(
        self,
        config: dict[str, Any],
        projects: list[GitProject],
        worker_names: list[str],
        eval_lock: util.MasterLock,
    ) -> list[GitProject]:
        """Configure individual projects and return succeeded projects."""
        succeeded_projects = []

        for project in projects:
            try:
                config_for_project(
                    config=config,
                    project=project,
                    project_config=ProjectConfig(
                        worker_names=worker_names,
                        nix_eval_config=NixEvalConfig(
                            supported_systems=self.config.build_systems,
                            failed_build_report_limit=self.config.failed_build_report_limit,
                            worker_count=self.config.eval_worker_count
                            or multiprocessing.cpu_count(),
                            max_memory_size=self.config.eval_max_memory_size,
                            eval_lock=eval_lock,
                            gcroots_user=self.config.gcroots_user,
                            cache_failed_builds=self.config.cache_failed_builds,
                            show_trace=self.config.show_trace_on_failure,
                        ),
                        build_config=BuildConfig(
                            post_build_steps=[
                                x.to_buildstep() for x in self.config.post_build_steps
                            ],
                            branch_config_dict=self.config.branches,
                            outputs_path=self.config.outputs_path,
                        ),
                        per_repo_effects_secrets=self.config.effects_per_repo_secrets,
                    ),
                )
            except Exception:  # noqa: BLE001
                log.failure(f"Failed to configure project {project.name}")
            else:
                succeeded_projects.append(project)

        return succeeded_projects

    def _setup_backend_services(
        self,
        config: dict[str, Any],
        backends: dict[str, GitBackend],
        worker_names: list[str],
    ) -> None:
        """Setup backend-specific services, schedulers, and reporters."""
        for backend in backends.values():
            # Reload backend projects
            reload_builder = backend.create_reload_builder([worker_names[0]])
            if reload_builder is not None:
                config["builders"].append(reload_builder)
                config["schedulers"].extend(
                    [
                        schedulers.ForceScheduler(
                            name=f"reload-{backend.type}-projects",
                            builderNames=[reload_builder.name],
                            buttonName="Update projects",
                        ),
                        PeriodicWithStartup(
                            name=f"reload-{backend.type}-projects-bidaily",
                            builderNames=[reload_builder.name],
                            periodicBuildTimer=12 * 60 * 60,
                            run_on_startup=not backend.are_projects_cached(),
                        ),
                    ]
                )

            config["services"].append(backend.create_reporter())
            config.setdefault("secretsProviders", [])
            config["secretsProviders"].extend(backend.create_secret_providers())

    def _setup_www_config(
        self,
        config: dict[str, Any],
        backends: dict[str, GitBackend],
        succeeded_projects: list[GitProject],
        auth: AuthBase | None,
    ) -> None:
        """Configure WWW settings including auth, change hooks, and avatar methods."""
        config["www"].setdefault("plugins", {})
        config["www"].setdefault("change_hook_dialects", {})
        config["www"].setdefault("avatar_methods", [])

        # Setup change hooks and avatar methods for backends
        for backend in backends.values():
            config["www"]["change_hook_dialects"][backend.change_hook_name] = (
                backend.create_change_hook()
            )

            avatar_method = backend.create_avatar_method()
            if avatar_method is not None:
                config["www"]["avatar_methods"].append(avatar_method)

        # Setup authentication and authorization
        if "auth" not in config["www"]:
            if auth is not None:
                config["www"]["auth"] = auth

            config["www"]["authz"] = setup_authz(
                admins=self.config.admins,
                backends=list(backends.values()),
                projects=succeeded_projects,
                allow_unauthenticated_control=self.config.allow_unauthenticated_control,
            )

    def configure(self, config: dict[str, Any]) -> None:
        # Initialize config defaults
        config.setdefault("projects", [])
        config.setdefault("secretsProviders", [])
        config.setdefault("www", {})
        config.setdefault("workers", [])
        config.setdefault("services", [])
        config.setdefault("schedulers", [])
        config.setdefault("builders", [])
        config.setdefault("change_source", [])

        # Setup components
        backends = self._initialize_backends()
        auth = self._setup_auth(backends)

        # Load projects from backends
        projects: list[GitProject] = []
        for backend in backends.values():
            projects += backend.load_projects()

        # Setup workers
        worker_names = self._setup_workers(config)

        # Initialize database and eval lock
        eval_lock = util.MasterLock("nix-eval")

        # Configure projects
        succeeded_projects = self._configure_projects(
            config, projects, worker_names, eval_lock
        )

        # Setup backend services
        self._setup_backend_services(config, backends, worker_names)

        # Setup database components
        config["services"].append(DatabaseSetupService())

        # Setup build canceller
        config["services"].append(create_build_canceller(succeeded_projects))

        # Setup systemd secrets
        credentials_directory = os.environ.get("CREDENTIALS_DIRECTORY", "./secrets")
        Path(credentials_directory).mkdir(exist_ok=True, parents=True)
        systemd_secrets = SecretInAFile(dirname=credentials_directory)
        config["secretsProviders"].append(systemd_secrets)

        # Setup change sources for projects
        for project in succeeded_projects:
            change_source = project.create_change_source()
            if change_source is not None:
                config["change_source"].append(change_source)

        # Setup WWW configuration
        self._setup_www_config(config, backends, succeeded_projects, auth)
