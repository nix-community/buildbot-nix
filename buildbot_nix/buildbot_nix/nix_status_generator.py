import copy
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar

from buildbot.interfaces import IReportGenerator
from buildbot.master import BuildMaster
from buildbot.process.build import Build
from buildbot.process.buildrequest import BuildRequest
from buildbot.process.properties import Properties
from buildbot.process.results import CANCELLED
from buildbot.reporters import utils
from buildbot.reporters.base import ReporterBase
from buildbot.reporters.generators.utils import BuildStatusGeneratorMixin
from buildbot.reporters.message import MessageFormatterBase, MessageFormatterRenderable
from buildbot.reporters.utils import getDetailsForBuild
from twisted.logger import Logger
from zope.interface import implementer  # type: ignore[import]

if TYPE_CHECKING:
    from buildbot.db.buildrequests import BuildRequestModel

log = Logger()


class CombinedBuildEvent(Enum):
    STARTED_NIX_EVAL = "started-nix-eval"
    FINISHED_NIX_EVAL = "finished-nix-eval"
    STARTED_NIX_BUILD = "started-nix-build"
    FINISHED_NIX_BUILD = "finished-nix-build"
    STARTED_NIX_EFFECTS = "started-nix-effects"
    FINISHED_NIX_EFFECTS = "finished-nix-effects"

    @staticmethod
    async def produce_event_for_build_requests_by_id(
        master: BuildMaster,
        buildrequest_ids: Iterable[int],
        event: "CombinedBuildEvent",
        result: None | int,
    ) -> None:
        for buildrequest_id in buildrequest_ids:
            builds: Any = await master.data.get(
                ("buildrequests", str(buildrequest_id), "builds")
            )

            # only report with `buildrequets` if there are no builds to report for
            if not builds:
                buildrequest: Any = await master.data.get(
                    ("buildrequests", str(buildrequest_id))
                )
                if result is not None:
                    buildrequest["results"] = result
                master.mq.produce(
                    ("buildrequests", str(buildrequest["buildrequestid"]), str(event)),
                    copy.deepcopy(buildrequest),
                )
            else:
                for build in builds:
                    await CombinedBuildEvent.produce_event_for_build(
                        master, event, build, result
                    )

    @staticmethod
    async def produce_event_for_build(
        master: BuildMaster,
        event: "CombinedBuildEvent",
        build: Build | dict[str, Any],
        result: None | int,
    ) -> None:
        if isinstance(build, Build):
            build_db: Any = await master.data.get(("builds", str(build.buildid)))
            if result is not None:
                build_db["results"] = result
            master.mq.produce(
                ("builds", str(build.buildid), event.name), copy.deepcopy(build_db)
            )
        elif isinstance(build, dict):
            if result is not None:
                build["results"] = result
            master.mq.produce(
                ("builds", str(build["buildid"]), event.name), copy.deepcopy(build)
            )


@implementer(IReportGenerator)
class BuildNixEvalStatusGenerator(BuildStatusGeneratorMixin):
    wanted_event_keys: ClassVar[list[Any]] = [
        ("builds", None, str(CombinedBuildEvent.STARTED_NIX_EVAL.name)),
        ("builds", None, str(CombinedBuildEvent.FINISHED_NIX_EVAL.name)),
        ("builds", None, str(CombinedBuildEvent.STARTED_NIX_BUILD.name)),
        ("builds", None, str(CombinedBuildEvent.FINISHED_NIX_BUILD.name)),
        ("builds", None, str(CombinedBuildEvent.STARTED_NIX_EFFECTS.name)),
        ("builds", None, str(CombinedBuildEvent.FINISHED_NIX_EFFECTS.name)),
        ("buildrequests", None, str(CombinedBuildEvent.STARTED_NIX_BUILD.name)),
        ("buildrequests", None, str(CombinedBuildEvent.FINISHED_NIX_BUILD.name)),
    ]

    compare_attrs: ClassVar[list[str]] = ["start_formatter", "end_formatter"]

    start_formatter: MessageFormatterBase
    endj_formatter: MessageFormatterBase

    def __init__(
        self,
        tags: None | list[str] = None,
        builders: None | list[str] = None,
        schedulers: None | list[str] = None,
        branches: None | list[str] = None,
        want_logs_content: bool = False,
        add_patch: bool = False,
        start_formatter: None | MessageFormatterBase = None,
        end_formatter: None | MessageFormatterBase = None,
    ) -> None:
        super().__init__(
            "all",
            tags,
            builders,
            schedulers,
            branches,
            None,
            want_logs_content,
            add_patch,
        )

        self.start_formatter = start_formatter or MessageFormatterRenderable(
            "Build started."
        )
        self.end_formatter = end_formatter or MessageFormatterRenderable("Build done.")

    # TODO: copy pasted from buildbot, make it static upstream and reuse
    @staticmethod
    async def partial_build_dict(
        master: BuildMaster, brdict: dict[str, Any]
    ) -> dict[str, Any]:
        buildrequest_model: (
            BuildRequestModel | None
        ) = await master.db.buildrequests.getBuildRequest(brdict["buildrequestid"])
        assert buildrequest_model is not None

        bdict = {}

        props = Properties()
        buildrequest = await BuildRequest.fromBrdict(master, buildrequest_model)
        builder = await master.botmaster.getBuilderById(buildrequest_model.builderid)

        await Build.setup_properties_known_before_build_starts(
            props, [buildrequest], builder
        )
        Build.setupBuildProperties(props, [buildrequest])

        bdict["properties"] = props.asDict()
        await utils.get_details_for_buildrequest(master, buildrequest, bdict)
        return bdict

    # TODO: copy pasted from buildbot, somehow reuse
    async def buildrequest_message(
        self, master: BuildMaster, build: dict[str, Any]
    ) -> dict[str, Any]:
        patches = self._get_patches_for_build(build)
        users: list[str] = []
        buildmsg = await self.start_formatter.format_message_for_build(
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

    async def generate(
        self,
        master: BuildMaster,
        reporter: ReporterBase,
        key: tuple[str, None | Any, str],
        data: dict[str, Any],  # TODO database types
    ) -> None | dict[str, Any]:
        what, _, event = key
        if what == "builds":
            is_new = event == "new"

            formatter = self.start_formatter if is_new else self.end_formatter

            await getDetailsForBuild(
                master,
                data,
                want_properties=formatter.want_properties,
                want_steps=formatter.want_steps,
                want_logs=formatter.want_logs,
                want_logs_content=formatter.want_logs_content,
            )

            if not self.is_message_needed_by_props(data):
                return None

            report: dict[str, Any] = await self.build_message(
                formatter, master, reporter, data
            )

            event_typed: CombinedBuildEvent = CombinedBuildEvent.__members__[event]
            match event_typed:
                case (
                    CombinedBuildEvent.STARTED_NIX_EVAL
                    | CombinedBuildEvent.FINISHED_NIX_EVAL
                ):
                    report["builds"][0]["properties"]["status_name"] = (
                        "nix-eval",
                        "generator",
                    )
                case (
                    CombinedBuildEvent.STARTED_NIX_BUILD
                    | CombinedBuildEvent.FINISHED_NIX_BUILD
                ):
                    if (
                        report["builds"][0]["properties"]["status_name"][0]
                        == "nix-eval"
                    ):
                        report["builds"][0]["properties"]["status_name"] = (
                            "nix-build",
                            "generator",
                        )
                case (
                    CombinedBuildEvent.STARTED_NIX_EFFECTS
                    | CombinedBuildEvent.FINISHED_NIX_EFFECTS
                ):
                    report["builds"][0]["properties"]["status_name"] = (
                        "nix-effects",
                        "generator",
                    )
                case _:
                    msg = f"Unexpected event: {event_typed}"
                    raise ValueError(msg)

            match event_typed:
                case (
                    CombinedBuildEvent.FINISHED_NIX_EVAL
                    | CombinedBuildEvent.FINISHED_NIX_BUILD
                    | CombinedBuildEvent.FINISHED_NIX_EFFECTS
                ):
                    report["builds"][0]["complete"] = True
                    report["builds"][0]["complete_at"] = datetime.now(tz=UTC)

            return report
        if what == "buildrequests":
            build: dict[str, Any] = await self.partial_build_dict(master, data)

            # TODO: figure out when do we trigger this
            if event == "canceled-nix-build":
                build["complete"] = True
                build["results"] = CANCELLED

            if not self.is_message_needed_by_props(build):
                return None

            return await self.buildrequest_message(master, build)
        return None
