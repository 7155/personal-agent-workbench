-- Preserve the source-event identity while making delivery and decision
-- attempts recoverable. Applied migrations 0207/0208 remain unchanged.
DROP TRIGGER agent_jev_work_event_outbox;
DROP TRIGGER agent_jev_room_event_outbox;
ALTER TABLE agent_jev_owner_events RENAME TO agent_jev_owner_events_0208;
CREATE TABLE agent_jev_owner_events (
    source_id TEXT NOT NULL,
    graph_id TEXT NOT NULL REFERENCES agent_jev_graphs(graph_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','processing','retry','reconcile','blocked','done')),
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at_ms INTEGER NOT NULL,
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_generation INTEGER NOT NULL DEFAULT 0 CHECK(lease_generation >= 0),
    lease_until_ms INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    attempt_budget INTEGER NOT NULL DEFAULT 6 CHECK(attempt_budget >= 1),
    next_retry_at_ms INTEGER NOT NULL DEFAULT 0,
    result_kind TEXT NOT NULL DEFAULT '',
    wake_condition TEXT NOT NULL DEFAULT '',
    updated_at_ms INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(graph_id, source_id)
);
INSERT INTO agent_jev_owner_events
    (source_id,graph_id,kind,state,result_json,created_at_ms,updated_at_ms)
SELECT source_id,graph_id,kind,
    CASE WHEN state IN ('processing','interrupted') THEN 'retry'
         WHEN state='done' AND COALESCE(json_extract(result_json,'$.status'),'') IN
              ('host_error','owner_error','decision_unavailable','failed','evaluating','decided',
               'pending','stale_decision','stale_observation','superseded_evaluation',
               'no_progress_budget','decision_external_not_allowed','disabled') THEN 'retry'
         ELSE state END,
    result_json,created_at_ms,created_at_ms
FROM agent_jev_owner_events_0208;
DROP TABLE agent_jev_owner_events_0208;
CREATE INDEX agent_jev_owner_events_pending
    ON agent_jev_owner_events(state,next_retry_at_ms,created_at_ms);
ALTER TABLE agent_jev_host_roots ADD COLUMN last_event_claim_sequence INTEGER NOT NULL DEFAULT 0;

-- Inputs are frozen once for each bounded generation, including failures.
-- The existing table remains the current-generation compatibility projection.
CREATE TABLE agent_jev_decision_generations (
    graph_id TEXT NOT NULL REFERENCES agent_jev_graphs(graph_id) ON DELETE CASCADE,
    event_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation >= 0),
    input_hash TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    observation_json TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('evaluating','decided','applied','failed','superseded')),
    command_id TEXT NOT NULL,
    selected_id TEXT NOT NULL DEFAULT '',
    answer_json TEXT NOT NULL DEFAULT '{}',
    model_call INTEGER NOT NULL DEFAULT 1 CHECK(model_call IN (0,1)),
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY(graph_id,event_id,generation)
);
CREATE INDEX agent_jev_decision_generation_budget
    ON agent_jev_decision_generations(graph_id,snapshot_hash,model_call);
CREATE INDEX agent_jev_decision_generation_command
    ON agent_jev_decision_generations(command_id);
-- 0208 recorded a denied external-policy prerequisite as an applied no-op.
-- It must become retryable when the owner later changes that configuration.
UPDATE agent_jev_decisions SET phase='failed'
WHERE phase='applied' AND json_extract(answer_json,'$.status')='decision_external_not_allowed';
INSERT INTO agent_jev_decision_generations
    (graph_id,event_id,generation,input_hash,snapshot_hash,observation_json,candidates_json,
     phase,command_id,selected_id,answer_json,model_call,created_at_ms,updated_at_ms)
SELECT graph_id,event_id,generation,input_hash,snapshot_hash,'{}','[]',phase,command_id,
       selected_id,answer_json,CASE WHEN generation=0 THEN 0 ELSE 1 END,updated_at_ms,updated_at_ms
FROM agent_jev_decisions;

-- A late worker cannot commit after its decision generation was superseded.
-- The receipt is inserted inside the canonical WorkItem transaction, so a
-- failed fence rolls back the WorkItem change and its events as well.
CREATE TRIGGER agent_jev_decision_command_fence BEFORE INSERT ON agent_jev_commands
WHEN EXISTS (SELECT 1 FROM agent_jev_decision_generations h
             WHERE h.graph_id=NEW.graph_id AND h.command_id=NEW.command_id)
 AND NOT EXISTS (SELECT 1 FROM agent_jev_decisions d
                 WHERE d.graph_id=NEW.graph_id AND d.command_id=NEW.command_id AND d.phase='decided')
BEGIN
    SELECT RAISE(ABORT,'stale Jev decision generation');
END;

CREATE TRIGGER agent_jev_work_event_outbox AFTER INSERT ON agent_room_work_events
WHEN NEW.event_type IN ('assigned','reassigned','resumed','accepted','assignment_failed',
                       'submitted','completed','returned','blocked','escalated','cancelled','failed')
BEGIN
    INSERT OR IGNORE INTO agent_jev_owner_events(source_id,graph_id,kind,created_at_ms)
    SELECT NEW.event_id,g.graph_id,
        CASE WHEN NEW.event_type='submitted' THEN 'work_submitted'
             WHEN NEW.event_type IN ('completed','returned') THEN 'work_reviewed'
             WHEN NEW.event_type IN ('assigned','reassigned','resumed','accepted','assignment_failed')
                  THEN 'assignment_changed'
             ELSE 'requirements_changed' END,
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
         OR json_extract(NEW.payload_json,'$.activityKind')='child');
END;
