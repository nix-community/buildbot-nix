-- Server-side session revocation: session cookies are stateless
-- (signed, verified without a database), so logout must record the
-- session id to make captured cookie copies unusable before expiry.
CREATE TABLE revoked_sessions (
    session_id TEXT PRIMARY KEY,
    -- When the underlying cookie expires anyway; rows past this are pruned.
    expires_at TIMESTAMPTZ NOT NULL
);
