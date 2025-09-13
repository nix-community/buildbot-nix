import copy
from collections.abc import Iterable, Sequence
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
from buildbot.util.twisted import async_to_deferred
from twisted.logger import Logger
from zope.interface import implementer  # type: ignore[import]

if TYPE_CHECKING:
    from buildbot.db.buildrequests import BuildRequestModel

log = Logger()


class NixEvalWarningsFormatter(MessageFormatterRenderable):
    """Custom message formatter that includes evaluation warnings count only for nix-eval builders."""

    def __init__(self, template: str, subject: str | None = None) -> None:
        super().__init__(template, subject)

    @async_to_deferred
    async def format_message_for_build(
        self, master: BuildMaster, build: dict[str, Any], **kwargs: Any
    ) -> Any:
        # Get the basic message from parent
        msgdict = await super().format_message_for_build(master, build, **kwargs)

        # Get the event type if available (set by the generator)
        event_type = build.get("_event_type", "unknown")

        # Only add warnings count if this is actually the nix-eval phase
        # Check for warnings_count in the event data (prefixed with _)
        is_nix_eval_event = event_type in ["started-nix-eval", "finished-nix-eval"]

        if is_nix_eval_event:
            # Get warnings from the event data (for finished-nix-eval)
            # This is passed directly when the event is produced
            warnings_count = build.get("_warnings_count", 0)

            # Modify the description to include warnings count
            original_body = msgdict.get("body", "")
            if warnings_count > 0:
                warning_suffix = (
                    f" ({warnings_count} warning{'s' if warnings_count != 1 else ''})"
                )
                msgdict["body"] = original_body + warning_suffix

        return msgdict


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
        **extra_data: Any,
    ) -> None:
        if isinstance(build, Build):
            build_db: Any = await master.data.get(("builds", str(build.buildid)))
            if result is not None:
                build_db["results"] = result
            # Add event type and any extra data passed to the event
            build_db["_event_type"] = event.value
            for key, value in extra_data.items():
                build_db[f"_{key}"] = value
            event_key = ("builds", str(build.buildid), event.value)
            master.mq.produce(event_key, copy.deepcopy(build_db))
        elif isinstance(build, dict):
            if result is not None:
                build["results"] = result
            # Add event type and any extra data passed to the event
            build["_event_type"] = event.value
            for key, value in extra_data.items():
                build[f"_{key}"] = value
            event_key = ("builds", str(build["buildid"]), event.value)
            master.mq.produce(event_key, copy.deepcopy(build))


@implementer(IReportGenerator)
class BuildNixEvalStatusGenerator(BuildStatusGeneratorMixin):
    wanted_event_keys: ClassVar[list[Any]] = [
        ("builds", None, CombinedBuildEvent.STARTED_NIX_EVAL.value),
        ("builds", None, CombinedBuildEvent.FINISHED_NIX_EVAL.value),
        ("builds", None, CombinedBuildEvent.STARTED_NIX_BUILD.value),
        ("builds", None, CombinedBuildEvent.FINISHED_NIX_BUILD.value),
        ("builds", None, CombinedBuildEvent.STARTED_NIX_EFFECTS.value),
        ("builds", None, CombinedBuildEvent.FINISHED_NIX_EFFECTS.value),
        ("buildrequests", None, CombinedBuildEvent.STARTED_NIX_BUILD.value),
        ("buildrequests", None, CombinedBuildEvent.FINISHED_NIX_BUILD.value),
    ]

    compare_attrs: ClassVar[Sequence[str]] = [
        "mode",
        "tags",
        "builders",
        "schedulers",
        "branches",
        "add_patch",
        "start_formatter",
        "end_formatter",
    ]

    start_formatter: MessageFormatterBase
    end_formatter: MessageFormatterBase

    def __init__(  # noqa: PLR0913
        self,
        mode: str = "all",
        *,
        tags: list[str] | None = None,
        builders: list[str] | None = None,
        schedulers: list[str] | None = None,
        branches: list[str] | None = None,
        add_patch: bool = False,
        start_formatter: MessageFormatterBase | None = None,
        end_formatter: MessageFormatterBase | None = None,
    ) -> None:
        super().__init__(
            mode=mode,
            tags=tags,
            builders=builders,
            schedulers=schedulers,
            branches=branches,
            subject=None,
            add_logs=None,
            add_patch=add_patch,
        )

        self.start_formatter = start_formatter or NixEvalWarningsFormatter(
            "Build started."
        )
        self.end_formatter = end_formatter or NixEvalWarningsFormatter("Build done.")

    # TODO: copy pasted from buildbot, make it static upstream and reuse
    @staticmethod
    async def partial_build_dict(
        master: BuildMaster, brdict: dict[str, Any]
    ) -> dict[str, Any]:
        buildrequest_model: (
            BuildRequestModel | None
        ) = await master.db.buildrequests.getBuildRequest(brdict["buildrequestid"])
        if buildrequest_model is None:
            msg = f"Failed to get build request for id {brdict['buildrequestid']}"
            raise RuntimeError(msg)

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

    def _get_status_name_for_event(
        self, event_type: CombinedBuildEvent, current_status_name: tuple[str, str]
    ) -> tuple[str, str]:
        """Determine the status name based on the event type."""
        match event_type:
            case (
                CombinedBuildEvent.STARTED_NIX_EVAL
                | CombinedBuildEvent.FINISHED_NIX_EVAL
            ):
                return ("nix-eval", "generator")
            case (
                CombinedBuildEvent.STARTED_NIX_BUILD
                | CombinedBuildEvent.FINISHED_NIX_BUILD
            ):
                # Only update from nix-eval to nix-build
                if current_status_name[0] == "nix-eval":
                    return ("nix-build", "generator")
                return current_status_name
            case (
                CombinedBuildEvent.STARTED_NIX_EFFECTS
                | CombinedBuildEvent.FINISHED_NIX_EFFECTS
            ):
                return ("nix-effects", "generator")
            case _:
                msg = f"Unexpected event: {event_type}"
                raise ValueError(msg)

    def _is_finish_event(self, event_type: CombinedBuildEvent) -> bool:
        """Check if the event is a finish event."""
        return event_type in [
            CombinedBuildEvent.FINISHED_NIX_EVAL,
            CombinedBuildEvent.FINISHED_NIX_BUILD,
            CombinedBuildEvent.FINISHED_NIX_EFFECTS,
        ]

    async def _handle_build_event(
        self,
        master: BuildMaster,
        reporter: ReporterBase,
        event: str,
        data: dict[str, Any],
    ) -> None | dict[str, Any]:
        """Handle build-related events."""
        # Check if this is a start event
        is_start_event = event in [
            CombinedBuildEvent.STARTED_NIX_EVAL.value,
            CombinedBuildEvent.STARTED_NIX_BUILD.value,
            CombinedBuildEvent.STARTED_NIX_EFFECTS.value,
        ]

        formatter = self.start_formatter if is_start_event else self.end_formatter

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
        event_type = CombinedBuildEvent(event)

        # Update status name
        current_status = report["builds"][0]["properties"].get("status_name", ("", ""))
        report["builds"][0]["properties"]["status_name"] = (
            self._get_status_name_for_event(event_type, current_status)
        )

        # Mark as complete if it's a finish event
        if self._is_finish_event(event_type):
            report["builds"][0]["complete"] = True
            report["builds"][0]["complete_at"] = datetime.now(tz=UTC)

        return report

    async def _handle_buildrequest_event(
        self,
        master: BuildMaster,
        event: str,
        data: dict[str, Any],
    ) -> None | dict[str, Any]:
        """Handle buildrequest-related events."""
        build: dict[str, Any] = await self.partial_build_dict(master, data)

        # TODO: figure out when do we trigger this
        if event == "canceled-nix-build":
            build["complete"] = True
            build["results"] = CANCELLED

        if not self.is_message_needed_by_props(build):
            return None

        return await self.buildrequest_message(master, build)

    async def generate(
        self,
        master: BuildMaster,
        reporter: ReporterBase,
        key: tuple[str, None | Any, str],
        data: dict[str, Any],  # TODO database types
    ) -> None | dict[str, Any]:
        what, _, event = key

        if what == "builds":
            return await self._handle_build_event(master, reporter, event, data)

        if what == "buildrequests":
            return await self._handle_buildrequest_event(master, event, data)

        return None
