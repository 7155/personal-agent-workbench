import { publicErrorText } from '@/features/overview/management-ui';

const unavailableModelPattern = /(?:model\s+["']?[^"']+["']?\s+is\s+not\s+supported|unsupported\s+model|model_not_supported|模型.*(?:不支持|不可用))/i;
const modelAuthFailurePattern = /(?:invalidated oauth token|invalid[_ -]grant|refresh[_ -]token[_ -](?:reused|expired|invalid)|(?:access|oauth)[_ -]token (?:is |has )?(?:expired|invalid)|authentication failed|invalid[_ -]?(?:api[_ -]?)?key|模型账号登录已失效)/i;
const providerRequestFailurePattern = /(?:error\s+from\s+provider|upstream\s+request\s+failed|provider[_\s-](?:request|response|error)|模型服务.*(?:失败|异常))/i;
const networkInterruptionPattern = /(?:网络中断|fetch failed|websocket\s+(?:error|failure|closed)|network\s+(?:error|failure)|connection\s+(?:reset|closed|refused)|econn(?:reset|refused)|socket hang up|broken pipe|remote end closed)/i;
const nativeRouteMismatchPattern = /(?:route[_\s-]policy[_\s-]rejected|unexpected\s+(?:request\s+)?body\s+field|body\s+field\s+is\s+not\s+allowlisted|unknown\s+pathid)/i;
const memoryBootstrapBudgetPattern = /(?:memory\s+(?:bootstrap|curation\s+packet).*(?:budget|context\s+window|max_chars|token\s+limit|input\s+limit|input\s+too\s+large|too\s+long|exceed)|记忆(?:召回|上下文|整理).*(?:超限|超上限|预算|上下文过大|输入过大))/i;
const modelQuotaPattern = /(?:usage\s+limit\s+has\s+been\s+reached|quota(?:\s+|[_-])(?:exceeded|exhausted|depleted)|rate\s*limit(?:ed)?|too\s+many\s+requests|请求过于频繁|额度(?:已用尽|不足|超限)|配额(?:已用尽|不足|超限)|\b429\b)/i;
const memoryBootstrapFailureCodePattern = /^memory_bootstrap_(?:budget_exceeded|failed)$/u;
const legacyRoomParticipantBusyPattern = /^(?:.+ is currently busy|Room participants are currently busy: .+)$/u;
export const ROOM_PARTICIPANT_BUSY_TEXT = '目标伙伴正在处理另一条请求；草稿已保留，待当前请求结束后可再次发送。';
export const MEMORY_BOOTSTRAP_SKIPPED_TEXT = '记忆召回本轮已跳过，消息仍可继续；下次会重新尝试。';
export const MODEL_QUOTA_EXHAUSTED_TEXT = '模型服务额度暂时用尽，请稍后重试或切换已配置模型。';
export const MODEL_AUTH_FAILURE_TEXT = '模型账号登录已失效或凭据无效。请在系统设置的“模型账号”中重新登录或更新密钥，再继续当前对话。';
export const TEAM_WORKER_UNAVAILABLE_TEXT = '团队尚未配置执行服务，请联系管理员配置模型与隔离运行环境。';
export const SESSION_WORKSPACE_MISSING_TEXT = (
  '这个 Session 的工作目录已不存在。请选择新的工作目录后继续，或返回桌面新建工作。'
);
export const ROOM_WORKSPACE_MISSING_TEXT = (
  '这个 Room 的工作目录已不存在。请选择新的工作目录后继续，或返回桌面新建协作。'
);
export type AgentCommandReceiptState =
  | 'pending'
  | 'accepted'
  | 'failed'
  | 'conflict';

export type AgentCommandReceiptRecoveryState =
  | 'in_flight'
  | 'unresolved'
  | 'new_command_required';

export interface AgentCommandReceiptFailure {
  code:
    | 'AGENT_COMMAND_PENDING'
    | 'AGENT_COMMAND_FAILED'
    | 'AGENT_COMMAND_CONFLICT';
  state: AgentCommandReceiptState;
  clientMessageId: string;
  causeCode: string;
  recoveryState?: AgentCommandReceiptRecoveryState;
}

export function agentCommandReceiptFailure(
  value: unknown,
): AgentCommandReceiptFailure | undefined {
  const payload = errorPayload(value);
  if (!payload) return undefined;
  const code = payload.code;
  if (
    code !== 'AGENT_COMMAND_PENDING'
    && code !== 'AGENT_COMMAND_FAILED'
    && code !== 'AGENT_COMMAND_CONFLICT'
  ) {
    return undefined;
  }
  const receipt = record(payload.commandReceipt);
  if (!receipt) return undefined;
  const state = receipt?.state;
  if (
    state !== 'pending'
    && state !== 'accepted'
    && state !== 'failed'
    && state !== 'conflict'
  ) {
    return undefined;
  }
  const rawRecoveryState = receipt.recoveryState;
  const recoveryState = (
    rawRecoveryState === 'in_flight'
    || rawRecoveryState === 'unresolved'
    || rawRecoveryState === 'new_command_required'
  )
    ? rawRecoveryState
    : undefined;
  return {
    code,
    state,
    clientMessageId: stringValue(receipt.clientMessageId),
    causeCode: stringValue(receipt.causeCode),
    ...(recoveryState ? { recoveryState } : {}),
  };
}

export function isAgentCommandPending(value: unknown): boolean {
  return (
    agentCommandReceiptFailure(value)?.code
    === 'AGENT_COMMAND_PENDING'
  );
}

export function isUnresolvedAgentCommandPending(
  value: unknown,
): boolean {
  const failure = agentCommandReceiptFailure(value);
  return (
    failure?.code === 'AGENT_COMMAND_PENDING'
    && failure.recoveryState === 'unresolved'
  );
}

export function isAmbiguousAgentPromptFailure(value: unknown): boolean {
  if (agentCommandReceiptFailure(value)) return false;
  const responseStatus = transportResponseStatus(value);
  if (responseStatus !== undefined) {
    return responseStatus >= 500;
  }
  if (value instanceof DOMException) {
    return (
      value.name === 'AbortError'
      || value.name === 'NetworkError'
      || value.name === 'TimeoutError'
    );
  }
  // fetch rejects network failures as TypeError. Route validation has already
  // succeeded before the transport reaches fetch, so this is an unknown
  // delivery outcome rather than a model rejection.
  return value instanceof TypeError;
}

export function isAgentTurnConflict(value: unknown): boolean {
  return (
    agentCommandReceiptFailure(value)?.causeCode
    === 'AGENT_TURN_CONFLICT'
  );
}

export function isAgentSessionIdleFailure(value: unknown): boolean {
  return (
    agentCommandReceiptFailure(value)?.causeCode
    === 'SESSION_IDLE'
  );
}

export function publicAgentErrorText(
  value: unknown,
  fallback = '本轮没有完成，请重试或切换模型。',
): string {
  const payload = errorPayload(value);
  const errorCode = stringValue(payload?.errorCode);
  const message = (value instanceof Error ? value.message : String(value ?? '')).trim();
  // TeamGateway rejects execution before persistence with this explicit
  // status/code pair. It is a definitive admission failure, so expose the
  // recovery path instead of the generic ambiguous-delivery copy.
  const responseStatus = transportResponseStatus(value);
  if (
    errorCode === 'team_worker_unavailable'
    && (responseStatus === undefined || responseStatus === 409)
  ) return TEAM_WORKER_UNAVAILABLE_TEXT;
  if (isModelAuthError(message)) return MODEL_AUTH_FAILURE_TEXT;
  if (/APP_API_(?:BASE_URL|KEY)/u.test(message)) {
    return '模型连接配置不完整。请连接 PAW 或检查模型服务设置后重试。';
  }
  if (memoryBootstrapFailureCodePattern.test(errorCode) || memoryBootstrapBudgetPattern.test(message)) {
    return MEMORY_BOOTSTRAP_SKIPPED_TEXT;
  }
  if (/(?:servers?.*overloaded|model.*at capacity|overloaded_error)/i.test(message)) {
    return '模型服务当前繁忙。自动重试结束后可稍后继续，或停止本轮后切换模型。';
  }
  if (isAgentTurnConflict(value)) {
    return '当前对话仍在处理或自动重试，输入已保留。请等待本轮结束，或先停止本轮。';
  }
  if (modelQuotaPattern.test(message)) {
    return MODEL_QUOTA_EXHAUSTED_TEXT;
  }
  const receiptFailure = agentCommandReceiptFailure(value);
  if (
    receiptFailure?.causeCode === 'ROOM_PARTICIPANT_BUSY'
    || legacyRoomParticipantBusyPattern.test(message)
  ) {
    return ROOM_PARTICIPANT_BUSY_TEXT;
  }
  if (receiptFailure?.code === 'AGENT_COMMAND_PENDING') {
    if (receiptFailure.recoveryState === 'unresolved') {
      return (
        '无法确认这条消息是否已执行。为避免重复执行，系统不会自动重试；'
        + '请刷新对话检查结果后，再决定是否发送新的请求。'
      );
    }
    return (
      '服务端仍在确认这条消息是否已接收。系统不会自动重试；'
      + '若后续收到确认事件，会在原消息上更新。'
    );
  }
  if (receiptFailure?.code === 'AGENT_COMMAND_CONFLICT') {
    if (receiptFailure.recoveryState === 'new_command_required') {
      return '这次发送内容已经变化，输入已保留；请直接重新发送一次。';
    }
    return '这条消息没有发送；输入已保留，请直接重新发送一次。';
  }
  if (isAgentWorkspaceMissingError(value)) {
    return SESSION_WORKSPACE_MISSING_TEXT;
  }
  if (unavailableModelPattern.test(message)) {
    return '当前模型不可用，请切换模型后重试。';
  }
  if (isAgentNetworkInterruption(message)) {
    return '网络中断，模型未能生成最终回复。已完成的结果已保留，请继续或切换模型。';
  }
  if (providerRequestFailurePattern.test(message)) {
    return '模型服务请求失败，请重试或切换模型。';
  }
  if (nativeRouteMismatchPattern.test(message)) {
    return '控制中心组件版本不一致，请更新并重新打开控制中心。';
  }
  return publicErrorText(value, fallback);
}

export function isAgentWorkspaceMissingError(value: unknown): boolean {
  return errorPayload(value)?.errorCode === 'session_workspace_missing';
}

export function isAgentNetworkInterruption(value: unknown): boolean {
  const message = (value instanceof Error ? value.message : String(value ?? '')).trim();
  return networkInterruptionPattern.test(message);
}

export function isModelAuthError(value: unknown): boolean {
  return modelAuthFailurePattern.test(value instanceof Error ? value.message : String(value ?? ''));
}

/** A provider quota failure is distinct from a local memory-size failure. */
export function isModelQuotaError(value: unknown): boolean {
  const payload = errorPayload(value);
  const code = stringValue(payload?.errorCode);
  const message = (value instanceof Error ? value.message : String(value ?? '')).trim();
  return modelQuotaPattern.test(code) || modelQuotaPattern.test(message);
}

function errorPayload(value: unknown): Record<string, unknown> | undefined {
  const outer = record(value);
  return record(outer?.payload) ?? outer;
}

function transportResponseStatus(value: unknown): number | undefined {
  if (!(value instanceof Error)) return undefined;
  const status = record(value)?.status;
  return typeof status === 'number' && Number.isInteger(status)
    ? status
    : undefined;
}

function record(value: unknown): Record<string, unknown> | undefined {
  return (
    typeof value === 'object'
    && value !== null
    && !Array.isArray(value)
  )
    ? value as Record<string, unknown>
    : undefined;
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : '';
}
