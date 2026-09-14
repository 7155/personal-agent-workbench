-- Freeze the project brief version used by each workspace and immutable draft.
-- Existing rows use revision zero, meaning no brief was published when the row
-- was created; they become stale as soon as a project publishes revision one.
ALTER TABLE team_workspaces
ADD COLUMN requirements_revision INTEGER NOT NULL DEFAULT 0
CHECK (requirements_revision >= 0);

ALTER TABLE team_drafts
ADD COLUMN requirements_revision INTEGER NOT NULL DEFAULT 0
CHECK (requirements_revision >= 0);

CREATE INDEX IF NOT EXISTS idx_team_workspaces_requirements
ON team_workspaces(space_id, requirements_revision);

CREATE INDEX IF NOT EXISTS idx_team_drafts_requirements
ON team_drafts(space_id, requirements_revision);
