import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { PawExtensionInstallationProvider, usePawExtensionInstallation } from '@/paw-os/extensions/installation';
import { MockControlTransport } from '@/test/mock-transport';
import { pawExtensionApps } from '@/paw-os/extensions/registry';
import { usePluginCatalog } from './api';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
describe('installed inventory consumers', () => {
  it('reads after the mutation when a pre-mutation shared inventory arrives late, updating both consumers', async () => {
    const app = pawExtensionApps.find(item => item.id === 'extension:zhanggui-wenshu')!;
    const bindingCapability = `pawos.extension.binding.${app.bindingSha256.slice(0, 40)}`;
    const installed = { runtimeAvailable: true, items: [{ id: app.packageId, installed: true, enabled: true, version: app.version, capabilities: [bindingCapability], extensionApp: { ...app, bindingCapability } }] };
    let finishOld!: (value: unknown) => void;
    const old = new Promise(resolve => { finishOld = resolve; });
    let reads = 0;
    const transport = new MockControlTransport({ routes: {
      'agent.extensions.list': () => ++reads === 1 ? old : installed,
      'agent.eval-lab.apps.get': { ok: true, items: [] },
      'agent.tools.list': { schemaVersion: 'rag-ime.capability-catalog.v1', ok: true, revision: 'global', effectiveAtMs: 1, projectScope: { supported: false }, items: [] },
      'agent.configuration.get': { configuration: { revision: 1, configuration: {} } },
      'agent.extensions.catalog': { items: [] },
      'agent.extensions.proposals': { items: [] },
      'agent.lifecycleHooks.get': { items: [] },
      'agent.extensions.apply': { ok: true, receipt: { receiptId: 'install-accepted' } },
    } });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}><ControlTransportProvider transport={transport}><PawExtensionInstallationProvider pollIntervalMs={0}>{children}</PawExtensionInstallationProvider></ControlTransportProvider></QueryClientProvider>;
    const { result } = renderHook(() => ({ appCenter: usePluginCatalog(), desktop: usePawExtensionInstallation() }), { wrapper });
    await waitFor(() => expect(reads).toBe(1));
    let mutation!: Promise<unknown>;
    act(() => { mutation = result.current.appCenter.apply.mutateAsync({ previewToken: 'token', payloadSha256: 'hash', confirmText: 'apply' }); });
    await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId === 'agent.extensions.apply')).toBe(true));
    await act(async () => { finishOld({ runtimeAvailable: true, items: [] }); await mutation; });
    await waitFor(() => expect(reads).toBe(2));
    expect(result.current.appCenter.installed.data).toEqual(installed);
    await waitFor(() => expect(result.current.desktop.isAvailable(app.id)).toBe(true));
  });

  it('keeps a confirmed install refresh in its original connection across both real consumers', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const create = (label: string) => new MockControlTransport({ routes: {
      'agent.extensions.list': { runtimeAvailable: true, items: [] },
      'agent.eval-lab.apps.get': { ok: true, items: [] },
      'agent.tools.list': { schemaVersion: 'rag-ime.capability-catalog.v1', ok: true, revision: label, effectiveAtMs: 1, projectScope: { supported: false }, items: [] },
      'agent.configuration.get': { configuration: { revision: 1, configuration: {} } },
      'agent.extensions.catalog': { items: [] },
      'agent.extensions.proposals': { items: [] },
      'agent.lifecycleHooks.get': { items: [] },
      'agent.extensions.apply': { ok: true, receipt: { receiptId: label } },
    } });
    const a = create('A'); const b = create('B');
    const mount = (transport: MockControlTransport) => renderHook(() => ({ appCenter: usePluginCatalog(), desktop: usePawExtensionInstallation() }), {
      wrapper: ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}><ControlTransportProvider transport={transport}><PawExtensionInstallationProvider pollIntervalMs={0}>{children}</PawExtensionInstallationProvider></ControlTransportProvider></QueryClientProvider>,
    });
    const first = mount(a); const second = mount(b);
    await waitFor(() => { expect(first.result.current.desktop.ready).toBe(true); expect(second.result.current.desktop.ready).toBe(true); });
    const reads = (value: MockControlTransport) => value.requests.filter(({ request }) => request.pathId === 'agent.extensions.list').length;
    expect(reads(a)).toBe(1); expect(reads(b)).toBe(1);
    await act(async () => { await first.result.current.appCenter.apply.mutateAsync({ previewToken: 'token', payloadSha256: 'hash', confirmText: 'apply' }); });
    await waitFor(() => expect(first.result.current.desktop.ready).toBe(true));
    expect(reads(a)).toBe(2); expect(reads(b)).toBe(1);
    expect(second.result.current.appCenter.catalog.data?.revision).toBe('B');
  });

  it('shares App Center and desktop reads, without one hidden consumer cancelling the other', async () => {
    let visibility: DocumentVisibilityState = 'visible';
    vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility);
    let finish!: (value: unknown) => void;
    const pending = new Promise(resolve => { finish = resolve; });
    const transport = new MockControlTransport({ routes: {
      'agent.extensions.list': () => pending,
      'agent.eval-lab.apps.get': { ok: true, items: [] },
      'agent.tools.list': { schemaVersion: 'rag-ime.capability-catalog.v1', ok: true, revision: 'global', effectiveAtMs: 1, projectScope: { supported: false }, items: [] },
      'agent.configuration.get': { configuration: { revision: 1, configuration: {} } },
      'agent.extensions.catalog': { items: [] },
      'agent.extensions.proposals': { items: [] },
      'agent.lifecycleHooks.get': { items: [] },
    } });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}><ControlTransportProvider transport={transport}><PawExtensionInstallationProvider pollIntervalMs={0}>{children}</PawExtensionInstallationProvider></ControlTransportProvider></QueryClientProvider>;
    const { result } = renderHook(() => ({ appCenter: usePluginCatalog(), desktop: usePawExtensionInstallation() }), { wrapper });
    await waitFor(() => expect(transport.requests.filter(({ request }) => request.pathId === 'agent.extensions.list')).toHaveLength(1));
    const request = transport.requests.find(({ request }) => request.pathId === 'agent.extensions.list')!.request;
    expect(request.signal?.aborted).toBe(false);
    act(() => { visibility = 'hidden'; document.dispatchEvent(new Event('visibilitychange')); });
    expect(request.signal?.aborted).toBe(false);
    await act(async () => { finish({ runtimeAvailable: true, items: [] }); await pending; });
    await waitFor(() => expect(result.current.appCenter.installed.data).toEqual({ runtimeAvailable: true, items: [] }));
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.extensions.list')).toHaveLength(1);
  });
});
