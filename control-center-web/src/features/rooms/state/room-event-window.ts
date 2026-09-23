import type { RoomProjectionState } from '@/contracts/room-reducer';
import type { UiRoomEvent } from '@/contracts/ui-events';

/** An ordinary delayed snapshot cannot rewind live state. Only an explicit
 * server recovery control can authorize a lower cursor, and the replacement
 * must at least reach the control's durable high-water mark. */
export function canApplyRoomSnapshot(state: RoomProjectionState, sequence: number): boolean {
  if (!Number.isSafeInteger(sequence) || sequence < 0) return false;
  if (state.needsSnapshot && state.recoveryCursor !== undefined
    && sequence < state.recoveryCursor) return false;
  if (sequence >= state.lastSequence) return true;
  return state.needsSnapshot
    && state.recoveryCursor !== undefined
    && state.recoveryCursor < state.lastSequence
    && sequence >= state.recoveryCursor;
}

/** Cache only the contiguous domain prefix actually accepted by the reducer.
 * Rejected tails and recovery controls must never become a later snapshot. */
export function acceptedRoomEvents(
  before: RoomProjectionState, after: RoomProjectionState, events: readonly UiRoomEvent[],
): UiRoomEvent[] {
  return events.filter((event) => event.roomId === before.roomId
    && event.eventType !== 'snapshot_required'
    && event.sequence > before.lastSequence && event.sequence <= after.lastSequence);
}

/** Merge a validated snapshot with its buffered live tail. Leave holes visible
 * for the replay validator; do not synthesize or relabel missing events. */
export function appendRoomEventWindow(
  roomId: string, current: readonly UiRoomEvent[], incoming: readonly UiRoomEvent[],
): UiRoomEvent[] {
  const cursor = current.at(-1)?.sequence ?? 0;
  const additions = new Map<number, UiRoomEvent>();
  for (const event of incoming) {
    if (event.roomId !== roomId || event.eventType === 'snapshot_required' || event.sequence <= cursor) continue;
    const previous = additions.get(event.sequence);
    if (previous && previous.eventId !== event.eventId) {
      throw new TypeError('Conflicting Room event identities at one sequence');
    }
    if (!previous) additions.set(event.sequence, event);
  }
  return [...current, ...[...additions.values()].sort((a, b) => a.sequence - b.sequence)];
}
