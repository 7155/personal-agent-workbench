import type { ControlTransport } from '@/platform/transport';
import { roomSendJournal, type RoomSendAttempt } from '../runtime/room-send-journal';
import { useRoomLiveStore } from '../state/live-store';

export type RoomSendResult =
  | { status: 'accepted'; response: Record<string, unknown> }
  | { status: 'uncertain' | 'rejected'; error: unknown };

export interface RoomSendDelivery {
  /** The admitted copy, including the original bindings of an explicit retry. */
  attempt: RoomSendAttempt;
  settled: Promise<RoomSendResult>;
}

/** Own the ordinary Room command's admission and receipt transaction, across
 * both Room surfaces. The journal is the only in-flight lock and retry record;
 * the live store is the only optimistic/accepted projection owner. Neither is
 * execution authority: a successful POST is not a completed Pi turn.
 *
 * Admission is synchronous so a caller can update its composer only when it
 * owns this delivery. UI drafts, attachments, scrolling and Jev stay outside
 * the transaction; unmounting a view does not cancel or replay an admitted send.
 */
export function startRoomSend(
  transport: ControlTransport,
  roomId: string,
  proposed: RoomSendAttempt,
): RoomSendDelivery | undefined {
  if (!roomId || proposed.request.params?.roomId !== roomId
    || !['agent.room.message', 'agent.room.participant.steer'].includes(proposed.request.pathId)) return;
  const journal = roomSendJournal(transport, roomId);
  const pending = journal.getSnapshot();
  if (pending && (pending.status === 'sending' || pending.clientMessageId !== proposed.clientMessageId)) return;
  // A retry may not rebind the same identity to a new draft, question or Root.
  const attempt = structuredClone(pending ?? proposed);
  if (!journal.start(attempt)) return;

  async function deliver(): Promise<RoomSendResult> {
    const { clientMessageId, request, attachments } = attempt;
    const body = request.body as Record<string, unknown>;
    const steering = request.pathId === 'agent.room.participant.steer';
    let responseReceived = false;
    try {
      if (!steering) useRoomLiveStore.getState().appendOptimistic(roomId, {
        clientMessageId,
        text: String(body.message),
        attachments,
        nowMs: Date.now(),
        ...(body.answerToPostId ? { answerToPostId: String(body.answerToPostId) } : {}),
        ...(body.retryOfRootId ? { retryOfRootId: String(body.retryOfRootId) } : {}),
      });
      // Let the admitted surface register its presentation (notably a steer
      // receipt) before a transport can synchronously publish the matching SSE.
      await Promise.resolve();
      // Transport code must not mutate the journal or composer recovery copy.
      let response = await transport.request<Record<string, unknown>>(structuredClone(request));
      responseReceived = true;
      const gate = response.startConfirmation;
      if (typeof gate === 'object' && gate !== null && 'status' in gate && gate.status === 'pending'
        && 'gateId' in gate && typeof gate.gateId === 'string') {
        // Current Hosts authorize the dispatch itself. Only an older durable
        // receipt can require this obsolete one-time gate; never add a second
        // user-facing approval step when explicitly recovering that command.
        response = await transport.request<Record<string, unknown>>({
          pathId: 'agent.room.startGate.confirm',
          params: { roomId },
          body: { gateId: gate.gateId, decision: 'confirm' },
        });
      }
      if (response.ok !== true) throw new Error('Room request has no confirmed receipt');
      useRoomLiveStore.getState().acceptMessage(roomId, response);
      journal.resolve(clientMessageId);
      return { status: 'accepted', response };
    } catch (error) {
      // A received ACK followed by a gate/projection failure cannot prove that
      // admission failed. Keep its identity for an explicit, idempotent replay.
      journal.fail(attempt, error, responseReceived);
      if (!steering) {
        try { useRoomLiveStore.getState().discardOptimistic(roomId, clientMessageId); }
        catch { /* The settled journal remains recoverable even if the local store needs a fresh snapshot. */ }
      }
      return { status: journal.getSnapshot() ? 'uncertain' : 'rejected', error };
    }
  }

  return { attempt: structuredClone(attempt), settled: deliver() };
}
