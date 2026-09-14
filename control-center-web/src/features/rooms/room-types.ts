export type RoomCollaborationRole = 'coordinator' | 'researcher' | 'implementer' | 'reviewer' | 'specialist';

export interface RoomParticipant {
  id: string;
  sessionId: string;
  roleId: string;
  roleVersion: string;
  displayName: string;
  /** TeamGateway enriches shared Room participants with the owning human. */
  ownerUserId?: string;
  ownerDisplayName?: string;
  canControl?: boolean;
  collaborationRole?: RoomCollaborationRole;
  status: string;
  ordinal: number;
}

export type RoomKind = 'collaboration' | 'roleplay';
export type RoomExecutionMode = 'read_only' | 'per_action' | 'workspace_managed' | 'full_trust';
export type RoomRoutingPolicy = 'moderator' | 'manual_mentions' | 'sequential' | 'natural' | 'parallel' | 'invite_only';

export const ROOM_PERMISSION_POLICY_SCHEMA_VERSION = 'rag-ime.room-permission-policy.v1' as const;

export type RoomPermissionChildExecutionMode = RoomExecutionMode | 'inherit';
export type RoomPermissionLayer = 'room' | 'partner' | 'toolAgent';

export type RoomPermissionPolicy = {
  schemaVersion: typeof ROOM_PERMISSION_POLICY_SCHEMA_VERSION;
  room: { executionMode: RoomExecutionMode };
  partner: { executionMode: RoomPermissionChildExecutionMode };
  toolAgent: { executionMode: RoomPermissionChildExecutionMode };
};

export interface RoomEffectivePermissionPolicy {
  room: RoomExecutionMode;
  partner: RoomExecutionMode;
  toolAgent: RoomExecutionMode;
}

const ROOM_PERMISSION_MODE_RANK: Record<RoomExecutionMode, number> = {
  read_only: 0,
  per_action: 1,
  workspace_managed: 2,
  full_trust: 3,
};

const ROOM_EXECUTION_MODES: Record<RoomExecutionMode, true> = {
  read_only: true,
  per_action: true,
  workspace_managed: true,
  full_trust: true,
};

export function defaultRoomPermissionPolicy(roomKind: RoomKind): RoomPermissionPolicy {
  return {
    schemaVersion: ROOM_PERMISSION_POLICY_SCHEMA_VERSION,
    room: { executionMode: roomKind === 'roleplay' ? 'per_action' : 'full_trust' },
    partner: { executionMode: 'inherit' },
    toolAgent: { executionMode: 'inherit' },
  };
}

export function parseRoomPermissionPolicy(
  value: unknown,
  roomKind?: RoomKind,
): RoomPermissionPolicy | undefined {
  const source = roomPermissionRecord(value);
  const room = roomPermissionRecord(source.room);
  const partner = roomPermissionRecord(source.partner);
  const toolAgent = roomPermissionRecord(source.toolAgent);
  const roomMode = room.executionMode;
  const partnerMode = partner.executionMode;
  const toolAgentMode = toolAgent.executionMode;
  if (
    !hasExactKeys(source, ['schemaVersion', 'room', 'partner', 'toolAgent'])
    || !hasExactKeys(room, ['executionMode'])
    || !hasExactKeys(partner, ['executionMode'])
    || !hasExactKeys(toolAgent, ['executionMode'])
  ) {
    return undefined;
  }
  if (
    source.schemaVersion !== ROOM_PERMISSION_POLICY_SCHEMA_VERSION
    || !isRoomExecutionMode(roomMode)
    || !isRoomPermissionChildExecutionMode(partnerMode)
    || !isRoomPermissionChildExecutionMode(toolAgentMode)
  ) {
    return undefined;
  }
  const policy: RoomPermissionPolicy = {
    schemaVersion: ROOM_PERMISSION_POLICY_SCHEMA_VERSION,
    room: { executionMode: roomMode },
    partner: { executionMode: partnerMode },
    toolAgent: { executionMode: toolAgentMode },
  };
  const effective = effectiveRoomPermissionPolicy(policy);
  if (
    !roomPermissionModeFitsParent(effective.partner, effective.room)
    || !roomPermissionModeFitsParent(effective.toolAgent, effective.partner)
    || (roomKind === 'roleplay'
      && ROOM_PERMISSION_MODE_RANK[effective.room] > ROOM_PERMISSION_MODE_RANK.per_action)
  ) {
    return undefined;
  }
  return policy;
}

export function effectiveRoomPermissionPolicy(
  policy: RoomPermissionPolicy,
): RoomEffectivePermissionPolicy {
  const room = policy.room.executionMode;
  const partner = policy.partner.executionMode === 'inherit'
    ? room
    : policy.partner.executionMode;
  const toolAgent = policy.toolAgent.executionMode === 'inherit'
    ? partner
    : policy.toolAgent.executionMode;
  return { room, partner, toolAgent };
}

export function roomPermissionModeFitsParent(
  child: RoomExecutionMode,
  parent: RoomExecutionMode,
): boolean {
  return ROOM_PERMISSION_MODE_RANK[child] <= ROOM_PERMISSION_MODE_RANK[parent];
}

export function updateRoomPermissionPolicy(
  policy: RoomPermissionPolicy,
  layer: RoomPermissionLayer,
  executionMode: RoomPermissionChildExecutionMode,
): RoomPermissionPolicy {
  if (layer === 'room' && executionMode === 'inherit') return policy;
  const roomMode = layer === 'room'
    ? executionMode as RoomExecutionMode
    : policy.room.executionMode;
  let partnerMode = layer === 'partner'
    ? executionMode
    : policy.partner.executionMode;
  let partnerEffective = partnerMode === 'inherit' ? roomMode : partnerMode;
  if (!roomPermissionModeFitsParent(partnerEffective, roomMode)) {
    partnerMode = 'inherit';
    partnerEffective = roomMode;
  }
  let toolAgentMode = layer === 'toolAgent'
    ? executionMode
    : policy.toolAgent.executionMode;
  const toolAgentEffective = toolAgentMode === 'inherit'
    ? partnerEffective
    : toolAgentMode;
  if (!roomPermissionModeFitsParent(toolAgentEffective, partnerEffective)) {
    toolAgentMode = 'inherit';
  }
  return {
    schemaVersion: ROOM_PERMISSION_POLICY_SCHEMA_VERSION,
    room: { executionMode: roomMode },
    partner: { executionMode: partnerMode },
    toolAgent: { executionMode: toolAgentMode },
  };
}

export function roomPermissionPoliciesEqual(
  left: RoomPermissionPolicy | undefined,
  right: RoomPermissionPolicy | undefined,
): boolean {
  return left === right || Boolean(
    left
    && right
    && left.room.executionMode === right.room.executionMode
    && left.partner.executionMode === right.partner.executionMode
    && left.toolAgent.executionMode === right.toolAgent.executionMode
  );
}

export function roomPermissionPolicyNeedsDangerousConfirmation(
  policy: RoomPermissionPolicy,
): boolean {
  const effective = effectiveRoomPermissionPolicy(policy);
  return effective.room === 'full_trust' || effective.partner === 'full_trust';
}

export function roomPermissionPolicyNeedsWorkspaceConfirmation(
  policy: RoomPermissionPolicy,
): boolean {
  const effective = effectiveRoomPermissionPolicy(policy);
  return effective.room === 'workspace_managed' || effective.partner === 'workspace_managed';
}

function isRoomExecutionMode(value: unknown): value is RoomExecutionMode {
  return typeof value === 'string'
    && ROOM_EXECUTION_MODES[value as RoomExecutionMode] === true;
}

function isRoomPermissionChildExecutionMode(
  value: unknown,
): value is RoomPermissionChildExecutionMode {
  return value === 'inherit' || isRoomExecutionMode(value);
}

function roomPermissionRecord(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function hasExactKeys(
  value: Record<string, unknown>,
  expectedKeys: readonly string[],
): boolean {
  const keys = Object.keys(value);
  return keys.length === expectedKeys.length
    && expectedKeys.every((key) => Object.hasOwn(value, key));
}

export interface RoomTopic {
  id: string;
  roomId: string;
  title: string;
  summary: string;
  status: 'active' | 'archived';
  ordinal: number;
  createdAtMs: number;
  updatedAtMs: number;
}

export interface RoomArtifact {
  id: string;
  roomId: string;
  topicId: string;
  displayName: string;
  path: string;
  mediaType: string;
  status: 'active' | 'archived';
  createdAtMs: number;
  updatedAtMs: number;
}

export type RoomWorkState = 'queued' | 'active' | 'review' | 'blocked' | 'done' | 'failed' | 'cancelled';

/** Dual-axis independent review verdict recorded on a WorkItem: operability
 * (does it run) and requirement (does it satisfy the ask) stay separate. */
export interface RoomWorkItemReview {
  operabilityVerdict: string;
  requirementVerdict: string;
  evidenceRefs: string[];
  reason: string;
  reviewerParticipantId: string;
  reviewedAtMs: number | null;
}

export interface RoomWorkItem {
  id: string;
  roomId: string;
  topicId: string;
  rootTurnId: string;
  rootWorkId: string;
  parentWorkId: string;
  objective: string;
  expectedOutput: string;
  acceptanceCriteria: string[];
  accountableParticipantId: string;
  currentOwnerParticipantId: string;
  offeredToParticipantId: string;
  createdByParticipantId: string;
  clientMessageId: string;
  state: RoomWorkState;
  depth: number;
  revision: number;
  resultSummary: string;
  artifactRefs: string[];
  evidenceRefs: string[];
  review?: RoomWorkItemReview;
  blocker: Record<string, unknown>;
  acceptedTurnId: string;
  createdAtMs: number;
  updatedAtMs: number;
  completedAtMs: number | null;
}

export interface RoomSummary {
  id: string;
  title: string;
  status: string;
  roomKind?: RoomKind;
  avatar?: string;
  description?: string;
  scenarioPrompt?: string;
  /** App-owned Rooms are restored only by their owning surface. */
  ownerAppId?: string;
  surfaceKey?: string;
  routingPolicy: RoomRoutingPolicy;
  routingConfig?: {
    maxResponders?: number;
    naturalJitter?: number;
    fallbackParticipantId?: string;
  };
  moderatorParticipantId: string;
  activeTopicId?: string;
  configRevision?: number;
  workspaceRoots?: string[];
  executionMode?: RoomExecutionMode;
  permissionPolicy?: RoomPermissionPolicy;
  topics?: RoomTopic[];
  artifacts?: RoomArtifact[];
  workItems?: RoomWorkItem[];
  startGate?: {
    status: 'pending' | 'confirmed';
    gateId: string;
    objective: string;
    workItemId: string;
    clientMessageId: string;
    rootId: string;
    confirmedAtMs: number;
  } | null;
  updatedAtMs: number;
  participants: RoomParticipant[];
}
