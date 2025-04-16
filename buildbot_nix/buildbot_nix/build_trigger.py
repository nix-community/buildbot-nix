import dataclasses
import graphlib
from collections.abc import Coroutine, Generator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from buildbot.plugins import steps, util
from buildbot.process import buildstep
from buildbot.process.properties import Properties
from buildbot.process.results import ALL_RESULTS, SUCCESS, statusToString, worst_status
from buildbot.reporters.utils import getURLForBuildrequest
from buildbot.schedulers.triggerable import Triggerable
from twisted.internet import defer
from twisted.python.failure import Failure

from .errors import BuildbotNixError
from .failed_builds import FailedBuild, FailedBuildDB
from .models import (
    CacheStatus,
    NixDerivation,
    NixEvalJob,
    NixEvalJobError,
    NixEvalJobSuccess,
)
from .nix_status_generator import CombinedBuildEvent
from .projects import GitProject

if TYPE_CHECKING:
    from buildbot.db.buildrequests import BuildRequestModel
    from buildbot.db.builds import BuildModel
    from buildbot.process.log import StreamLog


class BuildTrigger(buildstep.ShellMixin, steps.BuildStep):
    """Dynamic trigger that creates a build for every attribute."""

    project: GitProject
    successful_jobs: list[NixEvalJobSuccess]
    failed_jobs: list[NixEvalJobError]
    combine_builds: bool
    drv_info: dict[str, NixDerivation]
    builds_scheduler: str
    skipped_builds_scheduler: str
    failed_eval_scheduler: str
    cached_failure_scheduler: str
    dependency_failed_scheduler: str
    result_list: list[int]
    ended: bool
    running: bool
    wait_for_finish_deferred: defer.Deferred[tuple[list[int], int]] | None
    brids: list[int]
    consumers: dict[int, Any]
    failed_builds_db: FailedBuildDB

    @dataclass
    class ScheduledJob:
        job: NixEvalJob
        builder_ids: dict[int, int]
        results: defer.Deferred[list[int]]

        def __iter__(self) -> Generator[Any, None, None]:
            for field in dataclasses.fields(self):
                yield getattr(self, field.name)

    @dataclass
    class DoneJob:
        job: NixEvalJobSuccess
        builder_ids: dict[int, int]
        results: list[int]

        def __iter__(self) -> Generator[Any, None, None]:
            for field in dataclasses.fields(self):
                yield getattr(self, field.name)

    def __init__(
        self,
        project: GitProject,
        builds_scheduler: str,
        skipped_builds_scheduler: str,
        failed_eval_scheduler: str,
        dependency_failed_scheduler: str,
        cached_failure_scheduler: str,
        successful_jobs: list[NixEvalJobSuccess],
        failed_jobs: list[NixEvalJobError],
        combine_builds: bool,
        failed_builds_db: FailedBuildDB,
        **kwargs: Any,
    ) -> None:
        self.project = project
        self.successful_jobs = successful_jobs
        self.failed_jobs = failed_jobs
        self.combine_builds = combine_builds
        self.config = None
        self.builds_scheduler = builds_scheduler
        self.skipped_builds_scheduler = skipped_builds_scheduler
        self.failed_eval_scheduler = failed_eval_scheduler
        self.cached_failure_scheduler = cached_failure_scheduler
        self.dependency_failed_scheduler = dependency_failed_scheduler
        self.failed_builds_db = failed_builds_db
        self._result_list: list[int | None] = []
        self.ended = False
        self.running = False
        self.wait_for_finish_deferred: defer.Deferred[tuple[list[int], int]] | None = (
            None
        )
        self.brids = []
        self.consumers = {}
        self.update_result_futures: list[Coroutine[Any, Any, None]] = []
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
                "cancel",
                {"reason": "parent build was interrupted"},
                ("buildrequests", brid),
            )
        if self.running and not self.ended:
            self.ended = True
            # if we are interrupted because of a connection lost, we interrupt synchronously
            if self.build.conn is None and self.wait_for_finish_deferred is not None:
                self.wait_for_finish_deferred.cancel()

    def get_scheduler_by_name(self, name: str) -> Triggerable:
        schedulers = self.master.scheduler_manager.namedServices
        if name not in schedulers:
            message = f"unknown triggered scheduler: {name!r}"
            raise BuildbotNixError(message)
        # todo: check ITriggerableScheduler
        return schedulers[name]

    @staticmethod
    def set_common_properties(
        props: Properties,
        project: GitProject,
        source: str,
        combine_builds: bool,
        job: NixEvalJob,
    ) -> Properties:
        name = f"{project.nix_ref_type}:{project.name}#checks.{job.attr}"
        props.setProperty("virtual_builder_name", name, source)
        props.setProperty("status_name", f"nix-build {name}", source)
        props.setProperty("virtual_builder_tags", "", source)
        props.setProperty("attr", job.attr, source)
        props.setProperty("combine_builds", combine_builds, source)
        props.setProperty("default_branch", project.default_branch, source)

        if isinstance(job, NixEvalJobSuccess):
            props.setProperty("drv_path", job.drvPath, source)
            props.setProperty("system", job.system, source)
            props.setProperty("out_path", job.outputs["out"] or None, source)
            props.setProperty("cacheStatus", job.cacheStatus, source)

        return props

    def schedule_eval_failure(self, job: NixEvalJobError) -> tuple[str, Properties]:
        source = "nix-eval-nix"

        props = BuildTrigger.set_common_properties(
            Properties(), self.project, source, self.combine_builds, job
        )
        props.setProperty("error", job.error, source)

        return (self.failed_eval_scheduler, props)

    def schedule_cached_failure(
        self,
        job: NixEvalJobSuccess,
        first_failure: FailedBuild,
    ) -> tuple[str, Properties]:
        source = "nix-eval-nix"

        props = BuildTrigger.set_common_properties(
            Properties(), self.project, source, self.combine_builds, job
        )
        props.setProperty("first_failure_url", first_failure.url, source)

        return (self.cached_failure_scheduler, props)

    def schedule_dependency_failed(
        self,
        job: NixEvalJobSuccess,
        dependency: NixEvalJobSuccess,
    ) -> tuple[str, Properties]:
        source = "nix-eval-nix"

        props = BuildTrigger.set_common_properties(
            Properties(), self.project, source, self.combine_builds, job
        )
        props.setProperty("dependency.attr", dependency.attr, source)

        return (self.dependency_failed_scheduler, props)

    def schedule_success(
        self,
        build_props: Properties,
        job: NixEvalJobSuccess,
    ) -> tuple[str, Properties]:
        source = "nix-eval-nix"

        drv_path = job.drvPath
        out_path = job.outputs["out"] or None

        props = BuildTrigger.set_common_properties(
            Properties(), self.project, source, self.combine_builds, job
        )

        build_props.setProperty(f"{job.attr}-out_path", out_path, source)
        build_props.setProperty(f"{job.attr}-drv_path", drv_path, source)

        # TODO: allow to skip if the build is cached?
        if job.cacheStatus in {CacheStatus.notBuilt, CacheStatus.cached}:
            return (self.builds_scheduler, props)
        return (self.skipped_builds_scheduler, props)

    async def schedule(
        self,
        ss_for_trigger: list[dict[str, Any]],
        scheduler_name: str,
        props: Properties,
    ) -> tuple[dict[int, int], defer.Deferred[list[int]]]:
        scheduler: Triggerable = self.get_scheduler_by_name(scheduler_name)

        ids_deferred, results_deferred = scheduler.trigger(
            waited_for=True,
            sourcestamps=ss_for_trigger,
            set_props=props,
            parent_buildid=self.build.buildid,
            parent_relationship="Triggered from",
        )

        brids: dict[int, int]
        _, brids = await ids_deferred

        for brid in brids.values():
            url = getURLForBuildrequest(self.master, brid)
            await self.addURL(f"{scheduler.name} #{brid}", url)
            self.update_result_futures.append(self._add_results(brid))

        if not self.combine_builds:
            await CombinedBuildEvent.produce_event_for_build_requests_by_id(
                self.master, brids.values(), CombinedBuildEvent.STARTED_NIX_BUILD, None
            )

        return brids, results_deferred

    async def _add_results(self, brid: Any) -> None:
        async def _is_buildrequest_complete(brid: Any) -> bool:
            buildrequest: (
                BuildRequestModel | None
            ) = await self.master.db.buildrequests.getBuildRequest(brid)
            if buildrequest is None:
                message = "Failed to get build request by its ID"
                raise BuildbotNixError(message)
            return buildrequest.complete

        event = ("buildrequests", str(brid), "complete")
        await self.master.mq.waitUntilEvent(
            event, lambda: _is_buildrequest_complete(brid)
        )
        builds: list[BuildModel] = await self.master.db.builds.getBuilds(
            buildrequestid=brid
        )
        for build in builds:
            self._result_list.append(build.results)
        self.updateSummary()

    def prepare_sourcestamp_list_for_trigger(
        self,
    ) -> list[
        dict[str, Any]
    ]:  # TODO: ISourceStamp? its defined but never used anywhere an doesn't include `asDict` method
        ss_for_trigger = {}
        objs_from_build = self.build.getAllSourceStamps()
        for ss in objs_from_build:
            ss_for_trigger[ss.codebase] = ss.asDict()

        return [ss_for_trigger[k] for k in sorted(ss_for_trigger.keys())]

    @staticmethod
    def sort_jobs_by_closures(
        jobs: list[NixEvalJobSuccess], job_closures: dict[str, set[str]]
    ) -> list[NixEvalJobSuccess]:
        sorted_jobs = []
        sorter = graphlib.TopologicalSorter(job_closures)

        for item in sorter.static_order():
            i = 0
            while i < len(jobs):
                if item == jobs[i].drvPath:
                    sorted_jobs.append(jobs[i])
                    del jobs[i]
                else:
                    i += 1

        return sorted_jobs

    @staticmethod
    def get_failed_dependents(
        job: NixEvalJobSuccess,
        jobs: list[NixEvalJobSuccess],
        job_closures: dict[str, set[str]],
    ) -> list[NixEvalJobSuccess]:
        jobs = list(jobs)
        failed_checks: list[NixEvalJob] = [job]
        failed_paths: list[str] = [job.drvPath]
        removed = []
        while True:
            old_paths = list(failed_paths)
            for build in list(jobs):
                deps: set[str] = job_closures.get(build.drvPath, set())

                for path in old_paths:
                    if path in deps:
                        failed_checks.append(build)
                        failed_paths.append(build.drvPath)
                        jobs.remove(build)
                        removed.append(build)

                        break
            if old_paths == failed_paths:
                break

        return removed

    async def run(self) -> int:
        """
        This function implements a relatively simple scheduling algorithm. At the start we compute the
        interdependencies between each of the jobs we want to run and at every iteration we schedule those
        who's dependencies have completed successfully. If a job fails, we recursively fail every jobs which
        depends on it.
        We also run fake builds for failed evaluations so that they nicely show up in the UI and also Forge
        reports. The reporting is based on custom events and logic, see `BuildNixEvalStatusGenerator` for
        the receiving side.
        """
        await CombinedBuildEvent.produce_event_for_build(
            self.master, CombinedBuildEvent.STARTED_NIX_BUILD, self.build, None
        )

        done: list[BuildTrigger.DoneJob] = []
        scheduled: list[BuildTrigger.ScheduledJob] = []

        self.running = True
        build_props = self.build.getProperties()
        ss_for_trigger = self.prepare_sourcestamp_list_for_trigger()
        scheduler_log: StreamLog = await self.addLog("scheduler")

        # inject failed buildsteps for any failed eval jobs we got
        overall_result = SUCCESS if not self.failed_jobs else util.FAILURE
        # inject failed buildsteps for any failed eval jobs we got
        if self.failed_jobs:
            scheduler_log.addStdout("The following jobs failed to evaluate:\n")
            for failed_job in self.failed_jobs:
                scheduler_log.addStdout(f"\t- {failed_job.attr} failed eval\n")
                brids, results_deferred = await self.schedule(
                    ss_for_trigger,
                    *self.schedule_eval_failure(failed_job),
                )
                scheduled.append(
                    BuildTrigger.ScheduledJob(failed_job, brids, results_deferred)
                )
                self.brids.extend(brids)

        # get all job derivations
        job_set = {job.drvPath for job in self.successful_jobs}

        # restrict the set of input derivations for all jobs to those which themselves are jobs
        job_closures = {
            k.drvPath: set(k.neededSubstitutes)
            .union(set(k.neededBuilds))
            .intersection(job_set)
            .difference({k.drvPath})
            for k in self.successful_jobs
        }

        # sort them according to their dependencies
        build_schedule_order = self.sort_jobs_by_closures(
            self.successful_jobs, job_closures
        )

        while build_schedule_order or scheduled:
            scheduler_log.addStdout("Scheduling...\n")

            # check which jobs should be scheduled now
            schedule_now = []
            for build in list(build_schedule_order):
                failed_build = self.failed_builds_db.check_build(build.drvPath)
                if job_closures.get(build.drvPath):
                    pass
                elif failed_build is not None and self.build.reason != "rebuild":
                    scheduler_log.addStdout(
                        f"\t- skipping {build.attr} due to cached failure, first failed at {failed_build.time}\n"
                        f"\t  see build at {failed_build.url}\n"
                    )
                    build_schedule_order.remove(build)

                    brids, results_deferred = await self.schedule(
                        ss_for_trigger,
                        *self.schedule_cached_failure(build, failed_build),
                    )
                    scheduled.append(
                        BuildTrigger.ScheduledJob(build, brids, results_deferred)
                    )
                    self.brids.extend(brids.values())
                elif failed_build is not None and self.build.reason == "rebuild":
                    self.failed_builds_db.remove_build(build.drvPath)
                    scheduler_log.addStdout(
                        f"\t- not skipping {build.attr} with cached failure due to rebuild, first failed at {failed_build.time}\n"
                    )

                    build_schedule_order.remove(build)
                    schedule_now.append(build)
                else:
                    build_schedule_order.remove(build)
                    schedule_now.append(build)

            if not schedule_now:
                scheduler_log.addStdout("\tNo builds to schedule found.\n")

            # schedule said jobs
            for job in schedule_now:
                scheduler_log.addStdout(f"\t- {job.attr}\n")
                brids, results_deferred = await self.schedule(
                    ss_for_trigger,
                    *self.schedule_success(build_props, job),
                )

                scheduled.append(
                    BuildTrigger.ScheduledJob(job, brids, results_deferred)
                )

                self.brids.extend(brids.values())

            scheduler_log.addStdout("Waiting...\n")

            # wait for one to complete
            self.wait_for_finish_deferred = defer.DeferredList(
                [job.results for job in scheduled],
                fireOnOneCallback=True,
                fireOnOneErrback=True,
            )

            results: list[int]
            index: int
            results, index = await self.wait_for_finish_deferred  # type: ignore[assignment]

            job, brids, _ = scheduled[index]
            done.append(BuildTrigger.DoneJob(job, brids, results))
            del scheduled[index]
            result = results[0]
            scheduler_log.addStdout(
                f"Found finished build {job.attr}, result {util.Results[result].upper()}\n"
            )
            if not self.combine_builds:
                await CombinedBuildEvent.produce_event_for_build_requests_by_id(
                    self.master,
                    brids.values(),
                    CombinedBuildEvent.FINISHED_NIX_BUILD,
                    result,
                )

            # if it failed, remove all dependent jobs, schedule placeholders and add them to the list of scheduled jobs
            if isinstance(job, NixEvalJobSuccess):
                if result != SUCCESS:
                    if (
                        self.build.reason == "rebuild"
                        or not self.failed_builds_db.check_build(job.drvPath)
                    ) and result == util.FAILURE:
                        url = await self.build.getUrl()
                        self.failed_builds_db.add_build(
                            job.drvPath, datetime.now(tz=UTC), url
                        )

                    removed = self.get_failed_dependents(
                        job, build_schedule_order, job_closures
                    )
                    for removed_job in removed:
                        scheduler, props = self.schedule_dependency_failed(
                            removed_job, job
                        )
                        brids, results_deferred = await self.schedule(
                            ss_for_trigger, scheduler, props
                        )
                        build_schedule_order.remove(removed_job)
                        scheduled.append(
                            BuildTrigger.ScheduledJob(
                                removed_job, brids, results_deferred
                            )
                        )
                        self.brids.extend(brids.values())
                    scheduler_log.addStdout(
                        "\t- removed jobs: "
                        + ", ".join([job.drvPath for job in removed])
                        + "\n"
                    )

                for job_closure in job_closures.values():
                    if job.drvPath in job_closure:
                        job_closure.remove(job.drvPath)

            overall_result = worst_status(result, overall_result)
            scheduler_log.addStdout(
                f"\t- new result: {util.Results[overall_result].upper()} \n"
            )

        while self.update_result_futures:
            await self.update_result_futures.pop()

        await CombinedBuildEvent.produce_event_for_build(
            self.master,
            CombinedBuildEvent.FINISHED_NIX_BUILD,
            self.build,
            overall_result,
        )
        scheduler_log.addStdout("Done!\n")
        return overall_result

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
