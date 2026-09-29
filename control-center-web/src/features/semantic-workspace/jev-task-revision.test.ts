import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ControlTransport } from '@/platform/transport';
import { commandJevRevision, pendingJevRevision, readJevRevision, type JevRevisionInput } from './jev-task-revision';

afterEach(() => sessionStorage.clear());
const input: JevRevisionInput = { action: 'revise_task', graphId: 'g', rootId: 'r', taskId: 'a', taskHash: 'hash', expectedTopologyRevision: 2,
  expectedRequirementsRevision: 1, objective: '调整世界高度', expectedOutput: '可运行世界', acceptanceCriteria: ['高度为64'], reason: '更新需求' };
const receipt = { ok: true, graphId: 'g', changedTaskId: 'a', revisionId: 'revision:1', status: 'awaiting_drain' };
const transport = (request: ReturnType<typeof vi.fn>, identity = 'revision-test') => ({ request, kind: 'http', connectionIdentity: identity } as unknown as ControlTransport);
describe('task revision transport', () => {
  it('reads the server impact scope and rejects another task binding', async () => {
    const request = vi.fn().mockResolvedValue({ ok: true, ...input, available: true, affectedTaskIds: ['a', 'c'], downstreamTaskIds: ['c'], retainedAcceptedTaskIds: ['b'] });
    expect((await readJevRevision(transport(request), 'room', 'g', 'a')).retainedAcceptedTaskIds).toEqual(['b']);
    expect(request.mock.calls[0][0].body.action).toBe('revision_options');
    request.mockResolvedValue({ ok: true, ...input, taskId: 'b' });
    await expect(readJevRevision(transport(request), 'room', 'g', 'a')).rejects.toThrow('匹配');
  });
  it('recovers the same request after timeout and transport remount without allowing a different revision', async () => {
    const request = vi.fn().mockRejectedValue(new Error('disconnected'));
    await expect(commandJevRevision(transport(request), 'room', input)).rejects.toThrow('disconnected');
    const first = request.mock.calls[0][0].body;
    const restored = transport(request);
    expect(pendingJevRevision(restored, 'room', 'g', 'a')).toEqual(input);
    await expect(commandJevRevision(restored, 'room', { ...input, objective: 'different' })).rejects.toThrow('上次');
    expect(request).toHaveBeenCalledTimes(1);
    request.mockResolvedValue(receipt);
    expect((await commandJevRevision(restored, 'room', input)).status).toBe('awaiting_drain');
    expect(request.mock.calls[1][0].body).toEqual(first);
    expect(pendingJevRevision(restored, 'room', 'g', 'a')).toBeUndefined();
  });
  it('retains an uncertain command when the response is for another task', async () => {
    const request = vi.fn().mockResolvedValue({ ...receipt, changedTaskId: 'b' }); const connection = transport(request);
    await expect(commandJevRevision(connection, 'room', input)).rejects.toThrow('匹配');
    expect(pendingJevRevision(connection, 'room', 'g', 'a')).toEqual(input);
  });
  it('allows a new snapshot after a first definitive conflict', async () => {
    const request = vi.fn().mockRejectedValue(Object.assign(new Error('conflict'), { status: 409 })); const connection = transport(request);
    await expect(commandJevRevision(connection, 'room', input)).rejects.toThrow('conflict');
    expect(pendingJevRevision(connection, 'room', 'g', 'a')).toBeUndefined();
    request.mockResolvedValue(receipt);
    await commandJevRevision(connection, 'room', { ...input, taskHash: 'fresh' });
    expect(request.mock.calls[0][0].body.clientMessageId).not.toBe(request.mock.calls[1][0].body.clientMessageId);
  });
});
