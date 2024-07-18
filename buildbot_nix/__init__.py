import json
import multiprocessing
import os
import re
import uuid
from collections import defaultdict
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from buildbot.config.builder import BuilderConfig
from buildbot.configurators import ConfiguratorBase
from buildbot.interfaces import WorkerSetupError
from buildbot.locks import MasterLock
from buildbot.plugins import schedulers, steps, util, worker
from buildbot.process import buildstep, logobserver, remotecommand
from buildbot.process.project import Project
from buildbot.process.properties import Properties
from buildbot.process.results import ALL_RESULTS, statusToString
from buildbot.secrets.providers.file import SecretInAFile
from buildbot.steps.trigger import Trigger
from buildbot.www.authz import Authz
from buildbot.www.authz.endpointmatchers import EndpointMatcherBase, Match

if TYPE_CHECKING:
    from buildbot.process.log import StreamLog
    from buildbot.www.auth import AuthBase

from twisted.internet import defer
from twisted.logger import Logger

from .common import (
    slugify_project_name,
)
from .gitea_projects import GiteaBackend, GiteaConfig
from .github_projects import (
    GithubBackend,
    GithubConfig,
)
from .projects import GitBackend, GitProject
from .secrets import read_secret_file

SKIPPED_BUILDER_NAME = "skipped-builds"

log = Logger()


class BuildbotNixError(Exception):
    pass


class BuildTrigger(Trigger):
    """Dynamic trigger that creates a build for every attribute."""

    project: GitProject

    def __init__(
        self,
        project: GitProject,
        builds_scheduler: str,
        skipped_builds_scheduler: str,
        jobs: list[dict[str, Any]],
        **kwargs: Any,
    ) -> None:
        if "name" not in kwargs:
            kwargs["name"] = "trigger"
        self.project = project
        self.jobs = jobs
        self.config = None
        self.builds_scheduler = builds_scheduler
        self.skipped_builds_scheduler = skipped_builds_scheduler
        Trigger.__init__(
            self,
            waitForFinish=True,
            schedulerNames=[builds_scheduler, skipped_builds_scheduler],
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
        build_props = self.build.getProperties()
        repo_name = self.project.name
        project_id = slugify_project_name(repo_name)
        source = f"nix-eval-{project_id}"

        triggered_schedulers = []
        for job in self.jobs:
            attr = job.get("attr", "eval-error")
            name = attr
            if repo_name is not None:
                name = f"github:{repo_name}#checks.{name}"
            else:
                name = f"checks.{name}"
            error = job.get("error")
            props = Properties()
            props.setProperty("virtual_builder_name", name, source)
            props.setProperty("status_name", f"nix-build .#checks.{attr}", source)
            props.setProperty("virtual_builder_tags", "", source)

            if error is not None:
                props.setProperty("error", error, source)
                triggered_schedulers.append((self.skipped_builds_scheduler, props))
                continue

            if job.get("isCached"):
                triggered_schedulers.append((self.skipped_builds_scheduler, props))
                continue

            drv_path = job.get("drvPath")
            system = job.get("system")
            out_path = job.get("outputs", {}).get("out")

            build_props.setProperty(f"{attr}-out_path", out_path, source)
            build_props.setProperty(f"{attr}-drv_path", drv_path, source)

            props.setProperty("attr", attr, source)
            props.setProperty("system", system, source)
            props.setProperty("drv_path", drv_path, source)
            props.setProperty("out_path", out_path, source)
            # we use this to identify builds when running a retry
            props.setProperty("build_uuid", str(uuid.uuid4()), source)

            triggered_schedulers.append((self.builds_scheduler, props))
        return triggered_schedulers

    def getCurrentSummary(self) -> dict[str, str]:  # noqa: N802
        """The original build trigger will the generic builder name `nix-build` in this case, which is not helpful"""
        if not self.triggeredNames:
            return {"step": "running"}
        summary = []
        if self._result_list:
            for status in ALL_RESULTS:
                count = self._result_list.count(status)
                if count:
                    summary.append(
                        f"{self._result_list.count(status)} {statusToString(status, count)}",
                    )
        return {"step": f"({', '.join(summary)})"}


class NixEvalCommand(buildstep.ShellMixin, steps.BuildStep):
    """Parses the output of `nix-eval-jobs` and triggers a `nix-build` build for
    every attribute.
    """

    project: GitProject

    def __init__(
        self, project: GitProject, supported_systems: list[str], **kwargs: Any
    ) -> None:
        kwargs = self.setupShellMixin(kwargs)
        super().__init__(**kwargs)
        self.project = project
        self.observer = logobserver.BufferLogObserver()
        self.addLogObserver("stdio", self.observer)
        self.supported_systems = supported_systems

    @defer.inlineCallbacks
    def run(self) -> Generator[Any, object, Any]:
        # run nix-eval-jobs --flake .#checks to generate the dict of stages
        cmd: remotecommand.RemoteCommand = yield self.makeRemoteShellCommand()
        yield self.runCommand(cmd)

        # if the command passes extract the list of stages
        result = cmd.results()
        if result == util.SUCCESS:
            # create a ShellCommand for each stage and add them to the build
            jobs = []

            for line in self.observer.getStdout().split("\n"):
                if line != "":
                    try:
                        job = json.loads(line)
                    except json.JSONDecodeError as e:
                        msg = f"Failed to parse line: {line}"
                        raise BuildbotNixError(msg) from e
                    jobs.append(job)
            repo_name = self.project.name
            project_id = slugify_project_name(repo_name)
            filtered_jobs = []
            for job in jobs:
                system = job.get("system")
                if not system or system in self.supported_systems:  # report eval errors
                    filtered_jobs.append(job)

            self.build.addStepsAfterCurrentStep(
                [
                    BuildTrigger(
                        self.project,
                        builds_scheduler=f"{project_id}-nix-build",
                        skipped_builds_scheduler=f"{project_id}-nix-skipped-build",
                        name="build flake",
                        jobs=filtered_jobs,
                    ),
                ],
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


@dataclass
class CachixConfig:
    name: str
    signing_key_secret_name: str | None = None
    auth_token_secret_name: str | None = None

    def cachix_env(self) -> dict[str, str]:
        env = {}
        if self.signing_key_secret_name is not None:
            env["CACHIX_SIGNING_KEY"] = util.Secret(self.signing_key_secret_name)
        if self.auth_token_secret_name is not None:
            env["CACHIX_AUTH_TOKEN"] = util.Secret(self.auth_token_secret_name)
        return env


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
            doStepIf=lambda s: s.getProperty("branch") == project.default_branch,
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
    return util.BuilderConfig(
        name=f"{project.name}/nix-skipped-build",
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

    def __init__(
        self,
        # Shape of this file: [ { "name": "<worker-name>", "pass": "<worker-password>", "cores": "<cpu-cores>" } ]
        admins: list[str],
        auth_backend: str,
        build_retries: int,
        github: GithubConfig | None,
        gitea: GiteaConfig | None,
        url: str,
        nix_supported_systems: list[str],
        nix_eval_worker_count: int | None,
        nix_eval_max_memory_size: int,
        post_build_steps: list[steps.BuildStep] | None = None,
        nix_workers_secret_name: str = "buildbot-nix-workers",  # noqa: S107
        cachix: CachixConfig | None = None,
        outputs_path: str | None = None,
    ) -> None:
        super().__init__()
        self.nix_workers_secret_name = nix_workers_secret_name
        self.nix_eval_max_memory_size = nix_eval_max_memory_size
        self.nix_eval_worker_count = nix_eval_worker_count
        self.nix_supported_systems = nix_supported_systems
        self.post_build_steps = post_build_steps or []
        self.auth_backend = auth_backend
        self.admins = admins
        self.github = github
        self.gitea = gitea
        self.url = url
        self.cachix = cachix
        self.build_retries = build_retries
        if outputs_path is None:
            self.outputs_path = None
        else:
            self.outputs_path = Path(outputs_path)

    def configure(self, config: dict[str, Any]) -> None:
        backends: dict[str, GitBackend] = {}

        if self.github is not None:
            backends["github"] = GithubBackend(self.github, self.url)

        if self.gitea is not None:
            backends["gitea"] = GiteaBackend(self.gitea)

        auth: AuthBase | None = (
            backends[self.auth_backend].create_auth()
            if self.auth_backend != "none"
            else None
        )

        projects: list[GitProject] = []

        for backend in backends.values():
            projects += backend.load_projects()

        worker_config = json.loads(read_secret_file(self.nix_workers_secret_name))
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

        if self.cachix is not None:
            self.post_build_steps.append(
                steps.ShellCommand(
                    name="Upload cachix",
                    env=self.cachix.cachix_env(),
                    command=[
                        "cachix",
                        "push",
                        self.cachix.name,
                        util.Interpolate("result-%(prop:attr)s"),
                    ],
                )
            )

        for project in projects:
            config_for_project(
                config,
                project,
                worker_names,
                self.nix_supported_systems,
                self.nix_eval_worker_count or multiprocessing.cpu_count(),
                self.nix_eval_max_memory_size,
                eval_lock,
                self.post_build_steps,
                self.outputs_path,
                self.build_retries,
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
            config.setdefault("secretProviders", [])
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
                admins=self.admins,
                backends=list(backends.values()),
                projects=projects,
            )
