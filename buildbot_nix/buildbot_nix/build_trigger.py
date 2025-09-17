
import graphlib
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from buildbot.plugins import steps, util
from buildbot.process import buildstep
from buildbot.process.log import StreamLog
from buildbot.process.properties import Properties
from buildbot.process.results import ALL_RESULTS, SUCCESS, statusToString, worst_status
from buildbot.reporters.utils import getURLForBuild, getURLForBuildrequest
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


@dataclass
class TriggerConfig:
    """Configuration for build trigger schedulers."""

    builds_scheduler: str
    failed_eval_scheduler: str
    dependency_failed_scheduler: str
    cached_failure_scheduler: str


@dataclass
class JobsConfig:
    """Configuration for job results and build settings."""

    successful_jobs: list[NixEvalJobSuccess]
    failed_jobs: list[NixEvalJobError]
    failed_builds_db: FailedBuildDB | None
    failed_build_report_limit: int


class BuildTrigger(buildstep.ShellMixin, steps.BuildStep):
    """Dynamic trigger that creates a build for every attribute."""

    project: GitProject
    successful_jobs: list[NixEvalJobSuccess]
    failed_jobs: list[NixEvalJobError]
    drv_info: dict[str, NixDerivation]
    builds_scheduler: str
    failed_eval_scheduler: str
    cached_failure_scheduler: str
    dependency_failed_scheduler: str
    result_list: list[int]
    ended: bool
    running: bool
    wait_for_finish_deferred: defer.Deferred[tuple[list[int], int]] | None
    brids: list[int]
    consumers: dict[int, Any]
    failed_builds_db: FailedBuildDB | None

    @dataclass
    class ScheduledJob:
        job: NixEvalJob
        builder_ids: dict[int, int]
        results: defer.Deferred[list[int]]

    @dataclass
    class DoneJob:
        job: NixEvalJobSuccess
        builder_ids: dict[int, int]
        results: list[int]

    @dataclass
    class SchedulingContext:
        """Context for scheduling operations."""

        build_schedule_order: list[NixEvalJobSuccess]
        job_closures: dict[str, set[str]]
        ss_for_trigger: list[dict[str, Any]]
        scheduled: list["BuildTrigger.ScheduledJob"]
        scheduler_log: StreamLog
        schedule_now: list[NixEvalJobSuccess]

    def __init__(
        self,
        project: GitProject,
        trigger_config: "TriggerConfig",
        jobs_config: "JobsConfig",
        nix_attr_prefix: str = "checks",
        **kwargs: Any,
    ) -> None:
        self.project = project
        self.trigger_config = trigger_config
        self.jobs_config = jobs_config
        self.nix_attr_prefix = nix_attr_prefix
        self.config = None
        self._result_list: list[int | None] = []
        self._skipped_count: int = 0
        self._failed_notifications_sent: int = (
            0  # Count of failed build notifications already sent
        )
        self.ended = False
        self.running = False
        self.wait_for_finish_deferred: defer.Deferred[tuple[list[int], int]] | None = (
            None
        )
        self.brids = []
        self.consumers = {}
        self.update_result_futures: list[Coroutine[Any, Any, None]] = []
        super().__init__(**kwargs)

    def interrupt(self, reason: str | Failure) -> None:  # noqa: ARG002
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
            if (
                self.build
                and self.build.conn is None
                and self.wait_for_finish_deferred is not None
            ):
                self.wait_for_finish_deferred.cancel()

    def _should_send_individual_notification_for_failure(self) -> bool:
        """Check if we should send individual notification for a failure."""
        # Check if we've exceeded the limit
        return (
            self._failed_notifications_sent < self.jobs_config.failed_build_report_limit
        )

    def get_scheduler_by_name(self, name: str) -> Triggerable:
        schedulers = self.master.scheduler_manager.namedServices
        if name not in schedulers:
            message = f"unknown triggered scheduler: {name!r}"
            raise BuildbotNixError(message)
        # todo: check ITriggerableScheduler
        return schedulers[name]

    def set_common_properties(
        self,
        props: Properties,
        project: GitProject,
        source: str,
        job: NixEvalJob,
    ) -> Properties:
        name = (
            f"{project.nix_ref_type}:{project.name}#{self.nix_attr_prefix}.{job.attr}"
        )
        props.setProperty("virtual_builder_name", name, source)
        props.setProperty("status_name", f"nix-build {name}", source)
        props.setProperty("virtual_builder_tags", "", source)
        props.setProperty("attr", job.attr, source)
        props.setProperty("default_branch", project.default_branch, source)

        if isinstance(job, NixEvalJobSuccess):
            props.setProperty("drv_path", job.drvPath, source)
            props.setProperty("system", job.system, source)
            props.setProperty("out_path", job.outputs["out"] or None, source)
            props.setProperty("cacheStatus", job.cacheStatus, source)

        return props

    def _create_scheduler_props(
        self, job: NixEvalJob, **extra_props: Any
    ) -> Properties:
        """Helper to create properties for scheduler methods."""
        source = "nix-eval-nix"
        props = self.set_common_properties(Properties(), self.project, source, job)
        for key, value in extra_props.items():
            props.setProperty(key, value, source)
        return props

    def schedule_eval_failure(self, job: NixEvalJobError) -> tuple[str, Properties]:
        props = self._create_scheduler_props(job, error=job.error)
        return (self.trigger_config.failed_eval_scheduler, props)

    def schedule_cached_failure(
        self,
        job: NixEvalJobSuccess,
        first_failure: FailedBuild,
    ) -> tuple[str, Properties]:
        props = self._create_scheduler_props(job, first_failure_url=first_failure.url)
        return (self.trigger_config.cached_failure_scheduler, props)

    def schedule_dependency_failed(
        self,
        job: NixEvalJobSuccess,
        dependency: NixEvalJobSuccess,
    ) -> tuple[str, Properties]:
        props = self._create_scheduler_props(
            job, **{"dependency.attr": dependency.attr}
        )
        return (self.trigger_config.dependency_failed_scheduler, props)

    def schedule_success(
        self,
        build_props: Properties,
        job: NixEvalJobSuccess,
    ) -> tuple[str, Properties] | None:
        source = "nix-eval-nix"

        drv_path = job.drvPath
        out_path = job.outputs["out"] or None

        build_props.setProperty(f"{job.attr}-out_path", out_path, source)
        build_props.setProperty(f"{job.attr}-drv_path", drv_path, source)

        # Schedule builds that need building or substituting
        if job.cacheStatus in {CacheStatus.notBuilt, CacheStatus.cached}:
            props = self.set_common_properties(Properties(), self.project, source, job)
            return (self.trigger_config.builds_scheduler, props)

        # For jobs already in local store, just store the output path for gcroot registration
        # These don't need rebuilding or substituting, but we want to preserve their outputs
        if out_path and job.cacheStatus == CacheStatus.local:
            build_props.setProperty(f"{job.attr}-skipped_out_path", out_path, source)
        return None

    async def schedule(
        self,
        ss_for_trigger: list[dict[str, Any]],
        scheduler_name: str,
        props: Properties,
        *,
        send_notification: bool = False,
    ) -> tuple[dict[int, int], defer.Deferred[list[int]]]:
        scheduler: Triggerable = self.get_scheduler_by_name(scheduler_name)

        ids_deferred, results_deferred = scheduler.trigger(
            waited_for=True,
            sourcestamps=ss_for_trigger,
            set_props=props,
            parent_buildid=self.build.buildid if self.build else None,
            parent_relationship="Triggered from",
        )

        brids: dict[int, int]
        _, brids = await ids_deferred

        for brid in brids.values():
            url = getURLForBuildrequest(self.master, brid)
            await self.addURL(f"{scheduler.name} #{brid}", url)
            self.update_result_futures.append(self._add_results(brid))

        # Only send notification if explicitly requested (for failures only)
        if send_notification:
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
        objs_from_build = self.build.getAllSourceStamps() if self.build else []
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

    async def _schedule_failed_evaluations(
        self,
        ss_for_trigger: list[dict[str, Any]],
        scheduled: list[ScheduledJob],
        scheduler_log: StreamLog,
    ) -> int:
        """Schedule builds for failed evaluation jobs."""
        overall_result = SUCCESS if not self.jobs_config.failed_jobs else util.FAILURE

        if self.jobs_config.failed_jobs:
            scheduler_log.addStdout("The following jobs failed to evaluate:\n")
            for failed_job in self.jobs_config.failed_jobs:
                scheduler_log.addStdout(f"\t- {failed_job.attr} failed eval\n")
                # Check if we should send individual notification for this failure
                should_notify = self._should_send_individual_notification_for_failure()
                if should_notify:
                    self._failed_notifications_sent += 1
                brids, results_deferred = await self.schedule(
                    ss_for_trigger,
                    *self.schedule_eval_failure(failed_job),
                    send_notification=should_notify,
                )
                scheduled.append(
                    BuildTrigger.ScheduledJob(failed_job, brids, results_deferred)
                )
                self.brids.extend(brids.values())

        return overall_result

    async def _process_build_for_scheduling(
        self,
        build: NixEvalJobSuccess,
        ctx: SchedulingContext,
    ) -> None:
        """Process a single build to determine if it should be scheduled."""
        failed_build = None
        if self.jobs_config.failed_builds_db is not None:
            failed_build = self.jobs_config.failed_builds_db.check_build(build.drvPath)

        if ctx.job_closures.get(build.drvPath):
            # Has dependencies, skip for now
            return

        if failed_build is not None and self.build:
            if self.build.reason != "rebuild":
                # Skip due to cached failure
                ctx.scheduler_log.addStdout(
                    f"\t- skipping {build.attr} due to cached failure, first failed at {failed_build.time}\n"
                    f"\t  see build at {failed_build.url}\n"
                )
                ctx.build_schedule_order.remove(build)

                # Check if we should send individual notification for this cached failure
                should_notify = self._should_send_individual_notification_for_failure()
                if should_notify:
                    self._failed_notifications_sent += 1
                brids, results_deferred = await self.schedule(
                    ctx.ss_for_trigger,
                    *self.schedule_cached_failure(build, failed_build),
                    send_notification=should_notify,
                )
                ctx.scheduled.append(
                    BuildTrigger.ScheduledJob(build, brids, results_deferred)
                )
                self.brids.extend(brids.values())
            else:
                # Rebuild requested, remove from cache and schedule
                if self.jobs_config.failed_builds_db is not None:
                    self.jobs_config.failed_builds_db.remove_build(build.drvPath)
                ctx.scheduler_log.addStdout(
                    f"\t- not skipping {build.attr} with cached failure due to rebuild, first failed at {failed_build.time}\n"
                )
                ctx.build_schedule_order.remove(build)
                ctx.schedule_now.append(build)
        else:
            # No cached failure, schedule normally
            ctx.build_schedule_order.remove(build)
            ctx.schedule_now.append(build)

    async def _get_failed_build_url(self, brids: dict[str, Any]) -> str:
        """Get the URL of the actual failed build."""
        for brid in brids.values():
            builds: list[BuildModel] = await self.master.db.builds.getBuilds(
                buildrequestid=brid
            )
            if builds:
                return getURLForBuild(self.master, builds[0].builderid, builds[0].number)
        return ""

    async def _update_failed_builds_cache(
        self, job: NixEvalJobSuccess, brids: dict[str, Any]
    ) -> None:
        """Update the failed builds cache if needed."""
        if self.jobs_config.failed_builds_db is None:
            return

        should_add_to_cache = (
            self.build and self.build.reason == "rebuild"
        ) or not self.jobs_config.failed_builds_db.check_build(job.drvPath)

        if should_add_to_cache:
            url = await self._get_failed_build_url(brids)
            self.jobs_config.failed_builds_db.add_build(
                job.drvPath, datetime.now(tz=UTC), url
            )

    async def _handle_completed_job(
        self,
        job: NixEvalJob,
        ctx: SchedulingContext,
        brids: dict[str, Any],
        result: int,
    ) -> None:
        """Handle a completed job by updating closures and managing failures."""
        if not isinstance(job, NixEvalJobSuccess):
            return

        # Handle failure-specific logic
        if result != SUCCESS:
            # Update failed builds cache if needed
            if result == util.FAILURE:
                await self._update_failed_builds_cache(job, brids)

            # Schedule dependent failures (MUST happen before updating job_closures!)
            removed = self.get_failed_dependents(
                job, ctx.build_schedule_order, ctx.job_closures
            )
            for removed_job in removed:
                scheduler, props = self.schedule_dependency_failed(removed_job, job)
                # Check if we should send individual notification for dependency failures
                should_notify = self._should_send_individual_notification_for_failure()
                if should_notify:
                    self._failed_notifications_sent += 1
                dep_brids, results_deferred = await self.schedule(
                    ctx.ss_for_trigger,
                    scheduler,
                    props,
                    send_notification=should_notify,
                )
                ctx.build_schedule_order.remove(removed_job)
                ctx.scheduled.append(
                    BuildTrigger.ScheduledJob(removed_job, dep_brids, results_deferred)
                )
                self.brids.extend(dep_brids.values())

            if removed:
                ctx.scheduler_log.addStdout(
                    "\t- removed jobs: "
                    + ", ".join([job.drvPath for job in removed])
                    + "\n"
                )

        # Update job closures for ALL completed jobs (both success and failure)
        # This MUST happen after get_failed_dependents since it needs the closures intact
        for job_closure in ctx.job_closures.values():
            if job.drvPath in job_closure:
                job_closure.remove(job.drvPath)

    async def _schedule_ready_jobs(
        self,
        ctx: SchedulingContext,
        build_props: Properties,
    ) -> list[NixEvalJobSuccess]:
        """Schedule jobs that are ready to run. Returns list of skipped jobs."""
        skipped_jobs = []
        for job in ctx.schedule_now:
            schedule_result = self.schedule_success(build_props, job)
            if schedule_result is not None:
                ctx.scheduler_log.addStdout(f"\t- {job.attr}\n")
                brids, results_deferred = await self.schedule(
                    ctx.ss_for_trigger,
                    *schedule_result,
                )
                ctx.scheduled.append(
                    BuildTrigger.ScheduledJob(job, brids, results_deferred)
                )
                self.brids.extend(brids.values())
            else:
                # Skipped build - output path already stored as property
                ctx.scheduler_log.addStdout(
                    f"\t- {job.attr} (skipped, already built)\n"
                )
                skipped_jobs.append(job)
                self._skipped_count += 1

        # Update summary after processing skipped jobs
        if skipped_jobs:
            self.updateSummary()

        return skipped_jobs

    async def _wait_and_process_completed(
        self,
        ctx: SchedulingContext,
        done: list[DoneJob],
        overall_result: int,
    ) -> int:
        """Wait for and process completed jobs."""
        ctx.scheduler_log.addStdout("Waiting...\n")

        self.wait_for_finish_deferred = defer.DeferredList(
            [job.results for job in ctx.scheduled],
            fireOnOneCallback=True,
            fireOnOneErrback=True,
        )

        results: list[int]
        index: int
        results, index = await self.wait_for_finish_deferred  # type: ignore[assignment]

        # Process completed job
        scheduled_job = ctx.scheduled[index]
        job, brids = scheduled_job.job, scheduled_job.builder_ids
        done.append(BuildTrigger.DoneJob(job, brids, results))
        del ctx.scheduled[index]
        result = results[0]

        ctx.scheduler_log.addStdout(
            f"Found finished build {job.attr}, result {util.Results[result].upper()}\n"
        )

        # Only send notifications for failed builds that are under the limit
        if result != SUCCESS:
            # This is a runtime failure, check if we should notify
            should_notify = self._should_send_individual_notification_for_failure()
            if should_notify:
                self._failed_notifications_sent += 1
                await CombinedBuildEvent.produce_event_for_build_requests_by_id(
                    self.master,
                    brids.values(),
                    CombinedBuildEvent.FINISHED_NIX_BUILD,
                    result,
                )

        # Handle completed job and update closures
        await self._handle_completed_job(job, ctx, brids, result)

        overall_result = worst_status(result, overall_result)
        ctx.scheduler_log.addStdout(
            f"\t- new result: {util.Results[overall_result].upper()} \n"
        )
        return overall_result

    async def _process_scheduler_iteration(
        self,
        ctx: SchedulingContext,
        build_props: Properties,
        done: list[DoneJob],
        overall_result: int,
    ) -> int:
        """Process one iteration of the scheduler loop."""
        ctx.scheduler_log.addStdout("Scheduling...\n")

        # Determine which jobs to schedule now
        ctx.schedule_now = []
        for build in list(ctx.build_schedule_order):
            await self._process_build_for_scheduling(build, ctx)

        if not ctx.schedule_now:
            ctx.scheduler_log.addStdout("\tNo builds to schedule found.\n")

        # Schedule ready jobs and get list of skipped jobs
        skipped_jobs = await self._schedule_ready_jobs(ctx, build_props)

        # Update closures for all skipped jobs (they're done and don't need building)
        for job in skipped_jobs:
            for job_closure in ctx.job_closures.values():
                if job.drvPath in job_closure:
                    job_closure.remove(job.drvPath)

        # Only wait if there are scheduled jobs
        if ctx.scheduled:
            overall_result = await self._wait_and_process_completed(
                ctx, done, overall_result
            )

        return overall_result

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
        if self.build:
            await CombinedBuildEvent.produce_event_for_build(
                self.master, CombinedBuildEvent.STARTED_NIX_BUILD, self.build, None
            )

        done: list[BuildTrigger.DoneJob] = []
        scheduled: list[BuildTrigger.ScheduledJob] = []

        self.running = True
        build_props = self.build.getProperties() if self.build else Properties()
        ss_for_trigger = self.prepare_sourcestamp_list_for_trigger()
        scheduler_log: StreamLog = await self.addLog("scheduler")

        # Schedule failed evaluations
        overall_result = await self._schedule_failed_evaluations(
            ss_for_trigger, scheduled, scheduler_log
        )

        # Prepare job dependencies
        job_set = {job.drvPath for job in self.jobs_config.successful_jobs}
        job_closures = {
            k.drvPath: set(k.neededSubstitutes)
            .union(set(k.neededBuilds))
            .intersection(job_set)
            .difference({k.drvPath})
            for k in self.jobs_config.successful_jobs
        }
        build_schedule_order = self.sort_jobs_by_closures(
            self.jobs_config.successful_jobs, job_closures
        )

        # Create scheduling context that will be reused throughout the loop
        ctx = BuildTrigger.SchedulingContext(
            build_schedule_order=build_schedule_order,
            job_closures=job_closures,
            ss_for_trigger=ss_for_trigger,
            scheduled=scheduled,
            schedule_now=[],
            scheduler_log=scheduler_log,
        )

        # Main scheduling loop
        while ctx.build_schedule_order or ctx.scheduled:
            overall_result = await self._process_scheduler_iteration(
                ctx, build_props, done, overall_result
            )

        while self.update_result_futures:
            await self.update_result_futures.pop()

        if self.build:
            await CombinedBuildEvent.produce_event_for_build(
                self.master,
                CombinedBuildEvent.FINISHED_NIX_BUILD,
                self.build,
                overall_result,
            )
        ctx.scheduler_log.addStdout("Done!\n")
        return overall_result

    def getCurrentSummary(self) -> dict[str, str]:  # noqa: N802
        summary = []

        # Show completed build results
        if self._result_list:
            for status in ALL_RESULTS:
                count = self._result_list.count(status)
                if count:
                    summary.append(
                        f"{count} {statusToString(status, count)}",
                    )

        # Show skipped builds
        if self._skipped_count > 0:
            summary.append(f"{self._skipped_count} skipped")

        return {"step": f"({', '.join(summary)})" if summary else "running"}

    def getResultSummary(self) -> dict[str, str]:  # noqa: N802
        """Get the final summary when the step completes."""
        return self.getCurrentSummary()
