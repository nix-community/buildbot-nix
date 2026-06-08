-- Initial engine schema: projects, builds, build_attributes, log metadata,
-- failed-build cache, failed-status records.

CREATE TABLE projects (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- "github" or "gitea"
    forge TEXT NOT NULL,
    -- Stable forge repository ID; survives renames/transfers.
    forge_repo_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    default_branch TEXT NOT NULL,
    url TEXT NOT NULL,
    private BOOLEAN NOT NULL DEFAULT FALSE,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    -- Per-project sequential build number counter.
    next_build_number BIGINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (forge, forge_repo_id)
);

CREATE INDEX projects_owner_name_idx ON projects (owner, name);

CREATE TABLE builds (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    -- Per-project sequential number used in URLs.
    number BIGINT NOT NULL,
    -- Post-merge git tree hash; build identity across contexts.
    tree_hash TEXT,
    commit_sha TEXT NOT NULL,
    branch TEXT NOT NULL,
    pr_number BIGINT,
    -- Forge identity of the PR author (e.g. "github:alice"); used for
    -- the PR-author restart/cancel authz rule.
    pr_author TEXT,
    -- pending | evaluating | building | succeeded | failed | cancelled
    status TEXT NOT NULL DEFAULT 'pending',
    -- Monotonic generation for forge status posts; stale posts dropped.
    status_generation BIGINT NOT NULL DEFAULT 0,
    -- Effects started-flag: never auto-re-run effects on recovery.
    effects_started BOOLEAN NOT NULL DEFAULT FALSE,
    eval_warnings JSONB,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    UNIQUE (project_id, number)
);

CREATE INDEX builds_project_branch_idx ON builds (project_id, branch);
CREATE INDEX builds_tree_hash_idx ON builds (tree_hash);
CREATE INDEX builds_status_idx ON builds (status);

CREATE TABLE build_attributes (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    build_id BIGINT NOT NULL REFERENCES builds (id) ON DELETE CASCADE,
    -- Flake attribute name, e.g. "checks.x86_64-linux.foo".
    attr TEXT NOT NULL,
    system TEXT,
    drv_path TEXT,
    -- Output name -> store path (null when not statically known).
    outputs JSONB,
    -- pending | building | succeeded | failed | cancelled | skipped_local
    -- | dependency_failed | cached_failure | failed_eval
    status TEXT NOT NULL DEFAULT 'pending',
    -- True when the result came from a cache/substituter or local store.
    cached BOOLEAN NOT NULL DEFAULT FALSE,
    error TEXT,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    UNIQUE (build_id, attr)
);

CREATE INDEX build_attributes_build_idx ON build_attributes (build_id);
CREATE INDEX build_attributes_attr_idx ON build_attributes (attr);
CREATE INDEX build_attributes_drv_path_idx ON build_attributes (drv_path);

CREATE TABLE logs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    attribute_id BIGINT NOT NULL REFERENCES build_attributes (id) ON DELETE CASCADE,
    -- Path of the zstd-compressed log file relative to the log directory.
    path TEXT NOT NULL,
    size_bytes BIGINT NOT NULL DEFAULT 0,
    truncated BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX logs_attribute_idx ON logs (attribute_id);

-- Failed-build cache (opt-in via cacheFailedBuilds); columns ported from
-- the buildbot-era db/failed_builds.py.
CREATE TABLE failed_builds (
    derivation TEXT PRIMARY KEY,
    timestamp DOUBLE PRECISION NOT NULL,
    url TEXT NOT NULL
);

-- Per-revision failed commit statuses needing a success-flip on rebuild;
-- ported from db/failed_status.py.
CREATE TABLE failed_statuses (
    revision TEXT NOT NULL,
    status_name TEXT NOT NULL,
    timestamp DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (revision, status_name)
);
