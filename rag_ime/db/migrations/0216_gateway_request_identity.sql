-- Additive admission ledger; historical approvals and Session transcripts stay intact.
CREATE TABLE agent_gateway_requests (
    session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
    tool_call_id TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('admitted', 'completed', 'unknown')),
    response_json TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY(session_id, tool_call_id)
);
CREATE TABLE agent_gateway_cancelled_turns (
    session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL,
    cancelled_at_ms INTEGER NOT NULL,
    PRIMARY KEY(session_id, turn_id)
);
