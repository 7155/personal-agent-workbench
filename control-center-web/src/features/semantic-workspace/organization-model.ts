/** Organization is a view over real spaces, never a replacement Runtime. */
export const CATEGORY_LABELS = {
  active: '正在推进', waiting: '等待条件', incubating: '构思想法', reference: '参考资料', unknown: '未分类',
} as const;
export type Category = keyof typeof CATEGORY_LABELS;
export type Placement = 'desk' | 'shelf';
export interface Space {
  key: string; title: string; category: Category; placement: Placement;
  pinned: boolean; group: string; revision: number;
  sourceRevision?: string; updatedAtMs: number | null;
}
export interface Receipt { id: string; spaceKey: string; appliedRevision?: number; createdAtMs?: number }
export interface Proposal {
  id: string; spaceKey: string; category: Category; basis: string; expiresAtMs: number;
  expectedRevision?: number; sourceRevision?: string;
}
export interface Catalog {
  items: Space[]; receipts: Receipt[]; unavailable: string[]; failedKeys: string[];
  requestedCount: number;
  readOnlyReason?: string;
}
export const EMPTY_CATALOG: Catalog = { items: [], receipts: [], unavailable: [], failedKeys: [], requestedCount: 0 };
export type Change =
  | { operation: 'category'; value: Category }
  | { operation: 'placement'; value: Placement }
  | { operation: 'pinned'; value: boolean }
  | { operation: 'group'; value: string }
  | { operation: 'proposal'; value: string };
export type CommandBody = { commandId: string; spaceKey: string; expectedRevision: number } & Change;
export type OrganizationRequest = {
  pathId: 'agent.organization.read'; body: { keys: string[] }; signal?: AbortSignal; timeoutMs?: number;
} | {
  pathId: 'agent.organization.suggest'; body: { spaceKey: string }; signal?: AbortSignal; timeoutMs?: number;
} | {
  pathId: 'agent.organization.command'; body: CommandBody; signal?: AbortSignal; timeoutMs?: number;
} | {
  pathId: 'agent.organization.undo'; body: { receiptId: string }; signal?: AbortSignal; timeoutMs?: number;
};
export type RequestOrganization = (request: OrganizationRequest) => Promise<unknown>;

export function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('整理服务响应格式无效。');
  return value as Record<string, unknown>;
}
export function isCategory(value: unknown): value is Category {
  return typeof value === 'string' && Object.hasOwn(CATEGORY_LABELS, value);
}
export function isSpaceKey(value: unknown): value is string {
  if (typeof value !== 'string' || value.length > 300 || /[\u0000-\u001f\u007f]/u.test(value)) return false;
  const split = value.indexOf(':');
  const kind = value.slice(0, split); const id = value.slice(split + 1);
  return split > 0 && (kind === 'session' || kind === 'room') && id.length > 0 && id === id.trim();
}
export function spaceKeys(values: readonly string[]): string[] {
  if (!values.every(isSpaceKey)) throw new Error('目录包含无效的空间标识。');
  return [...new Set(values)].sort();
}
export function integer(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
}
export function ok(value: unknown): Record<string, unknown> {
  const data = record(value);
  if (data.ok !== true) throw new Error(typeof data.error === 'string' ? data.error : '整理服务没有确认操作结果。');
  return data;
}
export function parseSpace(raw: unknown): Space {
  const item = record(raw);
  if (!isSpaceKey(item.key) || typeof item.title !== 'string' || !isCategory(item.category)
    || (item.placement !== 'desk' && item.placement !== 'shelf') || typeof item.pinned !== 'boolean'
    || typeof item.group !== 'string' || item.group.length > 80 || !integer(item.revision)
    || (item.pinned && item.placement === 'shelf')
    || (item.sourceRevision !== undefined && typeof item.sourceRevision !== 'string')
    || (item.updatedAtMs !== undefined && item.updatedAtMs !== null && !integer(item.updatedAtMs))) {
    throw new Error('空间整理状态格式无效，未用空状态替代。');
  }
  return { key: item.key, title: item.title, category: item.category, placement: item.placement,
    pinned: item.pinned, group: item.group, revision: item.revision,
    sourceRevision: item.sourceRevision as string | undefined,
    updatedAtMs: integer(item.updatedAtMs) ? item.updatedAtMs : null };
}
export function parseBatch(value: unknown, requestedKeys: readonly string[]): Omit<Catalog, 'failedKeys' | 'requestedCount'> {
  const data = ok(value); const requested = new Set(requestedKeys);
  if (!Array.isArray(data.items) || !Array.isArray(data.unavailable) || !Array.isArray(data.receipts)) {
    throw new Error('整理目录响应不完整。');
  }
  const items = data.items.map(parseSpace); const seen = new Set<string>();
  for (const item of items) {
    if (!requested.has(item.key) || seen.has(item.key)) throw new Error('整理目录返回了重复或越界的空间。');
    seen.add(item.key);
  }
  const unavailable: string[] = [];
  for (const key of data.unavailable) {
    if (!isSpaceKey(key) || !requested.has(key) || seen.has(key)) throw new Error('不可用空间响应无效。');
    seen.add(key); unavailable.push(key);
  }
  if (seen.size !== requested.size) throw new Error('整理目录缺少部分空间的读取结果。');
  const byKey = new Map(items.map(item => [item.key, item])); const receiptIds = new Set<string>();
  const receipts = data.receipts.map((raw): Receipt => {
    const row = record(raw);
    if (typeof row.id !== 'string' || !row.id || row.id.length > 100 || !isSpaceKey(row.spaceKey)
      || !byKey.has(row.spaceKey) || receiptIds.has(row.id)
      || (row.appliedRevision !== undefined && (!integer(row.appliedRevision) || row.appliedRevision !== byKey.get(row.spaceKey)!.revision))
      || (row.createdAtMs !== undefined && !integer(row.createdAtMs))) throw new Error('整理回执格式无效。');
    receiptIds.add(row.id);
    return { id: row.id, spaceKey: row.spaceKey, appliedRevision: row.appliedRevision as number | undefined,
      createdAtMs: row.createdAtMs as number | undefined };
  });
  return { items, unavailable, receipts,
    readOnlyReason: typeof data.readOnlyReason === 'string' ? data.readOnlyReason : undefined };
}
export function parseProposal(value: unknown, item: Space, now = Date.now()): Proposal | null {
  const data = ok(value);
  if (data.proposal === null) return null;
  const p = record(data.proposal);
  if (typeof p.id !== 'string' || !p.id || p.id.length > 100 || p.spaceKey !== item.key || !isCategory(p.category)
    || p.category === 'unknown' || typeof p.basis !== 'string' || !integer(p.expiresAtMs) || p.expiresAtMs <= now
    || (p.expectedRevision !== undefined && p.expectedRevision !== item.revision)
    || (p.sourceRevision !== undefined && (typeof p.sourceRevision !== 'string'
      || (item.sourceRevision !== undefined && p.sourceRevision !== item.sourceRevision)))) throw new Error('建议过期或格式无效，未应用任何修改。');
  return { id: p.id, spaceKey: item.key, category: p.category, basis: p.basis, expiresAtMs: p.expiresAtMs,
    expectedRevision: p.expectedRevision as number | undefined, sourceRevision: p.sourceRevision as string | undefined };
}

const normalize = (value: string) => value.normalize('NFKC').toLocaleLowerCase().trim().replace(/\s+/gu, ' ');
export function selectSpaces(items: readonly Space[], options: {
  query: string; placement: Placement | 'all'; category?: Category | 'all'; sort?: 'recent' | 'title';
}): Space[] {
  const terms = normalize(options.query).split(' ').filter(Boolean);
  return items.filter(item => {
    const haystack = normalize(`${item.title} ${item.group} ${CATEGORY_LABELS[item.category]}`);
    return (options.placement === 'all' || item.placement === options.placement)
      && (!options.category || options.category === 'all' || item.category === options.category)
      && terms.every(term => haystack.includes(term));
  }).sort((a, b) => Number(b.pinned) - Number(a.pinned)
    || (options.sort === 'title' ? 0 : (b.updatedAtMs ?? -1) - (a.updatedAtMs ?? -1))
    || a.title.localeCompare(b.title, 'zh-CN') || a.key.localeCompare(b.key));
}
export function pageItems<T>(items: readonly T[], page: number, size = 25): { items: T[]; page: number; pages: number } {
  if (!integer(size) || size < 1 || !integer(page)) throw new Error('分页参数无效。');
  const pages = Math.max(1, Math.ceil(items.length / size)); const current = Math.min(page, pages - 1);
  return { items: items.slice(current * size, (current + 1) * size), page: current, pages };
}
