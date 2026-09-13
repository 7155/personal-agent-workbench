import { Archive, ArchiveRestore, BriefcaseBusiness, FilePlus2, FolderOpen, GitBranch, ListEnd, ListStart, ListTodo, LoaderCircle, MessageSquarePlus, MessagesSquare, MoreHorizontal, PanelLeftClose, PanelLeftOpen, PanelRightClose, PanelRightOpen, PanelsTopLeft, Plus, RefreshCw, Settings2, ShieldCheck, Sparkles, Trash2, UserMinus, UserPlus, X } from 'lucide-react';
import * as RadioGroup from '@radix-ui/react-radio-group';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { flushSync } from 'react-dom';
import { Virtuoso, type VirtuosoHandle } from 'react-virtuoso';
import { useShallow } from 'zustand/react/shallow';
import { useControlTransport } from '@/app/control-transport';
import { useComposerClearance } from '@/components/layout/use-composer-clearance';
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Disclosure,
  EmptyState,
  IconButton,
  Menu,
  MenuContent,
  MenuItem,
  MenuTrigger,
  SegmentedControl,
  Select,
} from '@/components/primitives';
import {
  MAX_COMPOSER_ATTACHMENTS,
  MAX_COMPOSER_ATTACHMENT_BYTES,
  isComposerAttachmentMimeType,
} from '@/contracts/attachment-policy';
import type { AgentPersonaV1 } from '@/contracts/generated/agent-persona.v1';
import type { PickedFile } from '@/platform/transport';
import {
  parseRoomEventPage,
  type RoomAttachmentReceipt,
  type RoomProjectionState,
} from '@/contracts/room-reducer';
import { GenericUserInputCard } from '@/features/agent/review/AgentReviewDialogs';
import { unrestrictedWorkspaceRoots } from '@/features/agent/composer/permission-policy';
import { roleItems } from '@/features/agent/types';
import { useMediaQuery, useModalPanel } from '@/features/agent/overlay-dialog';
import { publicErrorText } from '@/features/overview/management-ui';
import { usePawOsDesktop } from '@/features/paw-os/surface-context';
import { RoomStatusPanel } from './RoomStatusPanel';
import { ConnectedRoomTaskGraph } from './RoomTaskGraph';
import { RoomMemberBoundaryDialog } from './RoomMemberBoundaryDialog';
import { RoomPaneResizer } from './RoomPaneResizer';
import { RoomComposer, roomMentionedParticipants } from './composer/RoomComposer';
import {
  roomCollaborationRoleDescription,
  roomCollaborationRoleLabel,
} from './room-copy';
import {
  RoomPermissionPolicyEditor,
  participantName,
  pathName,
  recommendedCreateRole,
  roomAvatarIcon,
  roomAvatarOptions,
  roomCollaborationRoleOptions,
  roomCreateParticipantLabel,
  roomPathName,
  roomPermissionLayerPresentation,
  roomWorkspaceViewOptions,
  roomWorkStateLabel,
} from './room-presentation';
import { roomCandidatePlanetName, roomParticipantPlanetName } from './room-participant-identity';
import {
  latestPendingGroupedRoomInput,
  type PendingRoomQuestion,
} from './room-question';
import {
  defaultRoomPermissionPolicy,
  parseRoomPermissionPolicy,
  roomPermissionPoliciesEqual,
  roomPermissionPolicyNeedsDangerousConfirmation,
  roomPermissionPolicyNeedsWorkspaceConfirmation,
  type RoomCollaborationRole,
  type RoomKind,
  type RoomParticipant,
  type RoomPermissionPolicy,
  type RoomSummary,
  type RoomWorkItem,
  type RoomWorkState,
} from './room-types';
import { RoomTurn } from './timeline/RoomTurn';
import {
  selectActivePublicRoomTurn,
  selectPublicRoomTurnOrder,
  selectRoomExecutionOverview,
  selectRoomTurnExecution,
} from './runtime/room-execution-lanes';
import { useRoomLiveSession } from './runtime/use-room-live-session';
import { useRoomLiveStore } from './state/live-store';
import './rooms.css';

export { RoomTurn } from './timeline/RoomTurn';
export type {
  RoomArtifact,
  RoomCollaborationRole,
  RoomEffectivePermissionPolicy,
  RoomExecutionMode,
  RoomKind,
  RoomParticipant,
  RoomPermissionChildExecutionMode,
  RoomPermissionLayer,
  RoomPermissionPolicy,
  RoomRoutingPolicy,
  RoomSummary,
  RoomTopic,
  RoomWorkItem,
  RoomWorkState,
} from './room-types';

const emptyRoomTurnIds: string[] = [];
const NO_ROOM_PROJECT = '__no_room_project__';
const ROOM_WORK_ITEM_STATES: Record<string, true> = {
  queued: true,
  active: true,
  review: true,
  blocked: true,
  done: true,
  failed: true,
  cancelled: true,
};
const ROOM_ATTACHMENT_LIMIT = MAX_COMPOSER_ATTACHMENTS;
const ROOM_ATTACHMENT_MAX_BYTES = MAX_COMPOSER_ATTACHMENT_BYTES;
const roomTimelineComponents = {
  Header: RoomTimelineScrollHeader,
  Footer: RoomTimelineScrollFooter,
};

interface RoomTimelineContext {
  roomId: string;
  historyLoadRevision: number;
}

function RoomTimelineScrollHeader({ context }: { context?: RoomTimelineContext }) {
  return <>
    <div aria-hidden="true" className="room-timeline__header-space" />
    {context?.roomId ? <RoomHistoryControl
      historyLoadRevision={context.historyLoadRevision}
      roomId={context.roomId}
    /> : null}
  </>;
}

function RoomHistoryControl({
  historyLoadRevision,
  roomId,
}: {
  historyLoadRevision: number;
  roomId: string;
}) {
  const transport = useControlTransport();
  const markerRef = useRef<HTMLDivElement>(null);
  const history = useRoomLiveStore((state) => state.historyByRoomId[roomId]);
  const prependHistory = useRoomLiveStore((state) => state.prependHistory);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  async function loadEarlier(): Promise<void> {
    if (loading || !history?.hasMore || !history.firstSequence) return;
    const requestedBefore = history.firstSequence;
    const scroller = markerRef.current?.closest<HTMLElement>('[data-virtuoso-scroller="true"]');
    const previousHeight = scroller?.scrollHeight ?? 0;
    setLoading(true);
    setError('');
    try {
      const page = parseRoomEventPage(await transport.request({
        pathId: 'agent.room.history',
        params: { roomId },
        query: {
          beforeSequence: requestedBefore,
          limit: 200,
        },
      }));
      let applied = false;
      flushSync(() => {
        applied = prependHistory(roomId, page);
      });
      if (!applied) {
        throw new Error('活动流已更新，请重新载入较早记录。');
      }
      if (scroller) {
        requestAnimationFrame(() => {
          scroller.scrollTop += Math.max(0, scroller.scrollHeight - previousHeight);
        });
      }
    } catch (requestError) {
      setError(publicErrorText(requestError, '暂时无法载入较早记录，请稍后重试。'));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (historyLoadRevision < 1 || loading || error || !history?.hasMore) return;
    void loadEarlier();
  }, [historyLoadRevision]);

  if (!history?.hasMore && !history?.retainedPrefixTruncated && !error) {
    return null;
  }
  return <div ref={markerRef} className="room-history-control" data-idle={history?.hasMore && !loading && !error || undefined}>
    {loading ? <span role="status"><LoaderCircle className="ui-spin" size={14} />正在补全较早记录</span> : null}
    {error ? <Button
      leadingIcon={<RefreshCw size={14} />}
      onClick={() => void loadEarlier()}
      size="small"
      variant="quiet"
    >重试较早记录</Button> : null}
    {!history?.hasMore && history?.retainedPrefixTruncated
      ? <span role="status">更早记录已按保留策略截断</span>
      : null}
    {error ? <span role="alert">{error}</span> : null}
  </div>;
}

function RoomTimelineScrollFooter() {
  return <div aria-hidden="true" className="room-timeline__footer-space" />;
}

function roomTimelineScrollerIsAtBottom(scroller: HTMLElement): boolean {
  return scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop <= 120;
}

function scrollRoomTimelineToLatest(
  timeline: VirtuosoHandle | null,
  scroller: HTMLElement | null,
  index: number,
): void {
  timeline?.scrollToIndex({ align: 'end', behavior: 'auto', index });
  if (!scroller) return;
  requestAnimationFrame(() => {
    scroller.scrollTo({ behavior: 'auto', top: scroller.scrollHeight });
  });
}

function latestRoomTimelineContentKey(
  projection: RoomProjectionState | undefined,
  turnOrder: readonly string[],
): string {
  const turnId = turnOrder.at(-1) ?? '';
  if (!projection || !turnId) return '';
  const turn = projection.turnsById[turnId];
  const messageId = selectRoomTurnExecution(projection, turnId).messageIds.at(-1) ?? '';
  const message = messageId ? projection.messagesById[messageId] : undefined;
  return [turnId, messageId, message?.status ?? turn?.status ?? ''].join('\u001f');
}

export function RoomsFeature({ initialRoomId = '', pawOsWorkbench = false }: { initialRoomId?: string; pawOsWorkbench?: boolean } = {}) {
  const transport = useControlTransport();
  const pawOsDesktop = usePawOsDesktop();
  const [rooms, setRooms] = useState<RoomSummary[]>([]);
  const [personas, setPersonas] = useState<AgentPersonaV1[]>([]);
  const [selectedId, setSelectedId] = useState(initialRoomId);
  const roomDraftsRef = useRef(roomDraftsStore);
  const roomAttachmentsRef = useRef(new Map<string, RoomAttachmentReceipt[]>());
  const roomErrorsRef = useRef(new Map<string, {
    message: string;
    source: 'connection' | 'operation';
  }>());
  const roomSendLocksRef = useRef(new Set<string>());
  const selectedRoomIdRef = useRef('');
  const roomComposerRef = useRef<HTMLTextAreaElement>(null);
  const roomRailTriggerRef = useRef<HTMLButtonElement>(null);
  const roomRailCloseRef = useRef<HTMLButtonElement>(null);
  const roomRailRef = useRef<HTMLElement>(null);
  const roomStatusToggleRef = useRef<HTMLButtonElement>(null);
  const roomStatusRef = useRef<HTMLElement>(null);
  const roomWorkspaceRef = useRef<HTMLElement>(null);
  const roomTimelineRef = useRef<VirtuosoHandle>(null);
  const roomTimelineFollowIntentRef = useRef(true);
  const roomTimelineLatestContentKeyRef = useRef('');
  const [roomTimelineScroller, setRoomTimelineScroller] = useState<HTMLElement | null>(null);
  useComposerClearance(roomWorkspaceRef);
  const [roomRailOpen, setRoomRailOpen] = useState(roomRailInitiallyOpen);
  const roomRailOverlay = useMediaQuery('(max-width: 760px)');
  const roomStatusOverlay = useMediaQuery('(max-width: 1360px)');
  const [statusOpen, setStatusOpen] = useState(false);
  const [workspaceView, setWorkspaceView] = useState<'posts' | 'execution' | 'sessions'>('posts');
  const [historyLoadRevision, setHistoryLoadRevision] = useState(0);
  const [snapshotLoading, setSnapshotLoading] = useState(false);
  const [roomRecoveryStates, setRoomRecoveryStates] = useState<Record<string, 'recovering' | 'failed' | 'synced'>>({});
  const [draft, setDraft] = useState('');
  const [attachments, setAttachments] = useState<RoomAttachmentReceipt[]>([]);
  const [error, setError] = useState('');
  const [roomCatalogError, setRoomCatalogError] = useState('');
  const [roleCatalogError, setRoleCatalogError] = useState('');
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [catalogReloadRevision, setCatalogReloadRevision] = useState(0);
  const [includeArchived, setIncludeArchived] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [createError, setCreateError] = useState('');
  const [createTitle, setCreateTitle] = useState('');
  const [createRoomKind, setCreateRoomKind] = useState<RoomKind>('collaboration');
  const [createAvatar, setCreateAvatar] = useState('briefcase');
  const [createDescription, setCreateDescription] = useState('');
  const [createScenarioPrompt, setCreateScenarioPrompt] = useState('');
  const [createPermissionPolicy, setCreatePermissionPolicy] = useState<RoomPermissionPolicy>(
    () => defaultRoomPermissionPolicy('collaboration'),
  );
  const [projectPaths, setProjectPaths] = useState<string[]>([]);
  const [workspaceRoots, setWorkspaceRoots] = useState<string[]>([]);
  const [workspacePicking, setWorkspacePicking] = useState(false);
  const [selectedRoleIds, setSelectedRoleIds] = useState<string[]>([]);
  const [coordinatorRoleId, setCoordinatorRoleId] = useState('');
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsTitle, setSettingsTitle] = useState('');
  const [settingsAvatar, setSettingsAvatar] = useState('members');
  const [settingsDescription, setSettingsDescription] = useState('');
  const [settingsScenarioPrompt, setSettingsScenarioPrompt] = useState('');
  const [settingsPermissionPolicy, setSettingsPermissionPolicy] = useState<RoomPermissionPolicy>();
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [settingsError, setSettingsError] = useState('');
  const [memberSavingRoleId, setMemberSavingRoleId] = useState('');
  const [memberRemovingId, setMemberRemovingId] = useState('');
  const [memberUpdatingId, setMemberUpdatingId] = useState('');
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteConfirmTitle, setDeleteConfirmTitle] = useState('');
  const [deleting, setDeleting] = useState(false);
  const [topicsOpen, setTopicsOpen] = useState(false);
  const [topicTitle, setTopicTitle] = useState('');
  const [topicSummary, setTopicSummary] = useState('');
  const [topicEditingId, setTopicEditingId] = useState('');
  const [topicSaving, setTopicSaving] = useState(false);
  const [topicError, setTopicError] = useState('');
  const [artifactPicking, setArtifactPicking] = useState(false);
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [boundaryParticipant, setBoundaryParticipant] = useState<RoomParticipant>();
  const [abortingTurnIds, setAbortingTurnIds] = useState<Set<string>>(() => new Set());
  const [sendingRoomIds, setSendingRoomIds] = useState<Set<string>>(() => new Set());
  const visibleTurnOrder = useRoomLiveStore(useShallow((state) => {
    const projection = state.projections[selectedId];
    return projection ? selectPublicRoomTurnOrder(projection) : emptyRoomTurnIds;
  }));
  const selectedRoomProjection = useRoomLiveStore((state) => state.projections[selectedId]);
  const runtimeWorkItems = useMemo(
    () => selectedRoomProjection ? selectRoomExecutionOverview(selectedRoomProjection) : [],
    [selectedRoomProjection],
  );
  const pendingQuestion = useRoomLiveStore((state) => (
    state.projections[selectedId]?.pendingUserQuestion
  ));
  const activeRoomTurn = useRoomLiveStore((state) => {
    const projection = state.projections[selectedId];
    if (!projection) return undefined;
    return selectActivePublicRoomTurn(projection);
  });
  const pendingGroupedInput = useRoomLiveStore((state) => (
    latestPendingGroupedRoomInput(state.projections[selectedId])
  ));
  const roomRailModal = roomRailOverlay && roomRailOpen;
  const roomStatusModal = roomStatusOverlay && statusOpen;
  const selectedRoomRecoveryState = roomRecoveryStates[selectedId];
  const selectedRoomErrorSource = roomErrorsRef.current.get(selectedId)?.source;
  const latestTimelineContentKey = useMemo(
    () => latestRoomTimelineContentKey(selectedRoomProjection, visibleTurnOrder),
    [selectedRoomProjection, visibleTurnOrder],
  );

  selectedRoomIdRef.current = selectedId;

  const handleRoomTimelineScrollerRef = useCallback((scroller: HTMLElement | Window | null) => {
    setRoomTimelineScroller(scroller instanceof HTMLElement ? scroller : null);
  }, []);

  useEffect(() => {
    roomTimelineFollowIntentRef.current = true;
    roomTimelineLatestContentKeyRef.current = latestTimelineContentKey;
  }, [selectedId]);

  useEffect(() => {
    if (!roomTimelineScroller) return;
    let pointerScrollActive = false;
    const leaveLiveFollow = () => {
      roomTimelineFollowIntentRef.current = false;
    };
    const handleWheel = (event: WheelEvent) => {
      if (event.deltaY < 0) leaveLiveFollow();
    };
    const handlePointerDown = () => {
      pointerScrollActive = true;
    };
    const handlePointerEnd = () => {
      pointerScrollActive = false;
    };
    const handleScroll = () => {
      if (pointerScrollActive && !roomTimelineScrollerIsAtBottom(roomTimelineScroller)) {
        leaveLiveFollow();
      }
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (
        event.key === 'ArrowUp'
        || event.key === 'PageUp'
        || event.key === 'Home'
        || (event.key === ' ' && event.shiftKey)
      ) {
        leaveLiveFollow();
      }
    };
    roomTimelineScroller.addEventListener('wheel', handleWheel, { passive: true });
    roomTimelineScroller.addEventListener('pointerdown', handlePointerDown);
    roomTimelineScroller.addEventListener('scroll', handleScroll, { passive: true });
    roomTimelineScroller.addEventListener('keydown', handleKeyDown);
    window.addEventListener('pointerup', handlePointerEnd);
    window.addEventListener('pointercancel', handlePointerEnd);
    return () => {
      roomTimelineScroller.removeEventListener('wheel', handleWheel);
      roomTimelineScroller.removeEventListener('pointerdown', handlePointerDown);
      roomTimelineScroller.removeEventListener('scroll', handleScroll);
      roomTimelineScroller.removeEventListener('keydown', handleKeyDown);
      window.removeEventListener('pointerup', handlePointerEnd);
      window.removeEventListener('pointercancel', handlePointerEnd);
    };
  }, [roomTimelineScroller]);

  useEffect(() => {
    const previousContentKey = roomTimelineLatestContentKeyRef.current;
    roomTimelineLatestContentKeyRef.current = latestTimelineContentKey;
    if (
      !latestTimelineContentKey
      || latestTimelineContentKey === previousContentKey
      || !roomTimelineFollowIntentRef.current
    ) return;
    const frame = window.requestAnimationFrame(() => {
      if (!roomTimelineFollowIntentRef.current) return;
      scrollRoomTimelineToLatest(
        roomTimelineRef.current,
        roomTimelineScroller,
        visibleTurnOrder.length - 1,
      );
    });
    return () => window.cancelAnimationFrame(frame);
  }, [latestTimelineContentKey, roomTimelineScroller, visibleTurnOrder.length]);

  function selectRoomId(roomId: string): void {
    selectedRoomIdRef.current = roomId;
    setSelectedId(roomId);
    setWorkspaceView('posts');
    setHistoryLoadRevision(0);
    setDraft(roomDraftsRef.current.get(roomId) ?? '');
    setAttachments(roomAttachmentsRef.current.get(roomId) ?? []);
    setError(roomErrorsRef.current.get(roomId)?.message ?? '');
  }


  function persistRoomDraft(roomId: string, value: string): void {
    if (roomId) roomDraftsRef.current.set(roomId, value);
  }

  function updateRoomDraft(roomId: string, value: string): void {
    persistRoomDraft(roomId, value);
    if (selectedRoomIdRef.current === roomId) setDraft(value);
  }

  function updateRoomAttachments(
    roomId: string,
    value: RoomAttachmentReceipt[] | ((current: RoomAttachmentReceipt[]) => RoomAttachmentReceipt[]),
  ): void {
    const current = roomAttachmentsRef.current.get(roomId) ?? [];
    const next = typeof value === 'function' ? value(current) : value;
    if (next.length) roomAttachmentsRef.current.set(roomId, next);
    else roomAttachmentsRef.current.delete(roomId);
    if (selectedRoomIdRef.current === roomId) setAttachments(next);
  }

  function mergeRoomAttachments(roomId: string, imported: PickedFile[]): void {
    const receipts = imported.map((file) => roomAttachmentFromPicked(file, roomId));
    updateRoomAttachments(roomId, (current) => {
      const merged = new Map(current.map((item) => [item.mediaId, item]));
      for (const receipt of receipts) merged.set(receipt.mediaId, receipt);
      return [...merged.values()].slice(0, ROOM_ATTACHMENT_LIMIT);
    });
  }

  async function pasteRoomImages(roomId: string, files?: File[]): Promise<boolean> {
    const current = roomAttachmentsRef.current.get(roomId) ?? [];
    const remaining = ROOM_ATTACHMENT_LIMIT - current.length;
    if (remaining < 1) {
      setRoomError(roomId, `每条消息最多添加 ${ROOM_ATTACHMENT_LIMIT} 个附件。`);
      return false;
    }
    if (!transport.pasteImages) {
      setRoomError(roomId, '当前平台暂不支持从剪贴板导入附件。');
      return false;
    }
    try {
      validateRoomAttachmentFiles(files ?? [], remaining);
      const imported = await transport.pasteImages({
        roomId,
        ...(files?.length ? { files } : {}),
        maxFiles: files?.length || remaining,
      });
      mergeRoomAttachments(roomId, imported);
      setRoomError(roomId, '');
      return imported.length > 0;
    } catch (requestError) {
      setRoomError(roomId, publicErrorText(requestError, '附件没有导入，请重试。'));
      return false;
    }
  }

  async function pickRoomImages(roomId: string): Promise<void> {
    const current = roomAttachmentsRef.current.get(roomId) ?? [];
    const remaining = ROOM_ATTACHMENT_LIMIT - current.length;
    if (remaining < 1) {
      setRoomError(roomId, `每条消息最多添加 ${ROOM_ATTACHMENT_LIMIT} 个附件。`);
      return;
    }
    if (!transport.pickFiles) {
      setRoomError(roomId, '当前平台暂不支持选择附件。');
      return;
    }
    try {
      const imported = await transport.pickFiles({
        purpose: 'attachment',
        roomId,
        multiple: true,
        maxFiles: remaining,
      });
      mergeRoomAttachments(roomId, imported);
      setRoomError(roomId, '');
    } catch (requestError) {
      setRoomError(roomId, publicErrorText(requestError, '附件没有导入，请重试。'));
    }
  }

  function setRoomError(
    roomId: string,
    value: string,
    source: 'connection' | 'operation' = 'operation',
  ): void {
    if (roomId) {
      if (value) roomErrorsRef.current.set(roomId, { message: value, source });
      else roomErrorsRef.current.delete(roomId);
    }
    if (selectedRoomIdRef.current === roomId) setError(value);
  }

  function clearRoomConnectionError(roomId: string): void {
    if (roomErrorsRef.current.get(roomId)?.source !== 'connection') return;
    roomErrorsRef.current.delete(roomId);
    if (selectedRoomIdRef.current === roomId) setError('');
  }

  async function decideRoomApproval(
    roomId: string,
    approvalId: string,
    decision: 'approved' | 'rejected',
    payloadSha256: string,
  ): Promise<void> {
    try {
      await transport.request({
        pathId: 'agent.approval.decide',
        params: { approvalId },
        body: {
          decision: decision === 'approved' ? 'approve' : 'reject',
          payloadSha256,
        },
      });
      setRoomError(roomId, '');
    } catch (requestError) {
      setRoomError(roomId, publicErrorText(requestError, '审批没有完成，请重试。'));
      throw requestError;
    }
  }

  function closeRoomRail(restoreFocus = true): void {
    if (!roomRailOpen) return;
    setRoomRailOpen(false);
    if (restoreFocus) requestAnimationFrame(() => roomRailTriggerRef.current?.focus());
  }

  function closeRoomRailIfOverlay(restoreFocus = true): void {
    if (roomRailOverlay) closeRoomRail(restoreFocus);
  }

  useEffect(() => {
    if (roomRailOverlay) setRoomRailOpen(false);
  }, [roomRailOverlay]);

  useEffect(() => {
    if (roomStatusOverlay) setStatusOpen(false);
  }, [roomStatusOverlay]);

  useModalPanel({
    active: roomStatusModal,
    panelRef: roomStatusRef,
    returnFocusRef: roomStatusToggleRef,
    onClose: () => setStatusOpen(false),
  });

  useEffect(() => {
    if (!roomRailOpen || !roomRailOverlay) return;
    roomRailCloseRef.current?.focus();
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        setRoomRailOpen(false);
        requestAnimationFrame(() => roomRailTriggerRef.current?.focus());
        return;
      }
      if (event.key !== 'Tab') return;
      const focusable = [...(roomRailRef.current?.querySelectorAll<HTMLButtonElement>('button:not([disabled])') ?? [])];
      const first = focusable[0];
      const last = focusable.at(-1);
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', closeOnEscape);
    return () => document.removeEventListener('keydown', closeOnEscape);
  }, [roomRailOpen, roomRailOverlay]);

  useEffect(() => {
    let active = true;
    const requestRoleCatalog = async () => {
      try {
        return await transport.request({ pathId: 'agent.roles.list' });
      } catch (firstError) {
        await new Promise((resolve) => window.setTimeout(resolve, 180));
        if (!active) throw firstError;
        return transport.request({ pathId: 'agent.roles.list' });
      }
    };
    setCatalogLoading(true);
    const roomRequest = transport.request({
      pathId: 'agent.rooms.list',
      query: { limit: 100, ownerAppId: '', ...(includeArchived ? { includeArchived: true } : {}) },
    });
    const roleRequest = requestRoleCatalog();
    const sessionRequest = transport.request({ pathId: 'agent.sessions.list', query: { limit: 200 } });
    const applyRoomCatalog = (items: RoomSummary[]) => {
      setRooms(items.filter((item) => !item.ownerAppId || item.id === initialRoomId));
      setSelectedId((current) => {
        const next = items.some((item) => item.id === current)
          ? current
          : items.some((item) => item.id === initialRoomId)
            ? initialRoomId
            : items.find((item) => item.status === 'active')?.id ?? items[0]?.id ?? '';
        selectedRoomIdRef.current = next;
        return next;
      });
      setRoomCatalogError('');
    };
    void roomRequest.then((value) => {
      if (!active) return;
      applyRoomCatalog(roomItems(value));
    }).catch((requestError) => {
      if (active) setRoomCatalogError(publicErrorText(requestError, '协作空间暂时无法读取，请稍后重试。'));
    });
    void Promise.allSettled([roomRequest, roleRequest, sessionRequest]).then(([roomResult, roleResult, sessionResult]) => {
      if (!active) return;
      const loadedRooms = roomResult.status === 'fulfilled' ? roomItems(roomResult.value) : [];
      if (roleResult.status === 'fulfilled') {
        setPersonas(roleItems(roleResult.value));
        setRoleCatalogError('');
      } else {
        setRoleCatalogError(publicErrorText(roleResult.reason, '角色目录暂时没有同步；Room 运行事实仍可阅读。'));
      }
      const loadedSessionRoots = sessionResult.status === 'fulfilled'
        ? sessionWorkspaceRoots(sessionResult.value)
        : [];
      setProjectPaths(uniquePaths([
        ...loadedRooms.flatMap((item) => item.workspaceRoots ?? []),
        ...loadedSessionRoots,
      ]).filter((path) => path !== '/'));
    }).finally(() => { if (active) setCatalogLoading(false); });
    return () => { active = false; };
  }, [catalogReloadRevision, includeArchived, initialRoomId, transport]);

  useEffect(() => {
    if (!selectedId) {
      setDraft('');
      setError('');
      return;
    }
    setDraft(roomDraftsRef.current.get(selectedId) ?? '');
    setAttachments(roomAttachmentsRef.current.get(selectedId) ?? []);
    setError(roomErrorsRef.current.get(selectedId)?.message ?? '');
  }, [selectedId]);
  const retryRoomSnapshot = useRoomLiveSession({
    roomId: selectedId,
    transport,
    onLoadingChange: setSnapshotLoading,
    onSnapshot: (_roomId, snapshot) => {
      const snapshotRoom = snapshot.room as unknown as RoomSummary;
      setRooms((current) => current.map((item) => (
        item.id === snapshotRoom.id ? snapshotRoom : item
      )));
    },
    onMetadata: (roomId, value) => {
      const refreshedRoom = roomFromGetResponse(value, roomId);
      if (!refreshedRoom) return;
      setRooms((current) => current.map((item) => (
        item.id === refreshedRoom.id ? refreshedRoom : item
      )));
    },
    onEvents: () => undefined,
    onConnectionRestored: clearRoomConnectionError,
    onRecoveryState: (roomId, state) => {
      setRoomRecoveryStates((current) => (
        current[roomId] === state ? current : { ...current, [roomId]: state }
      ));
    },
    onConnectionError: (roomId, liveError, fallback) => {
      setRoomError(
        roomId,
        publicErrorText(liveError, fallback),
        'connection',
      );
    },
  });

  const room = rooms.find((item) => item.id === selectedId);
  const storedRoomPermissionPolicy = useMemo(
    () => parseRoomPermissionPolicy(room?.permissionPolicy, room?.roomKind),
    [room?.permissionPolicy, room?.roomKind],
  );
  const partnerPermissionPresentation = storedRoomPermissionPolicy
    ? roomPermissionLayerPresentation(
        storedRoomPermissionPolicy,
        'partner',
        room?.roomKind ?? 'collaboration',
      )
    : undefined;
  const activeParticipants = room?.participants.filter((participant) => participant.status === 'active') ?? [];
  const participantAliases = Object.fromEntries(
    activeParticipants.map((participant) => [participant.id, roomParticipantPlanetName(participant)]),
  );
  const unresolvedWork = room?.workItems?.filter((work) => (
    ['blocked', 'review', 'active', 'queued'].includes(work.state)
  )) ?? [];
  const activeWork = unresolvedWork[0];
  const managedTaskBusyState = room?.roomKind === 'roleplay'
    ? undefined
    : activeWork?.state === 'blocked'
      ? 'blocked'
      : activeRoomTurn || activeWork
        ? 'running'
        : undefined;
  const roomHeadlineState = activeWork
      ? roomWorkStateLabel(activeWork.state)
      : activeRoomTurn
        ? '执行中'
        : '先把目标聊清楚';
  const roomSettingsChanged = Boolean(room && (
    settingsTitle.trim() !== room.title
    || settingsAvatar !== (room.avatar ?? (room.roomKind === 'roleplay' ? 'sparkles' : 'briefcase'))
    || settingsDescription.trim() !== (room.description ?? '').trim()
    || settingsScenarioPrompt.trim() !== (room.scenarioPrompt ?? '').trim()
    || !roomPermissionPoliciesEqual(settingsPermissionPolicy, storedRoomPermissionPolicy)
  ));

  async function send(
    composerDraft = draft,
    options: {
      preserveMessage?: boolean;
      includeComposerState?: boolean;
      question?: PendingRoomQuestion;
      retryOfRootId?: string;
    } = {},
  ): Promise<boolean> {
    if (!room || room.status !== 'active') return false;
    const includeComposerState = options.includeComposerState !== false;
    const authoritativeQuestion = useRoomLiveStore
      .getState()
      .projections[room.id]
      ?.pendingUserQuestion;
    const matchesAuthoritativeQuestion = Boolean(
      options.question
      && authoritativeQuestion
      && options.question.postId === authoritativeQuestion.postId
      && options.question.roomId === authoritativeQuestion.roomId
      && options.question.rootId === authoritativeQuestion.rootId
      && options.question.sequence === authoritativeQuestion.sequence
    );
    const selectedAttachments = includeComposerState && !matchesAuthoritativeQuestion
      ? roomAttachmentsRef.current.get(room.id) ?? []
      : [];
    const message = (options.preserveMessage ? composerDraft : composerDraft.trim())
      || (selectedAttachments.length ? '请查看附件。' : '');
    const pendingQuestionAnswer = Boolean(message.trim() && matchesAuthoritativeQuestion);
    const steering = Boolean(activeRoomTurn && !pendingQuestionAnswer);
    if (steering && selectedAttachments.length) {
      setRoomError(room.id, '当前回合进行中时可以立即干预文字；图片请在下一轮发送。');
      return false;
    }
    if (!message.trim() || roomSendLocksRef.current.has(room.id)) return false;
    const addressedParticipants = pendingQuestionAnswer
      ? []
      : roomMentionedParticipants(activeParticipants, message, participantAliases);
    if (steering && addressedParticipants.length > 1) {
      setRoomError(room.id, '当前回合只能点名一位伙伴，请只保留一个 @伙伴。');
      return false;
    }
    const steerParticipantId = steering
      ? addressedParticipants[0]?.id ?? activeRoomTurn?.participantIds[0] ?? activeParticipants[0]?.id ?? ''
      : '';
    if (steering && !steerParticipantId) {
      setRoomError(room.id, '当前回合还没有可点名的伙伴，请稍后重试。');
      return false;
    }
    const clientMessageId = `room-web-${crypto.randomUUID()}`;
    roomTimelineFollowIntentRef.current = true;
    roomSendLocksRef.current.add(room.id);
    setSendingRoomIds((current) => new Set(current).add(room.id));
    if (!steering) {
      useRoomLiveStore.getState().appendOptimistic(
        room.id,
        {
          clientMessageId,
          text: message,
          attachments: selectedAttachments,
          nowMs: Date.now(),
          ...(pendingQuestionAnswer && authoritativeQuestion
            ? { answerToPostId: authoritativeQuestion.postId }
            : {}),
          ...(options.retryOfRootId
            ? { retryOfRootId: options.retryOfRootId }
            : {}),
        },
      );
    }
    if (includeComposerState) {
      updateRoomDraft(room.id, '');
      if (!pendingQuestionAnswer) updateRoomAttachments(room.id, []);
    }
    setRoomError(room.id, '');
    try {
      const response = await transport.request<Record<string, unknown>>(steering && activeRoomTurn
        ? {
            pathId: 'agent.room.participant.steer',
            params: { roomId: room.id },
            body: {
              action: 'steer_participant',
              rootId: activeRoomTurn.rootId ?? activeRoomTurn.id,
              participantId: steerParticipantId,
              clientActionId: clientMessageId,
              message,
            },
          }
        : {
            pathId: 'agent.room.message',
            params: { roomId: room.id },
            body: {
              message,
              clientMessageId,
              ...(options.retryOfRootId
                ? { retryOfRootId: options.retryOfRootId }
                : {}),
              attachmentIds: selectedAttachments.map((attachment) => attachment.mediaId),
              ...(pendingQuestionAnswer && authoritativeQuestion
                ? {
                    answerToPostId: authoritativeQuestion.postId,
                    answerToRootId: authoritativeQuestion.rootId,
                  }
                : {}),
              ...(addressedParticipants.length
                ? { participantIds: addressedParticipants.map((participant) => participant.id) }
                : {}),
              ...(room.roomKind !== 'roleplay' && activeWork?.id
                ? { workItemId: activeWork.id }
                : {}),
            },
          });
      useRoomLiveStore.getState().acceptMessage(room.id, response);
      const workItem = record(response).workItem;
      if (isRoomWorkItem(workItem) && workItem.roomId === room.id) {
        setRooms((current) => current.map((item) => item.id === room.id
          ? {
              ...item,
              workItems: [
                ...(item.workItems ?? []).filter((candidate) => candidate.id !== workItem.id),
                workItem,
              ],
            }
          : item));
      }
      return true;
    } catch (requestError) {
      if (!steering) {
        useRoomLiveStore.getState().discardOptimistic(room.id, clientMessageId);
      }
      if (includeComposerState) {
        updateRoomDraft(room.id, roomDraftsRef.current.get(room.id) || composerDraft);
        if (!pendingQuestionAnswer) {
          updateRoomAttachments(room.id, (current) => {
            const restored = new Map(current.map((item) => [item.mediaId, item]));
            for (const item of selectedAttachments) {
              if (!restored.has(item.mediaId)) restored.set(item.mediaId, item);
            }
            return [...restored.values()].slice(0, ROOM_ATTACHMENT_LIMIT);
          });
        }
      }
      setRoomError(room.id, publicErrorText(requestError, '消息暂时未发送，请稍后重试。'));
      return false;
    } finally {
      roomSendLocksRef.current.delete(room.id);
      setSendingRoomIds((current) => {
        const next = new Set(current);
        next.delete(room.id);
        return next;
      });
    }
  }
  async function abortRootTurn(turnId: string): Promise<void> {
    if (!room || !turnId || abortingTurnIds.has(turnId)) return;
    setAbortingTurnIds((current) => new Set(current).add(turnId));
    setRoomError(room.id, '');
    try {
      const receipt = await transport.request<Record<string, unknown>>({
        pathId: 'agent.room.abort',
        params: { roomId: room.id },
        body: {
          roomTurnId: turnId,
          clientRequestId: `room-abort-${crypto.randomUUID()}`,
        },
      });
      if (receipt.ok !== true || receipt.status === 'cancellation_pending') {
        const pending = cancellationTargetLabels(receipt.pendingTargets);
        setRoomError(
          room.id,
          pending.length > 0
            ? `停止信号已送达，但仍在确认：${pending.join('、')}。可再次停止，界面不会把它误报为已结束。`
            : '停止信号已送达，但后台尚未返回完整终止凭据。可再次停止。',
        );
        return;
      }
      useRoomLiveStore.getState().abortTurn(
        room.id,
        turnId,
        Date.now(),
      );
    } catch (requestError) {
      setRoomError(room.id, publicErrorText(requestError, '暂时无法停止整条协作任务，请稍后重试。'));
    } finally {
      setAbortingTurnIds((current) => {
        const next = new Set(current);
        next.delete(turnId);
        return next;
      });
    }
  }
  function beginCreateRoom(): void {
    const eligiblePersonas = personas.filter((persona) => persona.selectableModes.includes('coordinator'));
    if (
      eligiblePersonas.length < 2
      && personas.filter((persona) => persona.selectableModes.includes('assistant')).length < 2
    ) {
      setError('至少需要两位可用伙伴，才能开始多人协作。');
      return;
    }
    const timelineDefaults = ['companion-future-v1', 'companion-present-v1', 'companion-firstlight-v1']
      .map((roleId) => eligiblePersonas.find((persona) => persona.roleId === roleId)?.roleId)
      .filter((roleId): roleId is string => Boolean(roleId));
    const defaults = [
      ...timelineDefaults,
      ...eligiblePersonas
        .map((persona) => persona.roleId)
        .filter((roleId) => !timelineDefaults.includes(roleId)),
    ].slice(0, 4);
    setError('');
    setCreateError('');
    setCreateTitle('');
    setCreateRoomKind('collaboration');
    setCreatePermissionPolicy(defaultRoomPermissionPolicy('collaboration'));
    setCreateAvatar('briefcase');
    setCreateDescription('');
    setCreateScenarioPrompt('');
    setWorkspaceRoots(projectPaths.slice(0, 1));
    setSelectedRoleIds(defaults);
    setCoordinatorRoleId(defaults[0] ?? '');
    setCreateOpen(true);
  }
  function updateCreateRoomKind(kind: RoomKind): void {
    const requiredMode = kind === 'collaboration' ? 'coordinator' : 'assistant';
    const eligible = personas.filter((persona) => persona.selectableModes.includes(requiredMode));
    const timelineDefaults = ['companion-future-v1', 'companion-present-v1', 'companion-firstlight-v1']
      .map((roleId) => eligible.find((persona) => persona.roleId === roleId)?.roleId)
      .filter((roleId): roleId is string => Boolean(roleId));
    const defaults = [
      ...timelineDefaults,
      ...eligible
        .map((persona) => persona.roleId)
        .filter((roleId) => !timelineDefaults.includes(roleId)),
    ].slice(0, 4);
    setCreateRoomKind(kind);
    setCreatePermissionPolicy(defaultRoomPermissionPolicy(kind));
    setCreateAvatar(kind === 'roleplay' ? 'sparkles' : 'briefcase');
    setSelectedRoleIds(defaults);
    setCoordinatorRoleId(defaults[0] ?? '');
    setCreateError('');
  }
  async function pickWorkspaceRoot(): Promise<void> {
    if (workspacePicking || creating) return;
    if (!transport.pickFiles) {
      setCreateError('当前页面不能选择本地起始项目，请在桌面应用中选择。');
      return;
    }
    setWorkspacePicking(true);
    setCreateError('');
    try {
      const picked = await transport.pickFiles({
        purpose: 'workspace-root',
        selection: 'directory',
        multiple: false,
        maxFiles: 1,
      });
      const roots = uniquePaths(picked.map((item) => item.path ?? ''));
      if (roots.length) {
        setWorkspaceRoots(roots);
        setProjectPaths((current) => uniquePaths([...roots, ...current]));
      }
    } catch (pickError) {
      setCreateError(publicErrorText(pickError, '工作区选择失败，请稍后重试。'));
    } finally {
      setWorkspacePicking(false);
    }
  }
  function toggleParticipant(roleId: string): void {
    setCreateError('');
    setSelectedRoleIds((current) => {
      const next = current.includes(roleId)
        ? current.filter((item) => item !== roleId)
        : current.length < 4 ? [...current, roleId] : current;
      if (!next.includes(coordinatorRoleId)) setCoordinatorRoleId(next[0] ?? '');
      return next;
    });
  }
  async function createRoom(): Promise<void> {
    if (creating) return;
    const participants = selectedRoleIds
      .map((roleId) => personas.find((persona) => persona.roleId === roleId))
      .filter((persona): persona is AgentPersonaV1 => Boolean(persona))
      .map((persona) => ({
        roleId: persona.roleId,
        roleVersion: persona.version,
        displayName: persona.displayName,
        ...(createRoomKind === 'collaboration'
          ? {
              collaborationRole: recommendedCreateRole(
                persona.roleId,
                selectedRoleIds,
                coordinatorRoleId,
              ),
            }
          : {}),
      }));
    if (participants.length < 2) {
      setCreateError('请选择 2 至 4 位伙伴。');
      return;
    }
    const title = createTitle.trim();
    if (!title) return;
    setCreating(true);
    setCreateError('');
    try {
      const response = await transport.request<Record<string, unknown>>({
        pathId: 'agent.rooms.create',
        body: {
          title,
          roomKind: createRoomKind,
          avatar: createAvatar,
          description: createDescription.trim(),
          scenarioPrompt: createScenarioPrompt.trim(),
          participants,
          routingPolicy: createRoomKind === 'collaboration' ? 'parallel' : 'natural',
          workspaceRoots: createRoomKind === 'collaboration'
            ? createPermissionPolicy.room.executionMode === 'per_action'
              || createPermissionPolicy.room.executionMode === 'full_trust'
              ? unrestrictedWorkspaceRoots(...workspaceRoots)
              : uniquePaths(workspaceRoots)
            : [],
          permissionPolicy: createPermissionPolicy,
          ...(roomPermissionPolicyNeedsWorkspaceConfirmation(createPermissionPolicy)
            ? { workspaceScopeConfirmation: 'APPROVE_WORKSPACE_SCOPE' }
            : {}),
          ...(roomPermissionPolicyNeedsDangerousConfirmation(createPermissionPolicy)
            ? { dangerousModeConfirmation: 'ENABLE_FULL_TRUST' }
            : {}),
          routingConfig: { maxResponders: 1, naturalJitter: createRoomKind === 'roleplay' ? 0.04 : 0, fallbackParticipantId: '' },
        },
      });
      const created = isRoom(record(response).room) ? record(response).room as unknown as RoomSummary : undefined;
      if (!created) throw new Error('服务端没有返回可验证的协作空间。');
      setRooms((current) => [created, ...current]);
      selectRoomId(created.id);
      setCreateOpen(false);
    } catch (requestError) { setCreateError(publicErrorText(requestError, '协作空间暂时无法创建，请稍后重试。')); }
    finally { setCreating(false); }
  }
  function requestRoomArchiveChange(): void {
    if (!room || archiving) return;
    setError('');
    if (room.status === 'archived') {
      void updateRoomArchiveState(false);
      return;
    }
    setArchiveOpen(true);
  }

  async function updateRoomArchiveState(archived: boolean): Promise<void> {
    if (!room || archiving) return;
    setArchiving(true);
    setError('');
    try {
      const response = await transport.request<Record<string, unknown>>({
        pathId: 'agent.room.archive',
        params: { roomId: room.id },
        body: { archived },
      });
      const updated = isRoom(record(response).room) ? record(response).room as unknown as RoomSummary : undefined;
      const expectedStatus = archived ? 'archived' : 'active';
      if (!updated || updated.status !== expectedStatus) throw new Error(`服务端没有确认协作空间已${archived ? '收起' : '恢复'}。`);
      const nextRooms = archived && !includeArchived
        ? rooms.filter((item) => item.id !== room.id)
        : rooms.map((item) => item.id === room.id ? updated : item);
      setRooms(nextRooms);
      selectRoomId(nextRooms.some((item) => item.id === room.id) ? room.id : nextRooms[0]?.id ?? '');
      setArchiveOpen(false);
    } catch (requestError) { setError(publicErrorText(requestError, '协作空间状态暂时无法更新，请稍后重试。')); }
    finally { setArchiving(false); }
  }
  function beginRoomSettings(): void {
    if (!room) return;
    setSettingsTitle(room.title);
    setSettingsAvatar(room.avatar ?? (room.roomKind === 'roleplay' ? 'sparkles' : 'briefcase'));
    setSettingsDescription(room.description ?? '');
    setSettingsScenarioPrompt(room.scenarioPrompt ?? '');
    setSettingsPermissionPolicy(storedRoomPermissionPolicy);
    setSettingsError('');
    setSettingsOpen(true);
  }
  async function saveRoomSettings(): Promise<void> {
    if (!room || settingsSaving || !settingsTitle.trim() || !roomSettingsChanged) return;
    setSettingsSaving(true);
    setSettingsError('');
    try {
      const permissionPolicyChanged = !roomPermissionPoliciesEqual(
        settingsPermissionPolicy,
        storedRoomPermissionPolicy,
      );
      const response = await transport.request<Record<string, unknown>>({
        pathId: 'agent.room.archive',
        params: { roomId: room.id },
        body: {
          title: settingsTitle.trim(),
          avatar: settingsAvatar,
          description: settingsDescription.trim(),
          scenarioPrompt: settingsScenarioPrompt.trim(),
          routingPolicy: room.routingPolicy,
          routingConfig: room.routingConfig ?? { maxResponders: 1, naturalJitter: room.roomKind === 'roleplay' ? 0.04 : 0, fallbackParticipantId: '' },
          ...(settingsPermissionPolicy ? { permissionPolicy: settingsPermissionPolicy } : {}),
          ...(permissionPolicyChanged
            && settingsPermissionPolicy
            && roomPermissionPolicyNeedsWorkspaceConfirmation(settingsPermissionPolicy)
            ? { workspaceScopeConfirmation: 'APPROVE_WORKSPACE_SCOPE' }
            : {}),
          ...(permissionPolicyChanged
            && settingsPermissionPolicy
            && roomPermissionPolicyNeedsDangerousConfirmation(settingsPermissionPolicy)
            ? { dangerousModeConfirmation: 'ENABLE_FULL_TRUST' }
            : {}),
        },
      });
      const updated = isRoom(record(response).room) ? record(response).room as unknown as RoomSummary : undefined;
      if (!updated) throw new Error('服务端没有返回更新后的协作空间。');
      setRooms((current) => current.map((item) => item.id === updated.id ? updated : item));
      setSettingsOpen(false);
    } catch (requestError) {
      setSettingsError(publicErrorText(requestError, '协作空间设置暂时无法保存，请稍后重试。'));
    } finally {
      setSettingsSaving(false);
    }
  }
  async function addRoomParticipant(persona: AgentPersonaV1): Promise<void> {
    if (!room || room.status !== 'active' || memberSavingRoleId || activeParticipants.length >= 4) return;
    setMemberSavingRoleId(persona.roleId);
    setSettingsError('');
    try {
      const response = await transport.request<Record<string, unknown>>({
        pathId: 'agent.room.participant.add',
        params: { roomId: room.id },
        body: {
          roleId: persona.roleId,
          roleVersion: persona.version,
          collaborationRole: 'implementer',
        },
      });
      const updated = isRoom(record(response).room) ? record(response).room as unknown as RoomSummary : undefined;
      if (!updated) throw new Error('服务端没有返回加入伙伴后的协作空间。');
      setRooms((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (requestError) {
      setSettingsError(publicErrorText(requestError, '这位伙伴暂时无法加入，请稍后重试。'));
    } finally {
      setMemberSavingRoleId('');
    }
  }
  async function removeRoomParticipant(participant: RoomParticipant): Promise<void> {
    if (!room || room.status !== 'active' || memberRemovingId) return;
    setMemberRemovingId(participant.id);
    setSettingsError('');
    try {
      const response = await transport.request<Record<string, unknown>>({
        pathId: 'agent.room.participant.remove',
        params: { roomId: room.id },
        body: { participantId: participant.id },
      });
      const updated = isRoom(record(response).room) ? record(response).room as unknown as RoomSummary : undefined;
      if (!updated) throw new Error('服务端没有返回移出伙伴后的协作空间。');
      setRooms((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (requestError) {
      setSettingsError(publicErrorText(requestError, '这位伙伴暂时无法移出；请先完成或转交她名下的工作。'));
    } finally {
      setMemberRemovingId('');
    }
  }
  async function updateRoomParticipantRole(
    participant: RoomParticipant,
    collaborationRole: RoomCollaborationRole,
  ): Promise<void> {
    if (!room || room.status !== 'active' || memberUpdatingId || participant.collaborationRole === collaborationRole) return;
    setMemberUpdatingId(participant.id);
    setSettingsError('');
    try {
      const response = await transport.request<Record<string, unknown>>({
        pathId: 'agent.room.participant.update',
        params: { roomId: room.id },
        body: { participantId: participant.id, collaborationRole },
      });
      const updated = isRoom(record(response).room) ? record(response).room as unknown as RoomSummary : undefined;
      if (!updated) throw new Error('服务端没有返回更新后的伙伴分工。');
      setRooms((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (requestError) {
      setSettingsError(publicErrorText(requestError, '分工暂时无法修改；请先等待这位伙伴完成当前回合。'));
    } finally {
      setMemberUpdatingId('');
    }
  }
  async function deleteRoomPermanently(): Promise<void> {
    if (!room || room.status !== 'archived' || deleting || deleteConfirmTitle !== room.title) return;
    setDeleting(true);
    setSettingsError('');
    try {
      await transport.request({
        pathId: 'agent.room.delete',
        params: { roomId: room.id },
        body: { confirmTitle: deleteConfirmTitle },
      });
      const nextRooms = rooms.filter((item) => item.id !== room.id);
      useRoomLiveStore.getState().remove(room.id);
      roomDraftsRef.current.delete(room.id);
      roomErrorsRef.current.delete(room.id);
      setRooms(nextRooms);
      selectRoomId(nextRooms[0]?.id ?? '');
      setDeleteOpen(false);
      setSettingsOpen(false);
      setDeleteConfirmTitle('');
    } catch (requestError) {
      setSettingsError(publicErrorText(requestError, '暂时无法永久删除这个协作空间；请确认它已收起，并且没有未完成任务。'));
      setDeleteOpen(false);
    } finally {
      setDeleting(false);
    }
  }
  async function createTopic(): Promise<void> {
    if (!room || topicSaving || !topicTitle.trim()) return;
    setTopicSaving(true);
    setTopicError('');
    try {
      const response = await transport.request<Record<string, unknown>>({
        pathId: topicEditingId ? 'agent.room.topic.update' : 'agent.room.topic.create',
        params: { roomId: room.id },
        body: {
          ...(topicEditingId ? { topicId: topicEditingId } : {}),
          title: topicTitle.trim(),
          summary: topicSummary.trim(),
        },
      });
      const updated = isRoom(record(response).room) ? record(response).room as unknown as RoomSummary : undefined;
      if (!updated) throw new Error('服务端没有返回更新后的话题。');
      setRooms((current) => current.map((item) => item.id === updated.id ? updated : item));
      setTopicTitle('');
      setTopicSummary('');
      setTopicEditingId('');
    } catch (requestError) {
      setTopicError(publicErrorText(requestError, '话题暂时无法创建，请稍后重试。'));
    } finally {
      setTopicSaving(false);
    }
  }
  async function updateTopic(topicId: string, changes: Record<string, string | boolean>): Promise<void> {
    if (!room || topicSaving) return;
    setTopicSaving(true);
    setTopicError('');
    try {
      const response = await transport.request<Record<string, unknown>>({
        pathId: 'agent.room.topic.update',
        params: { roomId: room.id },
        body: { topicId, ...changes },
      });
      const updated = isRoom(record(response).room) ? record(response).room as unknown as RoomSummary : undefined;
      if (!updated) throw new Error('服务端没有返回更新后的话题。');
      setRooms((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (requestError) {
      setTopicError(publicErrorText(requestError, '话题状态暂时无法更新，请稍后重试。'));
    } finally {
      setTopicSaving(false);
    }
  }
  async function addRoomArtifact(): Promise<void> {
    if (!room || artifactPicking) return;
    const sourceRoomId = room.id;
    if (!room.workspaceRoots?.length) {
      setRoomError(sourceRoomId, '“一起聊聊”不会访问项目目录；请在任务协作空间中分享工作文件。');
      return;
    }
    if (!transport.pickFiles) {
      setRoomError(sourceRoomId, '当前平台不能选择共享文件，请在桌面控制中心中操作。');
      return;
    }
    setArtifactPicking(true);
    setRoomError(sourceRoomId, '');
    try {
      const selected = await transport.pickFiles({
        purpose: 'room-artifact',
        selection: 'file',
        multiple: false,
        maxFiles: 1,
      });
      const file = selected[0];
      if (!file?.path) return;
      const response = await transport.request<Record<string, unknown>>({
        pathId: 'agent.room.artifact.add',
        params: { roomId: sourceRoomId },
        body: {
          path: file.path,
          displayName: file.name,
          mediaType: file.mimeType,
          topicId: room.activeTopicId ?? '',
        },
      });
      const updated = isRoom(record(response).room) ? record(response).room as unknown as RoomSummary : undefined;
      if (!updated) throw new Error('服务端没有返回新增的共享文件。');
      setRooms((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (requestError) {
      setRoomError(sourceRoomId, publicErrorText(requestError, '暂时无法分享这个文件，请确认它位于已授权的工作目录中。'));
    } finally {
      setArtifactPicking(false);
    }
  }
  return <>
    <main className="rooms-feature" data-paw-agent-workbench={pawOsWorkbench || undefined} data-route-id="rooms" data-rail-open={roomRailOpen} data-status-open={statusOpen}>
      <h1 className="rooms-feature__title">多人协作</h1>
      <aside ref={roomRailRef} className="rooms-rail" id="rooms-list-drawer" aria-hidden={roomStatusModal || undefined} aria-label="协作空间列表" inert={roomStatusModal ? true : undefined} role={roomRailModal ? 'dialog' : undefined} aria-modal={roomRailModal ? true : undefined}>
        <header><span><strong>协作空间</strong><small>和伙伴一起聊，也一起把事做完</small></span><div className="rooms-rail-actions"><IconButton label={includeArchived ? '隐藏已收起的协作空间' : '显示已收起的协作空间'} icon={includeArchived ? <ArchiveRestore size={16} /> : <Archive size={16} />} aria-pressed={includeArchived} onClick={() => setIncludeArchived((current) => !current)} tooltip /><IconButton disabled={catalogLoading || creating} label="开始新的协作" icon={<MessageSquarePlus size={17} />} onClick={() => { closeRoomRailIfOverlay(false); beginCreateRoom(); }} tooltip /></div><IconButton ref={roomRailCloseRef} className="rooms-rail-mobile-close" label="关闭协作空间列表" icon={<X size={17} />} onClick={() => closeRoomRail()} tooltip /></header>
        <div>{rooms.length ? rooms.map((item) => <button type="button" key={item.id} aria-label={`打开协作空间：${item.title}`} aria-current={item.id === selectedId} onClick={() => { selectRoomId(item.id); closeRoomRailIfOverlay(); }}>{roomAvatarIcon(item)}<span><strong>{item.title}</strong><small>{item.status === 'archived' ? '已收起 · ' : ''}{item.participants.filter((participant) => participant.status === 'active').map(roomParticipantPlanetName).join(' · ')}</small></span></button>) : !catalogLoading ? <p className="rooms-rail-empty">还没有协作空间</p> : null}</div>
      </aside>
      <RoomPaneResizer side="rail" />
      <button className="rooms-rail-backdrop" aria-hidden="true" disabled={!roomRailModal} tabIndex={-1} onClick={() => closeRoomRail()} type="button" />
      <section ref={roomWorkspaceRef} className="room-workspace" aria-hidden={roomRailModal || roomStatusModal || undefined} inert={roomRailModal || roomStatusModal ? true : undefined}>
        <header><IconButton ref={roomRailTriggerRef} className="rooms-rail-trigger" label={roomRailOpen ? '收起协作空间列表' : '打开协作空间列表'} icon={roomRailOpen ? <PanelLeftClose size={17} /> : <PanelLeftOpen size={17} />} aria-controls="rooms-list-drawer" aria-expanded={roomRailOpen} onClick={() => { if (!roomRailOpen && roomStatusModal) setStatusOpen(false); setRoomRailOpen((current) => !current); }} tooltip /><span><strong>{room?.title ?? '协作空间'}</strong><small>{!room ? '选一个协作空间，或开始新的对话' : room.status === 'archived' ? '已收起' : `${room.roomKind === 'roleplay' ? '一起聊聊' : roomPathName(room)} · ${roomHeadlineState}`}</small></span><SegmentedControl aria-label="协作空间视图" items={roomWorkspaceViewOptions(room?.roomKind)} onValueChange={(value) => setWorkspaceView(value as typeof workspaceView)} value={workspaceView} /><div className="room-header-actions"><span className="room-header-actions__desktop">{room && pawOsDesktop ? <IconButton label="在独立窗口中打开这个 Room" icon={<PanelsTopLeft size={16} />} onClick={() => pawOsDesktop.openWindow({ appId: 'agent', target: { kind: 'room', id: room.id, title: room.title, subtitle: `${room.participants.filter((participant) => participant.status === 'active').length} 位伙伴 · ${roomWorkStateLabel(activeWork?.state ?? 'queued')}` } })} tooltip /> : null}{room ? <IconButton label="设置这个协作空间" icon={<Settings2 size={16} />} onClick={beginRoomSettings} tooltip /> : null}{room ? <Menu><MenuTrigger asChild><IconButton label="更多协作空间操作" icon={<MoreHorizontal size={17} />} tooltip /></MenuTrigger><MenuContent align="end"><MenuItem onSelect={requestRoomArchiveChange}>{room.status === 'archived' ? <ArchiveRestore size={15} /> : <Archive size={15} />}{room.status === 'archived' ? '恢复协作空间' : '收起协作空间'}</MenuItem></MenuContent></Menu> : null}</span>{room ? <span className="room-header-actions__mobile"><Menu><MenuTrigger asChild><IconButton label="更多协作空间操作" icon={<MoreHorizontal size={17} />} tooltip /></MenuTrigger><MenuContent align="end">{pawOsDesktop ? <MenuItem onSelect={() => pawOsDesktop.openWindow({ appId: 'agent', target: { kind: 'room', id: room.id, title: room.title, subtitle: `${room.participants.filter((participant) => participant.status === 'active').length} 位伙伴` } })}><PanelsTopLeft size={15} />独立窗口</MenuItem> : null}<MenuItem onSelect={beginRoomSettings}><Settings2 size={15} />设置协作空间</MenuItem><MenuItem onSelect={requestRoomArchiveChange}>{room.status === 'archived' ? <ArchiveRestore size={15} /> : <Archive size={15} />}{room.status === 'archived' ? '恢复协作空间' : '收起协作空间'}</MenuItem></MenuContent></Menu></span> : null}<IconButton ref={roomStatusToggleRef} label={statusOpen ? '关闭协作进展' : '看看协作进展'} icon={statusOpen ? <PanelRightClose size={17} /> : <PanelRightOpen size={17} />} onClick={() => { if (!statusOpen && roomRailModal) setRoomRailOpen(false); setStatusOpen((current) => !current); }} tooltip /></div></header>
        {room ? <div className="room-context-bar">
          <div className="room-topic-tabs" aria-label="协作话题">
            <MessagesSquare size={14} />
            {(room.topics ?? []).filter((topic) => topic.status === 'active').map((topic) => <button type="button" key={topic.id} aria-current={topic.id === room.activeTopicId} onClick={() => { if (topic.id !== room.activeTopicId) void updateTopic(topic.id, { activate: true }); }}>{topic.title}</button>)}
            <IconButton label="管理话题" icon={<Plus size={14} />} disabled={topicSaving} onClick={() => { setTopicError(''); setTopicsOpen(true); }} tooltip />
          </div>
          <div className="room-context-actions">
            {activeWork ? <span className="room-work-summary" data-state={activeWork.state} title={activeWork.objective}><GitBranch size={13} />{roomWorkStateLabel(activeWork.state)} · {participantName(room, activeWork.currentOwnerParticipantId)} · {activeWork.objective}</span> : <span>{room.description || (room.roomKind === 'roleplay' ? '让几位伙伴一起聊聊' : '先聊清楚，再一起把事情做完')}</span>}
            {room.roomKind !== 'roleplay' ? <IconButton label="分享工作文件" icon={artifactPicking ? <LoaderCircle className="ui-spin" size={15} /> : <FilePlus2 size={15} />} disabled={artifactPicking || room.status !== 'active'} onClick={() => void addRoomArtifact()} tooltip /> : null}
          </div>
        </div> : <div aria-hidden="true" className="room-context-bar room-context-bar--empty" />}
        <div className="room-error-slot" aria-live="polite">
          {error ? selectedRoomErrorSource === 'connection' && selectedRoomRecoveryState !== 'synced'
            ? selectedRoomRecoveryState === 'recovering'
              ? <p className="room-catalog-warning" role="status">正在恢复协作进度；已显示的对话不会丢失。</p>
              : <div className="room-error room-error--action" role="alert"><span>{error}</span><Button leadingIcon={<RefreshCw size={14} />} onClick={retryRoomSnapshot} size="small" variant="quiet">重试同步</Button></div>
            : <p className="room-error" role="alert">{error}</p>
            : null}
          {!error && roomCatalogError ? <p className="room-error" role="alert">{roomCatalogError}</p> : null}
          {!error && !roomCatalogError && roleCatalogError ? <div className="room-catalog-warning room-catalog-warning--action" role="status"><span>{roleCatalogError}</span><Button disabled={catalogLoading} leadingIcon={<RefreshCw size={14} />} onClick={() => setCatalogReloadRevision((revision) => revision + 1)} size="small" variant="quiet">重新同步角色</Button></div> : null}
        </div>
        {workspaceView === 'posts' ? <><div className="room-timeline" aria-label="协作对话时间线">
          {room ? visibleTurnOrder.length ? <Virtuoso
            ref={roomTimelineRef}
            context={{ historyLoadRevision, roomId: room.id }}
            components={roomTimelineComponents}
            computeItemKey={(_index, turnId) => turnId}
            data={visibleTurnOrder}
            atBottomStateChange={(atBottom) => {
              if (atBottom) roomTimelineFollowIntentRef.current = true;
            }}
            followOutput={false}
            initialTopMostItemIndex={{ index: 'LAST', align: 'end' }}
            increaseViewportBy={300}
            scrollerRef={handleRoomTimelineScrollerRef}
            startReached={() => setHistoryLoadRevision((revision) => revision + 1)}
            itemContent={(_index, turnId) => <RoomTurn
              key={turnId}
              turnId={turnId}
              roomId={room.id}
              room={room}
              personas={personas}
              abortingTurnIds={abortingTurnIds}
              roomSyncState={selectedRoomRecoveryState}
              onAbortTurn={(rootId) => void abortRootTurn(rootId)}
              retryingTurn={sendingRoomIds.has(room.id)}
              onRetryTurn={room.status === 'active'
                ? (message, retryOfRootId) => void send(message, {
                    preserveMessage: true,
                    includeComposerState: false,
                    retryOfRootId,
                  })
                : undefined}
              onAnswerQuestion={room.status === 'active'
                ? async (question, value) => {
                    if (question.roomId !== room.id) return false;
                    const accepted = await send(value, {
                      preserveMessage: true,
                      includeComposerState: false,
                      question,
                    });
                    if (accepted) requestAnimationFrame(() => roomComposerRef.current?.focus());
                    return accepted;
                  }
                : undefined}
              onApprovalDecision={room.status === 'active'
                ? (approvalId, decision, payloadSha256) => decideRoomApproval(
                    room.id,
                    approvalId,
                    decision,
                    payloadSha256,
                  )
                : undefined}
            />}
          /> : snapshotLoading
            ? <p className="room-empty">正在读取对话…</p>
            : <EmptyState icon={MessagesSquare} title="还没有公开消息" description="说出你想完成的事；只有遇到会影响实现的歧义，伙伴才会继续提问。" />
            : catalogLoading
              ? <p className="room-empty">正在读取协作空间…</p>
            : <EmptyState icon={MessagesSquare} title="选择一个协作空间" description="从左侧选择，或新建一个协作空间。" />}
          {room && visibleTurnOrder.length ? <nav aria-label="协作时间线导航" className="room-timeline-nav">
            <IconButton
              label="查看较早记录"
              icon={<ListStart size={16} />}
              onClick={() => {
                roomTimelineFollowIntentRef.current = false;
                setHistoryLoadRevision((revision) => revision + 1);
                roomTimelineRef.current?.scrollToIndex({ align: 'start', behavior: 'auto', index: 0 });
              }}
              tooltip
            />
            <IconButton
              label="查看最新进展"
              icon={<ListEnd size={16} />}
              onClick={() => {
                roomTimelineFollowIntentRef.current = true;
                scrollRoomTimelineToLatest(
                  roomTimelineRef.current,
                  roomTimelineScroller,
                  visibleTurnOrder.length - 1,
                );
              }}
              tooltip
            />
            <IconButton
              label="查看任务总览"
              icon={<ListTodo size={16} />}
              onClick={() => setWorkspaceView('execution')}
              tooltip
            />
          </nav> : null}
        </div><div className="room-composer-dock">{pendingGroupedInput ? (
          <GenericUserInputCard
            activity={pendingGroupedInput}
            sessionId={pendingGroupedInput.sourceSessionId}
            onError={(message) => setRoomError(selectedId, message)}
          />
        ) : <div className="room-composer-cluster">{room ? <RoomComposer
          key={room.id}
          inputRef={roomComposerRef}
          room={room}
          participantAliases={participantAliases}
          personas={personas}
          draft={draft}
          attachments={attachments}
          sending={sendingRoomIds.has(room.id)}
          taskBusyState={managedTaskBusyState}
          pendingUserAnswer={pendingQuestion?.roomId === room.id}
          onDraftChange={(value) => {
            const previous = roomDraftsRef.current.get(room.id) ?? '';
            persistRoomDraft(room.id, value);
            if (
              value !== previous
              && roomErrorsRef.current.get(room.id)?.source === 'operation'
            ) setRoomError(room.id, '');
          }}
          onAttachmentsChange={(value) => updateRoomAttachments(room.id, value)}
          onPasteImages={(files) => pasteRoomImages(room.id, files)}
          onPasteFromClipboard={() => void pasteRoomImages(room.id)}
          onPickAttachments={() => void pickRoomImages(room.id)}
          onSend={(value) => {
            const question = pendingQuestion?.roomId === room.id ? pendingQuestion : undefined;
            if (
              activeRoomTurn
              && !question
              && roomMentionedParticipants(activeParticipants, value, participantAliases).length > 1
            ) {
              setRoomError(room.id, '当前回合只能点名一位伙伴，请只保留一个 @伙伴。');
              return false;
            }
            return send(value, { question });
          }}
        /> : !catalogLoading ? <RoomComposer
          room={undefined}
          personas={personas}
          draft=""
          attachments={[]}
          sending={false}
          onDraftChange={() => undefined}
          onSend={() => undefined}
          onAttachmentsChange={() => undefined}
          onPasteImages={() => undefined}
          onPasteFromClipboard={() => undefined}
          onPickAttachments={() => undefined}
        /> : null}</div>}</div></> : workspaceView === 'sessions' ? <section className="room-session-workspace" aria-label="伙伴与权限">
          <header><span><strong>伙伴与工作权限</strong><small>每位伙伴保留自己的工作上下文；分工负责引导协作，真正能做什么仍由工作目录、工具和你的授权决定。</small></span></header>
          <div>{activeParticipants.map((participant) => <article key={participant.id}><span><strong>{roomParticipantPlanetName(participant)}</strong><small>{roomCollaborationRoleLabel(participant.collaborationRole)} · {partnerPermissionPresentation ? `${partnerPermissionPresentation.effectiveLabel} · ${partnerPermissionPresentation.inheritanceLabel}` : '分层权限不可用'}</small></span><Button variant="quiet" size="small" leadingIcon={<ShieldCheck size={14} />} onClick={() => setBoundaryParticipant(participant)}>查看能做什么</Button></article>)}</div>
          {!activeParticipants.length ? <p className="room-empty room-session-workspace__empty">还没有伙伴加入这个协作空间。</p> : null}
        </section> : null}
        <section className="room-execution-workspace room-execution-workspace--cockpit" aria-label="任务流转与验收" hidden={workspaceView !== 'execution'}>
          {workspaceView === 'execution' && room ? <ConnectedRoomTaskGraph projection={selectedRoomProjection} room={room} runtimeWorkItems={runtimeWorkItems} /> : workspaceView === 'execution' ? <p className="room-empty">请选择一个协作空间。</p> : null}
        </section>
      </section>
      <button className="agent-status-backdrop room-status-backdrop" aria-hidden="true" disabled={!roomStatusModal} tabIndex={-1} onClick={() => setStatusOpen(false)} type="button" />
      <RoomPaneResizer side="status" />
      <RoomStatusPanel
        ref={roomStatusRef}
        room={room}
        roomId={room?.id ?? ''}
        open={statusOpen}
        modal={roomStatusModal}
        onClose={() => setStatusOpen(false)}
      />
    </main>
    <Dialog open={createOpen} onOpenChange={(open) => { if (!creating) { setCreateOpen(open); if (!open) setCreateError(''); } }}>
      <DialogContent className="room-create-dialog">
        <DialogHeader><DialogTitle>开始一起做事</DialogTitle><DialogDescription>先选这次是要完成任务，还是只想和几位伙伴聊聊。任务会先对齐目标，再开始执行。</DialogDescription></DialogHeader>
        <form id="room-create-form" className="room-create-form" onSubmit={(event) => { event.preventDefault(); void createRoom(); }}>
          {createError ? <p className="room-dialog-error" role="alert">{createError}</p> : null}
          <fieldset><legend>这次想怎么一起</legend><div className="room-kind-options">
            <label><input type="radio" name="room-kind" checked={createRoomKind === 'collaboration'} onChange={() => updateCreateRoomKind('collaboration')} /><span><BriefcaseBusiness size={17} /><strong>一起完成任务</strong><small>对齐目标后，伙伴可以分工、交接和验收</small></span></label>
            <label><input type="radio" name="room-kind" checked={createRoomKind === 'roleplay'} onChange={() => updateCreateRoomKind('roleplay')} /><span><Sparkles size={17} /><strong>一起聊聊</strong><small>只共享对话背景，不会访问项目文件</small></span></label>
          </div></fieldset>
          <label className="room-create-field"><span>取个名字 <small>必填</small></span><input maxLength={120} value={createTitle} onChange={(event) => { setCreateTitle(event.target.value); setCreateError(''); }} placeholder={createRoomKind === 'roleplay' ? '例如：深夜茶话会' : '例如：发布前检查'} aria-label="协作空间名称" /></label>
          <fieldset><legend>邀请伙伴 <small>至少 2 位 · {selectedRoleIds.length}/4</small></legend><div className="room-role-options">{personas.filter((persona) => persona.selectableModes.includes(createRoomKind === 'roleplay' ? 'assistant' : 'coordinator')).map((persona, index) => { const checked = selectedRoleIds.includes(persona.roleId); return <label key={`${persona.roleId}:${persona.version}`}><input type="checkbox" checked={checked} disabled={!checked && selectedRoleIds.length >= 4} onChange={() => toggleParticipant(persona.roleId)} /><span><strong>{roomCandidatePlanetName(index)}<em>{roomCreateParticipantLabel(createRoomKind, checked, persona.roleId, selectedRoleIds, coordinatorRoleId)}</em></strong><small>{persona.tagline}</small></span></label>; })}</div></fieldset>
          {createRoomKind === 'collaboration' ? <p className="room-create-role-flow">所有伙伴地位平等，可以直接互相 @、提问和回复；其中一位只在最后负责 Root 汇合与最终回复，不承担消息转发。</p> : null}
          {createRoomKind === 'collaboration' ? <section className="room-create-projects" aria-label="起始项目">
            <header><strong>起始项目</strong><small>可选，仅用于上下文</small></header>
            <RadioGroup.Root
              aria-label="最近使用的起始项目"
              value={workspaceRoots[0] ?? NO_ROOM_PROJECT}
              onValueChange={(value) => {
                setWorkspaceRoots(value === NO_ROOM_PROJECT ? [] : [value]);
                setCreateError('');
              }}
            >
              <RadioGroup.Item value={NO_ROOM_PROJECT}><MessagesSquare size={16} /><span><strong>不预选项目</strong><small>仍授予整个系统权限</small></span></RadioGroup.Item>
              {projectPaths.map((path) => <RadioGroup.Item key={path} value={path} title={path}><FolderOpen size={16} /><span><strong>{pathName(path)}</strong><small>{path}</small></span></RadioGroup.Item>)}
            </RadioGroup.Root>
            {workspaceRoots[0] && !projectPaths.includes(workspaceRoots[0]) ? <div className="room-create-project-picked" title={workspaceRoots.join('\n')}><FolderOpen size={16} /><span><strong>{pathName(workspaceRoots[0])}</strong><small>{workspaceRoots[0]}</small></span></div> : null}
            <Button type="button" variant="quiet" leadingIcon={workspacePicking ? <LoaderCircle className="ui-spin" size={15} /> : <FolderOpen size={15} />} disabled={workspacePicking || creating} onClick={() => void pickWorkspaceRoot()}>{workspaceRoots.length ? '换一个项目' : '选择起始项目'}</Button>
          </section> : null}
          <fieldset>
            <legend>三层工作权限 <small>只能逐层保持或收窄</small></legend>
            <RoomPermissionPolicyEditor
              onChange={setCreatePermissionPolicy}
              policy={createPermissionPolicy}
              roomKind={createRoomKind}
            />
          </fieldset>
          <Disclosure
            className="room-create-optional"
            summary={<>补充背景与外观 <small>可选</small></>}
          >
            <div>
              <div className="room-create-pair">
                <label className="room-create-field"><span>{createRoomKind === 'roleplay' ? '想聊些什么' : '这次大致想做什么'}</span><input maxLength={500} value={createDescription} onChange={(event) => setCreateDescription(event.target.value)} placeholder={createRoomKind === 'roleplay' ? '给伙伴一个轻松、明确的话题' : '任务细节可以稍后在对话中一起确认'} aria-label="协作空间简介" /></label>
                <label className="room-create-field"><span>图标</span><Select aria-label="协作空间图标" onValueChange={setCreateAvatar} options={roomAvatarOptions()} value={createAvatar} /></label>
              </div>
              <label className="room-create-field"><span>{createRoomKind === 'roleplay' ? '共同背景' : '一起工作的约定'}</span><textarea maxLength={8000} rows={3} value={createScenarioPrompt} onChange={(event) => setCreateScenarioPrompt(event.target.value)} placeholder={createRoomKind === 'roleplay' ? '可以写下彼此关系、共同背景，以及不希望触碰的话题。' : '可以写下项目背景、不能改动的地方和交付习惯。'} aria-label={createRoomKind === 'roleplay' ? '共同背景' : '协作约定'} /></label>
            </div>
          </Disclosure>
        </form>
        <DialogFooter><Button variant="quiet" disabled={creating || workspacePicking} onClick={() => setCreateOpen(false)}>先不开始</Button><Button type="submit" form="room-create-form" variant="primary" loading={creating} disabled={!createTitle.trim() || selectedRoleIds.length < 2 || workspacePicking}>{createRoomKind === 'roleplay' ? '开始群聊' : createPermissionPolicy.room.executionMode === 'full_trust' ? '启用全自动并开始协作' : '开始协作'}</Button></DialogFooter>
      </DialogContent>
    </Dialog>
    <Dialog open={settingsOpen} onOpenChange={(open) => { if (!settingsSaving && !memberSavingRoleId && !memberRemovingId && !memberUpdatingId && !deleting) { setSettingsOpen(open); if (!open) setSettingsError(''); } }}>
      <DialogContent className="room-settings-dialog">
        <DialogHeader><DialogTitle>设置协作空间</DialogTitle><DialogDescription>{room?.roomKind === 'roleplay' ? '名称、共同背景和不抬权的分层权限会在保存后更新；伙伴邀请会单独立即生效。已经发生的对话不会被改写。' : '名称、协作约定和工作权限会在保存后从下一轮生效；伙伴与分工会单独立即更新。'}</DialogDescription></DialogHeader>
        <form id="room-settings-form" className="room-create-form" onSubmit={(event) => { event.preventDefault(); void saveRoomSettings(); }}>
          {settingsError ? <p className="room-dialog-error" role="alert">{settingsError}</p> : null}
          <div className="room-create-pair">
            <label className="room-create-field"><span>名称</span><input maxLength={120} value={settingsTitle} onChange={(event) => setSettingsTitle(event.target.value)} aria-label="协作空间名称" /></label>
            <label className="room-create-field"><span>图标</span><Select aria-label="协作空间图标" onValueChange={setSettingsAvatar} options={roomAvatarOptions()} value={settingsAvatar} /></label>
          </div>
          <label className="room-create-field"><span>简介</span><input maxLength={500} value={settingsDescription} onChange={(event) => setSettingsDescription(event.target.value)} aria-label="协作空间简介" /></label>
          <fieldset>
            <legend>三层工作权限 <small>从服务端策略原样载入</small></legend>
            <RoomPermissionPolicyEditor
              onChange={settingsPermissionPolicy ? setSettingsPermissionPolicy : undefined}
              policy={settingsPermissionPolicy}
              roomKind={room?.roomKind ?? 'collaboration'}
            />
          </fieldset>
          <fieldset className="room-member-manager">
            <legend>伙伴 <small>至少 2 位 · {activeParticipants.length}/4</small></legend>
            <p>这里的邀请、移出与分工调整会立即生效，但不会扩大工具权限。新伙伴从下一轮开始参与，不会补读此前的完整对话；任务中仍可随时点名或正式交接。</p>
            <div>{personas.filter((persona) => persona.selectableModes.includes(room?.roomKind === 'roleplay' ? 'assistant' : 'coordinator')).map((persona, index) => {
              const participant = activeParticipants.find((item) => item.roleId === persona.roleId && item.roleVersion === persona.version);
              const isRequiredModerator = room?.routingPolicy === 'moderator' && participant?.id === room.moderatorParticipantId;
              const mutationPending = Boolean(memberRemovingId || memberSavingRoleId || memberUpdatingId);
              const removeDisabled = !participant || room?.status !== 'active' || activeParticipants.length <= 2 || isRequiredModerator || mutationPending;
              return <article key={`${persona.roleId}:${persona.version}`} data-active={Boolean(participant)}>
                <span><strong>{participant ? roomParticipantPlanetName(participant) : roomCandidatePlanetName(index)}</strong><small>{participant ? room?.roomKind === 'roleplay' ? '一起聊天 · 已加入' : `${roomCollaborationRoleLabel(participant.collaborationRole)} · ${roomCollaborationRoleDescription(participant.collaborationRole)}` : persona.tagline}</small></span>
                <div className="room-member-actions">
                  {participant && room?.roomKind !== 'roleplay' ? <Select aria-label={`${roomParticipantPlanetName(participant)} 负责什么`} disabled={room?.status !== 'active' || mutationPending} onValueChange={(value) => void updateRoomParticipantRole(participant, value as RoomCollaborationRole)} options={roomCollaborationRoleOptions(participant.collaborationRole)} value={participant.collaborationRole ?? 'implementer'} /> : null}
                  {participant ? <IconButton label={`移出 ${roomParticipantPlanetName(participant)}`} icon={memberRemovingId === participant.id ? <LoaderCircle className="ui-spin" size={15} /> : <UserMinus size={15} />} disabled={removeDisabled} onClick={() => void removeRoomParticipant(participant)} tooltip /> : <IconButton label={`邀请 ${roomCandidatePlanetName(index)}`} icon={memberSavingRoleId === persona.roleId ? <LoaderCircle className="ui-spin" size={15} /> : <UserPlus size={15} />} disabled={room?.status !== 'active' || activeParticipants.length >= 4 || mutationPending} onClick={() => void addRoomParticipant(persona)} tooltip />}
                </div>
              </article>;
            })}</div>
          </fieldset>
          <label className="room-create-field"><span>{room?.roomKind === 'roleplay' ? '共同背景' : '一起工作的约定'}</span><textarea maxLength={8000} rows={5} value={settingsScenarioPrompt} onChange={(event) => setSettingsScenarioPrompt(event.target.value)} aria-label={room?.roomKind === 'roleplay' ? '共同背景' : '协作约定'} /></label>
        </form>
        <section className="room-danger-zone"><span><strong>永久删除</strong><small>{room?.status === 'archived' ? '这会删除对话、协作记录和伙伴工作记录，无法恢复。' : '为防止误删，请先收起这个协作空间并结束未完成任务。'}</small></span><Button variant="danger" leadingIcon={<Trash2 size={15} />} disabled={room?.status !== 'archived' || settingsSaving || deleting} onClick={() => { setDeleteConfirmTitle(''); setDeleteOpen(true); }}>删除协作空间</Button></section>
        <DialogFooter><Button variant="quiet" disabled={settingsSaving || Boolean(memberSavingRoleId || memberRemovingId || memberUpdatingId)} onClick={() => setSettingsOpen(false)}>关闭</Button><Button type="submit" form="room-settings-form" variant="primary" loading={settingsSaving} disabled={!settingsTitle.trim() || !roomSettingsChanged || Boolean(memberSavingRoleId || memberRemovingId || memberUpdatingId)}>保存更改</Button></DialogFooter>
      </DialogContent>
    </Dialog>
    <Dialog open={deleteOpen} onOpenChange={(open) => { if (!deleting) { setDeleteOpen(open); if (!open) setDeleteConfirmTitle(''); } }}>
      <DialogContent><DialogHeader><DialogTitle>永久删除这个协作空间？</DialogTitle><DialogDescription>这会删除“{room?.title}”的消息、协作记录和伙伴工作记录。请输入完整名称确认。</DialogDescription></DialogHeader><label className="room-create-field"><span>协作空间名称</span><input autoComplete="off" value={deleteConfirmTitle} onChange={(event) => setDeleteConfirmTitle(event.target.value)} aria-label="输入协作空间名称确认永久删除" /></label><DialogFooter><Button variant="quiet" disabled={deleting} onClick={() => setDeleteOpen(false)}>取消</Button><Button variant="danger" leadingIcon={<Trash2 size={15} />} loading={deleting} disabled={!room || deleteConfirmTitle !== room.title} onClick={() => void deleteRoomPermanently()}>永久删除</Button></DialogFooter></DialogContent>
    </Dialog>
    <Dialog open={topicsOpen} onOpenChange={(open) => { if (!topicSaving) { setTopicsOpen(open); if (!open) { setTopicError(''); setTopicEditingId(''); setTopicTitle(''); setTopicSummary(''); } } }}>
      <DialogContent className="room-topics-dialog">
        <DialogHeader><DialogTitle>整理话题</DialogTitle><DialogDescription>用话题把讨论分开，伙伴和历史不会被复制。切换后，下一条消息会沿用新的话题上下文。</DialogDescription></DialogHeader>
        {topicError ? <p className="room-dialog-error" role="alert">{topicError}</p> : null}
        <div className="room-topic-manager">
          {(room?.topics ?? []).map((topic) => <div key={topic.id} data-archived={topic.status === 'archived'} data-active={topic.id === room?.activeTopicId}><span><strong>{topic.title}</strong><small>{topic.summary || '还没有摘要'}</small></span><div><Button variant="quiet" disabled={topicSaving} onClick={() => { setTopicEditingId(topic.id); setTopicTitle(topic.title); setTopicSummary(topic.summary); }}>编辑</Button>{topic.status === 'active' && topic.id !== room?.activeTopicId ? <Button variant="quiet" disabled={topicSaving} onClick={() => void updateTopic(topic.id, { activate: true })}>聊这个话题</Button> : null}{topic.status === 'active' && topic.id !== room?.activeTopicId ? <IconButton label={`归档话题 ${topic.title}`} icon={<Archive size={15} />} disabled={topicSaving} onClick={() => void updateTopic(topic.id, { archived: true })} tooltip /> : null}</div></div>)}
        </div>
        <form id="room-topic-form" className="room-topic-form" onSubmit={(event) => { event.preventDefault(); void createTopic(); }}>
          <label className="room-create-field"><span>{topicEditingId ? '编辑名称' : '新话题'}</span><input maxLength={120} value={topicTitle} onChange={(event) => setTopicTitle(event.target.value)} placeholder="例如：发布风险" aria-label="话题名称" /></label>
          <label className="room-create-field"><span>摘要</span><textarea maxLength={2000} rows={2} value={topicSummary} onChange={(event) => setTopicSummary(event.target.value)} placeholder="记录这个话题正在解决什么" aria-label="话题摘要" /></label>
        </form>
        <DialogFooter>{topicEditingId ? <Button variant="quiet" disabled={topicSaving} onClick={() => { setTopicEditingId(''); setTopicTitle(''); setTopicSummary(''); }}>取消编辑</Button> : <Button variant="quiet" disabled={topicSaving} onClick={() => setTopicsOpen(false)}>关闭</Button>}<Button type="submit" form="room-topic-form" variant="primary" loading={topicSaving} disabled={!topicTitle.trim()}>{topicEditingId ? '保存话题' : '创建话题'}</Button></DialogFooter>
      </DialogContent>
    </Dialog>
    <Dialog open={archiveOpen} onOpenChange={(open) => { if (!archiving) setArchiveOpen(open); }}>
      <DialogContent><DialogHeader><DialogTitle>先把这个协作空间收起来？</DialogTitle><DialogDescription>“{room?.title}”会从当前列表收起，但对话和交付仍会安全保留在本机。</DialogDescription></DialogHeader>{error ? <p className="room-dialog-error" role="alert">{error}</p> : null}<DialogFooter><Button variant="quiet" disabled={archiving} onClick={() => setArchiveOpen(false)}>保持原样</Button><Button variant="danger" loading={archiving} onClick={() => void updateRoomArchiveState(true)}>收起协作空间</Button></DialogFooter></DialogContent>
    </Dialog>
    <RoomMemberBoundaryDialog
      permissionPolicy={room?.permissionPolicy}
      roomKind={room?.roomKind}
      participant={boundaryParticipant}
      workspaceRoots={room?.workspaceRoots ?? []}
      onClose={() => setBoundaryParticipant(undefined)}
    />
  </>;
}

function roomAttachmentFromPicked(file: PickedFile, roomId: string): RoomAttachmentReceipt {
  if (
    file.roomId !== roomId
    || file.sessionId !== undefined
    || !/^media_[A-Za-z0-9_-]{12,80}$/u.test(file.id)
    || !isComposerAttachmentMimeType(file.mimeType)
    || !Number.isSafeInteger(file.byteSize)
    || file.byteSize < 1
    || file.byteSize > ROOM_ATTACHMENT_MAX_BYTES
    || !file.sha256
    || !/^[0-9a-f]{64}$/u.test(file.sha256)
    || file.path !== undefined
  ) {
    throw new TypeError('Room 附件导入返回了无效的受管回执。');
  }
  return {
    mediaId: file.id,
    roomId,
    fileName: file.name.slice(0, 160) || '附件',
    mimeType: file.mimeType.toLowerCase(),
    byteSize: file.byteSize,
    sha256: file.sha256,
  };
}

function validateRoomAttachmentFiles(files: File[], remaining: number): void {
  if (!files.length) return;
  if (files.length > remaining) throw new TypeError(`本条消息还能添加 ${remaining} 个附件。`);
  for (const file of files) {
    if (!Number.isSafeInteger(file.size) || file.size < 1 || file.size > ROOM_ATTACHMENT_MAX_BYTES) {
      throw new TypeError('每个附件必须小于 20 MiB 且不能为空。');
    }
  }
}

function roomItems(value: unknown): RoomSummary[] { const source = record(value); return (Array.isArray(source.items) ? source.items : Array.isArray(source.rooms) ? source.rooms : []).filter(isRoom); }
function isRoom(value: unknown): value is RoomSummary { const item = record(value); return typeof item.id === 'string' && typeof item.title === 'string' && Array.isArray(item.participants); }
function isRoomWorkItem(value: unknown): value is RoomWorkItem {
  const item = record(value);
  return typeof item.id === 'string'
    && typeof item.roomId === 'string'
    && typeof item.objective === 'string'
    && typeof item.expectedOutput === 'string'
    && typeof item.currentOwnerParticipantId === 'string'
    && typeof item.acceptedTurnId === 'string'
    && Array.isArray(item.acceptanceCriteria)
    && ROOM_WORK_ITEM_STATES[String(item.state)] === true;
}
function sessionWorkspaceRoots(value: unknown): string[] {
  const source = record(value);
  const items = Array.isArray(source.items)
    ? source.items
    : Array.isArray(source.sessions) ? source.sessions : [];
  return items.flatMap((value) => {
    const roots = record(value).workspaceRoots;
    return Array.isArray(roots) ? roots.map(String) : [];
  });
}
function cancellationTargetLabels(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const labels: Record<string, string> = {
    provider: '模型生成',
    tool: '工具调用',
    shell: '命令行进程',
    retry: '自动重试',
    compaction: '上下文压缩',
    branch_summary: '分支整理',
    timer: '定时唤醒',
    continuation: '后续任务',
    session: '伙伴工作记录',
  };
  return [...new Set(value.flatMap((raw) => {
    if (typeof raw === 'string' && raw.trim()) return [raw.trim()];
    const item = record(raw);
    const surface = textValue(item.surface);
    if (!surface) return [];
    const targetCount = Array.isArray(item.targetIds) ? item.targetIds.length : 0;
    const label = labels[surface] ?? '后台任务';
    return [targetCount > 1 ? `${label}（${targetCount} 项）` : label];
  }))];
}
function textValue(value: unknown): string { return typeof value === 'string' ? value : ''; }
/* Drafts survive route unmount (the route is lazy-mounted): module scope, not
   component refs — the pattern Clowder gets right. */
const roomDraftsStore = new Map<string, string>();
((globalThis as { __RAG_DRAFT_STORES__?: Array<{ clear(): void }> }).__RAG_DRAFT_STORES__ ??= []).push(roomDraftsStore);

function roomRailInitiallyOpen(): boolean {
  return typeof window === 'undefined'
    || typeof window.matchMedia !== 'function'
    || !window.matchMedia('(max-width: 760px)').matches;
}
function record(value: unknown): Record<string, unknown> { return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {}; }
function uniquePaths(values: string[]): string[] { return values.map((value) => value.trim()).filter((value, index, all) => value.startsWith('/') && all.indexOf(value) === index).slice(0, 12); }


function roomFromGetResponse(value: unknown, roomId: string): RoomSummary | undefined {
  const candidate = record(record(value).room);
  return (
    candidate.id === roomId
    && typeof candidate.title === 'string'
    && Array.isArray(candidate.participants)
  )
    ? candidate as unknown as RoomSummary
    : undefined;
}
