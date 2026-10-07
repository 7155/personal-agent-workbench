import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { abortAgentTurn, applyAgentSnapshot, createAgentProjection, reduceAgentEvents } from '@/contracts/agent-reducer';
import { agentEventFixture, roomEventFixture } from '@/test/fixtures/events';
import { createRoomProjection, reduceRoomEvents } from '@/contracts/room-reducer';
import { FxActivityStack } from './ActivitySummary';
import { publicToolResultView } from './public-tool-result';

afterEach(cleanup);
describe('tool receipts on authoritative turn cancellation', () => {
  // https://github.com/7155/personal-agent-workbench/issues/144
  it('uses the exact Durable tool outcome in live and cold UI without rewriting other terminal tools', () => {
    const events = [
      agentEventFixture(1, 'tool_finished', { toolCallId: 'successful-read', toolName: 'read', result: { content: [{ type: 'text', text: 'original contents' }] } }),
      agentEventFixture(2, 'tool_finished', { toolCallId: 'genuine-error', toolName: 'bash', isError: true, result: { content: [{ type: 'text', text: 'aborted by an external script' }] } }),
      agentEventFixture(3, 'tool_started', { toolCallId: 'stopped-call', toolName: 'bash', args: { command: 'sleep 60' } }),
      agentEventFixture(4, 'tool_finished', {
        toolCallId: 'stopped-call', toolName: 'bash', isError: true,
        result: { content: [{ type: 'text', text: 'original harness error' }] },
        durableToolOutcome: abortedOutcome(),
      }),
      agentEventFixture(5, 'turn_completed', { status: 'aborted' }),
    ];
    const live = reduceAgentEvents(createAgentProjection('session-1'), events);
    const cold = applyAgentSnapshot(createAgentProjection('session-1'), {
      sessionId: 'session-1', runtimeEngine: 'durable', status: 'idle', messages: [], liveEvents: events,
      lastSequence: 5, resumeToken: 'session-1:5',
    });
    for (const state of [live, cold]) {
      expect(state.activitiesById['successful-read'].status).toBe('completed');
      expect(state.activitiesById['genuine-error'].status).toBe('failed');
      const stopped = state.activitiesById['stopped-call'];
      expect(stopped.status).toBe('aborted');
      expect(stopped.payload.result).toEqual(events[3].payload.result);
      expect(publicToolResultView(stopped).fields.find(item => item.id === 'status')?.value).toBe('已停止');
      expect(publicToolResultView(stopped).error).toBeUndefined();
      expect(publicToolResultView(state.activitiesById['genuine-error']).error).toContain('aborted by an external script');
    }
    const view = render(<FxActivityStack activities={[cold.activitiesById['stopped-call']]} />);
    fireEvent.click(screen.getByRole('button', { name: /运行命令.*已停止/ }));
    expect(view.container.querySelector('.ccui-execution-mark')).toHaveAttribute('data-state', 'cancelled');
    expect(screen.queryByText('失败')).not.toBeInTheDocument();
    expect(screen.getAllByText('已停止').length).toBeGreaterThan(0);
  });

  it.each(['sessionId', 'turnId', 'toolCallId', 'toolName', 'schemaVersion', 'entryId', 'taskId'])(
    'retains a failure when the top-level owner receipt has a mismatched or invalid %s', (field) => {
      const state = reduceAgentEvents(createAgentProjection('session-1'), [
        agentEventFixture(1, 'tool_finished', {
          toolCallId: 'stopped-call', toolName: 'bash', isError: true,
          durableToolOutcome: { ...abortedOutcome(), [field]: 'foreign' },
          result: { durableToolOutcome: abortedOutcome(), status: 'aborted', content: [{ type: 'text', text: 'aborted' }] },
        }),
        agentEventFixture(2, 'turn_completed', { status: 'aborted' }),
      ]);
      expect(state.activitiesById['stopped-call'].status).toBe('failed');
      expect(state.activitiesById['stopped-call'].settledByTurnStatus).toBeUndefined();
    },
  );

  it('treats an outcome embedded in arbitrary tool output as output rather than cancellation authority', () => {
    const state = reduceAgentEvents(createAgentProjection('session-1'), [agentEventFixture(1, 'tool_finished', {
      toolCallId: 'stopped-call', toolName: 'bash', isError: true,
      result: { details: { durableToolOutcome: abortedOutcome() }, content: [{ type: 'text', text: 'aborted' }] },
    })]);
    expect(state.activitiesById['stopped-call'].status).toBe('failed');
  });

  it('binds a Room tool outcome to the original participant Session turn rather than its Root', () => {
    const owner = { ...abortedOutcome(), sessionId: 'session-room-1', turnId: 'original-participant-turn' };
    const makeEvent = (durableToolOutcome: unknown) => roomEventFixture(1, 'participant_activity', {
      sourceEventType: 'tool_finished', sourceTurnId: 'original-participant-turn',
      toolCallId: 'stopped-call', toolName: 'bash', isError: true, durableToolOutcome,
    });
    for (const [outcome, expected] of [[owner, 'aborted'], [{ ...owner, turnId: 'room-turn-1' }, 'failed']] as const) {
      const state = reduceRoomEvents(createRoomProjection('room-1'), [makeEvent(outcome)]);
      expect(Object.values(state.activitiesById).find(activity => activity.payload.toolCallId === 'stopped-call')?.status).toBe(expected);
    }
  });

  it.each(['tool_started', 'tool_progress', 'tool_finished'])('keeps the original Room stopped receipt after a late %s', (sourceEventType) => {
    const owner = { ...abortedOutcome(), sessionId: 'session-room-1', turnId: 'original-participant-turn' };
    const base = { sourceTurnId: owner.turnId, toolCallId: owner.toolCallId, toolName: owner.toolName };
    const settled = reduceRoomEvents(createRoomProjection('room-1'), [roomEventFixture(1, 'participant_activity', {
      ...base, sourceEventType: 'tool_finished', isError: true, durableToolOutcome: owner,
      result: { content: [{ type: 'text', text: 'original receipt' }] },
    })]);
    const later = reduceRoomEvents(settled, [roomEventFixture(2, 'participant_activity', { ...base, sourceEventType, isError: false })]);
    expect(later.activitiesById).toEqual(settled.activitiesById);
    expect(later.lastSequence).toBe(2);
  });

  it.each(['tool_started', 'tool_progress', 'tool_finished'])('keeps the exact stopped outcome quiet after a late %s while admitting a new turn', (eventType) => {
    const settled = reduceAgentEvents(createAgentProjection('session-1'), [
      agentEventFixture(1, 'tool_finished', { toolCallId: 'stopped-call', toolName: 'bash', isError: true,
        durableToolOutcome: abortedOutcome(), result: { content: [{ type: 'text', text: 'original receipt' }] } }),
      agentEventFixture(2, 'turn_completed', { status: 'aborted' }),
    ]);
    const late = reduceAgentEvents(settled, [agentEventFixture(3, eventType, {
      toolCallId: 'stopped-call', toolName: 'bash', summary: 'late update', isError: false,
    })]);
    expect(late.activitiesById['stopped-call']).toEqual(settled.activitiesById['stopped-call']);
    expect(late.status).toBe('idle');
    expect(late.lastSequence).toBe(3);
    const successor = reduceAgentEvents(late, [{ ...agentEventFixture(4, 'tool_started', {
      toolCallId: 'stopped-call', toolName: 'bash',
    }), turnId: 'turn-2' }]);
    expect(successor.activitiesById['stopped-call'].status).toBe('running');
    expect(successor.activitiesById['stopped-call'].turnId).toBe('turn-2');
  });

  it('preserves a completed tool and marks only interrupted work as stopped in both summary and detail', () => {
    const before = reduceAgentEvents(createAgentProjection('session-1'), [
      agentEventFixture(1, 'tool_started', { toolCallId: 'completed-read', toolName: 'read', args: { path: 'src/app.ts' } }),
      agentEventFixture(2, 'tool_finished', { toolCallId: 'completed-read', toolName: 'read', result: { content: [{ type: 'text', text: 'file contents' }] } }),
      agentEventFixture(3, 'tool_started', { toolCallId: 'pending-test', toolName: 'bash', args: { command: 'pnpm test' } }),
    ]);
    const stopped = abortAgentTurn(before, 'turn-1', 100);
    const activities = Object.values(stopped.activitiesById);
    const completed = activities.find(item => item.payload.toolCallId === 'completed-read')!;
    const interrupted = activities.find(item => item.payload.toolCallId === 'pending-test')!;
    expect(completed.settledByTurnStatus).toBeUndefined();
    expect(interrupted.settledByTurnStatus).toBe('aborted');
    expect(publicToolResultView(completed).fields.find(item => item.id === 'status')?.value).toBe('已完成');
    expect(publicToolResultView(interrupted).fields.find(item => item.id === 'status')?.value).toBe('已停止');
    const view = render(<FxActivityStack activities={[interrupted]} />);
    const row = screen.getByRole('button', { name: /pnpm test.*已停止/ });
    fireEvent.click(row);
    expect(view.container.querySelector('.ccui-execution-mark')).toHaveAttribute('data-state', 'cancelled');
    expect(screen.queryByText('已完成')).not.toBeInTheDocument();
    expect(screen.getAllByText('已停止').length).toBeGreaterThan(0);
  });
});

function abortedOutcome() {
  return {
    schemaVersion: 'rag-ime.pi-durable-tool-outcome.v1', status: 'aborted',
    sessionId: 'session-1', runtimeSessionId: 'd83d8430-2260-4ae8-ad3a-229a5279ce09', turnId: 'turn-1', clientMessageId: 'client-1',
    toolCallId: 'stopped-call', toolName: 'bash', entryId: 'durable:30', taskId: 'durable:task:20',
    assistantEntryId: 'durable:29', generationTaskId: 'durable:task:19',
  };
}
