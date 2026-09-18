import { expect, it, vi } from 'vitest';
import type { ControlTransport } from '@/platform/transport';
import { readSessionCatalog } from './session-catalog';

it('includes old imported conversations beyond the first page', async () => {
  const request = vi.fn().mockResolvedValueOnce({ items: [{ id: 'recent' }], hasMore: true, nextBeforeUpdatedAtMs: 42, nextBeforeId: 'recent' })
    .mockResolvedValueOnce({ items: [{ id: 'codex-import' }], hasMore: false });
  const result = await readSessionCatalog({ request } as unknown as ControlTransport, false);
  expect(result.items).toEqual([{ id: 'recent' }, { id: 'codex-import' }]);
  expect(request.mock.calls[1][0].query).toMatchObject({ beforeUpdatedAtMs: 42, beforeId: 'recent', includeArchived: false });
});

it('rejects a repeating cursor instead of looping forever', async () => {
  const request = vi.fn().mockResolvedValue({ items: [], hasMore: true, nextBeforeUpdatedAtMs: 42, nextBeforeId: 'same' });
  await expect(readSessionCatalog({ request } as unknown as ControlTransport, false)).rejects.toThrow('分页异常');
  expect(request).toHaveBeenCalledTimes(2);
});
