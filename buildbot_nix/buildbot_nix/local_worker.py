"""Nix-specific worker implementations."""

from __future__ import annotations

from typing import Any

from buildbot.plugins import worker
from buildbot.util.twisted import async_to_deferred


class NixLocalWorker(worker.LocalWorker):
    """LocalWorker with increased max_line_length for nix-eval-jobs output."""

    @async_to_deferred
    async def reconfigService(self, name: str, **kwargs: Any) -> None:
        # First do the normal reconfiguration
        await super().reconfigService(name, **kwargs)
        self.remote_worker.bot.max_line_length = 10485760  # 10MB
