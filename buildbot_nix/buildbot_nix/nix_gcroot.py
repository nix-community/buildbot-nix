"""Nix GC root registration builder."""

from __future__ import annotations

from typing import TYPE_CHECKING

from buildbot.plugins import steps, util

if TYPE_CHECKING:
    from buildbot.config.builder import BuilderConfig

    from .projects import GitProject


def nix_register_gcroot_config(
    project: GitProject,
    worker_names: list[str],
    gcroots_user: str = "buildbot-worker",
) -> BuilderConfig:
    factory = util.BuildFactory()

    # if the change got pulled in from a PR, the roots haven't been created yet
    factory.addStep(
        steps.ShellCommand(
            name="Register gcroot",
            command=[
                "nix-store",
                "--add-root",
                # FIXME: cleanup old build attributes
                util.Interpolate(
                    f"/nix/var/nix/gcroots/per-user/{gcroots_user}/%(prop:project)s/%(prop:attr)s",
                ),
                "-r",
                util.Property("out_path"),
            ],
            logEnviron=False,
        ),
    )

    return util.BuilderConfig(
        name=f"{project.name}/nix-register-gcroot",
        project=project.name,
        workernames=worker_names,
        collapseRequests=False,
        env={},
        factory=factory,
    )
