-- Presentation metadata only. Canonical Session/Room lifecycle stays with Pi.
CREATE TABLE agent_space_organization (
    space_key TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    data_json TEXT NOT NULL
);
CREATE TABLE agent_space_organization_receipts (
    command_id TEXT PRIMARY KEY,
    space_key TEXT NOT NULL,
    intent_json TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    applied_revision INTEGER NOT NULL,
    undone INTEGER NOT NULL DEFAULT 0,
    created_at_ms INTEGER NOT NULL
);
CREATE TABLE agent_space_organization_proposals (
    id TEXT PRIMARY KEY,
    space_key TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    expected_revision INTEGER NOT NULL,
    category TEXT NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    decision_json TEXT NOT NULL
);
