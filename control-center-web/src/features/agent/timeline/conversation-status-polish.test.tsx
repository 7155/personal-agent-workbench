import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { UiAgentMessage } from '@/contracts/ui-events';
import { agentEventFixture } from '@/test/fixtures/events';
import agentFxCss from '@/paw-os/styles/paw-os-agent-fx.css?raw';
import { useAgentLiveStore } from '../state/live-store';
import agentCss from '../agent.css?raw';
import { AgentTurn } from './AgentTimeline';

afterEach(() => {
  cleanup();
  useAgentLiveStore.getState().clear('session-status-polish');
  vi.useRealTimers();
});

describe('conversation status polish', () => {
  it('recognizes visible reply text without treating message completion as turn completion', () => {
    vi.useFakeTimers({ now: 12_000 });
    const sessionId = 'session-status-polish';
    const turnId = 'turn-status-polish';
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId)], liveEvents: [], lastSequence: 0, resumeToken: '', status: 'busy', partial: true,
    });
    const view = render(<AgentTurn sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />);
    expect(view.container.querySelector('.agent-assistant-pending')).toHaveTextContent('本轮尚未收到响应进展');
    act(() => useAgentLiveStore.getState().applyEvents(sessionId, [{
      ...agentEventFixture(1, 'text_delta', { messageId: `${turnId}:assistant`, delta: 'UI_STOP\n1. 已显示正文' }), sessionId, turnId,
    }]));
    expect(view.container.querySelector('.agent-assistant-pending')).toHaveTextContent('回复已开始');
    expect(view.container.querySelector('.agent-assistant-pending')).toHaveTextContent('本轮用时 12秒');
    expect(view.container.querySelector('.agent-assistant-pending')).not.toHaveTextContent('本轮尚未收到响应进展');
    expect(view.container.querySelector('.agent-assistant-pending')).not.toHaveTextContent('正在输出');
    act(() => useAgentLiveStore.getState().applyEvents(sessionId, [{
      ...agentEventFixture(2, 'message_completed', { message: {
        ...userMessage(sessionId, turnId), id: `${turnId}:assistant`, role: 'assistant',
        blocks: [{ id: `${turnId}:assistant:text`, type: 'text', status: 'completed', presentationKind: 'markdown', data: { text: 'UI_STOP\n1. 已显示正文' } }],
      } }), sessionId, turnId,
    }]));
    expect(view.container.querySelector('.agent-assistant-pending')).toHaveTextContent('等待本轮结束');
    expect(view.container.querySelector('.agent-assistant-pending')).toHaveTextContent('已收到回复，尚未收到本轮结束回执');
    expect(useAgentLiveStore.getState().projections[sessionId]?.turnsById[turnId]?.status).toBe('running');
    act(() => useAgentLiveStore.getState().applyEvents(sessionId, [{
      ...agentEventFixture(3, 'turn_completed', { status: 'completed' }), sessionId, turnId,
    }]));
    expect(view.container.querySelector('.agent-assistant-pending')).not.toBeInTheDocument();
  });

  it('does not count a user message or whitespace-only assistant delta as visible reply progress', () => {
    const sessionId = 'session-status-polish';
    const turnId = 'turn-status-polish';
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId)], liveEvents: [], lastSequence: 0, resumeToken: '', status: 'busy', partial: true,
    });
    const view = render(<AgentTurn sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />);
    act(() => useAgentLiveStore.getState().applyEvents(sessionId, [{
      ...agentEventFixture(1, 'text_delta', { delta: ' \n ' }), sessionId, turnId,
    }]));
    expect(view.container.querySelector('.agent-assistant-pending')).toHaveTextContent('本轮尚未收到响应进展');
    expect(view.container.querySelector('.agent-assistant-pending')).not.toHaveTextContent('回复已开始');
  });

  it.each([
    ['status_changed', { status: 'retrying', phase: 'provider_retry', activityState: 'running', summary: '模型连接暂时不可用，正在自动重试。' }, '正在重试连接'],
    ['compaction_started', { reason: 'automatic' }, '正在整理上下文'],
    ['tool_started', { toolCallId: 'after-reply', toolName: 'read' }, '正在执行'],
    ['status_changed', { status: 'aborting' }, '正在停止'],
  ] as const)('keeps %s control feedback above previously visible reply text', (eventType, payload, phase) => {
    const sessionId = 'session-status-polish';
    const turnId = 'turn-status-polish';
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId)], liveEvents: [], lastSequence: 0, resumeToken: '', status: 'busy', partial: true,
    });
    const view = render(<AgentTurn sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />);
    act(() => useAgentLiveStore.getState().applyEvents(sessionId, [
      { ...agentEventFixture(1, 'text_delta', { delta: '已显示的部分回复' }), sessionId, turnId },
      { ...agentEventFixture(2, eventType, payload), sessionId, turnId },
    ]));
    expect(view.container.querySelector('.agent-assistant-pending')).toHaveTextContent(phase);
    expect(view.container.querySelector('.agent-assistant-pending')).not.toHaveTextContent('回复已开始');
    act(() => useAgentLiveStore.getState().applyEvents(sessionId, [{
      ...agentEventFixture(3, 'turn_failed', { error: '模型连接未恢复' }), sessionId, turnId,
    }]));
    expect(view.container.querySelector('.agent-assistant-pending')).not.toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('本轮未完成');
  });

  it('labels the whole turn clock honestly after tools finish without inventing reasoning or a terminal receipt', () => {
    const now = 1_800_000_000_000;
    vi.useFakeTimers();
    vi.setSystemTime(now);
    const sessionId = 'session-status-polish';
    const turnId = 'turn-status-polish';
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [{ ...userMessage(sessionId, turnId), createdAtMs: now - 696_000, completedAtMs: now - 695_999 }],
      liveEvents: [{
        ...agentEventFixture(1, 'tool_finished', { toolCallId: 'finished-memory', toolName: 'memory', result: { ok: true } }),
        sessionId, turnId, createdAtMs: now - 5_000,
      }],
      lastSequence: 1, resumeToken: `${sessionId}:1`, status: 'busy', partial: true,
    });
    const view = render(<AgentTurn sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />);
    const pending = view.container.querySelector('.agent-assistant-pending')!;
    expect(pending).toHaveTextContent('等待后续响应');
    expect(pending).toHaveTextContent('本轮用时 11分 36秒');
    expect(pending).toHaveTextContent('尚未收到本轮结束回执');
    expect(pending).not.toHaveTextContent('Thinking');
    expect(pending).not.toHaveTextContent('等待模型');
    expect(pending.querySelector('time')).toHaveAttribute('aria-hidden', 'true');
    act(() => vi.advanceTimersByTime(5_000));
    expect(pending).toHaveTextContent('本轮用时 11分 41秒');
    expect(useAgentLiveStore.getState().projections[sessionId]?.turnsById[turnId]?.status).toBe('running');
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('does not show a live marker on an old running turn behind a newer terminal turn', () => {
    const sessionId = 'session-status-polish';
    const turnId = 'turn-status-polish';
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId)], liveEvents: [], lastSequence: 0, resumeToken: '', status: 'busy', partial: true,
    });
    useAgentLiveStore.getState().appendOptimistic(sessionId, { clientMessageId: 'later-work', turnId: 'turn-later', text: '后续工作', nowMs: 1_000 });
    useAgentLiveStore.getState().applyEvents(sessionId, [{
      ...agentEventFixture(1, 'turn_completed', { status: 'completed' }), sessionId, turnId: 'turn-later', createdAtMs: 2_000,
    }]);
    expect(useAgentLiveStore.getState().projections[sessionId]?.turnsById[turnId]?.status).toBe('running');
    const view = render(<AgentTurn sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />);
    expect(view.container.querySelector('.agent-assistant-pending')).not.toBeInTheDocument();
  });

  it('keeps an explicit Provider retry visible behind a rejected follow-up and removes it on its terminal receipt', () => {
    const sessionId = 'session-status-polish';
    const turnId = 'turn-status-polish';
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId)], liveEvents: [], lastSequence: 0, resumeToken: '', status: 'busy', partial: true,
    });
    useAgentLiveStore.getState().appendOptimistic(sessionId, { clientMessageId: 'rejected-work', turnId: 'turn-rejected', text: '后续请求', nowMs: 1_000 });
    useAgentLiveStore.getState().applyEvents(sessionId, [
      { ...agentEventFixture(1, 'turn_failed', { error: '运行中的请求仍在重试' }), sessionId, turnId: 'turn-rejected', createdAtMs: 2_000 },
      { ...agentEventFixture(2, 'status_changed', { status: 'retrying', phase: 'provider_retry', activityState: 'running', summary: '模型连接暂时不可用，正在自动重试。' }), sessionId, turnId, createdAtMs: 2_100 },
    ]);
    expect(useAgentLiveStore.getState().projections[sessionId]?.status).toBe('retrying');
    const view = render(<AgentTurn sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />);
    expect(view.container.querySelector('.agent-assistant-pending')).toHaveTextContent('正在重试连接');
    expect(view.container.querySelector('.agent-assistant-pending')).toHaveTextContent('正在自动重试');
    act(() => useAgentLiveStore.getState().applyEvents(sessionId, [{
      ...agentEventFixture(3, 'turn_failed', { error: '模型连接未恢复' }), sessionId, turnId, createdAtMs: 3_000,
    }]));
    expect(view.container.querySelector('.agent-assistant-pending')).not.toBeInTheDocument();
  });

  it.each([
    ['reasoning_summary', { requestId: 'live-reasoning', source: 'provider_reasoning_summary', state: 'running', items: [], summary: '正在分析问题与下一步。' }, '正在分析'],
    ['compaction_started', { reason: 'automatic' }, '正在整理上下文'],
    ['tool_started', { toolCallId: 'live-memory', toolName: 'memory' }, '正在执行'],
  ] as const)('keeps the reported %s phase separate from the whole-turn duration', (eventType, payload, phase) => {
    const now = 1_800_000_000_000;
    vi.useFakeTimers();
    vi.setSystemTime(now);
    const sessionId = 'session-status-polish';
    const turnId = 'turn-status-polish';
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [{ ...userMessage(sessionId, turnId), createdAtMs: now - 696_000, completedAtMs: now - 695_999 }],
      liveEvents: [{ ...agentEventFixture(1, eventType, payload), sessionId, turnId, createdAtMs: now - 2_000 }],
      lastSequence: 1, resumeToken: `${sessionId}:1`, status: 'busy', partial: true,
    });
    const view = render(<AgentTurn sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />);
    expect(view.container.querySelector('.agent-assistant-pending')).toHaveTextContent(phase);
    expect(view.container.querySelector('.agent-assistant-pending')).toHaveTextContent('本轮用时 11分 36秒');
  });

  it('removes the thinking status as soon as the turn receives a terminal event', () => {
    const sessionId = 'session-status-polish';
    const turnId = 'turn-status-polish';
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId)],
      liveEvents: [],
      lastSequence: 0,
      resumeToken: '',
      status: 'busy',
      partial: true,
    });

    const { container } = render(
      <AgentTurn sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />,
    );
    expect(screen.getByRole('status')).toHaveClass('agent-assistant-pending');
    expect(container.querySelector('[data-turn-status="running"]')).toBeInTheDocument();

    act(() => useAgentLiveStore.getState().applyEvents(sessionId, [
      {
        ...agentEventFixture(1, 'turn_completed', { status: 'completed' }),
        eventId: `${sessionId}:1`,
        sessionId,
        turnId,
      },
    ]));

    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(container.querySelector('.agent-assistant-pending')).not.toBeInTheDocument();
  });

  it('keeps the live planet row after the newest visible work instead of pinning it above the steps', () => {
    const sessionId = 'session-status-polish';
    const turnId = 'turn-status-polish';
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId)],
      liveEvents: [{
        ...agentEventFixture(1, 'tool_finished', {
          toolCallId: 'call-latest',
          toolName: 'shell',
          result: { ok: true },
        }),
        eventId: `${sessionId}:1`,
        sessionId,
        turnId,
      }],
      lastSequence: 1,
      resumeToken: `${sessionId}:1`,
      status: 'busy',
      partial: true,
    });

    const { container } = render(
      <AgentTurn presentation="fx" sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />,
    );
    const work = container.querySelector('.agent-turn-work')!;
    const pending = container.querySelector('.agent-assistant-pending')!;
    expect(work).toBeInTheDocument();
    expect(pending).toBeInTheDocument();
    expect(work.compareDocumentPosition(pending) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('keeps ordinary and PAWOS thinking states content-sized without a card surface', () => {
    expect(agentCss).toMatch(/\.agent-assistant-pending \{[^}]*display: inline-flex;/);
    expect(agentCss).toMatch(/\.agent-assistant-pending \{[^}]*width: fit-content;/);
    expect(agentCss).toMatch(/\.agent-assistant-pending \{[^}]*min-height: 0;/);
    expect(agentCss).toMatch(/\.agent-assistant-pending \{[^}]*border: 0;/);
    expect(agentFxCss).toMatch(/\.agent-assistant-pending \{[^}]*width: fit-content;/);
    expect(agentFxCss).toMatch(/\.agent-assistant-pending \{[^}]*box-shadow: none;/);
  });

  it('animates autocompact with composited properties and a reduced-motion state', () => {
    expect(agentCss).toMatch(/\.agent-compaction-notice \{[^}]*animation: agent-autocompact-enter var\(--duration-enter\) var\(--ease-standard\) both;/);
    expect(agentCss).toContain('@keyframes agent-autocompact-fold-top');
    expect(agentCss).toContain('transform: translateY(5px) scaleX(.72);');
    expect(agentCss).toContain("@media (prefers-reduced-motion: reduce)");
    expect(agentCss).toMatch(/\.agent-compaction-notice\[data-state='running'\] \.agent-compaction-notice__fold > i \{ animation: none; \}/);
    expect(agentCss).not.toMatch(/\.agent-compaction-notice[^}]*transition:\s*all/);
  });

  it('keeps both streaming sweeps on compositor transforms instead of repainting backgrounds', () => {
    expect(agentCss).toMatch(/\.agent-markdown__active-tail::after\s*\{[^}]*width:\s*calc\(200% \+ 14px\);[^}]*will-change:\s*transform;/s);
    expect(agentCss).toMatch(/@keyframes agent-tail-shimmer\s*\{[^}]*transform:\s*translate3d\(0,[^}]*\}[^}]*transform:\s*translate3d\(-50%,/s);
    expect(agentCss).toMatch(/\.agent-code-block\[data-streaming\] > figcaption::after\s*\{[^}]*width:\s*200%;[^}]*will-change:\s*transform;/s);
    expect(agentCss).toMatch(/@keyframes agent-code-stream-sweep\s*\{[^}]*transform:\s*translate3d\(0,[^}]*\}[^}]*transform:\s*translate3d\(-50%,/s);
    expect(agentCss).not.toMatch(/@keyframes (?:agent-tail-shimmer|agent-code-stream-sweep)[^@]*background-position/s);
  });

  it('keeps terminal failures as a compact recoverable inline notice', () => {
    const sessionId = 'session-status-polish';
    const turnId = 'turn-status-polish';
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId)],
      liveEvents: [{
        ...agentEventFixture(1, 'turn_failed', { error: '503 upstream request failed' }),
        eventId: `${sessionId}:1`,
        sessionId,
        turnId,
      }],
      lastSequence: 1,
      resumeToken: `${sessionId}:1`,
      status: 'faulted',
    });

    render(
      <AgentTurn
        sessionId={sessionId}
        turnId={turnId}
        onApprovalDecision={() => {}}
        onRetryTurn={() => true}
        onSwitchModel={() => {}}
      />,
    );

    const failure = screen.getByRole('alert');
    expect(failure).toHaveClass('agent-turn__failure');
    expect(failure).toHaveTextContent('本轮未完成');
    expect(screen.getByRole('button', { name: '交给 Trace Agent' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '重试本轮' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '切换模型' })).toBeInTheDocument();
    expect(agentCss).toMatch(/\.agent-turn__failure \{[^}]*width: fit-content;/);
    expect(agentCss).toMatch(/\.agent-turn__failure \{[^}]*min-height: 0;/);
    expect(agentCss).toMatch(/\.agent-turn__failure \{[^}]*border: 0;/);
    expect(agentFxCss).toMatch(/\.agent-turn__failure \{[^}]*background: transparent;/);
  });
});

function userMessage(sessionId: string, turnId: string): UiAgentMessage {
  return {
    schemaVersion: 'rag-ime.agent-message.v1',
    id: `${turnId}:user`,
    sessionId,
    turnId,
    role: 'user',
    status: 'completed',
    blocks: [{
      id: `${turnId}:user:text`,
      type: 'text',
      status: 'completed',
      presentationKind: 'markdown',
      data: { text: '检查对话状态' },
    }],
    attachments: [],
    citations: [],
    createdAtMs: 0,
    completedAtMs: 1,
  };
}
