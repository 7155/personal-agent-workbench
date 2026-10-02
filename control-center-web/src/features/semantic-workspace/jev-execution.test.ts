import { describe, expect, it } from 'vitest';
import { MockControlTransport } from '@/test/mock-transport';
import { acknowledgeJevAdmission, createJevWork, jevAbstention, jevAwaitingPlan, jevIsBusy, jevLeafTasks, jevPhaseStep, jevStatusLabel, jevTaskStage, parseJevList, parseJevSnapshot, pendingJevInput } from './jev-execution';

export function graphFixture(id = 'graph-one', overrides: Record<string, unknown> = {}) {
  return { ok: true, mode: 'jev', graphId: id, rootId: `root:${id}`, snapshotVersion: 'version-one', phase: 'execute', requirementsRevision: 1, stopped: false, final: {},
    tasks: [{ id: 'task-one', root_id: `root:${id}`, room_id: 'room-one', state: 'active', revision: 1, owner_id: 'actor-one', parent_id: '', objective: '核对实现', expected_output: '有证据的核对结果', acceptance: ['关键行为符合要求'], result: '', artifacts: [], evidence: [], accepted_turn_id: 'dispatch-one' }],
    edges: [], ready: [], running: [], review: [], blocked: [], events: [],
    effects: [{ effectId: 'dispatch-one', operation: 'dispatch', state: 'accepted', request: { taskId: 'task-one', taskRevision: 1, ownerId: 'actor-one', purpose: 'execute' }, receipt: {} }], ...overrides };
}
export function graphList(...ids: string[]) {
  return { ok: true, mode: 'jev', items: ids.map(id => ({ graph_id: id, root_turn_id: `root:${id}`, room_id: 'room-one', objective: `目标 ${id}`, phase: 'execute', stopped: 0, created_at_ms: 1 })) };
}

describe('Jev execution receipts', () => {
  it('keeps only room-owned attachment receipts for an older Jev input', () => {
    const graph = parseJevSnapshot(graphFixture('graph-one', { roomId: 'room-one', rootAttachmentReceipts: [
      { ownerType: 'room', roomId: 'room-one', mediaId: 'media_abcdefghijklmnop', fileName: 'comparison.md', mimeType: 'text/markdown', byteSize: 40182 },
      { ownerType: 'room', roomId: 'other-room', mediaId: 'media_abcdefghijklmnop', fileName: 'private.md', mimeType: 'text/markdown', byteSize: 10 },
    ] }), 'graph-one');
    expect(graph.rootAttachments).toEqual([{ roomId: 'room-one', mediaId: 'media_abcdefghijklmnop', fileName: 'comparison.md', mimeType: 'text/markdown', byteSize: 40182 }]);
  });
  it('shows a drained latest abstention without releasing the open Root', () => {
    const graph = parseJevSnapshot(graphFixture('graph-one', { effects: [], events: [{ source_id: 'latest', state: 'done', result_json: { status: 'abstained' } }] }), 'graph-one');
    expect(jevAbstention(graph)?.id).toBe('latest');
    expect(jevStatusLabel(graph)).toBe('暂未选出下一步');
    expect(jevStatusLabel({ ...graph, phase: 'route' })).toBe('等待重新判断');
    expect(jevIsBusy(graph)).toBe(true);
    expect(jevAbstention({ ...graph, events: [{ id: 'later', kind: 'work_submitted', state: 'done', result: { status: 'applied' } }, ...graph.events] })).toBeNull();
    expect(jevAbstention({ ...graph, events: [...graph.events, { id: 'queued', kind: 'executor_drained', state: 'pending', result: {} }] })).toBeNull();
    expect(jevAbstention({ ...graph, running: ['task-one'] })).toBeNull();
    expect(jevAbstention({ ...graph, stopped: true })).toBeNull();
    for (const executionStatus of ['prepared', 'admitted', 'running', 'unknown', '']) {
      expect(jevAbstention({ ...graph, effects: [{ effectId: 'in-flight', operation: 'dispatch', state: 'accepted', executionStatus, request: {}, receipt: {} }] })).toBeNull();
    }
  });
  it.each(['awaiting_input', 'awaiting_approval', 'deferred'] as const)('accepts the actual %s lifecycle phase without inventing execution', phase => {
    const graph = parseJevSnapshot(graphFixture('graph-one', { phase, effects: [], planApproval: { status: phase, planHash: 'current-plan', requirementsRevision: 1, proposal: { tasks: [] } } }), 'graph-one');
    expect(graph.phase).toBe(phase);
    expect(jevPhaseStep(graph.phase)).toBe('plan');
    expect(jevAwaitingPlan(graph)).toBe(true);
    expect(jevIsBusy(graph)).toBe(false);
    expect(jevLeafTasks(graph)).toEqual([]);
  });
  it('does not turn an accepted dispatch or active WorkItem into runtime execution', () => {
    const graph = parseJevSnapshot(graphFixture(), 'graph-one');
    expect(jevTaskStage(graph.tasks[0], graph)).toBe('dispatched');
    expect(jevTaskStage(graph.tasks[0], { ...graph, running: ['task-one'] })).toBe('running');
    expect(jevTaskStage({ ...graph.tasks[0], revision: 2 }, graph)).toBe('unknown');
    expect(jevTaskStage({ ...graph.tasks[0], state: 'review' }, graph)).toBe('review');
    expect(jevTaskStage({ ...graph.tasks[0], state: 'done' }, graph)).toBe('done');
  });

  it('rejects another graph and filters another room from the list', () => {
    expect(() => parseJevSnapshot(graphFixture(), 'other-graph')).toThrow('没有与当前工作匹配');
    expect(parseJevList(graphList('graph-one'), 'other-room')).toEqual([]);
    expect(() => parseJevList({ ok: true }, 'room-one')).toThrow('有效的');
  });

  it('shows a returned task only from the committed owner receipt for its current revision', () => {
    const graph = parseJevSnapshot(graphFixture('graph-one', { effects: [], events: [
      { source_id: 'verify-return', kind: 'verification', state: 'done', result_json: JSON.stringify({ status: 'applied', receipt: { application: { status: 'applied', operation: 'return', task: { id: 'task-one', revision: 2 } } } }) },
    ] }), 'graph-one');
    const task = { ...graph.tasks[0], state: 'queued', revision: 2 };
    expect(jevTaskStage(task, graph)).toBe('returned');
    expect(jevTaskStage({ ...task, revision: 3 }, graph)).toBe('queued');
    expect(jevTaskStage(task, { ...graph, events: [{ ...graph.events[0], result: { status: 'owner_error' } }] })).toBe('queued');
  });

  it('retries an uncertain admission with exactly the same id and request, never a second root', async () => {
    let first = true;
    const transport = new MockControlTransport({ routes: { 'agent.jev.command': () => {
      if (first) { first = false; throw new Error('connection lost after admission'); }
      return { ok: true, accepted: true, graphId: 'graph-one', rootId: 'root:graph-one' };
    } } });
    await expect(createJevWork(transport, 'room-one', { message: '核对实现', previousRootId: 'older-root', verificationMode: 'auto' })).rejects.toThrow('connection lost');
    await expect(createJevWork(transport, 'room-one', { message: '另一件事' })).rejects.toThrow('上次发送尚未确认');
    await expect(createJevWork(transport, 'room-one', { message: '核对实现', verificationMode: 'independent' })).rejects.toThrow('上次发送尚未确认');
    await createJevWork(transport, 'room-one', { message: '核对实现', previousRootId: 'root:graph-one', verificationMode: 'auto' });
    expect(transport.requests).toHaveLength(2);
    expect(transport.requests[1].request.body).toEqual(transport.requests[0].request.body);
  });

  it('replays an older pending request without adding a new field to its committed identity', async () => {
    let first = true;
    const transport = new MockControlTransport({ routes: { 'agent.jev.command': () => {
      if (first) { first = false; throw new Error('connection lost after admission'); }
      return { ok: true, accepted: true, graphId: 'legacy-pending' };
    } } });
    await expect(createJevWork(transport, 'legacy-room', { message: '核对旧请求' })).rejects.toThrow('connection lost');
    await createJevWork(transport, 'legacy-room', { message: '核对旧请求', verificationMode: 'auto' });
    expect(transport.requests).toHaveLength(2);
    expect(transport.requests[1].request.body).toEqual(transport.requests[0].request.body);
    expect(transport.requests[1].request.body).not.toHaveProperty('verificationMode');
  });

  it('preserves attachment ownership and explicit routing policy in admission', async () => {
    const transport = new MockControlTransport({ routes: { 'agent.jev.command': { ok: true, accepted: true, graphId: 'graph-one' } } });
    await createJevWork(transport, 'room-one', { message: '阅读附件', attachmentIds: ['managed-room-media'], modelRouting: 'participant', toolApprovalMode: 'jev_dangerous' });
    expect(transport.requests[0].request).toMatchObject({ params: { roomId: 'room-one' }, body: { attachmentIds: ['managed-room-media'], modelRouting: 'participant', toolApprovalMode: 'jev_dangerous' } });
  });
  it('recovers the admission identity after a transport remount and clears it only from an exact server receipt', async () => {
    const first = new MockControlTransport({ routes: { 'agent.jev.command': () => { throw new Error('connection lost'); } } });
    Object.defineProperty(first, 'connectionIdentity', { value: 'http:jev-durable-fixture' });
    await expect(createJevWork(first, 'room-durable', { message: '保留这个请求' })).rejects.toThrow('connection lost');
    const second = new MockControlTransport();
    Object.defineProperty(second, 'connectionIdentity', { value: 'http:jev-durable-fixture' });
    expect(pendingJevInput(second, 'room-durable')?.message).toBe('保留这个请求');
    const id = (first.requests[0].request.body as Record<string, unknown>).clientMessageId;
    const items = parseJevList({ ok: true, mode: 'jev', items: [{ graph_id: 'admitted', room_id: 'room-durable', clientMessageId: id }] }, 'room-durable');
    expect(acknowledgeJevAdmission(second, 'room-durable', items)).toBe(true);
    expect(pendingJevInput(second, 'room-durable')).toBeUndefined();
    expect(second.requests).toHaveLength(0);
  });
});

it('can recover the original Jev admission after its caller edits the submitted input', async () => {
  let first = true;
  const transport = new MockControlTransport({ routes: { 'agent.jev.command': () => {
    if (first) { first = false; throw new Error('lost ACK'); }
    return { ok: true, accepted: true, graphId: 'original-graph', rootId: 'original-root' };
  } } });
  const input = { message: 'original task', attachmentIds: ['media-original'] };
  await expect(createJevWork(transport, 'room-journal-negative', input)).rejects.toThrow('lost ACK');
  input.message = 'next draft'; input.attachmentIds.push('media-next');
  await createJevWork(transport, 'room-journal-negative', { message: 'original task', attachmentIds: ['media-original'] });
  expect(transport.requests[1].request.body).toMatchObject({ message: 'original task', attachmentIds: ['media-original'] });
  expect(transport.requests[1].request.body).toEqual(transport.requests[0].request.body);
  expect(pendingJevInput(transport, 'room-journal-negative')).toBeUndefined();
});

it('does not let a late failed retry replace a newer admission after the original receipt was recovered', async () => {
  let call = 0;
  let retryStarted!: () => void;
  let nextStarted!: () => void;
  let failRetry!: (error: Error) => void;
  let failNext!: (error: Error) => void;
  const retryReady = new Promise<void>(resolve => { retryStarted = resolve; });
  const nextReady = new Promise<void>(resolve => { nextStarted = resolve; });
  const transport = new MockControlTransport({ routes: { 'agent.jev.command': () => {
    call += 1;
    if (call === 1) throw new Error('lost ACK');
    if (call === 2) return new Promise((_resolve, reject) => { failRetry = reject; retryStarted(); });
    return new Promise((_resolve, reject) => { failNext = reject; nextStarted(); });
  } } });
  const room = 'room-journal-late-retry';
  await expect(createJevWork(transport, room, { message: 'original task' })).rejects.toThrow('lost ACK');
  const originalId = (transport.requests[0].request.body as { clientMessageId: string }).clientMessageId;
  const retry = createJevWork(transport, room, { message: 'original task' }).catch(error => error);
  await retryReady;
  expect(acknowledgeJevAdmission(transport, room, [{ id: 'graph-original', roomId: room, rootId: 'root-original', title: 'original task', phase: 'final', clientMessageId: originalId, stopped: false, createdAtMs: 1 }])).toBe(true);
  const next = createJevWork(transport, room, { message: 'next task' }).catch(error => error);
  await nextReady;
  try {
    failRetry(new Error('late transport failure'));
    await retry;
    expect(pendingJevInput(transport, room)).toEqual({ message: 'next task' });
  } finally {
    failNext(new Error('end observation'));
    await next;
  }
});

it('shares an observed admission when the same backend transport remounts', async () => {
  let complete!: (value: object) => void;
  let started!: () => void;
  const ready = new Promise<void>(resolve => { started = resolve; });
  const first = new MockControlTransport({ routes: { 'agent.jev.command': () => new Promise(resolve => {
    complete = resolve; started();
  }) } });
  const remounted = new MockControlTransport({ routes: { 'agent.jev.command': () => {
    throw new Error('unexpected duplicate admission');
  } } });
  Object.defineProperty(first, 'connectionIdentity', { value: 'journal-live-remount' });
  Object.defineProperty(remounted, 'connectionIdentity', { value: 'journal-live-remount' });
  const input = { message: 'one task' };
  const original = createJevWork(first, 'room-live-remount', input);
  await ready;
  const joined = createJevWork(remounted, 'room-live-remount', input).catch(error => error);
  complete({ ok: true, accepted: true, graphId: 'graph-original', rootId: 'root-original' });
  expect(await joined).toEqual(await original);
  expect(remounted.requests).toHaveLength(0);
});

it('restores an existing v1 admission without a live owner and preserves its original request', async () => {
  const identity = 'journal-existing-v1';
  const room = 'room-existing-v1';
  const input = { message: 'original task', attachmentIds: ['original-media'], previousRootId: 'original-root' };
  const storageKey = `paw.jev.pending.v1:${encodeURIComponent(identity)}:${room}`;
  sessionStorage.setItem(storageKey, JSON.stringify({ input, signature: 'historical signature', clientMessageId: 'original-client-id', uncertain: false }));
  const transport = new MockControlTransport({ routes: { 'agent.jev.command': { ok: true, accepted: true, graphId: 'restored-graph' } } });
  Object.defineProperty(transport, 'connectionIdentity', { value: identity });
  const observed = pendingJevInput(transport, room)!;
  observed.attachmentIds!.push('edited-draft');
  expect(pendingJevInput(transport, room)).toEqual(input);
  await createJevWork(transport, room, { ...input, previousRootId: 'new-observation' });
  expect(transport.requests[0].request.body).toEqual({ action: 'create', ...input, strategy: 'auto', clientMessageId: 'original-client-id' });
  expect(sessionStorage.getItem(storageKey)).toBeNull();
});
