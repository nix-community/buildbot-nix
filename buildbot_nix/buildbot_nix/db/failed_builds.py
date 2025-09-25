"""Database connector for tracking failed builds using SQLAlchemy."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from buildbot.db import base

if TYPE_CHECKING:
    from buildbot.db.connector import DBConnector


class FailedBuild:
    """Data class representing a failed build."""

    def __init__(self, derivation: str, time: datetime, url: str) -> None:
        self.derivation = derivation
        self.time = time
        self.url = url


class FailedBuildsConnectorComponent(base.DBConnectorComponent):
    """
    Database connector component for tracking failed builds.
    This allows us to cache failed builds and skip them in subsequent runs.
    """

    def __init__(self, connector: DBConnector) -> None:
        super().__init__(connector)

        # Define the table
        self.failed_builds = sa.Table(
            "failed_builds",
            connector.model.metadata,
            sa.Column("derivation", sa.String(512), primary_key=True, nullable=False),
            sa.Column("timestamp", sa.Float, nullable=False),
            sa.Column("url", sa.String(1024), nullable=False),
        )

    async def setup(self) -> None:
        """Set up the database table if it doesn't exist."""

        # Create the table if it doesn't exist
        def thd(conn: sa.engine.Connection) -> None:
            self.failed_builds.create(conn, checkfirst=True)

        await self.db.pool.do_with_engine(thd)

    async def add_build(self, derivation: str, time: datetime, url: str) -> None:
        """Add or update a failed build in the database."""

        def thd(conn: sa.engine.Connection) -> None:
            # Compute timestamp
            timestamp = time.timestamp()

            # Use a transaction-based upsert that works across all databases
            trans = conn.begin()
            try:
                # Try to update first
                update_stmt = (
                    self.failed_builds.update()
                    .where(self.failed_builds.c.derivation == derivation)
                    .values(timestamp=timestamp, url=url)
                )
                result = conn.execute(update_stmt)

                # If no rows were updated, insert a new one
                if result.rowcount == 0:
                    insert_stmt = self.failed_builds.insert().values(
                        derivation=derivation,
                        timestamp=timestamp,
                        url=url,
                    )
                    conn.execute(insert_stmt)

                trans.commit()
            except Exception:
                trans.rollback()
                raise

        await self.db.pool.do(thd)

    async def check_build(self, derivation: str) -> FailedBuild | None:
        """Check if a build has failed before and return its details."""

        def thd(conn: sa.engine.Connection) -> FailedBuild | None:
            stmt = sa.select(
                self.failed_builds.c.timestamp, self.failed_builds.c.url
            ).where(self.failed_builds.c.derivation == derivation)
            result = conn.execute(stmt)
            row = result.fetchone()

            if row:
                timestamp, url = row
                time = datetime.fromtimestamp(timestamp, tz=UTC)
                return FailedBuild(derivation=derivation, time=time, url=url)
            return None

        return await self.db.pool.do(thd)

    async def remove_build(self, derivation: str) -> None:
        """Remove a failed build from the database."""

        def thd(conn: sa.engine.Connection) -> None:
            stmt = self.failed_builds.delete().where(
                self.failed_builds.c.derivation == derivation
            )
            conn.execute(stmt)

        await self.db.pool.do(thd)

    async def cleanup_old_builds(self, days_old: int = 30) -> None:
        """Remove build entries older than specified days."""

        def thd(conn: sa.engine.Connection) -> None:
            cutoff_time = datetime.now(tz=UTC).timestamp() - (days_old * 24 * 60 * 60)
            stmt = self.failed_builds.delete().where(
                self.failed_builds.c.timestamp < cutoff_time
            )
            conn.execute(stmt)

        await self.db.pool.do(thd)
