import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { PawWorkbenchPlanningTools } from './PawWorkbenchPlanningTools';

afterEach(cleanup);

describe('Workbench selected task handoff', () => {
  it('uses the selected task title, description and exact id in its draft', async () => {
    const onOpenAgent = vi.fn();
    const first = { id: 'first-task', title: '首项任务', status: 'active' };
    const selected = { id: 'chosen-task', title: '选中的工作', detail: '先核对实际来源。', status: 'todo' };
    render(<PawWorkbenchPlanningTools date="2026-09-05" onDateChange={vi.fn()} onOpenAgent={onOpenAgent} planning={{ tasks: [first, selected] }} projectName="PAW" projectPath="/work/paw" selectedTask={selected} />);
    await userEvent.click(screen.getByRole('button', { name: '更多规划操作' }));
    await userEvent.click(screen.getByRole('button', { name: '拆解当前任务' }));
    expect(onOpenAgent).toHaveBeenCalledWith(expect.stringContaining('选中的工作（chosen-task）'));
    expect(onOpenAgent).toHaveBeenCalledWith(expect.stringContaining('先核对实际来源。'));
    expect(onOpenAgent.mock.calls[0]?.[0]).not.toContain('首项任务');
    expect(screen.queryByRole('dialog', { name: '规划操作' })).not.toBeInTheDocument();
  });

  it('does not silently substitute the first task for an explicit empty selection', async () => {
    render(<PawWorkbenchPlanningTools date="2026-09-05" onDateChange={vi.fn()} onOpenAgent={vi.fn()} planning={{ tasks: [{ id: 'first-task', title: '首项任务' }] }} projectName="PAW" projectPath="/work/paw" selectedTask={null} />);
    await userEvent.click(screen.getByRole('button', { name: '更多规划操作' }));
    expect(screen.getByRole('button', { name: '拆解当前任务' })).toBeDisabled();
  });

  it('keeps date navigation available and returns keyboard focus without dispatching work', async () => {
    const user = userEvent.setup();
    const onDateChange = vi.fn(), onOpenAgent = vi.fn();
    render(<PawWorkbenchPlanningTools date="2026-10-08" onDateChange={onDateChange} onOpenAgent={onOpenAgent} planning={{}} projectName="PAW" projectPath="/work/paw" />);
    expect(screen.getByLabelText('规划日期')).toHaveValue('2026-10-08');
    const trigger = screen.getByRole('button', { name: '更多规划操作' });
    await user.tab(); await user.tab(); await user.tab();
    expect(trigger).toHaveFocus();
    await user.keyboard('{Enter}');
    await user.click(screen.getByRole('button', { name: '前一天' }));
    expect(onDateChange).toHaveBeenCalledWith('2026-10-07');
    await user.click(screen.getByRole('button', { name: '后一天' }));
    expect(onDateChange).toHaveBeenCalledWith('2026-10-09');
    await user.keyboard('{Escape}');
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(screen.queryByRole('dialog', { name: '规划操作' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('规划日期')).toHaveValue('2026-10-08');
    expect(onOpenAgent).not.toHaveBeenCalled();
  });
});
