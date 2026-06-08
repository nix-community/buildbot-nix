-- Scheduled Hercules effects (onSchedule): discovered on default-branch
-- builds via `nixbot-effects list-schedules`, persisted here, executed
-- by the engine's schedule loop via `run-scheduled`.

CREATE TABLE scheduled_effects (
    project_id BIGINT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    schedule_name TEXT NOT NULL,
    effect TEXT NOT NULL,
    -- Raw `when` spec from the flake (minute/hour/dayOfWeek/dayOfMonth).
    when_spec JSONB NOT NULL,
    last_run TIMESTAMPTZ,
    PRIMARY KEY (project_id, schedule_name, effect)
);
