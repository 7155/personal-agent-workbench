-- Immutable, versioned project objectives and acceptance criteria.
-- Revision zero is represented by the absence of a row; every persisted
-- brief is a frozen snapshot of the publishing member's display name.
CREATE TABLE IF NOT EXISTS team_project_briefs (
    space_id TEXT NOT NULL REFERENCES team_spaces(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    objective TEXT NOT NULL,
    acceptance_criteria_json TEXT NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    updated_by_user_id TEXT NOT NULL REFERENCES team_users(id) ON DELETE RESTRICT,
    updated_by_display_name TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (space_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_team_project_briefs_history
ON team_project_briefs(space_id, revision DESC);
