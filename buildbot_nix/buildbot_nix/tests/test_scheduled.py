"""Tests for scheduled effects: parsing, cron matching, persistence."""

# ruff: noqa: PLR2004, S106 (test literals; secret_name is a credential id)
from __future__ import annotations

import asyncio
import os
import shutil
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import asyncpg
import pytest

from buildbot_nix.config import ScheduleWhen
from buildbot_nix.effects import EffectsContext
from buildbot_nix.scheduled import (
    ScheduledEffectsStore,
    due_occurrence,
    is_due,
    parse_schedules_from_json,
    run_scheduled_effect,
)

from .support import ephemeral_postgres

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def test_parse_schedules() -> None:
    schedules = parse_schedules_from_json(
        {
            "nightly": {
                "when": {"minute": 30, "hour": 2, "dayOfWeek": ["Mon", "Fri"]},
                "effects": ["deploy", "backup"],
            },
            "monthly": {"when": {"dayOfMonth": [1]}, "effects": ["report"]},
        }
    )
    assert schedules["nightly"].when.minute == 30
    assert schedules["nightly"].effects == ["deploy", "backup"]
    assert schedules["monthly"].when.dayOfMonth == [1]


def test_is_due_exact() -> None:
    when = ScheduleWhen(minute=30, hour=2)
    assert is_due(when, "s", datetime(2026, 6, 5, 2, 30, tzinfo=UTC))
    assert not is_due(when, "s", datetime(2026, 6, 5, 2, 31, tzinfo=UTC))
    assert not is_due(when, "s", datetime(2026, 6, 5, 3, 30, tzinfo=UTC))


def test_is_due_hour_list_and_days() -> None:
    when = ScheduleWhen(minute=0, hour=[6, 18], dayOfWeek=["Mon"])
    monday = datetime(2026, 6, 1, 6, 0, tzinfo=UTC)  # a Monday
    tuesday = datetime(2026, 6, 2, 6, 0, tzinfo=UTC)
    assert is_due(when, "s", monday)
    assert not is_due(when, "s", tuesday)
    assert is_due(when, "s", monday.replace(hour=18))
    assert not is_due(when, "s", monday.replace(hour=12))

    monthly = ScheduleWhen(minute=0, hour=0, dayOfMonth=[15])
    assert is_due(monthly, "s", datetime(2026, 6, 15, 0, 0, tzinfo=UTC))
    assert not is_due(monthly, "s", datetime(2026, 6, 16, 0, 0, tzinfo=UTC))


def test_deterministic_defaults() -> None:
    when = ScheduleWhen()
    fields1 = when.resolved("nightly")
    assert fields1 == when.resolved("nightly")
    assert fields1["minute"] != when.resolved("weekly")["minute"]
    assert fields1["hour"] == list(range(24))
    for hour in (0, 13, 23):
        due_at = datetime(2026, 6, 5, hour, fields1["minute"], tzinfo=UTC)
        assert is_due(when, "nightly", due_at)


# --- persistence -------------------------------------------------------------

pytestmark_pg = pytest.mark.skipif(
    shutil.which("initdb") is None, reason="postgresql not available"
)


@pytest.fixture(scope="module")
def postgres_dsn(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    if shutil.which("initdb") is None:
        pytest.skip("postgresql not available")
    with ephemeral_postgres(tmp_path_factory, "sched") as dsn:
        yield dsn


@pytestmark_pg
def test_store_roundtrip_and_due(postgres_dsn: str) -> None:
    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            project_id = await pool.fetchval(
                """
                INSERT INTO projects (forge, forge_repo_id, owner, name,
                                      default_branch, url)
                VALUES ('github', 'sched-1', 'acme', 'widget', 'main', 'u')
                RETURNING id
                """
            )
            store = ScheduledEffectsStore(pool)
            schedules = parse_schedules_from_json(
                {
                    "nightly": {
                        "when": {"minute": 30, "hour": 2},
                        "effects": ["deploy"],
                    }
                }
            )
            await store.replace_schedules(project_id, schedules)

            due_time = datetime(2026, 6, 5, 2, 30, tzinfo=UTC)
            due = await store.due_effects(due_time)
            assert len(due) == 1
            assert due[0].schedule_name == "nightly"
            assert due[0].effect == "deploy"

            # Not due at other times (outside the sweep window).
            assert await store.due_effects(due_time.replace(minute=36)) == []

            # Marked as run: not due again in the same minute.
            await store.mark_run(due[0], due_time)
            assert await store.due_effects(due_time) == []
            # Due again the next day.
            next_day = due_time.replace(day=6)
            assert len(await store.due_effects(next_day)) == 1

            # Re-discovery replaces the schedule set.
            await store.replace_schedules(project_id, {})
            assert await store.due_effects(due_time.replace(day=7)) == []
        finally:
            await pool.close()

    asyncio.run(run())


def test_due_occurrence_window() -> None:
    # The sweep loop drifts past minute boundaries; occurrences within
    # the window must still be found.
    when = ScheduleWhen(minute=30, hour=2)
    occ = due_occurrence(when, "s", datetime(2026, 6, 5, 2, 33, 10, tzinfo=UTC))
    assert occ == datetime(2026, 6, 5, 2, 30, tzinfo=UTC)
    assert due_occurrence(when, "s", datetime(2026, 6, 5, 2, 36, tzinfo=UTC)) is None
    assert due_occurrence(when, "s", datetime(2026, 6, 5, 2, 29, tzinfo=UTC)) is None


def test_run_scheduled_effect_passes_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "effects-secret").write_text('{"token": "s3"}')
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))

    bindir = tmp_path / "bin"
    bindir.mkdir()
    record = tmp_path / "record"
    script = bindir / "buildbot-effects"
    script.write_text(
        "#!/bin/sh\n"
        f'out="{record}"\n'
        'echo "$@" > "$out"\n'
        "while [ $# -gt 0 ]; do\n"
        '  if [ "$1" = --secrets ]; then cat "$2" >> "$out"; fi\n'
        "  shift\n"
        "done\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

    worktree = tmp_path / "checkout"
    worktree.mkdir()
    ctx = EffectsContext(
        worktree_path=worktree,
        rev="deadbeef",
        branch="main",
        repo="acme/widget",
        secret_name="effects-secret",
    )
    assert asyncio.run(run_scheduled_effect(ctx, "nightly", "deploy"))
    recorded = record.read_text()
    assert "--secrets" in recorded
    assert '{"token": "s3"}' in recorded
    # The secrets file is removed after the run.
    assert not list(tmp_path.glob("checkout-secrets.json"))


@pytestmark_pg
def test_due_effects_window_and_bad_spec(postgres_dsn: str) -> None:
    async def run() -> None:
        pool = await asyncpg.create_pool(postgres_dsn)
        try:
            project_id = await pool.fetchval(
                """
                INSERT INTO projects (forge, forge_repo_id, owner, name,
                                      default_branch, url)
                VALUES ('github', 'sched-2', 'acme', 'gadget', 'main', 'u')
                RETURNING id
                """
            )
            store = ScheduledEffectsStore(pool)
            schedules = parse_schedules_from_json(
                {
                    "nightly": {
                        "when": {"minute": 30, "hour": 2},
                        "effects": ["deploy"],
                    },
                    # Repo-controlled misspelling: must not abort the sweep.
                    "broken": {
                        "when": {"minute": 0, "hour": 0, "dayOfWeek": ["monday"]},
                        "effects": ["report"],
                    },
                }
            )
            await store.replace_schedules(project_id, schedules)

            # The sweep drifted past 2:30: the occurrence is still found,
            # and the malformed schedule is skipped instead of raising.
            late = datetime(2026, 6, 5, 2, 31, 40, tzinfo=UTC)
            due = [
                d for d in await store.due_effects(late) if d.project_id == project_id
            ]
            assert [d.schedule_name for d in due] == ["nightly"]

            # Marked as run at the sweep time: the same occurrence does
            # not fire again on the next sweep.
            await store.mark_run(due[0], late)
            assert [
                d
                for d in await store.due_effects(late.replace(minute=32))
                if d.project_id == project_id
            ] == []
        finally:
            await pool.close()

    asyncio.run(run())


def test_run_scheduled_effect_secret_read_failure_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Runs in a fire-and-forget task: a missing CREDENTIALS_DIRECTORY
    # must be reported as failure, not raised.
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    ctx = EffectsContext(
        worktree_path=tmp_path,
        rev="deadbeef",
        branch="main",
        repo="acme/widget",
        secret_name="effects-secret",
    )
    assert not asyncio.run(run_scheduled_effect(ctx, "nightly", "deploy"))
