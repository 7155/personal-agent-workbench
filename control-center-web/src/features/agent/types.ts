import type { AgentModelCatalogV1 } from '@/contracts/generated/agent-model-catalog.v1';
import type { AgentPersonaV1 } from '@/contracts/generated/agent-persona.v1';
import type { AgentSessionV1 } from '@/contracts/generated/agent-session.v1';
import type { ControlToolManifestV1 } from '@/contracts/generated/control-tool-manifest.v1';
import type { PickedFile } from '@/platform/transport';

export type SessionSummary = Pick<
  AgentSessionV1,
  | 'id'
  | 'title'
  | 'mode'
  | 'status'
  | 'roleId'
  | 'roleVersion'
  | 'roleBookRevisionId'
  | 'updatedAtMs'
  | 'workspaceRoots'
> &
  Partial<Pick<
    AgentSessionV1,
    | 'lastMessagePreview'
    | 'lastTerminalTurnId'
    | 'messageCount'
    | 'modelProfile'
    | 'toolProfileVersion'
    | 'executionMode'
    | 'workspaceScopeGranted'
    | 'toolAllowlistMode'
    | 'allowedTools'
    | 'projectContextEnabled'
    | 'piSkillsEnabled'
    | 'codexSkillsEnabled'
    | 'roomParticipant'
    | 'surfaceKind'
    | 'ownerAppId'
    | 'surfaceKey'
    | 'evaluationSnapshot'
  >> & {
    /** TeamGateway adds ownership and audience to project Session projections. */
    ownerUserId?: string;
    spaceId?: string;
    canControl?: boolean;
    audience?: 'owner' | 'project' | string;
  };

export interface AgentPermissionSelection {
  mode: 'assistant' | 'coordinator';
  toolProfileVersion: (
    | 'control-center-v1'
    | 'control-center-full-access-v1'
    | 'subagent-readonly-v1'
    | 'control-center-auto-approve-v1'
  );
  executionMode: 'read_only' | 'per_action' | 'workspace_managed' | 'full_trust';
  /** System-wide presets add `/`; scoped presets retain only selected roots. */
  workspaceRoots?: string[];
  workspaceScopeConfirmed?: boolean;
  dangerousModeConfirmed?: boolean;
}

export interface AgentSessionListResponse {
  ok: boolean;
  items?: SessionSummary[];
  sessions?: SessionSummary[];
  activeSessionId?: string | null;
}

export interface AgentRoleListResponse {
  ok: boolean;
  items?: AgentPersonaV1[];
  roles?: AgentPersonaV1[];
}

export interface ComposerAttachment extends PickedFile {
  source: 'picker' | 'clipboard' | 'path';
  /** Browser-only bytes used for an immediate thumbnail after paste/import. */
  previewFile?: File;
}

export type ThinkingLevel = AgentModelCatalogV1['thinkingLevel'];
export type ModelCatalog = AgentModelCatalogV1;
export type ToolManifest = ControlToolManifestV1;

export type AgentCommandSource = 'extension' | 'prompt' | 'skill';

export type AgentProductCommandName =
  | 'new'
  | 'resume'
  | 'branch'
  | 'name'
  | 'compact'
  | 'model'
  | 'thinking'
  | 'permissions'
  | 'tools'
  | 'session'
  | 'status'
  | 'subagents'
  | 'settings'
  | 'hotkeys'
  | 'stop'
  | 'help';

export interface AgentCommand {
  name: string;
  invocation: string;
  description: string;
  source: AgentCommandSource;
}

export function sessionItems(
  value: unknown,
  options: { includeAppOwned?: boolean } = {},
): SessionSummary[] {
  if (!isRecord(value)) return [];
  const source = Array.isArray(value.items)
    ? value.items
    : Array.isArray(value.sessions)
      ? value.sessions
      : [];
  // Delegated workers have durable runtime records for recovery and audit, but
  // they are not user conversations. Keep them out of the conversation rail
  // even when an older backend includes them in the generic session response.
  return source.filter((item): item is SessionSummary => (
    isSessionSummary(item)
    && !isTransientSubagentSession(item)
    && (options.includeAppOwned || !isAppOwnedSession(item))
  ));
}

export function sessionPermissionLabel(session: SessionSummary): string {
  if (session.toolProfileVersion === 'control-center-full-access-v1') return '完全访问';
  if (session.toolProfileVersion === 'control-center-auto-approve-v1') return '全自动';
  const executionMode = session.executionMode
    ?? (session.toolProfileVersion === 'subagent-readonly-v1'
      ? 'read_only'
      : 'per_action');
  return {
    read_only: '只读（沙箱）',
    per_action: '写入与命令确认（旧配置）',
    workspace_managed: '工作区托管（沙箱）',
    full_trust: '全自动（旧配置）',
  }[executionMode];
}

export function activeSessionId(value: unknown): string {
  if (!isRecord(value)) return '';
  return typeof value.activeSessionId === 'string' ? value.activeSessionId : '';
}

export function roleItems(value: unknown): AgentPersonaV1[] {
  if (!isRecord(value)) return [];
  const source = Array.isArray(value.items)
    ? value.items
    : Array.isArray(value.roles)
      ? value.roles
      : [];
  return source.filter(isPersona);
}

export function toolItems(value: unknown): ToolManifest[] {
  if (!isRecord(value)) return [];
  const source = Array.isArray(value.items) ? value.items : [];
  return source.filter(isToolManifest);
}

export function commandItems(value: unknown): AgentCommand[] {
  if (!isRecord(value) || value.schemaVersion !== 'rag-ime.agent-command-catalog.v1') return [];
  const source = Array.isArray(value.items) ? value.items : [];
  return source.filter(isAgentCommand).slice(0, 200);
}

export function isModelCatalog(value: unknown): value is ModelCatalog {
  return (
    isRecord(value) &&
    value.schemaVersion === 'rag-ime.agent-model-catalog.v1' &&
    Array.isArray(value.providers)
  );
}

function isSessionSummary(value: unknown): value is SessionSummary {
  return (
    isRecord(value) &&
    typeof value.id === 'string' &&
    typeof value.title === 'string' &&
    typeof value.updatedAtMs === 'number'
  );
}

function isTransientSubagentSession(value: unknown): boolean {
  return isRecord(value) && value.sessionKind === 'subagent_runtime';
}

function isAppOwnedSession(value: unknown): boolean {
  if (!isRecord(value) || typeof value.surfaceKind !== 'string') return false;
  return Boolean(value.surfaceKind) && value.surfaceKind !== 'agent';
}

function isAgentCommand(value: unknown): value is AgentCommand {
  if (!isRecord(value)) return false;
  const source = value.source;
  const name = typeof value.name === 'string' ? value.name : '';
  return (
    (source === 'extension' || source === 'prompt' || source === 'skill')
    && typeof value.description === 'string'
    && typeof value.invocation === 'string'
    && value.invocation === `/${name}`
    && name.length > 0
    && name.length <= 80
    && !/[\s/\\]/u.test(name)
  );
}

function isPersona(value: unknown): value is AgentPersonaV1 {
  return (
    isRecord(value) &&
    value.schemaVersion === 'rag-ime.agent-persona.v1' &&
    typeof value.roleId === 'string' &&
    typeof value.displayName === 'string'
  );
}

function isToolManifest(value: unknown): value is ToolManifest {
  return (
    isRecord(value) &&
    value.schemaVersion === 'rag-ime.control-tool-manifest.v1' &&
    typeof value.id === 'string' &&
    typeof value.displayName === 'string' &&
    typeof value.description === 'string' &&
    Array.isArray(value.sessionModes) &&
    Array.isArray(value.operations)
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
