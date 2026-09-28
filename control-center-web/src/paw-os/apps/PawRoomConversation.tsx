import { ArrowUpRight, Check, ChevronRight, Copy, GitBranch, ListChecks } from 'lucide-react';
import { useCallback, useMemo, useState, type ReactNode } from 'react';
import type { RoomActivityProjection, RoomProjectionState } from '@/contracts/room-reducer';
import { writeClipboardText } from '@/platform/clipboard';
import { publicAgentErrorText } from '@/features/agent/public-error';
import { PublicToolOutput } from '@/features/agent/timeline/ActivitySummary';
import {
  publicToolOutputText,
  publicToolResultView,
  type PublicToolResultView,
} from '@/features/agent/timeline/public-tool-result';
import { ConversationSurface } from '@/features/conversation-ui';
import {
  conversationClock,
  type ConversationSurfaceController,
} from '@/features/conversation-ui';
import {
  roomApprovalDecision,
  roomPhase,
  roomTranscript,
  roomTranscriptRetrySource,
} from '@/features/conversation-ui/adapters/room-transcript';
import type { AssistantBlock, AssistantMessage, ToolCallBlock } from '@/features/conversation-ui';
import { roomCollaborationRoleLabel } from '@/features/rooms/room-copy';
import type { RoomSummary, RoomWorkItem } from '@/features/rooms/room-types';
import { roomWorkStateLabel } from '@/features/rooms/room-presentation';
import { RoomPlanetAvatar } from '@/features/rooms/RoomPlanetAvatar';
import { openPawOsRoute, usePawOsDesktop } from '@/features/paw-os/surface-context';
import { JEV_TASK_STAGE_LABELS, type JevSnapshot, type JevTask } from '@/features/semantic-workspace/jev-execution';
import { TraceAgentHandoffButton } from '@/features/trace-agent/handoff';
import { runtimeToolWindowRequest } from '../runtime/runtime-tool-window';
import { roomFocusCelestialName } from './room-focus-projection';
import { readableManagedReadExcerpt, roomDispatchPlanFromActivity, roomEscapedManagedRead, roomToolEvidence } from './room-gravity-projection';
import { jevToolGroups, PawJevToolRecordDialog, PawJevToolRecords } from './PawJevToolRecords';
import { toolExecutionOutcome } from '@/features/conversation-ui/model/tool-receipt';
import './paw-room-conversation-navigation.css';

/**
 * The Room's public conversation, on the shared clean-room surface.
 *
 * The Room keeps every Runtime contract it owned before — one card per real
 * loop, pending approvals decided inline, background processes reachable, the
 * structured tool reader, retry only for the newest unsuperseded failure — but
 * the reading craft (pinned scroll, variable-height virtualization, turn
 * cards, tool receipts) is now the same code the planet observer and any
 * other PAWOS conversation mount.
 */
export function PawRoomConversation({
  empty,
  lead,
  tail,
  collaborationMode = 'room',
  graph,
  onApprovalDecision,
  onOpenProcessActivity,
  onRetryTurn,
  onContinueTurn,
  participantId,
  rootId,
  projection,
  readOnly = false,
  retryingTurn,
  room,
}: {
  onApprovalDecision?: (approvalId: string, decision: 'approved' | 'rejected', payloadSha256: string) => Promise<void>;
  onOpenProcessActivity?: (activity: RoomActivityProjection) => void;
  /** Only a mount that owns the Room composer can resend a failed request; a
   *  planet observer reads the same history without offering retry. */
  onRetryTurn?: (message: string, rootId: string) => void;
  /** Continue is a new Room prompt that uses the retained Room context. */
  onContinueTurn?: (rootId: string) => void;
  /** Restrict the transcript to one partner's public lane (planet view). */
  participantId?: string;
  rootId?: string;
  projection: RoomProjectionState;
  /** Observation-only mounts keep approval state visible but do not expose a
   * mutation control. The owning Room remains the intervention surface. */
  readOnly?: boolean;
  retryingTurn?: boolean;
  room: RoomSummary;
  lead?: ReactNode;
  tail?: ReactNode;
  collaborationMode?: 'room' | 'jev';
  graph?: JevSnapshot | null;
  empty?: ReactNode;
}) {
  const desktop = usePawOsDesktop();
  const [toolInspection, setToolInspection] = useState<{ blocks: ToolCallBlock[]; details: Record<string, ReactNode> } | null>(null);
  const actorName = useCallback((candidateId: string | null | undefined) => {
    const participant = candidateId
      ? room.participants.find((item) => item.id === candidateId)
      : undefined;
    return participant ? roomFocusCelestialName(participant.ordinal) : collaborationMode === 'jev' ? '团队' : 'Sol';
  }, [room.participants, collaborationMode]);
  const actorRole = useCallback((candidateId: string | null | undefined) => {
    const participant = candidateId
      ? room.participants.find((item) => item.id === candidateId)
      : undefined;
    return participant ? collaborationMode === 'jev'
      ? participant.collaborationRole === 'coordinator' ? '协调伙伴' : participant.collaborationRole === 'reviewer' ? '复核伙伴' : '协作伙伴'
      : roomCollaborationRoleLabel(participant.collaborationRole) : '';
  }, [room.participants, collaborationMode]);

  /* A dispatch names the real task it carries, not only a WorkItem id. */
  const workItemObjective = useCallback((workItemId: string) => (
    room.workItems?.find((item) => item.id === workItemId)?.objective ?? ''
  ), [room.workItems]);

  const transcript = useMemo(() => roomTranscript(projection, {
    actorName,
    actorRole,
    workItemObjective,
    rootId,
    jevGraph: collaborationMode === 'jev' ? graph : null,
    ...(participantId ? { participantId } : {}),
  }), [actorName, actorRole, collaborationMode, graph, participantId, rootId, projection, workItemObjective]);

  const boundParticipant = useCallback((id: string | null | undefined, sourceSessionId: string) => {
    if (!id || !sourceSessionId || projection.roomId !== room.id) return undefined;
    return room.participants.find((candidate) => candidate.id === id && candidate.sessionId === sourceSessionId);
  }, [projection.roomId, room.id, room.participants]);

  const dispatchTarget = useCallback((activity: RoomActivityProjection) => {
    const plan = roomDispatchPlanFromActivity(activity);
    if (!plan || !plan.targetParticipantId || activity.participantId !== plan.targetParticipantId) return undefined;
    const declaredSession = typeof activity.payload.targetSessionId === 'string' ? activity.payload.targetSessionId : '';
    if (declaredSession && declaredSession !== activity.sourceSessionId) return undefined;
    return boundParticipant(plan.targetParticipantId, activity.sourceSessionId);
  }, [boundParticipant]);

  const dispatchTask = useCallback((activity: RoomActivityProjection): RoomWorkItem | JevTask | undefined => {
    const plan = roomDispatchPlanFromActivity(activity);
    const target = dispatchTarget(activity);
    if (!plan || !target || !plan.dispatchId) return undefined;
    const jevDispatch = plan.routingPolicy === 'jev' || plan.dispatchId.startsWith('jev-');
    if (!jevDispatch) return room.workItems?.find((item) => item.id === plan.workItemId
      && item.roomId === room.id && item.rootTurnId === activity.turnId
      && item.currentOwnerParticipantId === target.id);
    if (!graph || graph.rootId !== activity.turnId) return undefined;
    const effect = graph.effects.find((candidate) => candidate.operation === 'dispatch'
      && candidate.request.graphId === graph.graphId
      && candidate.request.roomId === room.id
      && candidate.request.rootId === activity.turnId
      && candidate.request.dispatchId === plan.dispatchId
      && candidate.request.ownerId === target.id
      && candidate.request.sessionId === activity.sourceSessionId);
    const taskId = typeof effect?.request.taskId === 'string' ? effect.request.taskId : '';
    const revision = effect?.request.taskRevision;
    const subjectTaskId = typeof activity.payload.subjectTaskId === 'string' ? activity.payload.subjectTaskId : '';
    if (!taskId || (subjectTaskId && subjectTaskId !== taskId)) return undefined;
    return graph.tasks.find((item) => item.id === taskId && item.revision === revision
      && (plan.purpose === 'verify' || item.ownerId === target.id));
  }, [dispatchTarget, graph, room.id, room.workItems]);

  const renderBlockDetail = useCallback((block: AssistantBlock) => {
    const activity = transcript.activityByBlockId[block.id];
    if (!activity || block.kind !== 'tool') return undefined;
    const escapedRead = roomEscapedManagedRead(activity);
    if (escapedRead) return <section className="paw-room-managed-read" aria-label="受管资源读取结果">
      <p>已读取受管资源的一个片段。这是带转义符的机器文本；颜色控制符已从下面的预览中隐藏。</p>
      <details>
        <summary>查看整理后的片段</summary>
        <pre>{publicToolOutputText(readableManagedReadExcerpt(escapedRead.preview))}</pre>
      </details>
      {escapedRead.truncated ? <small>这里只显示本次公开的返回片段，原始工具回执仍保留。</small> : null}
    </section>;
    const plan = roomDispatchPlanFromActivity(activity);
    if (plan) {
      const task = dispatchTask(activity);
      const target = dispatchTarget(activity);
      return <div className="paw-room-dispatch-detail">
        {block.output ? <p className="paw-room-dispatch-detail__decision"><GitBranch size={16} aria-hidden /><span>{block.output}</span></p> : null}
        {task ? <details>
          <summary><ListChecks size={16} aria-hidden /><span>查看任务详情</span><ChevronRight className="paw-room-dispatch-detail__chevron" size={14} aria-hidden /></summary>
          <div className="paw-room-dispatch-detail__task">
            <strong>{task.objective}</strong>
            <small>当前任务状态：{'artifactRefs' in task ? roomWorkStateLabel(task.state) : JEV_TASK_STAGE_LABELS[task.state] ?? task.state} · 修订：{task.revision}</small>
            {task.expectedOutput ? <p>预期交付：{task.expectedOutput}</p> : null}
            <DispatchEvidenceLinks
              refs={taskReferences(task)}
              room={room}
              sessionId={'artifactRefs' in task || task.acceptedTurnId !== plan.dispatchId ? '' : target?.sessionId ?? ''}
            />
          </div>
        </details> : null}
        {plan.dispatchId || plan.workItemId && !task ? <details className="paw-room-dispatch-detail__runtime">
          <summary>运行记录<ChevronRight className="paw-room-dispatch-detail__chevron" size={14} aria-hidden /></summary>
          {plan.dispatchId ? <CopyableReference label="派遣标识" value={plan.dispatchId} /> : null}
          {plan.workItemId && !task ? <CopyableReference label="Room 工作项标识" value={plan.workItemId} /> : null}
        </details> : null}
      </div>;
    }
    const facts = roomToolEvidence(activity.payload)?.facts ?? [];
    if (transcript.reclaimedToolBlockIds.has(block.id)) return facts.length ? <dl className="paw-room-tool-facts">
      {facts.map((fact) => <div key={`${fact.label}:${fact.value}`}><dt>{fact.label === '失败原因' ? '原始停止回执' : fact.label}</dt><dd>{fact.value}</dd></div>)}
    </dl> : undefined;
    const uncertain = toolExecutionOutcome(activity.payload) === 'unknown';
    const view = roomToolResultView(activity);
    if (!facts.length && !view) return undefined;
    return <>
      {facts.length ? (
        <dl className="paw-room-tool-facts">
          {facts.map((fact) => <div key={`${fact.label}:${fact.value}`}><dt>{uncertain && fact.label === '失败原因' ? '回执异常' : fact.label}</dt><dd>{fact.value}</dd></div>)}
        </dl>
      ) : null}
      {view ? <PublicToolOutput view={view} /> : null}
    </>;
  }, [dispatchTarget, dispatchTask, room, transcript.activityByBlockId]);

  const renderBlockAction = useCallback((block: AssistantBlock) => {
    const activity = transcript.activityByBlockId[block.id];
    if (!activity) return undefined;
    const target = dispatchTarget(activity);
    const approval = !readOnly && onApprovalDecision ? roomApprovalDecision(activity) : undefined;
    const processWindow = !readOnly && onOpenProcessActivity ? roomProcessWindowRequest(activity, room.id) : null;
    if (!approval && !processWindow && !target) return undefined;
    return <>
      {target ? <a
        aria-label={`查看伙伴执行：${roomFocusCelestialName(target.ordinal)}`}
        className="paw-room-conversation__route-link"
        href={`#/agent?session=${encodeURIComponent(target.sessionId)}`}
        onClick={(event) => { event.preventDefault(); openPawOsRoute(desktop, `/agent?session=${encodeURIComponent(target.sessionId)}`); }}
      ><span>查看伙伴执行</span><ArrowUpRight size={14} aria-hidden /></a> : null}
      {approval ? (
        <RoomApprovalAction
          decision={onApprovalDecision!}
          participantId={activity.participantId}
          roomId={room.id}
          sourceSessionId={activity.sourceSessionId}
          turnId={activity.turnId}
          {...approval}
        />
      ) : null}
      {processWindow ? (
        <button onClick={() => onOpenProcessActivity?.(activity)} type="button">查看后台 Bash</button>
      ) : null}
    </>;
  }, [desktop, dispatchTarget, onApprovalDecision, onOpenProcessActivity, readOnly, room.id, transcript.activityByBlockId]);

  const toolGroups = useMemo(() => collaborationMode === 'jev' || Boolean(participantId)
    ? jevToolGroups(transcript.messages, block => {
      if (!block.id.startsWith('tool:')) return true;
      const activity = transcript.activityByBlockId[block.id];
      return Boolean(activity && (roomApprovalDecision(activity) || onOpenProcessActivity && roomProcessWindowRequest(activity, room.id)));
    }) : new Map(), [collaborationMode, participantId, onOpenProcessActivity, room.id, transcript]);
  const openToolRecords = useCallback((blocks: ToolCallBlock[]) => {
    setToolInspection({ blocks, details: Object.fromEntries(blocks.map(block => [block.id, renderBlockDetail(block)])) });
  }, [renderBlockDetail]);
  const renderBlock = useCallback((block: AssistantBlock) => {
    const group = toolGroups.get(block.id);
    if (group === null) return null;
    return group ? <PawJevToolRecords blocks={group} onOpen={openToolRecords} /> : undefined;
  }, [toolGroups, openToolRecords]);
  const currentTools = useMemo(() => new Map(transcript.messages.flatMap(message => message.role === 'assistant'
    ? message.blocks.filter((block): block is ToolCallBlock => block.kind === 'tool').map(block => [block.id, block] as const) : [])), [transcript.messages]);

  const controller = useMemo<ConversationSurfaceController>(() => ({
    conversationId: participantId ? `${room.id}:${participantId}:${rootId || "history"}` : room.id,
    messages: transcript.messages,
    phase: transcript.phase,
    capabilities: {
      /* Executed failed Room turns continue from retained context. Retry is a
       * compatibility fallback for callers without Continue. Fork, rewind and
       * message edit belong to a Session, not to shared Room history. */
      retry: Boolean(onRetryTurn || onContinueTurn) && !readOnly,
      edit: false,
      fork: false,
      rewind: false,
      interrupt: false,
      copy: true,
    },
    canRetry: (message: AssistantMessage) => Boolean(
      !readOnly && (onRetryTurn || onContinueTurn) && message.error && message.turnId && roomTranscriptRetrySource(projection, message.turnId),
    ),
    retry: (message: AssistantMessage) => {
      if (readOnly) return;
      const source = message.turnId ? roomTranscriptRetrySource(projection, message.turnId) : undefined;
      if (source) {
        if (onContinueTurn) onContinueTurn(source.rootId);
        else onRetryTurn?.(source.text, source.rootId);
      }
    },
      retryPending: !readOnly && Boolean(retryingTurn),
    renderBlockDetail,
    renderBlockAction,
    renderBlock,
    resolveMessageSessionId: (message) => boundParticipant(message.actorId, message.actorSessionId ?? '')?.sessionId ?? '',
    renderMessageAvatar: (message) => {
      const participant = room.participants.find(item => item.id === message.actorId);
      return participant ? <RoomPlanetAvatar ordinal={participant.ordinal} size={25} decorative /> : undefined;
    },
    renderMessageIdentity: (message) => {
      const participant = boundParticipant(message.actorId, message.actorSessionId ?? '');
      if (!participant) return undefined;
      return <a
        aria-label={`打开 ${message.actor} 的 Session`}
        className="paw-room-conversation__identity-link"
        href={`#/agent?session=${encodeURIComponent(participant.sessionId)}`}
        onClick={(event) => { event.preventDefault(); openPawOsRoute(desktop, `/agent?session=${encodeURIComponent(participant.sessionId)}`); }}
      ><RoomPlanetAvatar ordinal={participant.ordinal} size={25} decorative /><strong>{message.actor}</strong></a>;
    },
    readOnly,
    formatTimestamp: conversationClock,
  }), [
    onRetryTurn,
    onContinueTurn,
    boundParticipant,
    desktop,
    participantId,
    projection,
    renderBlockAction,
    renderBlockDetail,
    renderBlock,
    readOnly,
    retryingTurn,
    room.id,
    room.participants,
    transcript.messages,
    transcript.phase,
  ]);

  return <><ConversationSurface
    controller={controller}
    density={participantId ? 'compact' : 'comfortable'}
    label={participantId ? '行星公开对话' : 'Room 公开对话'}
    {...(lead ? { lead } : {})}
    {...(tail ? { tail } : {})}
    {...(empty ? { empty } : {})}
  /><PawJevToolRecordDialog
    open={Boolean(toolInspection)}
    blocks={toolInspection?.blocks.map(block => currentTools.get(block.id) ?? block) ?? []}
    onClose={() => setToolInspection(null)}
    renderDetail={block => renderBlockDetail(block) ?? toolInspection?.details[block.id]}
  /></>;
}

function taskReferences(task: RoomWorkItem | JevTask): string[] {
  return 'artifactRefs' in task
    ? [...task.artifactRefs, ...task.evidenceRefs]
    : [...task.artifacts, ...task.evidence];
}

function evidenceDestination(reference: string, room: RoomSummary, sessionId: string): {
  label: string;
  route: string;
} | undefined {
  const document = /^workdoc:(workdoc_[a-f0-9]{32})@\d+$/u.exec(reference);
  if (document) return { label: '打开工作文档', route: `/work-documents?document=${encodeURIComponent(document[1]!)}` };
  if (/^trace:[A-Za-z0-9:_-]{1,200}$/u.test(reference)) {
    return { label: '打开 Trace', route: `/observability?traceId=${encodeURIComponent(reference)}` };
  }
  const artifact = room.artifacts?.find((item) => item.id === reference && item.roomId === room.id);
  const source = artifact?.path ?? reference;
  const path = source.trim()
    .replace(/\s+(?:unchanged\s+)?sha256:[a-f\d]{64}$/iu, '')
    .replace(/#sha256:[a-f\d]{64}$/iu, '')
    .replace(/:\d+(?::\d+)?$/u, '');
  if (!sessionId || !path || /[\r\n\0?#]/u.test(path)
    || /(^|[\\/])\.\.(?:[\\/]|$)/u.test(path)
    || (/^[a-z][a-z\d+.-]*:/iu.test(path) && !/^[a-z]:[\\/]/iu.test(path))
    || !/(?:^|[\\/])[^\\/]+\.[a-z\d]{1,12}$/iu.test(path)) return undefined;
  return { label: '打开文件', route: `/files?${new URLSearchParams({ session: sessionId, path })}` };
}

function DispatchEvidenceLinks({ refs, room, sessionId }: { refs: string[]; room: RoomSummary; sessionId: string }) {
  const desktop = usePawOsDesktop();
  if (!refs.length) return null;
  return <section aria-label="任务证据与产物" className="paw-room-dispatch-detail__refs">
    <strong>证据与产物</strong>
    <ul>{refs.map((reference, index) => {
      const destination = evidenceDestination(reference, room, sessionId);
      return <li key={`${reference}:${index}`}>{destination
        ? <a
            href={`#${destination.route}`}
            onClick={(event) => { event.preventDefault(); openPawOsRoute(desktop, destination.route); }}
          >{destination.label}：{reference}</a>
        : <CopyableReference label="原始证据引用" value={reference} />}</li>;
    })}</ul>
  </section>;
}

function CopyableReference({ label, value }: { label: string; value: string }) {
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle');
  return <div className="paw-room-dispatch-detail__reference">
    <span>{label} · 来自 Room 运行记录，尚无可核实的页面位置</span>
    <code>{value}</code>
    <button
      aria-label={`复制${label}`}
      onClick={() => void writeClipboardText(value).then(
        () => setCopyState('copied'),
        () => setCopyState('failed'),
      )}
      type="button"
     data-copied={copyState === 'copied' || undefined}>{copyState === 'copied' ? <Check size={14} aria-hidden /> : <Copy size={14} aria-hidden />}<span aria-live="polite">{copyState === 'copied' ? '已复制' : '复制'}</span></button>
    {copyState === 'failed' ? <small role="alert">无法复制，请选择标识手动复制。</small> : null}
  </div>;
}

function RoomApprovalAction({ approvalId, decision, participantId, payloadSha256, roomId, sourceSessionId, turnId }: {
  approvalId: string;
  payloadSha256: string;
  decision: (approvalId: string, choice: 'approved' | 'rejected', payloadSha256: string) => Promise<void>;
  participantId: string | null;
  roomId: string;
  sourceSessionId: string;
  turnId: string;
}) {
  const [submitting, setSubmitting] = useState<'' | 'approved' | 'rejected'>('');
  const [error, setError] = useState('');
  const decide = (choice: 'approved' | 'rejected') => {
    if (submitting) return;
    setError('');
    setSubmitting(choice);
    void decision(approvalId, choice, payloadSha256)
      .catch((reason: unknown) => setError(publicAgentErrorText(reason)))
      .finally(() => setSubmitting(''));
  };
  return <>
    <button disabled={Boolean(submitting)} onClick={() => decide('approved')} type="button">
      {submitting === 'approved' ? '正在批准' : '批准并继续'}
    </button>
    <button disabled={Boolean(submitting)} onClick={() => decide('rejected')} type="button">
      {submitting === 'rejected' ? '正在拒绝' : '拒绝'}
    </button>
    {error ? (
      <small role="alert">
        {error}
        <TraceAgentHandoffButton
          handoff={{
            kind: 'room',
            entityId: approvalId,
            title: 'Room 审批操作失败',
            summary: error,
            error,
            ...(sourceSessionId ? { sessionId: sourceSessionId } : {}),
            roomId,
            sourceRoute: `/rooms?room=${encodeURIComponent(roomId)}`,
            refs: {
              approvalId,
              participantId,
              turnId,
            },
          }}
        />
      </small>
    ) : null}
  </>;
}

/** Room tool activities project through the exact Session tool-result view
 *  (`arguments` → `args`), so a diff/edit/write/read receipt expands into the
 *  same structured detail as the Session timeline (PF-CM-004/007). */
function roomToolResultView(activity: RoomActivityProjection): PublicToolResultView | null {
  const payload = activity.payload;
  const args = payload.args ?? payload.arguments;
  const view = publicToolResultView({
    kind: typeof payload.sourceEventType === 'string' ? payload.sourceEventType : activity.kind,
    status: activity.status,
    payload: {
      ...payload,
      ...(typeof args === 'object' && args !== null && !Array.isArray(args) ? { args } : {}),
    },
  });
  return view.output ? view : null;
}

export function roomProcessWindowRequest(activity: RoomActivityProjection, roomId: string) {
  const request = runtimeToolWindowRequest({
    eventType: 'participant_activity',
    roomId,
    ...(activity.participantId ? { participantId: activity.participantId } : {}),
    sourceSessionId: activity.sourceSessionId,
    payload: {
      sourceEventType: typeof activity.payload.sourceEventType === 'string'
        ? activity.payload.sourceEventType
        : activity.kind,
      data: activity.payload,
    },
  });
  return request?.target.kind === 'process-terminal'
    && Boolean(request.target.runId || request.target.terminalId)
    ? request
    : null;
}
