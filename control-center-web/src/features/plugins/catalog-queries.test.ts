import { QueryClient, QueryObserver } from '@tanstack/react-query';
import { describe, expect, it, vi } from 'vitest';
import { observeCatalogQuery, prepareCatalogRefresh, readCatalogQuery } from './catalog-queries';

describe('shared catalog read leases', () => {
  it.each(['imperative', 'disabled'] as const)('keeps a pre-receipt %s read stale after it finishes', async kind => {
    const client = new QueryClient();
    let finishOld!: (value: string) => void;
    const old = new Promise<string>(resolve => { finishOld = resolve; });
    const queryFn = vi.fn().mockReturnValueOnce(old).mockResolvedValue('after receipt');
    const options = { queryKey: ['catalog', kind], queryFn, staleTime: 30_000 };
    const observer = kind === 'disabled' ? new QueryObserver(client, { ...options, enabled: false }) : undefined;
    const unsubscribe = observer?.subscribe(() => undefined);
    try {
      const first = kind === 'imperative' ? readCatalogQuery(client, options) : client.fetchQuery(options);
      const refreshed = prepareCatalogRefresh(client, [options.queryKey])();
      // An inactive query must not eagerly issue a second read, but the old
      // success must not erase receipt invalidation for the next consumer.
      await Promise.resolve(); await Promise.resolve();
      expect(queryFn).toHaveBeenCalledTimes(1);
      finishOld('before receipt');
      await Promise.all([first, refreshed]);
      expect(client.getQueryState(options.queryKey)?.isInvalidated).toBe(true);
      await expect(readCatalogQuery(client, options)).resolves.toBe('after receipt');
      expect(queryFn).toHaveBeenCalledTimes(2);
    } finally { unsubscribe?.(); client.clear(); }
  });

  it('keeps a mounted projection current until its lifecycle signal releases the observer', async () => {
    const client = new QueryClient(); const controller = new AbortController();
    let revision = 0;
    const options = { queryKey: ['catalog', 'observed'], queryFn: vi.fn(async () => ++revision), staleTime: 0 };
    const onData = vi.fn();
    await observeCatalogQuery(client, options, controller.signal, { onData, onError: vi.fn() });
    expect(onData).toHaveBeenLastCalledWith(1);
    expect(client.getQueryCache().find({ queryKey: options.queryKey })?.getObserversCount()).toBe(1);
    await client.invalidateQueries({ queryKey: options.queryKey }, { cancelRefetch: false });
    expect(onData).toHaveBeenLastCalledWith(2);
    controller.abort();
    expect(client.getQueryCache().find({ queryKey: options.queryKey })?.getObserversCount()).toBe(0);
    await client.invalidateQueries({ queryKey: options.queryKey });
    expect(options.queryFn).toHaveBeenCalledTimes(2);
  });

  it('coalesces concurrent receipt barriers into one read after the original flight', async () => {
    const client = new QueryClient(); const first = new AbortController(); const second = new AbortController();
    let finishOld!: (value: string) => void;
    const old = new Promise<string>(resolve => { finishOld = resolve; });
    let count = 0;
    const options = { queryKey: ['catalog', 'trailing'], queryFn: () => ++count === 1 ? old : Promise.resolve('after receipt'), staleTime: 0 };
    const aData = vi.fn(); const bData = vi.fn();
    const a = observeCatalogQuery(client, options, first.signal, { onData: aData, onError: vi.fn() });
    const b = observeCatalogQuery(client, options, second.signal, { onData: bData, onError: vi.fn() });
    const refreshA = prepareCatalogRefresh(client, [options.queryKey]);
    const refreshB = prepareCatalogRefresh(client, [options.queryKey]);
    const pending = Promise.all([refreshA(), refreshB()]);
    finishOld('before receipt');
    await Promise.all([a, b, pending]);
    expect(count).toBe(2);
    expect(aData).toHaveBeenLastCalledWith('after receipt');
    expect(bData).toHaveBeenLastCalledWith('after receipt');
    first.abort(); second.abort();
  });

  it('coalesces consumers and releases only the cancelled reader', async () => {
    const client = new QueryClient();
    let finish!: (value: string) => void;
    let transportSignal!: AbortSignal;
    const queryFn = vi.fn(({ signal }: { signal: AbortSignal }) => {
      transportSignal = signal;
      return new Promise<string>(resolve => { finish = resolve; });
    });
    const options = { queryKey: ['catalog', 'test'], queryFn };
    const first = new AbortController(); const second = new AbortController();
    const a = readCatalogQuery(client, options, first.signal);
    const b = readCatalogQuery(client, options, second.signal);
    expect(queryFn).toHaveBeenCalledTimes(1);
    first.abort();
    await expect(a).rejects.toMatchObject({ name: 'AbortError' });
    expect(transportSignal.aborted).toBe(false);
    finish('confirmed catalog');
    await expect(b).resolves.toBe('confirmed catalog');
    expect(client.getQueryCache().find({ queryKey: options.queryKey })?.getObserversCount()).toBe(0);
  });

  it('aborts the transport when its last pending reader leaves', async () => {
    const client = new QueryClient(); const controller = new AbortController();
    let transportSignal!: AbortSignal;
    const promise = readCatalogQuery(client, { queryKey: ['catalog', 'only'], queryFn: ({ signal }) => {
      transportSignal = signal;
      return new Promise<string>(() => undefined);
    } }, controller.signal);
    controller.abort();
    await expect(promise).rejects.toMatchObject({ name: 'AbortError' });
    expect(transportSignal.aborted).toBe(true);
  });

  it('uses the existing fresh cache and never starts an already-cancelled reader', async () => {
    const client = new QueryClient(); const queryFn = vi.fn(async () => 'current');
    const options = { queryKey: ['catalog', 'fresh'], queryFn, staleTime: 30_000 };
    await expect(readCatalogQuery(client, options)).resolves.toBe('current');
    await expect(readCatalogQuery(client, options)).resolves.toBe('current');
    const controller = new AbortController(); controller.abort();
    await expect(readCatalogQuery(client, options, controller.signal)).rejects.toMatchObject({ name: 'AbortError' });
    expect(queryFn).toHaveBeenCalledTimes(1);
  });
});
