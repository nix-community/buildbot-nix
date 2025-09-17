"""Nix evaluation builder and steps."""

from __future__ import annotations

import copy
import html
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from buildbot.interfaces import WorkerSetupError
from buildbot.plugins import steps, util
from buildbot.process import buildstep, logobserver, remotecommand
from buildbot.process.properties import Properties
from buildbot.steps.trigger import Trigger
from twisted.logger import Logger

from .build_trigger import BuildTrigger, JobsConfig, TriggerConfig
from .errors import BuildbotNixError
from .models import NixEvalJob, NixEvalJobError, NixEvalJobModel, NixEvalJobSuccess
from .nix_status_generator import CombinedBuildEvent
from .repo_config import BranchConfig

if TYPE_CHECKING:
    from buildbot.config.builder import BuilderConfig
    from buildbot.locks import MasterLock
    from buildbot.process.log import StreamLog

    from .failed_builds import FailedBuildDB
    from .projects import GitProject

log = Logger()


@dataclass
class NixEvalConfig:
    """Configuration for nix evaluation."""

    supported_systems: list[str]
    failed_build_report_limit: int
    worker_count: int
    max_memory_size: int

    eval_lock: MasterLock
    failed_builds_db: FailedBuildDB | None
    gcroots_user: str = "buildbot-worker"

    show_trace: bool = False


class GitLocalPrMerge(steps.Git):
    """GitHub sometimes fires the PR webhook before it has computed the merge commit.
    This is a workaround to fetch the merge commit and checkout the PR branch in CI.
    """

    async def run_vc(
        self,
        branch: str,
        revision: str,
        patch: str,
    ) -> int:
        build_props = self.build.getProperties()
        # TODO: abstract this into an interface as well
        merge_base = build_props.getProperty(
            "github.base.sha"
        ) or build_props.getProperty("base_sha")
        pr_head = build_props.getProperty("github.head.sha") or build_props.getProperty(
            "head_sha"
        )
        auth_workdir = self._get_auth_data_workdir()

        # Not a PR, fallback to default behavior
        if merge_base is None or pr_head is None:
            return await super().run_vc(branch, revision, patch)

        # The code below is a modified version of Git.run_vc
        self.stdio_log: StreamLog = await self.addLogForRemoteCommands("stdio")
        self.stdio_log.addStdout(f"Merging {merge_base} into {pr_head}\n")

        git_installed = await self.checkFeatureSupport()

        if not git_installed:
            msg = "git is not installed on worker"
            raise WorkerSetupError(msg)

        has_git = await self.pathExists(
            self.build.path_module.join(self.workdir, ".git")
        )

        try:
            await self._git_auth.download_auth_files_if_needed(auth_workdir)

            if not has_git:
                await self._dovccmd(
                    ["clone", "--recurse-submodules", self.repourl, "."]
                )

            patched = await self.sourcedirIsPatched()

            if patched:
                await self._dovccmd(["clean", "-f", "-f", "-d", "-x"])

            await self._dovccmd(
                ["fetch", "-f", "-t", self.repourl, merge_base, pr_head]
            )

            await self._dovccmd(["checkout", "--detach", "-f", pr_head])

            await self._dovccmd(
                [
                    "-c",
                    "user.email=buildbot@example.com",
                    "-c",
                    "user.name=buildbot",
                    "merge",
                    "--no-ff",
                    "-m",
                    f"Merge {merge_base} into {pr_head}",
                    merge_base,
                ]
            )
            self.updateSourceProperty("got_revision", pr_head)
            return await self.parseCommitDescription()
        finally:
            await self._git_auth.remove_auth_files_if_needed(auth_workdir)


class NixEvalCommand(buildstep.ShellMixin, steps.BuildStep):
    """Parses the output of `nix-eval-jobs` and triggers a `nix-build` build for
    every attribute.
    """

    project: GitProject

    renderables = ("drv_gcroots_dir",)

    def __init__(
        self,
        project: GitProject,
        nix_eval_config: NixEvalConfig,
        drv_gcroots_dir: util.Interpolate,
        **kwargs: Any,
    ) -> None:
        kwargs = self.setupShellMixin(kwargs)
        super().__init__(**kwargs)
        self.project = project
        self.nix_eval_config = nix_eval_config
        self.observer = logobserver.BufferLogObserver(wantStderr=True)
        self.addLogObserver("stdio", self.observer)
        self.warnings_count = 0
        self.warnings_processed = False
        self.drv_gcroots_dir = drv_gcroots_dir

    async def produce_event(self, event: str, result: None | int) -> None:
        if not self.build:
            return
        build: dict[str, Any] = await self.master.data.get(
            ("builds", str(self.build.buildid))
        )
        if result is not None:
            build["results"] = result
        event_key = ("builds", str(self.build.buildid), event)
        self.master.mq.produce(event_key, copy.deepcopy(build))

    async def run(self) -> int:
        await self.produce_event(CombinedBuildEvent.STARTED_NIX_EVAL.value, None)

        branch_config: BranchConfig = await BranchConfig.extract_during_step(self)

        # run nix-eval-jobs --flake .#checks to generate the dict of stages
        # !! Careful, the command attribute has to be specified here as the call
        # !! to `makeRemoteShellCommand` inside `BranchConfig.extract_during_step`
        # !! overrides `command`...
        cmd: remotecommand.RemoteCommand = await self.makeRemoteShellCommand(
            collectStdout=True,
            collectStderr=False,
            stdioLogName="stdio",
            command=[
                "nix-eval-jobs",
                "--option",
                "eval-cache",
                "false",
                "--workers",
                str(self.nix_eval_config.worker_count),
                "--max-memory-size",
                str(self.nix_eval_config.max_memory_size),
                "--option",
                "accept-flake-config",
                "true",
                "--gc-roots-dir",
                str(self.drv_gcroots_dir),
                "--force-recurse",
                "--check-cache-status",
                *(["--show-trace"] if self.nix_eval_config.show_trace else []),
                "--flake",
                f"{branch_config.flake_dir}#{branch_config.attribute}",
                *(
                    ["--reference-lock-file", branch_config.lock_file]
                    if branch_config.lock_file != "flake.lock"
                    else []
                ),
            ],
        )
        await self.runCommand(cmd)

        # if the command passes extract the list of stages
        result = cmd.results()

        # Process warnings if any
        result = await self._process_warnings(result)

        if self.build:
            await CombinedBuildEvent.produce_event_for_build(
                self.master,
                CombinedBuildEvent.FINISHED_NIX_EVAL,
                self.build,
                result,
                warnings_count=self.warnings_count,
            )
        if result in (util.SUCCESS, util.WARNINGS):
            # create a ShellCommand for each stage and add them to the build
            jobs: list[NixEvalJob] = []

            for line in self.observer.getStdout().split("\n"):
                if line != "":
                    try:
                        job = json.loads(line)
                    except json.JSONDecodeError as e:
                        msg = f"Failed to parse line: {line}"
                        raise BuildbotNixError(msg) from e
                    jobs.append(NixEvalJobModel.validate_python(job))

            failed_jobs: list[NixEvalJobError] = []
            successful_jobs: list[NixEvalJobSuccess] = []

            for job in jobs:
                # report unbuildable jobs
                if isinstance(job, NixEvalJobError):
                    failed_jobs.append(job)
                elif (
                    job.system in self.nix_eval_config.supported_systems
                    and isinstance(job, NixEvalJobSuccess)
                ):
                    successful_jobs.append(job)

            self.number_of_jobs = len(successful_jobs)

            if self.build:
                trigger_config = TriggerConfig(
                    builds_scheduler=f"{self.project.project_id}-nix-build",
                    failed_eval_scheduler=f"{self.project.project_id}-nix-failed-eval",
                    dependency_failed_scheduler=f"{self.project.project_id}-nix-dependency-failed",
                    cached_failure_scheduler=f"{self.project.project_id}-nix-cached-failure",
                )

                jobs_config = JobsConfig(
                    successful_jobs=successful_jobs,
                    failed_jobs=failed_jobs,
                    failed_builds_db=self.nix_eval_config.failed_builds_db,
                    failed_build_report_limit=self.nix_eval_config.failed_build_report_limit,
                )
                self.build.addStepsAfterCurrentStep(
                    [
                        BuildTrigger(
                            project=self.project,
                            trigger_config=trigger_config,
                            jobs_config=jobs_config,
                            nix_attr_prefix=branch_config.attribute,
                            name="build flake",
                        ),
                    ]
                )

        return result

    async def _process_warnings(self, result: int) -> int:
        """Process stderr output for warnings and update build status."""
        # Only process warnings once
        if self.warnings_processed:
            return result

        stderr_output = self.observer.getStderr()
        if not stderr_output:
            return result

        self.warnings_processed = True
        warning_lines = stderr_output.strip().split("\n")
        warnings_list = self._format_warnings(warning_lines)

        if not warnings_list:
            return result

        # Build HTML for each warning as a separate item
        warnings_html = []
        for _idx, warning in enumerate(warnings_list, 1):
            escaped_warning = html.escape(warning)
            # Create a nice card for each warning
            warnings_html.append(
                f"""<div style="background-color: #fefefe; border-left: 4px solid #ffc107;
                padding: 10px 15px; margin-bottom: 10px; border-radius: 3px;">
                <pre style="margin: 0; white-space: pre-wrap; color: #664d03; font-family: 'Monaco', 'Menlo', monospace; font-size: 13px;">{escaped_warning}</pre>
                </div>"""
            )

        # Add HTML log with formatted warnings
        await self.addHTMLLog(
            "Evaluation Warnings",
            f"""<div style="background-color: #fff3cd; border: 1px solid #ffc107;
            border-radius: 6px; padding: 20px; margin: 10px 0;">
            <h3 style="color: #856404; margin: 0 0 20px 0; font-size: 18px; display: flex; align-items: center;">
                <span style="margin-right: 10px;">⚠️</span>
                <span>Found {self.warnings_count} Evaluation Warning{"s" if self.warnings_count != 1 else ""}</span>
            </h3>
            <div>
                {"".join(warnings_html)}
            </div>
            </div>""",
        )

        # Set step to WARNINGS status if we have warnings but succeeded
        if result == util.SUCCESS and self.warnings_count > 0:
            result = util.WARNINGS

        return result

    def _format_warnings(self, warning_lines: list[str]) -> list[str]:
        """Extract and format evaluation warnings, returning a list of warning messages."""
        if not warning_lines:
            return []

        # Only keep evaluation warnings, filter out Nix configuration warnings
        eval_warnings = []
        i = 0

        while i < len(warning_lines):
            line = warning_lines[i]
            # Check if this is the start of an evaluation warning
            if "evaluation warning:" in line:
                # Remove the "evaluation warning:" prefix since we're already in that context
                first_line = line.replace("evaluation warning:", "").strip()
                warning_block = [first_line] if first_line else []
                self.warnings_count += 1
                i += 1

                # Collect all continuation lines (indented or empty lines followed by indented)
                while i < len(warning_lines):
                    next_line = warning_lines[i]
                    if next_line.startswith((" ", "\t")):
                        # Indented line - part of the warning, remove leading spaces
                        warning_block.append(next_line.strip())
                        i += 1
                    elif not next_line.strip():
                        # Empty line - might be part of multi-line warning
                        # Check if there's an indented line after it
                        if i + 1 < len(warning_lines) and warning_lines[
                            i + 1
                        ].startswith((" ", "\t")):
                            # Keep empty lines within warnings for readability
                            warning_block.append("")
                            i += 1
                        else:
                            # No indented line after empty line, warning is done
                            break
                    else:
                        # Non-indented, non-empty line - new warning or other output
                        break

                if warning_block:
                    eval_warnings.append("\n".join(warning_block))
            else:
                i += 1

        return eval_warnings

    def getCurrentSummary(self) -> dict[str, str]:  # noqa: N802
        """Show warning count in the step summary."""
        if self.warnings_count > 0:
            return {
                "step": f"({self.warnings_count} warning{'s' if self.warnings_count != 1 else ''})"
            }
        return {"step": "running"}


class RegisterSkippedGcroots(buildstep.ShellMixin, steps.BuildStep):
    """Register gcroots for all skipped builds at once."""

    project: GitProject
    gcroots_user: str
    branch_config: dict  # models.BranchConfigDict

    def __init__(
        self,
        project: GitProject,
        gcroots_user: str,
        branch_config: dict,  # models.BranchConfigDict
        **kwargs: Any,
    ) -> None:
        kwargs = self.setupShellMixin(kwargs)
        super().__init__(**kwargs)
        self.project = project
        self.gcroots_user = gcroots_user
        self.branch_config = branch_config

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

        return util.SUCCESS


class BuildbotEffectsCommand(buildstep.ShellMixin, steps.BuildStep):
    """Evaluate the effects of a flake and run them on the default branch."""

    def __init__(self, project: GitProject, **kwargs: Any) -> None:
        kwargs = self.setupShellMixin(kwargs)
        super().__init__(**kwargs)
        self.project = project
        self.observer = logobserver.BufferLogObserver()
        self.addLogObserver("stdio", self.observer)

    async def run(self) -> int:
        # run nix-eval-jobs --flake .#checks to generate the dict of stages
        cmd: remotecommand.RemoteCommand = await self.makeRemoteShellCommand()
        await self.runCommand(cmd)

        # if the command passes extract the list of stages
        result = cmd.results()
        if result == util.SUCCESS:
            # create a ShellCommand for each stage and add them to the build
            effects = json.loads(self.observer.getStdout())

            if self.build:
                self.build.addStepsAfterCurrentStep(
                    [
                        BuildbotEffectsTrigger(
                            project=self.project,
                            effects_scheduler=f"{self.project.project_id}-run-effect",
                            name="Buildbot effect",
                            effects=effects,
                        ),
                    ],
                )

        return result


class BuildbotEffectsTrigger(Trigger):
    """Dynamic trigger that run buildbot effect defined in a flake."""

    def __init__(
        self,
        project: GitProject,
        effects_scheduler: str,
        effects: list[dict[str, Any]],
        **kwargs: Any,
    ) -> None:
        if "name" not in kwargs:
            kwargs["name"] = "trigger"
        self.effects = effects
        self.config = None
        self.effects_scheduler = effects_scheduler
        self.project = project
        super().__init__(
            waitForFinish=True,
            schedulerNames=[effects_scheduler],
            haltOnFailure=True,
            flunkOnFailure=True,
            sourceStamps=[],
            alwaysUseLatest=False,
            updateSourceStamp=False,
            **kwargs,
        )

    async def run(self) -> int:  # type: ignore[override]
        if self.build and self.master:
            await CombinedBuildEvent.produce_event_for_build(
                self.master, CombinedBuildEvent.STARTED_NIX_EFFECTS, self.build, None
            )

        results = await super().run()

        if self.build and self.master:
            await CombinedBuildEvent.produce_event_for_build(
                self.master,
                CombinedBuildEvent.FINISHED_NIX_EFFECTS,
                self.build,
                results,
            )

        return results

    def createTriggerProperties(self, props: Any) -> Any:  # noqa: N802
        return props

    def getSchedulersAndProperties(self) -> list[tuple[str, Any]]:  # noqa: N802
        source = f"buildbot-run-effect-{self.project.project_id}"

        triggered_schedulers = []
        for effect in self.effects:
            props = Properties()
            props.setProperty("virtual_builder_name", effect, source)
            props.setProperty("status_name", f"effects.{effect}", source)
            props.setProperty("virtual_builder_tags", "", source)
            props.setProperty("command", effect, source)

            triggered_schedulers.append((self.effects_scheduler, props))

        return triggered_schedulers


def nix_eval_config(
    project: GitProject,
    worker_names: list[str],
    git_url: str,
    nix_eval_config: NixEvalConfig,
    build_config: Any,  # BuildConfig from nix_build module
) -> BuilderConfig:
    """Uses nix-eval-jobs to evaluate hydraJobs from flake.nix in parallel.
    For each evaluated attribute a new build pipeline is started.
    """
    factory = util.BuildFactory()
    # check out the source
    url_with_secret = util.Interpolate(git_url)
    factory.addStep(
        GitLocalPrMerge(
            repourl=url_with_secret,
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
    drv_gcroots_dir = util.Interpolate(
        f"/nix/var/nix/gcroots/per-user/{nix_eval_config.gcroots_user}/%(prop:project)s/drvs/",
    )

    factory.addStep(
        NixEvalCommand(
            project=project,
            env={"CLICOLOR_FORCE": "1"},
            name="Evaluate flake",
            nix_eval_config=nix_eval_config,
            haltOnFailure=True,
            locks=[nix_eval_config.eval_lock.access("exclusive")],
            drv_gcroots_dir=drv_gcroots_dir,
            logEnviron=False,
        ),
    )

    # Register gcroots for all skipped builds at once
    factory.addStep(
        RegisterSkippedGcroots(
            project=project,
            gcroots_user=nix_eval_config.gcroots_user,
            branch_config=build_config.branch_config_dict,
            name="Register gcroots for skipped builds",
            doStepIf=lambda s: s.getProperty("event") == "push"
            and build_config.branch_config_dict.do_register_gcroot(
                project.default_branch, s.getProperty("branch")
            ),
            hideStepIf=lambda results, _s: results == util.SKIPPED,
        ),
    )
    factory.addStep(
        steps.ShellCommand(
            name="Cleanup drv paths",
            command=[
                "rm",
                "-rf",
                drv_gcroots_dir,
            ],
            logEnviron=False,
        ),
    )
    factory.addStep(
        BuildbotEffectsCommand(
            project=project,
            env={},
            name="Evaluate effects",
            command=[
                "buildbot-effects",
                "--rev",
                util.Property("revision"),
                "--branch",
                util.Property("branch"),
                "--repo",
                util.Property("github.repository.html_url"),
                "list",
            ],
            flunkOnFailure=True,
            # TODO: support other branches?
            doStepIf=lambda c: c.build.getProperty("branch", "")
            == project.default_branch,
            logEnviron=False,
        )
    )

    return util.BuilderConfig(
        name=f"{project.name}/nix-eval",
        workernames=worker_names,
        project=project.name,
        factory=factory,
        properties={"status_name": "nix-eval"},
    )
