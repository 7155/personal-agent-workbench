-- A membership request is durable before any exact cancellation is sent.
-- The participant stays active for existing execution and verification reads
-- until every live responsibility has been drained and transferred.
CREATE TABLE agent_jev_participant_removals (
  removal_id TEXT PRIMARY KEY,
  room_id TEXT NOT NULL REFERENCES agent_rooms(id),
  participant_id TEXT NOT NULL REFERENCES agent_room_participants(id),
  client_message_id TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  target_participant_id TEXT NOT NULL DEFAULT '',
  targets_json TEXT NOT NULL DEFAULT '{}',
  stop_root INTEGER NOT NULL DEFAULT 0 CHECK(stop_root IN (0, 1)),
  reason TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK(status IN ('pending', 'completed')),
  stage TEXT NOT NULL DEFAULT 'requested',
  detail TEXT NOT NULL DEFAULT '',
  created_at_ms INTEGER NOT NULL,
  updated_at_ms INTEGER NOT NULL
);
CREATE UNIQUE INDEX agent_jev_one_pending_participant_removal
  ON agent_jev_participant_removals(room_id, participant_id)
  WHERE status = 'pending';
CREATE UNIQUE INDEX agent_jev_participant_removal_client
  ON agent_jev_participant_removals(room_id, client_message_id);
CREATE INDEX agent_jev_participant_removal_pending
  ON agent_jev_participant_removals(status, updated_at_ms);

-- A new clientMessageId can reselect a destination without changing the
-- original removal request/receipt or the already-requested stop evidence.
CREATE TABLE agent_jev_participant_removal_targets (
  room_id TEXT NOT NULL REFERENCES agent_rooms(id),
  client_message_id TEXT NOT NULL,
  removal_id TEXT NOT NULL REFERENCES agent_jev_participant_removals(removal_id),
  request_hash TEXT NOT NULL,
  target_participant_id TEXT NOT NULL DEFAULT '',
  created_at_ms INTEGER NOT NULL,
  PRIMARY KEY(room_id, client_message_id)
);
