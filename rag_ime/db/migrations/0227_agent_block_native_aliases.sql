-- Exact native transcript identities are bound only by the original producer event.
-- No legacy content/timestamp matching or inferred backfill.
CREATE TABLE IF NOT EXISTS agent_block_native_aliases (
    session_id TEXT NOT NULL CHECK (length(session_id) > 0),
    native_pi_session_id TEXT NOT NULL DEFAULT '',
    native_message_id TEXT NOT NULL CHECK (length(native_message_id) > 0),
    source_message_id TEXT NOT NULL CHECK (length(source_message_id) > 0),
    generation INTEGER NOT NULL CHECK (generation >= 0),
    PRIMARY KEY (session_id, native_pi_session_id, native_message_id),
    UNIQUE (session_id, native_pi_session_id, source_message_id, generation),
    FOREIGN KEY (session_id, source_message_id, generation)
        REFERENCES agent_block_message_envelopes(session_id, message_id, generation)
);

CREATE INDEX IF NOT EXISTS idx_agent_block_native_aliases_source
    ON agent_block_native_aliases(session_id, source_message_id, generation);
