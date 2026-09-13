import { afterEach, expect, it, vi } from 'vitest';
import type { ControlRequest, ControlTransport } from '@/platform/transport';
import { requestLabControl } from './control-request';

const prepare: ControlRequest = { pathId: 'agent.eval-lab.projects.command', body: {
  action: 'prepare_app', projectId: 'project-one', expectedRevision: 14,
  clientRequestId: 'lab-project:original-prepare', input: { directory: 'polar-research-app' },
} };
function delayed() {
  let settle!: (value: unknown) => void;
  const request = vi.fn((_input: ControlRequest) => new Promise((resolve) => { settle = resolve; }));
  return { transport: { kind: 'http', request } as unknown as ControlTransport, request, resolve: (value: unknown) => settle(value) };
}
afterEach(() => vi.useRealTimers());
it('keeps the same prepare observation alive for a real 202 second completion', async () => {
  vi.useFakeTimers(); const bridge = delayed(); const receipt = { ok: true, clientRequestId: 'lab-project:original-prepare' };
  const result = requestLabControl(bridge.transport, prepare);
  const observed = expect(result).resolves.toEqual(receipt);
  await vi.advanceTimersByTimeAsync(202_000);
  expect(bridge.request).toHaveBeenCalledTimes(1);
  expect(bridge.request.mock.calls[0]?.[0]).toMatchObject({ body: prepare.body, timeoutMs: 300_000 });
  bridge.resolve(receipt); await observed;
});
it('ends prepare observation at 300 seconds without issuing another command', async () => {
  vi.useFakeTimers(); const bridge = delayed();
  const result = requestLabControl(bridge.transport, prepare);
  const rejected = expect(result).rejects.toThrow('核对');
  await vi.advanceTimersByTimeAsync(299_999);
  expect((bridge.request.mock.calls[0]?.[0] as ControlRequest).signal?.aborted).toBe(false);
  await vi.advanceTimersByTimeAsync(1); await rejected;
  expect((bridge.request.mock.calls[0]?.[0] as ControlRequest).signal?.aborted).toBe(true);
  expect(bridge.request).toHaveBeenCalledTimes(1);
});
it('keeps ordinary Lab reads at 20 seconds and permits explicit observation abort', async () => {
  vi.useFakeTimers(); const bridge = delayed();
  const rejected = expect(requestLabControl(bridge.transport, { pathId: 'agent.eval-lab.projects.get' })).rejects.toThrow('核对');
  await vi.advanceTimersByTimeAsync(20_000); await rejected;
  expect(bridge.request.mock.calls[0]?.[0]).not.toHaveProperty('timeoutMs');
  const controller = new AbortController();
  const stopped = expect(requestLabControl(bridge.transport, { ...prepare, signal: controller.signal })).rejects.toThrow('取消');
  controller.abort(); await stopped;
  expect(bridge.request).toHaveBeenCalledTimes(2);
});
