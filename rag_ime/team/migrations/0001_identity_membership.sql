-- Dedicated team identity database.  This migration directory is intentionally
-- separate from rag_ime/db/migrations so a team server never opens the
-- personal Memory/Trace database as an identity store.
CREATE TABLE IF NOT EXISTS team_users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    username_key TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    failed_login_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_login_count >= 0),
    locked_until_ms INTEGER NOT NULL DEFAULT 0 CHECK (locked_until_ms >= 0),
    authorization_revision INTEGER NOT NULL DEFAULT 1 CHECK (authorization_revision >= 1),
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_team_users_active_role
ON team_users(active, role);

CREATE TABLE IF NOT EXISTS team_sessions (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    csrf_hash TEXT NOT NULL,
    csrf_token TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    revoked_at_ms INTEGER,
    FOREIGN KEY (user_id) REFERENCES team_users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_team_sessions_user
ON team_sessions(user_id, expires_at_ms);

CREATE TABLE IF NOT EXISTS team_spaces (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('personal', 'project')),
    name TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    FOREIGN KEY (owner_user_id) REFERENCES team_users(id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_team_spaces_personal_owner
ON team_spaces(owner_user_id) WHERE kind = 'personal';

CREATE INDEX IF NOT EXISTS idx_team_spaces_owner_kind
ON team_spaces(owner_user_id, kind);

CREATE TABLE IF NOT EXISTS team_project_members (
    space_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('owner', 'maintainer', 'contributor', 'viewer')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    membership_revision INTEGER NOT NULL DEFAULT 1 CHECK (membership_revision >= 1),
    added_at_ms INTEGER NOT NULL,
    PRIMARY KEY (space_id, user_id),
    FOREIGN KEY (space_id) REFERENCES team_spaces(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES team_users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_team_project_members_user
ON team_project_members(user_id, space_id);

CREATE INDEX IF NOT EXISTS idx_team_project_members_manager
ON team_project_members(space_id, role);
