import type { ControlRequest, ControlTransport } from '@/platform/transport';
import { ControlRoutePolicyError } from '@/platform/routes';
import { jevRecord } from './jev-execution';
import { observeRequest } from './organization-request';
import { JevCommandJournal } from './jev-command-journal';
import type { JevRevisionControls } from './jev-task-revision';

export type JevAssignmentAction = 'reassign' | 'request_reclaim';
export interface JevAssignmentOptions {
  graphId: string; taskId: string; taskHash: string; ownerId: string;
  action: JevAssignmentAction | ''; targetParticipantIds: string[]; unavailableReason: string;
}
export interface JevAssignmentInput {
  action: JevAssignmentAction; graphId: string; taskId: string; taskHash: string;
  targetParticipantId: string; reason: string;
}
export interface JevTaskControls {
  revision?: JevRevisionControls;
  load: (graphId: string, taskId: string, signal?: AbortSignal) => Promise<JevAssignmentOptions>;
  pending: (graphId: string, taskId: string) => JevAssignmentInput | undefined;
  submit: (input: JevAssignmentInput) => Promise<'requested' | 'applied'>;
}

export async function readJevAssignment(transport: ControlTransport, roomId: string, graphId: string, taskId: string, signal?: AbortSignal): Promise<JevAssignmentOptions> {
  const value = jevRecord(await observeRequest<ControlRequest, unknown>(request => transport.request(request), {
    pathId: 'agent.jev.command', params: { roomId }, signal, timeoutMs: 15_000,
    body: { action: 'assignment_options', graphId, taskId, clientMessageId: `paw-jev-options-${crypto.randomUUID()}` },
  }, { timeout: '伙伴列表暂时无法读取，请重试。' }));
  if (value.ok !== true || value.graphId !== graphId || value.taskId !== taskId || typeof value.taskHash !== 'string'
    || !['', 'reassign', 'request_reclaim'].includes(String(value.action)) || !Array.isArray(value.targetParticipantIds)) {
    throw new Error('任务操作没有与当前任务匹配，请重新读取。');
  }
  return { graphId, taskId, taskHash: value.taskHash, ownerId: String(value.ownerId || ''),
    action: value.action as JevAssignmentOptions['action'], targetParticipantIds: value.targetParticipantIds.filter((id): id is string => typeof id === 'string'),
    unavailableReason: String(value.unavailableReason || '') };
}

const keyFor = (roomId: string, graphId: string, taskId: string) => JSON.stringify([roomId, graphId, taskId]);
const journal = new JevCommandJournal<JevAssignmentInput>({
  storagePrefix: 'paw.jev.assignment.v1', requestPrefix: 'paw-jev-assignment-',
  conflictMessage: '上次任务操作尚未确认，请先核实同一次操作。',
  restoreInput(value, key) {
    const input = jevRecord(value);
    return ['reassign', 'request_reclaim'].includes(String(input.action))
      && ['graphId', 'taskId', 'taskHash', 'targetParticipantId', 'reason'].every(field => typeof input[field] === 'string')
      && key === keyFor(JSON.parse(key)[0], String(input.graphId), String(input.taskId))
      ? value as JevAssignmentInput : undefined;
  },
  rejectionStatuses: [400, 401, 403, 404, 409, 413, 422],
  localRejection: error => error instanceof ControlRoutePolicyError,
});
export function pendingJevAssignment(transport: ControlTransport, roomId: string, graphId: string, taskId: string) {
  const attempt = journal.read(transport, keyFor(roomId, graphId, taskId)); return attempt?.uncertain ? attempt.input : undefined;
}

export async function commandJevAssignment(transport: ControlTransport, roomId: string, input: JevAssignmentInput): Promise<'requested' | 'applied'> {
  return journal.execute(transport, keyFor(roomId, input.graphId, input.taskId), input, async attempt => {
    const value = jevRecord(await observeRequest<ControlRequest, unknown>(request => transport.request(request), {
      pathId: 'agent.jev.command', params: { roomId }, timeoutMs: 30_000, body: { ...attempt.input, clientMessageId: attempt.clientMessageId },
    }, { timeout: '任务操作暂未收到回执，请核实同一次操作。' }));
    const expected = attempt.input.action === 'request_reclaim' ? 'requested' : 'applied';
    if (value.status !== expected) throw new Error('服务端尚未确认任务操作，请核实同一次操作。');
    return expected;
  });
}
