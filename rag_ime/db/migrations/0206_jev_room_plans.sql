CREATE TABLE agent_jev_room_plans (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    work_ids_json TEXT NOT NULL DEFAULT '{}',
    created_at_ms INTEGER NOT NULL
);
CREATE INDEX agent_jev_room_plans_room ON agent_jev_room_plans(room_id, created_at_ms);
