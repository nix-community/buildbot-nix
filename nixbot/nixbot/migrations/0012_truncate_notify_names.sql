-- pg_notify payloads are limited to ~8000 bytes; attribute and effect
-- names are repo-controlled, so a near-limit name made every insert or
-- update of that row fail. Truncate the name in the payload: listeners
-- only use it as a change hint and refetch current state by id.

CREATE OR REPLACE FUNCTION notify_attribute_status() RETURNS trigger AS $$
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
        'attr', left(NEW.attr, 256),
        'status', NEW.status)::text);
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION notify_effect_status() RETURNS trigger AS $$
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
        'effect', left(NEW.name, 256),
        'status', NEW.status)::text);
    RETURN NEW;
END $$ LANGUAGE plpgsql;
