-- Thin references to original owned Session commands, never another Runtime.
CREATE TABLE agent_coordinator_work (
    work_id TEXT PRIMARY KEY,
    coordinator_id TEXT NOT NULL,
    source_session_id TEXT NOT NULL,
    target_session_id TEXT NOT NULL,
    task TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);
CREATE INDEX agent_coordinator_work_source
ON agent_coordinator_work(source_session_id, created_at_ms, work_id);

-- Keep original-command tombstones even if a Source/target is later deleted.
CREATE TABLE agent_coordinator_work_attempts (
    attempt_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES agent_coordinator_work(work_id),
    target_session_id TEXT NOT NULL,
    client_message_id TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    retry_of_client_message_id TEXT NOT NULL DEFAULT '',
    turn_id TEXT NOT NULL DEFAULT '',
    acceptance_json TEXT NOT NULL DEFAULT '{}',
    terminal_refs_json TEXT NOT NULL DEFAULT '[]',
    context_item_id TEXT NOT NULL DEFAULT '',
    projected_at_ms INTEGER,
    retired_reason TEXT NOT NULL DEFAULT '',
    last_error_code TEXT NOT NULL DEFAULT '',
    checked_at_ms INTEGER NOT NULL DEFAULT 0,
    created_at_ms INTEGER NOT NULL,
    UNIQUE(target_session_id, client_message_id)
);
CREATE INDEX agent_coordinator_attempt_harvest
ON agent_coordinator_work_attempts(projected_at_ms, retired_reason, checked_at_ms, created_at_ms, attempt_id);
