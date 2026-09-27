import { describe, expect, it, vi } from 'vitest';
import { createSnapshotRequestQueue } from './snapshot-request-queue';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

const tick = async () => { for (let n = 0; n < 5; n += 1) await Promise.resolve(); };

describe('snapshot request queue', () => {
  it('coalesces same-tick reads without resolving before the read', async () => {
    const read = deferred<boolean>();
    const run = vi.fn(() => read.promise);
    const queue = createSnapshotRequestQueue({ merge: Math.max, run });
    const done = vi.fn();
    const first = queue.request(1).then(done);
    const second = queue.request(2);
    await tick();
    expect(run).toHaveBeenCalledTimes(1);
    expect(run).toHaveBeenCalledWith(2);
    expect(done).not.toHaveBeenCalled();
    read.resolve(true);
    await first;
    expect(await second).toBe(true);
  });

  it('waits for one trailing read for invalidations made during an active read', async () => {
    const firstRead = deferred<boolean>();
    const secondRead = deferred<boolean>();
    const run = vi.fn().mockReturnValueOnce(firstRead.promise).mockReturnValueOnce(secondRead.promise);
    const queue = createSnapshotRequestQueue<number>({ merge: Math.max, run });
    const first = queue.request(1);
    await tick();
    const done = vi.fn();
    const second = queue.request(2).then(done);
    const third = queue.request(3);
    firstRead.resolve(true);
    expect(await first).toBe(true);
    await tick();
    expect(run).toHaveBeenCalledTimes(2);
    expect(run).toHaveBeenLastCalledWith(3);
    expect(done).not.toHaveBeenCalled();
    secondRead.resolve(true);
    await second;
    expect(await third).toBe(true);
  });

  it('closes both active and queued waiters even when the read ignores abort', async () => {
    const read = deferred<boolean>();
    const run = vi.fn(() => read.promise);
    const queue = createSnapshotRequestQueue({ merge: Math.max, run });
    const first = queue.request(1);
    await tick();
    const second = queue.request(2);
    queue.close();
    expect(await first).toBe(false);
    expect(await second).toBe(false);
    expect(await queue.request(3)).toBe(false);
    read.resolve(true);
    await tick();
    expect(run).toHaveBeenCalledTimes(1);
    expect(queue.busy).toBe(false);
  });

  it('recovers after a rejected read without stranding trailing waiters', async () => {
    const read = deferred<boolean>();
    const run = vi.fn().mockReturnValueOnce(read.promise).mockResolvedValueOnce(true);
    const queue = createSnapshotRequestQueue<number>({ merge: Math.max, run });
    const first = queue.request(1);
    await tick();
    const second = queue.request(2);
    read.reject(new Error('temporary failure'));
    expect(await first).toBe(false);
    expect(await second).toBe(true);
  });

  it('does not launch a scheduled read after close', async () => {
    const run = vi.fn(async () => true);
    const queue = createSnapshotRequestQueue({ merge: Math.max, run });
    const result = queue.request(1);
    queue.close();
    expect(await result).toBe(false);
    await tick();
    expect(run).not.toHaveBeenCalled();
  });
});
