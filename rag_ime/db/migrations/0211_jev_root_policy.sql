-- User-selected policy and managed input references, separate from task state.
ALTER TABLE agent_jev_host_roots ADD COLUMN policy_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE agent_jev_host_roots ADD COLUMN attachment_ids_json TEXT NOT NULL DEFAULT '[]';
