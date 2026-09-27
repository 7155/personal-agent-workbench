/** Lexical checks supplement, never replace, the host's authorized file access. */
export function workspaceFilePath(root: string, relative: string): string {
  if (!root.startsWith('/') || !relative || relative.startsWith('/') || /[\u0000-\u001f\\]/u.test(relative)) throw new Error('项目文件路径无效。');
  if (relative.split('/').some(part => part === '.' || part === '..' || part === '')) throw new Error('项目文件路径包含无效目录段。');
  return `${root.replace(/\/+$/, '')}/${relative}`;
}

export function isMissingWorkspaceFile(error: unknown): boolean {
  const value = error as { code?: string; message?: string } | null;
  return value?.code === 'ENOENT' || /path does not exist in the authorized workspace/.test(value?.message ?? '');
}

/** Bounded parallel reads; output order is stable regardless of completion order. */
export async function mapBounded<T, R>(items: readonly T[], concurrency: number, read: (item: T) => Promise<R>): Promise<R[]> {
  if (!Number.isSafeInteger(concurrency) || concurrency < 1) throw new TypeError('concurrency must be a positive integer.');
  const results = new Array<R>(items.length); let cursor = 0;
  const worker = async () => { while (cursor < items.length) { const index = cursor++; results[index] = await read(items[index]); } };
  await Promise.all(Array.from({ length: Math.min(items.length, Math.max(1, Math.floor(concurrency))) }, worker));
  return results;
}

/** Serial latest-value writer for viewport state. No overlapping read/CAS/write. */
export function createCoalescingWriter<T>(write: (value: T) => Promise<void>, onError: (error: unknown) => void = () => {}) {
  let latest: { value: T } | undefined, running: Promise<void> | undefined, disposed = false;
  async function drain() {
    try {
      while (latest && !disposed) {
        const current = latest; latest = undefined;
        try { await write(current.value); } catch (error) { if (!disposed) onError(error); }
      }
    } finally { running = undefined; }
  }
  return {
    push(value: T) {
      if (disposed) return Promise.resolve();
      latest = { value };
      if (!running) running = drain();
      return running;
    },
    dispose() { disposed = true; latest = undefined; },
  };
}
