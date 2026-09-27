-- Jev third-mode control metadata. WorkItem, participant and Pi state stay owned
-- by their existing stores. No transcript, goal copy or old room_kernel revival.
CREATE TABLE agent_jev_graphs (
  graph_id TEXT PRIMARY KEY,
  room_id TEXT NOT NULL REFERENCES agent_rooms(id),
  root_turn_id TEXT NOT NULL,
  root_work_id TEXT NOT NULL REFERENCES agent_room_work_items(id),
  controller_id TEXT NOT NULL,
  controller_participant_id TEXT NOT NULL REFERENCES agent_room_participants(id),
  controller_session_id TEXT NOT NULL,
  mode TEXT NOT NULL CHECK(mode = 'jev'),
  topology_revision INTEGER NOT NULL DEFAULT 0 CHECK(topology_revision >= 0),
  created_at_ms INTEGER NOT NULL,
  UNIQUE(room_id, root_turn_id)
);
CREATE TABLE agent_jev_edges (
  graph_id TEXT NOT NULL REFERENCES agent_jev_graphs(graph_id) ON DELETE CASCADE,
  prerequisite TEXT NOT NULL REFERENCES agent_room_work_items(id),
  dependent TEXT NOT NULL REFERENCES agent_room_work_items(id),
  kind TEXT NOT NULL CHECK(kind IN ('requires','context')),
  CHECK(prerequisite <> dependent),
  PRIMARY KEY(graph_id, prerequisite, dependent, kind)
);
CREATE TABLE agent_jev_commands (
  command_id TEXT PRIMARY KEY,
  graph_id TEXT NOT NULL REFERENCES agent_jev_graphs(graph_id),
  intent_hash TEXT NOT NULL,
  operation TEXT NOT NULL,
  task_id TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at_ms INTEGER NOT NULL
);
CREATE INDEX agent_jev_commands_graph ON agent_jev_commands(graph_id,created_at_ms);
CREATE TABLE agent_jev_reclaims (
  reclaim_id TEXT PRIMARY KEY,
  graph_id TEXT NOT NULL REFERENCES agent_jev_graphs(graph_id),
  task_id TEXT NOT NULL REFERENCES agent_room_work_items(id),
  binding_json TEXT NOT NULL,
  dispatch_id TEXT NOT NULL,
  runtime_session_id TEXT NOT NULL,
  target_participant_id TEXT NOT NULL REFERENCES agent_room_participants(id),
  status TEXT NOT NULL CHECK(status IN ('requested','applied')),
  proof_json TEXT NOT NULL DEFAULT '{}',
  created_at_ms INTEGER NOT NULL,
  updated_at_ms INTEGER NOT NULL
);
CREATE UNIQUE INDEX agent_jev_one_reclaim_per_task ON agent_jev_reclaims(task_id)
WHERE status = 'requested';
CREATE TABLE agent_jev_runtime_effects (
  effect_id TEXT PRIMARY KEY,
  graph_id TEXT NOT NULL REFERENCES agent_jev_graphs(graph_id),
  command_id TEXT NOT NULL REFERENCES agent_jev_commands(command_id),
  operation TEXT NOT NULL CHECK(operation IN ('dispatch','cancel')),
  request_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending','sending','accepted','rejected','unknown','not_sent')),
  receipt_json TEXT NOT NULL DEFAULT '{}',
  updated_at_ms INTEGER NOT NULL
);
CREATE INDEX agent_jev_effects_pending ON agent_jev_runtime_effects(graph_id,state,updated_at_ms);
-- Reserves only Jev prepared/in-flight effects, not another Pi execution state.
-- Runtime drain/non-admission proof is required before releasing a used slot.
CREATE TABLE agent_jev_executor_claims (
  session_id TEXT PRIMARY KEY,
  graph_id TEXT NOT NULL REFERENCES agent_jev_graphs(graph_id),
  task_id TEXT NOT NULL REFERENCES agent_room_work_items(id),
  effect_id TEXT NOT NULL UNIQUE REFERENCES agent_jev_runtime_effects(effect_id),
  binding_json TEXT NOT NULL,
  created_at_ms INTEGER NOT NULL
);
CREATE TABLE agent_jev_decisions (
  graph_id TEXT NOT NULL REFERENCES agent_jev_graphs(graph_id),
  event_id TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  snapshot_hash TEXT NOT NULL,
  generation INTEGER NOT NULL,
  lease_until_ms INTEGER NOT NULL,
  phase TEXT NOT NULL CHECK(phase IN ('evaluating','decided','applied','failed','superseded')),
  selected_id TEXT NOT NULL DEFAULT '',
  answer_json TEXT NOT NULL DEFAULT '{}',
  command_id TEXT NOT NULL,
  updated_at_ms INTEGER NOT NULL,
  PRIMARY KEY(graph_id,event_id)
);
