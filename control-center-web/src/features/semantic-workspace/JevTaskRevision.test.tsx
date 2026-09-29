import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { JevTaskRevision } from './JevTaskRevision';
import { jevLeafTasks, jevTaskStage, parseJevSnapshot } from './jev-execution';

afterEach(cleanup);
const raw = { ok: true, mode: 'jev', graphId: 'g', rootId: 'r', phase: 'execute', snapshotVersion: 'one', activeTaskIds: ['a', 'b', 'c'],
  tasks: [{ id: 'a', state: 'active', objective: '地形', expected_output: '世界', acceptance: ['可运行'] },
    { id: 'b', state: 'done', objective: '独立音效' }, { id: 'c', state: 'queued', objective: '集成' }],
};
const graph = parseJevSnapshot(raw, 'g'); const task = graph.tasks[0];
const controls = () => ({ load: vi.fn().mockResolvedValue({ graphId: 'g', rootId: 'r', taskId: 'a', taskHash: 'exact', expectedTopologyRevision: 2,
  expectedRequirementsRevision: 1, available: true, affectedTaskIds: ['a', 'c'], downstreamTaskIds: ['c'], retainedAcceptedTaskIds: ['b'] }),
  pending: vi.fn().mockReturnValue(undefined), submit: vi.fn().mockResolvedValue({ ok: true, graphId: 'g', changedTaskId: 'a', revisionId: 'rev', status: 'awaiting_drain' }) });
describe('task revision inspector', () => {
  it('loads scope on demand and shows drain rather than claiming the replacement is running', async () => {
    const api = controls(); const user = userEvent.setup(); render(<JevTaskRevision graph={graph} task={task} controls={api} active />);
    expect(api.load).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: '修改任务要求' }));
    expect(await screen.findByText('将重做 2 项任务')).toBeVisible();
    expect(screen.getByText('保留 1 项已验收任务')).toBeVisible();
    await user.type(screen.getByLabelText('修改说明'), '扩大地形');
    await user.click(screen.getByRole('button', { name: '应用修改' }));
    expect(api.submit).toHaveBeenCalledWith(expect.objectContaining({ taskHash: 'exact', expectedTopologyRevision: 2, reason: '扩大地形', acceptanceCriteria: ['可运行'] }));
    expect(await screen.findByRole('status')).toHaveTextContent('等待受影响的旧执行停止');
    expect(screen.queryByText('新版本已生效')).not.toBeInTheDocument();
  });
  it('rejects empty acceptance and disables edits when the room is not active', async () => {
    const api = controls(); const user = userEvent.setup(); const view = render(<JevTaskRevision graph={graph} task={task} controls={api} active />);
    await user.click(screen.getByRole('button', { name: '修改任务要求' }));
    await screen.findByLabelText('验收标准 · 每行一条');
    await user.clear(screen.getByLabelText('验收标准 · 每行一条'));
    await user.click(screen.getByRole('button', { name: '应用修改' }));
    expect(screen.getByRole('alert')).toHaveTextContent('1–8'); expect(api.submit).not.toHaveBeenCalled();
    view.rerender(<JevTaskRevision graph={graph} task={task} controls={api} active={false} />);
    expect(screen.getByRole('button', { name: '应用修改' })).toBeDisabled();
  });
  it('keeps historical tasks out of active progress and marks affected accepted tasks as revising', () => {
    const revised = parseJevSnapshot({ ...raw, activeTaskIds: ['b', 'a2', 'c2'], tasks: [...raw.tasks, { id: 'a2', state: 'queued' }, { id: 'c2', state: 'queued' }] }, 'g');
    expect(jevLeafTasks(revised).map(item => item.id)).toEqual(['b', 'a2', 'c2']);
    expect(revised.historicalTasks?.map(item => item.id)).toEqual(['a', 'c']);
    expect(jevTaskStage(task, revised)).toBe('superseded');
    expect(jevTaskStage(graph.tasks[1], { ...graph, revisions: [{ revisionId: 'rev', status: 'awaiting_drain', changedTaskId: 'b', affectedTaskIds: ['b'], retainedAcceptedTaskIds: [], successorTaskIds: [] }] })).toBe('revising');
  });
  it('can reconcile the exact uncertain request even after the Root stops', async () => {
    const api = controls(); const pending = { action: 'revise_task', graphId: 'g', rootId: 'r', taskId: 'a', taskHash: 'exact',
      expectedTopologyRevision: 2, expectedRequirementsRevision: 1, objective: '保留原修改', expectedOutput: '世界', acceptanceCriteria: ['可运行'], reason: '说明' };
    api.pending.mockReturnValue(pending);
    const user = userEvent.setup(); render(<JevTaskRevision graph={{ ...graph, stopped: true }} task={task} controls={api} active />);
    expect(screen.getByLabelText('任务目标')).toBeDisabled();
    await user.click(screen.getByRole('button', { name: '核实这次修改' }));
    expect(api.submit).toHaveBeenCalledWith(pending);
    expect(api.load).not.toHaveBeenCalled();
  });
});
