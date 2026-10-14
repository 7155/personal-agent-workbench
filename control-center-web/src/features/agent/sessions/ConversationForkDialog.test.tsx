import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import { HttpControlTransport } from '@/platform/http-transport';
import type { ControlRequest } from '@/platform/transport';
import { StubControlTransport } from '@/test/stub-control-transport';
import { ConversationForkDialog, type ConversationNode } from './ConversationForkDialog';

afterEach(() => { cleanup(); vi.useRealTimers(); });

const nodes: ConversationNode[] = [
  { entryId: 'public-first', role: 'user', text: '第一条公开请求', createdAtMs: 1 },
  { entryId: 'public-second', role: 'assistant', text: '第二条公开回答', createdAtMs: 2 },
];
function candidates() { return { items: nodes.map((node, index) => ({ ...node, entryId: `pi-${index}` })) }; }
function deferred<T = unknown>() {
  let resolve!: (value: T) => void; let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function dialog(transport: StubControlTransport, { open = true, sessionId = 'session-first', onJump = vi.fn() } = {}) {
  return <ControlTransportProvider transport={transport}><TooltipProvider><ConversationForkDialog
    open={open} sessionId={sessionId} sessionTitle="公开对话" nodes={nodes}
    branchAvailable branchBlocked={false} onOpenChange={vi.fn()} onJump={onJump} onCreated={vi.fn()}
  /></TooltipProvider></ControlTransportProvider>;
}
function readRequests(transport: StubControlTransport) { return transport.requests.filter(request => request.pathId === 'agent.session.forks.list'); }

describe('conversation path read recovery', () => {
  it('keeps the chosen public node through a failed read and explicit read-only retry', async () => {
    const first = deferred(); const second = deferred(); let reads = 0;
    const transport = new StubControlTransport('mock', { 'agent.session.forks.list': () => ++reads === 1 ? first.promise : second.promise });
    render(dialog(transport));
    const selected = screen.getByRole('radio', { name: /第一条公开请求/ }); fireEvent.click(selected);
    await act(async () => first.reject(new Error('read unavailable')));
    expect(selected).toBeChecked();
    expect(screen.getByRole('button', { name: '跳到节点' })).toBeEnabled();
    expect(screen.getByRole('button', { name: '创建分支' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '重试历史节点' }));
    expect(selected).toBeChecked();
    expect(screen.getByRole('button', { name: '创建分支' })).toBeDisabled();
    await act(async () => second.resolve(candidates()));
    await waitFor(() => expect(screen.getByRole('button', { name: '创建分支' })).toBeEnabled());
    expect(selected).toBeChecked();
    expect(readRequests(transport)).toHaveLength(2);
    expect(transport.requests.some(request => request.pathId === 'agent.session.forks.create')).toBe(false);
  });

  it.each(['close', 'session', 'transport'] as const)('cancels the retired read on %s and ignores its late failure', async change => {
    const pending = deferred(); const next = deferred(); let reads = 0;
    const transport = new StubControlTransport('mock', { 'agent.session.forks.list': () => ++reads === 1 ? pending.promise : next.promise });
    const replacement = new StubControlTransport('mock', { 'agent.session.forks.list': () => next.promise });
    const view = render(dialog(transport));
    view.rerender(dialog(change === 'transport' ? replacement : transport, { open: change !== 'close', sessionId: change === 'session' ? 'session-next' : 'session-first' }));
    expect(readRequests(transport)[0]?.signal?.aborted).toBe(true);
    await act(async () => pending.reject(new Error('retired read failed')));
    expect(screen.queryByText(/retired read failed/)).not.toBeInTheDocument();
    if (change !== 'close') {
      expect(screen.getByText(/正在核对/)).toBeInTheDocument();
      await act(async () => next.resolve(candidates()));
      await waitFor(() => expect(screen.getByRole('button', { name: '创建分支' })).toBeEnabled());
    }
  });

  it('releases a stalled HTTP history read without losing public navigation or creating a branch', async () => {
    let signal: AbortSignal | undefined;
    const transport = new StubControlTransport('mock', {});
    const http = new HttpControlTransport({ baseUrl: 'http://127.0.0.1:8768', fetch: (async (_url, init) => {
      signal = init?.signal as AbortSignal;
      return { ok: true, headers: new Headers({ 'Content-Type': 'application/json' }), text: () => new Promise((_resolve, reject) => {
        signal?.addEventListener('abort', () => reject(signal?.reason), { once: true });
      }) } as Response;
    }) as typeof fetch });
    transport.request = <T,>(request: ControlRequest) => { transport.requests.push(request); return http.request<T>(request); };
    vi.useFakeTimers();
    render(dialog(transport));
    fireEvent.click(screen.getByRole('radio', { name: /第一条公开请求/ }));
    await act(async () => { await vi.advanceTimersByTimeAsync(45_001); });
    expect(signal?.aborted).toBe(true);
    expect(screen.getByRole('button', { name: '重试历史节点' })).toBeEnabled();
    expect(screen.getByRole('radio', { name: /第一条公开请求/ })).toBeChecked();
    expect(screen.getByRole('button', { name: '跳到节点' })).toBeEnabled();
    expect(screen.getByRole('button', { name: '创建分支' })).toBeDisabled();
    expect(transport.requests.some(request => request.pathId === 'agent.session.forks.create')).toBe(false);
  });
});
