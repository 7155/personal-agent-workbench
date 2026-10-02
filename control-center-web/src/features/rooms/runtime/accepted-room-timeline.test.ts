import { describe, expect, it } from 'vitest';

import {
  appendOptimisticRoomMessage,
  createRoomProjection,
  reduceRoomEvent,
} from '@/contracts/room-reducer';
import { parseRoomEvent } from '@/contracts/validators';

import { acceptedRoomTimelineEvents, mergeAcceptedRoomTimeline } from './accepted-room-timeline';

describe('mergeAcceptedRoomTimeline', () => {
  it('shows the canonical acknowledgement immediately and ignores the same SSE replay', () => {
    const optimistic = appendOptimisticRoomMessage(createRoomProjection('room-1'), {
      clientMessageId: 'client-1',
      text: '请检查当前实现',
      attachments: [{
        mediaId: 'media_room_attachment01',
        roomId: 'room-1',
        fileName: 'diagram.png',
        mimeType: 'image/png',
        byteSize: 128,
        sha256: 'a'.repeat(64),
      }],
      nowMs: 1,
    });
    const timelineEvents = [
      event(1, 'user_message', {
        messageId: 'post-user-1',
        postId: 'post-user-1',
        clientMessageId: 'client-1',
        rootId: 'root-1',
        text: '请检查当前实现',
        attachmentReceipts: [{
          schemaVersion: 'rag-ime.agent-media.v1',
          mediaId: 'media_room_attachment01',
          ownerType: 'room',
          ownerId: 'room-1',
          roomId: 'room-1',
          fileName: 'diagram.png',
          mimeType: 'image/png',
          byteSize: 128,
          sha256: 'a'.repeat(64),
          origin: 'user_attachment',
          createdAtMs: 1,
        }],
      }),
      event(2, 'route_decision', {
        rootId: 'root-1',
        taskId: 'task-1',
        dispatchId: 'dispatch-1',
        targetParticipantId: 'participant-1',
        status: 'queued',
        summary: '澄·今 已接手',
      }),
    ];

    const accepted = mergeAcceptedRoomTimeline(optimistic, acceptedRoomTimelineEvents({ timelineEvents }));
    expect(accepted.lastSequence).toBe(2);
    expect(accepted.messageOrder).toEqual(['post-user-1']);
    expect(accepted.messagesById['post-user-1'].clientMessageId).toBe('client-1');
    expect(accepted.messagesById['post-user-1'].message?.attachments).toEqual([
      'media_room_attachment01',
    ]);
    expect(accepted.messagesById['post-user-1'].message?.blocks.filter(
      (block) => block.type === 'image',
    )).toHaveLength(1);
    expect(accepted.activityOrder).toHaveLength(1);
    expect(accepted.activitiesById[accepted.activityOrder[0]].summary).toBe('澄·今 已接手');

    const sameHttpReplay = mergeAcceptedRoomTimeline(accepted, acceptedRoomTimelineEvents({ timelineEvents }));
    expect(sameHttpReplay).toEqual(accepted);
    const sameSseReplay = reduceRoomEvent(
      accepted,
      parseRoomEvent(timelineEvents[1]),
    );
    expect(sameSseReplay.disposition).toBe('ignored-duplicate');
    expect(sameSseReplay.state).toBe(accepted);
  });

  it('keeps malformed acknowledgement items out of the validated event batch', () => {
    const valid = event(1, 'participant_status', { status: 'working' });
    expect(acceptedRoomTimelineEvents({ timelineEvents: [{ sequence: 1 }, valid] })).toEqual([parseRoomEvent(valid)]);
  });

  it('does not partially apply an acknowledgement that would create a sequence gap', () => {
    const initial = reduceRoomEvent(
      createRoomProjection('room-1'),
      parseRoomEvent(event(1, 'participant_status', { status: 'room_created' })),
    ).state;
    const merged = mergeAcceptedRoomTimeline(initial, acceptedRoomTimelineEvents({
      timelineEvents: [event(3, 'route_decision', {
        rootId: 'root-1',
        dispatchId: 'dispatch-1',
        targetParticipantId: 'participant-1',
      })],
    }));

    expect(merged).toBe(initial);
    expect(merged.lastSequence).toBe(1);
    expect(merged.needsSnapshot).toBe(false);
  });
});

function event(sequence: number, eventType: string, payload: Record<string, unknown>) {
  return {
    schemaVersion: 'rag-ime.agent-room-event.v1',
    eventId: `room-1:${sequence}`,
    roomId: 'room-1',
    sequence,
    turnId: 'root-1',
    eventType,
    participantId: eventType === 'user_message' ? null : 'participant-1',
    sourceSessionId: eventType === 'user_message' ? '' : 'session-1',
    topicId: 'topic-1',
    createdAtMs: sequence * 10,
    payload,
    resumeToken: `room-1:${sequence}`,
  };
}
