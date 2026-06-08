"""Deduplicated live eval warnings.

Evaluator stderr lines (warnings/errors) are normalized into stable
group keys so retry spam ("retrying in 120081 ms (attempt 2/5)" for
hundreds of narinfo URLs) collapses into one row with a counter that
the build page renders while the eval is still running.
"""

from __future__ import annotations

import re

# Volatile parts that would defeat deduplication: per-path URLs (keep
# the host), retry timers/attempt counters, nix base32 hashes.
_URL_RE = re.compile(r"(https?://[^/'\"\s)]+)[^'\"\s)]*")
_RETRY_RE = re.compile(r";? retrying in \d+ ms")
_ATTEMPT_RE = re.compile(r"\s*\(attempt \d+/\d+\)")
_NIX_HASH_RE = re.compile(r"\b[0-9a-df-np-sv-z]{32}\b")
_PREFIX_RE = re.compile(r"^(?:evaluation warning:|warning:|error:)\s*")
_WS_RE = re.compile(r"\s+")

MAX_GROUPS = 50
# Warning text is repo-controlled; keep single messages bounded.
MAX_MESSAGE_LEN = 500


def normalize_warning(line: str) -> tuple[str, str]:
    """(level, dedupe message) for one ANSI-stripped stderr line."""
    level = "error" if "error:" in line.split("warning:", maxsplit=1)[0] else "warning"
    msg = _PREFIX_RE.sub("", line.strip())
    msg = _URL_RE.sub(r"\1/…", msg)
    msg = _RETRY_RE.sub("", msg)
    msg = _ATTEMPT_RE.sub("", msg)
    msg = _NIX_HASH_RE.sub("…", msg)
    msg = _WS_RE.sub(" ", msg).strip()
    if len(msg) > MAX_MESSAGE_LEN:
        msg = msg[: MAX_MESSAGE_LEN - 1] + "…"
    return level, msg


class LiveWarningAggregator:
    """Counts occurrences per normalized warning; bounded group count."""

    def __init__(self, max_groups: int = MAX_GROUPS) -> None:
        self._groups: dict[tuple[str, str], int] = {}
        self._max_groups = max_groups

    def add(self, line: str) -> bool:
        """Record a line; True when the snapshot changed."""
        key = normalize_warning(line)
        if key in self._groups:
            self._groups[key] += 1
            return True
        if len(self._groups) >= self._max_groups:
            return False
        self._groups[key] = 1
        return True

    def __bool__(self) -> bool:
        return bool(self._groups)

    def snapshot(self) -> list[dict[str, str | int]]:
        """Errors first, then insertion order."""
        items = sorted(
            self._groups.items(), key=lambda kv: 0 if kv[0][0] == "error" else 1
        )
        return [
            {"level": level, "message": message, "count": count}
            for (level, message), count in items
        ]
