import { MessageCircle, Satellite, X } from 'lucide-react';
import { useMemo, useRef, useState } from 'react';
import type { PawOsWindowRequest } from '@/features/paw-os/surface-context';
import { useControlTransport } from '@/app/control-transport';
import { useRoomLiveStore } from '@/features/rooms/state/live-store';
import { useRoomLiveSession } from '@/features/rooms/runtime/use-room-live-session';
import type { RoomSummary } from '@/features/rooms/room-types';
import { usePageVisibility } from '@/platform/use-page-visibility';
import type { PawWindowNode } from '../runtime/desktop-store';
import { FocusFlowLedger } from './PawRoomFocusOverview';
import { useRoomLiveFocusData } from './PawRoomLiveFocusOverview';
import { buildRoomFocusProjection, roomFocusHasCoordinator, roomFocusOriginLabel, roomFocusStateLabel, type RoomFocusProjection } from './room-focus-projection';
import { roomPlanetObserverWindowRequest } from './room-satellite-auto-open';
import type { RoomSatelliteSnapshots } from './room-message-flow';
import '../styles/paw-os-room-focus.css';

type ParticipantsProps = {
  roomId: string;
  windows: PawWindowNode[];
  selectedParticipantId?: string;
  onInspect: (request: PawOsWindowRequest) => void;
  onCloseInspector: () => void;
};

/** This lazy shell surface reads the same retained snapshot as the main Room.
 * Missing Runtime data stays unknown; an open observer is never a status. */
export default function PawRoomFocusParticipants(props: ParticipantsProps) {
  const transport = useControlTransport();
  const pageVisible = usePageVisibility();
  const [room, setRoom] = useState(() => focusRoomRecord(useRoomLiveStore.getState().snapshotsByRoomId[props.roomId]?.room));
  const projection = useRoomLiveStore((state) => state.projections[props.roomId]);
  // A cold-open conversation snapshot is not kept in snapshotsByRoomId. Join
  // the main Room's existing shared lease to receive it and later roster
  // metadata; this does not start a second subscription or execution loop.
  useRoomLiveSession({
    roomId: props.roomId,
    transport,
    active: pageVisible,
    onSnapshot: (_roomId, snapshot) => setRoom(focusRoomRecord(snapshot.room)),
    onMetadata: (_roomId, value) => {
      const record = typeof value === 'object' && value !== null ? value as { room?: unknown } : {};
      const next = focusRoomRecord(record.room);
      if (next) setRoom(next);
    },
    onLoadingChange: () => undefined,
    onConnectionRestored: () => undefined,
    onConnectionError: () => undefined,
    onRecoveryState: () => undefined,
    onEvents: () => undefined,
  });
  const focus = useMemo(() => room ? buildRoomFocusProjection(room, projection) : null, [room, projection]);
  if (!room || !focus) return <nav aria-label="Room 伙伴" className="paw-room-focus-participants">
    <div className="paw-room-focus-participants__track">
      {props.windows.map((node) => <button
        aria-pressed={node.target?.id === props.selectedParticipantId}
        className="paw-room-focus-participant"
        key={node.id}
        onClick={() => props.onInspect({ appId: node.appId, target: node.target! })}
        type="button"
      ><i aria-hidden="true" data-state="unknown" /><strong>{node.title}</strong><span>状态同步中</span></button>)}
      {!props.windows.length ? <span className="paw-room-focus-participants__empty">正在恢复伙伴状态…</span> : null}
    </div>
  </nav>;
  return <LiveParticipants {...props} focus={focus} onSelect={(participantId) => {
    const participant = room.participants.find((item) => item.id === participantId);
    if (participant) props.onInspect(roomPlanetObserverWindowRequest(participant, props.roomId));
  }} />;
}

function focusRoomRecord(value: unknown): RoomSummary | undefined {
  if (typeof value !== 'object' || value === null) return undefined;
  const record = value as Partial<RoomSummary>;
  // Same metadata boundary as PawRoomWorkspace, after the shared Runtime
  // snapshot parser. No routing, permission, or status defaults are invented.
  return typeof record.id === 'string' && typeof record.title === 'string' && Array.isArray(record.participants)
    ? record as RoomSummary : undefined;
}

function LiveParticipants({ focus, onSelect, ...props }: ParticipantsProps & {
  focus: RoomFocusProjection;
  onSelect: (participantId: string) => void;
}) {
  const data = useRoomLiveFocusData(props.roomId, focus);
  return <PawRoomFocusParticipantBar {...data} selectedParticipantId={props.selectedParticipantId} onSelect={onSelect} onCloseInspector={props.onCloseInspector} />;
}

/** One action per participant. The full report stays in the main conversation. */
export function PawRoomFocusParticipantBar({ focus, satellitesByParticipant, selectedParticipantId, onSelect, onCloseInspector, intercomStatus, onRefreshTraffic }: {
  focus: RoomFocusProjection;
  satellitesByParticipant: RoomSatelliteSnapshots;
  selectedParticipantId?: string;
  onSelect: (participantId: string) => void;
  onCloseInspector: () => void;
  intercomStatus?: 'loading' | 'ready' | 'error';
  onRefreshTraffic?: () => void;
}) {
  const [trafficOpen, setTrafficOpen] = useState(false);
  const trafficTrigger = useRef<HTMLButtonElement>(null);
  const trafficId = `room-focus-traffic-${focus.goal.rootId}`;
  const closeTraffic = () => {
    setTrafficOpen(false);
    trafficTrigger.current?.focus({ preventScroll: true });
  };
  const inspect = (participantId: string) => {
    setTrafficOpen(false);
    onSelect(participantId);
  };
  return <>
    <nav aria-label="Room 伙伴" className="paw-room-focus-participants">
      <div className="paw-room-focus-participants__track">
        {focus.partners.map((partner) => {
          const snapshot = satellitesByParticipant[partner.participantId];
          const satelliteLabel = snapshot?.status === 'ready' ? `卫星 ${snapshot.satellites.length}`
            : snapshot?.status === 'error' ? '卫星暂不可用' : '卫星读取中';
          const stateLabel = roomFocusStateLabel(partner.state);
          const memberLabel = partner.ownerDisplayName?.trim()
            ? `${partner.ownerDisplayName.trim()} · ${partner.celestialName}`
            : partner.celestialName;
          return <button
            aria-label={`${memberLabel}，${stateLabel}，${satelliteLabel}`}
            aria-pressed={selectedParticipantId === partner.participantId}
            className="paw-room-focus-participant"
            key={partner.participantId}
            onClick={() => inspect(partner.participantId)}
            title={`${memberLabel} · ${partner.displayName} · ${partner.currentAction}`}
            type="button"
          >
            <i aria-hidden="true" data-state={partner.state} />
            <strong>{memberLabel}</strong>
            <span>{stateLabel}</span>
            <small><Satellite aria-hidden="true" size={12} />{satelliteLabel}</small>
          </button>;
        })}
        {!focus.partners.length ? <span className="paw-room-focus-participants__empty">还没有伙伴加入</span> : null}
      </div>
      <button aria-controls={trafficId} aria-expanded={trafficOpen} className="paw-room-focus-participants__traffic" onClick={() => {
        if (trafficOpen) closeTraffic();
        else { onCloseInspector(); setTrafficOpen(true); }
      }} ref={trafficTrigger} type="button"><MessageCircle aria-hidden="true" size={15} /><span>消息流</span></button>
    </nav>
    {trafficOpen ? <section aria-label="Room 消息流" className="paw-room-focus-traffic paw-room-focus-overview" id={trafficId} onKeyDown={(event) => {
      if (event.key === 'Escape') { event.stopPropagation(); closeTraffic(); }
    }}>
      <header className="paw-room-focus-traffic__header"><strong>消息流</strong><button aria-label="关闭消息流" onClick={closeTraffic} type="button"><X aria-hidden="true" size={16} /></button></header>
      <FocusFlowLedger
        flow={focus.flow}
        originLabel={roomFocusOriginLabel(roomFocusHasCoordinator(focus.partners))}
        partners={focus.partners}
        rootId={focus.goal.rootId}
        workItems={focus.workItems}
        intercomStatus={intercomStatus}
        onOpenParticipant={inspect}
        onRefreshTraffic={onRefreshTraffic}
      />
    </section> : null}
  </>;
}
