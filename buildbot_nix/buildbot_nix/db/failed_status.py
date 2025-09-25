"""Database connector for tracking failed build statuses using SQLAlchemy."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from buildbot.db import base

if TYPE_CHECKING:
    from buildbot.db.connector import DBConnector


class FailedStatusConnectorComponent(base.DBConnectorComponent):
    """
    Database connector component for tracking failed GitHub/Gitea statuses.
    This allows us to know which commits have failed statuses that need clearing
    when rebuilds succeed.
    """

    def __init__(self, connector: DBConnector) -> None:
        super().__init__(connector)

        # Define the table
        self.failed_statuses = sa.Table(
            "failed_statuses",
            connector.model.metadata,
            sa.Column("revision", sa.String(255), nullable=False),
            sa.Column("status_name", sa.String(255), nullable=False),
            sa.Column("timestamp", sa.Float, nullable=False),
            sa.PrimaryKeyConstraint("revision", "status_name"),
        )

    async def setup(self) -> None:
        """Set up the database table if it doesn't exist."""

        # Create the table if it doesn't exist
        def thd(conn: sa.engine.Connection) -> None:
            self.failed_statuses.create(conn, checkfirst=True)

        await self.db.pool.do_with_engine(thd)

    async def mark_status_failed(self, revision: str, status_name: str) -> None:
        """Mark that this revision+status has failed on GitHub/Gitea."""

        def thd(conn: sa.engine.Connection) -> None:
            # Compute timestamp once to avoid drift
            timestamp = datetime.now(tz=UTC).timestamp()

            # Use a transaction-based upsert that works across all databases
            trans = conn.begin()
            try:
                # Try to update first
                update_stmt = (
                    self.failed_statuses.update()
                    .where(
                        (self.failed_statuses.c.revision == revision)
                        & (self.failed_statuses.c.status_name == status_name)
                    )
                    .values(timestamp=timestamp)
                )
                result = conn.execute(update_stmt)

                # If no rows were updated, insert a new one
                if result.rowcount == 0:
                    insert_stmt = self.failed_statuses.insert().values(
                        revision=revision,
                        status_name=status_name,
                        timestamp=timestamp,
                    )
                    conn.execute(insert_stmt)

                trans.commit()
            except Exception:
                trans.rollback()
                raise

        await self.db.pool.do(thd)

    async def get_all_failed_statuses_for_revision(self, revision: str) -> list[str]:
        """Get all failed status names for a given revision."""

        def thd(conn: sa.engine.Connection) -> list[str]:
            stmt = sa.select(self.failed_statuses.c.status_name).where(
                self.failed_statuses.c.revision == revision
            )
            result = conn.execute(stmt)
            return [row[0] for row in result.fetchall()]

        return await self.db.pool.do(thd)

    async def cleanup_old_statuses(self, days_old: int = 30) -> None:
        """Remove status entries older than specified days."""

        def thd(conn: sa.engine.Connection) -> None:
            cutoff_time = datetime.now(tz=UTC).timestamp() - (days_old * 24 * 60 * 60)
            stmt = self.failed_statuses.delete().where(
                self.failed_statuses.c.timestamp < cutoff_time
            )
            conn.execute(stmt)

        await self.db.pool.do(thd)
