"""Scheduled effects builder and schedulers.

This module provides support for Hercules CI-compatible scheduled effects (onSchedule).
Scheduled effects run on a cron-like schedule rather than in response to git pushes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from buildbot.plugins import steps, util
from buildbot.schedulers import timed

from .models import ScheduledEffectConfig, ScheduleWhen
from .nix_eval import GitLocalPrMerge

if TYPE_CHECKING:
    from buildbot.config.builder import BuilderConfig

    from .projects import GitProject


def buildbot_effects_scheduled_config(
    project: GitProject,
    git_url: str,
    worker_names: list[str],
    secrets: str | None,
) -> BuilderConfig:
    """Builder for running scheduled effects.

    This builder checks out the default branch and runs a scheduled effect.
    """
    factory = util.BuildFactory()
    url_with_secret = util.Interpolate(git_url)

    factory.addStep(
        GitLocalPrMerge(
            repourl=url_with_secret,
            branch=project.default_branch,
            method="clean",
            submodules=True,
            haltOnFailure=True,
            logEnviron=False,
            sshPrivateKey=project.private_key_path.read_text()
            if project.private_key_path
            else None,
            sshKnownHosts=project.known_hosts_path.read_text()
            if project.known_hosts_path
            else None,
        ),
    )

    secrets_list = []
    secrets_args = []
    if secrets is not None:
        secrets_list = [("../secrets.json", util.Secret(secrets))]
        secrets_args = ["--secrets", "../secrets.json"]

    factory.addSteps(
        [
            steps.ShellCommand(
                name="Run scheduled effect",
                command=[
                    "buildbot-effects",
                    "--rev",
                    util.Property("got_revision"),
                    "--branch",
                    project.default_branch,
                    "--repo",
                    util.Property("project"),
                    *secrets_args,
                    "run-scheduled",
                    util.Property("schedule_name"),
                    util.Property("effect_name"),
                ],
                haltOnFailure=True,
                logEnviron=False,
            ),
        ],
        withSecrets=secrets_list,
    )

    return util.BuilderConfig(
        name=f"{project.name}/run-scheduled-effect",
        project=project.name,
        workernames=worker_names,
        collapseRequests=False,
        env={},
        factory=factory,
    )


def create_scheduled_effect_schedulers(
    project: GitProject,
    schedule_configs: dict[str, ScheduledEffectConfig],
) -> list[timed.Nightly]:
    """Create Buildbot Nightly schedulers for each scheduled effect.

    Args:
        project: The git project
        schedule_configs: Dict mapping schedule name to ScheduledEffectConfig

    Returns:
        List of Nightly schedulers
    """
    nightly_schedulers: list[timed.Nightly] = []

    for schedule_name, config in schedule_configs.items():
        for effect in config.effects:
            nightly_kwargs = config.when.to_buildbot_nightly_kwargs(schedule_name)

            scheduler = timed.Nightly(
                name=f"{project.project_id}-scheduled-{schedule_name}-{effect}",
                builderNames=[f"{project.name}/run-scheduled-effect"],
                properties={
                    "schedule_name": schedule_name,
                    "effect_name": effect,
                    "project": project.project_id,
                    "virtual_builder_name": f"scheduled.{schedule_name}.{effect}",
                    "status_name": f"scheduled.{schedule_name}.{effect}",
                },
                **nightly_kwargs,
            )
            nightly_schedulers.append(scheduler)

    return nightly_schedulers


def parse_schedules_from_json(
    schedules_json: dict[str, Any],
) -> dict[str, ScheduledEffectConfig]:
    """Parse schedule configurations from JSON (as returned by buildbot-effects list-schedules).

    Args:
        schedules_json: Dict from buildbot-effects list-schedules output

    Returns:
        Dict mapping schedule name to ScheduledEffectConfig
    """
    result: dict[str, ScheduledEffectConfig] = {}

    for schedule_name, schedule_data in schedules_json.items():
        when_data = schedule_data.get("when", {})
        effects = schedule_data.get("effects", [])

        when = ScheduleWhen(
            minute=when_data.get("minute"),
            hour=when_data.get("hour"),
            dayOfWeek=when_data.get("dayOfWeek"),
            dayOfMonth=when_data.get("dayOfMonth"),
        )

        result[schedule_name] = ScheduledEffectConfig(
            name=schedule_name,
            when=when,
            effects=effects,
        )

    return result
