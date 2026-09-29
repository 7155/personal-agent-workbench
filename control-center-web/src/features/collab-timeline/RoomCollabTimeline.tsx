import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { usePageVisibility } from '@/platform/use-page-visibility';
import { selectPublicRoomTurnOrder } from '@/features/rooms/runtime/room-execution-lanes';
import type { AgentSubagentRunV1 } from '@/contracts/generated/agent-subagent-run.v1';
import type { RoomProjectionState } from '@/contracts/room-reducer';
import { RoomPlanetAvatar, type RoomPlanetActivity } from '@/features/rooms/RoomPlanetAvatar';
import type { RoomSummary } from '@/features/rooms/room-types';
import type { JevSnapshot } from '@/features/semantic-workspace/jev-execution';
import { CollabTimelineOverlay, CollabTimelinePeek, CollabTimelineStage, type CollabAvatarRenderer } from './CollabTimelineStage';
import type { CollabLane, CollabLaneState } from './model';
import { buildRoomCollabTimeline } from './room-timeline';

const ACTIVITY: Record<CollabLaneState, RoomPlanetActivity> = {
  idle: 'static', thinking: 'thinking', working: 'working', waiting: 'waiting', reviewing: 'thinking', done: 'done', error: 'error', stopped: 'stopped',
};

export function roomPlanetAvatarRenderer(active: boolean): CollabAvatarRenderer {
  return (lane, size, state) => lane.ordinal === undefined ? null
    : <RoomPlanetAvatar ordinal={lane.ordinal} size={size} decorative activity={active ? ACTIVITY[state] : 'static'} />;
}

export interface RoomCollabTimelineProps {
  room?: RoomSummary;
  projection?: RoomProjectionState;
  graph?: JevSnapshot | null;
  satellites?: Record<string, readonly AgentSubagentRunV1[] | undefined>;
  /** A specific Root (e.g. the Jev graph root); defaults to the latest public one. */
  rootId?: string;
  active: boolean;
  onOpenParticipant?: (participantId: string) => void;
  onOpenSatellite?: (lane: CollabLane) => void;
}

/** Full Room stage. Pure projection over existing Room / Jev / Tool Agent state. */
export function RoomCollabTimeline({ room, projection, graph, satellites, rootId, active, onOpenParticipant, onOpenSatellite, eventsPanel = true }: RoomCollabTimelineProps & { eventsPanel?: boolean }) {
  const timeline = useRoomTimeline({ room, projection, graph, satellites, rootId, active });
  if (!timeline) return null;
  return <CollabTimelineStage timeline={timeline} active={active} eventsPanel={eventsPanel} renderAvatar={roomPlanetAvatarRenderer(active)}
    onOpenLane={(lane) => lane.kind === 'partner' ? onOpenParticipant?.(lane.id) : lane.kind === 'satellite' ? onOpenSatellite?.(lane) : undefined} />;
}

/** Rail peek that expands into the full stage. */
export function RoomCollabTimelineLauncher(props: RoomCollabTimelineProps & { renderTrigger?: (open: () => void) => ReactNode }) {
  const [open, setOpen] = useState(false);
  const timeline = useRoomTimeline({ ...props, active: props.active });
  if (!timeline || !timeline.lanes.some((lane) => lane.kind !== 'origin' && timeline.segments.some((segment) => segment.laneId === lane.id))) return null;
  return <>
    {props.renderTrigger ? props.renderTrigger(() => setOpen(true))
      : <CollabTimelinePeek timeline={timeline} active={props.active} renderAvatar={roomPlanetAvatarRenderer(false)} onExpand={() => setOpen(true)} />}
    <CollabTimelineOverlay open={open} onClose={() => setOpen(false)} title="协作全景">
      <RoomCollabTimeline {...props} active={props.active && open} onOpenParticipant={(id) => { setOpen(false); props.onOpenParticipant?.(id); }} />
    </CollabTimelineOverlay>
  </>;
}

export function useRoomTimeline({ room, projection, graph, satellites, rootId, active }: Omit<RoomCollabTimelineProps, 'onOpenParticipant'>) {
  const turn = projection && rootId ? projection.turnsById[rootId] : undefined;
  const dataLive = Boolean(projection && (rootId
    ? turn && (turn.status === 'running' || turn.status === 'queued') && !graph?.final
    : roomRoundLive(projection)));
  // A clock only while the round is live and watched, so open segments grow.
  const now = useLiveClock(dataLive && active);
  return useMemo(() => (room ? buildRoomCollabTimeline({ room, projection, graph: graph ?? null, satellites, live: dataLive, nowMs: now, ...(rootId ? { rootId } : {}) }) : undefined), [room, projection, graph, satellites, rootId, dataLive, now]);
}

function roomRoundLive(projection: RoomProjectionState): boolean {
  const id = selectPublicRoomTurnOrder(projection).at(-1);
  return Boolean(id && ['running', 'queued'].includes(projection.turnsById[id]?.status ?? ''));
}

export function useLiveClock(enabled: boolean, intervalMs = 1000): number {
  const visible = usePageVisibility();
  const ticking = enabled && visible;
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!ticking) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [ticking, intervalMs]);
  return now;
}
