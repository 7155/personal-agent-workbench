import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import type { UiAgentBlock } from '@/contracts/ui-events';
import { AgentBlock } from './BlockRenderer';

afterEach(cleanup);

function toolBlock(status: string): UiAgentBlock {
  return { id: 'structured-tool', type: 'tool_result', status: 'completed', presentationKind: 'tool_result.v1',
    data: { toolName: 'read', status, query: '检查项目入口' } };
}

describe('structured tool receipt presentation', () => {
  it.each([
    ['pending', 'pending', 'warning', '等待开始'],
    ['running', 'running', 'info', '进行中'],
    ['completed', 'success', 'success', '已完成'],
    ['failed', 'error', 'danger', '失败'],
    ['blocked', 'error', 'danger', '失败'],
    ['cancelled', 'cancelled', 'warning', '已停止'],
    ['aborted', 'cancelled', 'warning', '已停止'],
    ['unrecognized_provider_status', 'pending', 'warning', '状态待确认'],
  ])('projects %s without inventing success or opening tool details', (status, mark, tone, label) => {
    const { container } = render(<AgentBlock block={toolBlock(status)} />);
    const details = container.querySelector('details')!;
    const summary = details.querySelector('summary')!;
    expect(details).toHaveAttribute('data-tone', tone);
    expect(summary.querySelector('.ccui-execution-mark')).toHaveAttribute('data-state', mark);
    expect(summary).toHaveTextContent(label);
    expect(summary).toHaveAttribute('aria-expanded', 'false');
    expect(summary).not.toHaveTextContent('正在处理');
    fireEvent.click(summary);
    expect(summary).toHaveAttribute('aria-expanded', 'true');
    const contentId = summary.getAttribute('aria-controls')!;
    expect(container.querySelectorAll(`[id="${contentId}"]`)).toHaveLength(1);
    expect(document.getElementById(contentId)).toHaveTextContent(label);
    expect(screen.getByText('检查项目入口')).toBeVisible();
  });

  it('keeps the reader’s disclosure choice through pending, running and terminal receipts', () => {
    const view = render(<AgentBlock block={toolBlock('pending')} />);
    const summary = view.container.querySelector('summary')!;
    fireEvent.keyDown(summary, { key: 'Enter' });
    expect(summary).toHaveAttribute('aria-expanded', 'true');
    fireEvent.keyDown(summary, { key: ' ' });
    expect(summary).toHaveAttribute('aria-expanded', 'false');
    view.rerender(<AgentBlock block={toolBlock('running')} />);
    expect(summary).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(summary);
    view.rerender(<AgentBlock block={toolBlock('completed')} />);
    expect(summary).toHaveAttribute('aria-expanded', 'true');
  });
});
