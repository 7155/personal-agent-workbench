/** One in-flight read plus one coalesced trailing read; never a command queue. */
export interface SnapshotRequestQueue<Request> {
  request(request: Request): Promise<boolean>;
  readonly busy: boolean;
  close(): void;
}

interface Batch<Request> {
  request: Request;
  waiters: Array<(result: boolean) => void>;
}

export function createSnapshotRequestQueue<Request>(options: {
  merge(current: Request, next: Request): Request;
  run(request: Request): Promise<boolean>;
}): SnapshotRequestQueue<Request> {
  let pending: Batch<Request> | undefined;
  let running: Batch<Request> | undefined;
  let scheduled = false;
  let closed = false;

  function settle(batch: Batch<Request> | undefined, result: boolean): void {
    for (const resolve of batch?.waiters.splice(0) ?? []) resolve(result);
  }

  function schedule(): void {
    if (closed || running || scheduled || !pending) return;
    scheduled = true;
    queueMicrotask(() => {
      scheduled = false;
      if (closed || running || !pending) return;
      const batch = pending;
      pending = undefined;
      // Set this before invoking run: a synchronous callback during run is
      // an invalidation of this read, and must belong to the trailing read.
      running = batch;
      const finish = (result: boolean) => {
        if (running === batch) running = undefined;
        settle(batch, !closed && result);
        schedule();
      };
      try {
        void Promise.resolve(options.run(batch.request)).then(
          finish,
          () => finish(false),
        );
      } catch {
        finish(false);
      }
    });
  }

  return {
    request(request) {
      if (closed) return Promise.resolve(false);
      return new Promise<boolean>((resolve) => {
        if (pending) {
          pending.request = options.merge(pending.request, request);
          pending.waiters.push(resolve);
        } else {
          pending = { request, waiters: [resolve] };
        }
        schedule();
      });
    },
    get busy() {
      return !closed && Boolean(running || pending || scheduled);
    },
    close() {
      if (closed) return;
      closed = true;
      settle(pending, false);
      settle(running, false);
      pending = undefined;
      // Aborting the underlying read belongs to the owning Session. Its late
      // result cannot resolve these waiters twice or start another read.
    },
  };
}
