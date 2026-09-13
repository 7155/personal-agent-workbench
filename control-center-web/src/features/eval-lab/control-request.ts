import type { ControlRequest, ControlTransport } from '@/platform/transport';

const instances = new WeakMap<ControlTransport, string>();
export function labConnectionKey(transport: ControlTransport): string {
  if (transport.connectionIdentity) return JSON.stringify([typeof window === 'undefined' ? '' : window.location.origin, transport.connectionIdentity]);
  let key = instances.get(transport);
  if (!key) { key = `${transport.kind}:${crypto.randomUUID()}`; instances.set(transport, key); }
  return key;
}

/** A lost HTTP observation never cancels or retries the underlying Lab job. */
export async function requestLabControl(transport: ControlTransport, input: ControlRequest): Promise<unknown> {
  const preparingApp = input.pathId === 'agent.eval-lab.projects.command'
    && input.body !== null && typeof input.body === 'object' && !Array.isArray(input.body)
    && input.body.action === 'prepare_app';
  // Full corpus + immutable App packaging is still synchronous. Extending only
  // this observation does not cancel/replay work or invent background progress.
  const timeoutMs = preparingApp ? 300_000 : 20_000;
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  let rejectAbort: (() => void) | undefined;
  const deadline = new Promise<never>((_resolve, reject) => {
    rejectAbort = () => { controller.abort(); reject(new Error('读取已取消。')); };
    if (input.signal?.aborted) { rejectAbort(); return; }
    input.signal?.addEventListener('abort', rejectAbort, { once: true });
    timer = setTimeout(() => {
      controller.abort();
      reject(new Error('服务未及时返回回执，请核对本次操作。'));
    }, timeoutMs);
  });
  try {
    return await Promise.race([transport.request({ ...input, ...(preparingApp ? { timeoutMs } : {}), signal: controller.signal }), deadline]);
  } finally {
    if (timer) clearTimeout(timer);
    if (rejectAbort) input.signal?.removeEventListener('abort', rejectAbort);
  }
}
