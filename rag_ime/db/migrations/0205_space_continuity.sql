-- Derived proposals and explicit user decisions; Goal/WorkItem/Pi retain authority.
CREATE TABLE IF NOT EXISTS agent_space_decisions (
    id TEXT PRIMARY KEY, space_key TEXT NOT NULL, text TEXT NOT NULL,
    source_json TEXT NOT NULL, supersedes_id TEXT NOT NULL DEFAULT '',
    created_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_space_decisions_space ON agent_space_decisions(space_key,created_at_ms);
CREATE TABLE IF NOT EXISTS agent_space_resume_proposals (
    id TEXT PRIMARY KEY, space_key TEXT NOT NULL, revision TEXT NOT NULL,
    proposal_json TEXT NOT NULL, expires_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_space_resume_intents (
    command_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL, space_key TEXT NOT NULL,
    payload_json TEXT NOT NULL, created_at_ms INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS agent_space_resume_proposal_once
ON agent_space_resume_intents(proposal_id);
