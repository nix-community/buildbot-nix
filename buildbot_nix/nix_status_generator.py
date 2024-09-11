from datetime import UTC, datetime
from typing import Any, ClassVar

from buildbot.interfaces import IRenderable, IReportGenerator
from buildbot.master import BuildMaster
from buildbot.process.build import Build
from buildbot.process.buildrequest import BuildRequest
from buildbot.process.properties import Properties
from buildbot.process.results import CANCELLED
from buildbot.reporters import utils
from buildbot.reporters.base import ReporterBase
from buildbot.reporters.generators.utils import BuildStatusGeneratorMixin
from buildbot.reporters.message import MessageFormatterRenderable
from buildbot.reporters.utils import getDetailsForBuild
from twisted.internet import defer
from twisted.logger import Logger
from zope.interface import implementer

log = Logger()


@implementer(IReportGenerator)
class BuildNixEvalStatusGenerator(BuildStatusGeneratorMixin):
    wanted_event_keys: ClassVar[list[Any]] = [
        ("builds", None, "started-nix-eval"),
        ("builds", None, "finished-nix-eval"),
        ("builds", None, "started-nix-build"),
        ("builds", None, "finished-nix-build"),
        ("buildrequests", None, "started-nix-build"),
        ("buildrequests", None, "canceled-nix-build"),
    ]

    compare_attrs: ClassVar[list[str]] = ["start_formatter", "end_formatter"]

    start_formatter: IRenderable
    end_formatter: IRenderable

    def __init__(
        self,
        tags: None | list[str] = None,
        builders: None | list[str] = None,
        schedulers: None | list[str] = None,
        branches: None | list[str] = None,
        add_logs: bool | list[str] = False,
        add_patch: bool = False,
        start_formatter: None | IRenderable = None,
        end_formatter: None | IRenderable = None,
    ) -> None:
        super().__init__(
            "all", tags, builders, schedulers, branches, None, add_logs, add_patch
        )
        self.start_formatter = start_formatter
        if self.start_formatter is None:
            self.start_formatter = MessageFormatterRenderable("Build started.")
        self.end_formatter = end_formatter
        if self.end_formatter is None:
            self.end_formatter = MessageFormatterRenderable("Build done.")

    # TODO: copy pasted from buildbot, make it static upstream and reuse
    @staticmethod
    @defer.inlineCallbacks
    def partial_build_dict(
        master: BuildMaster, buildrequest: BuildRequest
    ) -> defer.Generator[Any, object, dict[str, Any]]:
        brdict: Any = yield master.db.buildrequests.getBuildRequest(
            buildrequest["buildrequestid"]
        )
        bdict = {}

        props = Properties()
        buildrequest = yield BuildRequest.fromBrdict(master, brdict)
        builder = yield master.botmaster.getBuilderById(brdict["builderid"])

        yield Build.setup_properties_known_before_build_starts(
            props, [buildrequest], builder
        )
        Build.setupBuildProperties(props, [buildrequest])

        bdict["properties"] = props.asDict()
        yield utils.get_details_for_buildrequest(master, brdict, bdict)
        return bdict

    # TODO: copy pasted from buildbot, somehow reuse
    @defer.inlineCallbacks
    def buildrequest_message(
        self, master: BuildMaster, build: dict[str, Any]
    ) -> defer.Generator[Any, Any, dict[str, Any]]:
        patches = self._get_patches_for_build(build)
        users: list[str] = []
        buildmsg = yield self.start_formatter.format_message_for_build(
            master, build, is_buildset=True, mode=self.mode, users=users
        )

        return {
            "body": buildmsg["body"],
            "subject": buildmsg["subject"],
            "type": buildmsg["type"],
            "results": build["results"],
            "builds": [build],
            "buildset": build["buildset"],
            "users": list(users),
            "patches": patches,
            "logs": [],
            "extra_info": buildmsg["extra_info"],
        }

    @defer.inlineCallbacks
    def generate(
        self,
        master: BuildMaster,
        reporter: ReporterBase,
        key: tuple[str, None | Any, str],
        data: Build | BuildRequest,
    ) -> defer.Generator[Any, Any, Any]:
        what, _, event = key
        log.info("{what} produced {event}", what=what, event=event)
        if what == "builds":
            is_new = event == "new"

            formatter = self.start_formatter if is_new else self.end_formatter

            yield getDetailsForBuild(
                master,
                data,
                want_properties=formatter.want_properties,
                want_steps=formatter.want_steps,
                want_logs=formatter.want_logs,
                want_logs_content=formatter.want_logs_content,
            )

            if not self.is_message_needed_by_props(data):
                return None

            report: dict[str, Any] = yield self.build_message(
                formatter, master, reporter, data
            )

            if event in {"started-nix-eval", "finished-nix-eval"}:
                report["builds"][0]["properties"]["status_name"] = (
                    "nix-eval",
                    "generator",
                )
            if (event in {"started-nix-build", "finished-nix-build"}) and report[
                "builds"
            ][0]["properties"]["status_name"][0] == "nix-eval":
                report["builds"][0]["properties"]["status_name"] = (
                    "nix-build",
                    "generator",
                )
            if event in {"finished-nix-eval", "finished-nix-build"}:
                report["builds"][0]["complete"] = True
                report["builds"][0]["complete_at"] = datetime.now(tz=UTC)

            return report
        elif what == "buildrequests":
            build: dict[str, Any] = yield self.partial_build_dict(master, data)

            if event == "canceled-nix-build":
                build["complete"] = True
                build["results"] = CANCELLED

            if not self.is_message_needed_by_props(build):
                return None

            report = yield self.buildrequest_message(master, build)
            return report
