"""Scheduled Hercules effects (`onSchedule`), tasks 3.3.

Ported from scheduled.py + nix_eval.py:ScheduledEffectsEvaluateCommand:
on each successful default-branch build the orchestrator discovers
schedules via `buildbot-effects list-schedules` and persists them; a
periodic loop runs due effects via `buildbot-effects run-scheduled`.

Cron-like `when` specs; defaults resolve in
config.ScheduleWhen.resolved. All times are UTC.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .config import ScheduledEffectConfig, ScheduleWhen
from .effects import EffectsError, _effects_args, _read_secret_file, _run

if TYPE_CHECKING:
    from pathlib import Path

    import asyncpg

    from .effects import EffectsContext

logger = logging.getLogger(__name__)


def parse_schedules_from_json(
    schedules_json: dict[str, Any],
) -> dict[str, ScheduledEffectConfig]:
    """Parse `buildbot-effects list-schedules` output."""
    result: dict[str, ScheduledEffectConfig] = {}
    for schedule_name, schedule_data in schedules_json.items():
        when_data = schedule_data.get("when", {})
        result[schedule_name] = ScheduledEffectConfig(
            name=schedule_name,
            when=ScheduleWhen(
                minute=when_data.get("minute"),
                hour=when_data.get("hour"),
                dayOfWeek=when_data.get("dayOfWeek"),
                dayOfMonth=when_data.get("dayOfMonth"),
            ),
            effects=schedule_data.get("effects", []),
        )
    return result


async def discover_schedules(
    ctx: EffectsContext,
) -> dict[str, ScheduledEffectConfig]:
    """`buildbot-effects list-schedules` on a default-branch checkout."""
    returncode, output, errors = await _run(
        ["buildbot-effects", "list-schedules", *_effects_args(ctx, None)],
        ctx.worktree_path,
        time_limit=ctx.timeout,
    )
    if returncode != 0:
        msg = f"buildbot-effects list-schedules failed ({returncode}): {errors[-2000:]}"
        raise EffectsError(msg)
    if not output.strip():
        return {}
    try:
        return parse_schedules_from_json(json.loads(output))
    except json.JSONDecodeError as e:
        msg = f"failed to parse list-schedules output: {e}"
        raise EffectsError(msg) from e


def is_due(when: ScheduleWhen, schedule_name: str, now: datetime) -> bool:
    """Whether the schedule matches the current UTC minute."""
    fields = when.resolved(schedule_name)
    if now.minute != fields["minute"]:
        return False
    hour = fields["hour"]
    hours = hour if isinstance(hour, list) else [hour]
    if now.hour not in hours:
        return False
    if "dayOfWeek" in fields and now.weekday() not in fields["dayOfWeek"]:
        return False
    return not ("dayOfMonth" in fields and now.day not in fields["dayOfMonth"])


# How far back a sweep looks for a matching minute: the sweep loop
# sleeps 60s between iterations, so its sampling points drift and can
# skip a whole minute.
DUE_WINDOW_MINUTES = 5


def due_occurrence(
    when: ScheduleWhen, schedule_name: str, now: datetime
) -> datetime | None:
    """The most recent scheduled occurrence within the sweep window, or
    None when the schedule did not fire in that window."""
    for offset in range(DUE_WINDOW_MINUTES):
        candidate = (now - timedelta(minutes=offset)).replace(second=0, microsecond=0)
        if is_due(when, schedule_name, candidate):
            return candidate
    return None


async def run_scheduled_effect(
    ctx: EffectsContext, schedule_name: str, effect: str
) -> bool:
    """`buildbot-effects run-scheduled <schedule> <effect>` in a
    default-branch checkout. Returns success. Per-repo secrets are
    written next to the checkout for the duration of the run, like
    `effects.run_effect`."""
    secrets_file: Path | None = None
    if ctx.secret_name is not None:
        secrets_file = (
            ctx.worktree_path.parent / f"{ctx.worktree_path.name}-secrets.json"
        )
        try:
            secrets_file.write_text(_read_secret_file(ctx.secret_name))
        except (EffectsError, OSError):
            # Runs in a fire-and-forget task; an exception would only
            # surface as "Task exception was never retrieved".
            logger.exception(
                "failed to prepare effects secrets",
                extra={"schedule": schedule_name, "effect": effect},
            )
            return False
    try:
        returncode, output, errors = await _run(
            [
                "buildbot-effects",
                "run-scheduled",
                *_effects_args(ctx, secrets_file),
                schedule_name,
                effect,
            ],
            ctx.worktree_path,
            time_limit=ctx.timeout,
        )
    except EffectsError:
        logger.exception(
            "scheduled effect failed",
            extra={"schedule": schedule_name, "effect": effect},
        )
        return False
    finally:
        if secrets_file is not None:
            secrets_file.unlink(missing_ok=True)
    if returncode != 0:
        logger.error(
            "scheduled effect failed",
            extra={
                "schedule": schedule_name,
                "effect": effect,
                "output_tail": (output + errors)[-2000:],
            },
        )
    return returncode == 0


@dataclass(frozen=True)
class DueEffect:
    project_id: int
    schedule_name: str
    effect: str
    when: ScheduleWhen


class ScheduledEffectsStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def replace_schedules(
        self, project_id: int, schedules: dict[str, ScheduledEffectConfig]
    ) -> None:
        """Replace a project's schedules with the freshly discovered set."""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM scheduled_effects WHERE project_id = $1", project_id
            )
            for config in schedules.values():
                for effect in config.effects:
                    await conn.execute(
                        """
                        INSERT INTO scheduled_effects
                            (project_id, schedule_name, effect, when_spec)
                        VALUES ($1, $2, $3, $4::jsonb)
                        """,
                        project_id,
                        config.name,
                        effect,
                        config.when.model_dump_json(exclude_none=True),
                    )

    async def due_effects(self, now: datetime | None = None) -> list[DueEffect]:
        """Effects whose schedule fired within the sweep window and
        which have not run for that occurrence yet."""
        now = now or datetime.now(tz=UTC)
        rows = await self.pool.fetch(
            """
            SELECT project_id, schedule_name, effect, when_spec, last_run
            FROM scheduled_effects
            WHERE last_run IS NULL OR last_run < date_trunc('minute', $1::timestamptz)
            """,
            now,
        )
        due = []
        for row in rows:
            try:
                when = ScheduleWhen.model_validate(json.loads(row["when_spec"]))
                occurrence = due_occurrence(when, row["schedule_name"], now)
            except Exception:
                # when-specs are repo-controlled; one malformed spec
                # must not abort the sweep for all other projects.
                logger.exception(
                    "invalid schedule spec, skipping",
                    extra={
                        "project_id": row["project_id"],
                        "schedule": row["schedule_name"],
                    },
                )
                continue
            last_run = row["last_run"]
            if occurrence is not None and (last_run is None or last_run < occurrence):
                due.append(
                    DueEffect(
                        project_id=row["project_id"],
                        schedule_name=row["schedule_name"],
                        effect=row["effect"],
                        when=when,
                    )
                )
        return due

    async def mark_run(self, due: DueEffect, now: datetime | None = None) -> None:
        await self.pool.execute(
            """
            UPDATE scheduled_effects SET last_run = $4
            WHERE project_id = $1 AND schedule_name = $2 AND effect = $3
            """,
            due.project_id,
            due.schedule_name,
            due.effect,
            now or datetime.now(tz=UTC),
        )
