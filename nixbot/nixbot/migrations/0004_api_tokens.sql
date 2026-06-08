-- Personal API tokens: stored hashed, shown once at creation,
-- optional expiry, immediate revocation by deletion.

CREATE TABLE api_tokens (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- Provider-qualified owner identity, e.g. "github:alice".
    user_qualified TEXT NOT NULL,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ
);

CREATE INDEX api_tokens_user_idx ON api_tokens (user_qualified);

-- Forge OAuth tokens captured at login, stored server-side and keyed
-- by an opaque session id: the session cookie is signed but not
-- encrypted, so the token itself must never be embedded in it.
CREATE TABLE forge_tokens (
    session_id TEXT PRIMARY KEY,
    token TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);
