import { agentChildProgress, type AgentActivityProjection, type AgentChildProgress } from '@/contracts/agent-reducer';
import { approvalDecisionReasonLabel, approvalDecisionView, approvalNeedsHumanDecision } from '@/contracts/approval-decision';
import { publicToolName } from '../tool-presentation';
import { subagentTemplateLabel } from '../status/subagent-presentation';
import {
  codeModeDetailsFromPayload,
  codeModeOutputFromPayload,
  codeModeSourceFromInput,
  type CodeModeDetails,
} from '@/features/conversation-ui/model/codemode';
const PUBLIC_TOOL_OUTPUT_MAX_CHARS = 6_000;
const PUBLIC_TOOL_OUTPUT_MAX_LINES = 40;
const INSPECTABLE_TOOL_RESULT_MAX_CHARS = 24_000;
const INSPECTABLE_TOOL_RESULT_TRUNCATED = '\n[结果已截断；完整内容请通过受控工具结果引用分页读取]';

export interface PublicToolActivityProjection {
  kind: string;
  status: AgentActivityProjection['status'] | 'aborted';
  payload: Record<string, unknown>;
  settledByTurnStatus?: AgentActivityProjection['settledByTurnStatus'];
}

export interface PublicToolResultField {
  id: string;
  label: string;
  value: string;
}

export interface PublicToolRequestField extends PublicToolResultField {
  code?: boolean;
}

export interface PublicToolResultView {
  toolId: string;
  toolLabel: string;
  operation: string;
  summary: string;
  resultKind: PublicToolResultKind;
  target?: string;
  /** A non-redacted workspace-relative target that can safely be opened in Files. */
  targetPath?: string;
  change?: {
    additions: number;
    deletions: number;
  };
  fields: PublicToolResultField[];
  request: PublicToolRequestField[];
  output?: {
    text: string;
    truncated: boolean;
  };
  /** Native Pi codemode receipt, kept inside the historical outer Tool row. */
  codeMode?: CodeModeDetails;
  /** The submitted JavaScript source, when the codemode argument is public. */
  code?: string;
  /** Header label when the output block is not a tool return, e.g. the
   * concrete content a collaboration tool sent on the user's behalf. */
  outputLabel?: string;
  resultItems: PublicToolResultItem[];
  /** Header label for structured result items, e.g. delegation runs. */
  resultItemsLabel?: string;
  rawResult?: {
    format: 'json' | 'text';
    value: unknown;
  };
  language?: string;
  sources: string[];
  sourceLinks?: Array<{ label: string; href: string }>;
  preview?: PublicToolSemanticPreview;
  error?: string;
  recovery?: 'approval' | 'permission';
  destination?: {
    href: string;
    label: string;
  };
}

export type PublicToolResultKind =
  | 'terminal'
  | 'codemode'
  | 'code'
  | 'matches'
  | 'files'
  | 'change'
  | 'browser'
  | 'semantic'
  | 'structured';

export interface PublicToolResultItem {
  id: string;
  label: string;
  /** Bounded task title for hover when the visible label is shortened. */
  title?: string;
  text: string;
  kind?: string;
  /** A non-redacted workspace-relative path, when the receipt carries one. */
  path?: string;
}

export interface PublicToolSemanticPreview {
  kind: 'atom' | 'book' | 'collection' | 'evidence' | 'timeline' | 'role_book';
  title: string;
  description?: string;
  badges: string[];
  items: Array<{
    id: string;
    label?: string;
    text: string;
    href?: string;
  }>;
}

export function publicToolLabel(toolId: string): string {
  return publicToolName(toolId);
}

const toolDestinations: Record<string, { href: string; label: string }> = {
  overview: { href: '#/overview', label: '打开当前状态' },
  input: { href: '#/input', label: '打开输入法与词库' },
  voice: { href: '#/voice', label: '打开语音输入' },
  planning: { href: '#/planning', label: '打开任务' },
  memory: { href: '#/memory', label: '打开我的记忆' },
  agent_role_book: { href: '#/memory?layer=role-books', label: '打开伙伴记忆' },
  knowledge: { href: '#/knowledge', label: '打开知识库' },
  models: { href: '#/configuration', label: '打开模型与连接' },
  runtime: { href: '#/diagnostics', label: '打开运行检查' },
  configuration: { href: '#/configuration', label: '打开设置' },
  // `agents` delegates private subagents inside this Session; it must not
  // deep-link to Rooms, which is a different collaboration surface.
  room_partner: { href: '#/rooms', label: '打开多人协作' },
};

const operationLabels: Record<string, string> = {
  status: '检查状态',
  capabilities: '查看可用能力',
  recent_activity: '查看近期活动',
  get_settings: '读取设置',
  preview_settings: '预览设置变更',
  apply_settings: '应用设置变更',
  rollback_settings: '撤销设置变更',
  profile: '查看当前方案',
  candidate_explain: '解释候选结果',
  dashboard: '查看任务面板',
  catalog: '查看目录',
  read: '读取内容',
  recent: '查看近期记录',
  search: '搜索',
  get: '读取详情',
  explain: '追溯事实来源',
  review: '审阅草案',
  remember_preview: '预览新增记忆',
  correct_preview: '预览事实更正',
  forget_preview: '预览遗忘',
  remember_apply: '应用新增记忆',
  correct_apply: '应用事实更正',
  forget_apply: '应用遗忘',
  governance_rollback: '回滚记忆变更',
  propose_revision: '提出角色书修订',
  list_bases: '查看知识库',
  find: '定位文档证据',
  open: '读取引用窗口',
  recall: '检索知识',
  deep_recall: '深度检索',
  route_status: '检查检索路由',
  profiles: '查看模型方案',
  probe: '检查模型连接',
  cache_stats: '查看缓存状态',
  health: '检查运行状态',
  components: '检查运行组件',
  diagnose: '运行诊断',
  history: '查看历史摘要',
  audit: '查看审计记录',
  delegate: '委派协作任务',
  artifact: '查看协作产物',
  list: '浏览工作区',
  run: '运行受控命令',
  symbols: '查找符号',
  hover: '查看悬浮信息',
  definition: '定位定义',
  references: '查找引用',
  diagnostics: '查看诊断',
  rename: '预览重命名',
  code_action_apply: '预览代码动作',
};

const componentLabels: Record<string, string> = {
  inputMethod: '输入法',
  sidecar: '控制服务',
  predictor: '预测服务',
  foregroundContext: '前台上下文',
  hybridRag: '混合检索',
  memoryCompiler: '记忆整理',
  sqlite: '本地数据库',
  voiceAgent: '语音代理',
  voiceMicrophone: '麦克风权限',
  voiceAccessibility: '辅助功能权限',
  voiceRecognition: '语音定稿',
};

const countFields: Array<[string, string]> = [
  ['count', '结果数量'],
  ['resultCount', '结果数量'],
  ['itemCount', '记录数量'],
  ['entryCount', '条目数量'],
  ['changeCount', '变更数量'],
  ['taskCount', '任务数量'],
  ['runCount', '运行数量'],
  ['providerCount', '服务数量'],
  ['profileCount', '方案数量'],
  ['completed', '已完成'],
  ['artifacts', '产物数量'],
];

const booleanFields: Array<[string, string, string, string]> = [
  ['enabled', '功能状态', '已启用', '未启用'],
  ['connected', '连接状态', '已连接', '未连接'],
  ['ready', '就绪状态', '已就绪', '未就绪'],
  ['healthy', '健康状态', '正常', '需要检查'],
  ['available', '可用状态', '可用', '不可用'],
  ['readOnly', '能力范围', '只读', '包含受确认保护的操作'],
  ['reviewRequired', '审阅要求', '需要审阅', '无需额外审阅'],
  ['approvalRequiredForApply', '应用保护', '需要本机确认', '无需本机确认'],
];

const memoryCountFields: Array<[string, string]> = [
  ['eventCount', '输入记录'],
  ['memoryItemCount', '记忆项目'],
  ['memoryBookCount', '主题书'],
  ['memoryAtomCount', '当前事实'],
  ['timelineCount', '活动时间线'],
  ['roleBookCount', '角色书'],
  ['retrievalDocCount', '可检索文档'],
  ['pendingCompileEvents', '待整理记录'],
];

export function publicToolResultView(activity: PublicToolActivityProjection): PublicToolResultView {
  if (activity.settledByTurnStatus === 'aborted') activity = { ...activity, status: 'aborted' };
  const payload = activity.payload;
  const carrier = record(payload.result ?? payload.partialResult);
  const carrierDetails = record(carrier.details);
  const envelope = Object.keys(carrierDetails).length > 0 ? carrierDetails : carrier;
  const envelopeResult = record(envelope.result);
  const carrierResult = record(carrier.result);
  const domain = Object.keys(envelopeResult).length > 0 ? envelopeResult : carrierResult;
  const publicResult = record(payload.publicResult);
  const layers = [domain, envelope, carrier, publicResult, payload];
  const toolId = firstText(
    [payload, envelope, carrier],
    ['toolId', 'toolName', 'tool'],
  ).toLowerCase();
  const toolLabel = publicToolLabel(toolId);
  const expectedNoop = payload.expectedNoop === true;
  const args = record(payload.args);
  const codeMode = toolId === 'codemode' ? codeModeDetailsFromPayload(payload) : undefined;
  const codeModeInput = toolId === 'codemode'
    ? (typeof payload.args === 'string'
      ? payload.args
      : typeof payload.arguments === 'string'
        ? payload.arguments
        : JSON.stringify(args))
    : '';
  const code = toolId === 'codemode' ? codeModeSourceFromInput(codeModeInput) : '';
  const codeOutput = toolId === 'codemode' ? codeModeOutputFromPayload(payload) : '';
  // Collaboration receipts keep their op inside the call arguments; lifting it
  // here lets the generic 操作 field carry the precise public label instead of
  // settling on "受控操作".
  const operation = firstText([envelope, carrier, payload], ['operation'])
    || (collaborationToolIds.has(toolId) ? text(args.op) : '');
  const collaboration = collaborationToolResult(toolId, operation, args, layers);
  const fields: PublicToolResultField[] = [];
  const seen = new Set<string>();
  const append = (id: string, label: string, value: string) => {
    if (!value || seen.has(id)) return;
    seen.add(id);
    fields.push({ id, label, value });
  };

  append('status', '状态', activityStatusLabel(activity.status));

  if (operation) {
    const operationLabel = toolId === 'todo'
      ? ({ init: '建立 Todo', start: '开始任务', done: '完成任务', drop: '放弃任务', append: '追加任务', view: '查看 Todo', rm: '移除 Todo' } as Record<string, string>)[operation]
      : collaboration?.operationLabel ?? operationLabels[operation];
    append('operation', '操作', operationLabel ?? '受控操作');
  }

  const ok = firstBoolean([envelope, domain, carrier], ['ok']);
  if (!expectedNoop && ok !== undefined) append('ok', '执行结果', ok ? '成功' : '未成功');

  // Collaboration receipts carry their own precise state labels; the generic
  // service-status projection would mislabel a document/goal state as a
  // service condition.
  const resultStatus = collaboration
    ? ''
    : publicStatusLabel(firstText(layers, ['status', 'state', 'availability']));
  if (resultStatus) append('resultStatus', '服务状态', resultStatus);

  const summary = firstPublicText(layers, ['summary', 'message', 'label']);
  if (summary) append('summary', activity.kind === 'tool_progress' ? '当前进度' : '结果摘要', summary);
  const approvalDecision = approvalDecisionView(payload);
  if (approvalDecision.mode) {
    append(
      'approvalDecisionMode',
      '审批方式',
      approvalDecision.mode === 'model'
        ? 'Luna Max 独立判定'
        : approvalDecision.mode === 'policy'
          ? '安全策略自动处理'
          : '人工确认',
    );
    if (approvalDecision.model) {
      append(
        'approvalModel',
        '审批模型',
        /(?:^|[./_-])luna(?:$|[./_-])/i.test(approvalDecision.model)
          ? 'Luna Max'
          : approvalDecision.model,
      );
    }
    if (approvalDecision.decision) {
      append(
        'approvalDecision',
        '审批结论',
        approvalDecision.decision === 'approve' ? '批准' : '拒绝',
      );
    }
    if (approvalDecision.status === 'failed_closed') {
      append(
        'approvalDecisionStatus',
        '审批状态',
        '无法形成可验证裁决，已按拒绝处理',
      );
    }
    if (approvalDecision.rationaleSummary) {
      append(
        'approvalRationale',
        '裁决说明',
        approvalDecision.rationaleSummary,
      );
    }
    if (approvalDecision.reasonCodes.length) {
      append(
        'approvalReasonCodes',
        '判定依据',
        boundedList(
          approvalDecision.reasonCodes.map(approvalDecisionReasonLabel),
          8,
        ),
      );
    }
    if (approvalDecision.receiptId) {
      append('approvalDecisionReceiptId', '决策回执', approvalDecision.receiptId);
    }
  }

  for (const [key, label] of countFields) {
    const value = firstFiniteNumber(layers, [key]);
    if (value !== undefined) append(key, label, `${value} 项`);
  }
  for (const [key, label, trueLabel, falseLabel] of booleanFields) {
    const value = firstBoolean(layers, [key]);
    if (value !== undefined) append(key, label, value ? trueLabel : falseLabel);
  }

  const tools = firstArray(layers, ['tools']);
  const capabilityLabels = tools
    .map((item) => capabilityLabel(item))
    .filter((item): item is string => Boolean(item));
  const declaredToolCount = firstFiniteNumber(layers, ['toolCount']);
  if (declaredToolCount !== undefined || capabilityLabels.length > 0) {
    append('toolCount', '能力数量', `${declaredToolCount ?? capabilityLabels.length} 项`);
  }
  if (capabilityLabels.length > 0) {
    append('tools', '可用能力', boundedList(capabilityLabels, 6));
  }

  const approvalOperations = firstArray(layers, ['approvalGatedOperations']);
  if (approvalOperations.length > 0) {
    append('approvalCount', '本机确认保护', `${approvalOperations.length} 项操作`);
  }

  const components = firstRecord(layers, ['components']);
  if (Object.keys(components).length > 0) {
    const unavailableIds = firstArray(layers, ['unhealthyComponents'])
      .map((item) => text(item))
      .filter(Boolean);
    const readiness = Object.values(components)
      .map((item) => record(item).ok)
      .filter((item): item is boolean => typeof item === 'boolean');
    if (readiness.length > 0) {
      append('components', '运行组件', `${readiness.filter(Boolean).length} / ${readiness.length} 可用`);
    } else if (unavailableIds.length <= Object.keys(components).length) {
      append('components', '运行组件', `${Object.keys(components).length - unavailableIds.length} / ${Object.keys(components).length} 可用`);
    }
    const unavailable = (unavailableIds.length > 0
      ? unavailableIds
      : Object.entries(components).filter(([, item]) => record(item).ok === false).map(([key]) => key))
      .map((key) => componentLabels[key])
      .filter((item): item is string => Boolean(item));
    if (unavailable.length > 0) {
      append('unavailableComponents', '需要检查', boundedList(unavailable, 5));
    } else if (unavailableIds.length > 0) {
      append('unavailableComponents', '需要检查', `${unavailableIds.length} 项组件`);
    }
  }

  const memory = firstRecord(layers, ['memory']);
  for (const [key, label] of memoryCountFields) {
    const value = finiteNumber(memory[key]);
    if (value !== undefined) append(`memory.${key}`, label, `${value} 条`);
  }

  const items = firstArray(layers, ['items']);
  if (items.length > 0 && !seen.has('resultCount') && !seen.has('itemCount')) {
    append('items', toolId === 'knowledge' ? '引用数量' : '近期活动', `${items.length} 条`);
  }
  const sourceCounts = safeActivitySourceCounts(items);
  if (sourceCounts) append('activitySources', '活动来源', sourceCounts);

  const writePolicy = firstPublicText(layers, ['writePolicy', 'safety']);
  if (writePolicy) append('writePolicy', '写入保护', writePolicy);

  const codeResult = publicCodeToolResult(toolId, args, publicResult, envelope, carrier);
  if (codeResult.file) append('file', '文件', codeResult.file);
  if (codeResult.lines !== undefined) append('lineCount', '行数', `${codeResult.lines} 行`);
  if (codeResult.additions !== undefined || codeResult.deletions !== undefined) {
    append('changes', '变更', `+${codeResult.additions ?? 0} / -${codeResult.deletions ?? 0}`);
  }
  if (collaboration) {
    for (const field of collaboration.fields) append(field.id, field.label, field.value);
  }

  const knowledgeSources = toolId === 'knowledge' ? safeKnowledgeSources(items) : undefined;
  const sources = knowledgeSources?.labels
    ?? safeSourceLabels(payload.sources ?? payload.documents ?? payload.books);
  const preview = semanticToolPreview(toolId, operation, layers);
  const resultKind = publicToolResultKind(toolId, Boolean(preview));
  const projectedItems = publicToolResultItems(resultKind, layers, codeResult.output?.text ?? '');
  const resultItems = projectedItems.length ? projectedItems : collaboration?.resultItems ?? [];
  const rawResult = inspectableRawResult(payload);
  const subagentResult = toolId === 'subagent'
    ? publicSubagentResult(layers)
    : undefined;
  const error = activity.status !== 'aborted' && !expectedNoop && (activity.status === 'failed' || payload.isError === true)
    ? publicToolError(layers, carrier)
    : '';
  const recovery = error ? publicToolRecovery(error, payload) : undefined;
  const output = collaboration?.output ?? subagentResult?.output ?? (toolId === 'codemode' && codeOutput
    ? { text: publicToolOutputText(codeOutput), truncated: publicToolOutputWasTruncated(codeOutput) }
    : codeResult.output);
  const outputLabel = collaboration?.output
    ? collaboration.outputLabel
    : !subagentResult?.output && codeResult.output
      ? codeResult.outputLabel
      : undefined;
  const request = collaboration?.request.length ? collaboration.request : codeResult.request;

  return {
    toolId,
    toolLabel,
    operation,
    summary: subagentResult?.summary || summary || collaboration?.summary || codeResult.summary || `${toolLabel} ${activity.status === 'running' ? '正在处理' : activity.status === 'failed' ? '执行失败' : activity.status === 'aborted' ? '已停止' : '已完成'}`,
    resultKind,
    ...(codeMode ? { codeMode } : {}),
    ...(code ? { code } : {}),
    ...(codeResult.file ? { target: codeResult.file } : {}),
    ...(codeResult.filePath ? { targetPath: codeResult.filePath } : {}),
    ...(codeResult.additions !== undefined || codeResult.deletions !== undefined ? {
      change: {
        additions: codeResult.additions ?? 0,
        deletions: codeResult.deletions ?? 0,
      },
    } : {}),
    fields,
    request,
    ...(output ? { output } : {}),
    ...(output && outputLabel ? { outputLabel } : {}),
    resultItems,
    ...(collaboration?.resultItemsLabel && resultItems === collaboration.resultItems
      ? { resultItemsLabel: collaboration.resultItemsLabel }
      : {}),
    ...(rawResult ? { rawResult } : {}),
    ...(resultKind === 'code' && codeResult.file ? { language: publicCodeLanguage(codeResult.file) } : {}),
    sources,
    ...(knowledgeSources?.links.length ? { sourceLinks: knowledgeSources.links } : {}),
    ...(preview ? { preview } : {}),
    ...(error ? { error } : {}),
    ...(recovery ? { recovery } : {}),
    ...(toolDestinations[toolId] ? { destination: toolDestinations[toolId] } : {}),
  };
}

interface PublicSubagentResult {
  summary: string;
  output?: {
    text: string;
    truncated: boolean;
  };
}

function publicSubagentResult(layers: Record<string, unknown>[]): PublicSubagentResult | undefined {
  const results = firstArray(layers, ['results']);
  if (results.length === 0) return undefined;

  const projected = results.slice(0, 8).map((value, index) => {
    const item = record(value);
    const status = publicSubagentStatus(text(item.status));
    const rawOutput = text(item.output).trim();
    const rawError = text(item.errorMessage ?? item.error ?? item.stderr).trim();
    const usableOutput = rawOutput && !/^\(no output\)$/iu.test(rawOutput)
      ? rawOutput
      : rawError;
    const body = publicToolOutputText(usableOutput)
      || (status === '失败' ? '子进程失败，但没有返回错误明细。' : '子进程未返回内容。');
    return {
      index,
      status,
      body,
      hasReturnedContent: Boolean(usableOutput),
      sourceLength: usableOutput.length,
      sourceTruncated: publicToolOutputWasTruncated(usableOutput),
    };
  });
  const latest = [...projected].reverse().find((item) => item.hasReturnedContent) ?? projected.at(-1)!;
  const outputText = publicToolOutputText(projected
    .map((item) => `子 Agent ${item.index + 1} · ${item.status}\n${item.body}`)
    .join('\n\n---\n\n'));
  const output = outputText
    ? {
        text: outputText,
        truncated: projected.some((item) => item.sourceTruncated)
          || publicToolOutputWasTruncated(projected.map((item) => item.body).join('\n\n')),
      }
    : undefined;
  return {
    summary: latest.body.replace(/\s+/gu, ' ').trim().slice(0, 240),
    ...(output ? { output } : {}),
  };
}

function publicSubagentStatus(value: string): string {
  return ({
    completed: '已完成',
    failed: '失败',
    aborted: '已停止',
    timeout: '超时',
  } as Record<string, string>)[value.toLowerCase()] ?? '已返回';
}

interface CollaborationToolProjection {
  summary: string;
  /** Precise public label for the generic 操作 field, e.g. 发布协作消息. */
  operationLabel?: string;
  request: PublicToolRequestField[];
  fields: PublicToolResultField[];
  resultItems: PublicToolResultItem[];
  resultItemsLabel?: string;
  output?: {
    text: string;
    truncated: boolean;
  };
  outputLabel?: string;
}

const collaborationToolIds = new Set([
  'room_partner', 'agents', 'agent_goal', 'work_documents', 'workspace_job',
]);

const collaborationSettledStates: Record<string, string> = {
  completed: '已完成',
  running: '进行中',
  active: '进行中',
  waiting: '等待中',
  pending: '等待中',
  failed: '失败',
  aborted: '已停止',
  cancelled: '已取消',
  abandoned: '已放弃',
};

function collaborationStateLabel(value: string): string {
  return collaborationSettledStates[value.toLowerCase()] ?? '';
}

function delegatedTaskLabel(value: string): string {
  const firstLine = value.split(/\r?\n/u).find(line => line.trim()) ?? '';
  return boundedCollaborationText(firstLine.replace(/\s+/gu, ' ').trim(), 80);
}

function delegatedRunStateLabel(run: Record<string, unknown>): string {
  const result = record(run.result);
  const state = text(run.state ?? run.status);
  if (state === 'completed') {
    if (text(record(run.contract).status) === 'invalid' || run.contractStatus === 'invalid'
      || result.contractStatus === 'invalid') return '结果已返回，合同无效';
    if (run.deliveryStatus === 'returned' || result.deliveryStatus === 'returned'
      || run.verificationStatus === 'unverified' || result.verificationStatus === 'unverified') {
      return '结果已返回，待核验';
    }
  }
  if (state === 'failed' && (run.failureReason === 'output_budget' || run.error === 'output budget exceeded')) {
    return '失败：输出预算已用尽';
  }
  return collaborationStateLabel(state);
}

function childProgressLabel(run: AgentChildProgress): string {
  if (run.phase === 'terminal') return delegatedRunStateLabel({ ...run });
  if (run.phase === 'waiting') return '等待补充信息';
  if (run.phase === 'tool_started' || run.phase === 'tool_finished') {
    const read = ['read', 'read_file', 'workspace_read'].includes(run.toolName);
    const action = run.phase === 'tool_started'
      ? read ? '正在读取' : '正在使用'
      : run.isError ? read ? '读取失败' : '工具执行失败'
        : read ? '已读取' : '工具已返回';
    return [action, run.fileName || publicToolName(run.toolName)].filter(Boolean).join(' ');
  }
  return run.state === 'queued' ? '排队中' : '进行中';
}

/**
 * Concrete payload projection for collaboration, delegation, goal, work
 * document, and background-job receipts (PF-CM-007/010). The rows used to
 * settle on a raw tool id with an empty detail body; the sent content, task
 * briefs, acceptance criteria, evidence, and commands were only reachable via
 * the raw JSON. Everything projected here comes from the durable Runtime
 * payload — nothing is invented and the raw receipt stays available below.
 */
function collaborationToolResult(
  toolId: string,
  op: string,
  args: Record<string, unknown>,
  layers: Record<string, unknown>[],
): CollaborationToolProjection | undefined {
  const request: PublicToolRequestField[] = [];
  const fields: PublicToolResultField[] = [];
  const addRequest = (id: string, label: string, value: string, code = false) => {
    if (!value || request.some((field) => field.id === id)) return;
    request.push({ id, label, value, ...(code ? { code: true } : {}) });
  };
  const addField = (id: string, label: string, value: string) => {
    if (!value || fields.some((field) => field.id === id)) return;
    fields.push({ id, label, value });
  };

  if (toolId === 'room_partner') {
    const opLabel = ({
      post: '发布协作消息',
      reply: '回复协作消息',
      ask: '向伙伴提问',
      send: '私信伙伴',
      peer_send: '私信伙伴',
      peer_reply: '回复伙伴消息',
      peer_list: '查看伙伴消息',
      status: '查看协作状态',
      complete: '提交完成',
      list: '查看协作记录',
    } as Record<string, string>)[op] ?? '协作操作';
    const kind = text(args.kind) || firstText(layers, ['kind']);
    const kindLabel = ({
      result: '成果通报',
      work_result: '工作成果',
      comment: '协作评论',
      question: '协作提问',
      ask: '协作提问',
      update: '进展更新',
      decision: '协作决定',
      blocker: '受阻说明',
      status: '状态通报',
    } as Record<string, string>)[kind.toLowerCase()] ?? '';
    if (kindLabel) addRequest('kind', '消息类型', kindLabel);
    const replyTo = text(args.replyTo);
    if (replyTo) addRequest('replyTo', '回复目标', replyTo, true);
    const singleMessage = firstRecord(layers, ['message']);
    const content = text(args.content) || text(singleMessage.content);
    const output = content
      ? {
          text: publicToolOutputText(content),
          truncated: publicToolOutputWasTruncated(content),
        }
      : undefined;
    const outputLabel = op === 'peer_reply'
      ? '回复内容'
      : op === 'peer_send' || op === 'send'
        ? '私信内容'
        : '发送内容';
    const published = firstBoolean(layers, ['published']);
    if (published !== undefined) addField('published', '发布状态', published ? '已发布到 Room' : '尚未发布');
    const messageStatus = intercomStatusLabels[text(singleMessage.status).toLowerCase()];
    if (messageStatus) addField('messageStatus', '送达状态', messageStatus);
    const settled = firstArray(layers, ['settledWorkItems']);
    if (settled.length > 0) addField('settledWorkItems', '结算工作项', `${settled.length} 项`);
    // Partner-to-partner gravity: a peer_list result carries the real intercom
    // exchange (who told whom what, and whether it was delivered), and a reply
    // receipt carries the single message it answered. Both stay expandable.
    const intercomSource = firstArray(layers, ['messages']);
    const messageItems = intercomMessageItems(
      intercomSource.length > 0
        ? intercomSource
        : Object.keys(singleMessage).length > 0 ? [singleMessage] : [],
    );
    return {
      summary: messageItems.length && op === 'peer_list'
        ? `${opLabel} · ${messageItems.length} 条`
        : kindLabel ? `${opLabel} · ${kindLabel}` : opLabel,
      operationLabel: opLabel,
      request,
      fields,
      resultItems: messageItems,
      ...(messageItems.length ? { resultItemsLabel: '伙伴消息' } : {}),
      ...(output ? { output, outputLabel } : {}),
    };
  }

  if (toolId === 'agents') {
    const opLabel = ({
      delegate: '委派协作任务',
      start: '委派协作任务',
      status: '查询协作状态',
      collect: '收取协作结果',
      results: '收取协作结果',
      cancel: '取消委派',
      abort: '中止委派',
      artifact: '领取协作产物',
    } as Record<string, string>)[op] ?? '协作操作';
    const template = text(args.agent);
    const version = text(args.version);
    if (template) {
      addRequest('agent', '伙伴模板', version ? `${template} v${version}` : template, true);
    }
    addRequest('task', '任务简报', boundedCollaborationText(text(args.task), 2_000));
    addRequest('expectedOutput', '预期产出', boundedCollaborationText(text(args.expectedOutput), 800));
    const criteria = stringItems(args.acceptanceCriteria);
    if (criteria.length > 0) {
      addRequest('acceptanceCriteria', '验收标准', criteria.map((item, index) => `${index + 1}. ${item}`).join('\n'));
    }
    const allowedTools = stringItems(args.allowedTools).map((tool) => publicToolName(tool));
    if (allowedTools.length > 0) addRequest('allowedTools', '允许工具', boundedList(allowedTools, 8));
    const access = text(args.access).toLowerCase();
    if (access) addRequest('access', '访问权限', access === 'write' ? '可写' : access === 'read' ? '只读' : access);
    const contextMode = text(args.contextMode).toLowerCase();
    if (contextMode) {
      addRequest('contextMode', '上下文模式', contextMode === 'fresh' ? '全新上下文' : contextMode === 'inherit' ? '继承上下文' : contextMode);
    }
    const thinking = text(args.thinkingLevel).toLowerCase();
    if (thinking) {
      addRequest('thinkingLevel', '思考强度', ({ low: '低', medium: '中', high: '高' } as Record<string, string>)[thinking] ?? thinking);
    }
    const batch = firstRecord(layers, ['batch']);
    const batchState = collaborationStateLabel(text(batch.state));
    if (batchState) addField('batchState', '批次状态', batchState);
    if (batch.abortRequested === true && text(batch.state) !== 'aborted') {
      addField('abortRequested', '中止请求', '已发出，等待子 Agent 停止');
    }
    const accepted = firstBoolean(layers, ['accepted']);
    if (accepted !== undefined) addField('accepted', '受理状态', accepted ? '已受理' : '未受理');
    const waited = firstBoolean(layers, ['waited']);
    if (waited !== undefined) addField('waited', '等待方式', waited ? '同步等待结果' : '后台继续运行');
    const progress = firstRecord(layers, ['delegationProgress']);
    const childRuns = agentChildProgress(progress.runs);
    const batchRuns = Array.isArray(batch.runs) ? batch.runs : [];
    const runs = batchRuns.length ? batchRuns : childRuns.map((run) => ({ ...run, id: run.runId }));
    if (runs.length > 0) addField('runCountCollab', '子任务', `${runs.length} 项`);
    const resultItems = runs.slice(0, 8).flatMap((value, index): PublicToolResultItem[] => {
      const run = record(value);
      const runId = text(run.id);
      const live = childRuns.find((child) => child.runId === runId);
      const runTemplate = text(run.templateId) || template || 'Agent';
      const state = text(run.state ?? run.status);
      const terminal = ['completed', 'failed', 'aborted', 'timed_out'].includes(state);
      const runState = delegatedRunStateLabel(run);
      const taskLabel = delegatedTaskLabel(text(run.task) || text(run.taskLabel) || live?.taskLabel || '');
      const roleLabel = subagentTemplateLabel(runTemplate as Parameters<typeof subagentTemplateLabel>[0]) || runTemplate;
      const progressLabel = live && !terminal ? childProgressLabel(live) : runState;
      return [{
        id: runId || `run:${index}`,
        label: boundedCollaborationText(taskLabel, 32) || `子任务 ${Number.isInteger(run.ordinal) ? Number(run.ordinal) + 1 : index + 1}`,
        ...(taskLabel ? { title: taskLabel } : {}),
        text: [progressLabel, roleLabel].filter(Boolean).join(' · '),
      }];
    });
    return {
      summary: op === 'abort'
        ? '已请求停止子 Agent'
        : [`委派 ${template || 'Agent'}`, batchState].filter(Boolean).join(' · '),
      operationLabel: opLabel,
      request,
      fields,
      resultItems,
      ...(resultItems.length ? { resultItemsLabel: batchRuns.length ? '子任务结果' : '子任务进度' } : {}),
    };
  }

  if (toolId === 'agent_goal') {
    const opLabel = ({
      configure: '设定长期目标',
      update: '更新长期目标',
      complete: '标记目标完成',
      abandon: '放弃长期目标',
      progress: '记录目标进展',
      status: '查看目标进展',
      list: '查看长期目标',
    } as Record<string, string>)[op] ?? '目标操作';
    addRequest('summary', '目标结论', boundedCollaborationText(text(args.summary), 1_200));
    const goal = firstRecord(layers, ['goal']);
    const objective = boundedCollaborationText(text(goal.objective), 500);
    if (objective) addField('objective', '目标', objective);
    const successCriteria = boundedCollaborationText(text(goal.successCriteria), 500);
    if (successCriteria) addField('successCriteria', '成功标准', successCriteria);
    const goalState = collaborationStateLabel(text(goal.status));
    if (goalState) addField('goalState', '目标状态', goalState);
    const audit = record(goal.completionAudit);
    const evidence = [
      ...(Array.isArray(args.evidence) ? args.evidence : []),
      ...(Array.isArray(audit.evidence) ? audit.evidence : []),
    ];
    const seenEvidence = new Set<string>();
    const resultItems = evidence.flatMap((value): PublicToolResultItem[] => {
      const item = record(value);
      const reference = boundedCollaborationText(text(item.reference), 200);
      const summaryText = boundedCollaborationText(text(item.summary), 300);
      const dedupeKey = `${reference}|${summaryText}`;
      if ((!reference && !summaryText) || seenEvidence.has(dedupeKey)) return [];
      seenEvidence.add(dedupeKey);
      const kindLabel = ({
        test: '测试',
        receipt: '回执',
        artifact: '产物',
      } as Record<string, string>)[text(item.kind).toLowerCase()] ?? '证据';
      return [{
        id: `evidence:${seenEvidence.size}:${reference || summaryText}`,
        label: kindLabel,
        text: [summaryText, reference].filter(Boolean).join(' · '),
      }];
    }).slice(0, 12);
    return {
      summary: [opLabel, goalState].filter(Boolean).join(' · '),
      operationLabel: opLabel,
      request,
      fields,
      resultItems,
      ...(resultItems.length ? { resultItemsLabel: '完成证据' } : {}),
    };
  }

  if (toolId === 'work_documents') {
    const opLabel = ({
      'authority.context': '读取权威上下文',
      register: '登记工作文档',
      append: '追加工作记录',
      update: '更新工作文档',
      bind: '绑定工作文档',
      read: '读取工作文档',
      list: '浏览工作文档',
    } as Record<string, string>)[op] ?? '文档操作';
    const authorityKind = text(args.authorityKind) || firstText(layers, ['authorityKind']);
    const authorityKindLabel = ({
      session_goal: '会话目标',
      room_work_item: '协作工作项',
      todo: 'Todo',
    } as Record<string, string>)[authorityKind.toLowerCase()] ?? authorityKind;
    if (authorityKindLabel) addRequest('authorityKind', '权威对象', authorityKindLabel);
    const authorityId = text(args.authorityId) || firstText(layers, ['authorityId']);
    if (authorityId) addRequest('authorityId', '对象标识', authorityId, true);
    const state = collaborationStateLabel(firstText(layers, ['state', 'terminalState']));
    if (state) addField('documentState', '文档状态', state);
    const revision = firstFiniteNumber(layers, ['authorityRevision', 'documentRevision']);
    if (revision !== undefined) addField('authorityRevision', '权威修订', `第 ${revision} 版`);
    return {
      summary: [authorityKindLabel || opLabel, state].filter(Boolean).join(' · ') || opLabel,
      operationLabel: opLabel,
      request,
      fields,
      resultItems: [],
    };
  }

  if (toolId === 'workspace_job') {
    const opLabel = ({
      start: '启动后台任务',
      status: '查询后台任务',
      logs: '读取任务日志',
      cancel: '停止后台任务',
      list: '浏览后台任务',
    } as Record<string, string>)[op] ?? '后台任务操作';
    const job = firstRecord(layers, ['job']);
    const command = text(job.command) || text(args.command);
    if (command) addRequest('command', '命令', boundedCollaborationText(command, 300), true);
    const cwd = text(job.cwd);
    if (cwd) addRequest('cwd', '工作目录', boundedCollaborationText(cwd, 200), true);
    const label = boundedCollaborationText(text(job.label), 120);
    if (label) addField('jobLabel', '任务名称', label);
    const jobState = collaborationStateLabel(text(job.status));
    if (jobState) addField('jobState', '任务状态', jobState);
    return {
      summary: [label || opLabel, jobState].filter(Boolean).join(' · '),
      operationLabel: opLabel,
      request,
      fields,
      resultItems: [],
    };
  }

  return undefined;
}

const intercomWorkActionLabels: Record<string, string> = {
  accepted: '验收通过',
  submitted: '已提交',
  review: '待复核',
  rework: '要求返工',
  blocked: '受阻',
  dispatched: '已分派',
  assigned: '已指派',
  released: '已解除',
};

const intercomStatusLabels: Record<string, string> = {
  delivered: '已送达',
  pending: '待送达',
  queued: '待送达',
  replied: '已回复',
  failed: '发送失败',
};

/** Real partner-to-partner intercom traffic ("木星和地球通过引力交流"):
 * source → target, the work action, the actual message body, and delivery
 * state. Participant ids surface as short tails, never as full id walls. */
function intercomMessageItems(values: unknown[]): PublicToolResultItem[] {
  return values.slice(0, 8).flatMap((value, index): PublicToolResultItem[] => {
    const message = record(value);
    if (text(message.schemaVersion) !== 'rag-ime.agent-room-intercom.v1') return [];
    const content = boundedCollaborationText(text(message.content), 400);
    const actionLabel = intercomWorkActionLabels[text(message.workAction).toLowerCase()] ?? '';
    if (!content && !actionLabel) return [];
    const fromTail = participantTail(text(message.sourceParticipantId));
    const toTail = participantTail(text(message.targetParticipantId));
    const route = fromTail && toTail
      ? `伙伴 #${fromTail} → 伙伴 #${toTail}`
      : text(message.kind) === 'reply' ? '伙伴回复' : '伙伴消息';
    const statusLabel = intercomStatusLabels[text(message.status).toLowerCase()] ?? '';
    return [{
      id: text(message.id) || `intercom:${index}`,
      label: [route, actionLabel].filter(Boolean).join(' · '),
      text: [content, statusLabel].filter(Boolean).join(' · '),
    }];
  });
}

function participantTail(value: string): string {
  const match = /([0-9a-z]{4})[^0-9a-z]*$/iu.exec(value);
  return match?.[1] ?? '';
}

function boundedCollaborationText(value: string, limit: number): string {
  const masked = publicToolOutputText(value);
  if (masked.length <= limit) return masked;
  return `${masked.slice(0, Math.max(0, limit - 1)).trimEnd()}…`;
}

function stringItems(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => boundedCollaborationText(text(item), 300))
    .filter(Boolean)
    .slice(0, 12);
}

function inspectableRawResult(
  payload: Record<string, unknown>,
): PublicToolResultView['rawResult'] | undefined {
  const value = payload.result !== undefined
    ? payload.result
    : payload.partialResult;
  if (value === undefined || value === null) return undefined;
  return {
    format: typeof value === 'string' ? 'text' : 'json',
    value,
  };
}

const rawSecretKey = /(?:token|secret|password|api.?key|authorization|cookie)/iu;

export function inspectableRawResultText(value: unknown, format: 'json' | 'text'): string {
  if (format === 'text' && typeof value === 'string') {
    const source = value
      .slice(0, INSPECTABLE_TOOL_RESULT_MAX_CHARS + 4_096)
      .replace(/\r\n?/gu, '\n');
    return boundedInspectableText(maskRawResultString(source), value.length > source.length);
  }
  return boundedInspectableJson(value);
}

function boundedInspectableText(value: string, alreadyTruncated = false): string {
  const bodyLimit = INSPECTABLE_TOOL_RESULT_MAX_CHARS - INSPECTABLE_TOOL_RESULT_TRUNCATED.length;
  if (!alreadyTruncated && value.length <= INSPECTABLE_TOOL_RESULT_MAX_CHARS) return value;
  return `${value.slice(0, bodyLimit)}${INSPECTABLE_TOOL_RESULT_TRUNCATED}`;
}

function boundedInspectableJson(value: unknown): string {
  const bodyLimit = INSPECTABLE_TOOL_RESULT_MAX_CHARS - INSPECTABLE_TOOL_RESULT_TRUNCATED.length;
  const chunks: string[] = [];
  const seen = new WeakSet<object>();
  let length = 0;
  let truncated = false;
  const append = (part: string) => {
    if (truncated || !part) return;
    const remaining = bodyLimit - length;
    if (remaining <= 0) {
      truncated = true;
      return;
    }
    if (part.length > remaining) {
      chunks.push(part.slice(0, remaining));
      length += remaining;
      truncated = true;
      return;
    }
    chunks.push(part);
    length += part.length;
  };
  const visit = (item: unknown, depth: number, indent: string) => {
    if (truncated) return;
    if (item === null || typeof item === 'number' || typeof item === 'boolean') {
      append(JSON.stringify(item));
      return;
    }
    if (typeof item === 'string' || typeof item === 'bigint' || typeof item === 'undefined') {
      append(JSON.stringify(maskRawResultString(String(item))));
      return;
    }
    if (typeof item !== 'object') {
      append(JSON.stringify(String(item)));
      return;
    }
    if (depth >= 8) {
      append(JSON.stringify('[bounded]'));
      return;
    }
    if (seen.has(item)) {
      append(JSON.stringify('[circular]'));
      return;
    }
    seen.add(item);
    const nextIndent = `${indent}  `;
    if (Array.isArray(item)) {
      append('[');
      for (let index = 0; index < item.length && !truncated; index += 1) {
        append(`${index === 0 ? '' : ','}\n${nextIndent}`);
        visit(item[index], depth + 1, nextIndent);
      }
      if (!truncated && item.length > 0) append(`\n${indent}`);
      append(']');
      seen.delete(item);
      return;
    }
    append('{');
    const entries = Object.entries(item as Record<string, unknown>);
    for (let index = 0; index < entries.length && !truncated; index += 1) {
      const [key, child] = entries[index]!;
      append(`${index === 0 ? '' : ','}\n${nextIndent}${JSON.stringify(key)}: `);
      if (rawSecretKey.test(key)) append(JSON.stringify('[REDACTED_SECRET]'));
      else visit(child, depth + 1, nextIndent);
    }
    if (!truncated && entries.length > 0) append(`\n${indent}`);
    append('}');
    seen.delete(item);
  };
  try {
    visit(value, 0, '');
  } catch {
    return boundedInspectableText(maskRawResultString(String(value)), true);
  }
  const text = chunks.join('');
  return truncated ? `${text}${INSPECTABLE_TOOL_RESULT_TRUNCATED}` : text;
}

function maskRawResultString(value: string): string {
  return value
    .replace(/\bsk-[A-Za-z0-9_-]{6,}\b/gu, '[REDACTED_SECRET]')
    .replace(/\b([A-Za-z0-9_]*(?:api[_-]?key|access[_-]?token|password|secret|authorization))(\s*(?:=|:)\s*)([^\s;'"\\]+|"[^"]*"|'[^']*')/giu, '$1$2[REDACTED_SECRET]')
    .replace(/(--(?:api[_-]?key|token|password|secret)\s+)([^\s;'"\\]+|"[^"]*"|'[^']*')/giu, '$1[REDACTED_SECRET]');
}

const terminalResultTools = new Set(['bash', 'shell', 'workspace_shell', 'workspace_job']);
const codeResultTools = new Set(['read', 'read_file', 'workspace_read']);
const matchResultTools = new Set(['grep', 'workspace_search', 'workspace_lsp']);
const fileResultTools = new Set(['find', 'ls', 'workspace_list']);
const changeResultTools = new Set([
  'write', 'write_file', 'workspace_write', 'workspace_write_file',
  'edit', 'edit_file', 'workspace_edit', 'workspace_edit_file', 'workspace_patch',
]);

function publicToolResultKind(toolId: string, semantic: boolean): PublicToolResultKind {
  if (semantic) return 'semantic';
  if (toolId === 'codemode') return 'codemode';
  if (terminalResultTools.has(toolId)) return 'terminal';
  if (codeResultTools.has(toolId)) return 'code';
  if (matchResultTools.has(toolId)) return 'matches';
  if (fileResultTools.has(toolId)) return 'files';
  if (changeResultTools.has(toolId)) return 'change';
  if (toolId === 'browser') return 'browser';
  return 'structured';
}

function publicToolResultItems(
  kind: PublicToolResultKind,
  layers: Record<string, unknown>[],
  output: string,
): PublicToolResultItem[] {
  if (kind === 'matches') return publicMatchItems(output);
  if (kind === 'browser') return publicBrowserItems(layers);
  if (kind !== 'files') return [];
  const entries = firstArray(layers, ['entries', 'files', 'items']);
  const projected = entries.slice(0, 40).flatMap((value, index) => {
    if (typeof value === 'string') {
      const visible = publicStructuredText(value);
      const path = publicRelativeWorkspacePath(value);
      return visible ? [{
        id: `file:${index}:${visible}`,
        label: visible,
        text: '',
        kind: '',
        ...(path ? { path } : {}),
      }] : [];
    }
    const item = record(value);
    const label = publicDisplayText(
      item.name ?? item.fileName ?? item.title ?? item.label,
      '',
    );
    if (!label) return [];
    const rawPath = firstText(
      [item],
      ['relativePath', 'path', 'filePath', 'fileName', 'name', 'label'],
    );
    const path = publicRelativeWorkspacePath(rawPath);
    const itemKind = publicDisplayText(item.kind ?? item.type, '');
    return [{
      id: `file:${index}:${label}`,
      label,
      text: itemKind ? publicFileKindLabel(itemKind) : '',
      kind: itemKind,
      ...(path ? { path } : {}),
    }];
  });
  if (projected.length > 0) return projected;
  return output.split('\n').slice(0, 40).flatMap((line, index) => {
    const visible = publicStructuredText(line);
    const path = publicRelativeWorkspacePath(line);
    return visible ? [{
      id: `file-output:${index}:${visible}`,
      label: visible,
      text: '',
      kind: '',
      ...(path ? { path } : {}),
    }] : [];
  });
}

function publicBrowserItems(layers: Record<string, unknown>[]): PublicToolResultItem[] {
  const items = firstArray(layers, ['tabs', 'items', 'traces']);
  return items.slice(0, 40).flatMap((value, index) => {
    const item = record(value);
    const host = publicBrowserHost(text(item.url));
    const label = publicDisplayText(
      item.title ?? item.pageTitle ?? item.action ?? item.browserName ?? item.label,
      host,
    );
    if (!label) return [];
    const status = publicStatusLabel(text(item.status ?? item.state));
    const detail = [host && host !== label ? host : '', status].filter(Boolean).join(' · ');
    return [{
      id: `browser:${index}:${label}`,
      label,
      text: detail,
      kind: text(item.action ?? item.status),
    }];
  });
}

function publicBrowserHost(value: string): string {
  if (!value || value.length > 4_000) return '';
  try {
    const url = new URL(value);
    return url.protocol === 'https:' || url.protocol === 'http:'
      ? url.host.slice(0, 240)
      : '';
  } catch {
    return '';
  }
}

function publicMatchItems(output: string): PublicToolResultItem[] {
  return output.split('\n').slice(0, 40).flatMap((line, index) => {
    const visible = publicStructuredText(line);
    if (!visible) return [];
    const match = /^(.+?):(\d+)(?::\d+)?:\s?(.*)$/u.exec(visible);
    if (!match) {
      return [{ id: `match:${index}:${visible}`, label: `结果 ${index + 1}`, text: visible }];
    }
    const location = `${match[1]}:${match[2]}`;
    return [{
      id: `match:${index}:${location}`,
      label: location,
      text: match[3] || '匹配位置',
    }];
  });
}

function publicFileKindLabel(value: string): string {
  return ({
    directory: '文件夹',
    dir: '文件夹',
    file: '文件',
    symlink: '链接',
  } as Record<string, string>)[value.toLowerCase()] ?? value;
}

function publicCodeLanguage(file: string): string {
  const extension = file.toLowerCase().split('.').at(-1) ?? '';
  return ({
    ts: 'typescript',
    tsx: 'tsx',
    js: 'javascript',
    jsx: 'jsx',
    py: 'python',
    rs: 'rust',
    go: 'go',
    swift: 'swift',
    java: 'java',
    kt: 'kotlin',
    c: 'c',
    h: 'c',
    cc: 'cpp',
    cpp: 'cpp',
    css: 'css',
    html: 'html',
    json: 'json',
    md: 'markdown',
    yaml: 'yaml',
    yml: 'yaml',
    sh: 'shellscript',
    zsh: 'shellscript',
  } as Record<string, string>)[extension] ?? 'text';
}

function semanticToolPreview(
  toolId: string,
  operation: string,
  layers: Record<string, unknown>[],
): PublicToolSemanticPreview | undefined {
  if (toolId === 'agent_role_book') return roleBookToolPreview(operation, layers);
  if (toolId !== 'memory') return undefined;

  if (operation === 'read') {
    const book = firstRecord(layers, ['book']);
    if (Object.keys(book).length === 0) return undefined;
    const title = publicDisplayText(book.title, '未命名工具书');
    const description = publicLongText(book.summary);
    const badges = firstStringArray([book], ['tags']).slice(0, 6);
    const memories = firstArray([book], ['memories']);
    const items = memories.slice(0, 8).flatMap((value, index) => {
      const memory = record(value);
      const ref = record(memory.ref);
      const kind = memoryReferenceKind(memory, ref);
      const itemText = publicLongText(memory.text);
      if (!itemText) return [];
      const referenceId = memoryReferenceId(memory, ref);
      const layer = memoryLayerForKind(kind);
      return [{
        id: referenceId || `memory:${index}`,
        label: memoryTypeLabel(text(memory.type)),
        text: itemText,
        ...(referenceId && layer ? {
          href: `#/memory?layer=${encodeURIComponent(layer)}&id=${encodeURIComponent(referenceId)}`,
        } : {}),
      }];
    });
    return {
      kind: 'book',
      title: `《${title}》`,
      ...(description ? { description } : {}),
      badges,
      items,
    };
  }

  if (operation === 'catalog') {
    const items = firstArray(layers, ['items']).slice(0, 8).flatMap((value, index) => {
      const item = record(value);
      const ref = record(item.ref);
      const kind = memoryReferenceKind(item, ref);
      const itemText = publicDisplayText(item.title ?? item.name ?? item.label, '');
      if (!itemText) return [];
      const referenceId = memoryReferenceId(item, ref);
      const layer = memoryLayerForKind(kind);
      return [{
        id: referenceId || `catalog:${index}`,
        label: memoryCatalogKindLabel(kind),
        text: itemText,
        ...(referenceId && layer ? {
          href: `#/memory?layer=${encodeURIComponent(layer)}&id=${encodeURIComponent(referenceId)}`,
        } : {}),
      }];
    });
    if (items.length === 0) return undefined;
    return {
      kind: 'collection',
      title: '个人上下文目录',
      badges: [],
      items,
    };
  }

  if (['search', 'get', 'explain', 'recent', 'list'].includes(operation)) {
    const rawItems = firstArray(layers, ['items']);
    const singleItem = firstRecord(layers, ['item']);
    const values = rawItems.length ? rawItems : Object.keys(singleItem).length ? [singleItem] : [];
    const items = values.slice(0, 8).flatMap((value, index) => {
      const item = record(value);
      const ref = record(item.ref);
      const kind = memoryReferenceKind(item, ref);
      const itemText = publicLongText(
        item.text ?? item.summary ?? item.title ?? item.label ?? item.preview,
      );
      if (!itemText) return [];
      const referenceId = memoryReferenceId(item, ref);
      const layer = memoryLayerForKind(kind);
      return [{
        id: referenceId || `memory-result:${index}`,
        label: memoryCatalogKindLabel(kind),
        text: itemText,
        ...(referenceId && layer ? {
          href: `#/memory?layer=${encodeURIComponent(layer)}&id=${encodeURIComponent(referenceId)}`,
        } : {}),
      }];
    });
    if (!items.length) return undefined;
    const kinds = [...new Set(values.map((value) => {
      const item = record(value);
      const ref = record(item.ref);
      return memoryReferenceKind(item, ref);
    }).filter(Boolean))];
    const previewKind = kinds.length === 1 ? semanticPreviewKind(kinds[0]!) : 'collection';
    return {
      kind: previewKind,
      title: operation === 'search' ? '记忆召回结果' : '记忆详情',
      badges: kinds.map(memoryCatalogKindLabel).filter((value, index, source) => source.indexOf(value) === index),
      items,
    };
  }

  if (['remember_preview', 'correct_preview', 'forget_preview'].includes(operation)) {
    const proposedText = firstPublicText(layers, ['proposedText', 'text', 'summary']);
    const targetId = firstText(layers, ['targetId', 'targetMemoryId']);
    const proposalId = firstText(layers, ['proposalId']);
    const evidenceIds = firstArray(layers, ['evidenceIds']).map(text).filter(Boolean).slice(0, 8);
    const items: PublicToolSemanticPreview['items'] = [];
    if (proposedText) {
      items.push({
        id: targetId || proposalId || 'memory-proposal',
        label: operation === 'forget_preview' ? '将撤回' : operation === 'correct_preview' ? '更正为' : '新增事实',
        text: proposedText,
        ...(targetId ? { href: `#/memory?layer=atoms&id=${encodeURIComponent(targetId)}` } : {}),
      });
    }
    evidenceIds.forEach((evidenceId, index) => items.push({
      id: `proposal-evidence:${evidenceId}`,
      label: '原始证据',
      text: `来源证据 ${index + 1}`,
      href: `#/memory?layer=evidence&id=${encodeURIComponent(evidenceId)}`,
    }));
    if (!items.length) return undefined;
    return {
      kind: 'atom',
      title: '受治理记忆预览',
      badges: ['尚未应用', '需要本机审批'],
      items,
    };
  }

  if (['remember_apply', 'correct_apply', 'forget_apply', 'governance_rollback'].includes(operation)) {
    const memoryId = firstText(layers, ['memoryId', 'previousMemoryId']);
    const summary = firstPublicText(layers, ['summary']);
    if (!memoryId || !summary) return undefined;
    return {
      kind: 'atom',
      title: '记忆治理回执',
      badges: ['已留审计记录'],
      items: [{
        id: memoryId,
        label: '事实谱系',
        text: summary,
        href: `#/memory?layer=atoms&id=${encodeURIComponent(memoryId)}`,
      }],
    };
  }

  return undefined;
}

function roleBookToolPreview(
  operation: string,
  layers: Record<string, unknown>[],
): PublicToolSemanticPreview | undefined {
  const revision = firstRecord(layers, ['revision']);
  const draft = firstRecord(layers, ['draft']);
  const history = firstArray(layers, ['items']);
  const values = history.length
    ? history
    : Object.keys(revision).length
      ? [revision]
      : Object.keys(draft).length
        ? [draft]
        : [];
  const items = values.slice(0, 8).flatMap((value, index) => {
    const item = record(value);
    const revisionId = text(item.revisionId);
    const draftId = text(item.draftId);
    const id = revisionId || draftId || `role-book:${index}`;
    const revisionNumber = finiteNumber(item.revisionNumber);
    const itemText = publicDisplayText(
      item.changeSummary
        ?? item.summary
        ?? (revisionNumber !== undefined ? `角色书修订 #${revisionNumber}` : '角色书待审草案'),
      '角色书修订',
    );
    return [{
      id,
      label: text(item.status) === 'draft' || draftId ? '待审草案' : '角色书修订',
      text: itemText,
      ...(revisionId ? {
        href: `#/memory?layer=role-books&id=${encodeURIComponent(revisionId)}`,
      } : {}),
    }];
  });
  if (!items.length) return undefined;
  return {
    kind: 'role_book',
    title: operation === 'history' ? '角色书修订历史' : 'Agent 角色书',
    badges: operation === 'propose_revision' ? ['仅保存草案', '不能自行激活'] : [],
    items,
  };
}

interface PublicCodeToolResult {
  summary: string;
  file: string;
  filePath?: string;
  request: PublicToolRequestField[];
  output?: {
    text: string;
    truncated: boolean;
  };
  /** Human heading when the output is a concrete written body or diff. */
  outputLabel?: string;
  lines?: number;
  additions?: number;
  deletions?: number;
}

function publicCodeToolResult(
  toolId: string,
  args: Record<string, unknown>,
  publicResult: Record<string, unknown>,
  envelope: Record<string, unknown>,
  carrier: Record<string, unknown>,
): PublicCodeToolResult {
  const fileTools = new Set([
    'read', 'read_file', 'workspace_read',
    'write', 'write_file', 'workspace_write', 'workspace_write_file',
    'edit', 'edit_file', 'workspace_edit', 'workspace_edit_file',
  ]);
  const searchTools = new Set(['grep', 'workspace_search']);
  const listTools = new Set(['find', 'ls', 'workspace_list']);
  const commandTools = new Set(['bash', 'shell', 'workspace_shell', 'workspace_job']);
  const codeTools = new Set([...fileTools, ...searchTools, ...listTools, ...commandTools]);
  const rawOutputText = firstText([publicResult], ['outputPreview']);
  const managedEvidence = managedEvidencePreview(rawOutputText);
  const requestLayers = [
    publicResult,
    managedEvidence?.request ?? {},
    args,
    envelope,
    carrier,
  ];
  const rawPath = firstText(requestLayers, ['relativePath', 'fileName', 'file_path', 'path']);
  const file = codeTools.has(toolId) ? publicWorkspacePath(rawPath) : '';
  const filePath = codeTools.has(toolId) ? publicRelativeWorkspacePath(rawPath) : '';
  const request: PublicToolRequestField[] = [];
  const addRequest = (id: string, label: string, value: string, code = false) => {
    if (!value || request.some((field) => field.id === id)) return;
    request.push({ id, label, value, ...(code ? { code: true } : {}) });
  };
  if (file) addRequest('path', '目标', file, true);

  const operation = firstText(requestLayers, ['op']);
  const mode = firstText(requestLayers, ['mode']);
  const patternKind = firstText(requestLayers, ['patternKind']);
  const query = firstText(requestLayers, ['query']);
  const pattern = firstText(requestLayers, ['pattern']);
  const glob = firstText(requestLayers, ['glob']);
  const command = firstText(requestLayers, ['command']);
  if (operation) addRequest('op', '动作', operation, true);
  if (query) addRequest('query', '查询', query, true);
  if (pattern) addRequest('pattern', '模式', pattern, true);
  if (glob) addRequest('glob', '文件范围', glob, true);
  if (mode) addRequest('mode', '搜索方式', mode, true);
  if (patternKind) addRequest('patternKind', '模式类型', patternKind, true);
  if (command) addRequest('command', '命令', command, true);
  for (const [key, label] of [
    ['offset', '起始行'],
    ['limit', '上限'],
    ['context', '上下文行'],
    ['timeout', '超时'],
  ] as const) {
    const value = firstFiniteNumber(requestLayers, [key]);
    if (value !== undefined) {
      addRequest(key, label, key === 'timeout' ? `${value} 秒` : String(value), true);
    }
  }

  const outputText = managedEvidence?.summary ?? publicToolOutputText(rawOutputText);
  const output = outputText
    ? {
        text: outputText,
        truncated: managedEvidence?.truncated
          ?? (
            firstBoolean([publicResult], ['outputTruncated']) === true
            || publicToolOutputWasTruncated(rawOutputText)
          ),
      }
    : undefined;

  if (['write', 'write_file', 'workspace_write', 'workspace_write_file'].includes(toolId)) {
    const lines = firstFiniteNumber([publicResult], ['lineCount']) ?? publicLineCount(text(args.content));
    const additions = firstFiniteNumber([publicResult], ['additions']) ?? lines;
    // The written body lives in the call arguments; expose the bounded content
    // so expanding a write row shows what was written, not an empty change
    // card (PF-CM-007).
    const contentSource = text(args.content);
    const contentBody = contentSource ? publicToolOutputText(contentSource) : '';
    const contentOutput = contentBody
      ? { text: contentBody, truncated: publicToolOutputWasTruncated(contentSource) }
      : output;
    return {
      file,
      ...(filePath ? { filePath } : {}),
      request,
      ...(contentOutput ? { output: contentOutput, outputLabel: '写入内容' } : {}),
      ...(lines !== undefined ? { lines } : {}),
      ...(additions !== undefined ? { additions } : {}),
      summary: file ? `${file}${lines !== undefined ? ` +${lines}` : ' 已写入'}` : '文件已写入',
    };
  }
  if (['edit', 'edit_file', 'workspace_edit', 'workspace_edit_file', 'workspace_patch'].includes(toolId)) {
    const diff = firstText([envelope, carrier], ['diff', 'patch']);
    const diffChanges = publicDiffCounts(diff);
    const additions = firstFiniteNumber([publicResult, envelope, carrier], ['additions'])
      ?? diffChanges.additions;
    const deletions = firstFiniteNumber([publicResult, envelope, carrier], ['deletions'])
      ?? diffChanges.deletions;
    const changes = {
      ...(additions !== undefined ? { additions } : {}),
      ...(deletions !== undefined ? { deletions } : {}),
    };
    const changeLabel = changes.additions !== undefined || changes.deletions !== undefined
      ? ` +${changes.additions ?? 0} / -${changes.deletions ?? 0}`
      : ' 已更新';
    // The concrete diff is the payload of an edit receipt; keep it one level
    // below the +/- statistics instead of hiding it behind raw JSON.
    const diffBody = diff ? publicToolOutputText(diff) : '';
    const diffOutput = diffBody
      ? { text: diffBody, truncated: publicToolOutputWasTruncated(diff) }
      : output;
    return {
      file,
      ...(filePath ? { filePath } : {}),
      request,
      ...(diffOutput ? { output: diffOutput, outputLabel: '变更差异' } : {}),
      summary: file ? `${file}${changeLabel}` : '文件已更新',
      ...changes,
    };
  }
  if (['read', 'read_file', 'workspace_read'].includes(toolId)) {
    const truncation = firstRecord([envelope, carrier], ['truncation']);
    const totalLines = firstFiniteNumber([truncation], ['totalLines']);
    const lines = totalLines ?? publicLineCount(publicToolContentText(carrier));
    return {
      file,
      ...(filePath ? { filePath } : {}),
      request,
      ...(output ? { output } : {}),
      ...(lines !== undefined ? { lines } : {}),
      summary: file ? `${file}${lines !== undefined ? ` · ${lines} 行` : ' 已读取'}` : '文件已读取',
    };
  }
  if (searchTools.has(toolId)) {
    const needle = pattern || query;
    return {
      file,
      ...(filePath ? { filePath } : {}),
      request,
      ...(output ? { output } : {}),
      summary: needle
        ? `在 ${file || '工作区'} 搜索 “${needle.slice(0, 100)}”`
        : '搜索项目内容',
    };
  }
  if (listTools.has(toolId)) {
    const needle = pattern || query;
    return {
      file,
      ...(filePath ? { filePath } : {}),
      request,
      ...(output ? { output } : {}),
      summary: toolId === 'ls' || toolId === 'workspace_list'
        ? `列出 ${file || '工作区'}`
        : needle
          ? `查找 “${needle.slice(0, 100)}”`
          : '查找项目文件',
    };
  }
  if (commandTools.has(toolId)) {
    const firstLine = command.split('\n', 1)[0]?.slice(0, 140) ?? '';
    return {
      file,
      ...(filePath ? { filePath } : {}),
      request,
      ...(output ? { output } : {}),
      summary: firstLine ? `运行 ${firstLine}` : '运行项目命令',
    };
  }
  return { file: '', request: [], summary: '' };
}

function managedEvidencePreview(value: string): {
  request: Record<string, unknown>;
  summary: string;
  truncated: boolean;
} | undefined {
  const normalized = value.trim();
  if (!normalized.startsWith('{') || !normalized.includes('"evidenceHandle"')) return undefined;
  let envelope: Record<string, unknown>;
  try {
    envelope = record(JSON.parse(normalized));
  } catch {
    // Older durable events may contain a generic preview truncated in the
    // middle of the evidence JSON. Never expose that internal envelope; the
    // backend will rebuild a semantic receipt after the next snapshot.
    return {
      request: {},
      summary: '受管工具结果已安全保存；刷新对话后可查看语义摘要。',
      truncated: true,
    };
  }
  if (!text(envelope.evidenceHandle)) return undefined;
  const rawSummary = text(envelope.evidenceSummary ?? envelope.previewHead);
  const summary = publicToolOutputText(rawSummary)
    || '受管工具结果已安全保存。';
  const evidenceBytes = finiteNumber(envelope.evidenceBytes) ?? 0;
  return {
    request: record(envelope.evidenceRequest),
    summary,
    truncated: (
      rawSummary.length > summary.length
      || evidenceBytes > rawSummary.length
      || Boolean(envelope.continuation)
    ),
  };
}

export function publicToolOutputText(value: string): string {
  const redacted = value
    .replace(/\r\n?/gu, '\n')
    .replace(/\bsk-[A-Za-z0-9_-]{6,}\b/gu, '[REDACTED_SECRET]')
    .replace(/\b([A-Za-z0-9_]*(?:api[_-]?key|access[_-]?token|password|secret|authorization))(\s*(?:=|:)\s*)([^\s;'"\\]+|"[^"]*"|'[^']*')/giu, '$1$2[REDACTED_SECRET]')
    .replace(/(--(?:api[_-]?key|token|password|secret)\s+)([^\s;'"\\]+|"[^"]*"|'[^']*')/giu, '$1[REDACTED_SECRET]')
    .replace(/\/Users\/[^/\s]+\//gu, '~/')
    .replace(/\/Volumes\/[^/]+\//gu, '/…/')
    .replace(/\/private\/var\//gu, '/…/var/')
    .replace(/\/var\/folders\//gu, '/…/var/folders/');
  return redacted
    .split('\n')
    .slice(0, PUBLIC_TOOL_OUTPUT_MAX_LINES)
    .join('\n')
    .slice(0, PUBLIC_TOOL_OUTPUT_MAX_CHARS)
    .trim();
}

function publicToolOutputWasTruncated(value: string): boolean {
  const normalized = value.replace(/\r\n?/gu, '\n').trim();
  return (
    normalized.length > PUBLIC_TOOL_OUTPUT_MAX_CHARS
    || normalized.split('\n').length > PUBLIC_TOOL_OUTPUT_MAX_LINES
  );
}

function publicWorkspacePath(value: string): string {
  const normalized = value.replace(/\\/gu, '/').replace(/\/{2,}/gu, '/').trim();
  if (!normalized || normalized.length > 1_000) return '';
  if (/(?:api.?key|authorization|cookie|password|secret|bearer\s)/iu.test(normalized)) return '';
  const parts = normalized.split('/').filter(Boolean);
  if (parts.length === 0) return '';
  const absolute = normalized.startsWith('/') || normalized.startsWith('~/') || /^[a-z]:\//iu.test(normalized);
  const unsafeRelative = parts.some((part) => part === '..' || part === '.');
  const serverRedacted = normalized.startsWith('…/');
  const clientRedacted = normalized.includes('[REDACTED_PATH]');
  const candidate = serverRedacted
    ? normalized
    : absolute || unsafeRelative || clientRedacted
      ? parts.at(-1) ?? ''
      : parts.join('/');
  if (!candidate || candidate.length > 240 || /[\u0000-\u001f]/u.test(candidate)) return '';
  return candidate;
}

/**
 * A Files deep link needs the actual workspace-relative path. Absolute paths,
 * server-redacted paths, and traversal candidates intentionally remain display
 * only: their basename is useful in a receipt but is not a trustworthy target.
 */
function publicRelativeWorkspacePath(value: string): string {
  const normalized = value.replace(/\\/gu, '/').replace(/\/{2,}/gu, '/').trim();
  if (
    !normalized
    || normalized.startsWith('/')
    || normalized.startsWith('~/')
    || normalized.startsWith('…/')
    || /^[a-z]:\//iu.test(normalized)
    || normalized.includes('[REDACTED_PATH]')
  ) return '';
  const parts = normalized.split('/').filter(Boolean);
  if (parts.some((part) => part === '.' || part === '..')) return '';
  return publicWorkspacePath(normalized);
}

function publicLineCount(value: string): number | undefined {
  if (!value) return undefined;
  const lines = value.replace(/\r\n?/gu, '\n').split('\n');
  while (lines.length > 0 && lines.at(-1) === '') lines.pop();
  return Math.max(lines.length, 1);
}

function publicDiffCounts(value: string): Pick<PublicCodeToolResult, 'additions' | 'deletions'> {
  if (!value) return {};
  let additions = 0;
  let deletions = 0;
  for (const line of value.replace(/\r\n?/gu, '\n').split('\n').slice(0, 20_000)) {
    if (line.startsWith('+') && !line.startsWith('+++')) additions += 1;
    if (line.startsWith('-') && !line.startsWith('---')) deletions += 1;
  }
  return additions || deletions ? { additions, deletions } : {};
}

function publicToolContentText(carrier: Record<string, unknown>): string {
  const content = Array.isArray(carrier.content) ? carrier.content : [];
  return content
    .slice(0, 12)
    .map((item) => text(record(item).text))
    .filter(Boolean)
    .join('\n');
}

function publicStructuredText(value: string): string | undefined {
  let normalized = value.replace(/\s+/gu, ' ').trim();
  if (!normalized) return undefined;
  if (/^(?:\{|\[)/u.test(normalized)) return undefined;
  if (/(?:api.?key|authorization|cookie|password|secret|bearer\s|chain[- ]?of[- ]?thought|private reasoning|思维链)/iu.test(normalized)) return undefined;
  if (/(?:file:\/\/|\/Users\/|\/Volumes\/|\/private\/var\/|\/var\/folders\/)/u.test(normalized)) return undefined;
  normalized = normalized
    .replace(/\[REDACTED_SECRET\]/gu, '已隐藏敏感值')
    .replace(/\[REDACTED_PATH\]/gu, '已隐藏本机路径');
  return normalized.slice(0, 500);
}

function publicDisplayText(value: unknown, fallback: string): string {
  return publicStructuredText(text(value))?.slice(0, 180) || fallback;
}

function publicLongText(value: unknown): string {
  return publicStructuredText(text(value)) ?? '';
}

function firstStringArray(layers: Record<string, unknown>[], keys: string[]): string[] {
  return firstArray(layers, keys)
    .map((value) => publicDisplayText(value, ''))
    .filter(Boolean);
}

function memoryTypeLabel(value: string): string {
  return ({
    principle: '原则',
    fact: '事实',
    preference: '偏好',
    decision: '决定',
    event: '事件',
    note: '记录',
  } as Record<string, string>)[value.toLowerCase()] ?? '记忆';
}

function memoryCatalogKindLabel(value: string): string {
  return ({
    atom: '当前事实',
    memory_atom: '当前事实',
    current_fact: '当前事实',
    fact: '当前事实',
    book: '主题书',
    memory_book: '主题书',
    topic: '主题书',
    timeline: '活动时间线',
    daily_timeline: '活动时间线',
    activity_timeline: '活动时间线',
    evidence: '原始证据',
    event: '原始证据',
    role_book: '角色书',
    role_book_revision: '角色书',
    group: '分组',
    tag: '标签',
  } as Record<string, string>)[value.toLowerCase()] ?? '条目';
}

function memoryLayerForKind(value: string): string {
  return ({
    atom: 'atoms',
    memory_atom: 'atoms',
    current_fact: 'atoms',
    fact: 'atoms',
    book: 'books',
    memory_book: 'books',
    topic: 'books',
    timeline: 'timelines',
    daily_timeline: 'timelines',
    activity_timeline: 'timelines',
    evidence: 'evidence',
    event: 'evidence',
    role_book: 'role-books',
    role_book_revision: 'role-books',
  } as Record<string, string>)[value.toLowerCase()] ?? '';
}

function memoryReferenceKind(
  item: Record<string, unknown>,
  ref: Record<string, unknown>,
): string {
  return text(
    ref.referenceKind
    ?? ref.kind
    ?? ref.type
    ?? item.referenceKind
    ?? item.kind
    ?? item.type
    ?? item.docType,
  ).toLowerCase();
}

function memoryReferenceId(
  item: Record<string, unknown>,
  ref: Record<string, unknown>,
): string {
  return text(
    ref.referenceId
    ?? ref.id
    ?? item.referenceId
    ?? item.id
    ?? item.sourceId,
  );
}

function semanticPreviewKind(value: string): PublicToolSemanticPreview['kind'] {
  const layer = memoryLayerForKind(value);
  if (layer === 'atoms') return 'atom';
  if (layer === 'books') return 'book';
  if (layer === 'timelines') return 'timeline';
  if (layer === 'evidence') return 'evidence';
  return 'collection';
}

function publicToolError(layers: Record<string, unknown>[], carrier: Record<string, unknown>): string {
  for (const layer of layers) {
    for (const key of ['error', 'errorMessage', 'message', 'summary']) {
      const value = publicStructuredText(text(layer[key]));
      if (value) return value;
    }
  }
  const content = Array.isArray(carrier.content) ? carrier.content : [];
  for (const item of content.slice(0, 4)) {
    const value = publicStructuredText(text(record(item).text));
    if (value) return value;
  }
  return '工具执行失败，但没有返回可公开展示的错误明细。';
}

function publicToolRecovery(
  error: string,
  payload: Record<string, unknown>,
): 'approval' | 'permission' | undefined {
  if (approvalNeedsHumanDecision(payload) && text(payload.approvalId) && text(payload.payloadSha256)) return 'approval';
  if (/(?:approval required|requires approval|pending approval|需要(?:本机)?(?:审批|批准)|等待(?:审批|批准)|审批后|需(?:要)?本机确认)/iu.test(error)) {
    return 'permission';
  }
  if (/(?:permission denied|access denied|not allowed|allowlist|sandbox|权限不足|没有权限|未授权|授权目录|只读模式|运行协调)/iu.test(error)) {
    return 'permission';
  }
  return undefined;
}

function safeKnowledgeSources(items: unknown[]): {
  labels: string[];
  links: Array<{ label: string; href: string }>;
} {
  const labels: string[] = [];
  const links: Array<{ label: string; href: string }> = [];
  for (const value of items) {
    const item = record(value);
    const fileName = publicFileName(item.fileName ?? item.name ?? item.title);
    if (!fileName) continue;
    const citation = record(item.citation);
    const page = finiteNumber(citation.page);
    const startLine = finiteNumber(citation.startLine ?? item.startLine);
    const endLine = finiteNumber(citation.endLine ?? item.endLine);
    const location = page !== undefined && page > 0
      ? `第 ${page} 页`
      : startLine !== undefined && startLine > 0
        ? `${startLine}${endLine !== undefined && endLine > startLine ? `-${endLine}` : ''} 行`
        : '';
    const label = location ? `${fileName} · ${location}` : fileName;
    if (!labels.includes(label)) {
      labels.push(label);
      const baseId = safeKnowledgeRouteId(item.kbId ?? item.baseId ?? citation.kbId ?? citation.baseId);
      const documentId = safeKnowledgeRouteId(
        item.fileId ?? item.documentId ?? citation.fileId ?? citation.documentId,
      );
      if (documentId) {
        const base = baseId ? `base=${encodeURIComponent(baseId)}&` : '';
        links.push({
          label,
          href: `#/knowledge?${base}document=${encodeURIComponent(documentId)}&tab=viewer`,
        });
      }
    }
    if (labels.length >= 8) break;
  }
  return { labels, links };
}

function safeKnowledgeRouteId(value: unknown): string {
  const normalized = text(value);
  if (!normalized || normalized.length > 240 || /[\\/\u0000-\u001f]/u.test(normalized)) return '';
  if (/(?:api.?key|authorization|cookie|password|secret|bearer\s)/iu.test(normalized)) return '';
  return normalized;
}

function publicFileName(value: unknown): string {
  const normalized = text(value).replace(/\s+/gu, ' ').trim();
  if (!normalized || normalized.length > 240) return '';
  if (/[\\/]/u.test(normalized)) return '';
  if (/(?:api.?key|authorization|cookie|password|secret|bearer\s)/iu.test(normalized)) return '';
  return normalized;
}

export function safeSourceLabels(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const labels: string[] = [];
  for (const item of value) {
    const candidate = typeof item === 'string'
      ? publicText(item)
      : publicText(record(item).title ?? record(item).name ?? record(item).label);
    if (candidate && !labels.includes(candidate)) labels.push(candidate);
    if (labels.length >= 8) break;
  }
  return labels;
}

function capabilityLabel(value: unknown): string {
  const item = record(value);
  const id = text(item.id);
  return publicToolName(id, publicText(item.displayName ?? item.label));
}

function safeActivitySourceCounts(items: unknown[]): string {
  const labels: Record<string, string> = {
    voice: '语音',
    rime: '输入法',
    keyboard: '键盘',
    import: '导入',
  };
  const counts = new Map<string, number>();
  for (const item of items) {
    const source = text(record(item).source).toLowerCase();
    const label = labels[source];
    if (!label) continue;
    counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  return [...counts.entries()].map(([label, count]) => `${label} ${count} 条`).join('、');
}

function boundedList(values: string[], limit: number): string {
  const unique = [...new Set(values)];
  const visible = unique.slice(0, limit);
  return `${visible.join('、')}${unique.length > visible.length ? `，另 ${unique.length - visible.length} 项` : ''}`;
}

function activityStatusLabel(status: PublicToolActivityProjection['status']): string {
  switch (status) {
    case 'running': return '进行中';
    case 'waiting': return '等待确认';
    case 'failed': return '失败';
    case 'aborted': return '已停止';
    case 'completed': return '已完成';
  }
}

function publicStatusLabel(value: string): string {
  return ({
    online: '在线',
    offline: '离线',
    ready: '可用',
    running: '运行中',
    healthy: '正常',
    available: '可用',
    connected: '已连接',
    disabled: '未启用',
    unavailable: '不可用',
    degraded: '部分可用',
    failed: '失败',
    completed: '已完成',
    pending: '等待处理',
  } as Record<string, string>)[value.toLowerCase()] ?? publicText(value);
}

function firstRecord(layers: Record<string, unknown>[], keys: string[]): Record<string, unknown> {
  for (const layer of layers) {
    for (const key of keys) {
      if (!Object.hasOwn(layer, key)) continue;
      const value = record(layer[key]);
      if (Object.keys(value).length > 0) return value;
    }
  }
  return {};
}

function firstArray(layers: Record<string, unknown>[], keys: string[]): unknown[] {
  for (const layer of layers) {
    for (const key of keys) {
      if (Object.hasOwn(layer, key) && Array.isArray(layer[key])) return layer[key] as unknown[];
    }
  }
  return [];
}

function firstText(layers: Record<string, unknown>[], keys: string[]): string {
  for (const layer of layers) {
    for (const key of keys) {
      const value = text(layer[key]);
      if (value) return value;
    }
  }
  return '';
}

function firstPublicText(layers: Record<string, unknown>[], keys: string[]): string {
  for (const layer of layers) {
    for (const key of keys) {
      const value = publicText(layer[key]);
      if (value) return value;
    }
  }
  return '';
}

function firstBoolean(layers: Record<string, unknown>[], keys: string[]): boolean | undefined {
  for (const layer of layers) {
    for (const key of keys) {
      const value = layer[key];
      if (typeof value === 'boolean') return value;
    }
  }
  return undefined;
}

function firstFiniteNumber(layers: Record<string, unknown>[], keys: string[]): number | undefined {
  for (const layer of layers) {
    for (const key of keys) {
      const value = finiteNumber(layer[key]);
      if (value !== undefined) return value;
    }
  }
  return undefined;
}

function finiteNumber(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : undefined;
}

function publicText(value: unknown): string {
  const normalized = text(value).replace(/\s+/gu, ' ').trim();
  if (!normalized || normalized.length > 500) return '';
  if (/^(?:\{|\[)/u.test(normalized)) return '';
  if (/(?:\[REDACTED_|api.?key|authorization|cookie|password|secret|bearer\s|chain[- ]?of[- ]?thought|private reasoning|思维链)/iu.test(normalized)) return '';
  if (/(?:file:\/\/|\/Users\/|\/Volumes\/|\/private\/var\/|\/var\/folders\/)/u.test(normalized)) return '';
  if (/^[a-z][a-z0-9_.:/-]*$/iu.test(normalized)) return '';
  return normalized.slice(0, 240);
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}
