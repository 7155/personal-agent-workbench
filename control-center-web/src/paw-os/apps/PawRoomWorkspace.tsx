import { useRoomReadingRecovery, useRoomViewRecovery } from '@/features/semantic-workspace/reading-recovery';
import { recoveryScope, useWorkspaceRecovery, WorkspaceRecoveryNotice } from '@/features/semantic-workspace/workspace-recovery';
import { JevCompanion } from '@/features/semantic-workspace/JevCompanion';
import { JevPolicyControls } from '@/features/semantic-workspace/JevPolicyControls';
import { JevPlanReview } from '@/features/semantic-workspace/JevPlanReview';
import { PawRoomRemovalProgress, useRoomRemovals } from './PawRoomRemovalProgress';
import { jevRailHasContent, PawJevTeamPanels } from './PawJevTeamPanels';
import { RoomPlanetAvatar } from '@/features/rooms/RoomPlanetAvatar';
import { useJevExecution } from '@/features/semantic-workspace/use-jev-execution';
import { jevAbstention, jevStatusLabel, jevTaskCountLabel, pendingJevInput } from '@/features/semantic-workspace/jev-execution';
import {
  Archive,
  ChartGantt,
  ChevronDown,
  CircleAlert,
  ExternalLink,
  Focus,
  GitBranch,
  ListChecks,
  LoaderCircle,
  Maximize2,
  MessageCircle,
  Orbit,
  PanelRightClose,
  PanelRightOpen,
  PanelsTopLeft,
  Plus,
  Settings2,
  StopCircle,
  UserMinus,
  UserPlus,
  Users,
  X,
  type LucideIcon,
} from 'lucide-react';
import { useCallback, useEffect, useId, useMemo, useRef, useState, useSyncExternalStore, type KeyboardEvent } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger, Select } from '@/components/primitives';
import { isComposerAttachmentMimeType } from '@/contracts/attachment-policy';
import type { RoomActivityProjection, RoomAttachmentReceipt } from '@/contracts/room-reducer';
import type { AgentPersonaV1 } from '@/contracts/generated/agent-persona.v1';
import type { ControlRequest, PickedFile } from '@/platform/transport';
import { GenericUserInputCard } from '@/features/agent/review/AgentReviewDialogs';
import { QueueTray, useConversationQueue } from '@/features/conversation-ui';
import { mergeQueueBackToDraft } from '@/features/conversation-ui/model/queue';
import { usePawOsDesktop } from '@/features/paw-os/surface-context';
import { publicErrorText } from '@/features/overview/management-ui';
import {
  publicAgentErrorText,
  ROOM_WORKSPACE_MISSING_TEXT,
  SESSION_WORKSPACE_MISSING_TEXT,
} from '@/features/agent/public-error';
import { TraceAgentHandoffButton } from '@/features/trace-agent/handoff';
import { RoomComposer, roomMentionedParticipants } from '@/features/rooms/composer/RoomComposer';
import { RoomCapabilityControls } from '@/features/rooms/composer/RoomCapabilityControls';
import { roomCollaborationRoleLabel, roomPlanetName } from '@/features/rooms/room-copy';
import { latestPendingGroupedRoomInput, type PendingRoomQuestion } from '@/features/rooms/room-question';
import {
  RoomPermissionPolicyEditor,
  roomCollaborationRoleOptions,
  roomPermissionLayerPresentation,
  roomWorkStateLabel,
} from '@/features/rooms/room-presentation';
import {
  selectActivePublicRoomTurn,
  selectPublicRoomTurnOrder,
} from '@/features/rooms/runtime/room-execution-lanes';
import { roomSendJournal, type RoomSendAttempt } from '@/features/rooms/runtime/room-send-journal';
import { startRoomSend } from '@/features/rooms/application/room-send';
import { useRoomLiveSession } from '@/features/rooms/runtime/use-room-live-session';
import { usePageVisibility } from '@/platform/use-page-visibility';
import { PawWindowChromePortal, usePawWindowChromeTarget } from '../shell/PawWindowChrome';
import { roomCancellationOutcome } from '@/features/rooms/runtime/room-cancellation';
import { roomProjection, useRoomLiveStore } from '@/features/rooms/state/live-store';
import {
  parseRoomPermissionPolicy,
  roomPermissionPoliciesEqual,
  roomPermissionPolicyNeedsDangerousConfirmation,
  roomPermissionPolicyNeedsWorkspaceConfirmation,
  type RoomPermissionPolicy,
  type RoomSummary,
  type RoomWorkItem,
} from '@/features/rooms/room-types';
import { PawRoomConversation, roomProcessWindowRequest } from './PawRoomConversation';
import { PawRoomCollaboration } from './PawRoomCollaboration';
import { PawRoomResponseStatus } from './PawRoomResponseStatus';
import { PawRoomRoundSheet } from './PawRoomRoundSheet';
import { useRoomObserverAutoOpen, useRoomWorkStatusVisible } from './room-observer-preference';
import { PawRoomWorkStatus } from './PawRoomWorkStatus';
import { buildRoomWorkStatus } from './room-work-status';
import type { RoomRoundTaskRow } from './room-round-task-sheet';
/* 星空按钮按下之前，星空代码不进入 Room 默认对话的 bundle 路径。 */
import { LazyPawRoomStarfield } from './PawStarfieldLazy';
import { buildRoomFocusProjection, roomFocusHasCoordinator, roomFocusOriginLabel, type RoomFocusProjection } from './room-focus-projection';
import {
  roomCollaborationPlanetRequests,
  roomPartnerSessionWindowRequest,
  roomPlanetObserverWindowRequest,
} from './room-satellite-auto-open';
/* Shared conversation modules (tool result panels, diff reader) style the
 * Room's tool receipts too; the Room window must not depend on a Session
 * window having loaded them first. */
import '@/features/agent/agent.css';
import '@/features/rooms/rooms.css';
import './paw-jev-conversation.css';
import './paw-jev-mission.css';

export { PawRoomConversation } from './PawRoomConversation';

type RoomToolPanel = 'focus' | 'governance';

type OptimisticSteerReceipt = {
  clientActionId: string;
  message: string;
  participantId: string;
};

const roomToolPanelLabels: Record<RoomToolPanel, string> = {
  focus: '态势',
  governance: '治理',
};

const roomToolPanelIcons: Record<RoomToolPanel, LucideIcon> = {
  focus: Focus,
  governance: Settings2,
};
const roomToolPanelItems = Object.keys(roomToolPanelLabels) as RoomToolPanel[];

/* This is the active-participant ceiling enforced by agent-room.v1 and
   AgentRoomService. The displayed count still comes only from the current
   Room snapshot; this constant is a capacity rule, not a second roster. */
const ROOM_PARTICIPANT_LIMIT = 8;
const ROOM_TIMELINE_END_THRESHOLD_PX = 96;
/* Below this Room width the task rail floats over the conversation instead
   of taking a column away from it. */
const JEV_RAIL_OVERLAY_WIDTH = 900;
const JEV_RAIL_PREFERENCE_KEY = 'paw.jev.task-rail.v1';

function useJevRailPreference(): ['open' | 'closed', (value: 'open' | 'closed') => void] {
  const [value, setValue] = useState<'open' | 'closed'>(() => {
    try { return globalThis.localStorage?.getItem(JEV_RAIL_PREFERENCE_KEY) === 'open' ? 'open' : 'closed'; } catch { return 'closed'; }
  });
  const update = useCallback((next: 'open' | 'closed') => {
    setValue(next);
    try { globalThis.localStorage?.setItem(JEV_RAIL_PREFERENCE_KEY, next); } catch { /* presentation only */ }
  }, []);
  return [value, update];
}

function useContainerNarrow(ref: { current: HTMLElement | null }, width: number): boolean {
  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    const node = ref.current;
    if (!node || typeof ResizeObserver === 'undefined') return;
    const measure = () => setNarrow(node.clientWidth > 0 && node.clientWidth < width);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [ref, width]);
  return narrow;
}

export function followRoomTimelineIfReaderAtEnd(
  timeline: HTMLElement | null,
  scheduleFrame: (callback: FrameRequestCallback) => number = requestAnimationFrame,
): boolean {
  if (!timeline) return false;
  const readerIsAtEnd = () => (
    timeline.scrollHeight - timeline.clientHeight - timeline.scrollTop
    <= ROOM_TIMELINE_END_THRESHOLD_PX
  );
  if (!readerIsAtEnd()) return false;
  scheduleFrame(() => {
    if (!readerIsAtEnd()) return;
    if (typeof timeline.scrollTo === 'function') {
      timeline.scrollTo({ top: timeline.scrollHeight, behavior: 'smooth' });
    } else {
      timeline.scrollTop = timeline.scrollHeight;
    }
  });
  return true;
}

export function PawRoomWorkspace({
  active = true,
  interfaceMode = 'traditional',
  initialDraft,
  initialError,
  participantProcessLocation = 'session-window',
  personas,
  record,
  recordId,
  onRoomUpdated,
  onJevEvents,
}: {
  active?: boolean;
  interfaceMode?: 'traditional' | 'jev';
  initialDraft?: string;
  initialError?: string;
  /** Extension Apps can keep public Room inspection inside their own surface. */
  participantProcessLocation?: 'session-window' | 'room-transcript';
  personas: AgentPersonaV1[];
  record?: RoomSummary;
  recordId: string;
  onRoomUpdated: (room: RoomSummary) => void;
  onJevEvents?: (events: readonly unknown[]) => void;
}) {
  const transport = useControlTransport();
  const desktop = usePawOsDesktop();
  const windowChromeTarget = usePawWindowChromeTarget();
  const pageVisible = usePageVisibility();
  const jevEnabled = interfaceMode === 'jev' && record?.roomKind !== 'roleplay';
  const jev = useJevExecution({ roomId: recordId, transport, enabled: jevEnabled, active: pageVisible });
  const [observerAutoOpen, setObserverAutoOpen] = useRoomObserverAutoOpen();
  const [workStatusVisible, setWorkStatusVisible] = useRoomWorkStatusVisible();
  // A covered-but-open PAW window is still a live conversation. Focus only
  // controls interaction/animation; document visibility owns network pause.
  const liveActive = pageVisible;
  const timelineRef = useRef<HTMLDivElement>(null);
  const composerRegionRef = useRef<HTMLDivElement>(null);
  const runtimeWarmupSessionIdsRef = useRef(new Set<string>());
  const recovery = useWorkspaceRecovery<RoomAttachmentReceipt>(`room:${recordId}`, initialDraft ?? '');
  const { draft, setDraft, attachments, setAttachments } = recovery;
  const [sending, setSending] = useState(false);
  const sendJournal = roomSendJournal(transport, recordId);
  const pendingSend = useSyncExternalStore(sendJournal.subscribe, sendJournal.getSnapshot);
  const [optimisticSteer, setOptimisticSteer] = useState<OptimisticSteerReceipt | null>(null);
  const optimisticSteerRef = useRef<OptimisticSteerReceipt | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(initialError ?? '');
  const [connectionError, setConnectionError] = useState('');
  const [panel, setPanel] = useState<RoomToolPanel | 'none'>('none');
  const [partnerSettingsOpen, setPartnerSettingsOpen] = useState(false);
  const [controlsExpanded, setControlsExpanded] = useState(false);
  const [embeddedFocusActive, setEmbeddedFocusActive] = useState(false);
  const roomFocusGroup = `room:${recordId}`;
  const desktopFocusGroup = desktop?.collaborationFocusGroup;
  const hasDesktopFocusSource = desktopFocusGroup !== undefined;
  const collaborationFocusActive = !jevEnabled && (hasDesktopFocusSource
    ? desktopFocusGroup === roomFocusGroup
    : embeddedFocusActive);
  const externalCollaborationFocus = !jevEnabled && hasDesktopFocusSource && collaborationFocusActive;
  const previousFocusRef = useRef(collaborationFocusActive);
  const [view, setView] = useRoomViewRecovery(`room:${recordId}`);
  const workspaceRef = useRef<HTMLElement>(null);
  const jevRailToggleRef = useRef<HTMLButtonElement>(null);
  const [jevRailPreference, setJevRailPreference] = useJevRailPreference();
  const [jevRailOverlayOpen, setJevRailOverlayOpen] = useState(false);
  const jevRailNarrow = useContainerNarrow(workspaceRef, JEV_RAIL_OVERLAY_WIDTH);
  useEffect(() => { if (!jevRailNarrow) setJevRailOverlayOpen(false); }, [jevRailNarrow]);
  // The desktop roster and selected partner own details in external focus.
  // Derive this immediately so a restored inline inspector never claims space.
  const visiblePanel = externalCollaborationFocus || jevEnabled && panel === 'focus' ? 'none' : panel;
  const visibleView = jevEnabled ? ['timeline', 'messages', 'tasks'].includes(view) ? view : 'conversation' : externalCollaborationFocus ? 'rounds' : view;
  const collaborationView = ['timeline', 'messages', 'tasks'].includes(visibleView);
  const [selectedParticipantId, setSelectedParticipantId] = useState('');
  const collaborationTriggerRef = useRef<HTMLButtonElement>(null);
  const [abortingTurnIds, setAbortingTurnIds] = useState<Set<string>>(() => new Set());
  const [collaborationOpenFailures, setCollaborationOpenFailures] = useState<Set<string>>(() => new Set());
  const [resumeErrorByRow, setResumeErrorByRow] = useState<Record<string, string>>({});
  const [resumingWorkItemId, setResumingWorkItemId] = useState('');
  const [recoveryState, setRecoveryState] = useState<'recovering' | 'failed' | 'synced'>('recovering');
  function settleJevComposer(input: NonNullable<ReturnType<typeof pendingJevInput>>) {
    setDraft(value => value.trim() === input.message ? '' : value);
    setAttachments(items => items.filter(item => !input.attachmentIds?.includes(item.mediaId)));
    setError('');
  }
  useEffect(() => { if (jev.recoveredAdmission) settleJevComposer(jev.recoveredAdmission); }, [jev.recoveredAdmission]);

  useEffect(() => {
    if (initialDraft !== undefined) setDraft(initialDraft);
    if (initialError) setError(initialError);
  }, [initialDraft, initialError]);

  useEffect(() => {
    setConnectionError('');
    setPanel('none');
    setEmbeddedFocusActive(false);
    setSelectedParticipantId('');
    setCollaborationOpenFailures(new Set());
  }, [recordId]);
  useEffect(() => {
    runtimeWarmupSessionIdsRef.current.clear();
  }, [recordId]);
  useEffect(() => {
    if (!externalCollaborationFocus) return;
    setPanel('none');
    setSelectedParticipantId('');
  }, [externalCollaborationFocus]);
  useEffect(() => {
    const previousFocus = previousFocusRef.current;
    previousFocusRef.current = collaborationFocusActive;
    if (!previousFocus || collaborationFocusActive) return;
    /* A desktop-wide exit may arrive from the shell, another Room, or a
       surviving window. Only presentation owned by this Room is stale then;
       the desktop focus owner remains the store. */
    setPanel('none');
    setSelectedParticipantId('');
  }, [collaborationFocusActive]);

  const clearOptimisticSteer = useCallback((clientActionId: string) => {
    if (optimisticSteerRef.current?.clientActionId !== clientActionId) return;
    optimisticSteerRef.current = null;
    setOptimisticSteer(null);
  }, []);
  const acknowledgeOptimisticSteer = useCallback((events: readonly unknown[]) => {
    const pending = optimisticSteerRef.current;
    if (!pending) return;
    const acknowledged = events.some((event) => roomEventClientActionId(event) === pending.clientActionId);
    if (acknowledged) clearOptimisticSteer(pending.clientActionId);
  }, [clearOptimisticSteer]);

  useEffect(() => {
    optimisticSteerRef.current = null;
    setOptimisticSteer(null);
  }, [recordId]);

  const projection = useRoomLiveStore((state) => state.projections[recordId]);
  const roundHistoryReady = useRoomLiveStore((state) => Boolean(state.snapshotsByRoomId[recordId]));
  const focusProjection = useMemo(
    () => record ? buildRoomFocusProjection(record, projection) : undefined,
    [projection, record],
  );
  const participantAliases = useMemo(() => Object.fromEntries(
    focusProjection?.partners.map((partner) => [partner.participantId, partner.celestialName]) ?? [],
  ), [focusProjection]);
  const prewarmParticipantSession = useCallback((sessionId: string) => {
    if (!sessionId || runtimeWarmupSessionIdsRef.current.has(sessionId)) return;
    runtimeWarmupSessionIdsRef.current.add(sessionId);
    void transport.request({
      pathId: 'agent.runtime.ensure',
      body: { sessionId },
    }).catch(() => {
      runtimeWarmupSessionIdsRef.current.delete(sessionId);
    });
  }, [transport]);
  const moderatorSessionId = record?.participants.find((participant) => (
    participant.id === record.moderatorParticipantId
    && participant.status === 'active'
  ))?.sessionId ?? '';
  useEffect(() => {
    if (!liveActive) return;
    prewarmParticipantSession(moderatorSessionId);
  }, [liveActive, moderatorSessionId, prewarmParticipantSession]);
  useEffect(() => {
    if (!liveActive || !record || !draft.trim()) return;
    const mentioned = roomMentionedParticipants(
      record.participants.filter((participant) => participant.status === 'active'),
      draft,
      participantAliases,
    );
    for (const participant of mentioned) {
      prewarmParticipantSession(participant.sessionId);
    }
  }, [draft, liveActive, participantAliases, prewarmParticipantSession, record]);
  const pendingQuestion = projection?.pendingUserQuestion;
  const pendingGroupedInput = latestPendingGroupedRoomInput(projection);
  const activeTurn = projection ? selectActivePublicRoomTurn(projection) : undefined;
  const latestTurn = projection
    ? projection.turnsById[selectPublicRoomTurnOrder(projection).at(-1) ?? '']
    : undefined;
  const collaborationParticipantSignature = useMemo(() => record?.participants
    .map((participant) => [
      participant.id,
      participant.status,
      participant.sessionId,
      participant.ordinal,
      participant.collaborationRole ?? '',
    ].join(':'))
    .sort()
    .join('\u0000') ?? '', [record?.participants]);
  const collaborationParticipantRequests = useMemo(
    () => record ? roomCollaborationPlanetRequests(record, projection) : [],
    [collaborationParticipantSignature, record, projection],
  );
  const collaborationParticipantIds = useRef(new Map<string, { turnId: string; ids: Set<string> }>());
  const collaborationSyncKeyRef = useRef('');
  const activeWork = record?.workItems?.find((item) => ['queued', 'active', 'review', 'blocked'].includes(item.state));
  // WorkItem lifecycle metadata can outlive its execution Root. Only the
  // public execution projection decides whether sending is a live steer.
  const taskBusyState = record?.roomKind === 'roleplay' || !activeTurn
    ? undefined
    : activeWork?.state === 'blocked'
      ? 'blocked' as const
      : 'running' as const;
  // A failed latest turn means the Room already admitted and retained work.
  // The composer exposes Continue for that state; it must not resubmit the
  // original request or invoke the Runtime retry path.
  const continuationAvailable = Boolean(
    record?.status === 'active'
    && latestTurn?.status === 'failed'
    && !activeTurn
    && !sending
    && !pendingQuestion
  );
  /* Sending into a running Room steers the active partner. Queueing is the
   * other honest choice: the follow-up stays in the browser, ahead of the
   * Runtime send path, until this turn settles — so it can still be reordered,
   * edited, or pulled back into the composer on stop. */
  const queue = useConversationQueue({
    busy: (jevEnabled ? jev.busy || jev.awaitingPlan || jev.loading || jev.creating || Boolean(jev.pendingInput || jev.pendingPlan || jev.planSending || jev.error) || Boolean(activeTurn && !jev.liveSnapshot) : Boolean(activeTurn)) || sending || Boolean(pendingSend),
    conversationId: recovery.ownerId,
    onDispose: items => recovery.recoverInput(current => ({ ...current, draft: mergeQueueBackToDraft(items, current.draft) })),
    send: (value) => {
      // The queue still owns input refused before admission. Read the journal
      // synchronously, including before React renders a newly admitted send.
      if (sending || sendJournal.getSnapshot()) return false;
      const admission = send(value);
      if (admission === false) return false;
      void admission;
    },
  });
  const queueFollowUp = useCallback((value: string) => queue.enqueue(value), [queue]);

  const retrySnapshot = useRoomLiveSession({
    active: liveActive,
    roomId: recordId,
    transport,
    onLoadingChange: setLoading,
    onSnapshot: (_roomId, snapshot) => {
      acknowledgeOptimisticSteer(snapshot.events);
      onJevEvents?.(snapshot.events);
      const room = asRoom(snapshot.room);
      if (room) onRoomUpdated(room);
    },
    onMetadata: (_roomId, value) => {
      const room = roomFromResponse(value);
      if (room) onRoomUpdated(room);
    },
    onConnectionRestored: () => { setConnectionError(''); if (jevEnabled) jev.refresh(); },
    onRecoveryState: (_roomId, state) => setRecoveryState(state),
    onConnectionError: (_roomId, reason, fallback) => setConnectionError(roomErrorText(reason, fallback)),
    onEvents: (_roomId, events) => {
      acknowledgeOptimisticSteer(events);
      onJevEvents?.(events);
      if (jevEnabled) jev.onEvents(events);
    },
  });

  function send(
    rawValue: string,
    options: { question?: PendingRoomQuestion; retryOfRootId?: string; preserveDraft?: boolean } = {},
  ): boolean | Promise<boolean> {
    if (!record || record.status !== 'active' || sending) return false;
    const pending = sendJournal.getSnapshot();
    if (pending) {
      // Identical text can answer a different question or target a new Root.
      // Only the explicit recovery action may replay the old binding.
      setError('上次发送尚未确认，请先核实上次发送；新草稿会保留。');
      return false;
    }
    if (recovery.checking || recovery.issues.length) { setError('请先核实或移除恢复失败的附件。'); return false; }
    const authoritativeQuestion = roomProjection(recordId).pendingUserQuestion;
    const answersQuestion = Boolean(
      options.question
      && authoritativeQuestion
      && options.question.postId === authoritativeQuestion.postId
      && options.question.rootId === authoritativeQuestion.rootId
    );
    // Continue/retry belongs to the retained turn, not to the next composer
    // draft. Explicit uncertain-command recovery uses its journal copy below.
    const composerAttachments = options.preserveDraft ? [] : attachments;
    const message = rawValue.trim() || (composerAttachments.length ? '请查看附件。' : '');
    if (!message) return false;
    if (jevEnabled && !answersQuestion) {
      if (jev.loading && !jev.liveSnapshot || jev.error) {
        setError('正在核实当前 Jev 任务，请同步状态后再发送。');
        jev.refresh();
        return false;
      }
      if ((jev.busy || activeTurn && !jev.liveSnapshot) && !pendingJevInput(transport, recordId)) {
        if (composerAttachments.length) { setError('附件已保留，当前任务结束后可以发送。'); return false; }
        const queued = queue.enqueue(rawValue);
        if (queued && !options.preserveDraft) setDraft('');
        return queued;
      }
      const selectedAttachments = composerAttachments;
      setSending(true); setError('');
      return (async () => {
        try {
          const accepted = await jev.send(message, selectedAttachments.map(item => item.mediaId));
          if (!accepted) return false;
          if (!options.preserveDraft) setDraft(current => current === rawValue ? '' : current);
          setAttachments(current => current.filter(item => !selectedAttachments.some(sent => sent.mediaId === item.mediaId)));
          retrySnapshot();
          followRoomTimelineIfReaderAtEnd(timelineRef.current);
          return true;
        } catch (reason) {
          setError(roomErrorText(reason, 'Jev 发送尚未确认。重试将核实同一次请求。'));
          return false;
        } finally { setSending(false); }
      })();
    }
    const steering = Boolean(activeTurn && !answersQuestion);
    if (steering && composerAttachments.length) {
      setError('当前回合执行中只能发送文字干预；图片会保留到下一轮。');
      return false;
    }
    const clientMessageId = `paw-room-${crypto.randomUUID()}`;
    /* The composer writes the visible planet name (@Mars); resolving it back
     * through the same alias map is what submits the stable Runtime ID. */
    const addressed = answersQuestion
      ? []
      : roomMentionedParticipants(
          record.participants.filter((item) => item.status === 'active'),
          message,
          participantAliases,
        );
    if (steering && addressed.length > 1) {
      if (!options.preserveDraft) setDraft(rawValue);
      setError('当前回合只能点名一位伙伴，请只保留一个 @伙伴。');
      return false;
    }
    const steerParticipantId = steering
      ? addressed[0]?.id
        ?? activeTurn?.participantIds[0]
        ?? record.participants.find((item) => item.status === 'active')?.id
        ?? ''
      : '';
    if (steering && !steerParticipantId) {
      if (!options.preserveDraft) setDraft(rawValue);
      setError('当前回合还没有可点名的伙伴，请稍后重试。');
      return false;
    }
    const selectedAttachments = answersQuestion ? [] : composerAttachments;
    const request: ControlRequest = steering && activeTurn
        ? {
            pathId: 'agent.room.participant.steer',
            params: { roomId: recordId },
            body: {
              action: 'steer_participant',
              rootId: activeTurn.rootId ?? activeTurn.id,
              participantId: steerParticipantId,
              clientActionId: clientMessageId,
              message,
            },
          }
        : {
            pathId: 'agent.room.message',
            params: { roomId: recordId },
            body: {
              message,
              clientMessageId,
              attachmentIds: selectedAttachments.map((item) => item.mediaId),
              ...(options.retryOfRootId ? { retryOfRootId: options.retryOfRootId } : {}),
              ...(answersQuestion && authoritativeQuestion
                ? { answerToPostId: authoritativeQuestion.postId, answerToRootId: authoritativeQuestion.rootId }
                : {}),
              ...(addressed.length ? { participantIds: addressed.map((item) => item.id) } : {}),
              ...(record.roomKind !== 'roleplay' && activeWork?.id
                ? { workItemId: activeWork.id }
                : {}),
            },
          };
    return deliverRoomSend({ request, clientMessageId, rawValue, attachments: selectedAttachments, status: 'sending', preserveDraft: options.preserveDraft });
  }

  async function deliverRoomSend(attempt: RoomSendAttempt): Promise<boolean> {
    if (!record || record.status !== 'active') return false;
    const delivery = startRoomSend(transport, recordId, attempt);
    if (!delivery) return false;
    const preserveDraft = delivery.attempt.preserveDraft ?? false;
    const { clientMessageId, rawValue, attachments: selectedAttachments } = delivery.attempt;
    const body = asRecord(delivery.attempt.request.body);
    const message = String(body.message);
    const steering = delivery.attempt.request.pathId === 'agent.room.participant.steer';
    const answersQuestion = Boolean(body.answerToPostId);
    const steerParticipantId = String(body.participantId ?? '');
    setSending(true);
    if (!preserveDraft) setDraft(current => current.trim() === rawValue.trim() ? '' : current);
    if (!answersQuestion) setAttachments(current => current.filter(item => !selectedAttachments.some(sent => sent.mediaId === item.mediaId)));
    setError('');
    if (steering) {
      const receipt = {
        clientActionId: clientMessageId,
        message,
        participantId: steerParticipantId,
      } satisfies OptimisticSteerReceipt;
      optimisticSteerRef.current = receipt;
      setOptimisticSteer(receipt);
    }
    try {
      const result = await delivery.settled;
      if (result.status !== 'accepted') {
        if (steering) clearOptimisticSteer(clientMessageId);
        if (!preserveDraft) setDraft((current) => current || rawValue);
        if (!answersQuestion) setAttachments((current) => {
          const restored = new Map(current.map((attachment) => [attachment.mediaId, attachment]));
          for (const attachment of selectedAttachments) if (!restored.has(attachment.mediaId)) restored.set(attachment.mediaId, attachment);
          return [...restored.values()].slice(0, 8);
        });
        setError(result.status === 'uncertain'
          ? 'Room 发送尚未确认。核实上次发送会使用原请求，避免重复执行。'
          : roomErrorText(result.error, 'Room 消息未被接收，请重试。'));
        return false;
      }
      try {
        const timelineEvents = result.response.timelineEvents;
        if (Array.isArray(timelineEvents)) acknowledgeOptimisticSteer(timelineEvents);
        const workItem = asWorkItem(result.response.workItem);
        if (workItem) onRoomUpdated({
          ...record,
          workItems: [...(record.workItems ?? []).filter((item) => item.id !== workItem.id), workItem],
        });
        followRoomTimelineIfReaderAtEnd(timelineRef.current);
      } catch {
        // Presentation failure cannot undo an accepted command or invite replay.
        setError('消息已接收，但界面暂未更新，请重新同步。');
      }
      return true;
    } finally {
      setSending(false);
    }
  }

  async function retryJevAdmission() {
    const input = jev.pendingInput;
    if (!input || sending || jev.creating || record?.status !== 'active') return;
    setSending(true); setError('');
    try {
      if (await jev.retryPending()) { settleJevComposer(input); retrySnapshot(); }
    } catch (reason) { setError(roomErrorText(reason, 'Jev 发送尚未确认，可重试同一次发送。')); }
    finally { setSending(false); }
  }

  async function retryJevPlan() {
    const input = jev.pendingPlan;
    if (!input || sending || jev.planSending || record?.status !== 'active') return;
    setSending(true); setError('');
    try {
      if (await jev.retryPendingPlan()) { if (input.message) settleJevComposer({ message: input.message, attachmentIds: input.attachmentIds }); retrySnapshot(); }
    } catch (reason) { setError(roomErrorText(reason, '方案操作尚未确认，可核实同一次操作。')); }
    finally { setSending(false); }
  }

  async function abortTurn(rootId: string): Promise<void> {
    if (!rootId || abortingTurnIds.has(rootId)) return;
    /* Stopping must not silently discard held follow-ups: the queue only ever
     * held them, so they go back to the composer the user can still edit. */
    if (queue.queue.length) setDraft(queue.restoreToDraft(draft));
    setAbortingTurnIds((current) => new Set(current).add(rootId));
    try {
      const receipt = await transport.request<Record<string, unknown>>({
        pathId: 'agent.room.abort',
        params: { roomId: recordId },
        body: { roomTurnId: rootId, clientRequestId: `paw-room-abort-${crypto.randomUUID()}` },
      });
      const outcome = roomCancellationOutcome(receipt);
      if (outcome === 'terminated') {
        useRoomLiveStore.getState().abortTurn(recordId, rootId, Date.now());
      } else if (outcome === 'already_terminal') {
        retrySnapshot();
      } else {
        setError('停止信号已送达，仍在等待所有伙伴返回终止回执。');
      }
    } catch (reason) { setError(publicErrorText(reason, '暂时无法停止这轮协作。')); }
    finally {
      setAbortingTurnIds((current) => { const next = new Set(current); next.delete(rootId); return next; });
    }
  }

  async function decideApproval(approvalId: string, decision: 'approved' | 'rejected', payloadSha256: string): Promise<void> {
    try {
      await transport.request({
        pathId: 'agent.approval.decide',
        params: { approvalId },
        body: { decision: decision === 'approved' ? 'approve' : 'reject', payloadSha256 },
      });
      setError('');
    } catch (reason) {
      setError(publicErrorText(reason, '审批没有完成，请重试。'));
      throw reason;
    }
  }

  async function pickAttachments(): Promise<void> {
    if (!transport.pickFiles) { setError('当前环境不能选择附件。'); return; }
    try {
      const imported = await transport.pickFiles({
        purpose: 'attachment',
        roomId: recordId,
        multiple: true,
        maxFiles: Math.max(1, 8 - attachments.length),
      });
      mergePickedAttachments(imported);
    } catch (reason) { setError(publicErrorText(reason, '附件没有导入，请重试。')); }
  }

  async function pasteFiles(files?: File[]): Promise<boolean> {
    if (!transport.pasteImages) { setError('当前环境不能导入剪贴板文件。'); return false; }
    try {
      const imported = await transport.pasteImages({
        roomId: recordId,
        ...(files?.length ? { files } : {}),
        maxFiles: files?.length || Math.max(1, 8 - attachments.length),
      });
      mergePickedAttachments(imported);
      return imported.length > 0;
    } catch (reason) { setError(publicErrorText(reason, '附件没有导入，请重试。')); return false; }
  }

  function mergePickedAttachments(files: PickedFile[]): void {
    const receipts = files.map((file) => roomAttachment(file, recordId));
    setAttachments((current) => {
      const byId = new Map(current.map((item) => [item.mediaId, item]));
      receipts.forEach((item) => byId.set(item.mediaId, item));
      return [...byId.values()].slice(0, 8);
    });
  }

  async function manageWorkspaceRoots(): Promise<void> {
    if (!record || !transport.pickFiles) {
      setError('当前环境不能选择工作区。');
      return;
    }
    try {
      const picked = await transport.pickFiles({
        purpose: 'workspace-root',
        selection: 'directory',
        multiple: true,
        maxFiles: 4,
      });
      const workspaceRoots = picked
        .map((item) => item.path)
        .filter((path): path is string => Boolean(path));
      if (!workspaceRoots.length) return;
      const permissionPolicy = parseRoomPermissionPolicy(record.permissionPolicy, record.roomKind);
      const workspaceConfirmationRequired = permissionPolicy
        ? roomPermissionPolicyNeedsWorkspaceConfirmation(permissionPolicy)
        : record.executionMode === 'workspace_managed';
      const dangerousConfirmationRequired = permissionPolicy
        ? roomPermissionPolicyNeedsDangerousConfirmation(permissionPolicy)
        : record.executionMode === 'full_trust';
      const response = await transport.request<Record<string, unknown>>({
        pathId: 'agent.room.archive',
        params: { roomId: recordId },
        body: {
          workspaceRoots,
          ...(permissionPolicy ? { permissionPolicy } : {}),
          ...(workspaceConfirmationRequired
            ? { workspaceScopeConfirmation: 'APPROVE_WORKSPACE_SCOPE' }
            : {}),
          ...(dangerousConfirmationRequired
            ? { dangerousModeConfirmation: 'ENABLE_FULL_TRUST' }
            : {}),
        },
      });
      const updated = roomFromResponse(response);
      if (updated) onRoomUpdated(updated);
      setError('');
      retrySnapshot();
    } catch (reason) {
      setError(roomErrorText(reason, 'Room 工作目录没有更新。'));
    }
  }

  const title = record?.title || '未命名 Room';
  const activeParticipants = record?.participants.filter((participant) => participant.status === 'active') ?? [];
  const activeTopic = record?.topics?.find((topic) => topic.id === record.activeTopicId)
    ?? record?.topics?.find((topic) => topic.status === 'active');
  const activeRootId = activeTurn?.rootId ?? activeTurn?.id ?? '';
  /* The Room moderator is the backend's Root actor. Require that canonical
   * participant to be active before exposing a recovery command; never fall
   * back to the first participant or a lane that merely happens to be visible. */
  const activeRootActorId = activeTurn && record?.moderatorParticipantId
    && record.participants.some((participant) => (
      participant.id === record.moderatorParticipantId && participant.status === 'active'
    ))
    ? record.moderatorParticipantId
    : '';
  const abortingActiveTurn = Boolean(activeRootId && abortingTurnIds.has(activeRootId));
  const openParticipantObserver = useCallback((participant: RoomSummary['participants'][number], background = false) => desktop?.openWindow(
    roomPlanetObserverWindowRequest(participant, recordId, background),
  ), [desktop, recordId]);
  const openParticipantById = useCallback((participantId: string, background = false) => {
    const participant = record?.participants.find((candidate) => candidate.id === participantId);
    if (!participant) return;
    openParticipantObserver(participant, background);
  }, [openParticipantObserver, record?.participants]);
  const openJevFile = useCallback((sessionId: string, path: string) => {
    const route = `/files?session=${encodeURIComponent(sessionId)}&path=${encodeURIComponent(path)}`;
    if (desktop?.openRoute) desktop.openRoute(route);
    else desktop?.openApp?.('files', route);
  }, [desktop]);
  const selectAndOpenParticipant = useCallback((participantId: string) => {
    setSelectedParticipantId(participantId);
    if (participantProcessLocation === 'room-transcript') {
      setView('conversation');
      setPanel('none');
      setEmbeddedFocusActive(false);
      desktop?.setCollaborationFocusGroup?.(null);
      return;
    }
    if (collaborationFocusActive) {
      openParticipantById(participantId);
      return;
    }
    const participant = record?.participants.find((candidate) => candidate.id === participantId);
    if (!participant) return;
    desktop?.openWindow(roomPartnerSessionWindowRequest(participant));
  }, [collaborationFocusActive, desktop, openParticipantById, participantProcessLocation, record?.participants]);
  const openProcessActivity = useCallback((activity: RoomActivityProjection) => {
    const request = roomProcessWindowRequest(activity, recordId);
    if (request) desktop?.openWindow({ ...request, background: false });
  }, [desktop, recordId]);
  const resumeBlockedWorkItem = useCallback(async (row: RoomRoundTaskRow): Promise<void> => {
    if (!record || !row.blockedWorkItemId) return;
    if (!activeRootActorId) {
      setResumeErrorByRow((current) => ({ ...current, [row.key]: '当前没有可用的 active Root，无法恢复。' }));
      return;
    }
    const workItemId = row.blockedWorkItemId;
    setResumingWorkItemId(workItemId);
    setResumeErrorByRow((current) => {
      if (!(row.key in current)) return current;
      const next = { ...current };
      delete next[row.key];
      return next;
    });
    try {
      const response = await transport.request<Record<string, unknown>>({
        pathId: 'agent.room.workItem.resume',
        params: { roomId: record.id, workItemId },
        body: {
          actorParticipantId: activeRootActorId,
          clientActionId: `paw-room-work-resume-${crypto.randomUUID()}`,
          phase: 'recovery',
          timeoutSeconds: 300,
        },
      });
      const workItem = asWorkItem(asRecord(response).workItem);
      if (!workItem) throw new Error('恢复回执缺少 WorkItem。');
      /* The response is an authoritative dispatch receipt. Reflect only its
       * WorkItem; the subsequent live snapshot supplies any later settlement. */
      onRoomUpdated({
        ...record,
        workItems: [...(record.workItems ?? []).filter((item) => item.id !== workItem.id), workItem],
      });
      retrySnapshot();
    } catch (reason) {
      setResumeErrorByRow((current) => ({
        ...current,
        [row.key]: publicErrorText(reason, '恢复没有完成，请重试。'),
      }));
    } finally {
      setResumingWorkItemId('');
    }
  }, [activeRootActorId, onRoomUpdated, record, retrySnapshot, transport]);
  /* PF-CM-013：协作态势可以弹出成一扇独立观察窗，主 Room 留给公开对话。 */
  const openFocusWindow = useCallback(() => {
    if (!record) return;
    desktop?.openWindow({
      appId: 'agent',
      target: {
        kind: 'room',
        id: recordId,
        title: `${record.title} · 协作态势`,
        subtitle: 'Sol 协作全景 · 目标、伙伴与交接实时同步',
        panel: 'focus',
      },
    });
  }, [desktop, record, recordId]);
  const enterCollaborationMode = useCallback(() => {
    setView('rounds');
    setPanel(hasDesktopFocusSource ? 'none' : 'focus');
    if (!hasDesktopFocusSource) setEmbeddedFocusActive(true);
    if (!desktop || !record) return;
    /* Collaboration focus is an explicit Room view choice. Opening a
       participant Session is still an ordinary desktop action and must not
       enter this mode as a side effect. */
    desktop.setCollaborationFocusGroup?.(roomFocusGroup);
    setCollaborationOpenFailures(new Set());
  }, [desktop, hasDesktopFocusSource, record, roomFocusGroup]);
  const exitCollaborationFocus = useCallback(() => {
    setEmbeddedFocusActive(false);
    setPanel('none');
    setSelectedParticipantId('');
    desktop?.setCollaborationFocusGroup?.(null);
  }, [desktop]);
  const closeCollaborationPanel = useCallback(() => {
    setPanel('none');
    collaborationTriggerRef.current?.focus();
  }, []);
  const retryCollaborationPlanet = useCallback((participantId: string) => {
    const participant = record?.participants.find((candidate) => candidate.id === participantId);
    if (!desktop || !participant) return;
    try {
      desktop.openWindow(roomPlanetObserverWindowRequest(participant, recordId, true));
      setCollaborationOpenFailures((current) => {
        if (!current.has(participantId)) return current;
        const next = new Set(current);
        next.delete(participantId);
        return next;
      });
    } catch {
      setCollaborationOpenFailures((current) => current.has(participantId)
        ? current
        : new Set(current).add(participantId));
    }
  }, [desktop, record?.participants, recordId]);
  /* Restore this round's admitted observers even after they have submitted.
   * Progress and terminal updates do not reopen a window the user collapsed. */
  useEffect(() => {
    if (!observerAutoOpen || !collaborationFocusActive || !desktop || !record) {
      if (!observerAutoOpen || !collaborationFocusActive) {
        collaborationSyncKeyRef.current = '';
        collaborationParticipantIds.current.clear();
      }
      return;
    }
    const turnId = latestTurn?.id ?? '';
    const syncKey = `${record.id}:${turnId}:${collaborationParticipantRequests.map((request) => request.target.id).join('\u0000')}`;
    if (syncKey === collaborationSyncKeyRef.current) return;
    collaborationSyncKeyRef.current = syncKey;
    const desired = new Set(collaborationParticipantRequests.map((request) => request.target.id));
    const previousRound = collaborationParticipantIds.current.get(record.id);
    const previous = previousRound?.turnId === turnId ? previousRound.ids : new Set<string>();
    const failures = new Set<string>();
    for (const request of collaborationParticipantRequests) {
      if (previous.has(request.target.id)) continue;
      try {
        desktop.openWindow(request);
      } catch {
        failures.add(request.target.id);
      }
    }
    collaborationParticipantIds.current.set(record.id, { turnId, ids: desired });
    setCollaborationOpenFailures((current) => new Set([
      ...[...current].filter((id) => desired.has(id) && previous.has(id)), ...failures,
    ]));
  }, [
    observerAutoOpen,
    collaborationParticipantRequests,
    collaborationParticipantSignature,
    collaborationFocusActive,
    desktop,
    latestTurn?.id,
    record,
  ]);
  useEffect(() => {
    setCollaborationOpenFailures((current) => {
      if (!current.size) return current;
      const next = new Set([...current].filter((participantId) => record?.participants.some((participant) => (
        participant.id === participantId && participant.status === 'active'
      ))));
      return next.size === current.size ? current : next;
    });
  }, [record?.participants]);
  useEffect(() => {
    if (!selectedParticipantId) return;
    if (!record?.participants.some((participant) => (
      participant.id === selectedParticipantId && participant.status === 'active'
    ))) setSelectedParticipantId('');
  }, [record?.participants, selectedParticipantId]);
  useEffect(() => {
    if (!desktop || !record) return;
    desktop.bindRoomMain?.({ kind: 'room', id: record.id, title: record.title, subtitle: undefined });
  }, [desktop, record]);
  /* status overlay 克制：为 0 的计数是噪音，状态行只亮出真实存在的工作。 */
  const submittedPartnerCount = focusProjection?.partners.filter((partner) => partner.state === 'completed').length ?? 0;
  const signalChips = ([
    ['active', focusProjection?.counts.active ?? 0, '进行'],
    ['review', focusProjection?.counts.review ?? 0, '复核'],
    ['blocked', focusProjection?.counts.blocked ?? 0, '受阻'],
    ['submitted', submittedPartnerCount, '伙伴执行结束'],
    ['complete', focusProjection?.workItems.filter((item) => item.source === 'work-item' && item.state === 'completed').length ?? 0, '工作项完成'],
  ] as const).filter(([, count]) => count > 0);
  /* 没有主持就没有 Sol：signal chrome 只有在真的有伙伴担任 coordinator 时
     才用 Sol 命名这个 Room 的原点，否则统一叫「主 Room」。 */
  const coordinatorActive = focusProjection ? roomFocusHasCoordinator(focusProjection.partners) : false;
  const originLabel = roomFocusOriginLabel(coordinatorActive);
  const visibleError = error || connectionError || (pendingSend?.status === 'uncertain' ? '上次发送尚未确认，原请求仍保留。' : '') || (jevEnabled && jev.pendingInput ? '上次发送尚未确认，原内容与附件仍保留。' : jevEnabled && jev.pendingPlan ? '上次方案操作尚未确认。' : '');
  const syncOffline = Boolean(connectionError) && connectionError !== ROOM_WORKSPACE_MISSING_TEXT;
  const visibleRecoveryState = syncOffline ? 'failed' : recoveryState;
  const hasRoomHistory = Boolean(projection?.turnOrder.length);
  const roomContentReady = Boolean(record && projection && (
    hasRoomHistory || (!loading && recoveryState === 'synced' && !connectionError)
  ));
  useRoomReadingRecovery(timelineRef, `room:${recordId}`, roomContentReady && visibleView !== 'rounds', visibleView);
  const roomRecoverySurface = connectionError || recoveryState === 'failed' ? (
    <section aria-label="Room 记录暂时不可用" className="paw-room-workspace__recovery" role="region">
      <CircleAlert aria-hidden="true" size={24} />
      <strong>Room 记录暂时不可用</strong>
      <p>{connectionError === ROOM_WORKSPACE_MISSING_TEXT
        ? '重新选择工作目录后，可以继续读取协作记录。'
        : '重新同步以读取协作记录。输入的草稿会保留，不会重新发送消息。'}</p>
      {connectionError === ROOM_WORKSPACE_MISSING_TEXT ? (
        <button onClick={() => void manageWorkspaceRoots()} type="button">选择工作目录</button>
      ) : (
        <button disabled={loading} onClick={() => retrySnapshot()} type="button">{loading ? '正在重新同步…' : '重新同步'}</button>
      )}
    </section>
  ) : (
    <div aria-label="正在恢复 Room 协作现场" className="paw-room-workspace__loading" role="status"><LoaderCircle aria-hidden="true" className="ui-spin" size={18} />正在恢复 Room 协作现场</div>
  );
  const workStatus = focusProjection ? buildRoomWorkStatus({
    focus: focusProjection, projection, recoveryState: visibleRecoveryState,
    visible: pageVisible, stopping: abortingActiveTurn,
    pendingInput: Boolean(pendingGroupedInput && pendingGroupedInput.turnId === focusProjection.goal.rootId),
  }) : undefined;
  // Stop remains owned by the actual active Root, independently of whether a
  // status animation is appropriate (for example, while waiting for input).
  const runtimeBusy = visibleRecoveryState === 'synced' && (jevEnabled ? jev.busy : Boolean(activeTurn));
  const runtimeStatusLabel = jevEnabled ? jevStatusLabel(jev.liveSnapshot, jev.loading) : workStatus?.headline ?? '连接中';
  const chromeStatus = (jevEnabled ? jev.stopping : abortingActiveTurn) ? 'stopping' : (jevEnabled ? jev.busy && !jevAbstention(jev.liveSnapshot) : workStatus?.animate) && active ? 'busy' : visibleRecoveryState;
  const stopCurrentWork = () => {
    if (queue.queue.length) setDraft(queue.restoreToDraft(draft));
    if (jevEnabled && jev.liveSnapshot) void jev.stop(); else void abortTurn(activeRootId);
  };
  const roomChromeControls = <div aria-label="Room 窗口控制" onFocusCapture={event => {
    // A partly clipped item can receive focus without the browser revealing
    // its full hit target. Keep keyboard navigation inside the view strip.
    event.target.scrollIntoView?.({ block: 'nearest', inline: 'nearest', behavior: 'auto' });
  }} className="paw-room-window-chrome" data-controls-expanded={controlsExpanded} data-agent-mode={jevEnabled ? 'jev' : undefined} data-coordinator={coordinatorActive || undefined} data-external-focus={externalCollaborationFocus || undefined} data-status={chromeStatus}>
    <button className="paw-chat-controls-toggle" type="button" aria-expanded={controlsExpanded} aria-label={controlsExpanded ? '收起 Room 控件' : '展开 Room 控件'} onClick={() => setControlsExpanded(value => !value)}><ChevronDown size={15} /><span>视图</span></button>
    <button type="button" aria-pressed={collaborationView} aria-label={collaborationView ? '返回 Room 对话' : '查看 Room 协作全景'} onClick={() => { setView(collaborationView ? 'conversation' : 'timeline'); setPanel('none'); exitCollaborationFocus(); }}><ChartGantt size={15} /><span>协作</span></button>
    <button type="button" aria-label="查看 Room 任务" onClick={() => { if (jevEnabled) setJevRail(true); else { exitCollaborationFocus(); setPanel('none'); setView('tasks'); } }}><ListChecks size={15} /></button>
    {jevEnabled ? <span aria-label="Agent 中的 Jev 任务模式" className="paw-room-workspace__mode">Jev</span> : coordinatorActive && !externalCollaborationFocus ? <span aria-label="Agent 中的 Sol 协作模式" className="paw-room-workspace__mode">Sol</span> : null}
    {jevEnabled ? <nav aria-label="Jev 工作台视图"><button type="button" aria-pressed={visiblePanel === 'none'} onClick={() => setPanel('none')}><MessageCircle size={14} /><span>对话与进展</span></button><button type="button" aria-pressed={visiblePanel === 'governance'} onClick={() => setPanel('governance')}><Users size={14} /><span>伙伴与设置</span></button></nav> : !externalCollaborationFocus ? <nav aria-label="Room 工作台视图">
      <button aria-label="执行记录" aria-pressed={!collaborationFocusActive && panel === 'none' && view === 'rounds'} data-room-view="rounds" onClick={() => { setView('rounds'); exitCollaborationFocus(); }} type="button"><ListChecks size={14} /><span>执行记录</span></button>
      <button aria-label="协作时间线" aria-pressed={view === 'timeline'} data-room-view="timeline" onClick={() => { setView('timeline'); exitCollaborationFocus(); }} type="button"><ChartGantt size={14} /><span>时间线</span></button>
      <button aria-label="消息流" aria-pressed={view === 'messages'} data-room-view="messages" onClick={() => { setView('messages'); exitCollaborationFocus(); }} type="button"><GitBranch size={14} /><span>消息流</span></button>
      <button aria-label="协同模式" aria-pressed={collaborationFocusActive} data-room-view="collaboration" onClick={enterCollaborationMode} ref={collaborationTriggerRef} type="button"><Focus size={14} /><span>协同模式</span></button>
      <button aria-label="对话" aria-pressed={!collaborationFocusActive && panel === 'none' && view === 'conversation'} data-room-view="conversation" onClick={() => { setView('conversation'); exitCollaborationFocus(); }} type="button"><MessageCircle size={14} /><span>对话</span></button>
      <button aria-label="星空" aria-pressed={view === 'starfield'} data-room-view="starfield" onClick={() => { setView('starfield'); exitCollaborationFocus(); }} type="button"><Orbit size={14} /><span>星空</span></button>
    </nav> : null}
    <div className="paw-room-workspace__runtime"><span data-terminal={!jevEnabled && workStatus?.state === 'stopped' ? 'aborted' : !jevEnabled && workStatus?.state === 'failed' ? 'failed' : undefined} data-compact-status={jevEnabled || externalCollaborationFocus ? undefined : workStatus?.state === 'stopped' ? '已停止' : workStatus?.state === 'failed' ? '失败' : undefined}><i />{runtimeStatusLabel}</span>{runtimeBusy ? <button aria-label={jevEnabled ? '停止 Jev 执行' : '停止整轮协作'} disabled={jevEnabled ? jev.stopping : abortingActiveTurn} onClick={stopCurrentWork} type="button"><StopCircle size={16} /></button> : null}</div>
  </div>;
  const partnerControls = record ? <RoomCapabilityControls
    participants={record.participants}
    aliases={participantAliases}
    room={record}
    onRoomUpdated={onRoomUpdated}
    showSessionSettings={jevEnabled}
    automaticModels={jevEnabled && jev.modelRouting === 'balanced'}
    modelSyncKey={jevEnabled ? jev.liveSnapshot?.version : undefined}
    busy={(jevEnabled ? jev.busy : Boolean(activeTurn)) || sending}
    showModelControls
    disabled={record.status !== 'active'}
    onSelectTool={(name) => {
      setDraft((current) => `${current}${current.trim() ? '\n' : ''}${name}：`);
      setPartnerSettingsOpen(false);
    }}
  /> : null;
  const jevGraph = jev.snapshot ?? jev.liveSnapshot;
  const jevHistorical = Boolean(jev.snapshot && jev.liveSnapshot && jev.snapshot.graphId !== jev.liveSnapshot.graphId);
  const jevObjective = jevGraph?.currentRootObjective || jevGraph?.tasks.find(task => !task.parentId)?.objective || record?.description || record?.title || 'Jev 当前目标';
  const jevRailAvailable = jevRailHasContent(jev.snapshot);
  const jevRailVisible = jevRailAvailable && (jevRailNarrow ? jevRailOverlayOpen : jevRailPreference !== 'closed');
  const setJevRail = (open: boolean) => {
    if (jevRailNarrow) setJevRailOverlayOpen(open);
    else setJevRailPreference(open ? 'open' : 'closed');
    if (!open) queueMicrotask(() => jevRailToggleRef.current?.focus({ preventScroll: true }));
  };
  const openJevRail = () => {
    setJevRail(true);
    requestAnimationFrame(() => workspaceRef.current?.querySelector<HTMLElement>('.paw-jev-rail button')?.focus({ preventScroll: true }));
  };
  /* The plan and clarification forms are anchored to the plan card inside the
     current round. Keep reading position otherwise; this is an explicit jump
     requested by the user. */
  const focusJevPlan = () => {
    const plan = timelineRef.current?.querySelector<HTMLElement>('.jev-plan-review');
    if (!plan) return;
    plan.scrollIntoView({ block: 'start', behavior: window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
    plan.querySelector<HTMLElement>('.jev-plan-review__question button, .jev-plan-review__primary, textarea')?.focus({ preventScroll: true });
  };
  const jevRootAttachments = jevGraph?.rootAttachments?.length ?? 0;
  const jevLead = jevEnabled ? <div className="jev-mission__goal">
    <Dialog>
      <DialogTrigger asChild><button className="paw-jev-objective-trigger" type="button" aria-label="查看完整任务要求" title={jevObjective}><strong>{jevObjective.trim().split('\n')[0].slice(0, 140)}</strong><Maximize2 size={13} aria-hidden="true" /></button></DialogTrigger>
      <DialogContent className="paw-jev-objective-detail"><DialogTitle>任务要求</DialogTitle><DialogDescription>本轮任务的原始目标，分工和验收围绕这些要求推进。</DialogDescription><div>{jevObjective}</div></DialogContent>
    </Dialog>
    <span className="jev-mission__roster">
      {activeParticipants.length ? <span className="jev-mission__planets" aria-hidden>{activeParticipants.slice(0, 5).map(participant => <RoomPlanetAvatar key={participant.id} ordinal={participant.ordinal} size={18} decorative />)}</span> : null}
      <span>{!record ? '正在恢复 Room 协作现场' : `${activeParticipants.length} 位伙伴`}{jevGraph ? ` · ${jevTaskCountLabel(jevGraph)}` : ''}{jevRootAttachments ? ` · ${jevRootAttachments} 项附件` : ''}{record?.ownerAppId === 'extension:agent-lab' ? ' · Agent Lab 只读沙盒' : ''}</span>
    </span>
  </div> : null;
  const jevRailToggle = jevEnabled && jevRailAvailable ? <button ref={jevRailToggleRef} type="button" className="jev-mission__rail-toggle" aria-pressed={jevRailVisible} aria-label={jevRailVisible ? '收起任务栏' : '展开任务栏'} title={jevRailVisible ? '收起任务栏' : '展开任务栏'} onClick={() => setJevRail(!jevRailVisible)}>
    {jevRailVisible ? <PanelRightClose size={16} aria-hidden /> : <PanelRightOpen size={16} aria-hidden />}<span>任务</span>
  </button> : null;

  return (
    <section
      ref={workspaceRef}
      className={`paw-room-workspace paw-room-workspace--conversation${jevEnabled ? ' paw-room-workspace--jev' : ''}`}
      data-agent-mode={jevEnabled ? 'jev' : 'room'}
      data-collaboration-mode={!jevEnabled && collaborationFocusActive}
      data-external-focus={externalCollaborationFocus || undefined}
      data-panel={visiblePanel}
      data-view={visibleView}
      data-window-chrome={windowChromeTarget ? 'portal' : 'fallback'}
      data-controls-expanded={controlsExpanded}
      data-room-id={recordId}
      data-status={chromeStatus}
    >
      {windowChromeTarget ? <PawWindowChromePortal>{roomChromeControls}</PawWindowChromePortal> : <header className="paw-room-workspace__header">{roomChromeControls}</header>}

      <WorkspaceRecoveryNotice recovery={recovery} />
      {controlsExpanded && !externalCollaborationFocus && !jevEnabled ? <section aria-label="Room 当前协作" className="paw-room-workspace__signal">
        <div className="paw-room-workspace__objective">
          <div><strong>{(focusProjection?.goal.title !== '主话题' && focusProjection?.goal.title) || (activeTopic?.title !== '主话题' && activeTopic?.title) || record?.description || record?.title || activeWork?.objective || '当前协作'}</strong></div>
          <span>{`${activeParticipants.length} 颗行星 · ${focusProjection?.workItems.length ?? 0} 项任务`}{record?.ownerAppId === 'extension:agent-lab' ? ' · Agent Lab 只读沙盒' : ''}</span>
        </div>
        {focusProjection ? <div aria-label={`${originLabel} 当前状态`} className="paw-room-workspace__signal-status">
          {signalChips.length
            ? signalChips.map(([tone, count, label]) => <span data-tone={tone} key={tone}><i />{count} {label}</span>)
            : <span data-tone="idle"><i />待命</span>}
        </div> : null}
      </section> : null}

      <div className="paw-room-workspace__body" data-jev-layout={jevEnabled && visiblePanel === 'none' ? 'team' : undefined} data-jev-rail={jevEnabled && visiblePanel === 'none' ? jevRailVisible ? jevRailNarrow ? 'overlay' : 'open' : 'closed' : undefined}>
        {controlsExpanded && jevEnabled && visiblePanel === 'none' ? <JevCompanion presentation="stage" execution={jev} room={record} active={active && pageVisible} connected={!connectionError} onStop={stopCurrentWork}
          lead={jevLead} trailing={jevRailToggle} onOpenTasks={jevRailAvailable ? openJevRail : undefined} onOpenPlan={focusJevPlan} /> : null}
        {jevEnabled && visiblePanel === 'none' && jevRailVisible ? <>
          {jevRailNarrow ? <button type="button" className="paw-jev-rail__scrim" aria-label="关闭任务栏" tabIndex={-1} onClick={() => setJevRail(false)} /> : null}
          <div className="paw-jev-rail__host" data-overlay={jevRailNarrow || undefined} onKeyDown={event => { if (jevRailNarrow && event.key === 'Escape' && !event.defaultPrevented) { event.stopPropagation(); setJevRail(false); } }}>
            <PawJevTeamPanels graph={jev.snapshot} taskControls={jev.taskControls} room={record} projection={projection} historical={jevHistorical} onCollapse={() => setJevRail(false)} active={active && pageVisible && !connectionError && !jev.error} observeCompletions={Boolean(jev.liveSnapshot && jev.snapshot?.graphId === jev.liveSnapshot.graphId)} onOpenParticipant={openParticipantById} onOpenFile={desktop?.openRoute || desktop?.openApp ? openJevFile : undefined} />
          </div>
        </> : null}
        <section aria-label={`${title} 主 Room`} className="paw-room-workspace__main" role="region">
          {visibleView === 'starfield' && focusProjection ? (
            <LazyPawRoomStarfield
              active={active}
              focus={focusProjection}
              roomId={recordId}
              onExit={() => setView('conversation')}
              onOpenParticipant={openParticipantById}
            />
          ) : (visibleView === 'timeline' || visibleView === 'messages' || visibleView === 'tasks') && record ? (
            <div className="paw-room-timeline-view">
              <PawRoomCollaboration
                active={liveActive}
                section={visibleView === 'messages' ? 'messages' : visibleView === 'tasks' ? 'tasks' : 'timeline'}
                onSectionChange={setView}
                focus={focusProjection}
                room={record}
                projection={projection}
                roomId={recordId}
                graph={jevEnabled ? jevGraph : undefined}
                onSelectRoot={jevEnabled ? rootId => { const graph = jev.items.find(item => item.rootId === rootId); if (graph) jev.selectGraph(graph.id); } : undefined}
                onOpenParticipant={openParticipantById}
              />
            </div>
          ) : visibleView === 'rounds' ? (
            <div className="paw-room-timeline" ref={timelineRef}>
              {roomContentReady && projection && record ? (
                <PawRoomRoundSheet
                  readingRecoveryKey={recoveryScope(transport, `room:${recordId}`)}
                  readingRecoveryReady={roundHistoryReady}
                  participantDestination={participantProcessLocation === 'room-transcript' ? 'room-transcript' : collaborationFocusActive ? 'observer' : 'session'}
                  onOpenParticipant={selectAndOpenParticipant}
                  onResumeBlocked={resumeBlockedWorkItem}
                  projection={projection}
                  resumeErrorByRow={resumeErrorByRow}
                  resumingWorkItemId={resumingWorkItemId}
                  room={record}
                  selectedParticipantId={selectedParticipantId}
                />
              ) : roomRecoverySurface}
            </div>
          ) : <div className="paw-room-timeline" ref={timelineRef}>
              {roomContentReady && projection && record ? <PawRoomConversation
                active={active && liveActive && !loading}
                collaborationMode={jevEnabled ? 'jev' : 'room'}
                graph={jevEnabled ? jev.snapshot : null}
                empty={loading
                  ? <div className="paw-room-workspace__loading"><LoaderCircle className="ui-spin" size={18} />正在恢复 Room 协作现场</div>
                  : jevEnabled ? <div className="paw-room-workspace__empty paw-jev-empty">
                    <span className="paw-jev-empty__planets" aria-hidden>{activeParticipants.slice(0, 5).map(participant => <RoomPlanetAvatar key={participant.id} ordinal={participant.ordinal} size={36} activity="idle" decorative />)}</span>
                    <strong>描述一个目标，团队会接手</strong>
                    <p>简单问题会直接回答；需要分工时，Jev 先给出完整方案，你确认后伙伴才开始执行。</p>
                    <ol><li><b>1</b>说清目标与验收标准</li><li><b>2</b>确认方案与分工</li><li><b>3</b>在任务栏跟进执行、复核与交付</li></ol>
                  </div> : <div className="paw-room-workspace__empty"><Users size={24} /><strong>Room 已准备好</strong><p>发送目标，伙伴会分工、执行并汇合结果。</p></div>}
                {...(optimisticSteer ? {
                  lead: (
                    <article
                      className="ccui-turn ccui-user-turn paw-room-workspace__optimistic-steer"
                      data-client-action-id={optimisticSteer.clientActionId}
                      data-delivery="sending"
                    >
                      <div className="ccui-user-bubble">
                        <div className="ccui-user-text">{optimisticSteer.message}</div>
                      </div>
                      <div className="ccui-user-footer">
                        <div aria-label="等待 Room 回执" aria-live="polite" className="ccui-steer-receipt" role="status">
                          <span>尚未送达伙伴 · {roomPlanetName(record.participants.find((participant) => participant.id === optimisticSteer.participantId)?.ordinal ?? 0)}</span>
                        </div>
                      </div>
                    </article>
                  ),
                } : {})}
                onApprovalDecision={decideApproval}
                onOpenProcessActivity={openProcessActivity}
                onRetryTurn={(message, retryOfRootId) => void send(message, { retryOfRootId, preserveDraft: true })}
                onContinueTurn={(rootId) => void send('继续。请基于当前 Room 已保留的上下文、工具结果和伙伴进展接着完成，不要重复已经完成的操作。', { preserveDraft: true })}
                projection={projection}
                planReview={jevEnabled ? <JevPlanReview execution={jev} room={record} onAdjust={() => {
                  setDraft(value => value || '我想调整方案：');
                  composerRegionRef.current?.querySelector<HTMLTextAreaElement>('textarea[aria-label="协作消息"]')?.focus();
                }} /> : undefined}
                retryingTurn={sending}
                room={record}
              /> : roomRecoverySurface}
          </div>}

          <div className="paw-room-workspace__composer" ref={composerRegionRef}>
              <PawRoomResponseStatus submitting={sending || jevEnabled && jev.creating}
                busy={jevEnabled ? jev.busy || Boolean(activeTurn && !jev.liveSnapshot) : Boolean(activeTurn)}
                stopping={jevEnabled ? jev.stopping : abortingActiveTurn}
                graph={jevEnabled ? jev.liveSnapshot : null} />
              {!jevEnabled && workStatusVisible && focusProjection && workStatus ? <PawRoomWorkStatus
                key={recordId}
                focus={focusProjection}
                status={{ ...workStatus, animate: workStatus.animate && active }}
                onOpenParticipant={selectAndOpenParticipant}
                onRetrySync={() => retrySnapshot()}
                onAnswer={() => composerRegionRef.current?.querySelector<HTMLElement>(
                  '[data-pending-room-input] input, [data-pending-room-input] select, [data-pending-room-input] button, textarea[aria-label="协作消息"]',
                )?.focus({ preventScroll: false })}
              /> : null}
              {collaborationOpenFailures.size ? <div className="paw-room-workspace__planet-open-error" role="alert">
                <CircleAlert size={14} />
                <div>
                  <span>{collaborationOpenFailures.size} 颗活跃行星未能打开</span>
                  <div aria-label="未打开的行星">
                    {[...collaborationOpenFailures].map((participantId) => <button
                      aria-label={`重试打开 ${participantAliases[participantId] ?? participantId}`}
                      key={participantId}
                      onClick={() => retryCollaborationPlanet(participantId)}
                      type="button"
                    >{participantAliases[participantId] ?? participantId} · 重试</button>)}
                  </div>
                  <TraceAgentHandoffButton handoff={{
                    kind: 'room',
                    entityId: `room-planets:${recordId}`,
                    title: 'Room 行星窗口打开失败',
                    summary: `${collaborationOpenFailures.size} 颗活跃行星未能打开。`,
                    roomId: recordId,
                    sourceRoute: `/rooms?room=${encodeURIComponent(recordId)}`,
                    refs: {
                      participantIds: [...collaborationOpenFailures].join(','),
                      participantCount: collaborationOpenFailures.size,
                    },
                  }} />
                </div>
              </div> : null}
              {visibleError ? (
                <div className="paw-room-workspace__error" role="alert">
                  <CircleAlert size={14} />
                  <span>{visibleError}{jevEnabled && jev.pendingInput ? ` · 原请求含 ${jev.pendingInput.attachmentIds?.length ?? 0} 项附件` : ''}</span>
                  {pendingSend ? (
                    <button disabled={sending || pendingSend.status === 'sending'} onClick={() => void deliverRoomSend(pendingSend)} type="button">核实上次发送</button>
                  ) : jevEnabled && jev.pendingInput ? (
                    <button disabled={sending || jev.creating} onClick={() => void retryJevAdmission()} type="button">重试上次发送</button>
                  ) : jevEnabled && jev.pendingPlan ? (
                    <button disabled={sending || Boolean(jev.planSending)} onClick={() => void retryJevPlan()} type="button">核实上次方案操作</button>
                  ) : visibleError === ROOM_WORKSPACE_MISSING_TEXT ? (
                    <button onClick={() => void manageWorkspaceRoots()} type="button">选择工作目录</button>
                  ) : !error && connectionError ? (
                    <button onClick={() => retrySnapshot()} type="button">重新同步</button>
                  ) : (
                    <button onClick={() => setError('')} type="button">知道了</button>
                  )}
                  <TraceAgentHandoffButton
                    handoff={{
                      kind: 'room',
                      entityId: recordId,
                      title: error ? 'Room 操作未完成' : 'Room 同步失败',
                      summary: visibleError,
                      error: visibleError,
                      roomId: recordId,
                      sourceRoute: `/rooms?room=${encodeURIComponent(recordId)}`,
                    }}
                  />
                </div>
              ) : null}
              {pendingGroupedInput ? <div data-pending-room-input><GenericUserInputCard activity={pendingGroupedInput} sessionId={pendingGroupedInput.sourceSessionId} onError={setError} /></div> : (
                <>
                  <QueueTray busy={sending} controller={queue} />
                  <RoomComposer
                    expandInDialog={jevEnabled}
                    capabilityControls={record ? <>
                      <Dialog open={partnerSettingsOpen} onOpenChange={setPartnerSettingsOpen}>
                        <DialogTrigger asChild><button type="button" className="paw-room-observer-toggle" aria-label="伙伴工具与记忆" title="伙伴工具、记忆与模型设置">
                          <Settings2 size={15} aria-hidden="true" /><span>伙伴设置</span>
                        </button></DialogTrigger>
                        <DialogContent className="paw-jev-partner-settings">
                          <DialogTitle>伙伴设置</DialogTitle>
                          <DialogDescription>选择伙伴，查看模型、工具和记忆设置。{jevEnabled ? '任务执行期间，修改选项会暂时锁定。' : ''}</DialogDescription>
                          <div className="paw-jev-partner-settings__controls">{partnerControls}</div>
                        </DialogContent>
                      </Dialog>
                      {jevEnabled ? <JevPolicyControls modelRouting={jev.modelRouting} toolApprovalMode={jev.toolApprovalMode} verificationMode={jev.verificationMode} onModelRouting={jev.setModelRouting} onToolApprovalMode={jev.setToolApprovalMode} onVerificationMode={jev.setVerificationMode} /> : null}
                      {!jevEnabled ? <button type="button" role="switch" aria-label="显示任务状态栏" aria-checked={workStatusVisible}
                        className="paw-room-observer-toggle"
                        title={workStatusVisible ? '隐藏输入框上方的任务状态栏' : '显示输入框上方的任务状态栏'}
                        onClick={() => setWorkStatusVisible(!workStatusVisible)}>
                        <ListChecks size={15} aria-hidden="true" /><span>任务状态</span><small>{workStatusVisible ? '开' : '关'}</small>
                      </button> : null}
                      {!jevEnabled ? <button type="button" role="switch" aria-label="伙伴窗口自动弹出" aria-checked={observerAutoOpen}
                        className="paw-room-observer-toggle"
                        title={observerAutoOpen ? '协同模式下自动弹出伙伴窗口；点击关闭。' : '协同模式下不自动弹窗；仍可手动打开伙伴。点击开启。'}
                        onClick={() => setObserverAutoOpen(!observerAutoOpen)}>
                        <PanelsTopLeft size={15} aria-hidden="true" /><span>自动弹窗</span><small>{observerAutoOpen ? '开' : '关'}</small>
                      </button> : null}
                    </> : undefined}
                    room={record}
                    participantAliases={participantAliases}
                    personas={personas}
                    draft={draft}
                    attachments={attachments}
                    sending={sending}
                    uncertainSubmission={jevEnabled && Boolean(jev.pendingInput)}
                    awaitingExecutionStart={!jevEnabled && activeTurn?.status === 'queued'}
                    taskBusyState={jevEnabled ? jev.busy || activeTurn && !jev.liveSnapshot
                      ? jev.liveSnapshot?.phase === 'route' && jevAbstention(jev.liveSnapshot) ? 'waiting' : 'running'
                      : undefined : taskBusyState}
                    busySubmitBehavior={jevEnabled ? 'queue' : 'steer'}
                    onStop={stopCurrentWork}
                    stopping={jevEnabled ? jev.stopping : abortingActiveTurn}
                    onInvitePartners={() => setPanel('governance')}
                    pendingUserAnswer={pendingQuestion?.roomId === recordId}
                    queueDepth={queue.queue.length}
                    onDraftChange={setDraft}
                    onQueue={queueFollowUp}
                    onSend={(value) => send(value, { question: pendingQuestion?.roomId === recordId ? pendingQuestion : undefined })}
                    continuationAvailable={continuationAvailable}
                    onContinue={() => void send('继续。请基于当前 Room 已保留的上下文、工具结果和伙伴进展接着完成，不要重复已经完成的操作。', { preserveDraft: true })}
                    onAttachmentsChange={setAttachments}
                    onPasteImages={pasteFiles}
                    onPasteFromClipboard={() => void pasteFiles()}
                    onPickAttachments={() => void pickAttachments()}
                  />
                </>
              )}
          </div>
        </section>
        {visiblePanel !== 'none' && record ? <PawRoomToolWorkspace
          onClosePanel={closeCollaborationPanel}
          onError={setError}
          onOpenParticipant={openParticipantById}
          onPanelChange={setPanel}
          {...(desktop ? { onPopout: openFocusWindow } : {})}
          onRefresh={async () => { retrySnapshot(); }}
          onRoomUpdated={onRoomUpdated}
          panel={visiblePanel}
          personas={personas}
          focusProjection={focusProjection}
          projection={projection}
          liveActive={liveActive}
          room={record}
          onSelectParticipant={setSelectedParticipantId}
          selectedParticipantId={selectedParticipantId}
        /> : null}
      </div>
    </section>
  );

}

function PawRoomToolWorkspace({
  onClosePanel,
  onError,
  onOpenParticipant,
  onSelectParticipant,
  selectedParticipantId,
  onPanelChange,
  onPopout,
  onRefresh,
  onRoomUpdated,
  panel,
  personas,
  focusProjection,
  liveActive,
  projection,
  room,
}: {
  onClosePanel: () => void;
  onError: (message: string) => void;
  onOpenParticipant: (participantId: string, background?: boolean) => void;
  onSelectParticipant: (participantId: string) => void;
  selectedParticipantId: string;
  onPanelChange: (panel: RoomToolPanel) => void;
  onPopout?: () => void;
  onRefresh: () => Promise<void>;
  onRoomUpdated: (room: RoomSummary) => void;
  panel: RoomToolPanel;
  personas: AgentPersonaV1[];
  focusProjection?: RoomFocusProjection;
  projection?: import('@/contracts/room-reducer').RoomProjectionState;
  liveActive: boolean;
  room: RoomSummary;
}) {
  const tabId = useId();
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const moveTabFocus = useCallback((event: KeyboardEvent<HTMLButtonElement>, current: RoomToolPanel) => {
    const currentIndex = roomToolPanelItems.indexOf(current);
    if (currentIndex < 0) return;
    let nextIndex: number;
    if (event.key === 'ArrowRight') nextIndex = (currentIndex + 1) % roomToolPanelItems.length;
    else if (event.key === 'ArrowLeft') nextIndex = (currentIndex - 1 + roomToolPanelItems.length) % roomToolPanelItems.length;
    else if (event.key === 'Home') nextIndex = 0;
    else if (event.key === 'End') nextIndex = roomToolPanelItems.length - 1;
    else return;
    event.preventDefault();
    onPanelChange(roomToolPanelItems[nextIndex]);
    tabRefs.current[nextIndex]?.focus();
  }, [onPanelChange]);
  const handleAsideKeyDown = useCallback((event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== 'Escape') return;
    event.stopPropagation();
    onClosePanel();
  }, [onClosePanel]);
  /* 空间指向：协作态势键在标题栏尾端，面板也必须从尾端展开。data-side 把这个
     朝向写成契约而不是 DOM 顺序的副作用，CSS 用同名网格区落位。 */
  return <aside aria-label="Room 协作态势" className="paw-room-tools" data-side="trailing" onKeyDown={handleAsideKeyDown}>
    <header className="paw-room-tools__header">
      <span><Focus aria-hidden="true" size={15} /><strong>协作态势</strong></span>
      <div className="paw-room-tools__actions">
        {onPopout ? <button aria-label="在协作窗口中打开协作态势" onClick={onPopout} type="button"><ExternalLink aria-hidden="true" size={14} /></button> : null}
        <button aria-label="关闭协作态势" onClick={onClosePanel} type="button"><X aria-hidden="true" size={15} /></button>
      </div>
    </header>
    <nav aria-label="协作工具视图" aria-orientation="horizontal" className="paw-room-tools__tabs" role="tablist">
      {roomToolPanelItems.map((item, index) => {
        const Icon = roomToolPanelIcons[item];
        return <button
          aria-controls={`${tabId}-panel`}
          aria-selected={item === panel}
          id={`${tabId}-${item}`}
          key={item}
          onClick={() => onPanelChange(item)}
          onKeyDown={(event) => moveTabFocus(event, item)}
          role="tab"
          ref={(node) => { tabRefs.current[index] = node; }}
          tabIndex={item === panel ? 0 : -1}
          type="button"
        ><Icon aria-hidden="true" size={14} /><span>{roomToolPanelLabels[item]}</span></button>;
      })}
    </nav>
    <div aria-labelledby={`${tabId}-${panel}`} className="paw-room-tools__content" id={`${tabId}-panel`} role="tabpanel">
      {panel === 'focus' && focusProjection ? <PawRoomCollaboration
        active={liveActive}
        focus={focusProjection}
        roomId={room.id}
        room={room}
        projection={projection}
        initialSection="tasks"
        onOpenParticipant={onOpenParticipant}
        onSelectParticipant={onSelectParticipant}
        selectedParticipantId={selectedParticipantId}
      /> : null}
      {panel === 'governance' ? <PawRoomGovernance personas={personas} room={room} onError={onError} onRefresh={onRefresh} onRoomUpdated={onRoomUpdated} /> : null}
    </div>
  </aside>;
}

export function PawRoomGovernance({
  personas,
  room,
  onError,
  onRefresh,
  onRoomUpdated,
}: {
  personas: AgentPersonaV1[];
  room?: RoomSummary;
  onError: (message: string) => void;
  onRefresh: () => Promise<void>;
  onRoomUpdated: (room: RoomSummary) => void;
}) {
  if (!room) return <div className="paw-room-governance paw-room-governance--empty">Room 元数据尚未恢复。</div>;
  return (
    <PawRoomGovernanceInner
      key={room.id}
      onError={onError}
      onRefresh={onRefresh}
      onRoomUpdated={onRoomUpdated}
      personas={personas}
      room={room}
    />
  );
}


function PawRoomGovernanceInner({
  personas,
  room,
  onError,
  onRefresh,
  onRoomUpdated,
}: {
  personas: AgentPersonaV1[];
  room: RoomSummary;
  onError: (message: string) => void;
  onRefresh: () => Promise<void>;
  onRoomUpdated: (room: RoomSummary) => void;
}) {
  const transport = useControlTransport();
  const [busyKey, setBusyKey] = useState('');
  const removals = useRoomRemovals(room, onRefresh);
  const [joinedIds, setJoinedIds] = useState<string[]>([]);
  const previousMemberIds = useRef(new Set(room.participants.filter((item) => item.status === 'active').map((item) => item.id)));
  useEffect(() => {
    const current = new Set(room.participants.filter((item) => item.status === 'active').map((item) => item.id));
    const joined = [...current].filter((id) => !previousMemberIds.current.has(id));
    previousMemberIds.current = current;
    if (joined.length) setJoinedIds(joined);
  }, [room.participants]);
  useEffect(() => {
    if (!joinedIds.length) return;
    const timer = window.setTimeout(() => setJoinedIds([]), 4200);
    return () => window.clearTimeout(timer);
  }, [joinedIds]);
  const [topicTitle, setTopicTitle] = useState('');
  const [topicSummary, setTopicSummary] = useState('');
  const [workObjective, setWorkObjective] = useState('');
  const [workOutput, setWorkOutput] = useState('');
  const [workOwner, setWorkOwner] = useState('');
  const [title, setTitle] = useState(room.title);
  const [description, setDescription] = useState(room.description ?? '');
  const storedPermissionPolicy = useMemo(
    () => parseRoomPermissionPolicy(room.permissionPolicy, room.roomKind),
    [room.permissionPolicy, room.roomKind],
  );
  const [permissionPolicy, setPermissionPolicy] = useState<RoomPermissionPolicy | undefined>(
    storedPermissionPolicy,
  );
  const activeParticipants = room.participants.filter((item) => item.status === 'active');
  const availablePersonas = personas.filter((persona) => !activeParticipants.some((item) => item.roleId === persona.roleId && item.roleVersion === persona.version));
  const participantLimitReached = activeParticipants.length >= ROOM_PARTICIPANT_LIMIT;
  const nextPlanetName = roomPlanetName(Math.max(-1, ...room.participants.map((participant) => participant.ordinal)) + 1);
  const permissionPolicyChanged = !roomPermissionPoliciesEqual(
    permissionPolicy,
    storedPermissionPolicy,
  );
  const permissionDisplayLabel = permissionPolicy
    ? roomPermissionLayerPresentation(
        permissionPolicy,
        'room',
        room.roomKind ?? 'collaboration',
      ).effectiveLabel
    : '分层权限不可用';

  async function mutate(key: string, request: ControlRequest): Promise<void> {
    setBusyKey(key);
    onError('');
    try {
      const response = await transport.request<Record<string, unknown>>(request);
      removals.receive(response.removal);
      const updated = roomFromResponse(response);
      if (updated) onRoomUpdated(updated);
      else await onRefresh();
    } catch (reason) { onError(publicErrorText(reason, 'Room 设置没有更新。')); }
    finally { setBusyKey(''); }
  }

  function removeParticipant(participantId: string, extra: { replacementParticipantId?: string; stopRoot?: boolean } = {}) {
    void mutate(`remove:${participantId}`, { pathId: 'agent.room.participant.remove', params: { roomId: room.id },
      body: { participantId, clientMessageId: `remove-${crypto.randomUUID()}`, ...extra } });
  }

  async function createTopic(): Promise<void> {
    if (!topicTitle.trim()) return;
    await mutate('topic:create', { pathId: 'agent.room.topic.create', params: { roomId: room.id }, body: { title: topicTitle.trim(), summary: topicSummary.trim() } });
    setTopicTitle(''); setTopicSummary('');
  }

  async function createWorkItem(): Promise<void> {
    const ownerId = workOwner || activeParticipants[0]?.id || '';
    if (!workObjective.trim() || !workOutput.trim() || !ownerId) return;
    await mutate('work:create', {
      pathId: 'agent.room.workItem.create',
      params: { roomId: room.id },
      body: {
        objective: workObjective.trim(),
        expectedOutput: workOutput.trim(),
        currentOwnerParticipantId: ownerId,
        accountableParticipantId: room.moderatorParticipantId || ownerId,
        createdByParticipantId: room.moderatorParticipantId || ownerId,
        clientMessageId: `paw-work-${crypto.randomUUID()}`,
        topicId: room.activeTopicId ?? '',
        acceptanceCriteria: [],
        state: 'queued',
        depth: 0,
      },
    });
    setWorkObjective(''); setWorkOutput('');
  }

  return <div className="paw-room-governance">
    <header><span><strong>Room 治理</strong><small>伙伴、话题、工作项与边界</small></span><button onClick={() => void onRefresh()} type="button">刷新</button></header>
    <section>
      <header><span><Users size={15} /><strong>伙伴与分工</strong></span><small>{activeParticipants.length}/{ROOM_PARTICIPANT_LIMIT}</small></header>
      {busyKey.startsWith('add:') ? <p className="paw-room-governance__arrival" role="status"><LoaderCircle className="ui-spin" size={16} />正在邀请 {nextPlanetName} 加入…</p> : joinedIds.length ? <p className="paw-room-governance__arrival" role="status"><UserPlus size={16} />{activeParticipants.filter((item) => joinedIds.includes(item.id)).map((item) => roomPlanetName(item.ordinal)).join('、')} 已加入，可以在对话中 @ 点名接手</p> : null}
      <div className="paw-room-governance__members">{activeParticipants.map((participant) => <article key={participant.id} data-arriving={joinedIds.includes(participant.id) || undefined}>
        <RoomPlanetAvatar ordinal={participant.ordinal} size={30} decorative />
        <span><strong>{roomPlanetName(participant.ordinal)}</strong>{room.roomKind === 'roleplay' ? <small>{roomCollaborationRoleLabel(participant.collaborationRole)}</small> : null}</span>
        {room.roomKind !== 'roleplay' ? <Select aria-label={`${roomPlanetName(participant.ordinal)} 的分工`} disabled={Boolean(busyKey)} onValueChange={(collaborationRole) => void mutate(`role:${participant.id}`, { pathId: 'agent.room.participant.update', params: { roomId: room.id }, body: { participantId: participant.id, collaborationRole } })} options={roomCollaborationRoleOptions(participant.collaborationRole)} value={participant.collaborationRole ?? 'implementer'} /> : null}
        <button aria-label={`移出 ${roomPlanetName(participant.ordinal)}`} disabled={Boolean(busyKey) || removals.items.some(item => item.participantId === participant.id && item.status === 'pending') || activeParticipants.length <= 2 || (room.routingPolicy === 'moderator' && participant.id === room.moderatorParticipantId)} onClick={() => removeParticipant(participant.id)} type="button">{busyKey === `remove:${participant.id}` ? <LoaderCircle className="ui-spin" size={14} /> : <UserMinus size={14} />}</button>
      </article>)}</div>
      <PawRoomRemovalProgress room={room} items={removals.items} error={removals.error} busy={Boolean(busyKey)} onRemove={removeParticipant} />
      {availablePersonas.length ? <div className="paw-room-governance__invite"><span aria-hidden="true"><UserPlus size={14} />邀请伙伴</span><Select aria-label="邀请伙伴" disabled={Boolean(busyKey) || participantLimitReached} onValueChange={(key) => { if (participantLimitReached) return; const persona = personas.find((item) => `${item.roleId}:${item.version}` === key); if (persona) void mutate(`add:${persona.roleId}`, { pathId: 'agent.room.participant.add', params: { roomId: room.id }, body: { roleId: persona.roleId, roleVersion: persona.version, collaborationRole: 'implementer' } }); }} options={participantLimitReached ? [{ value: '', label: `已达 ${ROOM_PARTICIPANT_LIMIT} 人上限` }] : availablePersonas.map((persona) => ({ value: `${persona.roleId}:${persona.version}`, label: `${nextPlanetName} · ${persona.tagline || '协作伙伴'}` }))} placeholder={participantLimitReached ? `已达 ${ROOM_PARTICIPANT_LIMIT} 人上限` : `选择 ${nextPlanetName} 的分工…`} value="" /></div> : null}
    </section>

    <section>
      <header><span><MessageCircle size={15} /><strong>话题</strong></span><small>{room.topics?.length ?? 0}</small></header>
      <div className="paw-room-governance__topics">{(room.topics ?? []).map((topic) => <article data-active={topic.id === room.activeTopicId || undefined} key={topic.id}><span><strong>{topic.title}</strong><small>{topic.summary || '暂无摘要'}</small></span>{topic.status === 'active' && topic.id !== room.activeTopicId ? <button onClick={() => void mutate(`topic:${topic.id}`, { pathId: 'agent.room.topic.update', params: { roomId: room.id }, body: { topicId: topic.id, activate: true } })} type="button">切换</button> : null}{topic.status === 'active' && topic.id !== room.activeTopicId ? <button aria-label={`归档 ${topic.title}`} onClick={() => void mutate(`archive-topic:${topic.id}`, { pathId: 'agent.room.topic.update', params: { roomId: room.id }, body: { topicId: topic.id, archived: true } })} type="button"><Archive size={13} /></button> : null}</article>)}</div>
      <div className="paw-room-governance__form"><input aria-label="新话题名称" maxLength={120} onChange={(event) => setTopicTitle(event.target.value)} placeholder="新话题" value={topicTitle} /><input aria-label="新话题摘要" maxLength={2000} onChange={(event) => setTopicSummary(event.target.value)} placeholder="摘要（可选）" value={topicSummary} /><button disabled={!topicTitle.trim() || Boolean(busyKey)} onClick={() => void createTopic()} type="button"><Plus size={14} />创建</button></div>
    </section>

    <section>
      <header><span><GitBranch size={15} /><strong>工作项</strong></span><small>{room.workItems?.length ?? 0}</small></header>
      <div className="paw-room-governance__work">{(room.workItems ?? []).map((work) => <article data-state={work.state} key={work.id}><span><strong>{work.objective}</strong><small>{roomWorkStateLabel(work.state)} · {participantName(room, work.currentOwnerParticipantId)}</small></span><Select aria-label={`重新分配 ${work.objective}`} disabled={Boolean(busyKey) || ['done', 'failed', 'cancelled'].includes(work.state)} onValueChange={(targetParticipantId) => void mutate(`work:${work.id}`, { pathId: 'agent.room.workItem.reassign', params: { roomId: room.id, workItemId: work.id }, body: { actorParticipantId: room.moderatorParticipantId || activeParticipants[0]?.id, targetParticipantId, reason: '用户在 Room 治理面板重新分配' } })} options={activeParticipants.map((participant) => ({ value: participant.id, label: roomPlanetName(participant.ordinal) }))} value={work.currentOwnerParticipantId} /></article>)}</div>
      <div className="paw-room-governance__form"><input aria-label="工作项目标" maxLength={500} onChange={(event) => setWorkObjective(event.target.value)} placeholder="要完成什么" value={workObjective} /><input aria-label="工作项交付" maxLength={500} onChange={(event) => setWorkOutput(event.target.value)} placeholder="期望交付" value={workOutput} /><Select aria-label="工作项负责人" onValueChange={setWorkOwner} options={activeParticipants.map((participant) => ({ value: participant.id, label: roomPlanetName(participant.ordinal) }))} placeholder="选择负责人" value={workOwner} /><button disabled={!workObjective.trim() || !workOutput.trim() || Boolean(busyKey)} onClick={() => void createWorkItem()} type="button"><Plus size={14} />创建</button></div>
    </section>

    <section>
      <header><span><Settings2 size={15} /><strong>空间设置</strong></span><small>{permissionDisplayLabel}</small></header>
      <label className="paw-room-jev-control">Room 路由中控<Select aria-label="Room 路由中控" disabled={Boolean(busyKey)} value={room.routingPolicy} options={[
        { value: 'jev', label: 'Jev 中控' }, { value: 'natural', label: '传统主控' },
        ...(!['jev', 'natural'].includes(room.routingPolicy) ? [{ value: room.routingPolicy, label: '当前路由策略' }] : []),
      ]} onValueChange={routingPolicy => void mutate('routing', { pathId: 'agent.room.archive', params: { roomId: room.id }, body: { routingPolicy } })} /></label>
      {room.routingPolicy === 'jev' ? <p>未指定负责人的新消息由 Jev 按公开责任选择伙伴。消息正文和伙伴责任会发送给 Jev；@ 指定与已有任务负责人优先，无法判断时保留原主控。</p> : null}

      <div className="paw-room-governance__form">
        <input aria-label="Room 名称" maxLength={120} onChange={(event) => setTitle(event.target.value)} value={title} />
        <input aria-label="Room 简介" maxLength={500} onChange={(event) => setDescription(event.target.value)} placeholder="简介" value={description} />
        <RoomPermissionPolicyEditor
          onChange={permissionPolicy ? setPermissionPolicy : undefined}
          menuSelect
          policy={permissionPolicy}
          roomKind={room.roomKind ?? 'collaboration'}
        />
        <button
          disabled={!title.trim() || Boolean(busyKey)}
          onClick={() => void mutate('settings', {
            pathId: 'agent.room.archive',
            params: { roomId: room.id },
            body: {
              archived: false,
              title: title.trim(),
              description: description.trim(),
              ...(permissionPolicy ? { permissionPolicy } : {}),
              routingPolicy: room.routingPolicy,
              routingConfig: (room.routingConfig ?? null) as unknown as Record<string, unknown>,
              moderatorParticipantId: room.moderatorParticipantId,
              ...(permissionPolicyChanged
                && permissionPolicy
                && roomPermissionPolicyNeedsWorkspaceConfirmation(permissionPolicy)
                ? { workspaceScopeConfirmation: 'APPROVE_WORKSPACE_SCOPE' }
                : {}),
              ...(permissionPolicyChanged
                && permissionPolicy
                && roomPermissionPolicyNeedsDangerousConfirmation(permissionPolicy)
                ? { dangerousModeConfirmation: 'ENABLE_FULL_TRUST' }
                : {}),
            } as unknown as ControlRequest['body'],
          })}
          type="button"
        >
          保存
        </button>
      </div>
      <button className="paw-room-governance__archive" disabled={Boolean(busyKey)} onClick={() => void mutate('archive', { pathId: 'agent.room.archive', params: { roomId: room.id }, body: { archived: true } })} type="button"><Archive size={14} />收起 Room</button>
    </section>
  </div>;
}

function roomAttachment(file: PickedFile, roomId: string): RoomAttachmentReceipt {
  if (file.roomId !== roomId || !isComposerAttachmentMimeType(file.mimeType) || !file.sha256) throw new TypeError('Room 附件回执无效。');
  return { mediaId: file.id, roomId, fileName: file.name.slice(0, 160) || '附件', mimeType: file.mimeType.toLowerCase(), byteSize: file.byteSize, sha256: file.sha256 };
}

function roomFromResponse(value: unknown): RoomSummary | undefined {
  const source = asRecord(value);
  return asRoom(source.room) ?? asRoom(value);
}

function roomErrorText(reason: unknown, fallback: string): string {
  const message = publicAgentErrorText(reason, fallback);
  return message === SESSION_WORKSPACE_MISSING_TEXT
    ? ROOM_WORKSPACE_MISSING_TEXT
    : message;
}

function asRoom(value: unknown): RoomSummary | undefined {
  const source = asRecord(value);
  return typeof source.id === 'string' && typeof source.title === 'string' && Array.isArray(source.participants) ? source as unknown as RoomSummary : undefined;
}

function asWorkItem(value: unknown): RoomWorkItem | undefined {
  const source = asRecord(value);
  return typeof source.id === 'string' && typeof source.roomId === 'string' && typeof source.objective === 'string' ? source as unknown as RoomWorkItem : undefined;
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function roomEventClientActionId(value: unknown): string {
  const event = asRecord(value);
  const payload = asRecord(event.payload);
  const message = asRecord(payload.message);
  const post = asRecord(payload.post);
  const publicationSource = asRecord(post.publicationSource);
  const candidates = [
    payload.clientActionId,
    payload.clientMessageId,
    payload.client_action_id,
    payload.client_message_id,
    message.clientActionId,
    message.clientMessageId,
    post.clientActionId,
    post.clientMessageId,
    publicationSource.kind === 'user' ? publicationSource.ref : undefined,
  ];
  return candidates.find((candidate): candidate is string => (
    typeof candidate === 'string' && candidate.trim().length > 0
  ))?.trim() ?? '';
}

function participantName(room: RoomSummary | undefined, participantId: string): string {
  const participant = room?.participants.find((item) => item.id === participantId);
  return participant ? roomPlanetName(participant.ordinal) : '未分配';
}
