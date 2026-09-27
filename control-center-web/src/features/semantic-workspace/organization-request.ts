import type { OrganizationRequest, RequestOrganization } from './organization-model';

/** Bound observation even on hosts that ignore timeoutMs or AbortSignal.
 * Aborting observation never asserts that a server-side write was cancelled.
 */
export function requestOrganization(request: RequestOrganization, input: OrganizationRequest): Promise<unknown> {
  return observeRequest(request, input);
}

export function observeRequest<Input extends { signal?: AbortSignal; timeoutMs?: number }, Result>(
  request: (input: Input) => Promise<Result>, input: Input,
  messages: { timeout?: string; aborted?: string } = {},
): Promise<Result> {
  return new Promise((resolve, reject) => {
    const controller = new AbortController();
    let settled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const finish = (error: unknown, value?: unknown) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      input.signal?.removeEventListener('abort', abort);
      if (error) reject(error); else resolve(value as Result);
    };
    const abort = () => {
      finish(new DOMException(messages.aborted ?? '整理请求观察已取消', 'AbortError'));
      controller.abort();
    };
    if (input.signal?.aborted) { abort(); return; }
    input.signal?.addEventListener('abort', abort, { once: true });
    timer = setTimeout(() => {
      finish(new Error(messages.timeout ?? '整理请求超时，尚不能确认操作结果。'));
      controller.abort();
    }, input.timeoutMs ?? 25_000);
    Promise.resolve().then(() => {
      if (settled) return;
      return request({ ...input, signal: controller.signal });
    }).then(value => finish(null, value), error => finish(error));
  });
}
