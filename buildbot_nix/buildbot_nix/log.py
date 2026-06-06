"""Structured logging for the engine.

Emits one JSON object per line on stderr so journald/log shippers can
parse fields without fragile regex. Extra fields passed via
`logger.info("msg", extra={"build_id": 42})` are included verbatim.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

# Attributes present on every LogRecord; everything else is user-supplied
# via `extra=` and gets serialized into the JSON line.
_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        entry.update(
            {
                key: value
                for key, value in record.__dict__.items()
                if key not in _RESERVED
            }
        )
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def setup_logging(level: str = "info", *, json_format: bool = True) -> None:
    handler = logging.StreamHandler(sys.stderr)
    if json_format:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
