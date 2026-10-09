import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';
import { ToolCard } from './ToolCard';
import { toolReceiptStatus } from './ToolStatusMark';
import type { ToolCallBlock } from '../model/types';
import { ChatPresentationProvider } from '../reading/chat-presentation';
import { MotionActivityBoundary } from '@/design/motion';

afterEach(cleanup);
const block: ToolCallBlock = { id: 'call-one', kind: 'tool', name: '读取文件', summary: 'src/app.ts', input: 'src/app.ts', status: 'running' };

describe('render-only tool receipt UI', () => {
  it('gates v2 pointer feedback without delaying keyboard evidence access or remounting content', async () => {
    const user = userEvent.setup();
    const view = (active: boolean, version: 'v1' | 'v2' = 'v2') => <ChatPresentationProvider ownerKey={`tool-motion-${version}`} defaultVersion={version}>
      <MotionActivityBoundary active={active}><ToolCard block={{ ...block, output: 'original result' }}/></MotionActivityBoundary>
    </ChatPresentationProvider>;
    const { container, rerender } = render(view(true));
    const card = () => container.querySelector('.ccui-tool-card')!;
    const trigger = screen.getByRole('button', { name: /读取文件/ });
    await user.click(trigger);
    expect(card()).toHaveAttribute('data-interaction', 'pointer');
    expect(card()).toHaveAttribute('data-motion', 'true');
    const output = screen.getByText('original result');
    const input = screen.getByRole('tab', { name: '调用参数' });
    input.focus();
    await user.keyboard('{Enter}');
    expect(card()).toHaveAttribute('data-interaction', 'keyboard');
    expect(input).toHaveAttribute('aria-selected', 'true');
    expect(input).toHaveFocus();
    expect(screen.getByText('original result')).toBe(output);
    rerender(view(false));
    expect(card()).toHaveAttribute('data-motion', 'false');
    fireEvent.pointerDown(trigger);
    expect(card()).toHaveAttribute('data-motion', 'false');
    rerender(view(true, 'v1'));
    expect(card()).not.toHaveAttribute('data-feedback');
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
  });
  it('keeps useful tool evidence while simplifying generic v2 summaries and preserving v1', () => {
    const generic = { ...block, name: 'codemode', summary: '代码执行 已完成', status: 'success' as const };
    const { rerender } = render(<ChatPresentationProvider ownerKey="receipt-new" defaultVersion="v2"><ToolCard block={generic}/></ChatPresentationProvider>);
    expect(screen.queryByText('代码执行 已完成')).not.toBeInTheDocument();
    expect(screen.getByText('已完成')).toBeInTheDocument();
    rerender(<ChatPresentationProvider ownerKey="receipt-new" defaultVersion="v2"><ToolCard block={{ ...generic, summary: '命令执行完成，退出码 0' }}/></ChatPresentationProvider>);
    expect(screen.getByText('命令执行完成，退出码 0')).toBeInTheDocument();
    rerender(<ChatPresentationProvider ownerKey="receipt-old" defaultVersion="v1"><ToolCard block={generic}/></ChatPresentationProvider>);
    expect(screen.getByText('代码执行 已完成')).toBeInTheDocument();
  });
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
  it('does not present a successful dispatch as a completed task', () => {
    const { rerender } = render(<ToolCard block={{ ...block, receiptKind: 'dispatch', status: 'success', name: '团队 → Mercury · 执行分派' }} />);
    expect(screen.getByText('已分派')).toBeInTheDocument();
    expect(screen.queryByText('已完成')).not.toBeInTheDocument();
    rerender(<ToolCard block={{ ...block, receiptKind: 'dispatch', status: 'error' }} />);
    expect(screen.getByText('分派失败')).toBeInTheDocument();
    expect(screen.queryByText('已分派')).not.toBeInTheDocument();
  });
  it('keeps unknown receipt states pending and failure distinct from cancellation', () => {
    expect(toolReceiptStatus('unrecognized')).toBe('pending');
    expect(toolReceiptStatus('failed')).toBe('error');
    expect(toolReceiptStatus('aborted')).toBe('cancelled');
    render(<ToolCard block={{ ...block, status: 'error', output: '读取失败：文件不存在' }} />);
    expect(screen.getByText('读取失败：文件不存在')).toBeInTheDocument();
    expect(screen.getByText('失败')).toBeInTheDocument();
  });
  it('renders codemode source, nested call receipts, and final output in one expandable card', async () => {
    const user = userEvent.setup();
    render(<ToolCard block={{
      ...block,
      name: 'codemode',
      input: JSON.stringify({ code: '// @options: {"timeout_ms": 30000}\nconst value = await tools.read({ path: "README.md" });\ntext(value);' }),
      output: 'Script completed\nWall time 0.4 seconds\nOutput:\n最终结果',
      status: 'success',
      codeMode: {
        calls: [{ id: 'call-one/1', name: 'read', args: '{"path":"README.md"}', status: 'ok', durationMs: 42, cost: 0.002 }, {
          id: 'call-one/2', name: 'bash', args: '{"command":"pnpm test"}', status: 'error', durationMs: 1_250, error: '命令失败',
        }],
        fullOutputPath: '/tmp/codemode-output.txt',
        nestedCallsComplete: false,
      },
    }} />);
    expect(screen.getByText('代码执行')).toBeInTheDocument();
    expect(screen.getByText(/部分恢复/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /代码执行/ }));
    expect(screen.getByText(/const value = await tools\.read/)).toBeInTheDocument();
    expect(screen.getByText('read')).toBeInTheDocument();
    expect(screen.getByText(/42ms/)).toBeInTheDocument();
    expect(screen.getByText(/\$0\.002/)).toBeInTheDocument();
    expect(screen.getByText('bash')).toBeInTheDocument();
    expect(screen.getByText('1.3s')).toBeInTheDocument();
    expect(screen.getByText('命令失败')).toBeInTheDocument();
    expect(screen.getByText('最终结果')).toBeInTheDocument();
    expect(screen.getByText('/tmp/codemode-output.txt')).toBeInTheDocument();
  });

  it.each([
    { status: 'running', calls: ['ok'], complete: true, expected: '脚本执行中' },
    { status: 'running', calls: ['ok'], complete: false, expected: '脚本执行中' },
    { status: 'running', calls: [], complete: false, expected: '脚本执行中' },
    { status: 'pending', calls: ['ok'], complete: false, expected: '脚本状态待确认' },
    { status: 'pending', calls: [], complete: false, expected: '脚本状态待确认' },
    { status: 'error', calls: ['running'], complete: false, expected: '脚本失败' },
    { status: 'cancelled', calls: ['running'], complete: false, expected: '已停止' },
    { status: 'success', calls: ['ok'], complete: false, expected: '脚本完成' },
  ] as const)('keeps the outer codemode state authoritative: $status / $calls / $complete', ({ status, calls, complete, expected }) => {
    render(<ToolCard block={{ ...block, name: 'codemode', status, codeMode: {
      calls: calls.map((childStatus, index) => ({ id: `child-${index}`, name: 'read', args: '{}', status: childStatus })),
      nestedCallsComplete: complete,
    } }} />);
    const summary = document.querySelector('.ccui-tool-main > span');
    expect(summary).toHaveTextContent(expected);
    if (status !== 'success') expect(summary).not.toHaveTextContent('脚本完成');
  });

  it('does not let completed nested calls hide an unknown outer execution outcome', () => {
    render(<ToolCard block={{ ...block, name: 'codemode', status: 'success', executionOutcome: 'unknown', codeMode: {
      calls: [{ id: 'child', name: 'read', args: '{}', status: 'ok' }], nestedCallsComplete: false,
    } }} />);
    expect(screen.getByText('回执待核实')).toBeInTheDocument();
    expect(document.querySelector('.ccui-tool-main > span')).not.toHaveTextContent('脚本完成');
  });
});
