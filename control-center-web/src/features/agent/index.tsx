import { AlertCircle, FolderTree, GitBranch, History, LoaderCircle, Network, PanelLeftClose, PanelLeftOpen, PanelRightClose, PanelRightOpen } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useControlTransport } from '@/app/control-transport';
import { useComposerClearance } from '@/components/layout/use-composer-clearance';
import { IconButton } from '@/components/primitives';
import { isComposerImageMimeType } from '@/contracts/attachment-policy';
import {
  useAgentLiveSession,
  type AgentLiveSnapshotLoader,
} from './runtime/use-agent-live-session';
import {
  agentMessageDelivery,
  resolveAgentTurnUserMessage,
  type AgentActivityProjection,
  type AgentMessageProjection,
  type AgentProjectionState,
} from '@/contracts/agent-reducer';
import type { AgentPersonaV1 } from '@/contracts/generated/agent-persona.v1';
import { approvalNeedsHumanDecision } from '@/contracts/approval-decision';
import { AgentComposer, type AgentComposerEditState, type AgentMessageDelivery } from './composer/AgentComposer';
import { unrestrictedWorkspaceRoots } from './composer/permission-policy';
import { AgentPaneResizer } from './layout/AgentPaneResizer';
import { SessionRail } from './sessions/SessionRail';
import { AgentConversationState } from './sessions/AgentConversationState';
import {
  ConversationForkDialog,
  resolveConversationEntryId,
  type ConversationNode,
} from './sessions/ConversationForkDialog';
import { NewSessionDialog, type NewSessionInput } from './sessions/NewSessionDialog';
import { AgentStatusPanel } from './status/AgentStatusPanel';
import { AgentFilesPanel } from './workspace/AgentFilesPanel';
import { SessionSubagentPanel } from './delegation/SessionSubagentPanel';
import { useMediaQuery, useModalPanel } from './overlay-dialog';
import { agentProjection, useAgentLiveStore } from './state/live-store';
import { useModelSelectionController } from './state/use-model-selection-controller';
import { useSessionComposerInputs } from './state/use-session-composer-inputs';
import { AgentTimeline } from './timeline/AgentTimeline';
import { AgentSendTimingTracker, monotonicNow } from './send-stage-timing';
import { toolIntentPrompt } from './tool-presentation';
import { useProductIdentity } from '@/features/identity/product-identity';
import { usePawOsAppSurface, usePawOsDesktop } from '@/features/paw-os/surface-context';
import {
  capabilityScopeLabel,
  requireSessionCapabilityCatalog,
  type CapabilityCatalog,
  type CapabilityMutationOutcome,
  type CapabilityPreference,
} from '@/features/plugins/capability-policy';
import {
  isAgentCommandPending,
  isAgentTurnConflict,
  isAmbiguousAgentPromptFailure,
  isUnresolvedAgentCommandPending,
  publicAgentErrorText,
} from './public-error';
import { ApprovalReviewDialog, GenericUserInputCard, MemoryReviewDialog } from './review/AgentReviewDialogs';
import {
  activeSessionId,
  commandItems,
  isModelCatalog,
  roleItems,
  sessionPermissionLabel,
  sessionItems,
  toolItems,
  type AgentCommand,
  type AgentPermissionSelection,
  type AgentProductCommandName,
  type ModelCatalog,
  type SessionSummary,
  type ThinkingLevel,
  type ToolManifest,
} from './types';
import './agent.css';
import { resolveAgentSurfaceLayout } from './surface-layout';

export function AgentFeature({ pawOsWorkbench = false }: { pawOsWorkbench?: boolean } = {}) {
  return <AgentWorkspace pawOsWorkbench={pawOsWorkbench} />;
}

function AgentWorkspace({ pawOsWorkbench }: { pawOsWorkbench: boolean }) {
  const transport = useControlTransport();
  const identity = useProductIdentity();
  const appSurface = usePawOsAppSurface();
  const pawOsDesktop = usePawOsDesktop();
  const browserMobileViewport = useMediaQuery('(max-width: 760px)');
  const browserStatusOverlayViewport = useMediaQuery('(max-width: 1360px)');
  const surfaceLayout = resolveAgentSurfaceLayout({
    browserMobile: browserMobileViewport,
    browserStatusOverlay: browserStatusOverlayViewport,
    surface: appSurface?.appId === 'agent' ? appSurface : null,
  });
  const mobileViewport = surfaceLayout.compact;
  const statusOverlayViewport = surfaceLayout.overlayInspectors;
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedSessionId = searchParams.get('session')?.trim() ?? '';
  const requestedDraft = searchParams.get('draft')?.trim().slice(0, 4_000) ?? '';
  const requestedSubagentsOpen = searchParams.get('subagents') === 'open';
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [personas, setPersonas] = useState<AgentPersonaV1[]>([]);
  // A Room task/owner deep link already carries the exact Session identity.
  // Start its snapshot in parallel with the slower rail catalog instead of
  // leaving the conversation blank until all projects and Sessions arrive.
  const [selectedId, setSelectedId] = useState(requestedSessionId);
  const [catalog, setCatalog] = useState<ModelCatalog>();
  const [commands, setCommands] = useState<AgentCommand[]>([]);
  const [tools, setTools] = useState<ToolManifest[]>([]);
  const [toolCatalogStatus, setToolCatalogStatus] = useState<'loading' | 'ready' | 'failed'>('loading');
  const [capabilityCatalog, setCapabilityCatalog] = useState<CapabilityCatalog>();
  const [capabilityCatalogError, setCapabilityCatalogError] = useState('');
  const [capabilityPolicyMutations, setCapabilityPolicyMutations] = useState<Map<string, CapabilityMutationOutcome>>(() => new Map());
  const [conversationForkAvailable, setConversationForkAvailable] = useState(false);
  const [conversationRewriteAvailable, setConversationRewriteAvailable] = useState(false);
  const [editTarget, setEditTarget] = useState<AgentComposerEditState>();
  const [rewriteResolvingSessionIds, setRewriteResolvingSessionIds] = useState<Set<string>>(() => new Set());
  const [showArchived, setShowArchived] = useState(false);
  const [loading, setLoading] = useState(true);
  const [sessionLoadError, setSessionLoadError] = useState('');
  const [contextSnapshot, setContextSnapshot] = useState<{
    sessionId: string;
    state: 'restoring' | 'partial';
  }>();
  const [sendingSessionIds, setSendingSessionIds] = useState<Set<string>>(() => new Set());
  const [stoppingSessionIds, setStoppingSessionIds] = useState<Set<string>>(() => new Set());
  const [modelPickerRequest, setModelPickerRequest] = useState(0);
  const [thinkingPickerRequest, setThinkingPickerRequest] = useState(0);
  const [permissionPickerRequest, setPermissionPickerRequest] = useState(0);
  const [toolPickerRequest, setToolPickerRequest] = useState(0);
  const [toolPickerQuery, setToolPickerQuery] = useState('');
  const [helpRequest, setHelpRequest] = useState(0);
  const [requestedApproval, setRequestedApproval] = useState<AgentActivityProjection>();
  const [newSessionOpen, setNewSessionOpen] = useState(false);
  const [forkDialogOpen, setForkDialogOpen] = useState(false);
  const [forkDialogNodes, setForkDialogNodes] = useState<ConversationNode[]>([]);
  const [forkDialogInitialEntryId, setForkDialogInitialEntryId] = useState('');
  const [timelineJumpRequest, setTimelineJumpRequest] = useState<{ messageId: string; requestId: number }>();
  const [timelineFollow, setTimelineFollow] = useState({ following: true, unseenUpdates: 0 });
  const [scrollToLatestRequest, setScrollToLatestRequest] = useState(0);
  const [snapshotReadySessionId, setSnapshotReadySessionId] = useState('');
  const [metadataGraceSessionId, setMetadataGraceSessionId] = useState('');
  const [railOpen, setRailOpen] = useState(() => !mobileViewport);
  const [statusOpen, setStatusOpen] = useState(shouldOpenTaskCenterByDefault);
  const [filesOpen, setFilesOpen] = useState(false);
  const [subagentsOpen, setSubagentsOpen] = useState(requestedSubagentsOpen);
  const [error, setVisibleError] = useState('');
  const [sendTimings] = useState(() => new AgentSendTimingTracker());
  const listedSession = sessions.find((item) => item.id === selectedId);
  // Keep a deep-linked conversation usable while a stale rail refresh catches
  // up. Actions still reconcile the id against the canonical catalog first.
  const session = listedSession ?? (
    selectedId && !loading ? provisionalSessionRecord(selectedId) : undefined
  );
  const sessionMetadataKnown = Boolean(listedSession);
  const evaluationSnapshot = listedSession?.evaluationSnapshot === true;
  const sessionControlsAvailable = sessionMetadataKnown && !evaluationSnapshot;
  const visibleRailSessions = sessions.filter((item) => item.evaluationSnapshot !== true);
  const isRoomParticipant = Boolean(session?.roomParticipant);
  const sessionOwnerKind = !session ? 'unknown' : isRoomParticipant ? 'room' : 'user';
  const isUserConversation = sessionOwnerKind === 'user';
  const railToggleRef = useRef<HTMLButtonElement>(null);
  const railRef = useRef<HTMLElement>(null);
  const statusToggleRef = useRef<HTMLButtonElement>(null);
  const statusRef = useRef<HTMLElement>(null);
  const filesToggleRef = useRef<HTMLButtonElement>(null);
  const filesRef = useRef<HTMLElement>(null);
  const subagentsToggleRef = useRef<HTMLButtonElement>(null);
  const subagentsRef = useRef<HTMLElement>(null);
  const conversationRef = useRef<HTMLElement>(null);
  useComposerClearance(conversationRef);
  const selectedIdRef = useRef(selectedId);
  const sessionErrorsRef = useRef(new Map<string, string>());
  const catalogNoticesRef = useRef(new Map<string, Map<string, string>>());
  const sessionSendLocksRef = useRef(new Set<string>());
  const sessionActionLocksRef = useRef(new Set<string>());
  const sessionMetadataRef = useRef<{
    evaluationSnapshot: boolean;
    known: boolean;
    sessionId: string;
  }>({ evaluationSnapshot: false, known: false, sessionId: '' });
  sessionMetadataRef.current = {
    evaluationSnapshot,
    known: sessionMetadataKnown,
    sessionId: selectedId,
  };
  const stopReconcileEscalatedSessionsRef = useRef(new Set<string>());
  const modelCatalogCacheRef = useRef(new Map<string, ModelCatalog>());
  const forkCatalogCacheRef = useRef(new Map<string, Record<string, unknown>>());
  const loadAgentSnapshotRef = useRef<AgentLiveSnapshotLoader>(
    () => Promise.resolve(false),
  );
  const loadFullSnapshotRef = useRef<() => void>(() => {});
  const loadSessionCatalogsRef = useRef<() => Promise<void>>(
    () => Promise.resolve(),
  );
  const startMutableControlsRef = useRef<() => void>(() => {});
  const startMutableCatalogsRef = useRef<() => void>(() => {});
  const evaluationSnapshotHydratedRef = useRef<{ full: boolean; sessionId: string }>({
    full: false,
    sessionId: '',
  });
  const rewriteResolveGenerationRef = useRef(0);
  const editTargetSessionIdRef = useRef('');
  const requestedDraftAppliedRef = useRef('');
  selectedIdRef.current = selectedId;
  const {
    draft,
    attachments,
    setSessionDraft,
    setSessionAttachments,
    setSelectedDraft,
    persistSelectedDraft,
    setSelectedAttachments,
    restoreSessionInputIfUntouched,
    mergeSessionAttachments,
    seedSessionInput,
    deleteSessionInput,
  } = useSessionComposerInputs({
    selectedSessionId: selectedId,
    getSelectedSessionId: () => selectedIdRef.current,
  });
  const sending = sendingSessionIds.has(selectedId);
  const contextSnapshotState = contextSnapshot?.sessionId === selectedId
    ? contextSnapshot.state
    : undefined;
  const modelSelection = useModelSelectionController({
    transport,
    selectedSessionId: selectedId,
    setCatalog,
    updateSession: (updated) => {
      setSessions((current) => current.map((item) => (
        item.id === updated.id
          ? { ...item, ...updated, roomParticipant: updated.roomParticipant ?? item.roomParticipant }
          : item
      )));
    },
    setSessionError,
    errorText,
  });
  const modelChanging = modelSelection.changingSessionIds.has(selectedId);
  const stopping = stoppingSessionIds.has(selectedId);
  const rewriteResolving = rewriteResolvingSessionIds.has(selectedId);
  const capabilityPolicyMutation = capabilityPolicyMutations.get(selectedId);
  const capabilityPolicyPending = capabilityPolicyMutation?.status === 'pending';
  useEffect(() => {
    if (!selectedId || sessionMetadataKnown) {
      setMetadataGraceSessionId('');
      return;
    }
    const timer = window.setTimeout(() => {
      setMetadataGraceSessionId(selectedId);
    }, 25);
    return () => window.clearTimeout(timer);
  }, [selectedId, sessionMetadataKnown]);
  const sessionSnapshotMayStart = Boolean(selectedId) && (
    sessionMetadataKnown || metadataGraceSessionId === selectedId
  );
  const loadAgentSnapshot = useAgentLiveSession({
    sessionId: selectedId,
    transport,
    active: sessionSnapshotMayStart,
    live: sessionMetadataKnown && !evaluationSnapshot,
    snapshotView: evaluationSnapshot ? 'full' : 'recent',
    onSnapshot: (snapshot) => {
      if (snapshot.view === 'recent') {
        setContextSnapshot({ sessionId: snapshot.sessionId, state: 'partial' });
      } else {
        evaluationSnapshotHydratedRef.current = {
          full: true,
          sessionId: snapshot.sessionId,
        };
        setContextSnapshot((current) => (
          current?.sessionId === snapshot.sessionId ? undefined : current
        ));
      }
      setCatalogNotice(snapshot.sessionId, 'snapshot', '');
      setSnapshotReadySessionId(snapshot.sessionId);
      startMutableControlsRef.current();
    },
    onSnapshotError: (failure) => {
      if (failure.view === 'recent') {
        setContextSnapshot({ sessionId: failure.sessionId, state: 'partial' });
        setCatalogNotice(
          failure.sessionId,
          'snapshot',
          `最近记录暂时无法恢复。${errorText(failure.error)}`,
        );
      } else {
        setContextSnapshot({ sessionId: failure.sessionId, state: 'partial' });
        setCatalogNotice(
          failure.sessionId,
          'snapshot',
          `对话记录暂时无法恢复。${errorText(failure.error)}`,
        );
      }
      if (failure.recoverable) {
        setSnapshotReadySessionId(failure.sessionId);
        startMutableControlsRef.current();
      }
    },
    onEvent: (event) => {
      sendTimings.observe(event);
      if (event.eventType === 'turn_completed' || event.eventType === 'turn_failed') {
        setSessionStopping(event.sessionId, false);
        void refreshSessionRail();
        const stopWasWaitingForTerminal = stopReconcileEscalatedSessionsRef.current.delete(
          event.sessionId,
        );
        void loadAgentSnapshotRef.current({
          preserveAfterSequence: event.sequence,
        }).then((loaded) => {
          if (selectedIdRef.current !== event.sessionId || !loaded || !stopWasWaitingForTerminal) return;
          if (
            sessionErrorsRef.current.get(event.sessionId)
            === STOP_RECONCILE_ESCALATED_MESSAGE
          ) {
            setSessionError(event.sessionId, '');
          }
          void loadSessionCatalogsRef.current();
        });
      }
      if (event.eventType === 'session_configuration_changed') {
        modelSelection.applyConfigurationEvent(event.sessionId, event.payload);
      }
    },
    onConnectionRestored: (activeSessionId) => {
      setCatalogNotice(activeSessionId, 'live-stream', '');
    },
    onConnectionError: (activeSessionId, streamError) => {
      setCatalogNotice(activeSessionId, 'live-stream', errorText(streamError));
    },
  });
  loadAgentSnapshotRef.current = loadAgentSnapshot;

  function selectSessionId(sessionId: string): void {
    selectedIdRef.current = sessionId;
    setSelectedId(sessionId);
  }

  function visibleSessionError(sessionId: string): string {
    const operationError = sessionErrorsRef.current.get(sessionId) ?? '';
    const notices = [
      ...(catalogNoticesRef.current.get(sessionId)?.values() ?? []),
    ].filter(Boolean).join(' ');
    return [operationError, notices].filter(Boolean).join(' ');
  }

  function refreshVisibleSessionError(sessionId: string): void {
    if (selectedIdRef.current === sessionId) {
      setVisibleError(visibleSessionError(sessionId));
    }
  }

  function setSessionError(sessionId: string, value: string): void {
    if (!sessionId) return;
    if (value) sessionErrorsRef.current.set(sessionId, value);
    else sessionErrorsRef.current.delete(sessionId);
    refreshVisibleSessionError(sessionId);
  }

  function setCatalogNotice(sessionId: string, key: string, value = ''): void {
    if (!sessionId) return;
    const notices = new Map(catalogNoticesRef.current.get(sessionId) ?? []);
    if (value) notices.set(key, value);
    else notices.delete(key);
    if (notices.size > 0) catalogNoticesRef.current.set(sessionId, notices);
    else catalogNoticesRef.current.delete(sessionId);
    refreshVisibleSessionError(sessionId);
  }

  function setError(value: string): void {
    const sessionId = selectedIdRef.current;
    if (sessionId) {
      setSessionError(sessionId, value);
      return;
    }
    setVisibleError(value);
  }

  function beginSessionSend(sessionId: string): boolean {
    if (sessionSendLocksRef.current.has(sessionId)) return false;
    sessionSendLocksRef.current.add(sessionId);
    setSendingSessionIds((current) => new Set(current).add(sessionId));
    return true;
  }
  function beginSessionAction(sessionId: string): boolean {
    if (!sessionId || sessionActionLocksRef.current.has(sessionId)) return false;
    sessionActionLocksRef.current.add(sessionId);
    return true;
  }

  function endSessionAction(sessionId: string): void {
    sessionActionLocksRef.current.delete(sessionId);
  }


  function endSessionSend(sessionId: string): void {
    sessionSendLocksRef.current.delete(sessionId);
    setSendingSessionIds((current) => {
      const next = new Set(current);
      next.delete(sessionId);
      return next;
    });
  }

  function updatePendingSession(
    setter: (value: (current: Set<string>) => Set<string>) => void,
    sessionId: string,
    pending: boolean,
  ): void {
    setter((current) => {
      const next = new Set(current);
      if (pending) next.add(sessionId);
      else next.delete(sessionId);
      return next;
    });
  }

  function setSessionStopping(sessionId: string, pending: boolean): void {
    updatePendingSession(setStoppingSessionIds, sessionId, pending);
  }

  function setSessionRewriteResolving(sessionId: string, pending: boolean): void {
    updatePendingSession(setRewriteResolvingSessionIds, sessionId, pending);
  }

  const ensure = useAgentLiveStore((state) => state.ensure);
  const activeTurnId = useAgentLiveStore((state) => latestActiveTurnId(
    state.projections[selectedId],
  ));
  const pendingMemoryReview = useAgentLiveStore((state) => latestWaitingActivity(
    state.projections[selectedId],
    (activity) => activity.kind === 'user_input_required' && activity.payload.requestKind === 'memory_review',
  ));
  const pendingGenericInput = useAgentLiveStore((state) => latestWaitingActivity(
    state.projections[selectedId],
    (activity) => activity.kind === 'user_input_required' && activity.payload.requestKind !== 'memory_review',
  ));
  const pendingApproval = useAgentLiveStore((state) => latestWaitingActivity(
    state.projections[selectedId],
    (activity) => activity.kind === 'approval_required' && approvalNeedsHumanDecision(activity.payload),
  ));
  const projectPaths = useMemo(() => sessions
    .flatMap((item) => item.workspaceRoots ?? [])
    .filter((path, index, values) => path.startsWith('/') && values.indexOf(path) === index), [sessions]);
  const approvalForReview = pendingApproval ?? requestedApproval;
  const railModal = mobileViewport && railOpen;
  const statusModal = statusOverlayViewport && statusOpen;
  const filesModal = statusOverlayViewport && filesOpen;
  const subagentsModal = statusOverlayViewport && subagentsOpen;
  const sidePanelOpen = statusOpen || filesOpen || subagentsOpen;
  const sidePanelModal = statusModal || filesModal || subagentsModal;

  useEffect(() => {
    if (mobileViewport) setRailOpen(false);
  }, [mobileViewport]);

  useEffect(() => {
    if (statusOverlayViewport) {
      setStatusOpen(false);
      setFilesOpen(false);
    }
  }, [statusOverlayViewport]);

  useEffect(() => {
    setTimelineFollow({ following: true, unseenUpdates: 0 });
  }, [selectedId]);

  useModalPanel({
    active: railModal,
    panelRef: railRef,
    returnFocusRef: railToggleRef,
    onClose: closeMobileRail,
    initialFocusSelector: '[data-drawer-autofocus]',
  });
  useModalPanel({
    active: statusModal,
    panelRef: statusRef,
    returnFocusRef: statusToggleRef,
    onClose: closeStatusPanel,
  });
  useModalPanel({
    active: filesModal,
    panelRef: filesRef,
    returnFocusRef: filesToggleRef,
    onClose: closeFilesPanel,
    initialFocusSelector: '[data-drawer-autofocus]',
  });
  useModalPanel({
    active: subagentsModal,
    panelRef: subagentsRef,
    returnFocusRef: subagentsToggleRef,
    onClose: closeSubagentsPanel,
  });

  const loadSessions = useCallback(async (preferredId = '') => {
    setLoading(true);
    setSessionLoadError('');
    try {
      // Session history is the primary page payload. Persona defaults may need
      // Pi Provider discovery, so do not hold the conversation rail behind
      // that independent catalog request.
      const sessionResponse = await transport.request({
        pathId: 'agent.sessions.list',
        query: { limit: 100, includeArchived: showArchived },
      });
      const nextSessions = sessionItems(sessionResponse);
      const usableSessions = nextSessions;
      const railSessions = usableSessions.filter((item) => !item.roomParticipant);
      setSessions(usableSessions);
      setSessionLoadError('');
      void transport.request({ pathId: 'agent.roles.list' }).then(
        (roleResponse) => {
          const nextRoles = roleItems(roleResponse);
          if (nextRoles.length) setPersonas(nextRoles);
        },
        () => undefined,
      );
      const preferredSessionId = usableSessions.some((item) => item.id === preferredId) ? preferredId : '';
      const backendActiveId = activeSessionId(sessionResponse);
      const activeId = railSessions.some((item) => item.id === backendActiveId) ? backendActiveId : '';
      const meaningful = (item: SessionSummary): boolean => (
        (item.messageCount ?? 0) > 0 || Boolean(item.lastMessagePreview?.trim())
      );
      const meaningfulId = railSessions.find(meaningful)?.id ?? '';
      const activeMeaningfulId = railSessions.find((item) => item.id === activeId && meaningful(item))?.id ?? '';
      setSelectedId((current) => {
        const currentId = railSessions.some((item) => item.id === current) ? current : '';
        const next = preferredSessionId || currentId || activeMeaningfulId || meaningfulId || activeId || railSessions[0]?.id || '';
        selectedIdRef.current = next;
        return next;
      });
      setError('');
    } catch (loadError) {
      setSessionLoadError(errorText(loadError));
    } finally {
      setLoading(false);
    }
  }, [showArchived, transport]);

  const refreshSessionRail = useCallback(async (
    preserveOptimisticPreview = false,
  ): Promise<SessionSummary[]> => {
    try {
      const response = await transport.request({
        pathId: 'agent.sessions.list',
        query: { limit: 100, includeArchived: showArchived },
      });
      const nextSessions = sessionItems(response);
      setSessions((current) => {
        const existingById = new Map(current.map((item) => [item.id, item]));
        return nextSessions.map((item) => {
          const existing = existingById.get(item.id);
          if (!existing) return item;
          const preservePreview = preserveOptimisticPreview && Object.keys(
            agentProjection(item.id).optimisticByClientMessageId,
          ).length > 0;
          return {
            ...existing,
            ...item,
            roomParticipant: item.roomParticipant ?? existing.roomParticipant,
            ...(preservePreview
              ? {
                  messageCount: existing.messageCount,
                  lastMessagePreview: existing.lastMessagePreview,
                  updatedAtMs: existing.updatedAtMs,
                }
              : {}),
          };
        });
      });
      return nextSessions;
    } catch {
      // The transcript event stream remains authoritative for the open
      // conversation. A rail refresh must never replace a completed turn with
      // a page-level error; the next terminal event or explicit reload retries.
      return [];
    }
  }, [showArchived, transport]);

  async function reconcileSessionForAction(
    sessionId: string,
    currentSession?: SessionSummary,
  ): Promise<SessionSummary | undefined> {
    if (!sessionId) return undefined;
    const refreshed = await refreshSessionRail(true);
    const canonical = refreshed.find((item) => item.id === sessionId);
    if (canonical) return canonical;
    // A paged/stale catalog must not strand a Session whose transcript is
    // already open. Keep the current record as the action target when Pi can
    // still be reached through the existing projection.
    if (currentSession) return currentSession;
    try {
      const snapshot = await transport.request({
        pathId: 'agent.session.snapshot',
        params: { sessionId },
        query: { view: 'recent' },
      });
      useAgentLiveStore.getState().hydrate(sessionId, snapshot);
      const fallback = provisionalSessionRecord(sessionId);
      setSessions((current) => current.some((item) => item.id === sessionId)
        ? current
        : [...current, fallback]);
      return fallback;
    } catch {
      return undefined;
    }
  }

  useEffect(() => {
    if (requestedSessionId && requestedSessionId !== selectedIdRef.current) {
      selectSessionId(requestedSessionId);
    }
    void loadSessions(requestedSessionId);
  }, [loadSessions, requestedSessionId]);
  useEffect(() => {
    if (!['open', 'memory'].includes(searchParams.get('tools') ?? '') || !sessionControlsAvailable
      || selectedId !== requestedSessionId || toolCatalogStatus !== 'ready') return;
    setToolPickerQuery(searchParams.get('tools') === 'memory' ? '记忆' : '');
    setToolPickerRequest((value) => value + 1);
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      next.delete('tools');
      return next;
    }, { replace: true });
  }, [requestedSessionId, searchParams, selectedId, sessionControlsAvailable, setSearchParams, toolCatalogStatus]);

  useEffect(() => {
    if (!requestedDraft) {
      requestedDraftAppliedRef.current = '';
      return;
    }
    if (!selectedId || !sessionMetadataKnown || evaluationSnapshot) return;
    const handoffKey = `${selectedId}\u0000${requestedDraft}`;
    if (requestedDraftAppliedRef.current === handoffKey) return;
    restoreSessionInputIfUntouched(selectedId, requestedDraft, []);
    requestedDraftAppliedRef.current = handoffKey;
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      next.delete('draft');
      return next;
    }, { replace: true });
  }, [
    evaluationSnapshot,
    requestedDraft,
    restoreSessionInputIfUntouched,
    selectedId,
    sessionMetadataKnown,
    setSearchParams,
  ]);
  useEffect(() => {
    setVisibleError(visibleSessionError(selectedId));
    setContextSnapshot(undefined);
    // A Session's last Pi-confirmed catalog is safe to render while the
    // background refresh runs. Clearing it here caused a visible dead window
    // every time the user returned to a conversation.
    setCatalog(modelCatalogCacheRef.current.get(selectedId));
    setCommands([]);
    setCapabilityCatalog(undefined);
    setCapabilityCatalogError('');
    setToolCatalogStatus('loading');
    setConversationForkAvailable(false);
    setConversationRewriteAvailable(false);
    setSnapshotReadySessionId('');
    if (editTargetSessionIdRef.current !== selectedId) {
      rewriteResolveGenerationRef.current += 1;
      editTargetSessionIdRef.current = '';
      setEditTarget(undefined);
    }
    setRequestedApproval(undefined);
    setForkDialogNodes([]);
  }, [selectedId]);
  useEffect(() => {
    if (!selectedId) return;
    ensure(selectedId);
    let fullSnapshotRequestId = 0;
    let fullSnapshotAbort: AbortController | undefined;
    let mutableControlsInitialized = false;
    let mutableCatalogsInitialized = false;
    evaluationSnapshotHydratedRef.current = { full: false, sessionId: selectedId };
    let active = true;
    const currentSessionMetadata = (): {
      evaluationSnapshot: boolean;
      known: boolean;
    } => {
      const metadata = sessionMetadataRef.current;
      return metadata.sessionId === selectedId
        ? metadata
        : { evaluationSnapshot: false, known: false };
    };
    const liveSessionControlsAvailable = (): boolean => {
      const metadata = currentSessionMetadata();
      return metadata.known && !metadata.evaluationSnapshot;
    };
    async function loadFullSnapshot(): Promise<void> {
      const requestId = fullSnapshotRequestId + 1;
      fullSnapshotRequestId = requestId;
      fullSnapshotAbort?.abort();
      const abort = new AbortController();
      fullSnapshotAbort = abort;
      const baselineSequence = agentProjection(selectedId).lastSequence;
      setContextSnapshot({ sessionId: selectedId, state: 'restoring' });
      try {
        const fullSnapshot = await transport.request({
          pathId: 'agent.session.snapshot',
          params: { sessionId: selectedId },
          signal: abort.signal,
        });
        if (!active || abort.signal.aborted || requestId !== fullSnapshotRequestId) return;
        const current = agentProjection(selectedId);
        const hasNewerActivity = current.lastSequence !== baselineSequence
          || Boolean(latestActiveTurnId(current))
          || Object.keys(current.optimisticByClientMessageId).length > 0;
        if (hasNewerActivity) {
          setContextSnapshot({ sessionId: selectedId, state: 'partial' });
          return;
        }
        useAgentLiveStore.getState().hydrate(selectedId, fullSnapshot);
        if (currentSessionMetadata().evaluationSnapshot) {
          evaluationSnapshotHydratedRef.current = { full: true, sessionId: selectedId };
        }
        setContextSnapshot((currentSnapshot) => (
          currentSnapshot?.sessionId === selectedId ? undefined : currentSnapshot
        ));
        setSessionError(selectedId, '');
      } catch (loadError) {
        if (!active || abort.signal.aborted || requestId !== fullSnapshotRequestId) return;
        setContextSnapshot({ sessionId: selectedId, state: 'partial' });
        setSessionError(selectedId, `完整记录暂时无法加载。${errorText(loadError)}`);
      } finally {
        if (requestId === fullSnapshotRequestId) fullSnapshotAbort = undefined;
      }
    }
    loadFullSnapshotRef.current = () => { void loadFullSnapshot(); };
    async function loadSessionCatalogs(): Promise<void> {
      if (!liveSessionControlsAvailable()) return;
      setToolCatalogStatus('loading');
      setCapabilityCatalogError('');
      const runtimeRequest = transport.request({ pathId: 'agent.runtime.get' });
      void runtimeRequest.then(
        (value) => {
          if (!active || !liveSessionControlsAvailable()) return;
          const runtimePayload = isRecord(value) ? value : {};
          const runtimeCapabilities = isRecord(runtimePayload.capabilities)
            ? runtimePayload.capabilities
            : {};
          setConversationForkAvailable(runtimeCapabilities.conversationFork === true);
          setConversationRewriteAvailable(runtimeCapabilities.conversationRewrite === true);
        },
        () => {
          if (active && liveSessionControlsAvailable()) {
            setConversationForkAvailable(false);
            setConversationRewriteAvailable(false);
          }
        },
      );
      const publishNotice = (key: string, value = '') => {
        if (!active || !liveSessionControlsAvailable()) return;
        // Catalog warnings have their own Session-local owner. A successful
        // retry clears only its source; it cannot erase a newer Stop, send,
        // approval, permission, or memory-review error (and vice versa).
        setCatalogNotice(selectedId, key, value);
      };
      const modelTask = transport.request({
        pathId: 'agent.session.models',
        params: { sessionId: selectedId },
      }).then((value) => {
        if (!active || !liveSessionControlsAvailable()) return;
        if (!isModelCatalog(value)) {
          throw new Error('model catalog response is invalid');
        }
        modelSelection.acceptConfirmedCatalog(selectedId, value);
        modelCatalogCacheRef.current.set(selectedId, value);
        publishNotice('model');
      }).catch((reason: unknown) => {
        if (!active || !liveSessionControlsAvailable()) return;
        const cachedCatalog = modelCatalogCacheRef.current.get(selectedId);
        if (cachedCatalog) {
          modelSelection.acceptConfirmedCatalog(selectedId, cachedCatalog);
        } else {
          setCatalog(undefined);
        }
        publishNotice(
          'model',
          modelCatalogNotice(reason, Boolean(cachedCatalog)),
        );
      });
      // Provider discovery owns Host startup. Start the Session-specific
      // command inspection only after that independent catalog settles, so a
      // large transcript/context open cannot push the model picker behind the
      // native bridge timeout.
      const commandTask = modelTask.then(() => {
        if (!liveSessionControlsAvailable()) return undefined;
        return transport.request({
          pathId: 'agent.session.commands',
          params: { sessionId: selectedId },
        });
      }).then(
        (value) => {
          if (!active || !liveSessionControlsAvailable() || value === undefined) return;
          setCommands(commandItems(value));
          publishNotice('commands');
        },
        () => {
          if (!active || !liveSessionControlsAvailable()) return;
          setCommands([]);
          publishNotice(
            'commands',
            'Pi 命令暂时不可用，仍可直接发送消息。',
          );
        },
      );
      const toolTask = transport.request({
        pathId: 'agent.tools.list',
        query: { sessionId: selectedId },
      }).then((value) => {
        if (!active || !liveSessionControlsAvailable()) return;
        const nextCapabilityCatalog = requireSessionCapabilityCatalog(value, selectedId);
        setCapabilityCatalog(nextCapabilityCatalog);
        setTools(toolItems(value));
        setToolCatalogStatus('ready');
        publishNotice('tools');
      }).catch((reason: unknown) => {
        if (!active || !liveSessionControlsAvailable()) return;
        const message = errorText(reason);
        setCapabilityCatalog(undefined);
        setCapabilityCatalogError(message);
        setTools([]);
        setToolCatalogStatus('failed');
        publishNotice('tools', `${message} 模型不会获得未核对的工具能力。`);
      });
      await Promise.allSettled([modelTask, commandTask, toolTask]);
    }
    function startMutableCatalogs(): void {
      if (!liveSessionControlsAvailable() || mutableCatalogsInitialized) return;
      mutableCatalogsInitialized = true;
      void loadSessionCatalogs();
    }
    function startMutableControls(): void {
      if (!liveSessionControlsAvailable() || mutableControlsInitialized) return;
      mutableControlsInitialized = true;
      startMutableCatalogs();
    }
    loadSessionCatalogsRef.current = loadSessionCatalogs;
    startMutableControlsRef.current = startMutableControls;
    startMutableCatalogsRef.current = startMutableCatalogs;
    return () => {
      active = false;
      fullSnapshotAbort?.abort();
      loadFullSnapshotRef.current = () => {};
      loadSessionCatalogsRef.current = () => Promise.resolve();
      startMutableControlsRef.current = () => {};
      startMutableCatalogsRef.current = () => {};
      sendTimings.clearSession(selectedId);
    };
  }, [ensure, selectedId, sendTimings, transport]);
  useEffect(() => {
    if (!selectedId || evaluationSnapshot || !sessionMetadataKnown) return;
    startMutableCatalogsRef.current();
    if (snapshotReadySessionId === selectedId) startMutableControlsRef.current();
  }, [evaluationSnapshot, selectedId, sessionMetadataKnown, snapshotReadySessionId]);
  useEffect(() => {
    // A Room task deep link starts snapshot recovery before the Session rail
    // catalog resolves. Fork discovery is therefore a separate optimization:
    // it begins only after ownership is confirmed as a user conversation and
    // never causes the transcript/stream effect to restart.
    if (
      !selectedId
      || !sessionMetadataKnown
      || evaluationSnapshot
      || !isUserConversation
      || snapshotReadySessionId !== selectedId
    ) return;
    const sessionStatus = agentProjection(selectedId).status;
    if (sessionStatus !== 'idle' && sessionStatus !== 'active') return;
    let active = true;
    void transport.request<Record<string, unknown>>({
      pathId: 'agent.session.forks.list',
      params: { sessionId: selectedId },
    }).then(
      (response) => {
        if (active) forkCatalogCacheRef.current.set(selectedId, response);
      },
      () => {
        // Fork discovery is an idle optimization. The explicit edit/branch
        // action still retries and owns any user-visible error.
      },
    );
    return () => { active = false; };
  }, [evaluationSnapshot, isUserConversation, selectedId, sessionMetadataKnown, snapshotReadySessionId, transport]);

  // Persona is optional Package data. Ordinary Sessions remain usable and
  // visually neutral when that Package is absent; legacy role metadata is
  // projected only for Room participant deep links that still own it.
  const persona = isRoomParticipant
    ? personas.find((item) => item.roleId === session?.roleId)
    : undefined;
  const subagentPackageEnabled = commands.some((command) => command.name === 'subagents');
  useEffect(() => {
    if (!subagentPackageEnabled) setSubagentsOpen(false);
  }, [subagentPackageEnabled]);
  useEffect(() => {
    if (!evaluationSnapshot) return;
    setStatusOpen(false);
    setFilesOpen(false);
    setSubagentsOpen(false);
  }, [evaluationSnapshot]);
  const busy = Boolean(activeTurnId);
  const branchBlocked = busy || sending;
  const rewriteBlocked = (
    branchBlocked
    || rewriteResolving
    || !conversationRewriteAvailable
    || !isUserConversation
  );
  const imageSupport = useMemo(() => selectedModelImageSupport(catalog), [catalog]);
  function closeMobileRail(): void {
    setRailOpen(false);
  }

  function closeStatusPanel(): void {
    setStatusOpen(false);
  }

  function closeFilesPanel(): void {
    setFilesOpen(false);
  }

  function closeSubagentsPanel(): void {
    setSubagentsOpen(false);
  }

  function closeSidePanel(): void {
    setStatusOpen(false);
    setFilesOpen(false);
    setSubagentsOpen(false);
  }

  function toggleRail(): void {
    setRailOpen((value) => {
      const next = !value;
      if (next && mobileViewport) closeSidePanel();
      return next;
    });
  }

  function toggleStatus(): void {
    setStatusOpen((value) => {
      const next = !value;
      if (next) {
        setFilesOpen(false);
        setSubagentsOpen(false);
        if (mobileViewport) setRailOpen(false);
      }
      return next;
    });
  }

  function toggleFiles(): void {
    setFilesOpen((value) => {
      const next = !value;
      if (next) {
        setStatusOpen(false);
        setSubagentsOpen(false);
        if (mobileViewport) setRailOpen(false);
      }
      return next;
    });
  }

  function toggleSubagents(): void {
    setSubagentsOpen((value) => {
      const next = !value;
      if (next) {
        setStatusOpen(false);
        setFilesOpen(false);
        if (mobileViewport) setRailOpen(false);
      }
      return next;
    });
  }

  function selectSession(sessionId: string): void {
    selectSessionId(sessionId);
    if (mobileViewport) setRailOpen(false);
  }

  async function archiveSession(sessionId: string, archived: boolean): Promise<void> {
    if (sessions.find((item) => item.id === sessionId)?.evaluationSnapshot === true) return;
    try {
      await transport.request({
        pathId: 'agent.session.archive',
        params: { sessionId },
        body: { archived },
      });
      const currentSelectedId = selectedIdRef.current;
      await loadSessions(currentSelectedId === sessionId && archived && !showArchived ? '' : currentSelectedId);
      setError('');
    } catch (requestError) {
      setError(errorText(requestError));
    }
  }

  async function deleteSession(sessionId: string): Promise<void> {
    if (sessions.find((item) => item.id === sessionId)?.evaluationSnapshot === true) return;
    try {
      await transport.request({ pathId: 'agent.session.delete', params: { sessionId } });
      useAgentLiveStore.getState().clear(sessionId);
      deleteSessionInput(sessionId);
      sessionErrorsRef.current.delete(sessionId);
      catalogNoticesRef.current.delete(sessionId);
      sessionSendLocksRef.current.delete(sessionId);
      forkCatalogCacheRef.current.delete(sessionId);
      setSessions((current) => current.filter((item) => item.id !== sessionId));
      const currentSelectedId = selectedIdRef.current;
      if (currentSelectedId === sessionId) selectSessionId('');
      await loadSessions(currentSelectedId === sessionId ? '' : currentSelectedId);
      setError('');
    } catch (requestError) {
      const message = errorText(requestError);
      setError(message);
      throw new Error(message, { cause: requestError });
    }
  }

  async function createSession(input: NewSessionInput): Promise<boolean> {
    try {
      const toolProfileVersion = input.toolProfileVersion
        ?? (input.executionMode === 'read_only'
          ? 'subagent-readonly-v1'
          : input.executionMode === 'workspace_managed'
            ? 'control-center-v1'
          : input.executionMode === 'full_trust'
            ? 'control-center-auto-approve-v1'
            : 'control-center-full-access-v1');
      const workspaceRoots = isWellPairedExplicitProfile(toolProfileVersion, input.executionMode)
        ? unrestrictedWorkspaceRoots(...input.workspaceRoots)
        : input.workspaceRoots;
      const response = await transport.request<Record<string, unknown>>({
        pathId: 'agent.sessions.create',
        body: {
          title: input.title,
          mode: 'coordinator',
          executionMode: input.executionMode,
          toolProfileVersion,
          workspaceRoots,
          ...(input.executionMode === 'full_trust'
            ? { dangerousModeConfirmation: 'ENABLE_FULL_TRUST' }
            : {}),
          ...(input.workspaceScopeConfirmed
            ? { workspaceScopeConfirmation: 'APPROVE_WORKSPACE_SCOPE' }
            : {}),
        },
      });
      const created = isRecord(response.session) ? response.session as unknown as SessionSummary : undefined;
      if (created?.id) await loadSessions(created.id);
      setError('');
      return true;
    } catch (requestError) {
      setError(errorText(requestError));
      return false;
    }
  }

  async function send(
    requestedDelivery: AgentMessageDelivery = busy ? 'steer' : 'prompt',
    composerDraft = draft,
  ): Promise<void> {
    if (!session || !sessionControlsAvailable || sending || modelChanging) return;
    const sendStartedAt = monotonicNow();
    const delivery: AgentMessageDelivery = busy
      ? (requestedDelivery === 'followUp' ? 'followUp' : 'steer')
      : 'prompt';
    const value = composerDraft.trim();
    if (editTarget) {
      if (!value && attachments.length === 0) return;
      if (editTarget.resolving || !editTarget.entryId) {
        setSessionError(session.id, '正在定位这条历史消息，请稍候。');
        return;
      }
      if (attachments.some((item) => isComposerImageMimeType(item.mimeType)) && imageSupport !== 'supported') {
        setError(imageSupport === 'unsupported'
          ? '当前模型不支持图片，请移除图片附件或切换到支持图片的模型。'
          : '尚未确认当前模型的图片能力，请稍后再发送。');
        return;
      }
      const message = value || '请查看附件。';
      const selectedAttachments = attachments;
      const target = editTarget;
      const clientMessageId = `web-rewrite-${crypto.randomUUID()}`;
      if (!beginSessionSend(session.id)) return;
      sendTimings.begin(session.id, clientMessageId, sendStartedAt);
      useAgentLiveStore.getState().rewriteOptimistic(session.id, target.messageId, {
        clientMessageId,
        text: message,
        attachments: selectedAttachments.map((item) => item.id),
        nowMs: Date.now(),
      });
      sendTimings.optimistic(clientMessageId);
      rewriteResolveGenerationRef.current += 1;
      editTargetSessionIdRef.current = '';
      forkCatalogCacheRef.current.delete(session.id);
      setSessionDraft(session.id, '');
      setSessionAttachments(session.id, []);
      setSessionError(session.id, '');
      setEditTarget(undefined);
      setScrollToLatestRequest((current) => current + 1);

      // Rewind and Provider admission can restore a cold Pi Session. The
      // branch change is already visible above; reconcile the authoritative
      // response in the background instead of freezing the composer for it.
      void (async () => {
        try {
          const response = await transport.request<Record<string, unknown>>({
            pathId: 'agent.session.rewrite',
            params: { sessionId: session.id },
            body: {
              entryId: target.entryId,
              message,
              attachments: selectedAttachments.map((item) => item.id),
              clientMessageId,
            },
          });
          sendTimings.accepted(clientMessageId, response);
        } catch (requestError) {
          sendTimings.failed(clientMessageId);
          try {
            const snapshot = await transport.request({
              pathId: 'agent.session.snapshot',
              params: { sessionId: session.id },
            });
            useAgentLiveStore.getState().discardOptimistic(session.id, clientMessageId);
            useAgentLiveStore.getState().hydrate(session.id, snapshot);
            const accepted = Object.values(agentProjection(session.id).messagesById)
              .some((item) => item.clientMessageId === clientMessageId);
            if (!accepted) {
              const restored = restoreSessionInputIfUntouched(session.id, value, selectedAttachments);
              if (restored && selectedIdRef.current === session.id) {
                editTargetSessionIdRef.current = session.id;
                setEditTarget(target);
              }
              setSessionError(session.id, errorText(requestError));
            }
          } catch {
            useAgentLiveStore.getState().failOptimistic(
              session.id,
              clientMessageId,
              errorText(requestError),
              Date.now(),
              'ambiguous',
            );
            restoreSessionInputIfUntouched(session.id, value, selectedAttachments);
            setSessionError(session.id, '暂时无法确认修改后的消息是否已接收；已保留输入，请刷新对话核对。');
          }
        } finally {
          endSessionSend(session.id);
        }
      })();
      return;
    }
    if (value === '/new') { setSelectedDraft(''); setNewSessionOpen(true); return; }
    if (value === '/resume') { setSelectedDraft(''); setRailOpen(true); return; }
    if (value === '/branch') { setSelectedDraft(''); openForkDialog(); return; }
    if (isCommand(value, '/name')) {
      const title = normalizedSessionTitle(commandArgument(value, '/name'));
      if (!title) {
        setError('请在 /name 后输入新的对话名称。');
        return;
      }
      if (!beginSessionSend(session.id)) return;
      try {
        await transport.request({ pathId: 'agent.session.rename', params: { sessionId: session.id }, body: { title } });
        setSessions((current) => current.map((item) => item.id === session.id ? { ...item, title, updatedAtMs: Date.now() } : item));
        setSessionDraft(session.id, '');
        setSessionError(session.id, '');
      } catch (requestError) { setSessionError(session.id, errorText(requestError)); }
      finally { endSessionSend(session.id); }
      return;
    }
    if (isCommand(value, '/compact')) {
      if (!beginSessionSend(session.id)) return;
      try { await transport.request({ pathId: 'agent.session.compact', params: { sessionId: session.id }, body: { instructions: commandArgument(value, '/compact') } }); setSessionDraft(session.id, ''); }
      catch (requestError) { setSessionError(session.id, errorText(requestError)); }
      finally { endSessionSend(session.id); }
      return;
    }
    if (value === '/model' || value === '/thinking') { setSelectedDraft(''); openModelPicker(); return; }
    if (value === '/permissions') { setSelectedDraft(''); setPermissionPickerRequest((current) => current + 1); return; }
    if (value === '/tools') { setSelectedDraft(''); openToolPicker(); return; }
    if (value === '/status' || value === '/session') { setSelectedDraft(''); setFilesOpen(false); setSubagentsOpen(false); setStatusOpen(true); return; }
    if (value === '/subagents') {
      if (!subagentPackageEnabled) {
        setError('当前 Session 没有安装或启用 Subagent Package。');
        return;
      }
      setSelectedDraft(''); setFilesOpen(false); setStatusOpen(false); setSubagentsOpen(true); return;
    }
    if (value === '/settings') { setSelectedDraft(''); window.location.hash = '/configuration'; return; }
    if (value === '/help' || value === '/hotkeys') { setSelectedDraft(''); setHelpRequest((current) => current + 1); return; }
    if (value === '/stop') { setSelectedDraft(''); await stop(); return; }
    if (!value && attachments.length === 0) return;
    if (value.startsWith('/') && !isAdvertisedPiCommand(value, commands)) {
      setError('这个命令不在当前对话的控制中心或 Pi RPC 命令目录中，未发送给模型。');
      return;
    }
    if (attachments.some((item) => isComposerImageMimeType(item.mimeType)) && imageSupport !== 'supported') {
      setError(imageSupport === 'unsupported'
        ? '当前模型不支持图片，请移除图片附件或切换到支持图片的模型。'
        : '尚未确认当前模型的图片能力，请稍后再发送。');
      return;
    }
    const message = value || '请查看附件。';
    const selectedAttachments = attachments;
    const sessionId = selectedId;
    if (!sessionId || !beginSessionAction(sessionId)) return;
    const actionPreflight = listedSession
      ? undefined
      : reconcileSessionForAction(sessionId, session);
    setSessionDraft(sessionId, '');
    setSessionAttachments(sessionId, []);
    setSessionError(sessionId, '');
    /* Submitting is a claim on the end of the transcript. Without this a reader
       who had scrolled up to check an earlier turn watched their own message
       land off-screen with no sign it was accepted. */
    setScrollToLatestRequest((current) => current + 1);
    promptSession(
      sessionId,
      message,
      selectedAttachments.map((item) => item.id),
      delivery,
      () => {
        restoreSessionInputIfUntouched(sessionId, value, selectedAttachments);
      },
      undefined,
      sendStartedAt,
      '',
      '',
      false,
      actionPreflight,
    );
    endSessionAction(sessionId);
  }

  function promptSession(
    sessionId: string,
    message: string,
    attachmentIds: string[],
    delivery: AgentMessageDelivery = 'prompt',
    restoreInput?: () => void,
    onAdmissionRolledBack?: () => void,
    startedAt = monotonicNow(),
    requestedClientMessageId = '',
    retryOfClientMessageId = '',
    reuseOptimistic = false,
    preflight?: Promise<SessionSummary | undefined>,
  ): boolean {
    if (!beginSessionSend(sessionId)) return false;
    const clientMessageId = (
      requestedClientMessageId
      || `web-${crypto.randomUUID()}`
    );
    sendTimings.begin(sessionId, clientMessageId, startedAt);
    if (reuseOptimistic) {
      useAgentLiveStore.getState().requeueOptimistic(
        sessionId,
        clientMessageId,
        Date.now(),
      );
    } else {
      useAgentLiveStore.getState().appendOptimistic(sessionId, {
        clientMessageId,
        ...(retryOfClientMessageId
          ? { retryOfClientMessageId }
          : {}),
        text: message,
        attachments: attachmentIds,
        nowMs: Date.now(),
        ...(delivery === 'prompt' ? {} : { turnId: activeTurnId, delivery }),
      });
      const optimisticProjection = agentProjection(sessionId);
      const optimisticPreview = message.replace(/\s+/g, ' ').trim().slice(0, 240);
      setSessions((current) => current.map((item) => (
        item.id === sessionId
          ? {
              ...item,
              messageCount: Math.max(
                (item.messageCount ?? 0) + 1,
                optimisticProjection.messageOrder.length,
              ),
              lastMessagePreview: optimisticPreview || item.lastMessagePreview,
              updatedAtMs: Date.now(),
            }
          : item
      )));
    }
    sendTimings.optimistic(clientMessageId);
    setSessionError(sessionId, '');
    const requestBody = {
      message,
      attachments: attachmentIds,
      clientMessageId,
      ...(retryOfClientMessageId
        ? { retryOfClientMessageId }
        : {}),
      ...(delivery === 'prompt' ? {} : { delivery }),
    };
    const requestAdmission = () => (
      transport.request<Record<string, unknown>>({
        pathId: 'agent.session.prompt',
        params: { sessionId },
        body: requestBody,
      })
    );
    const handlePendingAdmission = (requestError: unknown): boolean => {
      if (!isAgentCommandPending(requestError)) return false;
      if (
        !agentProjection(sessionId)
          .optimisticByClientMessageId[clientMessageId]
      ) {
        return true;
      }
      sendTimings.failed(clientMessageId);
      useAgentLiveStore.getState().failOptimistic(
        sessionId,
        clientMessageId,
        publicAgentErrorText(requestError),
        Date.now(),
        isUnresolvedAgentCommandPending(requestError)
          ? 'unresolved'
          : 'pending',
      );
      setSessionError(sessionId, '');
      return true;
    };
    // Admission and the optimistic turn are synchronous. Restoring a Pi
    // Session, refreshing context, or starting a Provider can still make the
    // HTTP receipt slow, but must not make the click itself feel stalled.
    void (async () => {
      try {
        if (preflight) {
          let actionSession: SessionSummary | undefined;
          try {
            actionSession = await preflight;
          } catch {
            actionSession = undefined;
          }
          if (!actionSession) {
            sendTimings.failed(clientMessageId);
            useAgentLiveStore.getState().discardOptimistic(sessionId, clientMessageId);
            restoreInput?.();
            setSessionError(sessionId, '当前对话暂时无法确认，请重新打开后再发送。');
            return;
          }
        }
        const response = await requestAdmission();
        if (isCancelledPromptAdmission(response)) {
          sendTimings.failed(clientMessageId);
          useAgentLiveStore.getState().discardOptimistic(
            sessionId,
            clientMessageId,
          );
          setSessionStopping(sessionId, false);
          setSessionError(sessionId, '');
          void refreshSessionRail();
          return;
        }
        useAgentLiveStore.getState().acknowledgeOptimistic(
          sessionId,
          clientMessageId,
          Date.now(),
        );
        sendTimings.accepted(clientMessageId, response);
      } catch (requestError) {
        if (handlePendingAdmission(requestError)) return;
        sendTimings.failed(clientMessageId);
        if (isAmbiguousAgentPromptFailure(requestError)) {
          useAgentLiveStore.getState().failOptimistic(
            sessionId,
            clientMessageId,
            '暂时无法确认是否已接收。系统不会自动重试；手动重试会核对同一条消息。',
            Date.now(),
            'ambiguous',
          );
          return;
        }
        if (isAgentTurnConflict(requestError)) {
          useAgentLiveStore.getState().discardOptimistic(sessionId, clientMessageId);
          void refreshSessionRail();
          restoreInput?.();
          onAdmissionRolledBack?.();
          setSessionError(sessionId, '上一轮仍在处理，输入已保留；可以继续补充或先停止当前轮。');
          return;
        }
        const failure = publicAgentErrorText(requestError);
        const projection = agentProjection(sessionId);
        const hasOptimisticTurn = Boolean(projection.optimisticByClientMessageId[clientMessageId]);
        useAgentLiveStore.getState().failOptimistic(sessionId, clientMessageId, failure, Date.now());
        void refreshSessionRail();
        restoreInput?.();
        setSessionError(sessionId, hasOptimisticTurn ? '' : failure);
      } finally {
        endSessionSend(sessionId);
      }
    })();
    return true;
  }

  function retryTurn(
    turnId: string,
    onAdmissionRolledBack?: () => void,
  ): boolean {
    const sessionId = selectedId;
    if (!sessionControlsAvailable || !sessionId || sending || latestActiveTurnId(agentProjection(sessionId))) return false;
    if (!beginSessionAction(sessionId)) return false;
    const sendStartedAt = monotonicNow();
    void (async () => {
      try {
        const actionSession = await reconcileSessionForAction(sessionId, session);
        if (!actionSession) {
          setError('当前对话暂时无法确认，请重新打开后再重试。');
          onAdmissionRolledBack?.();
          return;
        }
        let projection = agentProjection(sessionId);
        let userMessage = resolveAgentTurnUserMessage(projection, turnId);
        if (!userMessage) {
          try {
            const snapshot = await transport.request({
              pathId: 'agent.session.snapshot',
              params: { sessionId },
            });
            useAgentLiveStore.getState().hydrate(sessionId, snapshot);
            projection = agentProjection(sessionId);
            userMessage = resolveAgentTurnUserMessage(projection, turnId);
          } catch {
            // Keep the rendered failure available. If the durable fallback is
            // unavailable, the existing explicit input error is more honest
            // than replaying a guessed message.
          }
        }
        if (!userMessage) {
          setError('找不到这轮的原始输入，无法安全重试。');
          onAdmissionRolledBack?.();
          return;
        }
        if (
          userMessage.admissionState === 'pending'
          || userMessage.admissionState === 'unresolved'
        ) {
          setError(
            '这条消息仍无法确认是否已执行；为避免重复执行，不能自动重试。'
            + '请刷新对话检查结果后，再决定是否发送新的请求。',
          );
          onAdmissionRolledBack?.();
          return;
        }
        if (latestActiveTurnId(projection)) {
          onAdmissionRolledBack?.();
          return;
        }
        const message = userMessage.blocks
          .map((block) => (
            typeof block.data.text === 'string'
              ? block.data.text
              : typeof block.data.markdown === 'string'
                ? block.data.markdown
                : ''
          ))
          .filter(Boolean)
          .join('\n')
          .trim();
        if (!message && !userMessage.attachments.length) {
          setError('找不到这轮的原始输入，无法安全重试。');
          onAdmissionRolledBack?.();
          return;
        }
        const replayAmbiguousAdmission = (
          userMessage.admissionState === 'ambiguous'
          && Boolean(userMessage.clientMessageId)
        );
        const originalDelivery = agentMessageDelivery(userMessage);
        const mayRetryFailedReceipt = Boolean(
          userMessage.id.startsWith('local:')
          && userMessage.clientMessageId,
        );
        const submitted = promptSession(
          actionSession.id,
          message || '请查看附件。',
          userMessage.attachments,
          replayAmbiguousAdmission ? originalDelivery : 'prompt',
          undefined,
          onAdmissionRolledBack,
          sendStartedAt,
          replayAmbiguousAdmission
            ? userMessage.clientMessageId
            : '',
          replayAmbiguousAdmission
            ? ''
            : mayRetryFailedReceipt && originalDelivery === 'prompt'
              ? userMessage.clientMessageId ?? ''
              : '',
          replayAmbiguousAdmission,
        );
        if (!submitted) onAdmissionRolledBack?.();
      } finally {
        endSessionAction(sessionId);
      }
    })();
    return true;
  }

  function continueTurn(turnId: string): boolean {
    if (!sessionControlsAvailable || !session || sending || latestActiveTurnId(agentProjection(session.id))) return false;
    const projection = agentProjection(session.id);
    if (projection.turnOrder.at(-1) !== turnId || projection.turnsById[turnId]?.status !== 'failed') {
      setError('只能从当前最新的中断处继续。');
      return false;
    }
    return promptSession(
      session.id,
      '继续完成上一轮。请基于当前 Session 已保留的工具结果和文件生成最终回复，不要重复已经完成的操作。',
      [],
      'prompt',
      undefined,
      undefined,
      monotonicNow(),
    );
  }

  function openModelPicker(): void {
    if (!sessionControlsAvailable) return;
    if (!catalog) {
      setError('模型目录暂时不可用，无法切换模型。');
      return;
    }
    setModelPickerRequest((current) => current + 1);
  }

  function openToolPicker(): void {
    setToolPickerQuery('');
    if (!sessionControlsAvailable) return;
    if (toolCatalogStatus !== 'ready') {
      setError('工具目录暂时不可用。');
      return;
    }
    const hasAvailableTool = tools.some((tool) => (
      tool.availability === 'online'
      && session
      && tool.sessionModes.includes(session.mode)
      && tool.enabled !== false
    ));
    if (!hasAvailableTool) {
      setError('当前权限模式没有可用工具。');
      return;
    }
    setToolPickerRequest((current) => current + 1);
  }

  function runProductCommand(command: AgentProductCommandName): void {
    if ((busy || sending) && command !== 'resume' && command !== 'session' && command !== 'status' && command !== 'subagents' && command !== 'stop') return;
    switch (command) {
      case 'new':
        setSelectedDraft('');
        setNewSessionOpen(true);
        break;
      case 'resume':
        setSelectedDraft('');
        setRailOpen(true);
        break;
      case 'branch':
        setSelectedDraft('');
        openForkDialog();
        break;
      case 'model':
        openModelPicker();
        break;
      case 'thinking':
        setThinkingPickerRequest((current) => current + 1);
        break;
      case 'tools':
        openToolPicker();
        break;
      case 'permissions':
        setPermissionPickerRequest((current) => current + 1);
        break;
      case 'session':
      case 'status':
        setFilesOpen(false);
        setSubagentsOpen(false);
        setStatusOpen(true);
        break;
      case 'subagents':
        setFilesOpen(false);
        setStatusOpen(false);
        setSubagentsOpen(true);
        break;
      case 'settings':
        setSelectedDraft('');
        window.location.hash = '/configuration';
        break;
      case 'stop':
        if (busy) void stop();
        break;
      case 'help':
      case 'hotkeys':
        setHelpRequest((current) => current + 1);
        break;
      case 'name':
      case 'compact':
        // These commands are inserted into the composer so their optional or
        // required argument stays editable before the API call.
        break;
    }
  }

  function openForkDialog(initialEntryId = ''): void {
    if (!sessionControlsAvailable || !session) return;
    setForkDialogNodes(conversationNodesForSession(session.id));
    setForkDialogInitialEntryId(initialEntryId);
    setForkDialogOpen(true);
  }

  async function beginEditMessage(messageId = ''): Promise<void> {
    if (!sessionControlsAvailable || !session || rewriteBlocked) {
      setError(isRoomParticipant
        ? '这段对话属于 Room participant，历史修改由 Room 管理。'
        : conversationRewriteAvailable
          ? '请等待当前回复结束后再修改历史消息。'
          : '当前 Pi Runtime 尚未提供原位修改能力。');
      return;
    }
    const projection = agentProjection(session.id);
    const message = messageId
      ? projection.messagesById[messageId]
      : [...projection.messageOrder]
        .reverse()
        .map((id) => projection.messagesById[id])
        .find((item) => item?.role === 'user' && !item.id.startsWith('local:'));
    if (!message || message.role !== 'user' || message.id.startsWith('local:')) {
      setError('当前对话里没有可修改的上一条用户消息。');
      return;
    }
    const text = message.blocks
      .map((block) => typeof block.data.text === 'string' ? block.data.text : '')
      .filter(Boolean)
      .join('\n')
      .trim();
    if (!text && message.attachments.length === 0) {
      setError('这条消息没有可编辑的公开内容。');
      return;
    }
    const nodes = conversationNodesForSession(session.id);
    const attachments = message.attachments.map((id, index) => ({
      id,
      name: `原附件 ${index + 1}`,
      mimeType: '',
      byteSize: 0,
      source: 'path' as const,
    }));
    const generation = rewriteResolveGenerationRef.current + 1;
    rewriteResolveGenerationRef.current = generation;
    editTargetSessionIdRef.current = session.id;
    setEditTarget({ entryId: '', messageId: message.id, resolving: true });
    setSessionDraft(session.id, text);
    setSessionAttachments(session.id, attachments);
    setTimelineJumpRequest({ messageId: message.id, requestId: Date.now() });
    setError('');

    const cached = forkCatalogCacheRef.current.get(session.id);
    const cachedEntryId = cached
      ? resolveConversationEntryId(cached, nodes, message.id)
      : '';
    if (cachedEntryId) {
      setEditTarget({ entryId: cachedEntryId, messageId: message.id });
      return;
    }

    setSessionRewriteResolving(session.id, true);
    try {
      const response = await transport.request<Record<string, unknown>>({
        pathId: 'agent.session.forks.list',
        params: { sessionId: session.id },
      });
      if (
        selectedIdRef.current !== session.id
        || rewriteResolveGenerationRef.current !== generation
      ) return;
      forkCatalogCacheRef.current.set(session.id, response);
      const entryId = resolveConversationEntryId(response, nodes, message.id);
      if (!entryId) throw new Error('Pi 没有返回这条公开消息对应的可回溯锚点。');
      setEditTarget({ entryId, messageId: message.id });
    } catch (requestError) {
      if (rewriteResolveGenerationRef.current !== generation) return;
      editTargetSessionIdRef.current = '';
      setEditTarget(undefined);
      setSessionError(session.id, publicAgentErrorText(requestError, '暂时无法定位这条历史消息。'));
    } finally {
      if (rewriteResolveGenerationRef.current === generation) {
        setSessionRewriteResolving(session.id, false);
      }
    }
  }

  function cancelEdit(): void {
    rewriteResolveGenerationRef.current += 1;
    editTargetSessionIdRef.current = '';
    if (session) setSessionRewriteResolving(session.id, false);
    setEditTarget(undefined);
    setSelectedDraft('');
    setSelectedAttachments([]);
  }

  function jumpToMessage(messageId: string): void {
    setTimelineJumpRequest({ messageId, requestId: Date.now() });
  }

  async function acceptFork(created: SessionSummary, selectedText: string): Promise<void> {
    setSessions((current) => [created, ...current.filter((item) => item.id !== created.id)]);
    seedSessionInput(created.id, selectedText, []);
    sessionErrorsRef.current.set(created.id, '');
    selectSessionId(created.id);
    if (mobileViewport) setRailOpen(false);
  }

  async function stop(): Promise<void> {
    if (!sessionControlsAvailable || !session || stopping) return;
    const sessionId = session.id;
    const stopStartedAt = monotonicNow();
    const stopDeadlineAt = stopStartedAt + STOP_RECONCILE_BUDGET_MS;
    stopReconcileEscalatedSessionsRef.current.delete(sessionId);
    setSessionStopping(sessionId, true);
    try {
      const abortResult = await settleBeforeDeadline(
        transport.request<Record<string, unknown>>({
          pathId: 'agent.session.abort',
          params: { sessionId },
          body: {},
        }),
        stopDeadlineAt,
      );
      if (abortResult.kind === 'timeout') {
        stopReconcileEscalatedSessionsRef.current.add(sessionId);
        setSessionStopping(sessionId, false);
        setSessionError(sessionId, STOP_RECONCILE_ESCALATED_MESSAGE);
        return;
      }
      if (abortResult.kind === 'rejected') throw abortResult.error;
      const abortReceipt = abortResult.value;
      if (abortCancelledPendingAdmission(abortReceipt)) {
        discardSessionOptimisticMessages(sessionId);
        sendTimings.clearSession(sessionId);
        setSessionStopping(sessionId, false);
        setSessionError(sessionId, '');
        return;
      }
      // Abort ACK is the synchronous UI boundary. Pi's terminal SSE settles
      // the turn and triggers quiet recent reconciliation in the stream
      // handler; a full archive request never sits on the Stop path.
      stopReconcileEscalatedSessionsRef.current.add(sessionId);
      setSessionError(sessionId, '');
    } catch (requestError) {
      stopReconcileEscalatedSessionsRef.current.delete(sessionId);
      setSessionStopping(sessionId, false);
      setSessionError(sessionId, errorText(requestError));
    }
  }

  async function pasteImages(files?: File[]): Promise<boolean> {
    if (!sessionControlsAvailable || !session) { setError('请先选择一个对话。'); return false; }
    if (!transport.pasteImages) { setError('当前平台暂不支持从剪贴板导入附件。'); return false; }
    const remaining = 8 - attachments.length;
    if (remaining <= 0) { setError('单次消息最多支持 8 个附件。'); return false; }
    if (files && files.length > remaining) { setError(`当前消息还可以粘贴 ${remaining} 个附件。`); return false; }
    const oversized = files?.find((file) => file.size <= 0 || file.size > MAX_AGENT_IMAGE_BYTES);
    if (oversized) { setError(`${oversized.name || '附件'} 必须小于 20 MiB 且不能为空。`); return false; }
    try {
      const maxFiles = files?.length || remaining;
      const imported = await transport.pasteImages({
        sessionId: session.id,
        ...(files?.length ? { files } : {}),
        maxFiles,
      });
      if (!imported.length) {
        setSessionError(session.id, '剪贴板里没有可导入的文件。');
        return false;
      }
      const attachmentsWithPreviews = files && transport.kind !== 'native'
        ? imported.map((attachment, index) => {
          const file = files[index];
          return file
            && file.name === attachment.name
            && file.type.toLowerCase() === attachment.mimeType.toLowerCase()
            && file.size === attachment.byteSize
            ? { ...attachment, previewFile: file }
            : attachment;
        })
        : imported;
      mergeSessionAttachments(session.id, attachmentsWithPreviews, 'clipboard');
      setSessionError(session.id, '');
      return true;
    } catch (pasteError) { setSessionError(session.id, errorText(pasteError)); return false; }
  }

  async function pickAttachments(): Promise<void> {
    if (!sessionControlsAvailable || !session) { setError('请先选择一个对话。'); return; }
    if (!transport.pickFiles) { setError('当前平台暂不支持选择附件。'); return; }
    const remaining = 8 - attachments.length;
    if (remaining <= 0) { setError('单次消息最多支持 8 个附件。'); return; }
    try {
      const imported = await transport.pickFiles({
        multiple: true,
        purpose: 'attachment',
        sessionId: session.id,
        maxFiles: remaining,
      });
      if (!imported.length) return;
      mergeSessionAttachments(session.id, imported, 'picker');
      setSessionError(session.id, '');
    } catch (pickError) { setSessionError(session.id, errorText(pickError)); }
  }
  function chooseTool(tool: ToolManifest): void {
    if (!sessionControlsAvailable) return;
    const intent = toolIntentPrompt(tool.id, tool.displayName);
    setSelectedDraft((current) => current.trim() ? `${current.trimEnd()}\n${intent}：` : `${intent}：`);
  }

  async function changeCapabilityPreference(
    canonicalId: string,
    preference: CapabilityPreference,
    catalogSnapshot: CapabilityCatalog | undefined = capabilityCatalog,
  ): Promise<void> {
    if (!sessionControlsAvailable || !session || !catalogSnapshot?.sessionPolicy) return;
    const ownerSessionId = session.id;
    const setMutation = (outcome: CapabilityMutationOutcome) => {
      setCapabilityPolicyMutations((current) => {
        const next = new Map(current);
        next.set(ownerSessionId, outcome);
        return next;
      });
    };
    if (busy || stopping) {
      setMutation({
        canonicalId,
        preference,
        status: 'failed',
        message: '当前任务仍在运行；只有空闲对话才能修改这项策略。运行中的任务不会被停止或隐藏。',
      });
      return;
    }
    const currentSessionPreferences = catalogSnapshot.sessionPolicy.disclosurePreferences.session;
    setMutation({
      canonicalId,
      preference,
      status: 'pending',
      message: '正在等待后端确认并重新读取有效能力。',
    });
    try {
      await transport.request({
        pathId: 'agent.session.capability-policy.update',
        params: { sessionId: ownerSessionId },
        body: {
          capabilityDisclosurePreferences: {
            ...currentSessionPreferences,
            [canonicalId]: preference,
          },
        },
      });
      const refreshed = await transport.request({
        pathId: 'agent.tools.list',
        query: { sessionId: ownerSessionId },
      });
      const nextCapabilityCatalog = requireSessionCapabilityCatalog(refreshed, ownerSessionId);
      const updatedItem = nextCapabilityCatalog.items.find((item) => item.canonicalId === canonicalId);
      if (!updatedItem) {
        throw new Error('能力设置已提交，但后端目录不再包含这项能力。');
      }
      if (selectedIdRef.current === ownerSessionId) {
        setCapabilityCatalog(nextCapabilityCatalog);
        setTools(toolItems(refreshed));
        setToolCatalogStatus('ready');
        setCapabilityCatalogError('');
      }
      setMutation({
        canonicalId,
        preference,
        status: 'succeeded',
        message: `后端已确认${updatedItem.disclosure.effective === 'enabled' ? '披露' : '隐藏'}；生效来源为${capabilityScopeLabel(updatedItem.effectiveScope)}。执行授权未由这次设置更改。`,
      });
    } catch (requestError) {
      setMutation({
        canonicalId,
        preference,
        status: 'failed',
        message: `当前对话的能力没有更新。${errorText(requestError)}`,
      });
    }
  }

  async function retryCapabilityPreference(): Promise<void> {
    if (!sessionControlsAvailable || !capabilityPolicyMutation) return;
    const ownerSessionId = selectedIdRef.current;
    const retryMutation = capabilityPolicyMutation;
    setCapabilityPolicyMutations((current) => {
      const next = new Map(current);
      next.set(ownerSessionId, {
        ...retryMutation,
        status: 'pending',
        message: '正在重新读取当前对话策略，再重试这项调整。',
      });
      return next;
    });
    try {
      const response = await transport.request({
        pathId: 'agent.tools.list',
        query: { sessionId: ownerSessionId },
      });
      const refreshedCatalog = requireSessionCapabilityCatalog(response, ownerSessionId);
      if (selectedIdRef.current !== ownerSessionId) {
        setCapabilityPolicyMutations((current) => {
          const next = new Map(current);
          next.set(ownerSessionId, {
            ...retryMutation,
            status: 'failed',
            message: '重试期间切换了当前对话；未向新对话发送原来的设置。',
          });
          return next;
        });
        return;
      }
      setCapabilityCatalog(refreshedCatalog);
      setTools(toolItems(response));
      setToolCatalogStatus('ready');
      setCapabilityCatalogError('');
      await changeCapabilityPreference(
        retryMutation.canonicalId,
        retryMutation.preference,
        refreshedCatalog,
      );
    } catch (retryError) {
      setCapabilityPolicyMutations((current) => {
        const next = new Map(current);
        next.set(ownerSessionId, {
          ...retryMutation,
          status: 'failed',
          message: `当前对话的能力没有更新。${errorText(retryError)}`,
        });
        return next;
      });
    }
  }

  async function retryCapabilityCatalog(): Promise<void> {
    const ownerSessionId = selectedIdRef.current;
    if (!sessionControlsAvailable || !ownerSessionId) return;
    setToolCatalogStatus('loading');
    try {
      const response = await transport.request({
        pathId: 'agent.tools.list',
        query: { sessionId: ownerSessionId },
      });
      const nextCapabilityCatalog = requireSessionCapabilityCatalog(response, ownerSessionId);
      if (selectedIdRef.current !== ownerSessionId) return;
      setCapabilityCatalog(nextCapabilityCatalog);
      setTools(toolItems(response));
      setToolCatalogStatus('ready');
      setCapabilityCatalogError('');
      setCatalogNotice(ownerSessionId, 'tools');
    } catch (catalogError) {
      if (selectedIdRef.current !== ownerSessionId) return;
      const message = errorText(catalogError);
      setCapabilityCatalog(undefined);
      setCapabilityCatalogError(message);
      setTools([]);
      setToolCatalogStatus('failed');
      setCatalogNotice(ownerSessionId, 'tools', `${message} 模型不会获得未核对的工具能力。`);
    }
  }

  async function reconcileUnconfirmedRuntimePolicy(sessionId: string): Promise<void> {
    // Runtime policy is owned by the backend Session receipt. A success flag
    // without that receipt must never become a locally inferred grant.
    await loadSessions(sessionId);
    if (selectedIdRef.current === sessionId) await retryCapabilityCatalog();
    setSessionError(sessionId, '权限更新未返回确认结果，已重新读取对话状态。');
  }

  async function changePermission(selection: AgentPermissionSelection): Promise<void> {
    if (!sessionControlsAvailable || !session) return;
    if (busy || stopping) {
      setSessionError(session.id, '请先结束或停止当前任务，再调整运行权限。');
      return;
    }
    const currentProfile = session.toolProfileVersion ?? 'control-center-v1';
    const currentExecutionMode = session.executionMode
      ?? (currentProfile === 'control-center-auto-approve-v1'
        ? 'full_trust'
        : currentProfile === 'subagent-readonly-v1'
          ? 'read_only'
          : 'per_action');
    if (
      selection.mode === session.mode
      && selection.toolProfileVersion === currentProfile
      && selection.executionMode === currentExecutionMode
      && session.toolAllowlistMode !== 'explicit'
    ) return;
    try {
      const workspaceRoots = selection.mode === 'coordinator'
        ? isWellPairedExplicitProfile(selection.toolProfileVersion, selection.executionMode)
          ? unrestrictedWorkspaceRoots(...(selection.workspaceRoots ?? session.workspaceRoots))
          : (selection.workspaceRoots ?? session.workspaceRoots)
        : [];
      const response = await transport.request<Record<string, unknown>>({
        pathId: 'agent.session.mode.update',
        params: { sessionId: session.id },
        body: {
          mode: selection.mode,
          executionMode: selection.executionMode,
          workspaceRoots,
          toolProfileVersion: selection.toolProfileVersion,
          toolAllowlistMode: 'profile',
          ...(selection.dangerousModeConfirmed
            ? { dangerousModeConfirmation: 'ENABLE_FULL_TRUST' }
            : {}),
          ...(selection.workspaceScopeConfirmed
            ? { workspaceScopeConfirmation: 'APPROVE_WORKSPACE_SCOPE' }
            : {}),
        },
      });
      if (!isRecord(response.session)) {
        await reconcileUnconfirmedRuntimePolicy(session.id);
        return;
      }
      const updated = response.session as unknown as SessionSummary;
      setSessions((current) => current.map((item) => item.id === session.id ? updated : item));
      if (selectedIdRef.current !== session.id) return;
      setToolCatalogStatus('loading');
      try {
        const toolResponse = await transport.request({ pathId: 'agent.tools.list', query: { sessionId: session.id } });
        const nextCapabilityCatalog = requireSessionCapabilityCatalog(toolResponse, session.id);
        if (selectedIdRef.current !== session.id) return;
        setCapabilityCatalog(nextCapabilityCatalog);
        setTools(toolItems(toolResponse));
        setToolCatalogStatus('ready');
        setCapabilityCatalogError('');
        setSessionError(session.id, '');
      } catch (catalogError) {
        if (selectedIdRef.current !== session.id) return;
        setTools([]);
        setCapabilityCatalog(undefined);
        setToolCatalogStatus('failed');
        setCapabilityCatalogError(errorText(catalogError));
        setSessionError(session.id, `权限已更新，但工具目录刷新失败。${errorText(catalogError)}`);
      }
    } catch (requestError) { setSessionError(session.id, errorText(requestError)); }
  }

  async function pickWorkspaceRoots(
    single = false,
    ownerSessionId = selectedIdRef.current,
  ): Promise<string[] | null> {
    if (!transport.pickFiles) {
      setSessionError(ownerSessionId, '当前平台不能选择本地工作区；请在桌面控制中心中配置运行协调权限。');
      return null;
    }
    try {
      const picked = await transport.pickFiles({
        purpose: 'workspace-root',
        selection: 'directory',
        multiple: !single,
        maxFiles: single ? 1 : 4,
      });
      const roots = picked
        .map((item) => item.path?.trim() ?? '')
        .filter((path, index, values) => path.startsWith('/') && values.indexOf(path) === index);
      if (!roots.length) return null;
      return roots;
    } catch (pickError) {
      setSessionError(ownerSessionId, `工作区选择失败。${errorText(pickError)}`);
      return null;
    }
  }

  async function manageWorkspaceRoots(): Promise<void> {
    if (!sessionControlsAvailable || !session) return;
    const selectedRoots = await pickWorkspaceRoots(false, session.id);
    if (selectedRoots === null) return;
    const toolProfileVersion = session.toolProfileVersion ?? 'control-center-v1';
    const executionMode = session.executionMode
      ?? (toolProfileVersion === 'control-center-auto-approve-v1'
        ? 'full_trust'
        : toolProfileVersion === 'subagent-readonly-v1'
          ? 'read_only'
          : 'per_action');
    const unrestricted = isWellPairedExplicitProfile(toolProfileVersion, executionMode);
    const workspaceRoots = unrestricted
      ? unrestrictedWorkspaceRoots(...selectedRoots)
      : selectedRoots;
    const toolAllowlistMode = unrestricted
      ? 'profile'
      : session.toolAllowlistMode ?? 'profile';
    try {
      const response = await transport.request<Record<string, unknown>>({
        pathId: 'agent.session.mode.update',
        params: { sessionId: session.id },
        body: {
          mode: 'coordinator',
          executionMode,
          workspaceRoots,
          toolProfileVersion,
          toolAllowlistMode,
          ...(toolAllowlistMode === 'explicit'
            ? { allowedTools: session.allowedTools ?? [] }
            : {}),
          ...(executionMode === 'workspace_managed'
            ? { workspaceScopeConfirmation: 'APPROVE_WORKSPACE_SCOPE' }
            : {}),
          ...(executionMode === 'full_trust'
            ? { dangerousModeConfirmation: 'ENABLE_FULL_TRUST' }
            : {}),
        },
      });
      if (!isRecord(response.session)) {
        await reconcileUnconfirmedRuntimePolicy(session.id);
        return;
      }
      const updated = response.session as unknown as SessionSummary;
      setSessions((current) => current.map((item) => item.id === session.id ? updated : item));
      if (selectedIdRef.current !== session.id) return;
      const toolResponse = await transport.request({ pathId: 'agent.tools.list', query: { sessionId: session.id } });
      if (selectedIdRef.current !== session.id) return;
      const nextCapabilityCatalog = requireSessionCapabilityCatalog(toolResponse, session.id);
      setCapabilityCatalog(nextCapabilityCatalog);
      setTools(toolItems(toolResponse));
      setToolCatalogStatus('ready');
      setCapabilityCatalogError('');
      setSessionError(session.id, '');
    } catch (requestError) {
      setSessionError(session.id, `工作区权限没有更新。${errorText(requestError)}`);
    }
  }

  function changeModel(provider: string, modelId: string, level: ThinkingLevel): void {
    if (!sessionControlsAvailable || !session || !catalog) return;
    const targetModel = catalog.providers.find((item) => item.id === provider)?.models.find((item) => item.id === modelId);
    if (!targetModel) { setError('Pi 模型目录中没有这个模型。'); return; }
    if (attachments.some((item) => isComposerImageMimeType(item.mimeType)) && !targetModel.supportsImages) {
      setError('当前消息含有图片，请先移除图片再切换到不支持图片的模型。');
      return;
    }
    modelSelection.select(session.id, catalog, { provider, modelId, level });
  }

  async function decideApproval(approvalId: string, decision: 'approved' | 'rejected', payloadSha256: string): Promise<void> {
    const ownerSessionId = selectedIdRef.current;
    if (!sessionControlsAvailable) return;
    try {
      await transport.request({
        pathId: 'agent.approval.decide',
        params: { approvalId },
        body: { decision: decision === 'approved' ? 'approve' : 'reject', payloadSha256 },
      });
      if (selectedIdRef.current === ownerSessionId) setRequestedApproval(undefined);
      setSessionError(ownerSessionId, '');
    }
    catch (requestError) {
      setSessionError(ownerSessionId, errorText(requestError));
      throw requestError;
    }
  }

  return (
    <main className="agent-feature" data-paw-agent-workbench={pawOsWorkbench || undefined} data-route-id="agent" data-rail-open={railOpen} data-status-open={sidePanelOpen} data-side-panel={subagentsOpen ? 'subagents' : filesOpen ? 'files' : statusOpen ? 'status' : 'none'}>
      <h1 className="agent-feature__title">Agent 任务中心</h1>
      <SessionRail ref={railRef} sessions={visibleRailSessions} selectedId={selectedId} loading={loading} error={sessionLoadError} open={railOpen} modal={railModal} blocked={sidePanelModal || newSessionOpen} showArchived={showArchived} onSelect={selectSession} onCreate={() => { if (mobileViewport) setRailOpen(false); setNewSessionOpen(true); }} onShowArchivedChange={setShowArchived} onArchive={(sessionId, archived) => void archiveSession(sessionId, archived)} onDelete={deleteSession} onOpenWindow={pawOsDesktop ? (targetSession) => pawOsDesktop.openWindow({ appId: 'agent', target: { kind: 'session', id: targetSession.id, title: targetSession.title, subtitle: `${sessionProjectName(targetSession)} · ${sessionPermissionLabel(targetSession)}` } }) : undefined} onRetry={() => void loadSessions(selectedIdRef.current || requestedSessionId)} onClose={closeMobileRail} />
      <AgentPaneResizer side="rail" />
      <button className="agent-rail-backdrop" aria-hidden="true" disabled={!railModal} tabIndex={-1} onClick={closeMobileRail} type="button" />
      <section
        ref={conversationRef}
        className="agent-conversation"
        aria-hidden={railModal || sidePanelModal || undefined}
        inert={railModal || sidePanelModal ? true : undefined}
      >
        <header className="agent-conversation__header">
          <IconButton ref={railToggleRef} className="agent-rail-toggle" label={railOpen ? '收起对话列表' : '展开对话列表'} icon={railOpen ? <PanelLeftClose size={16} /> : <PanelLeftOpen size={16} />} onClick={toggleRail} tooltip />
          <span>
            <strong>{session?.title ?? identity.assistantName}</strong>
            <small>
              {evaluationSnapshot
                ? '评测快照 · 只读证据'
                : sessionMetadataKnown && session
                  ? `${sessionProjectName(session)} · 本地 · ${sessionPermissionLabel(session)}`
                  : '正在确认对话元数据'}
              {contextSnapshotState ? (
                <span className="agent-context-snapshot-state" data-state={contextSnapshotState}>
                  {contextSnapshotState === 'restoring'
                    ? '正在加载完整记录'
                    : '最近上下文'}
                </span>
              ) : null}
            </small>
          </span>
          {error ? <p role="alert" title={error}><AlertCircle size={14} /><span>{error}</span></p> : null}
          <div className="agent-conversation__actions">
            {sessionControlsAvailable && contextSnapshotState ? <IconButton
              label="加载完整记录"
              icon={contextSnapshotState === 'restoring'
                ? <LoaderCircle className="ui-spin" size={16} />
                : <History size={16} />}
              disabled={contextSnapshotState === 'restoring'}
              onClick={() => loadFullSnapshotRef.current()}
              tooltip
            /> : null}
            {sessionControlsAvailable ? <IconButton label="查看对话路径与分支" icon={<GitBranch size={16} />} onClick={() => openForkDialog()} tooltip /> : null}
            {sessionControlsAvailable && subagentPackageEnabled ? <IconButton ref={subagentsToggleRef} className="agent-subagents-toggle" aria-controls="agent-subagent-panel" aria-expanded={subagentsOpen} label={subagentsOpen ? '收起子 Agent 工作台' : '打开子 Agent 工作台'} icon={<Network size={16} />} onClick={toggleSubagents} tooltip /> : null}
            {sessionControlsAvailable ? <IconButton ref={filesToggleRef} className="agent-files-toggle" aria-controls="agent-files-panel" aria-expanded={filesOpen} label={filesOpen ? '收起文件目录' : '展开文件目录'} icon={<FolderTree size={16} />} onClick={toggleFiles} tooltip /> : null}
            {sessionControlsAvailable ? <IconButton ref={statusToggleRef} className="agent-status-toggle" aria-controls="agent-status-panel" aria-expanded={statusOpen} label={statusOpen ? '收起任务中心' : '展开任务中心'} icon={statusOpen ? <PanelRightClose size={16} /> : <PanelRightOpen size={16} />} onClick={toggleStatus} tooltip /> : null}
          </div>
        </header>
        {selectedId ? (
          <AgentTimeline
            assistantName={identity.assistantName}
            sessionId={selectedId}
            persona={persona}
            loading={loading && !session}
            modelSelectionAvailable={sessionControlsAvailable && Boolean(catalog)}
            turnRecoveryDisabled={!sessionControlsAvailable || busy || sending || stopping || modelChanging}
            forkAvailable={sessionControlsAvailable && conversationForkAvailable && !branchBlocked && isUserConversation}
            rewriteAvailable={sessionControlsAvailable && !rewriteBlocked}
            jumpRequest={timelineJumpRequest}
            scrollToLatestRequest={scrollToLatestRequest}
            onFollowStateChange={setTimelineFollow}
            onForkFromMessage={openForkDialog}
            onEditMessage={(messageId) => void beginEditMessage(messageId)}
            onRetryTurn={retryTurn}
            onContinueTurn={continueTurn}
            onSwitchModel={openModelPicker}
            onApprovalDecision={(id, decision, hash) => { void decideApproval(id, decision, hash).catch(() => {}); }}
            onOpenApproval={sessionControlsAvailable ? setRequestedApproval : undefined}
            onRequestPermission={sessionControlsAvailable
              ? () => setPermissionPickerRequest((current) => current + 1)
              : undefined}
          />
        ) : null}
        {session && sessionControlsAvailable ? (
          <div className="agent-composer-dock">
            {pendingGenericInput && !pendingApproval && !pendingMemoryReview ? (
              <GenericUserInputCard
                activity={pendingGenericInput}
                sessionId={selectedId}
                onError={(message) => setSessionError(selectedId, message)}
              />
            ) : null}
          <AgentComposer
            assistantName={identity.assistantName}
            attachments={attachments}
            busy={busy}
            capabilityCatalog={capabilityCatalog}
            capabilityPolicyPending={capabilityPolicyPending}
            catalog={catalog}
            commands={commands}
            draft={draft}
            editState={editTarget}
            helpRequest={helpRequest}
            imageSupport={imageSupport}
            modelChanging={modelChanging}
            modelPickerRequest={modelPickerRequest}
            thinkingPickerRequest={thinkingPickerRequest}
            permissionPickerRequest={permissionPickerRequest}
            persona={persona}
            sending={sending || rewriteResolving}
            session={session}
            sessionMetadataKnown={sessionMetadataKnown}
            showJumpLatest={!timelineFollow.following}
            unseenUpdates={timelineFollow.unseenUpdates}
            stopping={stopping}
            toolCatalogStatus={toolCatalogStatus}
            toolPickerRequest={toolPickerRequest}
            toolPickerQuery={toolPickerQuery}
            tools={tools}
            onAttachmentsChange={setSelectedAttachments}
            onCancelEdit={cancelEdit}
            onCapabilityPreferenceChange={(canonicalId, preference) => void changeCapabilityPreference(canonicalId, preference)}
            onDraftChange={persistSelectedDraft}
            onEditPrevious={() => void beginEditMessage()}
            onJumpLatest={() => setScrollToLatestRequest((current) => current + 1)}
            onModelChange={changeModel}
            onPasteFromClipboard={() => void pasteImages()}
            onPasteImages={pasteImages}
            onPermissionChange={(selection) => void changePermission(selection)}
            onPickAttachments={() => void pickAttachments()}
            onProductCommand={runProductCommand}
            onSend={(delivery, value) => void send(delivery, value)}
            onStop={stop}
            onToolSelect={chooseTool}
            onWorkspaceRootsChange={() => void manageWorkspaceRoots()}
          />
          </div>
        ) : session ? (
          <div className="agent-conversation-state agent-session-readonly" data-read-only={evaluationSnapshot || undefined}>
            <strong>{evaluationSnapshot ? '评测快照，只读证据' : '正在确认对话权限'}</strong>
            <span>
              {evaluationSnapshot
                ? '这段对话来自冻结评测记录，不能继续提问、改写、分支或删除。'
                : '对话元数据尚未确认，暂不开放发送、权限或其他 Session 操作。'}
            </span>
          </div>
        ) : (
          <AgentConversationState
            loading={loading}
            error={sessionLoadError}
            onCreate={() => setNewSessionOpen(true)}
            onOpenRail={() => setRailOpen(true)}
          />
        )}
      </section>
      <button className="agent-status-backdrop" aria-hidden="true" disabled={!sidePanelModal} tabIndex={-1} onClick={closeSidePanel} type="button" />
      <AgentPaneResizer side="status" />
      {sessionControlsAvailable ? <AgentFilesPanel
        ref={filesRef}
        sessionId={selectedId}
        workspaceRoots={session?.workspaceRoots ?? []}
        open={filesOpen}
        modal={filesModal}
        onClose={closeFilesPanel}
        onManageRoots={() => void manageWorkspaceRoots()}
      /> : null}
      {sessionControlsAvailable && subagentPackageEnabled ? <SessionSubagentPanel
        ref={subagentsRef}
        sessionId={selectedId}
        session={session}
        tools={tools}
        open={subagentsOpen}
        modal={subagentsModal}
        onClose={closeSubagentsPanel}
      /> : null}
      {sessionControlsAvailable ? <AgentStatusPanel
        ref={statusRef}
        sessionId={selectedId}
        session={session}
        open={statusOpen}
        surfaceActive={appSurface?.active ?? true}
        capabilityCatalogError={capabilityCatalogError}
        modal={statusModal}
        commands={commands}
        tools={tools}
        toolCatalogStatus={toolCatalogStatus}
        capabilityCatalog={capabilityCatalog}
        capabilityPolicyMutation={capabilityPolicyMutation}
        busy={busy}
        contextSnapshotState={contextSnapshotState}
        onCapabilityCatalogRetry={() => void retryCapabilityCatalog()}
        onCapabilityPolicyRetry={retryCapabilityPreference}
        onCapabilityPreferenceChange={(canonicalId, preference) => void changeCapabilityPreference(canonicalId, preference)}
        onClose={closeStatusPanel}
      /> : null}
      {sessionControlsAvailable ? <MemoryReviewDialog
        activity={pendingApproval ? undefined : pendingMemoryReview}
        sessionId={selectedId}
        onError={(message) => setSessionError(selectedId, message)}
      /> : null}
      {sessionControlsAvailable ? <ApprovalReviewDialog activity={approvalForReview} onDecision={decideApproval} /> : null}
      <NewSessionDialog
        open={newSessionOpen}
        projects={projectPaths}
        defaultRoots={[]}
        onOpenChange={setNewSessionOpen}
        onPickRoots={() => pickWorkspaceRoots(true)}
        onCreate={createSession}
      />
      {sessionControlsAvailable ? <ConversationForkDialog
        assistantName={identity.assistantName}
        open={forkDialogOpen}
        sessionId={session?.id ?? ''}
        sessionTitle={session?.title ?? '新对话'}
        nodes={forkDialogNodes}
        initialEntryId={forkDialogInitialEntryId}
        branchAvailable={sessionControlsAvailable && conversationForkAvailable && isUserConversation}
        branchBlocked={branchBlocked}
        branchUnavailableReason={isRoomParticipant
          ? '这段对话属于 Room participant，历史分支与修改由 Room 管理。'
          : undefined}
        onOpenChange={setForkDialogOpen}
        onJump={jumpToMessage}
        onCreated={(created, selectedText) => { void acceptFork(created, selectedText); }}
      /> : null}
    </main>
  );
}

function isRecord(value: unknown): value is Record<string, unknown> { return typeof value === 'object' && value !== null && !Array.isArray(value); }


function isCancelledPromptAdmission(value: unknown): boolean {
  return isRecord(value)
    && value.accepted === false
    && value.cancelled === true
    && value.admissionCancelled === true;
}

function abortCancelledPendingAdmission(value: unknown): boolean {
  if (!isRecord(value)) return false;
  const runtimeReceipt = isRecord(value.runtimeReceipt)
    ? value.runtimeReceipt
    : {};
  const lifecycle = isRecord(runtimeReceipt.lifecycle)
    ? runtimeReceipt.lifecycle
    : {};
  return value.schemaVersion === 'rag-ime.agent-abort.v1'
    && runtimeReceipt.schemaVersion
      === 'rag-ime.pi-session-abort-receipt.v1'
    && runtimeReceipt.pendingAdmission === true
    && runtimeReceipt.admissionCancelled === true
    && lifecycle.schemaVersion === 'pi.agent-abort-receipt.v1'
    && lifecycle.drained === true
    && lifecycle.idle === true;
}

function discardSessionOptimisticMessages(sessionId: string): void {
  const liveStore = useAgentLiveStore.getState();
  const projection = liveStore.projections[sessionId];
  for (const clientMessageId of Object.keys(
    projection?.optimisticByClientMessageId ?? {},
  )) {
    liveStore.discardOptimistic(sessionId, clientMessageId);
  }
}


function conversationNodeText(blocks: Array<{ type: string; data: Record<string, unknown> }>): string {
  const value = blocks.map((block) => {
    const candidates = [block.data.text, block.data.markdown, block.data.code, block.data.message, block.data.summary];
    return candidates.find((item): item is string => typeof item === 'string' && item.trim().length > 0) ?? '';
  }).filter(Boolean).join('\n').replace(/\s+/gu, ' ').trim();
  return value.slice(0, 480) || '非文本消息';
}

function conversationNodesForSession(sessionId: string): ConversationNode[] {
  const projection = agentProjection(sessionId);
  return projection.messageOrder
    .map((messageId) => projection.messagesById[messageId])
    .filter((message) => (
      message
      && (message.role === 'user' || message.role === 'assistant')
    ))
    .map((message) => ({
      entryId: message!.id,
      role: message!.role as ConversationNode['role'],
      text: conversationNodeText(message!.blocks),
      createdAtMs: message!.createdAtMs,
    }))
    .filter((node) => node.text.length > 0);
}

function provisionalSessionRecord(id: string): SessionSummary {
  return {
    id,
    title: 'Session',
    mode: 'assistant',
    status: 'idle',
    roleId: '',
    roleVersion: '',
    roleBookRevisionId: '',
    updatedAtMs: 0,
    workspaceRoots: [],
  };
}

function sessionProjectName(session: SessionSummary): string {
  const root = session.workspaceRoots?.[0] ?? '';
  return root.split('/').filter(Boolean).at(-1) ?? '未指定项目';
}
function isWellPairedExplicitProfile(
  profile: string | undefined,
  executionMode: string | undefined,
): boolean {
  return (
    (profile === 'control-center-full-access-v1' && executionMode === 'per_action')
    || (profile === 'control-center-auto-approve-v1' && executionMode === 'full_trust')
  );
}
function isCommand(value: string, invocation: string): boolean {
  return value === invocation || value.startsWith(`${invocation} `);
}
function commandArgument(value: string, invocation: string): string {
  return value.slice(invocation.length).trim();
}
function normalizedSessionTitle(value: string): string {
  return value.split(/\s+/u).filter(Boolean).join(' ').slice(0, 120);
}
function isAdvertisedPiCommand(value: string, commands: AgentCommand[]): boolean {
  return commands.some((command) => isCommand(value, command.invocation));
}
function errorText(value: unknown): string {
  const message = value instanceof Error ? value.message : String(value);
  if (/invalid route parameter:\s*limit/i.test(message)) return '对话列表暂时无法加载，请刷新后重试。';
  return publicAgentErrorText(value, '操作未完成，请刷新状态后重试。');
}
function modelCatalogNotice(value: unknown, usingCachedCatalog = false): string {
  const message = value instanceof Error ? value.message : String(value ?? '');
  if (/session runtime is unavailable|workspace (?:does not exist|no longer exists)/i.test(message)) {
    return '这段对话的工作目录已不可用；对话记录仍保留，可以归档后选择其他对话。';
  }
  if (usingCachedCatalog) {
    return '模型目录刷新失败，正在继续使用这段对话上次由 Pi 确认的模型状态。';
  }
  return '模型目录暂时不可用，对话记录仍可查看。';
}
function shouldOpenTaskCenterByDefault(): boolean {
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && !window.matchMedia('(max-width: 1360px)').matches;
}

const MAX_AGENT_IMAGE_BYTES = 20 * 1024 * 1024;
const STOP_RECONCILE_BUDGET_MS = 1_450;
const STOP_RECONCILE_ESCALATED_MESSAGE = '1.5 秒内未收到终态，已进入 Pi 终止兜底；状态会继续同步。';

type DeadlineSettlement<T> =
  | { kind: 'resolved'; value: T }
  | { kind: 'rejected'; error: unknown }
  | { kind: 'timeout' };

async function settleBeforeDeadline<T>(
  request: Promise<T>,
  deadlineAt: number,
): Promise<DeadlineSettlement<T>> {
  const remainingMs = Math.max(0, deadlineAt - monotonicNow());
  if (remainingMs === 0) return { kind: 'timeout' };
  return new Promise((resolve) => {
    let settled = false;
    let timer = 0;
    const finish = (result: DeadlineSettlement<T>) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      resolve(result);
    };
    timer = window.setTimeout(() => finish({ kind: 'timeout' }), remainingMs);
    request.then(
      (value) => finish({ kind: 'resolved', value }),
      (error: unknown) => finish({ kind: 'rejected', error }),
    );
  });
}

function latestActiveTurnId(projection?: AgentProjectionState): string {
  if (!projection) return '';
  // A rejected follow-up is not a terminal fence for Pi's ongoing retry.
  if (projection.status === 'retrying') {
    for (const id of [...projection.activityOrder].reverse()) {
      const activity = projection.activitiesById[id];
      if (activity?.payload.phase === 'provider_retry' && activity.status === 'running'
        && projection.turnsById[activity.turnId]?.status === 'running') return activity.turnId;
    }
  }

  // The newest visible turn owns retry, stop, and model-change availability.
  // A stale older streaming flag must not revive after a later terminal turn.
  for (let index = projection.turnOrder.length - 1; index >= 0; index -= 1) {
    const turnId = projection.turnOrder[index];
    const turn = turnId ? projection.turnsById[turnId] : undefined;
    if (!turn || (turn.messageIds.length === 0 && turn.activityIds.length === 0)) continue;
    return turn.status === 'queued' || turn.status === 'running' || turn.status === 'waiting'
      ? turnId
      : '';
  }
  return '';
}

function latestWaitingActivity(
  projection: ReturnType<typeof agentProjection> | undefined,
  predicate: (activity: ReturnType<typeof agentProjection>['activitiesById'][string]) => boolean,
) {
  if (!projection) return undefined;
  for (let index = projection.activityOrder.length - 1; index >= 0; index -= 1) {
    const activity = projection.activitiesById[projection.activityOrder[index] ?? ''];
    if (activity?.status === 'waiting' && predicate(activity)) return activity;
  }
  return undefined;
}

function selectedModelImageSupport(catalog?: ModelCatalog): 'supported' | 'unsupported' | 'unknown' {
  if (!catalog) return 'unknown';
  const selected = isRecord(catalog.selected) ? catalog.selected : {};
  const providerId = typeof selected.provider === 'string' ? selected.provider : '';
  const modelId = typeof selected.id === 'string' && selected.id
    ? selected.id
    : typeof selected.modelId === 'string'
      ? selected.modelId
      : '';
  const model = catalog.providers.find((provider) => provider.id === providerId)?.models.find((item) => item.id === modelId);
  if (!model && typeof selected.supportsImages === 'boolean') {
    return selected.supportsImages ? 'supported' : 'unsupported';
  }
  if (!model) return 'unknown';
  return model.supportsImages ? 'supported' : 'unsupported';
}
