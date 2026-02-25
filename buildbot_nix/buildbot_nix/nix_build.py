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
from twisted.internet import threads

if TYPE_CHECKING:
    from buildbot.config.builder import BuilderConfig
    from buildbot.process.build import Build
    from buildbot.process.log import StreamLog

    from . import models
    from .projects import GitProject


def join_traversalsafe(root: Path, joined: Path) -> Path:
    """Join paths safely, preventing directory traversal attacks."""
    root = root.resolve()

    for part in joined.parts:
        new_root = (root / part).resolve()

        if not new_root.is_relative_to(root):
            msg = f"Joined path attempted to traverse upwards when processing {root} against {part} (gave {new_root})"
            raise ValueError(msg)

        root = new_root

    return root


def join_all_traversalsafe(root: Path, *paths: str) -> Path:
    """Join multiple paths safely, preventing directory traversal attacks."""
    for path in paths:
        root = join_traversalsafe(root, Path(path))

    return root


def write_output_path(
    outputs_path: Path, project: GitProject, branch: str, attr: str, out_path: str
) -> Path:
    """Write an output path to the outputs directory.

    Returns the file path that was written to.
    """
    owner = urllib.parse.quote_plus(project.owner)
    repo = urllib.parse.quote_plus(project.repo)
    target = urllib.parse.quote_plus(branch)
    safe_attr = urllib.parse.quote_plus(attr)

    file = join_all_traversalsafe(outputs_path, owner, repo, target, safe_attr)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(out_path)
    return file


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
    name = "update_build_output"

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

    async def run(self) -> int:
        props = self.build.getProperties()

        if (
            not self.branch_config.do_update_outputs(
                self.project.default_branch, props.getProperty("branch")
            )
            or props.getProperty("event", "push") != "push"
        ):
            return util.SKIPPED

        out_path = props.getProperty("out_path")

        if not out_path:  # if, e.g., the build fails and doesn't produce an output
            return util.SKIPPED

        branch = props.getProperty("branch")
        attr = props.getProperty("attr")

        try:
            write_output_path(self.path, self.project, branch, attr, out_path)
        except ValueError as e:
            error_log: StreamLog = await self.addLog("path_error")
            error_log.addStderr(f"Path traversal prevented ... skipping update: {e}")
            return util.FAILURE

        return util.SUCCESS


class ProcessSkippedBuilds(buildstep.ShellMixin, steps.BuildStep):
    """Process skipped builds by registering gcroots and updating build outputs."""

    def __init__(
        self,
        project: GitProject,
        gcroots_user: str,
        branch_config: models.BranchConfigDict,
        outputs_path: Path | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs = self.setupShellMixin(kwargs)
        super().__init__(**kwargs)
        self.project = project
        self.gcroots_user = gcroots_user
        self.branch_config = branch_config
        self.outputs_path = outputs_path

    async def run(self) -> int:
        if self.build is None:
            msg = "Build object is None"
            raise RuntimeError(msg)
        props = self.build.getProperties()

        # Collect all skipped builds' output paths from properties
        skipped_outputs = {}
        for prop_name, (value, _source) in props.properties.items():
            if prop_name.endswith("-skipped_out_path") and value:
                attr = prop_name[: -len("-skipped_out_path")]
                skipped_outputs[attr] = value

        if not skipped_outputs:
            return util.SKIPPED

        # Register gcroots for all skipped builds
        for attr, out_path in skipped_outputs.items():
            gcroot_path = f"/nix/var/nix/gcroots/per-user/{self.gcroots_user}/{self.project.name}/{attr}"
            cmd = await self.makeRemoteShellCommand(
                command=[
                    "nix-store",
                    "--add-root",
                    gcroot_path,
                    "-r",
                    out_path,
                ],
                logEnviron=False,
            )
            await self.runCommand(cmd)
            if cmd.didFail():
                return util.FAILURE

        # Update build outputs if configured and conditions are met
        if (
            self.outputs_path is not None
            and self.branch_config.do_update_outputs(
                self.project.default_branch, props.getProperty("branch")
            )
            and props.getProperty("event", "push") == "push"
        ):
            branch = props.getProperty("branch")
            for attr, out_path in skipped_outputs.items():
                try:
                    write_output_path(
                        self.outputs_path, self.project, branch, attr, out_path
                    )
                except ValueError as e:
                    error_log: StreamLog = await self.addLog("path_error")
                    error_log.addStderr(
                        f"Path traversal prevented ... skipping update: {e}"
                    )
                    return util.FAILURE

        return util.SUCCESS


def _gcroot_already_points_to(gc_root: str, out_path: str) -> bool:
    """Check if gc_root symlink already points to out_path (sync I/O)."""
    p = Path(gc_root)
    return p.exists() and p.readlink() == Path(out_path)


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

    if s.getProperty("event", "push") != "push":
        return False
    if not branch_config.do_register_gcroot(
        s.getProperty("default_branch"), s.getProperty("branch")
    ):
        return False

    already_registered: bool = await threads.deferToThread(  # type: ignore[no-untyped-call]
        _gcroot_already_points_to, str(gc_root), out_path
    )
    return not already_registered


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
