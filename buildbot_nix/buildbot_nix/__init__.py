import atexit
import copy
import json
import multiprocessing
import os
import re
import urllib.parse
from collections import defaultdict
from multiprocessing import cpu_count
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
from .build_trigger import BuildTrigger
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

SKIPPED_BUILDER_NAMES = [
    f"skipped-builds-{n:03}" for n in range(int(max(4, int(cpu_count() * 0.25))))
]

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
        supported_systems: list[str],
        job_report_limit: int | None,
        failed_builds_db: FailedBuildDB,
        worker_count: int,
        max_memory_size: int,
        drv_gcroots_dir: util.Interpolate,
        show_trace: bool = False,
        **kwargs: Any,
    ) -> None:
        kwargs = self.setupShellMixin(kwargs)
        super().__init__(**kwargs)
        self.project = project
        self.observer = logobserver.BufferLogObserver()
        self.addLogObserver("stdio", self.observer)
        self.supported_systems = supported_systems
        self.job_report_limit = job_report_limit
        self.failed_builds_db = failed_builds_db
        self.worker_count = worker_count
        self.max_memory_size = max_memory_size
        self.drv_gcroots_dir = drv_gcroots_dir
        self.show_trace = show_trace

    async def produce_event(self, event: str, result: None | int) -> None:
        build: dict[str, Any] = await self.master.data.get(
            ("builds", str(self.build.buildid))
        )
        if result is not None:
            build["results"] = result
        self.master.mq.produce(
            ("builds", str(self.build.buildid), event), copy.deepcopy(build)
        )

    async def run(self) -> int:
        await self.produce_event("started-nix-eval", None)

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
                "--workers",
                str(self.worker_count),
                "--max-memory-size",
                str(self.max_memory_size),
                "--option",
                "accept-flake-config",
                "true",
                "--gc-roots-dir",
                str(self.drv_gcroots_dir),
                "--force-recurse",
                "--check-cache-status",
                *(["--show-trace"] if self.show_trace else []),
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
        await CombinedBuildEvent.produce_event_for_build(
            self.master, CombinedBuildEvent.FINISHED_NIX_EVAL, self.build, result
        )
        if result == util.SUCCESS:
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
                elif job.system in self.supported_systems and isinstance(
                    job, NixEvalJobSuccess
                ):
                    successful_jobs.append(job)

            self.number_of_jobs = len(successful_jobs)

            self.build.addStepsAfterCurrentStep(
                [
                    BuildTrigger(
                        project=self.project,
                        builds_scheduler=f"{self.project.project_id}-nix-build",
                        skipped_builds_scheduler=f"{self.project.project_id}-nix-skipped-build",
                        failed_eval_scheduler=f"{self.project.project_id}-nix-failed-eval",
                        dependency_failed_scheduler=f"{self.project.project_id}-nix-dependency-failed",
                        cached_failure_scheduler=f"{self.project.project_id}-nix-cached-failure",
                        name="build flake",
                        successful_jobs=successful_jobs,
                        failed_jobs=failed_jobs,
                        combine_builds=(
                            self.job_report_limit is not None
                            and self.number_of_jobs > self.job_report_limit
                        ),
                        failed_builds_db=self.failed_builds_db,
                    ),
                ]
            )

        return result


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
        worker_names: list[str],
        post_build_steps: list[models.PostBuildStep],
        branch_config_dict: models.BranchConfigDict,
        outputs_path: Path | None,
        show_trace: bool = False,
        **kwargs: Any,
    ) -> None:
        self.project = project
        self.worker_names = worker_names
        self.post_build_steps = post_build_steps
        self.branch_config_dict = branch_config_dict
        self.outputs_path = outputs_path
        self.show_trace = show_trace

        super().__init__(**kwargs)

    async def run(self) -> int:
        if self.build.reason != "rebuild":
            attr = self.getProperty("attr")
            # show eval error
            error_log: StreamLog = await self.addLog("nix_error")
            msg = [
                f"{attr} was failed because it has failed previously and its failure has been cached.",
            ]
            url = self.getProperty("first_failure_url")
            if url:
                msg.append(f"  failed build: {url}")
                error_log.addStderr("\n".join(msg) + "\n")
            return util.FAILURE
        self.build.addStepsAfterCurrentStep(
            nix_build_steps(
                self.project,
                self.worker_names,
                self.post_build_steps,
                self.branch_config_dict,
                self.outputs_path,
                self.show_trace,
            )
        )
        return util.SUCCESS


class NixBuildCommand(buildstep.ShellMixin, steps.BuildStep):
    """Builds a nix derivation."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs = self.setupShellMixin(kwargs)
        super().__init__(**kwargs)

    async def run(self) -> int:
        if self.build.reason == "rebuild" and not self.getProperty("combine_builds"):
            await CombinedBuildEvent.produce_event_for_build(
                self.master, CombinedBuildEvent.STARTED_NIX_BUILD, self.build, None
            )

        # run `nix build`
        cmd: remotecommand.RemoteCommand = await self.makeRemoteShellCommand()
        await self.runCommand(cmd)

        res = cmd.results()

        if self.build.reason == "rebuild" and not self.getProperty("combine_builds"):
            await CombinedBuildEvent.produce_event_for_build(
                self.master, CombinedBuildEvent.FINISHED_NIX_BUILD, self.build, res
            )

        return res


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


# GitHub somtimes fires the PR webhook before it has computed the merge commit
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
    supported_systems: list[str],
    eval_lock: MasterLock,
    worker_count: int,
    max_memory_size: int,
    job_report_limit: int | None,
    failed_builds_db: FailedBuildDB,
    show_trace: bool = False,
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
        "/nix/var/nix/gcroots/per-user/buildbot-worker/%(prop:project)s/drvs/",
    )

    factory.addStep(
        NixEvalCommand(
            project=project,
            env={"CLICOLOR_FORCE": "1"},
            name="Evaluate flake",
            supported_systems=supported_systems,
            job_report_limit=job_report_limit,
            failed_builds_db=failed_builds_db,
            haltOnFailure=True,
            locks=[eval_lock.access("exclusive")],
            worker_count=worker_count,
            max_memory_size=max_memory_size,
            drv_gcroots_dir=drv_gcroots_dir,
            logEnviron=False,
            show_trace=show_trace,
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
        properties=dict(status_name="nix-eval"),
    )


@async_to_deferred
async def do_register_gcroot_if(
    s: steps.BuildStep | Build, branch_config: models.BranchConfigDict
) -> bool:
    gc_root = await util.Interpolate(
        "/nix/var/nix/gcroots/per-user/buildbot-worker/%(prop:project)s/%(prop:attr)s"
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
    worker_names: list[str],
    post_build_steps: list[steps.BuildStep],
    branch_config: models.BranchConfigDict,
    outputs_path: Path | None = None,
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
        *post_build_steps,
        Trigger(
            name="Register gcroot",
            waitForFinish=True,
            schedulerNames=[f"{project.project_id}-nix-register-gcroot"],
            haltOnFailure=True,
            flunkOnFailure=True,
            sourceStamps=[],
            alwaysUseLatest=False,
            updateSourceStamp=False,
            doStepIf=lambda s: do_register_gcroot_if(s, branch_config),
            copy_properties=["out_path", "attr"],
            set_properties={"report_status": False},
        ),
        steps.ShellCommand(
            name="Delete temporary gcroots",
            command=["rm", "-f", util.Interpolate("result-%(prop:attr)s")],
            logEnviron=False,
        ),
    ]

    if outputs_path is not None:
        out_steps.append(
            UpdateBuildOutput(
                project=project,
                name="Update build output",
                path=outputs_path,
                branch_config=branch_config,
            ),
        )

    return out_steps


def nix_build_config(
    project: GitProject,
    worker_names: list[str],
    post_build_steps: list[steps.BuildStep],
    branch_config_dict: models.BranchConfigDict,
    outputs_path: Path | None = None,
    show_trace: bool = False,
) -> BuilderConfig:
    """Builds one nix flake attribute."""
    factory = util.BuildFactory()
    factory.addSteps(
        nix_build_steps(
            project,
            worker_names,
            post_build_steps,
            branch_config_dict,
            outputs_path,
            show_trace,
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
    worker_names: list[str],
    skipped_worker_names: list[str],
    branch_config_dict: models.BranchConfigDict,
    post_build_steps: list[steps.BuildStep],
    outputs_path: Path | None = None,
    show_trace: bool = False,
) -> BuilderConfig:
    """Dummy builder that is triggered when a build is cached as failed."""
    factory = util.BuildFactory()
    factory.addStep(
        CachedFailureStep(
            project=project,
            worker_names=worker_names,
            post_build_steps=post_build_steps,
            branch_config_dict=branch_config_dict,
            outputs_path=outputs_path,
            name="Cached failure",
            haltOnFailure=True,
            flunkOnFailure=True,
            show_trace=show_trace,
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


def nix_skipped_build_config(
    project: GitProject,
    worker_names: list[str],
    branch_config_dict: models.BranchConfigDict,
    outputs_path: Path | None = None,
) -> BuilderConfig:
    """Dummy builder that is triggered when a build is skipped."""
    factory = util.BuildFactory()

    # This is just a dummy step showing the cached build
    factory.addStep(
        steps.BuildStep(
            name="Nix build (cached)",
            doStepIf=lambda _: False,
        ),
    )

    # if the change got pulled in from a PR, the roots haven't been created yet
    factory.addStep(
        Trigger(
            name="Register gcroot",
            waitForFinish=True,
            schedulerNames=[f"{project.project_id}-nix-register-gcroot"],
            haltOnFailure=True,
            flunkOnFailure=True,
            sourceStamps=[],
            alwaysUseLatest=False,
            updateSourceStamp=False,
            doStepIf=lambda s: do_register_gcroot_if(s, branch_config_dict),
            copy_properties=["out_path", "attr"],
            set_properties={"report_status": False},
        ),
    )
    if outputs_path is not None:
        factory.addStep(
            UpdateBuildOutput(
                project=project,
                name="Update build output",
                path=outputs_path,
                branch_config=branch_config_dict,
            ),
        )
    return util.BuilderConfig(
        name=f"{project.name}/nix-skipped-build",
        project=project.name,
        workernames=worker_names,
        collapseRequests=False,
        env={},
        factory=factory,
        do_build_if=lambda build: do_register_gcroot_if(build, branch_config_dict)
        and outputs_path is not None,
    )


def nix_register_gcroot_config(
    project: GitProject,
    worker_names: list[str],
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
                    "/nix/var/nix/gcroots/per-user/buildbot-worker/%(prop:project)s/%(prop:attr)s",
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
    worker_names: list[str],
    nix_supported_systems: list[str],
    nix_eval_worker_count: int,
    nix_eval_max_memory_size: int,
    eval_lock: MasterLock,
    post_build_steps: list[steps.BuildStep],
    job_report_limit: int | None,
    failed_builds_db: FailedBuildDB,
    per_repo_effects_secrets: dict[str, str],
    branch_config_dict: models.BranchConfigDict,
    outputs_path: Path | None = None,
    show_trace: bool = False,
) -> None:
    config["projects"].append(Project(project.name))
    config["schedulers"].extend(
        [
            schedulers.SingleBranchScheduler(
                name=f"{project.project_id}-primary",
                change_filter=util.ChangeFilter(
                    repository=project.url,
                    filter_fn=lambda c: branch_config_dict.do_run(
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
            # this is triggered from `nix-eval` when the build is skipped
            schedulers.Triggerable(
                name=f"{project.project_id}-nix-skipped-build",
                builderNames=[f"{project.name}/nix-skipped-build"],
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
    effects_secrets_cred = per_repo_effects_secrets.get(key)

    config["builders"].extend(
        [
            # Since all workers run on the same machine, we only assign one of them to do the evaluation.
            # This should prevent excessive memory usage.
            nix_eval_config(
                project,
                worker_names,
                git_url=project.get_project_url(),
                supported_systems=nix_supported_systems,
                job_report_limit=job_report_limit,
                worker_count=nix_eval_worker_count,
                max_memory_size=nix_eval_max_memory_size,
                eval_lock=eval_lock,
                failed_builds_db=failed_builds_db,
                show_trace=show_trace,
            ),
            nix_build_config(
                project,
                worker_names,
                outputs_path=outputs_path,
                branch_config_dict=branch_config_dict,
                post_build_steps=post_build_steps,
                show_trace=show_trace,
            ),
            buildbot_effects_config(
                project,
                git_url=project.get_project_url(),
                worker_names=worker_names,
                secrets=effects_secrets_cred,
            ),
            nix_skipped_build_config(
                project=project,
                worker_names=SKIPPED_BUILDER_NAMES,
                branch_config_dict=branch_config_dict,
                outputs_path=outputs_path,
            ),
            nix_failed_eval_config(project, SKIPPED_BUILDER_NAMES),
            nix_dependency_failed_config(project, SKIPPED_BUILDER_NAMES),
            nix_cached_failure_config(
                project=project,
                worker_names=worker_names,
                skipped_worker_names=SKIPPED_BUILDER_NAMES,
                branch_config_dict=branch_config_dict,
                post_build_steps=post_build_steps,
                outputs_path=outputs_path,
                show_trace=show_trace,
            ),
            nix_register_gcroot_config(project, worker_names),
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
        options: dict[str, Any],
    ) -> Match | None:
        return await self.check_builder(epobject, epdict, "build")

    async def match_BuildEndpoint_rebuild(  # noqa: N802
        self, epobject: Any, epdict: dict[str, Any], options: dict[str, Any]
    ) -> Match | None:
        return await self.check_builder(epobject, epdict, "build")

    async def match_BuildEndpoint_stop(  # noqa: N802
        self,
        epobject: Any,
        epdict: dict[str, Any],
        options: dict[str, Any],
    ) -> Match | None:
        return await self.check_builder(epobject, epdict, "build")

    async def match_BuildRequestEndpoint_stop(  # noqa: N802
        self,
        epobject: Any,
        epdict: dict[str, Any],
        options: dict[str, Any],
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
            for builder in ["nix-build", "nix-skipped-build", "nix-eval"]:
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

    def configure(self, config: dict[str, Any]) -> None:
        backends: dict[str, GitBackend] = {}

        if self.config.github is not None:
            backends["github"] = GithubBackend(self.config.github, self.config.url)

        if self.config.gitea is not None:
            backends["gitea"] = GiteaBackend(self.config.gitea, self.config.url)

        if self.config.pull_based is not None:
            backends["pull_based"] = PullBasedBacked(self.config.pull_based)

        auth: AuthBase | None = None
        if self.config.auth_backend == AuthBackendConfig.httpbasicauth:
            auth = OAuth2ProxyAuth(self.config.http_basic_auth_password)
        elif self.config.auth_backend == AuthBackendConfig.none:
            pass
        elif backends[self.config.auth_backend] is not None:
            auth = backends[self.config.auth_backend].create_auth()

        projects: list[GitProject] = []

        for backend in backends.values():
            projects += backend.load_projects()

        config.setdefault("projects", [])
        config.setdefault("secretsProviders", [])
        config.setdefault("www", {})
        config.setdefault("workers", [])
        config.setdefault("services", [])
        config.setdefault("schedulers", [])
        config.setdefault("builders", [])

        worker_names = []
        for w in self.config.nix_worker_secrets().workers:
            for i in range(w.cores):
                worker_name = f"{w.name}-{i:03}"
                config["workers"].append(worker.Worker(worker_name, w.password))
                worker_names.append(worker_name)

        for i in range(self.config.local_workers):
            worker_name = f"local-{i}"
            config["workers"].append(worker.LocalWorker(worker_name))
            worker_names.append(worker_name)

        if worker_names == []:
            msg = f"No workers configured in {self.config.nix_workers_secret_file} and {self.config.local_workers} local workers."
            raise BuildbotNixError(msg)

        eval_lock = util.MasterLock("nix-eval")

        global DB  # noqa: PLW0603
        if DB is None:
            DB = FailedBuildDB(Path("failed_builds.dbm"))
            # Hacky but we have no other hooks just now to run code on shutdown.
            atexit.register(lambda: DB.close() if DB is not None else None)

        succeeded_projects = []
        for project in projects:
            try:
                config_for_project(
                    config=config,
                    project=project,
                    worker_names=worker_names,
                    nix_supported_systems=self.config.build_systems,
                    nix_eval_worker_count=self.config.eval_worker_count
                    or multiprocessing.cpu_count(),
                    nix_eval_max_memory_size=self.config.eval_max_memory_size,
                    eval_lock=eval_lock,
                    post_build_steps=[
                        x.to_buildstep() for x in self.config.post_build_steps
                    ],
                    job_report_limit=self.config.job_report_limit,
                    per_repo_effects_secrets=self.config.effects_per_repo_secrets,
                    failed_builds_db=DB,
                    branch_config_dict=self.config.branches,
                    outputs_path=self.config.outputs_path,
                    show_trace=self.config.show_trace_on_failure,
                )
            except Exception:  # noqa: BLE001
                log.failure(f"Failed to configure project {project.name}")
            else:
                succeeded_projects.append(project)

        config["workers"].extend(worker.LocalWorker(w) for w in SKIPPED_BUILDER_NAMES)

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
                        # project list twice a day and on startup
                        PeriodicWithStartup(
                            name=f"reload-{backend.type}-projects-bidaily",
                            builderNames=[reload_builder.name],
                            periodicBuildTimer=12 * 60 * 60,
                            run_on_startup=not backend.are_projects_cached(),
                        ),
                    ],
                )
            config["services"].append(backend.create_reporter())
            config.setdefault("secretsProviders", [])
            config["secretsProviders"].extend(backend.create_secret_providers())

        credentials_directory = os.environ.get("CREDENTIALS_DIRECTORY", "./secrets")
        Path(credentials_directory).mkdir(exist_ok=True, parents=True)
        systemd_secrets = SecretInAFile(dirname=credentials_directory)
        config["secretsProviders"].append(systemd_secrets)

        config["www"].setdefault("plugins", {})

        config["www"].setdefault("change_hook_dialects", {})
        for backend in backends.values():
            config["www"]["change_hook_dialects"][backend.change_hook_name] = (
                backend.create_change_hook()
            )

        config.setdefault("change_source", [])
        for project in succeeded_projects:
            change_source = project.create_change_source()
            if change_source is not None:
                config["change_source"].append(change_source)

        config["www"].setdefault("avatar_methods", [])

        for backend in backends.values():
            avatar_method = backend.create_avatar_method()
            if avatar_method is not None:
                config["www"]["avatar_methods"].append(avatar_method)

        if "auth" not in config["www"]:
            # TODO one cannot have multiple auth backends...
            if auth is not None:
                config["www"]["auth"] = auth

            config["www"]["authz"] = setup_authz(
                admins=self.config.admins,
                backends=list(backends.values()),
                projects=succeeded_projects,
            )
