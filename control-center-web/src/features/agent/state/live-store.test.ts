import { afterEach, describe, expect, it } from 'vitest';
import type { AgentSnapshot } from '@/contracts/agent-reducer';
import { parseAgentEvent } from '@/contracts/validators';
import { useAgentLiveStore } from './live-store';

const sessionId = 'session-room-managed';

afterEach(() => {
  useAgentLiveStore.getState().reset();
});

describe('Agent live store snapshot hydration', () => {
  it('keeps a newer confirmed projection when reconnect hydration returns an older snapshot', () => {
    const confirmedSnapshot: AgentSnapshot = {
      messages: [],
      liveEvents: [],
      lastSequence: 8,
      resumeToken: `${sessionId}:8`,
      status: 'working',
    };
    const store = useAgentLiveStore.getState();
    store.hydrateSnapshot(sessionId, confirmedSnapshot);
    const confirmed = useAgentLiveStore.getState().projections[sessionId];

    store.hydrateSnapshot(sessionId, {
      ...confirmedSnapshot,
      lastSequence: 5,
      resumeToken: `${sessionId}:5`,
      status: 'idle',
    });

    expect(useAgentLiveStore.getState().projections[sessionId]).toBe(confirmed);
    expect(useAgentLiveStore.getState().projections[sessionId]).toMatchObject({
      lastSequence: 8,
      resumeToken: `${sessionId}:8`,
      status: 'working',
    });
  });

  it('does not let an equal-cursor busy snapshot revive a completed live turn', () => {
    const store = useAgentLiveStore.getState();
    const delta = event(1, 'turn-terminal', 'text_delta', {
      delta: '已经完成的回答',
      replaceBlock: true,
    });
    store.applyEvents(sessionId, [
      delta,
      event(2, 'turn-terminal', 'turn_completed', { status: 'completed' }),
    ]);
    const terminal = useAgentLiveStore.getState().projections[sessionId];
    expect(terminal.status).toBe('idle');
    expect(terminal.turnsById['turn-terminal']?.status).toBe('completed');

    store.hydrateSnapshot(sessionId, {
      messages: [],
      liveEvents: [delta],
      lastSequence: 2,
      resumeToken: `${sessionId}:2`,
      snapshotScope: 'recent',
      partial: true,
      runtimeQuiescent: false,
      status: 'busy',
    });

    const after = useAgentLiveStore.getState().projections[sessionId];
    expect(after).toBe(terminal);
    expect(after.status).toBe('idle');
    expect(after.turnsById['turn-terminal']?.status).toBe('completed');
  });

  it('does not replace durable history with a newer empty full snapshot', () => {
    const store = useAgentLiveStore.getState();
    store.hydrateSnapshot(sessionId, {
      messages: [
        message('history-user', 'user', 'history', '之前的消息'),
        message('history-assistant', 'assistant', 'history', '之前的回复'),
      ],
      liveEvents: [],
      lastSequence: 10,
      resumeToken: `${sessionId}:10`,
      status: 'idle',
    });
    const before = useAgentLiveStore.getState().projections[sessionId];

    store.hydrateSnapshot(sessionId, {
      messages: [],
      liveEvents: [],
      lastSequence: 11,
      resumeToken: `${sessionId}:11`,
      status: 'idle',
    });

    const after = useAgentLiveStore.getState().projections[sessionId];
    expect(after).not.toBe(before);
    expect(after.lastSequence).toBe(11);
    expect(after.messageOrder).toEqual(['history-user', 'history-assistant']);
    expect(after.messagesById['history-assistant']).toBeDefined();
  });

  it('does not replace cached history with an empty recent snapshot', () => {
    const store = useAgentLiveStore.getState();
    store.hydrateSnapshot(sessionId, {
      messages: [message('recent-cached', 'assistant', 'history', '缓存中的回答')],
      liveEvents: [],
      lastSequence: 20,
      resumeToken: `${sessionId}:20`,
      status: 'idle',
    });

    store.hydrateSnapshot(sessionId, {
      messages: [],
      liveEvents: [],
      lastSequence: 21,
      resumeToken: `${sessionId}:21`,
      snapshotScope: 'recent',
      partial: true,
      status: 'active',
    });

    const after = useAgentLiveStore.getState().projections[sessionId];
    expect(after.messageOrder).toEqual(['recent-cached']);
    expect(after.status).toBe('active');
  });

  it('resets every projection when an authenticated Team scope is replaced', () => {
    const store = useAgentLiveStore.getState();
    store.ensure('scope-a-session');
    store.ensure('scope-b-session');
    expect(Object.keys(useAgentLiveStore.getState().projections)).toEqual([
      'scope-a-session',
      'scope-b-session',
    ]);

    store.reset();

    expect(useAgentLiveStore.getState().projections).toEqual({});
  });

  it.each([true, false])('restores activity-only turns consistently with snapshot quiescence (%s)', (runtimeQuiescent) => {
    const store = useAgentLiveStore.getState();
    store.hydrateSnapshot(sessionId, {
      messages: [message('old-answer', 'assistant', 'old-turn', '保留历史')],
      liveEvents: [], lastSequence: 1, resumeToken: `${sessionId}:1`, status: 'idle',
    });
    store.applyEvents(sessionId, [event(2, 'tool-only-turn', 'tool_started', {
      toolCallId: 'call-1', toolName: 'bash',
    })]);
    expect(useAgentLiveStore.getState().projections[sessionId].turnsById['tool-only-turn']?.status).toBe('running');

    store.hydrateSnapshot(sessionId, {
      messages: [], liveEvents: [], lastSequence: 3, resumeToken: `${sessionId}:3`,
      status: 'idle', snapshotScope: 'recent', partial: true, runtimeQuiescent,
    });
    const state = useAgentLiveStore.getState().projections[sessionId];
    expect(state.messagesById['old-answer']).toBeDefined();
    expect(state.turnsById['tool-only-turn']?.status).toBe(runtimeQuiescent ? 'completed' : 'running');
    expect(Object.values(state.activitiesById).find((activity) => activity.turnId === 'tool-only-turn')?.status)
      .toBe(runtimeQuiescent ? 'completed' : 'running');
  });

  it('settles restored historical activities without completing a new unaccepted prompt', () => {
    const store = useAgentLiveStore.getState();
    store.hydrateSnapshot(sessionId, {
      messages: [message('old-answer', 'assistant', 'old-turn', '保留历史')],
      liveEvents: [], lastSequence: 1, resumeToken: `${sessionId}:1`, status: 'idle',
    });
    store.applyEvents(sessionId, [event(2, 'tool-only-turn', 'tool_started', {
      toolCallId: 'old-call', toolName: 'bash',
    })]);
    store.appendOptimistic(sessionId, { clientMessageId: 'new-prompt', text: '新的任务', nowMs: 40 });
    const before = useAgentLiveStore.getState().projections[sessionId];
    const pendingId = before.optimisticByClientMessageId['new-prompt'];
    const pendingTurnId = before.messagesById[pendingId].turnId;

    store.hydrateSnapshot(sessionId, {
      messages: [], liveEvents: [], lastSequence: 3, resumeToken: `${sessionId}:3`,
      status: 'idle', snapshotScope: 'recent', partial: true, runtimeQuiescent: true,
    });
    const state = useAgentLiveStore.getState().projections[sessionId];
    expect(state.turnsById['tool-only-turn']?.status).toBe('completed');
    expect(state.turnsById[pendingTurnId]?.status).toBe(before.turnsById[pendingTurnId].status);
    expect(state.optimisticByClientMessageId['new-prompt']).toBe(pendingId);
    expect(state.status).toBe('busy');
  });

  it('does not leave an equal-cursor history alias active after the real turn completes', () => {
    const clientMessageId = 'client-equal-cursor-race';
    const store = useAgentLiveStore.getState();
    store.appendOptimistic(sessionId, {
      clientMessageId,
      text: '检查完成后告诉我结果',
      nowMs: 10,
    });
    store.applyEvents(sessionId, [event(1, 'turn-real', 'message_completed', {
      clientMessageId,
      message: message('runtime-user', 'user', 'turn-real', '检查完成后告诉我结果'),
    })]);
    store.hydrateSnapshot(sessionId, {
      messages: [message('history-user', 'user', 'history', '检查完成后告诉我结果')],
      liveEvents: [],
      lastSequence: 1,
      resumeToken: `${sessionId}:1`,
      status: 'busy',
    });
    store.applyEvents(sessionId, [
      event(2, 'turn-real', 'message_completed', {
        message: message('runtime-assistant', 'assistant', 'turn-real', '检查完成。'),
      }),
      event(3, 'turn-real', 'turn_completed', { status: 'completed' }),
    ]);

    const settled = useAgentLiveStore.getState().projections[sessionId];
    expect(settled.status).toBe('idle');
    expect(settled.turnsById['turn-real']?.status).toBe('completed');
    expect(settled.turnsById['history:history-user']).toBeUndefined();
    expect(settled.turnOrder.filter((turnId) => (
      ['queued', 'running', 'waiting'].includes(settled.turnsById[turnId]?.status ?? '')
    ))).toEqual([]);
  });

  it.each(['failed', 'faulted'])('settles an orphan tool as failed when the Runtime snapshot is %s', (status) => {
    const store = useAgentLiveStore.getState();
    store.hydrateSnapshot(sessionId, {
      messages: [message('old-answer', 'assistant', 'old-turn', '保留已完成的历史')],
      liveEvents: [], lastSequence: 1, resumeToken: `${sessionId}:1`, status: 'idle',
    });
    store.applyEvents(sessionId, [event(2, 'lost-terminal', 'tool_started', {
      toolCallId: 'orphan-call', toolName: 'bash',
    })]);
    store.hydrateSnapshot(sessionId, {
      messages: [], liveEvents: [], lastSequence: 3, resumeToken: `${sessionId}:3`,
      status, snapshotScope: 'recent', partial: true, runtimeQuiescent: true,
    });
    const state = useAgentLiveStore.getState().projections[sessionId];
    expect(state.status).toBe(status);
    expect(state.turnsById['lost-terminal']?.status).toBe('failed');
    expect(Object.values(state.activitiesById).find((activity) => activity.turnId === 'lost-terminal')?.status).toBe('failed');
    expect(state.messagesById['old-answer']?.status).toBe('completed');
    expect(state.turnOrder.some((id) => ['queued', 'running', 'waiting'].includes(state.turnsById[id]!.status))).toBe(false);
  });
});

function event(
  sequence: number,
  turnId: string,
  eventType: string,
  payload: Record<string, unknown>,
) {
  return parseAgentEvent({
    schemaVersion: 'rag-ime.agent-event.v1',
    eventId: `${sessionId}:${sequence}`,
    sessionId,
    turnId,
    sequence,
    createdAtMs: sequence * 10,
    eventType,
    payload,
    resumeToken: `${sessionId}:${sequence}`,
  });
}

function message(
  id: string,
  role: 'user' | 'assistant',
  turnId: string,
  text: string,
) {
  return {
    schemaVersion: 'rag-ime.agent-message.v1',
    id,
    sessionId,
    turnId,
    role,
    status: 'completed',
    blocks: [{
      id: `${id}:text`,
      type: 'text',
      status: 'completed',
      presentationKind: 'markdown',
      data: { text },
    }],
    attachments: [],
    citations: [],
    createdAtMs: 20,
    completedAtMs: 21,
  };
}
