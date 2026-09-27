import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';
import { TooltipProvider } from '@/components/primitives';
import { MockControlTransport } from '@/test/mock-transport';
import type { ControlRequest } from '@/platform/transport';
import { useJevExecution } from './use-jev-execution';
import { JevCompanion } from './JevCompanion';

afterEach(cleanup);
const task = { id: 'task-one', state: 'active', revision: 1, owner_id: 'actor-one', objective: '核对依赖', acceptance: ['给出实际验证结果'] };
function mount(overrides: Record<string, unknown> = {}, presentation: 'sidebar' | 'stage' = 'sidebar') {
  let failure = '';
  const transport = new MockControlTransport({ routes: { 'agent.jev.get': (request: ControlRequest) => {
    if (failure) throw new Error(failure);
    return request.query?.graphId
    ? { ok: true, mode: 'jev', graphId: 'graph-one', rootId: 'root-one', snapshotVersion: 'v1', phase: 'execute', tasks: [task], effects: [], edges: [], final: {}, ...overrides }
    : { ok: true, mode: 'jev', items: [{ graph_id: 'graph-one', room_id: 'room-one', objective: '当前目标', phase: overrides.phase || 'execute' }] };
  } } });
  function Surface() { const execution = useJevExecution({ roomId: 'room-one', enabled: true, active: true, transport }); return <TooltipProvider><JevCompanion execution={execution} presentation={presentation} /></TooltipProvider>; }
  return { ...render(<Surface />), transport, fail: (reason: string) => { failure = reason; } };
}
describe('Jev conversation companion', () => {
  it('pauses motion for a latest drained abstention while keeping Stop available', async () => {
    const view = mount({ events: [{ source_id: 'latest', state: 'done', result_json: { status: 'abstained', receipt: { decision: { answer: { choice: 'insufficient_evidence' } } } } }] }, 'stage');
    await screen.findByText('暂未选出下一步');
    expect(view.container.querySelector('.jev-companion')).toHaveAttribute('data-motion', 'paused');
    await userEvent.setup().click(screen.getByText('暂未选出下一步'));
    expect(screen.getByRole('button', { name: '停止当前任务' })).toBeEnabled();
    await userEvent.setup().click(screen.getByText('查看调度回执'));
    expect(screen.getByText('调度器选择了“现有证据不足”。')).toBeVisible();
  });
  it.each(['awaiting_input', 'awaiting_approval', 'deferred'])('keeps the plan step current while the lifecycle is %s', async phase => {
    mount({ phase, planApproval: { status: phase, planHash: 'plan-hash', proposal: { tasks: [] } } });
    const rail = await screen.findByRole('list', { name: '任务阶段' });
    expect(rail.querySelector('[aria-current="step"]')).toHaveTextContent('计划');
    expect(screen.queryByRole('button', { name: '停止当前任务' })).not.toBeInTheDocument();
    expect(screen.queryByText('核对依赖')).not.toBeInTheDocument();
  });
  it('shows stale state on the collapsed stage bar while retaining the last known graph', async () => {
    const view = mount({}, 'stage'); const user = userEvent.setup();
    await screen.findByText('推进任务与复核');
    view.fail('任务状态暂时无法读取');
    await user.click(screen.getByRole('button', { name: '同步 Jev 任务' }));
    expect(await screen.findByText('状态待更新')).toBeVisible();
    expect(screen.getByText('推进任务与复核')).toBeVisible();
    expect(view.container.querySelector('.jev-companion__disclosure')).not.toHaveAttribute('open');
    await user.click(screen.getByText('状态待更新'));
    expect(view.container.querySelector('.jev-companion__warning')).toBeVisible();
    expect(screen.getByText('核对依赖')).toBeVisible();
  });
  it('exposes task ownership, dependency and actual review evidence without a progress percentage', async () => {
    mount({ tasks: [task, { ...task, id: 'task-two', objective: '实现改动', state: 'review', owner_id: 'actor-two', result: '已完成改动，等待独立核对' }], edges: [{ prerequisite: 'task-one', dependent: 'task-two', kind: 'requires' }], review: ['task-two'], effects: [{ effectId: 'effect-one', operation: 'dispatch', state: 'accepted', executionStatus: 'admitted', request: { taskId: 'task-one', taskRevision: 1, ownerId: 'actor-one', purpose: 'execute' } }] });
    await screen.findByText('核对依赖');
    expect(screen.queryByLabelText('选择 Jev 工作')).not.toBeInTheDocument();
    expect(screen.getByText('已派发')).toBeInTheDocument();
    expect(screen.getByText('等待复核')).toBeInTheDocument();
    expect(screen.getByRole('list', { name: '实现改动的依赖' })).toHaveTextContent('核对依赖');
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getAllByText('结果与验收依据')[1]);
    expect(screen.getByText('已完成改动，等待独立核对')).toBeVisible();
    expect(screen.queryByText('正在执行')).not.toBeInTheDocument();
  });
  it('shows auxiliary execution only with runtime evidence and keeps failed finals distinct', async () => {
    const view = mount({ phase: 'plan', effects: [{ effectId: 'plan-one', operation: 'dispatch', state: 'accepted', executionStatus: 'running', request: { taskId: 'task-one', taskRevision: 1, purpose: 'plan' } }] });
    await screen.findByText('规划任务与依赖');
    expect(view.container.querySelector('.jev-task')).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByText('执行交接 · 1'));
    expect(screen.getByText('执行中')).toBeVisible();
    cleanup();
    mount({ phase: 'final', final: { status: 'failed', content: '缺少模型权限，尚未完成验证。' } });
    await screen.findByText('本次未完成');
    expect(screen.getByText('缺少模型权限，尚未完成验证。')).toBeVisible();
    expect(screen.queryByRole('button', { name: '停止当前任务' })).not.toBeInTheDocument();
  });
  it('identifies the active verifier separately from the task owner', async () => {
    mount({ tasks: [{ ...task, state: 'review' }], effects: [{ effectId: 'verify-one', operation: 'dispatch', state: 'accepted', executionStatus: 'running', request: { taskId: 'task-one', taskRevision: 1, purpose: 'verify', ownerId: 'independent-verifier' } }] });
    await screen.findByText('正在复核');
    const tasks = within(screen.getByRole('list', { name: '任务依赖与负责人' }));
    expect(tasks.getByText('independent-verifier')).toBeVisible();
    expect(tasks.getByText('任务负责人：actor-one')).toBeVisible();
  });
  it('settings are local until the next explicit submission', async () => {
    const { transport } = mount(); const user = userEvent.setup();
    await screen.findByText('核对依赖');
    await user.click(screen.getByText('下一次任务设置'));
    await user.selectOptions(screen.getByLabelText('Jev 推进方式'), 'plan');
    await user.click(screen.getByRole('button', { name: 'Jev 模型与工具设置' }));
    await user.click(within(screen.getByRole('menu')).getByRole('menuitemradio', { name: '危险操作由 Jev 自动审批' }));
    await waitFor(() => expect(transport.requests.every(call => call.request.pathId === 'agent.jev.get')).toBe(true));
  });
});
