-- Replace end-of-eval warning text with warnings streamed during the
-- eval, deduplicated into [{"level", "message", "count"}] groups.
ALTER TABLE builds DROP COLUMN eval_warnings;
ALTER TABLE builds ADD COLUMN eval_warnings JSONB;

-- The build page listens on build_events to refresh; without this the
-- warnings panel would only update on status transitions.
CREATE OR REPLACE FUNCTION notify_build_status() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE'
        AND OLD.status IS NOT DISTINCT FROM NEW.status
        AND OLD.eval_warnings IS NOT DISTINCT FROM NEW.eval_warnings THEN
        RETURN NEW;
    END IF;
    PERFORM pg_notify('build_events', json_build_object(
        'build_id', NEW.id,
        'project_id', NEW.project_id,
        'status', NEW.status)::text);
    RETURN NEW;
END $$ LANGUAGE plpgsql;
