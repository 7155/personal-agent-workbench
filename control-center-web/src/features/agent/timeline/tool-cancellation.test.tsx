import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { abortAgentTurn, createAgentProjection, reduceAgentEvents } from '@/contracts/agent-reducer';
import { agentEventFixture } from '@/test/fixtures/events';
import { FxActivityStack } from './ActivitySummary';
import { publicToolResultView } from './public-tool-result';

afterEach(cleanup);
describe('tool receipts on authoritative turn cancellation', () => {
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
