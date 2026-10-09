import { ChatPresentationSettings } from '@/features/conversation-ui/reading/ChatPresentationSettings';
import { ChatPresentationProvider, useChatPresentation } from '@/features/conversation-ui/reading/chat-presentation';
import { FocusScope, Popover, PopoverContent, PopoverTrigger } from '@/components/primitives';
import type { InitialSessionSubmission } from './agent-workspace-loader';
import { PAW_EXTENSION_INSTALLATION_CHANGED_EVENT, installationChangeMatchesTransport } from '@/paw-os/extensions/installation';
import { capabilityCatalogQueryOptions, observeCatalogQuery, pluginQueryKeys, prepareCatalogRefresh, readCatalogQuery, useCatalogQueryClient } from '@/features/plugins/catalog-queries';
import { sameAgentCompactionTarget, type AgentCompactionTarget } from '@/contracts/agent-compaction-target';
import { useWorkspaceRecovery, WorkspaceRecoveryNotice } from '@/features/semantic-workspace/workspace-recovery';
import { mergeQueueBackToDraft } from '@/features/conversation-ui/model/queue';
import './paw-workbench-reading.css';
import { PawSessionTaskbar } from './PawSessionTaskbar';
import type { AgentWorkflowStateV1 } from '@/contracts/generated/agent-workflow-state.v1';
import { latestWorkflowGoal } from '@/features/agent/status/AgentWorkflowPanel';
import {
  ChevronDown,
  CircleAlert,
  FolderTree,
  GitBranch,
  History,
  ListChecks,
  LoaderCircle,
  MessageSquare,
  Network,
  Orbit,
  ShieldCheck,
  StopCircle,
  Wrench,
  X,
} from 'lucide-react';
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type FocusEvent,
  type ReactNode,
  type KeyboardEvent,
} from 'react';
import { useShallow } from 'zustand/react/shallow';
import { applyWorkspaceDraft, messageWithWorkspaceContext, type WorkspaceDraftRequest, type WorkspaceComposerContext } from './workspace-draft';
import { WorkspaceProjectContext } from './WorkspaceProjectContext';
import { useControlTransport } from '@/app/control-transport';
import { attachmentImportErrorText } from '@/platform/attachment-import';
import { useComposerClearance } from '@/components/layout/use-composer-clearance';
import {
  agentMessageDelivery,
  resolveAgentTurnUserMessage,
  type AgentActivityProjection,
  type AgentMessageProjection,
  type AgentProjectionState,
} from '@/contracts/agent-reducer';
import { approvalNeedsHumanDecision } from '@/contracts/approval-decision';
import { parseContract } from '@/contracts/validators';
import type { AgentPersonaV1 } from '@/contracts/generated/agent-persona.v1';
import {
  AgentComposer,
  type AgentMessageDelivery,
} from '@/features/agent/composer/AgentComposer';
import { unrestrictedWorkspaceRoots } from '@/features/agent/composer/permission-policy';
import { SessionSubagentPanel } from '@/features/agent/delegation/SessionSubagentPanel';
import {
  agentCommandReceiptFailure,
  isAgentCommandPending,
  isAgentSessionIdleFailure,
  isAgentTurnConflict,
  isAmbiguousAgentPromptFailure,
  isAgentWorkspaceMissingError,
  isUnresolvedAgentCommandPending,
  publicAgentErrorText,
  SESSION_WORKSPACE_MISSING_TEXT,
} from '@/features/agent/public-error';
import { hasUndurableAgentAttachments } from '@/features/agent/optimistic-attachments';
import {
  useAgentLiveSession,
  type AgentRecoveryState,
  type AgentLiveSnapshotLoader,
} from '@/features/agent/runtime/use-agent-live-session';
import { openPawOsRoute, usePawOsDesktop } from '@/features/paw-os/surface-context';
import {
  backgroundJobWindowRequest,
  createRuntimeToolWindowProjector,
  shouldAutoOpenRuntimeToolWindow,
} from '../runtime/runtime-tool-window';
import { PawWindowChromePortal, usePawWindowChromeTarget } from '../shell/PawWindowChrome';
import {
  ApprovalReviewDialog,
  GenericUserInputCard,
  MemoryReviewDialog,
} from '@/features/agent/review/AgentReviewDialogs';
import {
  ConversationForkDialog,
  resolveConversationEntryId,
  type ConversationNode,
} from '@/features/agent/sessions/ConversationForkDialog';
import { agentProjection, agentProjectionKey, agentSessionAddress, selectAgentProjection, latestActiveAgentTurnId as latestActiveTurnId, useAgentLiveStore, type AgentSessionTarget } from '@/features/agent/state/live-store';
import { AgentStatusPanel } from '@/features/agent/status/AgentStatusPanel';
import { AgentTimeline, initialAgentResponseTurnId, labProjectUserDraft, type AgentUserMessagePresentation } from '@/features/agent/timeline/AgentTimeline';
import { QueueTray, useConversationQueue } from '@/features/conversation-ui';
import { toolIntentPrompt } from '@/features/agent/tool-presentation';
import { AgentFilesPanel } from '@/features/agent/workspace/AgentFilesPanel';
import { pawBrowserHost } from './paw-browser-host';
import { TraceAgentHandoffButton } from '@/features/trace-agent/handoff';
import { usePageVisibility } from '@/platform/use-page-visibility';
import { PawContextTrace } from './PawContextTrace';
/* 星空按钮按下之前，星空代码不进入 Agent 主页/对话的 bundle 路径。 */
import { LazyPawSessionStarfield } from './PawStarfieldLazy';
import {
  commandItems,
  isModelCatalog,
  sessionItems,
  toolItems,
  type AgentCommand,
  type AgentPermissionSelection,
  type AgentProductCommandName,
  type ComposerAttachment,
  type CodemodeMode,
  type ModelCatalog,
  type SessionSummary,
  type ThinkingLevel,
  type ToolManifest,
} from '@/features/agent/types';
import {
  capabilityScopeLabel,
  type CapabilityCatalog,
  type CapabilityMutationOutcome,
  type CapabilityPreference,
} from '@/features/plugins/capability-policy';
import '@/features/agent/agent.css';
import { screenContextForMessage, type ScreenContext } from '@/features/screen-assistant/screen-assistant-model';
import { ProjectQuickActions } from '@/features/eval-lab/projects/ProjectQuickActions';

type WorkbenchPanel = 'none' | 'files' | 'subagents' | 'status';
type SessionWorkspaceView = 'conversation' | 'trace' | 'starfield';
const STOP_UNCONFIRMED_TEXT = '尚有后台资源未确认停止。请查看任务与状态后重试。';

export function sessionWorkspaceProjectionSlice(
  state: ReturnType<typeof useAgentLiveStore.getState>,
  target: AgentSessionTarget,
) {
  const projection = selectAgentProjection(state, target);
  return {
    activeTurnId: projection?.durableRecovery?.activeTurn?.turnId ?? latestActiveTurnId(projection),
    hasTurns: Boolean(projection?.turnOrder.length),
    goal: projection?.goal,
    lastTurnStatus: projection?.turnsById[projection.turnOrder.at(-1) ?? '']?.status,
    pendingMemoryReview: latestWaitingActivity(
      projection,
      (activity) => activity.kind === 'user_input_required' && activity.payload.requestKind === 'memory_review',
    ),
    pendingGenericInput: latestWaitingActivity(
      projection,
      (activity) => activity.kind === 'user_input_required' && activity.payload.requestKind !== 'memory_review',
    ),
    pendingApproval: latestWaitingActivity(
      projection,
      (activity) => activity.kind === 'approval_required' && approvalNeedsHumanDecision(activity.payload),
    ),
    telemetry: projection?.telemetry,
    runtimeEngine: projection?.runtimeEngine,
    durableRecovery: projection?.durableRecovery,
  };
}


export type WorkspaceComposerHeaderView = {
  session: SessionSummary; draft: string; disabled: boolean; sourceMessageId(): string | undefined;
};

export function PawSessionWorkspace(props: Parameters<typeof PawSessionWorkspaceBody>[0]) {
  const presentation = useChatPresentation();
  const ownerKey = props.record?.ownerAppId || 'builtin:agent';
  return presentation ? <PawSessionWorkspaceBody {...props} /> : <ChatPresentationProvider ownerKey={ownerKey} defaultVersion={ownerKey.startsWith('extension:') ? 'v1' : 'v2'}><PawSessionWorkspaceBody {...props} /></ChatPresentationProvider>;
}

function PawSessionWorkspaceBody({
  active = true,
  persona,
  record,
  recordMetadataKnown = Boolean(record),
  recordId,
  initialDraft = '',
  initialSubmission,
  renderComposerHeader,
  initialAttachments = [],
  draftRequest,
  composerContext,
  screenContext,
  onNewWork,
  onSessionCreated,
  onSessionActivity,
  onSessionUpdated,
  traceFocusNodeId = '',
  toolPickerIntent,
  appearance = 'full',
  showComposerControls = appearance !== 'embedded',
  composerPlaceholder,
  userMessagePresentation,
  fullHistoryOnOpen = false,
}: {
  active?: boolean;
  persona?: AgentPersonaV1;
  record?: SessionSummary;
  recordMetadataKnown?: boolean;
  recordId: string;
  initialDraft?: string;
  initialSubmission?: InitialSessionSubmission;
  renderComposerHeader?: (view: WorkspaceComposerHeaderView) => ReactNode;
  initialAttachments?: ComposerAttachment[];
  draftRequest?: WorkspaceDraftRequest;
  composerContext?: WorkspaceComposerContext;
  screenContext?: ScreenContext;
  /** 反向证据链落点：直接进入轨迹视图并聚焦这个装配节点。 */
  traceFocusNodeId?: string;
  toolPickerIntent?: { id: string; query: string };
  onNewWork: () => void;
  onSessionCreated: (session: SessionSummary, draft: string) => void;
  onSessionActivity?: () => void;
  onSessionUpdated: (session: SessionSummary) => void;
  appearance?: 'full' | 'embedded';
  showComposerControls?: boolean;
  composerPlaceholder?: string;
  userMessagePresentation?: AgentUserMessagePresentation;
  /** Earth and other audit-heavy surfaces can opt into the complete turn log
   * on first paint; ordinary Agent keeps its bounded recent snapshot. */
  fullHistoryOnOpen?: boolean;
}) {
  const transport = useControlTransport();
  const presentation = useChatPresentation();
  const catalogQueryClient = useCatalogQueryClient();
  const address = agentSessionAddress(transport, recordId);
  const workspaceScopeRef = useRef({ recordId, transport });
  if (workspaceScopeRef.current.recordId !== recordId || workspaceScopeRef.current.transport !== transport) {
    workspaceScopeRef.current = { recordId, transport };
  }
  const workspaceScope = workspaceScopeRef.current;
  const electronHost = pawBrowserHost();
  const desktop = usePawOsDesktop();
  const windowChromeTarget = usePawWindowChromeTarget();
  const embedded = appearance === 'embedded';
  const workspaceRecord = record ?? provisionalSessionRecord(recordId);
  const permissionsLocked = workspaceRecord.metadata?.primaryAssistant === true || workspaceRecord.metadata?.primaryTask === true;
  const initialSubmissionRef = useRef<string | undefined>(undefined);
  const evaluationSnapshot = record?.evaluationSnapshot === true;
  const pageVisible = usePageVisibility();
  // Keep every mounted chat window current even when another PAW window has
  // focus. Only a hidden document suspends the authoritative event stream.
  const liveActive = pageVisible;
  const projectionSlice = useAgentLiveStore(useShallow(
    (state) => sessionWorkspaceProjectionSlice(state, address),
  ));
  // Read the existing workflow cache without creating another query observer
  // or changing its fetch options. The workflow panel owns reads/mutations.
  const workflowKey = useMemo(() => ['agent', 'workflow', agentProjectionKey(address)] as const, [address]);
  const workflowReceipt = useSyncExternalStore(
    useCallback((notify) => catalogQueryClient.getQueryCache().subscribe(({ query }) => {
      if (query.queryKey[0] === workflowKey[0] && query.queryKey[1] === workflowKey[1] && query.queryKey[2] === workflowKey[2]) notify();
    }), [catalogQueryClient, workflowKey]),
    useCallback(() => catalogQueryClient.getQueryData<AgentWorkflowStateV1>(workflowKey), [catalogQueryClient, workflowKey]),
  );
  const taskbarGoal = latestWorkflowGoal(
    workflowReceipt?.sessionId === recordId && workflowReceipt.goal.sessionId === recordId ? workflowReceipt.goal : undefined,
    projectionSlice.goal,
  );
  const [catalog, setCatalog] = useState<ModelCatalog>();
  const durableSession = workspaceRecord.runtimeEngine === 'durable' || catalog?.runtimeEngine === 'durable' || projectionSlice.runtimeEngine === 'durable';
  const durablePaused = projectionSlice.durableRecovery?.paused === true;
  const compactionTarget = projectionSlice.durableRecovery?.compactionTarget;
  const classicHistoryAvailable = recordMetadataKnown && record?.id === recordId && !durableSession;
  const [commands, setCommands] = useState<AgentCommand[]>([]);
  const [tools, setTools] = useState<ToolManifest[]>([]);
  const [toolCatalogStatus, setToolCatalogStatus] = useState<'loading' | 'ready' | 'failed'>('loading');
  const [capabilityCatalog, setCapabilityCatalog] = useState<CapabilityCatalog>();
  const [capabilityCatalogError, setCapabilityCatalogError] = useState('');
  const [capabilityMutation, setCapabilityMutation] = useState<CapabilityMutationOutcome>();
  const recovery = useWorkspaceRecovery<ComposerAttachment>(`session:${recordId}`, initialSubmission?.message ?? initialDraft ?? '', initialAttachments, initialSubmission ? 'replace' : 'append');
  const { draft, setDraft, attachments, setAttachments } = recovery;
  useEffect(() => {
    if (draftRequest) setDraft(current => applyWorkspaceDraft(current, draftRequest));
  }, [draftRequest]);
  // A failed send or restored queue can contain the transport-only Lab snapshot.
  // Keep the real request editable; the snapshot will be attached again on send.
  useEffect(() => {
    if (userMessagePresentation !== 'project-context') return;
    const request = labProjectUserDraft(draft);
    if (request !== undefined) setDraft(request);
  }, [draft, setDraft, userMessagePresentation]);
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [resuming, setResuming] = useState(false);
  const resumeRequestRef = useRef<{ controller: AbortController; recordId: string; transport: typeof transport; target?: AgentCompactionTarget } | undefined>(undefined);
  const compactionStopRequestRef = useRef<{ controller: AbortController; recordId: string; transport: typeof transport; target: AgentCompactionTarget } | undefined>(undefined);
  const turnStopRequestRef = useRef<{
    scope: typeof workspaceScope; clientMessageId?: string; acknowledged: boolean; admissionCancelled: boolean;
  } | undefined>(undefined);
  const resumeOwnerRef = useRef({ recordId, transport });
  resumeOwnerRef.current = { recordId, transport };
  useEffect(() => {
    setResuming(false);
    setStopping(false);
    return () => {
      const request = resumeRequestRef.current;
      if (request?.recordId === recordId && request.transport === transport) {
        request.controller.abort(); resumeRequestRef.current = undefined;
      }
      const stopRequest = compactionStopRequestRef.current;
      if (stopRequest?.recordId === recordId && stopRequest.transport === transport) {
        stopRequest.controller.abort(); compactionStopRequestRef.current = undefined;
      }
      if (turnStopRequestRef.current?.scope === workspaceScope) turnStopRequestRef.current = undefined;
    };
  }, [recordId, transport]);
  const pendingFeedbackTurnId = useAgentLiveStore(state => initialAgentResponseTurnId(selectAgentProjection(state, address)));
  const [stopping, setStopping] = useState(false);
  useEffect(() => {
    // A newer native target may appear before an older control request returns.
    // Release only that stale request's local lock; never retarget its payload.
    const resume = resumeRequestRef.current;
    if (resume?.target && !sameAgentCompactionTarget(resume.target, compactionTarget)) {
      resume.controller.abort(); resumeRequestRef.current = undefined; setResuming(false);
    }
    const stopRequest = compactionStopRequestRef.current;
    if (stopRequest && !sameAgentCompactionTarget(stopRequest.target, compactionTarget)) {
      stopRequest.controller.abort(); compactionStopRequestRef.current = undefined; setStopping(false);
    }
  }, [compactionTarget]);
  const [modelChangeRequest, setModelChangeRequest] = useState<{ scope: typeof workspaceScope }>();
  const modelChanging = modelChangeRequest?.scope === workspaceScope;
  const [codemodeSelection, setCodemodeSelection] = useState<{ scope: typeof workspaceScope; mode?: CodemodeMode }>();
  const codemodeMode = codemodeSelection?.scope === workspaceScope ? codemodeSelection.mode : undefined;
  const codemodeRequestRef = useRef<{ scope: typeof workspaceScope } | undefined>(undefined);
  const [codemodeChangeRequest, setCodemodeChangeRequest] = useState<{ scope: typeof workspaceScope }>();
  const codemodeChanging = codemodeChangeRequest?.scope === workspaceScope;
  useEffect(() => () => {
    // An old Session or transport ACK cannot update a reopened workspace.
    if (codemodeRequestRef.current?.scope === workspaceScope) codemodeRequestRef.current = undefined;
  }, [workspaceScope]);
  const [panel, setPanel] = useState<WorkbenchPanel>('none');
  const [statusPanelVisited, setStatusPanelVisited] = useState(false);
  const [toolMenuOpen, setToolMenuOpen] = useState(false);
  const [controlsExpanded, setControlsExpanded] = useState(false);
  const [workspaceView, setWorkspaceView] = useState<SessionWorkspaceView>(embedded ? 'conversation' : traceFocusNodeId ? 'trace' : 'conversation');
  const [error, setError] = useState('');
  // Switching Sessions must preserve each unresolved Stop's transport and target.
  const [compactionStopWarnings, setCompactionStopWarnings] = useState(() => new Map<string, {
    transport: typeof transport; target: AgentCompactionTarget;
  }>());
  useEffect(() => {
    // Only validated owner metadata may retire this target's uncertainty.
    // A missing/history-only target cannot clear it or a separate turn Stop.
    if (compactionTarget === undefined) return;
    setCompactionStopWarnings(current => {
      const warning = current.get(recordId);
      if (warning?.transport !== transport || sameAgentCompactionTarget(warning.target, compactionTarget)) return current;
      const next = new Map(current);
      next.delete(recordId);
      return next;
    });
  }, [compactionTarget, recordId, transport]);
  const compactionStopError = compactionStopWarnings.get(recordId)?.transport === transport
    ? STOP_UNCONFIRMED_TEXT : '';
  const [attachmentError, setAttachmentError] = useState('');
  // Import receipts belong to the current input owner. Count every in-flight
  // picker/paste/drop so one completion cannot unlock another partial input.
  const attachmentImports = useMemo(() => ({ active: false, pending: new Set<symbol>() }), [workspaceScope]);
  const [, refreshAttachmentImports] = useState(0);
  const attachmentImportPending = attachmentImports.pending.size > 0;
  useEffect(() => {
    attachmentImports.active = true;
    setAttachmentError('');
    return () => { attachmentImports.active = false; attachmentImports.pending.clear(); };
  }, [attachmentImports]);
  const [syncError, setSyncError] = useState('');
  const [syncState, setSyncState] = useState<AgentRecoveryState>('recovering');
  // Visible history is independent of command admission. A background expansion
  // does not revoke an accepted recent snapshot, and an old owner cannot grant it.
  const [historyRead, setHistoryRead] = useState<{
    scope: typeof workspaceScope; view?: 'recent' | 'full'; expanding?: boolean;
  }>();
  const hasSnapshot = historyRead?.scope === workspaceScope && Boolean(historyRead.view);
  const contextSnapshotState = historyRead?.scope === workspaceScope && historyRead.expanding
    ? 'restoring' : hasSnapshot && historyRead?.view === 'full' ? undefined : 'partial';
  useEffect(() => {
    setSyncError(''); setSyncState('recovering'); setHistoryRead(undefined);
    setCatalog(undefined); setTools([]); setCommands([]); setCapabilityCatalog(undefined);
    setCapabilityCatalogError(''); setToolCatalogStatus('loading');
  }, [recordId, transport]);
  const visibleError = error || compactionStopError || (syncError && (!hasSnapshot || syncState === 'failed')
    ? '连接暂时不可用，系统会继续自动重连。' : '');
  const [modelPickerRequest, setModelPickerRequest] = useState(0);
  const [thinkingPickerRequest, setThinkingPickerRequest] = useState(0);
  const [permissionPickerRequest, setPermissionPickerRequest] = useState(0);
  const [toolPickerRequest, setToolPickerRequest] = useState(0);
  const [toolPickerQuery, setToolPickerQuery] = useState('');
  const appliedToolIntent = useRef('');
  useEffect(() => {
    if (!toolPickerIntent || appliedToolIntent.current === toolPickerIntent.id || toolCatalogStatus !== 'ready') return;
    appliedToolIntent.current = toolPickerIntent.id;
    setToolPickerQuery(toolPickerIntent.query);
    setToolPickerRequest((value) => value + 1);
  }, [toolCatalogStatus, toolPickerIntent]);
  const [helpRequest, setHelpRequest] = useState(0);
  const [requestedApproval, setRequestedApproval] = useState<AgentActivityProjection>();
  const [conversationForkAvailable, setConversationForkAvailable] = useState(false);
  const [conversationRewriteAvailable, setConversationRewriteAvailable] = useState(false);
  const [forkDialogOpen, setForkDialogOpen] = useState(false);
  const [forkDialogNodes, setForkDialogNodes] = useState<ConversationNode[]>([]);
  const [forkDialogInitialEntryId, setForkDialogInitialEntryId] = useState('');
  const [editState, setEditState] = useState<{ entryId: string; messageId: string; resolving?: boolean }>();
  const editRequestRef = useRef(0);
  useEffect(() => {
    setEditState(undefined);
    return () => { editRequestRef.current += 1; };
  }, [workspaceScope]);
  const [jumpRequest, setJumpRequest] = useState<{ messageId: string; requestId: number }>();
  const [timelineFollow, setTimelineFollow] = useState({ following: true, unseenUpdates: 0 });
  const [scrollToLatestRequest, setScrollToLatestRequest] = useState(0);
  const toolMenuContainerRef = useRef<HTMLDivElement>(null);
  const toolMenuButtonRef = useRef<HTMLButtonElement>(null);
  const toolPanelReturnFocusRef = useRef<HTMLButtonElement | null>(null);
  const toolMenuRef = useRef<HTMLElement>(null);
  const toolMenuInitialFocusRef = useRef<'first' | 'last'>('first');
  const primaryRef = useRef<HTMLDivElement>(null);
  const toolPanelRef = useRef<HTMLElement>(null);
  const [toolPanelOverlay, setToolPanelOverlay] = useState(true);
  const previousToolPanel = useRef<WorkbenchPanel>('none');
  const toolPanelTrapsFocus = active && panel !== 'none' && toolPanelOverlay;
  // CSS owns docking. Observe its result rather than duplicate breakpoints or
  // remount the file tree when a window moves between docked and overlay layouts.
  useEffect(() => {
    const element = toolPanelRef.current;
    const body = primaryRef.current?.parentElement;
    if (!element || !body || panel === 'none') return;
    const update = () => setToolPanelOverlay(getComputedStyle(element).position !== 'static');
    update();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(update);
    observer.observe(body);
    return () => observer.disconnect();
  }, [panel]);
  useEffect(() => {
    const opened = previousToolPanel.current !== panel;
    previousToolPanel.current = panel;
    if (active && panel !== 'none' && (opened || toolPanelOverlay)) {
      toolPanelRef.current?.querySelector<HTMLElement>('[data-drawer-autofocus]')?.focus({ preventScroll: true });
    }
  }, [active, panel, toolPanelOverlay]);
  const terminalSnapshotTimerRef = useRef<number | undefined>(undefined);
  const catalogAbortRef = useRef<AbortController | undefined>(undefined);
  const catalogRequestRef = useRef(0);
  const loadAgentSnapshotRef = useRef<AgentLiveSnapshotLoader>(
    async () => false,
  );
  const sessionActionLockRef = useRef(false);
  /* The composer floats over the full-height conversation canvas, so the
     timeline must reserve exactly the overlay's rendered height as footer
     space — the same measured-clearance contract the classic workspace uses. */
  useComposerClearance(primaryRef, '.paw-session-workspace__composer');
  const runtimeToolWindow = useMemo(() => createRuntimeToolWindowProjector(), [recordId]);

  useEffect(() => {
    setWorkspaceView(embedded ? 'conversation' : traceFocusNodeId ? 'trace' : 'conversation');
    setPanel('none');
    setStatusPanelVisited(false);
    setToolMenuOpen(false);
  }, [embedded, recordId, traceFocusNodeId]);

  const busy = Boolean(projectionSlice.activeTurnId || compactionTarget);
  const submissionBlockedReason = attachmentImportPending ? '正在导入附件'
    : editState && (editState.resolving || !editState.entryId) ? '正在定位历史消息'
      : recovery.checking ? '正在核实恢复的附件'
        : recovery.issues.length ? '请先处理失效附件' : '';
  const queueAdmissionBlocked = sending || modelChanging || Boolean(submissionBlockedReason);
  /* A held follow-up is the composer's own queue, not a Runtime delivery.
     干预/接续 hand the message to Pi immediately; a queued draft never leaves
     the client until this turn settles, which is what keeps it editable,
     reorderable, revocable, and restorable when the turn is stopped. */
  const queue = useConversationQueue({
    busy: busy || durablePaused || queueAdmissionBlocked,
    conversationId: recovery.ownerId,
    onDispose: items => recovery.recoverInput(current => ({ ...current, draft: mergeQueueBackToDraft(items, current.draft) })),
    send: (text) => {
      // Queue consumption is synchronous; an async send that returns before
      // admission must not discard the input the queue still owns.
      if (queueAdmissionBlocked || !acceptsComposerInput()) return false;
      if (!acceptsImmediateInput(text)) return false;
      void send('prompt', text);
    },
  });
  const pendingMemoryReview = projectionSlice.pendingMemoryReview;
  const pendingGenericInput = projectionSlice.pendingGenericInput;
  const pendingApproval = projectionSlice.pendingApproval;
  const imageSupport = durableSession ? 'unsupported' : selectedModelImageSupport(catalog);

  const loadFullSnapshot = useCallback(async (): Promise<void> => {
    if (!liveActive) return;
    const scope = workspaceScope;
    const load = loadAgentSnapshotRef.current;
    if (workspaceScopeRef.current !== scope) return;
    setHistoryRead(current => ({ ...(current?.scope === scope ? current : {}), scope, expanding: true }));
    const loaded = await load({ view: 'full' });
    if (workspaceScopeRef.current !== scope) return;
    if (!loaded) setHistoryRead(current => current?.scope === scope ? { ...current, expanding: false } : current);
  }, [liveActive, workspaceScope]);

  const loadControlCatalog = useCallback(async (afterChange = false) => {
    const scope = workspaceScope;
    if (!liveActive || document.visibilityState === 'hidden' || workspaceScopeRef.current !== scope) return;
    const postChangeRefresh = afterChange ? prepareCatalogRefresh(catalogQueryClient, [pluginQueryKeys.catalog(transport, recordId)]) : undefined;
    catalogAbortRef.current?.abort();
    const controller = new AbortController();
    catalogAbortRef.current = controller;
    const signal = controller.signal;
    const requestId = ++catalogRequestRef.current;
    const isCurrent = () => workspaceScopeRef.current === scope && requestId === catalogRequestRef.current && !signal?.aborted;
    setToolCatalogStatus('loading');
    // Publish each independent catalog as it arrives. A slow model/command
    // lookup must not keep the already-confirmed memory and tool switches hidden.
    const requestOptions = signal ? { signal } : {};
    const catalogReads = Promise.allSettled([
      transport.request({ pathId: 'agent.session.models', params: { sessionId: recordId }, ...requestOptions }).then((value) => {
        if (isCurrent() && isModelCatalog(value)) setCatalog(value);
      }),
      transport.request({ pathId: 'agent.session.commands', params: { sessionId: recordId }, ...requestOptions }).then((value) => {
        if (isCurrent()) setCommands(commandItems(value));
      }),
      observeCatalogQuery(catalogQueryClient, { ...capabilityCatalogQueryOptions(transport, recordId), staleTime: 0 }, signal, {
        onData: value => {
          if (!isCurrent()) return;
          setTools(toolItems(value));
          setCapabilityCatalog(value);
          setCapabilityCatalogError('');
          setToolCatalogStatus('ready');
        },
        onError: reason => {
          if (!isCurrent()) return;
          setTools([]);
          setCapabilityCatalog(undefined);
          setCapabilityCatalogError(errorText(reason));
          setToolCatalogStatus('failed');
        },
        onFetching: () => { if (isCurrent()) setToolCatalogStatus('loading'); },
      }),
      transport.request<Record<string, unknown>>({ pathId: 'agent.runtime.get', ...requestOptions }).then((value) => {
        if (!isCurrent()) return;
        const capabilities = asRecord(value.capabilities);
        setConversationForkAvailable(capabilities.conversationFork === true);
        setConversationRewriteAvailable(capabilities.conversationRewrite === true);
      }).catch(() => {
        if (!isCurrent()) return;
        setConversationForkAvailable(false);
        setConversationRewriteAvailable(false);
      }),
    ]);
    await Promise.all([catalogReads, postChangeRefresh?.()]);
  }, [catalogQueryClient, liveActive, recordId, transport, workspaceScope]);

  const refreshControlCatalog = useCallback((afterChange = false) => {
    if (evaluationSnapshot || !liveActive) return;
    void loadControlCatalog(afterChange);
  }, [evaluationSnapshot, liveActive, loadControlCatalog]);

  useEffect(() => {
    if (evaluationSnapshot || !liveActive) return;
    // An accepted installation receipt invalidates displayed capabilities;
    // the session catalog remains the authority and no permissions are changed.
    const refresh = (event: Event) => {
      if (installationChangeMatchesTransport(event, transport)) refreshControlCatalog(true);
    };
    window.addEventListener(PAW_EXTENSION_INSTALLATION_CHANGED_EVENT, refresh);
    return () => window.removeEventListener(PAW_EXTENSION_INSTALLATION_CHANGED_EVENT, refresh);
  }, [evaluationSnapshot, liveActive, refreshControlCatalog, transport]);

  const loadAgentSnapshot = useAgentLiveSession({
    sessionId: recordId,
    transport,
    active: liveActive,
    live: liveActive && !evaluationSnapshot,
    snapshotView: evaluationSnapshot || (fullHistoryOnOpen && hasSnapshot) ? 'full' : 'recent',
    onLoadingChange: setLoading,
    onRecoveryState: setSyncState,
    onSnapshot: (snapshot) => {
      if (workspaceScopeRef.current === workspaceScope && codemodeRequestRef.current?.scope !== workspaceScope) {
        const native = asRecord(snapshot.value);
        if (!native.sessionId || native.sessionId === recordId) {
          setCodemodeSelection({ scope: workspaceScope, mode: nativeCodemodeMode(native.codemodeMode) });
        }
      }
      setHistoryRead(current => ({
        scope: workspaceScope,
        view: current?.scope === workspaceScope && current.view === 'full' ? 'full' : snapshot.view,
      }));
      setSyncError('');
      // A transcript snapshot cannot confirm that a captured process drained.
      setError(current => current === STOP_UNCONFIRMED_TEXT ? current : '');
      refreshControlCatalog();
    },
    onSnapshotError: (failure) => {
      setHistoryRead(current => current?.scope === workspaceScope ? { ...current, expanding: false } : current);
      if (isAgentWorkspaceMissingError(failure.error)) {
        setError(errorText(failure.error));
      } else {
        setSyncError(errorText(failure.error));
      }
      if (failure.recoverable) refreshControlCatalog();
    },
    onEvent: (event) => {
      if (event.eventType === 'snapshot_required') return;
      if (event.eventType === 'session_configuration_changed') {
        refreshControlCatalog(true);
        const payload = asRecord(event.payload);
        const mode = nativeCodemodeMode(payload.codemodeMode);
        if (payload.kind === 'codemode' && mode && codemodeMode !== undefined
          && workspaceScopeRef.current === workspaceScope && codemodeRequestRef.current?.scope !== workspaceScope) {
          setCodemodeSelection({ scope: workspaceScope, mode });
        }
      }
      const completedMessage = asRecord(asRecord(event.payload).message);
      if (
        event.eventType === 'message_completed'
        && completedMessage.role === 'assistant'
        && completedMessage.status === 'completed'
      ) {
        if (terminalSnapshotTimerRef.current !== undefined) {
          window.clearTimeout(terminalSnapshotTimerRef.current);
        }
        terminalSnapshotTimerRef.current = window.setTimeout(() => {
          terminalSnapshotTimerRef.current = undefined;
          if (workspaceScopeRef.current !== workspaceScope) return;
          void loadAgentSnapshotRef.current({
            preserveAfterSequence: event.sequence,
          });
        }, 350);
      }
      if (event.eventType === 'compaction_started' || event.eventType === 'compaction_completed') {
        // Standalone compaction has no turn terminal to refresh its controls.
        // Only the following current native metadata may replace the target.
        void loadAgentSnapshotRef.current({ preserveAfterSequence: event.sequence });
      }
      const runtimeWindow = runtimeToolWindow(event);
      if (runtimeWindow && shouldAutoOpenRuntimeToolWindow(runtimeWindow)) {
        desktop?.openWindow(runtimeWindow);
      }
      if (event.eventType === 'turn_completed' || event.eventType === 'turn_failed') {
        if (terminalSnapshotTimerRef.current !== undefined) {
          window.clearTimeout(terminalSnapshotTimerRef.current);
          terminalSnapshotTimerRef.current = undefined;
        }
        setStopping(false);
        if (turnStopRequestRef.current?.acknowledged) turnStopRequestRef.current = undefined;
        setError(current => current === STOP_UNCONFIRMED_TEXT ? current : '');
        void loadAgentSnapshotRef.current({
          preserveAfterSequence: event.sequence,
        });
        onSessionActivity?.();
      }
    },
    onConnectionError: (_sessionId, reason) => {
      setStopping(false);
      setHistoryRead(current => current?.scope === workspaceScope ? { ...current, expanding: false } : current);
      setSyncError(errorText(reason));
    },
    onConnectionRestored: () => { if (hasSnapshot) setSyncError(''); },
  });
  loadAgentSnapshotRef.current = loadAgentSnapshot;

  useEffect(() => () => {
    catalogRequestRef.current += 1;
    catalogAbortRef.current?.abort();
    catalogAbortRef.current = undefined;
    if (terminalSnapshotTimerRef.current !== undefined) {
      window.clearTimeout(terminalSnapshotTimerRef.current);
      terminalSnapshotTimerRef.current = undefined;
    }
  }, [liveActive, recordId, transport]);

  async function reconcileSessionForAction(): Promise<SessionSummary | undefined> {
    let canonical = record;
    try {
      const response = await transport.request({
        pathId: 'agent.sessions.list',
        query: { limit: 100, includeArchived: true },
      });
      canonical = sessionItems(response, { includeAppOwned: true })
        .find((item) => item.id === recordId) ?? canonical;
      if (canonical && canonical !== record) onSessionUpdated(canonical);
    } catch {
      // The catalog is advisory for an already-open Session. Keep using the
      // route record when the refresh endpoint is temporarily unavailable.
    }
    if (canonical) return canonical;
    try {
      const snapshot = await transport.request({
        pathId: 'agent.session.snapshot',
        params: { sessionId: recordId },
        query: { view: 'recent' },
      });
      useAgentLiveStore.getState().hydrate(address, snapshot);
      return workspaceRecord;
    } catch {
      return undefined;
    }
  }



  /* A prompt that failOptimistic just marked failed already has one recovery
     surface: the timeline's failed-turn card, carrying the same reason plus
     重试本轮 and 切换模型. Adding the workspace alert on top of it gave one
     failure two banners. The alert stays for everything no turn owns, and for
     a failure the reader cannot see because another view is on screen. */
  function turnFailureIsVisible(clientMessageId: string): boolean {
    return workspaceView === 'conversation'
      && timelineOwnsTurnFailure(agentProjection(address), clientMessageId);
  }

  /* One settle path for every prompt admission failure, shared by send and
     retry. It mirrors the standalone Agent feature: a pending/unresolved
     receipt keeps the optimistic message visible in that state (the Runtime
     may still execute it, so the input must not come back for a double send),
     an ambiguous transport loss marks the message retriable-by-verification,
     and a turn conflict returns the input instead of inventing a failed turn.
     Nothing here awaits a snapshot; recovery refreshes stay quiet. */
  function settlePromptAdmissionFailure(
    clientMessageId: string,
    reason: unknown,
    options: {
      restoreInput?: () => void;
      onAdmissionRolledBack?: () => void;
      replayAmbiguousAdmission?: boolean;
    } = {},
  ): void {
    const store = useAgentLiveStore.getState();
    if (isAgentCommandPending(reason)) {
      if (agentProjection(address).optimisticByClientMessageId[clientMessageId]) {
        store.failOptimistic(
          address,
          clientMessageId,
          errorText(reason),
          Date.now(),
          isUnresolvedAgentCommandPending(reason) ? 'unresolved' : 'pending',
        );
      }
      return;
    }
    if (isAmbiguousAgentPromptFailure(reason)) {
      store.failOptimistic(
        address,
        clientMessageId,
        '暂时无法确认是否已接收。系统不会自动重试；手动重试会核对同一条消息。',
        Date.now(),
        'ambiguous',
      );
      options.onAdmissionRolledBack?.();
      return;
    }
    const commandConflict = agentCommandReceiptFailure(reason);
    if (commandConflict?.code === 'AGENT_COMMAND_CONFLICT') {
      store.discardOptimistic(address, clientMessageId);
      options.restoreInput?.();
      options.onAdmissionRolledBack?.();
      if (isAgentTurnConflict(reason)) {
        void loadAgentSnapshot();
        setError('上一轮仍在处理，输入已保留；可以继续补充或先停止当前轮。');
      } else {
        // A fingerprint/idempotency conflict did not execute a turn. Refreshing
        // here races onSnapshot's error reset and erases the only useful retry
        // instruction, so keep the restored draft and let the next send mint a
        // fresh clientMessageId.
        setError(errorText(reason));
      }
      return;
    }
    store.failOptimistic(
      address,
      clientMessageId,
      errorText(reason),
      Date.now(),
      options.replayAmbiguousAdmission ? 'ambiguous' : undefined,
    );
    options.restoreInput?.();
    options.onAdmissionRolledBack?.();
    if (!turnFailureIsVisible(clientMessageId)) {
      setError(errorText(reason));
    }
  }

  async function resumeCurrentTask(): Promise<void> {
    if (evaluationSnapshot) return;
    const projection = agentProjection(address);
    const recovery = projection.durableRecovery;
    if (resumeRequestRef.current?.recordId === recordId && resumeRequestRef.current.transport === transport
      || sending || stopping || modelChanging || projection.needsSnapshot
      || compactionStopRequestRef.current
      || !recovery?.paused || !recovery.recoverable || !recovery.activeTurn && !recovery.compactionTarget) return;
    const target = recovery.compactionTarget;
    const turnId = recovery.activeTurn?.turnId;
    const clientMessageId = recovery.activeTurn?.clientMessageId;
    resumeRequestRef.current?.controller.abort();
    const controller = new AbortController();
    const request = { controller, recordId, transport, target: target ?? undefined };
    resumeRequestRef.current = request;
    const ownsRequest = () => !controller.signal.aborted && resumeRequestRef.current === request
      && resumeOwnerRef.current.recordId === recordId && resumeOwnerRef.current.transport === transport;
    const ownsTarget = () => ownsRequest() && (target
      ? sameAgentCompactionTarget(agentProjection(address).durableRecovery?.compactionTarget, target)
      : agentProjection(address).durableRecovery?.activeTurn?.turnId === turnId
        && agentProjection(address).durableRecovery?.activeTurn?.clientMessageId === clientMessageId);
    setResuming(true);
    setError('');
    try {
      const response = asRecord(await transport.request({
        pathId: 'agent.session.resume', params: { sessionId: recordId },
        body: target ? { compactionTarget: target } : { turnId: turnId!, clientMessageId: clientMessageId! }, signal: controller.signal,
      }));
      if (!ownsTarget()) return;
      const receipt = asRecord(response.runtimeReceipt);
      if (response.schemaVersion !== 'rag-ime.agent-session-resume.v1' || response.ok !== true
        || response.sessionId !== recordId || (target
          ? !sameAgentCompactionTarget(response.compactionTarget, target) || !validCompactionReceipt(receipt, target, 'resume')
          : response.turnId !== turnId || response.clientMessageId !== clientMessageId
            || receipt.schemaVersion !== 'rag-ime.pi-session-resume.v1' || receipt.accepted !== true
            || receipt.runtimeEngine !== 'durable' || typeof receipt.resumed !== 'boolean')) {
        throw new Error('Durable resume receipt is not confirmed');
      }
      if (target) clearCompactionStopWarning(target);
      // The ACK proves admission, not execution. Read the same Session and let
      // current native metadata/events clear pause without a new prompt/turn.
      const loaded = await loadAgentSnapshotRef.current();
      if (ownsTarget() && !loaded) setError('恢复尚未确认，原任务与进度已保留。请重新同步后再继续。');
    } catch {
      if (!ownsTarget()) return;
      await loadAgentSnapshotRef.current();
      if (ownsTarget() && agentProjection(address).durableRecovery?.paused) {
        setError('恢复尚未确认，原任务与进度已保留。可以重新同步，或再次继续当前任务。');
      }
    } finally {
      if (ownsRequest()) { resumeRequestRef.current = undefined; setResuming(false); }
    }
  }

  function acceptsEngineInput(rawDraft: string): boolean {
    if (!durableSession) return true;
    if (attachments.length) {
      setError('Pi Durable 暂不支持附件。草稿和附件已保留，请移除附件后继续。');
      return false;
    }
    if (rawDraft.trim() === '/branch') {
      setError('Pi Durable 暂不支持历史分支。草稿已保留。');
      return false;
    }
    return true;
  }

  function acceptsImmediateInput(rawDraft: string): boolean {
    if (!acceptsEngineInput(rawDraft)) return false;
    if (durablePaused || compactionTarget) {
      setError(compactionTarget ? '请先继续或停止压缩；新消息可以排到下一轮。' : '当前任务已暂停，请先继续当前任务；新消息可以排到下一轮。');
      return false;
    }
    return true;
  }

  function acceptsComposerInput(): boolean {
    // This guard is synchronous too: a file import may have started before
    // React commits the disabled button, and false keeps Composer's draft.
    return !evaluationSnapshot && workspaceScopeRef.current === workspaceScope && !sending && !modelChanging
      && !sessionActionLockRef.current && !attachmentImports.pending.size
      && !recovery.checking && !recovery.issues.length
      && !(editState && (editState.resolving || !editState.entryId));
  }

  function settleCancelledPromptAdmission(clientMessageId: string): void {
    useAgentLiveStore.getState().discardOptimistic(address, clientMessageId);
    if (workspaceScopeRef.current !== workspaceScope) return;
    const stopRequest = turnStopRequestRef.current;
    if (stopRequest?.scope === workspaceScope && stopRequest.clientMessageId === clientMessageId) {
      stopRequest.admissionCancelled = true;
      // Cancellation proves this input never became a native turn. The Stop
      // receipt still owns background-resource drain and any warning about it.
      if (stopRequest.acknowledged) {
        turnStopRequestRef.current = undefined;
        setStopping(false);
      }
    }
    void loadAgentSnapshot();
  }

  useEffect(() => {
    if (evaluationSnapshot || !initialSubmission || initialSubmissionRef.current === initialSubmission.clientMessageId
      || !hasSnapshot || attachmentImportPending || recovery.checking || recovery.issues.length || !record || sending || modelChanging) return;
    void send('prompt', initialSubmission.message, initialSubmission.message, initialSubmission.clientMessageId);
  }, [evaluationSnapshot, initialSubmission, hasSnapshot, attachmentImportPending, recovery.checking, recovery.issues.length, record, sending, modelChanging]);

  async function send(delivery: AgentMessageDelivery, rawDraft: string, displayDraft = rawDraft, initialClientMessageId?: string): Promise<void> {
    if (evaluationSnapshot) return;
    if (attachmentImports.pending.size) return;
    if (recovery.checking || recovery.issues.length) { setError('请先核实或移除恢复失败的附件。'); return; }
    if (!workspaceRecord || sending || modelChanging) return;
    if (!acceptsEngineInput(rawDraft)) return;
    const value = rawDraft.trim();
    if (editState) {
      if (editState.resolving || !editState.entryId) {
        setError('正在定位这条历史消息，请稍候。');
        return;
      }
      if (!value && !attachments.length) return;
      const message = value || '请查看附件。';
      const selectedAttachments = attachments;
      const target = editState;
      const scope = workspaceScope;
      const editRequest = ++editRequestRef.current;
      const clientMessageId = `paw-rewrite-${crypto.randomUUID()}`;
      setSending(true);
      setDraft('');
      setAttachments([]);
      setEditState(undefined);
      setError('');
      useAgentLiveStore.getState().rewriteOptimistic(address, target.messageId, {
        clientMessageId,
        text: message,
        attachments: selectedAttachments.map((item) => item.id),
        nowMs: Date.now(),
      });
      try {
        await transport.request({
          pathId: 'agent.session.rewrite',
          params: { sessionId: recordId },
          body: {
            entryId: target.entryId,
            message,
            attachments: selectedAttachments.map((item) => item.id),
            clientMessageId,
          },
        });
        /* The rewrite is accepted; rebuilding the visible history is the quiet
           snapshot's job and never holds the composer. */
        void loadAgentSnapshot();
      } catch (reason) {
        useAgentLiveStore.getState().discardOptimistic(address, clientMessageId);
        await loadAgentSnapshot().catch(() => undefined);
        let restored = false;
        recovery.recoverInput(current => {
          // A rejected rewrite still owns its original input, never a newer
          // thought. Restore text and attachments together through that owner.
          if (current.draft || current.attachments.length) return current;
          restored = true;
          return { draft: displayDraft, attachments: selectedAttachments };
        });
        if (workspaceScopeRef.current === scope && editRequestRef.current === editRequest) {
          if (restored) setEditState(target);
          setError(errorText(reason));
        }
      } finally {
        setSending(false);
      }
      return;
    }
    if (value === '/new') { setDraft(''); onNewWork(); return; }
    if (value === '/branch') {
      setDraft(''); openForkDialog(); return;
    }
    if (isCommand(value, '/name')) {
      const title = value.slice('/name'.length).trim().slice(0, 120);
      if (!title) { setError('请在 /name 后输入新的 Session 名称。'); return; }
      setSending(true);
      try {
        const response = await transport.request<Record<string, unknown>>({
          pathId: 'agent.session.rename',
          params: { sessionId: recordId },
          body: { title },
        });
        const updated = asSession(response.session) ?? { ...workspaceRecord, title, updatedAtMs: Date.now() };
        onSessionUpdated(updated);
        setDraft('');
      } catch (reason) { setError(errorText(reason)); }
      finally { setSending(false); }
      return;
    }
    if (isCommand(value, '/compact')) {
      setSending(true);
      try {
        await transport.request({
          pathId: 'agent.session.compact',
          params: { sessionId: recordId },
          body: { instructions: value.slice('/compact'.length).trim() },
        });
        setDraft('');
        await loadAgentSnapshot();
      } catch (reason) { setError(errorText(reason)); }
      finally { setSending(false); }
      return;
    }
    if (!value && !attachments.length) return;
    if (sessionActionLockRef.current) return;
    sessionActionLockRef.current = true;
    const message = value || '请查看附件。';
    const selectedAttachments = attachments;
    const clientMessageId = initialClientMessageId ?? `paw-${crypto.randomUUID()}`;
    if (initialClientMessageId) initialSubmissionRef.current = initialClientMessageId;
    const selectedScreenContext = screenContextForMessage(screenContext, selectedAttachments.map((item) => item.id), agentProjection(address));
    const effectiveDelivery: AgentMessageDelivery = busy
      ? (delivery === 'followUp' ? 'followUp' : 'steer')
      : 'prompt';
    setSending(true);
    setDraft('');
    setAttachments([]);
    setError('');
    /* Submitting is a claim on the end of the transcript. Without this a reader
       who had scrolled up to check an earlier turn watched their own message
       land off-screen with no sign it was accepted. */
    setScrollToLatestRequest((value) => value + 1);
    useAgentLiveStore.getState().appendOptimistic(address, {
      clientMessageId,
      text: message,
      attachments: selectedAttachments.map((item) => item.id),
      nowMs: Date.now(),
      ...(effectiveDelivery === 'prompt'
        ? {}
        : { turnId: latestActiveTurnId(agentProjection(address)), delivery: effectiveDelivery }),
    });
    /* The input only comes back if the reader has not already started the next
       thought; a fresh draft never gets clobbered by an old failure. */
    const restoreInput = (): void => {
      recovery.recoverInput(current => ({
        draft: current.draft.trim() ? current.draft : displayDraft.trim(),
        attachments: current.attachments.length ? current.attachments : selectedAttachments,
      }));
    };
    // Admission and the optimistic turn are synchronous. Catalog reconciliation,
    // restoring a Pi Session, or starting a Provider can still make the receipt
    // slow, but must not make the click itself feel stalled.
    void (async () => {
      try {
        // An open workspace already owns a concrete Session record. Its prompt
        // endpoint is the authority for admission; waiting on the paged catalog
        // here can strand a valid send behind an unrelated refresh. Only a
        // provisional deep link needs recovery before its first action.
        if (!record) {
          const actionRecord = await reconcileSessionForAction();
          if (!actionRecord) {
            useAgentLiveStore.getState().discardOptimistic(address, clientMessageId);
            restoreInput();
            setError('当前 Session 暂时无法确认，请重新打开后再发送。');
            return;
          }
        }
        const response = await transport.request<Record<string, unknown>>({
          pathId: 'agent.session.prompt',
          params: { sessionId: recordId },
          body: {
            message,
            attachments: selectedAttachments.map((item) => item.id),
            clientMessageId,
            ...(selectedScreenContext ? { screenContext: selectedScreenContext } : {}),
            ...(effectiveDelivery === 'prompt' ? {} : { delivery: effectiveDelivery }),
          },
        });
        if (isCancelledPromptAdmission(response)) {
          settleCancelledPromptAdmission(clientMessageId);
          return;
        }
        useAgentLiveStore.getState().acknowledgeOptimistic(address, clientMessageId, Date.now());
        void loadAgentSnapshot();
      } catch (reason) {
        if (effectiveDelivery !== 'prompt' && isAgentSessionIdleFailure(reason)) {
          // The projection can be one terminal event behind the Runtime. If a
          // message was auto-routed as Steer/Follow-up but Pi proves the turn
          // is already idle, the rejected receipt is safe to supersede once
          // as a new prompt. Keep explicit lineage; never replay an unknown or
          // pending admission.
          useAgentLiveStore.getState().discardOptimistic(address, clientMessageId);
          const retryClientMessageId = `paw-retry-${crypto.randomUUID()}`;
          useAgentLiveStore.getState().appendOptimistic(address, {
            clientMessageId: retryClientMessageId,
            text: message,
            attachments: selectedAttachments.map((item) => item.id),
            nowMs: Date.now(),
          });
          try {
            const retryResponse = await transport.request<Record<string, unknown>>({
              pathId: 'agent.session.prompt',
              params: { sessionId: recordId },
              body: {
                message,
                attachments: selectedAttachments.map((item) => item.id),
                clientMessageId: retryClientMessageId,
                ...(selectedScreenContext ? { screenContext: selectedScreenContext } : {}),
              },
            });
            if (isCancelledPromptAdmission(retryResponse)) {
              settleCancelledPromptAdmission(retryClientMessageId);
              return;
            }
            useAgentLiveStore.getState().acknowledgeOptimistic(address, retryClientMessageId, Date.now());
            void loadAgentSnapshot();
          } catch (retryReason) {
            settlePromptAdmissionFailure(retryClientMessageId, retryReason, { restoreInput });
          }
          return;
        }
        settlePromptAdmissionFailure(clientMessageId, reason, { restoreInput });
      } finally {
        setSending(false);
        sessionActionLockRef.current = false;
      }
    })();
  }

  function clearCompactionStopWarning(target: AgentCompactionTarget): void {
    setCompactionStopWarnings(current => {
      const warning = current.get(recordId);
      if (warning?.transport !== transport || !sameAgentCompactionTarget(warning.target, target)) return current;
      const next = new Map(current);
      next.delete(recordId);
      return next;
    });
  }

  function retainCompactionStopWarning(target: AgentCompactionTarget): void {
    setCompactionStopWarnings(current => new Map(current).set(recordId, { transport, target }));
  }

  async function stopCompaction(target: AgentCompactionTarget): Promise<void> {
    if (compactionStopRequestRef.current || resumeRequestRef.current || stopping) return;
    const controller = new AbortController();
    const request = { controller, recordId, transport, target };
    compactionStopRequestRef.current = request;
    const ownsRequest = () => !controller.signal.aborted && compactionStopRequestRef.current === request
      && resumeOwnerRef.current.recordId === recordId && resumeOwnerRef.current.transport === transport;
    const ownsTarget = () => ownsRequest()
      && sameAgentCompactionTarget(agentProjection(address).durableRecovery?.compactionTarget, target);
    setStopping(true);
    clearCompactionStopWarning(target);
    setError('');
    if (queue.queue.length) setDraft(current => queue.restoreToDraft(current));
    try {
      const response = asRecord(await transport.request({ pathId: 'agent.session.abort',
        params: { sessionId: recordId }, body: { compactionTarget: target }, signal: controller.signal }));
      if (!ownsTarget()) return;
      if (response.schemaVersion !== 'rag-ime.agent-abort.v1' || response.ok !== true || response.sessionId !== recordId
        || !sameAgentCompactionTarget(response.compactionTarget, target)
        || !validCompactionReceipt(asRecord(response.runtimeReceipt), target, 'abort')) {
        throw new Error(STOP_UNCONFIRMED_TEXT);
      }
      // A terminal receipt confirms these tasks only. Current native metadata
      // clears the control target; no user turn is fabricated or settled here.
      const loaded = await loadAgentSnapshotRef.current();
      if (ownsTarget() && !loaded) retainCompactionStopWarning(target);
    } catch {
      if (!ownsTarget()) return;
      await loadAgentSnapshotRef.current();
      if (ownsTarget()) retainCompactionStopWarning(target);
    } finally {
      if (ownsRequest()) { compactionStopRequestRef.current = undefined; setStopping(false); }
    }
  }

  async function stop(): Promise<void> {
    if (evaluationSnapshot) return;
    const projection = agentProjection(address);
    const target = projection.durableRecovery?.compactionTarget;
    if (target) { await stopCompaction(target); return; }
    if (!busy || stopping) return;
    const activeTurnId = latestActiveTurnId(projection);
    const pendingAdmission = activeTurnId.startsWith('local-turn:')
      ? resolveAgentTurnUserMessage(projection, activeTurnId) : undefined;
    const request = { scope: workspaceScope, clientMessageId: pendingAdmission?.clientMessageId,
      acknowledged: false, admissionCancelled: false };
    turnStopRequestRef.current = request;
    const ownsRequest = () => turnStopRequestRef.current === request && workspaceScopeRef.current === workspaceScope;
    setStopping(true);
    /* Stopping the turn cancels the intent behind everything held for it, so
       the drafts come back to the composer instead of firing into a Session
       the reader just interrupted. */
    if (queue.queue.length) setDraft((current) => queue.restoreToDraft(current));
    try {
      const receipt = asRecord(await transport.request({ pathId: 'agent.session.abort', params: { sessionId: recordId }, body: {} }));
      if (!ownsRequest()) return;
      const jobs = asRecord(receipt.backgroundJobs);
      if (receipt.ok === false || jobs.drained === false
        || Array.isArray(jobs.pendingJobIds) && jobs.pendingJobIds.length > 0) {
        turnStopRequestRef.current = undefined;
        setStopping(false);
        setError(STOP_UNCONFIRMED_TEXT);
        return;
      }
      request.acknowledged = true;
      const runtimeReceipt = asRecord(receipt.runtimeReceipt);
      // Stop can win before Pi creates a turn, so no terminal event exists.
      // Either exact prompt cancellation or the captured admission's Stop
      // receipt settles that local lock; an ordinary turn still waits for SSE.
      if (request.admissionCancelled || (request.clientMessageId
        && runtimeReceipt.pendingAdmission === true && runtimeReceipt.admissionCancelled === true)) {
        turnStopRequestRef.current = undefined;
        setStopping(false);
      } else if (!request.clientMessageId) turnStopRequestRef.current = undefined;
      setError('');
    } catch (reason) {
      if (!ownsRequest()) return;
      turnStopRequestRef.current = undefined;
      setStopping(false);
      setError(errorText(reason));
    }
  }

  function retryTurn(turnId: string, onAdmissionRolledBack?: () => void): boolean {
    if (evaluationSnapshot || !workspaceRecord || sending || busy || sessionActionLockRef.current) return false;
    sessionActionLockRef.current = true;
    void (async () => {
      try {
        const actionRecord = await reconcileSessionForAction();
        if (!actionRecord) {
          setError('当前 Session 暂时无法确认，请重新打开后再重试。');
          onAdmissionRolledBack?.();
          return;
        }
        let current = agentProjection(address);
        let userMessage = resolveAgentTurnUserMessage(current, turnId);
        if (!userMessage) {
          try {
            const snapshot = await transport.request({
              pathId: 'agent.session.snapshot',
              params: { sessionId: recordId },
            });
            useAgentLiveStore.getState().hydrate(address, snapshot);
            current = agentProjection(address);
            userMessage = resolveAgentTurnUserMessage(current, turnId);
          } catch {
            // Keep the rendered failure available when a quiet resync is
            // unavailable; the resolver can still use the current projection.
          }
        }
        if (!userMessage) {
          setError('找不到这轮的原始输入，无法安全重试。');
          onAdmissionRolledBack?.();
          return;
        }
        if (hasUndurableAgentAttachments(userMessage.attachments)) {
          setError('附件未能导入，当前失败卡不能安全重试。请重新上传附件后发送。');
          onAdmissionRolledBack?.();
          return;
        }
        if (userMessage.admissionState === 'pending' || userMessage.admissionState === 'unresolved') {
          setError('这条消息仍无法确认是否已执行；为避免重复执行，不能自动重试。请先重新同步 Session。');
          onAdmissionRolledBack?.();
          return;
        }
        if (latestActiveTurnId(current)) {
          onAdmissionRolledBack?.();
          return;
        }
        const message = userMessage.blocks
          .map((block) => typeof block.data.text === 'string' ? block.data.text : '')
          .filter(Boolean)
          .join('\n')
          .trim();
        if (!message && !userMessage.attachments.length) {
          setError('找不到这轮的原始输入，无法安全重试。');
          onAdmissionRolledBack?.();
          return;
        }
        replayTurnMessage(
          userMessage,
          message || '请查看附件。',
          onAdmissionRolledBack,
        );
      } catch (reason) {
        setError(errorText(reason));
        onAdmissionRolledBack?.();
      } finally {
        sessionActionLockRef.current = false;
      }
    })();
    return true;
  }

  function replayTurnMessage(
    userMessage: AgentMessageProjection,
    message: string,
    onAdmissionRolledBack?: () => void,
  ): boolean {
    const current = agentProjection(address);
    const selectedScreenContext = screenContextForMessage(screenContext, userMessage.attachments, current);
    // A durable Runtime message proves the original command was accepted; a
    // later Provider/Tool turn failure is a new execution attempt, not a
    // successor to a failed command receipt. Only the local optimistic row
    // retained after a pre-accept rejection may use receipt retry lineage.
    const mayRetryFailedReceipt = userMessage.id.startsWith('local:')
      && Boolean(userMessage.clientMessageId);
    const hasRetrySuccessor = mayRetryFailedReceipt && current.messageOrder.some((messageId) => {
      const candidate = current.messagesById[messageId];
      return candidate?.role === 'user'
        && candidate.retryOfClientMessageId === userMessage.clientMessageId;
    });
    if (hasRetrySuccessor) {
      // The receipt store permits one successor per failed command. A stale
      // timeline row or a double click must not submit a sibling with the same
      // retryOfClientMessageId and turn into AGENT_COMMAND_CONFLICT.
      setError('这轮已有重试请求，等待它完成后再继续。');
      onAdmissionRolledBack?.();
      return false;
    }
    const replayAmbiguousAdmission = userMessage.admissionState === 'ambiguous' && Boolean(userMessage.clientMessageId);
    const originalDelivery = agentMessageDelivery(userMessage);
    const clientMessageId = replayAmbiguousAdmission
      ? userMessage.clientMessageId!
      : `paw-retry-${crypto.randomUUID()}`;
    const retryOfClientMessageId = replayAmbiguousAdmission
      || !mayRetryFailedReceipt
      || originalDelivery !== 'prompt'
      ? ''
      : userMessage.clientMessageId ?? '';
    setSending(true);
    setError('');
    if (replayAmbiguousAdmission) {
      useAgentLiveStore.getState().requeueOptimistic(address, clientMessageId, Date.now());
    } else {
      useAgentLiveStore.getState().appendOptimistic(address, {
        clientMessageId,
        ...(retryOfClientMessageId ? { retryOfClientMessageId } : {}),
        text: message,
        attachments: userMessage.attachments,
        nowMs: Date.now(),
      });
    }
    // Same admission contract as send: the retry click settles synchronously,
    // the HTTP receipt releases the composer, the snapshot refresh stays quiet.
    void (async () => {
      try {
        const response = await transport.request<Record<string, unknown>>({
          pathId: 'agent.session.prompt',
          params: { sessionId: recordId },
          body: {
            message,
            attachments: userMessage.attachments,
            clientMessageId,
            ...(selectedScreenContext ? { screenContext: selectedScreenContext } : {}),
            ...(retryOfClientMessageId ? { retryOfClientMessageId } : {}),
            ...(replayAmbiguousAdmission && originalDelivery !== 'prompt'
              ? { delivery: originalDelivery }
              : {}),
          },
        });
        if (isCancelledPromptAdmission(response)) {
          settleCancelledPromptAdmission(clientMessageId);
          return;
        }
        useAgentLiveStore.getState().acknowledgeOptimistic(address, clientMessageId, Date.now());
        void loadAgentSnapshot();
      } catch (reason) {
        settlePromptAdmissionFailure(clientMessageId, reason, {
          onAdmissionRolledBack,
          replayAmbiguousAdmission,
        });
      } finally {
        setSending(false);
      }
    })();
    return true;
  }

  function continueTurn(turnId: string): boolean {
    if (evaluationSnapshot) return false;
    const current = agentProjection(address);
    if (current.turnOrder.at(-1) !== turnId || current.turnsById[turnId]?.status !== 'failed') return false;
    void send('prompt', '继续。请基于当前 Session 已保留的工具结果和文件生成最终回复，不要重试或重复已经完成的操作；如果仍缺少信息，明确说明下一步。');
    return true;
  }

  function openForkDialog(initialEntryId = ''): void {
    if (evaluationSnapshot) return;
    if (durableSession) { setError('Pi Durable 暂不支持历史分支。'); return; }
    setForkDialogNodes(conversationNodes(agentProjection(address)));
    setForkDialogInitialEntryId(initialEntryId);
    setForkDialogOpen(true);
  }

  async function beginEditMessage(messageId = ''): Promise<void> {
    if (evaluationSnapshot) return;
    if (!record || busy || sending || !classicHistoryAvailable || !conversationRewriteAvailable || record.roomParticipant) {
      setError(durableSession ? 'Pi Durable 暂不支持历史改写。' : record?.roomParticipant
        ? '这段对话属于 Room 伙伴，历史修改由 Room 管理。'
        : conversationRewriteAvailable
          ? '请等待当前回复结束后再修改历史消息。'
          : '当前 Pi Runtime 尚未提供原位修改能力。');
      return;
    }
    const current = agentProjection(address);
    const message = messageId
      ? current.messagesById[messageId]
      : [...current.messageOrder].reverse().map((id) => current.messagesById[id])
        .find((item) => item?.role === 'user' && !item.id.startsWith('local:'));
    if (!message || message.role !== 'user' || message.id.startsWith('local:')) {
      setError('当前对话里没有可修改的上一条用户消息。');
      return;
    }
    const text = conversationText(message.blocks);
    if (!text && !message.attachments.length) {
      setError('这条消息没有可编辑的公开内容。');
      return;
    }
    const originalAttachments: ComposerAttachment[] = message.attachments.map((id, index) => ({
      id,
      name: `原附件 ${index + 1}`,
      mimeType: '',
      byteSize: 0,
      source: 'path',
    }));
    const scope = workspaceScope;
    const request = ++editRequestRef.current;
    const ownsEdit = () => workspaceScopeRef.current === scope && editRequestRef.current === request;
    cancelAttachmentImports();
    setDraft(text);
    setAttachments(originalAttachments);
    setEditState({ entryId: '', messageId: message.id, resolving: true });
    setJumpRequest({ messageId: message.id, requestId: Date.now() });
    setError('');
    try {
      const response = await transport.request<Record<string, unknown>>({
        pathId: 'agent.session.forks.list',
        params: { sessionId: recordId },
      });
      if (!ownsEdit()) return;
      const entryId = resolveConversationEntryId(response, conversationNodes(current), message.id);
      if (!entryId) throw new Error('Pi 没有返回这条公开消息对应的可回溯锚点。');
      setEditState({ entryId, messageId: message.id });
    } catch (reason) {
      if (!ownsEdit()) return;
      setEditState(undefined);
      setDraft('');
      setAttachments([]);
      setError(errorText(reason));
    }
  }

  function cancelEdit(): void {
    editRequestRef.current += 1;
    cancelAttachmentImports();
    setEditState(undefined);
    setDraft('');
    setAttachments([]);
  }

  async function decideApproval(approvalId: string, decision: 'approved' | 'rejected', payloadSha256: string): Promise<void> {
    if (evaluationSnapshot) return;
    try {
      await transport.request({
        pathId: 'agent.approval.decide',
        params: { approvalId },
        body: { decision: decision === 'approved' ? 'approve' : 'reject', payloadSha256 },
      });
      setRequestedApproval(undefined);
      await loadAgentSnapshot();
    } catch (reason) {
      setError(errorText(reason));
      throw reason;
    }
  }

  function cancelAttachmentImports(): void {
    if (!attachmentImports.pending.size) return;
    attachmentImports.pending.clear();
    refreshAttachmentImports(value => value + 1);
  }

  function beginAttachmentImport() {
    const scope = workspaceScope;
    const token = Symbol('attachment-import');
    attachmentImports.pending.add(token);
    refreshAttachmentImports(value => value + 1);
    setAttachmentError('');
    return {
      isCurrent: () => workspaceScopeRef.current === scope && attachmentImports.active && attachmentImports.pending.has(token),
      finish: () => {
        if (!attachmentImports.pending.delete(token)) return;
        if (workspaceScopeRef.current === scope && attachmentImports.active) refreshAttachmentImports(value => value + 1);
      },
    };
  }

  async function pickAttachments(): Promise<void> {
    if (durableSession) { setAttachmentError('Pi Durable 暂不支持附件。'); return; }
    if (!transport.pickFiles) { setAttachmentError('当前环境不能选择附件，请将文件放入项目后告诉 Agent 文件名。'); return; }
    if (attachments.length >= 8) { setAttachmentError('最多添加 8 个附件，请先移除已有附件。'); return; }
    const request = beginAttachmentImport();
    try {
      const imported = await transport.pickFiles({
        multiple: true,
        purpose: 'attachment',
        sessionId: recordId,
        maxFiles: Math.max(1, 8 - attachments.length),
      });
      if (!request.isCurrent()) return;
      setAttachments((current) => mergeAttachments(current, imported.map((item) => ({ ...item, source: 'picker' as const }))));
    } catch (reason) { if (request.isCurrent()) setAttachmentError(attachmentImportErrorText(reason)); }
    finally { request.finish(); }
  }

  async function pasteFiles(files?: File[]): Promise<boolean> {
    if (durableSession) { setAttachmentError('Pi Durable 暂不支持附件。'); return false; }
    if (!transport.pasteImages) { setAttachmentError('未能读取剪贴板文件，请改用选择附件。'); return false; }
    if (attachments.length >= 8) { setAttachmentError('最多添加 8 个附件，请先移除已有附件。'); return false; }
    const request = beginAttachmentImport();
    try {
      const imported = await transport.pasteImages({ sessionId: recordId, ...(files?.length ? { files } : {}), maxFiles: Math.max(1, 8 - attachments.length) });
      if (!request.isCurrent()) return false;
      // Browser transports echo the pasted bytes back as receipts; reusing the
      // local File gives image chips an instant thumbnail before upload settles.
      setAttachments((current) => mergeAttachments(current, imported.map((item, index) => {
        const file = files?.[index];
        const previewFile = file
          && file.name === item.name
          && file.size === item.byteSize
          && transport.kind !== 'native'
          ? { previewFile: file }
          : {};
        return { ...item, source: 'clipboard' as const, ...previewFile };
      })));
      return imported.length > 0;
    } catch (reason) { if (request.isCurrent()) setAttachmentError(attachmentImportErrorText(reason)); return false; }
    finally { request.finish(); }
  }

  async function changePermission(selection: AgentPermissionSelection): Promise<void> {
    if (evaluationSnapshot) return;
    if (!record || busy) { setError('请先停止当前回合，再调整运行权限。'); return; }
    const scope = workspaceScope;
    const isCurrent = () => workspaceScopeRef.current === scope;
    try {
      const scopedWorkspaceRoots = (selection.workspaceRoots ?? record.workspaceRoots ?? [])
        .filter((root) => root !== '/');
      const workspaceRoots = selection.executionMode === 'per_action'
        || selection.executionMode === 'full_trust'
        ? unrestrictedWorkspaceRoots(...scopedWorkspaceRoots)
        : scopedWorkspaceRoots;
      const response = await transport.request<Record<string, unknown>>({
        pathId: 'agent.session.mode.update',
        params: { sessionId: recordId },
        body: {
          mode: selection.mode,
          executionMode: selection.executionMode,
          workspaceRoots,
          toolProfileVersion: selection.toolProfileVersion,
          toolAllowlistMode: 'profile',
          ...(selection.workspaceScopeConfirmed
            ? { workspaceScopeConfirmation: 'APPROVE_WORKSPACE_SCOPE' }
            : {}),
          ...(selection.executionMode === 'full_trust'
            ? { dangerousModeConfirmation: 'ENABLE_FULL_TRUST' }
            : {}),
        },
      });
      if (!isCurrent()) return;
      const updated = asSession(response.session);
      if (updated) onSessionUpdated(updated);
      await loadControlCatalog(true);
      if (isCurrent()) setError('');
    } catch (reason) { if (isCurrent()) setError(errorText(reason)); }
  }

  async function manageWorkspaceRoots(): Promise<void> {
    if (!record || (!transport.pickFiles && !electronHost?.pickWorkspaceDirectory)) {
      setError('当前环境不能选择起始项目。');
      return;
    }
    const scope = workspaceScope;
    const isCurrent = () => workspaceScopeRef.current === scope;
    try {
      const selectedRoots = transport.pickFiles
        ? (await transport.pickFiles({ purpose: 'workspace-root', selection: 'directory', multiple: true, maxFiles: 4 }))
          .map((item) => item.path?.trim())
          .filter((path): path is string => Boolean(path))
        : [(await electronHost?.pickWorkspaceDirectory?.())?.path?.trim()]
          .filter((path): path is string => Boolean(path));
      // A picker result is still an unsubmitted UI choice. Navigation retires
      // it; an already-sent mutation below remains owned by its original A.
      if (!isCurrent() || !selectedRoots.length) return;
      const executionMode = record.executionMode ?? 'per_action';
      const unrestricted = executionMode === 'per_action' || executionMode === 'full_trust';
      const workspaceRoots = unrestricted
        ? unrestrictedWorkspaceRoots(...selectedRoots)
        : selectedRoots;
      const toolProfileVersion = executionMode === 'full_trust'
        ? 'control-center-auto-approve-v1'
        : executionMode === 'per_action'
          ? 'control-center-full-access-v1'
          : record.toolProfileVersion ?? 'control-center-v1';
      const response = await transport.request<Record<string, unknown>>({
        pathId: 'agent.session.mode.update',
        params: { sessionId: recordId },
        body: {
          mode: 'coordinator',
          executionMode,
          workspaceRoots,
          toolProfileVersion,
          toolAllowlistMode: 'profile',
          ...(executionMode === 'workspace_managed'
            ? { workspaceScopeConfirmation: 'APPROVE_WORKSPACE_SCOPE' }
            : {}),
          ...(executionMode === 'full_trust'
            ? { dangerousModeConfirmation: 'ENABLE_FULL_TRUST' }
            : {}),
        },
      });
      if (!isCurrent()) return;
      const updated = asSession(response.session);
      if (updated) onSessionUpdated(updated);
      await loadControlCatalog(true);
      if (!isCurrent()) return;
      await loadAgentSnapshot();
      if (isCurrent()) setError('');
    } catch (reason) { if (isCurrent()) setError(errorText(reason)); }
  }

  async function changeCodemodeMode(mode: CodemodeMode): Promise<void> {
    if (evaluationSnapshot || busy || sending || codemodeMode === undefined || mode === codemodeMode
      || codemodeRequestRef.current?.scope === workspaceScope) return;
    const request = { scope: workspaceScope };
    codemodeRequestRef.current = request;
    setCodemodeChangeRequest(request);
    const isCurrent = () => workspaceScopeRef.current === request.scope && codemodeRequestRef.current === request;
    try {
      const value = await transport.request({
        pathId: 'agent.session.codemode.select', params: { sessionId: recordId }, body: { mode },
      });
      if (!isCurrent()) return;
      const receipt = parseContract('agent-session-codemode-selection.v1', value);
      if (receipt.sessionId !== recordId || !receipt.capability.available
        || !receipt.capability.modes.includes(receipt.codemodeMode)) {
        throw new Error('代码执行编排方式未获得当前对话的确认。');
      }
      setCodemodeSelection({ scope: workspaceScope, mode: receipt.codemodeMode });
      setError('');
    } catch (reason) {
      if (isCurrent()) setError(errorText(reason));
    } finally {
      if (codemodeRequestRef.current === request) codemodeRequestRef.current = undefined;
      setCodemodeChangeRequest(current => current === request ? undefined : current);
    }
  }

  async function changeModel(provider: string, modelId: string, level: ThinkingLevel): Promise<void> {
    if (evaluationSnapshot) return;
    const request = { scope: workspaceScope };
    const isCurrent = () => workspaceScopeRef.current === request.scope;
    setModelChangeRequest(request);
    try {
      await transport.request({ pathId: 'agent.session.model.select', params: { sessionId: recordId }, body: { provider, modelId } });
      // Finish the already-authorized selection against the captured target,
      // even if another workspace now owns the visible composer.
      await transport.request({ pathId: 'agent.session.thinking.select', params: { sessionId: recordId }, body: { level } });
      if (!isCurrent()) return;
      const refreshed = await transport.request({ pathId: 'agent.session.models', params: { sessionId: recordId } });
      if (!isCurrent()) return;
      if (isModelCatalog(refreshed)) setCatalog(refreshed);
      setError('');
    } catch (reason) { if (isCurrent()) setError(errorText(reason)); }
    finally { setModelChangeRequest(current => current === request ? undefined : current); }
  }

  async function changeCapabilityPreference(canonicalId: string, preference: CapabilityPreference): Promise<void> {
    if (!capabilityCatalog?.sessionPolicy) return;
    const scope = workspaceScope;
    const isCurrent = () => workspaceScopeRef.current === scope;
    setCapabilityMutation({ canonicalId, preference, status: 'pending', message: '正在更新当前 Session 的能力披露。' });
    try {
      await transport.request({
        pathId: 'agent.session.capability-policy.update',
        params: { sessionId: recordId },
        body: {
          capabilityDisclosurePreferences: {
            ...capabilityCatalog.sessionPolicy.disclosurePreferences.session,
            [canonicalId]: preference,
          },
        },
      });
      if (!isCurrent()) return;
      await prepareCatalogRefresh(catalogQueryClient, [pluginQueryKeys.catalog(transport, recordId)])();
      const next = await readCatalogQuery(catalogQueryClient, capabilityCatalogQueryOptions(transport, recordId));
      if (!isCurrent()) return;
      setCapabilityCatalog(next);
      setTools(toolItems(next));
      const updated = next.items.find((item) => item.canonicalId === canonicalId);
      setCapabilityMutation({
        canonicalId,
        preference,
        status: 'succeeded',
        message: `已按${capabilityScopeLabel(updated?.effectiveScope ?? 'session')}范围更新。`,
      });
    } catch (reason) {
      if (isCurrent()) setCapabilityMutation({ canonicalId, preference, status: 'failed', message: errorText(reason) });
    }
  }

  function runProductCommand(command: AgentProductCommandName): void {
    setToolMenuOpen(false);
    if (command === 'new') onNewWork();
    else if (command === 'resume') setPanel('none');
    else if (command === 'branch') openForkDialog();
    else if (command === 'model') setModelPickerRequest((value) => value + 1);
    else if (command === 'thinking') setThinkingPickerRequest((value) => value + 1);
    else if (command === 'permissions') setPermissionPickerRequest((value) => value + 1);
    else if (command === 'tools') { setToolPickerQuery(''); setToolPickerRequest((value) => value + 1); }
    else if (command === 'status' || command === 'session') setPanel('status');
    else if (command === 'subagents') setPanel('subagents');
    else if (command === 'stop') void stop();
    else if (command === 'settings') openPawOsRoute(desktop, '/configuration');
    else if (command === 'help' || command === 'hotkeys') setHelpRequest((value) => value + 1);
  }

  const closeToolMenu = useCallback((restoreFocus = false): void => {
    if (restoreFocus) toolMenuButtonRef.current?.focus();
    setToolMenuOpen(false);
  }, []);
  const closeToolPanel = useCallback((): void => {
    setPanel('none');
    requestAnimationFrame(() => {
      const trigger = toolPanelReturnFocusRef.current ?? toolMenuButtonRef.current;
      if (trigger?.isConnected) trigger.focus({ preventScroll: true });
    });
  }, []);

  const openToolMenu = useCallback((initialFocus: 'first' | 'last' = 'first'): void => {
    toolMenuInitialFocusRef.current = initialFocus;
    setToolMenuOpen(true);
  }, []);

  useEffect(() => {
    if (!toolMenuOpen) return;
    const items = Array.from(toolMenuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? []);
    if (!items.length) return;
    const index = toolMenuInitialFocusRef.current === 'last' ? items.length - 1 : 0;
    items[index]?.focus();
  }, [toolMenuOpen]);

  useEffect(() => {
    if (!toolMenuOpen) return;
    const handlePointerDown = (event: PointerEvent): void => {
      const target = event.target;
      if (!(target instanceof Node) || !toolMenuContainerRef.current?.contains(target)) {
        closeToolMenu(false);
      }
    };
    document.addEventListener('pointerdown', handlePointerDown);
    return () => document.removeEventListener('pointerdown', handlePointerDown);
  }, [closeToolMenu, toolMenuOpen]);

  function moveToolMenuFocus(direction: 'next' | 'previous' | 'first' | 'last'): void {
    const items = Array.from(toolMenuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? []);
    if (!items.length) return;
    const currentIndex = items.indexOf(document.activeElement as HTMLButtonElement);
    const index = direction === 'first'
      ? 0
      : direction === 'last'
        ? items.length - 1
        : direction === 'next'
          ? (currentIndex + 1 + items.length) % items.length
          : (currentIndex - 1 + items.length) % items.length;
    items[index]?.focus();
  }

  function handleToolMenuKeyDown(event: KeyboardEvent<HTMLElement>): void {
    if (event.key === 'Escape') {
      event.preventDefault();
      closeToolMenu(true);
      return;
    }
    // Let the browser advance to the next focusable control. The menu closes
    // from blur, so Tab never becomes an accidental focus trap.
    if (event.key === 'Tab') return;
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      moveToolMenuFocus('next');
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      moveToolMenuFocus('previous');
    } else if (event.key === 'Home') {
      event.preventDefault();
      moveToolMenuFocus('first');
    } else if (event.key === 'End') {
      event.preventDefault();
      moveToolMenuFocus('last');
    }
  }

  function handleToolMenuBlur(event: FocusEvent<HTMLElement>): void {
    const nextTarget = event.relatedTarget;
    if (!(nextTarget instanceof Node) || !toolMenuContainerRef.current?.contains(nextTarget)) {
      closeToolMenu(false);
    }
  }

  function handleToolMenuButtonKeyDown(event: KeyboardEvent<HTMLButtonElement>): void {
    if (event.key === 'Escape' && toolMenuOpen) {
      event.preventDefault();
      closeToolMenu(true);
      return;
    }
    if (event.key === 'Tab' && toolMenuOpen) {
      // Do not cancel Tab; closing here lets the browser keep its normal tab
      // order even when Shift+Tab returned focus to the trigger first.
      closeToolMenu(false);
      return;
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (!toolMenuOpen) {
        openToolMenu(event.key === 'ArrowUp' ? 'last' : 'first');
      } else {
        moveToolMenuFocus(event.key === 'ArrowUp' ? 'previous' : 'next');
      }
    }
  }

  function openToolPanel(next: Exclude<WorkbenchPanel, 'none'>, trigger?: HTMLButtonElement): void {
    toolPanelReturnFocusRef.current = trigger ?? toolMenuButtonRef.current;
    if (next === 'status') setStatusPanelVisited(true);
    setPanel(next);
    closeToolMenu(true);
  }

  // Full history is a low-frequency read. The window caption keeps only its
  // view/status/tools; the existing tools menu owns this labelled action.
  const historyLoadControl = !evaluationSnapshot ? (
    <button
      aria-label="加载完整记录"
      aria-busy={contextSnapshotState === 'restoring' || undefined}
      className="paw-session-history-load"
      disabled={contextSnapshotState === 'restoring'}
      onClick={() => { if (windowChromeTarget) closeToolMenu(true); void loadFullSnapshot(); }}
      role={windowChromeTarget ? 'menuitem' : undefined}
      title={contextSnapshotState === 'restoring' ? '正在加载完整记录' : '加载完整记录'}
      type="button"
    >
      {contextSnapshotState === 'restoring'
        ? <LoaderCircle className="ui-spin" size={15} />
        : <History size={15} />}
      <span>加载完整记录</span>
    </button>
  ) : null;

  const title = workspaceRecord.title || '未命名 Session';
  const sessionChrome = (
      <div className="paw-session-workspace__header" data-controls-expanded={controlsExpanded} data-status={stopping ? 'stopping' : durablePaused ? 'paused' : busy ? 'busy' : 'idle'}>
        {!windowChromeTarget ? <div className="paw-session-workspace__identity">
          <div>
            <span className="paw-session-workspace__breadcrumb"><small>Agent</small><i>/</i><strong>{title}</strong></span>
            <small>Session · {workspaceRecord.mode === 'coordinator' ? '协调' : '单聊'}</small>
          </div>
        </div> : null}
        {!evaluationSnapshot ? <Popover open={controlsExpanded} onOpenChange={setControlsExpanded}>
          <PopoverTrigger asChild><button className="paw-chat-controls-toggle" type="button" aria-expanded={controlsExpanded} aria-label={controlsExpanded ? '收起对话控件' : '展开对话控件'}><ChevronDown size={15} /><span>视图</span></button></PopoverTrigger>
          <PopoverContent align="end" className="paw-session-view-popover" aria-label="对话视图">
            <nav aria-label="当前 Session 视图" className="paw-session-workspace__view-switch">
              <button aria-label="对话" aria-pressed={workspaceView === 'conversation'} onClick={() => { setWorkspaceView('conversation'); setPanel('none'); setControlsExpanded(false); }} type="button"><MessageSquare size={15} /><span>对话</span></button>
              <button aria-label="Agent 轨迹" aria-pressed={workspaceView === 'trace'} onClick={() => { setWorkspaceView('trace'); setPanel('none'); setControlsExpanded(false); }} type="button"><GitBranch size={15} /><span>Agent 轨迹</span></button>
              <button aria-label="星空" aria-pressed={workspaceView === 'starfield'} onClick={() => { setWorkspaceView('starfield'); setPanel('none'); setControlsExpanded(false); }} type="button"><Orbit size={15} /><span>星空</span></button>
            </nav>
            <ChatPresentationSettings />
          </PopoverContent>
        </Popover> : <span className="paw-session-workspace__snapshot-label"><ShieldCheck size={14} />评测快照</span>}
        <div className="paw-session-workspace__runtime">
          <span data-context={contextSnapshotState}><i />{evaluationSnapshot
            ? '只读证据'
            : syncError && syncState !== 'synced'
              ? '正在恢复连接'
            : stopping
            ? '正在停止'
            : durablePaused
              ? '已暂停'
            : busy
              ? '正在执行'
            : contextSnapshotState === 'restoring'
              ? '正在加载完整记录'
              : contextSnapshotState === 'partial'
                ? '最近消息'
                : '已同步'}</span>
          {!windowChromeTarget ? historyLoadControl : null}
          {!windowChromeTarget && !evaluationSnapshot && busy && !compactionTarget ? <button aria-label="停止当前回合" disabled={stopping} onClick={() => void stop()} type="button"><StopCircle size={16} /></button> : null}
        </div>
        {!evaluationSnapshot ? <div className="paw-session-workspace__tools" data-open={toolMenuOpen || undefined} ref={toolMenuContainerRef}>
          <button
            aria-controls="paw-session-tools-menu"
            aria-expanded={toolMenuOpen}
            aria-haspopup="menu"
            aria-label="对话工具"
            onClick={() => { if (toolMenuOpen) closeToolMenu(true); else openToolMenu(); }}
            onKeyDown={handleToolMenuButtonKeyDown}
            ref={toolMenuButtonRef}
            type="button"
          >
            <Wrench size={15} />
            <span>对话工具</span>
            {pendingApproval || pendingGenericInput || pendingMemoryReview ? <small className="paw-session-workspace__attention">待处理</small> : null}
          </button>
          {toolMenuOpen ? <nav
            aria-label="对话工具菜单"
            id="paw-session-tools-menu"
            onBlur={handleToolMenuBlur}
            onKeyDown={handleToolMenuKeyDown}
            ref={toolMenuRef}
            role="menu"
          >
            {!windowChromeTarget ? <button data-active={panel === 'status' || undefined} onClick={() => openToolPanel('status')} role="menuitem" type="button"><ListChecks size={15} /><span>任务与状态</span></button> : null}
            <button data-active={panel === 'subagents' || undefined} onClick={() => openToolPanel('subagents')} role="menuitem" type="button"><Network size={15} /><span>子 Agent</span></button>
            {!windowChromeTarget ? <button data-active={panel === 'files' || undefined} onClick={() => openToolPanel('files')} role="menuitem" type="button"><FolderTree size={15} /><span>文件</span></button> : null}
            {windowChromeTarget ? historyLoadControl : null}
            {!windowChromeTarget && contextSnapshotState ? <button disabled={contextSnapshotState === 'restoring'} onClick={() => { closeToolMenu(true); void loadFullSnapshot(); }} role="menuitem" type="button"><History size={15} /><span>{contextSnapshotState === 'restoring' ? '正在恢复完整对话' : '恢复完整对话与待办'}</span></button> : null}
          </nav> : null}
        </div> : null}
      </div>
  );
  return (
    <>
      {!embedded && windowChromeTarget ? <PawWindowChromePortal>{sessionChrome}</PawWindowChromePortal> : null}
      <section
        className="paw-session-workspace paw-chatfx"
        data-design={!embedded && !evaluationSnapshot ? 'workbench' : undefined}
        data-chat-presentation-version={presentation?.version}
        data-chrome-in-window={windowChromeTarget ? true : undefined}
        data-appearance={appearance}
        data-panel={panel}
        data-status={stopping ? 'stopping' : durablePaused ? 'paused' : busy ? 'busy' : 'idle'}
      >
      {embedded || windowChromeTarget ? null : sessionChrome}
      <WorkspaceRecoveryNotice recovery={recovery} />
      {!embedded && !evaluationSnapshot && windowChromeTarget ? <div inert={toolPanelTrapsFocus || undefined}><PawSessionTaskbar title={title} selected={panel} demo={transport.kind === 'mock'}
        state={{ busy, stopping, paused: durablePaused, pending: sending || Boolean(pendingFeedbackTurnId),
          waiting: Boolean(pendingApproval || pendingGenericInput || pendingMemoryReview), disconnected: syncState !== 'synced',
          error: Boolean(visibleError), goal: taskbarGoal, turnStatus: projectionSlice.lastTurnStatus }}
        onOpenPanel={(next, trigger) => { if (panel === next) closeToolPanel(); else openToolPanel(next, trigger); }} /></div> : null}

      <div className="paw-session-workspace__body">
        <div className="paw-session-workspace__primary" ref={primaryRef} inert={toolPanelTrapsFocus || undefined}>
          <div className="paw-session-workspace__viewport">
            <section
              aria-hidden={workspaceView !== 'conversation'}
              aria-label="Session 对话"
              className="paw-agent-next paw-session-workspace__conversation paw-chatfx"
              data-active={workspaceView === 'conversation' || undefined}
              data-agent-tree="projection"
              data-message-flow="separated"
              inert={workspaceView !== 'conversation'}
              role="region"
            >
              <div aria-hidden="true" className="agent-fx-fade agent-fx-fade--top" />
              <div aria-hidden="true" className="agent-fx-fade agent-fx-fade--bottom" />
              {loading && !projectionSlice.hasTurns ? <div className="paw-session-workspace__loading"><LoaderCircle className="ui-spin" size={18} />正在载入最近对话</div> : null}
              <AgentTimeline
                pendingFeedbackTurnId={evaluationSnapshot ? '' : pendingFeedbackTurnId}
                active={liveActive}
                activityPresentation="grouped"
                failurePresentation={embedded ? 'compact' : 'default'}
                presentation={embedded ? 'default' : 'fx'}
                showConversationNavigation={!embedded}
                userMessagePresentation={userMessagePresentation ?? (embedded ? 'request-tail' : 'full')}
                sessionId={recordId}
                includeRoomPublicPosts={Boolean(workspaceRecord.roomParticipant)}
                persona={persona}
                loading={loading}
                modelSelectionAvailable={!evaluationSnapshot && Boolean(catalog)}
                turnRecoveryDisabled={busy || sending || stopping || modelChanging}
                forkAvailable={!evaluationSnapshot && !embedded && classicHistoryAvailable && conversationForkAvailable && !busy && !sending && !workspaceRecord.roomParticipant}
                rewriteAvailable={!evaluationSnapshot && !embedded && classicHistoryAvailable && conversationRewriteAvailable && !busy && !sending && !workspaceRecord.roomParticipant}
                jumpRequest={jumpRequest}
                scrollToLatestRequest={scrollToLatestRequest}
                onFollowStateChange={setTimelineFollow}
                onForkFromMessage={evaluationSnapshot ? undefined : openForkDialog}
                onEditMessage={evaluationSnapshot ? undefined : (messageId) => void beginEditMessage(messageId)}
                onRetryTurn={evaluationSnapshot ? undefined : retryTurn}
                onContinueTurn={evaluationSnapshot ? undefined : continueTurn}
                onSwitchModel={evaluationSnapshot ? undefined : () => setModelPickerRequest((value) => value + 1)}
                onApprovalDecision={evaluationSnapshot ? undefined : (id, decision, hash) => void decideApproval(id, decision, hash)}
                onOpenApproval={evaluationSnapshot ? undefined : setRequestedApproval}
                onRequestPermission={evaluationSnapshot ? undefined : () => setPermissionPickerRequest((value) => value + 1)}
              />
            </section>

            {embedded ? null : <section
              aria-hidden={workspaceView !== 'trace'}
              aria-label="Session Agent 轨迹"
              className="paw-session-workspace__trace"
              data-active={workspaceView === 'trace' || undefined}
              inert={workspaceView !== 'trace'}
              role="region"
            >
              <SessionContextTrace
                active={workspaceView === 'trace'}
                focusNodeId={traceFocusNodeId}
                sessionId={recordId}
              />
            </section>}

            {embedded ? null : <section
              aria-hidden={workspaceView !== 'starfield'}
              className="paw-session-workspace__starfield"
              data-active={workspaceView === 'starfield' || undefined}
              inert={workspaceView !== 'starfield'}
            >
              {/* The sky mounts only while watched: no hidden polling, and the
                  conversation/trace stacked views keep their own state. The
                  component renders an immersive fullscreen overlay; Esc or
                  its exit control returns to the conversation. */}
              {workspaceView === 'starfield' ? <LazyPawSessionStarfield
                active={active && workspaceView === 'starfield'}
                busy={busy}
                sessionId={recordId}
                sessionTitle={title}
                onExit={() => setWorkspaceView('conversation')}
                onOpenRun={(run) => desktop?.openWindow({
                  appId: 'agent',
                  target: {
                    kind: 'subagent',
                    id: run.id,
                    sessionId: recordId,
                    title: run.task || '子 Agent',
                    subtitle: `Session · ${workspaceRecord.title || recordId}`,
                  },
                })}
                onOpenWorkbench={() => { setWorkspaceView('conversation'); setPanel('subagents'); }}
              /> : null}
            </section>}
          </div>

          <div className="paw-session-workspace__composer" data-read-only={evaluationSnapshot || undefined}>
            {(durablePaused || compactionTarget) && !evaluationSnapshot ? <div className="agent-first-response" role="status" aria-live="polite">
              <strong>{compactionTarget ? durablePaused ? '压缩已暂停，进度已保存' : '正在压缩上下文' : '任务已暂停，进度已保存'}</strong>
              {durablePaused && projectionSlice.durableRecovery?.recoverable && (projectionSlice.durableRecovery.activeTurn || compactionTarget) ? <button
                aria-label={compactionTarget ? '继续压缩' : '继续当前任务'} disabled={resuming || sending || stopping || modelChanging}
                onClick={() => void resumeCurrentTask()} type="button">{resuming ? '正在恢复…' : compactionTarget ? '继续压缩' : '继续当前任务'}</button> : null}
              {compactionTarget ? <button aria-label="停止压缩" disabled={stopping || resuming}
                onClick={() => void stop()} type="button">{stopping ? '正在停止…' : '停止压缩'}</button> : null}
            </div> : pendingFeedbackTurnId && !evaluationSnapshot ? <div className="agent-first-response" role="status" aria-live="polite"><LoaderCircle aria-hidden className="ui-spin" size={15} /><strong>等待响应</strong></div> : null}
            {attachmentImportPending ? <div className="agent-first-response" role="status" aria-live="polite">
              <LoaderCircle aria-hidden className="ui-spin" size={15} /><strong>正在导入附件，完成后即可发送。</strong>
            </div> : null}
            {attachmentError ? <div className="paw-session-workspace__error paw-session-workspace__attachment-error" role="alert">
              <CircleAlert size={14} aria-hidden="true" />
              <span>{attachmentError}</span>
              <button type="button" onClick={() => void pickAttachments()}>重新选择</button>
              <button type="button" aria-label="关闭附件提示" onClick={() => setAttachmentError('')}><X size={14} /></button>
            </div> : null}
            {visibleError ? (
              <div className="paw-session-workspace__error" role="alert">
                <CircleAlert size={14} />
                <span>{visibleError}</span>
                {visibleError === STOP_UNCONFIRMED_TEXT ? (
                  <button onClick={() => openToolPanel('status')} type="button">查看任务与状态</button>
                ) : error === SESSION_WORKSPACE_MISSING_TEXT ? (
                  <button onClick={() => void manageWorkspaceRoots()} type="button">选择工作目录</button>
                ) : (
                  <button onClick={() => { setError(''); setSyncError(''); void loadAgentSnapshot(); }} type="button">{error ? '重新同步' : '立即重连'}</button>
                )}
                {!evaluationSnapshot ? <TraceAgentHandoffButton
                  handoff={{
                    kind: 'session',
                    entityId: `session:${recordId}:error`,
                    title: 'Session 操作失败',
                    summary: visibleError,
                    error: error || compactionStopError || syncError,
                    sessionId: recordId,
                    sourceRoute: `/agent?session=${encodeURIComponent(recordId)}`,
                    refs: { surface: 'session-workspace' },
                  }}
                /> : null}
              </div>
            ) : null}
            {workspaceRecord && !evaluationSnapshot ? <QueueTray busy={busy || sending} controller={queue} /> : null}
            {pendingGenericInput && !pendingApproval && !pendingMemoryReview ? (
              <GenericUserInputCard activity={pendingGenericInput} sessionId={recordId} onError={setError} />
            ) : null}
            {workspaceRecord && evaluationSnapshot ? (
              <div className="paw-session-workspace__snapshot-notice">
                <ShieldCheck size={16} />
                <span><strong>评测记录，只读</strong><small>仅展示已保存的对话、Tool 回执与结果；此处不会继续执行。</small></span>
              </div>
            ) : null}
            {workspaceRecord && !evaluationSnapshot ? (
              <>
              {composerContext?.kind === 'project' ? <WorkspaceProjectContext context={composerContext} /> : composerContext ? <div className="paw-workspace-context"><div className="paw-workspace-context__body"><details><summary><strong>{composerContext.label}</strong><span>{composerContext.detail}</span></summary><pre>{composerContext.text}</pre></details>{composerContext.items?.length ? <ul>{composerContext.items.map(item=><li key={item.id}><span>{item.label}</span><button aria-label={`移除 ${item.label}`} onClick={item.onRemove}><X size={12} aria-hidden="true"/></button></li>)}</ul> : null}</div><button aria-label="移除地图上下文" onClick={composerContext.onClear}><X size={16} aria-hidden="true"/></button></div> : null}
              {renderComposerHeader?.({ session: workspaceRecord, draft, disabled: sending || stopping,
                sourceMessageId: () => latestPublicSessionMessageId(agentProjection(address)) })}
              <AgentComposer
                inputOwnerId={agentProjectionKey(address)}
                attachments={attachments}
                attachmentsAvailable={!durableSession}
                busy={busy}
                codemodeMode={codemodeMode}
                codemodeModePending={codemodeChanging}
                capabilityCatalog={capabilityCatalog}
                capabilityPolicyPending={capabilityMutation?.status === 'pending'}
                catalog={catalog}
                commands={commands}
                contextUsage={projectionSlice.telemetry ? {
                  ...projectionSlice.telemetry.context,
                  compactionCount: projectionSlice.telemetry.compactionCount,
                  latestCompaction: projectionSlice.telemetry.latestCompaction,
                } : null}
                draft={draft}
                helpRequest={helpRequest}
                imageSupport={imageSupport}
                editState={editState}
                modelChanging={modelChanging}
                modelPickerRequest={modelPickerRequest}
                thinkingPickerRequest={thinkingPickerRequest}
                permissionPickerRequest={permissionPickerRequest}
                permissionLocked={permissionsLocked}
                persona={persona}
                sending={sending}
                submissionBlocked={Boolean(submissionBlockedReason)}
                submissionBlockedReason={submissionBlockedReason}
                session={workspaceRecord}
                sessionMetadataKnown={recordMetadataKnown && record?.id === recordId}
                stopping={stopping}
                showStop={!compactionTarget}
                toolCatalogStatus={toolCatalogStatus}
                toolPickerRequest={toolPickerRequest}
                toolPickerQuery={toolPickerQuery}
                tools={tools}
                minimal={!showComposerControls}
                placeholder={composerPlaceholder}
                onAttachmentsChange={setAttachments}
                onCapabilityPreferenceChange={(id, preference) => void changeCapabilityPreference(id, preference)}
                onDraftChange={setDraft}
                onCancelEdit={cancelEdit}
                onEditPrevious={() => void beginEditMessage()}
                onCodemodeModeChange={(mode) => void changeCodemodeMode(mode)}
                onModelChange={(provider, modelId, level) => void changeModel(provider, modelId, level)}
                onPasteFromClipboard={() => void pasteFiles()}
                onPasteImages={pasteFiles}
                onPickAttachments={() => void pickAttachments()}
                onProductCommand={runProductCommand}
                onSend={(delivery, value) => {
                  if (!acceptsComposerInput() || !acceptsImmediateInput(value)) return false;
                  const input = userMessagePresentation === 'project-context' ? labProjectUserDraft(value) ?? value : value;
                  void send(delivery, editState ? value : messageWithWorkspaceContext(input, composerContext), input);
                  return true;
                }}
                onStop={() => void stop()}
                showJumpLatest={!timelineFollow.following}
                unseenUpdates={timelineFollow.unseenUpdates}
                onJumpLatest={() => setScrollToLatestRequest((value) => value + 1)}
                onToolSelect={(tool) => setDraft((current) => `${current.trimEnd()}${current.trim() ? '\n' : ''}${toolIntentPrompt(tool.id, tool.displayName)}：`)}
                onPermissionChange={(selection) => void changePermission(selection)}
                onWorkspaceRootsChange={() => void manageWorkspaceRoots()}
                queueDepth={queue.queue.length}
                onQueue={(value) => acceptsComposerInput() && acceptsEngineInput(value) && queue.enqueue(messageWithWorkspaceContext(userMessagePresentation === 'project-context' ? labProjectUserDraft(value) ?? value : value, composerContext))}
              />
              </>
            ) : null}
          </div>
        </div>

        {/* Wide windows place files beside the conversation; compact windows use
            the same labelled drawer. Close and Escape return to its trigger. */}
        {!evaluationSnapshot && !embedded && (panel !== 'none' || statusPanelVisited) ? <FocusScope asChild loop={toolPanelTrapsFocus} trapped={toolPanelTrapsFocus} onMountAutoFocus={event => event.preventDefault()} onUnmountAutoFocus={event => event.preventDefault()}><aside
          ref={toolPanelRef}
          role={toolPanelOverlay ? 'dialog' : undefined}
          aria-modal={toolPanelTrapsFocus || undefined}
          aria-hidden={panel === 'none' || undefined}
          className="paw-session-workspace__side"
          aria-label="对话工具侧栏"
          data-tool={panel}
          hidden={panel === 'none'}
          inert={panel === 'none' ? true : undefined}
          onKeyDown={(event) => {
            if (event.key !== 'Escape') return;
            event.stopPropagation();
            closeToolPanel();
          }}
        >
          {panel === 'files' ? (
            <AgentFilesPanel
              key={agentProjectionKey(address)}
              toolbar={!workspaceRecord.roomParticipant && workspaceRecord.workspaceRoots?.[0] ? (<ProjectQuickActions active={active && liveActive && !loading && panel === 'files'} compact context={{
                  projectId: recordId,
                  title: workspaceRecord.title || recordId,
                  sessionId: recordId,
                  cwd: workspaceRecord.workspaceRoots[0],
                }} />) : undefined}
              sessionId={recordId}
              workspaceRoots={workspaceRecord.workspaceRoots ?? []}
              rootsLockedReason={permissionsLocked ? '工作区已在开始时确定。要使用其他目录，请返回入口新建工作。' : undefined}
              open
              onClose={closeToolPanel}
              onManageRoots={() => void manageWorkspaceRoots()}
            />
          ) : panel === 'subagents' ? (
            <SessionSubagentPanel
              sessionId={recordId}
              session={workspaceRecord}
              tools={tools}
              compactEmpty
              open
              onClose={closeToolPanel}
              onOpenRun={(run) => desktop?.openWindow({
                appId: 'agent',
                target: {
                  kind: 'subagent',
                  id: run.id,
                  sessionId: recordId,
                  title: run.task || '子 Agent',
                  subtitle: `Session · ${workspaceRecord.title || recordId}`,
                },
              })}
            />
          ) : (
            <AgentStatusPanel
              sessionId={recordId}
              session={workspaceRecord}
              keepContentMounted
              open={panel === 'status'}
              surfaceActive={active && panel === 'status'}
              minimal
              commands={commands}
              tools={tools}
              toolCatalogStatus={toolCatalogStatus}
              capabilityCatalog={capabilityCatalog}
              capabilityCatalogError={capabilityCatalogError}
              capabilityPolicyMutation={capabilityMutation}
              contextSnapshotState={contextSnapshotState}
              busy={busy}
              onCapabilityPreferenceChange={(id, preference) => void changeCapabilityPreference(id, preference)}
              onCapabilityPolicyRetry={() => capabilityMutation && void changeCapabilityPreference(capabilityMutation.canonicalId, capabilityMutation.preference)}
              onCapabilityCatalogRetry={() => void loadControlCatalog()}
              onOpenBackgroundJob={(job) => desktop?.openWindow(backgroundJobWindowRequest(job))}
              onClose={closeToolPanel}
            />
          )}
        </aside></FocusScope> : null}
      </div>

      {!evaluationSnapshot ? <MemoryReviewDialog activity={pendingApproval ? undefined : pendingMemoryReview} sessionId={recordId} onError={setError} /> : null}
      {!evaluationSnapshot ? <ApprovalReviewDialog activity={pendingApproval ?? requestedApproval} onDecision={decideApproval} /> : null}
      {!evaluationSnapshot ? <ConversationForkDialog
        assistantName={persona?.displayName ?? 'Agent'}
        open={forkDialogOpen}
        sessionId={recordId}
        sessionTitle={title}
        nodes={forkDialogNodes}
        initialEntryId={forkDialogInitialEntryId}
        branchAvailable={classicHistoryAvailable && conversationForkAvailable && !workspaceRecord.roomParticipant}
        branchBlocked={busy || sending}
        branchUnavailableReason={durableSession ? 'Pi Durable 暂不支持历史分支。' : workspaceRecord.roomParticipant ? '这段对话属于 Room 伙伴，历史分支由 Room 管理。' : undefined}
        onOpenChange={setForkDialogOpen}
        onJump={(messageId) => setJumpRequest({ messageId, requestId: Date.now() })}
        onCreated={onSessionCreated}
      /> : null}
      </section>
    </>
  );
}

function SessionContextTrace({
  active,
  focusNodeId,
  sessionId,
}: {
  active: boolean;
  focusNodeId: string;
  sessionId: string;
}) {
  const transport = useControlTransport();
  const address = agentSessionAddress(transport, sessionId);
  const projection = useAgentLiveStore((state) => (
    active ? selectAgentProjection(state, address) : undefined
  ));
  return (
    <PawContextTrace
      active={active}
      focusNodeId={focusNodeId}
      projection={projection}
      sessionId={sessionId}
    />
  );
}

function conversationNodes(projection?: AgentProjectionState): ConversationNode[] {
  if (!projection) return [];
  return projection.messageOrder
    .map((messageId) => projection.messagesById[messageId])
    .filter((message) => message && (message.role === 'user' || message.role === 'assistant'))
    .map((message) => ({
      entryId: message!.id,
      role: message!.role as ConversationNode['role'],
      text: conversationText(message!.blocks),
      createdAtMs: message!.createdAtMs,
    }))
    .filter((node) => node.text.length > 0);
}

function conversationText(blocks: Array<{ type: string; data: Record<string, unknown> }>): string {
  return blocks.map((block) => {
    const candidates = [block.data.text, block.data.markdown, block.data.code, block.data.message, block.data.summary];
    return candidates.find((item): item is string => typeof item === 'string' && item.trim().length > 0) ?? '';
  }).filter(Boolean).join('\n').replace(/\s+/gu, ' ').trim().slice(0, 480);
}

/** Same receipt shape the standalone Agent feature reads: Stop raced the
 *  admission and won, so the optimistic message must vanish, not acknowledge. */
export function latestPublicSessionMessageId(projection: AgentProjectionState): string | undefined {
  return [...projection.messageOrder].reverse().find(id => {
    const message = projection.messagesById[id];
    return !id.startsWith('local:') && message?.status === 'completed'
      && (message.role === 'user' || message.role === 'assistant')
      && message.blocks.some(block => block.type === 'text' && typeof block.data.text === 'string' && block.data.text.trim());
  });
}

function isCancelledPromptAdmission(value: unknown): boolean {
  return isRecord(value)
    && value.accepted === false
    && value.cancelled === true
    && value.admissionCancelled === true;
}

/** True when the latest turn is the one this optimistic message failed, so the
 *  timeline renders its failed-turn card for exactly this failure. */
function timelineOwnsTurnFailure(
  projection: AgentProjectionState,
  clientMessageId: string,
): boolean {
  const messageId = projection.optimisticByClientMessageId[clientMessageId] ?? '';
  const message = projection.messagesById[messageId];
  if (message?.status !== 'failed') return false;
  if (projection.turnOrder.at(-1) !== message.turnId) return false;
  return projection.turnsById[message.turnId]?.status === 'failed';
}

function latestWaitingActivity(
  projection: AgentProjectionState | undefined,
  predicate: (activity: AgentActivityProjection) => boolean,
): AgentActivityProjection | undefined {
  if (!projection) return undefined;
  for (let index = projection.activityOrder.length - 1; index >= 0; index -= 1) {
    const activity = projection.activitiesById[projection.activityOrder[index] ?? ''];
    if (activity?.status === 'waiting' && predicate(activity)) return activity;
  }
  return undefined;
}

function selectedModelImageSupport(catalog?: ModelCatalog): 'supported' | 'unsupported' | 'unknown' {
  if (!catalog) return 'unknown';
  const selected = asRecord(catalog.selected);
  const providerId = typeof selected.provider === 'string' ? selected.provider : '';
  const modelId = typeof selected.id === 'string'
    ? selected.id
    : typeof selected.modelId === 'string'
      ? selected.modelId
      : '';
  const model = catalog.providers.find((provider) => provider.id === providerId)?.models.find((item) => item.id === modelId);
  if (model) return model.supportsImages ? 'supported' : 'unsupported';
  return typeof selected.supportsImages === 'boolean' ? (selected.supportsImages ? 'supported' : 'unsupported') : 'unknown';
}

function mergeAttachments(current: ComposerAttachment[], next: ComposerAttachment[]): ComposerAttachment[] {
  const byId = new Map(current.map((item) => [item.id, item]));
  next.forEach((item) => byId.set(item.id, item));
  return [...byId.values()].slice(0, 8);
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

function asSession(value: unknown): SessionSummary | undefined {
  const item = asRecord(value);
  return typeof item.id === 'string' && typeof item.title === 'string' && typeof item.updatedAtMs === 'number'
    ? item as unknown as SessionSummary
    : undefined;
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}



function isCommand(value: string, command: string): boolean {
  return value === command || value.startsWith(`${command} `);
}


function errorText(reason: unknown): string {
  return publicAgentErrorText(reason, 'Session 操作没有完成，请重新同步后重试。');
}

function validCompactionReceipt(receipt: Record<string, unknown>, target: AgentCompactionTarget, action: 'resume' | 'abort'): boolean {
  if (receipt.schemaVersion !== `rag-ime.pi-compaction-${action}.v1` || receipt.accepted !== true
    || receipt.runtimeEngine !== 'durable' || !sameAgentCompactionTarget(receipt.compactionTarget, target)
    || !receipt.state || typeof receipt.state !== 'object' || Array.isArray(receipt.state)) return false;
  if (action === 'resume') return typeof receipt.resumed === 'boolean';
  if (receipt.drained !== true || !Array.isArray(receipt.outcomes) || receipt.outcomes.length !== target.taskIds.length) return false;
  const outcomes = receipt.outcomes.map(asRecord);
  return outcomes.every((outcome, index) => Object.keys(outcome).length === 2
    && outcome.taskId === target.taskIds[index] && ['completed', 'aborted', 'failed'].includes(String(outcome.status)));
}

/** Absence is a Runtime capability boundary, not a frontend default. */
function nativeCodemodeMode(value: unknown): CodemodeMode | undefined {
  return value === 'on' || value === 'only' || value === 'off' ? value : undefined;
}
