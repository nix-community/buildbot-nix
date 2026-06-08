"""Race-condition tests: cancellation during retry, restart
recovery idempotence. Out-of-order supersede, shared-context
cancellation, and tree-hash dedup are covered in
test_canceller.py / test_orchestrator.py."""

# ruff: noqa: ARG001 (fake _run_once must match the real signature)
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from nixbot.executor import (
    BuildSettings,
    FairScheduler,
    LogWriter,
    NixBuildExecutor,
)
from nixbot.scheduler import BuildOutcome

from .support import mk_job

if TYPE_CHECKING:
    from pathlib import Path


def test_cancel_during_retry_window(tmp_path: Path) -> None:
    """Cancellation arriving between a transient failure and its retry:
    the retry must not fire and the attribute ends cancelled."""

    async def run() -> tuple[BuildOutcome, int]:
        executor = NixBuildExecutor(FairScheduler(1), BuildSettings(log_dir=tmp_path))
        cancel_event = asyncio.Event()
        attempts = 0

        async def fake_run_once(
            job: object, log_writer: object, cwd: object, cancel: asyncio.Event
        ) -> tuple[BuildOutcome, bool]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                # Transient failure; cancel lands while the retry is
                # being considered.
                cancel_event.set()
                return BuildOutcome.failure, True
            return BuildOutcome.success, False

        executor._run_once = fake_run_once  # type: ignore[assignment,method-assign]  # noqa: SLF001
        writer = LogWriter(path=tmp_path / "log.zst")
        outcome = await executor.build_attribute(
            "b", mk_job(), writer, tmp_path, cancel_event
        )
        await writer.close()
        return outcome, attempts

    outcome, attempts = asyncio.run(run())
    assert attempts == 1  # retry suppressed
    assert outcome == BuildOutcome.cancelled  # ends cancelled, not failed
