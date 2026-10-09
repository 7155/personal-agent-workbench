-- 0225 is reserved for the independent Wake admission candidate. Room
-- identities remain typed; no Room ID is stored in a Session target column.
CREATE TABLE agent_coordinator_room_work_attempts (
    attempt_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL UNIQUE,
    coordinator_id TEXT NOT NULL,
    source_session_id TEXT NOT NULL,
    target_room_id TEXT NOT NULL,
    client_message_id TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    task TEXT NOT NULL,
    root_id TEXT NOT NULL DEFAULT '',
    acceptance_json TEXT NOT NULL DEFAULT '{}',
    terminal_refs_json TEXT NOT NULL DEFAULT '[]',
    context_item_id TEXT NOT NULL DEFAULT '',
    projected_at_ms INTEGER,
    retired_reason TEXT NOT NULL DEFAULT '',
    last_error_code TEXT NOT NULL DEFAULT '',
    checked_at_ms INTEGER NOT NULL DEFAULT 0,
    created_at_ms INTEGER NOT NULL,
    UNIQUE(target_room_id, client_message_id)
);
CREATE INDEX agent_coordinator_room_attempt_harvest
ON agent_coordinator_room_work_attempts(projected_at_ms, retired_reason, checked_at_ms, created_at_ms, attempt_id);

CREATE TRIGGER agent_coordinator_room_attempt_identity_immutable
BEFORE UPDATE ON agent_coordinator_room_work_attempts
WHEN NEW.attempt_id<>OLD.attempt_id OR NEW.work_id<>OLD.work_id
  OR NEW.coordinator_id<>OLD.coordinator_id OR NEW.source_session_id<>OLD.source_session_id
  OR NEW.target_room_id<>OLD.target_room_id OR NEW.client_message_id<>OLD.client_message_id
  OR NEW.request_sha256<>OLD.request_sha256 OR NEW.task<>OLD.task
  OR (OLD.root_id<>'' AND NEW.root_id<>OLD.root_id)
BEGIN SELECT RAISE(ABORT, 'original coordinator Room attempt is immutable'); END;
CREATE TRIGGER agent_coordinator_room_attempt_append_only
BEFORE DELETE ON agent_coordinator_room_work_attempts
BEGIN SELECT RAISE(ABORT, 'original coordinator Room attempt is append-only'); END;

-- Share the original Source delivery/admission index, not an additional loop.
ALTER TABLE agent_coordinator_result_deliveries ADD COLUMN target_kind TEXT NOT NULL DEFAULT 'session'
CHECK (target_kind IN ('session', 'room'));
ALTER TABLE agent_coordinator_result_deliveries ADD COLUMN target_room_id TEXT NOT NULL DEFAULT ''
CHECK ((target_kind='session' AND target_room_id='')
    OR (target_kind='room' AND target_session_id='' AND target_room_id<>''));
CREATE TRIGGER agent_coordinator_delivery_room_identity_immutable
BEFORE UPDATE ON agent_coordinator_result_deliveries
WHEN NEW.target_kind<>OLD.target_kind OR NEW.target_room_id<>OLD.target_room_id
BEGIN SELECT RAISE(ABORT, 'original coordinator delivery target is immutable'); END;
