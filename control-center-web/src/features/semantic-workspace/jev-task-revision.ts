import type { ControlRequest, ControlTransport } from '@/platform/transport';
import { ControlRoutePolicyError } from '@/platform/routes';
import { jevRecord } from './jev-execution';
import { observeRequest } from './organization-request';

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

type Attempt = { input: JevRevisionInput; clientMessageId: string; uncertain: boolean };
const journal = new WeakMap<ControlTransport, Map<string, Attempt>>();
const keyFor = (room: string, graph: string, task: string) => JSON.stringify([room, graph, task]);
function storageKey(transport: ControlTransport, key: string) {
  const identity = transport.connectionIdentity || (transport.kind === 'native' ? 'native-local' : '');
  return identity ? `paw.jev.revision.v1:${encodeURIComponent(identity)}:${encodeURIComponent(key)}` : '';
}
function get(transport: ControlTransport, key: string): Attempt | undefined {
  const cached = journal.get(transport)?.get(key); if (cached) return cached;
  const storage = storageKey(transport, key); if (!storage) return;
  try {
    const entry = jevRecord(JSON.parse(sessionStorage.getItem(storage) || 'null')); const input = jevRecord(entry.input);
    if (entry.uncertain === true && typeof entry.clientMessageId === 'string' && input.action === 'revise_task'
      && ['graphId', 'rootId', 'taskId', 'taskHash', 'objective', 'expectedOutput', 'reason'].every(field => typeof input[field] === 'string')
      && Number.isInteger(input.expectedTopologyRevision) && Number.isInteger(input.expectedRequirementsRevision)
      && strings(input.acceptanceCriteria) && (input.rootObjective === undefined || typeof input.rootObjective === 'string')
      && key === keyFor(JSON.parse(key)[0], String(input.graphId), String(input.taskId))) return entry as unknown as Attempt;
  } catch { /* Keep in-memory recovery if storage is unavailable. */ }
}
function save(transport: ControlTransport, key: string, attempt?: Attempt) {
  let entries = journal.get(transport); if (!entries) { entries = new Map(); journal.set(transport, entries); }
  if (attempt) entries.set(key, attempt); else entries.delete(key);
  const storage = storageKey(transport, key); if (!storage) return;
  try {
    if (attempt) sessionStorage.setItem(storage, JSON.stringify({ ...attempt, uncertain: true })); else sessionStorage.removeItem(storage);
  } catch { /* The live view still retains the exact request. */ }
}
export function pendingJevRevision(transport: ControlTransport, room: string, graph: string, task: string) {
  const attempt = get(transport, keyFor(room, graph, task)); return attempt?.uncertain ? attempt.input : undefined;
}
export async function commandJevRevision(transport: ControlTransport, roomId: string, input: JevRevisionInput): Promise<JevRevisionReceipt> {
  const key = keyFor(roomId, input.graphId, input.taskId); const previous = get(transport, key);
  if (previous && JSON.stringify(previous.input) !== JSON.stringify(input)) throw new Error('上次修改尚未确认，请先核实同一次修改。');
  const attempt = previous ?? { input: structuredClone(input), clientMessageId: `paw-jev-revision-${crypto.randomUUID()}`, uncertain: false };
  save(transport, key, attempt);
  try {
    const value = jevRecord(await observeRequest<ControlRequest, unknown>(request => transport.request(request), {
      pathId: 'agent.jev.command', params: { roomId }, timeoutMs: 30_000, body: { ...attempt.input, clientMessageId: attempt.clientMessageId },
    }, { timeout: '修改暂未收到回执，请核实同一次修改。' }));
    if (value.ok !== true || value.graphId !== input.graphId || value.changedTaskId !== input.taskId
      || typeof value.revisionId !== 'string' || !value.revisionId || !['awaiting_drain', 'applied'].includes(String(value.status))) {
      throw new Error('尚未收到匹配的任务修改回执，请核实同一次修改。');
    }
    save(transport, key); return value as unknown as JevRevisionReceipt;
  } catch (error) {
    const status = jevRecord(error).status;
    const rejected = !attempt.uncertain && (error instanceof ControlRoutePolicyError
      || typeof status === 'number' && [400, 401, 403, 404, 409, 413, 422].includes(status));
    save(transport, key, rejected ? undefined : { ...attempt, uncertain: true }); throw error;
  }
}
