/* eslint-disable */
/**
 * This file is generated. Do not edit it by hand.
 * Source: rag_ime/contracts/json/agent-room.v1.json
 */

export interface AgentRoomV1 {
  schemaVersion: 'rag-ime.agent-room.v1';
  id: string;
  title: string;
  status: 'active' | 'archived';
  roomKind?: 'collaboration' | 'roleplay';
  avatar?: string;
  description?: string;
  scenarioPrompt?: string;
  ownerAppId?: string;
  surfaceKey?: string;
  routingPolicy:
    'manual_mentions' | 'moderator' | 'sequential' | 'natural' | 'parallel' | 'invite_only' | 'jev';
  routingConfig?: {
    maxResponders: 1;
    naturalJitter: number;
    fallbackParticipantId: string;
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
  permissionPolicy: RoomPermissionPolicy;
  executionMode: 'read_only' | 'per_action' | 'workspace_managed' | 'full_trust';
  createdAtMs: number;
  updatedAtMs: number;
  lastEventSequence: number;
  /**
   * @minItems 2
   * @maxItems 8
   */
  participants:
    | [
        {
          [k: string]: unknown;
        },
        {
          [k: string]: unknown;
        },
      ]
    | [
        {
          [k: string]: unknown;
        },
        {
          [k: string]: unknown;
        },
        {
          [k: string]: unknown;
        },
      ]
    | [
        {
          [k: string]: unknown;
        },
        {
          [k: string]: unknown;
        },
        {
          [k: string]: unknown;
        },
        {
          [k: string]: unknown;
        },
      ]
    | [
        {
          [k: string]: unknown;
        },
        {
          [k: string]: unknown;
        },
        {
          [k: string]: unknown;
        },
        {
          [k: string]: unknown;
        },
        {
          [k: string]: unknown;
        },
      ]
    | [
        {
          [k: string]: unknown;
        },
        {
          [k: string]: unknown;
        },
        {
          [k: string]: unknown;
        },
        {
          [k: string]: unknown;
        },
        {
          [k: string]: unknown;
        },
        {
          [k: string]: unknown;
        },
      ]
    | [
        {
          [k: string]: unknown;
        },
        {
          [k: string]: unknown;
        },
        {
          [k: string]: unknown;
        },
        {
          [k: string]: unknown;
        },
        {
          [k: string]: unknown;
        },
        {
          [k: string]: unknown;
        },
        {
          [k: string]: unknown;
        },
      ]
    | [
        {
          [k: string]: unknown;
        },
        {
          [k: string]: unknown;
        },
        {
          [k: string]: unknown;
        },
        {
          [k: string]: unknown;
        },
        {
          [k: string]: unknown;
        },
        {
          [k: string]: unknown;
        },
        {
          [k: string]: unknown;
        },
        {
          [k: string]: unknown;
        },
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
