import type { ControlTransport } from '@/platform/transport';

/** Follow the stable date/id cursor so old imported conversations remain searchable. */
export async function readSessionCatalog(transport: ControlTransport, includeArchived: boolean, isCurrent = () => true) {
  const items: unknown[] = [];
  const seen = new Set<string>();
  let cursor: { beforeUpdatedAtMs: number; beforeId: string } | undefined;
  while (isCurrent()) {
    const page = await transport.request({ pathId: 'agent.sessions.list', query: { limit: 100, includeArchived, ...cursor } }) as Record<string, unknown>;
    if (!Array.isArray(page.items)) throw new Error('工作记录格式异常，请重试。');
    items.push(...page.items);
    if (page.hasMore !== true) return { ...page, items };
    const time = page.nextBeforeUpdatedAtMs;
    const id = page.nextBeforeId;
    const key = `${time}:${id}`;
    if (typeof time !== 'number' || typeof id !== 'string' || !id || seen.has(key)) {
      throw new Error('工作记录分页异常，请重新读取。');
    }
    seen.add(key);
    cursor = { beforeUpdatedAtMs: time, beforeId: id };
  }
  return { items };
}
