-- Scope the failed-build cache per project: a global derivation key
-- leaked one (possibly private) project's build URL into another
-- project's status descriptions and blocked unrelated projects'
-- identical derivations. Existing rows carry no project, so drop them
-- (it is only a cache).
DELETE FROM failed_builds;
ALTER TABLE failed_builds
    ADD COLUMN project_id BIGINT NOT NULL
        REFERENCES projects (id) ON DELETE CASCADE;
ALTER TABLE failed_builds DROP CONSTRAINT failed_builds_pkey;
ALTER TABLE failed_builds ADD PRIMARY KEY (project_id, derivation);
