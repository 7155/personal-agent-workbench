-- Project-scoped preview deployments and short-lived browser access leases.
-- A ready deployment is the current active preview; older ready rows become
-- retained when a newer deployment is activated.
CREATE TABLE IF NOT EXISTS team_preview_deployments (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL REFERENCES team_spaces(id) ON DELETE CASCADE,
    branch TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    requirements_revision INTEGER NOT NULL CHECK (requirements_revision >= 0),
    client_request_id TEXT NOT NULL,
    requested_by_user_id TEXT NOT NULL REFERENCES team_users(id) ON DELETE RESTRICT,
    requested_by_membership_revision INTEGER NOT NULL CHECK (requested_by_membership_revision >= 0),
    requested_by_display_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'starting'
        CHECK (status IN ('starting', 'ready', 'retained', 'failed', 'stopped', 'recovery_required')),
    created_at_ms INTEGER NOT NULL,
    ready_at_ms INTEGER,
    error TEXT,
    updated_at_ms INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_team_preview_request
ON team_preview_deployments(space_id, client_request_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_team_preview_one_starting
ON team_preview_deployments(space_id)
WHERE status = 'starting';

CREATE UNIQUE INDEX IF NOT EXISTS idx_team_preview_one_ready
ON team_preview_deployments(space_id)
WHERE status = 'ready';

CREATE INDEX IF NOT EXISTS idx_team_preview_latest
ON team_preview_deployments(space_id, created_at_ms DESC, id DESC);

CREATE TABLE IF NOT EXISTS team_preview_tickets (
    id TEXT PRIMARY KEY,
    deployment_id TEXT NOT NULL REFERENCES team_preview_deployments(id) ON DELETE RESTRICT,
    login_session_id TEXT NOT NULL REFERENCES team_sessions(id) ON DELETE RESTRICT,
    actor_user_id TEXT NOT NULL REFERENCES team_users(id) ON DELETE RESTRICT,
    membership_revision INTEGER NOT NULL CHECK (membership_revision >= 0),
    token_hash TEXT NOT NULL UNIQUE,
    expires_at_ms INTEGER NOT NULL,
    consumed_at_ms INTEGER,
    created_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_team_preview_ticket_expiry
ON team_preview_tickets(deployment_id, expires_at_ms, consumed_at_ms);

CREATE TABLE IF NOT EXISTS team_preview_leases (
    id TEXT PRIMARY KEY,
    deployment_id TEXT NOT NULL REFERENCES team_preview_deployments(id) ON DELETE RESTRICT,
    login_session_id TEXT NOT NULL REFERENCES team_sessions(id) ON DELETE RESTRICT,
    actor_user_id TEXT NOT NULL REFERENCES team_users(id) ON DELETE RESTRICT,
    membership_revision INTEGER NOT NULL CHECK (membership_revision >= 0),
    token_hash TEXT NOT NULL UNIQUE,
    expires_at_ms INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_team_preview_lease_expiry
ON team_preview_leases(deployment_id, expires_at_ms);
