import type { ControlTransport } from '@/platform/transport';
import { sessionItems } from '@/features/agent/types';

type SessionCatalogPage = Record<string, unknown> & { items: unknown[] };

/** Follow the stable date/id cursor so old imported conversations remain searchable. */
export async function readSessionCatalog(
  transport: ControlTransport,
  includeArchived: boolean,
  isCurrent = () => true,
  signal?: AbortSignal,
  onPage?: (page: SessionCatalogPage) => void,
  stopWhen?: (page: SessionCatalogPage) => boolean,
) {
  const items: unknown[] = [];
  const seen = new Set<string>();
  let cursor: { beforeUpdatedAtMs: number; beforeId: string } | undefined;
  while (isCurrent()) {
    signal?.throwIfAborted();
    const page = await transport.request({ pathId: 'agent.sessions.list', query: { limit: 100, includeArchived, ...cursor }, signal }) as Record<string, unknown>;
    signal?.throwIfAborted();
    if (!isCurrent()) break;
    if (!Array.isArray(page.items)) throw new Error('工作记录格式异常，请重试。');
    items.push(...page.items);
    if (page.hasMore !== true || stopWhen?.({ ...page, items })) {
      onPage?.({ ...page, items: [...items] });
      return { ...page, items };
    }
    const time = page.nextBeforeUpdatedAtMs;
    const id = page.nextBeforeId;
    const key = `${time}:${id}`;
    if (typeof time !== 'number' || typeof id !== 'string' || !id || seen.has(key)) {
      throw new Error('工作记录分页异常，请重新读取。');
    }
    seen.add(key);
    cursor = { beforeUpdatedAtMs: time, beforeId: id };
    // Publish a stable first paint while the complete, searchable directory
    // continues paging. Existing callers still await the full result.
    onPage?.({ ...page, items: [...items] });
  }
  return { items };
}

/** Read canonical metadata for one original identity, including older/archived rows. */
export async function readSelectedSession(
  transport: ControlTransport,
  sessionId: string,
  isCurrent = () => true,
  signal?: AbortSignal,
) {
  const findSelected = (page: unknown) => sessionItems(page, { includeAppOwned: true }).find((item) => item.id === sessionId);
  const page = await readSessionCatalog(transport, true, isCurrent, signal, undefined, (value) => Boolean(findSelected(value)));
  if (!isCurrent()) return;
  const selected = findSelected(page);
  if (selected?.evaluationSnapshot !== undefined && typeof selected.evaluationSnapshot !== 'boolean') {
    throw new Error('Session 工作记录格式异常，请重新读取。');
  }
  return selected;
}
