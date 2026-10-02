import { afterEach, describe, expect, it } from 'vitest';
import type { AgentSnapshot } from '@/contracts/agent-reducer';
import { parseAgentEvent } from '@/contracts/validators';
import { useAgentLiveStore } from './live-store';

const sessionId = 'session-room-managed';

afterEach(() => {
  useAgentLiveStore.getState().clear(sessionId);
});

describe('Agent live store snapshot hydration', () => {
  it('requires the reset owner and preserves only unresolved local admissions across a reset', () => {
    const store = useAgentLiveStore.getState();
    store.hydrateSnapshot(sessionId, {
      messages: [message('old', 'assistant', 'old-turn', 'old epoch')],
      liveEvents: [], lastSequence: 10, resumeToken: `${sessionId}:10`, status: 'idle',
    });
    store.appendOptimistic(sessionId, { clientMessageId: 'pending', text: 'not yet confirmed', nowMs: 11 });
    store.applyEvents(sessionId, [{
      ...event(6, '', 'snapshot_required', { reason: 'event_replay_gap' }),
      eventId: `${sessionId}:snapshot-required:5`, resumeToken: `${sessionId}:snapshot-required:5`,
    }]);
    const before = useAgentLiveStore.getState().projections[sessionId];
    const replacement: AgentSnapshot = {
      messages: [], liveEvents: [], lastSequence: 5, resumeToken: `${sessionId}:5`, status: 'idle',
    };
    expect(store.hydrateSnapshot(sessionId, replacement)).toBe(false);
    expect(store.hydrateSnapshot(sessionId, { ...replacement, lastSequence: 20 })).toBe(false);
    expect(useAgentLiveStore.getState().projections[sessionId]).toBe(before);
    expect(store.hydrateSnapshot(sessionId, replacement, { recoveryCursor: 5 })).toBe(true);
    const after = useAgentLiveStore.getState().projections[sessionId];
    expect(after).toMatchObject({ lastSequence: 5, needsSnapshot: false });
    expect(after.messagesById.old).toBeUndefined();
    expect(after.messagesById[after.optimisticByClientMessageId.pending]?.blocks[0]?.data.text).toBe('not yet confirmed');
    expect(after.recoveryCursor).toBeUndefined();
  });

  it('an ordinary gap or malformed reset identity cannot authorize a lower snapshot', () => {
    const store = useAgentLiveStore.getState();
    store.hydrateSnapshot(sessionId, { messages: [], liveEvents: [], lastSequence: 10, resumeToken: `${sessionId}:10` });
    store.applyEvents(sessionId, [event(6, '', 'snapshot_required', {})]);
    expect(store.hydrateSnapshot(sessionId, {
      messages: [], liveEvents: [], lastSequence: 5, resumeToken: `${sessionId}:5`,
    }, { recoveryCursor: 5 })).toBe(false);
    expect(useAgentLiveStore.getState().projections[sessionId]).toMatchObject({ lastSequence: 10, needsSnapshot: true });
  });

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

  it('imports history arriving behind SSE without rewinding the live turn or cursor', () => {
    const store = useAgentLiveStore.getState();
    store.applyEvents(sessionId, [event(1, 'live', 'text_delta', { delta: '正在生成' })]);
    const current = useAgentLiveStore.getState().projections[sessionId];
    expect(store.hydrateSnapshot(sessionId, {
      messages: [message('old', 'assistant', 'old-turn', '旧回答')],
      liveEvents: [], lastSequence: 0, resumeToken: `${sessionId}:0`, status: 'idle',
    })).toBe(true);
    const next = useAgentLiveStore.getState().projections[sessionId];
    expect(next.messagesById.old).toBeDefined();
    expect(next.messagesById['live:assistant']).toEqual(current.messagesById['live:assistant']);
    expect(next.turnsById.live.status).toBe('running');
    expect(next.status).toBe(current.status);
    expect(next.lastSequence).toBe(1);
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

  it('retains dozens of loaded tool receipts when a nonempty recent window rolls forward', () => {
    const store = useAgentLiveStore.getState();
    const tools = Array.from({ length: 40 }, (_, i) => event(i + 1, 'history', 'tool_finished', {
      toolCallId: `call-${i}`, toolName: 'read', publicResult: { summary: `result-${i}` },
    }));
    store.hydrateSnapshot(sessionId, {
      messages: [message('old', 'assistant', 'history', '已读取')],
      liveEvents: tools, lastSequence: 40, resumeToken: `${sessionId}:40`, status: 'idle',
    });
    const ids = [...useAgentLiveStore.getState().projections[sessionId].activityOrder];
    expect(ids).toHaveLength(40);
    store.hydrateSnapshot(sessionId, {
      messages: [message('new', 'user', 'next', '继续')], liveEvents: [],
      lastSequence: 41, resumeToken: `${sessionId}:41`, status: 'busy', snapshotScope: 'recent', partial: true,
    });
    const state = useAgentLiveStore.getState().projections[sessionId];
    expect(state.activityOrder).toEqual(ids);
    expect(state.messageOrder).toEqual(['old', 'new']);
    expect(state.turnsById.history.activityIds).toEqual(ids);
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
