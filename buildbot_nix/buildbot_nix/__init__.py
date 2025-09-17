import atexit
import copy
import html
import json
import multiprocessing
import os
import re
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from buildbot.config.builder import BuilderConfig
from buildbot.configurators import ConfiguratorBase
from buildbot.interfaces import WorkerSetupError
from buildbot.locks import MasterLock
from buildbot.plugins import schedulers, steps, util, worker
from buildbot.process import buildstep, logobserver, remotecommand
from buildbot.process.build import Build
from buildbot.process.project import Project
from buildbot.process.properties import Properties
from buildbot.secrets.providers.file import SecretInAFile
from buildbot.steps.trigger import Trigger
from buildbot.util.twisted import async_to_deferred
from buildbot.www.authz import Authz
from buildbot.www.authz.endpointmatchers import EndpointMatcherBase, Match

from buildbot_nix.pull_based.backend import PullBasedBacked

if TYPE_CHECKING:
    from buildbot.process.log import StreamLog

from buildbot.www.auth import AuthBase
from twisted.logger import Logger

from . import models
from .build_trigger import BuildTrigger, JobsConfig, TriggerConfig
from .errors import BuildbotNixError
from .failed_builds import FailedBuildDB
from .gitea_projects import GiteaBackend
from .github_projects import (
    GithubBackend,
)
from .models import (
    AuthBackendConfig,
    BuildbotNixConfig,
    NixEvalJob,
    NixEvalJobError,
    NixEvalJobModel,
    NixEvalJobSuccess,
)
from .nix_status_generator import CombinedBuildEvent
from .oauth2_proxy_auth import OAuth2ProxyAuth
from .projects import GitBackend, GitProject
from .repo_config import BranchConfig


@dataclass
class BuildConfig:
    """Configuration for build commands."""

    post_build_steps: list[steps.BuildStep] | list[models.PostBuildStep]
    branch_config_dict: models.BranchConfigDict
    outputs_path: Path | None = None


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


@dataclass
class ProjectConfig:
    """Configuration for config_for_project function."""

    worker_names: list[str]
    nix_eval_config: NixEvalConfig
    build_config: BuildConfig
    per_repo_effects_secrets: dict[str, str]


class NixLocalWorker(worker.LocalWorker):
    """LocalWorker with increased max_line_length for nix-eval-jobs output."""

    @async_to_deferred
    async def reconfigService(self, name: str, **kwargs: Any) -> None:
        # First do the normal reconfiguration
        await super().reconfigService(name, **kwargs)
        self.remote_worker.bot.max_line_length = 10485760  # 10MB


log = Logger()


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

    def getSchedulersAndProperties(self) -> list[tuple[str, Properties]]:  # noqa: N802
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


class EvalErrorStep(steps.BuildStep):
    """Shows the error message of a failed evaluation."""

    async def run(self) -> int:
        error = self.getProperty("error")
        attr = self.getProperty("attr")
        # show eval error
        error_log: StreamLog = await self.addLog("nix_error")
        error_log.addStderr(f"{attr} failed to evaluate:\n{error}")
        return util.FAILURE


class DependencyFailedStep(steps.BuildStep):
    """Shows a dependency failure."""

    async def run(self) -> int:
        dependency_attr = self.getProperty("dependency.attr")
        attr = self.getProperty("attr")
        # show eval error
        error_log: StreamLog = await self.addLog("nix_error")
        error_log.addStderr(
            f"{attr} was failed because it depends on a failed build of {dependency_attr}.\n"
        )
        return util.FAILURE


class CachedFailureStep(steps.BuildStep):
    """Shows a dependency failure."""

    project: GitProject
    worker_names: list[str]
    post_build_steps: list[models.PostBuildStep]
    branch_config_dict: models.BranchConfigDict
    outputs_path: Path | None
    show_trace: bool

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
            # show eval error
            # Create expanded HTML log with clickable link
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


class RegisterSkippedGcroots(buildstep.ShellMixin, steps.BuildStep):
    """Register gcroots for all skipped builds at once."""

    project: GitProject
    gcroots_user: str
    branch_config: models.BranchConfigDict

    def __init__(
        self,
        project: GitProject,
        gcroots_user: str,
        branch_config: models.BranchConfigDict,
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


# GitHub sometimes fires the PR webhook before it has computed the merge commit
# This is a workaround to fetch the merge commit and checkout the PR branch in CI
class GitLocalPrMerge(steps.Git):
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


def nix_eval_config(
    project: GitProject,
    worker_names: list[str],
    git_url: str,
    nix_eval_config: NixEvalConfig,
    build_config: BuildConfig,
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


def nix_failed_eval_config(
    project: GitProject,
    worker_names: list[str],
) -> BuilderConfig:
    """Dummy builder that is triggered when a build fails to evaluate."""
    factory = util.BuildFactory()
    factory.addStep(
        EvalErrorStep(
            name="Nix evaluation",
            doStepIf=lambda _: True,  # not done steps cannot fail...
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
    """Dummy builder that is triggered when a build fails to evaluate."""
    factory = util.BuildFactory()
    factory.addStep(
        DependencyFailedStep(
            name="Dependency failed",
            doStepIf=lambda _: True,  # not done steps cannot fail...
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
                    # TODO: gittea
                    util.Property("github.base.repo.full_name"),
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


def normalize_virtual_builder_name(name: str) -> str:
    if re.match(r"^[^:]+:", name) is not None:
        # rewrites github:nix-community/srvos#checks.aarch64-linux.nixos-stable-example-hardware-hetzner-online-intel -> nix-community/srvos/nix-build
        match = re.match(r"[^:]+:(?P<owner>[^/]+)/(?P<repo>[^#]+)#.+", name)
        if match:
            return f"{match['owner']}/{match['repo']}/nix-build"

    return name


class AnyProjectEndpointMatcher(EndpointMatcherBase):
    def __init__(self, builders: set[str] | None = None, **kwargs: Any) -> None:
        if builders is None:
            builders = set()
        self.builders = builders
        super().__init__(**kwargs)

    async def check_builder(
        self,
        endpoint_object: Any,
        endpoint_dict: dict[str, Any],
        object_type: str,
    ) -> Match | None:
        res = await endpoint_object.get({}, endpoint_dict)
        if res is None:
            return None

        builderid = res.get("builderid")
        if builderid is None:
            builder_name = res["builder_names"][0]
        else:
            builder = await self.master.data.get(("builders", builderid))
            builder_name = builder["name"]

        builder_name = normalize_virtual_builder_name(builder_name)
        if builder_name in self.builders:
            log.warn(
                "Builder {builder} allowed by {role}: {builders}",
                builder=builder_name,
                role=self.role,
                builders=self.builders,
            )
            return Match(self.master, **{object_type: res})
        log.warn(
            "Builder {builder} not allowed by {role}: {builders}",
            builder=builder_name,
            role=self.role,
            builders=self.builders,
        )
        return None

    async def match_ForceSchedulerEndpoint_force(  # noqa: N802
        self,
        epobject: Any,
        epdict: dict[str, Any],
        _options: dict[str, Any],
    ) -> Match | None:
        return await self.check_builder(epobject, epdict, "build")

    async def match_BuildEndpoint_rebuild(  # noqa: N802
        self, epobject: Any, epdict: dict[str, Any], _options: dict[str, Any]
    ) -> Match | None:
        return await self.check_builder(epobject, epdict, "build")

    async def match_BuildEndpoint_stop(  # noqa: N802
        self,
        epobject: Any,
        epdict: dict[str, Any],
        _options: dict[str, Any],
    ) -> Match | None:
        return await self.check_builder(epobject, epdict, "build")

    async def match_BuildRequestEndpoint_stop(  # noqa: N802
        self,
        epobject: Any,
        epdict: dict[str, Any],
        _options: dict[str, Any],
    ) -> Match | None:
        return await self.check_builder(epobject, epdict, "buildrequest")


def setup_authz(
    backends: list[GitBackend], projects: list[GitProject], admins: list[str]
) -> Authz:
    allow_rules = []
    allowed_builders_by_org: defaultdict[str, set[str]] = defaultdict(
        lambda: {backend.reload_builder_name for backend in backends},
    )

    for project in projects:
        if project.belongs_to_org:
            for builder in ["nix-build", "nix-eval"]:
                allowed_builders_by_org[project.owner].add(f"{project.name}/{builder}")

    for org, allowed_builders in allowed_builders_by_org.items():
        allow_rules.append(
            AnyProjectEndpointMatcher(
                builders=allowed_builders,
                role=org,
                defaultDeny=False,
            ),
        )

    allow_rules.append(util.AnyEndpointMatcher(role="admin", defaultDeny=False))
    allow_rules.append(util.AnyControlEndpointMatcher(role="admins"))
    return util.Authz(
        roleMatchers=[
            util.RolesFromUsername(roles=["admin"], usernames=admins),
            util.RolesFromGroups(groupPrefix=""),  # so we can match on ORG
        ],
        allowRules=allow_rules,
    )


class PeriodicWithStartup(schedulers.Periodic):
    def __init__(self, *args: Any, run_on_startup: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.run_on_startup = run_on_startup

    async def activate(self) -> None:
        if self.run_on_startup:
            await self.setState("last_build", None)
        await super().activate()


DB: FailedBuildDB | None = None


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

        if self.config.pull_based is not None:
            backends["pull_based"] = PullBasedBacked(self.config.pull_based)

        return backends

    def _setup_auth(self, backends: dict[str, GitBackend]) -> AuthBase | None:
        """Setup authentication based on configuration."""
        if self.config.auth_backend == AuthBackendConfig.httpbasicauth:
            return OAuth2ProxyAuth(self.config.http_basic_auth_password)
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
                            failed_builds_db=DB,
                            gcroots_user=self.config.gcroots_user,
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

        global DB  # noqa: PLW0603
        if DB is None and self.config.cache_failed_builds:
            DB = FailedBuildDB(Path("failed_builds.dbm"))
            atexit.register(lambda: DB.close() if DB is not None else None)

        # Configure projects
        succeeded_projects = self._configure_projects(
            config, projects, worker_names, eval_lock
        )

        # Setup backend services
        self._setup_backend_services(config, backends, worker_names)

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
