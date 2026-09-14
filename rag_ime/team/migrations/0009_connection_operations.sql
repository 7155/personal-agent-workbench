-- Durable effect receipts never contain credentials or submitted issue bodies.
-- An unfinished operation remains uncertain after restart and is not resent.
CREATE TABLE team_connection_operations (
    session_id TEXT NOT NULL REFERENCES team_session_bindings(session_id),
    request_id TEXT NOT NULL,
    grant_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    arguments_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'succeeded', 'rejected', 'unknown')),
    result_json TEXT,
    error_code TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY (session_id, request_id)
);
