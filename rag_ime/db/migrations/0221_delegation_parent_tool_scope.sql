ALTER TABLE agent_subagent_batches ADD COLUMN parent_tool_call_id TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_subagent_batches ADD COLUMN parent_turn_id TEXT NOT NULL DEFAULT '';
