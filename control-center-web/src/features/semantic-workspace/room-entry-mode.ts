import { useEffect, useRef, useState } from 'react';
import type { ControlTransport } from '@/platform/transport';
import { usePageVisibility } from '@/platform/use-page-visibility';
import { publicAgentErrorText } from '@/features/agent/public-error';
import type { AgentInterfaceMode } from './agent-mode-store';
import { jevRecord, parseJevList } from './jev-execution';
import { recoveryScope } from './workspace-recovery';

const localChoices = new WeakMap<ControlTransport, Map<string, AgentInterfaceMode>>();
const choiceKey = (transport: ControlTransport, roomId: string) => {
  const scope = recoveryScope(transport, `room:${roomId}`);
  return scope ? `${scope}:entry-mode` : '';
};

/** A scoped UI hint for reopening a Room, never a Runtime or dispatch owner. */
export function rememberRoomEntryMode(transport: ControlTransport, roomId: string, mode: AgentInterfaceMode) {
  let choices = localChoices.get(transport);
  if (!choices) { choices = new Map(); localChoices.set(transport, choices); }
  choices.set(roomId, mode);
  const key = choiceKey(transport, roomId);
  if (key) try { localStorage.setItem(key, mode); } catch { /* Keep this window's explicit choice if storage is unavailable. */ }
}

export function readRoomEntryMode(transport: ControlTransport, roomId: string): AgentInterfaceMode | undefined {
  const current = localChoices.get(transport)?.get(roomId);
  if (current) return current;
  const key = choiceKey(transport, roomId);
  try {
    const saved = key ? localStorage.getItem(key) : null;
    return saved === 'jev' || saved === 'traditional' ? saved : undefined;
  } catch { return undefined; }
}

/** Read existing graph evidence without creating or resuming work. */
export function useRoomEntryMode({ roomId, roomKind, transport, active = true }: {
  roomId: string; roomKind?: string; transport: ControlTransport; active?: boolean;
}) {
  const visible = usePageVisibility();
  const enabled = active && visible;
  const [revision, setRevision] = useState(0);
  const observedGraph = useRef('');
  const [state, setState] = useState(() => ({ roomId, transport,
    mode: readRoomEntryMode(transport, roomId), checking: Boolean(roomId), error: '',
  }));
  useEffect(() => {
    if (!roomId || !enabled) return;
    if (roomKind === 'roleplay') {
      setState({ roomId, transport, mode: 'traditional', checking: false, error: '' });
      return;
    }
    const abort = new AbortController();
    setState(previous => ({ roomId, transport,
      mode: previous.roomId === roomId && previous.transport === transport ? previous.mode : readRoomEntryMode(transport, roomId),
      checking: true, error: '',
    }));
    void (async () => {
      // Direct links may open before the optional Room catalog arrives.
      const room = roomKind ? null : jevRecord(jevRecord(await transport.request({ pathId: 'agent.room.get', params: { roomId }, signal: abort.signal })).room);
      if (abort.signal.aborted) return;
      if (room && room.id !== roomId) throw new Error('工作记录没有与当前 Room 匹配。');
      if ((roomKind || room?.roomKind) === 'roleplay') return 'traditional' as const;
      const value = await transport.request({ pathId: 'agent.jev.get', params: { roomId }, signal: abort.signal });
      const graphs = parseJevList(value, roomId);
      return graphs.length ? 'jev' as const : readRoomEntryMode(transport, roomId) ?? 'traditional';
    })().then(mode => {
      if (abort.signal.aborted || !mode) return;
      rememberRoomEntryMode(transport, roomId, mode);
      setState({ roomId, transport, mode, checking: false, error: '' });
    }).catch(error => {
      if (abort.signal.aborted) return;
      observedGraph.current = '';
      setState(previous => previous.roomId === roomId && previous.transport === transport ? {
        ...previous, checking: false, error: publicAgentErrorText(error, '工作记录暂未同步，请重试。'),
      } : previous);
    });
    return () => abort.abort();
  }, [roomId, roomKind, transport, enabled, revision]);
  const bound = state.roomId === roomId && state.transport === transport;
  return {
    mode: roomKind === 'roleplay' ? 'traditional' as const : bound ? state.mode : readRoomEntryMode(transport, roomId),
    checking: bound ? state.checking : Boolean(roomId),
    error: bound ? state.error : '',
    onEvents: (events: readonly unknown[]) => {
      if (roomKind === 'roleplay' || !roomId || bound && state.mode === 'jev') return;
      for (const value of events) {
        const event = jevRecord(value); const payload = jevRecord(event.payload);
        if (event.roomId !== roomId || event.eventType !== 'participant_status'
          || payload.status !== 'jev_updated' || typeof payload.graphId !== 'string' || !payload.graphId) continue;
        const identity = `${roomId}:${payload.graphId}`;
        if (observedGraph.current === identity) return;
        observedGraph.current = identity;
        // Reuse the existing Room stream; a new graph needs one fresh owner
        // read, not another subscription or a workspace remount.
        setRevision(value => value + 1);
        return;
      }
    },
    refresh: () => setRevision(value => value + 1),
  };
}
