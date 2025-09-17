"""Nix build builder and steps."""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from buildbot.plugins import steps, util
from buildbot.process import buildstep, remotecommand
from buildbot.steps.trigger import Trigger
from buildbot.util.twisted import async_to_deferred

if TYPE_CHECKING:
    from buildbot.config.builder import BuilderConfig
    from buildbot.process.build import Build
    from buildbot.process.log import StreamLog

    from . import models
    from .projects import GitProject


@dataclass
class BuildConfig:
    """Configuration for build commands."""

    post_build_steps: list[steps.BuildStep] | list[models.PostBuildStep]
    branch_config_dict: models.BranchConfigDict
    outputs_path: Path | None = None


class NixBuildCommand(buildstep.ShellMixin, steps.BuildStep):
    """Builds a nix derivation."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs = self.setupShellMixin(kwargs)
        super().__init__(**kwargs)

    async def run(self) -> int:
        # run `nix build`
        cmd: remotecommand.RemoteCommand = await self.makeRemoteShellCommand()
        await self.runCommand(cmd)

        return cmd.results()


class UpdateBuildOutput(steps.BuildStep):
    """Updates store paths in a public www directory.
    This is useful to prefetch updates without having to evaluate
    on the target machine.
    """

    project: GitProject
    path: Path
    branch_config: models.BranchConfigDict

    def __init__(
        self,
        project: GitProject,
        path: Path,
        branch_config: models.BranchConfigDict,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.project = project
        self.path = path
        self.branch_config = branch_config

    def join_traversalsafe(self, root: Path, joined: Path) -> Path:
        root = root.resolve()

        for part in joined.parts:
            new_root = (root / part).resolve()

            if not new_root.is_relative_to(root):
                msg = f"Joined path attempted to traverse upwards when processing {root} against {part} (gave {new_root})"
                raise ValueError(msg)

            root = new_root

        return root

    def join_all_traversalsafe(self, root: Path, *paths: str) -> Path:
        for path in paths:
            root = self.join_traversalsafe(root, Path(path))

        return root

    async def run(self) -> int:
        props = self.build.getProperties()

        if (
            not self.branch_config.do_update_outputs(
                self.project.default_branch, props.getProperty("branch")
            )
            or props.getProperty("event") != "push"
        ):
            return util.SKIPPED

        out_path = props.getProperty("out_path")

        if not out_path:  # if, e.g., the build fails and doesn't produce an output
            return util.SKIPPED

        owner = urllib.parse.quote_plus(self.project.owner)
        repo = urllib.parse.quote_plus(self.project.repo)
        target = urllib.parse.quote_plus(props.getProperty("branch"))
        attr = urllib.parse.quote_plus(props.getProperty("attr"))

        try:
            file = self.join_all_traversalsafe(self.path, owner, repo, target, attr)
        except ValueError as e:
            error_log: StreamLog = await self.addLog("path_error")
            error_log.addStderr(f"Path traversal prevented ... skipping update: {e}")
            return util.FAILURE

        file.parent.mkdir(parents=True, exist_ok=True)

        file.write_text(out_path)

        return util.SUCCESS


@async_to_deferred
async def do_register_gcroot_if(
    s: steps.BuildStep | Build,
    branch_config: models.BranchConfigDict,
    gcroots_user: str,
) -> bool:
    gc_root = await util.Interpolate(
        f"/nix/var/nix/gcroots/per-user/{gcroots_user}/%(prop:project)s/%(prop:attr)s"
    ).getRenderingFor(s.getProperties())
    out_path = s.getProperty("out_path")

    return (
        s.getProperty("event") == "push"
        and branch_config.do_register_gcroot(
            s.getProperty("default_branch"), s.getProperty("branch")
        )
        and not (
            Path(str(gc_root)).exists()
            and Path(str(gc_root)).readlink() == Path(out_path)
        )
    )


def nix_build_steps(
    project: GitProject,
    build_config: BuildConfig,
    gcroots_user: str = "buildbot-worker",
    *,
    show_trace: bool = False,
) -> list[steps.BuildStep]:
    out_steps = [
        NixBuildCommand(
            # TODO: we want this eventually once we are able to the internal json format
            # env={"CLICOLOR_FORCE": "1"},
            name="Build flake attr",
            command=[
                "nix",
                "build",
                "-L",
                *(["--show-trace"] if show_trace else []),
                "--option",
                "keep-going",
                "true",
                # stop stuck builds after 20 minutes
                "--max-silent-time",
                str(60 * 20),
                "--accept-flake-config",
                "--out-link",
                util.Interpolate("result-%(prop:attr)s"),
                util.Interpolate("%(prop:drv_path)s^*"),
            ],
            # 3 hours, defaults to 20 minutes
            # We increase this over the default since the build output might end up in a different `nix build`.
            timeout=60 * 60 * 3,
            haltOnFailure=True,
            logEnviron=False,
        ),
        *build_config.post_build_steps,
        Trigger(
            name="Register gcroot",
            waitForFinish=True,
            schedulerNames=[f"{project.project_id}-nix-register-gcroot"],
            haltOnFailure=True,
            flunkOnFailure=True,
            sourceStamps=[],
            alwaysUseLatest=False,
            updateSourceStamp=False,
            doStepIf=lambda s: do_register_gcroot_if(
                s, build_config.branch_config_dict, gcroots_user
            ),
            copy_properties=["out_path", "attr"],
            set_properties={"report_status": False},
        ),
        steps.ShellCommand(
            name="Delete temporary gcroots",
            command=["rm", "-f", util.Interpolate("result-%(prop:attr)s")],
            logEnviron=False,
        ),
    ]

    if build_config.outputs_path is not None:
        out_steps.append(
            UpdateBuildOutput(
                project=project,
                name="Update build output",
                path=build_config.outputs_path,
                branch_config=build_config.branch_config_dict,
            ),
        )

    return out_steps


def nix_build_config(
    project: GitProject,
    worker_names: list[str],
    build_config: BuildConfig,
    gcroots_user: str = "buildbot-worker",
    *,
    show_trace: bool = False,
) -> BuilderConfig:
    """Builds one nix flake attribute."""
    factory = util.BuildFactory()
    factory.addSteps(
        nix_build_steps(
            project,
            build_config,
            gcroots_user,
            show_trace=show_trace,
        )
    )

    return util.BuilderConfig(
        name=f"{project.name}/nix-build",
        project=project.name,
        workernames=worker_names,
        collapseRequests=False,
        env={},
        factory=factory,
    )
