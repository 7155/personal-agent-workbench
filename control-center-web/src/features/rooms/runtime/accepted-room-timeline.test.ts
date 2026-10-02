import { describe, expect, it } from 'vitest';

import {
  appendOptimisticRoomMessage,
  createRoomProjection,
  reduceRoomEvent,
  type RoomProjectionState,
} from '@/contracts/room-reducer';
import { parseRoomEvent } from '@/contracts/validators';

import { acceptedRoomTimelineEvents, mergeAcceptedRoomTimeline } from './accepted-room-timeline';

describe('mergeAcceptedRoomTimeline', () => {
  const retainedProjection = () => {
    let state: RoomProjectionState = { ...createRoomProjection('room-1'), lastSequence: 10,
      lastEventId: 'room-1:10', resumeToken: 'room-1:10' };
    state = appendOptimisticRoomMessage(state, { clientMessageId: 'client-1', text: 'original', nowMs: 100 });
    return appendOptimisticRoomMessage(state, { clientMessageId: 'client-2', text: 'unrelated', nowMs: 101 });
  };
  const oldUserAck = () => event(1, 'user_message', { clientMessageId: 'client-1', text: 'original' });

  it('reconciles an exact old user acknowledgement after canonical history is trimmed without replaying it', () => {
    const initial = retainedProjection();
    const merged = mergeAcceptedRoomTimeline(initial, acceptedRoomTimelineEvents({ timelineEvents: [oldUserAck()] }));
    expect(merged.optimisticByClientMessageId['client-1']).toBeUndefined();
    expect(merged.optimisticByClientMessageId['client-2']).toBe('local-room:client-2');
    expect(merged.messageOrder).toEqual(['local-room:client-2']);
    expect(merged.turnOrder).toEqual(['local-room-turn:client-2']);
    expect(merged.messagesById['room-1:1:user']).toBeUndefined();
    expect(merged.lastSequence).toBe(10);
    expect(merged.lastEventId).toBe('room-1:10');
    expect(merged.resumeToken).toBe('room-1:10');
    expect(merged.needsSnapshot).toBe(false);
    expect(initial.messageOrder).toHaveLength(2);
  });

  it.each(['foreign-room', 'other-client', 'missing-client', 'non-user', 'malformed'] as const)
    ('does not clear an optimistic request from an unbound old acknowledgement: %s', (kind) => {
      const initial = retainedProjection();
      const ack = oldUserAck();
      if (kind === 'foreign-room') ack.roomId = 'room-other';
      if (kind === 'other-client') ack.payload.clientMessageId = 'client-other';
      if (kind === 'missing-client') delete ack.payload.clientMessageId;
      if (kind === 'non-user') ack.eventType = 'participant_status';
      const events = acceptedRoomTimelineEvents({ timelineEvents: kind === 'malformed' ? [{ sequence: 1 }] : [ack] });
      expect(mergeAcceptedRoomTimeline(initial, events)).toBe(initial);
      expect(initial.optimisticByClientMessageId['client-1']).toBe('local-room:client-1');
      expect(initial.optimisticByClientMessageId['client-2']).toBe('local-room:client-2');
    });

  it.each(['foreign-room', 'gap', 'snapshot-pending'] as const)('rolls back old acknowledgement cleanup if its batch is rejected: %s', (kind) => {
    const initial = kind === 'snapshot-pending'
      ? { ...retainedProjection(), needsSnapshot: true }
      : retainedProjection();
    const later = event(kind === 'gap' ? 12 : kind === 'snapshot-pending' ? 11 : 2, 'route_decision', { rootId: 'root-1' });
    if (kind === 'foreign-room') later.roomId = 'room-other';
    const merged = mergeAcceptedRoomTimeline(initial, acceptedRoomTimelineEvents({ timelineEvents: [oldUserAck(), later] }));
    expect(merged).toBe(initial);
    expect(merged.messageOrder).toHaveLength(2);
    expect(merged.lastSequence).toBe(10);
    expect(merged.needsSnapshot).toBe(kind === 'snapshot-pending');
  });

  it.each(['roomId', 'clientMessageId'] as const)('does not clear a different optimistic message through a stale mapping: %s', (field) => {
    const initial = retainedProjection();
    initial.messagesById['local-room:client-1'] = {
      ...initial.messagesById['local-room:client-1']!,
      [field]: field === 'roomId' ? 'room-other' : 'client-other',
    };
    const merged = mergeAcceptedRoomTimeline(initial, acceptedRoomTimelineEvents({ timelineEvents: [oldUserAck()] }));
    expect(merged).toBe(initial);
    expect(merged.messageOrder).toHaveLength(2);
  });

  it('keeps a request unknown when an accepted response contains no trustworthy user timeline receipt', () => {
    const initial = retainedProjection();
    expect(mergeAcceptedRoomTimeline(initial, acceptedRoomTimelineEvents({ ok: true }))).toBe(initial);
    expect(initial.optimisticByClientMessageId['client-1']).toBe('local-room:client-1');
  });

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
