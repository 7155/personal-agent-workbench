import type { ToolCallBlock } from './types';

export type CodeModeCallStatus = 'running' | 'ok' | 'error' | 'cancelled';

export interface CodeModeCall {
  id: string;
  name: string;
  args: string;
  status: CodeModeCallStatus;
  durationMs?: number;
  error?: string;
  cost?: number;
}

export interface CodeModeDetails {
  calls: CodeModeCall[];
  fullOutputPath?: string;
  /** Cold history may report that the nested-call receipt was incomplete. */
  nestedCallsComplete?: boolean;
}

const CODEMODE_OUTPUT_HEADER = /^Script (?:completed|failed)\nWall time [^\n]+\nOutput:\n/u;
const CODEMODE_STATUSES = new Set<CodeModeCallStatus>(['running', 'ok', 'error', 'cancelled']);

/** Keep Pi's codemode receipt fields bounded and display-safe at the UI edge. */
export function parseCodeModeDetails(value: unknown): CodeModeDetails | undefined {
  const root = Array.isArray(value) ? { calls: value } : record(value);
  if (!Array.isArray(root.calls)) return undefined;
  const calls: CodeModeCall[] = [];
  for (const candidate of root.calls) {
    const call = record(candidate);
    const id = text(call.id);
    const name = text(call.name);
    const args = displayArgs(call.arguments ?? call.args);
    const status = call.status;
    if (!id || !name || !CODEMODE_STATUSES.has(status as CodeModeCallStatus)) continue;
    calls.push({
      id,
      name,
      args,
      status: status as CodeModeCallStatus,
      ...(nonNegativeNumber(call.durationMs) !== undefined ? { durationMs: nonNegativeNumber(call.durationMs) } : {}),
      ...(text(call.error) ? { error: text(call.error) } : {}),
      ...(nonNegativeNumber(call.cost) !== undefined ? { cost: nonNegativeNumber(call.cost) } : {}),
    });
  }
  const fullOutputPath = text(root.fullOutputPath);
  const statedCompleteness = typeof root.nestedCallsComplete === 'boolean'
    ? root.nestedCallsComplete
    : typeof root.complete === 'boolean'
      ? root.complete
      : undefined;
  const nestedCallsComplete = calls.length !== root.calls.length ? false : statedCompleteness;
  return {
    calls,
    ...(fullOutputPath ? { fullOutputPath } : {}),
    ...(nestedCallsComplete !== undefined ? { nestedCallsComplete } : {}),
  };
}

/** Pi places details on the tool result; older host projections may lift it. */
export function codeModeDetailsFromPayload(payload: Record<string, unknown>): CodeModeDetails | undefined {
  const result = record(payload.result);
  const publicResult = record(payload.publicResult);
  const detailCandidates = [result.details, payload.details, publicResult.details, result, payload].map(parseCodeModeDetails);
  const genericCandidates = [result.nestedCalls, payload.nestedCalls, publicResult.nestedCalls].map(parseCodeModeDetails);
  const details = detailCandidates.find(value => value?.calls.length) ?? detailCandidates.find(value => value !== undefined);
  const generic = genericCandidates.find(value => value?.calls.length) ?? genericCandidates.find(value => value !== undefined);
  if (!generic) {
    return details && details.nestedCallsComplete === undefined && typeof payload.nestedCallsComplete === 'boolean'
      ? { ...details, nestedCallsComplete: payload.nestedCallsComplete } : details;
  }
  const detailById = new Map(details?.calls.map(call => [call.id, call]) ?? []);
  const calls = generic.calls.map(call => {
    const detail = detailById.get(call.id);
    if (!detail || detail.name !== call.name) return call;
    return { ...detail, ...call, args: call.args || detail.args,
      // Pi's generic receipt represents cancellation as error; its codemode
      // details retain the explicit cancellation outcome for the same child.
      status: detail.status === 'cancelled' && call.status === 'error' ? 'cancelled' as const : call.status };
  });
  const missing = details?.calls.filter(detail => !calls.some(call => call.id === detail.id && call.name === detail.name)) ?? [];
  const complete = missing.length || details?.nestedCallsComplete === false ? false
    : generic.nestedCallsComplete ?? (typeof payload.nestedCallsComplete === 'boolean' ? payload.nestedCallsComplete : details?.nestedCallsComplete);
  return { ...details, ...generic, calls: [...calls, ...missing],
    ...(complete !== undefined ? { nestedCallsComplete: complete } : {}) };
}

/** Read the final text emitted by Pi from the public result envelope. */
export function codeModeOutputFromPayload(payload: Record<string, unknown>): string {
  const result = record(payload.result);
  const publicResult = record(payload.publicResult);
  const candidates: unknown[] = [
    result.output,
    result.outputPreview,
    publicResult.output,
    publicResult.outputPreview,
    result.content,
    publicResult.content,
    payload.output,
    payload.outputPreview,
    payload.content,
  ];
  for (const candidate of candidates) {
    const value = contentText(candidate);
    if (value) return codeModeOutputText(value);
  }
  return '';
}

/** The exact script is carried as codemode's `{ code }` argument JSON. */
export function codeModeSourceFromInput(input: string | undefined): string {
  if (!input) return '';
  const parsed = safeJson(input);
  if (parsed) return text(parsed.code) || input;
  return input;
}

/** Pi's result content starts with a presentation header; keep only final output. */
export function codeModeOutputText(value: string | undefined): string {
  return (value ?? '').replace(CODEMODE_OUTPUT_HEADER, '').trim();
}

export function isCodeModeTool(block: Pick<ToolCallBlock, 'name'>): boolean {
  return block.name.trim().toLowerCase() === 'codemode';
}

function safeJson(value: string): Record<string, unknown> | undefined {
  try {
    const parsed: unknown = JSON.parse(value);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : undefined;
  } catch {
    return undefined;
  }
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function contentText(value: unknown): string {
  if (typeof value === 'string') return value;
  if (!Array.isArray(value)) return '';
  return value
    .map((item) => {
      if (typeof item === 'string') return item;
      const candidate = record(item);
      return text(candidate.text) || text(candidate.content);
    })
    .filter(Boolean)
    .join('\n');
}

function displayArgs(value: unknown): string {
  if (typeof value === 'string') return value;
  if (!value || typeof value !== 'object') return '';
  try {
    return JSON.stringify(value);
  } catch {
    return '';
  }
}

function nonNegativeNumber(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : undefined;
}
