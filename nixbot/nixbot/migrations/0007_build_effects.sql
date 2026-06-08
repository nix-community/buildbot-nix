-- Hercules-style effects get their own rows: they are not attributes
-- (failures stay non-fatal; no gcroots/outputs/caching).
CREATE TABLE build_effects (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    build_id BIGINT NOT NULL REFERENCES builds (id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    -- running | succeeded | failed
    status TEXT NOT NULL DEFAULT 'running',
    error TEXT,
    -- zstd log file relative to the state dir; the logs table
    -- cannot be used without an attribute row.
    log_path TEXT,
    log_size BIGINT NOT NULL DEFAULT 0,
    log_truncated BOOLEAN NOT NULL DEFAULT FALSE,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    UNIQUE (build_id, name)
);

CREATE INDEX build_effects_build_idx ON build_effects (build_id);

-- Status changes push to the frontend like build/attribute changes
-- do (0005); without this the Effects section never updates live.
CREATE FUNCTION notify_effect_status() RETURNS trigger AS $$
DECLARE
    pid BIGINT;
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.status IS NOT DISTINCT FROM NEW.status THEN
        RETURN NEW;
    END IF;
    SELECT project_id INTO pid FROM builds WHERE id = NEW.build_id;
    PERFORM pg_notify('build_events', json_build_object(
        'build_id', NEW.build_id,
        'project_id', pid,
        'effect', NEW.name,
        'status', NEW.status)::text);
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER build_effects_status_notify
AFTER INSERT OR UPDATE ON build_effects
FOR EACH ROW EXECUTE FUNCTION notify_effect_status();
