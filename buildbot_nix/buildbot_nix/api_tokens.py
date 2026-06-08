"""Personal API tokens.

Tokens are generated in the UI after login, shown exactly once, and
stored only as SHA-256 hashes. Optional expiry; revocation deletes the
row and takes effect immediately; expired rows are pruned
opportunistically. A valid token authenticates as its owner for both
read and control API usage.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .auth import User

if TYPE_CHECKING:
    import asyncpg

TOKEN_PREFIX = "bnix_"  # noqa: S105


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class TokenInfo:
    id: int
    name: str
    created_at: datetime
    expires_at: datetime | None


class ApiTokenStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def create(
        self, user: User, name: str, expires_at: datetime | None = None
    ) -> str:
        """Returns the plaintext token — the only time it is visible."""
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        await self.pool.execute(
            """
            INSERT INTO api_tokens (user_qualified, name, token_hash, expires_at,
                                    groups)
            VALUES ($1, $2, $3, $4, $5)
            """,
            user.qualified,
            name,
            _hash(token),
            expires_at,
            list(user.groups),
        )
        return token

    async def _prune_expired(self) -> None:
        await self.pool.execute("DELETE FROM api_tokens WHERE expires_at < now()")

    async def authenticate(self, token: str) -> User | None:
        if not token.startswith(TOKEN_PREFIX):
            return None
        await self._prune_expired()
        # No constant-time comparison needed: the row is looked up BY
        # the hash of the presented token, so a timing side channel
        # could only leak information about hashes the attacker already
        # computed themselves.
        row = await self.pool.fetchrow(
            "SELECT user_qualified, expires_at, groups "
            "FROM api_tokens WHERE token_hash = $1",
            _hash(token),
        )
        if row is None:
            return None
        if row["expires_at"] is not None and row["expires_at"] < datetime.now(tz=UTC):
            return None
        provider, _, username = row["user_qualified"].rpartition(":")
        return User(provider=provider, username=username, groups=tuple(row["groups"]))

    async def list_for(self, user: User) -> list[TokenInfo]:
        # Keeps /settings free of long-expired tokens.
        await self._prune_expired()
        rows = await self.pool.fetch(
            "SELECT id, name, created_at, expires_at FROM api_tokens "
            "WHERE user_qualified = $1 ORDER BY id",
            user.qualified,
        )
        return [
            TokenInfo(
                id=row["id"],
                name=row["name"],
                created_at=row["created_at"],
                expires_at=row["expires_at"],
            )
            for row in rows
        ]

    async def revoke(self, user: User, token_id: int) -> bool:
        """Immediate revocation; only the owner may revoke."""
        result = await self.pool.fetchval(
            "DELETE FROM api_tokens WHERE id = $1 AND user_qualified = $2 RETURNING id",
            token_id,
            user.qualified,
        )
        return result is not None
