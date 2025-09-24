"""Buildbot effects builder."""

from __future__ import annotations

from typing import TYPE_CHECKING

from buildbot.plugins import steps, util

from .nix_eval import GitLocalPrMerge

if TYPE_CHECKING:
    from .projects import GitProject


def buildbot_effects_config(
    project: GitProject, git_url: str, worker_names: list[str], secrets: str | None
) -> util.BuilderConfig:
    """Builds one nix flake attribute."""
    factory = util.BuildFactory()
    url_with_secret = util.Interpolate(git_url)
    factory.addStep(
        GitLocalPrMerge(
            repourl=url_with_secret,
            method="clean",
            submodules=True,
            haltOnFailure=True,
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
                name="Run effects",
                command=[
                    "buildbot-effects",
                    "--rev",
                    util.Property("revision"),
                    "--branch",
                    util.Property("branch"),
                    "--repo",
                    util.Property("project"),
                    *secrets_args,
                    "run",
                    util.Property("command"),
                ],
                logEnviron=False,
            ),
        ],
        withSecrets=secrets_list,
    )
    return util.BuilderConfig(
        name=f"{project.name}/run-effect",
        project=project.name,
        workernames=worker_names,
        collapseRequests=False,
        env={},
        factory=factory,
    )
