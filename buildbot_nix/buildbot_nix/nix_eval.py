"""Nix evaluation builder and steps."""

from __future__ import annotations

import contextlib
import copy
import html
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from buildbot.interfaces import WorkerSetupError
from buildbot.plugins import steps, util
from buildbot.process import buildstep, logobserver, remotecommand
from buildbot.process.properties import Properties
from buildbot.steps.trigger import Trigger
from twisted.internet import reactor
from twisted.logger import Logger

from .build_trigger import BuildTrigger, JobsConfig, TriggerConfig
from .errors import BuildbotNixError
from .models import (
    CacheStatus,
    NixEvalJob,
    NixEvalJobError,
    NixEvalJobModel,
    NixEvalJobSuccess,
)
from .nix_build import ProcessSkippedBuilds
from .nix_status_generator import CombinedBuildEvent
from .repo_config import BranchConfig

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from buildbot.config.builder import BuilderConfig
    from buildbot.locks import MasterLock
    from buildbot.process.log import StreamLog

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
    gcroots_user: str = "buildbot-worker"
    cache_failed_builds: bool = False

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

        # Check if this is a rebuild and try to reuse evaluation from original build
        if not self.build or not self.build.requests:
            log.info("No build requests available, skipping rebuild check")
        else:
            buildset_id = self.build.requests[0].bsid
            if buildset_id is not None:
                buildset = await self.master.data.get(("buildsets", str(buildset_id)))
                if (
                    buildset
                    and (rebuilt_buildid := buildset.get("rebuilt_buildid")) is not None
                ):
                    # This is a rebuild - try to reuse evaluation from original build
                    jobs = await self._reconstruct_jobs_from_rebuild(rebuilt_buildid)
                    if jobs is not None:
                        # Successfully reconstructed jobs, process them
                        await self._process_jobs_and_trigger_builds(jobs, branch_config)
                        result = util.SUCCESS
                        if self.build:
                            await CombinedBuildEvent.produce_event_for_build(
                                self.master,
                                CombinedBuildEvent.FINISHED_NIX_EVAL,
                                self.build,
                                result,
                                warnings_count=self.warnings_count,
                            )
                        return result

        # Either not a rebuild or reconstruction failed - run full evaluation
        result = await self._run_nix_eval_jobs(branch_config)

        if self.build:
            await CombinedBuildEvent.produce_event_for_build(
                self.master,
                CombinedBuildEvent.FINISHED_NIX_EVAL,
                self.build,
                result,
                warnings_count=self.warnings_count,
            )

        return result

    async def _run_nix_eval_jobs(self, branch_config: BranchConfig) -> int:
        """Run nix-eval-jobs and process the results."""
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
        result = await self._process_warnings(result, branch_config=branch_config)

        if result in (util.SUCCESS, util.WARNINGS):
            # Parse the nix-eval-jobs output
            jobs: list[NixEvalJob] = []

            for line in self.observer.getStdout().split("\n"):
                if line != "":
                    try:
                        job = json.loads(line)
                    except json.JSONDecodeError as e:
                        msg = f"Failed to parse line: {line}"
                        raise BuildbotNixError(msg) from e
                    jobs.append(NixEvalJobModel.validate_python(job))

            # Process jobs and trigger builds
            await self._process_jobs_and_trigger_builds(jobs, branch_config)

        return result

    async def _process_jobs_and_trigger_builds(
        self, jobs: list[NixEvalJob], branch_config: BranchConfig
    ) -> None:
        """Process jobs and trigger builds. Used by both normal eval and rebuild paths."""
        failed_jobs: list[NixEvalJobError] = []
        successful_jobs: list[NixEvalJobSuccess] = []

        for job in jobs:
            # report unbuildable jobs
            if isinstance(job, NixEvalJobError):
                failed_jobs.append(job)
            elif job.system in self.nix_eval_config.supported_systems and isinstance(
                job, NixEvalJobSuccess
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
                cache_failed_builds=self.nix_eval_config.cache_failed_builds,
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

    async def _check_store_paths_batch(
        self, paths: list[str], batch_size: int = 1000
    ) -> AsyncGenerator[bool, None]:
        """Check validity of store paths in batches. Yields validity status for each path."""
        for i in range(0, len(paths), batch_size):
            batch_paths = paths[i : i + batch_size]
            cmd = await self.makeRemoteShellCommand(
                command=["nix-store", "--check-validity", *batch_paths],
                collectStdout=True,
                collectStderr=False,
            )
            await self.runCommand(cmd)

            if cmd.results() == util.SUCCESS:
                # All paths in batch are valid
                for _ in batch_paths:
                    yield True
            else:
                # Check individually to find which are valid
                for path in batch_paths:
                    cmd = await self.makeRemoteShellCommand(
                        command=["nix-store", "--check-validity", path],
                        collectStdout=False,
                        collectStderr=False,
                    )
                    await self.runCommand(cmd)
                    yield cmd.results() == util.SUCCESS

    async def _reconstruct_job_from_build(
        self, build_id: int, original_build_id: int
    ) -> tuple[NixEvalJobSuccess, str] | None:
        """Validate and reconstruct a NixEvalJob from build properties.

        Returns tuple of (job, out_path) or None if validation fails.
        """
        props = await self.master.db.builds.getBuildProperties(build_id)
        required_props = ["attr", "drv_path", "out_path", "system"]

        # Validate required properties
        for prop in required_props:
            if prop not in props or props[prop][0] is None:
                log.info(
                    f"Cannot reconstruct job from build {original_build_id}: missing required property '{prop}'"
                )
                return None

        # Extract properties
        attr = props["attr"][0]
        drv_path = props["drv_path"][0]
        out_path = props["out_path"][0]
        system = props["system"][0]

        job = NixEvalJobSuccess(
            attr=attr,
            attrPath=attr.split("."),
            drvPath=drv_path,
            outputs={"out": out_path},
            system=system,
            name=attr,
            cacheStatus=CacheStatus.notBuilt,
            neededBuilds=[],
            neededSubstitutes=[],
        )

        return job, out_path

    async def _reconstruct_jobs_from_rebuild(
        self, original_build_id: int
    ) -> list[NixEvalJob] | None:
        """Reconstruct job list from the original build's triggered builds."""
        # Get all builds triggered by the original eval
        triggered_builds = await self.master.db.builds.get_triggered_builds(
            original_build_id
        )

        if not triggered_builds:
            return None

        # Reconstruct all jobs
        jobs = []
        outputs_to_check = []

        for build in triggered_builds:
            result = await self._reconstruct_job_from_build(build.id, original_build_id)
            if result is None:
                # Missing required properties, can't reconstruct
                return None

            job, out_path = result
            jobs.append(job)

            # Collect outputs that need checking
            if build.results == util.SUCCESS and out_path:
                outputs_to_check.append((job, out_path))

        # Batch check which outputs still exist
        if outputs_to_check:
            output_paths = [path for _, path in outputs_to_check]

            # Process validity results as they come from the generator
            validity_iter = self._check_store_paths_batch(output_paths)
            i = 0
            async for is_valid in validity_iter:
                if is_valid:
                    outputs_to_check[i][0].cacheStatus = CacheStatus.local
                i += 1

        # Verify derivations exist for jobs that need rebuilding
        jobs_to_rebuild = [job for job in jobs if job.cacheStatus != CacheStatus.local]

        if jobs_to_rebuild and not await self._verify_derivations_exist(
            jobs_to_rebuild
        ):
            return None

        self.descriptionDone = [f"reused eval from build {original_build_id}"]
        return cast("list[NixEvalJobError | NixEvalJobSuccess]", jobs)

    async def _verify_derivations_exist(
        self, jobs: Sequence[NixEvalJobError | NixEvalJobSuccess]
    ) -> bool:
        """Verify all derivations exist for the given jobs."""
        drv_paths = [job.drvPath for job in jobs if isinstance(job, NixEvalJobSuccess)]

        # Check all derivations - if any is invalid, return False
        async for is_valid in self._check_store_paths_batch(drv_paths):
            if not is_valid:
                return False
        return True

    async def _process_warnings(self, result: int, branch_config: BranchConfig) -> int:
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

        flake_attr = f"{branch_config.flake_dir}#{branch_config.attribute}"

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
            <div style="margin-top: 20px; padding: 15px; background-color: #f8f9fa; border-radius: 4px; border: 1px solid #dee2e6;">
                <p style="margin: 0 0 10px 0; color: #495057; font-weight: 500;">💡 To get detailed stacktraces for these warnings:</p>
                <pre style="margin: 0; padding: 10px; background-color: #fff; border: 1px solid #dee2e6; border-radius: 3px; color: #212529; font-family: 'Monaco', 'Menlo', monospace; font-size: 13px;">nix-eval-jobs --workers 2 --option abort-on-warn true --force-recurse --flake {html.escape(flake_attr)}{f" --reference-lock-file {html.escape(branch_config.lock_file)}" if branch_config.lock_file != "flake.lock" else ""}</pre>
                <p style="margin: 10px 0 0 0; color: #6c757d; font-size: 12px;">This will cause the evaluation to fail at the first warning and provide a full stacktrace.</p>
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


class ScheduledEffectsEvaluateCommand(buildstep.ShellMixin, steps.BuildStep):
    """Evaluate scheduled effects from a flake and update schedulers if changed.

    This step runs on push to default branch after successful build to discover
    onSchedule definitions and trigger a reconfig if schedules have changed.
    """

    def __init__(
        self,
        project: GitProject,
        schedules_cache_file: str,
        **kwargs: Any,
    ) -> None:
        kwargs = self.setupShellMixin(kwargs)
        super().__init__(**kwargs)
        self.project = project
        self.schedules_cache_file = schedules_cache_file
        self.observer = logobserver.BufferLogObserver()
        self.addLogObserver("stdio", self.observer)

    async def run(self) -> int:
        cmd: remotecommand.RemoteCommand = await self.makeRemoteShellCommand()
        await self.runCommand(cmd)

        result = cmd.results()
        if result != util.SUCCESS:
            return result

        try:
            new_schedules = json.loads(self.observer.getStdout())
        except json.JSONDecodeError:
            log.error("Failed to parse scheduled effects output")  # noqa: TRY400
            return util.FAILURE

        cache_path = Path(self.schedules_cache_file)
        old_schedules: dict[str, Any] = {}
        if cache_path.exists():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                old_schedules = json.loads(cache_path.read_text())

        if new_schedules != old_schedules:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(new_schedules, indent=2))

            schedule_count = len(new_schedules)
            effect_count = sum(
                len(s.get("effects", [])) for s in new_schedules.values()
            )

            self.descriptionDone = [
                f"found {schedule_count} schedule(s) with {effect_count} effect(s), triggering reconfig"
            ]

            if self.master:
                # Schedule a reconfig after this build completes
                # We use callLater to avoid blocking the build
                reactor.callLater(5, self._trigger_reconfig)  # type: ignore[attr-defined]
        else:
            self.descriptionDone = ["schedules unchanged"]

        return util.SUCCESS

    def _trigger_reconfig(self) -> None:
        """Trigger a buildbot reconfig to pick up new schedules."""
        if self.master:
            log.info(
                f"Triggering reconfig due to schedule changes in {self.project.name}"
            )
            # This will cause buildbot to reload its configuration
            # The NixConfigurator will read the updated cache files
            self.master.reconfig()


def nix_eval_config(  # noqa: PLR0913
    project: GitProject,
    worker_names: list[str],
    git_url: str,
    nix_eval_config: NixEvalConfig,
    build_config: Any,  # BuildConfig from nix_build module
    schedules_cache_dir: str | None = None,
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
    # use one gcroots directory per worker. We clean up old drv paths with systemd-tmpfiles as well.
    drv_gcroots_dir = util.Interpolate(
        f"/nix/var/nix/gcroots/per-user/{nix_eval_config.gcroots_user}/%(prop:project)s/drvs/%(prop:workername)s/",
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

    # Process skipped builds (register gcroots and update outputs)
    factory.addStep(
        ProcessSkippedBuilds(
            project=project,
            gcroots_user=nix_eval_config.gcroots_user,
            branch_config=build_config.branch_config_dict,
            outputs_path=build_config.outputs_path,
            name="Process skipped builds",
            doStepIf=lambda s: (s.getProperty("event", "push") == "push")
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
            alwaysRun=True,
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
                util.Property("project"),
                "list",
            ],
            flunkOnFailure=True,
            # TODO: support other branches?
            doStepIf=lambda c: (
                c.getProperty("branch", "") == project.default_branch
                and c.build.results in (util.SUCCESS, util.WARNINGS)
            ),
            logEnviron=False,
        )
    )

    # Evaluate scheduled effects
    schedules_cache_file = f"{schedules_cache_dir}/{project.project_id}-schedules.json"
    factory.addStep(
        ScheduledEffectsEvaluateCommand(
            project=project,
            schedules_cache_file=schedules_cache_file,
            env={},
            name="Evaluate scheduled effects",
            command=[
                "buildbot-effects",
                "--rev",
                util.Property("revision"),
                "--branch",
                util.Property("branch"),
                "--repo",
                util.Property("project"),
                "list-schedules",
            ],
            flunkOnFailure=False,  # Don't fail the build if schedule eval fails
            warnOnFailure=True,
            doStepIf=lambda c: (
                c.getProperty("branch", "") == project.default_branch
                and c.build.results in (util.SUCCESS, util.WARNINGS)
            ),
            hideStepIf=lambda results, _s: results == util.SKIPPED,
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
