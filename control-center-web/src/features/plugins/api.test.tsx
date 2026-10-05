import { QueryClient, QueryClientProvider, QueryObserver } from '@tanstack/react-query';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { MockControlTransport } from '@/test/mock-transport';
import { pluginQueryKeys, usePluginCatalog } from './api';
import { PAW_EXTENSION_INSTALLATION_CHANGED_EVENT } from '@/paw-os/extensions/installation';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('plugin lifecycle cache', () => {
  it('announces a confirmed receipt while an App Center refetch is still pending', async () => {
    const client = new QueryClient();
    client.setQueryData(pluginQueryKeys.installed(), { items: [] });
    let finishRead!: (value: { items: never[] }) => void;
    const pendingRead = new Promise<{ items: never[] }>((resolve) => { finishRead = resolve; });
    const inventory = new QueryObserver(client, { queryKey: pluginQueryKeys.installed(), staleTime: Infinity, queryFn: () => pendingRead });
    const unsubscribe = inventory.subscribe(() => {});
    const dispatch = vi.spyOn(window, 'dispatchEvent');
    const transport = new MockControlTransport({ routes: {
      'agent.extensions.apply': { ok: true, receipt: { receiptId: 'confirmed' } },
    } });
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}><ControlTransportProvider transport={transport}>{children}</ControlTransportProvider></QueryClientProvider>;
    const { result } = renderHook(() => usePluginCatalog('', false), { wrapper });
    let mutation!: Promise<unknown>;
    act(() => { mutation = result.current.apply.mutateAsync({ previewToken: 'token', payloadSha256: 'hash', confirmText: 'apply' }); });
    try {
      await waitFor(() => expect(client.getQueryState(pluginQueryKeys.installed())?.fetchStatus).toBe('fetching'));
      expect(dispatch.mock.calls.filter(([event]) => event.type === PAW_EXTENSION_INSTALLATION_CHANGED_EVENT)).toHaveLength(1);
    } finally {
      await act(async () => { finishRead({ items: [] }); await mutation; });
      unsubscribe();
    }
  });

  it.each([{ ok: false }, { ok: true }, null])('does not announce an unconfirmed apply: %j', async (response) => {
    const client = new QueryClient();
    const dispatch = vi.spyOn(window, 'dispatchEvent');
    const transport = new MockControlTransport({ routes: { 'agent.extensions.apply': () => response } });
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}><ControlTransportProvider transport={transport}>{children}</ControlTransportProvider></QueryClientProvider>;
    const { result } = renderHook(() => usePluginCatalog('', false), { wrapper });
    await act(async () => { await expect(result.current.apply.mutateAsync({ previewToken: 'token', payloadSha256: 'hash', confirmText: 'apply' })).rejects.toThrow('未收到有效的更改回执'); });
    expect(dispatch.mock.calls.filter(([event]) => event.type === PAW_EXTENSION_INSTALLATION_CHANGED_EVENT)).toHaveLength(0);
  });

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
