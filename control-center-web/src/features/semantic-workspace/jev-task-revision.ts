import type { ControlRequest, ControlTransport } from '@/platform/transport';
import { ControlRoutePolicyError } from '@/platform/routes';
import { jevRecord } from './jev-execution';
import { observeRequest } from './organization-request';
import { JevCommandJournal } from './jev-command-journal';

export interface JevRevisionOptions {
  graphId: string; rootId: string; taskId: string; taskHash: string;
  expectedTopologyRevision: number; expectedRequirementsRevision: number;
  affectedTaskIds: string[]; downstreamTaskIds: string[]; retainedAcceptedTaskIds: string[];
  available: boolean; unavailableReason: string;
}
export interface JevRevisionInput {
  action: 'revise_task'; graphId: string; rootId: string; taskId: string; taskHash: string;
  expectedTopologyRevision: number; expectedRequirementsRevision: number;
  objective: string; expectedOutput: string; acceptanceCriteria: string[]; reason: string; rootObjective?: string;
}
export interface JevRevisionReceipt {
  revisionId: string; graphId: string; status: 'awaiting_drain' | 'applied'; changedTaskId: string;
}
export interface JevRevisionControls {
  load: (graphId: string, taskId: string, signal?: AbortSignal) => Promise<JevRevisionOptions>;
  pending: (graphId: string, taskId: string) => JevRevisionInput | undefined;
  submit: (input: JevRevisionInput) => Promise<JevRevisionReceipt>;
}
const strings = (v: unknown): v is string[] => Array.isArray(v) && v.every(item => typeof item === 'string');
export async function readJevRevision(transport: ControlTransport, roomId: string, graphId: string, taskId: string, signal?: AbortSignal): Promise<JevRevisionOptions> {
  const value = jevRecord(await observeRequest<ControlRequest, unknown>(request => transport.request(request), {
    pathId: 'agent.jev.command', params: { roomId }, signal, timeoutMs: 15_000,
    body: { action: 'revision_options', graphId, taskId, clientMessageId: `paw-jev-revision-options-${crypto.randomUUID()}` },
  }, { timeout: '任务修改范围暂时无法读取，请重试。' }));
  if (value.ok !== true || value.graphId !== graphId || value.taskId !== taskId || typeof value.rootId !== 'string'
    || typeof value.taskHash !== 'string' || typeof value.available !== 'boolean'
    || !Number.isInteger(value.expectedTopologyRevision) || !Number.isInteger(value.expectedRequirementsRevision)
    || !strings(value.affectedTaskIds) || !strings(value.downstreamTaskIds) || !strings(value.retainedAcceptedTaskIds)) {
    throw new Error('修改范围没有与当前任务匹配，请重新读取。');
  }
  return value as unknown as JevRevisionOptions;
}

const keyFor = (room: string, graph: string, task: string) => JSON.stringify([room, graph, task]);
const journal = new JevCommandJournal<JevRevisionInput>({
  storagePrefix: 'paw.jev.revision.v1', requestPrefix: 'paw-jev-revision-',
  conflictMessage: '上次修改尚未确认，请先核实同一次修改。',
  restoreInput(value, key) {
    const input = jevRecord(value);
    return input.action === 'revise_task'
      && ['graphId', 'rootId', 'taskId', 'taskHash', 'objective', 'expectedOutput', 'reason'].every(field => typeof input[field] === 'string')
      && Number.isInteger(input.expectedTopologyRevision) && Number.isInteger(input.expectedRequirementsRevision)
      && strings(input.acceptanceCriteria) && (input.rootObjective === undefined || typeof input.rootObjective === 'string')
      && key === keyFor(JSON.parse(key)[0], String(input.graphId), String(input.taskId))
      ? value as JevRevisionInput : undefined;
  },
  rejectionStatuses: [400, 401, 403, 404, 409, 413, 422],
  localRejection: error => error instanceof ControlRoutePolicyError,
});
export function pendingJevRevision(transport: ControlTransport, room: string, graph: string, task: string) {
  const attempt = journal.read(transport, keyFor(room, graph, task)); return attempt?.uncertain ? attempt.input : undefined;
}
export async function commandJevRevision(transport: ControlTransport, roomId: string, input: JevRevisionInput): Promise<JevRevisionReceipt> {
  return journal.execute(transport, keyFor(roomId, input.graphId, input.taskId), input, async attempt => {
    const value = jevRecord(await observeRequest<ControlRequest, unknown>(request => transport.request(request), {
      pathId: 'agent.jev.command', params: { roomId }, timeoutMs: 30_000, body: { ...attempt.input, clientMessageId: attempt.clientMessageId },
    }, { timeout: '修改暂未收到回执，请核实同一次修改。' }));
    if (value.ok !== true || value.graphId !== attempt.input.graphId || value.changedTaskId !== attempt.input.taskId
      || typeof value.revisionId !== 'string' || !value.revisionId || !['awaiting_drain', 'applied'].includes(String(value.status))) {
      throw new Error('尚未收到匹配的任务修改回执，请核实同一次修改。');
    }
    return value as unknown as JevRevisionReceipt;
  });
}
