from zope.interface import implementer
from buildbot.interfaces import IReportGenerator, IRenderable
from buildbot.reporters.generators.utils import BuildStatusGeneratorMixin
from buildbot.reporters.message import MessageFormatterRenderable
from twisted.internet import defer
from typing import Any, cast
from buildbot.reporters.base import ReporterBase
from buildbot.master import BuildMaster
from buildbot.process.build import Build
from buildbot.reporters.utils import getDetailsForBuild
from datetime import datetime, timezone

@implementer(IReportGenerator)
class BuildNixEvalStatusGenerator(BuildStatusGeneratorMixin):
    wanted_event_keys = [
        ('builds', None, 'started-nix-eval'),
        ('builds', None, 'finished-nix-eval'),
        ('builds', None, 'started-nix-build'),
        ('builds', None, 'finished-nix-build'),
    ]

    compare_attrs = ['start_formatter', 'end_formatter']

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
    ):
        super().__init__('all', tags, builders, schedulers, branches, None, add_logs, add_patch)
        self.start_formatter = start_formatter
        if self.start_formatter is None:
            self.start_formatter = MessageFormatterRenderable('Build started.')
        self.end_formatter = end_formatter
        if self.end_formatter is None:
            self.end_formatter = MessageFormatterRenderable('Build done.')

    @defer.inlineCallbacks
    def generate(self, master: BuildMaster, reporter: ReporterBase, key: tuple[str, None | Any, str], build: Build) -> defer.Generator[Any, object, Any]:
        _, _, event = key
        is_new = event == 'new'

        formatter = self.start_formatter if is_new else self.end_formatter

        yield getDetailsForBuild(
            master,
            build,
            want_properties=formatter.want_properties,
            want_steps=formatter.want_steps,
            want_logs=formatter.want_logs,
            want_logs_content=formatter.want_logs_content,
        )

        if not self.is_message_needed_by_props(build):
            return None

        report = yield self.build_message(formatter, master, reporter, build)
        reportT: dict[str, Any] = cast(dict[str, Any], report)

        if event == "started-nix-eval" or event == "finished-nix-eval":
            reportT["builds"][0]["properties"]["status_name"] = ("nix-eval", "generator")
        if (event == "started-nix-build" or event == "finished-nix-build") and reportT["builds"][0]["properties"]["status_name"][0] == "nix-eval":
            reportT["builds"][0]["properties"]["status_name"] = ("nix-build", "generator")
        if event == "finished-nix-eval" or event == "finished-nix-build":
            reportT["builds"][0]["complete"] = True
            reportT["builds"][0]["complete_at"] = datetime.now(tz=timezone.utc)

        return reportT
