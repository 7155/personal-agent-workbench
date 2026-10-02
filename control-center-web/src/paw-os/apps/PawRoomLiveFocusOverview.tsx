import { useQueries, useQuery } from '@tanstack/react-query';
import { useMemo, type ComponentProps } from 'react';
import type { AgentSubagentRunV1 } from '@/contracts/generated/agent-subagent-run.v1';
import type { RoomProjectionState } from '@/contracts/room-reducer';
import type { JevSnapshot } from '@/features/semantic-workspace/jev-execution';
import { RoomCollabTimeline } from '@/features/collab-timeline/RoomCollabTimeline';
import { usePawOsDesktop } from '@/features/paw-os/surface-context';
import type { RoomSummary } from '@/features/rooms/room-types';
import { useControlTransport } from '@/app/control-transport';
import { usePageVisibility } from '@/platform/use-page-visibility';
import { hasActiveSubagentRuns, subagentRuns } from '@/features/agent/status/subagent-data';
import { PawRoomFocusOverview } from './PawRoomFocusOverview';
import type { RoomFocusProjection } from './room-focus-projection';
import { hasRoomSatelliteSnapshot, mergeRoomMessageFlow, roomIntercomMessages, roomSatellites, type RoomSatelliteSnapshots } from './room-message-flow';

/** Read-only joins over existing Pi/Room APIs. No Session is opened or started. */
export function PawRoomLiveFocusOverview({
  roomId,
  active = true,
  ...props
}: ComponentProps<typeof PawRoomFocusOverview> & { roomId: string; active?: boolean }) {
  const { rawSatellites: _raw, ...data } = useRoomLiveFocusData(roomId, props.focus, active);
  return <PawRoomFocusOverview {...props} {...data} />;
}

/** Room timeline with each partner's retained Tool Agent satellites joined in. */
export function RoomCollabTimelineLive({ roomId, focus, room, projection, graph, active = true, onOpenParticipant, onSelectRoot }: {
  roomId: string;
  focus?: RoomFocusProjection;
  room: RoomSummary;
  projection?: RoomProjectionState;
  graph?: JevSnapshot | null;
  onSelectRoot?: (rootId: string) => void;
  active?: boolean;
  onOpenParticipant?: (participantId: string) => void;
}) {
  const desktop = usePawOsDesktop();
  const empty = useMemo<RoomFocusProjection>(() => ({ goal: { title: '', description: '', rootId: '', state: 'idle' }, workItems: [], partners: [], handoffs: [], flow: [], rootEvidence: [], counts: { active: 0, review: 0, blocked: 0, completed: 0 } }), []);
  const data = useRoomLiveFocusData(roomId, focus ?? empty, active);
  const satellites = useMemo(() => Object.fromEntries((focus?.partners ?? []).map((partner) => [partner.sessionId, data.rawSatellites[partner.participantId] ?? []])), [focus, data.rawSatellites]);
  return <RoomCollabTimeline room={room} projection={projection} graph={graph} onSelectRoot={onSelectRoot} satellites={satellites} active={active}
    {...(onOpenParticipant ? { onOpenParticipant } : {})}
    onOpenSatellite={(lane) => {
      const owner = room.participants.find((participant) => participant.id === lane.parentId);
      if (!owner || !desktop || !lane.runId) return;
      desktop.openWindow({ appId: 'agent', target: { kind: 'subagent', id: lane.runId, sessionId: owner.sessionId, title: lane.label, subtitle: `卫星 · ${room.title}` } });
    }} />;
}

/** Shared read-only data for the overview and the compact collaboration bar. */
export function useRoomLiveFocusData(roomId: string, focus: RoomFocusProjection, active = true) {
  const transport = useControlTransport();
  const pageVisible = usePageVisibility();
  const enabled = active && pageVisible;
  const partners = focus.partners;
  const roomBusy = ['running', 'waiting'].includes(focus.goal.state)
    || partners.some((partner) => ['running', 'waiting'].includes(partner.state));
  const sessionIds = partners.map((partner) => partner.sessionId);
  const satelliteQueries = useQueries({
    queries: partners.map((partner) => ({
      queryKey: ['paw-room-focus', 'satellites', partner.sessionId],
      queryFn: async ({ signal }: { signal: AbortSignal }) => {
        const value = await transport.request({ pathId: 'agent.subagents.list', query: { sessionId: partner.sessionId, limit: 50 }, signal });
        if (!hasRoomSatelliteSnapshot(value, partner.sessionId)) throw new Error('卫星数据未返回');
        return value;
      },
      enabled: enabled && Boolean(partner.sessionId),
      refetchInterval: enabled ? (query: { state: { data: unknown } }) => roomBusy || hasActiveSubagentRuns(subagentRuns(query.state.data)) ? 5_000 : false : false as const,
      retry: false,
    })),
  });
  // Store.list_for_session selects the entire Room queue, so one connected
  // participant is enough. Querying every planet would fetch duplicates.
  const reader = partners.find((partner) => partner.collaborationRole === 'coordinator' && partner.state !== 'disconnected')
    ?? partners.find((partner) => partner.state !== 'disconnected');
  const intercomQuery = useQuery({
    queryKey: ['paw-room-focus', 'intercom', roomId, reader?.sessionId],
    queryFn: async ({ signal }) => {
      const response = await transport.request({ pathId: 'agent.session.intercom.list', params: { sessionId: reader!.sessionId }, query: { limit: 200 }, signal });
      const value = response as { ok?: boolean; items?: unknown[] };
      if (!value?.ok || !Array.isArray(value.items)) throw new Error('直接通信记录未返回');
      return response;
    },
    enabled: enabled && Boolean(reader?.sessionId),
    refetchInterval: enabled ? (query) => roomBusy
      || satelliteQueries.some((satelliteQuery) => hasActiveSubagentRuns(subagentRuns(satelliteQuery.data)))
      || roomIntercomMessages(query.state.data, roomId).some((message) => ['queued', 'delivering'].includes(message.status))
      ? 3_000 : false : false,
    retry: false,
  });
  const satellitesByParticipant: RoomSatelliteSnapshots = Object.fromEntries(partners.map((partner, index) => {
    const query = satelliteQueries[index]!;
    return [partner.participantId, {
      status: query.data ? 'ready' : query.isError ? 'error' : 'loading',
      satellites: query.data ? roomSatellites(query.data, sessionIds) : [],
      ...(query.dataUpdatedAt ? { updatedAtMs: query.dataUpdatedAt } : {}),
      ...(query.isError ? { error: '暂时无法读取卫星状态' } : {}),
    }];
  }));
  const flow = mergeRoomMessageFlow(focus.flow, roomIntercomMessages(intercomQuery.data, roomId));
  const rawSatellites: Record<string, AgentSubagentRunV1[]> = Object.fromEntries(partners.map((partner, index) => {
    const query = satelliteQueries[index]!;
    return [partner.participantId, query.data ? subagentRuns(query.data).filter((run) => !sessionIds.includes(run.childSessionId)) : []];
  }));
  return {
    focus: { ...focus, flow },
    satellitesByParticipant,
    rawSatellites,
    intercomStatus: (!reader || intercomQuery.isError ? 'error' : intercomQuery.isSuccess ? 'ready' : 'loading') as 'loading' | 'ready' | 'error',
    onRefreshTraffic: () => { void intercomQuery.refetch(); satelliteQueries.forEach((query) => { void query.refetch(); }); },
  };
}
