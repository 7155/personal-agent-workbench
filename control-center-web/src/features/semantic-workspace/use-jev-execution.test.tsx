import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MockControlTransport } from '@/test/mock-transport';
import type { ControlRequest } from '@/platform/transport';
import { HttpControlTransport } from '@/platform/http-transport';
import { createJevWork } from './jev-execution';
import { useJevExecution } from './use-jev-execution';

const graph = (id: string, final = false) => ({ ok: true, mode: 'jev', graphId: id, rootId: `root:${id}`, snapshotVersion: id,
  phase: final ? 'final' : 'execute', requirementsRevision: 1, stopped: false,
  tasks: [], edges: [], ready: [], running: [], review: [], blocked: [], effects: [], events: [], final: final ? { status: 'completed', content: '已完成' } : {} });
const catalog = { ok: true, mode: 'jev', items: [
  { graph_id: 'current', room_id: 'one', root_turn_id: 'root:current', phase: 'execute' },
  { graph_id: 'history', room_id: 'one', root_turn_id: 'root:history', phase: 'final' },
] };
afterEach(cleanup);

describe('Jev snapshot lifecycle', () => {
  it.each(['rejected', 'network'])('keeps an unconfirmed %s Stop on its original error path', async failure => {
    const transport = new MockControlTransport({ routes: {
      'agent.jev.get': (request: ControlRequest) => request.query?.graphId ? graph('current') : catalog,
      'agent.jev.command': () => {
        if (failure === 'network') throw new Error('offline response lost');
        return { ok: false, status: 'unknown' };
      },
    } });
    const { result } = renderHook(() => useJevExecution({ roomId: 'one', enabled: true, active: true, transport }));
    await waitFor(() => expect(result.current.liveSnapshot?.graphId).toBe('current'));
    await act(async () => result.current.stop());
    expect(result.current.snapshot?.stopped).toBe(false);
    expect(result.current.error).not.toBe('');
    expect(transport.requests.filter(call => call.request.pathId === 'agent.jev.command')).toHaveLength(1);
  });

  it('observes an accepted Stop until its classifier settles without a second Stop', async () => {
    let stopped = false;
    let drained = false;
    const transport = new MockControlTransport({ routes: {
      'agent.jev.get': (request: ControlRequest) => request.query?.graphId ? {
        ...graph('current'), stopped, classificationDrained: drained,
        pendingClassifications: drained ? [] : [{ requestId: 'original', graphId: 'current', status: stopped ? 'cancellation_requested' : 'pending' }],
      } : { ...catalog, items: [{ ...catalog.items[0], stopped }] },
      'agent.jev.command': () => { stopped = true; return { ok: false, status: 'cancellation_pending', pendingTargets: ['classification'] }; },
    } });
    const { result } = renderHook(() => useJevExecution({ roomId: 'one', enabled: true, active: true, transport }));
    await waitFor(() => expect(result.current.liveSnapshot?.graphId).toBe('current'));
    await act(async () => result.current.stop());
    expect(result.current.snapshot?.stopped).toBe(true);
    expect(result.current.snapshot?.classificationDrained).toBe(false);
    expect(result.current.busy).toBe(true);
    expect(result.current.error).toBe('');
    drained = true;
    act(() => result.current.onEvents([{ payload: { status: 'jev_updated', classificationSettled: true, graphId: 'current' } }]));
    await waitFor(() => expect(result.current.busy).toBe(false));
    expect(result.current.snapshot?.stopped).toBe(true);
    expect(transport.requests.filter(call => call.request.pathId === 'agent.jev.command')).toHaveLength(1);
  });

  it('polls a stopped historical classifier without replacing a newer completed live Root', async () => {
    let drained = false;
    const transport = new MockControlTransport({ routes: { 'agent.jev.get': (request: ControlRequest) => request.query?.graphId
      ? request.query.graphId === 'current' ? graph('current', true) : { ...graph('history'), stopped: true,
        classificationDrained: drained, pendingClassifications: drained ? [] : [{ requestId: 'old-original', graphId: 'history', status: 'cancellation_requested' }] }
      : catalog,
    } });
    const { result } = renderHook(() => useJevExecution({ roomId: 'one', enabled: true, active: true, transport }));
    await waitFor(() => expect(result.current.liveSnapshot?.graphId).toBe('current'));
    act(() => result.current.selectGraph('history'));
    await waitFor(() => expect(result.current.snapshot?.graphId).toBe('history'));
    expect(result.current.busy).toBe(false);
    vi.useFakeTimers();
    try {
      // Re-render under the fake clock so the existing observation timer is owned by it.
      act(() => result.current.onEvents([{ payload: { status: 'jev_updated' } }]));
      await act(async () => { await Promise.resolve(); });
      const before = transport.requests.length;
      drained = true;
      await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
      expect(transport.requests.length).toBeGreaterThan(before);
      expect(result.current.snapshot?.classificationDrained).toBe(true);
      expect(result.current.snapshot?.graphId).toBe('history');
      expect(result.current.liveSnapshot?.graphId).toBe('current');
      expect(result.current.busy).toBe(false);
      act(() => result.current.onEvents([{ payload: { status: 'jev_updated', graphId: 'history', classificationSettled: true } }]));
      await act(async () => { await Promise.resolve(); });
      expect(result.current.liveSnapshot?.graphId).toBe('current');
      const settledCount = transport.requests.length;
      await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
      expect(transport.requests).toHaveLength(settledCount);
      expect(transport.requests.every(call => call.request.pathId === 'agent.jev.get')).toBe(true);
    } finally { vi.useRealTimers(); }
  });

  it.each([false, true])('follows a newly started Root unless history was explicitly selected (%s)', async explicitHistory => {
    let nextTurn = false;
    const transport = new MockControlTransport({ routes: { 'agent.jev.get': (request: ControlRequest) => request.query?.graphId
      ? graph(String(request.query.graphId), request.query.graphId !== 'next')
      : { ...catalog, items: nextTurn ? [{ graph_id: 'next', room_id: 'one', root_turn_id: 'root:next', phase: 'execute' }, ...catalog.items] : catalog.items } } });
    const { result } = renderHook(() => useJevExecution({ roomId: 'one', enabled: true, active: true, transport }));
    await waitFor(() => expect(result.current.snapshot?.graphId).toBe('current'));
    if (explicitHistory) {
      act(() => result.current.selectGraph('history'));
      await waitFor(() => expect(result.current.snapshot?.graphId).toBe('history'));
    }
    nextTurn = true;
    act(() => result.current.onEvents([{ payload: { mode: 'jev', graphId: 'next' } }]));
    await waitFor(() => expect(result.current.liveSnapshot?.graphId).toBe('next'));
    expect(result.current.snapshot?.graphId).toBe(explicitHistory ? 'history' : 'next');
    expect(transport.requests.every(call => call.request.pathId === 'agent.jev.get')).toBe(true);
  });

  it('shares the initial read with reconnect instead of aborting the first usable snapshot', async () => {
    let resolve!: (value: unknown) => void;
    const pending = new Promise(value => { resolve = value; });
    const transport = new MockControlTransport({ routes: { 'agent.jev.get': (request: ControlRequest) => request.query?.graphId ? pending : catalog } });
    const { result } = renderHook(() => useJevExecution({ roomId: 'one', enabled: true, active: true, transport }));
    await waitFor(() => expect(transport.requests).toHaveLength(2));
    const initialRead = transport.requests[1].request;
    act(() => { result.current.refresh(); result.current.refresh(); });
    expect(initialRead.signal?.aborted).toBe(false);
    expect(transport.requests).toHaveLength(2);
    await act(async () => resolve(graph('current')));
    await waitFor(() => expect(result.current.snapshot?.graphId).toBe('current'));
  });

  it('publishes each usable snapshot and coalesces dense events into one fresh read', async () => {
    const reads: { resolve: (value: unknown) => void; request: ControlRequest }[] = [];
    const transport = new MockControlTransport({ routes: { 'agent.jev.get': (request: ControlRequest) => request.query?.graphId
      ? new Promise(resolve => reads.push({ resolve, request })) : catalog } });
    const { result } = renderHook(() => useJevExecution({ roomId: 'one', enabled: true, active: true, transport }));
    await waitFor(() => expect(reads).toHaveLength(1));
    act(() => {
      result.current.refresh();
      for (let i = 0; i < 20; i++) result.current.onEvents([{ payload: { status: 'jev_updated' } }]);
    });
    expect(reads[0].request.signal?.aborted).toBe(false);
    expect(transport.requests).toHaveLength(2);
    await act(async () => reads[0].resolve({ ...graph('current'), snapshotVersion: 'first' }));
    await waitFor(() => expect(reads).toHaveLength(2));
    expect(result.current.snapshot?.version).toBe('first');
    act(() => {
      for (let i = 0; i < 20; i++) result.current.onEvents([{ payload: { mode: 'jev' } }]);
    });
    await act(async () => reads[1].resolve({ ...graph('current'), snapshotVersion: 'second' }));
    await waitFor(() => expect(reads).toHaveLength(3));
    expect(result.current.snapshot?.version).toBe('second');
    await act(async () => reads[2].resolve({ ...graph('current', true), snapshotVersion: 'latest' }));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.snapshot?.version).toBe('latest');
    expect(result.current.busy).toBe(false);
    expect(transport.requests).toHaveLength(6);
    expect(reads.every(read => !read.request.signal?.aborted)).toBe(true);
  });

  it('abandons a queued fresh read when hidden and reads anew on returning', async () => {
    let resolve!: (value: unknown) => void;
    const pending = new Promise(value => { resolve = value; });
    const transport = new MockControlTransport({ routes: { 'agent.jev.get': (request: ControlRequest) => request.query?.graphId ? pending : catalog } });
    const { result, rerender } = renderHook(({ active }) => useJevExecution({ roomId: 'one', enabled: true, active, transport }), { initialProps: { active: true } });
    await waitFor(() => expect(transport.requests).toHaveLength(2));
    act(() => result.current.onEvents([{ payload: { status: 'jev_updated' } }]));
    rerender({ active: false });
    expect(transport.requests[1].request.signal?.aborted).toBe(true);
    await act(async () => resolve(graph('current')));
    expect(result.current.snapshot).toBeNull();
    expect(transport.requests).toHaveLength(2);
    rerender({ active: true });
    await waitFor(() => expect(result.current.snapshot?.graphId).toBe('current'));
    expect(transport.requests).toHaveLength(4);
  });

  it('binds a replacement graph read to the latest explicit selection', async () => {
    let resolveOld!: (value: unknown) => void;
    let first = true;
    const old = new Promise(value => { resolveOld = value; });
    const transport = new MockControlTransport({ routes: { 'agent.jev.get': (request: ControlRequest) => {
      if (!request.query?.graphId) return catalog;
      if (first) { first = false; return old; }
      return graph(String(request.query.graphId), request.query.graphId === 'history');
    } } });
    const { result } = renderHook(() => useJevExecution({ roomId: 'one', enabled: true, active: true, transport }));
    await waitFor(() => expect(transport.requests).toHaveLength(2));
    act(() => result.current.selectGraph('history'));
    await waitFor(() => expect(result.current.snapshot?.graphId).toBe('history'));
    expect(transport.requests[1].request.signal?.aborted).toBe(true);
    await act(async () => resolveOld(graph('current')));
    expect(result.current.snapshot?.graphId).toBe('history');
    expect(result.current.liveSnapshot?.graphId).toBe('current');
  });

  it('does not let a pre-command read overwrite the confirmed plan refresh', async () => {
    let resolveOld!: (value: unknown) => void;
    const old = new Promise(value => { resolveOld = value; });
    let graphReads = 0;
    let approved = false;
    const view = () => ({ ...graph('current'), snapshotVersion: approved ? 'approved' : 'awaiting',
      phase: approved ? 'execute' : 'awaiting_approval', planApproval: { status: approved ? 'approved' : 'awaiting_approval', planHash: 'plan-hash' } });
    const transport = new MockControlTransport({ routes: {
      'agent.jev.get': (request: ControlRequest) => !request.query?.graphId ? catalog : ++graphReads === 2 ? old : view(),
      'agent.jev.command': () => { approved = true; return { ok: true, graphId: 'current' }; },
    } });
    const { result } = renderHook(() => useJevExecution({ roomId: 'one', enabled: true, active: true, transport }));
    await waitFor(() => expect(result.current.awaitingPlan).toBe(true));
    act(() => result.current.refresh());
    await waitFor(() => expect(graphReads).toBe(2));
    const oldView = view();
    await act(async () => { expect(await result.current.decidePlan('approve_plan')).toBe(true); });
    expect(result.current.snapshot?.version).toBe('approved');
    await act(async () => resolveOld(oldView));
    expect(result.current.snapshot?.version).toBe('approved');
    expect(transport.requests.filter(call => call.request.pathId === 'agent.jev.command')).toHaveLength(1);
  });

  it('releases the stop command after its fresh read while continuous events keep reading', async () => {
    const reads: ((value: unknown) => void)[] = [];
    let first = true;
    const transport = new MockControlTransport({ routes: {
      'agent.jev.get': (request: ControlRequest) => {
        if (!request.query?.graphId) return catalog;
        if (first) { first = false; return graph('current'); }
        return new Promise(resolve => reads.push(resolve));
      },
      'agent.jev.command': () => ({ ok: true }),
    } });
    const { result } = renderHook(() => useJevExecution({ roomId: 'one', enabled: true, active: true, transport }));
    await waitFor(() => expect(result.current.busy).toBe(true));
    act(() => result.current.refresh());
    await waitFor(() => expect(reads).toHaveLength(1));
    let stopped = false;
    act(() => { void result.current.stop().then(() => { stopped = true; }); });
    await waitFor(() => expect(transport.requests.some(call => call.request.pathId === 'agent.jev.command')).toBe(true));
    act(() => result.current.onEvents([{ payload: { status: 'jev_updated' } }]));
    await act(async () => reads[0](graph('current')));
    await waitFor(() => expect(reads).toHaveLength(2));
    expect(stopped).toBe(false);
    act(() => result.current.onEvents([{ payload: { status: 'jev_updated' } }]));
    await act(async () => reads[1]({ ...graph('current'), stopped: true }));
    await waitFor(() => expect(reads).toHaveLength(3));
    expect(stopped).toBe(true);
    expect(result.current.stopping).toBe(false);
    expect(result.current.snapshot?.stopped).toBe(true);
    expect(result.current.loading).toBe(true);
    act(() => result.current.onEvents([{ payload: { status: 'jev_updated' } }]));
    await act(async () => reads[2]({ ...graph('current'), stopped: true }));
    await waitFor(() => expect(reads).toHaveLength(4));
    expect(result.current.stopping).toBe(false);
  });

  it('uses reads only on opening/reconnecting and keeps current execution while selecting history', async () => {
    let completed = false;
    const transport = new MockControlTransport({ routes: { 'agent.jev.get': (request: ControlRequest) => request.query?.graphId
      ? graph(String(request.query.graphId), request.query.graphId === 'history' || completed) : catalog } });
    const { result } = renderHook(() => useJevExecution({ roomId: 'one', enabled: true, active: true, transport }));
    await waitFor(() => expect(result.current.snapshot?.graphId).toBe('current'));
    act(() => result.current.selectGraph('history'));
    await waitFor(() => expect(result.current.snapshot?.graphId).toBe('history'));
    expect(result.current.busy).toBe(true);
    expect(result.current.liveSnapshot?.graphId).toBe('current');
    completed = true;
    act(() => result.current.onEvents([{ payload: { status: 'jev_updated', graphId: 'current' } }]));
    await waitFor(() => expect(result.current.busy).toBe(false));
    expect(result.current.selectedId).toBe('history');
    expect(transport.requests.every(item => item.request.pathId === 'agent.jev.get')).toBe(true);
  });

  it('ignores a late graph response after changing rooms', async () => {
    let resolve!: (value: unknown) => void;
    const pending = new Promise(value => { resolve = value; });
    const transport = new MockControlTransport({ routes: { 'agent.jev.get': (request: ControlRequest) => {
      if (request.params?.roomId === 'two') return { ok: true, mode: 'jev', items: [] };
      return request.query?.graphId ? pending : catalog;
    } } });
    const { result, rerender } = renderHook(({ roomId }) => useJevExecution({ roomId, enabled: true, active: true, transport }), { initialProps: { roomId: 'one' } });
    await waitFor(() => expect(transport.requests.some(call => call.request.query?.graphId === 'current')).toBe(true));
    rerender({ roomId: 'two' });
    await waitFor(() => expect(result.current.loading).toBe(false));
    await act(async () => resolve(graph('current')));
    expect(result.current.snapshot).toBeNull(); expect(result.current.items).toEqual([]);
  });

  it('never treats a stop click as a stopped snapshot before the server confirms it', async () => {
    let resolve!: (value: unknown) => void;
    let stopped = false;
    const transport = new MockControlTransport({ routes: {
      'agent.jev.get': (request: ControlRequest) => request.query?.graphId ? { ...graph(String(request.query.graphId)), stopped } : catalog,
      'agent.jev.command': () => new Promise(value => { resolve = value; }),
    } });
    const { result } = renderHook(() => useJevExecution({ roomId: 'one', enabled: true, active: true, transport }));
    await waitFor(() => expect(result.current.busy).toBe(true));
    let request!: Promise<void>; act(() => { request = result.current.stop(); });
    expect(result.current.stopping).toBe(true); expect(result.current.snapshot?.stopped).toBe(false);
    stopped = true;
    await act(async () => { resolve({ ok: true }); await request; });
    expect(result.current.snapshot?.stopped).toBe(true);
    expect(transport.requests.find(item => item.request.pathId === 'agent.jev.command')?.request.body).toMatchObject({ action: 'stop', graphId: 'current' });
  });

  it('does not read or execute while disabled', () => {
    const transport = new MockControlTransport();
    renderHook(() => useJevExecution({ roomId: 'one', enabled: false, active: true, transport }));
    expect(transport.requests).toHaveLength(0);
  });

  it('recovers an accepted request after a lost HTTP response with GET only', async () => {
    const posts: Record<string, unknown>[] = [];
    const fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      if (init?.method === 'POST') { posts.push(JSON.parse(String(init.body))); throw new TypeError('Failed to fetch'); }
      const payload = url.searchParams.has('graphId') ? graph('accepted') : { ok: true, mode: 'jev', items: [{ graph_id: 'accepted', room_id: 'recovery-room', phase: 'execute', clientMessageId: posts[0].clientMessageId }] };
      return new Response(JSON.stringify(payload), { headers: { 'Content-Type': 'application/json' } });
    };
    const first = new HttpControlTransport({ baseUrl: 'https://accepted-recovery.example.test', fetch });
    await expect(createJevWork(first, 'recovery-room', { message: '核对原附件', attachmentIds: ['managed-attachment'] })).rejects.toThrow('Failed to fetch');
    const transport = new HttpControlTransport({ baseUrl: 'https://accepted-recovery.example.test', fetch });
    const { result } = renderHook(() => useJevExecution({ roomId: 'recovery-room', enabled: true, active: true, transport }));
    await waitFor(() => expect(result.current.snapshot?.graphId).toBe('accepted'));
    expect(result.current.recoveredAdmission).toMatchObject({ message: '核对原附件', attachmentIds: ['managed-attachment'] });
    expect(result.current.pendingInput).toBeUndefined();
    expect(posts).toHaveLength(1);
  });

  it('explicitly retries the original HTTP request with its attachments after a transport remount', async () => {
    const posts: Record<string, unknown>[] = [];
    const fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      if (init?.method === 'POST') {
        posts.push(JSON.parse(String(init.body)));
        if (posts.length === 1) throw new TypeError('Failed to fetch');
        return new Response(JSON.stringify({ ok: true, accepted: true, graphId: 'retry-accepted' }));
      }
      const payload = url.searchParams.has('graphId') ? graph('retry-accepted') : { ok: true, mode: 'jev', items: posts.length > 1 ? [{ graph_id: 'retry-accepted', room_id: 'retry-room', phase: 'execute' }] : [] };
      return new Response(JSON.stringify(payload));
    };
    const first = new HttpControlTransport({ baseUrl: 'https://retry-recovery.example.test', fetch });
    await expect(createJevWork(first, 'retry-room', { message: '核对原附件', attachmentIds: ['managed-room-media'], previousRootId: 'previous-root', modelRouting: 'participant', toolApprovalMode: 'jev_dangerous', verificationMode: 'independent' })).rejects.toThrow();
    const transport = new HttpControlTransport({ baseUrl: 'https://retry-recovery.example.test', fetch });
    const { result } = renderHook(() => useJevExecution({ roomId: 'retry-room', enabled: true, active: true, transport }));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.pendingInput?.attachmentIds).toEqual(['managed-room-media']);
    expect(result.current.verificationMode).toBe('independent');
    await act(async () => { expect(await result.current.retryPending()).toBe(true); });
    expect(posts).toHaveLength(2);
    expect(posts[1]).toEqual(posts[0]);
    expect(result.current.pendingInput).toBeUndefined();
  });

  it('sends the selected verification mode on the next new Root', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.jev.get': (request: ControlRequest) => request.query?.graphId ? graph('current') : catalog,
      'agent.jev.command': () => ({ ok: true, accepted: true, graphId: 'next', rootId: 'root:next' }),
    } });
    const { result } = renderHook(() => useJevExecution({ roomId: 'one', enabled: true, active: true, transport }));
    await waitFor(() => expect(result.current.liveSnapshot?.graphId).toBe('current'));
    expect(result.current.verificationMode).toBe('auto');
    act(() => result.current.setVerificationMode('independent'));
    await act(async () => { expect(await result.current.send('继续核对新任务')).toBe(true); });
    expect(transport.requests.find(call => call.request.pathId === 'agent.jev.command')?.request.body).toMatchObject({ action: 'create', message: '继续核对新任务', verificationMode: 'independent', previousRootId: 'root:current' });
  });

  it('sends plan adjustments into the same root with attachments, while a ready plan is not running', async () => {
    let status = 'awaiting_approval';
    const transport = new MockControlTransport({ routes: {
      'agent.jev.get': (request: ControlRequest) => request.query?.graphId ? { ...graph('current'), phase: status === 'planning' ? 'plan' : status, planApproval: { status, planHash: 'plan-hash', requirementsRevision: 1, proposal: { tasks: [] } } } : catalog,
      'agent.jev.command': () => { status = 'planning'; return { ok: true, graphId: 'current' }; },
    } });
    const { result } = renderHook(() => useJevExecution({ roomId: 'one', enabled: true, active: true, transport }));
    await waitFor(() => expect(result.current.awaitingPlan).toBe(true));
    expect(result.current.busy).toBe(false);
    await act(async () => { expect(await result.current.send('补充附件中的范围', ['managed-revision-media'])).toBe(true); });
    const commands = transport.requests.filter(call => call.request.pathId === 'agent.jev.command');
    expect(commands).toHaveLength(1);
    expect(commands[0].request.body).toMatchObject({ action: 'adjust_plan', graphId: 'current', rootId: 'root:current', planHash: 'plan-hash', message: '补充附件中的范围', attachmentIds: ['managed-revision-media'] });
  });

  it('reconciles a lost approval response by its exact action id without another POST', async () => {
    const commands: Record<string, unknown>[] = [];
    let status = 'awaiting_approval';
    const fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      if (init?.method === 'POST') { commands.push(JSON.parse(String(init.body))); status = 'approved'; throw new TypeError('Failed to fetch'); }
      return new Response(JSON.stringify(url.searchParams.has('graphId') ? { ...graph('current'), phase: status === 'approved' ? 'execute' : status, planApproval: { status, planHash: 'approved-hash', requirementsRevision: 1, lastActionClientMessageId: commands[0]?.clientMessageId || '', proposal: { tasks: [] } } } : catalog));
    };
    const transport = new HttpControlTransport({ baseUrl: 'https://plan-recovery.example.test', fetch });
    const { result } = renderHook(() => useJevExecution({ roomId: 'one', enabled: true, active: true, transport }));
    await waitFor(() => expect(result.current.awaitingPlan).toBe(true));
    await act(async () => { await expect(result.current.decidePlan('approve_plan')).rejects.toThrow('Failed to fetch'); });
    expect(result.current.pendingPlan?.planHash).toBe('approved-hash');
    act(() => result.current.refresh());
    await waitFor(() => expect(result.current.liveSnapshot?.planApproval?.status).toBe('approved'));
    expect(result.current.pendingPlan).toBeUndefined();
    expect(commands).toHaveLength(1);
  });
});
