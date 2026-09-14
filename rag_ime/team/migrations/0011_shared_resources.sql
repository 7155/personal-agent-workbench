-- Durable administrator publications, space selections, and immutable
-- Session resource snapshots for the dedicated Team identity database.
CREATE TABLE IF NOT EXISTS team_published_resources (
    publication_id TEXT PRIMARY KEY,
    package_id TEXT NOT NULL,
    version TEXT NOT NULL,
    digest TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'published'
        CHECK (status IN ('published', 'withdrawn')),
    metadata_json TEXT NOT NULL,
    published_by_user_id TEXT NOT NULL REFERENCES team_users(id) ON DELETE RESTRICT,
    published_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_team_published_package_version
ON team_published_resources(package_id, version);

CREATE INDEX IF NOT EXISTS idx_team_published_status_time
ON team_published_resources(status, published_at_ms DESC, publication_id);

CREATE TABLE IF NOT EXISTS team_space_resource_selections (
    space_id TEXT PRIMARY KEY REFERENCES team_spaces(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    publication_ids_json TEXT NOT NULL DEFAULT '[]',
    updated_by_user_id TEXT REFERENCES team_users(id) ON DELETE RESTRICT,
    updated_at_ms INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS team_session_resource_snapshots (
    session_id TEXT PRIMARY KEY REFERENCES team_session_bindings(session_id) ON DELETE CASCADE,
    space_id TEXT NOT NULL REFERENCES team_spaces(id) ON DELETE CASCADE,
    selection_revision INTEGER NOT NULL CHECK (selection_revision >= 0),
    publication_ids_json TEXT NOT NULL,
    items_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_team_session_resource_snapshots_space
ON team_session_resource_snapshots(space_id, created_at_ms, session_id);

-- Existing bindings predate shared-resource snapshots.  They intentionally
-- receive an explicit empty revision-zero snapshot rather than inheriting the
-- current space selection when the migration runs.
INSERT OR IGNORE INTO team_session_resource_snapshots(
    session_id, space_id, selection_revision, publication_ids_json, items_json, created_at_ms
)
SELECT session_id, space_id, 0, '[]', '[]', created_at_ms
FROM team_session_bindings;
