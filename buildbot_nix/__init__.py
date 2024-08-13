import json
import multiprocessing
import os
import re
import uuid
import sys
import graphlib
from collections import defaultdict
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast, Tuple

from buildbot.config.builder import BuilderConfig
from buildbot.configurators import ConfiguratorBase
from buildbot.interfaces import WorkerSetupError
from buildbot.locks import MasterLock
from buildbot.plugins import schedulers, steps, util, worker
from buildbot.process import buildstep, logobserver, remotecommand
from buildbot.process.build import Build
from buildbot.process.project import Project
from buildbot.process.properties import Properties
from buildbot.process.results import ALL_RESULTS, statusToString
from buildbot.secrets.providers.file import SecretInAFile
from buildbot.schedulers.base import BaseScheduler
from buildbot.steps.trigger import Trigger
from buildbot.www.authz import Authz
from buildbot.www.authz.endpointmatchers import EndpointMatcherBase, Match
from buildbot.www.oauth2 import OAuth2Auth
from buildbot.changes.gerritchangesource import GerritChangeSource
from buildbot.reporters.utils import getURLForBuild
from buildbot.reporters.utils import getURLForBuildrequest
from buildbot.process.buildstep import CANCELLED
from buildbot.process.buildstep import EXCEPTION
from buildbot.process.buildstep import SUCCESS
from buildbot.process.results import worst_status

# from buildbot.db.buildrequests import BuildRequestModel
# from buildbot.db.builds import BuildModel
if TYPE_CHECKING:
    from buildbot.process.log import StreamLog
    from buildbot.process.log import Log
    from buildbot.www.auth import AuthBase

from twisted.internet import defer
from twisted.logger import Logger
from twisted.python.failure import Failure

from . import models
from .common import (
    slugify_project_name,
)
from .gitea_projects import GiteaBackend
from .github_projects import (
    GithubBackend,
)
from .models import AuthBackendConfig, BuildbotNixConfig, NixEvalJob, NixEvalJobSuccess, NixEvalJobError, NixEvalJobModel
from .oauth2_proxy_auth import OAuth2ProxyAuth
from .projects import GitBackend, GitProject

SKIPPED_BUILDER_NAME = "skipped-builds"

log = Logger()


class BuildbotNixError(Exception):
    pass


class BuildTrigger(steps.BuildStep):
    """Dynamic trigger that creates a build for every attribute."""

    project: GitProject

    def __init__(
        self,
        project: GitProject,
        builds_scheduler: str,
        skipped_builds_scheduler: str,
        jobs: list[NixEvalJobSuccess],
        report_status: bool,
        drv_info: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        self.project = project
        self.jobs = jobs
        self.report_status = report_status
        self.drv_info = drv_info
        self.config = None
        self.builds_scheduler = builds_scheduler
        self._result_list: list[int] = []
        self.ended = False
        self.waitForFinishDeferred: defer.Deferred[tuple[list[int], int]] | None = None
        super().__init__(**kwargs)

    def interrupt(self, reason: str | Failure) -> None:
        # We cancel the buildrequests, as the data api handles
        # both cases:
        # - build started: stop is sent,
        # - build not created yet: related buildrequests are set to CANCELLED.
        # Note that there is an identified race condition though (more details
        # are available at buildbot.data.buildrequests).
        for brid in self.brids:
            self.master.data.control(
                "cancel", {'reason': 'parent build was interrupted'}, ("buildrequests", brid)
            )
        if self.running and not self.ended:
            self.ended = True
            # if we are interrupted because of a connection lost, we interrupt synchronously
            if self.build.conn is None and self.waitForFinishDeferred is not None:
                self.waitForFinishDeferred.cancel()

    def getSchedulerByName(self, name: str) -> BaseScheduler:
        schedulers = self.master.scheduler_manager.namedServices
        if name not in schedulers:
            raise ValueError(f"unknown triggered scheduler: {repr(name)}")
        sch = schedulers[name]
        # todo: check ITriggerableScheduler
        return sch

    def schedule_one(self, build_props: Properties, job: NixEvalJobSuccess) -> Tuple[BaseScheduler, Properties]:
        source = f"nix-eval-lix"
        attr = job.attr or "eval-error"
        name = attr
        name = f"hydraJobs.{name}"
        # error = job.error
        props = Properties()
        props.setProperty("virtual_builder_name", name, source)
        props.setProperty("status_name", f"nix-build .#hydraJobs.{attr}", source)
        props.setProperty("virtual_builder_tags", "", source)

        # if error is not None:
        #     props.setProperty("error", error, source)
        #     return (self.builds_scheduler, props)

        drv_path = job.drvPath
        system = job.system
        out_path = job.outputs["out"] or None

        build_props.setProperty(f"{attr}-out_path", out_path, source)
        build_props.setProperty(f"{attr}-drv_path", drv_path, source)

        props.setProperty("attr", attr, source)
        props.setProperty("system", system, source)
        props.setProperty("drv_path", drv_path, source)
        props.setProperty("out_path", out_path, source)
        props.setProperty("cacheStatus", job.cacheStatus, source)

        return (self.builds_scheduler, props)

    @defer.inlineCallbacks
    def _add_results(self, brid: Any) -> Generator[Any, Any, None]:
        @defer.inlineCallbacks
        def _is_buildrequest_complete(brid: Any) -> Generator[Any, Any, bool]:
            buildrequest: Any = yield self.master.db.buildrequests.getBuildRequest(brid) # TODO: once we bump to 4.1.x use BuildRequestModel | None
            if buildrequest is None:
                raise RuntimeError(f"Failed to get build request by its ID")
            return buildrequest['complete']

        event = ('buildrequests', str(brid), 'complete')
        yield self.master.mq.waitUntilEvent(event, lambda: _is_buildrequest_complete(brid))
        builds: Any = yield self.master.db.builds.getBuilds(buildrequestid=brid) # TODO: once we bump to 4.1.x use list[BuildModel]
        for build in builds:
            self._result_list.append(build["results"])
        self.updateSummary()


    def prepareSourcestampListForTrigger(self) -> list[dict[str, Any]]: # TODO: ISourceStamp? its defined but never used anywhere an doesn't include `asDict` method
        ss_for_trigger = {}
        objs_from_build = self.build.getAllSourceStamps()
        for ss in objs_from_build:
            ss_for_trigger[ss.codebase] = ss.asDict()

        trigger_values = [ss_for_trigger[k] for k in sorted(ss_for_trigger.keys())]
        return trigger_values

    @defer.inlineCallbacks
    def run(self) -> Generator[Any, Any, None]:
        build_props = self.build.getProperties()
        repo_name = self.project.name
        project_id = slugify_project_name(repo_name)
        source = f"nix-eval-{project_id}"

        all_deps: dict[str, set[str]] = dict()
        for drv, info in self.drv_info.items():
            all_deps[drv] = set(info.get("inputDrvs").keys())
        def closure_of(key: str, deps: dict[str, set[str]]) -> set[str]:
            r, size = set([key]), 0
            while len(r) != size:
                size = len(r)
                r.update(*[ deps[k] for k in r ])
            return r.difference([key])
        # essentially filters out jobs
        job_set = set(( drv for drv in ( job.drvPath for job in self.jobs ) if drv ))

        all_deps = { k: closure_of(k, all_deps).intersection(job_set) for k in job_set }
        builds_to_schedule = list(self.jobs)
        build_schedule_order = []
        sorter = graphlib.TopologicalSorter(all_deps)
        for item in sorter.static_order():
            i = 0
            while i < len(builds_to_schedule):
                if item == builds_to_schedule[i].drvPath:
                    build_schedule_order.append(builds_to_schedule[i])
                    del builds_to_schedule[i]
                else:
                    i += 1

        done = []
        scheduled: list[Tuple[NixEvalJobSuccess, dict[int, int], defer.Deferred[list[int]]]] = []
        all_results = SUCCESS
        ss_for_trigger = self.prepareSourcestampListForTrigger()
        while len(build_schedule_order) > 0 or len(scheduled) > 0:
            print('Scheduling..')
            schedule_now = []
            for build in list(build_schedule_order):
                drv_path = build.drvPath
                if drv_path is None:
                    log.warn("skipping build due to missing drvPath: {build}", build=build)
                    continue
                if not all_deps.get(drv_path, []):
                    build_schedule_order.remove(build)
                    schedule_now.append(build)
            if len(schedule_now) == 0:
                print('    No builds to schedule found.')
            for job in schedule_now:
                print(f"    - {job.attr}")
                (scheduler, props) = self.schedule_one(build_props, job)
                scheduler = self.getSchedulerByName(scheduler)

                idsDeferred, resultsDeferred = scheduler.trigger(
                    waited_for = True,
                    sourcestamps = ss_for_trigger,
                    set_props = props,
                    parent_buildid = self.build.buildid,
                    parent_relationship = "Triggered from",
                )

                brids: dict[int, int]
                try:
                    _, brids = yield idsDeferred
                except Exception as e:
                    yield self.addLogWithException(e)
                    # results = EXCEPTION
                scheduled.append((job, brids, resultsDeferred))


                for brid in brids.values():
                    url = getURLForBuildrequest(self.master, brid)
                    yield self.addURL(f"{scheduler.name} #{brid}", url)
                    self._add_results(brid)
            print('Waiting..')
            self.waitForFinishDeferred = defer.DeferredList([results for _, _, results in scheduled], fireOnOneCallback = True, fireOnOneErrback=True)
            results: list[int]
            index: int
            results, index = yield self.waitForFinishDeferred
            job, brids, _ = scheduled[index]
            done.append((job, brids, results))
            del scheduled[index]
            result = results[0]
            print(f'    Found finished build {job.attr}, result {util.Results[result].upper()}')
            if result != SUCCESS:
                failed_checks: list[NixEvalJob] = []
                failed_paths: list[str] = []
                removed = []
                while True:
                    old_paths = list(failed_paths)
                    print(failed_checks, old_paths)
                    for build in list(build_schedule_order):
                        deps: set[str] = all_deps.get(build.drvPath, set())
                        for path in old_paths:
                            if path in deps:
                                failed_checks.append(build)
                                failed_paths.append(build.drvPath)
                                build_schedule_order.remove(build)
                                removed.append(build.attr)

                                break
                    if old_paths == failed_paths:
                        break
                print('    Removed jobs: ' + ', '.join([ path or "" for path in removed ]))
            all_results = worst_status(result, all_results)
            print(f'    New result: {util.Results[all_results].upper()}')
            for dep in all_deps:
                if job.drvPath in all_deps[dep]:
                    all_deps[dep].remove(job.drvPath)
        print('Done!')
        return all_results

    def getCurrentSummary(self) -> dict[str, str]:  # noqa: N802
        summary = []
        if self._result_list:
            for status in ALL_RESULTS:
                count = self._result_list.count(status)
                if count:
                    summary.append(
                        f"{self._result_list.count(status)} {statusToString(status, count)}",
                    )
        return {"step": f"({', '.join(summary)})"}


class NixBuildCombined(steps.BuildStep):
    """Shows the error message of a failed evaluation."""

    name = "nix-build-combined"

    def run(self) -> Generator[Any, object, Any]:
        return self.build.results



class NixEvalCommand(buildstep.ShellMixin, steps.BuildStep):
    """Parses the output of `nix-eval-jobs` and triggers a `nix-build` build for
    every attribute.
    """

    project: GitProject

    def __init__(
        self,
        project: GitProject,
        supported_systems: list[str],
        job_report_limit: int | None,
        **kwargs: Any,
    ) -> None:
        kwargs = self.setupShellMixin(kwargs)
        super().__init__(**kwargs)
        self.project = project
        self.observer = logobserver.BufferLogObserver()
        self.addLogObserver("stdio", self.observer)
        self.supported_systems = supported_systems
        self.job_report_limit = job_report_limit

    @defer.inlineCallbacks
    def run(self) -> Generator[Any, object, Any]:
        # run nix-eval-jobs --flake .#checks to generate the dict of stages
        cmd: remotecommand.RemoteCommand = yield self.makeRemoteShellCommand()
        yield self.runCommand(cmd)

        # if the command passes extract the list of stages
        result = cmd.results()
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

            repo_name = self.project.name
            project_id = slugify_project_name(repo_name)

            failed_jobs: list[NixEvalJobError] = []
            successful_jobs: list[NixEvalJobSuccess] = []

            log.info("jobs: {jobs}", jobs = jobs)

            for job in jobs:
                if isinstance(job, NixEvalJobError):
                    failed_jobs.append(job)
                    continue
                elif not job.system or job.system in self.supported_systems:  # report unbuildable jobs
                    successful_jobs.append(job)

            self.number_of_jobs = len(successful_jobs)

            drv_show_log: Log = yield self.getLog("stdio")
            drv_show_log.addStdout(f"getting derivation infos\n")
            cmd = yield self.makeRemoteShellCommand(
                stdioLogName=None,
                collectStdout=True,
                command=(
                    ["nix", "derivation", "show", "--recursive"]
                    # essentially filters out jobs
                    + [ job.drvPath for job in successful_jobs ]
                ),
            )
            yield self.runCommand(cmd)
            drv_show_log.addStdout(f"done\n")
            try:
                drv_info = json.loads(cmd.stdout)
            except json.JSONDecodeError as e:
                msg = f"Failed to parse `nix derivation show` output for {cmd.command}"
                raise BuildbotNixError(msg) from e

            self.build.addStepsAfterCurrentStep(
                [
                    BuildTrigger(
                        self.project,
                        builds_scheduler=f"{project_id}-nix-build",
                        skipped_builds_scheduler=f"{project_id}-nix-skipped-build",
                        name="build flake",
                        jobs=successful_jobs,
                        report_status=(
                            self.job_report_limit is None
                            or self.number_of_jobs <= self.job_report_limit
                        ),
                        drv_info=drv_info,
                    ),
                ]
                + (
                    [
                        Trigger(
                            waitForFinish=True,
                            schedulerNames=[f"{project_id}-nix-build-combined"],
                            haltOnFailure=True,
                            flunkOnFailure=True,
                            sourceStamps=[],
                            alwaysUseLatest=False,
                            updateSourceStamp=False,
                        ),
                    ]
                    if self.job_report_limit is not None
                    and self.number_of_jobs > self.job_report_limit
                    else []
                ),
            )

        return result


# FIXME this leaks memory... but probably not enough that we care
class RetryCounter:
    def __init__(self, retries: int) -> None:
        self.builds: dict[uuid.UUID, int] = defaultdict(lambda: retries)

    def retry_build(self, build_id: uuid.UUID) -> int:
        retries = self.builds[build_id]
        if retries > 1:
            self.builds[build_id] = retries - 1
            return retries
        return 0


# For now we limit this to two. Often this allows us to make the error log
# shorter because we won't see the logs for all previous succeeded builds
RETRY_COUNTER = RetryCounter(retries=2)


class EvalErrorStep(steps.BuildStep):
    """Shows the error message of a failed evaluation."""

    @defer.inlineCallbacks
    def run(self) -> Generator[Any, object, Any]:
        error = self.getProperty("error")
        attr = self.getProperty("attr")
        # show eval error
        error_log: StreamLog = yield self.addLog("nix_error")
        error_log.addStderr(f"{attr} failed to evaluate:\n{error}")
        return util.FAILURE


class NixBuildCommand(buildstep.ShellMixin, steps.BuildStep):
    """Builds a nix derivation."""

    def __init__(self, retries: int, **kwargs: Any) -> None:
        kwargs = self.setupShellMixin(kwargs)
        self.retries = retries
        super().__init__(**kwargs)

    @defer.inlineCallbacks
    def run(self) -> Generator[Any, object, Any]:
        # run `nix build`
        cmd: remotecommand.RemoteCommand = yield self.makeRemoteShellCommand()
        yield self.runCommand(cmd)

        res = cmd.results()
        if res == util.FAILURE and self.retries > 0:
            retries = RETRY_COUNTER.retry_build(self.getProperty("build_uuid"))
            if retries > self.retries - 1:
                return util.RETRY
        return res


class UpdateBuildOutput(steps.BuildStep):
    """Updates store paths in a public www directory.
    This is useful to prefetch updates without having to evaluate
    on the target machine.
    """

    project: GitProject

    def __init__(self, project: GitProject, path: Path, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.project = project
        self.path = path

    def run(self) -> Generator[Any, object, Any]:
        props = self.build.getProperties()
        if props.getProperty("branch") != self.project.default_branch:
            return util.SKIPPED

        attr = Path(props.getProperty("attr")).name
        out_path = props.getProperty("out_path")
        # XXX don't hardcode this
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / attr).write_text(out_path)
        return util.SUCCESS


# GitHub somtimes fires the PR webhook before it has computed the merge commit
# This is a workaround to fetch the merge commit and checkout the PR branch in CI
class GitLocalPrMerge(steps.Git):
    @defer.inlineCallbacks
    def run_vc(
        self,
        branch: str,
        revision: str,
        patch: str,
    ) -> Generator[Any, object, Any]:
        build_props = self.build.getProperties()
        # TODO: abstract this into an interface as well
        merge_base = build_props.getProperty(
            "github.base.sha"
        ) or build_props.getProperty("base_sha")
        pr_head = build_props.getProperty("github.head.sha") or build_props.getProperty(
            "head_sha"
        )

        # Not a PR, fallback to default behavior
        if merge_base is None or pr_head is None:
            res = yield super().run_vc(branch, revision, patch)
            return res

        # The code below is a modified version of Git.run_vc
        self.stdio_log: StreamLog = yield self.addLogForRemoteCommands("stdio")
        self.stdio_log.addStdout(f"Merging {merge_base} into {pr_head}\n")

        git_installed = yield self.checkFeatureSupport()

        if not git_installed:
            msg = "git is not installed on worker"
            raise WorkerSetupError(msg)

        has_git = yield self.pathExists(
            self.build.path_module.join(self.workdir, ".git")
        )

        if not has_git:
            yield self._dovccmd(["clone", "--recurse-submodules", self.repourl, "."])

        patched = yield self.sourcedirIsPatched()

        if patched:
            yield self._dovccmd(["clean", "-f", "-f", "-d", "-x"])

        yield self._dovccmd(["fetch", "-f", "-t", self.repourl, merge_base, pr_head])

        yield self._dovccmd(["checkout", "--detach", "-f", pr_head])

        yield self._dovccmd(
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
        res = yield self.parseCommitDescription()
        return res


def nix_eval_config(
    project: GitProject,
    worker_names: list[str],
    git_url: str,
    supported_systems: list[str],
    eval_lock: MasterLock,
    worker_count: int,
    max_memory_size: int,
    job_report_limit: int | None,
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
        ),
    )
    drv_gcroots_dir = util.Interpolate(
        "/nix/var/nix/gcroots/per-user/buildbot-worker/%(prop:project)s/drvs/",
    )

    factory.addStep(
        NixEvalCommand(
            project=project,
            env={},
            name="evaluate flake",
            supported_systems=supported_systems,
            job_report_limit=job_report_limit,
            command=[
                "nix-eval-jobs",
                "--workers",
                str(worker_count),
                "--max-memory-size",
                str(max_memory_size),
                "--option",
                "accept-flake-config",
                "true",
                "--gc-roots-dir",
                drv_gcroots_dir,
                "--force-recurse",
                "--check-cache-status",
                "--flake",
                ".#checks",
            ],
            haltOnFailure=True,
            locks=[eval_lock.access("exclusive")],
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
        ),
    )

    return util.BuilderConfig(
        name=f"{project.name}/nix-eval",
        workernames=worker_names,
        project=project.name,
        factory=factory,
        properties=dict(status_name="nix-eval"),
    )


@defer.inlineCallbacks
def do_register_gcroot_if(s: steps.BuildStep) -> Generator[Any, object, Any]:
    gc_root = yield util.Interpolate(
        "/nix/var/nix/gcroots/per-user/buildbot-worker/%(prop:project)s/%(prop:attr)s"
    ).getRenderingFor(s.getProperties())
    out_path = yield util.Property("out_path").getRenderingFor(s.getProperties())
    default_branch = yield util.Property("default_branch").getRenderingFor(
        s.getProperties()
    )

    return s.getProperty("branch") == str(default_branch) and not (
        Path(str(gc_root)).exists() and Path(str(gc_root)).readlink() == str(out_path)
    )


def nix_build_config(
    project: GitProject,
    worker_names: list[str],
    post_build_steps: list[steps.BuildStep],
    outputs_path: Path | None = None,
    retries: int = 1,
) -> BuilderConfig:
    """Builds one nix flake attribute."""
    factory = util.BuildFactory()
    factory.addStep(
        NixBuildCommand(
            env={},
            name="Build flake attr",
            command=[
                "nix",
                "build",
                "-L",
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
            retries=retries,
            haltOnFailure=True,
        ),
    )
    factory.addSteps(post_build_steps)

    factory.addStep(
        Trigger(
            name="Register gcroot",
            waitForFinish=True,
            schedulerNames=[
                f"{slugify_project_name(project.name)}-nix-register-gcroot"
            ],
            haltOnFailure=True,
            flunkOnFailure=True,
            sourceStamps=[],
            alwaysUseLatest=False,
            updateSourceStamp=False,
            doStepIf=do_register_gcroot_if,
            copy_properties=["out_path", "attr"],
            set_properties={"report_status": False},
        ),
    )
    factory.addStep(
        steps.ShellCommand(
            name="Delete temporary gcroots",
            command=["rm", "-f", util.Interpolate("result-%(prop:attr)s")],
        ),
    )
    if outputs_path is not None:
        factory.addStep(
            UpdateBuildOutput(
                project=project,
                name="Update build output",
                path=outputs_path,
            ),
        )
    return util.BuilderConfig(
        name=f"{project.name}/nix-build",
        project=project.name,
        workernames=worker_names,
        collapseRequests=False,
        env={},
        factory=factory,
    )


def nix_skipped_build_config(
    project: GitProject,
    worker_names: list[str],
) -> BuilderConfig:
    """Dummy builder that is triggered when a build is skipped."""
    factory = util.BuildFactory()
    factory.addStep(
        EvalErrorStep(
            name="Nix evaluation",
            doStepIf=lambda s: s.getProperty("error"),
            hideStepIf=lambda _, s: not s.getProperty("error"),
        ),
    )

    # This is just a dummy step showing the cached build
    factory.addStep(
        steps.BuildStep(
            name="Nix build (cached)",
            doStepIf=lambda _: False,
            hideStepIf=lambda _, s: s.getProperty("error"),
        ),
    )

    # if the change got pulled in from a PR, the roots haven't been created yet
    factory.addStep(
        Trigger(
            name="Register gcroot",
            waitForFinish=True,
            schedulerNames=[
                f"{slugify_project_name(project.name)}-nix-register-gcroot"
            ],
            haltOnFailure=True,
            flunkOnFailure=True,
            sourceStamps=[],
            alwaysUseLatest=False,
            updateSourceStamp=False,
            doStepIf=do_register_gcroot_if,
            copy_properties=["out_path", "attr"],
            set_properties={"report_status": False},
        ),
    )
    return util.BuilderConfig(
        name=f"{project.name}/nix-skipped-build",
        project=project.name,
        workernames=worker_names,
        collapseRequests=False,
        env={},
        factory=factory,
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


def nix_build_combined_config(
    project: GitProject,
    worker_names: list[str],
) -> BuilderConfig:
    factory = util.BuildFactory()
    factory.addStep(NixBuildCombined())

    return util.BuilderConfig(
        name=f"{project.name}/nix-build-combined",
        project=project.name,
        workernames=worker_names,
        collapseRequests=False,
        env={},
        factory=factory,
        properties=dict(status_name="nix-build-combined"),
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
    outputs_path: Path | None = None,
    build_retries: int = 1,
) -> None:
    config["projects"].append(Project(project.name))
    config["schedulers"].extend(
        [
            schedulers.SingleBranchScheduler(
                name=f"{project.project_id}-default-branch",
                change_filter=util.ChangeFilter(
                    repository=project.url,
                    filter_fn=lambda c: c.branch == project.default_branch,
                ),
                builderNames=[f"{project.name}/nix-eval"],
                treeStableTimer=5,
            ),
            # this is compatible with bors or github's merge queue
            schedulers.SingleBranchScheduler(
                name=f"{project.project_id}-merge-queue",
                change_filter=util.ChangeFilter(
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
            # this is triggered from `nix-eval`
            schedulers.Triggerable(
                name=f"{project.project_id}-nix-build",
                builderNames=[f"{project.name}/nix-build"],
            ),
            # this is triggered from `nix-eval` when the build is skipped
            schedulers.Triggerable(
                name=f"{project.project_id}-nix-skipped-build",
                builderNames=[f"{project.name}/nix-skipped-build"],
            ),
            # this is triggered from `nix-eval` when the build contains too many outputs
            schedulers.Triggerable(
                name=f"{project.project_id}-nix-build-combined",
                builderNames=[f"{project.name}/nix-build-combined"],
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
    config["builders"].extend(
        [
            # Since all workers run on the same machine, we only assign one of them to do the evaluation.
            # This should prevent exessive memory usage.
            nix_eval_config(
                project,
                worker_names,
                git_url=project.get_project_url(),
                supported_systems=nix_supported_systems,
                job_report_limit=job_report_limit,
                worker_count=nix_eval_worker_count,
                max_memory_size=nix_eval_max_memory_size,
                eval_lock=eval_lock,
            ),
            nix_build_config(
                project,
                worker_names,
                outputs_path=outputs_path,
                retries=build_retries,
                post_build_steps=post_build_steps,
            ),
            nix_skipped_build_config(project, [SKIPPED_BUILDER_NAME]),
            nix_register_gcroot_config(project, worker_names),
            nix_build_combined_config(project, worker_names),
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

    @defer.inlineCallbacks
    def check_builder(
        self,
        endpoint_object: Any,
        endpoint_dict: dict[str, Any],
        object_type: str,
    ) -> Generator[defer.Deferred[Match], Any, Any]:
        res = yield endpoint_object.get({}, endpoint_dict)
        if res is None:
            return None

        builderid = res.get("builderid")
        if builderid is None:
            builder_name = res["builder_names"][0]
        else:
            builder = yield self.master.data.get(("builders", builderid))
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
        else:
            log.warn(
                "Builder {builder} not allowed by {role}: {builders}",
                builder=builder_name,
                role=self.role,
                builders=self.builders,
            )

    def match_ForceSchedulerEndpoint_force(  # noqa: N802
        self,
        epobject: Any,
        epdict: dict[str, Any],
        options: dict[str, Any],
    ) -> defer.Deferred[Match]:
        return self.check_builder(epobject, epdict, "build")

    def match_BuildEndpoint_rebuild(  # noqa: N802
        self,
        epobject: Any,
        epdict: dict[str, Any],
        options: dict[str, Any],
    ) -> defer.Deferred[Match]:
        return self.check_builder(epobject, epdict, "build")

    def match_BuildEndpoint_stop(  # noqa: N802
        self,
        epobject: Any,
        epdict: dict[str, Any],
        options: dict[str, Any],
    ) -> defer.Deferred[Match]:
        return self.check_builder(epobject, epdict, "build")

    def match_BuildRequestEndpoint_stop(  # noqa: N802
        self,
        epobject: Any,
        epdict: dict[str, Any],
        options: dict[str, Any],
    ) -> defer.Deferred[Match]:
        return self.check_builder(epobject, epdict, "buildrequest")


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

    @defer.inlineCallbacks
    def activate(self) -> Generator[Any, object, Any]:
        if self.run_on_startup:
            yield self.setState("last_build", None)
        yield super().activate()


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

        worker_config = json.loads(self.config.nix_workers_secret)
        worker_names = []

        config.setdefault("projects", [])
        config.setdefault("secretsProviders", [])
        config.setdefault("www", {})

        for item in worker_config:
            cores = item.get("cores", 0)
            for i in range(cores):
                worker_name = f"{item['name']}-{i:03}"
                config["workers"].append(worker.Worker(worker_name, item["pass"]))
                worker_names.append(worker_name)

        eval_lock = util.MasterLock("nix-eval")

        if self.config.cachix is not None:
            self.config.post_build_steps.append(
                models.PostBuildStep(
                    name="Upload cachix",
                    environment=self.config.cachix.environment,
                    command=[
                        "cachix",
                        "push",
                        self.config.cachix.name,
                        models.Interpolate("result-%(prop:attr)s"),
                    ],
                )
            )

        for project in projects:
            config_for_project(
                config,
                project,
                worker_names,
                self.config.build_systems,
                self.config.eval_worker_count or multiprocessing.cpu_count(),
                self.config.eval_max_memory_size,
                eval_lock,
                [x.to_buildstep() for x in self.config.post_build_steps],
                self.config.job_report_limit,
                self.config.outputs_path,
                self.config.build_retries,
            )

        config["workers"].append(worker.LocalWorker(SKIPPED_BUILDER_NAME))

        for backend in backends.values():
            # Reload backend projects
            config["builders"].append(backend.create_reload_builder([worker_names[0]]))
            config["schedulers"].extend(
                [
                    schedulers.ForceScheduler(
                        name=f"reload-{backend.type}-projects",
                        builderNames=[backend.reload_builder_name],
                        buttonName="Update projects",
                    ),
                    # project list twice a day and on startup
                    PeriodicWithStartup(
                        name=f"reload-{backend.type}-projects-bidaily",
                        builderNames=[backend.reload_builder_name],
                        periodicBuildTimer=12 * 60 * 60,
                        run_on_startup=not backend.are_projects_cached(),
                    ),
                ],
            )
            config["services"].append(backend.create_reporter())
            config.setdefault("secretsProviders", [])
            config["secretsProviders"].extend(backend.create_secret_providers())

        systemd_secrets = SecretInAFile(
            dirname=os.environ["CREDENTIALS_DIRECTORY"],
        )
        config["secretsProviders"].append(systemd_secrets)

        config["www"].setdefault("plugins", {})

        config["www"].setdefault("change_hook_dialects", {})
        for backend in backends.values():
            config["www"]["change_hook_dialects"][backend.change_hook_name] = (
                backend.create_change_hook()
            )

        config["www"].setdefault("avatar_methods", [])

        for backend in backends.values():
            avatar_method = backend.create_avatar_method()
            print(avatar_method)
            if avatar_method is not None:
                config["www"]["avatar_methods"].append(avatar_method)

        if "auth" not in config["www"]:
            # TODO one cannot have multiple auth backends...
            if auth is not None:
                config["www"]["auth"] = auth

            config["www"]["authz"] = setup_authz(
                admins=self.config.admins,
                backends=list(backends.values()),
                projects=projects,
            )
