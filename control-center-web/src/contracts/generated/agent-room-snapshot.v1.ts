/* eslint-disable */
/**
 * This file is generated. Do not edit it by hand.
 * Source: rag_ime/contracts/json/agent-room-snapshot.v1.json
 */

export interface AgentRoomSnapshotV1 {
  schemaVersion: 'rag-ime.agent-room-snapshot.v1';
  ok: true;
  room: Room;
  /**
   * @maxItems 2000
   */
  events: Event[];
  firstSequence: number;
  lastSequence: number;
  resumeToken: string;
  truncated: boolean;
}
export interface Room {
  schemaVersion: 'rag-ime.agent-room.v1';
  id: string;
  title: string;
  status: 'active' | 'archived';
  executionMode: 'read_only' | 'per_action' | 'workspace_managed' | 'full_trust';
  permissionPolicy: RoomPermissionPolicy;
  roomKind?: 'collaboration' | 'roleplay';
  avatar?: string;
  description?: string;
  scenarioPrompt?: string;
  ownerAppId?: string;
  surfaceKey?: string;
  routingPolicy?:
    'manual_mentions' | 'moderator' | 'sequential' | 'natural' | 'parallel' | 'invite_only';
  routingConfig?: {
    [k: string]: unknown;
  };
  moderatorParticipantId: string;
  nextSpeakerOrdinal?: number;
  activeTopicId?: string;
  configRevision?: number;
  /**
   * @maxItems 5
   */
  workspaceRoots:
    | []
    | [string]
    | [string, string]
    | [string, string, string]
    | [string, string, string, string]
    | [string, string, string, string, string];
  createdAtMs: number;
  updatedAtMs: number;
  lastEventSequence: number;
  /**
   * @minItems 2
   * @maxItems 8
   */
  participants:
    | [Participant, Participant]
    | [Participant, Participant, Participant]
    | [Participant, Participant, Participant, Participant]
    | [Participant, Participant, Participant, Participant, Participant]
    | [Participant, Participant, Participant, Participant, Participant, Participant]
    | [Participant, Participant, Participant, Participant, Participant, Participant, Participant]
    | [
        Participant,
        Participant,
        Participant,
        Participant,
        Participant,
        Participant,
        Participant,
        Participant,
      ];
  /**
   * @maxItems 200
   */
  topics?: {
    [k: string]: unknown;
  }[];
  /**
   * @maxItems 100
   */
  artifacts?: {
    [k: string]: unknown;
  }[];
  /**
   * @maxItems 100
   */
  workItems?: {
    [k: string]: unknown;
  }[];
  startGate?: {
    [k: string]: unknown;
  } | null;
}
export interface RoomPermissionPolicy {
  schemaVersion: 'rag-ime.room-permission-policy.v1';
  room: RoomPermissionLayer;
  partner: RoomPermissionLowerLayer;
  toolAgent: RoomPermissionLowerLayer;
}
export interface RoomPermissionLayer {
  executionMode: 'read_only' | 'per_action' | 'workspace_managed' | 'full_trust';
}
export interface RoomPermissionLowerLayer {
  executionMode: 'inherit' | 'read_only' | 'per_action' | 'workspace_managed' | 'full_trust';
}
export interface Participant {
  schemaVersion: 'rag-ime.agent-participant.v1';
  id: string;
  roomId: string;
  sessionId: string;
  ownerUserId?: string;
  ownerDisplayName?: string;
  spaceId?: string;
  canControl?: boolean;
  audience?: 'owner' | 'project';
  roleId: string;
  roleVersion: string;
  displayName: string;
  collaborationRole: 'coordinator' | 'researcher' | 'implementer' | 'reviewer' | 'specialist';
  status: 'active' | 'muted' | 'removed';
  ordinal: number;
  createdAtMs: number;
  lastSpokeAtMs: number | null;
}
export interface Event {
  schemaVersion: 'rag-ime.agent-room-event.v1';
  eventId: string;
  roomId: string;
  sequence: number;
  turnId: string;
  eventType:
    | 'user_message'
    | 'route_decision'
    | 'participant_status'
    | 'participant_delta'
    | 'participant_activity'
    | 'participant_message'
    | 'room_post'
    | 'room_config_changed'
    | 'topic_changed'
    | 'artifact_changed'
    | 'turn_completed'
    | 'turn_failed'
    | 'snapshot_required'
    | 'room_start_confirmation_required'
    | 'room_start_confirmation_confirmed'
    | 'room_start_confirmation_rejected';
  participantId: string | null;
  sourceSessionId: string;
  topicId?: string;
  createdAtMs: number;
  payload: {
    [k: string]: unknown;
  };
  resumeToken: string;
}
