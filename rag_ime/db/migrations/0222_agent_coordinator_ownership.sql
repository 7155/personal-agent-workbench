-- Separate global coordinator; existing read-only primary discussions are unchanged.
CREATE TABLE agent_coordinators (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    coordinator_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL UNIQUE,
    created_at_ms INTEGER NOT NULL
);
-- Deliberately retain tombstones after target deletion: an uncertain retry must
-- never create a replacement target under the same request identity.
CREATE TABLE agent_coordinator_objects (
    target_id TEXT PRIMARY KEY,
    target_kind TEXT NOT NULL CHECK (target_kind IN ('session', 'room')),
    coordinator_id TEXT NOT NULL,
    source_session_id TEXT NOT NULL,
    client_request_id TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL,
    task TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);
CREATE INDEX agent_coordinator_objects_source ON agent_coordinator_objects(source_session_id, created_at_ms DESC);
