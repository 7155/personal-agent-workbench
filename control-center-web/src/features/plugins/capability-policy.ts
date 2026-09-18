export type CapabilityPreference = 'inherit' | 'enabled' | 'disabled';
export type CapabilityEffective = 'enabled' | 'disabled';
export type CapabilityKind = 'tool' | 'skill' | 'extension';
export type CapabilityMutationStatus = 'pending' | 'succeeded' | 'failed';

export interface CapabilityMutationOutcome {
  canonicalId: string;
  preference: CapabilityPreference;
  status: CapabilityMutationStatus;
  message: string;
}

export type CapabilityCatalogItem = Record<string, unknown> & {
  id: string;
  canonicalId: string;
  kind: CapabilityKind;
  displayName: string;
  description: string;
  alwaysAvailable?: boolean;
  source: { kind: string; label: string };
  status: string;
  risk: string;
  requiredPermissions: string[];
  authorization: {
    state: 'authorized' | 'denied' | 'not_applicable';
    reason: string;
  };
  disclosure: {
    preference: CapabilityPreference;
    effective: CapabilityEffective;
    state: 'disclosed' | 'hidden';
    reason: string;
  };
  effectiveScope: 'scenario' | 'session' | 'project_default' | 'global_default' | 'built_in_default';
  reasons: string[];
  revision: string;
  effectiveAtMs: number;
};

export interface CapabilityCatalog {
  schemaVersion: 'rag-ime.capability-catalog.v1';
  ok: true;
  revision: string;
  effectiveAtMs: number;
  projectScope: {
    supported: boolean;
    identityKind: string;
    reason: string;
    projectId?: string;
  };
  sessionPolicy?: {
    sessionId: string;
    policyRevision: number;
    disclosurePreferences: {
      globalDefault: Record<string, CapabilityPreference>;
      projectDefault: Record<string, CapabilityPreference>;
      session: Record<string, CapabilityPreference>;
      effective: Record<string, CapabilityEffective>;
    };
    effectiveAtMs: number;
  };
  items: CapabilityCatalogItem[];
}

export interface CapabilityDefaultsSnapshot {
  revision: number;
  preferences: Record<string, CapabilityPreference>;
  projectPreferences: Record<string, Record<string, CapabilityPreference>>;
}

export const capabilityPreferenceOptions = [
  { value: 'inherit', label: '继承默认' },
  { value: 'enabled', label: '启用' },
  { value: 'disabled', label: '关闭' },
] as const;

export function parseCapabilityCatalog(value: unknown): CapabilityCatalog | null {
  const root = record(value);
  if (
    root.schemaVersion !== 'rag-ime.capability-catalog.v1'
    || root.ok !== true
    || typeof root.revision !== 'string'
    || typeof root.effectiveAtMs !== 'number'
    || !Array.isArray(root.items)
  ) return null;
  const projectScope = record(root.projectScope);
  if (typeof projectScope.supported !== 'boolean') return null;
  const items = root.items.map(parseCapabilityItem);
  if (items.some((item) => item === null)) return null;
  const sessionPolicy = parseSessionPolicy(root.sessionPolicy);
  if (root.sessionPolicy !== undefined && !sessionPolicy) return null;
  return {
    schemaVersion: 'rag-ime.capability-catalog.v1',
    ok: true,
    revision: root.revision,
    effectiveAtMs: root.effectiveAtMs,
    projectScope: {
      supported: projectScope.supported,
      identityKind: text(projectScope.identityKind),
      reason: text(projectScope.reason),
      ...(text(projectScope.projectId) ? { projectId: text(projectScope.projectId) } : {}),
    },
    ...(sessionPolicy ? { sessionPolicy } : {}),
    items: items as CapabilityCatalogItem[],
  };
}

export function requireCapabilityCatalog(value: unknown): CapabilityCatalog {
  const catalog = parseCapabilityCatalog(value);
  if (!catalog) {
    const root = record(value);
    if (root.ok === false) throw new Error('服务未能返回能力目录。请重试。');
    const observed = text(root.schemaVersion);
    if (!observed) throw new Error('能力目录缺少版本信息，暂时无法读取。请重试。');
    if (observed !== 'rag-ime.capability-catalog.v1') {
      throw new Error(`能力目录版本不兼容：当前界面需要 rag-ime.capability-catalog.v1，服务返回 ${observed}。请刷新 PAW 后重试。`);
    }
    throw new Error('能力目录数据不完整或格式异常，暂时无法读取功能状态。请重试。');
  }
  return catalog;
}

export function requireSessionCapabilityCatalog(value: unknown, sessionId: string): CapabilityCatalog {
  const catalog = requireCapabilityCatalog(value);
  if (!catalog.sessionPolicy) {
    throw new Error('能力目录缺少当前对话的临时设置；不会猜测覆盖关系或最终状态。');
  }
  if (catalog.sessionPolicy.sessionId !== sessionId) {
    throw new Error('能力目录不属于当前对话；不会显示或修改其他对话的设置。');
  }
  return catalog;
}

export function parseCapabilityDefaults(value: unknown): CapabilityDefaultsSnapshot | null {
  const envelope = record(record(value).configuration);
  const configuration = record(envelope.configuration);
  const defaults = record(configuration.sessionDefaults);
  if (typeof envelope.revision !== 'number') return null;
  const disclosure = record(configuration.capabilityDisclosure);
  return {
    revision: envelope.revision,
    preferences: preferenceMap(defaults.capabilityDisclosurePreferences),
    projectPreferences: projectPreferenceMap(disclosure.projectPreferences),
  };
}

export function preferenceMap(value: unknown): Record<string, CapabilityPreference> {
  return Object.fromEntries(Object.entries(record(value)).filter(
    (entry): entry is [string, CapabilityPreference] => isPreference(entry[1]),
  ));
}

export function preferenceLabel(value: CapabilityPreference): string {
  return capabilityPreferenceOptions.find((option) => option.value === value)?.label ?? '继承默认';
}

export function capabilityKindLabel(value: CapabilityKind): string {
  if (value === 'tool') return '工具';
  if (value === 'skill') return '技能';
  return '扩展';
}

export function capabilityScopeLabel(value: CapabilityCatalogItem['effectiveScope']): string {
  if (value === 'scenario') return '当前场景策略';
  if (value === 'session') return '当前对话临时设置';
  if (value === 'project_default') return '当前项目默认';
  if (value === 'global_default') return '所有对话默认';
  return '产品内置默认';
}

export function capabilityEffectiveLabel(value: CapabilityEffective): string {
  return value === 'enabled' ? '已启用' : '已关闭';
}

export function capabilityStatusLabel(value: string): string {
  if (value === 'online' || value === 'installed' || value === 'ready') return '当前可用';
  if (value === 'offline') return '暂时离线';
  if (value === 'disabled') return '已停用';
  if (value === 'unconfigured') return '待配置';
  if (value === 'available') return '可安装';
  return '状态未知';
}

export function capabilityRiskLabel(value: string): string {
  if (value === 'R0') return '只读';
  if (value === 'R1') return '受控变更';
  if (value === 'R2') return '写入或外部操作';
  if (value === 'R3') return '受禁区保护';
  return value || '风险未知';
}

export function projectScopeReason(reason: string): string {
  if (reason === 'session_workspace_scope') return '当前对话的授权工作区提供了稳定、仅含摘要的项目身份。';
  return reason === 'stable_project_identity_unavailable'
    ? '从伙伴对话进入此页并带上已授权工作区后，才能可靠保存当前项目默认。'
    : reason || '当前还没有可核对的项目范围。';
}

function parseCapabilityItem(value: unknown): CapabilityCatalogItem | null {
  const item = record(value);
  const source = record(item.source);
  const authorization = record(item.authorization);
  const disclosure = record(item.disclosure);
  if (
    typeof item.id !== 'string'
    || typeof item.canonicalId !== 'string'
    || !isKind(item.kind)
    || typeof item.displayName !== 'string'
    || typeof item.description !== 'string'
    || typeof source.kind !== 'string'
    || typeof source.label !== 'string'
    || typeof item.status !== 'string'
    || typeof item.risk !== 'string'
    || !stringArray(item.requiredPermissions)
    || !isAuthorizationState(authorization.state)
    || typeof authorization.reason !== 'string'
    || !isPreference(disclosure.preference)
    || !isEffective(disclosure.effective)
    || (disclosure.state !== 'disclosed' && disclosure.state !== 'hidden')
    || typeof disclosure.reason !== 'string'
    || !isScope(item.effectiveScope)
    || !stringArray(item.reasons)
    || typeof item.revision !== 'string'
    || typeof item.effectiveAtMs !== 'number'
  ) return null;
  return {
    ...item,
    id: item.id,
    canonicalId: item.canonicalId,
    kind: item.kind,
    displayName: item.displayName,
    description: item.description,
    source: { kind: source.kind, label: source.label },
    status: item.status,
    risk: item.risk,
    requiredPermissions: item.requiredPermissions,
    authorization: { state: authorization.state, reason: authorization.reason },
    disclosure: {
      preference: disclosure.preference,
      effective: disclosure.effective,
      state: disclosure.state,
      reason: disclosure.reason,
    },
    effectiveScope: item.effectiveScope,
    reasons: item.reasons,
    revision: item.revision,
    effectiveAtMs: item.effectiveAtMs,
  };
}

function parseSessionPolicy(value: unknown): CapabilityCatalog['sessionPolicy'] | undefined {
  if (value === undefined) return undefined;
  const policy = record(value);
  const preferences = record(policy.disclosurePreferences);
  if (
    typeof policy.sessionId !== 'string'
    || typeof policy.policyRevision !== 'number'
    || typeof policy.effectiveAtMs !== 'number'
  ) return undefined;
  return {
    sessionId: policy.sessionId,
    policyRevision: policy.policyRevision,
    disclosurePreferences: {
      globalDefault: preferenceMap(preferences.globalDefault),
      projectDefault: preferenceMap(preferences.projectDefault),
      session: preferenceMap(preferences.session),
      effective: effectiveMap(preferences.effective),
    },
    effectiveAtMs: policy.effectiveAtMs,
  };
}

function effectiveMap(value: unknown): Record<string, CapabilityEffective> {
  return Object.fromEntries(Object.entries(record(value)).filter(
    (entry): entry is [string, CapabilityEffective] => isEffective(entry[1]),
  ));
}

function isPreference(value: unknown): value is CapabilityPreference {
  return value === 'inherit' || value === 'enabled' || value === 'disabled';
}
function isEffective(value: unknown): value is CapabilityEffective { return value === 'enabled' || value === 'disabled'; }
function isKind(value: unknown): value is CapabilityKind { return value === 'tool' || value === 'skill' || value === 'extension'; }
function isScope(value: unknown): value is CapabilityCatalogItem['effectiveScope'] { return value === 'scenario' || value === 'session' || value === 'project_default' || value === 'global_default' || value === 'built_in_default'; }
function isAuthorizationState(value: unknown): value is CapabilityCatalogItem['authorization']['state'] { return value === 'authorized' || value === 'denied' || value === 'not_applicable'; }
function projectPreferenceMap(value: unknown): Record<string, Record<string, CapabilityPreference>> {
  return Object.fromEntries(Object.entries(record(value)).map(([projectId, preferences]) => [
    projectId,
    preferenceMap(preferences),
  ]));
}
function stringArray(value: unknown): value is string[] { return Array.isArray(value) && value.every((item) => typeof item === 'string'); }
function text(value: unknown): string { return typeof value === 'string' ? value : ''; }
function record(value: unknown): Record<string, unknown> { return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {}; }
