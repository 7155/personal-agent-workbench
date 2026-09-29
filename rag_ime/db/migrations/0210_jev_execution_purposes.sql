-- Control/evidence metadata; canonical task state remains in WorkStore.
ALTER TABLE agent_jev_host_roots ADD COLUMN phase TEXT NOT NULL DEFAULT 'execute';
ALTER TABLE agent_jev_host_roots ADD COLUMN epoch INTEGER NOT NULL DEFAULT 1;
ALTER TABLE agent_jev_host_roots ADD COLUMN requirements_revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE agent_jev_host_roots ADD COLUMN recovery_policy TEXT NOT NULL DEFAULT 'authorized_unsent';
ALTER TABLE agent_jev_host_roots ADD COLUMN max_parallel INTEGER NOT NULL DEFAULT 3;
ALTER TABLE agent_jev_host_roots ADD COLUMN final_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE agent_jev_host_roots ADD COLUMN previous_root_id TEXT NOT NULL DEFAULT '';
CREATE TABLE agent_jev_task_requirements (
 task_id TEXT PRIMARY KEY REFERENCES agent_room_work_items(id),
 graph_id TEXT NOT NULL REFERENCES agent_jev_graphs(graph_id),
 specification_json TEXT NOT NULL
);
CREATE TABLE agent_jev_execution_outputs (
 dispatch_id TEXT PRIMARY KEY REFERENCES agent_jev_runtime_effects(effect_id),
 purpose TEXT NOT NULL CHECK(purpose IN ('plan','execute','verify','synthesize')),
 subject_hash TEXT NOT NULL,
 source_turn_id TEXT NOT NULL,
 payload_json TEXT NOT NULL,
 created_at_ms INTEGER NOT NULL
);
CREATE TABLE agent_jev_verifications (
 graph_id TEXT NOT NULL REFERENCES agent_jev_graphs(graph_id),
 task_id TEXT NOT NULL REFERENCES agent_room_work_items(id),
 task_hash TEXT NOT NULL,
 dispatch_id TEXT NOT NULL REFERENCES agent_jev_runtime_effects(effect_id),
 result_json TEXT NOT NULL,
 PRIMARY KEY(graph_id,task_id,task_hash)
);
CREATE TABLE agent_jev_plan_receipts (
 graph_id TEXT NOT NULL REFERENCES agent_jev_graphs(graph_id),
 dispatch_id TEXT NOT NULL REFERENCES agent_jev_runtime_effects(effect_id),
 proposal_hash TEXT NOT NULL,
 aliases_json TEXT NOT NULL,
 PRIMARY KEY(graph_id,dispatch_id)
);
CREATE TABLE agent_jev_aux_settlements (
 dispatch_id TEXT PRIMARY KEY REFERENCES agent_jev_runtime_effects(effect_id),
 result_json TEXT NOT NULL,
 settled_at_ms INTEGER NOT NULL
);
