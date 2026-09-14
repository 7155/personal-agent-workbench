-- Versioned project repositories, isolated workspaces, immutable drafts, and
-- recoverable integration intents for the dedicated team database.
CREATE TABLE IF NOT EXISTS team_projects (
    space_id TEXT NOT NULL REFERENCES team_spaces(id) ON DELETE CASCADE,
    target_branch TEXT NOT NULL,
    repository_path TEXT NOT NULL,
    head_commit TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY (space_id, target_branch)
);

CREATE INDEX IF NOT EXISTS idx_team_projects_repository_path
ON team_projects(repository_path);

CREATE TABLE IF NOT EXISTS team_workspaces (
    space_id TEXT NOT NULL REFERENCES team_spaces(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL,
    target_branch TEXT NOT NULL,
    session_id TEXT,
    owner_user_id TEXT NOT NULL REFERENCES team_users(id) ON DELETE RESTRICT,
    workspace_path TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    base_revision INTEGER NOT NULL CHECK (base_revision >= 1),
    source_draft_id TEXT,
    source_draft_commit TEXT,
    source_adopted_at_ms INTEGER,
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY (space_id, workspace_id),
    FOREIGN KEY (space_id, target_branch)
        REFERENCES team_projects(space_id, target_branch)
        ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_team_workspaces_session
ON team_workspaces(session_id) WHERE session_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_team_workspaces_path
ON team_workspaces(workspace_path);

CREATE TABLE IF NOT EXISTS team_drafts (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    target_branch TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL REFERENCES team_users(id) ON DELETE RESTRICT,
    description TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    base_revision INTEGER NOT NULL CHECK (base_revision >= 1),
    draft_commit TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'integrated', 'conflict', 'verification_failed')),
    created_at_ms INTEGER NOT NULL,
    integrated_at_ms INTEGER,
    integrated_commit TEXT,
    result_json TEXT,
    FOREIGN KEY (space_id, workspace_id)
        REFERENCES team_workspaces(space_id, workspace_id)
        ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_team_drafts_commit
ON team_drafts(space_id, target_branch, draft_commit);

CREATE INDEX IF NOT EXISTS idx_team_drafts_space
ON team_drafts(space_id, target_branch, created_at_ms, id);

CREATE TABLE IF NOT EXISTS team_integration_intents (
    id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES team_drafts(id) ON DELETE RESTRICT,
    space_id TEXT NOT NULL,
    target_branch TEXT NOT NULL,
    expected_head TEXT NOT NULL,
    candidate_commit TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'prepared'
        CHECK (status IN ('prepared', 'verifier_failed', 'applied', 'conflict')),
    verifier_json TEXT,
    result_json TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    UNIQUE (draft_id, expected_head)
);

CREATE INDEX IF NOT EXISTS idx_team_integration_intents_recovery
ON team_integration_intents(space_id, target_branch, status, updated_at_ms);

CREATE TABLE IF NOT EXISTS team_workspace_adoptions (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    draft_id TEXT NOT NULL REFERENCES team_drafts(id) ON DELETE RESTRICT,
    draft_commit TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    target_tree_commit TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('adopted', 'conflict')),
    result_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    UNIQUE (workspace_id, draft_id),
    FOREIGN KEY (space_id, workspace_id)
        REFERENCES team_workspaces(space_id, workspace_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_team_workspace_adoptions_workspace
ON team_workspace_adoptions(space_id, workspace_id, created_at_ms, id);
