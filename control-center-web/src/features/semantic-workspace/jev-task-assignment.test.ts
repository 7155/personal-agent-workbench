import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ControlTransport } from '@/platform/transport';
import { ControlRoutePolicyError } from '@/platform/routes';
import { commandJevAssignment, pendingJevAssignment, readJevAssignment, type JevAssignmentInput } from './jev-task-assignment';

afterEach(() => { sessionStorage.clear(); vi.useRealTimers(); });
const input: JevAssignmentInput = { action: 'request_reclaim', graphId: 'graph', taskId: 'task', taskHash: 'server-hash', targetParticipantId: 'mars', reason: '调整分工' };
const transport = (request: ReturnType<typeof vi.fn>, identity?: string) => ({ request, kind: 'http', connectionIdentity: identity } as unknown as ControlTransport);

describe('task assignment requests', () => {
  it('reads a bound menu without sending a mutation', async () => {
    const request = vi.fn().mockResolvedValue({ ok: true, graphId: 'graph', taskId: 'task', taskHash: 'exact', ownerId: 'earth', action: 'request_reclaim', targetParticipantIds: ['mars'], unavailableReason: '' });
    const result = await readJevAssignment(transport(request), 'room', 'graph', 'task');
    expect(result.targetParticipantIds).toEqual(['mars']);
    expect(request.mock.calls[0][0].body).toMatchObject({ action: 'assignment_options', graphId: 'graph', taskId: 'task' });
    request.mockResolvedValue({ ok: true, graphId: 'old', taskId: 'task', taskHash: 'stale' });
    await expect(readJevAssignment(transport(request), 'room', 'graph', 'task')).rejects.toThrow('匹配');
  });
  it('keeps the exact intent and key after a timeout, including remount recovery', async () => {
    const request = vi.fn().mockRejectedValue(new Error('network disconnected'));
    const first = transport(request, 'assignment-fixture');
    await expect(commandJevAssignment(first, 'room', input)).rejects.toThrow('network');
    const firstKey = request.mock.calls[0][0].body.clientMessageId;
    const restored = transport(request, 'assignment-fixture');
    expect(pendingJevAssignment(restored, 'room', 'graph', 'task')).toEqual(input);
    await expect(commandJevAssignment(restored, 'room', { ...input, targetParticipantId: 'venus' })).rejects.toThrow('上次');
    expect(request).toHaveBeenCalledTimes(1);
    request.mockResolvedValue({ status: 'requested', replayed: true });
    await commandJevAssignment(restored, 'room', input);
    expect(request.mock.calls[1][0].body.clientMessageId).toBe(firstKey);
    expect(pendingJevAssignment(restored, 'room', 'graph', 'task')).toBeUndefined();
  });
  it('clears a first definitive rejection so current options can be read again', async () => {
    const request = vi.fn().mockRejectedValue(Object.assign(new Error('stale task'), { status: 409 }));
    const connection = transport(request);
    await expect(commandJevAssignment(connection, 'room', input)).rejects.toThrow('stale');
    expect(pendingJevAssignment(connection, 'room', 'graph', 'task')).toBeUndefined();
    request.mockResolvedValue({ status: 'requested' });
    await commandJevAssignment(connection, 'room', { ...input, taskHash: 'fresh' });
    expect(request.mock.calls[0][0].body.clientMessageId).not.toBe(request.mock.calls[1][0].body.clientMessageId);
  });
  it('clears a first local route rejection because no request was sent', async () => {
    const request = vi.fn().mockRejectedValue(new ControlRoutePolicyError('agent.jev.command', 'targetParticipantId is not allowlisted'));
    const connection = transport(request, 'local-route-rejection');
    await expect(commandJevAssignment(connection, 'room', input)).rejects.toBeInstanceOf(ControlRoutePolicyError);
    const firstKey = request.mock.calls[0][0].body.clientMessageId;
    expect(pendingJevAssignment(connection, 'room', 'graph', 'task')).toBeUndefined();
    const restored = transport(request, 'local-route-rejection');
    expect(pendingJevAssignment(restored, 'room', 'graph', 'task')).toBeUndefined();
    request.mockResolvedValue({ status: 'requested' });
    await commandJevAssignment(restored, 'room', { ...input, targetParticipantId: 'venus' });
    expect(request.mock.calls[1][0].body.clientMessageId).not.toBe(firstKey);
  });
  it('retains an earlier uncertain intent when a later local route rejects its retry', async () => {
    const request = vi.fn().mockRejectedValueOnce(new Error('network disconnected'))
      .mockRejectedValueOnce(new ControlRoutePolicyError('agent.jev.command', 'targetParticipantId is not allowlisted'));
    const connection = transport(request, 'earlier-uncertain');
    await expect(commandJevAssignment(connection, 'room', input)).rejects.toThrow('network disconnected');
    const firstKey = request.mock.calls[0][0].body.clientMessageId;
    await expect(commandJevAssignment(connection, 'room', input)).rejects.toBeInstanceOf(ControlRoutePolicyError);
    expect(pendingJevAssignment(connection, 'room', 'graph', 'task')).toEqual(input);
    expect(pendingJevAssignment(transport(request, 'earlier-uncertain'), 'room', 'graph', 'task')).toEqual(input);
    expect(request.mock.calls[1][0].body.clientMessageId).toBe(firstKey);
  });
  it('does not mistake an unrelated 200 response for acceptance', async () => {
    const request = vi.fn().mockResolvedValue({ ok: true }); const connection = transport(request);
    await expect(commandJevAssignment(connection, 'room', input)).rejects.toThrow('尚未确认');
    expect(pendingJevAssignment(connection, 'room', 'graph', 'task')).toEqual(input);
  });
});
