"""Nix error and failure builders and steps."""

from __future__ import annotations

import html
from typing import TYPE_CHECKING, Any

from buildbot.plugins import steps, util
from buildbot.steps.trigger import Trigger

if TYPE_CHECKING:
    from buildbot.config.builder import BuilderConfig
    from buildbot.process.log import StreamLog

    from .nix_build import BuildConfig
    from .projects import GitProject


class EvalErrorStep(steps.BuildStep):
    """Shows the error message of a failed evaluation."""

    async def run(self) -> int:
        error = self.getProperty("error")
        attr = self.getProperty("attr")
        error_log: StreamLog = await self.addLog("nix_error")
        error_log.addStderr(f"{attr} failed to evaluate:\n{error}")
        return util.FAILURE


class DependencyFailedStep(steps.BuildStep):
    """Shows a dependency failure."""

    async def run(self) -> int:
        dependency_attr = self.getProperty("dependency.attr")
        attr = self.getProperty("attr")
        error_log: StreamLog = await self.addLog("nix_error")
        error_log.addStderr(
            f"{attr} was failed because it depends on a failed build of {dependency_attr}.\n"
        )
        return util.FAILURE


class CachedFailureStep(steps.BuildStep):
    """Shows a cached build failure."""

    project: GitProject
    build_config: BuildConfig

    def __init__(
        self,
        project: GitProject,
        build_config: BuildConfig,
        **kwargs: Any,
    ) -> None:
        self.project = project
        self.build_config = build_config

        super().__init__(**kwargs)

    async def run(self) -> int:
        if self.build and self.build.reason != "rebuild":
            attr = self.getProperty("attr")
            url = self.getProperty("first_failure_url")
            if url:
                escaped_attr = html.escape(attr)
                escaped_url = html.escape(url)
                html_content = f"""
                <div style="font-family: monospace; background-color: #f8f9fa; padding: 10px; border-left: 4px solid #dc3545;">
                    <div style="color: #721c24; margin-bottom: 10px;">
                        {escaped_attr} was failed because it has failed previously and its failure has been cached.
                    </div>
                    <div style="margin-top: 10px;">
                        <strong>First failed build:</strong>
                        <a href="{escaped_url}" target="_blank" style="color: #0066cc; text-decoration: underline;">
                            {escaped_url}
                        </a>
                    </div>
                </div>
                """
                await self.addHTMLLog("cached_failure_info", html_content)
            return util.FAILURE
        self.build.addStepsAfterCurrentStep(
            [
                Trigger(
                    name="Rebuild cached failure",
                    waitForFinish=True,
                    schedulerNames=[f"{self.project.project_id}-rebuild"],
                    haltOnFailure=True,
                    flunkOnFailure=True,
                    sourceStamps=[],
                    alwaysUseLatest=False,
                    updateSourceStamp=False,
                    copy_properties=["attr", "system", "branch", "revision"],
                    set_properties={"reason": "rebuild"},
                )
            ]
        )
        return util.SUCCESS


def nix_failed_eval_config(
    project: GitProject,
    worker_names: list[str],
) -> BuilderConfig:
    """Dummy builder that is triggered when a build fails to evaluate."""
    factory = util.BuildFactory()
    factory.addStep(
        EvalErrorStep(
            name="Nix evaluation",
        ),
    )

    return util.BuilderConfig(
        name=f"{project.name}/nix-failed-eval",
        project=project.name,
        workernames=worker_names,
        collapseRequests=False,
        env={},
        factory=factory,
    )


def nix_dependency_failed_config(
    project: GitProject,
    worker_names: list[str],
) -> BuilderConfig:
    """Dummy builder that is triggered when a build has dependency failures."""
    factory = util.BuildFactory()
    factory.addStep(
        DependencyFailedStep(
            name="Dependency failed",
        ),
    )

    return util.BuilderConfig(
        name=f"{project.name}/nix-dependency-failed",
        project=project.name,
        workernames=worker_names,
        collapseRequests=False,
        env={},
        factory=factory,
    )


def nix_cached_failure_config(
    project: GitProject,
    skipped_worker_names: list[str],
    build_config: BuildConfig,
) -> BuilderConfig:
    """Dummy builder that is triggered when a build is cached as failed."""
    factory = util.BuildFactory()
    factory.addStep(
        CachedFailureStep(
            project=project,
            build_config=build_config,
            name="Cached failure",
            haltOnFailure=True,
            flunkOnFailure=True,
        ),
    )

    return util.BuilderConfig(
        name=f"{project.name}/nix-cached-failure",
        project=project.name,
        workernames=skipped_worker_names,
        collapseRequests=False,
        env={},
        factory=factory,
    )
