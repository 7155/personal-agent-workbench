import type { CapabilityCatalog, CapabilityCatalogItem, CapabilityPreference } from '@/features/plugins/capability-policy';
import { capabilityScopeLabel } from '@/features/plugins/capability-policy';
import type { SessionSummary, ToolManifest } from '../types';
import { toolAvailableForConversation, riskLabel } from './tool-policy';

/** Display values only. Availability still comes from the existing Session policy owner. */
export type CapabilitySection = 'all' | 'tool' | 'skill' | 'extension' | 'memory';
export type CapabilityFilter = 'all' | 'usable' | 'unavailable';
export type CapabilityDisplayState = 'usable' | 'disabled' | 'denied' | 'unavailable' | 'unknown';
export interface CapabilityDisplayRow {
  key: string; id: string; kind: 'tool' | 'skill' | 'extension';
  name: string; description: string; source: string; resourceLabel: string;
  state: CapabilityDisplayState; stateLabel: string; scope: string;
  preference: CapabilityPreference; disclosure: string; authorization: string;
  risk: string; permissions: string[]; reasons: string[]; revision: string;
  configurable: boolean; tool?: ToolManifest;
}
const RESOURCE_LABELS: Readonly<Record<string, string>> = {
  installed: '已安装', online: '在线', ready: '已就绪', offline: '离线',
  unconfigured: '待配置', available: '可安装', disabled: '已停用', removed: '已移除',
};
export function toolDisplayName(tool: ToolManifest): { name: string; description: string } {
  if (tool.id === 'memory') return { name: '记忆召回', description: '按需要把相关记忆加入对话。关闭只影响后续使用，不删除已保存的记忆。' };
  if (tool.id === 'knowledge') return { name: '知识库 / Agent RAG', description: '按当前问题检索已允许的知识库，保留资料来源。' };
  return { name: tool.displayName, description: tool.description };
}
function rowState(item: CapabilityCatalogItem | undefined, available: boolean): CapabilityDisplayState {
  if (item?.status === 'removed') return 'unavailable';
  if (item?.disclosure.effective === 'disabled' && item.effectiveScope !== 'scenario') return 'disabled';
  if (item?.authorization.state === 'denied') return 'denied';
  if (item?.disclosure.effective === 'disabled') return 'disabled';
  if (available) return 'usable';
  return item ? 'unavailable' : 'unknown';
}
export function buildCapabilityRows(tools: readonly ToolManifest[], session: SessionSummary | undefined,
  catalog: CapabilityCatalog | undefined, sessionId: string | undefined): CapabilityDisplayRow[] {
  if (catalog?.sessionPolicy && catalog.sessionPolicy.sessionId !== sessionId) return [];
  const itemById = new Map(catalog?.items.map(item => [item.canonicalId, item]) ?? []);
  const confirmed = Boolean(sessionId && catalog?.sessionPolicy?.sessionId === sessionId);
  const rows: CapabilityDisplayRow[] = []; const seen = new Set<string>();
  function make(id: string, kind: CapabilityDisplayRow['kind'], name: string, description: string,
    item: CapabilityCatalogItem | undefined, available: boolean, tool?: ToolManifest): CapabilityDisplayRow {
    const state = rowState(item, available);
    return { key: item?.canonicalId ?? `${kind}:${id}`, id, kind, name, description,
      source: item?.source.label || '来源未提供',
      resourceLabel: kind === 'tool' && item?.status === 'available' ? '已登记' : RESOURCE_LABELS[item?.status ?? tool?.availability ?? ''] || '资源状态未提供',
      state, stateLabel: ({ usable: kind === 'tool' ? '当前可用' : '已提供给对话', disabled: '当前已关闭',
        denied: '受权限限制', unavailable: '当前不可用', unknown: '状态待核实' })[state],
      scope: item ? capabilityScopeLabel(item.effectiveScope) : '使用范围未提供',
      preference: item ? catalog?.sessionPolicy?.disclosurePreferences.session[item.canonicalId] ?? 'inherit' : 'inherit',
      disclosure: item ? item.disclosure.state === 'disclosed' ? '已披露' : '未披露' : '未提供',
      authorization: state === 'disabled' ? '已关闭，启用后核对可用性' : item ? item.authorization.state === 'authorized' ? '已授权' : item.authorization.state === 'denied' ? '未获授权' : '不涉及单独授权' : '未提供',
      risk: riskLabel(item?.risk || tool?.riskLevel || ''), permissions: item?.requiredPermissions ?? [],
      reasons: Array.from(new Set([...(item?.reasons ?? []), item?.authorization.reason, item?.disclosure.reason].filter((v): v is string => Boolean(v)))),
      revision: item?.revision ?? '', configurable: confirmed && Boolean(item) && item?.status !== 'removed',
      ...(tool ? { tool } : {}),
    };
  }
  for (const tool of tools) {
    if (!tool.id.trim() || seen.has(`tool:${tool.id}`)) continue;
    seen.add(`tool:${tool.id}`);
    const item = itemById.get(`tool:${tool.id}`) ?? catalog?.items.find(value => value.kind === 'tool' && value.id === tool.id);
    const display = toolDisplayName(tool);
    rows.push(make(tool.id, 'tool', display.name, display.description, item,
      toolAvailableForConversation(tool, session, catalog, sessionId), tool));
  }
  for (const item of catalog?.items ?? []) {
    if (seen.has(`${item.kind}:${item.id}`)) continue;
    seen.add(`${item.kind}:${item.id}`);
    // Disclosure is not installation, and installation is not evidence of a running invocation.
    const available = item.kind !== 'tool' && ['online', 'ready', 'installed'].includes(item.status)
      && item.disclosure.effective === 'enabled' && item.disclosure.state === 'disclosed'
      && item.authorization.state !== 'denied';
    rows.push(make(item.id, item.kind, item.displayName, item.description, item, available));
  }
  return rows;
}
export function filterCapabilityRows(rows: readonly CapabilityDisplayRow[], section: CapabilitySection,
  filter: CapabilityFilter, query: string): CapabilityDisplayRow[] {
  const term = query.trim().toLocaleLowerCase();
  return rows.filter(row => (section === 'all' || section === 'memory' ? section !== 'memory' || row.id === 'memory' : row.kind === section)
    && (filter === 'all' || (filter === 'usable' ? row.state === 'usable' : row.state !== 'usable'))
    && (!term || [row.name, row.description, row.id, row.source, row.tool?.displayName ?? '', row.tool?.description ?? '', ...row.permissions].some(value => value.toLocaleLowerCase().includes(term))));
}
