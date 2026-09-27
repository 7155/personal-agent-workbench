-- A Jev revision keeps the original WorkItems and effects as history. Only
-- the explicitly superseded WorkItem IDs leave the current task graph.
ALTER TABLE agent_jev_host_roots ADD COLUMN current_objective TEXT NOT NULL DEFAULT '';

CREATE TABLE agent_jev_task_revisions (
  revision_id TEXT PRIMARY KEY,
  graph_id TEXT NOT NULL REFERENCES agent_jev_graphs(graph_id),
  command_id TEXT NOT NULL UNIQUE REFERENCES agent_jev_commands(command_id),
  status TEXT NOT NULL CHECK(status IN ('awaiting_drain','applied','cancelled')),
  base_topology_revision INTEGER NOT NULL,
  base_requirements_revision INTEGER NOT NULL,
  changed_task_id TEXT NOT NULL REFERENCES agent_room_work_items(id),
  request_json TEXT NOT NULL,
  affected_json TEXT NOT NULL,
  retained_accepted_json TEXT NOT NULL,
  required_dispatch_ids_json TEXT NOT NULL,
  successor_json TEXT NOT NULL DEFAULT '{}',
  created_at_ms INTEGER NOT NULL,
  applied_at_ms INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX agent_jev_one_pending_task_revision
  ON agent_jev_task_revisions(graph_id) WHERE status='awaiting_drain';

CREATE TABLE agent_jev_revision_targets (
  revision_id TEXT NOT NULL REFERENCES agent_jev_task_revisions(revision_id),
  task_id TEXT NOT NULL REFERENCES agent_room_work_items(id),
  PRIMARY KEY(revision_id,task_id)
);
CREATE INDEX agent_jev_revision_targets_task
  ON agent_jev_revision_targets(task_id,revision_id);

CREATE TABLE agent_jev_revision_dispatches (
  revision_id TEXT NOT NULL REFERENCES agent_jev_task_revisions(revision_id),
  dispatch_id TEXT NOT NULL REFERENCES agent_jev_runtime_effects(effect_id),
  PRIMARY KEY(revision_id,dispatch_id)
);

CREATE TABLE agent_jev_task_supersessions (
  graph_id TEXT NOT NULL REFERENCES agent_jev_graphs(graph_id),
  old_task_id TEXT PRIMARY KEY REFERENCES agent_room_work_items(id),
  new_task_id TEXT NOT NULL UNIQUE REFERENCES agent_room_work_items(id),
  revision_id TEXT NOT NULL REFERENCES agent_jev_task_revisions(revision_id),
  created_at_ms INTEGER NOT NULL
);
