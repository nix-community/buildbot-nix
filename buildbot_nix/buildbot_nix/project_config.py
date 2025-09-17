"""Project configuration for buildbot-nix."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from buildbot.plugins import schedulers, util
from buildbot.process.project import Project

from .buildbot_effects import buildbot_effects_config
from .nix_build import BuildConfig, nix_build_config
from .nix_error import (
    nix_cached_failure_config,
    nix_dependency_failed_config,
    nix_failed_eval_config,
)
from .nix_eval import NixEvalConfig, nix_eval_config
from .nix_gcroot import nix_register_gcroot_config

if TYPE_CHECKING:
    from .projects import GitProject


@dataclass
class ProjectConfig:
    """Configuration for config_for_project function."""

    worker_names: list[str]
    nix_eval_config: NixEvalConfig
    build_config: BuildConfig
    per_repo_effects_secrets: dict[str, str]


def config_for_project(
    config: dict[str, Any],
    project: GitProject,
    project_config: ProjectConfig,
) -> None:
    config["projects"].append(Project(project.name))
    config["schedulers"].extend(
        [
            schedulers.SingleBranchScheduler(
                name=f"{project.project_id}-primary",
                change_filter=util.ChangeFilter(
                    repository=project.url,
                    filter_fn=lambda c: project_config.build_config.branch_config_dict.do_run(
                        project.default_branch, c.branch
                    ),
                ),
                builderNames=[f"{project.name}/nix-eval"],
                treeStableTimer=5,
            )
        ]
    )
    config["schedulers"].extend(
        [
            # this is compatible with bors or github's merge queue
            schedulers.SingleBranchScheduler(
                name=f"{project.project_id}-merge-queue",
                change_filter=util.ChangeFilter(
                    # TODO add filter
                    repository=project.url,
                    branch_re="(gh-readonly-queue/.*|staging|trying)",
                ),
                builderNames=[f"{project.name}/nix-eval"],
            ),
            # build all pull requests
            schedulers.SingleBranchScheduler(
                name=f"{project.project_id}-prs",
                change_filter=util.ChangeFilter(
                    repository=project.url,
                    category="pull",
                ),
                builderNames=[f"{project.name}/nix-eval"],
            ),
        ]
    )
    config["schedulers"].extend(
        [
            # this is triggered from `nix-eval`
            schedulers.Triggerable(
                name=f"{project.project_id}-nix-build",
                builderNames=[f"{project.name}/nix-build"],
            ),
            schedulers.Triggerable(
                name=f"{project.project_id}-run-effect",
                builderNames=[f"{project.name}/run-effect"],
            ),
            # this is triggered from `nix-build` when the build has failed eval
            schedulers.Triggerable(
                name=f"{project.project_id}-nix-failed-eval",
                builderNames=[f"{project.name}/nix-failed-eval"],
            ),
            # this is triggered from `nix-build` when the build has a failed dependency
            schedulers.Triggerable(
                name=f"{project.project_id}-nix-dependency-failed",
                builderNames=[f"{project.name}/nix-dependency-failed"],
            ),
            # this is triggered from `nix-build` when the build has a failed dependency
            schedulers.Triggerable(
                name=f"{project.project_id}-nix-cached-failure",
                builderNames=[f"{project.name}/nix-cached-failure"],
            ),
            schedulers.Triggerable(
                name=f"{project.project_id}-nix-register-gcroot",
                builderNames=[f"{project.name}/nix-register-gcroot"],
            ),
            # this is triggered from cached failure rebuilds
            schedulers.Triggerable(
                name=f"{project.project_id}-rebuild",
                builderNames=[f"{project.name}/nix-eval"],
            ),
            # allow to manually trigger a nix-build
            schedulers.ForceScheduler(
                name=f"{project.project_id}-force",
                builderNames=[f"{project.name}/nix-eval"],
                properties=[
                    util.StringParameter(
                        name="project",
                        label=f"Name of the {project.pretty_type} repository.",
                        default=project.name,
                    ),
                ],
            ),
        ],
    )
    key = f"{project.type}:{project.owner}/{project.repo}"
    effects_secrets_cred = project_config.per_repo_effects_secrets.get(key)

    config["builders"].extend(
        [
            # Since all workers run on the same machine, we only assign one of them to do the evaluation.
            # This should prevent excessive memory usage.
            nix_eval_config(
                project,
                project_config.worker_names,
                git_url=project.get_project_url(),
                nix_eval_config=project_config.nix_eval_config,
                build_config=project_config.build_config,
            ),
            nix_build_config(
                project,
                project_config.worker_names,
                build_config=project_config.build_config,
                gcroots_user=project_config.nix_eval_config.gcroots_user,
                show_trace=project_config.nix_eval_config.show_trace,
            ),
            buildbot_effects_config(
                project,
                git_url=project.get_project_url(),
                worker_names=project_config.worker_names,
                secrets=effects_secrets_cred,
            ),
            nix_failed_eval_config(project, project_config.worker_names),
            nix_dependency_failed_config(project, project_config.worker_names),
            nix_cached_failure_config(
                project=project,
                skipped_worker_names=project_config.worker_names,
                build_config=project_config.build_config,
            ),
            nix_register_gcroot_config(
                project=project,
                worker_names=project_config.worker_names,
                gcroots_user=project_config.nix_eval_config.gcroots_user,
            ),
        ],
    )
