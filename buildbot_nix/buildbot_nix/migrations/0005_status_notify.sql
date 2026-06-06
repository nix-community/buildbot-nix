-- Push status changes to the web frontend over LISTEN/NOTIFY.
-- Triggers catch every write site (the engine updates statuses from
-- several places), so the frontend needs no polling.

CREATE FUNCTION notify_build_status() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.status IS NOT DISTINCT FROM NEW.status THEN
        RETURN NEW;
    END IF;
    PERFORM pg_notify('build_events', json_build_object(
        'build_id', NEW.id,
        'project_id', NEW.project_id,
        'status', NEW.status)::text);
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER builds_status_notify
AFTER INSERT OR UPDATE ON builds
FOR EACH ROW EXECUTE FUNCTION notify_build_status();

CREATE FUNCTION notify_attribute_status() RETURNS trigger AS $$
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
        'attr', NEW.attr,
        'status', NEW.status)::text);
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER build_attributes_status_notify
AFTER INSERT OR UPDATE ON build_attributes
FOR EACH ROW EXECUTE FUNCTION notify_attribute_status();
