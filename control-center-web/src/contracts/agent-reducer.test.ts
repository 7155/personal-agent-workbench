import { describe, expect, it } from 'vitest';

import {
  abortAgentTurn,
  acknowledgeOptimisticAgentMessage,
  appendOptimisticAgentMessage,
  applyAgentBackgroundJobReceipt,
  applyAgentSnapshot,
  agentSnapshotFromResponse,
  createAgentProjection,
  discardOptimisticAgentMessage,
  failOptimisticAgentMessage,
  requeueOptimisticAgentMessage,
  reduceAgentEvent,
  rewriteOptimisticAgentMessage,
  resolveAgentTurnUserMessage,
  reduceAgentEvents,
  mergeAgentSnapshotHistory,
  type AgentTodoProjection,
} from './agent-reducer';
import { parseAgentEvent } from './validators';
import { agentEventFixture as agentEvent } from '@/test/fixtures/events';
import type { AgentBackgroundJobV1 } from './generated/agent-background-job.v1';
import type { AgentLifecycleCancellationAuditV1 } from './generated/agent-lifecycle-cancellation-audit.v1';

describe('AgentEventReducer', () => {
  it('retains exact Durable paused input and tool receipts without settling the turn', () => {
    const snapshot = agentSnapshotFromResponse({
      sessionId: 'session-1', runtimeEngine: 'durable', projectionCurrent: true,
      paused: true, recoverable: true, activeTurn: { turnId: 'turn-1', clientMessageId: 'original-input' },
      items: [serverMessage('paused-user', 'user', 'turn-1', '保留原任务')],
      liveEvents: [agentEvent(1, 'tool_started', { toolCallId: 'kept-tool', toolName: 'read' }),
        agentEvent(2, 'tool_finished', { toolCallId: 'kept-tool', toolName: 'read', outputPreview: '已保存结果' })],
      status: 'busy', lastSequence: 2, resumeToken: 'session-1:2',
    });
    const state = applyAgentSnapshot(createAgentProjection('session-1'), snapshot);
    expect(state.durableRecovery).toEqual({ paused: true, recoverable: true, activeTurn: { turnId: 'turn-1', clientMessageId: 'original-input' } });
    expect(state.runtimeEngine).toBe('durable');
    expect(['running', 'waiting', 'queued']).toContain(state.turnsById['turn-1'].status);
    expect(state.activitiesById[state.activityOrder[0]!].status).toBe('completed');
    expect(state.messagesById['paused-user'].blocks[0]?.data.text).toBe('保留原任务');
    const terminal = reduceAgentEvent(state, agentEvent(3, 'turn_completed', { status: 'completed' })).state;
    expect(terminal.durableRecovery?.recoverable).toBe(false);
    expect(terminal.durableRecovery?.paused).toBe(false);
    expect(terminal.turnsById['turn-1'].status).toBe('completed');
  });

  it.each([
    { projectionCurrent: false }, { sessionId: 'another-session' }, { sessionId: undefined },
    { activeTurn: { turnId: 'turn-1' } }, { recoverable: 'true' }, { paused: undefined },
  ])('does not invent Durable recovery authority from incomplete or foreign metadata %j', override => {
    const state = applyAgentSnapshot(createAgentProjection('session-1'), agentSnapshotFromResponse({
      sessionId: 'session-1', runtimeEngine: 'durable', projectionCurrent: true,
      paused: true, recoverable: true, activeTurn: { turnId: 'turn-1', clientMessageId: 'original-input' },
      items: [], liveEvents: [], status: 'busy', lastSequence: 0, ...override,
    }));
    expect(state.durableRecovery).toBeUndefined();
  });

  it.each(['turn_completed', 'turn_failed'] as const)(
    'keeps the successor status when %s arrives for an older turn',
    (terminal) => {
      for (const successor of ['optimistic', 'running', 'completed'] as const) {
        let state = reduceAgentEvent(createAgentProjection('session-1'), agentEvent(1, 'status_changed', { status: 'busy' })).state;
        state = appendOptimisticAgentMessage(state, {
          clientMessageId: 'next', text: 'new work', nowMs: 20, turnId: 'turn-new',
        });
        let sequence = 2;
        if (successor !== 'optimistic') {
          state = reduceAgentEvent(state, {
            ...agentEvent(sequence++, successor === 'running' ? 'status_changed' : 'turn_completed', { status: successor === 'running' ? 'busy' : 'completed' }),
            turnId: 'turn-new',
          }).state;
        }
        state = reduceAgentEvent(state, agentEvent(sequence, terminal,
          terminal === 'turn_failed' ? { error: 'old error' } : { status: 'completed' })).state;
        expect(state.turnsById['turn-1'].status).toBe(terminal === 'turn_failed' ? 'failed' : 'completed');
        expect(state.turnsById['turn-new'].status).toBe(successor === 'optimistic' ? 'queued' : successor);
        expect(state.status).toBe(successor === 'completed' ? 'idle' : 'busy');
      }
    },
  );

  it.each(['turn_completed', 'turn_failed'] as const)(
    'does not let an unknown %s acquire the current status owner',
    (terminal) => {
      const state = appendOptimisticAgentMessage(createAgentProjection('session-1'), {
        clientMessageId: 'current', text: 'current work', nowMs: 20, turnId: 'turn-current',
      });
      const settled = reduceAgentEvent(state, {
        ...agentEvent(1, terminal, { error: 'unknown older outcome' }), turnId: 'unknown-old',
      }).state;
      expect(settled.turnsById['unknown-old'].status).toBe(terminal === 'turn_failed' ? 'failed' : 'completed');
      expect(settled.turnsById['turn-current'].status).toBe('queued');
      expect(settled.status).toBe('busy');
      expect(settled.turnOrder.at(-1)).toBe('turn-current');
      const currentFinished = reduceAgentEvent(settled, {
        ...agentEvent(2, 'turn_completed', {}), turnId: 'turn-current',
      }).state;
      expect(currentFinished.status).toBe('idle');
    },
  );

  it('retains a current failure when an unknown old completion is projected', () => {
    const current = reduceAgentEvent(createAgentProjection('session-1'), {
      ...agentEvent(1, 'turn_failed', { error: 'current failure' }), turnId: 'turn-current',
    }).state;
    const settled = reduceAgentEvent(current, {
      ...agentEvent(2, 'turn_completed', {}), turnId: 'unknown-old',
    }).state;
    expect(settled.status).toBe('failed');
    expect(settled.turnOrder.at(-1)).toBe('turn-current');
    expect(settled.turnsById['turn-current'].failure).toBe('current failure');
  });

  it('keeps a failed provider attempt running while a snapshot says Pi is retrying', () => {
    const snapshot = {
      messages: [{ ...serverMessage('attempt', 'assistant', 'turn-1', 'overloaded'), status: 'failed' }],
      liveEvents: [agentEvent(1, 'status_changed', {
        status: 'retrying', phase: 'provider_retry', activityState: 'running',
        summary: '正在自动重试', attempt: 7, maxAttempts: 7,
      })],
      status: 'busy', partial: true, runtimeQuiescent: false,
      lastSequence: 1, resumeToken: 'session-1:1',
    };
    const running = applyAgentSnapshot(createAgentProjection('session-1'), snapshot);
    expect(running.turnsById['turn-1'].status).toBe('running');
    const settled = applyAgentSnapshot(running, { ...snapshot, status: 'failed', runtimeQuiescent: true });
    expect(settled.turnsById['turn-1'].status).toBe('failed');
  });

  it('does not mark a settled user-only snapshot as a completed turn', () => {
    const state = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [
        serverMessage(
          'user-without-answer',
          'user',
          'turn-without-answer',
          '我想给 Agent 对话加 TUI 模式',
        ),
      ],
      liveEvents: [],
      lastSequence: 1,
      resumeToken: 'session-1:1',
      status: 'idle',
    });

    expect(state.turnsById['turn-without-answer']).toMatchObject({
      status: 'failed',
      failure: '未收到助手回复。',
    });
  });

  it('recovers retry input for an activity-only failed turn', () => {
    const state = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [
        serverMessage(
          'user-before-failure',
          'user',
          'turn-with-input',
          '继续完成这轮修复',
        ),
      ],
      liveEvents: [],
      lastSequence: 1,
      resumeToken: 'session-1:1',
      status: 'idle',
    });
    state.turnsById['turn-activity-failure'] = {
      id: 'turn-activity-failure',
      status: 'failed',
      messageIds: [],
      activityIds: ['activity:failed'],
      createdAtMs: 30,
      updatedAtMs: 31,
      failure: 'Session 操作没有完成',
    };
    state.turnOrder.push('turn-activity-failure');

    expect(resolveAgentTurnUserMessage(state, 'turn-activity-failure')?.id)
      .toBe('user-before-failure');
  });

  it('prefers an ambiguous follow-up when retrying its failed active turn', () => {
    let state = appendOptimisticAgentMessage(createAgentProjection('session-1'), {
      clientMessageId: 'original-prompt',
      text: '正在执行原任务',
      turnId: 'turn-active',
      nowMs: 10,
    });
    state = appendOptimisticAgentMessage(state, {
      clientMessageId: 'ambiguous-follow-up',
      text: '完成后继续整理',
      turnId: 'turn-active',
      delivery: 'followUp',
      nowMs: 20,
    });
    state = failOptimisticAgentMessage(
      state,
      'ambiguous-follow-up',
      '暂时无法确认是否已接收',
      30,
      'ambiguous',
    );

    expect(resolveAgentTurnUserMessage(state, 'turn-active')).toMatchObject({
      clientMessageId: 'ambiguous-follow-up',
      admissionState: 'ambiguous',
      blocks: [expect.objectContaining({
        data: expect.objectContaining({ text: '完成后继续整理' }),
      })],
    });
  });

  it('does not treat a partial recent snapshot as terminal history', () => {
    const sessionId = 'session-recent-partial';
    const turnId = 'turn:recent-running';
    const state = applyAgentSnapshot(
      createAgentProjection(sessionId),
      agentSnapshotFromResponse({
        messages: [],
        liveEvents: [{
          schemaVersion: 'rag-ime.agent-event.v1',
          eventId: `${sessionId}:40`,
          sessionId,
          turnId,
          sequence: 40,
          createdAtMs: 1_000,
          eventType: 'tool_started',
          payload: {
            toolCallId: 'tool:recent-running',
            toolName: 'workspace_read',
            summary: '正在读取当前文件',
            args: { path: 'README.md' },
          },
          resumeToken: `${sessionId}:40`,
        }],
        lastSequence: 40,
        resumeToken: `${sessionId}:40`,
        snapshotScope: 'recent',
        partial: true,
        status: 'active',
      }),
    );

    expect(state.turnsById[turnId]?.status).toBe('running');
    expect(state.activitiesById['tool:recent-running']?.status).toBe('running');
  });

  it('settles stale running activity when the recent runtime projection is explicitly quiescent', () => {
    const sessionId = 'session-recent-quiescent';
    const turnId = 'turn:recent-stale';
    const state = applyAgentSnapshot(
      createAgentProjection(sessionId),
      agentSnapshotFromResponse({
        messages: [],
        liveEvents: [{
          schemaVersion: 'rag-ime.agent-event.v1',
          eventId: `${sessionId}:40`,
          sessionId,
          turnId,
          sequence: 40,
          createdAtMs: 1_000,
          eventType: 'tool_started',
          payload: {
            toolCallId: 'tool:recent-stale',
            toolName: 'workspace_read',
            summary: '已经停止但终态事件未被前台收到',
            args: { path: 'README.md' },
          },
          resumeToken: `${sessionId}:40`,
        }],
        lastSequence: 40,
        resumeToken: `${sessionId}:40`,
        snapshotScope: 'recent',
        partial: true,
        runtimeQuiescent: true,
        status: 'idle',
      }),
    );

    expect(state.status).toBe('idle');
    expect(state.turnsById[turnId]?.status).toBe('completed');
    expect(state.activitiesById['tool:recent-stale']?.status).toBe('completed');
  });
  it('merges a bounded recent transcript into cached history and applies its terminal status', () => {
    const sessionId = 'session-1';
    const cached = applyAgentSnapshot(createAgentProjection(sessionId), {
      messages: [
        serverMessage('history-user', 'user', 'turn-history', '之前的问题'),
        serverMessage('history-assistant', 'assistant', 'turn-history', '之前的回答'),
        serverMessage('active-user', 'user', 'turn-active', '正在处理的问题'),
      ],
      liveEvents: [],
      lastSequence: 10,
      resumeToken: `${sessionId}:10`,
      status: 'busy',
    });
    expect(cached.turnsById['turn-active']?.status).toBe('running');

    const settled = applyAgentSnapshot(cached, agentSnapshotFromResponse({
      messages: [
        serverMessage('active-assistant', 'assistant', 'turn-active', '处理完成的回答'),
      ],
      liveEvents: [],
      lastSequence: 11,
      resumeToken: `${sessionId}:11`,
      snapshotScope: 'recent',
      partial: true,
      runtimeQuiescent: true,
      status: 'idle',
    }));

    expect(settled.messageOrder).toEqual([
      'history-user',
      'history-assistant',
      'active-user',
      'active-assistant',
    ]);
    expect(textOf(settled.messagesById['history-assistant'])).toBe('之前的回答');
    expect(textOf(settled.messagesById['active-assistant'])).toBe('处理完成的回答');
    expect(settled.status).toBe('idle');
    expect(settled.turnsById['turn-active']?.status).toBe('completed');
  });


  it('applies ordered deltas and ignores replayed duplicates', () => {
    const initial = createAgentProjection('session-1');
    const first = reduceAgentEvent(initial, agentEvent(1, 'text_delta', { delta: '你' }));
    const second = reduceAgentEvent(first.state, agentEvent(2, 'text_delta', { delta: '好' }));
    const replay = reduceAgentEvent(second.state, agentEvent(2, 'text_delta', { delta: '好' }));

    expect(replay.disposition).toBe('ignored-duplicate');
    expect(replay.state).toBe(second.state);
    expect(textOf(second.state.messagesById['turn-1:assistant'])).toBe('你好');
  });

  it('replaces one live assistant bubble when a mixed Tool message has no provider deltas', () => {
    const first = reduceAgentEvent(
      createAgentProjection('session-1'),
      agentEvent(1, 'text_delta', {
        delta: '已经找到结构，继续核对。',
        replaceContent: true,
      }),
    ).state;
    const second = reduceAgentEvent(
      first,
      agentEvent(2, 'text_delta', {
        delta: '结构已经确认，正在整理结论。',
        replaceContent: true,
      }),
    ).state;

    expect(second.turnsById['turn-1'].messageIds).toEqual(['turn-1:assistant']);
    expect(textOf(second.messagesById['turn-1:assistant'])).toBe('结构已经确认，正在整理结论。');
    expect(second.messagesById['turn-1:assistant'].status).toBe('streaming');
  });

  it('coalesces a frame-sized delta burst without losing its exact cursor', () => {
    const events = Array.from({ length: 200 }, (_, index) => (
      agentEvent(index + 1, 'text_delta', {
        delta: String.fromCharCode(97 + (index % 26)),
        contentIndex: 0,
        replaceBlock: index === 0,
      })
    ));
    const state = reduceAgentEvents(createAgentProjection('session-1'), events);

    expect(textOf(state.messagesById['turn-1:assistant'])).toBe(
      events.map((event) => String(event.payload.delta)).join(''),
    );
    expect(state.lastSequence).toBe(200);
    expect(state.lastEventId).toBe('session-1:200');
    expect(state.resumeToken).toBe('session-1:200');
  });

  it('keeps replacement boundaries and gaps deterministic inside a delta batch', () => {
    const segmented = reduceAgentEvents(createAgentProjection('session-1'), [
      agentEvent(1, 'text_delta', { delta: '前', replaceBlock: true }),
      agentEvent(2, 'text_delta', { delta: '段' }),
      agentEvent(3, 'text_delta', { delta: '后', replaceBlock: true }),
      agentEvent(4, 'text_delta', { delta: '段' }),
    ]);
    expect(segmented.turnsById['turn-1'].messageIds).toEqual([
      'turn-1:assistant',
      'turn-1:assistant:segment:3',
    ]);
    expect(textOf(segmented.messagesById['turn-1:assistant'])).toBe('前段');
    expect(textOf(segmented.messagesById['turn-1:assistant:segment:3'])).toBe('后段');

    const initial = reduceAgentEvent(
      createAgentProjection('session-1'),
      agentEvent(1, 'status_changed', { status: 'working' }),
    ).state;
    const gapped = reduceAgentEvents(initial, [
      agentEvent(3, 'text_delta', { delta: '丢失前序' }),
      agentEvent(4, 'text_delta', { delta: '不得越过栅栏' }),
    ]);
    expect(gapped.needsSnapshot).toBe(true);
    expect(gapped.lastSequence).toBe(1);
    expect(gapped.gap).toMatchObject({
      expectedSequence: 2,
      receivedSequence: 3,
    });
  });

  it('keeps completed turns referentially stable while the active tail streams', () => {
    const hydrated = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [
        serverMessage('stable-user', 'user', 'stable-turn', '之前的问题'),
        serverMessage('stable-assistant', 'assistant', 'stable-turn', '之前的回答'),
        serverMessage('active-user', 'user', 'turn-1', '继续'),
      ],
      liveEvents: [],
      lastSequence: 0,
      resumeToken: '',
      status: 'responding',
    });
    const stableTurn = hydrated.turnsById['stable-turn'];
    const activeTurn = hydrated.turnsById['turn-1'];
    const stableMessage = hydrated.messagesById['stable-assistant'];

    const streamed = reduceAgentEvent(
      hydrated,
      agentEvent(1, 'text_delta', { delta: '新的流式内容' }),
    ).state;

    expect(streamed.turnsById['stable-turn']).toBe(stableTurn);
    expect(streamed.messagesById['stable-assistant']).toBe(stableMessage);
    expect(streamed.turnsById['turn-1']).not.toBe(activeTurn);
    expect(hydrated.turnsById['turn-1'].messageIds).not.toContain('turn-1:assistant');
    expect(streamed.turnsById['turn-1'].messageIds).toContain('turn-1:assistant');
  });

  it('optimistically removes the abandoned future when rewriting a durable user message', () => {
    const hydrated = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [
        serverMessage('user-1', 'user', 'turn-old-1', '第一问'),
        serverMessage('assistant-1', 'assistant', 'turn-old-1', '第一答'),
        serverMessage('user-2', 'user', 'turn-old-2', '需要修改的第二问'),
        serverMessage('assistant-2', 'assistant', 'turn-old-2', '将被放弃的第二答'),
        serverMessage('user-3', 'user', 'turn-old-3', '将被放弃的第三问'),
        serverMessage('assistant-3', 'assistant', 'turn-old-3', '将被放弃的第三答'),
      ],
      liveEvents: [],
      lastSequence: 99,
      resumeToken: 'session-1:99',
      status: 'idle',
    });

    const rewritten = rewriteOptimisticAgentMessage(hydrated, 'user-2', {
      clientMessageId: 'rewrite-1',
      text: '修改后的第二问',
      nowMs: 1_000,
    });

    expect(rewritten.messageOrder).toEqual([
      'user-1',
      'assistant-1',
      'local:rewrite-1',
    ]);
    expect(rewritten.turnOrder).toEqual(['turn-old-1', 'local-turn:rewrite-1']);
    expect(rewritten.messagesById['assistant-2']).toBeUndefined();
    expect(rewritten.messagesById['user-3']).toBeUndefined();
    expect(textOf(rewritten.messagesById['local:rewrite-1'])).toBe('修改后的第二问');
    expect(rewritten.status).toBe('busy');
    expect(rewritten.lastSequence).toBe(99);
  });

  it('preserves assistant segments around a tool call instead of replacing earlier text', () => {
    let state = createAgentProjection('session-1');
    const events = [
      agentEvent(1, 'text_delta', { delta: '我先检查运行状态。', replaceBlock: true }),
      agentEvent(2, 'message_completed', {
        message: serverMessage('turn-1:assistant', 'assistant', 'turn-1', '我先检查运行状态。'),
      }),
      agentEvent(3, 'tool_started', {
        toolCallId: 'tool-interleaved',
        toolName: 'overview',
      }),
      agentEvent(4, 'tool_finished', {
        toolCallId: 'tool-interleaved',
        toolName: 'overview',
        result: { details: { result: { summary: '运行状态正常' } } },
      }),
      agentEvent(5, 'text_delta', { delta: '检查完成，一切正常。', replaceBlock: true }),
      agentEvent(6, 'message_completed', {
        message: serverMessage('turn-1:assistant', 'assistant', 'turn-1', '检查完成，一切正常。'),
      }),
      agentEvent(7, 'turn_completed', { status: 'completed' }),
    ];
    for (const event of events) state = reduceAgentEvent(state, event).state;

    expect(state.turnsById['turn-1'].messageIds).toEqual([
      'turn-1:assistant',
      'turn-1:assistant:segment:5',
    ]);
    expect(textOf(state.messagesById['turn-1:assistant'])).toBe('我先检查运行状态。');
    expect(textOf(state.messagesById['turn-1:assistant:segment:5'])).toBe('检查完成，一切正常。');
    expect(state.messagesById['turn-1:assistant']).toMatchObject({ createdAtMs: 10, timelineSequence: 1 });
    expect(state.messagesById['turn-1:assistant:segment:5']).toMatchObject({ createdAtMs: 50, timelineSequence: 5 });
    expect(state.activitiesById['tool-interleaved']).toMatchObject({
      createdAtMs: 30,
      timelineSequence: 3,
      status: 'completed',
    });
  });

  it('keeps durable transcript order when snapshot clocks drift', () => {
    const state = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [
        { ...serverMessage('history-user', 'user', 'turn-1', '先问'), timelineSequence: 1 },
        { ...serverMessage('history-answer', 'assistant', 'turn-1', '后答'), timelineSequence: 4 },
      ],
      liveEvents: [{
        ...agentEvent(7, 'tool_finished', {
          toolCallId: 'tool-durable',
          toolName: 'workspace_read',
          result: { details: { summary: '读取完成' } },
        }),
        timelineSequence: 3.8,
      }],
      lastSequence: 20,
      resumeToken: 'session-1:20',
      status: 'idle',
    });

    expect(state.messagesById['history-user']?.timelineSequence).toBe(1);
    expect(state.activitiesById['tool-durable']?.timelineSequence).toBe(3.8);
    expect(state.messagesById['history-answer']?.timelineSequence).toBe(4);
  });

  it('stops projection on a sequence gap until a snapshot is applied', () => {
    const first = reduceAgentEvent(
      createAgentProjection('session-1'),
      agentEvent(4, 'status_changed', { status: 'busy' }),
    );
    const gap = reduceAgentEvent(first.state, agentEvent(6, 'text_delta', { delta: 'lost' }));
    const pending = reduceAgentEvent(gap.state, agentEvent(7, 'text_delta', { delta: 'late' }));

    expect(gap.disposition).toBe('snapshot-required');
    expect(gap.state.gap).toMatchObject({ expectedSequence: 5, receivedSequence: 6 });
    expect(pending.disposition).toBe('ignored-snapshot-pending');

    const recovered = applyAgentSnapshot(gap.state, {
      messages: [serverMessage('server-user', 'user', 'turn-snapshot', '恢复后的问题')],
      liveEvents: [],
      lastSequence: 8,
      resumeToken: 'session-1:8',
    });
    expect(recovered.needsSnapshot).toBe(false);
    expect(recovered.lastSequence).toBe(8);
    expect(recovered.messageOrder).toEqual(['server-user']);
  });

  it('honors a snapshot-required control even when a restarted server sequence regresses', () => {
    const hydrated = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [],
      liveEvents: [],
      lastSequence: 12,
      resumeToken: 'session-1:12',
    });

    const control = reduceAgentEvent(
      hydrated,
      agentEvent(3, 'snapshot_required', { reason: 'event_replay_gap' }),
    );

    expect(control.disposition).toBe('snapshot-required');
    expect(control.state.needsSnapshot).toBe(true);
    expect(control.state.lastSequence).toBe(12);
    expect(control.state.gap).toMatchObject({
      expectedSequence: 13,
      receivedSequence: 3,
    });
  });

  it('projects queued steering and follow-up messages without replacing the active turn', () => {
    const active = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [serverMessage('active-user', 'user', 'turn-active', '继续检查')],
      liveEvents: [],
      lastSequence: 1,
      resumeToken: 'session-1:1',
      status: 'working',
      messageQueue: { steering: [], followUp: [] },
    });
    const optimistic = appendOptimisticAgentMessage(active, {
      clientMessageId: 'steer-1',
      text: '先不要修改配置',
      nowMs: 20,
      turnId: 'turn-active',
      delivery: 'steer',
    });
    const queued = reduceAgentEvent(
      optimistic,
      agentEvent(2, 'message_queue_updated', {
        steering: ['先不要修改配置'],
        followUp: ['完成后总结'],
      }),
    ).state;

    expect(optimistic.turnOrder).toEqual(['turn-active']);
    expect(optimistic.turnsById['turn-active']?.status).toBe('running');
    expect(optimistic.messagesById['local:steer-1']?.blocks[0]?.data.delivery).toBe('steer');
    expect(optimistic.messagesById['local:steer-1']?.deliveryState).toBe('sending');
    const accepted = acknowledgeOptimisticAgentMessage(optimistic, 'steer-1', 21);
    expect(accepted.messagesById['local:steer-1']?.deliveryState).toBe('accepted');
    expect(queued.messageQueue).toEqual({
      steering: ['先不要修改配置'],
      followUp: ['完成后总结'],
    });
    expect(queued.messagesById['local:steer-1']?.deliveryState).toBe('accepted');

    const applied = reduceAgentEvent(
      queued,
      agentEvent(3, 'message_queue_updated', {
        steering: [],
        followUp: ['完成后总结'],
      }),
    ).state;
    expect(applied.messagesById['local:steer-1']?.deliveryState).toBe('applied');

    const reconciled = reduceAgentEvent(
      applied,
      agentEvent(4, 'message_completed', {
        clientMessageId: 'steer-1',
        message: {
          ...serverMessage('server-steer-1', 'user', 'turn-active', '先不要修改配置'),
          clientMessageId: 'steer-1',
        },
      }),
    ).state;
    expect(reconciled.messagesById['server-steer-1']).toMatchObject({
      deliveryState: 'applied',
      blocks: [expect.objectContaining({
        data: expect.objectContaining({ delivery: 'steer' }),
      })],
    });

    const restored = applyAgentSnapshot(reconciled, {
      messages: [],
      liveEvents: [],
      lastSequence: 5,
      resumeToken: 'session-1:5',
      messageQueue: { steering: [], followUp: ['下一项任务'] },
    });
    expect(restored.messageQueue).toEqual({ steering: [], followUp: ['下一项任务'] });
  });

  it('projects provider retries as one live progress activity', () => {
    const started = reduceAgentEvent(
      createAgentProjection('session-1'),
      agentEvent(1, 'status_changed', {
        status: 'retrying',
        phase: 'provider_retry',
        activityState: 'running',
        summary: '模型连接暂时失败，正在自动重试（1/3）',
        attempt: 1,
        maxAttempts: 3,
        delayMs: 2_000,
      }),
    ).state;

    expect(started.status).toBe('retrying');
    expect(started.activitiesById['turn-1:status_changed']).toMatchObject({
      status: 'running',
      summary: '模型连接暂时失败，正在自动重试（1/3）',
    });

    const recovered = reduceAgentEvent(
      started,
      agentEvent(2, 'status_changed', {
        status: 'analyzing',
        phase: 'provider_retry',
        activityState: 'completed',
        summary: '模型连接已恢复，继续处理',
        attempt: 1,
        maxAttempts: 3,
        success: true,
      }),
    ).state;

    expect(recovered.status).toBe('analyzing');
    expect(recovered.activitiesById['turn-1:status_changed']).toMatchObject({
      status: 'completed',
      summary: '模型连接已恢复，继续处理',
    });
    expect(recovered.activityOrder).toEqual(['turn-1:status_changed']);
  });

  it('restores live tool and approval state from a snapshot without moving the SSE cursor', () => {
    const recovered = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [serverMessage('server-user', 'user', 'history:server-user', '执行检查')],
      liveEvents: [
        rawAgentEvent(41, 'tool_started', {
          toolCallId: 'tool-live-1',
          toolName: 'workspace_search',
          summary: '正在搜索工作区',
        }),
        {
          ...rawAgentEvent(42, 'approval_required', {
            approvalId: 'approval-live-1',
            payloadSha256: 'a'.repeat(64),
            operation: '写入文件',
          }),
          turnId: 'approval:approval-live-1',
        },
      ],
      lastSequence: 42,
      resumeToken: 'session-1:42',
      status: 'busy',
    });

    expect(recovered.lastSequence).toBe(42);
    expect(recovered.resumeToken).toBe('session-1:42');
    expect(recovered.activitiesById['tool-live-1']).toMatchObject({
      kind: 'tool_started',
      status: 'running',
    });
    expect(recovered.activitiesById['approval-live-1']).toMatchObject({
      kind: 'approval_required',
      status: 'waiting',
      turnId: 'approval:approval-live-1',
    });
    expect(recovered.status).toBe('waiting');
  });

  it('binds approval state to its owning tool call instead of creating another turn', () => {
    const started = reduceAgentEvent(
      createAgentProjection('session-1'),
      agentEvent(1, 'tool_started', {
        toolCallId: 'tool-bound-1',
        toolName: 'workspace_shell',
        args: { command: 'pwd' },
      }),
    ).state;
    const waiting = reduceAgentEvent(
      started,
      {
        ...agentEvent(2, 'approval_required', {
          approvalId: 'approval-bound-1',
          toolCallId: 'tool-bound-1',
          payloadSha256: 'a'.repeat(64),
          operation: 'run',
        }),
        turnId: 'approval:approval-bound-1',
      },
    ).state;

    expect(waiting.activityOrder).toEqual(['tool-bound-1']);
    expect(waiting.turnOrder).toEqual(['turn-1']);
    expect(waiting.activitiesById['tool-bound-1']).toMatchObject({
      kind: 'tool_started',
      status: 'waiting',
      turnId: 'turn-1',
      payload: {
        approvalId: 'approval-bound-1',
        toolCallId: 'tool-bound-1',
        toolName: 'workspace_shell',
        args: { command: 'pwd' },
      },
    });
    expect(waiting.activitiesById['approval-bound-1']).toBeUndefined();

    const resolved = reduceAgentEvent(
      waiting,
      {
        ...agentEvent(3, 'approval_resolved', {
          approvalId: 'approval-bound-1',
          toolCallId: 'tool-bound-1',
          state: 'rejected',
          automatic: true,
        }),
        turnId: 'approval:approval-bound-1',
      },
    ).state;
    expect(resolved.activityOrder).toEqual(['tool-bound-1']);
    expect(resolved.activitiesById['tool-bound-1']).toMatchObject({
      kind: 'tool_started',
      status: 'failed',
      turnId: 'turn-1',
      payload: {
        approvalId: 'approval-bound-1',
        toolName: 'workspace_shell',
      },
    });

    const finished = reduceAgentEvent(
      resolved,
      agentEvent(4, 'tool_finished', {
        toolCallId: 'tool-bound-1',
        toolName: 'workspace_shell',
        args: {},
        result: {
          details: {
            result: {
              decisionMode: 'model',
              decisionStatus: 'failed_closed',
              approvalId: 'approval-bound-1',
            },
          },
        },
      }),
    ).state;
    expect(finished.activityOrder).toEqual(['tool-bound-1']);
    expect(finished.activitiesById['tool-bound-1'].payload).toMatchObject({
      approvalId: 'approval-bound-1',
      args: { command: 'pwd' },
    });
    expect(finished.activitiesById['tool-bound-1']).toMatchObject({
      kind: 'tool_finished',
      status: 'failed',
      turnId: 'turn-1',
    });
  });

  it('keeps an authorized Room Tool running across its internal approval bridge', () => {
    const roomBinding = {
      todoId: '',
      todoRevision: 0,
      goalId: '',
      goalRevision: 0,
      turnId: 'turn-room-tool',
      roomBound: true,
      roomId: 'room-1',
      rootId: 'root-room-1',
      dispatchId: 'dispatch-room-1',
      generation: 7,
    };
    const started = reduceAgentEvent(
      createAgentProjection('session-1'),
      {
        ...agentEvent(1, 'tool_started', {
          toolCallId: 'tool-room-1',
          toolName: 'workspace_shell',
          args: { command: 'git status --short' },
        }),
        turnId: 'turn-room-tool',
      },
    ).state;
    const bridged = reduceAgentEvent(
      started,
      {
        ...agentEvent(2, 'approval_required', {
          approvalId: 'approval-room-1',
          toolCallId: 'tool-room-1',
          payloadSha256: 'b'.repeat(64),
          state: 'pending',
          causalMetadata: roomBinding,
        }),
        turnId: 'turn-room-tool',
      },
    ).state;
    const authorized = reduceAgentEvent(
      bridged,
      {
        ...agentEvent(3, 'approval_resolved', {
          approvalId: 'approval-room-1',
          toolCallId: 'tool-room-1',
          state: 'applied',
          automatic: true,
          decisionMode: 'policy',
        }),
        turnId: 'turn-room-tool',
      },
    ).state;

    expect(bridged.status).toBe('working');
    expect(bridged.turnsById['turn-room-tool']).toMatchObject({ status: 'running' });
    expect(bridged.activitiesById['tool-room-1']).toMatchObject({
      kind: 'tool_started',
      status: 'running',
      payload: {
        toolName: 'workspace_shell',
        causalMetadata: { roomBound: true, dispatchId: 'dispatch-room-1' },
      },
    });
    expect(authorized.status).toBe('working');
    expect(authorized.activitiesById['tool-room-1']).toMatchObject({
      kind: 'tool_started',
      status: 'running',
      payload: {
        approvalId: 'approval-room-1',
        automatic: true,
        decisionMode: 'policy',
      },
    });
  });

  it('reconciles a completed legacy approval row into the matching tool result', () => {
    const started = reduceAgentEvent(
      createAgentProjection('session-1'),
      agentEvent(1, 'tool_started', {
        toolCallId: 'tool-legacy-1',
        toolName: 'workspace_shell',
        args: { command: 'pwd' },
      }),
    ).state;
    const legacyApproval = reduceAgentEvent(
      started,
      {
        ...agentEvent(2, 'approval_resolved', {
          approvalId: 'approval-legacy-1',
          state: 'rejected',
          decisionMode: 'model',
          approvalModelDecision: {
            decision: 'deny',
            status: 'failed_closed',
          },
        }),
        turnId: 'approval:approval-legacy-1',
      },
    ).state;
    expect(legacyApproval.activityOrder).toEqual([
      'tool-legacy-1',
      'approval-legacy-1',
    ]);

    const reconciled = reduceAgentEvent(
      legacyApproval,
      agentEvent(3, 'tool_finished', {
        toolCallId: 'tool-legacy-1',
        toolName: 'workspace_shell',
        args: {},
        result: {
          details: {
            result: {
              approvalId: 'approval-legacy-1',
              summary: '审批未通过，命令没有执行。',
            },
          },
        },
      }),
    ).state;

    expect(reconciled.activityOrder).toEqual(['tool-legacy-1']);
    expect(reconciled.activitiesById['approval-legacy-1']).toBeUndefined();
    expect(reconciled.turnOrder).toEqual(['turn-1']);
    expect(reconciled.turnsById['approval:approval-legacy-1']).toBeUndefined();
    expect(reconciled.activitiesById['tool-legacy-1']).toMatchObject({
      kind: 'tool_finished',
      status: 'failed',
      turnId: 'turn-1',
      payload: {
        approvalId: 'approval-legacy-1',
        decisionMode: 'model',
      },
    });
  });

  it('keeps authoritative Tool usage on the matching toolCallId only', () => {
    const first = reduceAgentEvent(
      createAgentProjection('session-1'),
      agentEvent(1, 'tool_started', {
        toolCallId: 'tool-one',
        toolName: 'workspace_read',
      }),
    ).state;
    const second = reduceAgentEvent(
      first,
      agentEvent(2, 'tool_started', {
        toolCallId: 'tool-two',
        toolName: 'workspace_search',
      }),
    ).state;
    const finished = reduceAgentEvent(
      second,
      agentEvent(3, 'tool_finished', {
        toolCallId: 'tool-two',
        toolName: 'workspace_search',
        usage: { input: 2, output: 3, totalTokens: 5 },
        isError: false,
      }),
    ).state;

    expect(finished.activitiesById['tool-two'].payload.usage).toEqual({
      input: 2,
      output: 3,
      totalTokens: 5,
    });
    expect(finished.activitiesById['tool-one'].payload.usage).toBeUndefined();
  });

  it('restores a generic Pi question from the authoritative snapshot', () => {
    const recovered = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [],
      liveEvents: [{
        ...rawAgentEvent(7, 'user_input_required', {
          requestId: 'ui-select-1',
          method: 'select',
          title: '选择部署环境',
          options: ['预览', '生产'],
        }),
        turnId: 'turn-structured-1',
      }],
      lastSequence: 7,
      resumeToken: 'session-1:7',
      status: 'busy',
    });

    expect(recovered.activitiesById['ui-select-1']).toMatchObject({
      kind: 'user_input_required',
      status: 'waiting',
      turnId: 'turn-structured-1',
    });
    expect(recovered.activityOrder).toEqual(['ui-select-1']);
    expect(recovered.status).toBe('waiting');
  });

  it('settles one structured question once and retains timeout provenance', () => {
    const waiting = reduceAgentEvent(
      createAgentProjection('session-1'),
      agentEvent(1, 'user_input_required', {
        requestId: 'ui-timeout-1',
        method: 'confirm',
        title: '继续执行？',
      }),
    ).state;
    const resolved = reduceAgentEvent(
      waiting,
      agentEvent(2, 'user_input_required', {
        requestId: 'ui-timeout-1',
        method: 'confirm',
        resolutionState: 'cancelled',
        resolutionSource: 'timeout',
      }),
    ).state;
    const replay = reduceAgentEvent(
      resolved,
      agentEvent(2, 'user_input_required', {
        requestId: 'ui-timeout-1',
        method: 'confirm',
        resolutionState: 'cancelled',
        resolutionSource: 'timeout',
      }),
    );

    expect(resolved.activityOrder).toEqual(['ui-timeout-1']);
    expect(resolved.activitiesById['ui-timeout-1']).toMatchObject({
      status: 'completed',
      payload: {
        resolutionState: 'cancelled',
        resolutionSource: 'timeout',
      },
    });
    expect(replay.disposition).toBe('ignored-duplicate');
    expect(replay.state).toBe(resolved);
  });

  it('keeps Pi transcript anchors once when the bounded event journal replays the same turn', () => {
    const question = '昨天做到哪里了？';
    const answer = '已经完成按需加载边界。';
    const recovered = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [
        {
          ...serverMessage('pi-user', 'user', 'history:pi-user', question),
          createdAtMs: 1_000,
          completedAtMs: 1_000,
        },
        {
          ...serverMessage('pi-assistant', 'assistant', 'history:pi-user', answer),
          createdAtMs: 1_100,
          completedAtMs: 1_100,
          provider: 'gpt',
          model: 'gpt-5.6-sol',
        },
      ],
      liveEvents: [
        {
          ...rawAgentEvent(41, 'message_completed', {
            clientMessageId: 'web-rewrite-1',
            message: {
              ...serverMessage('event-user', 'user', 'turn-rewrite', question),
              createdAtMs: 1_500,
              completedAtMs: 1_500,
              clientMessageId: 'web-rewrite-1',
            },
          }),
          turnId: 'turn-rewrite',
        },
        {
          ...rawAgentEvent(42, 'text_delta', {
            messageId: 'event-assistant',
            blockId: 'event-assistant:text',
            delta: answer,
            replaceBlock: true,
          }),
          turnId: 'turn-rewrite',
        },
        {
          ...rawAgentEvent(43, 'message_completed', {
            message: {
              ...serverMessage('event-assistant', 'assistant', 'turn-rewrite', answer),
              createdAtMs: 1_100,
              completedAtMs: 1_100,
            },
          }),
          turnId: 'turn-rewrite',
        },
        {
          ...rawAgentEvent(44, 'tool_finished', {
            toolCallId: 'tool-proof',
            toolName: 'workspace_search',
            summary: '检索完成',
          }),
          turnId: 'turn-rewrite',
        },
        {
          ...rawAgentEvent(45, 'turn_completed', { status: 'completed' }),
          turnId: 'turn-rewrite',
        },
      ],
      lastSequence: 45,
      resumeToken: 'session-1:45',
      status: 'idle',
    });

    expect(recovered.messageOrder).toEqual(['pi-user', 'pi-assistant']);
    expect(recovered.messagesById['event-user']).toBeUndefined();
    expect(recovered.messagesById['event-assistant']).toBeUndefined();
    expect(recovered.messagesById['pi-user'].clientMessageId).toBe('web-rewrite-1');
    expect(recovered.activitiesById['tool-proof']).toMatchObject({
      status: 'completed',
      turnId: 'history:pi-user',
    });
    expect(recovered.turnOrder).toEqual(['history:pi-user']);
    expect(recovered.turnsById['turn-rewrite']).toBeUndefined();
    expect(recovered.turnsById['history:pi-user'].activityIds).toContain('tool-proof');
  });

  it('replaces a live user row when a bounded snapshot imports its transcript anchor', () => {
    const question = '最近输入有哪些';
    const liveMessage = {
      ...serverMessage('runtime-user', 'user', 'runtime-turn', question),
      createdAtMs: 1_000,
      completedAtMs: 1_000,
    };
    const live = reduceAgentEvent(
      createAgentProjection('session-1'),
      {
        ...agentEvent(41, 'message_completed', {
          clientMessageId: 'memory-steward:journal-2026-09-03:1000',
          message: liveMessage,
        }),
        turnId: 'runtime-turn',
        createdAtMs: 1_000,
        payload: {
          clientMessageId: 'memory-steward:journal-2026-09-03:1000',
          message: liveMessage,
        },
      },
    ).state;

    const recovered = applyAgentSnapshot(live, {
      messages: [{
        ...serverMessage('pi-user', 'user', 'history:pi-user', question),
        createdAtMs: 1_100,
        completedAtMs: 1_100,
      }],
      liveEvents: [],
      lastSequence: 42,
      resumeToken: 'session-1:42',
      snapshotScope: 'recent',
      partial: true,
      status: 'busy',
    });

    expect(recovered.messageOrder).toEqual(['pi-user']);
    expect(recovered.messagesById['runtime-user']).toBeUndefined();
    expect(recovered.messagesById['pi-user']?.clientMessageId).toBe(
      'memory-steward:journal-2026-09-03:1000',
    );
    expect(recovered.turnOrder).toEqual(['history:pi-user']);
  });

  it('reconciles a long-streamed final answer with its same-turn Pi transcript and keeps files', () => {
    const turnId = 'lab-guide-turn';
    const answer = '应用已经准备，保留实际运行记录。';
    const streamed = serverMessage(`${turnId}:assistant`, 'assistant', turnId, answer);
    const live = reduceAgentEvent(createAgentProjection('session-1'), {
      ...agentEvent(1, 'message_completed', { message: { ...streamed, createdAtMs:1_000, completedAtMs:1_000 } }),
      turnId,
      createdAtMs:80_000,
    }).state;
    const durable = { ...serverMessage('pi-completed-app', 'assistant', turnId, answer),
      createdAtMs:80_000, completedAtMs:80_000,
      blocks:[...streamed.blocks,{id:'app-source',type:'file',status:'completed',presentationKind:'file',data:{fileName:'index.html',text:'<h1>应用文件内容</h1>'}}],
    };
    const restored = applyAgentSnapshot(live, {
      messages:[durable],liveEvents:[],lastSequence:2,resumeToken:'session-1:2',status:'idle',snapshotScope:'recent',partial:true,
    });
    expect(restored.messageOrder).toEqual(['pi-completed-app']);
    expect(restored.messagesById['pi-completed-app'].blocks.some((block) => block.id==='app-source')).toBe(true);
  });

  it('absorbs a completed Pi row and its unfinished live alias while tools continue', () => {
    const turnId = 'turn-1';
    const answer = '已读取项目，继续检查。';
    const durable = { ...serverMessage('pi-progress', 'assistant', turnId, answer), createdAtMs: 80_000 };
    const restored = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [durable],
      liveEvents: [{ ...agentEvent(1, 'text_delta', { messageId: `${turnId}:assistant`, delta: answer }), createdAtMs: 1_000 }],
      status: 'busy', lastSequence: 1, resumeToken: 'session-1:1',
    });
    expect(restored.messageOrder).toEqual(['pi-progress']);
    expect(restored.messagesById['pi-progress'].status).toBe('completed');
    expect(restored.turnsById[turnId].status).toBe('running');
  });

  it('keeps an equal answer in a different turn outside the short replay window', () => {
    const answer = '同一条回答也可能被独立请求两次。';
    const live = reduceAgentEvent(createAgentProjection('session-1'), {
      ...agentEvent(1,'message_completed',{message:{...serverMessage('first:assistant','assistant','first',answer),createdAtMs:1_000}}),turnId:'first',
    }).state;
    const restored = applyAgentSnapshot(live, {
      messages:[{...serverMessage('second-pi','assistant','second',answer),createdAtMs:80_000}],liveEvents:[],lastSequence:2,resumeToken:'session-1:2',status:'idle',snapshotScope:'recent',partial:true,
    });
    expect(restored.messageOrder).toHaveLength(2);
  });

  it('keeps one user anchor when transcript and replay media ids differ', () => {
    const question = '收起所有工具和思考';
    const transcriptUser = serverMessage('pi-user-with-image', 'user', 'history:pi-user-with-image', question);
    const replayUser = serverMessage('event-user-with-image', 'user', 'turn-with-image', question);
    const recovered = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [{
        ...transcriptUser,
        attachments: ['transcript-media-copy'],
        blocks: [
          ...transcriptUser.blocks,
          {
            id: 'pi-user-with-image:image',
            type: 'image',
            status: 'completed',
            presentationKind: 'image',
            data: { mediaId: 'transcript-media-copy', receiptUrl: '/media/transcript' },
          },
        ],
        createdAtMs: 1_000,
        completedAtMs: 1_000,
      }],
      liveEvents: [
        {
          ...rawAgentEvent(41, 'message_completed', {
            clientMessageId: 'web-image-prompt',
            message: {
              ...replayUser,
              clientMessageId: 'web-image-prompt',
              attachments: ['uploaded-media'],
              blocks: [
                ...replayUser.blocks,
                {
                  id: 'event-user-with-image:image',
                  type: 'image',
                  status: 'completed',
                  presentationKind: 'image',
                  data: { mediaId: 'uploaded-media', receiptUrl: '/media/uploaded' },
                },
              ],
              createdAtMs: 1_000,
              completedAtMs: 1_000,
            },
          }),
          turnId: 'turn-with-image',
        },
        {
          ...rawAgentEvent(42, 'reasoning_summary', {
            requestId: 'reasoning-after-image',
            summary: '正在处理图片消息',
            source: 'provider_reasoning_summary',
            state: 'completed',
          }),
          turnId: 'turn-with-image',
        },
      ],
      lastSequence: 42,
      resumeToken: 'session-1:42',
      status: 'responding',
    });

    expect(recovered.messageOrder).toEqual(['pi-user-with-image']);
    expect(recovered.messagesById['event-user-with-image']).toBeUndefined();
    expect(recovered.messagesById['pi-user-with-image']).toMatchObject({
      clientMessageId: 'web-image-prompt',
      attachments: ['transcript-media-copy'],
    });
    expect(recovered.turnOrder).toEqual(['history:pi-user-with-image']);
    expect(recovered.activitiesById['reasoning-after-image']).toMatchObject({
      turnId: 'history:pi-user-with-image',
    });
  });

  it('keeps distinct same-text media messages without client acceptance identity', () => {
    const question = '分别检查这张图片';
    const transcriptUser = serverMessage('pi-user-first-image', 'user', 'history:pi-user-first-image', question);
    const replayUser = serverMessage('event-user-second-image', 'user', 'turn-second-image', question);
    const recovered = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [{
        ...transcriptUser,
        attachments: ['first-media'],
        createdAtMs: 1_000,
        completedAtMs: 1_000,
      }],
      liveEvents: [{
        ...rawAgentEvent(41, 'message_completed', {
          message: {
            ...replayUser,
            attachments: ['second-media'],
            createdAtMs: 1_000,
            completedAtMs: 1_000,
          },
        }),
        turnId: 'turn-second-image',
      }],
      lastSequence: 41,
      resumeToken: 'session-1:41',
      status: 'responding',
    });

    expect(recovered.messageOrder).toEqual([
      'pi-user-first-image',
      'event-user-second-image',
    ]);
  });

  it('keeps a later client-bound image message outside the replay window', () => {
    const question = '分别检查这张图片';
    const transcriptUser = serverMessage('pi-user-earlier-image', 'user', 'history:pi-user-earlier-image', question);
    const replayUser = serverMessage('event-user-later-image', 'user', 'turn-later-image', question);
    const recovered = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [{
        ...transcriptUser,
        attachments: ['earlier-media'],
        createdAtMs: 1_000,
        completedAtMs: 1_000,
      }],
      liveEvents: [{
        ...rawAgentEvent(41, 'message_completed', {
          clientMessageId: 'web-later-image-prompt',
          message: {
            ...replayUser,
            clientMessageId: 'web-later-image-prompt',
            attachments: ['later-media'],
            createdAtMs: 2_500,
            completedAtMs: 2_500,
          },
        }),
        turnId: 'turn-later-image',
      }],
      lastSequence: 41,
      resumeToken: 'session-1:41',
      status: 'responding',
    });

    expect(recovered.messageOrder).toEqual([
      'pi-user-earlier-image',
      'event-user-later-image',
    ]);
  });

  it('anchors replay-only reasoning to the matching durable turn without creating a phantom reply', () => {
    const recovered = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [
        {
          ...serverMessage('durable-user', 'user', 'history:durable-user', '读取很多文件'),
          createdAtMs: 1_000,
          completedAtMs: 1_000,
        },
        {
          ...serverMessage('durable-assistant', 'assistant', 'history:durable-user', 'READS-OK'),
          createdAtMs: 2_000,
          completedAtMs: 2_000,
        },
      ],
      liveEvents: [
        {
          ...rawAgentEvent(41, 'reasoning_summary', {
            requestId: 'reasoning-many-reads',
            summary: '检查只读结果',
            source: 'provider_reasoning_summary',
            state: 'completed',
          }),
          turnId: 'runtime-turn-many-reads',
        },
        {
          ...rawAgentEvent(42, 'message_completed', {
            message: {
              ...serverMessage(
                'runtime-assistant',
                'assistant',
                'runtime-turn-many-reads',
                'READS-OK',
              ),
              createdAtMs: 2_000,
              completedAtMs: 2_000,
            },
          }),
          turnId: 'runtime-turn-many-reads',
        },
      ],
      lastSequence: 42,
      resumeToken: 'session-1:42',
      status: 'idle',
    });

    expect(recovered.turnOrder).toEqual(['history:durable-user']);
    expect(recovered.turnsById['runtime-turn-many-reads']).toBeUndefined();
    expect(recovered.activitiesById['reasoning-many-reads']).toMatchObject({
      turnId: 'history:durable-user',
      status: 'completed',
    });
    expect(recovered.turnsById['history:durable-user']).toMatchObject({
      status: 'completed',
      messageIds: ['durable-user', 'durable-assistant'],
      activityIds: ['reasoning-many-reads'],
    });
  });

  it('retains an in-flight replay delta when Pi has no completed transcript message for it', () => {
    const recovered = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [serverMessage('pi-user', 'user', 'history:pi-user', '继续')],
      liveEvents: [
        rawAgentEvent(41, 'text_delta', {
          messageId: 'turn-1:assistant',
          delta: '仍在生成',
          replaceBlock: true,
        }),
      ],
      lastSequence: 41,
      resumeToken: 'session-1:41',
      status: 'responding',
    });

    expect(recovered.messageOrder).toEqual(['pi-user', 'turn-1:assistant']);
    expect(textOf(recovered.messagesById['turn-1:assistant'])).toBe('仍在生成');
  });

  it('replays lifecycle cancellation audits from snapshots and applies newer owner receipts', () => {
    const restoredAudit = lifecycleCancellationAudit({
      state: 'partial',
      updatedAtMs: 100,
      owners: {
        runtime: { status: 'succeeded', receipt: { lifecycle: 'aborted' } },
        approval: { status: 'succeeded', receipt: { cancelledCount: 1 } },
        job: { status: 'partial', receipt: { cancelled: 1, stillRunning: 1 } },
        delegation: { status: 'pending', receipt: {} },
      },
    });
    const restored = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [],
      liveEvents: [],
      lastSequence: 10,
      resumeToken: 'session-1:10',
      lifecycleCancellationAudits: [restoredAudit],
    });

    expect(restored.lifecycleCancellationAuditOrder).toEqual([restoredAudit.requestId]);
    expect(restored.lifecycleCancellationAuditsById[restoredAudit.requestId]).toMatchObject({
      state: 'partial',
      owners: {
        runtime: { status: 'succeeded' },
        job: { status: 'partial' },
        delegation: { status: 'pending' },
      },
    });

    const completedAudit = lifecycleCancellationAudit({
      state: 'completed',
      updatedAtMs: 200,
      owners: {
        runtime: { status: 'succeeded', receipt: { lifecycle: 'aborted' } },
        approval: { status: 'succeeded', receipt: { cancelledCount: 1 } },
        job: { status: 'succeeded', receipt: { cancelled: 2 } },
        delegation: { status: 'excluded', receipt: { reason: 'no_active_delegation' } },
      },
    });
    const updated = reduceAgentEvent(
      restored,
      agentEvent(11, 'lifecycle_cancellation_changed', { audit: completedAudit }),
    ).state;

    expect(updated.lifecycleCancellationAuditOrder).toEqual([completedAudit.requestId]);
    expect(updated.lifecycleCancellationAuditsById[completedAudit.requestId]).toEqual(completedAudit);
  });
  it('replaces an active Room authorization with the current Session workflow on reconnect', () => {
    const todo = {
      schemaVersion: 'rag-ime.agent-todo.v1',
      id: 'todo:room-session',
      sessionId: 'session-1',
      revision: 0,
      actor: 'agent',
      updatedAtMs: 0,
      phases: [],
      counts: { total: 0, pending: 0, inProgress: 0, completed: 0, abandoned: 0 },
    };
    const goal = {
      schemaVersion: 'rag-ime.agent-goal.v1',
      sessionId: 'session-1',
      configured: false,
      goalId: '',
      revision: 0,
      objective: '',
      successCriteria: '',
      evidenceExpectations: [],
      status: 'cleared',
      budget: { tokenLimit: null, timeLimitMs: null },
      usage: { tokens: 0, elapsedMs: 0 },
      remaining: { tokens: null, timeMs: null },
      budgetExceeded: false,
      completionAudit: null,
      cancellationAudit: null,
      updatedAtMs: 0,
    };
    const authorized = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [],
      liveEvents: [],
      lastSequence: 7,
      resumeToken: 'session-1:7',
      todo,
      goal,
      actGate: {
        allowed: true,
        reason: 'approved',
        message: '当前 Room 任务已经开始，可以在本轮权限范围内继续工作。',
        todoRevision: 0,
        goalRevision: 0,
      },
    });

    expect(authorized.actGate).toMatchObject({ allowed: true, reason: 'approved' });

    const pausedGoal = {
      ...goal,
      configured: true,
      goalId: 'goal:paused',
      revision: 1,
      objective: '等待恢复',
      successCriteria: '由用户恢复后继续',
      status: 'paused',
    };
    const reconnected = applyAgentSnapshot(authorized, {
      messages: [],
      liveEvents: [],
      lastSequence: 8,
      resumeToken: 'session-1:8',
      todo,
      goal: pausedGoal,
      actGate: {
        allowed: false,
        reason: 'goal_paused',
        message: '当前 Goal 已暂停，恢复后才能继续写入。',
        todoRevision: 0,
        goalRevision: 1,
      },
    });

    expect(reconnected.todo).toMatchObject({ id: todo.id, revision: todo.revision });
    expect(reconnected.goal).toMatchObject({
      sessionId: pausedGoal.sessionId,
      revision: pausedGoal.revision,
      status: pausedGoal.status,
    });
    expect(reconnected.actGate).toEqual({
      allowed: false,
      reason: 'goal_paused',
      message: '当前 Goal 已暂停，恢复后才能继续写入。',
      todoRevision: 0,
      goalRevision: 1,
    });
  });

  it('restores the durable Todo and advances it from live todo results', () => {
    const restored = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [],
      liveEvents: [],
      lastSequence: 0,
      resumeToken: '',
      todo: todoProjection({
        revision: 2,
        phases: [{
          name: '实现',
          tasks: [
            { content: '核对现状', status: 'completed' },
            { content: '实现清单', status: 'blocked', reason: '等待用户确认范围' },
          ],
        }],
        counts: { total: 2, pending: 0, inProgress: 0, blocked: 1, completed: 1, abandoned: 0 },
      }),
    });

    expect(restored.todo.counts).toEqual({
      total: 2,
      pending: 0,
      inProgress: 0,
      blocked: 1,
      completed: 1,
      abandoned: 0,
    });
    expect(restored.todo.phases[0]?.tasks[1]).toMatchObject({
      status: 'blocked',
      reason: '等待用户确认范围',
    });

    const advanced = reduceAgentEvent(
      restored,
      agentEvent(1, 'tool_finished', {
        toolCallId: 'todo-update',
        toolName: 'todo',
        result: {
          details: {
            result: {
              todo: todoProjection({
                revision: 3,
                phases: [{
                  name: '实现',
                  tasks: [
                    { content: '核对现状', status: 'completed' },
                    { content: '实现清单', status: 'completed' },
                  ],
                }],
                counts: { total: 2, pending: 0, inProgress: 0, completed: 2, abandoned: 0 },
              }),
            },
          },
        },
      }),
    ).state;

    expect(advanced.todo.revision).toBe(3);
    expect(advanced.todo.phases[0]?.tasks.map((item) => item.status)).toEqual(['completed', 'completed']);
    expect(advanced.todo.counts.completed).toBe(2);
  });

  it('uses the authoritative idle snapshot status to recover a stale aborting turn', () => {
    const recovered = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [],
      liveEvents: [
        rawAgentEvent(41, 'status_changed', { status: 'busy' }),
        rawAgentEvent(42, 'text_delta', { delta: 'partial' }),
        rawAgentEvent(43, 'status_changed', { status: 'aborting' }),
      ],
      lastSequence: 43,
      resumeToken: 'session-1:43',
      status: 'idle',
    });

    expect(recovered.status).toBe('idle');
    expect(recovered.turnsById['turn-1'].status).toBe('aborted');
    expect(recovered.messagesById['turn-1:assistant'].status).toBe('aborted');
  });

  it('preserves an aborted transcript when an idle snapshot also restores a failed tool receipt', () => {
    const abortedAssistant = {
      ...serverMessage('assistant-aborted', 'assistant', 'turn-1', '已停止。'),
      status: 'aborted',
      blocks: serverMessage('assistant-aborted', 'assistant', 'turn-1', '已停止。').blocks.map(
        (block) => ({ ...block, status: 'aborted' }),
      ),
    };
    const recovered = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [
        serverMessage('user-stop', 'user', 'turn-1', '运行长命令'),
        abortedAssistant,
      ],
      liveEvents: [
        rawAgentEvent(41, 'tool_started', {
          toolCallId: 'tool-stop',
          toolName: 'bash',
          summary: '运行长命令',
        }),
        rawAgentEvent(42, 'tool_finished', {
          toolCallId: 'tool-stop',
          toolName: 'bash',
          isError: true,
          result: {},
        }),
      ],
      lastSequence: 42,
      resumeToken: 'session-1:42',
      status: 'idle',
    });

    expect(recovered.turnsById['turn-1'].status).toBe('aborted');
    expect(recovered.messagesById['assistant-aborted'].status).toBe('aborted');
    expect(recovered.activitiesById['tool-stop'].status).toBe('failed');
  });

  it('treats a later successful provider retry as the terminal result of one turn', () => {
    const failedAttempt = {
      ...serverMessage('assistant-network-failure', 'assistant', 'turn-retry', '模型服务未能生成最终回复。'),
      status: 'failed',
      blocks: [
        ...serverMessage('assistant-network-failure', 'assistant', 'turn-retry', '模型服务未能生成最终回复。').blocks,
        {
          id: 'assistant-network-failure:error',
          type: 'error',
          status: 'failed',
          presentationKind: 'error',
          data: { message: 'fetch failed' },
        },
      ],
    };
    const recovered = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [
        serverMessage('user-retry', 'user', 'turn-retry', '本月销售额和上月相比怎样？'),
        failedAttempt,
        serverMessage('assistant-final', 'assistant', 'turn-retry', '目前缺少可访问的经营数据源。'),
      ],
      liveEvents: [],
      lastSequence: 206,
      resumeToken: 'session-1:206',
      status: 'idle',
    });

    expect(recovered.turnsById['turn-retry']).toMatchObject({ status: 'completed' });
    expect(recovered.turnsById['turn-retry'].failure).toBeUndefined();
    expect(recovered.messagesById['assistant-network-failure'].status).toBe('failed');
  });

  it('keeps a later failed attempt terminal even when the turn has an earlier completed assistant message', () => {
    const recovered = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [
        serverMessage('user-retry', 'user', 'turn-retry', '继续检查'),
        serverMessage('assistant-progress', 'assistant', 'turn-retry', '正在检查资料。'),
        {
          ...serverMessage('assistant-final-failure', 'assistant', 'turn-retry', '模型连接失败。'),
          status: 'failed',
        },
      ],
      liveEvents: [],
      lastSequence: 206,
      resumeToken: 'session-1:206',
      status: 'idle',
    });

    expect(recovered.turnsById['turn-retry'].status).toBe('failed');
    expect(recovered.messagesById['assistant-progress'].status).toBe('completed');
  });

  it('reopens a provisionally failed turn when later Tool work proves the retry is active', () => {
    const failedAttempt = {
      ...serverMessage('assistant-network-failure', 'assistant', 'turn-1', '模型服务未能生成最终回复。'),
      status: 'failed',
    };
    const failed = reduceAgentEvent(
      createAgentProjection('session-1'),
      agentEvent(1, 'message_completed', { message: failedAttempt }),
    ).state;
    expect(failed.turnsById['turn-1']?.status).toBe('failed');

    const resumed = reduceAgentEvent(
      failed,
      agentEvent(2, 'tool_started', {
        toolCallId: 'retry-tool',
        toolName: 'knowledge',
        summary: '继续核对经营数据源',
      }),
    ).state;

    expect(resumed.turnsById['turn-1']?.status).toBe('running');
  });

  it('does not reopen the last completed transcript turn when Pi marks the Session active', () => {
    const recovered = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [
        serverMessage('server-user', 'user', 'turn-history', '调用 overview'),
        serverMessage('server-assistant', 'assistant', 'turn-history', '控制中心运行正常。'),
      ],
      liveEvents: [rawAgentEvent(43, 'status_changed', { status: 'ready' })],
      lastSequence: 43,
      resumeToken: 'session-1:43',
      // AgentSessionStore uses active for an open transcript binding. It is
      // not evidence that Pi currently owns an in-flight turn.
      status: 'active',
    });

    expect(recovered.status).toBe('active');
    expect(recovered.turnsById['turn-history'].status).toBe('completed');
    expect(recovered.messagesById['server-assistant'].status).toBe('completed');
  });

  it('keeps bounded progress checkpoints for one logical tool call', () => {
    const started = reduceAgentEvent(
      createAgentProjection('session-1'),
      agentEvent(1, 'tool_started', {
        toolCallId: 'tool-progress-1',
        toolName: 'knowledge',
        summary: '开始检索知识库',
        args: { path: 'docs/acceptance.md' },
      }),
    ).state;
    const firstProgress = reduceAgentEvent(
      started,
      agentEvent(2, 'tool_progress', {
        toolCallId: 'tool-progress-1',
        partialResult: { details: { summary: '已找到候选来源' } },
      }),
    ).state;
    const secondProgress = reduceAgentEvent(
      firstProgress,
      agentEvent(3, 'tool_progress', {
        toolCallId: 'tool-progress-1',
        partialResult: { details: { result: { summary: '正在重排 4 条证据' } } },
      }),
    ).state;
    const finished = reduceAgentEvent(
      secondProgress,
      agentEvent(4, 'tool_finished', {
        toolCallId: 'tool-progress-1',
        args: {},
        result: { details: { result: { summary: '知识检索完成' } } },
      }),
    ).state;

    expect(finished.activityOrder).toEqual(['tool-progress-1']);
    expect(finished.activitiesById['tool-progress-1']).toMatchObject({
      kind: 'tool_finished',
      status: 'completed',
      createdAtMs: 10,
      updatedAtMs: 40,
      payload: {
        toolName: 'knowledge',
        args: { path: 'docs/acceptance.md' },
        progressHistory: [
          { kind: 'tool_started', summary: '开始检索知识库', createdAtMs: 10 },
          { kind: 'tool_progress', summary: '已找到候选来源', createdAtMs: 20 },
          { kind: 'tool_progress', summary: '正在重排 4 条证据', createdAtMs: 30 },
          { kind: 'tool_finished', summary: '知识检索完成', createdAtMs: 40 },
        ],
      },
    });
  });

  it('does not create a ghost parent turn for detached subagent progress', () => {
    const detachedProgress = {
      ...agentEvent(1, 'tool_progress', {
        toolCallId: 'subagent:subagent-batch:1',
        toolName: 'agents',
        summary: '子 Agent 已返回结果，待主持会话核验',
        state: 'completed',
      }),
      turnId: '',
    };

    const state = reduceAgentEvent(
      createAgentProjection('session-1'),
      detachedProgress,
    ).state;

    expect(state.lastSequence).toBe(1);
    expect(state.turnOrder).toEqual([]);
    expect(state.activityOrder).toEqual([]);
    expect(state.status).toBe('idle');
  });

  it('projects an Act Gate refusal as a safe no-op instead of a failed Tool', () => {
    const state = reduceAgentEvent(
      createAgentProjection('session-1'),
      agentEvent(1, 'tool_finished', {
        toolCallId: 'tool-goal-paused',
        toolName: 'edit',
        isError: true,
        result: {
          details: {
            ok: false,
            error: 'Act Gate blocked workspace mutation (goal_paused): 当前 Goal 已暂停，恢复后才能继续写入。',
          },
        },
      }),
    ).state;

    expect(state.activitiesById['tool-goal-paused']).toMatchObject({
      status: 'completed',
      summary: '工作区变更未执行：当前 Todo、Goal 或权限状态不允许执行。',
      payload: {
        isError: false,
        governanceBlocked: true,
      },
    });
  });

  it('projects loading an already-active Tool schema as an idempotent no-op', () => {
    const state = reduceAgentEvent(
      createAgentProjection('session-1'),
      agentEvent(1, 'tool_finished', {
        toolCallId: 'tool-load-active',
        toolName: 'tool_load',
        isError: true,
        result: {
          content: [{
            type: 'text',
            text: 'Tool schema is already active; call it directly and do not pass it to tool_load: desktop_semantic',
          }],
          details: {},
        },
      }),
    ).state;

    expect(state.activitiesById['tool-load-active']).toMatchObject({
      status: 'completed',
      summary: '工具已经可直接调用，无需重复加载。',
      payload: { isError: false, expectedNoop: true },
    });
  });

  it('merges an optimistic user message only by clientMessageId', () => {
    const optimistic = appendOptimisticAgentMessage(createAgentProjection('session-1'), {
      clientMessageId: 'client-1',
      text: '同一段文字',
      nowMs: 10,
    });
    const completed = reduceAgentEvent(
      optimistic,
      agentEvent(1, 'message_completed', {
        clientMessageId: 'client-1',
        message: {
          ...serverMessage('server-1', 'user', 'turn-server', '同一段文字'),
          clientMessageId: 'client-1',
        },
      }),
    ).state;

    expect(completed.messageOrder).toEqual(['server-1']);
    expect(completed.messagesById['local:client-1']).toBeUndefined();
    expect(completed.messagesById['server-1'].clientMessageId).toBe('client-1');
    expect(completed.optimisticByClientMessageId).toEqual({});
  });

  it('reconciles the first Home prompt when the durable user SSE omits clientMessageId', () => {
    const optimistic = appendOptimisticAgentMessage(createAgentProjection('session-created'), {
      // PawAgentHome creates this id before navigating into the new Session.
      clientMessageId: 'session-mte2oj9y-ek4g69yt',
      text: '进行记忆整理',
      nowMs: 10,
    });
    const durableMessage = {
      ...serverMessage('durable-home-user', 'user', 'turn-1', '进行记忆整理'),
      sessionId: 'session-created',
    };

    const received = reduceAgentEvent(
      optimistic,
      {
        ...agentEvent(2, 'message_completed', {
          // Pi's durable user row can arrive over SSE without the product
          // clientMessageId that was attached to the optimistic Home row.
          message: durableMessage,
        }),
        sessionId: 'session-created',
        turnId: 'turn-1',
        payload: {
          message: durableMessage,
        },
      },
    ).state;

    expect(received.messageOrder).toEqual(['durable-home-user']);
    expect(received.messagesById['local:session-mte2oj9y-ek4g69yt']).toBeUndefined();
    expect(received.messagesById['durable-home-user']?.clientMessageId).toBe(
      'session-mte2oj9y-ek4g69yt',
    );
    expect(received.optimisticByClientMessageId).toEqual({});
  });

  it('reconciles a PAWOS prompt when the durable user SSE omits clientMessageId', () => {
    const optimistic = appendOptimisticAgentMessage(createAgentProjection('session-pawos'), {
      // PawSessionWorkspace admits its prompt before the runtime durable row is
      // available. The normal PAWOS prefix must follow the same bounded path
      // as the Home `session-*` admission.
      clientMessageId: 'paw-01j-test-admission',
      text: '检查掌柜问数的结果',
      nowMs: 10,
    });
    const durableMessage = {
      ...serverMessage('durable-pawos-user', 'user', 'turn-pawos', '检查掌柜问数的结果'),
      sessionId: 'session-pawos',
    };

    const received = reduceAgentEvent(
      optimistic,
      {
        ...agentEvent(2, 'message_completed', {
          // The Pi durable user row can omit the product admission id.
          message: durableMessage,
        }),
        sessionId: 'session-pawos',
        turnId: 'turn-pawos',
        payload: { message: durableMessage },
      },
    ).state;

    expect(received.messageOrder).toEqual(['durable-pawos-user']);
    expect(received.messagesById['local:paw-01j-test-admission']).toBeUndefined();
    expect(received.messagesById['durable-pawos-user']?.clientMessageId).toBe(
      'paw-01j-test-admission',
    );
    expect(received.optimisticByClientMessageId).toEqual({});
  });

  it('replaces a fast Room mirror when the durable participant user event arrives', () => {
    const clientMessageId = 'eval-lab:evaluation-wizard';
    const mirrorBase = serverMessage(
      'room-event:room-a:2',
      'user',
      'room-turn:root-a',
      '冻结本轮评测合同',
    );
    const mirror = {
      ...mirrorBase,
      clientMessageId,
      createdAtMs: 1_000,
      completedAtMs: 1_000,
      blocks: [{
        ...mirrorBase.blocks[0],
        source: { kind: 'room_event', ref: 'room-a:2' },
      }],
    };
    const projected = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [mirror],
      liveEvents: [],
      lastSequence: 0,
      resumeToken: '',
      status: 'busy',
      partial: true,
    });
    const durable = {
      ...serverMessage('pi-user', 'user', 'pi-turn-a', '冻结本轮评测合同'),
      createdAtMs: 6_100,
      completedAtMs: 6_100,
    };

    const received = reduceAgentEvent(projected, {
      ...agentEvent(1, 'message_completed', { message: durable }),
      turnId: 'pi-turn-a',
      createdAtMs: 6_100,
      payload: { message: durable },
    }).state;

    expect(received.messageOrder).toEqual(['pi-user']);
    expect(received.messagesById['room-event:room-a:2']).toBeUndefined();
    expect(received.messagesById['pi-user']?.clientMessageId).toBe(clientMessageId);
  });

  it('replaces a bound Room mirror across recent to full history outside the time window', () => {
    const dispatchId = 'jev-dispatch:bound-long-turn';
    const roomClientMessageId = 'paw-jev:room-input';
    const mirrorBase = serverMessage(
      'room-event:room-bound:2',
      'user',
      'room-root:bound-long-turn',
      '同一条跨快照请求',
    );
    const mirror = {
      ...mirrorBase,
      clientMessageId: dispatchId,
      createdAtMs: 10_000,
      completedAtMs: 10_000,
      blocks: [{
        ...mirrorBase.blocks[0],
        source: { kind: 'room_event', ref: 'room-bound:2' },
        data: {
          ...mirrorBase.blocks[0].data,
          roomClientMessageId,
          roomEventId: 'room-bound:2',
        },
      }],
    };
    const current = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [mirror],
      liveEvents: [],
      lastSequence: 0,
      resumeToken: '',
      snapshotScope: 'recent',
      partial: true,
      status: 'busy',
    });
    const durable = {
      ...serverMessage('pi-bound-user', 'user', 'pi-turn:bound-long-turn', '同一条跨快照请求'),
      clientMessageId: dispatchId,
      createdAtMs: 100_000,
      completedAtMs: 100_000,
      blocks: [{
        ...serverMessage('pi-bound-user', 'user', 'pi-turn:bound-long-turn', '同一条跨快照请求').blocks[0],
        data: { text: '同一条跨快照请求', roomClientMessageId, roomEventId: 'room-bound:2' },
      }],
    };

    const merged = mergeAgentSnapshotHistory(current, {
      messages: [durable],
      liveEvents: [],
      lastSequence: 99,
      resumeToken: 'session-1:99',
      status: 'idle',
    });

    expect(merged.messageOrder).toEqual(['pi-bound-user']);
    expect(merged.messagesById['room-event:room-bound:2']).toBeUndefined();
    expect(merged.messagesById['pi-bound-user']).toMatchObject({
      clientMessageId: dispatchId,
      turnId: 'pi-turn:bound-long-turn',
    });
    expect(merged.messagesById['pi-bound-user'].blocks[0].data.roomClientMessageId)
      .toBe(roomClientMessageId);
  });

  it('keeps the durable Pi row when a recent snapshot repeats its Room mirror', () => {
    const dispatchId = 'jev-dispatch:recent-mirror';
    const roomClientMessageId = 'paw-jev:recent-mirror';
    const durable = {
      ...serverMessage('pi-recent-user', 'user', 'pi-turn:recent-mirror', '跨视图同一条请求'),
      clientMessageId: dispatchId,
      createdAtMs: 100_000,
      blocks: [{
        ...serverMessage('pi-recent-user', 'user', 'pi-turn:recent-mirror', '跨视图同一条请求').blocks[0],
        data: { text: '跨视图同一条请求', roomClientMessageId, roomEventId: 'room-recent:2' },
      }],
    };
    const current = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [durable],
      liveEvents: [],
      lastSequence: 10,
      resumeToken: 'session-1:10',
      status: 'idle',
    });
    const mirror = {
      ...serverMessage('room-event:room-recent:2', 'user', 'room-root:recent-mirror', '跨视图同一条请求'),
      clientMessageId: dispatchId,
      createdAtMs: 10_000,
      blocks: [{
        ...serverMessage('room-event:room-recent:2', 'user', 'room-root:recent-mirror', '跨视图同一条请求').blocks[0],
        source: { kind: 'room_event', ref: 'room-recent:2' },
        data: { text: '跨视图同一条请求', roomClientMessageId, roomEventId: 'room-recent:2' },
      }],
    };

    const refreshed = applyAgentSnapshot(current, {
      messages: [mirror],
      liveEvents: [],
      lastSequence: 10,
      resumeToken: 'session-1:10',
      snapshotScope: 'recent',
      partial: true,
      status: 'idle',
    });

    expect(refreshed.messageOrder).toEqual(['pi-recent-user']);
    expect(refreshed.messagesById['room-event:room-recent:2']).toBeUndefined();
    expect(refreshed.messagesById['pi-recent-user'].turnId).toBe('pi-turn:recent-mirror');
    expect(refreshed.messagesById['pi-recent-user'].blocks[0].data.roomClientMessageId)
      .toBe(roomClientMessageId);
  });

  it('does not correlate a Room alias across message roles', () => {
    const roomClientMessageId = 'paw-jev:role-boundary';
    const user = {
      ...serverMessage('room-event:role-boundary', 'user', 'room-root:role-boundary', '用户请求'),
      clientMessageId: roomClientMessageId,
      blocks: [{
        ...serverMessage('room-event:role-boundary', 'user', 'room-root:role-boundary', '用户请求').blocks[0],
        source: { kind: 'room_event', ref: 'room-role-boundary:1' },
        data: { text: '用户请求', roomClientMessageId, roomEventId: 'room-role-boundary:1' },
      }],
    };
    const current = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [user],
      liveEvents: [],
      lastSequence: 1,
      resumeToken: 'session-1:1',
      status: 'idle',
    });
    const assistant = {
      ...serverMessage('assistant-role-boundary', 'assistant', 'turn:role-boundary', '工具完成'),
      blocks: [{
        ...serverMessage('assistant-role-boundary', 'assistant', 'turn:role-boundary', '工具完成').blocks[0],
        data: { text: '工具完成', roomClientMessageId },
      }],
    };

    const refreshed = applyAgentSnapshot(current, {
      messages: [assistant],
      liveEvents: [],
      lastSequence: 1,
      resumeToken: 'session-1:1',
      snapshotScope: 'recent',
      partial: true,
      status: 'idle',
    });

    expect(refreshed.messageOrder).toEqual([
      'room-event:role-boundary',
      'assistant-role-boundary',
    ]);
    expect(refreshed.messagesById['room-event:role-boundary']).toBeDefined();
    expect(refreshed.messagesById['assistant-role-boundary']).toBeDefined();
  });

  it('keeps a pending optimistic user when an assistant reuses its Room alias', () => {
    const clientMessageId = 'paw-jev:pending-role-boundary';
    const optimistic = appendOptimisticAgentMessage(createAgentProjection('session-1'), {
      clientMessageId,
      text: '用户请求',
      nowMs: 10,
    });
    const assistant = {
      ...serverMessage('assistant-pending-role-boundary', 'assistant', 'turn:pending-role-boundary', '已完成'),
      blocks: [{
        ...serverMessage('assistant-pending-role-boundary', 'assistant', 'turn:pending-role-boundary', '已完成').blocks[0],
        data: { text: '已完成', roomClientMessageId: clientMessageId },
      }],
    };

    const received = reduceAgentEvent(
      optimistic,
      agentEvent(1, 'message_completed', { message: assistant }),
    ).state;

    expect(received.messageOrder).toEqual([
      'local:paw-jev:pending-role-boundary',
      'assistant-pending-role-boundary',
    ]);
    expect(received.messagesById['local:paw-jev:pending-role-boundary']).toBeDefined();
    expect(received.messagesById['assistant-pending-role-boundary']).toBeDefined();
    expect(received.optimisticByClientMessageId).toEqual({
      [clientMessageId]: 'local:paw-jev:pending-role-boundary',
    });
  });

  it('settles a synthetic busy snapshot turn when the real turn completes', () => {
    const clientMessageId = 'client-equal-cursor-race';
    const optimistic = appendOptimisticAgentMessage(createAgentProjection('session-1'), {
      clientMessageId,
      text: '检查完成后告诉我结果',
      nowMs: 10,
    });
    const accepted = reduceAgentEvent(
      optimistic,
      {
        ...agentEvent(1, 'message_completed', {
          clientMessageId,
          message: {
            ...serverMessage('runtime-user', 'user', 'turn-real', '检查完成后告诉我结果'),
            clientMessageId,
          },
        }),
        turnId: 'turn-real',
      },
    ).state;

    // The prompt receipt triggers a quiet snapshot. It can race with the user
    // SSE at the same durable cursor, while Pi still identifies the turn by a
    // synthetic history id and truthfully reports that the Session is busy.
    const snapped = applyAgentSnapshot(accepted, {
      messages: [
        serverMessage('history-user', 'user', 'history:history-user', '检查完成后告诉我结果'),
      ],
      liveEvents: [],
      lastSequence: 1,
      resumeToken: 'session-1:1',
      status: 'busy',
    });
    const answered = reduceAgentEvent(
      snapped,
      {
        ...agentEvent(2, 'message_completed', {
          message: serverMessage('runtime-assistant', 'assistant', 'turn-real', '检查完成。'),
        }),
        turnId: 'turn-real',
      },
    ).state;
    const settled = reduceAgentEvent(
      answered,
      {
        ...agentEvent(3, 'turn_completed', { status: 'completed' }),
        turnId: 'turn-real',
      },
    ).state;

    expect(settled.status).toBe('idle');
    expect(settled.turnsById['turn-real']?.status).toBe('completed');
    expect(settled.turnsById['history:history-user']?.status).not.toBe('running');
    expect(settled.turnOrder.filter((turnId) => (
      ['queued', 'running', 'waiting'].includes(settled.turnsById[turnId]?.status ?? '')
    ))).toEqual([]);
  });

  it('reconciles an accepted optimistic prompt with a nearby durable transcript message', () => {
    const optimistic = appendOptimisticAgentMessage(createAgentProjection('session-1'), {
      clientMessageId: 'client-without-persisted-id',
      text: '完成后继续下一条',
      nowMs: 10,
    });

    const restored = applyAgentSnapshot(optimistic, {
      messages: [
        serverMessage('server-user', 'user', 'history:server-user', '完成后继续下一条'),
        serverMessage('server-assistant', 'assistant', 'history:server-user', '已经完成。'),
      ],
      liveEvents: [],
      lastSequence: 8,
      resumeToken: 'session-1:8',
      status: 'idle',
    });

    expect(restored.messageOrder).toEqual(['server-user', 'server-assistant']);
    expect(restored.messagesById['local:client-without-persisted-id']).toBeUndefined();
    expect(restored.messagesById['server-user'].clientMessageId).toBe('client-without-persisted-id');
    expect(restored.optimisticByClientMessageId).toEqual({});
    expect(restored.turnsById['history:server-user']?.status).toBe('completed');
    expect(restored.status).toBe('idle');
  });

  it('does not reconcile a repeated prompt when two legacy transcript messages are equally eligible', () => {
    const optimistic = appendOptimisticAgentMessage(createAgentProjection('session-1'), {
      clientMessageId: 'client-repeated-prompt',
      text: '继续',
      nowMs: 20,
    });

    const restored = applyAgentSnapshot(optimistic, {
      messages: [
        serverMessage('server-user-old', 'user', 'history:server-user-old', '继续'),
        serverMessage('server-user-newer', 'user', 'history:server-user-newer', '继续'),
      ],
      liveEvents: [],
      lastSequence: 8,
      resumeToken: 'session-1:8',
      status: 'idle',
    });

    expect(restored.messagesById['local:client-repeated-prompt']).toMatchObject({
      clientMessageId: 'client-repeated-prompt',
      status: 'queued',
    });
    expect(restored.messagesById['server-user-old']?.clientMessageId).toBeUndefined();
    expect(restored.messagesById['server-user-newer']?.clientMessageId).toBeUndefined();
    expect(restored.status).toBe('busy');
  });

  it('does not reconcile a new repeated prompt to one older legacy transcript turn', () => {
    const optimistic = appendOptimisticAgentMessage(createAgentProjection('session-1'), {
      clientMessageId: 'client-new-repeat',
      text: '继续',
      nowMs: 40,
    });
    const oldUser = {
      ...serverMessage('server-user-old', 'user', 'history:server-user-old', '继续'),
      createdAtMs: 20,
      completedAtMs: 21,
    };
    const oldAssistant = {
      ...serverMessage('server-assistant-old', 'assistant', 'history:server-user-old', '已完成。'),
      createdAtMs: 25,
      completedAtMs: 26,
    };

    const restored = applyAgentSnapshot(optimistic, {
      messages: [oldUser, oldAssistant],
      liveEvents: [],
      lastSequence: 8,
      resumeToken: 'session-1:8',
      status: 'idle',
    });

    expect(restored.messagesById['local:client-new-repeat']).toMatchObject({
      clientMessageId: 'client-new-repeat',
      status: 'queued',
    });
    expect(restored.messagesById['server-user-old']?.clientMessageId).toBeUndefined();
    expect(restored.turnsById['history:server-user-old']?.status).toBe('completed');
    expect(restored.status).toBe('busy');
  });

  it('keeps an admitted first prompt pending across an earlier idle snapshot', () => {
    const optimistic = appendOptimisticAgentMessage(createAgentProjection('session-1'), {
      clientMessageId: 'client-first-prompt',
      text: 'hi',
      nowMs: 10,
    });

    const earlySnapshot = applyAgentSnapshot(optimistic, {
      messages: [],
      liveEvents: [],
      lastSequence: 0,
      resumeToken: 'session-1:0',
      status: 'idle',
    });

    expect(earlySnapshot.messagesById['local:client-first-prompt']).toMatchObject({
      role: 'user',
      status: 'queued',
    });
    expect(earlySnapshot.turnsById['local-turn:client-first-prompt']?.status).toBe('queued');
    expect(earlySnapshot.status).toBe('busy');

    const settledSnapshot = applyAgentSnapshot(earlySnapshot, {
      messages: [
        {
          ...serverMessage('server-user', 'user', 'turn-first-prompt', 'hi'),
          clientMessageId: 'client-first-prompt',
        },
        serverMessage('server-assistant', 'assistant', 'turn-first-prompt', 'Hi! How can I help?'),
      ],
      liveEvents: [],
      lastSequence: 14,
      resumeToken: 'session-1:14',
      status: 'idle',
    });

    expect(settledSnapshot.messageOrder).toEqual(['server-user', 'server-assistant']);
    expect(settledSnapshot.messagesById['local:client-first-prompt']).toBeUndefined();
    expect(settledSnapshot.turnsById['local-turn:client-first-prompt']).toBeUndefined();
    expect(settledSnapshot.turnsById['turn-first-prompt']?.status).toBe('completed');
    expect(settledSnapshot.status).toBe('idle');
  });

  it('does not fail an admitted prompt when an idle snapshot contains only its durable user anchor', () => {
    const clientMessageId = 'client-user-anchor-before-runtime-busy';
    const optimistic = appendOptimisticAgentMessage(createAgentProjection('session-1'), {
      clientMessageId,
      text: '先显示这条消息，再等待回答',
      nowMs: 10,
    });

    const earlySnapshot = applyAgentSnapshot(optimistic, {
      messages: [{
        ...serverMessage(
          'server-user-anchor',
          'user',
          'turn-user-anchor',
          '先显示这条消息，再等待回答',
        ),
        clientMessageId,
      }],
      liveEvents: [],
      lastSequence: 1,
      resumeToken: 'session-1:1',
      status: 'idle',
    });

    expect(earlySnapshot.messageOrder).toEqual(['server-user-anchor']);
    expect(earlySnapshot.messagesById[`local:${clientMessageId}`]).toBeUndefined();
    expect(earlySnapshot.turnsById['turn-user-anchor']).toMatchObject({
      status: 'running',
    });
    expect(earlySnapshot.turnsById['turn-user-anchor']?.failure).toBeUndefined();
    expect(earlySnapshot.status).toBe('busy');

    const answered = reduceAgentEvents(earlySnapshot, [
      {
        ...agentEvent(2, 'message_completed', {
          message: serverMessage(
            'server-assistant-answer',
            'assistant',
            'turn-user-anchor',
            '这是唯一的一次回答。',
          ),
        }),
        turnId: 'turn-user-anchor',
      },
      {
        ...agentEvent(3, 'turn_completed', { status: 'completed' }),
        turnId: 'turn-user-anchor',
      },
    ]);

    expect(answered.messageOrder).toEqual([
      'server-user-anchor',
      'server-assistant-answer',
    ]);
    expect(answered.turnsById['turn-user-anchor']?.status).toBe('completed');
    expect(answered.status).toBe('idle');
  });

  it('does not reconcile a failed optimistic admission by transcript text alone', () => {
    const optimistic = appendOptimisticAgentMessage(createAgentProjection('session-1'), {
      clientMessageId: 'client-failed-same-text',
      text: '重复文本仍需保留失败证据',
      nowMs: 10,
    });
    const failed = failOptimisticAgentMessage(
      optimistic,
      'client-failed-same-text',
      '运行时拒绝',
      15,
    );

    const restored = applyAgentSnapshot(failed, {
      messages: [serverMessage('older-user', 'user', 'history:older-user', '重复文本仍需保留失败证据')],
      liveEvents: [],
      lastSequence: 9,
      resumeToken: 'session-1:9',
      status: 'idle',
    });

    expect(restored.messagesById['local:client-failed-same-text']).toMatchObject({
      status: 'failed',
    });
    expect(restored.optimisticByClientMessageId).toHaveProperty('client-failed-same-text');
  });

  it('keeps an unaccepted prompt failure across a Session snapshot refresh', () => {
    const optimistic = appendOptimisticAgentMessage(createAgentProjection('session-1'), {
      clientMessageId: 'client-failed',
      text: '发送失败后仍可重试',
      nowMs: 10,
    });
    const failed = failOptimisticAgentMessage(
      optimistic,
      'client-failed',
      '当前模型不可用，请切换模型后重试。',
      20,
    );

    const restored = applyAgentSnapshot(failed, {
      messages: [],
      liveEvents: [],
      lastSequence: 7,
      resumeToken: 'session-1:7',
      status: 'idle',
    });

    expect(restored.messagesById['local:client-failed'].status).toBe('failed');
    expect(restored.messagesById['local:client-failed'].completedAtMs).toBe(20);
    expect(restored.turnsById['local-turn:client-failed']).toMatchObject({
      status: 'failed',
      updatedAtMs: 20,
      failure: '当前模型不可用，请切换模型后重试。',
    });
  });

  it('requeues an ambiguous admission with the same message identity', () => {
    const optimistic = appendOptimisticAgentMessage(
      createAgentProjection('session-1'),
      {
        clientMessageId: 'client-ambiguous',
        text: '只发送一次',
        nowMs: 10,
      },
    );
    const ambiguous = failOptimisticAgentMessage(
      optimistic,
      'client-ambiguous',
      '暂时无法确认是否已接收',
      20,
      'ambiguous',
    );
    const requeued = requeueOptimisticAgentMessage(
      ambiguous,
      'client-ambiguous',
      30,
    );

    expect(requeued.messageOrder).toEqual([
      'local:client-ambiguous',
    ]);
    expect(
      requeued.messagesById['local:client-ambiguous'],
    ).toMatchObject({
      clientMessageId: 'client-ambiguous',
      status: 'queued',
      completedAtMs: null,
    });
    expect(
      requeued.messagesById['local:client-ambiguous']
        .admissionState,
    ).toBeUndefined();
    expect(
      requeued.turnsById['local-turn:client-ambiguous'],
    ).toMatchObject({ status: 'queued' });
    expect(
      requeued.turnsById['local-turn:client-ambiguous']
        .failure,
    ).toBeUndefined();
  });

  it('keeps an unresolved admission waiting on the original message', () => {
    const optimistic = appendOptimisticAgentMessage(
      createAgentProjection('session-1'),
      {
        clientMessageId: 'client-unresolved',
        text: '只能执行一次',
        nowMs: 10,
      },
    );
    const unresolved = failOptimisticAgentMessage(
      optimistic,
      'client-unresolved',
      '无法确认是否已执行，不能自动重试。',
      20,
      'unresolved',
    );

    expect(unresolved.messageOrder).toEqual([
      'local:client-unresolved',
    ]);
    expect(
      unresolved.messagesById['local:client-unresolved'],
    ).toMatchObject({
      clientMessageId: 'client-unresolved',
      status: 'queued',
      admissionState: 'unresolved',
    });
    expect(
      unresolved.turnsById['local-turn:client-unresolved'],
    ).toMatchObject({
      status: 'waiting',
    });
    expect(
      unresolved.turnsById['local-turn:client-unresolved']?.failure,
    ).toBeUndefined();
    expect(unresolved.status).toBe('busy');
  });

  it('preserves a non-retryable pending admission across snapshot refresh', () => {
    const optimistic = appendOptimisticAgentMessage(
      createAgentProjection('session-1'),
      {
        clientMessageId: 'client-pending',
        text: '等待服务端确认',
        nowMs: 10,
      },
    );
    const pending = failOptimisticAgentMessage(
      optimistic,
      'client-pending',
      '服务端仍在确认；系统不会自动重试。',
      20,
      'pending',
    );
    const restored = applyAgentSnapshot(pending, {
      messages: [],
      liveEvents: [],
      lastSequence: 7,
      resumeToken: 'session-1:7',
      status: 'idle',
    });

    expect(
      restored.messagesById['local:client-pending'],
    ).toMatchObject({
      status: 'queued',
      admissionState: 'pending',
    });
    expect(
      restored.turnsById['local-turn:client-pending'],
    ).toMatchObject({
      status: 'waiting',
    });
    expect(
      restored.turnsById['local-turn:client-pending']?.failure,
    ).toBeUndefined();
    expect(restored.status).toBe('busy');
  });

  it('retains an unscoped backend failure as a diagnostic without inventing a failed turn', () => {
    const reduced = reduceAgentEvent(
      createAgentProjection('session-1'),
      {
        ...agentEvent(8, 'turn_failed', { error: 'lost acknowledgement' }),
        turnId: '',
      },
    ).state;

    expect(reduced.turnOrder).toEqual([]);
    expect(reduced.turnsById).not.toHaveProperty('unscoped');
    expect(reduced.status).toBe('idle');
    expect(reduced.diagnostics).toEqual(expect.arrayContaining([
      expect.objectContaining({
        eventType: 'turn_failed',
        summary: expect.stringContaining('turnId'),
      }),
    ]));
  });

  it('settles a pending acknowledgement from the exact durable SSE identity', () => {
    const clientMessageId = 'client-pending-then-accepted';
    const optimistic = appendOptimisticAgentMessage(
      createAgentProjection('session-1'),
      {
        clientMessageId,
        text: '只执行一次并等待证据',
        nowMs: 10,
      },
    );
    const pending = failOptimisticAgentMessage(
      optimistic,
      clientMessageId,
      '服务端仍在确认。',
      20,
      'pending',
    );
    const durableUser = {
      ...serverMessage(
        'durable-user-after-lost-ack',
        'user',
        'turn-after-lost-ack',
        '只执行一次并等待证据',
      ),
      clientMessageId,
    };

    const settled = reduceAgentEvents(pending, [
      {
        ...agentEvent(9, 'message_completed', {
          clientMessageId,
          message: durableUser,
        }),
        turnId: 'turn-after-lost-ack',
      },
      {
        ...agentEvent(10, 'message_completed', {
          message: serverMessage(
            'durable-assistant-after-lost-ack',
            'assistant',
            'turn-after-lost-ack',
            '请求只执行了一次。',
          ),
        }),
        turnId: 'turn-after-lost-ack',
      },
      {
        ...agentEvent(11, 'turn_completed', { status: 'completed' }),
        turnId: 'turn-after-lost-ack',
      },
    ]);

    expect(settled.messagesById[`local:${clientMessageId}`]).toBeUndefined();
    expect(settled.messageOrder).toEqual([
      'durable-user-after-lost-ack',
      'durable-assistant-after-lost-ack',
    ]);
    expect(settled.turnsById[`local-turn:${clientMessageId}`]).toBeUndefined();
    expect(settled.turnsById['turn-after-lost-ack']).toMatchObject({
      status: 'completed',
    });
    expect(settled.status).toBe('idle');
  });

  it('discards a rejected optimistic retry without leaving a duplicate turn', () => {
    const optimistic = appendOptimisticAgentMessage(createAgentProjection('session-1'), {
      clientMessageId: 'client-conflict',
      text: '不要把并发冲突记成新对话',
      nowMs: 10,
    });

    const discarded = discardOptimisticAgentMessage(
      optimistic,
      'client-conflict',
    );

    expect(discarded.messageOrder).toEqual([]);
    expect(discarded.turnOrder).toEqual([]);
    expect(discarded.optimisticByClientMessageId).toEqual({});
    expect(discarded.status).toBe('idle');
  });

  it('retains unknown events and can abort an active turn without throwing', () => {
    const unknown = reduceAgentEvent(
      createAgentProjection('session-1'),
      parseAgentEvent({
        ...rawAgentEvent(1, 'future_tool_panel', { safe: 'summary' }),
      }),
    ).state;
    expect(unknown.diagnostics[0]).toMatchObject({
      eventType: 'future_tool_panel',
      payload: { safe: 'summary' },
    });

    const streaming = reduceAgentEvent(
      unknown,
      agentEvent(2, 'text_delta', { delta: 'partial' }),
    ).state;
    const aborted = abortAgentTurn(streaming, 'turn-1', 30);
    expect(aborted.turnsById['turn-1'].status).toBe('aborted');
    expect(aborted.messagesById['turn-1:assistant'].status).toBe('aborted');
  });

  it('projects an abort fallback terminal event as aborted instead of completed', () => {
    const streaming = reduceAgentEvent(
      createAgentProjection('session-1'),
      agentEvent(1, 'text_delta', { delta: 'partial' }),
    ).state;
    const stopped = reduceAgentEvent(
      streaming,
      agentEvent(2, 'turn_completed', {
        status: 'aborted',
        aborted: true,
        terminalEvent: 'abort_timeout',
      }),
    ).state;

    expect(stopped.status).toBe('idle');
    expect(stopped.turnsById['turn-1'].status).toBe('aborted');
    expect(stopped.messagesById['turn-1:assistant'].status).toBe('aborted');
  });

  it('does not reopen a stopped turn when its aborted message arrives after the terminal event', () => {
    const streaming = reduceAgentEvent(
      createAgentProjection('session-1'),
      agentEvent(1, 'text_delta', { delta: 'partial' }),
    ).state;
    const stopped = reduceAgentEvent(
      streaming,
      agentEvent(2, 'turn_completed', { status: 'aborted', aborted: true }),
    ).state;
    const abortedMessage = {
      ...serverMessage('assistant-stop', 'assistant', 'turn-1', '已停止。'),
      status: 'aborted',
      blocks: serverMessage('assistant-stop', 'assistant', 'turn-1', '已停止。').blocks.map(
        (block) => ({ ...block, status: 'aborted' }),
      ),
    };
    const restored = reduceAgentEvent(
      stopped,
      agentEvent(3, 'message_completed', { message: abortedMessage }),
    ).state;

    expect(restored.turnsById['turn-1'].status).toBe('aborted');
    expect(restored.messagesById['assistant-stop'].status).toBe('aborted');
  });

  it('removes a superseded failure activity when Stop wins the terminal race', () => {
    const streaming = reduceAgentEvent(
      createAgentProjection('session-1'),
      agentEvent(1, 'text_delta', { delta: 'partial' }),
    ).state;
    const aborting = reduceAgentEvent(
      streaming,
      agentEvent(2, 'status_changed', { status: 'aborting' }),
    ).state;
    const failed = reduceAgentEvent(
      aborting,
      agentEvent(3, 'turn_failed', { error: 'fetch failed after user abort' }),
    ).state;
    const stopped = reduceAgentEvent(
      failed,
      agentEvent(4, 'turn_completed', {
        status: 'aborted',
        aborted: true,
        terminalCorrection: true,
        terminalEvent: 'abort_failure_race',
      }),
    ).state;

    expect(stopped.turnsById['turn-1'].status).toBe('aborted');
    expect(stopped.turnsById['turn-1'].failure).toBeUndefined();
    expect(stopped.turnsById['turn-1'].activityIds).toEqual([]);
    expect(stopped.activityOrder).toEqual([]);
  });

  it('projects background job snapshots and terminal events by durable job id', () => {
    const restored = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [],
      liveEvents: [],
      lastSequence: 4,
      resumeToken: 'session-1:4',
      backgroundJobs: [backgroundJob('running', 40)],
    });
    const completed = reduceAgentEvent(
      restored,
      agentEvent(5, 'background_job_completed', {
        jobId: 'bg_0123456789abcdef0123456789abcdef',
        status: 'completed',
        summary: '后台任务已完成',
        job: backgroundJob('completed', 50),
      }),
    ).state;

    expect(completed.backgroundJobOrder).toEqual(['bg_0123456789abcdef0123456789abcdef']);
    expect(completed.backgroundJobsById.bg_0123456789abcdef0123456789abcdef).toMatchObject({
      status: 'completed',
      exitCode: 0,
      outputBytes: 12,
      updatedAtMs: 50,
    });
  });

  it('keeps an equal-time terminal SSE job over a cancelling receipt but accepts a newer retry', () => {
    const running = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [],
      liveEvents: [],
      lastSequence: 4,
      resumeToken: 'session-1:4',
      backgroundJobs: [backgroundJob('running', 40)],
    });
    const cancelled = reduceAgentEvent(
      running,
      agentEvent(5, 'background_job_cancelled', {
        jobId: 'bg_0123456789abcdef0123456789abcdef',
        status: 'cancelled',
        summary: '后台任务已停止',
        job: backgroundJob('cancelled', 50),
      }),
    ).state;

    const staleReceipt = applyAgentBackgroundJobReceipt(cancelled, {
      job: backgroundJob('cancelling', 50),
    });
    expect(staleReceipt).toBe(cancelled);
    expect(staleReceipt.backgroundJobsById.bg_0123456789abcdef0123456789abcdef.status)
      .toBe('cancelled');

    const newerRetry = applyAgentBackgroundJobReceipt(staleReceipt, {
      job: backgroundJob('running', 51),
    });
    expect(newerRetry.backgroundJobsById.bg_0123456789abcdef0123456789abcdef)
      .toMatchObject({ status: 'running', updatedAtMs: 51 });
  });

  it('keeps ancillary receipts inside their authoritative run instead of creating avatar-only turns', () => {
    const running = reduceAgentEvent(
      createAgentProjection('session-1'),
      {
        ...agentEvent(1, 'tool_started', {
          toolCallId: 'tool-current-run',
          toolName: 'workspace_search',
          runId: 'run-current',
        }),
        turnId: 'turn-current',
      },
    ).state;
    const checkpointed = reduceAgentEvent(
      running,
      {
        ...agentEvent(2, 'memory_checkpointed', {
          sourceRole: 'tool_receipt',
          status: 'checkpointed',
          summary: '已应用工具回执已保存为记忆来源，等待异步整理',
        }),
        turnId: '',
      },
    ).state;
    const approved = reduceAgentEvent(
      checkpointed,
      {
        ...agentEvent(3, 'approval_resolved', {
          approvalId: 'approval-truncated-owner',
          toolCallId: 'tool-outside-bounded-journal',
          state: 'applied',
          automatic: true,
          decisionMode: 'policy',
          runId: 'run-current',
        }),
        turnId: 'room-root:current',
      },
    ).state;

    expect(approved.turnOrder).toEqual(['turn-current']);
    expect(approved.turnsById['turn-current']).toMatchObject({ status: 'running' });
    expect(approved.activityOrder).toEqual(['tool-current-run']);
  });

  it('hides source-capture bookkeeping from the conversation timeline', () => {
    const captured = reduceAgentEvent(
      createAgentProjection('session-1'),
      agentEvent(1, 'memory_checkpointed', {
        sourceRole: 'user',
        status: 'checkpointed',
        summary: '最终用户消息已保存为记忆来源，等待异步整理',
      }),
    ).state;
    expect(captured.activityOrder).toEqual([]);
    expect(captured.turnOrder).toEqual([]);

    const receipt = reduceAgentEvent(
      captured,
      agentEvent(2, 'memory_checkpointed', {
        sourceRole: 'tool_receipt',
        status: 'checkpointed',
        summary: '已应用工具回执已保存为记忆来源，等待异步整理',
      }),
    ).state;
    expect(receipt.activityOrder).toEqual([]);
    expect(receipt.turnOrder).toEqual([]);
  });

  it('settles live tool state when an authoritative restored session is idle', () => {
    const turnId = '831ba902-637d-48aa-b92f-2b1c32c4c969';
    const completedMessage = {
      ...serverMessage('assistant-current', 'assistant', turnId, '本轮已经完成。'),
      completedAtMs: 1_101,
    };
    const restored = applyAgentSnapshot(createAgentProjection('session-1'), {
      messages: [completedMessage],
      liveEvents: [
        {
          ...rawAgentEvent(1_100, 'tool_started', {
            toolCallId: 'tool-current-run',
            toolName: 'edit',
          }),
          turnId,
        },
        {
          ...rawAgentEvent(1_101, 'message_completed', {
            message: completedMessage,
          }),
          turnId,
        },
      ],
      lastSequence: 1_101,
      resumeToken: 'session-1:1101',
      status: 'idle',
    });

    expect(restored.status).toBe('idle');
    expect(restored.turnsById[turnId]).toMatchObject({ status: 'completed' });
    expect(restored.activitiesById['tool-current-run']).toMatchObject({
      status: 'completed',
    });
  });
});

function lifecycleCancellationAudit(
  overrides: Partial<AgentLifecycleCancellationAuditV1> = {},
): AgentLifecycleCancellationAuditV1 {
  return {
    schemaVersion: 'rag-ime.agent-lifecycle-cancellation-audit.v1',
    requestId: 'lifecycle:goal:1',
    sessionId: 'session-1',
    scopeKind: 'goal',
    scopeId: 'goal:session-1',
    sourceRevision: 3,
    transitionRevision: 4,
    action: 'pause',
    reason: '用户暂停当前目标',
    state: 'pending',
    sourceTurnId: 'turn-1',
    owners: {
      runtime: { status: 'pending', receipt: {} },
      approval: { status: 'pending', receipt: {} },
      job: { status: 'pending', receipt: {} },
      delegation: { status: 'pending', receipt: {} },
    },
    createdAtMs: 50,
    updatedAtMs: 50,
    ...overrides,
  };
}

function rawAgentEvent(
  sequence: number,
  eventType: string,
  payload: Record<string, unknown>,
) {
  return {
    schemaVersion: 'rag-ime.agent-event.v1',
    eventId: `session-1:${sequence}`,
    sessionId: 'session-1',
    turnId: 'turn-1',
    sequence,
    createdAtMs: sequence * 10,
    eventType,
    payload: {
      messageId: 'turn-1:assistant',
      blockId: 'turn-1:assistant:text',
      ...payload,
    },
    resumeToken: `session-1:${sequence}`,
  };
}

function serverMessage(id: string, role: 'user' | 'assistant', turnId: string, text: string) {
  return {
    schemaVersion: 'rag-ime.agent-message.v1',
    id,
    sessionId: 'session-1',
    turnId,
    role,
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
    createdAtMs: 20,
    completedAtMs: 21,
  };
}

function textOf(message: { blocks: { data: Record<string, unknown> }[] }): string {
  return String(message.blocks[0]?.data.text ?? '');
}

function todoProjection(overrides: Partial<AgentTodoProjection> = {}): AgentTodoProjection {
  return {
    schemaVersion: 'rag-ime.agent-todo.v1',
    id: 'todo:session-1',
    sessionId: 'session-1',
    revision: 1,
    actor: 'agent',
    updatedAtMs: 10,
    phases: [],
    counts: { total: 0, pending: 0, inProgress: 0, completed: 0, abandoned: 0 },
    ...overrides,
    roomLineage: overrides.roomLineage ?? null,
  };
}

function backgroundJob(
  status: AgentBackgroundJobV1['status'],
  updatedAtMs: number,
): AgentBackgroundJobV1 {
  const terminal = (
    status === 'completed'
    || status === 'failed'
    || status === 'cancelled'
    || status === 'orphaned'
  );
  return {
    schemaVersion: 'rag-ime.agent-background-job.v1',
    jobId: 'bg_0123456789abcdef0123456789abcdef',
    sessionId: 'session-1',
    label: '后台检查',
    status,
    command: 'python3 check.py',
    commandSha256: 'a'.repeat(64),
    cwd: '/tmp/project',
    networkAllowed: false,
    maxRunSeconds: 60,
    pid: terminal || status === 'queued' ? null : 42,
    createdAtMs: 10,
    startedAtMs: status === 'queued' ? 0 : 11,
    updatedAtMs,
    endedAtMs: terminal ? updatedAtMs : 0,
    exitCode: status === 'completed' ? 0 : status === 'failed' ? 1 : null,
    outputBytes: 12,
    logStartCursor: 0,
    logTruncated: false,
    cancelRequestedAtMs: status === 'cancelling' || status === 'cancelled'
      ? updatedAtMs
      : 0,
    error: '',
    approvalId: 'approval-1',
    causalMetadata: {
      todoId: 'todo-1',
      todoRevision: 1,
      goalId: 'goal-1',
      goalRevision: 1,
      turnId: 'turn-1',
      roomBound: false,
    },
  };
}
