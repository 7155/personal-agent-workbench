-- One automatic Source admission per original P1 result. No executor/ACK owner.
CREATE TABLE agent_coordinator_result_deliveries (
    delivery_id TEXT PRIMARY KEY,
    coordinator_id TEXT NOT NULL,
    source_session_id TEXT NOT NULL,
    target_session_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL UNIQUE,
    context_item_id TEXT NOT NULL UNIQUE,
    result_sha256 TEXT NOT NULL,
    source_client_message_id TEXT NOT NULL UNIQUE,
    phase TEXT NOT NULL CHECK (phase IN ('pending','reserved','uncertain','accepted','cancelled','retired')),
    envelope_json TEXT NOT NULL DEFAULT '',
    envelope_sha256 TEXT NOT NULL DEFAULT '',
    context_item_ids_json TEXT NOT NULL DEFAULT '[]',
    source_turn_id TEXT NOT NULL DEFAULT '',
    acceptance_json TEXT NOT NULL DEFAULT '{}',
    terminal_refs_json TEXT NOT NULL DEFAULT '[]',
    retired_reason TEXT NOT NULL DEFAULT '',
    last_error_code TEXT NOT NULL DEFAULT '',
    created_at_ms INTEGER NOT NULL,
    checked_at_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX agent_coordinator_delivery_pending
ON agent_coordinator_result_deliveries(phase, created_at_ms, delivery_id);

-- An original occurrence/client/envelope can never be transferred or rebuilt.
CREATE TRIGGER agent_coordinator_delivery_identity_immutable
BEFORE UPDATE ON agent_coordinator_result_deliveries
WHEN NEW.delivery_id<>OLD.delivery_id OR NEW.coordinator_id<>OLD.coordinator_id
  OR NEW.source_session_id<>OLD.source_session_id OR NEW.target_session_id<>OLD.target_session_id
  OR NEW.attempt_id<>OLD.attempt_id OR NEW.context_item_id<>OLD.context_item_id
  OR NEW.result_sha256<>OLD.result_sha256 OR NEW.source_client_message_id<>OLD.source_client_message_id
  OR (OLD.envelope_sha256<>'' AND (NEW.envelope_json<>OLD.envelope_json
      OR NEW.envelope_sha256<>OLD.envelope_sha256 OR NEW.context_item_ids_json<>OLD.context_item_ids_json))
  OR (OLD.source_turn_id<>'' AND NEW.source_turn_id<>OLD.source_turn_id)
BEGIN SELECT RAISE(ABORT, 'original coordinator delivery is immutable'); END;

CREATE TRIGGER agent_coordinator_delivery_append_only
BEFORE DELETE ON agent_coordinator_result_deliveries
BEGIN SELECT RAISE(ABORT, 'original coordinator delivery is append-only'); END;
