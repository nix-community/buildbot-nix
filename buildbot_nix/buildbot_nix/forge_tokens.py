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
