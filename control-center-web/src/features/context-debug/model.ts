export type JsonRecord = Record<string, unknown>;

export interface DebugTurnSummary {
  turnId: string;
  clientMessageId: string;
  capturedAtMs: number;
  updatedAtMs: number;
  modelCallCount: number;
  providerRequestCount: number;
  toolCallCount: number;
  runningToolCount: number;
  turnOrdinal?: number;
  assemblyPhase?: 'initial' | 'incremental' | 'compaction_recovery';
  summary?: string;
}

export type DebugTurnPhase = 'initial' | 'incremental' | 'compaction_recovery' | 'retained_start';

export interface DebugTurnDescription {
  phase: DebugTurnPhase;
  ordinal?: number;
  label: string;
  description: string;
}

export interface DebugContextDelta {
  omitted?: boolean;
  baseCallIndex?: number;
  commonPrefixMessages: number;
  removedMessageCount: number;
  addedMessageCount: number;
  addedMessages: unknown[];
  prefixBytes?: number;
  prefixSha256?: string;
  currentBytes?: number;
  deltaBytes?: number;
  duplicateBytes?: number;
}

export interface DebugCacheEvidence {
  requestIndex: number;
  prefixSha256: string;
  prefixBytes: number;
  deltaBytes: number;
  duplicateBytes: number;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheWriteTokens: number;
  capability: 'reported' | 'unsupported';
}

export interface DebugProviderExchange {
  index: number;
  capturedAtMs: number;
  payload?: unknown;
  status?: number;
  headers?: JsonRecord;
}

export interface DebugModelCall {
  index: number;
  runtimeTurnIndex?: number;
  capturedAtMs: number;
  updatedAtMs: number;
  completedAtMs?: number;
  contextMessages: unknown[];
  contextMessagesOmission?: JsonRecord;
  providerContext: JsonRecord;
  contextDelta: DebugContextDelta;
  providerExchanges: DebugProviderExchange[];
  assistantMessage?: unknown;
}

export interface DebugToolExecution {
  toolCallId: string;
  toolName: string;
  modelCallIndex?: number;
  runtimeTurnIndex?: number;
  startedAtMs: number;
  endedAtMs?: number;
  startSequence: number;
  endSequence?: number;
  args: unknown;
  result?: unknown;
  isError?: boolean;
  status: 'running' | 'completed' | 'failed';
  updates: Array<{ capturedAtMs: number; partialResult: unknown }>;
}

export interface DebugToolBatch {
  id: string;
  modelCallIndex?: number;
  runtimeTurnIndex?: number;
  stage: number;
  executionMode: 'parallel' | 'serial';
  startedAtMs: number;
  endedAtMs?: number;
  status: 'running' | 'completed' | 'failed';
  toolCallIds: string[];
}

export interface DebugContextRecord {
  sessionId: string;
  turnId: string;
  clientMessageId: string;
  capturedAtMs: number;
  updatedAtMs: number;
  prompt: string;
  systemPrompt: string;
  systemPromptOptions: unknown;
  model: JsonRecord;
  activeTools: string[];
  toolSchemas: JsonRecord[];
  cacheEvidence: DebugCacheEvidence[];
  modelCalls: DebugModelCall[];
  toolExecutions: DebugToolExecution[];
  toolBatches: DebugToolBatch[];
  raw: JsonRecord;
}

export interface DebugContextResponse {
  available: boolean;
  transient: boolean;
  sessionId: string;
  turnId: string;
  error: string;
  availableTurns: DebugTurnSummary[];
  context?: DebugContextRecord;
  telemetry: JsonRecord;
}

export function describeDebugTurn(
  context: DebugContextRecord,
  availableTurns: DebugTurnSummary[],
): DebugTurnDescription {
  const summary = availableTurns.find((turn) => turn.turnId === context.turnId);
  const rawOrdinal = number(context.raw.turnOrdinal);
  const ordinal = summary?.turnOrdinal ?? (rawOrdinal > 0 ? rawOrdinal : undefined);
  const rawPhase = text(context.raw.assemblyPhase);
  const explicitPhase = summary?.assemblyPhase
    ?? (rawPhase === 'initial' || rawPhase === 'incremental' || rawPhase === 'compaction_recovery'
      ? rawPhase
      : undefined);

  if (explicitPhase === 'initial' || ordinal === 1) {
    return {
      phase: 'initial',
      ordinal,
      label: '首轮装配',
      description: '从系统指令、项目约束、可用能力与第一条用户输入开始建立上下文。',
    };
  }
  if (explicitPhase === 'compaction_recovery' || contextHasCompactionRecovery(context)) {
    return {
      phase: 'compaction_recovery',
      ordinal,
      label: '压缩后恢复',
      description: '旧消息已被低分辨率恢复材料替代；下方展示恢复材料如何与最近对话重新装配。',
    };
  }
  const oldestRetainedTurn = [...availableTurns]
    .sort((left, right) => left.capturedAtMs - right.capturedAtMs)[0];
  if (oldestRetainedTurn?.turnId === context.turnId && ordinal === undefined) {
    return {
      phase: 'retained_start',
      label: '最早保留轮次',
      description: '这是当前本机保留窗口中的第一轮；更早历史可能已经轮换，不能据此认定为项目首轮。',
    };
  }
  return {
    phase: 'incremental',
    ordinal,
    label: '增量装配',
    description: '沿用稳定前缀，只把本轮输入、运行时上下文与工具结果追加到对话尾部。',
  };
}

export function normalizeDebugContextResponse(value: unknown): DebugContextResponse {
  const response = record(value);
  const rawContext = record(response.context);
  return {
    available: response.available === true && Object.keys(rawContext).length > 0,
    transient: response.transient !== false,
    sessionId: text(response.sessionId),
    turnId: text(response.turnId) || text(rawContext.turnId),
    error: text(response.error) || text(response.reason),
    availableTurns: array(response.availableTurns).map(normalizeTurnSummary).filter(Boolean) as DebugTurnSummary[],
    context: Object.keys(rawContext).length ? normalizeContextRecord(rawContext) : undefined,
    telemetry: record(response.telemetry),
  };
}

function normalizeContextRecord(value: JsonRecord): DebugContextRecord {
  const contextWindows = array(value.contextWindows).map(record);
  const providerRequests = array(value.providerRequests).map(record);
  const modelCalls = array(value.modelCalls).length
    ? array(value.modelCalls).map((item, index) => normalizeModelCall(record(item), index))
    : contextWindows.map((window, index) => normalizeLegacyModelCall(window, providerRequests[index], contextWindows[index - 1], index));
  const toolExecutions = array(value.toolExecutions).map(normalizeToolExecution).filter(Boolean) as DebugToolExecution[];
  const suppliedBatches = array(value.toolBatches).map(normalizeToolBatch).filter(Boolean) as DebugToolBatch[];
  return {
    sessionId: text(value.sessionId),
    turnId: text(value.turnId),
    clientMessageId: text(value.clientMessageId),
    capturedAtMs: number(value.capturedAtMs),
    updatedAtMs: number(value.updatedAtMs),
    prompt: text(value.prompt),
    systemPrompt: text(value.systemPrompt),
    systemPromptOptions: value.systemPromptOptions,
    model: record(value.model),
    activeTools: array(value.activeTools).map(text).filter(Boolean),
    toolSchemas: array(value.toolSchemas).map(record),
    cacheEvidence: array(value.cacheEvidence).map(normalizeCacheEvidence),
    modelCalls,
    toolExecutions,
    toolBatches: suppliedBatches.length ? suppliedBatches : deriveToolBatches(toolExecutions),
    raw: value,
  };
}

function normalizeModelCall(value: JsonRecord, fallbackIndex: number): DebugModelCall {
  const delta = record(value.contextDelta);
  return {
    index: positive(value.index, fallbackIndex + 1),
    runtimeTurnIndex: optionalNumber(value.runtimeTurnIndex),
    capturedAtMs: number(value.capturedAtMs),
    updatedAtMs: number(value.updatedAtMs) || number(value.capturedAtMs),
    completedAtMs: optionalNumber(value.completedAtMs),
    contextMessages: array(value.contextMessages),
    ...(record(value.contextMessages).omitted === true
      ? { contextMessagesOmission: record(value.contextMessages) }
      : {}),
    providerContext: record(value.providerContext),
    contextDelta: {
      ...(delta.omitted === true ? { omitted: true } : {}),
      baseCallIndex: optionalNumber(delta.baseCallIndex),
      commonPrefixMessages: number(delta.commonPrefixMessages),
      removedMessageCount: number(delta.removedMessageCount),
      addedMessageCount: number(delta.addedMessageCount),
      addedMessages: array(delta.addedMessages),
      prefixBytes: optionalNumber(delta.prefixBytes),
      prefixSha256: text(delta.prefixSha256) || undefined,
      currentBytes: optionalNumber(delta.currentBytes),
      deltaBytes: optionalNumber(delta.deltaBytes),
      duplicateBytes: optionalNumber(delta.duplicateBytes),
    },
    providerExchanges: array(value.providerExchanges).map((item, index) => normalizeProviderExchange(record(item), index)),
    assistantMessage: value.assistantMessage,
  };
}

function normalizeCacheEvidence(value: unknown): DebugCacheEvidence {
  const evidence = record(value);
  return {
    requestIndex: positive(evidence.requestIndex, 1),
    prefixSha256: text(evidence.prefixSha256),
    prefixBytes: number(evidence.prefixBytes),
    deltaBytes: number(evidence.deltaBytes),
    duplicateBytes: number(evidence.duplicateBytes),
    inputTokens: number(evidence.inputTokens),
    outputTokens: number(evidence.outputTokens),
    cacheReadTokens: number(evidence.cacheReadTokens),
    cacheWriteTokens: number(evidence.cacheWriteTokens),
    capability: evidence.capability === 'reported' ? 'reported' : 'unsupported',
  };
}

function normalizeLegacyModelCall(
  window: JsonRecord,
  request: JsonRecord | undefined,
  previous: JsonRecord | undefined,
  fallbackIndex: number,
): DebugModelCall {
  const messages = array(window.messages);
  const previousMessages = array(previous?.messages);
  const commonPrefix = commonMessagePrefix(previousMessages, messages);
  const index = positive(window.index, fallbackIndex + 1);
  return {
    index,
    capturedAtMs: number(window.capturedAtMs),
    updatedAtMs: number(request?.capturedAtMs) || number(window.capturedAtMs),
    contextMessages: messages,
    providerContext: {},
    contextDelta: {
      baseCallIndex: fallbackIndex > 0 ? fallbackIndex : undefined,
      commonPrefixMessages: commonPrefix,
      removedMessageCount: previousMessages.length - commonPrefix,
      addedMessageCount: messages.length - commonPrefix,
      addedMessages: messages.slice(commonPrefix),
    },
    providerExchanges: request ? [normalizeProviderExchange(request, fallbackIndex)] : [],
  };
}

function normalizeProviderExchange(value: JsonRecord, fallbackIndex: number): DebugProviderExchange {
  return {
    index: positive(value.index, fallbackIndex + 1),
    capturedAtMs: number(value.capturedAtMs),
    payload: value.payload,
    status: optionalNumber(value.status),
    headers: Object.keys(record(value.headers)).length ? record(value.headers) : undefined,
  };
}

function normalizeToolExecution(value: unknown): DebugToolExecution | undefined {
  const tool = record(value);
  const toolCallId = text(tool.toolCallId);
  const toolName = text(tool.toolName);
  if (!toolCallId || !toolName) return undefined;
  const rawStatus = text(tool.status);
  const status = rawStatus === 'running' || rawStatus === 'failed' ? rawStatus : 'completed';
  return {
    toolCallId,
    toolName,
    modelCallIndex: optionalNumber(tool.modelCallIndex),
    runtimeTurnIndex: optionalNumber(tool.runtimeTurnIndex),
    startedAtMs: number(tool.startedAtMs),
    endedAtMs: optionalNumber(tool.endedAtMs),
    startSequence: positive(tool.startSequence, 1),
    endSequence: optionalNumber(tool.endSequence),
    args: tool.args,
    result: tool.result,
    isError: typeof tool.isError === 'boolean' ? tool.isError : undefined,
    status,
    updates: array(tool.updates).map((item) => ({
      capturedAtMs: number(record(item).capturedAtMs),
      partialResult: record(item).partialResult,
    })),
  };
}

function normalizeToolBatch(value: unknown): DebugToolBatch | undefined {
  const batch = record(value);
  const id = text(batch.id);
  const toolCallIds = array(batch.toolCallIds).map(text).filter(Boolean);
  if (!id || !toolCallIds.length) return undefined;
  const mode = text(batch.executionMode) === 'parallel' ? 'parallel' : 'serial';
  const rawStatus = text(batch.status);
  return {
    id,
    modelCallIndex: optionalNumber(batch.modelCallIndex),
    runtimeTurnIndex: optionalNumber(batch.runtimeTurnIndex),
    stage: positive(batch.stage, 1),
    executionMode: mode,
    startedAtMs: number(batch.startedAtMs),
    endedAtMs: optionalNumber(batch.endedAtMs),
    status: rawStatus === 'running' || rawStatus === 'failed' ? rawStatus : 'completed',
    toolCallIds,
  };
}

export function deriveToolBatches(tools: DebugToolExecution[]): DebugToolBatch[] {
  const grouped = new Map<string, DebugToolExecution[]>();
  tools.forEach((tool) => {
    const key = `${tool.modelCallIndex ?? 'unknown'}:${tool.runtimeTurnIndex ?? 'unknown'}`;
    grouped.set(key, [...(grouped.get(key) ?? []), tool]);
  });
  const batches: DebugToolBatch[] = [];
  grouped.forEach((values) => {
    const ordered = [...values].sort((left, right) => left.startSequence - right.startSequence);
    const stages: DebugToolExecution[][] = [];
    let stageEnd = -1;
    ordered.forEach((tool) => {
      const end = tool.endSequence ?? Number.POSITIVE_INFINITY;
      if (!stages.length || tool.startSequence >= stageEnd) {
        stages.push([tool]);
        stageEnd = end;
      } else {
        stages.at(-1)?.push(tool);
        stageEnd = Math.max(stageEnd, end);
      }
    });
    stages.forEach((stageTools, index) => {
      const running = stageTools.some((tool) => tool.status === 'running');
      const failed = stageTools.some((tool) => tool.status === 'failed');
      const endTimes = stageTools.flatMap((tool) => tool.endedAtMs === undefined ? [] : [tool.endedAtMs]);
      batches.push({
        id: `call-${stageTools[0]?.modelCallIndex ?? 'unknown'}-turn-${stageTools[0]?.runtimeTurnIndex ?? 'unknown'}-stage-${index + 1}`,
        modelCallIndex: stageTools[0]?.modelCallIndex,
        runtimeTurnIndex: stageTools[0]?.runtimeTurnIndex,
        stage: index + 1,
        executionMode: stageTools.length > 1 ? 'parallel' : 'serial',
        startedAtMs: Math.min(...stageTools.map((tool) => tool.startedAtMs)),
        endedAtMs: running || !endTimes.length ? undefined : Math.max(...endTimes),
        status: running ? 'running' : failed ? 'failed' : 'completed',
        toolCallIds: stageTools.map((tool) => tool.toolCallId),
      });
    });
  });
  return batches.sort((left, right) => left.startedAtMs - right.startedAtMs || left.stage - right.stage);
}

function normalizeTurnSummary(value: unknown): DebugTurnSummary | undefined {
  const item = record(value);
  const turnId = text(item.turnId);
  if (!turnId) return undefined;
  const rawPhase = text(item.assemblyPhase);
  return {
    turnId,
    clientMessageId: text(item.clientMessageId),
    capturedAtMs: number(item.capturedAtMs),
    updatedAtMs: number(item.updatedAtMs),
    modelCallCount: number(item.modelCallCount),
    providerRequestCount: number(item.providerRequestCount),
    toolCallCount: number(item.toolCallCount),
    runningToolCount: number(item.runningToolCount),
    turnOrdinal: optionalNumber(item.turnOrdinal),
    assemblyPhase: rawPhase === 'initial' || rawPhase === 'incremental' || rawPhase === 'compaction_recovery'
      ? rawPhase
      : undefined,
    summary: text(item.summary) || undefined,
  };
}

function contextHasCompactionRecovery(context: DebugContextRecord): boolean {
  const candidates = context.modelCalls.flatMap((call) => [
    ...call.contextMessages,
    ...call.contextDelta.addedMessages,
  ]);
  return candidates.some((candidate) => {
    const item = record(candidate);
    const customType = text(item.customType).toLowerCase();
    if (customType.includes('compaction-recovery')) return true;
    const preview = messagePreview(candidate).toLowerCase();
    return preview.includes('<compaction-recovery>') || preview.includes('<room-compaction-recovery>');
  });
}

function commonMessagePrefix(left: unknown[], right: unknown[]): number {
  let index = 0;
  while (index < left.length && index < right.length && formatJson(left[index]) === formatJson(right[index])) index += 1;
  return index;
}

export function messagePreview(value: unknown): string {
  const parts: string[] = [];
  collectText(value, parts);
  return parts.join(' ').replace(/\s+/g, ' ').trim().slice(0, 240);
}

function collectText(value: unknown, output: string[]): void {
  if (typeof value === 'string') {
    output.push(value);
    return;
  }
  if (Array.isArray(value)) {
    value.forEach((item) => collectText(item, output));
    return;
  }
  const item = record(value);
  for (const key of ['text', 'content', 'name', 'toolName']) {
    if (key in item) collectText(item[key], output);
  }
}

export function formatJson(value: unknown): string {
  try {
    return JSON.stringify(value ?? null, null, 2);
  } catch {
    return String(value ?? '');
  }
}

export function record(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {};
}

export function array(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

export function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

export function number(value: unknown): number {
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function optionalNumber(value: unknown): number | undefined {
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function positive(value: unknown, fallback: number): number {
  const parsed = number(value);
  return parsed > 0 ? parsed : fallback;
}
