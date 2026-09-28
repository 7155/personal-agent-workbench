import { act, cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { previewRoomSnapshot } from '@/app/preview-room-data';
import { createRoomProjection, reduceRoomEvent, type RoomMessageProjection } from '@/contracts/room-reducer';
import type { RoomSummary } from '@/features/rooms/room-types';
import { jevCurrentTaskVersion, parseJevSnapshot } from '@/features/semantic-workspace/jev-execution';
import { jevPartnerProjection, jevPartnerWork, JevTaskProgress, PawJevTeamPanels } from './PawJevTeamPanels';

afterEach(() => { cleanup(); vi.useRealTimers(); });
const room = previewRoomSnapshot('team-room').room as unknown as RoomSummary;
const raw = { ok: true, mode: 'jev', graphId: 'graph', rootId: 'root', snapshotVersion: 'one', phase: 'execute', tasks: [
  { id: 'a', state: 'active', revision: 2, owner_id: room.participants[0].id, objective: '核对读取', parent_id: 'goal' },
  { id: 'b', state: 'review', revision: 1, owner_id: room.participants[1].id, objective: '保留草稿', parent_id: 'goal' },
], effects: [
  { effectId: 'old', operation: 'dispatch', executionStatus: 'running', request: { taskId: 'a', taskRevision: 1, ownerId: room.participants[0].id, purpose: 'execute' } },
  { effectId: 'verify', operation: 'dispatch', executionStatus: 'running', request: { taskId: 'b', taskRevision: 1, ownerId: room.participants[2].id, purpose: 'verify' } },
] };
describe('Jev equal partner windows', () => {
  it('separates submitted input attachments from produced file evidence', () => {
    const graph = parseJevSnapshot({ ...raw, roomId: room.id, effects: [], rootAttachmentReceipts: [
      { ownerType: 'room', roomId: room.id, mediaId: 'media_abcdefghijklmnop', fileName: 'comparison.md', mimeType: 'text/markdown', byteSize: 40182 },
    ] }, 'graph');
    render(<PawJevTeamPanels graph={graph} room={room} onOpenParticipant={vi.fn()} />);
    expect(screen.getByText('原始附件 1 项')).toBeVisible();
    expect(screen.getByText('comparison.md')).toBeVisible();
  });
  it('shows receipt-backed stages without turning running work into a percentage', () => {
    const graph = parseJevSnapshot({ ...raw, effects: [], running: ['a'] }, 'graph');
    const view = render(<JevTaskProgress graph={graph} task={graph.tasks[0]} active />);
    const progress = screen.getByRole('region', { name: '任务阶段进度' });
    expect(within(progress).queryByRole('progressbar')).not.toBeInTheDocument();
    expect(progress.querySelector('[aria-current="step"]')).toHaveTextContent('执行');
    view.rerender(<JevTaskProgress graph={graph} task={graph.tasks[1]} active />);
    expect(progress.querySelector('[aria-current="step"]')).toHaveTextContent('复核');
    expect(progress.querySelectorAll('[data-complete]')).toHaveLength(2);
    view.rerender(<JevTaskProgress graph={graph} task={{ ...graph.tasks[1], state: 'done' }} active />);
    expect(progress.querySelectorAll('[data-complete]')).toHaveLength(4);
    expect(progress.querySelector('.paw-jev-partner__spinner')).toBeNull();
  });

  it('opens the dependency task and its evidence inside the existing inspector', async () => {
    const graph = parseJevSnapshot({ ...raw, effects: [], edges: [{ prerequisite: 'a', dependent: 'b', kind: 'context' }], tasks: [
      { ...raw.tasks[0], state: 'done', result: '上游已核对实际文件内容' }, raw.tasks[1],
    ] }, 'graph');
    const view = render(<PawJevTeamPanels graph={graph} room={room} onOpenParticipant={vi.fn()} />);
    await userEvent.setup().click(screen.getByRole('button', { name: /保留草稿/ }));
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveFocus();
    dialog.scrollTop = 400;
    view.rerender(<PawJevTeamPanels graph={{ ...graph, version: 'updated' }} room={room} onOpenParticipant={vi.fn()} />);
    expect(dialog.scrollTop).toBe(400);
    await userEvent.setup().click(within(dialog).getByRole('button', { name: /参考：核对读取/ }));
    expect(dialog).toHaveTextContent('上游已核对实际文件内容');
    expect(screen.getAllByRole('dialog')).toHaveLength(1);
    expect(dialog.scrollTop).toBe(0);
    expect(dialog).toHaveFocus();
  });

  it('opens current task from historical versions using the committed lineage', async () => {
    const graph = parseJevSnapshot({ ...raw, effects: [], activeTaskIds: ['a3', 'b'],
      tasks: [...raw.tasks, { ...raw.tasks[0], id: 'a2', state: 'cancelled' }, { ...raw.tasks[0], id: 'a3', objective: '第三版读取要求' }],
      revisions: [{ revisionId: 'r1', status: 'applied', successors: { a: 'a2' } }, { revisionId: 'r2', status: 'applied', successors: { a2: 'a3' } }],
    }, 'graph');
    expect(jevCurrentTaskVersion(graph, 'a')?.id).toBe('a3');
    render(<PawJevTeamPanels graph={graph} room={room} onOpenParticipant={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(screen.getByText('旧版本任务 · 2'));
    await user.click(screen.getAllByRole('button', { name: /核对读取.*已由新版本接手/ })[0]);
    expect(screen.getByRole('dialog')).toHaveTextContent('已由新版本接手');
    await user.click(screen.getByRole('button', { name: '查看当前版本任务' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('第三版读取要求');
    expect(screen.queryByRole('button', { name: '查看当前版本任务' })).not.toBeInTheDocument();
    expect(jevCurrentTaskVersion({ ...graph, revisions: [{ revisionId: 'bad', status: 'applied', changedTaskId: 'a', affectedTaskIds: [], retainedAcceptedTaskIds: [], successorTaskIds: [], successors: { a: 'a2', a2: 'a' } }] }, 'a')).toBeUndefined();
  });
  it('condenses an ended round without losing the accepted count or task inspector', async () => {
    const graph = parseJevSnapshot({ ...raw, phase: 'final', final: { status: 'completed', content: '已完成' },
      tasks: raw.tasks.map(task => ({ ...task, state: 'done' })), effects: [] }, 'graph');
    render(<PawJevTeamPanels graph={graph} room={room} onOpenParticipant={vi.fn()} />);
    expect(screen.getByLabelText('任务与成果')).toHaveAttribute('data-summary', 'true');
    expect(screen.getByRole('progressbar', { name: '任务验收数' })).toHaveAttribute('value', '2');
    expect(screen.queryByRole('button', { name: '展开伙伴卡片' })).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole('button', { name: /Earth.*核对读取/ }));
    expect(screen.getByRole('dialog')).toHaveTextContent('核对读取');
  });

  it('shows a real participant tool event through the public progress projection', () => {
    const participant = room.participants[0];
    const graph = parseJevSnapshot({ ...raw, effects: [{ effectId: 'current', operation: 'dispatch', executionStatus: 'running', request: { taskId: 'a', taskRevision: 2, ownerId: participant.id, dispatchId: 'current' } }] }, 'graph');
    const projection = reduceRoomEvent(createRoomProjection(room.id), {
      schemaVersion: 'rag-ime.agent-room-event.v1', streamKind: 'room', resumeToken: `${room.id}:1`,
      roomId: room.id, eventId: `${room.id}:1`, sequence: 1, turnId: 'root',
      eventType: 'participant_activity', participantId: participant.id,
      sourceSessionId: participant.sessionId, topicId: '', createdAtMs: 100,
      payload: { sourceEventId: 'session:1', sourceEventType: 'tool_finished', data: {
        rootId: 'root', dispatchId: 'current', toolName: 'write', toolCallId: 'write-1',
        summary: '写入 terrain.js 尚未收到回执', isError: true,
        result: { details: { executionOutcome: 'unknown' } },
      } },
    }).state;
    render(<PawJevTeamPanels graph={graph} room={room} projection={projection} onOpenParticipant={vi.fn()} />);
    const card = screen.getByRole('region', { name: 'Earth 当前工作' });
    expect(card).toHaveTextContent('写入 terrain.js 尚未收到回执');
    expect(card).toHaveTextContent('回执待核实');
    expect(card).not.toHaveTextContent('等待伙伴公开过程');
    expect(card).not.toHaveTextContent('失败');
  });
  it('does not invent zero task progress before the first graph arrives', () => {
    render(<PawJevTeamPanels graph={null} room={room} onOpenParticipant={vi.fn()} />);
    expect(screen.getByText('正在同步任务进度')).toBeVisible();
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    expect(screen.queryByText(/已验收 0\/0/)).not.toBeInTheDocument();
  });
  it('shows planning activity instead of a zero-of-zero completion meter', () => {
    const graph = parseJevSnapshot({ ...raw, phase: 'plan', tasks: [
      { id: 'goal', state: 'active', revision: 0, owner_id: room.participants[0].id, objective: '梳理目标' },
    ], effects: [{ effectId: 'plan', operation: 'dispatch', executionStatus: 'running',
      request: { taskId: 'goal', taskRevision: 0, ownerId: room.participants[0].id, purpose: 'plan' } }] }, 'graph');
    const view = render(<PawJevTeamPanels graph={graph} room={room} onOpenParticipant={vi.fn()} />);
    const progress = screen.getByLabelText('任务完成进度');
    expect(progress).toHaveTextContent('正在拆分任务');
    expect(progress.querySelector('.paw-jev-partner__spinner')).not.toBeNull();
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    expect(progress).not.toHaveTextContent('0/0');
    view.rerender(<PawJevTeamPanels graph={graph} room={room} active={false} onOpenParticipant={vi.fn()} />);
    expect(progress).toHaveTextContent('方案进度待同步');
    expect(progress.querySelector('.paw-jev-partner__spinner')).toBeNull();
    view.rerender(<PawJevTeamPanels graph={{ ...graph, stopped: true }} room={room} onOpenParticipant={vi.fn()} />);
    expect(progress).toHaveTextContent('规划已停止');
    expect(progress.querySelector('.paw-jev-partner__spinner')).toBeNull();
  });
  it('binds current public text and tools to the exact dispatch or accepted Session turn', () => {
    const participant = room.participants[0];
    const graph = parseJevSnapshot({ ...raw, effects: [{ effectId: 'current', operation: 'dispatch', executionStatus: 'running', request: { taskId: 'a', taskRevision: 2, ownerId: participant.id, dispatchId: 'current', sessionId: 'session-one' }, receipt: { turnId: 'pi-current' } }] }, 'graph');
    const projection = createRoomProjection(room.id);
    projection.turnOrder = ['root', 'another-root'];
    for (const id of projection.turnOrder) projection.turnsById[id] = { id, rootId: id, status: 'running', messageIds: [], activityIds: [], participantIds: [participant.id], createdAtMs: 0, updatedAtMs: 0 };
    const message = (id: string, extra: Partial<RoomMessageProjection>): RoomMessageProjection => ({ id, roomId: room.id, turnId: 'root', participantId: participant.id, sourceSessionId: 'session-one', role: 'assistant', status: 'completed', text: id, createdAtMs: 0, projectionKind: 'post', ...extra });
    const messages = [message('old-plan', { dispatchId: 'old' }), message('current-text', { dispatchId: 'current' }), message('unbound', {}), message('exact-turn', { sourceTurnId: 'pi-current' }), message('other-session', { sourceTurnId: 'pi-current', sourceSessionId: 'other' }), message('conflicting-dispatch', { dispatchId: 'old', sourceTurnId: 'pi-current' }), message('other-root', { turnId: 'another-root', dispatchId: 'current' })];
    projection.messageOrder = messages.map(item => item.id);
    projection.messagesById = Object.fromEntries(messages.map(item => [item.id, item]));
    for (const [id, dispatchId] of [['old-tool', 'old'], ['current-tool', 'current']]) {
      projection.activityOrder.push(id);
      projection.activitiesById[id] = { id, turnId: 'root', participantId: participant.id, sourceSessionId: 'session-one', kind: 'tool', status: 'running', summary: id, payload: { dispatchId }, createdAtMs: 0 };
    }
    const scoped = jevPartnerProjection(projection, graph.rootId, jevPartnerWork(graph, room)[0]);
    expect(scoped.messageOrder).toEqual(['current-text', 'exact-turn']);
    expect(scoped.activityOrder).toEqual(['current-tool']);
    expect(projection.messageOrder).toHaveLength(7);
    expect(projection.activityOrder).toHaveLength(2);
  });
  it('shows a waiting placeholder instead of a prior purpose from the same participant', () => {
    const participant = room.participants[0];
    const graph = parseJevSnapshot({ ...raw, effects: [{ effectId: 'current', operation: 'dispatch', executionStatus: 'running', request: { taskId: 'a', taskRevision: 2, ownerId: participant.id, dispatchId: 'current' } }] }, 'graph');
    const projection = createRoomProjection(room.id);
    projection.turnOrder = ['root'];
    projection.turnsById.root = { id: 'root', status: 'running', messageIds: ['old'], activityIds: [], participantIds: [participant.id], createdAtMs: 0, updatedAtMs: 0 };
    projection.messageOrder = ['old'];
    projection.messagesById.old = { id: 'old', roomId: room.id, turnId: 'root', participantId: participant.id, sourceSessionId: participant.sessionId, role: 'assistant', status: 'completed', text: '待整份方案确认后执行', dispatchId: 'plan', createdAtMs: 0, projectionKind: 'post' };
    render(<PawJevTeamPanels graph={graph} room={room} projection={projection} onOpenParticipant={vi.fn()} />);
    expect(screen.getByText('等待伙伴公开过程')).toBeVisible();
    expect(screen.queryByText('待整份方案确认后执行')).not.toBeInTheDocument();
  });
  it('briefly smiles only for a newly observed done receipt, without extending it on later snapshots', () => {
    vi.useFakeTimers();
    const graph = parseJevSnapshot({ ...raw, effects: [] }, 'graph');
    const onOpenParticipant = vi.fn();
    const view = render(<PawJevTeamPanels graph={graph} room={room} onOpenParticipant={onOpenParticipant} />);
    const completed = { ...graph, version: 'two', tasks: graph.tasks.map(task => task.id === 'a' ? { ...task, state: 'done' } : task) };
    view.rerender(<PawJevTeamPanels graph={completed} room={room} onOpenParticipant={onOpenParticipant} />);
    const avatar = view.container.querySelector('.paw-jev-records [data-room-planet="0"]');
    expect(avatar).toHaveAttribute('data-activity', 'done');
    expect(avatar).toHaveAttribute('data-expression', 'happy');
    act(() => vi.advanceTimersByTime(500));
    view.rerender(<PawJevTeamPanels graph={{ ...completed, version: 'three' }} room={room} onOpenParticipant={onOpenParticipant} />);
    act(() => vi.advanceTimersByTime(500));
    expect(avatar).toHaveAttribute('data-activity', 'static');
  });
  it('does not celebrate initially completed work, reloads or a different graph', () => {
    const graph = parseJevSnapshot({ ...raw, effects: [] }, 'graph');
    const completed = { ...graph, tasks: graph.tasks.map(task => ({ ...task, state: 'done' })) };
    const view = render(<PawJevTeamPanels graph={completed} room={room} onOpenParticipant={vi.fn()} />);
    expect(view.container.querySelector('[data-activity="done"]')).toBeNull();
    view.unmount();
    const reopened = render(<PawJevTeamPanels graph={completed} room={room} onOpenParticipant={vi.fn()} />);
    expect(reopened.container.querySelector('[data-activity="done"]')).toBeNull();
    reopened.rerender(<PawJevTeamPanels graph={graph} room={room} onOpenParticipant={vi.fn()} />);
    reopened.rerender(<PawJevTeamPanels graph={{ ...completed, graphId: 'history' }} room={room} onOpenParticipant={vi.fn()} />);
    expect(reopened.container.querySelector('[data-activity="done"]')).toBeNull();
  });
  it.each(['historical', 'inactive'] as const)('keeps %s completion snapshots static', mode => {
    const graph = parseJevSnapshot({ ...raw, effects: [] }, 'graph');
    const props = { room, onOpenParticipant: vi.fn(), active: mode !== 'inactive', observeCompletions: mode !== 'historical' };
    const view = render(<PawJevTeamPanels {...props} graph={graph} />);
    view.rerender(<PawJevTeamPanels {...props} graph={{ ...graph, tasks: graph.tasks.map(task => ({ ...task, state: 'done' })) }} />);
    expect(view.container.querySelector('[data-activity="done"]')).toBeNull();
  });
  it('uses the current effect owner and task revision, not old sessions or the submitter', () => {
    const graph = parseJevSnapshot(raw, 'graph');
    expect(jevPartnerWork(graph, room).map(work => work.participant.id)).toEqual([room.participants[2].id]);
    render(<PawJevTeamPanels graph={graph} room={room} onOpenParticipant={vi.fn()} />);
    expect(screen.getByRole('region', { name: 'Venus 当前工作' })).toHaveTextContent('结果复核');
    expect(screen.queryByRole('region', { name: 'Earth 当前工作' })).not.toBeInTheDocument();
  });
  it('moves completed work to delivery history and removes active windows after final', () => {
    const graph = parseJevSnapshot({ ...raw, final: { status: 'completed', content: '完成' }, tasks: raw.tasks.map(task => ({ ...task, state: 'done', result: '已核对' })) }, 'graph');
    expect(jevPartnerWork(graph, room)).toEqual([]);
    render(<PawJevTeamPanels graph={graph} room={room} onOpenParticipant={vi.fn()} />);
    expect(screen.getByText('结果与交付')).toBeVisible();
    expect(screen.queryByRole('region', { name: /当前工作/ })).not.toBeInTheDocument();
    expect(screen.getByText('已验收 2/2')).toBeVisible();
    expect(screen.getAllByText('已验收', { exact: true })).toHaveLength(2);
  });
  it('keeps a long responsibility out of the rail and opens its complete evidence without sending work', async () => {
    const objective = `建立最小工程与接口契约。${'保留真实工作目录和验收边界，不声称尚未完成的结果。'.repeat(35)}`;
    const participant = room.participants[0];
    const graph = parseJevSnapshot({ ...raw, tasks: [{ ...raw.tasks[0], objective, acceptance: ['测试与构建均有回执'], expected_output: '可运行的工程骨架' }], effects: [{ effectId: 'current', operation: 'dispatch', executionStatus: 'running', request: { taskId: 'a', taskRevision: 2, ownerId: participant.id, purpose: 'execute', dispatchId: 'current' } }] }, 'graph');
    const onOpenParticipant = vi.fn();
    render(<PawJevTeamPanels graph={graph} room={room} onOpenParticipant={onOpenParticipant} />);
    const card = screen.getByRole('region', { name: 'Earth 当前工作' });
    expect(card).not.toHaveTextContent(objective);
    const user = userEvent.setup();
    const opener = within(card).getByRole('button', { name: /^建立最小工程/ });
    await user.click(opener);
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent(objective);
    expect(dialog).toHaveTextContent('测试与构建均有回执');
    expect(dialog).toHaveTextContent('可运行的工程骨架');
    expect(onOpenParticipant).not.toHaveBeenCalled();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
    await user.click(within(card).getByRole('button', { name: '查看 Earth 完整过程' }));
    expect(onOpenParticipant).toHaveBeenCalledWith(participant.id);
  });
  it('shows only the latest tool receipt from the exact dispatch instead of another transcript', () => {
    const participant = room.participants[0];
    const graph = parseJevSnapshot({ ...raw, effects: [{ effectId: 'current', operation: 'dispatch', executionStatus: 'unknown', request: { taskId: 'a', taskRevision: 2, ownerId: participant.id, dispatchId: 'current' } }] }, 'graph');
    const projection = createRoomProjection(room.id);
    projection.turnOrder = ['root'];
    projection.turnsById.root = { id: 'root', rootId: 'root', status: 'running', messageIds: [], activityIds: [], participantIds: [participant.id], createdAtMs: 0, updatedAtMs: 0 };
    for (const [id, dispatchId, summary] of [['old', 'previous', '上轮规划读取完成'], ['first', 'current', '读取工程入口'], ['latest', 'current', '写入文件超时，尚未确认结果']]) {
      projection.activityOrder.push(id);
      projection.activitiesById[id] = { id, turnId: 'root', participantId: participant.id, sourceSessionId: participant.sessionId, kind: 'participant_activity', status: id === 'latest' ? 'failed' : 'completed', summary, payload: { dispatchId, sourceEventType: 'tool_finished' }, createdAtMs: 0 };
    }
    const { container } = render(<PawJevTeamPanels graph={graph} room={room} projection={projection} active={false} onOpenParticipant={vi.fn()} />);
    expect(screen.getByText('写入文件超时，尚未确认结果')).toBeVisible();
    expect(screen.queryByText('读取工程入口')).not.toBeInTheDocument();
    expect(screen.queryByText('上轮规划读取完成')).not.toBeInTheDocument();
    expect(screen.getByText('失败')).toBeVisible();
    expect(screen.getByText(/上次同步 · 回执待核实/)).toBeVisible();
    expect(container.querySelector('.paw-jev-partner [data-room-planet]')).toHaveAttribute('data-activity', 'static');
    expect(container.querySelector('.ccui-conversation-surface')).toBeNull();
  });
  it('keeps the current verifier and delivery records in one rail with keyboard focus', async () => {
    const graph = parseJevSnapshot(raw, 'graph');
    const onOpenParticipant = vi.fn();
    render(<PawJevTeamPanels graph={graph} room={room} onOpenParticipant={onOpenParticipant} />);
    const left = screen.getByRole('complementary', { name: '任务与成果' });
    expect(within(left).getByRole('region', { name: 'Venus 当前工作' })).toBeVisible();
    expect(within(left).getByText('结果与交付')).toBeVisible();
    const user = userEvent.setup();
    const opener = within(left).getByRole('button', { name: '展开当前伙伴' });
    opener.focus();
    await user.keyboard('{Enter}');
    expect(within(screen.getByRole('dialog')).getByRole('region', { name: 'Venus 当前工作' })).toBeVisible();
    await user.keyboard('{Escape}');
    expect(opener).toHaveFocus();
    expect(onOpenParticipant).not.toHaveBeenCalled();
  });
  it('keeps proposed assignments unstarted and exposes their full scope and dependency on demand', async () => {
    const graph = parseJevSnapshot({ ...raw, phase: 'awaiting_approval', effects: [], tasks: [], planApproval: { status: 'awaiting_approval', proposal: { tasks: [
      { key: 'base', ownerParticipantId: room.participants[0].id, objective: '先建立工程基础', acceptanceCriteria: ['构建通过'], writeTargets: ['package.json'] },
      { key: 'scene', ownerParticipantId: room.participants[1].id, objective: '再实现三维场景', acceptanceCriteria: ['浏览器显示真实三维场景'], dependsOn: ['base'], writeTargets: ['src/scene.js'] },
    ] } } }, 'graph');
    render(<PawJevTeamPanels graph={graph} room={room} onOpenParticipant={vi.fn()} />);
    expect(screen.getAllByText('待确认 · 未开始')).toHaveLength(2);
    expect(screen.queryByText('已验收')).not.toBeInTheDocument();
    const left = screen.getByRole('complementary', { name: '任务与成果' });
    await userEvent.setup().click(within(left).getByRole('button', { name: /再实现三维场景/ }));
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent('浏览器显示真实三维场景');
    expect(dialog).toHaveTextContent('src/scene.js');
    expect(dialog).toHaveTextContent('先建立工程基础');
    await userEvent.setup().click(within(dialog).getByRole('button', { name: '先建立工程基础' }));
    expect(dialog).toHaveTextContent('package.json');
    expect(dialog).not.toHaveTextContent('src/scene.js');
  });
  it('shows exact task counts, accepted models and busy marks without treating admission or unknown as running', () => {
    const first = room.participants[0], second = room.participants[1];
    const request = (taskId: string, ownerId: string) => ({ taskId, taskRevision: 1, ownerId, purpose: 'execute', contextManifest: { executionScope: { modelSelection: { modelId: 'gpt-6-sol', thinkingLevel: 'max' } } } });
    const graph = parseJevSnapshot({ ...raw, tasks: [
      { id: 'ready', parent_id: 'goal', revision: 1, state: 'done', objective: '完成接口契约', owner_id: first.id },
      { id: 'one', parent_id: 'goal', revision: 1, state: 'active', objective: '实现导航交互，complex，由宿主调度；保留验收依据', owner_id: first.id },
      { id: 'two', parent_id: 'goal', revision: 1, state: 'active', objective: '补充检索模块，complex，由宿主调度', owner_id: second.id },
      { id: 'review', parent_id: 'goal', revision: 1, state: 'review', objective: '检查回归', owner_id: first.id },
      { id: 'failed', parent_id: 'goal', revision: 1, state: 'failed', objective: '未完成任务', owner_id: second.id },
    ], effects: [
      { effectId: 'one', operation: 'dispatch', state: 'accepted', executionStatus: 'running', request: request('one', first.id) },
      { effectId: 'two', operation: 'dispatch', state: 'accepted', executionStatus: 'running', request: request('two', second.id) },
    ] }, 'graph');
    const view = render(<PawJevTeamPanels graph={graph} room={room} onOpenParticipant={vi.fn()} />);
    expect(screen.getByText('已验收 1/5')).toBeVisible();
    expect(screen.getByText('执行中 2')).toBeVisible();
    expect(screen.getByText('待复核 1')).toBeVisible();
    expect(view.container.querySelectorAll('.paw-jev-partner__state .jev-activity-icon[data-state="running"][data-animated]')).toHaveLength(2);
    expect(screen.getAllByText('gpt-6-sol · max')).toHaveLength(2);
    expect(screen.getByRole('button', { name: '实现导航交互' })).toBeVisible();
    expect(screen.queryByText(/由宿主调度/)).not.toBeInTheDocument();
    view.rerender(<PawJevTeamPanels graph={graph} room={room} active={false} onOpenParticipant={vi.fn()} />);
    expect(view.container.querySelector('.paw-jev-partner__state .jev-activity-icon[data-animated]')).not.toBeInTheDocument();
    view.rerender(<PawJevTeamPanels graph={{ ...graph, stopped: true }} room={room} onOpenParticipant={vi.fn()} />);
    expect(view.container.querySelector('.paw-jev-partner__state .jev-activity-icon[data-animated]')).not.toBeInTheDocument();
    view.rerender(<PawJevTeamPanels graph={{ ...graph, effects: graph.effects.map((effect, index) => ({ ...effect, executionStatus: index ? 'unknown' : 'admitted', state: 'pending', receipt: {} })) }} room={room} onOpenParticipant={vi.fn()} />);
    expect(screen.getByText('执行中 0')).toBeVisible();
    expect(view.container.querySelector('.paw-jev-partner__state .jev-activity-icon[data-animated]')).not.toBeInTheDocument();
    expect(screen.queryByText('gpt-6-sol · max')).not.toBeInTheDocument();
  });
  it('lists only file-shaped artifact evidence and opens its original receipt instead of inventing planned files', async () => {
    const graph = parseJevSnapshot({ ...raw, effects: [], tasks: [{ ...raw.tasks[0], state: 'done', artifacts: ['/workspace/docs/contract.md'], evidence: ['/workspace/docs/contract.md', 'docs/verification.txt:12', 'pi-settlement:receipt-1'], result: '接口已验收' }], planApproval: { status: 'approved', proposal: { tasks: [{ key: 'future', objective: '下一项任务', writeTargets: ['/workspace/not-written.md'] }] } } }, 'graph');
    render(<PawJevTeamPanels graph={graph} room={room} onOpenParticipant={vi.fn()} />);
    expect(screen.getByText('交付文件 2 项')).toBeVisible();
    expect(screen.getByText('contract.md')).toBeVisible();
    expect(screen.getByText('verification.txt')).toBeVisible();
    expect(screen.queryByText('not-written.md')).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole('button', { name: /contract.md/ }));
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent('/workspace/docs/contract.md');
    expect(dialog).toHaveTextContent('pi-settlement:receipt-1');
    expect(dialog).toHaveTextContent('接口已验收');
    expect(within(dialog).getByRole('button', { name: /打开.*Session/ })).toBeVisible();
  });
  it('recognizes installed artifact paths with hash annotations while preserving the full evidence', async () => {
    const hash = 'a'.repeat(64);
    const main = 'src/main.js sha256:' + hash;
    const readme = 'README.md sha256:' + hash;
    const graph = parseJevSnapshot({ ...raw, effects: [], tasks: [{ ...raw.tasks[0], state: 'done',
      artifacts: [main, 'src/view/input.js unchanged sha256:' + hash, readme,
        '/workspace/docs/persistence.md sha256:' + hash, 'unconfirmed.md sha256:invalid',
        'dist/index.html sha256:' + hash + '; dist/assets/bundle.css'],
      evidence: [main, 'pi-settlement:receipt-1'], result: '真实产物已验收',
    }] }, 'graph');
    render(<PawJevTeamPanels graph={graph} room={room} onOpenParticipant={vi.fn()} />);
    expect(screen.getByText('交付文件 4 项')).toBeVisible();
    for (const name of ['main.js', 'input.js', 'README.md', 'persistence.md']) {
      expect(screen.getByText(name)).toBeVisible();
    }
    expect(screen.queryByText('unconfirmed.md')).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole('button', { name: /README\.md/ }));
    expect(screen.getByRole('dialog')).toHaveTextContent(readme);
    expect(screen.getByRole('dialog')).toHaveTextContent('src/view/input.js unchanged sha256:' + hash);
  });
  it('opens current file contents in the producing Session and keeps same-name task evidence separate', async () => {
    const user = userEvent.setup();
    const onOpenFile = vi.fn();
    const ref = 'docs/verification.md#sha256:' + 'a'.repeat(64);
    const graph = parseJevSnapshot({ ...raw, tasks: raw.tasks.map((task, index) => ({ ...task,
      state: 'done', accepted_turn_id: `worker-${index}`, artifacts: [ref], evidence: [ref],
    })), effects: raw.tasks.map((task, index) => ({ effectId: `worker-${index}`, operation: 'dispatch', state: 'accepted', executionStatus: 'completed',
      request: { taskId: task.id, taskRevision: task.revision, purpose: 'execute', sessionId: `producing-session-${index}`, ownerId: task.owner_id },
    })) }, 'graph');
    render(<PawJevTeamPanels graph={graph} room={room} onOpenParticipant={vi.fn()} onOpenFile={onOpenFile} />);
    expect(screen.getByText('交付文件 2 项')).toBeVisible();
    const files = screen.getAllByRole('button', { name: '打开文件 verification.md' });
    await user.click(files[0]);
    await user.click(files[1]);
    expect(onOpenFile.mock.calls).toEqual([
      ['producing-session-0', 'docs/verification.md'], ['producing-session-1', 'docs/verification.md'],
    ]);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await user.click(screen.getAllByRole('button', { name: 'verification.md 的任务与证据' })[1]);
    expect(screen.getByRole('dialog')).toHaveTextContent(raw.tasks[1].objective);
    expect(screen.getByRole('dialog')).toHaveTextContent(ref);
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: '打开当前文件 verification.md' }));
    expect(onOpenFile).toHaveBeenLastCalledWith('producing-session-1', 'docs/verification.md');
  });
  it('removes known evidence annotations for reading and never borrows a Session for an unbound delivery', async () => {
    const onOpenFile = vi.fn();
    const graph = parseJevSnapshot({ ...raw, effects: [], tasks: [
      { ...raw.tasks[0], state: 'done', artifacts: ['src/main.js unchanged sha256:' + 'a'.repeat(64), '/workspace/docs/report.txt:12:3', 'https://example.test/remote.md', 'media://record/asset.png'] },
      { ...raw.tasks[1], state: 'done', accepted_turn_id: 'missing-worker', artifacts: ['unbound.md'] },
    ] }, 'graph');
    render(<PawJevTeamPanels graph={graph} room={room} onOpenParticipant={vi.fn()} onOpenFile={onOpenFile} />);
    expect(screen.getByText('交付文件 3 项')).toBeVisible();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '打开文件 main.js' }));
    await user.click(screen.getByRole('button', { name: '打开文件 report.txt' }));
    expect(onOpenFile.mock.calls).toEqual([[room.participants[0].sessionId, 'src/main.js'], [room.participants[0].sessionId, '/workspace/docs/report.txt']]);
    expect(screen.queryByRole('button', { name: '打开文件 unbound.md' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /unbound.md/ }));
    expect(screen.getByRole('dialog')).toHaveTextContent('unbound.md');
    expect(onOpenFile).toHaveBeenCalledTimes(2);
  });
  it('defaults to compact partners, toggles full cards from the keyboard and labels progress as accepted task counts', async () => {
    const graph = parseJevSnapshot({ ...raw, tasks: [{ ...raw.tasks[0], state: 'done' }, raw.tasks[1]] }, 'graph');
    const onOpenParticipant = vi.fn();
    render(<PawJevTeamPanels graph={graph} room={room} onOpenParticipant={onOpenParticipant} />);
    const toggle = screen.getByRole('button', { name: '展开伙伴卡片' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    const controlled = document.getElementById(toggle.getAttribute('aria-controls')!);
    expect(controlled).toContainElement(screen.getByRole('region', { name: 'Venus 当前工作' }));
    const progress = screen.getByRole('progressbar', { name: '任务验收数' });
    expect(progress).toHaveAttribute('value', '1');
    expect(progress).toHaveAttribute('max', '2');
    expect(progress).toHaveAttribute('aria-valuetext', '1 项已验收，共 2 项任务；不是预计耗时进度');
    const user = userEvent.setup();
    toggle.focus();
    await user.keyboard('{Enter}');
    expect(screen.getByRole('button', { name: '收起伙伴卡片' })).toHaveAttribute('aria-expanded', 'true');
    expect(toggle).toHaveFocus();
    await user.keyboard(' ');
    expect(screen.getByRole('button', { name: '展开伙伴卡片' })).toHaveAttribute('aria-expanded', 'false');
    expect(onOpenParticipant).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: '展开当前伙伴' }));
    const dialog = screen.getByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: '展开伙伴卡片' }));
    expect(within(dialog).getByRole('button', { name: '收起伙伴卡片' })).toHaveAttribute('aria-expanded', 'true');
    expect(within(dialog).getByRole('region', { name: 'Venus 当前工作' })).toBeVisible();
  });
});
