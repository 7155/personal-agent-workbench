import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import {
  abortAgentTurn, agentSnapshotFromResponse, applyAgentSnapshot, createAgentProjection,
  reduceAgentEvent, type AgentProjectionState,
} from '@/contracts/agent-reducer';
import { agentEventFixture as agentEvent } from '@/test/fixtures/events';
import { FxActivityStack, resetActivityDisclosureOverrides } from './ActivitySummary';
import { publicToolResultView } from './public-tool-result';

// Synthetic regressions use only the previously observed policy/ledger read
// outcomes and mixed child terminal states. These are not live model results.
const parentCall = 'parent:delegate';
const batchId = 'batch:policy-ledger';
const started = () => agentEvent(1, 'tool_started', {
  toolName: 'agents', toolCallId: parentCall, args: { op: 'delegate' },
});
const initial = () => reduceAgentEvent(createAgentProjection('session-1'), started()).state;
const owner = (state: AgentProjectionState) => state.activitiesById[parentCall]!;
const view = (state: AgentProjectionState) => publicToolResultView(owner(state));
function child(sequence: number, ordinal: number, fields: Record<string, unknown> = {}) {
  return {
    ...agentEvent(sequence, 'tool_progress', {
      toolName: 'agents', toolCallId: `subagent:${batchId}`, batchId,
      parentToolCallId: parentCall, parentTurnId: 'turn-1', runId: `child:${ordinal}`,
      childProgress: {
        runId: `child:${ordinal}`, childSessionId: `session:child:${ordinal}`,
        ordinal, templateId: 'researcher', state: 'running', phase: 'tool_started',
        sourceEventId: `source:${sequence}`, sourceEventType: 'tool_started',
        updatedAtMs: 1000 + sequence, toolName: 'read', toolCallId: `read:${ordinal}`,
        fileName: ordinal === 0 ? 'ledger-evidence.json' : 'policy-evidence.json',
        isError: false, ...fields,
      },
    }),
    turnId: '',
  };
}
function advance(state: AgentProjectionState, sequence: number, ordinal: number, fields: Record<string, unknown> = {}) {
  return reduceAgentEvent(state, child(sequence, ordinal, fields)).state;
}
const readFinished = { phase: 'tool_finished', sourceEventType: 'tool_finished' };
const returned = {
  phase: 'terminal', sourceEventType: 'completed', state: 'completed', fileName: '',
  deliveryStatus: 'returned', verificationStatus: 'unverified',
};
const failed = {
  phase: 'terminal', sourceEventType: 'failed', state: 'failed', fileName: '',
  failureReason: 'output_budget',
};

afterEach(() => { cleanup(); resetActivityDisclosureOverrides(); });

describe('delegated child progress on the owning Tool', () => {
  it('uses a bounded task title and keeps role secondary without copying the full prompt', () => {
    const state = advance(initial(), 2, 0, {
      taskLabel: `核对账本 ${'材料'.repeat(50)}\nPRIVATE_PROMPT_BODY`,
    });
    const item = view(state).resultItems[0]!;
    expect(item.label).toMatch(/^核对账本/u);
    expect(item.label.length).toBeLessThanOrEqual(32);
    expect(item.title?.length).toBeLessThanOrEqual(80);
    expect(item.title).toContain('核对账本');
    expect(item.title).not.toContain('PRIVATE_PROMPT_BODY');
    expect(item.label).not.toContain('PRIVATE_PROMPT_BODY');
    expect(item.label).not.toContain('researcher');
    expect(item.text).toContain('研究员');
    const mounted = render(<FxActivityStack activities={[owner(state)]} sessionId="session-1" />);
    fireEvent.click(mounted.container.querySelector('.paw-activity')!);
    const title = mounted.container.querySelector('.agent-tool-list-result li > strong');
    expect(title).toHaveAttribute('title', item.title);
    expect(title).toHaveStyle({ WebkitLineClamp: '2', overflow: 'hidden' });
    const final = reduceAgentEvent(state, agentEvent(3, 'tool_finished', {
      toolName: 'agents', toolCallId: parentCall, result: { batch: { runs: [{
        id: 'child:0', ordinal: 0, templateId: 'researcher', state: 'completed',
        task: '整理政策适用规则\nPRIVATE_PROMPT_BODY',
        result: { deliveryStatus: 'returned', verificationStatus: 'unverified' },
      }] } },
    })).state;
    expect(view(final).resultItems[0]).toMatchObject({
      label: '整理政策适用规则', text: '结果已返回，待核验 · 研究员',
    });
  });

  it('keeps two interleaved reads separate and renders their truthful mixed outcomes', () => {
    const base = initial();
    let state = advance(base, 2, 1);
    state = advance(state, 3, 0);
    expect(view(state).resultItems.map(item => item.id)).toEqual(['child:0', 'child:1']);
    expect(view(state).resultItems.map(item => item.text)).toEqual([
      '正在读取 ledger-evidence.json · 研究员', '正在读取 policy-evidence.json · 研究员',
    ]);
    expect(owner(state).status).toBe(owner(base).status);
    expect(owner(state).updatedAtMs).toBe(owner(base).updatedAtMs);
    expect(state.status).toBe(base.status);
    expect(state.turnOrder).toEqual(['turn-1']);
    expect(state.activityOrder).toEqual([parentCall]);
    state = advance(state, 4, 1, readFinished);
    state = advance(state, 5, 0, readFinished);
    const mounted = render(<FxActivityStack activities={[owner(state)]} sessionId="session-1" />);
    fireEvent.click(mounted.container.querySelector('.paw-activity')!);
    expect(mounted.container).toHaveTextContent('已读取 policy-evidence.json · 研究员');
    expect(mounted.container).toHaveTextContent('已读取 ledger-evidence.json · 研究员');
    state = advance(state, 6, 0, { ...readFinished, fileName: 'late-receipts.json', isError: true });
    expect(view(state).resultItems[0]?.text).toBe('读取失败 late-receipts.json · 研究员');
    expect(owner(state).status).toBe('running');
    state = advance(state, 7, 1, returned);
    state = advance(state, 8, 0, failed);
    mounted.rerender(<FxActivityStack activities={[owner(state)]} sessionId="session-1" />);
    expect(mounted.container.querySelector('.paw-activity')).toHaveAttribute('aria-expanded', 'true');
    expect(mounted.container).toHaveTextContent('结果已返回，待核验 · 研究员');
    expect(mounted.container).toHaveTextContent('失败：输出预算已用尽 · 研究员');
    expect(mounted.container.querySelectorAll('.agent-tool-list-result li')).toHaveLength(2);
  });

  it.each([
    { parentToolCallId: 'unrelated:tool' }, { parentTurnId: 'unrelated:turn' },
    { runId: 'unrelated:child' }, { batchId: 'unrelated:batch' },
  ])('ignores mismatched correlation %j without creating a parent turn', (override) => {
    const state = initial();
    const event = child(2, 0);
    const next = reduceAgentEvent(state, { ...event, payload: { ...event.payload, ...override } }).state;
    expect(owner(next)).toEqual(owner(state));
    expect(next.turnOrder).toEqual(state.turnOrder);
    expect(next.activityOrder).toEqual(state.activityOrder);
    expect(next.status).toBe(state.status);
  });

  it('ignores foreign Sessions, reused child ordinals and switched batches', () => {
    const base = initial();
    expect(reduceAgentEvent(base, { ...child(2, 0), sessionId: 'foreign' }).state).toBe(base);
    let state = advance(base, 2, 0);
    const prior = owner(state);
    const collision = child(3, 0, { runId: 'other:child', childSessionId: 'other:session' });
    state = reduceAgentEvent(state, { ...collision, payload: { ...collision.payload, runId: 'other:child' } }).state;
    expect(owner(state)).toBe(prior);
    const switched = child(4, 1);
    state = reduceAgentEvent(state, { ...switched, payload: {
      ...switched.payload, batchId: 'other:batch', toolCallId: 'subagent:other:batch',
    } }).state;
    expect(owner(state)).toBe(prior);
  });

  it('ignores stale, duplicate and late nonterminal child receipts', () => {
    let state = advance(initial(), 2, 0, readFinished);
    const first = owner(state);
    state = advance(state, 3, 0, { updatedAtMs: 1001 });
    expect(owner(state)).toBe(first);
    const terminal = child(4, 0, failed);
    state = reduceAgentEvent(state, terminal).state;
    expect(reduceAgentEvent(state, terminal).state).toBe(state);
    const final = owner(state);
    state = advance(state, 5, 0);
    expect(owner(state)).toBe(final);
    expect(view(state).resultItems[0]?.text).toBe('失败：输出预算已用尽 · 研究员');
  });

  it('updates asynchronous child evidence without reopening its finished parent turn', () => {
    let state = initial();
    state = reduceAgentEvent(state, agentEvent(2, 'tool_finished', {
      toolName: 'agents', toolCallId: parentCall, result: { accepted: true },
    })).state;
    state = reduceAgentEvent(state, agentEvent(3, 'turn_completed', { status: 'completed' })).state;
    const parent = owner(state);
    const status = state.status;
    state = advance(state, 4, 0);
    expect(view(state).resultItems).toHaveLength(1);
    expect(owner(state).status).toBe(parent.status);
    expect(owner(state).updatedAtMs).toBe(parent.updatedAtMs);
    expect(state.turnsById['turn-1']?.status).toBe('completed');
    expect(state.status).toBe(status);
  });

  it('preserves parent cancellation and a child terminal state against late activity', () => {
    let state = advance(initial(), 2, 0, failed);
    state = abortAgentTurn(state, 'turn-1', 2000);
    const cancelled = owner(state);
    state = advance(state, 3, 0);
    expect(owner(state)).toBe(cancelled);
    expect(state.turnsById['turn-1']?.status).toBe('aborted');
    expect(state.status).toBe('idle');
    expect(view(state).resultItems[0]?.text).toBe('失败：输出预算已用尽 · 研究员');
    state = advance(state, 4, 1);
    expect(view(state).resultItems).toHaveLength(1);
  });

  it('uses explicit input requests for waiting and does not infer a wait from silence', () => {
    let state = advance(initial(), 2, 0, { phase: 'running', sourceEventType: 'running', fileName: '' });
    expect(view(state).resultItems[0]?.text).toBe('进行中 · 研究员');
    state = advance(state, 3, 0, { phase: 'waiting', sourceEventType: 'user_input_required' });
    expect(view(state).resultItems[0]?.text).toBe('等待补充信息 · 研究员');
    state = advance(state, 4, 0, { phase: 'running', sourceEventType: 'user_input_required', fileName: '' });
    expect(view(state).resultItems[0]?.text).toBe('进行中 · 研究员');
  });

  it('replays scoped evidence and degrades honestly when refresh has no child receipts', () => {
    const replay = applyAgentSnapshot(createAgentProjection('session-1'), agentSnapshotFromResponse({
      sessionId: 'session-1', items: [], liveEvents: [started(), child(2, 1, readFinished)],
      lastSequence: 2, status: 'working',
    }));
    expect(view(replay).resultItems[0]?.text).toBe('已读取 policy-evidence.json · 研究员');
    const missing = applyAgentSnapshot(createAgentProjection('session-1'), agentSnapshotFromResponse({
      sessionId: 'session-1', items: [], liveEvents: [started()], lastSequence: 1, status: 'working',
    }));
    expect(view(missing).resultItems).toEqual([]);
    const orphan = reduceAgentEvent(createAgentProjection('session-1'), child(1, 0, readFinished)).state;
    expect(orphan.turnOrder).toEqual([]);
    expect(orphan.activityOrder).toEqual([]);
    expect(orphan.status).toBe('idle');
  });

  it('uses the terminal batch result over earlier child progress after reconnect', () => {
    let state = advance(initial(), 2, 0, readFinished);
    state = reduceAgentEvent(state, agentEvent(3, 'tool_finished', {
      toolName: 'agents', toolCallId: parentCall,
      result: { batch: { id: batchId, state: 'failed', runs: [
        { id: 'child:0', ordinal: 0, templateId: 'researcher', state: 'failed', error: 'output budget exceeded' },
        { id: 'child:1', ordinal: 1, templateId: 'researcher', state: 'completed',
          result: { deliveryStatus: 'returned', verificationStatus: 'unverified' } },
      ] } },
    })).state;
    expect(view(state).resultItems.map(item => item.text)).toEqual([
      '失败：输出预算已用尽 · 研究员', '结果已返回，待核验 · 研究员',
    ]);
  });
});
