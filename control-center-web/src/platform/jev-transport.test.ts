import { describe, expect, it, vi } from 'vitest';
import { HttpControlTransport } from './http-transport';
import { createJevWork, pendingJevInput } from '@/features/semantic-workspace/jev-execution';
import { commandJevAssignment, readJevAssignment } from '@/features/semantic-workspace/jev-task-assignment';
import { commandJevRevision } from '@/features/semantic-workspace/jev-task-revision';
import { ControlRoutePolicyError } from './routes';

describe('Jev HTTP transport contract', () => {
  it('allows revision CAS and replacement requirements through the real route policy', async () => {
    const bodies: Record<string, unknown>[] = [];
    const transport = new HttpControlTransport({ baseUrl: 'https://revision-jev.example.test', fetch: async (_url, init) => {
      const body = JSON.parse(String(init?.body)); bodies.push(body);
      return new Response(JSON.stringify({ ok: true, graphId: body.graphId, changedTaskId: body.taskId, revisionId: 'rev', status: 'awaiting_drain' }));
    } });
    const input = { action: 'revise_task' as const, graphId: 'graph', rootId: 'root', taskId: 'task', taskHash: 'exact',
      expectedTopologyRevision: 3, expectedRequirementsRevision: 1, objective: 'new task', rootObjective: 'new goal',
      expectedOutput: 'artifact', acceptanceCriteria: ['criterion'], reason: 'change' };
    await commandJevRevision(transport, 'room', input);
    expect(bodies).toEqual([{ ...input, clientMessageId: expect.any(String) }]);
  });
  it('sends graph reads and owner commands through the same room-scoped route', async () => {
    const calls: { url: URL; init?: RequestInit }[] = [];
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: new URL(String(input)), init });
      return new Response(JSON.stringify({ ok: true }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    });
    const transport = new HttpControlTransport({ baseUrl: 'https://gateway.example.test', fetch });
    await transport.request({ pathId: 'agent.jev.get', params: { roomId: 'room:one' }, query: { graphId: 'graph:one' } });
    await transport.request({ pathId: 'agent.jev.command', params: { roomId: 'room:one' }, body: { action: 'create', message: '核对实现', clientMessageId: 'client-one', attachmentIds: ['media-one'], modelRouting: 'balanced', toolApprovalMode: 'dispatch', verificationMode: 'auto' } });
    await transport.request({ pathId: 'agent.jev.command', params: { roomId: 'room:one' }, body: { action: 'create', message: '由其他伙伴复核', clientMessageId: 'client-two', verificationMode: 'independent' } });
    await transport.request({ pathId: 'agent.jev.command', params: { roomId: 'room:one' }, body: { action: 'stop', graphId: 'graph:one', clientMessageId: 'stop-one' } });
    expect(calls.map(call => call.init?.method)).toEqual(['GET', 'POST', 'POST', 'POST']);
    expect(calls[0].url.pathname).toBe('/api/agent/rooms/room%3Aone/jev');
    expect(calls[0].url.searchParams.get('graphId')).toBe('graph:one');
    expect(JSON.parse(String(calls[1].init?.body))).toMatchObject({ attachmentIds: ['media-one'], modelRouting: 'balanced', toolApprovalMode: 'dispatch', verificationMode: 'auto' });
    expect(JSON.parse(String(calls[2].init?.body))).toMatchObject({ action: 'create', verificationMode: 'independent' });
    expect(JSON.parse(String(calls[3].init?.body))).toEqual({ action: 'stop', graphId: 'graph:one', clientMessageId: 'stop-one' });
  });

  it('allows correcting a definitive admission rejection without replaying it as an unknown request', async () => {
    const bodies: Record<string, unknown>[] = [];
    const transport = new HttpControlTransport({ baseUrl: 'https://rejected-jev.example.test', fetch: async (_input, init) => {
      bodies.push(JSON.parse(String(init?.body)));
      return bodies.length === 1 ? new Response(JSON.stringify({ error: 'invalid attachment owner' }), { status: 422 })
        : new Response(JSON.stringify({ ok: true, accepted: true, graphId: 'corrected' }));
    } });
    await expect(createJevWork(transport, 'room', { message: '核对附件', attachmentIds: ['invalid-media'] })).rejects.toThrow('invalid attachment owner');
    expect(pendingJevInput(transport, 'room')).toBeUndefined();
    await createJevWork(transport, 'room', { message: '核对附件', attachmentIds: ['valid-media'] });
    expect(bodies[1].clientMessageId).not.toBe(bodies[0].clientMessageId);
    expect(bodies[1].attachmentIds).toEqual(['valid-media']);
  });

  it('sends assignment options, queued reassignment and running reclaim with exact bindings', async () => {
    const calls: { url: URL; body: Record<string, unknown> }[] = [];
    const fetch = vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      calls.push({ url: new URL(String(request)), body });
      const result = body.action === 'assignment_options'
        ? { ok: true, graphId: body.graphId, taskId: body.taskId, taskHash: 'hash:queued', ownerId: 'earth', action: 'reassign', targetParticipantIds: ['mars'], unavailableReason: '' }
        : { ok: true, graphId: body.graphId, taskId: body.taskId, status: body.action === 'request_reclaim' ? 'requested' : 'applied' };
      return new Response(JSON.stringify(result), { headers: { 'Content-Type': 'application/json' } });
    });
    const transport = new HttpControlTransport({ baseUrl: 'https://assignment-jev.example.test', fetch });

    const options = await readJevAssignment(transport, 'room:one', 'graph:one', 'task:queued');
    expect(options.taskHash).toBe('hash:queued');
    await commandJevAssignment(transport, 'room:one', { action: 'reassign', graphId: 'graph:one', taskId: 'task:queued', taskHash: options.taskHash, targetParticipantId: 'mars', reason: '调整分工' });
    await commandJevAssignment(transport, 'room:one', { action: 'request_reclaim', graphId: 'graph:one', taskId: 'task:running', taskHash: 'hash:running', targetParticipantId: 'venus', reason: '执行中回收' });

    expect(fetch).toHaveBeenCalledTimes(3);
    expect(calls.map(call => call.url.pathname)).toEqual(Array(3).fill('/api/agent/rooms/room%3Aone/jev'));
    expect(calls.map(call => call.body.action)).toEqual(['assignment_options', 'reassign', 'request_reclaim']);
    expect(calls[1].body).toMatchObject({ graphId: 'graph:one', taskId: 'task:queued', taskHash: 'hash:queued', targetParticipantId: 'mars', reason: '调整分工' });
    expect(calls[2].body).toMatchObject({ graphId: 'graph:one', taskId: 'task:running', taskHash: 'hash:running', targetParticipantId: 'venus', reason: '执行中回收' });
    expect(calls.every(call => typeof call.body.clientMessageId === 'string')).toBe(true);

    await expect(transport.request({ pathId: 'agent.jev.command', params: { roomId: 'room:one' }, body: { action: 'reassign', graphId: 'graph:one', taskId: 'task:queued', taskHash: 'hash:queued', targetParticipantId: 'mars', clientMessageId: 'unexpected-field', unexpected: true } })).rejects.toBeInstanceOf(ControlRoutePolicyError);
    expect(fetch).toHaveBeenCalledTimes(3);
  });
});
