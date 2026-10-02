import {
  reconcileDuplicateRoomUserAcknowledgement,
  reduceRoomEvent,
  type RoomProjectionState,
} from '@/contracts/room-reducer';
import type { UiRoomEvent } from '@/contracts/ui-events';
import { parseRoomEvent } from '@/contracts/validators';

/** Parse only accepted timeline events returned by the normal Room transport. */
export function acceptedRoomTimelineEvents(response: unknown): UiRoomEvent[] {
  const value = record(response);
  const timelineEvents = Array.isArray(value.timelineEvents)
    ? value.timelineEvents
    : [];
  const accepted: UiRoomEvent[] = [];
  for (const item of timelineEvents) {
    try {
      accepted.push(parseRoomEvent(item));
    } catch {
      // A malformed acknowledgement item is neither projected nor authoritative.
    }
  }
  return accepted;
}

/** Merge the POST acknowledgement without racing the same SSE events. */
export function mergeAcceptedRoomTimeline(
  state: RoomProjectionState,
  events: readonly UiRoomEvent[],
): RoomProjectionState {
  let next = state;
  for (const event of events) {
    const reduced = reduceRoomEvent(next, event);
    if (reduced.disposition === 'snapshot-required'
      || reduced.disposition === 'ignored-foreign'
      || reduced.disposition === 'ignored-snapshot-pending') return state;
    if (reduced.disposition === 'applied') next = reduced.state;
    else if (reduced.disposition === 'ignored-duplicate') {
      next = reconcileDuplicateRoomUserAcknowledgement(next, event);
    }
  }
  return next;
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}
