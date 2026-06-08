"""Server-side storage for forge OAuth tokens.

The session cookie carries only an opaque session id: the cookie is
signed but not encrypted, and server-side storage lets logout
invalidate the token immediately.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import asyncpg


class TokenVault(Protocol):
    async def save(self, session_id: str, token: str, lifetime: int) -> None: ...

    async def get(self, session_id: str) -> str | None: ...

    async def delete(self, session_id: str) -> None: ...


class ForgeTokenStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def save(self, session_id: str, token: str, lifetime: int) -> None:
        # Lazy cleanup of expired rows.
        await self.pool.execute("DELETE FROM forge_tokens WHERE expires_at < now()")
        await self.pool.execute(
            "INSERT INTO forge_tokens (session_id, token, expires_at) "
            "VALUES ($1, $2, now() + make_interval(secs => $3))",
            session_id,
            token,
            float(lifetime),
        )

    async def get(self, session_id: str) -> str | None:
        return await self.pool.fetchval(
            "SELECT token FROM forge_tokens "
            "WHERE session_id = $1 AND expires_at > now()",
            session_id,
        )

    async def delete(self, session_id: str) -> None:
        await self.pool.execute(
            "DELETE FROM forge_tokens WHERE session_id = $1", session_id
        )


class SessionRevocations(Protocol):
    async def revoke(self, session_id: str, lifetime: int) -> None: ...

    async def is_revoked(self, session_id: str) -> bool: ...


class RevokedSessionStore:
    """Logout denylist for the stateless session cookies: the cookie
    stays validly signed until expiry, so revocation must be recorded
    server-side and checked on every authenticated request."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def revoke(self, session_id: str, lifetime: int) -> None:
        # Lazy pruning: rows are only needed until the cookie itself
        # would have expired.
        await self.pool.execute("DELETE FROM revoked_sessions WHERE expires_at < now()")
        await self.pool.execute(
            "INSERT INTO revoked_sessions (session_id, expires_at) "
            "VALUES ($1, now() + make_interval(secs => $2)) "
            "ON CONFLICT (session_id) DO NOTHING",
            session_id,
            float(lifetime),
        )

    async def is_revoked(self, session_id: str) -> bool:
        return bool(
            await self.pool.fetchval(
                "SELECT EXISTS(SELECT 1 FROM revoked_sessions WHERE session_id = $1)",
                session_id,
            )
        )
