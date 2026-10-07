import { act, cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { SessionSummary } from '@/features/agent/types';
import { ControlledSessions, primaryControlledSessions, type ControlledSessionView } from './ControlledSessions';

afterEach(cleanup);
const owner: SessionSummary = { id: 'discussion', title: '助手', mode: 'assistant', status: 'idle', updatedAtMs: 1,
  roleId: 'sol', roleVersion: '1', roleBookRevisionId: 'one', workspaceRoots: [], metadata: { primaryAssistant: true, assistantId: 'assistant-one' } };
function view(id: string): ControlledSessionView {
  return { session: { ...owner, id, title: `任务 ${id}`, metadata: { primaryTask: true, assistantId: 'assistant-one', sourceSessionId: owner.id } },
    task: `核对 ${id} 的实际文件`, statusLabel: '正在执行', activity: 'running', freshness: 'current', stopTarget: { turnId: `turn-${id}` } };
}
describe('assistant controlled Sessions', () => {
  it('accepts only explicit primary ownership, never same-project history or delegation-like fields', () => {
    const owned = view('owned');
    expect(primaryControlledSessions(owner, [owned,
      { ...view('other-source'), session: { ...view('other-source').session, metadata: { ...owned.session.metadata, sourceSessionId: 'another-discussion' } } },
      { ...view('other-assistant'), session: { ...view('other-assistant').session, metadata: { ...owned.session.metadata, assistantId: 'another-assistant' } } },
      { ...view('unrelated'), session: { ...view('unrelated').session, metadata: { parentSessionId: owner.id } } },
      { ...view('archived'), session: { ...view('archived').session, status: 'archived' } },
    ])).toEqual([owned]);
    expect(primaryControlledSessions({ ...owner, metadata: { primaryAssistant: true } }, [owned])).toEqual([]);
  });
  it('opens the actual Session and submits Stop once with its exact turn, without claiming completion', async () => {
    const user = userEvent.setup();
    let finish!: () => void;
    const onStop = vi.fn(() => new Promise<void>(resolve => { finish = resolve; }));
    const onOpen = vi.fn();
    const task = view('one');
    render(<ControlledSessions owner={owner} views={[task]} name="我的助手" onOpen={onOpen} onStop={onStop} />);
    await user.click(screen.getByRole('button', { name: /^打开 任务 one ·/ }));
    expect(onOpen).toHaveBeenCalledWith(task.session);
    const stop = screen.getByRole('button', { name: '停止 任务 one' });
    await user.dblClick(stop);
    expect(onStop).toHaveBeenCalledTimes(1);
    expect(onStop).toHaveBeenCalledWith(task.session, { turnId: 'turn-one' });
    expect(stop).toHaveAttribute('aria-busy', 'true');
    expect(stop).toHaveTextContent('停止');
    await act(async () => finish());
    expect(screen.getByText('正在执行')).toBeVisible();
    expect(screen.queryByText('已停止')).not.toBeInTheDocument();
  });
  it('requires a current owner-projected continuation capability and preserves failure feedback', async () => {
    const user = userEvent.setup();
    const task = { ...view('paused'), activity: 'paused' as const, statusLabel: '已暂停', stopTarget: undefined, canContinue: true };
    const onContinue = vi.fn().mockRejectedValue(new Error('操作尚未确认'));
    const props = { owner, name: '我的助手', onOpen: vi.fn(), onStop: vi.fn(), onContinue };
    const rendered = render(<ControlledSessions {...props} views={[{ ...task, freshness: 'recovering' }]} />);
    expect(screen.queryByRole('button', { name: '继续 任务 paused' })).not.toBeInTheDocument();
    expect(screen.getByText('正在重新同步 · 上次状态：已暂停')).toBeVisible();
    rendered.rerender(<ControlledSessions {...props} views={[task]} />);
    await user.click(screen.getByRole('button', { name: '继续 任务 paused' }));
    expect(onContinue).toHaveBeenCalledWith(task.session);
    expect(screen.getByRole('alert')).toHaveTextContent('操作尚未确认');
    expect(screen.getByText('已暂停')).toBeVisible();
  });
  it('progressively expands owned rows and opens output with its original producing Session', async () => {
    const user = userEvent.setup();
    const tasks = Array.from({ length: 6 }, (_, index) => ({ ...view(String(index)), outputs: [{ id: `file-${index}`, title: `result-${index}.md`, reference: `/work/result-${index}.md` }] }));
    const onOpenOutput = vi.fn();
    render(<ControlledSessions owner={owner} views={tasks} name="我的助手" onOpen={vi.fn()} onOpenOutput={onOpenOutput} />);
    const region = screen.getByRole('region', { name: '助手控制的 Sessions' });
    expect(within(region).queryByRole('button', { name: /^打开 任务 4 ·/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /显示更多 还有 2 个/ }));
    expect(screen.getByRole('button', { name: /^打开 任务 5 ·/ })).toBeVisible();
    expect(screen.queryByText('result-3.md')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '显示更多产物' }));
    await user.click(screen.getByRole('button', { name: /result-3.md/ }));
    expect(onOpenOutput).toHaveBeenCalledWith(tasks[3].session, tasks[3].outputs[0]);
    await user.click(screen.getByRole('button', { name: '收起对话' }));
    expect(screen.queryByRole('button', { name: /^打开 任务 5 ·/ })).not.toBeInTheDocument();
  });
});
