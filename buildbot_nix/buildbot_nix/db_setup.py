"""Service to register custom database components with BuildBot."""

from __future__ import annotations

from buildbot.util import service
from buildbot.util.twisted import async_to_deferred

from .db import FailedBuildsConnectorComponent, FailedStatusConnectorComponent


class DatabaseSetupService(service.BuildbotService):
    """Service that registers custom database components when the master starts."""

    name = "db_setup"

    @async_to_deferred
    async def startService(self) -> None:  # noqa: N802
        """Register our database components when the service starts."""
        await super().startService()

        # Register the failed_status component if not already registered
        if not hasattr(self.master.db, "failed_status"):
            self.master.db.failed_status = FailedStatusConnectorComponent(
                self.master.db
            )
            # Initialize the database tables
            await self.master.db.failed_status.setup()

        # Register the failed_builds component if not already registered
        if not hasattr(self.master.db, "failed_builds"):
            self.master.db.failed_builds = FailedBuildsConnectorComponent(
                self.master.db
            )
            # Initialize the database tables
            await self.master.db.failed_builds.setup()
