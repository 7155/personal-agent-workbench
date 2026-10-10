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

describe('optional exact-facts capability', () => {
  it('uses extended facts only after native begin explicitly advertises the capability', async () => {
    const host = bridge(); host.begin = vi.fn().mockResolvedValue({ producerEpoch: 4, factsVersion: 1 });
    const lease = createDesktopPetPublisher(host, identity);
    const facts = [{ id: 'session-1', activeTurnId: 'turn:one', waiting: [
      { turnId: 'turn:one', requestId: 'request:one', kind: 'input' as const }] }];
    lease.update({ ...value(), facts }); await tick();
    expect(host.publish.mock.calls[0][0]).toMatchObject({ facts });
    const legacy = bridge(); const oldLease = createDesktopPetPublisher(legacy, identity);
    oldLease.update({ ...value(), facts }); await tick();
    expect(legacy.publish.mock.calls[0][0]).not.toHaveProperty('facts');
  });
});

describe('atomic native fact budget', () => {
  it('packs whole facts under 4096 UTF8 bytes, keeps legacy rows/counts and never trims IDs or claims omitted empty requests', async () => {
    const host = bridge(); host.begin = vi.fn().mockResolvedValue({ producerEpoch: 4, factsVersion: 1 });
    const lease = createDesktopPetPublisher(host, identity);
    const conversations = Array.from({ length: 8 }, (_, index) => ({ id: `session-${index}`, label: '公开'.repeat(24), state: 'running' as const }));
    const facts = conversations.map(({ id }) => ({ id, activeTurnId: 't'.repeat(240), waiting: [
      { turnId: 't'.repeat(240), requestId: 'r'.repeat(240), kind: 'input' as const }],
      terminal: { eventId: 'e'.repeat(512), turnId: 'p'.repeat(240), sequence: 1, outcome: 'completed' as const } }));
    lease.update({ freshness: 'synced', counts: { ...emptyPetCounts(), running: 8 }, conversations, facts }); await tick();
    const packed = host.publish.mock.calls[0][0];
    expect(new TextEncoder().encode(JSON.stringify(packed)).byteLength).toBeLessThanOrEqual(4096);
    expect(packed.conversations).toEqual(conversations); expect(packed.counts.running).toBe(8);
    expect(packed.facts.length).toBeLessThan(8); expect(packed.facts.length).toBeGreaterThan(0);
    for (const fact of packed.facts) expect(fact).toEqual(facts.find(source => source.id === fact.id));
    lease.update({ freshness: 'synced', counts: { ...emptyPetCounts(), running: 8 }, conversations, facts }); await tick();
    expect(host.publish).toHaveBeenCalledTimes(1);
  });
  it('omits unknown extra or foreign facts atomically even if a capability exists', async () => {
    const host = bridge(); host.begin = vi.fn().mockResolvedValue({ producerEpoch: 4, factsVersion: 1 });
    const lease = createDesktopPetPublisher(host, identity);
    lease.update({ ...value(), facts: [{ id: 'session-1', activeTurnId: 'turn:one', prompt: 'private' },
      { id: 'foreign', activeTurnId: 'turn:one' }] } as never); await tick();
    expect(host.publish.mock.calls[0][0].facts).toEqual([]);
    expect(JSON.stringify(host.publish.mock.calls[0][0])).not.toContain('private');
  });
});
