"""Schema migrations: versioned SQL scripts applied at startup.

Scripts live in the `migrations/` package directory and are named
`NNNN_description.sql`. Each script runs in its own transaction; the
whole run is guarded by a Postgres advisory lock so concurrent service
starts cannot race. Applied versions are recorded in
`schema_migrations`.

asyncpg is used directly (not through SQLAlchemy) because it executes
multi-statement scripts natively.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from importlib import resources

import asyncpg

logger = logging.getLogger(__name__)

# Arbitrary constant identifying "buildbot-nix schema migrations".
ADVISORY_LOCK_KEY = 0x6275_6E69


class MigrationError(Exception):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


_SCRIPT_RE = re.compile(r"^(\d{4})_(.+)\.sql$")


def load_migrations() -> list[Migration]:
    """Load migration scripts shipped with the package, ordered by version."""
    migrations = []
    for entry in resources.files(__package__).joinpath("migrations").iterdir():
        m = _SCRIPT_RE.match(entry.name)
        if m is None:
            continue
        migrations.append(
            Migration(
                version=int(m.group(1)),
                name=m.group(2),
                sql=entry.read_text(),
            )
        )
    migrations.sort(key=lambda mig: mig.version)
    versions = [m.version for m in migrations]
    if len(set(versions)) != len(versions):
        msg = f"Duplicate migration versions: {versions}"
        raise MigrationError(msg)
    return migrations


async def apply_migrations(dsn: str) -> None:
    """Apply all pending migrations to the database at `dsn`."""
    migrations = load_migrations()
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", ADVISORY_LOCK_KEY)
        try:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version BIGINT PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            applied = {
                row["version"]
                for row in await conn.fetch("SELECT version FROM schema_migrations")
            }
            for migration in migrations:
                if migration.version in applied:
                    continue
                logger.info(
                    "applying migration",
                    extra={
                        "version": migration.version,
                        "migration": migration.name,
                    },
                )
                async with conn.transaction():
                    await conn.execute(migration.sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version, name) VALUES ($1, $2)",
                        migration.version,
                        migration.name,
                    )
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_KEY)
    finally:
        await conn.close()
