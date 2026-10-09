import { lazy, memo, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent, type ReactNode, type RefObject } from 'react';
import { ExternalLink, Maximize2, Minimize2, Minus, X } from 'lucide-react';
import {
  PawOsAppSurfaceProvider,
  PawOsDesktopProvider as FeatureDesktopProvider,
  type PawOsWindowRequest,
} from '@/features/paw-os/surface-context';
import { pawApp, pawAppForPath, type PawAppId } from '../runtime/app-registry';
import { usePawDesktopApi, usePawDesktopStore } from '../runtime/desktop-context';
import {
  PAW_WINDOW_MIN_HEIGHT,
  PAW_WINDOW_MIN_WIDTH,
  fitReachablePawWindowBounds,
  pawFocusWindowLayerSize,
  pawWindowArea,
  pawWindowLayerSize,
  satelliteGroup,
  type PawWindowBounds,
  type PawWindowNode,
  type PawWindowPlacement,
} from '../runtime/desktop-store';
import { PawAppProcess } from '../apps/PawApps';
import { PawAppIcon } from './PawAppIcon';
import { closePawWindows } from './window-close-route';
import { PawWindowChromeProvider } from './PawWindowChrome';
import { useRoomProjectionBridge } from '@/features/rooms/state/projection-bridge';
import { roomActivityFlowKind, roomWorkReviewFlow } from '@/features/rooms/room-flow-projection';
import type { RoomProjectionState } from '@/contracts/room-reducer';
import { animateWindowArrival } from './window-arrival';

const PawBackgroundToolWindows = lazy(() => import('./PawBackgroundToolWindows').then((module) => ({ default: module.PawBackgroundToolWindows })));
const PawRoomProjectionKeeper = lazy(() => import('./PawRoomProjectionKeeper'));
const PawRoomFocusParticipants = lazy(() => import('../apps/PawRoomFocusParticipants'));

const noRoomProjections: Record<string, RoomProjectionState> = {};
const selectRoomProjections = (state: { projections: Record<string, RoomProjectionState> }) => state.projections;
const selectNoRoomProjections = () => noRoomProjections;
const noWindows: Record<string, PawWindowNode> = {};
const selectWindows = (state: { windows: Record<string, PawWindowNode> }) => state.windows;
const selectNoWindows = () => noWindows;

export function PawWindowLayer() {
  const api = usePawDesktopApi();
  const idSignature = usePawDesktopStore((state) => Object.keys(state.windows).join('\u0000'));
  const overviewOpen = usePawDesktopStore((state) => state.overviewOpen);
  const collaborationFocusGroup = usePawDesktopStore((state) => state.collaborationFocusGroup);
  const activeWindowId = usePawDesktopStore((state) => state.activeWindowId);
  /* Room projections stream — during a live turn they change many times per
   * second. The layer only reads them for flow groups (which require a
   * participant window) and the focus mode bar, so an ordinary desktop
   * subscribes to a constant instead and never re-renders on Room events.
   * The participant signature ignores bounds/title churn so a geometry
   * commit cannot flip the subscription or fan into Room live-store. */
  const participantSignature = usePawDesktopStore((state) => Object.values(state.windows)
    .filter((node) => node.target?.kind === 'participant')
    .map((node) => node.id)
    .sort()
    .join('\u0000'));
  const wantsRoomProjections = Boolean(collaborationFocusGroup?.startsWith('room:'))
    || Boolean(participantSignature);
  const projections = useRoomProjectionBridge(wantsRoomProjections ? selectRoomProjections : selectNoRoomProjections);
  /* Live window geometry is the layer's most expensive input: the whole
   * windows record changes identity on every bounds commit, focus change and
   * runtime title bind. Only collaboration focus frames and Room flow paths
   * actually read bounds, so an ordinary desktop subscribes to a frozen empty
   * record and the layer stops re-rendering — and stops re-deriving focus
   * frames, flow groups and the rail — every time one window moves. Each
   * PawWindow still owns its own node subscription, so the window that moved
   * is the only thing React touches. */
  const wantsWindowGeometry = Boolean(collaborationFocusGroup) || Boolean(participantSignature);
  const windows = usePawDesktopStore(wantsWindowGeometry ? selectWindows : selectNoWindows);
  const ids = useMemo(() => idSignature.split('\u0000').filter(Boolean), [idSignature]);
  /* Focus and overview frames are laid out in full window-layer coordinates.
   * Collaboration focus hides the Dock; ordinary persisted frames are fitted
   * to the Dock-safe area by desktop-store.ts. */
  const [viewport, setViewport] = useState(() => collaborationFocusGroup ? pawFocusWindowLayerSize() : pawWindowLayerSize());
  const [focusFrameOverrides, setFocusFrameOverrides] = useState<Record<string, PawWindowBounds>>({});
  const [inspectedParticipant, setInspectedParticipant] = useState(() => {
    const node = activeWindowId ? api.getState().windows[activeWindowId] : undefined;
    return node?.target?.kind === 'participant' && collaborationFocusGroup === `room:${node.target.roomId}`
      ? { roomId: node.target.roomId, participantId: node.target.id }
      : null;
  });
  /* Keepalive identity is answered inside the subscription so the layer sees a
   * stable string: geometry churn cannot re-render it, and only an actual
   * Room window open/close/minimize produces a new value. */
  const keptRoomSignature = usePawDesktopStore(
    (state) => roomProjectionKeepaliveIds(state.windows, overviewOpen).join('\u0000'),
  );
  const keptRoomIds = useMemo(() => keptRoomSignature.split('\u0000').filter(Boolean), [keptRoomSignature]);
  const focusedRoomId = collaborationFocusGroup?.startsWith('room:') ? collaborationFocusGroup.slice('room:'.length) : '';
  const selectedParticipantId = inspectedParticipant?.roomId === focusedRoomId ? inspectedParticipant.participantId : '';
  const focusReservation = useMemo(() => focusedRoomId
    ? { modeBarHeight: 46, selectedParticipantId }
    : {}, [focusedRoomId, selectedParticipantId]);
  useEffect(() => {
    if (!focusedRoomId) { setInspectedParticipant(null); return; }
    const node = activeWindowId ? windows[activeWindowId] : undefined;
    if (focusedRoomId && node?.target?.kind === 'participant' && node.target.roomId === focusedRoomId) {
      const participantId = node.target.id;
      setInspectedParticipant((current) => current?.roomId === focusedRoomId && current.participantId === participantId
        ? current : { roomId: focusedRoomId, participantId });
    }
  }, [activeWindowId, focusedRoomId, windows]);
  const computedFocusFrames = useMemo(() => collaborationFocusGroup
    ? layoutCollaborationFocus(
        Object.values(windows).filter((node) => windowBelongsToFocus(node, collaborationFocusGroup)),
        viewport,
        focusReservation,
      )
    : new Map<string, PawWindowBounds>(), [collaborationFocusGroup, focusReservation, viewport, windows]);
  const focusFrames = useMemo(() => normalizeCollaborationFocusFrames(
    computedFocusFrames,
    focusFrameOverrides,
    viewport,
    focusReservation,
    Boolean(focusedRoomId),
  ), [computedFocusFrames, focusFrameOverrides, focusReservation, focusedRoomId, viewport]);
  const focusedRoomNodes = useMemo(() => focusedRoomId
    ? Object.values(windows).filter((node) => windowBelongsToFocus(node, `room:${focusedRoomId}`))
    : [], [focusedRoomId, windows]);
  const focusedRoomPlanets = useMemo(
    () => Object.values(windows).filter((node) => node.target?.kind === 'participant' && node.target.roomId === focusedRoomId),
    [focusedRoomId, windows],
  );
  const focusedRoomMain = focusedRoomNodes.find((node) => node.target?.kind === 'room' && !node.target.panel);
  const roomFocusRegions = useMemo(() => focusedRoomMain && !overviewOpen
    ? roomFocusPartnerRegions(computedFocusFrames, viewport)
    : [], [computedFocusFrames, focusedRoomMain, overviewOpen, viewport]);
  const roomFocusRegionIds = useMemo(() => new Set(roomFocusRegions.flatMap((region) => region.windowIds)), [roomFocusRegions]);
  const focusedRoomProjection = focusedRoomId ? projections[focusedRoomId] : undefined;
  const focusedRoomStatus = roomFocusStatus(focusedRoomProjection);
  const flowWindows = useMemo(() => Object.fromEntries(Object.entries(windows).map(([id, node]) => [
    id,
    focusFrames.has(id) ? { ...node, bounds: focusFrames.get(id)! } : node,
  ])), [focusFrames, windows]);
  const flowGroups = useMemo(() => roomWindowFlowGroups(flowWindows, projections), [flowWindows, projections]);
  const flowPulse = useWindowFlowPulse(flowGroups);
  useEffect(() => {
    setFocusFrameOverrides({});
  }, [collaborationFocusGroup, viewport.height, viewport.width]);
  /* A viewport drag emits resize events far faster than the frame rate, and
   * each one used to refit every window and replace the viewport object. Both
   * now happen at most once per frame, and an unchanged desktop size keeps its
   * existing object so the focus/overview layouts do not recompute at all. */
  useEffect(() => {
    let frame = 0;
    const apply = () => {
      frame = 0;
      setViewport((current) => {
        const next = collaborationFocusGroup ? pawFocusWindowLayerSize() : pawWindowLayerSize();
        return current.width === next.width && current.height === next.height ? current : next;
      });
      api.getState().fitWindowsToViewport();
    };
    apply();
    const update = () => {
      if (!frame) frame = window.requestAnimationFrame(apply);
    };
    window.addEventListener('resize', update);
    return () => {
      window.removeEventListener('resize', update);
      if (frame) window.cancelAnimationFrame(frame);
    };
  }, [api, collaborationFocusGroup]);
  const overviewFrames = useMemo(() => {
    if (!overviewOpen) return new Map<string, OverviewFrame>();
    const state = api.getState();
    return layoutOverview(ids.flatMap((id) => state.windows[id] ? [state.windows[id]] : []), viewport);
  }, [api, ids, overviewOpen, viewport]);
  const openFeatureWindow = useCallback((request: PawOsWindowRequest) => {
    if (!request.background && request.target.kind === 'participant' && request.target.roomId === focusedRoomId) {
      setInspectedParticipant({ roomId: focusedRoomId, participantId: request.target.id });
    }
    const existingBrowser = request.target.kind === 'browser-target' && !request.target.backgroundObserver
      ? Object.values(api.getState().windows).find((node) => node.appId === 'browser' && !(node.target?.kind === 'browser-target' && node.target.backgroundObserver))
      : undefined;
    const entityId = request.target.kind === 'browser-target' && !request.target.backgroundObserver
      ? existingBrowser?.entityId
      : request.target.kind === 'room' && request.target.panel
        ? `${request.target.id}:${request.target.panel}`
        : request.target.id;
    const windowId = api.getState().openApp(request.appId, {
      background: request.background,
      ...(entityId ? { entityId } : {}),
      target: request.target,
      title: request.target.title,
    });
    if (!request.background) {
      const shell = [...document.querySelectorAll<HTMLElement>('[data-paw-window-id]')].find((node) => node.dataset.pawWindowId === windowId);
      shell?.focus({ preventScroll: true });
      if (shell?.closest('.paw-room-focus-satellite-rail')) shell.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
    }
  }, [api, focusedRoomId]);
  const openFeatureApp = useCallback((appId: PawAppId, initialRoute?: string) => {
    api.getState().openApp(appId, { initialRoute });
  }, [api]);
  const openFeatureRoute = useCallback((route: string) => {
    openDesktopRoute(api, route);
  }, [api]);
  const bindRoomMain = useCallback((target: Extract<PawOsWindowRequest['target'], { kind: 'room' }>) => {
    api.getState().bindRoomMain(target);
  }, [api]);
  const setCollaborationFocusGroup = useCallback((group: string | null) => {
    api.getState().setCollaborationFocusGroup(group);
  }, [api]);
  const closeWindow = useCallback((windowId: string) => {
    closePawWindows(api, () => api.getState().closeWindow(windowId));
  }, [api]);
  const bindAgentMain = useCallback((
    windowId: string,
    target?: Extract<PawOsWindowRequest['target'], { kind: 'session' | 'room' }>,
  ) => {
    bindAgentMainRoute(api, windowId, target);
  }, [api]);
  const commitFocusFrame = useCallback((windowId: string, bounds: PawWindowBounds) => {
    setFocusFrameOverrides((current) => ({
      ...current,
      [windowId]: focusedRoomId ? bounds : clampFocusBounds(bounds, viewport, focusReservation),
    }));
  }, [focusReservation, focusedRoomId, viewport]);
  const closeFocusInspector = useCallback((windowId?: string) => {
    document.querySelector<HTMLButtonElement>('.paw-room-focus-participants button[aria-pressed="true"]')?.focus({ preventScroll: true });
    setInspectedParticipant(null);
    if (windowId) api.getState().minimizeWindow(windowId);
    if (focusedRoomMain) api.getState().focusWindow(focusedRoomMain.id);
  }, [api, focusedRoomMain]);
  const detachFocusInspector = useCallback((windowId: string) => {
    api.getState().setCollaborationFocusGroup(null);
    api.getState().focusWindow(windowId);
  }, [api]);
  return (
    <FeatureDesktopProvider bindAgentMain={bindAgentMain} bindRoomMain={bindRoomMain} collaborationFocusGroup={collaborationFocusGroup} closeWindow={closeWindow} openApp={openFeatureApp} openRoute={openFeatureRoute} openWindow={openFeatureWindow} setCollaborationFocusGroup={setCollaborationFocusGroup}>
      {focusedRoomId || ids.some((id) => id.includes(':background:')) ? <Suspense fallback={null}><PawBackgroundToolWindows /></Suspense> : null}
      <div className="paw-window-layer" data-overview={overviewOpen || undefined} data-room-focus={focusedRoomId || undefined}>
        {focusedRoomId ? <>
          <div aria-hidden="true" className="paw-room-focus-plane" />
          <header aria-label={`${focusedRoomMain?.title || focusedRoomId} Sol 协作聚焦`} className="paw-room-focus-modebar">
            <span><strong>SOL</strong><b>{focusedRoomMain?.title || '协作聚焦'}</b></span>
            <span><i data-status={focusedRoomStatus.key} />{focusedRoomStatus.label}</span>
          </header>
          <Suspense fallback={null}>
            <PawRoomFocusParticipants key={focusedRoomId} roomId={focusedRoomId} windows={focusedRoomPlanets} selectedParticipantId={selectedParticipantId} onInspect={openFeatureWindow} onCloseInspector={closeFocusInspector} />
          </Suspense>
        </> : null}
        {keptRoomIds.length ? (
          <Suspense fallback={null}>
            {keptRoomIds.map((roomId) => <PawRoomProjectionKeeper key={roomId} roomId={roomId} />)}
          </Suspense>
        ) : null}
        {ids.filter((id) => !roomFocusRegionIds.has(id)).map((id) => (
          <PawWindow
            collaborationFocusGroup={collaborationFocusGroup}
            flowState={flowPulse.targetWindowIds.has(id) ? 'arrival' : flowPulse.sourceWindowIds.has(id) ? 'source' : undefined}
            focusFrame={focusFrames.get(id)}
            key={id}
            onFocusFrameCommit={commitFocusFrame}
            onDismissInspector={closeFocusInspector}
            onDetachInspector={detachFocusInspector}
            overview={overviewOpen}
            overviewFrame={overviewFrames.get(id)}
            windowId={id}
          />
        ))}
        {roomFocusRegions.map((region) => <section
          aria-label={`${region.key === 'left' ? '左侧' : region.key === 'right' ? '右侧' : ''}伙伴窗口，纵向滚动查看全部 ${region.windowIds.length} 个窗口`}
          className="paw-room-focus-satellite-rail"
          data-position={region.key}
          key={region.key}
          onKeyDown={(event) => {
            if (event.target !== event.currentTarget || !['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown'].includes(event.key)) return;
            event.preventDefault();
            event.currentTarget.scrollBy({ top: (['ArrowUp', 'PageUp'].includes(event.key) ? -1 : 1) * event.currentTarget.clientHeight * .8 });
          }}
          style={{ left: region.bounds.x, top: region.bounds.y, width: region.bounds.width, height: region.bounds.height }}
          tabIndex={0}
        ><div className="paw-room-focus-satellite-track" style={{ height: region.contentHeight }}>
          {region.windowIds.map((id) => {
            const frame = focusFrames.get(id)!;
            return <PawWindow collaborationFocusGroup={collaborationFocusGroup}
              flowState={flowPulse.targetWindowIds.has(id) ? 'arrival' : flowPulse.sourceWindowIds.has(id) ? 'source' : undefined}
              focusFrame={{ ...frame, x: frame.x - region.bounds.x, y: frame.y - region.bounds.y }} key={id}
              onFocusFrameCommit={(windowId, bounds) => commitFocusFrame(windowId, { ...bounds, x: bounds.x + region.bounds.x, y: bounds.y + region.bounds.y })}
              onDismissInspector={closeFocusInspector} onDetachInspector={detachFocusInspector}
              overview={false} windowId={id} />;
          })}
        </div></section>)}
      </div>
    </FeatureDesktopProvider>
  );
}

function roomProjectionKeepaliveIds(windows: Record<string, PawWindowNode>, overviewOpen: boolean): string[] {
  const mainVisible = new Set<string>();
  const auxiliary = new Set<string>();
  for (const node of Object.values(windows)) {
    const target = node.target;
    if (target?.kind === 'participant') {
      auxiliary.add(target.roomId);
      continue;
    }
    if (target?.kind !== 'room') continue;
    if (target.panel) auxiliary.add(target.id);
    else if (!node.minimized || overviewOpen) mainVisible.add(target.id);
  }
  return [...auxiliary].filter((roomId) => !mainVisible.has(roomId));
}

function roomFocusStatus(projection?: RoomProjectionState): { key: string; label: string } {
  const turn = projection?.turnOrder
    .map((turnId) => projection.turnsById[turnId])
    .filter(Boolean)
    .at(-1);
  /* status overlay 克制：Room 名已在左侧，状态只说一个人话短语，不再重复 Sol。 */
  if (!projection) return { key: 'recovering', label: '正在恢复' };
  if (projection.needsSnapshot) return { key: 'recovering', label: '正在重新同步' };
  if (turn?.status === 'queued' || turn?.status === 'running') return { key: 'running', label: '协作进行中' };
  if (turn?.status === 'failed') return { key: 'failed', label: '最近一轮失败' };
  if (turn?.status === 'aborted') return { key: 'aborted', label: '最近一轮已停止' };
  return { key: 'synced', label: '已同步' };
}

type WindowFlowActor = 'root' | string;
type WindowFlowPoint = { x: number; y: number };
export type WindowFlowPacket = {
  id: string;
  pulseKey: string;
  sourceId: WindowFlowActor;
  targetIds: WindowFlowActor[];
  kind: 'request' | 'intercom' | 'question' | 'answer' | 'result' | 'context' | 'dispatch' | 'approval' | 'review';
  summary?: string;
  status?: string;
  workItemId?: string;
  refs?: string[];
  createdAtMs?: number;
};
export type WindowFlowGroup = {
  roomId: string;
  points: Map<WindowFlowActor, WindowFlowPoint>;
  windowIds: Map<WindowFlowActor, string>;
  actorNames?: Map<WindowFlowActor, string>;
  packets: WindowFlowPacket[];
};

export function roomWindowFlowGroups(
  windows: Record<string, PawWindowNode>,
  projections: Record<string, RoomProjectionState>,
): WindowFlowGroup[] {
  const nodes = Object.values(windows).filter((node) => !node.minimized);
  const roomIds = [...new Set(nodes.flatMap((node) => node.target?.kind === 'participant' ? [node.target.roomId] : []))];
  return roomIds.flatMap((roomId) => {
    const main = nodes.find((node) => node.appId === 'agent' && node.target?.kind === 'room' && node.target.id === roomId);
    if (!main) return [];
    const participantNodes = nodes.filter((node) => node.target?.kind === 'participant' && node.target.roomId === roomId);
    const projection = projections[roomId];
    if (!projection || !participantNodes.length) return [];
    const points = new Map<WindowFlowActor, WindowFlowPoint>([['root', windowCenter(main.bounds)]]);
    const windowIds = new Map<WindowFlowActor, string>([['root', main.id]]);
    const actorNames = new Map<WindowFlowActor, string>([['root', main.title]]);
    for (const node of participantNodes) {
      if (node.target?.kind === 'participant') {
        points.set(node.target.id, windowCenter(node.bounds));
        windowIds.set(node.target.id, node.id);
        actorNames.set(node.target.id, node.title);
      }
    }
    const packets: WindowFlowPacket[] = [];
    for (const activityId of projection.activityOrder.slice(-18)) {
      const activity = projection.activitiesById[activityId];
      if (!activity) continue;
      const kind = roomActivityFlowKind(activity);
      if (!kind) continue;
      const review = kind === 'review' ? roomWorkReviewFlow(activity) : undefined;
      const targetId = kind === 'approval'
        ? 'root'
        : kind === 'review'
          ? review?.targetParticipantId || reviewTargetParticipantId(activity.payload)
          : stringValue(activity.payload.targetParticipantId) || activity.participantId || '';
      if (!points.has(targetId)) continue;
      const sourceId = review?.sourceParticipantId
        || stringValue(activity.payload.sourceParticipantId)
        || stringValue(activity.payload.actorParticipantId)
        || (kind === 'approval' ? activity.participantId : '')
        || 'root';
      packets.push({
        id: `activity:${activity.id}`,
        pulseKey: `activity:${activity.id}:${activity.status}:${activity.updatedAtMs ?? activity.createdAtMs}`,
        sourceId: points.has(sourceId) ? sourceId : 'root',
        targetIds: [targetId],
        kind,
        summary: activity.summary,
        status: review?.status ?? activity.status,
        ...(review?.workItemId ? { workItemId: review.workItemId } : {}),
        ...(review?.refs.length ? { refs: review.refs } : {}),
        createdAtMs: activity.createdAtMs,
      });
    }
    for (const messageId of projection.messageOrder.slice(-18)) {
      const message = projection.messagesById[messageId];
      if (!message || message.projectionKind === 'execution') continue;
      if (message.role === 'assistant' && message.participantId && points.has(message.participantId)) {
        packets.push({ id: `message:${message.id}`, pulseKey: `message:${message.id}:${message.status}:${message.completedAtMs ?? message.createdAtMs}`, sourceId: message.participantId, targetIds: ['root'], kind: message.question ? 'question' : message.answerToPostId ? 'answer' : 'result', summary: message.text, status: message.status, createdAtMs: message.createdAtMs });
        continue;
      }
      const targets = (message.mentionedParticipantIds ?? []).filter((id) => points.has(id));
      if (message.role === 'user' && targets.length) packets.push({ id: `message:${message.id}`, pulseKey: `message:${message.id}:${message.status}:${message.completedAtMs ?? message.createdAtMs}`, sourceId: 'root', targetIds: targets, kind: 'request', summary: message.text, status: message.status, createdAtMs: message.createdAtMs });
    }
    return [{ roomId, points, windowIds, actorNames, packets: packets.slice(-8) }];
  });
}

export type WindowFlowPulse = {
  packetPulseKeys: ReadonlySet<string>;
  sourceWindowIds: ReadonlySet<string>;
  targetWindowIds: ReadonlySet<string>;
};

function useWindowFlowPulse(groups: WindowFlowGroup[]): WindowFlowPulse {
  const seenPulseKeys = useRef<Set<string> | null>(null);
  const [pulse, setPulse] = useState<WindowFlowPulse>(() => ({
    packetPulseKeys: new Set(),
    sourceWindowIds: new Set(),
    targetWindowIds: new Set(),
  }));
  const pulseSignature = groups.flatMap((group) => group.packets.map((packet) => packet.pulseKey)).join('\u001f');
  useEffect(() => {
    const nextKeys = new Set(groups.flatMap((group) => group.packets.map((packet) => packet.pulseKey)));
    const seen = seenPulseKeys.current;
    seenPulseKeys.current = new Set([...(seen ?? []), ...nextKeys]);
    if (!seen) return;
    const arriving = windowFlowArrivalPulse(groups, seen);
    if (!arriving.packetPulseKeys.size) return;
    setPulse(arriving);
    const timer = window.setTimeout(() => setPulse({
      packetPulseKeys: new Set(),
      sourceWindowIds: new Set(),
      targetWindowIds: new Set(),
    }), 980);
    return () => window.clearTimeout(timer);
  // The signature is the authoritative event/revision identity; geometry-only
  // window moves must not replay an information arrival.
  }, [pulseSignature]);
  return pulse;
}

export function windowFlowArrivalPulse(
  groups: WindowFlowGroup[],
  seenPulseKeys: ReadonlySet<string>,
): WindowFlowPulse {
  const packetPulseKeys = new Set(
    groups.flatMap((group) => group.packets.map((packet) => packet.pulseKey))
      .filter((key) => !seenPulseKeys.has(key)),
  );
  const sourceWindowIds = new Set<string>();
  const targetWindowIds = new Set<string>();
  for (const group of groups) {
    for (const packet of group.packets) {
      if (!packetPulseKeys.has(packet.pulseKey)) continue;
      const sourceWindowId = group.windowIds.get(packet.sourceId);
      if (sourceWindowId) sourceWindowIds.add(sourceWindowId);
      for (const targetId of packet.targetIds) {
        const targetWindowId = group.windowIds.get(targetId);
        if (targetWindowId) targetWindowIds.add(targetWindowId);
      }
    }
  }
  return { packetPulseKeys, sourceWindowIds, targetWindowIds };
}

export function isCollaborationSatellite(node: PawWindowNode): boolean {
  return !node.minimized && node.target?.kind === 'subagent';
}

export function windowBelongsToFocus(node: PawWindowNode, group: string): boolean {
  if (node.minimized) return false;
  /* Explicit Room focus includes its partners and persistent background
     resource observers. Ordinary desktop tools retain their own geometry. */
  if (group.startsWith('room:')) {
    const roomId = group.slice('room:'.length);
    if (node.target?.kind === 'room') return node.target.id === roomId && !node.target.panel;
    if (node.target?.kind === 'process-terminal' || node.target?.kind === 'browser-target') {
      return node.target.roomId === roomId && node.target.backgroundObserver === true;
    }
    return node.target?.kind === 'participant' && node.target.roomId === roomId;
  }
  if (satelliteGroup(node.target) === group) return true;
  if (group.startsWith('session:') && node.target?.kind === 'session') {
    return node.target.id === group.slice('session:'.length);
  }
  return false;
}

type CollaborationFocusReservation = {
  modeBarHeight?: number;
  ledgerHeight?: number;
  selectedParticipantId?: string;
};

export function layoutCollaborationFocus(
  nodes: PawWindowNode[],
  viewport: { width: number; height: number },
  reserved: CollaborationFocusReservation = {},
): Map<string, PawWindowBounds> {
  const frames = new Map<string, PawWindowBounds>();
  if (!nodes.length) return frames;
  const roomMain = nodes.find((node) => node.target?.kind === 'room' && !node.target.panel);
  if (roomMain) return layoutRoomCollaborationFocus(nodes, roomMain, viewport, reserved);
  const inset = 10;
  const gap = 10;
  const modeBarHeight = Math.max(0, reserved.modeBarHeight ?? 0);
  /* The ledger is an absolute overlay. It never reserves focus geometry,
     whether collapsed or open. */
  const ledgerSpace = 0;
  const usableTop = modeBarHeight;
  const usableBottom = Math.max(usableTop, viewport.height - ledgerSpace);
  const usableHeight = usableBottom - usableTop;
  const main = nodes.find((node) => !isCollaborationSatellite(node)) ?? nodes[0]!;
  const satellites = nodes.filter((node) => node.id !== main.id);
  if (!satellites.length) {
    frames.set(main.id, {
      x: inset,
      y: usableTop + inset,
      width: Math.max(PAW_WINDOW_MIN_WIDTH, viewport.width - inset * 2),
      height: Math.max(PAW_WINDOW_MIN_HEIGHT, usableHeight - inset * 2),
    });
    return frames;
  }
  if (usesHorizontalFocusRail(satellites.length, viewport.width, usableHeight, inset, gap)) {
    const minimumSatelliteHeight = 220;
    const maximumMainHeight = usableHeight - inset * 2 - gap - minimumSatelliteHeight;
    const mainHeight = Math.max(320, Math.min(Math.round(usableHeight * .54), maximumMainHeight));
    frames.set(main.id, { x: inset, y: usableTop + inset, width: viewport.width - inset * 2, height: mainHeight });
    const auxiliaryTop = usableTop + inset + mainHeight + gap;
    const auxiliaryHeight = Math.max(0, usableBottom - auxiliaryTop - inset);
    const satelliteWidth = Math.min(300, Math.max(260, Math.round(viewport.width * .5)));
    const satelliteHeight = Math.max(minimumSatelliteHeight, auxiliaryHeight - 12);
    satellites.forEach((node, index) => frames.set(node.id, {
      x: inset + index * (satelliteWidth + gap),
      y: auxiliaryTop,
      width: satelliteWidth,
      height: satelliteHeight,
    }));
    return frames;
  }
  if (viewport.width < 720) {
    const mainRatio = satellites.length >= 5 ? .49 : .58;
    const mainHeight = Math.max(PAW_WINDOW_MIN_HEIGHT, Math.round(usableHeight * mainRatio));
    const mainWidth = Math.max(PAW_WINDOW_MIN_WIDTH, viewport.width - inset * 2);
    const columns = Math.min(2, satellites.length);
    const rows = Math.ceil(satellites.length / columns);
    const auxiliaryTop = usableTop + inset + mainHeight + gap;
    const auxiliaryHeight = Math.max(0, usableBottom - auxiliaryTop - inset);
    const cellWidth = (viewport.width - inset * 2 - gap * (columns - 1)) / columns;
    const cellHeight = (auxiliaryHeight - gap * (rows - 1)) / rows;
    /* Two columns are attractive until the cards stop fitting. At that point
     * keeping their CSS min-size in the grid only makes the layer clip the
     * right-hand cards; the horizontal rail below preserves every card's
     * identity and gives the user an explicit scroll affordance. */
    if (cellWidth < PAW_WINDOW_MIN_WIDTH || cellHeight < PAW_WINDOW_MIN_HEIGHT) {
      return layoutHorizontalSatelliteRail(frames, main, satellites, viewport, usableTop, usableBottom, inset, gap);
    }
    frames.set(main.id, { x: inset, y: usableTop + inset, width: mainWidth, height: mainHeight });
    satellites.forEach((node, index) => frames.set(node.id, {
      x: inset + (index % columns) * (cellWidth + gap),
      y: auxiliaryTop + Math.floor(index / columns) * (cellHeight + gap),
      width: Math.max(PAW_WINDOW_MIN_WIDTH, cellWidth),
      height: Math.max(PAW_WINDOW_MIN_HEIGHT, cellHeight),
    }));
    return frames;
  }
  if (viewport.width < 1000) {
    const railWidth = Math.min(320, Math.max(PAW_WINDOW_MIN_WIDTH, Math.round(viewport.width * .34)));
    const height = (usableHeight - inset * 2 - gap * (satellites.length - 1)) / satellites.length;
    const mainWidth = viewport.width - inset * 2 - railWidth - gap;
    if (height < PAW_WINDOW_MIN_HEIGHT || mainWidth < PAW_WINDOW_MIN_WIDTH) {
      return layoutHorizontalSatelliteRail(frames, main, satellites, viewport, usableTop, usableBottom, inset, gap);
    }
    frames.set(main.id, {
      x: inset,
      y: usableTop + inset,
      width: Math.max(PAW_WINDOW_MIN_WIDTH, mainWidth),
      height: Math.max(PAW_WINDOW_MIN_HEIGHT, usableHeight - inset * 2),
    });
    satellites.forEach((node, index) => frames.set(node.id, {
      x: viewport.width - inset - railWidth,
      y: usableTop + inset + index * (height + gap),
      width: railWidth,
      height: Math.max(PAW_WINDOW_MIN_HEIGHT, height),
    }));
    return frames;
  }
  const railWidth = Math.min(320, Math.max(PAW_WINDOW_MIN_WIDTH, Math.round(viewport.width * .21)));
  const mainX = inset + railWidth + gap;
  const mainWidth = viewport.width - inset * 2 - railWidth * 2 - gap * 2;
  frames.set(main.id, {
    x: mainX,
    y: usableTop + inset,
    width: Math.max(PAW_WINDOW_MIN_WIDTH, mainWidth),
    height: Math.max(PAW_WINDOW_MIN_HEIGHT, usableHeight - inset * 2),
  });
  const left = satellites.filter((_, index) => index % 2 === 0);
  const right = satellites.filter((_, index) => index % 2 === 1);
  const maximumSideCount = Math.max(left.length, right.length);
  const sideHeight = (usableHeight - inset * 2 - gap * (maximumSideCount - 1)) / maximumSideCount;
  if (mainWidth < PAW_WINDOW_MIN_WIDTH || sideHeight < PAW_WINDOW_MIN_HEIGHT) {
    frames.clear();
    return layoutHorizontalSatelliteRail(frames, main, satellites, viewport, usableTop, usableBottom, inset, gap);
  }
  const placeRail = (items: PawWindowNode[], x: number) => {
    if (!items.length) return;
    const height = Math.max(PAW_WINDOW_MIN_HEIGHT, (usableHeight - inset * 2 - gap * (items.length - 1)) / items.length);
    items.forEach((node, index) => frames.set(node.id, {
      x,
      y: usableTop + inset + index * (height + gap),
      width: railWidth,
      height,
    }));
  };
  placeRail(left, inset);
  placeRail(right, viewport.width - inset - railWidth);
  return frames;
}

const ROOM_FOCUS_INSET = 10;
const ROOM_FOCUS_GAP = 12;
const ROOM_FOCUS_SCROLL_GUTTER = 16;
const ROOM_FOCUS_PARTNER_HEIGHT = 280;

/** Keep a centered reading column, with retained partners on both sides.
 * A narrow viewport moves partners into a bounded, vertically scrolling grid. */
function layoutRoomCollaborationFocus(
  nodes: PawWindowNode[],
  main: PawWindowNode,
  viewport: { width: number; height: number },
  reserved: CollaborationFocusReservation,
): Map<string, PawWindowBounds> {
  const inset = ROOM_FOCUS_INSET;
  const gap = ROOM_FOCUS_GAP;
  const top = Math.max(0, reserved.modeBarHeight ?? 0) + 48 + inset;
  const width = Math.max(PAW_WINDOW_MIN_WIDTH, viewport.width - inset * 2);
  const height = Math.max(PAW_WINDOW_MIN_HEIGHT, viewport.height - top - inset);
  const partners = nodes.filter((node) => node.id !== main.id && !node.minimized);
  const frames = new Map<string, PawWindowBounds>([[main.id, { x: inset, y: top, width, height }]]);
  if (!partners.length) return frames;
  const minimumSideWidth = PAW_WINDOW_MIN_WIDTH + ROOM_FOCUS_SCROLL_GUTTER;
  if (width >= 640 + 2 * (minimumSideWidth + gap)) {
    const mainWidth = Math.min(800, Math.max(640, width * .48), width - 2 * (minimumSideWidth + gap));
    const mainX = (viewport.width - mainWidth) / 2;
    const sideWidth = Math.min(440, (width - mainWidth - gap * 2) / 2);
    const rows = Math.ceil(partners.length / 2);
    const partnerHeight = Math.max(ROOM_FOCUS_PARTNER_HEIGHT, Math.min(480, (height - (rows - 1) * gap) / rows));
    frames.set(main.id, { x: mainX, y: top, width: mainWidth, height });
    partners.forEach((node, index) => {
      const side = index % 2;
      const sideCount = side ? Math.floor(partners.length / 2) : rows;
      const sideHeight = sideCount * partnerHeight + (sideCount - 1) * gap;
      frames.set(node.id, {
        x: side ? mainX + mainWidth + gap : mainX - gap - sideWidth,
        y: top + Math.max(0, (height - sideHeight) / 2) + Math.floor(index / 2) * (partnerHeight + gap),
        width: sideWidth - ROOM_FOCUS_SCROLL_GUTTER,
        height: partnerHeight,
      });
    });
  } else {
    const mainWidth = Math.min(800, width);
    const partnerAreaHeight = Math.min(360, Math.max(ROOM_FOCUS_PARTNER_HEIGHT, height * .42));
    const mainHeight = Math.max(PAW_WINDOW_MIN_HEIGHT, height - partnerAreaHeight - gap);
    const columns = Math.min(partners.length, width >= 2 * 320 + gap + ROOM_FOCUS_SCROLL_GUTTER ? 2 : 1);
    const gridWidth = Math.min(width, columns * 520 + (columns - 1) * gap + ROOM_FOCUS_SCROLL_GUTTER);
    const gridX = (viewport.width - gridWidth) / 2;
    const partnerWidth = (gridWidth - ROOM_FOCUS_SCROLL_GUTTER - (columns - 1) * gap) / columns;
    const partnerHeight = Math.max(ROOM_FOCUS_PARTNER_HEIGHT, Math.min(360, viewport.height - inset - top - mainHeight - gap));
    frames.set(main.id, { x: (viewport.width - mainWidth) / 2, y: top, width: mainWidth, height: mainHeight });
    partners.forEach((node, index) => frames.set(node.id, {
      x: gridX + (index % columns) * (partnerWidth + gap),
      y: top + mainHeight + gap + Math.floor(index / columns) * (partnerHeight + gap),
      width: partnerWidth, height: partnerHeight,
    }));
  }
  return frames;
}

export type RoomFocusPartnerRegion = {
  key: 'left' | 'right' | 'bottom';
  windowIds: string[];
  bounds: PawWindowBounds;
  contentHeight: number;
};

/** Region geometry comes from the automatic layout, so resizing an observer
 * cannot move a scrolling region or cover the main Room composer. */
export function roomFocusPartnerRegions(
  frames: ReadonlyMap<string, PawWindowBounds>,
  viewport: { width: number; height: number },
): RoomFocusPartnerRegion[] {
  const [mainEntry, ...partners] = [...frames];
  if (!mainEntry || !partners.length) return [];
  const main = mainEntry[1];
  const groups = new Map<RoomFocusPartnerRegion['key'], Array<[string, PawWindowBounds]>>();
  for (const entry of partners) {
    const frame = entry[1];
    const key = frame.x + frame.width <= main.x ? 'left'
      : frame.x >= main.x + main.width ? 'right' : 'bottom';
    groups.set(key, [...(groups.get(key) ?? []), entry]);
  }
  return [...groups].map(([key, entries]) => {
    const x = Math.min(...entries.map(([, frame]) => frame.x));
    const y = key === 'bottom' ? main.y + main.height + ROOM_FOCUS_GAP : main.y;
    const width = Math.max(...entries.map(([, frame]) => frame.x + frame.width)) - x + ROOM_FOCUS_SCROLL_GUTTER;
    const height = Math.max(1, viewport.height - ROOM_FOCUS_INSET - y);
    return {
      key, windowIds: entries.map(([id]) => id), bounds: { x, y, width, height },
      contentHeight: Math.max(height, ...entries.map(([, frame]) => frame.y + frame.height - y)),
    };
  });
}

function usesHorizontalFocusRail(
  satelliteCount: number,
  viewportWidth: number,
  usableHeight: number,
  inset = 10,
  gap = 10,
): boolean {
  if (satelliteCount < 5) return false;
  const columnCount = viewportWidth < 1_000 ? 1 : 2;
  const longestColumn = Math.ceil(satelliteCount / columnCount);
  const projectedHeight = (
    usableHeight - inset * 2 - gap * Math.max(0, longestColumn - 1)
  ) / longestColumn;
  return viewportWidth < 720 || projectedHeight < PAW_WINDOW_MIN_HEIGHT;
}

const PAW_ROOM_FOCUS_RAIL_HEIGHT = 220;

/**
 * Lay out satellites in one scrollable strip when a side/grid projection
 * cannot honor the window floor. The strip deliberately overflows in the x
 * axis; this is retained for the existing Session Tool Agent composition.
 */
function layoutHorizontalSatelliteRail(
  frames: Map<string, PawWindowBounds>,
  main: PawWindowNode,
  satellites: PawWindowNode[],
  viewport: { width: number; height: number },
  usableTop: number,
  usableBottom: number,
  inset: number,
  gap: number,
): Map<string, PawWindowBounds> {
  const mainTop = usableTop + inset;
  const railHeight = Math.max(PAW_ROOM_FOCUS_RAIL_HEIGHT, Math.min(260, Math.round((usableBottom - usableTop) * .32)));
  const railTop = Math.max(mainTop + PAW_WINDOW_MIN_HEIGHT + gap, usableBottom - inset - railHeight);
  const mainHeight = Math.max(PAW_WINDOW_MIN_HEIGHT, railTop - gap - mainTop);
  const mainWidth = Math.max(PAW_WINDOW_MIN_WIDTH, viewport.width - inset * 2);
  const satelliteWidth = Math.max(PAW_WINDOW_MIN_WIDTH, Math.min(320, Math.round(viewport.width * .34)));
  frames.set(main.id, {
    x: inset,
    y: mainTop,
    width: mainWidth,
    height: mainHeight,
  });
  satellites.forEach((node, index) => frames.set(node.id, {
    x: inset + index * (satelliteWidth + gap),
    y: railTop,
    width: satelliteWidth,
    height: railHeight,
  }));
  return frames;
}
function clampFocusBounds(
  bounds: PawWindowBounds,
  viewport: { width: number; height: number },
  reserved: { modeBarHeight?: number; ledgerHeight?: number },
): PawWindowBounds {
  const top = Math.max(0, reserved.modeBarHeight ?? 0);
  const bottom = Math.max(top, viewport.height - Math.max(0, reserved.ledgerHeight ?? 0));
  const width = Math.min(Math.max(PAW_WINDOW_MIN_WIDTH, bounds.width), Math.max(PAW_WINDOW_MIN_WIDTH, viewport.width));
  const height = Math.min(Math.max(PAW_WINDOW_MIN_HEIGHT, bounds.height), Math.max(PAW_WINDOW_MIN_HEIGHT, bottom - top));
  return {
    x: Math.min(Math.max(0, bounds.x), Math.max(0, viewport.width - width)),
    y: Math.min(Math.max(top, bounds.y), Math.max(top, bottom - height)),
    width,
    height,
  };
}

/** User adjustments stay inside the partner area, leaving the Room composer free. */
export function normalizeCollaborationFocusFrames(
  computed: ReadonlyMap<string, PawWindowBounds>,
  overrides: Readonly<Record<string, PawWindowBounds>>,
  viewport: { width: number; height: number },
  reserved: { modeBarHeight?: number; ledgerHeight?: number },
  containRoomFrames = false,
): Map<string, PawWindowBounds> {
  const entries = [...computed];
  const main = entries[0]?.[1];
  const regions = containRoomFrames ? roomFocusPartnerRegions(computed, viewport) : [];
  return new Map(entries.map(([id, frame], index) => {
    if (!containRoomFrames) return [id, overrides[id] ?? frame];
    if (!main || index === 0 || !overrides[id]) return [id, frame];
    const region = regions.find((value) => value.windowIds.includes(id));
    if (!region) return [id, frame];
    const left = region.bounds.x;
    const top = region.bounds.y;
    const right = region.bounds.x + region.bounds.width - ROOM_FOCUS_SCROLL_GUTTER;
    const bottom = region.bounds.y + region.contentHeight;
    const bounds = overrides[id]!;
    const width = Math.min(Math.max(PAW_WINDOW_MIN_WIDTH, bounds.width), right - left);
    const height = Math.min(Math.max(PAW_WINDOW_MIN_HEIGHT, bounds.height), bottom - top);
    return [id, { x: Math.min(Math.max(left, bounds.x), right - width), y: Math.min(Math.max(top, bounds.y), bottom - height), width, height }];
  }));
}

function windowCenter(bounds: PawWindowBounds): WindowFlowPoint {
  return { x: bounds.x + bounds.width / 2, y: bounds.y + bounds.height / 2 };
}

function reviewTargetParticipantId(payload: Record<string, unknown>): string {
  return stringValue(payload.targetParticipantId)
    || stringValue(payload.reviewerParticipantId)
    || stringValue(payload.verifierParticipantId)
    || stringValue(payload.reviewedParticipantId)
    || stringValue(payload.revieweeParticipantId);
}

function stringValue(value: unknown): string { return typeof value === 'string' ? value : ''; }

/* One window's geometry commit replaces the store's windows map, which
 * re-renders the layer — but it must not re-render every other window's App
 * tree. memo bails untouched windows out at the frame boundary; each window's
 * own store slice (its node, stack position, active flag) still re-renders
 * exactly the window that changed. */
const PawWindow = memo(function PawWindow({ collaborationFocusGroup, flowState, focusFrame, onFocusFrameCommit, onDismissInspector, onDetachInspector, overview, overviewFrame, windowId }: {
  collaborationFocusGroup: string | null;
  flowState?: 'source' | 'arrival';
  focusFrame?: PawWindowBounds;
  onFocusFrameCommit: (windowId: string, bounds: PawWindowBounds) => void;
  onDismissInspector: (windowId: string) => void;
  onDetachInspector: (windowId: string) => void;
  overview: boolean;
  overviewFrame?: OverviewFrame;
  windowId: string;
}) {
  const api = usePawDesktopApi();
  const node = usePawDesktopStore((state) => state.windows[windowId]);
  const zIndex = usePawDesktopStore((state) => state.stack.indexOf(windowId) + 10);
  const active = usePawDesktopStore((state) => state.activeWindowId === windowId);
  const subscribeExitCancellation = useCallback((cancel: () => void) => api.subscribe((state, previous) => {
    if (!state.windows[windowId] || state.windows[windowId].minimized
      || (state.foregroundWindowRequest !== previous.foregroundWindowRequest
        && state.foregroundWindowRequest?.windowId === windowId)) cancel();
  }), [api, windowId]);
  const openLinkedRoute = useCallback((event: ReactMouseEvent<HTMLElement>) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const anchor = (event.target as HTMLElement).closest<HTMLAnchorElement>('a[href]');
    if (!anchor || anchor.target === '_blank' || anchor.hasAttribute('download')) return;
    const href = anchor.getAttribute('href') ?? '';
    if (!href.startsWith('#/')) return;
    event.preventDefault();
    openDesktopRoute(api, href.slice(1));
  }, [api]);
  /* The App surface depends only on process identity and committed size.
   * Keeping the element referentially stable means frame-only re-renders —
   * z-order churn on every focus click, active/flow flags, drag position
   * commits — bail out at MemoizedWindowBody instead of reconciling the App
   * provider chain, so a window's own re-render walks chrome only. */
  const appId = node?.appId;
  const entityId = node?.entityId;
  const initialRoute = node?.initialRoute;
  const target = node?.target;
  const surfaceWidth = focusFrame?.width ?? node?.bounds.width ?? 0;
  const surfaceHeight = Math.max(0, focusFrame?.height ?? node?.bounds.height ?? 0);
  const roomFocus = Boolean(collaborationFocusGroup?.startsWith('room:'));
  const inFocus = Boolean(node && collaborationFocusGroup && windowBelongsToFocus(node, collaborationFocusGroup)
    && (!roomFocus || focusFrame));
  const inspector = roomFocus && inFocus && target?.kind === 'participant';
  const shouldHydrateNow = overview || (!collaborationFocusGroup || inFocus) && (active || inFocus);
  const [appHydrated, setAppHydrated] = useState(shouldHydrateNow);
  useEffect(() => {
    if (shouldHydrateNow) setAppHydrated(true);
  }, [shouldHydrateNow]);
  const appSurface = useMemo(() => (appId ? (
    <div className="paw-window-route-surface" onClick={openLinkedRoute}>
      <PawOsAppSurfaceProvider active={active && !overview} appId={appId} height={surfaceHeight} width={surfaceWidth} windowId={windowId}>
        {appHydrated || shouldHydrateNow
          ? <PawAppProcess appId={appId} entityId={entityId} initialRoute={initialRoute} target={target} />
          : <div aria-hidden="true" className="paw-app-boot"><PawAppIcon appId={appId} size={32} /></div>}
      </PawOsAppSurfaceProvider>
    </div>
  ) : null), [active, appHydrated, appId, entityId, initialRoute, openLinkedRoute, overview, shouldHydrateNow, surfaceHeight, surfaceWidth, target, windowId]);
  if (!node || (node.minimized && !overview)) return null;
  const app = pawApp(node.appId);
  const collaborationRole = collaborationFocusGroup
    ? !inFocus
      ? 'hidden'
      : isCollaborationSatellite(node)
      ? 'satellite'
      : node.appId === 'agent'
        ? 'primary'
        : 'unrelated'
    : undefined;
  return (
    <PawWindowFrame
      active={active}
      subscribeExitCancellation={subscribeExitCancellation}
      bounds={node.bounds}
      collaborationRole={collaborationRole}
      deferPointerInteractionUntilFocused={!collaborationFocusGroup && Boolean(satelliteGroup(node.target))}
      flowState={flowState}
      focusFrame={focusFrame}
      focusLocked={roomFocus && inFocus && !inspector}
      frameMode={focusFrame && target?.kind === 'participant'
        ? 'planet'
        : focusFrame && collaborationRole === 'satellite'
          ? 'focus-card'
          : 'window'}
      onBoundsCommit={(bounds) => {
        if (focusFrame) {
          onFocusFrameCommit(windowId, bounds);
          return;
        }
        api.getState().commitBounds(windowId, bounds);
        api.getState().fitWindowsToViewport();
      }}
      onClose={() => {
        if (inspector) { onDismissInspector(windowId); return; }
        closePawWindows(api, () => api.getState().closeWindow(windowId));
      }}
      onFocus={() => {
        api.getState().focusWindow(windowId);
      }}
      onDetach={inspector ? () => onDetachInspector(windowId) : undefined}
      onMinimize={() => {
        api.getState().minimizeWindow(windowId);
      }}
      onOpenFromOverview={() => api.getState().focusWindow(windowId)}
      onSnap={(placement) => {
        api.getState().snapWindow(windowId, placement);
      }}
      onToggleMaximize={() => {
        api.getState().toggleMaximize(windowId);
      }}
      placement={node.placement}
      overview={overview}
      overviewFrame={overviewFrame}
      appId={node.appId}
      title={node.title || app.label}
      subtitle={node.target?.subtitle}
      targetKind={node.target?.kind}
      windowChrome={node.target?.kind === 'room' && !node.target.panel ? 'room-workspace' : node.appId === 'agent' ? 'agent-session' : node.appId === 'browser' && !(node.target?.kind === 'browser-target' && node.target.backgroundObserver) ? 'browser-tabs' : node.appId === 'files' ? 'files-tools' : node.appId === 'terminal' ? 'terminal-tabs' : undefined}
      windowId={windowId}
      zIndex={roomFocus && inFocus ? inspector ? 40 : 20 : zIndex}
    >
      {appSurface}
    </PawWindowFrame>
  );
});

/** Keep reload aligned with the active main conversation without hashchange re-entry. */
export function bindAgentMainRoute(
  api: ReturnType<typeof usePawDesktopApi>,
  windowId: string,
  target?: Extract<PawOsWindowRequest['target'], { kind: 'session' | 'room' }>,
): void {
  api.getState().bindAgentMain(windowId, target);
  const state = api.getState();
  const node = state.windows[windowId];
  if (state.activeWindowId !== windowId || node?.appId !== 'agent') return;
  if (node.target?.kind !== target?.kind || node.target?.id !== target?.id) return;
  const route = node.initialRoute;
  if (route && window.location.hash !== `#${route}`) {
    window.history.replaceState(window.history.state, '', `${window.location.search}#${route}`);
  }
}

export function openDesktopRoute(api: ReturnType<typeof usePawDesktopApi>, route: string): void {
  const normalized = route.replace(/^#/, '');
  if (normalized.split(/[?#]/, 1)[0] === '/project-field') {
    api.getState().showWayfinder();
    return;
  }
  const app = pawAppForPath(normalized);
  if (!app) return;
  const search = normalized.split('?', 2)[1] ?? '';
  const params = new URLSearchParams(search);
  if (app.id === 'agent') {
    const sessionId = params.get('session') || params.get('sessionId');
    if (sessionId) {
      const existing = [...api.getState().stack].reverse().map(id => api.getState().windows[id])
        .find(node => node?.appId === 'agent' && node.target?.kind === 'session' && node.target.id === sessionId);
      if (existing) {
        api.getState().openApp('agent', { entityId: existing.entityId, initialRoute: normalized });
        return;
      }
      api.getState().openApp('agent', {
        entityId: sessionId,
        initialRoute: normalized,
        target: { kind: 'session', id: sessionId, title: 'Session' },
        title: 'Agent',
      });
      return;
    }
    const roomId = params.get('room');
    if (roomId) {
      api.getState().openApp('agent', {
        entityId: roomId,
        initialRoute: normalized,
        target: { kind: 'room', id: roomId, title: 'Room' },
        title: 'Agent',
      });
      return;
    }
  }
  const windowId = api.getState().openApp(app.id, { initialRoute: normalized, title: app.label });
  if (windowId && app.id !== 'agent') {
    // The hash is the reload/deep-link entry point. Keep it aligned with
    // explicit App page navigation without opening another history entry or
    // dispatching hashchange back through the desktop window router.
    window.history.replaceState(window.history.state, '', `${window.location.search}#${normalized}`);
  }
}

export function PawWindowFrame({ active, appId, bounds, children, collaborationRole, deferPointerInteractionUntilFocused = false, flowState, focusFrame, focusLocked = false, frameMode = 'window', onBoundsCommit, onClose, onDetach, onFocus, onMinimize, onOpenFromOverview, onSnap, onToggleMaximize, overview = false, overviewFrame, placement, subtitle, subscribeExitCancellation, targetKind, title, windowChrome, windowId, zIndex }: {
  active: boolean;
  appId: PawAppId;
  bounds: PawWindowBounds;
  children: ReactNode;
  collaborationRole?: 'primary' | 'satellite' | 'unrelated' | 'hidden';
  deferPointerInteractionUntilFocused?: boolean;
  flowState?: 'source' | 'arrival';
  focusFrame?: PawWindowBounds;
  focusLocked?: boolean;
  frameMode?: 'window' | 'focus-card' | 'planet';
  onBoundsCommit: (bounds: PawWindowBounds) => void;
  onClose: () => void;
  onDetach?: () => void;
  onFocus: () => void;
  onMinimize: () => void;
  onOpenFromOverview?: () => void;
  onSnap?: (placement: PawWindowPlacement) => void;
  onToggleMaximize: () => void;
  overview?: boolean;
  overviewFrame?: OverviewFrame;
  placement?: PawWindowPlacement;
  subtitle?: string;
  subscribeExitCancellation?: (cancel: () => void) => () => void;
  targetKind?: PawOsWindowRequest['target']['kind'];
  title: string;
  windowChrome?: string;
  windowId: string;
  zIndex: number;
}) {
  const shellRef = useRef<HTMLElement>(null);
  const [windowLeadingChromeTarget, setWindowLeadingChromeTarget] = useState<HTMLElement | null>(null);
  const [windowChromeTarget, setWindowChromeTarget] = useState<HTMLElement | null>(null);
  const maximized = placement === 'maximized';
  const identityIconId = targetKind === 'room' ? 'room' : appId;
  const planetFrame = frameMode === 'planet' && !overview;
  const collapsiblePlanet = Boolean(focusFrame && onDetach);
  const roomFocusPrimary = focusLocked && targetKind === 'room' && windowChrome === 'room-workspace';
  const interactionBounds = focusFrame ?? bounds;
  /* A focus frame or rail slot is laid out by its owning mode and clamped on
   * commit against that mode's own box, so only an ordinary desktop window
   * answers to the shared desktop area. */
  const containToDesktop = !focusFrame;
  const drag = useWindowDrag(shellRef, interactionBounds, onBoundsCommit, onFocus, focusFrame ? undefined : onSnap, active, deferPointerInteractionUntilFocused, containToDesktop);
  const exit = useWindowExit(shellRef, appId, subscribeExitCancellation);
  /* Windows arrive the way a real OS opens them: a short scale-up fade on the
   * inner surface (the shell's transform belongs to drag, snap and overview).
   * The same mount path covers restore-from-minimize, so a restored window
   * reads as returning instead of popping. Room-flow arrivals and
   * collaboration satellites carry their own authored choreography and are
   * left to it. */
  const authoredArrival = Boolean(flowState) || collaborationRole === 'satellite';
  useLayoutEffect(() => {
    const shell = shellRef.current;
    if (!active || overview || collaborationRole === 'hidden' || !shell || shell.contains(document.activeElement)) return;
    /* Opening or restoring a window changes the keyboard context too. Focus
     * the frame itself before child effects run; an App may still promote a
     * more specific autofocus target, while Tab naturally enters titlebar and
     * content controls from here. */
    shell.focus({ preventScroll: true });
    // A bar/Dock activation restores this window's keyboard and scroll context.
    // Pointer activation inside an already focused descendant returned above.
    if (shell.closest('.paw-room-focus-satellite-rail')) shell.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
  }, [active, collaborationRole, overview]);
  useEffect(() => {
    if (authoredArrival) return;
    const surface = shellRef.current?.querySelector<HTMLElement>('.paw-window, .paw-planet-surface');
    if (!surface || typeof surface.animate !== 'function' || pawWindowReducedMotion()) return;
    return animateWindowArrival(surface);
    // Mount-only by design: re-running on prop drift would re-arrive a window
    // that is already on stage.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const transform = focusFrame
    ? `translate3d(${focusFrame.x}px, ${focusFrame.y}px, 0)`
    : overview && overviewFrame
    ? `translate3d(${overviewFrame.x}px, ${overviewFrame.y}px, 0) scale(${overviewFrame.scale})`
    : `translate3d(${bounds.x}px, ${bounds.y}px, 0)`;
  useWindowPlacementFlip(shellRef, {
    bounds,
    enabled: !focusFrame && !overview && frameMode === 'window',
    placement,
  });
  const shellStyle = {
    width: focusFrame?.width ?? bounds.width,
    height: focusFrame?.height ?? bounds.height,
    transform,
    zIndex: overviewFrame?.zIndex ?? zIndex,
    ...(focusFrame ? {
      '--paw-focus-frame-height': `${focusFrame.height}px`,
      '--paw-focus-frame-transform': transform,
      '--paw-focus-frame-width': `${focusFrame.width}px`,
    } : {}),
  } as CSSProperties;
  return (
    <PawWindowChromeProvider leading={windowLeadingChromeTarget} trailing={windowChromeTarget}>
      <section aria-label={`${title}${subtitle ? ` · ${subtitle}` : ''}窗口`} aria-hidden={collaborationRole === 'hidden' || undefined} className="paw-window-shell" data-active={active || undefined} data-app={appId} data-collaboration-role={collaborationRole} data-flow-state={flowState} data-focus-layout={focusFrame ? true : undefined} data-focus-locked={focusLocked || undefined} data-frame-mode={frameMode} data-overview={overview || undefined} data-paw-window-id={windowId} data-placement={placement} data-window-target={targetKind} inert={collaborationRole === 'hidden' || undefined} onKeyDown={(event) => {
        if (focusFrame && planetFrame && event.key === 'Escape') { event.stopPropagation(); onClose(); }
      }} onPointerDown={() => { if (!overview && !active) onFocus(); }} ref={shellRef} style={shellStyle} tabIndex={-1}>
        {planetFrame ? (
          <div className="paw-planet-surface" data-flow-state={flowState}>
            <header className="paw-planet-identity" onPointerDown={focusLocked ? undefined : drag}>
              <span aria-hidden="true" className="paw-planet-identity-mark" />
              <div className="paw-planet-identity-copy">
                <strong>{title}</strong>
                {subtitle ? <small title={subtitle}>{subtitle}</small> : null}
              </div>
              <div className="paw-planet-model-slot" ref={setWindowChromeTarget} />
              {onDetach ? <button aria-label={`在独立窗口打开${title}`} className="paw-planet-detach" onClick={onDetach} type="button"><ExternalLink aria-hidden="true" size={14} /></button> : null}
              <button
                aria-label={collapsiblePlanet ? `收起${title}详情` : `关闭${title}行星窗口`}
                className="paw-planet-close"
                onClick={() => collapsiblePlanet ? onClose() : exit('close', onClose)}
                onPointerDown={(event) => event.stopPropagation()}
                title={collapsiblePlanet ? '收起详情' : '关闭行星窗口'}
                type="button"
              >
                <X aria-hidden="true" size={12} />
              </button>
            </header>
            <MemoizedWindowBody>{children}</MemoizedWindowBody>
          </div>
        ) : (
          <div aria-hidden={overview || undefined} className="paw-window" inert={overview ? true : undefined}>
            <header className="paw-window-titlebar" data-window-chrome={windowChrome} onDoubleClick={overview || focusFrame ? undefined : onToggleMaximize} onPointerDown={overview || focusLocked ? undefined : drag}>
              {/* One chrome language: every window — main Room, collaboration
                * focus primary and focus-card satellite alike — opens with the
                * same red/yellow/green cluster in the same slot. The lights stay
                * the first children so the shared nth-child colour rules keep
                * mapping onto close/minimize/maximize; App-owned leading chrome
                * docks after them instead of pushing them out of position.
                * Inside the collaboration focus layout the layout owns geometry,
                * so the maximize verb drops for primary and satellite together
                * rather than only for satellites. */}
              <div className="paw-traffic-lights" onDoubleClick={(event) => event.stopPropagation()} onPointerDown={(event) => event.stopPropagation()}>
                <button aria-label="关闭窗口" data-action="close" onClick={() => exit('close', onClose)} title="关闭" type="button"><X size={9} /></button>
                <button aria-label="最小化窗口" data-action="minimize" onClick={() => exit('minimize', onMinimize)} title="最小化" type="button"><Minus size={9} /></button>
                {focusFrame ? null : <button aria-label={maximized ? '还原窗口' : '最大化窗口'} data-action={maximized ? 'restore' : 'maximize'} onClick={onToggleMaximize} title={maximized ? '还原' : '最大化'} type="button">{maximized ? <Minimize2 size={8} /> : <Maximize2 size={8} />}</button>}
                {windowChrome ? <div className="paw-window-leading-slot" onDoubleClick={(event) => event.stopPropagation()} onPointerDown={(event) => event.stopPropagation()} ref={setWindowLeadingChromeTarget} /> : null}
              </div>
              {roomFocusPrimary ? null : <div className="paw-window-title"><PawAppIcon appId={identityIconId} size={16} /><strong title={title}>{title}</strong>{subtitle ? <small>{subtitle}</small> : null}</div>}
              {windowChrome ? <div className="paw-window-chrome-slot" onDoubleClick={(event) => event.stopPropagation()} onPointerDown={(event) => event.stopPropagation()} ref={setWindowChromeTarget} /> : null}
            </header>
            <MemoizedWindowBody>{children}</MemoizedWindowBody>
          </div>
        )}
        {overview ? (
          <button aria-label={`打开 ${title}`} className="paw-overview-window-target" onClick={onOpenFromOverview} type="button"><PawAppIcon appId={identityIconId} size={24} /><span>{title}</span></button>
        ) : (planetFrame && !focusFrame) || focusLocked ? null : (
          <PawWindowResizeHandles active={active} bounds={interactionBounds} containToDesktop={containToDesktop} deferPointerInteractionUntilFocused={deferPointerInteractionUntilFocused} onBoundsCommit={onBoundsCommit} onFocus={onFocus} shellRef={shellRef} />
        )}
      </section>
    </PawWindowChromeProvider>
  );
}

type PawWindowResizeHandle = 'north' | 'south' | 'east' | 'west' | 'north-east' | 'north-west' | 'south-east' | 'south-west';
const pawWindowResizeHandles: PawWindowResizeHandle[] = ['north', 'south', 'east', 'west', 'north-east', 'north-west', 'south-east', 'south-west'];
const pawWindowResizeLabels: Record<PawWindowResizeHandle, string> = {
  north: '调整窗口上边缘',
  south: '调整窗口下边缘',
  east: '调整窗口右边缘',
  west: '调整窗口左边缘',
  'north-east': '调整窗口右上角',
  'north-west': '调整窗口左上角',
  'south-east': '调整窗口右下角',
  'south-west': '调整窗口左下角',
};

function PawWindowResizeHandles({ active, bounds, containToDesktop, deferPointerInteractionUntilFocused, onBoundsCommit, onFocus, shellRef }: {
  active: boolean;
  bounds: PawWindowBounds;
  containToDesktop: boolean;
  deferPointerInteractionUntilFocused: boolean;
  onBoundsCommit: (bounds: PawWindowBounds) => void;
  onFocus: () => void;
  shellRef: RefObject<HTMLElement | null>;
}) {
  return pawWindowResizeHandles.map((handle) => (
    <PawWindowResizeHandle
      active={active}
      bounds={bounds}
      containToDesktop={containToDesktop}
      deferPointerInteractionUntilFocused={deferPointerInteractionUntilFocused}
      handle={handle}
      key={handle}
      onBoundsCommit={onBoundsCommit}
      onFocus={onFocus}
      shellRef={shellRef}
    />
  ));
}

function PawWindowResizeHandle({ active, bounds, containToDesktop, deferPointerInteractionUntilFocused, handle, onBoundsCommit, onFocus, shellRef }: {
  active: boolean;
  bounds: PawWindowBounds;
  containToDesktop: boolean;
  deferPointerInteractionUntilFocused: boolean;
  handle: PawWindowResizeHandle;
  onBoundsCommit: (bounds: PawWindowBounds) => void;
  onFocus: () => void;
  shellRef: RefObject<HTMLElement | null>;
}) {
  const resize = useWindowResize(shellRef, bounds, handle, onBoundsCommit, onFocus, active, deferPointerInteractionUntilFocused, containToDesktop);
  const horizontal = handle.includes('east') || handle.includes('west');
  const vertical = handle.includes('north') || handle.includes('south');
  const keyShortcuts = [horizontal ? 'ArrowLeft ArrowRight' : '', vertical ? 'ArrowUp ArrowDown' : ''].filter(Boolean).join(' ');
  const resizeFromKeyboard = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    const deltaX = event.key === 'ArrowRight' ? 16 : event.key === 'ArrowLeft' ? -16 : 0;
    const deltaY = event.key === 'ArrowDown' ? 16 : event.key === 'ArrowUp' ? -16 : 0;
    if ((!deltaX || !horizontal) && (!deltaY || !vertical)) return;
    event.preventDefault();
    if (!active) onFocus();
    onBoundsCommit(resizeWindowBounds(bounds, handle, deltaX, deltaY, containToDesktop ? pawWindowArea() : undefined));
  };
  return <button
    aria-keyshortcuts={keyShortcuts}
    aria-label={pawWindowResizeLabels[handle]}
    className="paw-window-resize"
    data-handle={handle}
    onKeyDown={resizeFromKeyboard}
    onPointerDown={resize}
    tabIndex={active ? 0 : -1}
    type="button"
  />;
}

type OverviewFrame = { x: number; y: number; scale: number; zIndex: number };

function layoutOverview(
  nodes: Array<{ id: string; bounds: PawWindowBounds }>,
  viewport: { width: number; height: number },
): Map<string, OverviewFrame> {
  const frames = new Map<string, OverviewFrame>();
  if (nodes.length === 0) return frames;
  const insetX = 44;
  const insetTop = 34;
  const insetBottom = 104;
  const gap = 24;
  const availableWidth = Math.max(320, viewport.width - insetX * 2);
  const availableHeight = Math.max(240, viewport.height - insetTop - insetBottom);
  const columns = Math.min(nodes.length, Math.max(1, Math.ceil(Math.sqrt(nodes.length * availableWidth / availableHeight))));
  const rows = Math.ceil(nodes.length / columns);
  const cellWidth = (availableWidth - gap * (columns - 1)) / columns;
  const cellHeight = (availableHeight - gap * (rows - 1)) / rows;
  nodes.forEach((node, index) => {
    const column = index % columns;
    const row = Math.floor(index / columns);
    const scale = Math.min(.82, (cellWidth - 20) / node.bounds.width, (cellHeight - 38) / node.bounds.height);
    const previewWidth = node.bounds.width * scale;
    const previewHeight = node.bounds.height * scale;
    frames.set(node.id, {
      x: insetX + column * (cellWidth + gap) + (cellWidth - previewWidth) / 2,
      y: insetTop + row * (cellHeight + gap) + (cellHeight - previewHeight) / 2,
      scale,
      zIndex: index + 20,
    });
  });
  return frames;
}

const MemoizedWindowBody = memo(function WindowBody({ children }: { children: ReactNode }) {
  return <div className="paw-window-body">{children}</div>;
});

function pawWindowReducedMotion(): boolean {
  if (document.documentElement.getAttribute('data-reduce-motion') === 'true') return true;
  return typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

type PawWindowPlacementFrame = {
  bounds: PawWindowBounds;
  placement?: PawWindowPlacement;
};

/* Maximize, restore and snap are layout changes, but their visible trip does
 * not have to be. React commits the destination width/height once; this FLIP
 * animation paints the previous rectangle through an inverse transform and
 * lets the compositor carry it to the destination. Live drag/resize never
 * enters this path, and overview/focus layouts keep their own choreography. */
function useWindowPlacementFlip(
  ref: RefObject<HTMLElement | null>,
  next: PawWindowPlacementFrame & { enabled: boolean },
): void {
  const previousRef = useRef<PawWindowPlacementFrame | undefined>(undefined);
  useLayoutEffect(() => {
    const previous = previousRef.current;
    previousRef.current = { bounds: next.bounds, placement: next.placement };
    const shell = ref.current;
    if (
      !previous
      || !next.enabled
      || previous.placement === next.placement
      || !shell
      || typeof shell.animate !== 'function'
      || pawWindowReducedMotion()
      || next.bounds.width <= 0
      || next.bounds.height <= 0
    ) return undefined;

    const scaleX = previous.bounds.width / next.bounds.width;
    const scaleY = previous.bounds.height / next.bounds.height;
    shell.dataset.placementAnimation = 'true';
    const animation = shell.animate([
      {
        transform: `translate3d(${previous.bounds.x}px, ${previous.bounds.y}px, 0) scale(${scaleX}, ${scaleY})`,
      },
      {
        transform: `translate3d(${next.bounds.x}px, ${next.bounds.y}px, 0) scale(1, 1)`,
      },
    ], {
      duration: 180,
      easing: 'cubic-bezier(.23, 1, .32, 1)',
    });
    const clear = () => {
      if (shell.dataset.placementAnimation) delete shell.dataset.placementAnimation;
    };
    const finished = (animation as unknown as { finished?: Promise<Animation> }).finished;
    const fallbackTimer = finished ? 0 : window.setTimeout(clear, 180);
    if (finished) void finished.then(clear, clear);
    return () => {
      if (fallbackTimer) window.clearTimeout(fallbackTimer);
      animation.cancel();
      clear();
    };
  }, [next.bounds.height, next.bounds.width, next.bounds.x, next.bounds.y, next.enabled, next.placement, ref]);
}

function useWindowExit(
  ref: RefObject<HTMLElement | null>,
  appId: PawAppId,
  subscribeCancellation?: (cancel: () => void) => () => void,
) {
  const pendingRef = useRef<{ animation: Animation; surface: HTMLElement } | null>(null);
  const cancel = useCallback(() => {
    const pending = pendingRef.current;
    if (!pending) return;
    // Invalidate before cancel(): its finished promise rejects asynchronously.
    pendingRef.current = null;
    delete pending.surface.dataset.exiting;
    pending.animation.cancel();
  }, []);
  useLayoutEffect(() => {
    const unsubscribe = subscribeCancellation?.(cancel);
    return () => { unsubscribe?.(); cancel(); };
  }, [cancel, subscribeCancellation]);
  return useCallback((kind: 'close' | 'minimize', finish: () => void) => {
    if (pendingRef.current) return;
    const surface = ref.current?.querySelector<HTMLElement>('.paw-window');
    if (!surface || typeof surface.animate !== 'function' || pawWindowReducedMotion()) {
      finish();
      return;
    }
    if (surface.dataset.exiting) return;
    surface.dataset.exiting = kind;
    let duration = 160;
    let easing = 'cubic-bezier(.77, 0, .175, 1)';
    let target = 'translate3d(0, 10px, 0) scale(.97)';
    if (kind === 'minimize') {
      duration = 180;
      target = 'translate3d(0, 34px, 0) scale(.9)';
      /* Minimize flies to the App's Dock tile, the way the reference genie
       * reads: window centre travels to the tile centre while the frame
       * scales toward the tile, 280ms on the shared genie curve. When the
       * Dock is hidden (a maximized window owns the desktop) or the tile
       * cannot be measured, the window keeps the older sink-in-place exit. */
      const genie = dockTileGenieDelta(appId, surface);
      if (genie) {
        target = `translate3d(${genie.dx.toFixed(1)}px, ${genie.dy.toFixed(1)}px, 0) scale(${genie.scale.toFixed(3)})`;
        duration = 280;
        easing = 'cubic-bezier(.4, 0, .2, 1)';
      }
    }
    const animation = surface.animate([
      { opacity: 1, transform: 'translate3d(0, 0, 0) scale(1)' },
      { opacity: 0, transform: target },
    ], {
      duration,
      easing,
      fill: 'forwards',
    });
    const pending = { animation, surface };
    pendingRef.current = pending;
    const complete = () => {
      if (pendingRef.current !== pending) return;
      cancel(); // Drop fill-forwards and the exiting marker before committing.
      finish();
    };
    void animation.finished.then(complete, complete);
  }, [ref, appId, cancel]);
}

/* The one genie geometry, shared by both directions of the minimize round
 * trip: the delta from the window's centre to its App's Dock tile centre and
 * the scale that lands the frame on the tile. Null when the tile cannot be
 * measured (the Dock is hidden while a maximized window owns the desktop), so
 * both callers can fall back to their in-place choreography. */
function dockTileGenieDelta(appId: PawAppId, surface: HTMLElement): { dx: number; dy: number; scale: number } | null {
  const tile = document.querySelector<HTMLElement>(`.paw-dock [data-desktop-app="${appId}"]`);
  if (!tile) return null;
  const windowRect = surface.getBoundingClientRect();
  const tileRect = tile.getBoundingClientRect();
  if (windowRect.width <= 0 || tileRect.width <= 0) return null;
  return {
    dx: tileRect.left + tileRect.width / 2 - (windowRect.left + windowRect.width / 2),
    dy: tileRect.top + tileRect.height / 2 - (windowRect.top + windowRect.height / 2),
    scale: Math.max(.06, Math.min(.24, tileRect.width / windowRect.width)),
  };
}

function useWindowDrag(ref: RefObject<HTMLElement | null>, bounds: PawWindowBounds, commit: (bounds: PawWindowBounds) => void, focus: () => void, snap: ((placement: PawWindowPlacement) => void) | undefined, active: boolean, deferPointerInteractionUntilFocused: boolean, containToDesktop: boolean) {
  return useCallback((event: ReactPointerEvent<HTMLElement>) => {
    if (event.button !== 0 || (event.target as HTMLElement).closest('button')) return;
    event.preventDefault();
    if (!active) {
      focus();
      if (deferPointerInteractionUntilFocused) return;
    }
    const shell = ref.current;
    if (!shell) return;
    const desktopRoot = shell.closest<HTMLElement>('.paw-desktop-root');
    event.currentTarget.setPointerCapture(event.pointerId);
    shell.dataset.interaction = 'dragging';
    setWindowInteraction(desktopRoot, true);
    const origin = { x: event.clientX, y: event.clientY };
    /* Measured once per gesture, never per move: the desktop cannot resize
     * while a captured pointer owns the drag, and reading it per event would
     * put a layout-dependent measurement on the frame path. */
    const area = containToDesktop ? pawWindowArea() : null;
    let next = bounds;
    let frame = 0;
    const render = () => {
      frame = 0;
      shell.style.transform = `translate3d(${next.x}px, ${next.y}px, 0)`;
    };
    const move = (moveEvent: PointerEvent) => {
      const travelled = { ...bounds, x: bounds.x + moveEvent.clientX - origin.x, y: bounds.y + moveEvent.clientY - origin.y };
      /* Ordinary windows may keep a recoverable partial offset instead of
       * sticking to the canvas edge. Focus layouts still own their own clamp. */
      next = area ? fitReachablePawWindowBounds(travelled, area) : { ...travelled, y: Math.max(0, travelled.y) };
      setSnapPreview(desktopRoot, snapPlacement(moveEvent.clientX, moveEvent.clientY));
      if (!frame) frame = window.requestAnimationFrame(render);
    };
    const finish = (finishEvent: PointerEvent) => {
      if (frame) window.cancelAnimationFrame(frame);
      render();
      delete shell.dataset.interaction;
      setSnapPreview(desktopRoot);
      setWindowInteraction(desktopRoot, false);
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', finish);
      window.removeEventListener('pointercancel', cancel);
      const placement = snapPlacement(finishEvent.clientX, finishEvent.clientY);
      if (placement && snap) {
        snap(placement);
        return;
      }
      commit(next);
    };
    const cancel = () => {
      if (frame) window.cancelAnimationFrame(frame);
      delete shell.dataset.interaction;
      setSnapPreview(desktopRoot);
      setWindowInteraction(desktopRoot, false);
      shell.style.transform = `translate3d(${bounds.x}px, ${bounds.y}px, 0)`;
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', finish);
      window.removeEventListener('pointercancel', cancel);
    };
    // The move stream never calls preventDefault; passive keeps the
    // compositor thread free while the pointer drives the transform.
    window.addEventListener('pointermove', move, { passive: true });
    window.addEventListener('pointerup', finish);
    window.addEventListener('pointercancel', cancel);
  }, [active, bounds, commit, containToDesktop, deferPointerInteractionUntilFocused, focus, ref, snap]);
}

function setSnapPreview(root: HTMLElement | null, placement?: PawWindowPlacement): void {
  if (!root) return;
  // Pointermove can outpace the frame rate; rewriting the same attribute on
  // the desktop root would invalidate style for the whole desktop subtree on
  // every event, so only touch the DOM when the preview actually changes.
  if (root.dataset.snapPreview === placement) return;
  if (placement) root.dataset.snapPreview = placement;
  else delete root.dataset.snapPreview;
}

/* Exactly one attribute write per gesture edge (start and finish), never per
 * move event. The wallpaper reads this to pause weather animation while a
 * window drag/resize owns the frame budget. */
function setWindowInteraction(root: HTMLElement | null, active: boolean): void {
  if (!root) return;
  if (active) root.dataset.windowInteraction = 'true';
  else delete root.dataset.windowInteraction;
}

function snapPlacement(clientX: number, clientY: number): PawWindowPlacement | undefined {
  if (clientY <= 14) return 'maximized';
  if (clientX <= 14) return 'left';
  if (clientX >= window.innerWidth - 14) return 'right';
  return undefined;
}

function useWindowResize(ref: RefObject<HTMLElement | null>, bounds: PawWindowBounds, handle: PawWindowResizeHandle, commit: (bounds: PawWindowBounds) => void, focus: () => void, active: boolean, deferPointerInteractionUntilFocused: boolean, containToDesktop: boolean) {
  return useCallback((event: ReactPointerEvent<HTMLElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    if (!active) {
      focus();
      if (deferPointerInteractionUntilFocused) return;
    }
    const shell = ref.current;
    if (!shell) return;
    const desktopRoot = shell.closest<HTMLElement>('.paw-desktop-root');
    event.currentTarget.setPointerCapture(event.pointerId);
    shell.dataset.interaction = 'resizing';
    shell.style.transformOrigin = '0 0';
    shell.style.willChange = 'transform';
    setWindowInteraction(desktopRoot, true);
    const origin = { x: event.clientX, y: event.clientY };
    const area = containToDesktop ? pawWindowArea() : undefined;
    let next = bounds;
    let frame = 0;
    const render = () => {
      frame = 0;
      shell.style.transform = `translate3d(${next.x}px, ${next.y}px, 0) scale(${next.width / bounds.width}, ${next.height / bounds.height})`;
    };
    const move = (moveEvent: PointerEvent) => {
      next = resizeWindowBounds(bounds, handle, moveEvent.clientX - origin.x, moveEvent.clientY - origin.y, area);
      if (!frame) frame = window.requestAnimationFrame(render);
    };
    const finish = () => {
      if (frame) window.cancelAnimationFrame(frame);
      /* The pointer stream only touched a compositor transform. Commit the
       * final geometry once, then remove the preview scale; this is the sole
       * live-resize layout pass. */
      shell.style.width = `${next.width}px`;
      shell.style.height = `${next.height}px`;
      shell.style.transform = `translate3d(${next.x}px, ${next.y}px, 0)`;
      shell.style.transformOrigin = '';
      shell.style.willChange = '';
      delete shell.dataset.interaction;
      setWindowInteraction(desktopRoot, false);
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', finish);
      window.removeEventListener('pointercancel', finish);
      commit(next);
    };
    window.addEventListener('pointermove', move, { passive: true });
    window.addEventListener('pointerup', finish);
    window.addEventListener('pointercancel', finish);
  }, [active, bounds, commit, containToDesktop, deferPointerInteractionUntilFocused, focus, handle, ref]);
}

/** UR-057 / PF-CM-005：每条边都跟随指针 1:1，但窗口最小尺寸与桌面边界都是硬约束。
 *  A north/west drag moves the opposite edge, so without the area limit the
 *  titlebar can be pushed above the desktop where no pointer can reach it. */
export function resizeWindowBounds(
  bounds: PawWindowBounds,
  handle: PawWindowResizeHandle,
  deltaX: number,
  deltaY: number,
  area?: PawWindowBounds,
): PawWindowBounds {
  const left = area?.x ?? 0;
  const top = area?.y ?? 0;
  const right = area ? area.x + area.width : Number.POSITIVE_INFINITY;
  const bottom = area ? area.y + area.height : Number.POSITIVE_INFINITY;
  const next = { ...bounds };
  if (handle.includes('east')) {
    next.width = Math.min(Math.max(PAW_WINDOW_MIN_WIDTH, bounds.width + deltaX), Math.max(PAW_WINDOW_MIN_WIDTH, right - bounds.x));
  }
  if (handle.includes('south')) {
    next.height = Math.min(Math.max(PAW_WINDOW_MIN_HEIGHT, bounds.height + deltaY), Math.max(PAW_WINDOW_MIN_HEIGHT, bottom - bounds.y));
  }
  if (handle.includes('west')) {
    next.width = Math.min(Math.max(PAW_WINDOW_MIN_WIDTH, bounds.width - deltaX), Math.max(PAW_WINDOW_MIN_WIDTH, bounds.x + bounds.width - left));
    next.x = bounds.x + bounds.width - next.width;
  }
  if (handle.includes('north')) {
    next.height = Math.min(Math.max(PAW_WINDOW_MIN_HEIGHT, bounds.height - deltaY), Math.max(PAW_WINDOW_MIN_HEIGHT, bounds.y + bounds.height - top));
    next.y = bounds.y + bounds.height - next.height;
  }
  return next;
}
