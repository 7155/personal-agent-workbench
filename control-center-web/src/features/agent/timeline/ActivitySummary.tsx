import { publicReasoningSummaryText } from './public-reasoning-summary';
import { ToolStatusMark, toolReceiptStatus } from '@/features/conversation-ui/components/ToolStatusMark';
import {
  BookOpenText,
  Bot,
  Brain,
  Check,
  CheckCircle2,
  Code2,
  ChevronRight,
  CircleDashed,
  Clock4,
  Copy,
  Cpu,
  Database,
  ExternalLink,
  FileDiff,
  FileText,
  GitBranch,
  Globe,
  Library,
  ListChecks,
  MessageSquareText,
  Search,
  ShieldAlert,
  ShieldCheck,
  Target,
  TerminalSquare,
  TriangleAlert,
  Users,
  Wrench,
  type LucideIcon,
} from 'lucide-react';
import {
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useId,
  useState,
  type Dispatch,
  type CSSProperties,
  type ReactNode,
  type SetStateAction,
  type UIEvent,
} from 'react';
import { publicToolFamily, publicToolName, type AgentToolFamily } from '../tool-presentation';
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/primitives';
import {
  agentToolProgressHistory,
  type AgentActivityProjection,
  type AgentToolProgressEntry,
} from '@/contracts/agent-reducer';
import {
  approvalDecisionView,
  approvalDecisionReasonLabel,
  approvalNeedsHumanDecision,
  type ApprovalDecisionView,
} from '@/contracts/approval-decision';
import { writeClipboardText } from '@/platform/clipboard';
import {
  evidenceEchoRoute,
  openEvidenceEchoEntity,
  type EvidenceEchoEntity,
} from '@/features/evidence-echo/evidence-echo';
import { usePawOsDesktop } from '@/features/paw-os/surface-context';
import { DiffPreview } from '../file-preview/DiffPreview';
import { SafeFieldList } from './BlockRenderer';
import {
  toggleDisclosureOnKeyPreservingAnchor,
  toggleDisclosurePreservingAnchor,
  useAutoFollowScroll,
} from './disclosure-anchor';
import {
  inspectableRawResultText,
  publicToolResultView,
  safeSourceLabels,
  type PublicToolResultView,
} from './public-tool-result';
import { publicAgentErrorText } from '../public-error';
import { routeDecisionPlanView } from './route-decision-plan';
import { RouteDecisionPlan } from './RouteDecisionPlan';
import { SmoothDisclosureReveal } from './SmoothDisclosureReveal';
import { ConversationPlanetMark, type ConversationPlanetState } from './ConversationPlanetMark';
import { TraceAgentHandoffButton, type TraceAgentHandoffInput } from '@/features/trace-agent/handoff';

const activityDisclosureOverrides = new Map<string, boolean>();
const activityDisclosureOverrideLimit = 512;

/** Virtualized turns may unmount while their measured height changes. Keep a
 * human's disclosure choice on the stable Runtime activity id so a real click
 * cannot flash open and immediately collapse during that remount. */
export function resetActivityDisclosureOverrides(): void {
  activityDisclosureOverrides.clear();
}

function useActivityDisclosure(
  key: string,
  initiallyOpen: boolean,
): [boolean, Dispatch<SetStateAction<boolean>>] {
  const [state, setOpenState] = useState(() => ({
    key,
    open: activityDisclosureOverrides.get(key) ?? initiallyOpen,
  }));
  // A virtualized component can be reused for a different Session/turn. Reset
  // before committing that render, so the previous row never flashes open.
  if (state.key !== key) {
    setOpenState({ key, open: activityDisclosureOverrides.get(key) ?? initiallyOpen });
  }
  const setOpen = useCallback<Dispatch<SetStateAction<boolean>>>((nextValue) => {
    setOpenState((current) => {
      const next = typeof nextValue === 'function' ? nextValue(current.open) : nextValue;
      if (activityDisclosureOverrides.size >= activityDisclosureOverrideLimit
        && !activityDisclosureOverrides.has(key)) {
        const oldest = activityDisclosureOverrides.keys().next().value;
        if (typeof oldest === 'string') activityDisclosureOverrides.delete(oldest);
      }
      activityDisclosureOverrides.set(key, next);
      return { key, open: next };
    });
  }, [key]);
  return [state.open, setOpen];
}

export function ActivitySummary({
  activities,
  inline = false,
  sessionId = '',
  onApprovalDecision,
  onOpenApproval,
  onRequestPermission,
}: {
  activities: AgentActivityProjection[];
  inline?: boolean;
  sessionId?: string;
  onApprovalDecision?: (approvalId: string, decision: 'approved' | 'rejected', hash: string) => void;
  onOpenApproval?: (activity: AgentActivityProjection) => void;
  onRequestPermission?: () => void;
}) {
  const automatedWaiting = activities.some((activity) => (
    activity.status === 'waiting'
    && Boolean(text(activity.payload.approvalId))
    && !approvalNeedsHumanDecision(activity.payload)
  ));
  const running = automatedWaiting || activities.some((activity) => activity.status === 'running');
  const [detailsOpen, setDetailsOpen] = useState(false);
  const waiting = activities.some((activity) => (
    activity.status === 'waiting'
    && (
      !text(activity.payload.approvalId)
      || approvalNeedsHumanDecision(activity.payload)
    )
  ));
  const failed = activities.some((activity) => activity.status === 'failed');
  const terminalFailure = activities.some((activity) => (
    activity.kind === 'turn_failed' && activity.status === 'failed'
  ));
  const pendingApproval = activities.find((activity) => (
    activity.status === 'waiting'
    && Boolean(text(activity.payload.approvalId))
    && Boolean(text(activity.payload.toolCallId))
    && approvalNeedsHumanDecision(activity.payload)
  ));
  const pendingApprovalId = text(pendingApproval?.payload.approvalId);
  // Tool histories are supporting detail, not the conversation itself. Keep
  // live, completed, and failed groups compact by default; only a real human
  // decision opens automatically so the required controls cannot be missed.
  const activityGroupId = activities[0]?.id ?? 'empty';
  const [inlineOpen, setInlineOpen] = useActivityDisclosure(
    `group:${sessionId}:${activities[0]?.turnId ?? ''}:${activityGroupId}`,
    Boolean(pendingApprovalId),
  );
  const [inlinePresence, setInlinePresence] = useState(inlineOpen);
  const inlineDetailId = `agent-activity-group-${useId().replace(/:/gu, '')}`;
  const presentedApprovalRef = useRef(pendingApprovalId);
  const inlineContentKey = activities
    .map((activity) => `${activity.id}:${activity.status}:${activity.updatedAtMs}`)
    .join('|');
  const {
    onScroll: handleInlineScroll,
    scrollRef: inlineTimelineRef,
  } = useAutoFollowScroll<HTMLDivElement>(inlineContentKey, inline && inlineOpen);
  useEffect(() => {
    if (!pendingApprovalId || pendingApprovalId === presentedApprovalRef.current) return;
    presentedApprovalRef.current = pendingApprovalId;
    setInlineOpen(true);
  }, [pendingApprovalId]);

  if (activities.length === 0) return null;
  const summary = aggregateSummary(activities);
  const title = running
    ? '正在处理'
    : waiting
      ? '等待你的确认'
      : terminalFailure
        ? '本轮未完成'
        : '操作记录';
  const inlineTools = compactToolSummary(activities);
  const inlineTitle = inlineTools.count
    ? running
      ? `正在处理 ${inlineTools.count} 项操作`
      : waiting
        ? `${inlineTools.count} 项操作等待确认`
        : `${inlineTools.count} 项操作`
    : title;
  // A long Agent loop can legitimately contain failed probes. Keep the group
  // neutral and reserve terminal red for a turn/provider failure. The inline
  // detail list still identifies every failed call and its recovery reason.
  const inlineSummary = inlineTools.count
    ? inlineTools.count === 1
      ? inlineTools.highlight || inlineTools.outcome || inlineTools.names
      : inlineTools.outcome || inlineTools.names
    : summary;
  const inlineStatus = terminalFailure
    ? '未完成'
    : waiting
      ? '等待确认'
      : running
        ? '进行中'
        : failed && inlineTools.count > 1
          ? '查看'
          : failed
            ? '失败'
            : '完成';
  const liveActivities = running || waiting ? activities.slice(-3) : [];
  const liveActivityCount = liveActivities.length;
  const state = terminalFailure ? 'failed' : waiting ? 'waiting' : running ? 'running' : failed ? 'mixed' : 'done';
  const summaryContent = (
    <>
      <span className="agent-activity__status" aria-hidden="true">
        {failed ? <TriangleAlert size={15} /> : waiting ? <ShieldAlert size={15} /> : running ? <CircleDashed size={15} /> : <CheckCircle2 size={15} />}
      </span>
      <span className="agent-activity__copy">
        <strong>{title}</strong>
        <small>{summary}</small>
      </span>
      <ChevronRight aria-hidden="true" size={16} />
    </>
  );
  if (inline) {
    /* Only a live group trades its Tool identity icon for a planet: motion in
       the transcript has to mean the Runtime is still working, and a settled
       group keeps the glyph that says what it was. */
    const inlinePlanetState: ConversationPlanetState | null = running
      ? activities.some((activity) => activity.status === 'running' && activity.kind === 'reasoning_summary')
        && !activities.some((activity) => activity.status === 'running' && ['tool_started', 'tool_progress'].includes(activity.kind))
        ? 'thinking'
        : 'running'
      : waiting
        ? 'waiting'
        : null;
    const InlineIcon = terminalFailure
      ? TriangleAlert
      : waiting
        ? ShieldAlert
        : running
          ? CircleDashed
          : inlineTools.count
            ? Wrench
            : activityPresentation(activities[activities.length - 1]!).icon;
    return (
      <div className="agent-activity-group" data-layout="interleaved">
        <details
          className="agent-activity agent-activity--inline"
          data-state={state}
          open={inlineOpen || inlinePresence}
        >
          <summary
            aria-controls={inlineDetailId}
            aria-expanded={inlineOpen}
            aria-label={`${inlineTitle}，${inlineSummary}，${inlineStatus}`}
            onClick={(event) => toggleDisclosurePreservingAnchor(event, setInlineOpen)}
            onKeyDown={(event) => toggleDisclosureOnKeyPreservingAnchor(event, setInlineOpen)}
          >
            {inlinePlanetState
              ? <ConversationPlanetMark size="md" state={inlinePlanetState} />
              : <InlineIcon aria-hidden="true" className="agent-activity__inline-icon" size={15} />}
            <strong>{inlineTitle}</strong>
            <span className="agent-activity__inline-tools">{inlineSummary}</span>
            <span className="agent-activity__inline-status agent-fx-pill" data-status={state} data-tone={state === 'done' ? 'ok' : state === 'running' ? 'run' : state === 'waiting' ? 'wait' : state === 'failed' ? 'danger' : 'warn'}>
              <ConversationPlanetMark
                motionActive={false}
                size="sm"
                state={inlinePlanetState ?? (state === 'failed' ? 'failed' : state === 'done' ? 'done' : 'idle')}
              />
              {inlineStatus}
            </span>
            <ChevronRight aria-hidden="true" size={15} />
          </summary>
          <SmoothDisclosureReveal
            id={inlineDetailId}
            onPresenceChange={setInlinePresence}
            open={inlineOpen}
          >
            <div
              aria-label="操作与思考过程"
              className="agent-activity__inline-timeline"
              data-bounded-scroll="true"
              onScroll={handleInlineScroll}
              ref={inlineTimelineRef}
              role="region"
              tabIndex={0}
            >
              {activities.map((activity) => (
                <ActivityRow
                  key={activity.id}
                  activity={activity}
                  sessionId={sessionId}
                  initiallyOpen={false}
                  onApprovalDecision={onApprovalDecision}
                  onOpenApproval={(selected) => {
                    setInlineOpen(false);
                    onOpenApproval?.(selected);
                  }}
                  onRequestPermission={() => {
                    setInlineOpen(false);
                    onRequestPermission?.();
                  }}
                />
              ))}
            </div>
          </SmoothDisclosureReveal>
        </details>
      </div>
    );
  }
  return (
    <div className="agent-activity-group">
      <Dialog open={detailsOpen} onOpenChange={setDetailsOpen}>
        <DialogTrigger asChild>
          <button
            className="agent-activity"
            data-state={state}
            type="button"
            aria-label={`查看活动详情：${title}，${summary}`}
          >
            {summaryContent}
          </button>
        </DialogTrigger>
        <DialogContent className="agent-activity-dialog">
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            <DialogDescription>{summary}。这里显示本轮真实活动与可公开的工具结果。</DialogDescription>
          </DialogHeader>
          <div className="agent-activity__timeline">
            {activities.map((activity) => (
              <ActivityRow
                key={activity.id}
                activity={activity}
                sessionId={sessionId}
                onApprovalDecision={onApprovalDecision}
                onOpenApproval={(selected) => {
                  setDetailsOpen(false);
                  onOpenApproval?.(selected);
                }}
                onRequestPermission={() => {
                  setDetailsOpen(false);
                  onRequestPermission?.();
                }}
              />
            ))}
          </div>
        </DialogContent>
      </Dialog>
      {liveActivities.length ? (
        <div className="agent-activity-live" aria-label="当前活动">
          {activities.length > liveActivityCount ? (
            <div className="agent-activity-live__scope">
              <small>当前显示最近 {liveActivityCount} / 共 {activities.length} 项活动</small>
              <button
                type="button"
                onClick={() => setDetailsOpen(true)}
                aria-label={`查看全部 ${activities.length} 项活动`}
              >
                查看全部
              </button>
            </div>
          ) : null}
          {liveActivities.map((activity) => (
            <ActivityRow
              key={activity.id}
              activity={activity}
              sessionId={sessionId}
              onApprovalDecision={onApprovalDecision}
              onOpenApproval={onOpenApproval}
              onRequestPermission={onRequestPermission}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

const ActivityRow = memo(function ActivityRow({
  activity,
  sessionId = '',
  initiallyOpen = false,
  hideSummary = false,
  onApprovalDecision,
  onOpenApproval,
  onRequestPermission,
}: {
  activity: AgentActivityProjection;
  sessionId?: string;
  initiallyOpen?: boolean;
  hideSummary?: boolean;
  onApprovalDecision?: (approvalId: string, decision: 'approved' | 'rejected', hash: string) => void;
  onOpenApproval?: (activity: AgentActivityProjection) => void;
  onRequestPermission?: () => void;
}) {
  const payload = activity.payload;
  const approvalId = text(payload.approvalId);
  const hash = text(payload.payloadSha256);
  const roomExecutionBridge = isAuthorizedRoomExecutionBridge(payload);
  const boundToTool = Boolean(approvalId && text(payload.toolCallId));
  const displayActivity: AgentActivityProjection = boundToTool && activity.kind.includes('approval')
    ? { ...activity, kind: 'tool_progress' }
    : activity;
  const presentation = activityPresentation(displayActivity);
  const approvalPresentation = approvalId && !roomExecutionBridge
    ? activityPresentation({
        ...activity,
        kind: activity.status === 'waiting' ? 'approval_required' : 'approval_resolved',
      })
    : null;
  const Icon = presentation.icon;
  const isToolActivity = ['tool_started', 'tool_progress', 'tool_finished'].includes(displayActivity.kind);
  const toolView = useMemo(
    () => (isToolActivity ? publicToolResultView(displayActivity) : null),
    [displayActivity, isToolActivity],
  );
  const visibleSummary = activity.kind === 'turn_failed'
    ? publicAgentErrorText(activity.summary, '模型服务请求失败，请重试或切换模型。')
    : publicActivitySummary(publicProgressSummary(activity.summary, activity), presentation.title);
  const canDecide = !roomExecutionBridge && activity.status === 'waiting' && approvalNeedsHumanDecision(payload) && approvalId && hash && onApprovalDecision;
  const routePlan = useMemo(() => routeDecisionPlanView(payload), [payload]);
  const progressHistory = isToolActivity ? agentToolProgressHistory(payload.progressHistory) : [];
  const [rowOpen, setRowOpen] = useActivityDisclosure(
    `row:${sessionId}:${activity.turnId}:${activity.id}`,
    // Tool payloads are supporting evidence. Even a running, failed, or
    // approval-bound tool starts folded so a short receipt cannot take over
    // the conversation viewport. A human click is still remembered across a
    // virtualized remount of this exact turn.
    // A hidden inner summary is already governed by the outer FX disclosure;
    // once the user opens that outer row, its body must be reachable. Visible
    // Tool rows still always start folded.
    Boolean(initiallyOpen) && (hideSummary || !isToolActivity),
  );
  const [rowPresence, setRowPresence] = useState(rowOpen);
  const detailId = `agent-activity-row-${useId().replace(/:/gu, '')}`;
  const nowMs = useActivityClock(activity.status === 'running');
  const receiptMeta = activityReceiptMeta(activity, nowMs, toolView);
  return (
    <details
      className="agent-activity-row"
      data-kind={presentation.kind}
      data-summary-hidden={hideSummary || undefined}
      data-state={activity.status}
      open={rowOpen || rowPresence}
    >
      <summary
        aria-controls={detailId}
        aria-expanded={rowOpen}
        hidden={hideSummary}
        onClick={(event) => toggleDisclosurePreservingAnchor(event, setRowOpen)}
        onKeyDown={(event) => toggleDisclosureOnKeyPreservingAnchor(event, setRowOpen)}
      >
        <span className="agent-activity-row__icon" data-kind={presentation.kind}>{isToolActivity ? <ToolStatusMark status={toolReceiptStatus(activity.status, activity.settledByTurnStatus)} /> : <Icon size={15} />}</span>
        <span>
          <strong>{presentation.title}</strong>
          <small>
            {toolView?.error ?? toolView?.summary ?? visibleSummary}
          </small>
        </span>
        <i data-status={activity.status}>
          {toolView?.sources.length ? `来源 ${toolView.sources.length} · ` : ''}
          {activity.settledByTurnStatus === 'aborted' ? '已停止' : statusLabel(activity.status)}
          {receiptMeta ? ` · ${receiptMeta}` : ''}
        </i>
      </summary>
      <SmoothDisclosureReveal
        id={detailId}
        onPresenceChange={setRowPresence}
        open={rowOpen}
      >
        <div className="agent-activity-row__details">
          {presentation.detail ? <p>{presentation.detail}</p> : null}
          {routePlan ? <RouteDecisionPlan view={routePlan} /> : null}
          <ToolProgressTimeline activity={activity} entries={progressHistory} />
          {toolView?.request.length ? <PublicToolRequest view={toolView} /> : null}
          {toolView ? <PublicToolResult activityId={activity.id} sessionId={sessionId} view={toolView} /> : null}
          {toolView && toolView.fields.every((field) => field.id === 'status') && !toolView.request.length && !toolView.output && !toolView.preview && !toolView.resultItems.length && !toolView.change && !toolView.error ? (
            <p className="agent-tool-unavailable">这条历史回执未包含可公开的调用参数或返回内容。</p>
          ) : null}
          {activity.kind === 'reasoning_summary'
            ? <ReasoningSummaryDetails items={reasoningItemsFromPayload(payload, visibleSummary)} />
            : toolView
              ? <PublicToolFields view={toolView} />
              : routePlan
                ? null
                : <SafeFieldList data={payload} />}
          {toolView?.error ? (
            <PublicToolError
              handoff={{
                kind: 'tool',
                entityId: text(payload.toolCallId) || activity.id,
                title: `${publicToolName(text(payload.toolId ?? payload.toolName), text(payload.displayName))}失败`,
                summary: toolView.summary || toolView.error,
                error: toolView.error,
                sessionId,
                refs: {
                  activityId: activity.id,
                  turnId: activity.turnId,
                  toolCallId: text(payload.toolCallId),
                  toolName: text(payload.toolId ?? payload.toolName),
                },
              }}
              reason={toolView.error}
            />
          ) : null}
          <SourceList
            items={toolView?.sources ?? safeSourceLabels(payload.sources ?? payload.documents ?? payload.books)}
            links={toolView?.sourceLinks ?? []}
          />
          {toolView?.destination ? (
            <a className="agent-tool-destination" href={toolView.destination.href}>
              {toolView.destination.label}<ExternalLink size={13} aria-hidden="true" />
            </a>
          ) : null}
          {approvalPresentation ? (
            <section className="agent-activity-row__approval" aria-label={`审批状态：${approvalPresentation.title}`}>
              <ShieldAlert aria-hidden="true" size={15} />
              <span>
                <strong>{approvalPresentation.title}</strong>
                <small>{approvalPresentation.detail ?? '审批状态与这次 Tool 调用使用同一 toolCallId。'}</small>
              </span>
              {canDecide ? (
                <div className="agent-activity-row__approval-actions">
                  <Button size="small" variant="quiet" onClick={() => onApprovalDecision?.(approvalId, 'rejected', hash)}>拒绝</Button>
                  <Button size="small" variant="primary" onClick={() => onApprovalDecision?.(approvalId, 'approved', hash)}>批准</Button>
                </div>
              ) : null}
            </section>
          ) : null}
          {toolView?.recovery === 'approval' && onOpenApproval && !roomExecutionBridge ? (
            <div className="agent-tool-recovery">
              <Button size="small" variant="primary" leadingIcon={<ShieldAlert size={14} />} onClick={() => onOpenApproval(activity)}>去审批</Button>
            </div>
          ) : toolView?.recovery === 'permission' && onRequestPermission ? (
            <div className="agent-tool-recovery">
              <Button size="small" variant="primary" leadingIcon={<ShieldAlert size={14} />} onClick={onRequestPermission}>请求权限</Button>
            </div>
          ) : null}
        </div>
      </SmoothDisclosureReveal>
    </details>
  );
});

function isAuthorizedRoomExecutionBridge(payload: Record<string, unknown>): boolean {
  const causal = objectValue(payload.causalMetadata);
  return causal.roomBound === true
    && Boolean(text(causal.roomId))
    && Boolean(text(causal.rootId))
    && Boolean(text(causal.dispatchId));
}

export function ReasoningActivitySummary({
  activities,
}: {
  activities: AgentActivityProjection[];
}) {
  const reasoning = reasoningSummaryItems(activities);
  const nowMs = useActivityClock(reasoning.running);
  const latestActivity = activities.at(-1);
  const receiptMeta = latestActivity
    ? activityReceiptMeta(latestActivity, nowMs, null)
    : '';
  if (reasoning.items.length === 0) return null;
  return (
    <ReasoningSummaryStrip
      items={reasoning.items}
      receiptMeta={receiptMeta}
      running={reasoning.running}
    />
  );
}

function ReasoningSummaryStrip({
  items,
  receiptMeta,
  running,
}: {
  items: string[];
  receiptMeta: string;
  running: boolean;
}) {
  const latest = items.at(-1) ?? '';
  return (
    <Dialog>
      <DialogTrigger asChild>
        <button
          aria-label={`查看 Agent 思考摘要：${latest}`}
          className="agent-reasoning-feed"
          data-state={running ? 'running' : 'completed'}
          type="button"
        >
          {running
            ? <ConversationPlanetMark size="md" state="thinking" />
            : <Brain aria-hidden="true" size={15} />}
          <strong>{running ? '正在思考' : '思考摘要'}</strong>
          <span>{latest}</span>
          <small className="agent-reasoning-feed__meta">
            {running ? '进行中' : '完成'} · {items.length} 项{receiptMeta ? ` · ${receiptMeta}` : ''}
          </small>
          <ChevronRight aria-hidden="true" size={15} />
        </button>
      </DialogTrigger>
      <DialogContent className="agent-activity-dialog agent-reasoning-dialog">
        <DialogHeader>
          <DialogTitle>{running ? '正在思考' : '思考摘要'}</DialogTitle>
          <DialogDescription>本轮思考过程与规划摘要。</DialogDescription>
        </DialogHeader>
        <ReasoningSummaryDetails items={items} />
      </DialogContent>
    </Dialog>
  );
}

function ReasoningSummaryDetails({ items }: { items: string[] }) {
  return (
    <section className="agent-reasoning-details" aria-label="可公开的思考摘要">
      <strong>可公开的思考摘要</strong>
      <ol>{items.map((item, index) => <li key={`${index}:${item}`}>{item}</li>)}</ol>
    </section>
  );
}

function reasoningSummaryItems(activities: AgentActivityProjection[]): { items: string[]; running: boolean } {
  const reasoning = activities.filter((activity) => (
    activity.kind === 'reasoning_summary'
    && text(activity.payload.source) === 'provider_reasoning_summary'
  ));
  const items = reasoning.flatMap((activity) => reasoningItemsFromPayload(activity.payload, activity.summary));
  return {
    // The compact strip intentionally shows only the latest sentence, but its
    // disclosure is the complete public reasoning record. Do not silently
    // discard earlier planning steps from a long-running turn.
    items: [...new Set(items)],
    running: reasoning.some((activity) => activity.status === 'running'),
  };
}

function reasoningItemsFromPayload(payload: Record<string, unknown>, fallback: string): string[] {
  const values = Array.isArray(payload.items) ? payload.items : [];
  const items = values
    .filter((value): value is string => typeof value === 'string')
    .map(publicReasoningSummaryText)
    .filter(Boolean);
  const summary = publicReasoningSummaryText(fallback);
  return items.length ? items : summary ? [summary] : [];
}

function useActivityClock(running: boolean): number {
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return undefined;
    let timer = 0;
    const updateVisibility = () => {
      window.clearInterval(timer);
      if (document.hidden) return;
      setNowMs(Date.now());
      // Elapsed time comes from the Runtime start, not accumulated UI ticks.
      timer = window.setInterval(() => setNowMs(Date.now()), 1_000);
    };
    updateVisibility();
    document.addEventListener('visibilitychange', updateVisibility);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', updateVisibility);
    };
  }, [running]);
  return nowMs;
}

function ToolProgressTimeline({
  activity,
  entries,
}: {
  activity: AgentActivityProjection;
  entries: AgentToolProgressEntry[];
}) {
  if (entries.length === 0) return null;
  return (
    <div className="agent-activity-row__source-panel" aria-label="工具过程记录">
      <strong><CircleDashed size={13} />过程记录</strong>
      <ol className="agent-activity-row__sources">
        {entries.map((entry) => (
          <li key={entry.eventId}>
            {checkpointOffset(entry.createdAtMs, activity.createdAtMs)} · {publicProgressSummary(entry.summary, activity)} · {statusLabel(entry.status)}
          </li>
        ))}
      </ol>
    </div>
  );
}

/** Durable progress receipts fall back to `<rawToolId>执行完成`. The receipt
 * text itself is authoritative, so only the leading raw id is translated into
 * the public tool vocabulary; explicit Runtime summaries pass through. */
function publicProgressSummary(summary: string, activity: AgentActivityProjection): string {
  if (activity.kind === 'reasoning_summary' && activity.payload.source === 'provider_reasoning_summary') return publicReasoningSummaryText(summary);
  const rawId = text(activity.payload.toolName ?? activity.payload.toolId);
  if (rawId && summary.startsWith(rawId)) {
    return `${publicToolName(rawId)}${summary.slice(rawId.length)}`;
  }
  return summary;
}

function PublicToolResult({ activityId, sessionId, view }: { activityId: string; sessionId?: string; view: PublicToolResultView }) {
  let primary: ReactNode = null;
  let outputRendered = false;
  if (view.resultKind === 'semantic' && view.preview) {
    primary = <SemanticToolPreview preview={view.preview} />;
  } else if (view.resultKind === 'codemode') {
    primary = <PublicCodeModeResult view={view} />;
    outputRendered = true;
  } else if (view.resultKind === 'terminal' && view.output) {
    primary = <PublicTerminalResult view={view} />;
    outputRendered = true;
  } else if (view.resultKind === 'code' && view.output) {
    primary = <PublicCodeResult sessionId={sessionId} view={view} />;
    outputRendered = true;
  } else if (view.resultKind === 'matches' && view.resultItems.length) {
    primary = <PublicResultList sessionId={sessionId} view={view} label="搜索匹配结果" icon={<Search size={14} />} />;
  } else if (view.resultKind === 'files' && view.resultItems.length) {
    primary = <PublicResultList sessionId={sessionId} view={view} label="项目文件结果" icon={<Database size={14} />} />;
  } else if (view.resultKind === 'browser' && view.resultItems.length) {
    primary = <PublicResultList view={view} label="浏览器结果" icon={<ExternalLink size={14} />} />;
  } else if (view.resultKind === 'change' && (view.target || view.change)) {
    primary = <PublicChangeResult view={view} />;
  } else if (view.resultKind === 'structured' && view.resultItems.length) {
    primary = (
      <>
        <PublicResultList sessionId={sessionId} view={view} label={view.resultItemsLabel ?? '结果明细'} icon={<GitBranch size={14} />} />
        {view.output ? <PublicToolOutput view={view} /> : null}
      </>
    );
    outputRendered = true;
  } else if (view.output) {
    primary = <PublicToolOutput view={view} />;
    outputRendered = true;
  }
  // A change card carries the concrete written body or diff below its +/-
  // statistics, and a semantic card can ride with the sent content. Never
  // hide the real payload behind the summary card (PF-CM-007).
  const supplementalOutput = !outputRendered
    && view.output
    && (view.resultKind === 'semantic' || view.resultKind === 'change')
    ? <PublicToolOutput view={view} />
    : null;
  return (
    <>
      {primary}
      {supplementalOutput}
      {view.rawResult ? <InspectableToolResult activityId={activityId} view={view} /> : null}
    </>
  );
}

function PublicTerminalResult({ view }: { view: PublicToolResultView }) {
  const output = view.output?.text ?? '';
  const { copy, state } = useCopyableText(output);
  return (
    <section
      aria-label="命令输出"
      className="agent-tool-result-view agent-tool-terminal-result"
      data-result-kind="terminal"
    >
      <header>
        <strong><TerminalSquare size={14} />命令输出</strong>
        <Button
          aria-live="polite"
          leadingIcon={state === 'copied' ? <Check size={13} /> : <Copy size={13} />}
          onClick={() => void copy()}
          size="small"
          variant="quiet"
        >
          {state === 'copied' ? '已复制输出' : '复制输出'}
        </Button>
      </header>
      <pre aria-label="命令输出内容" role="region" tabIndex={0}><code>{output}</code></pre>
      {view.output?.truncated ? <small>{view.rawResult ? '摘要已截断；可展开下方“完整返回”查看原始回执。' : '完整结果仍由本机工具回执保留。'}</small> : null}
      {state === 'failed' ? <small role="alert">无法复制输出，请手动选择内容。</small> : null}
    </section>
  );
}

function PublicCodeResult({ sessionId = '', view }: { sessionId?: string; view: PublicToolResultView }) {
  const file = view.target || '文件内容';
  const code = view.output?.text ?? '';
  const { copy, state } = useCopyableText(code);
  const target = workspaceFileEntity(sessionId, view.targetPath, file);
  return (
    <section
      aria-label={`文件内容：${file}`}
      className="agent-tool-result-view agent-tool-code-result"
      data-result-kind="code"
    >
      <header>
        <strong>
          <BookOpenText size={14} />
          {target ? <WorkspaceFileLink target={target}>{file}</WorkspaceFileLink> : file}
        </strong>
        <span>
          <small>{view.language ?? 'text'}</small>
          <Button
            aria-live="polite"
            leadingIcon={state === 'copied' ? <Check size={13} /> : <Copy size={13} />}
            onClick={() => void copy()}
            size="small"
            variant="quiet"
          >
            {state === 'copied' ? '已复制代码' : '复制代码'}
          </Button>
        </span>
      </header>
      <pre aria-label={`${file} 代码内容`} data-language={view.language ?? 'text'} role="region" tabIndex={0}><code>{code}</code></pre>
      {view.output?.truncated ? <small>{view.rawResult ? '代码摘要已截断；可展开下方“完整返回”查看原始回执。' : '完整结果仍由本机工具回执保留。'}</small> : null}
      {state === 'failed' ? <small role="alert">无法复制代码，请手动选择内容。</small> : null}
    </section>
  );
}

function PublicCodeModeResult({ view }: { view: PublicToolResultView }) {
  const calls = view.codeMode?.calls ?? [];
  return (
    <section
      aria-label="代码执行回执"
      className="agent-tool-result-view agent-tool-codemode-result"
      data-result-kind="codemode"
    >
      <header>
        <strong><Code2 size={14} />代码执行</strong>
        <small>{calls.length
          ? `${calls.length} 个嵌套调用${view.codeMode?.nestedCallsComplete === false ? ' · 部分恢复' : ''}`
          : `Pi 执行回执${view.codeMode?.nestedCallsComplete === false ? ' · 部分恢复' : ''}`}</small>
      </header>
      {view.code ? <div className="agent-tool-codemode__section">
        <strong>执行代码</strong>
        <pre aria-label="代码执行源代码" tabIndex={0}><code>{view.code}</code></pre>
      </div> : null}
      {calls.length ? <div className="agent-tool-codemode__section">
        <strong>嵌套工具调用</strong>
        <ol aria-label="嵌套工具调用">
          {calls.map((call) => <li key={call.id} data-status={call.status}>
            <span className="agent-tool-codemode__call-status" aria-label={codeModeCallStatusLabel(call.status)}>{codeModeCallStatusMark(call.status)}</span>
            <span className="agent-tool-codemode__call-main">
              <strong>{call.name}</strong>
              <code>{call.args || '{}'}</code>
              {call.error ? <small role="alert">{call.error}</small> : null}
            </span>
            <span className="agent-tool-codemode__call-meta">
              {call.durationMs !== undefined ? formatCodeModeDuration(call.durationMs) : null}
              {call.cost !== undefined ? formatCodeModeCost(call.cost) : null}
            </span>
          </li>)}
        </ol>
      </div> : null}
      {view.output ? <div className="agent-tool-codemode__section">
        <strong>最终输出</strong>
        <pre aria-label="代码执行最终输出" tabIndex={0}><code>{view.output.text}</code></pre>
        {view.output.truncated ? <small>输出已截断；完整返回仍由本机回执保留。</small> : null}
      </div> : null}
      {view.codeMode?.fullOutputPath ? <small className="agent-tool-codemode__path">完整输出：<code>{view.codeMode.fullOutputPath}</code></small> : null}
    </section>
  );
}

function codeModeCallStatusLabel(status: NonNullable<PublicToolResultView['codeMode']>['calls'][number]['status']): string {
  return ({ running: '运行中', ok: '成功', error: '失败', cancelled: '已取消' } as Record<typeof status, string>)[status];
}

function codeModeCallStatusMark(status: NonNullable<PublicToolResultView['codeMode']>['calls'][number]['status']): string {
  return ({ running: '…', ok: '✓', error: '!', cancelled: '×' } as Record<typeof status, string>)[status];
}

function formatCodeModeDuration(value: number): string {
  if (value < 1_000) return `${Math.round(value)}ms`;
  return `${(value / 1_000).toFixed(value >= 10_000 ? 0 : 1)}s`;
}

function formatCodeModeCost(value: number): string {
  return `$${value < 0.01 ? value.toFixed(3) : value.toFixed(2)}`;
}

function PublicResultList({
  sessionId = '',
  view,
  label,
  icon,
}: {
  sessionId?: string;
  view: PublicToolResultView;
  label: string;
  icon: ReactNode;
}) {
  const itemText = (item: PublicToolResultView['resultItems'][number]) => {
    if (!item.text) return item.label;
    return view.resultKind === 'matches'
      ? `${item.label}:${item.text}`
      : `${item.label}  ${item.text}`;
  };
  const copyText = view.resultItems
    .map(itemText)
    .join('\n');
  const { copy, state } = useCopyableText(copyText);
  return (
    <section
      aria-label={label}
      className="agent-tool-result-view agent-tool-list-result"
      data-result-kind={view.resultKind}
    >
      <header>
        <strong>{icon}{label}</strong>
        <span>
          <small>{view.resultItems.length} 项</small>
          <Button
            aria-live="polite"
            leadingIcon={state === 'copied' ? <Check size={13} /> : <Copy size={13} />}
            onClick={() => void copy()}
            size="small"
            variant="quiet"
          >
            {state === 'copied' ? '已复制' : '复制结果'}
          </Button>
        </span>
      </header>
      <ol>
        {view.resultItems.map((item) => (
          <li key={item.id}>
            <strong>
              {(() => {
                const target = workspaceFileEntity(sessionId, item.path, item.label);
                return target ? <WorkspaceFileLink target={target}>{item.label}</WorkspaceFileLink> : item.label;
              })()}
            </strong>
            {item.text ? <span>{view.resultKind === 'matches' ? ':' : null}{item.text}</span> : null}
          </li>
        ))}
      </ol>
      {view.output?.truncated ? <small>{view.rawResult ? '列表摘要已截断；可展开下方“完整返回”查看原始回执。' : '完整结果仍由本机工具回执保留。'}</small> : null}
      {state === 'failed' ? <small role="alert">无法复制结果，请手动选择内容。</small> : null}
    </section>
  );
}

function workspaceFileEntity(
  sessionId: string,
  path: string | undefined,
  label: string,
): EvidenceEchoEntity | undefined {
  if (!sessionId || !path) return undefined;
  return {
    appId: 'files',
    entityId: path,
    label,
    sessionId,
  };
}

function WorkspaceFileLink({
  target,
  children,
}: {
  target: EvidenceEchoEntity;
  children: ReactNode;
}) {
  const desktop = usePawOsDesktop();
  return (
    <a
      aria-label={`打开文件 ${target.label}`}
      className="agent-tool-file-link"
      href={evidenceEchoRoute(target)}
      onClick={(event) => {
        event.preventDefault();
        openEvidenceEchoEntity(desktop, target);
      }}
      title={target.entityId}
    >
      {children}
      <FileText aria-hidden="true" size={12} />
    </a>
  );
}

function PublicChangeResult({ view }: { view: PublicToolResultView }) {
  return (
    <section
      aria-label="文件变更结果"
      className="agent-tool-result-view agent-tool-change-result"
      data-result-kind="change"
    >
      <span aria-hidden="true"><GitBranch size={17} /></span>
      <span>
        <strong>{view.target || '项目文件'}</strong>
        <small>{view.summary}</small>
      </span>
      <dl aria-label="变更统计">
        <div data-tone="add"><dt>新增</dt><dd>+{view.change?.additions ?? 0}</dd></div>
        <div data-tone="delete"><dt>删除</dt><dd>−{view.change?.deletions ?? 0}</dd></div>
      </dl>
    </section>
  );
}

function InspectableToolResult({ activityId, view }: { activityId: string; view: PublicToolResultView }) {
  const raw = view.rawResult;
  const [open, setOpen] = useActivityDisclosure(`raw:${activityId}`, false);
  const [presence, setPresence] = useState(open);
  const detailId = `agent-tool-raw-${useId().replace(/:/gu, '')}`;
  if (!raw) return null;
  return (
    <details className="agent-tool-result-view agent-tool-raw-result" aria-label="完整工具返回" open={open || presence}>
      <summary
        aria-controls={detailId}
        aria-expanded={open}
        onClick={(event) => toggleDisclosurePreservingAnchor(event, setOpen)}
        onKeyDown={(event) => toggleDisclosureOnKeyPreservingAnchor(event, setOpen)}
      >
        <ChevronRight size={14} aria-hidden="true" />
        <strong>完整返回</strong>
        <small>{raw.format.toUpperCase()}</small>
      </summary>
      <SmoothDisclosureReveal
        id={detailId}
        onPresenceChange={setPresence}
        open={open}
      >
        <InspectableToolResultBody raw={raw} />
      </SmoothDisclosureReveal>
    </details>
  );
}

const RAW_RESULT_LINE_HEIGHT = 20;
const RAW_RESULT_VIEWPORT_HEIGHT = 320;
const RAW_RESULT_OVERSCAN = 10;
const RAW_RESULT_VIRTUALIZE_AT = 240;

function InspectableToolResultBody({
  raw,
}: {
  raw: NonNullable<PublicToolResultView['rawResult']>;
}) {
  const content = useMemo(
    () => inspectableRawResultText(raw.value, raw.format),
    [raw],
  );
  const lines = useMemo(() => content.split('\n'), [content]);
  const { copy, state } = useCopyableText(content);
  return (
    <div className="agent-tool-raw-result__body">
      <header>
        <small>{lines.length.toLocaleString()} 行 · {content.length.toLocaleString()} 字符</small>
        <Button
          aria-live="polite"
          leadingIcon={state === 'copied' ? <Check size={13} /> : <Copy size={13} />}
          onClick={() => void copy()}
          size="small"
          variant="quiet"
        >
          {state === 'copied' ? '已复制完整返回' : '复制完整返回'}
        </Button>
      </header>
      {lines.length >= RAW_RESULT_VIRTUALIZE_AT
        ? <VirtualizedRawResult lines={lines} />
        : <pre aria-label="完整工具返回内容" role="region" tabIndex={0}><code>{content}</code></pre>}
      {state === 'failed' ? <small role="alert">无法复制完整返回，请手动选择内容。</small> : null}
    </div>
  );
}

function VirtualizedRawResult({ lines }: { lines: string[] }) {
  const [scrollTop, setScrollTop] = useState(0);
  const frameRef = useRef<number | null>(null);
  const pendingScrollTopRef = useRef(0);
  useEffect(() => () => {
    if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
  }, []);
  const visibleCount = Math.ceil(RAW_RESULT_VIEWPORT_HEIGHT / RAW_RESULT_LINE_HEIGHT);
  const start = Math.max(0, Math.floor(scrollTop / RAW_RESULT_LINE_HEIGHT) - RAW_RESULT_OVERSCAN);
  const end = Math.min(lines.length, start + visibleCount + (RAW_RESULT_OVERSCAN * 2));
  const visibleText = useMemo(() => lines.slice(start, end).join('\n'), [end, lines, start]);
  const onScroll = (event: UIEvent<HTMLDivElement>) => {
    pendingScrollTopRef.current = event.currentTarget.scrollTop;
    if (frameRef.current !== null) return;
    frameRef.current = requestAnimationFrame(() => {
      frameRef.current = null;
      setScrollTop(pendingScrollTopRef.current);
    });
  };
  return (
    <div
      aria-label="完整工具返回内容"
      className="agent-tool-raw-result__virtual-scroll"
      onScroll={onScroll}
      role="region"
      tabIndex={0}
    >
      <div style={{ height: lines.length * RAW_RESULT_LINE_HEIGHT }}>
        <pre style={{ transform: `translateY(${start * RAW_RESULT_LINE_HEIGHT}px)` }}>
          <code>{visibleText}</code>
        </pre>
      </div>
    </div>
  );
}

export function PublicToolFields({ view }: { view: PublicToolResultView }) {
  const fieldsText = JSON.stringify(
    Object.fromEntries(view.fields.map((field) => [field.id, field.value])),
    null,
    2,
  );
  const { copy, state } = useCopyableText(fieldsText);
  if (view.preview) return null;
  if (view.fields.length === 0) {
    return view.error
      ? null
      : <p>工具没有返回额外的结构化明细。</p>;
  }
  return (
    <section className="agent-tool-result-panel" aria-label="工具结果明细">
      <header className="agent-tool-result-panel__header">
        <strong><TerminalSquare size={13} />结果明细</strong>
        <Button
          aria-live="polite"
          leadingIcon={state === 'copied' ? <Check size={13} /> : <Copy size={13} />}
          onClick={() => void copy()}
          size="small"
          variant="quiet"
        >
          {state === 'copied' ? '已复制明细' : '复制明细'}
        </Button>
      </header>
      <dl className="agent-safe-fields">
        {view.fields.map((field) => (
          <div key={field.id}><dt>{field.label}</dt><dd>{field.value}</dd></div>
        ))}
      </dl>
      {state === 'failed' ? <small role="alert">无法复制明细，请手动选择内容。</small> : null}
    </section>
  );
}

export function PublicToolRequest({ view }: { view: PublicToolResultView }) {
  const requestText = JSON.stringify(
    Object.fromEntries(view.request.map((field) => [field.id, field.value])),
    null,
    2,
  );
  const { copy, state } = useCopyableText(requestText);
  return (
    <section className="agent-tool-result-panel" aria-label="工具调用参数">
      <header className="agent-tool-result-panel__header">
        <strong><TerminalSquare size={13} />调用参数</strong>
        <Button
          aria-live="polite"
          leadingIcon={state === 'copied' ? <Check size={13} /> : <Copy size={13} />}
          onClick={() => void copy()}
          size="small"
          variant="quiet"
        >
          {state === 'copied' ? '已复制参数' : '复制参数'}
        </Button>
      </header>
      <dl className="agent-tool-request">
        {view.request.map((field) => (
          <div key={field.id}>
            <dt>{field.label}</dt>
            <dd>{field.code ? <code>{field.value}</code> : field.value}</dd>
          </div>
        ))}
      </dl>
      {state === 'failed' ? <small role="alert">无法复制参数，请手动选择内容。</small> : null}
    </section>
  );
}

/** A unified-diff body is only worth expanding when it reads as a diff. The
 * structured reader needs at least one hunk header or a file header pair;
 * anything else stays in the plain bounded fragment. */
function outputReadsAsUnifiedDiff(value: string): boolean {
  return /^@@ -\d[\d,]* \+\d[\d,]* @@/mu.test(value)
    || (/^--- /mu.test(value) && /^\+\+\+ /mu.test(value));
}

export function PublicToolOutput({ view }: { view: PublicToolResultView }) {
  const outputText = view.output?.text ?? '';
  const outputLabel = view.outputLabel ?? '返回片段';
  const { copy, state } = useCopyableText(outputText);
  if (!view.output) return null;
  // An edit/patch receipt's 变更差异 opens into the same structured diff
  // reader the conversation diff blocks use (unified/split, per-file counts),
  // not a flat +/- text wall. Room tool rows consume this exact component,
  // so Session and Room chronology upgrade together (PF-CM-004/007).
  if (view.outputLabel === '变更差异' && outputReadsAsUnifiedDiff(outputText)) {
    return (
      <section className="agent-tool-result-panel agent-tool-diff-output" aria-label={`工具${outputLabel}`}>
        <header className="agent-tool-result-panel__header">
          <strong><FileDiff size={13} />{outputLabel}</strong>
        </header>
        <DiffPreview
          content={outputText}
          disclosureRegionLabel={`${outputLabel}正文`}
          fileName={view.target ?? ''}
        />
        {view.output.truncated ? (
          <small>此处显示安全截断片段；完整结果仍由本机工具回执保留。</small>
        ) : null}
      </section>
    );
  }
  return (
    <section className="agent-tool-result-panel" aria-label={`工具${outputLabel}`}>
      <header className="agent-tool-result-panel__header">
        <strong><TerminalSquare size={13} />{outputLabel}</strong>
        <Button
          aria-live="polite"
          leadingIcon={state === 'copied' ? <Check size={13} /> : <Copy size={13} />}
          onClick={() => void copy()}
          size="small"
          variant="quiet"
        >
          {state === 'copied' ? '已复制结果' : '复制结果'}
        </Button>
      </header>
      <pre aria-label={view.outputLabel ? `${view.outputLabel}正文` : '工具返回内容'} role="region" tabIndex={0}>{outputText}</pre>
      {view.output.truncated ? (
        <small>此处显示安全截断片段；完整结果仍由本机工具回执保留。</small>
      ) : null}
      {state === 'failed' ? <small role="alert">无法复制结果，请选择内容后手动复制。</small> : null}
    </section>
  );
}

function SemanticToolPreview({ preview }: { preview: NonNullable<PublicToolResultView['preview']> }) {
  return (
    <section className="agent-tool-preview" data-kind={preview.kind} aria-label={`${preview.title}内容`}>
      <header>
        <span><BookOpenText size={17} /></span>
        <div>
          <strong>{preview.title}</strong>
          {preview.description ? <p>{preview.description}</p> : null}
        </div>
      </header>
      {preview.badges.length ? <ul className="agent-tool-preview__badges">{preview.badges.map((badge) => <li key={badge}>{badge}</li>)}</ul> : null}
      {preview.items.length ? (
        <ol className="agent-tool-preview__items">
          {preview.items.map((item) => (
            <li key={item.id}>
              {item.label ? <span>{item.label}</span> : null}
              {item.href ? <a href={item.href}>{item.text}</a> : <p>{item.text}</p>}
            </li>
          ))}
        </ol>
      ) : null}
    </section>
  );
}

export function PublicToolError({ handoff, reason }: { handoff?: TraceAgentHandoffInput; reason: string }) {
  const { copy, state } = useCopyableText(reason);
  return (
    <section className="agent-tool-result-panel" data-tone="error" aria-label="工具失败">
      <header className="agent-tool-result-panel__header">
        <strong><TriangleAlert size={13} />失败原因</strong>
        <Button
          aria-live="polite"
          leadingIcon={state === 'copied' ? <Check size={13} /> : <Copy size={13} />}
          onClick={() => void copy()}
          size="small"
          variant="quiet"
        >
          {state === 'copied' ? '已复制错误' : '复制错误'}
        </Button>
        {handoff ? <TraceAgentHandoffButton handoff={handoff} /> : null}
      </header>
      <p>{reason}</p>
      {state === 'failed' ? <small role="alert">无法复制错误，请手动选择内容。</small> : null}
    </section>
  );
}

function SourceList({
  items,
  links,
}: {
  items: string[];
  links: Array<{ label: string; href: string }>;
}) {
  if (items.length === 0) return null;
  const hrefs = new Map(links.map((link) => [link.label, link.href]));
  return (
    <div className="agent-activity-row__source-panel">
      <strong><BookOpenText size={13} />信息来源</strong>
      <ul className="agent-activity-row__sources">
        {items.map((item) => (
          <li key={item}>{hrefs.has(item) ? <a href={hrefs.get(item)}>{item}</a> : item}</li>
        ))}
      </ul>
    </div>
  );
}

interface ActivityPresentation {
  title: string;
  kind: 'thinking' | 'tool' | 'rag' | 'memory' | 'subagent' | 'approval' | 'runtime';
  icon: LucideIcon;
  detail?: string;
}

function activityPresentation(activity: AgentActivityProjection): ActivityPresentation {
  const payload = activity.payload;
  const toolId = text(payload.toolId ?? payload.toolName).toLowerCase();
  const toolView = activity.kind.startsWith('tool_') ? publicToolResultView(activity) : null;
  const operation = toolView?.operation ?? text(payload.operation);
  if (activity.kind === 'reasoning_summary') {
    return { title: '处理说明', kind: 'thinking', icon: Brain };
  }
  if (activity.kind === 'route_decision' || routeDecisionPlanView(payload)) {
    return {
      title: '分派决定',
      kind: 'subagent',
      icon: Users,
      detail: '按信号与权重选择伙伴；下方是这次分派的真实计划。',
    };
  }
  if (activity.kind === 'turn_failed') {
    const retryAttempts = finiteCount(payload.providerRetryAttempts);
    const detail = payload.retryExhausted === true && retryAttempts > 0
      ? `已自动重试 ${retryAttempts} 次，模型服务仍未恢复；请稍后重试或切换模型。`
      : '模型请求没有完成；可返回对话重试或切换模型。';
    return { title: '模型服务请求失败', kind: 'runtime', icon: TriangleAlert, detail };
  }
  if (activity.kind.includes('approval') || activity.kind === 'user_input_required') {
    const decision = approvalDecisionView(payload);
    if (decision.mode === 'model') {
      const model = approvalModelLabel(decision.model);
      const arbiter = `独立审批 Agent（${model}）`;
      const settledTitle = decision.status === 'failed_closed'
        ? `${arbiter}无法形成可验证裁决，已拒绝这次操作`
        : decision.decision === 'approve'
          ? `${arbiter}已批准这次操作`
          : decision.decision === 'deny'
            ? `${arbiter}已拒绝这次操作`
            : '';
      return {
        title: settledTitle || `${arbiter}正在评估这次操作`,
        kind: 'approval',
        icon: Brain,
        detail: approvalDecisionDetail(decision),
      };
    }
    if (decision.mode === 'policy') {
      return {
        title: '安全策略已自动处理这次操作',
        kind: 'approval',
        icon: ShieldCheck,
        detail: '只有已授权范围内的常规受控操作会直接执行；权限、哈希与沙箱边界仍会再次校验。',
      };
    }
    const memoryReview = activity.kind === 'user_input_required' && payload.requestKind === 'memory_review';
    const genericInput = activity.kind === 'user_input_required' && !memoryReview;
    return {
      title: memoryReview ? '记忆草案审阅' : genericInput ? text(payload.title) || '等待你的回答' : '权限确认',
      kind: 'approval',
      icon: genericInput ? MessageSquareText : ShieldAlert,
      detail: memoryReview
        ? 'Agent 已暂停，等待你在审阅弹窗中处理草案。'
        : genericInput
          ? 'Agent 已暂停；回答、取消或超时后会继续当前回合。'
          : '是否执行以你的本机确认结果为准。',
    };
  }
  if (activity.kind.includes('memory') || toolId.includes('memory')) {
    const title = ({
      catalog: '浏览记忆目录',
      read: '读取工具书',
      recent: '读取近期输入',
      trace: '查看记忆追溯',
      curation_prepare: '生成记忆整理草案',
      maintenance_preview: '生成记忆整理草案',
      maintenance_review: '查看记忆整理草案',
      maintenance_apply: '应用记忆整理',
      maintenance_rollback: '回滚记忆整理',
      list: '浏览记忆',
      search: '检索记忆',
    } as Record<string, string>)[operation] ?? '记忆操作';
    return { title, kind: 'memory', icon: BookOpenText };
  }
  if (toolId.includes('knowledge') || toolId.includes('rag') || text(payload.operation) === 'search') {
    const title = toolId === 'knowledge'
      ? ({ list_bases: '浏览知识库', search: '检索文档', find: '定位文档证据', open: '读取文档片段', status: '检查知识库' } as Record<string, string>)[operation] ?? '文档知识库'
      : '知识检索';
    return { title, kind: 'rag', icon: Search };
  }
  if (toolId.includes('subagent') || activity.kind.includes('subagent')) {
    return { title: '协作 Agent', kind: 'subagent', icon: GitBranch };
  }
  if (toolId.includes('runtime')) {
    return { title: '运行环境', kind: 'runtime', icon: Database };
  }
  const family = publicToolFamily(toolId);
  if (family === 'collaboration') {
    return { title: toolView?.toolLabel ?? '协作', kind: 'subagent', icon: Users };
  }
  if (family === 'goal') {
    return { title: toolView?.toolLabel ?? '长期目标', kind: 'tool', icon: Target };
  }
  if (family === 'docs') {
    return { title: toolView?.toolLabel ?? '工作文档', kind: 'tool', icon: FileText };
  }
  if (family === 'job') {
    return { title: toolView?.toolLabel ?? '后台任务', kind: 'runtime', icon: Clock4 };
  }
  if (toolId.includes('workspace')) {
    return { title: toolView?.toolLabel ?? '运行环境', kind: 'runtime', icon: TerminalSquare };
  }
  if (toolId.includes('planning') || toolId === 'todo') return { title: toolId === 'todo' ? 'Todo' : '规划', kind: 'tool', icon: Bot };
  return { title: toolView?.toolLabel ?? '工具操作', kind: 'tool', icon: toolFamilyIcons[family] };
}

/** Per-family glyphs keep dense step rows scannable without repeating text.
 * The accessible name stays the full tool label; the glyph is decoration. */
const toolFamilyIcons: Record<AgentToolFamily, LucideIcon> = {
  browser: Globe,
  collaboration: Users,
  docs: FileText,
  file: FileText,
  goal: Target,
  job: Clock4,
  knowledge: Library,
  memory: Database,
  plan: ListChecks,
  runtime: Cpu,
  search: Search,
  terminal: TerminalSquare,
  generic: Wrench,
};

function approvalModelLabel(model: string): string {
  if (!model || /(?:^|[./_-])luna(?:$|[./_-])/i.test(model)) return 'Luna Max';
  return model.split('/').at(-1)?.slice(0, 80) || '审批模型';
}

function approvalDecisionDetail(
  decision: ApprovalDecisionView,
): string {
  const outcome = decision.status === 'failed_closed'
    ? '审批模型未形成可验证裁决，系统已按拒绝处理；原操作没有执行。'
    : decision.decision === 'approve'
      ? '审批 Agent 认为已绑定的操作预览可执行。'
      : decision.decision === 'deny'
        ? '审批 Agent 认为这次操作不应执行；原操作没有获得执行权限。'
        : '审批 Agent 只读取用户请求、当前任务、结构化预览和既有裁决，不读取当前 Agent 的输出或推理；无需人工操作。';
  const rationale = decision.rationaleSummary
    ? ` 裁决说明：${decision.rationaleSummary}`
    : '';
  const reasons = decision.reasonCodes.length
    ? ` 判定依据：${decision.reasonCodes.map(approvalDecisionReasonLabel).join('、')}。`
    : '';
  const history = decision.historyEntryCount !== null
    ? ` 已参考 ${decision.historyEntryCount} 条同一${decision.contextKind === 'room' ? ' Room' : ' Session'} 审批历史。`
    : '';
  const receipt = decision.receiptId ? ` 决策回执：${decision.receiptId}。` : '';
  return `${outcome}${rationale}${reasons}${history}${receipt}`;
}

function publicActivitySummary(value: string, fallback: string): string {
  const summary = value.trim();
  if (!summary) return `${fallback}已更新`;
  if (/^[a-z][a-z0-9_.:/-]*$/i.test(summary) || /(?:session|participant|activity|event|tool_call|tool_result)/i.test(summary)) {
    return `${fallback}已更新`;
  }
  return summary;
}

function aggregateSummary(activities: AgentActivityProjection[]): string {
  const counts = new Map<string, number>();
  for (const activity of activities) {
    const label = activityPresentation(activity).title;
    counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([label, count]) => `${label} ${count}`)
    .join(' · ');
}

function compactToolSummary(activities: AgentActivityProjection[]) {
  const calls = new Map<string, AgentActivityProjection['status']>();
  const names = new Set<string>();
  let latestResult = '';
  let latestError = '';
  for (const activity of activities) {
    if (!['tool_started', 'tool_progress', 'tool_finished'].includes(activity.kind)) continue;
    calls.set(text(activity.payload.toolCallId) || activity.id, activity.status);
    names.add(activityPresentation(activity).title);
    const view = publicToolResultView(activity);
    if (view.summary) latestResult = boundedInlineSummary(view.summary);
    if (view.error) latestError = boundedInlineSummary(view.error);
  }
  const statuses = [...calls.values()];
  const completed = statuses.filter((status) => status === 'completed').length;
  const failed = statuses.filter((status) => status === 'failed').length;
  const waiting = statuses.filter((status) => status === 'waiting').length;
  const running = statuses.filter((status) => status === 'running').length;
  const outcome = [
    completed ? `${completed} 已完成` : '',
    failed ? `${failed} 失败` : '',
    waiting ? `${waiting} 待确认` : '',
    running ? `${running} 进行中` : '',
  ].filter(Boolean).join(' · ');
  return {
    count: calls.size,
    names: [...names].slice(0, 3).join(' · '),
    outcome,
    highlight: latestError || latestResult,
  };
}

function boundedInlineSummary(value: string, limit = 180): string {
  const normalized = value.replace(/\s+/g, ' ').trim();
  if (normalized.length <= limit) return normalized;
  return `${normalized.slice(0, Math.max(0, limit - 1)).trimEnd()}…`;
}

function statusLabel(status: AgentActivityProjection['status']): string {
  switch (status) {
    case 'running': return '进行中';
    case 'waiting': return '待确认';
    case 'failed': return '失败';
    case 'completed': return '完成';
  }
}

function activityDuration(activity: AgentActivityProjection, nowMs: number): string {
  const endMs = activity.status === 'running' ? nowMs : activity.updatedAtMs;
  const elapsedMs = Math.max(0, endMs - activity.createdAtMs);
  // A restored activity with an invalid epoch should not display a fantastical
  // timer, and a receipt without measurable elapsed time is not "0 ms" work.
  if (
    !Number.isFinite(elapsedMs)
    || elapsedMs === 0
    || (activity.status === 'running' && elapsedMs > 7 * 24 * 60 * 60 * 1_000)
  ) {
    return '';
  }
  return elapsedLabel(elapsedMs);
}

function checkpointOffset(createdAtMs: number, startedAtMs: number): string {
  return `+${elapsedLabel(Math.max(0, createdAtMs - startedAtMs))}`;
}

function useCopyableText(value: string): {
  copy: () => Promise<void>;
  state: 'idle' | 'copied' | 'failed';
} {
  const [receipt, setReceipt] = useState<{
    state: 'copied' | 'failed';
    value: string;
  } | null>(null);
  const state = receipt?.value === value ? receipt.state : 'idle';
  return {
    state,
    copy: async () => {
      try {
        await writeClipboardText(value);
        setReceipt({ state: 'copied', value });
      } catch {
        setReceipt({ state: 'failed', value });
      }
    },
  };
}


/**
 * Raw milliseconds stop being information almost immediately: a tool that has
 * been running for two minutes rendered as "98530095 ms", which a reader has to
 * decode before learning anything, and which looks like a bug even when the
 * number is correct. Milliseconds are kept only where they are the honest unit
 * — sub-second work, where "0.1 s" would round away the detail being reported.
 */
function elapsedLabel(elapsedMs: number): string {
  const ms = Math.max(0, Math.round(elapsedMs));
  if (ms < 1_000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1_000).toFixed(ms < 10_000 ? 1 : 0)} 秒`;
  if (ms < 3_600_000) {
    const minutes = Math.floor(ms / 60_000);
    const seconds = Math.round((ms % 60_000) / 1_000);
    return seconds ? `${minutes} 分 ${seconds} 秒` : `${minutes} 分`;
  }
  const hours = Math.floor(ms / 3_600_000);
  const minutes = Math.round((ms % 3_600_000) / 60_000);
  return minutes ? `${hours} 小时 ${minutes} 分` : `${hours} 小时`;
}


function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function finiteCount(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value)
    ? Math.max(0, Math.floor(value))
    : 0;
}

/** One quiet row per authoritative activity, in transcript order. */
export function FxActivityStack({
  activities,
  sessionId = '',
  onApprovalDecision,
  onOpenApproval,
  onRequestPermission,
}: {
  activities: AgentActivityProjection[];
  sessionId?: string;
  onApprovalDecision?: (approvalId: string, decision: 'approved' | 'rejected', hash: string) => void;
  onOpenApproval?: (activity: AgentActivityProjection) => void;
  onRequestPermission?: () => void;
}) {
  const firstActivity = activities[0];
  const disclosureKey = `fx-stack:${sessionId}:${firstActivity?.turnId ?? 'empty'}:${firstActivity?.id ?? 'empty'}`;
  const longStack = activities.length >= 5;
  const [stackOpen, setStackOpen] = useActivityDisclosure(
    disclosureKey,
    !longStack,
  );
  // Decide density on first presentation only. A stream crossing five items
  // must not collapse the work someone is following or selecting.
  const running = activities.some((activity) => activity.status === 'running');
  const { rootRef, motionActive } = useActivityVisibility(running);
  const listId = `paw-activity-stack-${useId().replace(/:/gu, '')}`;
  if (!activities.length) return null;
  const waiting = activities.some((activity) => activity.status === 'waiting');
  const failedCount = activities.filter((activity) => activity.status === 'failed').length;
  const stoppedCount = activities.filter((activity) => activity.settledByTurnStatus === 'aborted').length;
  const compactStatus = [
    running ? '步骤进行中' : waiting ? '步骤等待确认' : failedCount || stoppedCount ? '步骤已结束' : '步骤已完成',
    failedCount ? `${failedCount} 项失败` : '',
    stoppedCount ? `${stoppedCount} 项停止` : '',
    `${activities.length} 个步骤`,
  ].filter(Boolean).join(' · ');
  return (
    <div
      aria-label="工具与思考步骤"
      className="paw-activity-stack"
      data-count={activities.length}
      data-expanded={stackOpen || undefined}
      data-motion={motionActive ? 'active' : 'paused'}
      ref={rootRef}
      role="group"
    >
      <div className="paw-activity-stack__toolbar">
        {!stackOpen ? (
          <span aria-live="polite" className="paw-activity-stack__status">{compactStatus}</span>
        ) : null}
        <button
          aria-controls={listId}
          aria-expanded={stackOpen}
          aria-label={`${stackOpen ? '全部收起' : '全部展开'}工具与思考步骤，共 ${activities.length} 项`}
          className="paw-activity-stack__toggle"
          onClick={(event) => toggleDisclosurePreservingAnchor(event, setStackOpen)}
          onKeyDown={(event) => toggleDisclosureOnKeyPreservingAnchor(event, setStackOpen)}
          type="button"
        >
          <span>{stackOpen ? '全部收起' : '全部展开'}</span>
          <ChevronRight aria-hidden="true" size={13} />
        </button>
      </div>
      <SmoothDisclosureReveal
        ariaLabel="工具与思考步骤列表"
        className="paw-activity-stack__reveal"
        id={listId}
        open={stackOpen}
        role="tree"
      >
        {activities.map((activity, index) => (
          <FxActivityDisclosure
            activity={activity}
            motionActive={motionActive && stackOpen}
            sessionId={sessionId}
            key={activity.id}
            onApprovalDecision={onApprovalDecision}
            onOpenApproval={onOpenApproval}
            onRequestPermission={onRequestPermission}
            position={index + 1}
            setSize={activities.length}
          />
        ))}
      </SmoothDisclosureReveal>
    </div>
  );
}

function FxActivityDisclosure({
  activity,
  motionActive,
  sessionId = '',
  onApprovalDecision,
  onOpenApproval,
  onRequestPermission,
  position,
  setSize,
}: {
  activity: AgentActivityProjection;
  motionActive: boolean;
  sessionId?: string;
  onApprovalDecision?: (approvalId: string, decision: 'approved' | 'rejected', hash: string) => void;
  onOpenApproval?: (activity: AgentActivityProjection) => void;
  onRequestPermission?: () => void;
  position: number;
  setSize: number;
}) {
  const failed = activity.status === 'failed';
  const waiting = activity.status === 'waiting';
  const running = activity.status === 'running';
  const isToolRow = ['tool_started', 'tool_progress', 'tool_finished'].includes(activity.kind);
  /* Tool details never auto-open. Status, duration and progress remain in the
   * compact summary row; parameters, output and process history require an
   * explicit human disclosure. Non-tool lifecycle rows retain their live
   * behavior because they are the conversation state rather than tool data. */
  const [manuallyOpen, setManuallyOpen] = useActivityDisclosure(
    `fx:${sessionId}:${activity.turnId}:${activity.id}`,
    !isToolRow && failed,
  );
  const open = manuallyOpen || (!isToolRow && (running || waiting));
  const previousStatusRef = useRef(activity.status);
  useEffect(() => {
    const previous = previousStatusRef.current;
    previousStatusRef.current = activity.status;
    if (!isToolRow && activity.status === 'failed' && previous !== 'failed') setManuallyOpen(true);
  }, [activity.status, isToolRow, setManuallyOpen]);
  const detailId = `paw-activity-detail-${useId().replace(/:/gu, '')}`;
  const toolView = useMemo(
    () => (isToolRow ? publicToolResultView(activity) : null),
    [activity, isToolRow],
  );
  const label = fxActivityLabel(activity);
  const action = fxActivityAction(activity, toolView);
  const hint = fxActivityHint(activity, toolView, label);
  const Glyph = fxActivityGlyph(activity);
  const glyphKind = fxGlyphKind(activity);
  /* Subagent receipts land after background work; the violet tone separates
     "another Agent finished this for you" from the parent's own tool calls. */
  const subagent = isSubagentActivity(activity);
  const stopped = activity.settledByTurnStatus === 'aborted';
  const tone = failed ? 'danger' : waiting || stopped ? 'wait' : running ? 'run' : subagent ? 'vio' : 'ok';
  const statusText = stopped ? '已停止' : failed ? '失败' : waiting ? '等待确认' : running ? '进行中' : subagent ? '后台完成' : '完成';
  const nowMs = useActivityClock(running && motionActive);
  // A Tool receipt with an explicit duration stays authoritative; otherwise a
  // live row shows its real elapsed clock and a settled row its measured span.
  const meta = activityReceiptMeta(activity, nowMs, toolView);
  const progress = activityProgressView(activity);
  return (
    <div
      aria-level={1}
      aria-posinset={position}
      aria-setsize={setSize}
      className="paw-activity-node"
      data-state={activity.status}
      role="treeitem"
    >
      <button
        aria-controls={detailId}
        aria-expanded={open}
        aria-label={`${label}${hint ? `，${hint}` : ''}，${statusText}${meta ? `，${meta}` : ''}`}
        className="paw-activity"
        data-state={activity.status}
        onClick={(event) => toggleDisclosurePreservingAnchor(event, setManuallyOpen)}
        onKeyDown={(event) => toggleDisclosureOnKeyPreservingAnchor(event, setManuallyOpen)}
        type="button"
      >
        <span className="paw-activity__row">
          <span aria-hidden="true" className="paw-activity__signal" data-state={activity.status}>
            {running ? (
              <ConversationPlanetMark
                motionActive={motionActive}
                state={activity.kind === 'reasoning_summary' ? 'thinking' : 'running'}
              />
            ) : <span className="paw-activity__glyph" data-kind={glyphKind}>{isToolRow ? <ToolStatusMark status={toolReceiptStatus(activity.status, activity.settledByTurnStatus)} size={14} active={motionActive} /> : <Glyph size={14} />}</span>}
          </span>
          <span className="paw-activity__label">{action || label}</span>
          {hint ? <span className="paw-activity__hint">{hint}</span> : null}
          <span className={`fx-pill ${tone}`} data-redundant={Boolean(action) && !failed && !waiting && !stopped || undefined}>
            {statusText}
          </span>
          {meta ? <span className="fx-meta">{meta}</span> : null}
          {progress ? <span
            aria-label={`${label}：${progress.label}`}
            aria-valuemax={100}
            aria-valuemin={0}
            aria-valuenow={progress.percent}
            aria-valuetext={progress.label}
            className="paw-activity__progress"
            data-state={activity.status}
            role="progressbar"
            style={{ '--paw-activity-progress': String(progress.fraction) } as CSSProperties}
          >
            <span aria-hidden="true" className="paw-activity__progress-track"><i /></span>
            <small>{progress.label}</small>
          </span> : null}
          <ChevronRight aria-hidden="true" className="paw-chevron" size={13} />
        </span>
      </button>
      <SmoothDisclosureReveal
        ariaLabel={`${label}详情`}
        className="paw-activity__detail"
        id={detailId}
        open={open}
        role="region"
      ><div className="fx-inner">
        <ActivityRow
          activity={activity}
          sessionId={sessionId}
          hideSummary
          initiallyOpen
          onApprovalDecision={onApprovalDecision}
          onOpenApproval={onOpenApproval}
          onRequestPermission={onRequestPermission}
        />
      </div></SmoothDisclosureReveal>
    </div>
  );
}

/** Pause decorative work outside the visible surface without changing Runtime
 * state. An OS-hidden document or an offscreen/minimized window needs no ticks. */
function useActivityVisibility(running: boolean) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(() => !document.hidden);
  useEffect(() => {
    if (!running) return undefined;
    let intersecting = true;
    const update = () => setVisible(!document.hidden && intersecting);
    const observer = typeof IntersectionObserver === 'undefined' ? null : new IntersectionObserver((entries) => {
      intersecting = entries.some((entry) => entry.isIntersecting);
      update();
    });
    if (rootRef.current) observer?.observe(rootRef.current);
    document.addEventListener('visibilitychange', update);
    update();
    return () => {
      observer?.disconnect();
      document.removeEventListener('visibilitychange', update);
    };
  }, [running]);
  return { rootRef, motionActive: running && visible };
}

const toolActionVerbs: Record<string, string> = {
  read: '读取', read_file: '读取', workspace_read: '读取',
  write: '写入', write_file: '写入', workspace_write_file: '写入',
  edit: '编辑', edit_file: '编辑', workspace_edit_file: '编辑', workspace_patch: '编辑',
  bash: '运行', shell: '运行', workspace_shell: '运行',
  grep: '搜索', workspace_search: '搜索',
  find: '查找', ls: '浏览', workspace_list: '浏览',
  skill_load: '读取技能', tool_load: '读取工具说明',
};

function fxActivityAction(activity: AgentActivityProjection, view: PublicToolResultView | null): string {
  if (!view) return '';
  if (activity.settledByTurnStatus === 'aborted') return '已停止';
  const verb = toolActionVerbs[view.toolId];
  if (!verb) return '';
  return activity.status === 'running' ? `正在${verb}`
    : activity.status === 'completed' ? `已${verb}`
      : activity.status === 'failed' ? `${verb}失败` : verb;
}

function isSubagentActivity(activity: AgentActivityProjection): boolean {
  return activity.kind.includes('subagent')
    || text(activity.payload.toolId ?? activity.payload.toolName).toLowerCase().includes('subagent');
}

/* A raw Runtime id such as `room_partner` is an implementation detail, not a
 * label. Every row resolves through the public tool vocabulary; a curated
 * displayName only survives when it reads like a human name. */
function fxActivityLabel(activity: AgentActivityProjection): string {
  const toolId = text(activity.payload.toolId ?? activity.payload.toolName);
  if (toolId) return publicToolName(toolId, text(activity.payload.displayName));
  const display = text(activity.payload.displayName);
  if (display) return display;
  if (activity.kind === 'reasoning_summary') return '思考过程';
  if (activity.kind === 'user_input_required') return '等待你的输入';
  if (activity.kind === 'turn_failed') return '本轮失败';
  if (activity.kind === 'approval') return '审批';
  if (activity.kind === 'route_decision') return '分派决定';
  return '操作';
}

function fxActivityGlyph(activity: AgentActivityProjection): LucideIcon {
  const toolId = text(activity.payload.toolId ?? activity.payload.toolName);
  if (isSubagentActivity(activity)) return GitBranch;
  if (activity.kind === 'route_decision') return Users;
  if (toolId) return toolFamilyIcons[publicToolFamily(toolId)];
  if (activity.kind === 'reasoning_summary') return Brain;
  if (activity.kind === 'user_input_required') return MessageSquareText;
  if (activity.kind === 'turn_failed') return TriangleAlert;
  if (activity.kind.includes('approval')) return ShieldAlert;
  return Wrench;
}

/** The concrete object of a step — target file, sent message, or result
 * summary — rides in the row so a reader can scan the tree without opening
 * every disclosure (PF-CM-007). */
function fxActivityHint(
  activity: AgentActivityProjection,
  toolView: PublicToolResultView | null,
  label: string,
): string {
  if (toolView && !toolView.error) {
    const request = (id: string) => toolView.request.find((field) => field.id === id)?.value ?? '';
    const family = publicToolFamily(toolView.toolId);
    const target = toolView.target || request('path');
    const query = request('query') || request('pattern') || request('glob');
    const subject = family === 'terminal' ? request('command')
      : family === 'file' ? target
        : family === 'search' ? [query ? `“${query}”` : '', target].filter(Boolean).join(' · ')
          : '';
    if (subject) return boundedInlineSummary(subject, 200);
  }
  const raw = toolView
    ? toolView.error || toolView.summary
    : activity.kind === 'turn_failed'
      ? publicAgentErrorText(activity.summary, '模型服务请求失败，请重试或切换模型。')
      : publicActivitySummary(publicProgressSummary(activity.summary, activity), label);
  // The row is one lane wide and ellipsises in CSS, so the bound only has to
  // stop an unbounded receipt from riding in the DOM — not decide how much of
  // it the reader gets to see. 96 characters was doing the latter.
  const hint = boundedInlineSummary(raw, 200);
  return hint === label || hint === `${label}已更新` ? '' : hint;
}

/** Stable tone family for the row glyph so collaboration, delegation, and
 * thinking rows separate visually inside a dense step tree. */
function fxGlyphKind(activity: AgentActivityProjection): string {
  if (activity.kind === 'reasoning_summary') return 'thinking';
  if (activity.kind.includes('approval') || activity.kind === 'user_input_required') return 'approval';
  if (activity.kind === 'route_decision') return 'collaboration';
  const toolId = text(activity.payload.toolId ?? activity.payload.toolName).toLowerCase();
  if (toolId === 'agents' || isSubagentActivity(activity)) return 'delegation';
  const family = publicToolFamily(toolId);
  if (family === 'collaboration') return 'collaboration';
  if (family === 'plan' || family === 'goal' || family === 'docs') return 'plan';
  if (family === 'memory' || family === 'knowledge') return 'memory';
  return 'tool';
}

function fxActivityMeta(activity: AgentActivityProjection): string {
  const durationMs = Number(activity.payload.durationMs ?? activity.payload.duration ?? 0);
  if (Number.isFinite(durationMs) && durationMs > 0) return `${(durationMs / 1000).toFixed(1)}s`;
  return '';
}

function activityReceiptMeta(
  activity: AgentActivityProjection,
  nowMs: number,
  toolView: PublicToolResultView | null,
): string {
  const duration = fxActivityMeta(activity) || activityDuration(activity, nowMs);
  const tokens = activityTokenReceipt(activity, toolView);
  return [duration, tokens].filter(Boolean).join(' · ');
}

/** Tokens belong to this activity only. Tool rows accept only the exact
 * tool_result.usage receipt carried by their own toolCallId. Old results do
 * not get a content-length estimate because that would look authoritative. */
function activityTokenReceipt(
  activity: AgentActivityProjection,
  toolView: PublicToolResultView | null,
): string {
  const usage = objectValue(activity.payload.usage);
  const isToolActivity = ['tool_started', 'tool_progress', 'tool_finished'].includes(activity.kind);
  if (isToolActivity) {
    const exactTotal = firstFiniteTokenCount(usage, [
      'totalTokens',
      'total_tokens',
      'tokens',
    ]);
    if (exactTotal !== null) return `${formatActivityTokenCount(exactTotal)} token`;
    const exactOutput = firstFiniteTokenCount(usage, [
      'outputTokens',
      'output_tokens',
      'completionTokens',
      'completion_tokens',
      'generatedTokens',
      'generated_tokens',
      'output',
    ]);
    if (exactOutput !== null) return `${formatActivityTokenCount(exactOutput)} token`;
    return activity.kind === 'tool_finished' ? '无独立统计' : '';
  }

  const exactOutput = firstFiniteTokenCount(usage, [
    'outputTokens',
    'output_tokens',
    'completionTokens',
    'completion_tokens',
    'generatedTokens',
    'generated_tokens',
    'output',
  ]);
  if (exactOutput !== null) return exactOutput > 0 ? `${formatActivityTokenCount(exactOutput)} token` : '';

  // Provider totals commonly include the whole prompt. Without a matching
  // output field, an input-bearing usage object cannot be attributed to this
  // row's return value and must not masquerade as Tool output.
  const inputBearingUsage = firstFiniteTokenCount(usage, [
    'inputTokens',
    'input_tokens',
    'promptTokens',
    'prompt_tokens',
    'input',
  ]) !== null;
  const exactScoped = inputBearingUsage ? null : firstFiniteTokenCount(usage, [
    'tokens',
    'totalTokens',
    'total_tokens',
  ]);
  if (exactScoped !== null) return exactScoped > 0 ? `${formatActivityTokenCount(exactScoped)} token` : '';

  const publicContent = activity.kind === 'reasoning_summary'
    ? reasoningItemsFromPayload(activity.payload, activity.summary).join('\n')
    : toolView
      ? publicToolReceiptText(toolView)
      : '';
  const estimated = estimatePublicTokenCount(publicContent);
  return estimated > 0 ? `约 ${formatActivityTokenCount(estimated)} token` : '';
}

function publicToolReceiptText(view: PublicToolResultView): string {
  const values = [
    view.output?.text ?? '',
    ...view.resultItems.flatMap((item) => [item.label, item.text]),
    view.preview?.description ?? '',
    ...(view.preview?.items.flatMap((item) => [item.label ?? '', item.text]) ?? []),
    view.error ?? '',
    ...view.fields
      .filter((field) => !['status', 'operation', 'ok'].includes(field.id))
      .flatMap((field) => [field.label, field.value]),
  ].map((value) => value.replace(/\s+/gu, ' ').trim()).filter(Boolean);
  return [...new Set(values)].join('\n');
}

function estimatePublicTokenCount(value: string): number {
  let cjk = 0;
  let other = 0;
  for (const character of value) {
    if (/\s/u.test(character)) continue;
    if (/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u.test(character)) cjk += 1;
    else other += 1;
  }
  return cjk + Math.ceil(other / 4);
}

function firstFiniteTokenCount(
  source: Record<string, unknown>,
  keys: string[],
): number | null {
  for (const key of keys) {
    if (!(key in source)) continue;
    const value = source[key];
    if (typeof value === 'number' && Number.isFinite(value) && value >= 0) {
      return Math.floor(value);
    }
  }
  return null;
}

function formatActivityTokenCount(value: number): string {
  if (value < 1_000) return String(value);
  const compact = value >= 10_000 ? Math.round(value / 1_000) : Math.round(value / 100) / 10;
  return `${compact}k`;
}

function objectValue(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

export interface ActivityProgressView {
  fraction: number;
  percent: number;
  label: string;
}

/** A determinate meter is shown only when the Tool receipt contains a bounded
 * fraction or an explicit current/total count. Elapsed time and DOM position
 * are intentionally not progress signals. */
export function activityProgressView(activity: AgentActivityProjection): ActivityProgressView | null {
  if (!['tool_started', 'tool_progress', 'tool_finished'].includes(activity.kind)) return null;
  const count = explicitProgressCount(activity.summary);
  const supplied = activity.payload.progress;
  const suppliedFraction = typeof supplied === 'number' && Number.isFinite(supplied)
    ? Math.min(1, Math.max(0, supplied))
    : null;
  const fraction = count
    ? Math.min(1, Math.max(0, count.current / count.total))
    : suppliedFraction;
  if (fraction === null) return null;
  const percent = Math.round(fraction * 100);
  return {
    fraction,
    percent,
    label: count?.label ?? `${percent}%`,
  };
}

function explicitProgressCount(summary: string): { current: number; total: number; label: string } | null {
  const match = summary.match(/(\d[\d,]*)\s*\/\s*(\d[\d,]*)(?:\s*([^\s,.，。:：;；/]{1,8}))?/u);
  if (!match) return null;
  const current = Number(match[1]?.replaceAll(',', ''));
  const total = Number(match[2]?.replaceAll(',', ''));
  if (!Number.isFinite(current) || !Number.isFinite(total) || current < 0 || total <= 0) return null;
  const unit = match[3]?.trim() ?? '';
  return {
    current,
    total,
    label: `${match[1]} / ${match[2]}${unit ? ` ${unit}` : ''}`,
  };
}
