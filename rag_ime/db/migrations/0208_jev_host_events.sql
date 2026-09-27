-- Host scheduling metadata only. WorkItems, Pi transcripts and Room events
-- retain their existing owners. Sources enter the outbox in the owner commit.
CREATE TABLE agent_jev_owner_events (
    source_id TEXT NOT NULL,
    graph_id TEXT NOT NULL REFERENCES agent_jev_graphs(graph_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','processing','done','interrupted')),
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY(graph_id, source_id)
);
CREATE INDEX agent_jev_owner_events_pending ON agent_jev_owner_events(state, created_at_ms);
CREATE TABLE agent_jev_host_roots (
    graph_id TEXT PRIMARY KEY REFERENCES agent_jev_graphs(graph_id) ON DELETE CASCADE,
    request_hash TEXT NOT NULL,
    stopped INTEGER NOT NULL DEFAULT 0 CHECK(stopped IN (0,1)),
    external_allowed INTEGER NOT NULL DEFAULT 1 CHECK(external_allowed IN (0,1))
);
CREATE TRIGGER agent_jev_work_event_outbox AFTER INSERT ON agent_room_work_events
BEGIN
    INSERT OR IGNORE INTO agent_jev_owner_events(source_id,graph_id,kind,created_at_ms)
    SELECT NEW.event_id,g.graph_id,
        CASE NEW.event_type WHEN 'submitted' THEN 'work_submitted'
        WHEN 'completed' THEN 'work_reviewed' WHEN 'returned' THEN 'work_reviewed'
        WHEN 'reassigned' THEN 'assignment_changed' ELSE 'work_created' END,
        NEW.created_at_ms
    FROM agent_jev_graphs g JOIN agent_room_work_items w ON w.id=NEW.work_id
    WHERE g.room_id=w.room_id AND g.root_turn_id=w.root_turn_id;
END;
CREATE TRIGGER agent_jev_room_event_outbox AFTER INSERT ON agent_room_events
WHEN NEW.event_type IN ('room_post','turn_completed','turn_failed','participant_activity')
BEGIN
    INSERT OR IGNORE INTO agent_jev_owner_events(source_id,graph_id,kind,created_at_ms)
    SELECT NEW.event_id,g.graph_id,'executor_drained',NEW.created_at_ms
    FROM agent_jev_graphs g WHERE g.room_id=NEW.room_id AND g.root_turn_id=NEW.turn_id
    AND (NEW.event_type <> 'participant_activity'
         OR json_extract(NEW.payload_json,'$.activityKind')='child') ;
END;
