import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { parseJevSnapshot } from '@/features/semantic-workspace/jev-execution';
import { PawRoomResponseStatus, roomResponsePhase } from './PawRoomResponseStatus';

afterEach(cleanup);

function graph(overrides: Record<string, unknown> = {}) {
  return parseJevSnapshot({
    ok: true,
    mode: 'jev',
    graphId: 'graph-room-response',
    roomId: 'room-response',
    rootId: 'root-room-response',
    snapshotVersion: 'v1',
    phase: 'execute',
    tasks: [{
      id: 'task-one', state: 'active', revision: 1, owner_id: 'participant-one', parent_id: '',
      objective: '执行任务', expected_output: '', acceptance: [], result: '', artifacts: [], evidence: [], accepted_turn_id: '',
    }],
    ready: [], running: ['task-one'], review: [], blocked: [], events: [], effects: [], final: null,
    ...overrides,
  }, 'graph-room-response');
}

describe('Paw Room response status', () => {
  it('renders immediately while the send request is being admitted', () => {
    expect(roomResponsePhase({ submitting: true, busy: false, stopping: false, graph: null })).toBe('正在提交消息');
    render(<PawRoomResponseStatus submitting busy={false} stopping={false} graph={null} />);
    expect(screen.getByRole('status')).toHaveTextContent('Thinking正在提交消息');
  });

  it('uses the nullable final field and exact dispatch execution status', () => {
    const running = graph({ effects: [{
      effectId: 'dispatch-verify', operation: 'dispatch', state: 'accepted', executionStatus: 'running',
      request: { taskId: 'task-one', taskRevision: 1, purpose: 'verify' }, receipt: {},
    }] });
    expect(roomResponsePhase({ submitting: false, busy: true, stopping: false, graph: running })).toBe('正在核对结果');
    expect(roomResponsePhase({ submitting: false, busy: true, stopping: false, graph: graph({ final: { content: '已完成', status: 'completed', evidenceRefs: [] }, running: [] }) })).toBe('');
    expect(roomResponsePhase({ submitting: false, busy: true, stopping: false, graph: graph({ effects: [{
      effectId: 'dispatch-unknown', operation: 'dispatch', state: 'unknown', executionStatus: 'unknown', request: {}, receipt: {},
    }] }) })).toBe('正在核实执行回执');
  });
});
