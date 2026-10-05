import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { MockControlTransport } from '@/test/mock-transport';
import { pluginQueryKeys, usePluginCatalog } from './api';
import { controlTransportScopeKey } from '@/platform/transport-scope';
import { PAW_EXTENSION_INSTALLATION_CHANGED_EVENT } from '@/paw-os/extensions/installation';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const sessionId = 'same-session';
function catalog(revision: string) {
  return { schemaVersion: 'rag-ime.capability-catalog.v1', ok: true, revision, effectiveAtMs: 1,
    projectScope: { supported: false }, items: [],
    sessionPolicy: { sessionId, policyRevision: 1, effectiveAtMs: 1, disclosurePreferences: {} } };
}
function transport(label: string) {
  const value = new MockControlTransport({ routes: {
    'agent.tools.list': catalog(label),
    'agent.configuration.get': { configuration: { revision: label === 'A' ? 1 : 2, configuration: {} } },
    'agent.extensions.list': { items: [{ id: label }] },
    'agent.extensions.catalog': { items: [] },
    'agent.extensions.proposals': { items: [{ id: label }] },
    'agent.lifecycleHooks.get': { items: [] },
    'agent.extensions.apply': { ok: true, receipt: { receiptId: `${label}-receipt` } },
  } });
  Object.defineProperty(value, 'connectionIdentity', { value: 'http:same-origin' });
  return value;
}
function wrapper(client: QueryClient, value: MockControlTransport) {
  return ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}><ControlTransportProvider transport={value}>{children}</ControlTransportProvider></QueryClientProvider>;
}

describe('plugin catalog transport ownership', () => {
  it('keeps a pending mutation receipt with its original transport after the same hook switches owners', async () => {
    let finish!: (value: unknown) => void;
    const pendingReceipt = new Promise(resolve => { finish = resolve; });
    const a = new MockControlTransport({ routes: { 'agent.extensions.apply': () => pendingReceipt } });
    const b = new MockControlTransport();
    let active = a;
    const client = new QueryClient();
    client.setQueryData(pluginQueryKeys.installed(a), { items: [] });
    client.setQueryData(pluginQueryKeys.installed(b), { items: [] });
    const dispatch = vi.spyOn(window, 'dispatchEvent');
    const view = renderHook(() => usePluginCatalog(sessionId, false), { wrapper: ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}><ControlTransportProvider transport={active}>{children}</ControlTransportProvider></QueryClientProvider> });
    let mutation!: Promise<unknown>;
    act(() => { mutation = view.result.current.apply.mutateAsync({ previewToken: 'a-token', payloadSha256: 'a-hash', confirmText: 'apply' }); });
    await waitFor(() => expect(a.requests).toHaveLength(1));
    active = b; view.rerender();
    await act(async () => { await Promise.resolve(); finish({ ok: true, receipt: { receiptId: 'accepted-by-a' } }); await mutation; });
    const events = dispatch.mock.calls.map(([event]) => event).filter(event => event.type === PAW_EXTENSION_INSTALLATION_CHANGED_EVENT);
    expect(events).toHaveLength(1);
    expect((events[0] as CustomEvent).detail.transportScope).toBe(controlTransportScopeKey(a));
    expect(client.getQueryState(pluginQueryKeys.installed(a))?.isInvalidated).toBe(true);
    expect(client.getQueryState(pluginQueryKeys.installed(b))?.isInvalidated).toBe(false);
    expect(b.requests).toHaveLength(0);
  });

  it('does not reuse another transport’s fresh session catalog or inventory through a shared QueryClient', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const a = transport('A'); const b = transport('B');
    const first = renderHook(() => usePluginCatalog(sessionId), { wrapper: wrapper(client, a) });
    await waitFor(() => expect(first.result.current.catalog.data?.revision).toBe('A'));
    const second = renderHook(() => usePluginCatalog(sessionId), { wrapper: wrapper(client, b) });
    await waitFor(() => expect(second.result.current.catalog.data?.revision).toBe('B'));
    expect(first.result.current.catalog.data?.revision).toBe('A');
    expect(first.result.current.defaults.data?.revision).toBe(1);
    expect(second.result.current.defaults.data?.revision).toBe(2);
    expect(first.result.current.installed.data).toEqual({ items: [{ id: 'A' }] });
    expect(second.result.current.installed.data).toEqual({ items: [{ id: 'B' }] });
  });

  it('invalidates only the transport that accepted an installation receipt', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const a = transport('A'); const b = transport('B');
    const first = renderHook(() => usePluginCatalog(sessionId), { wrapper: wrapper(client, a) });
    const second = renderHook(() => usePluginCatalog(sessionId), { wrapper: wrapper(client, b) });
    await waitFor(() => { expect(first.result.current.catalog.isSuccess).toBe(true); expect(second.result.current.catalog.isSuccess).toBe(true); });
    const before = b.requests.length;
    await act(async () => { await first.result.current.apply.mutateAsync({ previewToken: 'a-token', payloadSha256: 'a-hash', confirmText: 'apply' }); });
    expect(b.requests).toHaveLength(before);
    expect(second.result.current.catalog.data?.revision).toBe('B');
    expect(a.requests.filter(({ request }) => request.pathId === 'agent.tools.list')).toHaveLength(2);
    expect(b.requests.filter(({ request }) => request.pathId === 'agent.tools.list')).toHaveLength(1);
  });
});
