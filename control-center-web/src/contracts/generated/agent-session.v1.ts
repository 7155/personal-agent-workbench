/* eslint-disable */
/**
 * This file is generated. Do not edit it by hand.
 * Source: rag_ime/contracts/json/agent-session.v1.json
 */

export type AgentSessionV1 = {
  [k: string]: unknown;
} & {
  schemaVersion: 'rag-ime.agent-session.v1';
  id: string;
  metadata?: {
    assistantId: string;
    primaryAssistant?: boolean;
    primaryTask?: boolean;
    sourceSessionId?: string;
    sourceMessageId?: string;
    clientRequestId?: string;
  };
  goal?: {
    goalId: string;
    revision: number;
    status: 'active' | 'paused' | 'completed' | 'cancelled' | 'cleared';
    objective: string;
    successCriteria: string;
  };
  piSessionId?: string;
  sessionFile?: string;
  runtimeBinding?: {
    schemaVersion: 'rag-ime.agent-runtime-binding.v1';
    driverId: string;
    runtimeKind: string;
    generation: number;
    state: 'prepared' | 'active' | 'stale';
    codemodeAvailable?: boolean;
    codemodeMode?: 'on' | 'only' | 'off';
    createdAtMs: number;
    updatedAtMs: number;
    [k: string]: unknown;
  };
  title: string;
  mode: 'assistant' | 'coordinator';
  status: 'idle' | 'active' | 'busy' | 'faulted' | 'archived';
  sessionKind?: 'conversation' | 'subagent_runtime';
  runtimeEngine?: 'classic' | 'durable';
  codemodeMode?: 'on' | 'only' | 'off';
  evaluationSnapshot?: boolean;
  surfaceKind?: 'agent' | 'extension_app' | 'builtin_app';
  ownerAppId?: string;
  surfaceKey?: string;
  roomParticipant?: {
    roomId: string;
    participantId: string;
    status: 'active' | 'muted' | 'removed';
  };
  roleId: string;
  roleVersion: string;
  roleBookRevisionId: string;
  modelProfile: string;
  thinkingLevel?: '' | 'off' | 'minimal' | 'low' | 'medium' | 'high' | 'xhigh' | 'max';
  toolProfileVersion: string;
  executionMode: 'read_only' | 'per_action' | 'workspace_managed' | 'full_trust';
  roomExecutionMode?: '' | 'room_unrestricted';
  workspaceScopeGranted: boolean;
  workspaceScopeSha256: string;
  workspaceScopeGrantedAtMs: number;
  toolAllowlistMode?: 'profile' | 'explicit';
  allowedTools?: string[];
  capabilityDisclosurePreferences: {
    [k: string]: 'inherit' | 'enabled' | 'disabled';
  };
  policyRevision: number;
  projectContextEnabled: boolean;
  piSkillsEnabled: boolean;
  codexSkillsEnabled: boolean;
  createdAtMs: number;
  updatedAtMs: number;
  lastOpenedAtMs?: number;
  archivedAtMs?: number | null;
  messageCount: number;
  lastMessagePreview?: string;
  lastTerminalTurnId?: string;
  workspaceRoots: string[];
  shellPolicyVersion?: string;
  [k: string]: unknown;
};
