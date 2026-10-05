import { describe, expect, it, vi } from 'vitest';
import { createDesktopPetPublisher } from './desktop-pet-publisher';
import { emptyPetCounts, type PetStatePublisherBridge } from './desktop-pet-snapshot';

const identity = { schemaVersion: 1 as const, sourceId: 'work-directory' as const, scopeId: 'scope-1' };
const value = (running = 1) => ({ freshness: 'synced' as const, counts: { ...emptyPetCounts(), running }, conversations: [{ id: 'session-1', label: 'Conversation', state: 'running' as const }] });
const tick = async () => { await Promise.resolve(); await Promise.resolve(); };
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; }
function bridge(): PetStatePublisherBridge & { publish: ReturnType<typeof vi.fn>; release: ReturnType<typeof vi.fn> } {
  return { begin: vi.fn().mockResolvedValue({ producerEpoch: 4 }), publish: vi.fn().mockResolvedValue(true), release: vi.fn().mockResolvedValue(true) };
}

describe('read-only companion publisher', () => {
  it('coalesces before begin, deduplicates equal projections and strips extra fields', async () => {
    const host = bridge(); const lease = createDesktopPetPublisher(host, identity);
    lease.update({ ...value(1), history: 'not forwarded' } as ReturnType<typeof value>);
    lease.update(value(2)); await tick();
    expect(host.publish).toHaveBeenCalledTimes(1);
    expect(host.publish).toHaveBeenLastCalledWith({ ...value(2), schemaVersion: 1, producerEpoch: 4, revision: 1 });
    lease.update(value(2)); await tick(); expect(host.publish).toHaveBeenCalledTimes(1);
  });
  it('retains only the last projection while a publish is pending', async () => {
    const host = bridge(); const response = deferred<boolean>(); host.publish.mockReturnValueOnce(response.promise);
    const lease = createDesktopPetPublisher(host, identity); lease.update(value(1)); await tick();
    lease.update(value(2)); lease.update(value(3));
    expect(host.publish).toHaveBeenCalledTimes(1);
    response.resolve(true); await tick();
    expect(host.publish).toHaveBeenCalledTimes(2);
    expect(host.publish.mock.calls[1][0]).toMatchObject({ counts: { running: 3 }, revision: 2 });
  });
  it('releases a late begin after owner unmount without publishing', async () => {
    const host = bridge(); const response = deferred<{ producerEpoch: number }>(); host.begin = () => response.promise;
    const lease = createDesktopPetPublisher(host, identity); lease.update(value()); lease.release();
    response.resolve({ producerEpoch: 7 }); await tick();
    expect(host.publish).not.toHaveBeenCalled(); expect(host.release).toHaveBeenCalledWith({ producerEpoch: 7 });
  });
  it('does not publish queued data after release or a revoked owner receipt', async () => {
    const host = bridge(); const response = deferred<boolean>(); host.publish.mockReturnValueOnce(response.promise);
    const lease = createDesktopPetPublisher(host, identity); lease.update(value()); await tick();
    lease.update(value(3)); lease.release(); response.resolve(true); await tick();
    expect(host.publish).toHaveBeenCalledTimes(1);
    const next = createDesktopPetPublisher(host, identity); host.publish.mockResolvedValue(false);
    next.update(value()); await tick(); next.update(value(2)); await tick();
    expect(host.publish).toHaveBeenCalledTimes(2);
  });
});
