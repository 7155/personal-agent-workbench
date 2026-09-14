import {
  Bot,
  BookOpenText,
  Check,
  ChevronRight,
  CircleDashed,
  Code2,
  FileText,
  FolderKanban,
  Gauge,
  ExternalLink,
  ListChecks,
  LoaderCircle,
  MessagesSquare,
  Paperclip,
  PanelRightClose,
  Plus,
  Radar,
  Search,
  Sparkles,
  SquareTerminal,
  TriangleAlert,
  Wrench,
  type LucideIcon,
} from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { forwardRef, useEffect, useId, useMemo, useState, type CSSProperties, type ReactNode } from 'react';
import { useControlTransport } from '@/app/control-transport';
import {
  Button,
  Disclosure,
  IconButton,
} from '@/components/primitives';
import type { AgentActivityProjection, AgentProjectionState, AgentTodoProjection, AgentTurnStatus } from '@/contracts/agent-reducer';
import type { AgentSubagentRunV1 } from '@/contracts/generated/agent-subagent-run.v1';
import type { AgentBackgroundJobV1 } from '@/contracts/generated/agent-background-job.v1';
import type { AgentWorkflowStateV1 } from '@/contracts/generated/agent-workflow-state.v1';
import type {
  CapabilityCatalog,
  CapabilityMutationOutcome,
  CapabilityPreference,
} from '@/features/plugins/capability-policy';
import { usePageVisibility } from '@/platform/use-page-visibility';
import { useAgentLiveStore } from '../state/live-store';
import type { AgentCommand, ToolManifest } from '../types';
import { publicToolResultView } from '../timeline/public-tool-result';
import { SubagentLaunchPanel } from '../delegation/SubagentLaunchPanel';
import type { SessionSummary } from '../types';
import { ContextRuntimeSections } from './ContextRuntimePanel';
import { uniqueToolActivities } from '../tool-count';
import { AgentBackgroundJobsView } from './AgentBackgroundJobsView';
import { AgentWorkflowPanel } from './AgentWorkflowPanel';
import { ContextXraySections } from './ContextXrayPanel';
import { CapabilitySessionView } from './CapabilitySessionView';
import { WorkspaceLspStatusView } from './WorkspaceLspStatusView';
import { SubagentConsoleDialog } from './SubagentConsole';
import {
  hasActiveSubagentRuns,
  subagentRuns,
  subagentTree,
  type SubagentTreeNode,
} from './subagent-data';
import {
  INVALID_SUBAGENT_CONTRACT_NOTICE,
  isContractInvalid,
  isUnverifiedReturn,
  subagentPresentationState,
  subagentStateLabel,
  subagentTemplateLabel,
  UNVERIFIED_SUBAGENT_NOTICE,
} from './subagent-presentation';

type IdleWindow = Window & typeof globalThis & {
  requestIdleCallback?: (callback: () => void, options?: { timeout: number }) => number;
  cancelIdleCallback?: (handle: number) => void;
};

export const AgentStatusPanel = forwardRef<HTMLElement, {
  sessionId: string;
  session?: SessionSummary;
  open: boolean;
  surfaceActive?: boolean;
  keepContentMounted?: boolean;
  minimal?: boolean;
  modal?: boolean;
  onClose: () => void;
  commands: AgentCommand[];
  tools: ToolManifest[];
  toolCatalogStatus: 'loading' | 'ready' | 'failed';
  capabilityCatalog?: CapabilityCatalog;
  capabilityCatalogError?: string;
  capabilityPolicyMutation?: CapabilityMutationOutcome;
  busy: boolean;
  contextSnapshotState?: 'restoring' | 'partial';
  onCapabilityPreferenceChange: (canonicalId: string, preference: CapabilityPreference) => void;
  onCapabilityPolicyRetry: () => void;
  onCapabilityCatalogRetry: () => void;
  onOpenBackgroundJob?: (job: AgentBackgroundJobV1) => void;
}>(function AgentStatusPanel({
  sessionId,
  session,
  open,
  surfaceActive = true,
  keepContentMounted = false,
  minimal = false,
  modal = false,
  onClose,
  tools,
  toolCatalogStatus,
  capabilityCatalog,
  capabilityCatalogError,
  capabilityPolicyMutation,
  busy,
  contextSnapshotState,
  onCapabilityPreferenceChange,
  onCapabilityPolicyRetry,
  onCapabilityCatalogRetry,
  onOpenBackgroundJob,
}, ref) {
  const transport = useControlTransport();
  const contentReady = useDeferredStatusContent(open, keepContentMounted);
  const pageVisible = usePageVisibility();
  const projection = useAgentLiveStore((state) => state.projections[sessionId]);
  const view = useMemo(() => projectStatusPanel(projection), [projection]);
  const logicalTools = useMemo(() => groupToolActivities(view.tools), [view.tools]);
  const [resolvedWorkflow, setResolvedWorkflow] = useState<AgentWorkflowStateV1>();
  const resolvedTodo = resolvedWorkflow?.sessionId === sessionId
    ? resolvedWorkflow.todo
    : undefined;
  const panelStatus = contextSnapshotState === 'restoring'
    ? '正在恢复上下文'
    : contextSnapshotState === 'partial'
      ? '仅显示最近上下文'
      : statusPanelLabel(projection, view, resolvedTodo);
  const backgroundJobs = useMemo(
    () => (projection?.backgroundJobOrder ?? [])
      .map((jobId) => projection?.backgroundJobsById[jobId])
      .filter((job) => job !== undefined),
    [projection],
  );
  const lifecycleCancellationAudits = useMemo(
    () => (projection?.lifecycleCancellationAuditOrder ?? [])
      .map((requestId) => projection?.lifecycleCancellationAuditsById?.[requestId])
      .filter((audit) => audit !== undefined),
    [projection],
  );
  const subagents = useQuery({
    queryKey: ['agent', 'status-panel', 'subagents', sessionId],
    queryFn: ({ signal }) => transport.request({
      pathId: 'agent.subagents.list',
      query: { sessionId, limit: 50 },
      signal,
    }),
    enabled: !minimal && surfaceActive && open && contentReady && pageVisible && Boolean(sessionId),
    refetchInterval: !minimal && surfaceActive && open && contentReady && pageVisible
      ? (query) => hasActiveSubagentRuns(subagentRuns(query.state.data)) ? 1_000 : 5_000
      : false,
    retry: false,
  });
  const runs = useMemo(() => subagentRuns(subagents.data), [subagents.data]);
  const runTree = useMemo(() => subagentTree(subagents.data), [subagents.data]);

  return (
    <aside
      id="agent-status-panel"
      ref={ref}
      className="agent-status-panel"
      data-open={open}
      aria-hidden={!open}
      aria-label="当前对话任务中心"
      aria-modal={modal || undefined}
      data-minimal={minimal || undefined}
      inert={open ? undefined : true}
      role={modal ? 'dialog' : undefined}
      tabIndex={-1}
    >
      <header>
        <span><strong>任务中心</strong><small>{panelStatus}</small></span>
        <IconButton icon={<PanelRightClose size={16} />} label="收起任务中心" onClick={onClose} tooltip />
      </header>
      {contentReady ? <div className="agent-status-panel__body">
        <AgentWorkflowPanel
          active={surfaceActive}
          sessionId={sessionId}
          fallbackTodo={projection?.todo}
          fallbackGoal={projection?.goal}
          fallbackActGate={projection?.actGate}
          compactEmpty={minimal}
          onWorkflowResolved={setResolvedWorkflow}
        />
        {lifecycleCancellationAudits.length ? (
          <StatusSection icon={CircleDashed} title="取消与暂停回执" count={lifecycleCancellationAudits.length} defaultOpen={!minimal}>
            <LifecycleCancellationView audits={lifecycleCancellationAudits} />
          </StatusSection>
        ) : null}
        {view.turn ? (
          <>
            <div className="agent-status-turn agent-todo-turn-summary" data-state={view.turn.status}>
              <TurnStateIcon status={view.turn.status} />
              <span><strong>当前回合 · {turnStatusLabel(view.turn.status)}</strong><small>{turnProgressLabel(view, logicalTools)}</small></span>
            </div>
            {view.tasks.length ? <CurrentTurnTaskPlan tasks={view.tasks} /> : null}
          </>
        ) : !projection ? (
          <StatusSection icon={ListChecks} title="执行进度" count={view.tasks.length} defaultOpen={!minimal}>
            <EmptyLine>还没有可展示的回合状态</EmptyLine>
          </StatusSection>
        ) : null}

        <StatusSection icon={SquareTerminal} title="后台任务" count={backgroundJobs.length} defaultOpen={!minimal}>
          <AgentBackgroundJobsView
            active={surfaceActive}
            sessionId={sessionId}
            jobs={backgroundJobs}
            onOpenJob={onOpenBackgroundJob}
          />
        </StatusSection>

        <StatusSection icon={MessagesSquare} title="消息队列" count={(projection?.messageQueue.steering.length ?? 0) + (projection?.messageQueue.followUp.length ?? 0)} defaultOpen={!minimal}>
          <MessageQueueView projection={projection} />
        </StatusSection>

        <StatusSection icon={Gauge} title="上下文与用量" count={projection?.telemetry?.compactionCount ?? 0} defaultOpen={!minimal}>
          <SessionTelemetryView projection={projection} />
        </StatusSection>
        <ContextXraySections sessionId={sessionId} open={open && surfaceActive} />

        <StatusSection
          icon={Sparkles}
          title="可用能力目录 · 当前对话工具与技能（未注入上下文）"
          count={capabilityCatalog?.items.length ?? 0}
          defaultOpen={false}
        >
          <CapabilitySessionView
            busy={busy}
            catalog={capabilityCatalog}
            error={capabilityCatalogError}
            mutation={capabilityPolicyMutation}
            status={toolCatalogStatus}
            onPreferenceChange={onCapabilityPreferenceChange}
            onRetryCatalog={onCapabilityCatalogRetry}
            onRetryMutation={onCapabilityPolicyRetry}
          />
        </StatusSection>

        <StatusSection
          icon={Code2}
          title="代码智能"
          count={tools.some((tool) => tool.id === 'workspace_lsp') ? 1 : 0}
          defaultOpen={false}
        >
          <WorkspaceLspStatusView
            capabilityCatalog={capabilityCatalog}
            catalogStatus={toolCatalogStatus}
            onRefresh={onCapabilityCatalogRetry}
            projection={projection}
            tools={tools}
          />
        </StatusSection>

        <StatusSection icon={Wrench} title="关键步骤" count={logicalTools.length} defaultOpen={!minimal}>
          {logicalTools.length ? (
            <div className="agent-status-tools">
              {logicalTools.map((item) => item.kind === 'attempts'
                ? <ToolAttemptGroup key={item.activities[0]!.id} activities={item.activities} />
                : <ToolStep key={item.activities[0]!.id} activity={item.activities[0]!} />)}
            </div>
          ) : <EmptyLine>本轮还没有工具步骤</EmptyLine>}
        </StatusSection>

        <StatusSection icon={Paperclip} title="附件与文件" count={view.files.length + view.attachmentCount} defaultOpen={!minimal}>
          {view.files.length || view.attachmentCount ? (
            <div className="agent-status-files">
              {view.attachmentCount ? <StatusRow icon={Paperclip} title={`${view.attachmentCount} 个受管附件`} detail="随会话消息保存" /> : null}
              {view.files.map((file) => <StatusRow key={file.id} icon={FileText} title={file.name} detail={file.kind} />)}
            </div>
          ) : <EmptyLine>当前会话没有附件或文件</EmptyLine>}
        </StatusSection>

        <StatusSection icon={FolderKanban} title="产物" count={view.artifacts.length + runs.filter((run) => run.artifact).length} defaultOpen={!minimal}>
          {view.artifacts.length || runs.some((run) => run.artifact) ? (
            <div className="agent-status-files">
              {view.artifacts.map((artifact) => <StatusRow key={artifact.id} icon={FolderKanban} title={artifact.name} detail={artifact.kind} />)}
              {runs.filter((run) => run.artifact).map((run) => <StatusRow key={`artifact:${run.id}`} icon={FolderKanban} title={`${subagentTemplateLabel(run.templateId)}协作产物`} detail={subagentStateLabel(run, 'result')} />)}
            </div>
          ) : <EmptyLine>本轮还没有可交付产物</EmptyLine>}
        </StatusSection>

        <StatusSection icon={Plus} title="启动子 Agent" count={1} defaultOpen={false}>
          <SubagentLaunchPanel
            availableTools={tools}
            parents={[{
              sessionId,
              label: session?.title || '当前 Session',
              detail: session?.mode === 'coordinator' ? '主持 Session' : '助手 Session',
              canControl: session?.canControl,
              canWrite: session?.mode === 'coordinator'
                && session.canControl !== false
                && session.executionMode !== 'read_only'
                && Boolean(session.workspaceRoots?.length),
              workspaceRoots: session?.workspaceRoots ?? [],
              piSkillsEnabled: session?.piSkillsEnabled,
              codexSkillsEnabled: session?.codexSkillsEnabled,
            }]}
          />
        </StatusSection>

        <StatusSection icon={Bot} title="子 Agent 运行树" count={runs.length} defaultOpen={!minimal}>
          {subagents.isPending ? <EmptyLine animated>正在读取协作状态</EmptyLine> : null}
          {subagents.error ? (
            <div className="agent-status-query-error" role="alert">
              <span>
                <strong>子智能体状态读取失败</strong>
                <small>当前列表没有被当作空结果；重新读取只会刷新这段对话的委派状态。</small>
              </span>
              <Button
                size="small"
                loading={subagents.isFetching}
                onClick={() => void subagents.refetch()}
              >
                重新读取子智能体
              </Button>
            </div>
          ) : null}
          {!subagents.isPending && !subagents.error && runs.length === 0 ? <EmptyLine>当前会话没有委派任务</EmptyLine> : null}
          {runs.length ? (
            <div className="agent-status-subagents">
              <SubagentTreeRows
                nodes={runTree.roots.length
                  ? runTree.roots
                  : runs.map((run) => ({ run, children: [] }))}
                sessionId={sessionId}
              />
            </div>
          ) : null}
        </StatusSection>
        <ContextRuntimeSections sessionId={sessionId} open={open && surfaceActive} />

        <a
          className="agent-status-observation-link"
          href={`#/observability?sessionId=${encodeURIComponent(sessionId)}`}
        >
          <Radar size={16} />
          <span><strong>运行记录</strong><small>回看这次对话用过的工具、检索和记忆</small></span>
          <ChevronRight size={15} />
        </a>
      </div> : <div aria-hidden="true" className="agent-status-panel__body agent-status-panel__body--pending" />}
    </aside>
  );
});

function useDeferredStatusContent(open: boolean, keepContentMounted: boolean): boolean {
  const [ready, setReady] = useState(false);
  useEffect(() => {
    if (!open) {
      if (!keepContentMounted) setReady(false);
      return;
    }
    const idleWindow = window as IdleWindow;
    let secondFrame = 0;
    let idleHandle = 0;
    const firstFrame = window.requestAnimationFrame(() => {
      secondFrame = window.requestAnimationFrame(() => {
        if (idleWindow.requestIdleCallback) {
          idleHandle = idleWindow.requestIdleCallback(() => setReady(true), { timeout: 120 });
        } else {
          idleHandle = window.setTimeout(() => setReady(true), 0);
        }
      });
    });
    return () => {
      window.cancelAnimationFrame(firstFrame);
      if (secondFrame) window.cancelAnimationFrame(secondFrame);
      if (idleHandle) {
        if (idleWindow.cancelIdleCallback) idleWindow.cancelIdleCallback(idleHandle);
        else window.clearTimeout(idleHandle);
      }
    };
  }, [keepContentMounted, open]);
  return ready;
}

function SessionTelemetryView({ projection }: { projection?: AgentProjectionState }) {
  const telemetry = projection?.telemetry;
  if (!telemetry) {
    const recovered = persistedMessageUsage(projection);
    if (!recovered) return <EmptyLine>发送一轮消息后显示上下文与缓存数据</EmptyLine>;
    const promptTokens = recovered.input + recovered.cacheRead + recovered.cacheWrite;
    const cachePercent = promptTokens > 0
      ? Math.round((recovered.cacheRead / promptTokens) * 100)
      : 0;
    return (
      <div className="agent-session-telemetry" data-source="persisted-messages">
        <div className="agent-session-telemetry__model">
          <span><strong>可见回合用量</strong><small>{[recovered.model, recovered.provider].filter(Boolean).join(' · ')}</small></span>
        </div>
        <div className="agent-session-telemetry__usage" aria-label="已持久化回合 Token 用量">
          <span><small>输入</small><strong>{formatTokenCount(promptTokens)}</strong></span>
          <span><small>输出</small><strong>{formatTokenCount(recovered.output)}</strong></span>
          <span><small>缓存</small><strong data-cache={cachePercent > 0 || undefined}>{cachePercent}%</strong></span>
        </div>
        <div className="agent-session-telemetry__context">
          <div><span><strong>上下文</strong><small>精确占用不可用</small></span><b>—</b></div>
          <p>当前 Provider 上下文精确占用未保存</p>
          <p>这里只合计已持久化助手消息的 Token 用量；下一轮响应会恢复 Runtime 精确计量。</p>
        </div>
      </div>
    );
  }
  const context = telemetry.context;
  const cumulative = telemetry.cumulativeUsage;
  const promptTokens = cumulative.input + cumulative.cacheRead + cumulative.cacheWrite;
  const cachePercent = promptTokens > 0 ? Math.round((cumulative.cacheRead / promptTokens) * 100) : 0;
  const percent = context.percent === null ? null : Math.min(100, Math.max(0, context.percent));
  return (
    <div className="agent-session-telemetry" data-compacting={telemetry.isCompacting || undefined}>
      <div className="agent-session-telemetry__model">
        <span><strong>{telemetry.model.name || telemetry.model.id}</strong><small>{telemetry.model.provider}</small></span>
        {telemetry.isCompacting ? <i><LoaderCircle size={13} />压缩中</i> : null}
      </div>
      <div className="agent-session-telemetry__usage" aria-label="会话累计 Token 用量">
        <span><small>输入</small><strong>{formatTokenCount(promptTokens)}</strong></span>
        <span><small>输出</small><strong>{formatTokenCount(cumulative.output)}</strong></span>
        <span><small>缓存</small><strong data-cache={cachePercent > 0 || undefined}>{cachePercent}%</strong></span>
      </div>
      <div className="agent-session-telemetry__context">
        <div>
          <span><strong>上下文</strong><small>{percent === null ? '待下一轮校准' : `${formatTokenCount(context.tokens ?? 0)} / ${formatTokenCount(context.contextWindow)}`}</small></span>
          <b>{percent === null ? '—' : `${Math.round(percent)}%`}</b>
        </div>
        <div className="agent-session-telemetry__track" aria-hidden="true">
          <span style={{ '--agent-context-progress': (percent ?? 0) / 100 } as CSSProperties} />
          {context.contextWindow > 0 ? <i style={{ left: `${Math.min(100, (context.compactAtTokens / context.contextWindow) * 100)}%` }} /> : null}
        </div>
        <p>
          {context.tokensUntilCompact === null
            ? '刚完成压缩，下一轮模型响应后恢复精确计量'
            : context.autoCompactEnabled
              ? `距自动压缩约 ${formatTokenCount(context.tokensUntilCompact)}`
              : `剩余上下文 ${formatTokenCount(context.remainingTokens ?? 0)} · 自动压缩已关闭`}
        </p>
      </div>
      {telemetry.latestCompaction ? (
        <div className="agent-session-telemetry__compaction" data-state={telemetry.latestCompaction.status}>
          <LoaderCircle size={14} data-running={telemetry.latestCompaction.status === 'running' || undefined} />
          <span><strong>{telemetry.latestCompaction.status === 'running' ? '正在压缩' : `已压缩 ${telemetry.compactionCount} 次`}</strong><small>{compactionSummary(telemetry.latestCompaction)}</small></span>
        </div>
      ) : null}
    </div>
  );
}

function persistedMessageUsage(projection?: AgentProjectionState): {
  input: number;
  output: number;
  cacheRead: number;
  cacheWrite: number;
  model: string;
  provider: string;
} | undefined {
  const messages = (projection?.messageOrder ?? [])
    .map((messageId) => projection?.messagesById[messageId])
    .filter((message) => (
      message?.role === 'assistant'
      && message.usage !== undefined
      && message.usage.totalTokens > 0
    ));
  if (!messages.length) return undefined;
  const usage = messages.reduce(
    (total, message) => ({
      input: total.input + (message?.usage?.input ?? 0),
      output: total.output + (message?.usage?.output ?? 0),
      cacheRead: total.cacheRead + (message?.usage?.cacheRead ?? 0),
      cacheWrite: total.cacheWrite + (message?.usage?.cacheWrite ?? 0),
    }),
    { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  );
  const latest = messages.at(-1);
  return {
    ...usage,
    model: latest?.model ?? '',
    provider: latest?.provider ?? '',
  };
}

function MessageQueueView({ projection }: { projection?: AgentProjectionState }) {
  const steering = projection?.messageQueue.steering ?? [];
  const followUp = projection?.messageQueue.followUp ?? [];
  if (!steering.length && !followUp.length) return <EmptyLine>当前没有待处理消息</EmptyLine>;
  return (
    <ol className="agent-status-message-queue">
      {steering.map((message, index) => <li key={`steer:${index}:${message}`}><b>干预</b><span>{message}</span></li>)}
      {followUp.map((message, index) => <li key={`follow:${index}:${message}`}><b>接续</b><span>{message}</span></li>)}
    </ol>
  );
}

function compactionSummary(value: NonNullable<NonNullable<AgentProjectionState['telemetry']>['latestCompaction']>): string {
  if (value.status === 'running') return '正在生成精简摘要';
  if (value.tokensBefore && value.estimatedTokensAfter) {
    return `${formatTokenCount(value.tokensBefore)} → 约 ${formatTokenCount(value.estimatedTokensAfter)}`;
  }
  return value.status === 'failed' ? '压缩未完成' : '下一轮响应后校准占用';
}

function formatTokenCount(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value >= 10_000_000 ? 1 : 2)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(value >= 100_000 ? 0 : 1)}K`;
  return String(Math.max(0, Math.round(value)));
}


type LifecycleCancellationAudit = AgentProjectionState['lifecycleCancellationAuditsById'][string];

function LifecycleCancellationView({ audits }: { audits: LifecycleCancellationAudit[] }) {
  return (
    <ol className="agent-lifecycle-audits" aria-label="取消与暂停收束回执">
      {audits.map((audit) => (
        <li key={audit.requestId} data-state={audit.state}>
          <header>
            <span>
              <strong>目标{audit.action === 'pause' ? '暂停' : '取消'}</strong>
              <small>版本 {audit.sourceRevision} → {audit.transitionRevision} · {formatLifecycleTime(audit.updatedAtMs)}</small>
            </span>
            <i>{lifecycleStateLabel(audit.state)}</i>
          </header>
          {audit.reason ? <p>{audit.reason}</p> : null}
          <dl>
            {(Object.entries(audit.owners) as Array<[keyof LifecycleCancellationAudit['owners'], LifecycleCancellationAudit['owners'][keyof LifecycleCancellationAudit['owners']]]>).map(([owner, outcome]) => (
              <div key={owner}>
                <dt>{lifecycleOwnerLabel(owner)}</dt>
                <dd data-state={outcome.status}>
                  <strong>{lifecycleOwnerStatusLabel(outcome.status)}</strong>
                  <small>{lifecycleReceiptSummary(outcome.receipt)}</small>
                </dd>
              </div>
            ))}
          </dl>
        </li>
      ))}
    </ol>
  );
}

function lifecycleStateLabel(state: LifecycleCancellationAudit['state']): string {
  return {
    pending: '正在收束',
    completed: '已完成',
    partial: '部分完成',
    unknown: '需要核对',
  }[state];
}

function lifecycleOwnerStatusLabel(
  status: LifecycleCancellationAudit['owners']['runtime']['status'],
): string {
  return {
    pending: '等待回执',
    succeeded: '已收束',
    excluded: '不在此次范围',
    partial: '部分收束',
    unknown: '状态未知',
  }[status];
}

function lifecycleOwnerLabel(owner: keyof LifecycleCancellationAudit['owners']): string {
  return {
    runtime: 'Runtime',
    approval: '审批',
    job: '后台任务',
    delegation: '委派',
  }[owner];
}

function lifecycleReceiptSummary(receipt: Record<string, unknown>): string {
  const entries = Object.entries(receipt);
  if (!entries.length) return '无额外回执';
  const visible = entries.flatMap(([key, value]) => {
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
      return [`${key}: ${String(value).replace(/\s+/gu, ' ').slice(0, 80)}`];
    }
    if (Array.isArray(value)) return [`${key}: ${value.length} 项`];
    if (value && typeof value === 'object') return [`${key}: ${Object.keys(value).length} 项`];
    return [];
  });
  return visible.slice(0, 3).join(' · ') || `${entries.length} 项公开回执`;
}

function formatLifecycleTime(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '时间未记录';
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value));
}

function StatusSection({
  icon: Icon,
  title,
  count,
  children,
  defaultOpen = true,
}: {
  icon: LucideIcon;
  title: string;
  count: number;
  children: ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const contentId = useId();
  return (
    <section className="agent-status-section" data-open={open} data-status-title={title}>
      <header>
        <button aria-controls={contentId} aria-expanded={open} onClick={() => setOpen((value) => !value)} type="button">
          <Icon size={15} />
          <strong>{title}</strong>
          {count > 0 ? <span>{count}</span> : null}
          <ChevronRight className="agent-status-section__chevron" size={14} />
        </button>
      </header>
      <div aria-hidden={!open} className="agent-status-section__content" id={contentId} inert={!open ? true : undefined}>
        <div>{open ? children : null}</div>
      </div>
    </section>
  );
}

const TASK_PLAN_PAGE_SIZE = 6;

/**
 * A turn can carry a long structured plan. Keep the initial panel scannable,
 * while retaining every public task behind an explicit, reversible disclosure.
 */
export function CurrentTurnTaskPlan({
  tasks,
}: {
  tasks: StatusPanelProjection['tasks'];
}) {
  const [visibleCount, setVisibleCount] = useState(TASK_PLAN_PAGE_SIZE);
  const visibleTasks = tasks.slice(0, visibleCount);
  const remaining = Math.max(0, tasks.length - visibleTasks.length);
  return (
    <Disclosure
      className="agent-status-task-plan"
      contentClassName="agent-status-task-plan__content"
      defaultOpen
      summary={<>
        <span className="agent-status-task-plan__icon"><ListChecks size={14} /></span>
        <span><strong>本轮计划</strong><small>当前 {visibleTasks.length} / 共 {tasks.length} 项</small></span>
        <ChevronRight aria-hidden="true" size={14} />
      </>}
    >
      <ol aria-label="本轮计划步骤" data-bounded-scroll={tasks.length > TASK_PLAN_PAGE_SIZE || undefined}>
        {visibleTasks.map((task, index) => (
          <li key={task.id} data-state={task.status}>
            <TaskStateIcon status={task.status} />
            <span><b>{index + 1}</b>{task.label}</span>
          </li>
        ))}
      </ol>
      {remaining ? (
        <Button
          aria-label={`显示更多计划步骤：${remaining} 项`}
          className="agent-status-task-plan__load-more"
          onClick={() => setVisibleCount(tasks.length)}
          size="small"
          variant="quiet"
        >
          显示更多 {Math.min(TASK_PLAN_PAGE_SIZE, remaining)} 项
        </Button>
      ) : null}
    </Disclosure>
  );
}

function ToolStep({ activity }: { activity: AgentActivityProjection }) {
  const view = publicToolResultView(activity);
  const [visibleFieldCount, setVisibleFieldCount] = useState(5);
  const visibleFields = view.fields.slice(0, visibleFieldCount);
  const argumentFieldCount = Object.keys(record(activity.payload.args)).length;
  const knowledge = view.toolLabel === '文档知识库';
  const stateIcon = activity.status === 'running'
    ? <LoaderCircle size={14} />
    : activity.status === 'waiting'
      ? <CircleDashed size={14} />
      : activity.status === 'failed'
        ? <TriangleAlert size={14} />
        : knowledge
          ? <Search size={14} />
          : <Wrench size={14} />;
  return (
    <Disclosure className="agent-status-tool" data-state={activity.status} contentClassName="agent-status-tool__details" summary={<>
        <span className="agent-status-tool__icon">{stateIcon}</span>
        <span><strong>{knowledge ? '知识库' : view.toolLabel}</strong><small title={view.error || view.summary}>{view.error || view.summary}</small></span>
        <i>{view.sources.length ? `来源 ${view.sources.length} · ` : ''}{activityStatusLabel(activity.status)}</i>
        <ChevronRight size={14} />
      </>}>
      <div>
        <p className="agent-status-tool__interface"><span>接口</span><code>{view.toolId || 'unknown'}</code></p>
        {view.operation ? <p><span>操作</span><code>{view.operation}</code></p> : null}
        <p><span>参数</span><strong>{argumentFieldCount} 个字段</strong></p>
        {visibleFields.map((field) => <p key={field.id}><span>{field.label}</span><strong>{field.value}</strong></p>)}
        {view.error ? <p className="agent-status-tool__error" role="alert"><TriangleAlert size={13} /><span>{view.error}</span></p> : null}
        {visibleFields.length < view.fields.length ? <Button className="agent-status-tool__load-more" onClick={() => setVisibleFieldCount((count) => Math.min(view.fields.length, count + 5))} size="small" variant="quiet">显示更多字段（{visibleFields.length}/{view.fields.length}）</Button> : null}
        {view.sources.length ? (
          <section className="agent-status-tool__sources">
            <strong><BookOpenText size={13} />信息来源</strong>
            <ul aria-label={`${view.toolLabel}公开来源`}>
              {view.sources.map((source) => <li key={source}>{source}</li>)}
            </ul>
          </section>
        ) : null}
        {view.destination ? (
          <a className="agent-tool-destination" href={view.destination.href}>
            {view.destination.label}<ExternalLink size={13} aria-hidden="true" />
          </a>
        ) : null}
      </div>
    </Disclosure>
  );
}

export type LogicalToolActivity = {
  kind: 'single' | 'attempts';
  activities: AgentActivityProjection[];
};

export function groupToolActivities(
  activities: readonly AgentActivityProjection[],
): LogicalToolActivity[] {
  const groups: LogicalToolActivity[] = [];
  for (const activity of activities) {
    const previous = groups.at(-1);
    if (
      isDelegationContractCorrection(activity)
      && previous?.kind === 'attempts'
      && previous.activities.every(isDelegationContractCorrection)
    ) {
      previous.activities.push(activity);
      continue;
    }
    if (isDelegationContractCorrection(activity)) {
      const priorSingle = previous?.kind === 'single'
        ? previous.activities[0]
        : undefined;
      if (priorSingle && isDelegationContractCorrection(priorSingle)) {
        groups.splice(-1, 1, { kind: 'attempts', activities: [priorSingle, activity] });
      } else {
        groups.push({ kind: 'single', activities: [activity] });
      }
      continue;
    }
    groups.push({ kind: 'single', activities: [activity] });
  }
  return groups;
}

function isDelegationContractCorrection(activity: AgentActivityProjection): boolean {
  if (activity.status !== 'failed') return false;
  const toolId = text(activity.payload.toolId ?? activity.payload.toolName);
  if (toolId !== 'agents') return false;
  const publicFailure = JSON.stringify(activity.payload).slice(0, 12_000);
  return /(?:validation failed|additional properties|tasks cannot be combined|todotask|outputschema|schema for function)/iu.test(publicFailure);
}

function ToolAttemptGroup({ activities }: { activities: AgentActivityProjection[] }) {
  const latest = publicToolResultView(activities.at(-1)!);
  return (
    <Disclosure className="agent-status-tool-attempts" contentClassName="agent-status-tool-attempts__details" summary={<>
        <span className="agent-status-tool__icon"><TriangleAlert size={14} /></span>
        <span><strong>多人协作 · 参数修正</strong><small>{activities.length} 次无效调用已折叠 · {latest.summary}</small></span>
        <i>{activities.length} 次</i>
        <ChevronRight size={14} />
      </>}>
      <div>
        <p>这些回执属于同一次委派的参数纠错，不代表 {activities.length} 个子任务失败。原始回执仍保留在下方。</p>
        {activities.map((activity, index) => (
          <ToolStep key={activity.id} activity={activity} />
        ))}
      </div>
    </Disclosure>
  );
}

function SubagentTreeRows({
  nodes,
  sessionId,
  level = 0,
}: {
  nodes: readonly SubagentTreeNode[];
  sessionId: string;
  level?: number;
}) {
  return nodes.map((node) => (
    <div className="agent-status-subagent-branch" key={node.run.id} data-level={level}>
      <SubagentRow
        run={node.run}
        sessionId={sessionId}
        level={level}
        childCount={node.children.length}
      />
      {node.children.length ? (
        <div className="agent-status-subagent-children" aria-label={`${node.run.task} 的子调用`}>
          <SubagentTreeRows nodes={node.children} sessionId={sessionId} level={level + 1} />
        </div>
      ) : null}
    </div>
  ));
}

function SubagentRow({
  run,
  sessionId,
  level,
  childCount,
}: {
  run: AgentSubagentRunV1;
  sessionId: string;
  level: number;
  childCount: number;
}) {
  const active = run.state === 'queued' || run.state === 'running';
  const elapsed = useRunElapsed(run);
  const presentationState = subagentPresentationState(run);
  return (
    <div className="agent-status-subagent" data-state={presentationState} data-depth={run.depth}>
      <span className="agent-status-subagent__state"><SubagentStateIcon state={presentationState} /></span>
      <span>
        <strong>
          {level > 0 ? 'Pattern 子调用 · ' : ''}{subagentTemplateLabel(run.templateId)}
          {run.attemptNumber > 1 ? ` · 尝试 ${run.attemptNumber}` : ''}
        </strong>
        <small>{publicText(run.task, '协作任务')}</small>
        {childCount ? <small className="agent-status-subagent__lineage">派生 {childCount} 个子节点</small> : null}
        {run.todoTask ? (
          <small className="agent-status-subagent__todo">
            关联 Todo：{run.todoPhase ? `${publicText(run.todoPhase, '当前阶段')} · ` : ''}{publicText(run.todoTask, '未知任务')}
          </small>
        ) : null}
      </span>
      <i><span>{subagentStateLabel(run)}</span>{elapsed ? <time>{elapsed}</time> : null}</i>
      {isUnverifiedReturn(run) ? (
        <small className="agent-status-subagent__verification">{UNVERIFIED_SUBAGENT_NOTICE}</small>
      ) : null}
      {isContractInvalid(run) ? (
        <small className="agent-status-subagent__verification" data-contract-invalid>
          {INVALID_SUBAGENT_CONTRACT_NOTICE}
        </small>
      ) : null}
      {(run.state === 'failed' || run.state === 'timed_out') && run.error.trim() ? (
        <small className="agent-status-subagent__error" role="alert">{run.error}</small>
      ) : null}
      <SubagentConsoleDialog run={run} sessionId={sessionId} triggerLabel={active ? '查看进度' : '查看结果'} />
    </div>
  );
}

function StatusRow({ icon: Icon, title, detail }: { icon: LucideIcon; title: string; detail: string }) {
  return <div className="agent-status-row"><Icon size={14} /><span><strong>{title}</strong><small>{detail}</small></span></div>;
}

function EmptyLine({ children, animated = false, tone = 'neutral' }: { children: ReactNode; animated?: boolean; tone?: 'neutral' | 'danger' }) {
  return <p className="agent-status-empty" data-animated={animated || undefined} data-tone={tone}>{animated ? <LoaderCircle size={13} /> : null}{children}</p>;
}

function TurnStateIcon({ status }: { status: AgentTurnStatus }) {
  if (status === 'queued' || status === 'running') return <LoaderCircle size={15} />;
  if (status === 'waiting') return <CircleDashed size={15} />;
  if (status === 'failed') return <TriangleAlert size={15} />;
  return <Check size={15} />;
}

function TaskStateIcon({ status }: { status: string }) {
  if (status === 'running') return <LoaderCircle size={13} />;
  if (status === 'completed') return <Check size={13} />;
  if (status === 'failed') return <TriangleAlert size={13} />;
  return <CircleDashed size={13} />;
}

function SubagentStateIcon({ state }: { state: ReturnType<typeof subagentPresentationState> }) {
  if (state === 'running') return <LoaderCircle size={15} />;
  if (state === 'queued') return <CircleDashed size={15} />;
  if (state === 'completed') return <Check size={15} />;
  if (state === 'returned') return <CircleDashed size={15} />;
  if (state === 'contract_invalid') return <TriangleAlert size={15} />;
  return <TriangleAlert size={15} />;
}

function useRunElapsed(run: AgentSubagentRunV1): string {
  const active = run.state === 'queued' || run.state === 'running';
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [active]);
  if (!active) return '';
  const startedAt = run.startedAtMs ?? run.createdAtMs;
  const seconds = Math.max(0, Math.floor((now - startedAt) / 1_000));
  const minutes = Math.floor(seconds / 60);
  return minutes ? `${minutes}分${String(seconds % 60).padStart(2, '0')}秒` : `${seconds}秒`;
}

interface StatusPanelProjection {
  turn?: AgentProjectionState['turnsById'][string];
  tasks: Array<{ id: string; label: string; status: string }>;
  tools: AgentActivityProjection[];
  files: Array<{ id: string; name: string; kind: string }>;
  artifacts: Array<{ id: string; name: string; kind: string }>;
  attachmentCount: number;
}

export function projectStatusPanel(projection?: AgentProjectionState): StatusPanelProjection {
  if (!projection) return { tasks: [], tools: [], files: [], artifacts: [], attachmentCount: 0 };
  const turn = [...projection.turnOrder].reverse()
    .map((id) => projection.turnsById[id])
    .find((item) => item && (item.messageIds.length > 0 || item.activityIds.length > 0));
  // The durable Session Todo has its own workflow panel above. Do not copy it
  // into the current-turn progress projection: tasks from an earlier goal
  // would otherwise masquerade as this turn's 0/N checklist.
  if (!turn) return { tasks: [], tools: [], files: [], artifacts: [], attachmentCount: 0 };
  const messages = turn.messageIds.map((id) => projection.messagesById[id]).filter(Boolean);
  const tasks: StatusPanelProjection['tasks'] = [];
  const files: StatusPanelProjection['files'] = [];
  const artifacts: StatusPanelProjection['artifacts'] = [];
  const attachmentIds = new Set<string>();
  for (const message of messages) {
    message.attachments.forEach((id) => attachmentIds.add(id));
    for (const block of message.blocks) {
      if (block.type === 'task_plan') {
        const items = Array.isArray(block.data.items) ? block.data.items : Array.isArray(block.data.tasks) ? block.data.tasks : [];
        items.forEach((item, index) => {
          const value = record(item);
          tasks.push({
            id: `${block.id}:${index}`,
            label: publicText(value.title ?? value.label ?? item, `步骤 ${index + 1}`),
            status: taskStatus(text(value.status)),
          });
        });
      }
      if (block.type === 'file') {
        const name = publicFileName(block.data.name ?? block.data.fileName ?? block.data.title);
        if (name) files.push({ id: block.id, name, kind: publicText(block.data.mimeType, '文件') });
        if (name && text(block.data.receiptId ?? block.data.artifactId)) artifacts.push({ id: `artifact:${block.id}`, name, kind: '文件产物' });
      }
      if (block.type === 'diff') {
        const name = publicFileName(block.data.fileName ?? block.data.title);
        if (name) artifacts.push({ id: `diff:${block.id}`, name, kind: '变更产物' });
      }
    }
  }
  const tools = uniqueToolActivities(turn.activityIds
    .map((id) => projection.activitiesById[id])
    .filter((activity): activity is AgentActivityProjection => Boolean(activity)));
  return {
    turn,
    tasks: uniqueBy(tasks, (item) => item.id),
    tools,
    files: uniqueBy(files, (item) => item.name),
    artifacts: uniqueBy(artifacts, (item) => item.name),
    attachmentCount: attachmentIds.size,
  };
}

function statusPanelLabel(
  projection: AgentProjectionState | undefined,
  view: StatusPanelProjection,
  resolvedTodo?: AgentTodoProjection,
): string {
  // The header describes the current Pi turn. A durable Session Todo is a
  // separate work record and may intentionally outlive a completed turn; do
  // not let an unfinished historical Todo relabel that completed turn as
  // currently executing.
  if (view.turn) {
    return `当前回合 · ${turnStatusLabel(view.turn.status)}`;
  }
  const todo = resolvedTodo ?? projection?.todo;
  if (todo?.counts.total) {
    if (todo.counts.completed + todo.counts.abandoned === todo.counts.total) {
      return todo.counts.abandoned ? 'Todo 已收束' : 'Todo 已完成';
    }
    if (todo.counts.inProgress > 0) {
      return `执行中 · ${todo.counts.completed}/${todo.counts.total}`;
    }
    return `待执行 · ${todo.counts.completed}/${todo.counts.total}`;
  }
  return '等待新回合';
}

function turnProgressLabel(
  view: StatusPanelProjection,
  logicalTools: LogicalToolActivity[],
): string {
  if (!view.turn) return '';
  if (view.tasks.length) {
    const completed = view.tasks.filter((task) => task.status === 'completed').length;
    return `${completed} / ${view.tasks.length} 项待办已完成`;
  }
  if (logicalTools.length) {
    const completed = logicalTools.filter((tool) => (
      (tool.kind === 'attempts' ? tool.activities.at(-1) : tool.activities[0])?.status === 'completed'
    )).length;
    return `${completed} / ${logicalTools.length} 个关键步骤已完成`;
  }
  return view.turn.status === 'queued' || view.turn.status === 'running' ? '正在等待下一条可公开进度' : '本轮没有结构化待办';
}

function turnStatusLabel(status: AgentTurnStatus): string {
  return ({ queued: '排队中', running: '正在生成', waiting: '等待确认', completed: '已完成', failed: '未完成', aborted: '已停止' })[status];
}

function taskStatus(value: string): string {
  if (['completed', 'done', 'success'].includes(value)) return 'completed';
  if (['running', 'in_progress', 'active'].includes(value)) return 'running';
  if (['failed', 'error'].includes(value)) return 'failed';
  return 'queued';
}

function activityStatusLabel(status: AgentActivityProjection['status']): string {
  return ({ running: '进行中', waiting: '待确认', completed: '完成', failed: '失败' })[status];
}

function publicText(value: unknown, fallback: string): string {
  const normalized = text(value).replace(/\s+/gu, ' ').trim().slice(0, 280);
  if (!normalized) return fallback;
  if (/(?:chain.of.thought|reasoning|api.?key|authorization|cookie|password|secret|bearer\s)/iu.test(normalized)) return fallback;
  return normalized.replace(/\/(?:Users|Volumes|private|tmp)\/[^\s,;，。]+/gu, '[本地路径]');
}

function publicFileName(value: unknown): string {
  const normalized = text(value).replace(/\s+/gu, ' ').trim().slice(0, 180);
  if (!normalized || /[\\/]/u.test(normalized)) return '';
  if (/(?:api.?key|authorization|cookie|password|secret)/iu.test(normalized)) return '';
  return normalized;
}

function uniqueBy<T>(items: T[], key: (item: T) => string): T[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    const value = key(item);
    if (seen.has(value)) return false;
    seen.add(value);
    return true;
  });
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}
