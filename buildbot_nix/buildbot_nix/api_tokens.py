"""Personal API tokens.

Tokens are generated in the UI after login, shown exactly once, and
stored only as SHA-256 hashes (lookup by hash makes the comparison
constant-time by construction; an additional hmac.compare_digest guards
the final check). Optional expiry; revocation deletes the row and takes
effect immediately. A valid token authenticates as its owner for both
read and control API usage.
"""

from __future__ import annotations

import hashlib
import hmac
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

    async def authenticate(self, token: str) -> User | None:
        if not token.startswith(TOKEN_PREFIX):
            return None
        token_hash = _hash(token)
        row = await self.pool.fetchrow(
            "SELECT user_qualified, token_hash, expires_at, groups "
            "FROM api_tokens WHERE token_hash = $1",
            token_hash,
        )
        if row is None:
            return None
        if not hmac.compare_digest(row["token_hash"], token_hash):
            return None
        if row["expires_at"] is not None and row["expires_at"] < datetime.now(tz=UTC):
            return None
        provider, _, username = row["user_qualified"].rpartition(":")
        return User(provider=provider, username=username, groups=tuple(row["groups"]))

    async def list_for(self, user: User) -> list[TokenInfo]:
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
