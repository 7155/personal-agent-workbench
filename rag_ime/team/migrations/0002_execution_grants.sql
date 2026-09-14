CREATE TABLE team_session_bindings (
    session_id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL REFERENCES team_spaces(id),
    owner_user_id TEXT NOT NULL REFERENCES team_users(id),
    membership_revision INTEGER NOT NULL,
    workspace_path TEXT NOT NULL,
    audience TEXT NOT NULL DEFAULT 'owner' CHECK (audience IN ('owner', 'project')),
    generation INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at_ms INTEGER NOT NULL
);
CREATE INDEX team_session_bindings_owner ON team_session_bindings(space_id, owner_user_id);
CREATE TABLE team_execution_attempts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES team_session_bindings(session_id),
    generation INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at_ms INTEGER NOT NULL,
    revoked_at_ms INTEGER,
    created_at_ms INTEGER NOT NULL,
    UNIQUE(session_id, generation)
);
