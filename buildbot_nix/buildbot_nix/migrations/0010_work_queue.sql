-- Work queue; semantics documented in work_queue.py.
CREATE TABLE work_queue (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- change | restart | effects | scheduled
    kind TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}',
    -- pending | running | done | failed
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX work_queue_pending_uniq
    ON work_queue (kind, dedup_key, md5(payload::text))
    WHERE status = 'pending';
-- Enforces per-key serialization against concurrent claimers; the
-- NOT EXISTS in claim_next alone races under snapshot isolation.
CREATE UNIQUE INDEX work_queue_running_uniq
    ON work_queue (dedup_key) WHERE status = 'running';
CREATE INDEX work_queue_pending_idx
    ON work_queue (created_at) WHERE status IN ('pending', 'running');

CREATE FUNCTION notify_work_queue() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('work_queue', NEW.id::text);
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER work_queue_notify
AFTER INSERT ON work_queue
FOR EACH ROW EXECUTE FUNCTION notify_work_queue();
