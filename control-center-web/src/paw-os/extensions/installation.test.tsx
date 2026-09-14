import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { MockControlTransport } from '@/test/mock-transport';
import { pawExtensionApps } from './registry';
import {
  PawExtensionInstallationProvider,
  PAW_EXTENSION_INSTALLATION_CHANGED_EVENT,
  projectPawExtensionInstallation,
  projectPawExtensionTeamSelection,
  usePawExtensionInstallation,
} from './installation';

afterEach(() => {
  cleanup();
  document.querySelector('meta[name="paw-deployment"]')?.remove();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('PAWOS Extension App installation projection', () => {
  const extension = pawExtensionApps[0]!;
  const bindingCapability = `pawos.extension.binding.${extension.bindingSha256.slice(0, 40)}`;
  const evidence = {
    id: extension.id,
    packageId: extension.packageId,
    version: extension.version,
    bindingSha256: extension.bindingSha256,
    bindingCapability,
    skillRef: extension.skillRef,
    skillSha256: extension.skillSha256,
    verticalSuiteId: extension.verticalSuiteId,
    verticalSuiteRevision: extension.verticalSuiteRevision,
    sandbox: extension.sandbox,
  };

  it('maps a Runtime Package ID to its manifest without treating disabled as enabled', () => {
    const projection = projectPawExtensionInstallation({
      ok: true,
      runtimeAvailable: true,
      items: [
        { id: extension.packageId, version: extension.version, installed: true, enabled: false, capabilities: [bindingCapability], extensionApp: evidence },
        { id: 'unrelated-package', installed: true, enabled: true },
      ],
    });

    expect([...projection.installedExtensionIds]).toEqual([extension.id]);
    expect([...projection.enabledExtensionIds]).toEqual([]);
  });

  it('projects installed and enabled Extension Apps through the transport hook', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.extensions.list': {
        ok: true,
        runtimeAvailable: true,
        items: [{ id: extension.packageId, version: extension.version, installed: true, enabled: true, capabilities: [bindingCapability], extensionApp: evidence }],
      },
    } });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <ControlTransportProvider transport={transport}>
        <PawExtensionInstallationProvider pollIntervalMs={0}>{children}</PawExtensionInstallationProvider>
      </ControlTransportProvider>
    );
    const { result } = renderHook(() => usePawExtensionInstallation(), { wrapper });

    await waitFor(() => expect(result.current.status).toBe('ready'));
    expect(result.current.isInstalled(extension.id)).toBe(true);
    expect(result.current.isEnabled(extension.id)).toBe(true);
    expect(result.current.isAvailable('agent')).toBe(true);
  });

  it('does not claim a stale installed projection when Runtime is unavailable', () => {
    const projection = projectPawExtensionInstallation({
      ok: true,
      runtimeAvailable: false,
      items: [{ id: extension.packageId, installed: true, enabled: true }],
    });

    expect([...projection.installedExtensionIds]).toEqual([]);
    expect([...projection.enabledExtensionIds]).toEqual([]);
  });

  it('projects only a published Team selection whose App metadata matches the registered manifest', () => {
    const projection = projectPawExtensionTeamSelection({
      spaceId: 'project-team',
      revision: 3,
      publicationIds: ['pub-selected', 'pub-withdrawn'],
      items: [
        {
          publicationId: 'pub-selected',
          packageId: extension.packageId,
          version: extension.version,
          digest: 'digest-selected',
          status: 'published',
          metadata: {
            displayName: extension.label,
            description: '',
            publisher: 'PAW',
            source: { kind: 'bundled', label: 'Product bundle' },
            permissions: [],
            compatibility: {},
            security: {},
            installable: true,
            distribution: 'team_staged_source',
            version: extension.version,
            extensionApp: { ...evidence, packageDigest: 'digest-selected' },
          },
        },
        {
          publicationId: 'pub-withdrawn',
          packageId: extension.packageId,
          version: extension.version,
          digest: 'digest-withdrawn',
          status: 'withdrawn',
          metadata: {
            displayName: extension.label,
            description: '',
            publisher: 'PAW',
            source: { kind: 'bundled', label: 'Product bundle' },
            permissions: [],
            compatibility: {},
            security: {},
            installable: true,
            distribution: 'team_staged_source',
            version: extension.version,
            extensionApp: { ...evidence, packageDigest: 'digest-withdrawn' },
          },
        },
      ],
    });

    expect(projection.installedExtensionIds.has(extension.id)).toBe(true);
    expect(projection.enabledExtensionIds.has(extension.id)).toBe(true);
    expect(projection.availableExtensionIds.has(extension.id)).toBe(true);
  });

  it('uses the Team selection adapter without probing local installation or eval-lab routes', async () => {
    const meta = document.createElement('meta');
    meta.name = 'paw-deployment';
    meta.content = 'team';
    document.head.appendChild(meta);
    const transport = new MockControlTransport({ routes: {
      'agent.extensions.list': { ok: true, runtimeAvailable: true, items: [] },
      'agent.eval-lab.apps.get': { ok: true, items: [] },
    } });
    const load = vi.fn().mockResolvedValue({
      spaceId: 'project-team',
      revision: 1,
      publicationIds: [],
      items: [],
    });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <ControlTransportProvider transport={transport}>
        <PawExtensionInstallationProvider pollIntervalMs={0} teamSelection={{ scopeKey: 'team:user:project', load }}>{children}</PawExtensionInstallationProvider>
      </ControlTransportProvider>
    );
    const { result } = renderHook(() => usePawExtensionInstallation(), { wrapper });

    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.source).toBe('team');
    expect(load).toHaveBeenCalledOnce();
    expect(transport.requests).toEqual([]);
  });

  it('keeps a required sandbox App unavailable in Team even when the evidence matches itself', () => {
    const projection = projectPawExtensionTeamSelection({
      spaceId: 'project-team',
      revision: 1,
      publicationIds: ['pub-required'],
      items: [{
        publicationId: 'pub-required',
        packageId: extension.packageId,
        version: extension.version,
        digest: 'digest-required',
        status: 'published',
        metadata: {
          displayName: extension.label,
          description: '',
          publisher: 'PAW',
          source: { kind: 'bundled', label: 'Product bundle' },
          permissions: [],
          compatibility: {},
          security: {},
          installable: true,
          distribution: 'team_staged_source',
          version: extension.version,
          extensionApp: {
            ...evidence,
            packageDigest: 'digest-required',
            sandbox: { ...extension.sandbox!, default: 'required' },
          },
        },
      }],
    });

    expect(projection.availableExtensionIds.has(extension.id)).toBe(false);
    expect(projection.enabledExtensionIds.has(extension.id)).toBe(false);
  });

  it('projects an uninstall by removing the Extension App identity from both sets', () => {
    const installed = projectPawExtensionInstallation({
      ok: true,
      items: [{ id: extension.packageId, version: extension.version, installed: true, enabled: true, capabilities: [bindingCapability], extensionApp: evidence }],
    });
    const uninstalled = projectPawExtensionInstallation({ ok: true, items: [] });

    expect(installed.enabledExtensionIds.has(extension.id)).toBe(true);
    expect(uninstalled.installedExtensionIds.has(extension.id)).toBe(false);
    expect(uninstalled.enabledExtensionIds.has(extension.id)).toBe(false);
  });

  it('refreshes immediately when App Center applies an install or uninstall receipt', async () => {
    let enabled = false;
    const transport = new MockControlTransport({ routes: {
      'agent.extensions.list': () => ({
        ok: true,
        runtimeAvailable: true,
        items: enabled
          ? [{ id: extension.packageId, version: extension.version, installed: true, enabled: true, capabilities: [bindingCapability], extensionApp: evidence }]
          : [],
      }),
    } });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <ControlTransportProvider transport={transport}>
        <PawExtensionInstallationProvider pollIntervalMs={0}>{children}</PawExtensionInstallationProvider>
      </ControlTransportProvider>
    );
    const { result } = renderHook(() => usePawExtensionInstallation(), { wrapper });
    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.isEnabled(extension.id)).toBe(false);

    enabled = true;
    act(() => window.dispatchEvent(new Event(PAW_EXTENSION_INSTALLATION_CHANGED_EVENT)));

    await waitFor(() => expect(result.current.isEnabled(extension.id)).toBe(true));
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.extensions.list')).toHaveLength(2);
  });

  it('does not poll while hidden, refreshes once on resume, and reconciles 60 seconds after install events', async () => {
    vi.useFakeTimers();
    let visibility: DocumentVisibilityState = 'hidden';
    vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility);
    const transport = new MockControlTransport({ routes: {
      'agent.extensions.list': { ok: true, runtimeAvailable: true, items: [] },
    } });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <ControlTransportProvider transport={transport}>
        <PawExtensionInstallationProvider>{children}</PawExtensionInstallationProvider>
      </ControlTransportProvider>
    );
    renderHook(() => usePawExtensionInstallation(), { wrapper });

    await flushAsyncWork();
    expect(extensionListRequestCount(transport)).toBe(0);

    visibility = 'visible';
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'));
      await Promise.resolve();
    });
    expect(extensionListRequestCount(transport)).toBe(1);

    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'));
      await Promise.resolve();
    });
    expect(extensionListRequestCount(transport)).toBe(1);

    act(() => window.dispatchEvent(new Event(PAW_EXTENSION_INSTALLATION_CHANGED_EVENT)));
    await flushAsyncWork();
    expect(extensionListRequestCount(transport)).toBe(2);

    await act(async () => {
      vi.advanceTimersByTime(59_999);
      await Promise.resolve();
    });
    expect(extensionListRequestCount(transport)).toBe(2);

    await act(async () => {
      vi.advanceTimersByTime(1);
      await Promise.resolve();
    });
    expect(extensionListRequestCount(transport)).toBe(3);

    visibility = 'hidden';
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'));
      window.dispatchEvent(new Event(PAW_EXTENSION_INSTALLATION_CHANGED_EVENT));
      vi.advanceTimersByTime(120_000);
    });
    await flushAsyncWork();
    expect(extensionListRequestCount(transport)).toBe(3);
  });

  it('aborts in-flight reconciliation when hidden and on unmount', async () => {
    let visibility: DocumentVisibilityState = 'visible';
    vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility);
    const transport = new MockControlTransport({ routes: {
      'agent.extensions.list': () => new Promise<never>(() => undefined),
    } });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <ControlTransportProvider transport={transport}>
        <PawExtensionInstallationProvider>{children}</PawExtensionInstallationProvider>
      </ControlTransportProvider>
    );
    const { unmount } = renderHook(() => usePawExtensionInstallation(), { wrapper });
    await flushAsyncWork();
    const firstSignal = transport.requests[0]?.request.signal;
    expect(firstSignal?.aborted).toBe(false);

    visibility = 'hidden';
    act(() => document.dispatchEvent(new Event('visibilitychange')));
    expect(firstSignal?.aborted).toBe(true);

    visibility = 'visible';
    act(() => document.dispatchEvent(new Event('visibilitychange')));
    await flushAsyncWork();
    expect(extensionListRequestCount(transport)).toBe(2);
    const secondSignal = transport.requests.filter(({ request }) => request.pathId === 'agent.extensions.list')[1]?.request.signal;
    expect(secondSignal?.aborted).toBe(false);

    unmount();
    expect(secondSignal?.aborted).toBe(true);
  });

  it('waits for the passive 60 second reconciliation after a failed request', async () => {
    vi.useFakeTimers();
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
    const transport = new MockControlTransport({ routes: {
      'agent.extensions.list': () => { throw new Error('offline'); },
    } });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <ControlTransportProvider transport={transport}>
        <PawExtensionInstallationProvider>{children}</PawExtensionInstallationProvider>
      </ControlTransportProvider>
    );
    renderHook(() => usePawExtensionInstallation(), { wrapper });
    await flushAsyncWork();
    expect(extensionListRequestCount(transport)).toBe(1);

    await act(async () => {
      vi.advanceTimersByTime(59_999);
      await Promise.resolve();
    });
    expect(extensionListRequestCount(transport)).toBe(1);

    await act(async () => {
      vi.advanceTimersByTime(1);
      await Promise.resolve();
    });
    expect(extensionListRequestCount(transport)).toBe(2);
  });

  it.each([
    ['version', { version: '9.9.9' }],
    ['binding digest', { bindingSha256: 'f'.repeat(64), bindingCapability: `pawos.extension.binding.${'f'.repeat(40)}` }],
    ['skill', { skillRef: 'wrong-skill' }],
    ['suite', { verticalSuiteId: 'wrong-suite' }],
    ['sandbox', { sandbox: { ...extension.sandbox, default: 'required' } }],
  ])('fails closed when Extension App %s evidence is stale or mismatched', (_label, override) => {
    const projection = projectPawExtensionInstallation({
      ok: true,
      runtimeAvailable: true,
      items: [{
        id: extension.packageId,
        version: extension.version,
        installed: true,
        enabled: true,
        capabilities: [bindingCapability],
        extensionApp: { ...evidence, ...override },
      }],
    });

    expect(projection.installedExtensionIds.has(extension.id)).toBe(true);
    expect(projection.enabledExtensionIds.has(extension.id)).toBe(false);
    expect(projection.availableExtensionIds.has(extension.id)).toBe(false);
  });

  it('requires a binding capability even when the Runtime reports the current version', () => {
    const projection = projectPawExtensionInstallation({
      ok: true,
      runtimeAvailable: true,
      items: [{
        id: extension.packageId,
        version: extension.version,
        installed: true,
        enabled: true,
        capabilities: [],
        extensionApp: { ...evidence, bindingCapability: '' },
      }],
    });

    expect(projection.enabledExtensionIds.has(extension.id)).toBe(false);
    expect(projection.availableExtensionIds.has(extension.id)).toBe(false);
  });

  it.each([
    'id',
    'packageId',
    'version',
    'bindingSha256',
    'bindingCapability',
    'skillRef',
    'skillSha256',
    'verticalSuiteId',
    'verticalSuiteRevision',
  ])('rejects Runtime evidence when required %s is missing', (field) => {
    const incomplete = { ...evidence } as Record<string, unknown>;
    delete incomplete[field];
    const projection = projectPawExtensionInstallation({
      ok: true,
      runtimeAvailable: true,
      items: [{
        id: extension.packageId,
        version: extension.version,
        installed: true,
        enabled: true,
        capabilities: [bindingCapability],
        extensionApp: incomplete,
      }],
    });

    expect(projection.enabledExtensionIds.has(extension.id)).toBe(false);
    expect(projection.availableExtensionIds.has(extension.id)).toBe(false);
  });

  it('rejects conflicting Runtime item and package identities', () => {
    const projection = projectPawExtensionInstallation({
      ok: true,
      runtimeAvailable: true,
      items: [{
        id: 'another-package',
        packageId: extension.packageId,
        version: extension.version,
        installed: true,
        enabled: true,
        capabilities: [bindingCapability],
        extensionApp: evidence,
      }],
    });

    expect(projection.installedExtensionIds.has(extension.id)).toBe(false);
    expect(projection.availableExtensionIds.has(extension.id)).toBe(false);
  });
});

function extensionListRequestCount(transport: MockControlTransport): number {
  return transport.requests.filter(({ request }) => request.pathId === 'agent.extensions.list').length;
}

async function flushAsyncWork(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}
