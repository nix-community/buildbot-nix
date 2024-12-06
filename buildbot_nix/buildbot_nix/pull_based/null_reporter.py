from typing import Any

from buildbot.reporters.base import ReporterBase


class NullReporter(ReporterBase):
    def checkConfig(self) -> None:  # type: ignore[override]
        super().checkConfig(generators=[])

    def reconfigService(self) -> None:  # type: ignore[override]
        super().reconfigService(generators=[])

    def sendMessage(self, reports: Any) -> None:
        pass
