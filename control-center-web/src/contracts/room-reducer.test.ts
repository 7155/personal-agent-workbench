import { describe, expect, it } from 'vitest';

import {
  appendOptimisticRoomMessage,
  applyRoomSnapshot,
  createRoomProjection,
  parseRoomConversationSnapshot,
  parseRoomEventPage,
  parseRoomEventSnapshot,
  roomActivityLaneIdentity,
  reduceRoomEvent,
  reduceRoomEvents,
  replayRoomConversationSnapshot,
  replayRoomEventSnapshot,
  selectRoomParticipantPublicProgress,
} from './room-reducer';
import { parseRoomEvent } from './validators';
import { roomEventFixture as roomEvent } from '@/test/fixtures/events';

describe('RoomEventReducer', () => {
  it('replays the 2,000-event message-first Room window without blocking the first paint', () => {
    const events = Array.from({ length: 1_000 }, (_value, index) => {
      const turnId = `room-turn-${index + 1}`;
      const messageSequence = index * 2 + 1;
      const terminalSequence = messageSequence + 1;
      const message = roomEvent(messageSequence, 'user_message', {
        messageId: `room-message-${index + 1}`,
        rootId: turnId,
        text: `真实 Room 消息 ${index + 1}`,
      });
      const terminal = roomEvent(terminalSequence, 'turn_completed', {
        rootId: turnId,
        status: 'completed',
      });
      return [
        {
          ...message,
          eventId: `room-1:${messageSequence}`,
          turnId,
          resumeToken: `room-1:${messageSequence}`,
        },
        {
          ...terminal,
          eventId: `room-1:${terminalSequence}`,
          turnId,
          participantId: null,
          sourceSessionId: '',
          resumeToken: `room-1:${terminalSequence}`,
        },
      ];
    }).flat();
    const snapshot = parseRoomConversationSnapshot({
      schemaVersion: 'rag-ime.agent-room-conversation-snapshot.v1',
      ok: true,
      room: {
        schemaVersion: 'rag-ime.agent-room.v1',
        id: 'room-1',
        title: '长 Room',
        status: 'active',
        executionMode: 'workspace_managed',
        routingPolicy: 'moderator',
        moderatorParticipantId: 'participant-1',
        permissionPolicy: {
          schemaVersion: 'rag-ime.room-permission-policy.v1',
          room: { executionMode: 'workspace_managed' },
          partner: { executionMode: 'inherit' },
          toolAgent: { executionMode: 'inherit' },
        },
        workspaceRoots: [],
        createdAtMs: 1,
        updatedAtMs: 2,
        lastEventSequence: 2_000,
        participants: [1, 2].map((ordinal) => ({
          schemaVersion: 'rag-ime.agent-participant.v1',
          id: `participant-${ordinal}`,
          roomId: 'room-1',
          sessionId: `session-room-${ordinal}`,
          roleId: ordinal === 1 ? 'companion-present-v1' : 'companion-firstlight-v1',
          roleVersion: '1',
          displayName: ordinal === 1 ? '澄' : '澄·初',
          collaborationRole: ordinal === 1 ? 'coordinator' : 'reviewer',
          status: 'active',
          ordinal: ordinal - 1,
          createdAtMs: 1,
          lastSpokeAtMs: null,
        })),
      },
      events: events.map((event) => Object.fromEntries(
        Object.entries(event).filter(([key]) => key !== 'streamKind'),
      )),
      firstEventSequence: 1,
      cursorSequence: 2_000,
      resumeToken: 'room-1:2000',
      deferredEventCount: 0,
      truncated: false,
    });

    const startedAt = performance.now();
    const projection = replayRoomConversationSnapshot(
      createRoomProjection('room-1'),
      snapshot,
    );
    const elapsedMs = performance.now() - startedAt;

    expect(projection.messageOrder).toHaveLength(1_000);
    expect(projection.turnOrder).toHaveLength(1_000);
    expect(projection.turnsById['room-turn-1000']?.status).toBe('completed');
    expect(elapsedMs).toBeLessThan(250);
  });

  it('projects participant deltas once and requests a snapshot on a gap', () => {
    const first = reduceRoomEvent(
      createRoomProjection('room-1'),
      roomEvent(1, 'participant_delta', { delta: '并' }),
    );
    const second = reduceRoomEvent(
      first.state,
      roomEvent(2, 'participant_delta', { delta: '行' }),
    );
    const duplicate = reduceRoomEvent(
      second.state,
      roomEvent(2, 'participant_delta', { delta: '行' }),
    );
    const gap = reduceRoomEvent(second.state, roomEvent(4, 'turn_completed', {}));

    expect(duplicate.disposition).toBe('ignored-duplicate');
    expect(second.state.messagesById['room-message-1'].text).toBe('并行');
    expect(gap.disposition).toBe('snapshot-required');
    expect(gap.state.needsSnapshot).toBe(true);
  });

  it('coalesces participant deltas while preserving the final Room cursor', () => {
    const events = Array.from({ length: 200 }, (_, index) => roomEvent(
      index + 1,
      'participant_delta',
      {
        delta: String.fromCharCode(97 + (index % 26)),
        rootId: 'room-turn-1',
        dispatchId: 'dispatch-1',
        contentIndex: 0,
      },
    ));
    const state = reduceRoomEvents(createRoomProjection('room-1'), events);

    expect(state.messagesById[
      'room-execution\u001froom-turn-1\u001fparticipant-1\u001fdispatch-1\u001froom-message-1'
    ].text).toBe(events.map((event) => String(event.payload.delta)).join(''));
    expect(state.lastSequence).toBe(200);
    expect(state.lastEventId).toBe('room-1:200');
    expect(state.resumeToken).toBe('room-1:200');
  });

  it('replaces provisional participant content without breaking append or final coalescing', () => {
    const deltas = [
      roomEvent(1, 'participant_delta', {
        rootId: 'room-turn-1',
        dispatchId: 'dispatch-1',
        messageId: 'provider-stream-1',
        blockId: 'room-final-1:text',
        contentIndex: 0,
        delta: '过时前缀',
      }),
      roomEvent(2, 'participant_delta', {
        rootId: 'room-turn-1',
        dispatchId: 'dispatch-1',
        messageId: 'provider-stream-1',
        blockId: 'room-final-1:text',
        contentIndex: 0,
        delta: '仍会追加',
      }),
      roomEvent(3, 'participant_delta', {
        rootId: 'room-turn-1',
        dispatchId: 'dispatch-1',
        messageId: 'provider-stream-1',
        blockId: 'room-final-1:text',
        contentIndex: 0,
        delta: '块替换',
        replaceBlock: true,
      }),
      roomEvent(4, 'participant_delta', {
        rootId: 'room-turn-1',
        dispatchId: 'dispatch-1',
        messageId: 'provider-stream-1',
        blockId: 'room-final-1:text',
        contentIndex: 0,
        delta: '后续追加',
      }),
      roomEvent(5, 'participant_delta', {
        rootId: 'room-turn-1',
        dispatchId: 'dispatch-1',
        messageId: 'provider-stream-1',
        blockId: 'room-final-1:text',
        contentIndex: 0,
        delta: '内容替换',
        replaceContent: true,
      }),
      roomEvent(6, 'participant_delta', {
        rootId: 'room-turn-1',
        dispatchId: 'dispatch-1',
        messageId: 'provider-stream-1',
        blockId: 'room-final-1:text',
        contentIndex: 0,
        delta: '最终追加',
      }),
    ];
    const executionId = [
      'room-execution',
      'room-turn-1',
      'participant-1',
      'dispatch-1',
      'provider-stream-1',
    ].join('\u001f');

    const appended = reduceRoomEvents(createRoomProjection('room-1'), deltas.slice(0, 2));
    expect(appended.messagesById[executionId].text).toBe('过时前缀仍会追加');

    const blockReplaced = reduceRoomEvents(createRoomProjection('room-1'), deltas.slice(0, 4));
    expect(blockReplaced.messagesById[executionId].text).toBe('块替换后续追加');

    const contentReplaced = reduceRoomEvents(createRoomProjection('room-1'), deltas);
    expect(contentReplaced.messagesById[executionId].text).toBe('内容替换最终追加');

    const completed = reduceRoomEvent(
      contentReplaced,
      parseRoomEvent(wireRoomEvent(7, 'participant_message', {
        rootId: 'room-turn-1',
        dispatchId: 'dispatch-1',
        message: roomServerMessage('room-final-1', '最终公开回复'),
      })),
    ).state;
    expect(completed.messageOrder).toEqual(['room-final-1']);
    expect(completed.turnsById['room-turn-1'].messageIds).toEqual(['room-final-1']);
    expect(completed.messagesById[executionId]).toBeUndefined();
    expect(completed.messagesById['room-final-1'].text).toBe('最终公开回复');
  });

  it('keeps completed Room turns referentially stable while another lane streams', () => {
    const user = reduceRoomEvent(
      createRoomProjection('room-1'),
      roomEvent(1, 'user_message', { messageId: 'stable-user', text: '旧任务' }),
    ).state;
    const completedEvent = roomEvent(2, 'turn_completed', { status: 'completed' });
    completedEvent.participantId = null;
    completedEvent.sourceSessionId = '';
    const completed = reduceRoomEvent(user, completedEvent).state;
    const stableTurn = completed.turnsById['room-turn-1'];
    const stableMessage = completed.messagesById['stable-user'];
    const nextTurnEvent = {
      ...roomEvent(3, 'participant_delta', {
        messageId: 'next-reply',
        delta: '新任务流式内容',
      }),
      turnId: 'room-turn-2',
    };
    const streamed = reduceRoomEvent(completed, nextTurnEvent).state;

    expect(streamed.turnsById['room-turn-1']).toBe(stableTurn);
    expect(streamed.messagesById['stable-user']).toBe(stableMessage);
    expect(streamed.turnsById['room-turn-2']).toBeDefined();
    expect(completed.turnsById['room-turn-2']).toBeUndefined();
  });

  it('keeps the user message and participant reply in one completed room turn', () => {
    const user = reduceRoomEvent(
      createRoomProjection('room-1'),
      roomEvent(1, 'user_message', {
        messageId: 'room-user-1',
        text: '@澄·远 请整理下一步',
      }),
    ).state;
    const reply = reduceRoomEvent(
      user,
      roomEvent(2, 'participant_delta', {
        messageId: 'room-assistant-1',
        delta: '已整理',
      }),
    ).state;
    const rootCompleted = roomEvent(3, 'turn_completed', { status: 'completed' });
    rootCompleted.participantId = null;
    rootCompleted.sourceSessionId = '';
    const completed = reduceRoomEvent(reply, rootCompleted).state;

    expect(completed.turnOrder).toEqual(['room-turn-1']);
    expect(completed.turnsById['room-turn-1']).toMatchObject({
      status: 'completed',
      messageIds: ['room-user-1', 'room-assistant-1'],
    });
    expect(completed.messagesById['room-assistant-1']).toMatchObject({
      role: 'assistant',
      status: 'completed',
      text: '已整理',
    });
  });

  it('replaces a provisional streaming reply when the final event has a real message id', () => {
    const streaming = reduceRoomEvent(
      createRoomProjection('room-1'),
      parseRoomEvent({
        ...wireRoomEvent(1, 'participant_delta', { delta: '先流式' }),
      }),
    ).state;
    const completed = reduceRoomEvent(
      streaming,
      parseRoomEvent({
        ...wireRoomEvent(2, 'participant_message', {
          message: roomServerMessage('room-final-1', '最终回复'),
        }),
      }),
    ).state;

    expect(completed.messageOrder).toEqual(['room-final-1']);
    expect(completed.turnsById['room-turn-1'].messageIds).toEqual(['room-final-1']);
    expect(completed.messagesById['room-final-1']).toMatchObject({
      status: 'completed',
      text: '最终回复',
    });
    expect(
      completed.messagesById['room-turn-1:participant-1:assistant'],
    ).toBeUndefined();
  });

  it('coalesces live delta aliases and reconnect replay into one final assistant message', () => {
    const events = [
      wireRoomEvent(1, 'participant_delta', {
        rootId: 'room-turn-1',
        dispatchId: 'dispatch-1',
        messageId: 'provider-stream-1',
        blockId: 'room-final-1:text',
        delta: '流式草稿不应重复',
      }),
      wireRoomEvent(2, 'participant_message', {
        rootId: 'room-turn-1',
        dispatchId: 'dispatch-1',
        message: roomServerMessage('room-final-1', '最终公开回复'),
      }),
      wireRoomEvent(3, 'participant_message', {
        rootId: 'room-turn-1',
        dispatchId: 'dispatch-1',
        message: roomServerMessage('room-final-1', '最终公开回复'),
      }),
    ];
    const live = reduceRoomEvents(
      createRoomProjection('room-1'),
      events.map((event) => parseRoomEvent(event)),
    );
    const replayed = replayRoomEventSnapshot(
      createRoomProjection('room-1'),
      parseRoomEventSnapshot(roomSnapshotFixture(events)),
    );

    for (const projection of [live, replayed]) {
      expect(projection.messageOrder).toEqual(['room-final-1']);
      expect(projection.turnsById['room-turn-1'].messageIds).toEqual(['room-final-1']);
      expect(Object.values(projection.messagesById)).toHaveLength(1);
      expect(projection.messagesById['room-final-1']).toMatchObject({
        projectionKind: 'post',
        status: 'completed',
        text: '最终公开回复',
      });
    }
  });

  it('keeps different source events when their assistant text happens to match', () => {
    const events = [
      wireRoomEvent(1, 'participant_message', {
        rootId: 'room-turn-1',
        dispatchId: 'dispatch-1',
        message: roomServerMessage('provider-reply-1', '相同的最终回复'),
      }),
      wireRoomEvent(2, 'room_post', {
        post: roomPost('post-1', '相同的最终回复', 'dispatch-1'),
      }),
      wireRoomEvent(3, 'participant_message', {
        rootId: 'room-turn-1',
        dispatchId: 'dispatch-1',
        message: roomServerMessage('provider-reply-late', '相同的最终回复'),
      }),
    ];
    const projection = reduceRoomEvents(
      createRoomProjection('room-1'),
      events.map((event) => parseRoomEvent(event)),
    );

    expect(projection.messageOrder).toEqual([
      'provider-reply-1',
      'post-1',
      'provider-reply-late',
    ]);
    expect(projection.turnsById['room-turn-1'].messageIds).toEqual([
      'provider-reply-1',
      'post-1',
      'provider-reply-late',
    ]);
    expect(Object.values(projection.messagesById)).toHaveLength(3);
    expect(projection.messagesById['post-1']).toMatchObject({
      postKind: 'result',
      text: '相同的最终回复',
    });
  });

  it('coalesces runtime and RoomPost aliases only when they share publication identity', () => {
    const events = [
      wireRoomEvent(1, 'participant_message', {
        rootId: 'room-turn-1',
        sourceEventId: 'publication:reply-1',
        message: roomServerMessage('provider-reply-1', '流式完成稿'),
      }),
      wireRoomEvent(2, 'room_post', {
        post: {
          ...roomPost('post-1', '正式公开回复', ''),
          chronology: {
            schemaVersion: 'wisdom-weasel.room-post-chronology.v1',
            roomEventId: 'publication:reply-1',
            roomEventSequence: 1,
            createdAtMs: 10,
            afterPostId: null,
            orderKey: 'room-event:00000000000000000001',
          },
        },
      }),
      wireRoomEvent(3, 'participant_message', {
        rootId: 'room-turn-1',
        sourceEventId: 'publication:reply-1',
        message: roomServerMessage('provider-reply-late', '重复回放的运行稿'),
      }),
    ];
    const projection = reduceRoomEvents(
      createRoomProjection('room-1'),
      events.map((event) => parseRoomEvent(event)),
    );

    expect(projection.messageOrder).toEqual(['post-1']);
    expect(projection.turnsById['room-turn-1'].messageIds).toEqual(['post-1']);
    expect(Object.values(projection.messagesById)).toHaveLength(1);
    expect(projection.messagesById['post-1']).toMatchObject({
      sourceEventId: 'publication:reply-1',
      sequence: 1,
      createdAtMs: 10,
      postKind: 'result',
      text: '正式公开回复',
      chronology: {
        roomEventId: 'publication:reply-1',
        roomEventSequence: 1,
        orderKey: 'room-event:00000000000000000001',
      },
    });
  });

  it('replaces only the matching execution lane when an authorized RoomPost is published', () => {
    const streaming = reduceRoomEvent(
      createRoomProjection('room-1'),
      parseRoomEvent(wireRoomEvent(1, 'participant_delta', {
        rootId: 'room-turn-1',
        dispatchId: 'dispatch-1',
        messageId: 'draft-1',
        delta: '尚未发布的草稿',
      })),
    ).state;
    const published = reduceRoomEvent(
      streaming,
      parseRoomEvent(wireRoomEvent(2, 'room_post', {
        post: roomPost('post-1', '正式交付', 'dispatch-1'),
      })),
    ).state;

    expect(published.messageOrder).toEqual(['post-1']);
    expect(Object.values(published.messagesById)).toHaveLength(1);
    expect(published.messagesById['post-1']).toMatchObject({
      projectionKind: 'post',
      postKind: 'result',
      dispatchId: 'dispatch-1',
      text: '正式交付',
      status: 'completed',
    });
    expect(published.turnsById['room-turn-1'].messageIds).toEqual(['post-1']);
    expect(published.turnsById['room-turn-1']).toMatchObject({
      status: 'running',
      terminalDispatchIds: ['dispatch-1'],
      terminalParticipantIds: ['participant-1'],
    });
  });

  it('settles published lanes without unlocking the Root before its terminal event', () => {
    const events = [
      roomEvent(1, 'route_decision', {
        rootId: 'room-turn-1',
        dispatchId: 'dispatch-1',
        targetParticipantId: 'participant-1',
      }),
      roomEvent(2, 'route_decision', {
        rootId: 'room-turn-1',
        dispatchId: 'dispatch-2',
        targetParticipantId: 'participant-1',
      }),
      roomEvent(3, 'room_post', {
        post: roomPost('post-1', '第一份交付', 'dispatch-1'),
      }),
      roomEvent(4, 'room_post', {
        post: roomPost('post-2', '第二份交付', 'dispatch-2'),
      }),
    ];
    let state = createRoomProjection('room-1');
    for (const event of events) state = reduceRoomEvent(state, event).state;

    expect(state.turnsById['room-turn-1']).toMatchObject({
      status: 'running',
      dispatchIds: ['dispatch-1', 'dispatch-2'],
      terminalDispatchIds: ['dispatch-1', 'dispatch-2'],
      terminalParticipantIds: ['participant-1'],
    });

    state = reduceRoomEvent(state, parseRoomEvent({
      ...wireRoomEvent(5, 'turn_completed', { status: 'completed' }),
      participantId: null,
      sourceSessionId: '',
    })).state;

    expect(state.turnsById['room-turn-1'].status).toBe('completed');
  });
  it('keeps a formal Root running through coordinator wait/progress and partner evidence', () => {
    const coordinatorRoute = wireRoomEvent(1, 'route_decision', {
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-coordinator',
      targetParticipantId: 'participant-1',
    });
    const partnerRoute = wireRoomEvent(2, 'route_decision', {
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-partner',
      targetParticipantId: 'participant-2',
    });
    partnerRoute.participantId = 'participant-2';
    partnerRoute.sourceSessionId = 'session-room-2';
    const coordinatorWait = wireRoomEvent(3, 'participant_activity', {
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-coordinator',
      sourceEventId: 'session-room-1:wait',
      sourceEventType: 'user_input_required',
      requestKind: 'user_input_required',
      method: 'select',
      options: ['继续', '停止'],
      status: 'waiting',
      summary: '等待伙伴结果',
    });
    const coordinatorProgress = wireRoomEvent(4, 'participant_activity', {
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-coordinator',
      sourceEventId: 'session-room-1:progress',
      sourceEventType: 'current_progress',
      state: 'running',
      summary: '正在汇总伙伴工作结果',
    });
    const partnerResult = wireRoomEvent(5, 'room_post', {
      post: formalRoomPost(
        'partner-work-result',
        '伙伴交付证据',
        'dispatch-partner',
        'work_result',
      ),
    });
    partnerResult.participantId = 'participant-2';
    partnerResult.sourceSessionId = 'session-room-2';
    const partnerTerminal = wireRoomEvent(6, 'turn_completed', {
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-partner',
      status: 'completed',
    });
    partnerTerminal.participantId = 'participant-2';
    partnerTerminal.sourceSessionId = 'session-room-2';
    const coordinatorTerminal = wireRoomEvent(7, 'turn_completed', {
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-coordinator',
      status: 'completed',
    });

    const state = reduceRoomEvents(createRoomProjection('room-1'), [
      coordinatorRoute,
      partnerRoute,
      coordinatorWait,
      coordinatorProgress,
      partnerResult,
      partnerTerminal,
      coordinatorTerminal,
    ].map((event) => parseRoomEvent(event)));

    expect(state.turnsById['room-turn-1']).toMatchObject({
      status: 'running',
      terminalDispatchIds: ['dispatch-partner', 'dispatch-coordinator'],
    });
    expect(state.messageOrder).toEqual(['partner-work-result']);
    expect(state.messagesById['partner-work-result']).toMatchObject({
      postKind: 'work_result',
      text: '伙伴交付证据',
    });
    expect(state.messagesById['coordinator-final']).toBeUndefined();
  });

  it('replays one moderator final whether it arrives before or after terminal lanes', () => {
    const orders = [
      [
        'final',
        'root-terminal',
        'partner-terminal',
        'coordinator-terminal',
      ],
      [
        'partner-terminal',
        'coordinator-terminal',
        'root-terminal',
        'final',
      ],
    ] as const;

    for (const order of orders) {
      const events = formalTerminalOrdering(order);
      const live = reduceRoomEvents(
        createRoomProjection('room-1'),
        events.map((event) => parseRoomEvent(event)),
      );
      const snapshot = parseRoomEventSnapshot(roomSnapshotFixture(events));
      const replayed = replayRoomEventSnapshot(createRoomProjection('room-1'), snapshot);

      expect(replayed.moderatorParticipantId).toBe('participant-1');
      for (const state of [live, replayed]) {
        expect(state.turnsById['room-turn-1']?.status).toBe('completed');
        expect(new Set(state.turnsById['room-turn-1']?.terminalDispatchIds)).toEqual(
          new Set(['dispatch-partner', 'dispatch-coordinator']),
        );
        expect(state.messageOrder).toEqual([
          'partner-work-result',
          'coordinator-final',
        ]);
        expect(Object.values(state.messagesById)).toHaveLength(2);
        expect(state.diagnostics.filter(
          (item) => item.eventType === 'room_event_after_root_terminal',
        )).toHaveLength(0);
      }
    }
  });

  it('does not let WorkDocument activity or a final-post runtime id create phantom dispatches', () => {
    const partnerRoute = wireRoomEvent(1, 'route_decision', {
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-partner',
      targetParticipantId: 'participant-2',
    });
    partnerRoute.participantId = 'participant-2';
    partnerRoute.sourceSessionId = 'session-room-2';
    const partnerResult = wireRoomEvent(2, 'room_post', {
      post: formalRoomPost(
        'partner-work-result',
        '伙伴交付证据',
        'dispatch-partner',
        'work_result',
        'participant-2',
      ),
    });
    partnerResult.participantId = 'participant-2';
    partnerResult.sourceSessionId = 'session-room-2';
    const partnerTerminal = wireRoomEvent(3, 'turn_completed', {
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-partner',
      status: 'completed',
    });
    partnerTerminal.participantId = 'participant-2';
    partnerTerminal.sourceSessionId = 'session-room-2';
    const documentSync = wireRoomEvent(4, 'participant_activity', {
      rootId: 'room-turn-1',
      attemptId: 'room-turn-1',
      dispatchId: 'room-turn-1',
      activityKind: 'work',
      phase: 'submitted',
      summary: 'WorkDocument 已同步',
    });
    const moderatorFinal = wireRoomEvent(5, 'room_post', {
      post: formalRoomPost(
        'coordinator-final',
        '主持者最终报告',
        'room-wake-dispatch:dispatch-partner:1',
        'result',
      ),
    });
    const events = [
      partnerRoute,
      partnerResult,
      partnerTerminal,
      documentSync,
      moderatorFinal,
    ];

    const replayed = replayRoomEventSnapshot(
      createRoomProjection('room-1'),
      parseRoomEventSnapshot(roomSnapshotFixture(events)),
    );

    expect(replayed.turnsById['room-turn-1']).toMatchObject({
      status: 'completed',
      dispatchIds: ['dispatch-partner'],
      terminalDispatchIds: ['dispatch-partner'],
    });
    expect(new Set(replayed.turnsById['room-turn-1']?.terminalParticipantIds)).toEqual(
      new Set(['participant-1', 'participant-2']),
    );
  });

  it('requires the persisted moderator for the formal final while retaining partner results', () => {
    const terminalEvents = formalTerminalOrdering([
      'partner-terminal',
      'coordinator-terminal',
      'root-terminal',
    ]);
    const terminalSnapshot = parseRoomEventSnapshot(roomSnapshotFixture(terminalEvents));
    let state = replayRoomEventSnapshot(createRoomProjection('room-1'), terminalSnapshot);

    const partnerResult = wireRoomEvent(7, 'room_post', {
      post: formalRoomPost(
        'partner-result-post',
        '伙伴误发的最终样式消息',
        'dispatch-partner',
        'result',
      ),
    });
    partnerResult.participantId = 'participant-2';
    partnerResult.sourceSessionId = 'session-room-2';
    state = reduceRoomEvent(state, parseRoomEvent(partnerResult)).state;

    expect(state.turnsById['room-turn-1'].status).toBe('running');
    expect(state.messageOrder).toEqual([
      'partner-work-result',
      'partner-result-post',
    ]);

    const moderatorResult = wireRoomEvent(8, 'room_post', {
      post: formalRoomPost(
        'moderator-result-post',
        '主持者最终报告',
        'dispatch-coordinator',
        'result',
      ),
    });
    state = reduceRoomEvent(state, parseRoomEvent(moderatorResult)).state;

    expect(state.turnsById['room-turn-1'].status).toBe('completed');
    expect(state.messageOrder).toEqual([
      'partner-work-result',
      'partner-result-post',
      'moderator-result-post',
    ]);
    expect(state.messageOrder.filter(
      (id) => state.messagesById[id]?.postKind === 'result'
        && state.messagesById[id]?.participantId === 'participant-1',
    )).toHaveLength(1);
  });

  it('keeps Light completion and immediate formal failure/abort behavior', () => {
    const lightRoute = wireRoomEvent(1, 'route_decision', {
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-light',
      targetParticipantId: 'participant-1',
    });
    const lightTerminal = wireRoomEvent(2, 'turn_completed', {
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-light',
      status: 'completed',
    });
    const light = reduceRoomEvents(createRoomProjection('room-1'), [
      lightRoute,
      lightTerminal,
    ].map((event) => parseRoomEvent(event)));
    expect(light.turnsById['room-turn-1'].status).toBe('completed');

    const formalEvidence = formalTerminalOrdering([
      'partner-terminal',
    ]).slice(0, 4);
    const failedRoot = wireRoomEvent(5, 'turn_failed', {
      rootId: 'room-turn-1',
      error: '主持者失败',
    });
    failedRoot.participantId = null;
    failedRoot.sourceSessionId = '';
    const failed = reduceRoomEvents(createRoomProjection('room-1'), [
      ...formalEvidence,
      failedRoot,
    ].map((event) => parseRoomEvent(event)));
    expect(failed.turnsById['room-turn-1'].status).toBe('failed');

    const abortedRoot = wireRoomEvent(5, 'turn_completed', {
      rootId: 'room-turn-1',
      status: 'aborted',
      aborted: true,
    });
    abortedRoot.participantId = null;
    abortedRoot.sourceSessionId = '';
    const aborted = reduceRoomEvents(createRoomProjection('room-1'), [
      ...formalEvidence,
      abortedRoot,
    ].map((event) => parseRoomEvent(event)));
    expect(aborted.turnsById['room-turn-1'].status).toBe('aborted');
  });


  it('keeps two concurrent dispatches from the same Agent in separate execution lanes', () => {
    const routeOne = roomEvent(1, 'route_decision', {
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-1',
      targetParticipantId: 'participant-1',
    });
    const routeTwo = roomEvent(2, 'route_decision', {
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-2',
      targetParticipantId: 'participant-1',
    });
    const firstDelta = roomEvent(3, 'participant_delta', {
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-1',
      messageId: 'shared-provider-message',
      delta: '第一条任务',
    });
    const secondDelta = roomEvent(4, 'participant_delta', {
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-2',
      messageId: 'shared-provider-message',
      delta: '第二条任务',
    });
    const firstDone = roomEvent(5, 'turn_completed', {
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-1',
    });
    const secondDone = roomEvent(6, 'turn_completed', {
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-2',
    });

    let state = createRoomProjection('room-1');
    for (const event of [routeOne, routeTwo, firstDelta, secondDelta]) {
      state = reduceRoomEvent(state, event).state;
    }

    const messages = state.messageOrder.map((id) => state.messagesById[id]);
    expect(messages).toHaveLength(2);
    expect(messages.map((message) => message.dispatchId)).toEqual([
      'dispatch-1',
      'dispatch-2',
    ]);
    expect(messages.map((message) => message.text)).toEqual([
      '第一条任务',
      '第二条任务',
    ]);

    state = reduceRoomEvent(state, firstDone).state;
    expect(state.turnsById['room-turn-1'].status).toBe('running');
    expect(state.turnsById['room-turn-1'].terminalDispatchIds).toEqual(['dispatch-1']);
    state = reduceRoomEvent(state, secondDone).state;
    expect(state.turnsById['room-turn-1']).toMatchObject({
      status: 'completed',
      dispatchIds: ['dispatch-1', 'dispatch-2'],
      terminalDispatchIds: ['dispatch-1', 'dispatch-2'],
    });
    state = reduceRoomEvent(state, parseRoomEvent({
      ...wireRoomEvent(7, 'turn_completed', { status: 'completed' }),
      participantId: null,
      sourceSessionId: '',
    })).state;
    expect(state.turnsById['room-turn-1'].status).toBe('completed');
  });

  it('settles a Pi-composed Room after the child and facilitator Sessions finish', () => {
    const rootId = 'room-turn-1';
    const facilitatorDispatchId = 'room-dispatch:facilitator';
    const childDispatchId = 'room-child:partner';
    const facilitatorId = 'participant-1';
    const partnerId = 'participant-2';
    const partnerRoute = roomEvent(3, 'route_decision', {
      rootId,
      dispatchId: childDispatchId,
      parentDispatchId: facilitatorDispatchId,
      targetParticipantId: partnerId,
      child: true,
    });
    partnerRoute.participantId = partnerId;
    partnerRoute.sourceSessionId = 'session-room-2';
    const childCompleted = parseRoomEvent({
      ...wireRoomEvent(4, 'participant_activity', {
        sourceEventId: 'session-room-2:terminal',
        sourceEventType: 'turn_completed',
        data: {
          rootId,
          dispatchId: childDispatchId,
          activityKind: 'child',
          phase: 'completed',
          status: 'completed',
        },
      }),
      participantId: partnerId,
      sourceSessionId: 'session-room-2',
    });
    const facilitatorCompleted = parseRoomEvent({
      ...wireRoomEvent(5, 'turn_completed', {
        sourceEventId: 'session-room-1:terminal',
        sourceEventType: 'turn_completed',
        data: {
          rootId,
          dispatchId: facilitatorDispatchId,
        },
      }),
      participantId: facilitatorId,
      sourceSessionId: 'session-room-1',
    });

    const state = reduceRoomEvents(createRoomProjection('room-1'), [
      roomEvent(1, 'user_message', { rootId, text: '请协作完成' }),
      roomEvent(2, 'route_decision', {
        rootId,
        dispatchId: facilitatorDispatchId,
        targetParticipantId: facilitatorId,
      }),
      partnerRoute,
      childCompleted,
      facilitatorCompleted,
    ]);

    expect(state.turnsById[rootId]).toMatchObject({
      status: 'completed',
      terminalParticipantIds: [facilitatorId, partnerId],
      terminalDispatchIds: [facilitatorDispatchId, childDispatchId],
      rootTerminalAtMs: 50,
    });
  });

  it('keeps a scheduled retry lane nonterminal and accepts its later completion', () => {
    const retryWaiting = roomEvent(1, 'participant_activity', {
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-1',
      sourceEventId: 'agent-session:failed-attempt',
      sourceEventType: 'turn_failed',
      status: 'retry_wait',
      retryAttempt: 2,
      summary: '模型连接中断，已进入有界重试等待',
    });
    const completed = roomEvent(2, 'turn_completed', {
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-1',
      status: 'completed',
    });

    let state = reduceRoomEvent(createRoomProjection('room-1'), retryWaiting).state;
    expect(state.activitiesById[state.activityOrder[0]!]).toMatchObject({
      status: 'waiting',
      payload: {
        sourceEventType: 'turn_failed',
        status: 'retry_wait',
        retryAttempt: 2,
      },
    });
    expect(state.turnsById['room-turn-1']).toMatchObject({
      status: 'running',
      dispatchIds: ['dispatch-1'],
      terminalDispatchIds: [],
      failedDispatchIds: [],
      terminalParticipantIds: [],
      failedParticipantIds: [],
    });

    state = reduceRoomEvent(state, completed).state;
    expect(state.turnsById['room-turn-1']).toMatchObject({
      status: 'completed',
      terminalDispatchIds: ['dispatch-1'],
      failedDispatchIds: [],
      terminalParticipantIds: ['participant-1'],
      failedParticipantIds: [],
    });
  });

  it('merges optimistic room input by clientMessageId and keeps unknown events', () => {
    const optimistic = appendOptimisticRoomMessage(createRoomProjection('room-1'), {
      clientMessageId: 'room-client-1',
      text: '@澄 检查状态',
      nowMs: 1,
    });
    const merged = reduceRoomEvent(
      optimistic,
      roomEvent(1, 'user_message', {
        messageId: 'room-user-1',
        clientMessageId: 'room-client-1',
        text: '@澄 检查状态',
      }),
    ).state;
    const unknown = reduceRoomEvent(
      merged,
      roomEvent(2, 'future_room_vote', { result: 'kept' }),
    ).state;

    expect(merged.messageOrder).toEqual(['room-user-1']);
    expect(unknown.diagnostics[0]).toMatchObject({ eventType: 'future_room_vote' });
  });

  it('reconciles replayed participant steer messages by their causal clientActionId', () => {
    const first = reduceRoomEvent(
      createRoomProjection('room-1'),
      roomEvent(1, 'user_message', {
        messageId: 'steer-user-first',
        clientActionId: 'room-steer-action-1',
        rootId: 'room-turn-1',
        text: '先停止旧方向，只验证 Stop。',
        delivery: 'steer',
      }),
    ).state;
    const replayed = reduceRoomEvent(
      first,
      roomEvent(2, 'user_message', {
        messageId: 'steer-user-replayed',
        clientActionId: 'room-steer-action-1',
        rootId: 'room-turn-1',
        text: '先停止旧方向，只验证 Stop。',
        delivery: 'steer',
      }),
    ).state;

    expect(replayed.messageOrder).toEqual(['steer-user-replayed']);
    expect(replayed.turnsById['room-turn-1'].messageIds).toEqual(['steer-user-replayed']);
    expect(replayed.messagesById['steer-user-replayed']).toMatchObject({
      clientMessageId: 'room-steer-action-1',
      text: '先停止旧方向，只验证 Stop。',
    });
  });

  it('shows an optimistic turn as queued before the router accepts it', () => {
    const optimistic = appendOptimisticRoomMessage(createRoomProjection('room-1'), {
      clientMessageId: 'room-client-pending',
      text: '立即显示发送状态',
      nowMs: 12,
    });

    expect(optimistic.turnsById['local-room-turn:room-client-pending']).toMatchObject({
      status: 'queued',
      createdAtMs: 12,
    });
  });

  it('retains the live optimistic sheet alias when an accepted turn is rebuilt from a snapshot', () => {
    const clientMessageId = 'room-client-snapshot-alias';
    const optimistic = appendOptimisticRoomMessage(createRoomProjection('room-1'), {
      clientMessageId,
      text: '跨快照保持这一轮',
      nowMs: 1,
    });
    const acceptedWireEvent = {
      ...wireRoomEvent(1, 'user_message', {
        messageId: 'room-user-snapshot-alias',
        clientMessageId,
        rootId: 'room-turn-authoritative',
        text: '跨快照保持这一轮',
      }),
      turnId: 'room-turn-authoritative',
      participantId: null,
      sourceSessionId: '',
    };
    const acceptedEvent = parseRoomEvent(acceptedWireEvent);
    const accepted = reduceRoomEvent(optimistic, acceptedEvent).state;
    expect(accepted.turnsById['room-turn-authoritative']?.logicalRootId).toBe(
      'local-room-turn:room-client-snapshot-alias',
    );

    const snapshot = parseRoomEventSnapshot(roomSnapshotFixture([
      acceptedWireEvent,
    ]));
    const replayed = replayRoomEventSnapshot(accepted, snapshot);
    expect(replayed.turnsById['room-turn-authoritative']?.logicalRootId).toBe(
      'local-room-turn:room-client-snapshot-alias',
    );

    const applied = applyRoomSnapshot(accepted, {
      messages: [replayed.messagesById['room-user-snapshot-alias']!],
      lastSequence: 1,
      resumeToken: 'room-1:1',
    });
    expect(applied.turnsById['room-turn-authoritative']?.logicalRootId).toBe(
      'local-room-turn:room-client-snapshot-alias',
    );
  });

  it('retains the original sheet alias when a truncated snapshot/replay keeps only a retry turn', () => {
    const firstClientMessageId = 'room-client-retry-lineage';
    const retryClientMessageId = 'room-client-retry-only';
    const optimistic = appendOptimisticRoomMessage(createRoomProjection('room-1'), {
      clientMessageId: firstClientMessageId,
      text: '原始用户轮次',
      nowMs: 1,
    });
    const accepted = reduceRoomEvent(optimistic, parseRoomEvent({
      ...wireRoomEvent(1, 'user_message', {
        messageId: 'room-user-authoritative',
        clientMessageId: firstClientMessageId,
        rootId: 'room-turn-authoritative',
        text: '原始用户轮次',
      }),
      turnId: 'room-turn-authoritative',
      participantId: null,
      sourceSessionId: '',
    })).state;
    const retrying = appendOptimisticRoomMessage(accepted, {
      clientMessageId: retryClientMessageId,
      text: '原始用户轮次（重试）',
      retryOfRootId: 'room-turn-authoritative',
      nowMs: 3,
    });
    const retryWireEvent = {
      ...wireRoomEvent(2, 'user_message', {
        messageId: 'room-user-retry-authoritative',
        clientMessageId: retryClientMessageId,
        rootId: 'room-turn-retry-authoritative',
        retryOfRootId: 'room-turn-authoritative',
        text: '原始用户轮次（重试）',
      }),
      turnId: 'room-turn-retry-authoritative',
      participantId: null,
      sourceSessionId: '',
    };
    const snapshot = parseRoomEventSnapshot(roomSnapshotFixture([retryWireEvent], {
      firstSequence: 2,
      truncated: true,
    }));

    const replayed = replayRoomEventSnapshot(retrying, snapshot);
    const applied = applyRoomSnapshot(retrying, {
      messages: [replayed.messagesById['room-user-retry-authoritative']!],
      lastSequence: 2,
      resumeToken: 'room-1:2',
    });

    for (const projection of [replayed, applied]) {
      expect(projection.turnOrder).toEqual(['room-turn-retry-authoritative']);
      expect(projection.turnsById['room-turn-retry-authoritative']?.logicalRootId).toBe(
        `local-room-turn:${firstClientMessageId}`,
      );
      expect(projection.optimisticByClientMessageId[retryClientMessageId]).toBeUndefined();
    }
  });

  it('unwraps the public data envelope used by real participant runtime events', () => {
    const delta = reduceRoomEvent(
      createRoomProjection('room-1'),
      roomEvent(1, 'participant_delta', {
        sourceEventId: 'agent-session:1',
        sourceEventType: 'text_delta',
        data: {
          dispatchId: 'dispatch-real',
          messageId: 'real-message',
          delta: '真实对话',
        },
      }),
    ).state;
    const activity = reduceRoomEvent(
      delta,
      roomEvent(2, 'participant_activity', {
        sourceEventId: 'agent-session:2',
        sourceEventType: 'tool_finished',
        data: {
          dispatchId: 'dispatch-real',
          status: 'completed',
          summary: '已整理相关资料',
        },
      }),
    ).state;

    expect(Object.values(activity.messagesById)).toContainEqual(
      expect.objectContaining({
        sourceMessageId: 'real-message',
        dispatchId: 'dispatch-real',
        text: '真实对话',
      }),
    );
    expect(activity.activitiesById['room-1:2:activity']).toMatchObject({
      summary: '已整理相关资料',
      status: 'completed',
    });
  });

  it('does not create a ghost Room turn for detached subagent progress', () => {
    const detachedProgress = parseRoomEvent({
      schemaVersion: 'rag-ime.agent-room-event.v1',
      eventId: 'room-1:1',
      roomId: 'room-1',
      sequence: 1,
      turnId: '',
      eventType: 'participant_activity',
      participantId: 'participant-1',
      sourceSessionId: 'session-room-1',
      createdAtMs: 10,
      payload: {
        sourceEventId: 'session-room-1:53',
        sourceEventType: 'tool_progress',
        data: {
          rootId: '',
          runId: 'subagent-run:1',
          state: 'completed',
          summary: '子 Agent 已返回结果，待主持会话核验',
          toolCallId: 'subagent:subagent-batch:1',
          toolName: 'agents',
        },
      },
      resumeToken: 'room-1:1',
    });

    const state = reduceRoomEvent(
      createRoomProjection('room-1'),
      detachedProgress,
    ).state;

    expect(state.lastSequence).toBe(1);
    expect(state.turnOrder).toEqual([]);
    expect(state.activityOrder).toEqual([]);
  });

  it('ignores persisted direct Session events that were never routed by the Room', () => {
    const directSessionEvents = [
      parseRoomEvent({
        schemaVersion: 'rag-ime.agent-room-event.v1',
        eventId: 'room-1:1',
        roomId: 'room-1',
        sequence: 1,
        turnId: 'session-turn:direct',
        eventType: 'participant_delta',
        participantId: 'participant-1',
        sourceSessionId: 'session-room-1',
        createdAtMs: 10,
        payload: {
          sourceEventId: 'session-room-1:101',
          sourceEventType: 'text_delta',
          data: {
            rootId: 'session-turn:direct',
            messageId: 'direct-message',
            delta: '不应进入 Room',
          },
        },
        resumeToken: 'room-1:1',
      }),
      parseRoomEvent({
        schemaVersion: 'rag-ime.agent-room-event.v1',
        eventId: 'room-1:2',
        roomId: 'room-1',
        sequence: 2,
        turnId: 'session-turn:direct',
        eventType: 'participant_activity',
        participantId: 'participant-1',
        sourceSessionId: 'session-room-1',
        createdAtMs: 20,
        payload: {
          sourceEventId: 'session-room-1:102',
          sourceEventType: 'tool_finished',
          data: {
            rootId: 'session-turn:direct',
            status: 'completed',
            summary: '普通 Session 工具已完成',
          },
        },
        resumeToken: 'room-1:2',
      }),
      parseRoomEvent({
        schemaVersion: 'rag-ime.agent-room-event.v1',
        eventId: 'room-1:3',
        roomId: 'room-1',
        sequence: 3,
        turnId: 'session-turn:direct',
        eventType: 'turn_completed',
        participantId: 'participant-1',
        sourceSessionId: 'session-room-1',
        createdAtMs: 30,
        payload: {
          sourceEventId: 'session-room-1:103',
          sourceEventType: 'turn_completed',
          data: { rootId: 'session-turn:direct' },
        },
        resumeToken: 'room-1:3',
      }),
    ];

    const state = reduceRoomEvents(
      createRoomProjection('room-1'),
      directSessionEvents,
    );

    expect(state.lastSequence).toBe(3);
    expect(state.turnOrder).toEqual([]);
    expect(state.messageOrder).toEqual([]);
    expect(state.activityOrder).toEqual([]);
    expect(state.diagnostics).toHaveLength(3);
    expect(state.diagnostics[0]?.eventType).toBe(
      'unrouted_participant_session_event',
    );
  });

  it('reduces started, progress, and finished for one tool call to one terminal activity', () => {
    const started = reduceRoomEvent(
      createRoomProjection('room-1'),
      roomEvent(1, 'participant_activity', {
        rootId: 'root-tool-1',
        dispatchId: 'dispatch-tool-1',
        sourceEventType: 'tool_started',
        toolCallId: 'tool-call-1',
        toolName: 'memory',
        arguments: {
          path: '…/project/rag_ime',
          pattern: 'room_event_projection',
          limit: 40,
        },
      }),
    ).state;
    const progressed = reduceRoomEvent(
      started,
      roomEvent(2, 'participant_activity', {
        rootId: 'root-tool-1',
        dispatchId: 'dispatch-tool-1',
        sourceEventType: 'tool_progress',
        toolCallId: 'tool-call-1',
        toolName: 'memory',
        summary: '正在筛选公开记录',
      }),
    ).state;
    const finished = reduceRoomEvent(
      progressed,
      roomEvent(3, 'participant_activity', {
        rootId: 'root-tool-1',
        dispatchId: 'dispatch-tool-1',
        sourceEventType: 'tool_finished',
        toolCallId: 'tool-call-1',
        toolName: 'memory',
        summary: '找到了两条可用历史输入',
        isError: false,
        result: {
          outputPreview: 'agent_event_projection.py:350:def room_event_projection',
          outputTruncated: false,
        },
      }),
    ).state;
    const staleProgress = reduceRoomEvent(
      finished,
      roomEvent(4, 'participant_activity', {
        rootId: 'root-tool-1',
        dispatchId: 'dispatch-tool-1',
        sourceEventType: 'tool_progress',
        toolCallId: 'tool-call-1',
        toolName: 'memory',
        summary: '迟到的处理中状态',
      }),
    ).state;

    const activityId = 'root-tool-1:participant-1:dispatch-tool-1:tool-call-1';
    expect(started.activitiesById[activityId]).toMatchObject({
      status: 'running',
      summary: 'memory',
      createdAtMs: 10,
      payload: {
        sourceEventType: 'tool_started',
        arguments: { pattern: 'room_event_projection' },
      },
    });
    expect(progressed.activityOrder).toEqual([activityId]);
    expect(progressed.activitiesById[activityId]).toMatchObject({
      status: 'running',
      summary: '正在筛选公开记录',
    });
    expect(finished.activityOrder).toEqual([activityId]);
    expect(finished.turnsById['room-turn-1'].activityIds).toEqual([activityId]);
    expect(finished.activitiesById[activityId]).toMatchObject({
      status: 'completed',
      summary: '找到了两条可用历史输入',
      createdAtMs: 10,
      updatedAtMs: 30,
      payload: {
        sourceEventType: 'tool_finished',
        arguments: { pattern: 'room_event_projection' },
        result: { outputPreview: 'agent_event_projection.py:350:def room_event_projection' },
      },
    });
    expect(finished.activitiesById[activityId].payload.progressHistory).toHaveLength(3);
    expect(staleProgress.activitiesById[activityId]).toEqual(finished.activitiesById[activityId]);
  });

  it('keeps a running Tool visible when its automatic Room authorization receipt arrives', () => {
    const started = reduceRoomEvent(
      createRoomProjection('room-1'),
      roomEvent(1, 'participant_activity', {
        rootId: 'root-tool-policy',
        dispatchId: 'dispatch-tool-policy',
        sourceEventType: 'tool_started',
        toolCallId: 'tool-call-policy',
        toolName: 'workspace_shell',
        arguments: { command: 'git status --short' },
      }),
    ).state;
    const authorized = reduceRoomEvent(
      started,
      roomEvent(2, 'participant_activity', {
        rootId: 'root-tool-policy',
        dispatchId: 'dispatch-tool-policy',
        sourceEventType: 'approval_resolved',
        toolCallId: 'tool-call-policy',
        approvalId: 'approval-tool-policy',
        automatic: true,
        decisionMode: 'policy',
        state: 'applied',
        summary: 'Room policy authorized this operation',
      }),
    ).state;

    const activity = authorized.activitiesById[
      'root-tool-policy:participant-1:dispatch-tool-policy:tool-call-policy'
    ];
    expect(authorized.activityOrder).toHaveLength(1);
    expect(activity).toMatchObject({
      status: 'running',
      summary: 'workspace_shell',
      payload: {
        sourceEventType: 'tool_started',
        toolName: 'workspace_shell',
        arguments: { command: 'git status --short' },
        approvalId: 'approval-tool-policy',
        automatic: true,
        decisionMode: 'policy',
        approvalSourceEventType: 'approval_resolved',
      },
    });
  });

  it('projects an automatic Room authorization failure as the owning Tool failure', () => {
    const started = reduceRoomEvent(
      createRoomProjection('room-1'),
      roomEvent(1, 'participant_activity', {
        rootId: 'root-tool-policy-failed',
        dispatchId: 'dispatch-tool-policy-failed',
        sourceEventType: 'tool_started',
        toolCallId: 'tool-call-policy-failed',
        toolName: 'workspace_shell',
        arguments: { command: 'git status --short' },
      }),
    ).state;
    const rejected = reduceRoomEvent(
      started,
      roomEvent(2, 'participant_activity', {
        rootId: 'root-tool-policy-failed',
        dispatchId: 'dispatch-tool-policy-failed',
        sourceEventType: 'approval_resolved',
        toolCallId: 'tool-call-policy-failed',
        approvalId: 'approval-tool-policy-failed',
        automatic: true,
        decisionMode: 'policy',
        state: 'failed',
        error: 'Room dispatch no longer matches the active generation',
      }),
    ).state;

    const activity = rejected.activitiesById[
      'root-tool-policy-failed:participant-1:dispatch-tool-policy-failed:tool-call-policy-failed'
    ];
    expect(rejected.activityOrder).toHaveLength(1);
    expect(activity).toMatchObject({
      status: 'failed',
      payload: {
        sourceEventType: 'tool_finished',
        toolName: 'workspace_shell',
        arguments: { command: 'git status --short' },
        approvalId: 'approval-tool-policy-failed',
        approvalSourceEventType: 'approval_resolved',
        approvalResolutionState: 'failed',
        error: 'Room dispatch no longer matches the active generation',
      },
    });

    const lateFinished = reduceRoomEvent(
      rejected,
      roomEvent(3, 'participant_activity', {
        rootId: 'root-tool-policy-failed',
        dispatchId: 'dispatch-tool-policy-failed',
        sourceEventType: 'tool_finished',
        toolCallId: 'tool-call-policy-failed',
        toolName: 'workspace_shell',
        result: { summary: 'bridge returned after the authorization failed' },
      }),
    ).state;

    expect(lateFinished.activitiesById[
      'root-tool-policy-failed:participant-1:dispatch-tool-policy-failed:tool-call-policy-failed'
    ]).toEqual(activity);
  });

  it('projects a recovered standalone automatic Room policy failure as a Tool failure', () => {
    const recovered = reduceRoomEvent(
      createRoomProjection('room-1'),
      roomEvent(1, 'participant_activity', {
        rootId: 'root-tool-policy-recovered',
        dispatchId: 'dispatch-tool-policy-recovered',
        sourceEventType: 'approval_resolved',
        toolCallId: 'tool-call-policy-recovered',
        toolName: 'runtime',
        approvalId: 'approval-tool-policy-recovered',
        automatic: true,
        decisionMode: 'policy',
        state: 'stale',
        error: 'Room dispatch changed before execution',
      }),
    ).state;

    expect(recovered.activitiesById[
      'root-tool-policy-recovered:participant-1:dispatch-tool-policy-recovered:tool-call-policy-recovered'
    ]).toMatchObject({
      status: 'failed',
      summary: 'Room dispatch changed before execution',
      payload: {
        sourceEventType: 'tool_finished',
        toolName: 'runtime',
        approvalId: 'approval-tool-policy-recovered',
        approvalSourceEventType: 'approval_resolved',
        approvalResolutionState: 'stale',
        state: 'failed',
      },
    });
  });

  it('drops a stale preparatory summary when tool_finished has no terminal summary', () => {
    const started = reduceRoomEvent(
      createRoomProjection('room-1'),
      roomEvent(1, 'participant_activity', {
        rootId: 'root-room-state',
        dispatchId: 'dispatch-room-state',
        sourceEventType: 'tool_started',
        toolCallId: 'room-state-call',
        toolName: 'room_state',
        summary: '准备查看协作状态',
        status: 'running',
      }),
    ).state;
    const finished = reduceRoomEvent(
      started,
      roomEvent(2, 'participant_activity', {
        rootId: 'root-room-state',
        dispatchId: 'dispatch-room-state',
        sourceEventType: 'tool_finished',
        toolCallId: 'room-state-call',
        toolName: 'room_state',
        result: {
          unchanged: true,
          stateRevision: 12,
        },
      }),
    ).state;
    const activity = finished.activitiesById[
      'root-room-state:participant-1:dispatch-room-state:room-state-call'
    ];

    expect(activity).toMatchObject({
      status: 'completed',
      summary: 'room_state',
      payload: {
        sourceEventType: 'tool_finished',
        result: { unchanged: true, stateRevision: 12 },
      },
    });
    expect(activity.payload).not.toHaveProperty('summary');
    expect(activity.payload).not.toHaveProperty('status');
  });

  it('projects tool lifecycle identically from live SSE and HTTP snapshot replay', () => {
    const events = [
      wireRoomEvent(1, 'participant_activity', {
        rootId: 'root-replay',
        dispatchId: 'dispatch-replay',
        sourceEventType: 'tool_started',
        toolCallId: 'call-replay',
        toolName: 'read',
        arguments: { path: '…/project/src/runtime.ts' },
      }),
      wireRoomEvent(2, 'participant_activity', {
        rootId: 'root-replay',
        dispatchId: 'dispatch-replay',
        sourceEventType: 'tool_progress',
        toolCallId: 'call-replay',
        toolName: 'read',
        summary: '正在读取公开片段',
      }),
      wireRoomEvent(3, 'participant_activity', {
        rootId: 'root-replay',
        dispatchId: 'dispatch-replay',
        sourceEventType: 'tool_finished',
        toolCallId: 'call-replay',
        toolName: 'read',
        summary: '公开片段已返回',
        result: { outputPreview: 'export const roomReady = true;' },
      }),
    ];
    const live = reduceRoomEvents(
      createRoomProjection('room-1'),
      events.map((event) => parseRoomEvent(event)),
    );
    const replayed = replayRoomEventSnapshot(
      createRoomProjection('room-1'),
      parseRoomEventSnapshot(roomSnapshotFixture(events)),
    );
    const activityId = 'root-replay:participant-1:dispatch-replay:call-replay';

    expect(replayed.activityOrder).toEqual(live.activityOrder);
    expect(replayed.turnsById['room-turn-1'].activityIds).toEqual(
      live.turnsById['room-turn-1'].activityIds,
    );
    expect(replayed.activitiesById[activityId]).toEqual(live.activitiesById[activityId]);
    expect(replayed.activitiesById[activityId]).toMatchObject({
      status: 'completed',
      payload: {
        sourceEventType: 'tool_finished',
        result: { outputPreview: 'export const roomReady = true;' },
      },
    });
  });

  it('keeps separate calls to the same tool as separate activities', () => {
    const projection = reduceRoomEvents(createRoomProjection('room-1'), [
      roomEvent(1, 'participant_activity', {
        rootId: 'root-same-tool',
        dispatchId: 'dispatch-same-tool',
        sourceEventType: 'tool_finished',
        toolCallId: 'read-call-1',
        toolName: 'read',
        result: { outputPreview: 'first' },
      }),
      roomEvent(2, 'participant_activity', {
        rootId: 'root-same-tool',
        dispatchId: 'dispatch-same-tool',
        sourceEventType: 'tool_finished',
        toolCallId: 'read-call-2',
        toolName: 'read',
        result: { outputPreview: 'second' },
      }),
    ]);

    expect(projection.activityOrder).toEqual([
      'root-same-tool:participant-1:dispatch-same-tool:read-call-1',
      'root-same-tool:participant-1:dispatch-same-tool:read-call-2',
    ]);
    expect(Object.values(projection.activitiesById)).toHaveLength(2);
  });

  it('coalesces reasoning and current-progress updates into one card per lane', () => {
    const projection = reduceRoomEvents(createRoomProjection('room-1'), [
      roomEvent(1, 'participant_activity', {
        rootId: 'room-turn-1',
        dispatchId: 'dispatch-1',
        sourceEventType: 'reasoning_summary',
        state: 'running',
        summary: '正在检查边界',
      }),
      roomEvent(2, 'participant_activity', {
        rootId: 'room-turn-1',
        dispatchId: 'dispatch-1',
        sourceEventType: 'reasoning_summary',
        state: 'completed',
        summary: '边界检查完成',
      }),
      roomEvent(3, 'participant_activity', {
        rootId: 'room-turn-1',
        dispatchId: 'dispatch-1',
        sourceEventType: 'current_progress',
        summary: '正在整理结果',
      }),
      roomEvent(4, 'participant_activity', {
        rootId: 'room-turn-1',
        dispatchId: 'dispatch-1',
        sourceEventType: 'current_progress',
        summary: '正在等待审阅',
      }),
    ]);

    expect(projection.activityOrder).toHaveLength(2);
    expect(Object.values(projection.activitiesById).map((activity) => activity.summary)).toEqual([
      '边界检查完成',
      '正在等待审阅',
    ]);
    const progress = Object.values(projection.activitiesById).find((activity) => (
      activity.payload.sourceEventType === 'current_progress'
    ));
    expect(progress?.payload.progressHistory).toEqual([
      expect.objectContaining({ summary: '正在整理结果', status: 'running' }),
      expect.objectContaining({ summary: '正在等待审阅', status: 'running' }),
    ]);
  });

  it('bounds producer-supplied public progress history before retaining it', () => {
    const projection = reduceRoomEvent(
      createRoomProjection('room-1'),
      roomEvent(1, 'participant_activity', {
        rootId: 'room-turn-1',
        dispatchId: 'dispatch-1',
        sourceEventId: 'progress-24',
        sourceEventType: 'current_progress',
        summary: '最新公开进展',
        progressHistory: Array.from({ length: 25 }, (_, index) => ({
          eventId: `progress-${index}`,
          status: 'running',
          summary: `公开进展 ${index}`,
          createdAtMs: index,
        })),
      }),
    ).state;

    const activity = Object.values(projection.activitiesById)[0];
    expect(activity?.payload.progressHistory).toHaveLength(20);
    expect(activity?.payload.progressHistory).toEqual([
      expect.objectContaining({ eventId: 'progress-5' }),
      ...Array.from({ length: 18 }, () => expect.any(Object)),
      expect.objectContaining({ eventId: 'progress-24' }),
    ]);
  });

  it('selects one latest public summary per participant across live events and snapshot hydration', () => {
    const events = [
      wireRoomEvent(1, 'participant_activity', {
        rootId: 'root-public',
        dispatchId: 'dispatch-research',
        participantId: 'participant-research',
        sourceSessionId: 'session-research',
        sourceEventType: 'reasoning_summary',
        state: 'running',
        summary: '正在核对安装栈恢复边界',
      }),
      wireRoomEvent(2, 'participant_activity', {
        rootId: 'root-public',
        dispatchId: 'dispatch-review',
        participantId: 'participant-review',
        sourceSessionId: 'session-review',
        sourceEventType: 'tool_progress',
        toolCallId: 'review-call',
        toolName: 'read',
        summary: '正在检查公开回执',
      }),
      wireRoomEvent(3, 'participant_activity', {
        rootId: 'root-public',
        dispatchId: 'dispatch-research',
        participantId: 'participant-research',
        sourceSessionId: 'session-research',
        sourceEventType: 'reasoning_summary',
        state: 'completed',
        summary: '恢复边界已经确认',
      }),
      wireRoomEvent(4, 'participant_activity', {
        rootId: 'root-public',
        dispatchId: 'dispatch-research',
        participantId: 'participant-research',
        sourceSessionId: 'session-research-next',
        sourceEventType: 'reasoning_summary',
        state: 'running',
        summary: '正在准备共同复核',
      }),
    ];
    const live = reduceRoomEvents(
      createRoomProjection('room-1'),
      events.map((event) => parseRoomEvent(event)),
    );
    const snapshot = parseRoomEventSnapshot(roomSnapshotFixture(events));
    const hydrated = replayRoomEventSnapshot(createRoomProjection('room-1'), snapshot);
    const rehydrated = replayRoomEventSnapshot(hydrated, snapshot);

    expect(selectRoomParticipantPublicProgress(live)).toEqual(
      selectRoomParticipantPublicProgress(hydrated),
    );
    expect(selectRoomParticipantPublicProgress(rehydrated)).toEqual([
      expect.objectContaining({
        participantId: 'participant-research',
        sourceSessionId: 'session-research-next',
        kind: 'reasoning',
        status: 'running',
        summary: '正在准备共同复核',
      }),
      expect.objectContaining({
        participantId: 'participant-review',
        sourceSessionId: 'session-review',
        kind: 'tool',
        summary: '正在检查公开回执',
        data: expect.objectContaining({ toolName: 'read' }),
      }),
    ]);
  });

  it('omits progress events that add no public information', () => {
    const state = reduceRoomEvents(
      createRoomProjection('room-1'),
      [
        parseRoomEvent(wireRoomEvent(1, 'participant_activity', {
          rootId: 'root-public',
          dispatchId: 'dispatch-research',
          participantId: 'participant-research',
          sourceSessionId: 'session-research',
          activityKind: 'work',
          state: 'completed',
          summary: '协作进度已经同步',
        })),
        parseRoomEvent(wireRoomEvent(2, 'participant_activity', {
          rootId: 'root-public',
          dispatchId: 'dispatch-research',
          participantId: 'participant-research',
          sourceSessionId: 'session-research',
          sourceEventType: 'current_progress',
          state: 'running',
          summary: '已完成导入预览，正在核对错误行',
        })),
      ],
    );

    expect(selectRoomParticipantPublicProgress(state)).toEqual([
      expect.objectContaining({
        kind: 'progress',
        summary: '已完成导入预览，正在核对错误行',
      }),
    ]);
  });

  it('keeps one authoritative selectable request until its Session resolves it', () => {
    const waiting = reduceRoomEvent(
      createRoomProjection('room-1'),
      roomEvent(1, 'participant_activity', {
        rootId: 'room-turn-1',
        dispatchId: 'dispatch-1',
        sourceEventId: 'session:input:1',
        sourceEventType: 'user_input_required',
        data: {
          requestId: 'input:1',
          requestKind: 'user_input_required',
          method: 'select',
          title: '选择部署环境',
          options: [{ id: 'staging', label: '预发布' }, { id: 'production', label: '生产' }],
        },
      }),
    ).state;
    const resolved = reduceRoomEvent(
      waiting,
      roomEvent(2, 'participant_activity', {
        rootId: 'room-turn-1',
        dispatchId: 'dispatch-1',
        sourceEventId: 'session:input:2',
        sourceEventType: 'user_input_required',
        data: {
          requestId: 'input:1',
          requestKind: 'user_input_required',
          method: 'select',
          resolutionState: 'resolved',
          resolutionSource: 'user',
        },
      }),
    ).state;
    const activityId = 'room-turn-1:participant-1:dispatch-1:input:1';

    expect(waiting.activitiesById[activityId]).toMatchObject({
      status: 'waiting',
      sourceSessionId: 'session-room-1',
      payload: { title: '选择部署环境', options: expect.any(Array) },
    });
    expect(resolved.activityOrder).toEqual([activityId]);
    expect(resolved.activitiesById[activityId]).toMatchObject({
      status: 'completed',
      payload: {
        title: '选择部署环境',
        resolutionState: 'resolved',
        resolutionSource: 'user',
      },
    });
  });

  it('keeps grouped clarification waiting even when it does not use a single-input method', () => {
    const pending = reduceRoomEvent(
      createRoomProjection('room-1'),
      roomEvent(1, 'participant_activity', {
        rootId: 'room-turn-1',
        dispatchId: 'dispatch-1',
        sourceEventId: 'session:grouped:1',
        requestId: 'grouped:1',
        requestKind: 'grouped_questions',
        questions: [
          { id: 'scope', question: '先覆盖哪一部分？', options: ['核心流程', '完整流程'] },
          { id: 'review', question: '如何复核？', options: ['伙伴互查', '直接交付'] },
        ],
      }),
    ).state;

    expect(Object.values(pending.activitiesById)).toEqual([
      expect.objectContaining({
        status: 'waiting',
        payload: expect.objectContaining({
          requestKind: 'grouped_questions',
          questions: expect.any(Array),
        }),
      }),
    ]);
  });

  it('prefers explicit public data identities over envelope fallbacks', () => {
    const event = roomEvent(1, 'participant_activity', {
      rootId: 'root-fallback',
      dispatchId: 'dispatch-fallback',
      messageId: 'message-fallback',
      blockId: 'block-fallback',
      sourceEventId: 'source-fallback',
      sourceEventType: 'user_input_required',
      data: {
        rootId: 'root-owner',
        dispatchId: 'dispatch-owner',
        messageId: 'message-owner',
        blockId: 'block-owner',
        sourceEventId: 'source-owner',
        sourceEventType: 'user_input_required',
        participantId: 'participant-owner',
        sourceSessionId: 'session-owner',
        requestId: 'input-owner',
        requestKind: 'plan_review',
        method: 'select',
        options: ['批准', '修改'],
      },
    });
    event.participantId = 'participant-fallback';
    event.sourceSessionId = 'session-fallback';

    const state = reduceRoomEvent(createRoomProjection('room-1'), event).state;
    const activity = state.activitiesById[
      'root-owner:participant-owner:dispatch-owner:input-owner'
    ];

    expect(activity).toMatchObject({
      participantId: 'participant-owner',
      sourceSessionId: 'session-owner',
      status: 'waiting',
      payload: {
        rootId: 'root-owner',
        dispatchId: 'dispatch-owner',
        messageId: 'message-owner',
        blockId: 'block-owner',
        sourceEventId: 'source-owner',
      },
    });
    expect(roomActivityLaneIdentity(activity)).toMatchObject({
      rootId: 'root-owner',
      participantId: 'participant-owner',
      dispatchId: 'dispatch-owner',
    });
  });

  it('isolates parallel activities by root, participant, and dispatch', () => {
    const first = reduceRoomEvent(
      createRoomProjection('room-1'),
      roomEvent(1, 'participant_activity', {
        dispatchId: 'dispatch-a',
        rootId: 'root-a',
        toolCallId: 'call-1',
        sourceEventType: 'tool_started',
        toolName: 'memory',
      }),
    ).state;
    const secondEvent = roomEvent(2, 'participant_activity', {
      dispatchId: 'dispatch-b',
      rootId: 'root-a',
      toolCallId: 'call-1',
      sourceEventType: 'tool_started',
      toolName: 'memory',
    });
    secondEvent.participantId = 'participant-2';
    secondEvent.sourceSessionId = 'session-room-2';
    const second = reduceRoomEvent(first, secondEvent).state;
    const activities = second.activityOrder.map((id) => second.activitiesById[id]);

    expect(second.activityOrder).toHaveLength(2);
    expect(activities.map(roomActivityLaneIdentity).map((identity) => identity.key)).toEqual([
      'root-a\u001fparticipant-1\u001fdispatch-a',
      'root-a\u001fparticipant-2\u001fdispatch-b',
    ]);
  });

  it('keeps lane failures scoped until all Room dispatches settle', () => {
    const routeOne = roomEvent(1, 'route_decision', {
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-1',
      targetParticipantId: 'participant-1',
    });
    const routeTwo = roomEvent(2, 'route_decision', {
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-2',
      targetParticipantId: 'participant-2',
    });
    routeTwo.participantId = 'participant-2';
    routeTwo.sourceSessionId = 'session-room-2';
    const firstDone = roomEvent(3, 'turn_completed', {
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-1',
    });
    const secondFailed = roomEvent(4, 'turn_failed', {
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-2',
      summary: '证据源暂时不可用',
      nextStep: '修正读取范围后在当前任务上重试',
    });
    secondFailed.participantId = 'participant-2';
    secondFailed.sourceSessionId = 'session-room-2';

    const routedOne = reduceRoomEvent(createRoomProjection('room-1'), routeOne).state;
    const routedBoth = reduceRoomEvent(routedOne, routeTwo).state;
    const oneTerminal = reduceRoomEvent(routedBoth, firstDone).state;
    const allTerminal = reduceRoomEvent(oneTerminal, secondFailed).state;
    const rootFailedEvent = roomEvent(5, 'turn_failed', {
      rootId: 'room-turn-1',
      error: '一条执行 lane 未完成',
    });
    rootFailedEvent.participantId = null;
    rootFailedEvent.sourceSessionId = '';
    const rootFailed = reduceRoomEvent(allTerminal, rootFailedEvent).state;

    expect(oneTerminal.turnsById['room-turn-1']).toMatchObject({
      status: 'running',
      participantIds: ['participant-1', 'participant-2'],
      terminalParticipantIds: ['participant-1'],
    });
    expect(allTerminal.turnsById['room-turn-1']).toMatchObject({
      status: 'failed',
      failure: '证据源暂时不可用；修正读取范围后在当前任务上重试',
      terminalParticipantIds: ['participant-1', 'participant-2'],
      failedParticipantIds: ['participant-2'],
    });
    expect(rootFailed.turnsById['room-turn-1']).toMatchObject({
      status: 'failed',
      rootTerminalAtMs: 40,
    });
    expect(rootFailed.turnsById['room-turn-1'].terminalDispatchIds).toEqual([
      'dispatch-1',
      'dispatch-2',
    ]);
    expect(rootFailed.turnsById['room-turn-1'].failedDispatchIds).toEqual([
      'dispatch-2',
    ]);
    expect(rootFailed.turnsById['room-turn-1'].failedParticipantIds).toEqual([
      'participant-2',
    ]);
  });

  it('closes a root as aborted and never lets a late completed event resurrect it', () => {
    const routeOne = roomEvent(1, 'route_decision', {
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-1',
      targetParticipantId: 'participant-1',
    });
    const routeTwo = roomEvent(2, 'route_decision', {
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-2',
      targetParticipantId: 'participant-2',
    });
    routeTwo.participantId = 'participant-2';
    routeTwo.sourceSessionId = 'session-room-2';
    const firstAborted = roomEvent(3, 'turn_completed', {
      status: 'aborted',
      aborted: true,
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-1',
    });
    const secondAborted = roomEvent(4, 'turn_completed', {
      status: 'aborted',
      aborted: true,
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-2',
    });
    secondAborted.participantId = 'participant-2';
    secondAborted.sourceSessionId = 'session-room-2';
    const rootAborted = roomEvent(5, 'turn_completed', {
      status: 'aborted',
      aborted: true,
      rootId: 'room-turn-1',
    });
    rootAborted.participantId = null;
    rootAborted.sourceSessionId = '';
    const lateCompleted = roomEvent(6, 'turn_completed', {
      status: 'completed',
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-1',
    });

    const routedOne = reduceRoomEvent(createRoomProjection('room-1'), routeOne).state;
    const routedBoth = reduceRoomEvent(routedOne, routeTwo).state;
    const oneStopped = reduceRoomEvent(routedBoth, firstAborted).state;
    const allStopped = reduceRoomEvent(oneStopped, secondAborted).state;
    const stoppedRoot = reduceRoomEvent(allStopped, rootAborted).state;
    const afterLate = reduceRoomEvent(stoppedRoot, lateCompleted).state;

    expect(oneStopped.turnsById['room-turn-1'].status).toBe('running');
    expect(allStopped.turnsById['room-turn-1']).toMatchObject({
      status: 'aborted',
      terminalParticipantIds: ['participant-1', 'participant-2'],
      abortedParticipantIds: ['participant-1', 'participant-2'],
    });
    expect(afterLate.turnsById['room-turn-1']).toMatchObject({
      status: 'aborted',
      abortedParticipantIds: ['participant-1', 'participant-2'],
    });
  });

  it.each(['live', 'snapshot', 'conversation'] as const)(
    'settles unfinished dispatches from an applied Root cancellation receipt during %s',
    (mode) => {
      // The initial moderator dispatch can have no individual terminal while
      // a later wake and Partner have already finished. The cancellation owner
      // publishes one whole-Root receipt after every execution surface stops.
      const events = [
        wireRoomEvent(1, 'route_decision', { dispatchId: 'initial', targetParticipantId: 'participant-1' }),
        wireRoomEvent(2, 'route_decision', { dispatchId: 'wake', targetParticipantId: 'participant-1' }),
        wireRoomEvent(3, 'turn_completed', { dispatchId: 'wake', status: 'completed' }),
        {
          ...wireRoomEvent(4, 'participant_status', {
            status: 'cancellation_applied',
            rootId: 'room-turn-1',
            cancellationReceiptId: 'room-cancel-1',
            pendingTargets: [],
            surfaces: { session: { state: 'terminated' }, provider: { state: 'terminated' } },
          }),
          participantId: null,
          sourceSessionId: '',
        },
      ];
      const initial = createRoomProjection('room-1');
      const state = mode === 'live'
        ? reduceRoomEvents(initial, events.map(parseRoomEvent))
        : mode === 'snapshot'
          ? replayRoomEventSnapshot(initial, parseRoomEventSnapshot(roomSnapshotFixture(events)))
          : replayRoomConversationSnapshot(initial, parseRoomConversationSnapshot(
            roomConversationSnapshotFixture(events, 4, 0),
          ));

      expect(state.turnsById['room-turn-1']).toMatchObject({
        status: 'aborted',
        terminalDispatchIds: ['initial', 'wake'],
        abortedDispatchIds: ['initial'],
        rootTerminalAtMs: 40,
      });
      const afterLateActivity = reduceRoomEvent(state, roomEvent(5, 'participant_activity', {
        dispatchId: 'initial', sourceEventType: 'tool_started', toolCallId: 'late-call',
      })).state;
      expect(afterLateActivity.turnsById['room-turn-1'].status).toBe('aborted');
      expect(Object.values(afterLateActivity.activitiesById).some((activity) => activity.status === 'running')).toBe(false);
    },
  );

  it.each([
    { status: 'cancellation_pending', pendingTargets: ['provider'] },
    { status: 'cancellation_applied', pendingTargets: ['provider'] },
    { status: 'cancellation_applied', rootId: 'another-root' },
    { status: 'cancellation_applied', cancellationReceiptId: '' },
  ])('does not close a Root from an incomplete or mismatched cancellation receipt: %j', (override) => {
    const state = reduceRoomEvents(createRoomProjection('room-1'), [
      roomEvent(1, 'route_decision', { dispatchId: 'initial', targetParticipantId: 'participant-1' }),
      parseRoomEvent({
        ...wireRoomEvent(2, 'participant_status', {
          rootId: 'room-turn-1',
          cancellationReceiptId: 'room-cancel-1', pendingTargets: [], ...override,
        }),
        participantId: null,
        sourceSessionId: '',
      }),
    ]);
    expect(state.turnsById['room-turn-1'].status).toBe('running');
    expect(state.turnsById['room-turn-1'].terminalDispatchIds).toEqual([]);
  });

  it('settles an interrupted tool activity when its Root is aborted', () => {
    const events = [
      roomEvent(1, 'route_decision', {
        rootId: 'room-turn-1',
        dispatchId: 'dispatch-1',
        targetParticipantId: 'participant-1',
      }),
      parseRoomEvent(wireRoomEvent(2, 'participant_activity', {
        data: {
          rootId: 'room-turn-1',
          dispatchId: 'dispatch-1',
          toolCallId: 'call-bash',
          toolName: 'bash',
        },
        sourceEventId: 'session-room-1:2',
        sourceEventType: 'tool_started',
      })),
    ];
    const rootAborted = parseRoomEvent({
      ...wireRoomEvent(3, 'turn_completed', {
        rootId: 'room-turn-1',
        status: 'aborted',
      }),
      participantId: null,
      sourceSessionId: '',
      createdAtMs: 0,
    });

    let state = createRoomProjection('room-1');
    for (const event of [...events, rootAborted]) {
      state = reduceRoomEvent(state, event).state;
    }
    const activity = Object.values(state.activitiesById).find(
      (candidate) => candidate.payload.toolCallId === 'call-bash',
    );
    const publicProgress = selectRoomParticipantPublicProgress(state).find(
      (candidate) => candidate.kind === 'tool',
    );

    expect(activity).toMatchObject({
      status: 'aborted',
      updatedAtMs: 20,
    });
    expect(publicProgress).toMatchObject({
      kind: 'tool',
      status: 'aborted',
    });
  });

  it('preserves settled lane evidence when Root aborts only unfinished work', () => {
    const routeOne = roomEvent(1, 'route_decision', {
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-1',
      targetParticipantId: 'participant-1',
    });
    const routeTwo = roomEvent(2, 'route_decision', {
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-2',
      targetParticipantId: 'participant-2',
    });
    routeTwo.participantId = 'participant-2';
    routeTwo.sourceSessionId = 'session-room-2';
    const published = roomEvent(3, 'room_post', {
      post: roomPost('post-settled', '第一项分工已交付', 'dispatch-1'),
    });
    const rootAborted = roomEvent(4, 'turn_completed', {
      status: 'aborted',
      aborted: true,
      rootId: 'room-turn-1',
    });
    rootAborted.participantId = null;
    rootAborted.sourceSessionId = '';

    let state = createRoomProjection('room-1');
    for (const event of [routeOne, routeTwo, published, rootAborted]) {
      state = reduceRoomEvent(state, event).state;
    }

    expect(state.turnsById['room-turn-1']).toMatchObject({
      status: 'aborted',
      terminalDispatchIds: ['dispatch-1', 'dispatch-2'],
      abortedDispatchIds: ['dispatch-2'],
      terminalParticipantIds: ['participant-1', 'participant-2'],
      abortedParticipantIds: ['participant-2'],
    });
    expect(state.messagesById['post-settled']).toMatchObject({
      status: 'completed',
      text: '第一项分工已交付',
    });
  });

  it('rejects late deltas, RoomPosts, and terminals after the Root fence', () => {
    const events = [
      roomEvent(1, 'route_decision', {
        rootId: 'room-turn-1', dispatchId: 'dispatch-1', targetParticipantId: 'participant-1',
      }),
      roomEvent(2, 'participant_delta', {
        rootId: 'room-turn-1', dispatchId: 'dispatch-1', messageId: 'draft-1', delta: '草稿',
      }),
      roomEvent(3, 'room_post', {
        post: roomPost('post-1', '正式交付', 'dispatch-1'),
      }),
    ];
    let state = createRoomProjection('room-1');
    for (const event of events) state = reduceRoomEvent(state, event).state;
    state = reduceRoomEvent(state, parseRoomEvent({
      ...wireRoomEvent(4, 'turn_completed', { status: 'completed' }),
      participantId: null,
      sourceSessionId: '',
    })).state;
    state = reduceRoomEvent(state, roomEvent(5, 'participant_delta', {
      rootId: 'room-turn-1', dispatchId: 'dispatch-1', messageId: 'late-draft', delta: '迟到草稿',
    })).state;
    state = reduceRoomEvent(state, roomEvent(6, 'room_post', {
      post: roomPost('post-late', '迟到公开 Post', 'dispatch-1'),
    })).state;
    state = reduceRoomEvent(state, roomEvent(7, 'turn_failed', {
      rootId: 'room-turn-1', error: '迟到失败',
    })).state;

    expect(state.lastSequence).toBe(7);
    expect(state.messageOrder).toEqual(['post-1']);
    expect(state.messagesById['post-1']).toMatchObject({
      status: 'completed',
      text: '正式交付',
    });
    expect(state.turnsById['room-turn-1']).toMatchObject({
      status: 'completed',
      rootTerminalAtMs: 40,
    });
    expect(state.diagnostics.filter(
      (item) => item.eventType === 'room_event_after_root_terminal',
    )).toHaveLength(3);
  });

  it('projects Room lifecycle events as completed instead of leaving a false running turn', () => {
    const created = reduceRoomEvent(
      createRoomProjection('room-1'),
      roomEvent(1, 'participant_status', { status: 'room_created' }),
    ).state;

    expect(created.activitiesById['room-1:1:activity'].status).toBe('completed');
    expect(created.turnsById['room-turn-1'].status).toBe('completed');
  });

  it('strictly validates and replays a retained event snapshot without looping on old gap markers', () => {
    const optimistic = appendOptimisticRoomMessage(createRoomProjection('room-1'), {
      clientMessageId: 'room-client-1',
      text: '@澄 检查状态',
      nowMs: 1,
    });
    const snapshot = parseRoomEventSnapshot(roomSnapshotFixture([
      wireRoomEvent(5, 'user_message', {
        clientMessageId: 'room-client-1',
        text: '@澄 检查状态',
      }),
      wireRoomEvent(6, 'snapshot_required', { reason: 'old_replay_gap' }),
      wireRoomEvent(7, 'participant_delta', {
        messageId: 'room-message-1',
        delta: '已恢复',
      }),
    ], { firstSequence: 5, truncated: true }));

    const replayed = replayRoomEventSnapshot(optimistic, snapshot);

    expect(replayed.lastSequence).toBe(7);
    expect(replayed.resumeToken).toBe('room-1:7');
    expect(replayed.needsSnapshot).toBe(false);
    expect(replayed.messageOrder).not.toContain('local-room:room-client-1');
    expect(replayed.messageOrder).toContain('room-1:5:user');
    expect(replayed.messagesById['room-message-1'].text).toBe('已恢复');
    expect(replayed.diagnostics).toEqual([
      expect.objectContaining({ eventType: 'snapshot_required', sequence: 6 }),
    ]);
  });

  it('retains Team ownership when restoring a strict Room snapshot', () => {
    const fixture = roomSnapshotFixture([]);
    const participant = {
      ...fixture.room.participants[0],
      ownerUserId: 'user-wang', ownerDisplayName: '小王', spaceId: 'project-website',
      canControl: false, audience: 'project',
    };
    const snapshot = parseRoomEventSnapshot({
      ...fixture,
      room: { ...fixture.room, participants: [participant, fixture.room.participants[1]] },
    });
    expect(snapshot.room.participants[0]).toMatchObject(participant);
    expect(() => parseRoomEventSnapshot({
      ...fixture,
      room: { ...fixture.room, participants: [{ ...participant, canControl: 'true' }, fixture.room.participants[1]] },
    })).toThrow(/Invalid agent-room-snapshot.v1 payload/);
  });

  it('rejects extra snapshot fields and non-contiguous retained events', () => {
    expect(() => parseRoomEventSnapshot({ ...roomSnapshotFixture([]), arbitrary: true })).toThrow(
      /Invalid agent-room-snapshot.v1 payload/,
    );
    expect(() => parseRoomEventSnapshot(roomSnapshotFixture([
      wireRoomEvent(1, 'user_message', { text: 'one' }),
      wireRoomEvent(3, 'turn_completed', {}),
    ], { firstSequence: 1 }))).toThrow(/contiguous/);
  });

  it('replays sparse message-first Room snapshots without treating deferred activity as a gap', () => {
    const events = [
      wireRoomEvent(1, 'user_message', {
        clientMessageId: 'client-conversation',
        messageId: 'message-user',
        text: '先显示真实消息',
      }),
      wireRoomEvent(2, 'route_decision', {
        rootId: 'room-turn-1',
        dispatchId: 'dispatch-1',
        targetParticipantId: 'participant-1',
      }),
      wireRoomEvent(20, 'participant_message', {
        dispatchId: 'dispatch-1',
        message: roomServerMessage('message-assistant', '消息已加载'),
      }),
      wireRoomEvent(25, 'turn_completed', {
        dispatchId: 'dispatch-1',
        status: 'completed',
      }),
    ];
    const snapshot = parseRoomConversationSnapshot(
      roomConversationSnapshotFixture(events, 30, 26),
    );
    const replayed = replayRoomConversationSnapshot(
      createRoomProjection('room-1'),
      snapshot,
    );

    expect(replayed.messageOrder).toEqual(['message-user', 'message-assistant']);
    expect(replayed.messagesById['message-assistant']?.text).toBe('消息已加载');
    expect(replayed.lastSequence).toBe(30);
    expect(replayed.resumeToken).toBe('room-1:30');
    expect(replayed.needsSnapshot).toBe(false);

    const live = reduceRoomEvent(
      replayed,
      parseRoomEvent(wireRoomEvent(31, 'room_config_changed', {})),
    );
    expect(live.disposition).toBe('applied');
    expect(live.state.needsSnapshot).toBe(false);
  });

  it('validates bounded Room history pages and their cursor invariants', () => {
    const items = [
      wireRoomEvent(3, 'participant_status', { status: 'working' }),
      wireRoomEvent(4, 'turn_completed', {}),
    ];
    const rawPage = {
      schemaVersion: 'rag-ime.agent-room-event-page.v1',
      ok: true,
      roomId: 'room-1',
      items,
      firstSequence: 3,
      lastSequence: 4,
      nextBeforeSequence: 3,
      hasMore: true,
      retainedFirstSequence: 1,
      retainedLastSequence: 10,
      retainedPrefixTruncated: false,
    };
    const page = parseRoomEventPage(rawPage);

    expect(page.items.map((event) => event.sequence)).toEqual([3, 4]);
    expect(() => parseRoomEventPage({
      ...rawPage,
      lastSequence: 5,
    })).toThrow(/bounds/);
  });
  it('projects model arbitration as running work instead of human review', () => {
    const pending = roomEvent(1, 'participant_activity', {
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-1',
      approvalId: 'approval-model-1',
      payloadSha256: 'c'.repeat(64),
      decisionMode: 'model',
      automatic: true,
      approvalModelDecision: {
        status: 'pending',
        model: 'openai-codex/gpt-5.6-luna',
      },
    });
    const deciding = reduceRoomEvent(createRoomProjection('room-1'), pending).state;
    const activityId = deciding.activityOrder[0]!;

    expect(deciding.activitiesById[activityId]).toMatchObject({
      status: 'running',
      payload: {
        approvalId: 'approval-model-1',
        decisionMode: 'model',
      },
    });

    const denied = roomEvent(2, 'participant_activity', {
      rootId: 'room-turn-1',
      dispatchId: 'dispatch-1',
      approvalId: 'approval-model-1',
      payloadSha256: 'c'.repeat(64),
      state: 'rejected',
      resolutionState: 'rejected',
      decisionMode: 'model',
      automatic: true,
      approvalDecisionReceiptId: 'approval-model-decision:1',
      approvalModelDecision: {
        status: 'decided',
        decision: 'deny',
        model: 'openai-codex/gpt-5.6-luna',
        reasonCodes: ['destructive_command'],
      },
    });
    const settled = reduceRoomEvent(deciding, denied).state;

    expect(settled.activityOrder).toEqual([activityId]);
    expect(settled.activitiesById[activityId]).toMatchObject({
      status: 'completed',
      payload: {
        approvalDecisionReceiptId: 'approval-model-decision:1',
        approvalModelDecision: {
          decision: 'deny',
          reasonCodes: ['destructive_command'],
        },
      },
    });
  });



  it('projects a canonical user wait and clears it only from a later user RoomPost on the same Root', () => {
    const question = wireRoomEvent(1, 'room_post', {
      post: {
        schemaVersion: 'wisdom-weasel.room-post.v2',
        postId: 'wait-post-1',
        roomId: 'room-1',
        rootId: 'room-turn-1',
        generation: 0,
        authorActorRef: 'participant:facilitator',
        kind: 'wait',
        visibility: 'room',
        content: '请选择发布方式。',
        question: {
          prompt: '发布预览版还是稳定版？',
          options: [
            { value: 'preview', label: '预览版', recommended: true },
            { value: 'stable', label: '稳定版' },
          ],
        },
        idempotencyKey: 'wait-post-1',
        publicationSource: { kind: 'room_commit', ref: 'commit:wait-1' },
        createdAtMs: 10,
      },
    });
    let state = reduceRoomEvent(
      createRoomProjection('room-1'),
      parseRoomEvent(question),
    ).state;
    expect(state.pendingUserQuestion).toMatchObject({
      postId: 'wait-post-1',
      roomId: 'room-1',
      rootId: 'room-turn-1',
      sequence: 1,
      prompt: '发布预览版还是稳定版？',
      options: [
        { value: 'preview', label: '预览版', recommended: true },
        { value: 'stable', label: '稳定版' },
      ],
    });

    state = appendOptimisticRoomMessage(state, {
      clientMessageId: 'answer-client-1',
      text: 'stable',
      nowMs: 20,
    });
    state = reduceRoomEvent(state, parseRoomEvent(wireRoomEvent(2, 'user_message', {
      messageId: 'answer-message-1',
      clientMessageId: 'answer-client-1',
      rootId: 'room-turn-1',
      text: 'stable',
    }))).state;
    expect(state.pendingUserQuestion?.postId).toBe('wait-post-1');

    const unrelatedPost = wireRoomEvent(3, 'room_post', {
      post: {
        schemaVersion: 'wisdom-weasel.room-post.v2',
        postId: 'unrelated-user-post',
        roomId: 'room-1',
        rootId: 'another-root',
        generation: 0,
        authorActorRef: 'user:local',
        kind: 'request',
        visibility: 'room',
        content: '另一项任务',
        idempotencyKey: 'user-message:unrelated',
        publicationSource: { kind: 'user', ref: 'unrelated' },
        createdAtMs: 30,
      },
    });
    unrelatedPost.turnId = 'another-root';
    state = reduceRoomEvent(state, parseRoomEvent(unrelatedPost)).state;
    expect(state.pendingUserQuestion?.postId).toBe('wait-post-1');

    let terminalState = reduceRoomEvent(
      createRoomProjection('room-1'),
      parseRoomEvent(question),
    ).state;
    const rootTerminal = wireRoomEvent(2, 'turn_completed', {
      rootId: 'room-turn-1',
      status: 'completed',
    });
    rootTerminal.participantId = null;
    rootTerminal.sourceSessionId = '';
    terminalState = reduceRoomEvent(terminalState, parseRoomEvent(rootTerminal)).state;
    expect(terminalState.pendingUserQuestion?.postId).toBe('wait-post-1');
    expect(terminalState.messagesById['wait-post-1']?.question).toMatchObject({
      status: 'pending',
    });

    const acceptedAnswer = wireRoomEvent(4, 'room_post', {
      post: {
        schemaVersion: 'wisdom-weasel.room-post.v2',
        postId: 'answer-post-1',
        roomId: 'room-1',
        rootId: 'room-turn-1',
        generation: 0,
        authorActorRef: 'user:local',
        kind: 'request',
        visibility: 'room',
        content: 'stable',
        idempotencyKey: 'user-message:answer-client-1',
        publicationSource: { kind: 'user', ref: 'answer-client-1' },
        createdAtMs: 40,
      },
    });
    acceptedAnswer.participantId = null;
    state = reduceRoomEvent(state, parseRoomEvent(acceptedAnswer)).state;
    expect(state.pendingUserQuestion).toBeUndefined();
    expect(state.messagesById['wait-post-1']?.question).toMatchObject({
      status: 'answered',
      answer: '稳定版',
    });
    expect(state.messagesById['answer-post-1']?.answerToPostId).toBe('wait-post-1');
  });
  it('reconciles one optimistic answer through user_message and its authoritative user RoomPost', () => {
    const clientMessageId = 'room-web-answer-1';
    let state = appendOptimisticRoomMessage(createRoomProjection('room-1'), {
      clientMessageId,
      text: '采用稳定版',
      nowMs: 1,
    });
    const acceptedMessage = wireRoomEvent(1, 'user_message', {
      messageId: 'accepted-answer-1',
      clientMessageId,
      rootId: 'room-turn-1',
      text: '采用稳定版',
    });
    acceptedMessage.participantId = null;
    state = reduceRoomEvent(state, parseRoomEvent(acceptedMessage)).state;

    const acceptedPost = wireRoomEvent(2, 'room_post', {
      post: {
        schemaVersion: 'wisdom-weasel.room-post.v2',
        postId: 'answer-post-1',
        roomId: 'room-1',
        rootId: 'room-turn-1',
        generation: 0,
        authorActorRef: 'user:local',
        kind: 'request',
        visibility: 'room',
        content: '采用稳定版',
        idempotencyKey: `user-message:${clientMessageId}`,
        publicationSource: { kind: 'user', ref: clientMessageId },
        createdAtMs: 20,
      },
    });
    acceptedPost.participantId = null;
    state = reduceRoomEvent(state, parseRoomEvent(acceptedPost)).state;

    expect(state.messageOrder).toEqual(['answer-post-1']);
    expect(state.turnOrder).toEqual(['room-turn-1']);
    expect(state.turnsById['room-turn-1'].messageIds).toEqual(['answer-post-1']);
    expect(state.messagesById['answer-post-1']).toMatchObject({
      clientMessageId,
      role: 'user',
      projectionKind: 'post',
      rootId: 'room-turn-1',
      text: '采用稳定版',
    });
    expect(state.optimisticByClientMessageId[clientMessageId]).toBeUndefined();
  });
});

function wireRoomEvent(
  sequence: number,
  eventType: string,
  payload: Record<string, unknown>,
) {
  return {
    schemaVersion: 'rag-ime.agent-room-event.v1',
    eventId: `room-1:${sequence}`,
    roomId: 'room-1',
    sequence,
    turnId: 'room-turn-1',
    eventType,
    participantId: 'participant-1' as string | null,
    sourceSessionId: 'session-room-1',
    createdAtMs: sequence * 10,
    payload,
    resumeToken: `room-1:${sequence}`,
  };
}

function roomSnapshotFixture(
  events: ReturnType<typeof wireRoomEvent>[],
  options: { firstSequence?: number; truncated?: boolean } = {},
) {
  const firstSequence = options.firstSequence ?? (events[0]?.sequence ?? 0);
  const lastSequence = events.at(-1)?.sequence ?? 0;
  return {
    schemaVersion: 'rag-ime.agent-room-snapshot.v1',
    ok: true,
    room: {
      schemaVersion: 'rag-ime.agent-room.v1',
      id: 'room-1',
      title: '快照 Room',
      status: 'active',
      permissionPolicy: {
        schemaVersion: 'rag-ime.room-permission-policy.v1',
        room: { executionMode: 'workspace_managed' },
        partner: { executionMode: 'inherit' },
        toolAgent: { executionMode: 'inherit' },
      },
      executionMode: 'workspace_managed',
      routingPolicy: 'moderator',
      moderatorParticipantId: 'participant-1',
      workspaceRoots: ['/Volumes/work/learnA'],
      createdAtMs: 1,
      updatedAtMs: 2,
      lastEventSequence: lastSequence,
      participants: [
        roomParticipant('participant-1', 'session-room-1', 0),
        roomParticipant('participant-2', 'session-room-2', 1),
      ],
    },
    events,
    firstSequence,
    lastSequence,
    resumeToken: lastSequence ? `room-1:${lastSequence}` : '',
    truncated: options.truncated ?? false,
  };
}

function roomConversationSnapshotFixture(
  events: Record<string, unknown>[],
  cursorSequence: number,
  deferredEventCount: number,
) {
  const room = roomSnapshotFixture([]).room;
  return {
    schemaVersion: 'rag-ime.agent-room-conversation-snapshot.v1',
    ok: true,
    room: { ...room, lastEventSequence: cursorSequence },
    events,
    firstEventSequence: Number(events[0]?.sequence ?? 0),
    cursorSequence,
    resumeToken: cursorSequence ? `room-1:${cursorSequence}` : '',
    deferredEventCount,
    truncated: false,
  };
}

function roomServerMessage(id: string, text: string) {
  return {
    schemaVersion: 'rag-ime.agent-message.v1',
    id,
    sessionId: 'session-room-1',
    turnId: 'room-turn-1',
    role: 'assistant',
    status: 'completed',
    blocks: [
      {
        id: `${id}:text`,
        type: 'text',
        status: 'completed',
        presentationKind: 'markdown',
        data: { text },
      },
    ],
    attachments: [],
    citations: [],
    createdAtMs: 10,
    completedAtMs: 20,
  };
}

function roomPost(postId: string, content: string, dispatchId: string) {
  return {
    schemaVersion: 'wisdom-weasel.room-post.v2',
    postId,
    roomId: 'room-1',
    rootId: 'room-turn-1',
    generation: 0,
    ...(dispatchId ? { dispatchId } : {}),
    authorActorRef: 'participant-1',
    kind: 'result',
    visibility: 'room',
    content,
    idempotencyKey: postId,
    publicationSource: { kind: 'room_commit', ref: `commit:${postId}` },
    createdAtMs: 20,
  };
}

function formalTerminalOrdering(
  order: readonly ('final' | 'partner-terminal' | 'coordinator-terminal' | 'root-terminal')[],
) {
  const coordinatorRoute = wireRoomEvent(1, 'route_decision', {
    rootId: 'room-turn-1',
    dispatchId: 'dispatch-coordinator',
    targetParticipantId: 'participant-1',
  });
  const partnerRoute = wireRoomEvent(2, 'route_decision', {
    rootId: 'room-turn-1',
    dispatchId: 'dispatch-partner',
    targetParticipantId: 'participant-2',
  });
  partnerRoute.participantId = 'participant-2';
  partnerRoute.sourceSessionId = 'session-room-2';
  const partnerResult = wireRoomEvent(3, 'room_post', {
    post: formalRoomPost(
      'partner-work-result',
      '伙伴交付证据',
      'dispatch-partner',
      'work_result',
      'participant-2',
    ),
  });
  partnerResult.participantId = 'participant-2';
  partnerResult.sourceSessionId = 'session-room-2';

  const terminalEvent = (name: (typeof order)[number], sequence: number) => {
    if (name === 'final') {
      return wireRoomEvent(sequence, 'room_post', {
        post: formalRoomPost(
          'coordinator-final',
          '主持者最终报告',
          'dispatch-coordinator',
          'result',
        ),
      });
    }
    if (name === 'partner-terminal') {
      const event = wireRoomEvent(sequence, 'turn_completed', {
        rootId: 'room-turn-1',
        dispatchId: 'dispatch-partner',
        status: 'completed',
      });
      event.participantId = 'participant-2';
      event.sourceSessionId = 'session-room-2';
      return event;
    }
    if (name === 'coordinator-terminal') {
      return wireRoomEvent(sequence, 'turn_completed', {
        rootId: 'room-turn-1',
        dispatchId: 'dispatch-coordinator',
        status: 'completed',
      });
    }
    const event = wireRoomEvent(sequence, 'turn_completed', {
      rootId: 'room-turn-1',
      status: 'completed',
    });
    event.participantId = null;
    event.sourceSessionId = '';
    return event;
  };

  return [
    coordinatorRoute,
    partnerRoute,
    partnerResult,
    ...order.map((name, index) => terminalEvent(name, index + 4)),
  ];
}

function formalRoomPost(
  postId: string,
  content: string,
  dispatchId: string,
  kind: 'result' | 'work_result',
  authorParticipantId = 'participant-1',
) {
  return {
    ...roomPost(postId, content, dispatchId),
    authorActorRef: authorParticipantId,
    kind,
    publicationSource: {
      kind: kind === 'work_result' ? 'room_post' : 'runtime_projection',
      ref: `${kind}:${postId}`,
    },
    ...(kind === 'work_result' ? {
      workResult: {
        proposedOperabilityVerdict: 'passed',
        proposedRequirementVerdict: 'satisfied',
      },
    } : {}),
  };
}

function roomParticipant(id: string, sessionId: string, ordinal: number) {
  return {
    schemaVersion: 'rag-ime.agent-participant.v1',
    id,
    roomId: 'room-1',
    sessionId,
    roleId: ordinal === 0 ? 'companion-present-v1' : 'companion-firstlight-v1',
    roleVersion: '1',
    displayName: ordinal === 0 ? '澄·今' : '澄·初',
    collaborationRole: ordinal === 0 ? 'coordinator' : 'researcher',
    status: 'active',
    ordinal,
    createdAtMs: 1,
    lastSpokeAtMs: null,
  };
}
