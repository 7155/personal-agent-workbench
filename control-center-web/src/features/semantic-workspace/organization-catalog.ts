import { parseBatch, spaceKeys, type Catalog, type RequestOrganization } from './organization-model';
import { requestOrganization } from './organization-request';

export function assertNotAborted(signal: AbortSignal) {
  if (signal.aborted) throw new DOMException('读取已取消', 'AbortError');
}
/** Read the complete caller-supplied catalog, not only the visible UI page.
 * This does not assert that the parent has enumerated every backend Room.
 * Concurrency is bounded; no Jev calls occur while loading/searching.
 */
export async function loadOrganizationCatalog(request: RequestOrganization, input: readonly string[], signal: AbortSignal,
  options: { concurrency?: number; maxKeys?: number } = {}): Promise<Catalog> {
  const keys = spaceKeys(input); const concurrency = options.concurrency ?? 3;
  if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 8) throw new Error('目录并发配置无效。');
  const maxKeys = options.maxKeys ?? 10_000;
  if (!Number.isInteger(maxKeys) || maxKeys < 1 || keys.length > maxKeys) throw new Error('目录过大，请按项目缩小范围；没有静默截断搜索。');
  assertNotAborted(signal);
  const batches = Array.from({ length: Math.ceil(keys.length / 100) }, (_, i) => keys.slice(i * 100, (i + 1) * 100));
  const results: Array<ReturnType<typeof parseBatch> | null> = Array(batches.length).fill(null);
  let cursor = 0;
  async function worker() {
    for (;;) {
      assertNotAborted(signal);
      const index = cursor++;
      if (index >= batches.length) return;
      try {
        const value = await requestOrganization(request, { pathId: 'agent.organization.read', body: { keys: batches[index] }, signal, timeoutMs: 15_000 });
        assertNotAborted(signal);
        results[index] = parseBatch(value, batches[index]);
      } catch (error) {
        assertNotAborted(signal);
        // Record exactly which batch failed. No stale items masquerade as fresh.
        results[index] = null;
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(concurrency, batches.length) }, worker));
  assertNotAborted(signal);
  const catalog: Catalog = { items: [], receipts: [], unavailable: [], failedKeys: [], requestedCount: keys.length };
  results.forEach((batch, index) => {
    if (!batch) { catalog.failedKeys.push(...batches[index]); return; }
    catalog.items.push(...batch.items); catalog.receipts.push(...batch.receipts); catalog.unavailable.push(...batch.unavailable);
    if (batch.readOnlyReason) catalog.readOnlyReason = batch.readOnlyReason;
  });
  catalog.receipts.sort((a, b) => (b.createdAtMs ?? 0) - (a.createdAtMs ?? 0) || a.id.localeCompare(b.id));
  return catalog;
}
