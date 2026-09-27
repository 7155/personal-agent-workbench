export type GISCommandActivity = {
  id: number; sessionId: string; label: string;
  status: 'queued' | 'running' | 'completed' | 'failed' | 'superseded';
  queuedAt: number; startedAt?: number; finishedAt?: number; error?: string;
};
export type GISQueueOptions = { isCurrent?: () => boolean; label?: string };

/**
 * Pi appends command receipts to a Session: preserve FIFO, including reads.
 * A stale queued command must be rejected BEFORE it mutates the old workspace.
 * An already-started mutation is never retried or reported as cancelled here.
 */
export function createGISCommandQueue({ onChange, maxPending = 32 }: {
  onChange?: (activities: GISCommandActivity[]) => void; maxPending?: number;
} = {}) {
  if (!Number.isSafeInteger(maxPending) || maxPending < 1) throw new TypeError('maxPending must be a positive integer.');
  const tails = new Map<string, Promise<unknown>>();
  const counts = new Map<string, number>();
  let sequence = 0;
  let activities: GISCommandActivity[] = [];
  const publish = (activity: GISCommandActivity) => {
    activities = [...activities.filter(item => item.id !== activity.id), activity].slice(-64);
    try { onChange?.(activities.map(item => ({ ...item }))); } catch { /* Observation must not block an accepted operation. */ }
  };
  return async function run<T>(sessionId: string, operation: () => Promise<T>, options: GISQueueOptions = {}): Promise<T> {
    if ((counts.get(sessionId) ?? 0) >= maxPending) throw new Error('操作队列已满，请等待当前任务完成。');
    const activity: GISCommandActivity = { id: ++sequence, sessionId, label: options.label ?? 'GIS 操作', status: 'queued', queuedAt: Date.now() };
    counts.set(sessionId, (counts.get(sessionId) ?? 0) + 1); publish(activity);
    const execute = async () => {
      if (options.isCurrent && !options.isCurrent()) {
        const error = Object.assign(new Error('项目已切换，尚未开始的旧操作已撤回。'), { code: 'command_superseded' });
        publish({ ...activity, status: 'superseded', finishedAt: Date.now(), error: error.message });
        throw error;
      }
      activity.startedAt = Date.now(); publish({ ...activity, status: 'running' });
      try {
        const value = await operation(); publish({ ...activity, status: 'completed', finishedAt: Date.now() }); return value;
      } catch (error) {
        publish({ ...activity, status: 'failed', finishedAt: Date.now(), error: error instanceof Error ? error.message : String(error) }); throw error;
      }
    };
    const pending = (tails.get(sessionId) ?? Promise.resolve()).then(execute, execute);
    tails.set(sessionId, pending);
    try { return await pending; }
    finally {
      const count = (counts.get(sessionId) ?? 1) - 1;
      count ? counts.set(sessionId, count) : counts.delete(sessionId);
      if (tails.get(sessionId) === pending) tails.delete(sessionId);
    }
  };
}
