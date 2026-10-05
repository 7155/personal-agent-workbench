import { act, cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { MockControlTransport } from '@/test/mock-transport';
import type { ControlRequest } from '@/platform/transport';
import { PawDesktopPetPublisher } from './PawDesktopPetPublisher';
import { PawWorkDirectoryProvider } from './PawWorkDirectory';

afterEach(() => {
  cleanup(); delete window.pawDesktopPetState;
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
  vi.useRealTimers();
});
const session = (title: string) => ({ id: 'same-session', title, status: 'busy', mode: 'assistant', roleId: '', roleVersion: '',
  workspaceRoots: ['/private/project'], updatedAtMs: 100, lastMessagePreview: 'Private transcript' });
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; }
function bridge() {
  let epoch = 0;
  const value = { begin: vi.fn().mockImplementation(async () => ({ producerEpoch: ++epoch })),
    publish: vi.fn().mockResolvedValue(true), release: vi.fn().mockResolvedValue(true) };
  window.pawDesktopPetState = value; return value;
}
function directoryTransport(result: unknown | ((request: ControlRequest) => unknown)) {
  return new MockControlTransport({ routes: {
    'agent.sessions.list': result,
    'agent.rooms.list': { ok: true, items: [] },
    'agent.memoryMaintenance.run': { ok: true, projection: { running: false } },
  } });
}
function tree(transport: MockControlTransport) {
  return <ControlTransportProvider transport={transport}>
    <PawWorkDirectoryProvider initialPollDelayMs={0} pollIntervalMs={30_000} maintenancePollIntervalMs={60_000}>
      <PawDesktopPetPublisher />
    </PawWorkDirectoryProvider>
  </ControlTransportProvider>;
}
async function flush() { await act(async () => { for (let i = 0; i < 6; i += 1) await Promise.resolve(); }); }

describe('existing directory to companion bridge integration', () => {
  it('uses only existing directory reads and releases the producer on unmount', async () => {
    vi.useFakeTimers(); const host = bridge(); const transport = directoryTransport({ ok: true, items: [session('A')] });
    const view = render(tree(transport)); await flush();
    expect(host.publish.mock.calls.at(-1)?.[0]).toMatchObject({ freshness: 'synced', counts: { running: 1 },
      conversations: [{ id: 'same-session', label: 'A', state: 'running' }] });
    expect(transport.requests.map(call => call.request.pathId).sort()).toEqual(['agent.memoryMaintenance.run', 'agent.rooms.list', 'agent.sessions.list']);
    expect(transport.activeSubscriptionCount()).toBe(0);
    expect(JSON.stringify(host.publish.mock.calls)).not.toMatch(/private|Private transcript|workspaceRoots/);
    view.unmount(); await flush(); expect(host.release).toHaveBeenCalledWith({ producerEpoch: 1 });
    const count = transport.requests.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(transport.requests).toHaveLength(count);
  });
  it('does not republish the old connection under a new producer when ids are reused', async () => {
    vi.useFakeTimers(); const host = bridge(); const a = directoryTransport({ ok: true, items: [session('Connection A')] });
    const nextRead = deferred<unknown>(); const b = directoryTransport(() => nextRead.promise);
    const view = render(tree(a)); await flush(); const oldScope = host.begin.mock.calls[0][0].scopeId;
    view.rerender(tree(b)); await flush();
    expect(host.release).toHaveBeenCalledWith({ producerEpoch: 1 });
    expect(host.begin.mock.calls[1][0].scopeId).not.toBe(oldScope);
    const newOwnerSnapshots = () => host.publish.mock.calls.map(([value]) => value).filter(value => value.producerEpoch === 2);
    expect(newOwnerSnapshots().every(value => value.conversations.length === 0 && value.freshness !== 'synced')).toBe(true);
    await act(async () => nextRead.resolve({ ok: true, items: [session('Connection B')] })); await flush();
    expect(newOwnerSnapshots().at(-1)).toMatchObject({ freshness: 'synced', conversations: [{ label: 'Connection B' }] });
    expect(newOwnerSnapshots().some(value => value.conversations.some((row: { label: string }) => row.label === 'Connection A'))).toBe(false);
  });
  it('ignores an old transport response after replacement and marks hidden sources unsynced', async () => {
    vi.useFakeTimers(); const host = bridge(); const oldRead = deferred<unknown>();
    const a = directoryTransport(() => oldRead.promise); const b = directoryTransport({ ok: true, items: [session('Current')] });
    const view = render(tree(a)); await flush(); view.rerender(tree(b)); await flush();
    await act(async () => oldRead.resolve({ ok: true, items: [session('Old late result')] })); await flush();
    expect(host.publish.mock.calls.at(-1)?.[0].conversations[0].label).toBe('Current');
    act(() => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' });
      document.dispatchEvent(new Event('visibilitychange'));
    }); await flush();
    expect(host.publish.mock.calls.at(-1)?.[0]).toMatchObject({ freshness: 'recovering', counts: { running: 0, unknown: 1 } });
    const count = b.requests.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(b.requests).toHaveLength(count);
  });
});
