-- Identity and links only: ordinary Session/Goal/Runtime owners still execute work.
CREATE TABLE agent_primary_assistants (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    assistant_id TEXT NOT NULL UNIQUE,
    created_at_ms INTEGER NOT NULL
);

CREATE TABLE agent_primary_session_links (
    -- Keep request identities after Session deletion, so retries cannot re-execute.
    session_id TEXT PRIMARY KEY,
    assistant_id TEXT NOT NULL REFERENCES agent_primary_assistants(assistant_id),
    kind TEXT NOT NULL CHECK (kind IN ('discussion', 'task')),
    project_key TEXT NOT NULL,
    client_request_id TEXT,
    source_session_id TEXT NOT NULL DEFAULT '',
    source_message_id TEXT NOT NULL DEFAULT '',
    authorization_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (assistant_id, client_request_id),
    CHECK ((kind = 'discussion' AND client_request_id IS NULL)
        OR (kind = 'task' AND client_request_id IS NOT NULL))
);

CREATE INDEX agent_primary_session_project_idx
    ON agent_primary_session_links(assistant_id, kind, project_key);
