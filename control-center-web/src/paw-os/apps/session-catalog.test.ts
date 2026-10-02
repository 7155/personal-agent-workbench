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

it('passes an abort signal and does not request a later page after leaving the catalog', async () => {
  let resolveFirst!: (value: unknown) => void;
  const first = new Promise<unknown>((resolve) => { resolveFirst = resolve; });
  const request = vi.fn().mockReturnValueOnce(first);
  const controller = new AbortController();
  const reading = readSessionCatalog({ request } as unknown as ControlTransport, false, () => true, controller.signal);

  expect(request.mock.calls[0]?.[0].signal).toBe(controller.signal);
  controller.abort();
  resolveFirst({ items: [{ id: 'recent' }], hasMore: true, nextBeforeUpdatedAtMs: 42, nextBeforeId: 'recent' });
  await expect(reading).rejects.toMatchObject({ name: 'AbortError' });
  expect(request).toHaveBeenCalledTimes(1);
});

it('publishes immutable accumulated pages before the complete catalog resolves', async () => {
  let resolveLater!: (value: unknown) => void;
  const later = new Promise((resolve) => { resolveLater = resolve; });
  const request = vi.fn().mockResolvedValueOnce({ items: [{ id: 'recent' }], hasMore: true, nextBeforeUpdatedAtMs: 42, nextBeforeId: 'recent' })
    .mockReturnValueOnce(later);
  const onPage = vi.fn();
  const reading = readSessionCatalog({ request } as unknown as ControlTransport, false, () => true, undefined, onPage);
  await vi.waitFor(() => expect(request).toHaveBeenCalledTimes(2));
  expect(onPage).toHaveBeenCalledTimes(1);
  const first = onPage.mock.calls[0][0];
  expect(first.items).toEqual([{ id: 'recent' }]);
  resolveLater({ items: [{ id: 'older' }], hasMore: false });
  expect((await reading).items).toEqual([{ id: 'recent' }, { id: 'older' }]);
  expect(onPage).toHaveBeenCalledTimes(2);
  expect(first.items).toEqual([{ id: 'recent' }]);
});

it('does not publish a late page for a superseded directory request', async () => {
  let current = true;
  let resolvePage!: (value: unknown) => void;
  const request = vi.fn().mockReturnValue(new Promise((resolve) => { resolvePage = resolve; }));
  const onPage = vi.fn();
  const reading = readSessionCatalog({ request } as unknown as ControlTransport, false, () => current, undefined, onPage);
  current = false;
  resolvePage({ items: [{ id: 'stale' }], hasMore: false });
  await reading;
  expect(onPage).not.toHaveBeenCalled();
});
