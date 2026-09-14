-- Encrypted GitHub connection ownership and explicit Session-scoped grants.
-- Credential material is sealed by TeamSecretVault before it reaches this
-- database; the secret blob is nullable so revocation can erase it.
CREATE TABLE IF NOT EXISTS team_connections (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL CHECK (provider = 'github'),
    scope TEXT NOT NULL CHECK (scope IN ('personal', 'project')),
    owner_user_id TEXT NOT NULL REFERENCES team_users(id) ON DELETE RESTRICT,
    space_id TEXT NOT NULL REFERENCES team_spaces(id) ON DELETE RESTRICT,
    label TEXT NOT NULL,
    account_login TEXT NOT NULL,
    repositories_json TEXT NOT NULL,
    operations_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'revoked', 'reconnect_required')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    secret_blob BLOB,
    secret_version INTEGER NOT NULL DEFAULT 1 CHECK (secret_version >= 1),
    refresh_state TEXT NOT NULL DEFAULT 'idle'
        CHECK (refresh_state IN ('idle', 'pending')),
    refresh_started_at_ms INTEGER,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_team_connections_scope
ON team_connections(scope, space_id, status, created_at_ms, id);

CREATE INDEX IF NOT EXISTS idx_team_connections_owner
ON team_connections(owner_user_id, scope, status, created_at_ms, id);

CREATE TABLE IF NOT EXISTS team_connection_grants (
    id TEXT PRIMARY KEY,
    connection_id TEXT NOT NULL REFERENCES team_connections(id) ON DELETE RESTRICT,
    session_id TEXT NOT NULL REFERENCES team_session_bindings(session_id) ON DELETE RESTRICT,
    space_id TEXT NOT NULL REFERENCES team_spaces(id) ON DELETE RESTRICT,
    actor_user_id TEXT NOT NULL REFERENCES team_users(id) ON DELETE RESTRICT,
    repository TEXT NOT NULL,
    operations_json TEXT NOT NULL,
    membership_revision INTEGER NOT NULL CHECK (membership_revision >= 1),
    connection_revision INTEGER NOT NULL CHECK (connection_revision >= 1),
    expires_at_ms INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'revoked', 'expired')),
    created_at_ms INTEGER NOT NULL,
    revoked_at_ms INTEGER
);

CREATE INDEX IF NOT EXISTS idx_team_connection_grants_session
ON team_connection_grants(session_id, status, expires_at_ms, created_at_ms, id);

CREATE INDEX IF NOT EXISTS idx_team_connection_grants_connection
ON team_connection_grants(connection_id, status, expires_at_ms);

CREATE INDEX IF NOT EXISTS idx_team_connection_grants_actor_space
ON team_connection_grants(actor_user_id, space_id, status, created_at_ms, id);
