import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { previewRoomSnapshot } from '@/app/preview-room-data';
import type { RoomSummary } from '@/features/rooms/room-types';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport } from '@/test/mock-transport';
import { JevPlanReview, jevPlanWaves } from './JevPlanReview';
import type { JevPlanTask } from './jev-execution';
import { useJevExecution } from './use-jev-execution';

afterEach(cleanup);
function mount({ questions = false, delayApprove = false, terminal = '', currentOwner, approved = false, objective = '核对文件恢复', runningRevision }: { questions?: boolean; delayApprove?: boolean; terminal?: '' | 'final' | 'stopped'; currentOwner?: 'single' | 'ambiguous'; approved?: boolean; objective?: string; runningRevision?: number } = {}) {
  let state = terminal || approved ? 'approved' : questions ? 'awaiting_input' : 'awaiting_approval';
  let release!: () => void;
  const onAdjust = vi.fn();
  const room = previewRoomSnapshot('plan-review-room').room as unknown as RoomSummary;
  const actualTasks = currentOwner ? (currentOwner === 'single' ? [1] : [1, 2]).map(index => ({ id: `work-${index}`, parent_id: 'goal', state: runningRevision === undefined ? 'done' : 'active', revision: 2, owner_id: room.participants[index].id, objective })) : [];
  const transport = new MockControlTransport({ routes: {
    'agent.jev.get': (request: ControlRequest) => request.query?.graphId ? { ok: true, mode: 'jev', graphId: 'plan-graph', rootId: 'plan-root', snapshotVersion: state, phase: terminal === 'final' ? 'final' : state === 'approved' ? 'execute' : state === 'planning' ? 'plan' : state, stopped: terminal === 'stopped', final: terminal === 'final' ? { content: '已结束', status: 'completed' } : null, tasks: [{ id: 'goal', state: 'queued', objective: '完善恢复能力' }, ...actualTasks],
      effects: runningRevision === undefined ? [] : [{ effectId: 'dispatch', operation: 'dispatch', executionStatus: 'running', request: { taskId: 'work-1', taskRevision: runningRevision } }],
      planApproval: { status: state, planHash: 'current-plan-hash', requirementsRevision: 2,
        proposal: { tasks: questions ? [] : [{ key: 'a', objective, expectedOutput: '可复验的读取结果', acceptanceCriteria: ['重复打开保留当前文件'], ownerParticipantId: currentOwner ? '' : room.participants[0].id }] },
        clarifications: questions ? [{ id: 'scope', question: '需要恢复哪些内容？', options: ['阅读位置和草稿', '仅阅读位置'] }] : [],
      } } : { ok: true, mode: 'jev', items: [{ graph_id: 'plan-graph', room_id: room.id, phase: state === 'approved' ? 'execute' : state === 'planning' ? 'plan' : state }] },
    'agent.jev.command': (request: ControlRequest) => {
      const body = request.body as Record<string, unknown>;
      if (body.action === 'approve_plan' && delayApprove) return new Promise(resolve => { release = () => { state = 'approved'; resolve({ ok: true, graphId: 'plan-graph' }); }; });
      state = body.action === 'defer_plan' ? 'deferred' : body.action === 'approve_plan' ? 'approved' : 'planning';
      return { ok: true, graphId: 'plan-graph' };
    },
  } });
  function Surface() { const execution = useJevExecution({ roomId: room.id, enabled: true, active: true, transport }); return <><output aria-label="实际运行">{String(execution.busy)}</output><JevPlanReview execution={execution} room={room} onAdjust={onAdjust} /></>; }
  return { ...render(<Surface />), transport, onAdjust, release: () => release(), commands: () => transport.requests.filter(call => call.request.pathId === 'agent.jev.command').map(call => call.request.body) };
}

describe('Jev whole-plan approval', () => {
  it('shows independent work together and dependent integration later without scheduling', () => {
    const task = (key: string, dependsOn: string[]): JevPlanTask => ({ key, dependsOn, objective: key, expectedOutput: key, acceptanceCriteria: [], contextRefs: [], writeTargets: [], ownerParticipantId: '' });
    expect(jevPlanWaves([task('integrate', ['code', 'design']), task('base', []), task('code', ['base']), task('design', ['base'])])?.map(wave => wave.map(item => item.key))).toEqual([['base'], ['code', 'design'], ['integrate']]);
    expect(jevPlanWaves([task('a', ['b']), task('b', ['a'])])).toBeNull();
    expect(jevPlanWaves([task('a', ['missing'])])).toBeNull();
  });
  it('shows the whole proposal without starting workers and defers only on an explicit action', async () => {
    const { commands, onAdjust } = mount(); const user = userEvent.setup();
    await screen.findByRole('region', { name: '整体执行方案' });
    expect(screen.getByText('交付：可复验的读取结果')).toBeVisible();
    expect(screen.getByRole('region', { name: '任务执行顺序' })).toHaveTextContent('可以先开始');
    await user.click(within(screen.getByRole('region', { name: '任务执行顺序' })).getByRole('button', { name: /核对文件恢复/ }));
    expect(screen.getByText('交付：可复验的读取结果').closest('[data-plan-key]')).toHaveFocus();
    await user.click(screen.getByText('完整任务与验收标准 · 1 项'));
    expect(screen.getByText('重复打开保留当前文件')).toBeVisible();
    expect(screen.getByLabelText('实际运行')).toHaveTextContent('false');
    expect(commands()).toEqual([]);
    await user.click(screen.getByRole('button', { name: '调整方案' }));
    expect(onAdjust).toHaveBeenCalledOnce(); expect(commands()).toEqual([]);
    await user.click(screen.getByRole('button', { name: '暂不执行' }));
    await screen.findByText('已保留，暂未执行');
    expect(commands()).toEqual([expect.objectContaining({ action: 'defer_plan', graphId: 'plan-graph', rootId: 'plan-root', planHash: 'current-plan-hash' })]);
    expect(screen.getByRole('button', { name: '开始执行' })).toBeEnabled();
    expect(screen.getByLabelText('实际运行')).toHaveTextContent('false');
  });

  it('binds approval to the displayed plan and never marks it approved before a receipt', async () => {
    const { commands, release } = mount({ delayApprove: true }); const user = userEvent.setup();
    await user.dblClick(await screen.findByRole('button', { name: '开始执行' }));
    expect(commands()).toHaveLength(1);
    expect(commands()[0]).toMatchObject({ action: 'approve_plan', planHash: 'current-plan-hash', rootId: 'plan-root' });
    expect(screen.getByRole('button', { name: '正在确认' })).toBeDisabled();
    expect(screen.queryByText(/执行方案已确认/)).not.toBeInTheDocument();
    await act(async () => release());
    await screen.findByText('执行方案已确认 · 版本 2');
    expect(screen.queryByRole('button', { name: '开始执行' })).not.toBeInTheDocument();
    const opener = screen.getByRole('button', { name: '执行方案已确认 · 版本 2' });
    await user.click(opener);
    expect(screen.getByRole('dialog', { name: '已确认的执行方案' })).toBeVisible();
    expect(screen.getByRole('region', { name: '任务执行顺序' })).toHaveTextContent('任务 1');
    expect(commands()).toHaveLength(1);
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it('answers real clarification questions through adjustment of the same root', async () => {
    const { commands } = mount({ questions: true }); const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: '阅读位置和草稿' }));
    expect(screen.queryByRole('button', { name: '开始执行' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '提交补充' }));
    await waitFor(() => expect(commands()).toHaveLength(1));
    expect(commands()[0]).toMatchObject({ action: 'adjust_plan', rootId: 'plan-root', planHash: 'current-plan-hash', message: '需要恢复哪些内容？\n阅读位置和草稿' });
  });

  it.each(['final', 'stopped'] as const)('keeps the approved plan readable after %s without execution controls', async terminal => {
    const { commands } = mount({ terminal }); const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: '执行方案已确认 · 版本 2' }));
    expect(screen.getByRole('dialog', { name: '已确认的执行方案' })).toBeVisible();
    expect(screen.getByText('交付：可复验的读取结果')).toBeVisible();
    expect(screen.queryByRole('button', { name: '开始执行' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '调整方案' })).not.toBeInTheDocument();
    expect(commands()).toEqual([]);
  });
  it.each(['single', 'ambiguous'] as const)('shows an unplanned owner only with a unique actual task binding (%s)', async currentOwner => {
    const { commands } = mount({ terminal: 'final', currentOwner });
    await userEvent.setup().click(await screen.findByRole('button', { name: '执行方案已确认 · 版本 2' }));
    if (currentOwner === 'single') expect(screen.getByText('当前 · Mars')).toBeVisible();
    else {
      expect(screen.queryByText(/当前 ·/)).not.toBeInTheDocument();
      expect(screen.getByText('执行时分配')).toBeVisible();
    }
    expect(commands()).toEqual([]);
  });
  it.each(['complex，建议宿主按 Sol max 调度', 'routine，建议 Luna max'])('uses known partner names while preserving the original task (%s)', async routing => {
    const room = previewRoomSnapshot('plan-review-room').room as unknown as RoomSummary;
    const objective = `由 ${room.participants[0].id} 核对交互（${routing}）。保留完整要求。`;
    const { commands } = mount({ approved: true, objective });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: '执行方案已确认 · 版本 2' }));
    const sequence = screen.getByRole('region', { name: '任务执行顺序' });
    expect(sequence).toHaveTextContent('Earth 核对交互');
    expect(sequence).not.toHaveTextContent(room.participants[0].id);
    expect(sequence).not.toHaveTextContent('max');
    await user.click(screen.getByText('完整任务与验收标准 · 1 项'));
    expect(screen.getByText(objective)).toBeVisible();
    expect(commands()).toEqual([]);
  });
  it.each([2, 1])('shows current status only from the matching task revision (%s)', async runningRevision => {
    const { commands } = mount({ approved: true, currentOwner: 'single', runningRevision });
    await userEvent.setup().click(await screen.findByRole('button', { name: '执行方案已确认 · 版本 2' }));
    expect(screen.getByLabelText('任务 1 当前状态')).toHaveTextContent(runningRevision === 2 ? '执行中' : '回执待核实');
    expect(commands()).toEqual([]);
  });
});
