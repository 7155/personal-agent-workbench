import { useCallback, useEffect, useRef } from 'react';
import type { ControlTransport } from '@/platform/transport';
import {
  getSharedRoomLiveSession,
  type RoomLiveSessionCallbacks,
  type RoomLiveSessionLease,
} from './shared-room-live-session';

/** React owns the lease only. The shared controller owns snapshot/SSE ordering;
 * each mounted Room/partner surface still shares exactly one subscription. */
export function useRoomLiveSession({
  roomId, transport, active = true, ...callbacks
}: { roomId: string; transport: ControlTransport; active?: boolean } & RoomLiveSessionCallbacks): () => void {
  const context = useRef({ roomId, transport, active, callbacks });
  context.current = { roomId, transport, active, callbacks };
  const leaseRef = useRef<RoomLiveSessionLease | null>(null);
  useEffect(() => {
    if (!roomId || !active) {
      leaseRef.current = null;
      context.current.callbacks.onLoadingChange(false);
      return;
    }
    // Render may switch Room before passive-effect cleanup. Old notifications
    // cannot call the new Room's callbacks during that small interval.
    const notify = (fn: (target: RoomLiveSessionCallbacks) => void) => {
      const current = context.current;
      if (current.active && current.roomId === roomId && current.transport === transport) fn(current.callbacks);
    };
    const lease = getSharedRoomLiveSession(transport, roomId).attach({
      onLoadingChange: (value) => notify((target) => target.onLoadingChange(value)),
      onSnapshot: (id, value) => notify((target) => target.onSnapshot(id, value)),
      onMetadata: (id, value) => notify((target) => target.onMetadata(id, value)),
      onConnectionRestored: (id) => notify((target) => target.onConnectionRestored(id)),
      onConnectionError: (id, error, fallback) => notify((target) => target.onConnectionError(id, error, fallback)),
      onRecoveryState: (id, value) => notify((target) => target.onRecoveryState(id, value)),
      onEvents: (id, events) => notify((target) => target.onEvents(id, events)),
    });
    leaseRef.current = lease;
    return () => {
      if (leaseRef.current === lease) leaseRef.current = null;
      lease.release();
    };
  }, [roomId, transport, active]);
  return useCallback(() => leaseRef.current?.retry(), []);
}
