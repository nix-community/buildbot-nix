-- Execution history for onSchedule effects: they have no build row,
-- so build_effects cannot carry them.
CREATE TABLE scheduled_effect_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    schedule_name TEXT NOT NULL,
    effect TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    error TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);

CREATE INDEX scheduled_effect_runs_project_idx
    ON scheduled_effect_runs (project_id, started_at DESC);
