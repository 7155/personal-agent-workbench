-- Keep 0216 receipts untouched; cancellation can bind a prompt before its ACK.
ALTER TABLE agent_gateway_requests ADD COLUMN turn_id TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_gateway_requests ADD COLUMN client_message_id TEXT NOT NULL DEFAULT '';
CREATE INDEX agent_gateway_requests_admission
    ON agent_gateway_requests(session_id, client_message_id, state, turn_id);
