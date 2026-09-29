import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';
import { ToolCard } from './ToolCard';
import { toolReceiptStatus } from './ToolStatusMark';
import type { ToolCallBlock } from '../model/types';

afterEach(cleanup);
const block: ToolCallBlock = { id: 'call-one', kind: 'tool', name: '读取文件', summary: 'src/app.ts', input: 'src/app.ts', status: 'running' };

describe('render-only tool receipt UI', () => {
  it('shows familiar action names while preserving the exact tool identity and disclosure', async () => {
    const user = userEvent.setup();
    const { rerender } = render(<ToolCard block={{ ...block, name: 'workspace_shell', output: 'exit 0' }} />);
    expect(screen.getByText('终端命令')).toHaveAttribute('title', 'workspace_shell');
    const trigger = screen.getByRole('button', { name: /终端命令/ });
    await user.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('exit 0')).toBeInTheDocument();
    rerender(<ToolCard block={{ ...block, name: 'custom_tool' }} />);
    expect(screen.getByText('custom_tool')).toBeInTheDocument();
  });
  it('transitions through backend states without losing the open result or hiding host actions', async () => {
    const user = userEvent.setup();
    const { rerender, container } = render(<ToolCard block={block} action={<button>查看后台任务</button>} />);
    expect(screen.getByText('正在执行')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '查看后台任务' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /读取文件/ }));
    expect(screen.getByRole('button', { name: /读取文件/ })).toHaveAttribute('aria-expanded', 'true');
    rerender(<ToolCard block={{ ...block, status: 'success', output: '真实文件内容' }} action={<button>查看后台任务</button>} />);
    expect(screen.getByText('真实文件内容')).toBeInTheDocument();
    expect(screen.getByText('已完成')).toBeInTheDocument();
    expect(container.querySelector('.ccui-execution-mark')).toHaveAttribute('data-state', 'success');
    rerender(<ToolCard block={{ ...block, status: 'cancelled' }} />);
    expect(screen.getByText('已停止')).toBeInTheDocument();
    expect(screen.queryByText('已完成')).not.toBeInTheDocument();
  });
  it('keeps unknown receipt states pending and failure distinct from cancellation', () => {
    expect(toolReceiptStatus('unrecognized')).toBe('pending');
    expect(toolReceiptStatus('failed')).toBe('error');
    expect(toolReceiptStatus('aborted')).toBe('cancelled');
    render(<ToolCard block={{ ...block, status: 'error', output: '读取失败：文件不存在' }} />);
    expect(screen.getByText('读取失败：文件不存在')).toBeInTheDocument();
    expect(screen.getByText('失败')).toBeInTheDocument();
  });
});
