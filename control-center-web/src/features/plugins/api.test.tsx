import { QueryClient, QueryClientProvider, QueryObserver } from '@tanstack/react-query';
import { act, cleanup, renderHook } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { MockControlTransport } from '@/test/mock-transport';
import { pluginQueryKeys, usePluginCatalog } from './api';

afterEach(cleanup);

describe('plugin lifecycle cache', () => {
  it('refreshes an active session once and invalidates inactive snapshots without fetching them', async () => {
    const client = new QueryClient();
    const keys = ['', 'open-session', 'another-session'].map(pluginQueryKeys.catalog);
    for (const key of keys) client.setQueryData(key, { items: [] });
    let activeReads = 0;
    const activeSession = new QueryObserver(client, {
      queryKey: pluginQueryKeys.catalog('open-session'),
      staleTime: Infinity,
      queryFn: async () => { activeReads += 1; return { items: [{ enabled: false }] }; },
    });
    const unsubscribe = activeSession.subscribe(() => {});
    client.setQueryData(pluginQueryKeys.defaults(), { revision: 1 });
    const transport = new MockControlTransport({ routes: {
      'agent.extensions.apply': { ok: true, receipt: { receiptId: 'disable-receipt' } },
    } });
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}><ControlTransportProvider transport={transport}>{children}</ControlTransportProvider></QueryClientProvider>;
    const { result } = renderHook(() => usePluginCatalog('', false), { wrapper });
    await act(async () => { await result.current.apply.mutateAsync({ previewToken: 'token', payloadSha256: 'hash', confirmText: 'apply' }); });
    for (const key of [keys[0], keys[2]]) expect(client.getQueryState(key)?.isInvalidated).toBe(true);
    expect(activeReads).toBe(1);
    expect(client.getQueryData(keys[1])).toEqual({ items: [{ enabled: false }] });
    expect(client.getQueryState(pluginQueryKeys.defaults())?.isInvalidated).toBe(false);
    expect(transport.requests.map(({ request }) => request.pathId)).toEqual(['agent.extensions.apply']);
    unsubscribe();
  });
});
