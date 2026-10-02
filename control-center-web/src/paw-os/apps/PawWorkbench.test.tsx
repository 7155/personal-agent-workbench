import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { PawWorkbench, type PawWorkbenchProps, type PawWorkbenchRecord } from './PawWorkbench';

afterEach(cleanup);

describe('PawWorkbench', () => {
  it('renders the project overview from the supplied route records without seed data', async () => {
    const task = { id: 'task-real', title: '统一 Agent 入口', status: 'in_progress', owner: '前端' };
    const onOpenTask = vi.fn();
    const { container } = renderWorkbench({
      pageId: 'overview',
      overview: { project: { name: 'personal-agent-workbench', path: '/work/paw', branch: 'main' }, metrics: { activeSessions: 3 } },
      planning: { tasks: [task], summary: { openTaskCount: 1, completedTaskCount: 0 } },
      onOpenTask,
    });

    // Workspace identity is a status rail, not a hero plate above the work.
    const ledger = container.querySelector('.paw-wb-ledger') as HTMLElement;
    expect(within(ledger).getByText('personal-agent-workbench')).toBeInTheDocument();
    expect(screen.getAllByText('/work/paw')).toHaveLength(2);
    expect(within(screen.getByLabelText('项目真实指标')).getByText('3')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /统一 Agent 入口/ }));
    expect(onOpenTask).toHaveBeenCalledWith(task);
    expect(screen.queryByText('今晚发布 v0.3')).not.toBeInTheDocument();
  });

  it('puts real tasks and WorkDocuments in the first viewport instead of a stack of summary plates', () => {
    const { container } = renderWorkbench({
      pageId: 'overview',
      overview: { project: { name: 'personal-agent-workbench', path: '/work/paw' }, metrics: { activeSessions: 3 } },
      planning: {
        goals: [{ id: 'goal-1', title: '完成 Workbench', status: 'active' }],
        tasks: [{ id: 'task-1', title: '统一 Agent 入口', status: 'active' }],
      },
      documents: [workDocument()],
    });

    const workspace = container.querySelector('.paw-wb-overview__workspace') as HTMLElement;
    expect(workspace).not.toBeNull();
    // The console band answers "next unresolved" above the working panes; the
    // panes themselves come before the status ledger in document order.
    const bands = [...container.querySelectorAll('.paw-wb-overview > *')].map((node) => node.className);
    expect(bands).toEqual(['paw-wb-now', 'paw-wb-overview__workspace', 'paw-wb-ledger']);
    expect([...workspace.querySelectorAll('[data-pane]')].map((pane) => pane.getAttribute('data-pane')))
      .toEqual(['tasks', 'documents', 'goals']);
    expect(within(workspace).getByRole('heading', { level: 2, name: '当前工作' })).toBeInTheDocument();
    expect(within(workspace).getByRole('button', { name: /统一 Agent 入口/ })).toBeInTheDocument();
    expect(within(workspace).getByRole('button', { name: /PAWOS 交互重建/ })).toBeInTheDocument();
    expect(container.querySelector('.paw-wb-project-lead')).toBeNull();
  });

  it('leads the overview with the next unresolved task and truthful pulse facts', async () => {
    const active = { id: 'active-task', title: '继续迁移', status: 'active' };
    const review = { id: 'review-task', title: '验收视觉回归', status: 'review' };
    const blocked = { id: 'blocked-task', title: '修复发布门禁', status: 'blocked', owner: '前端' };
    const onOpenTask = vi.fn();
    const { rerender } = renderWorkbench({
      pageId: 'overview',
      planning: { tasks: [active, review, blocked] },
      documents: [{ ...workDocument(), updatedAtMs: 1_700_000_000_000 }],
      onOpenTask,
    });

    const band = screen.getByLabelText('当前最需要处理的工作');
    expect(within(band).getByRole('heading', { level: 2, name: '修复发布门禁' })).toBeInTheDocument();
    const pulse = within(screen.getByLabelText('当前计划状态'));
    expect(pulse.getByText('受阻').nextElementSibling).toHaveTextContent('1');
    expect(pulse.getByText('待验收').nextElementSibling).toHaveTextContent('1');
    expect(pulse.getByText('进行中').nextElementSibling).toHaveTextContent('1');
    expect(pulse.getByText('证据更新').nextElementSibling).not.toHaveTextContent('暂无文档');
    await userEvent.click(within(band).getByRole('button', { name: '打开任务窗口' }));
    expect(onOpenTask).toHaveBeenCalledWith(blocked);

    rerender(<PawWorkbench {...baseProps({
      pageId: 'overview',
      planning: { tasks: [active] },
      resourceStates: { planning: { loading: true } },
    })} />);
    expect(screen.queryByLabelText('当前最需要处理的工作')).not.toBeInTheDocument();
  });

  it('keeps completion facts readable beside the plan states and omits them without tasks', () => {
    const { rerender } = renderWorkbench({
      pageId: 'overview',
      planning: { tasks: [
        { id: 'done-1', title: '完成迁移 A', status: 'done' },
        { id: 'done-2', title: '完成迁移 B', status: 'completed' },
        { id: 'active-1', title: '推进重构', status: 'active' },
        { id: 'blocked-1', title: '修复门禁', status: 'blocked' },
      ] },
    });

    const status = screen.getByLabelText('当前计划状态');
    expect(within(status).getByText('已完成').nextElementSibling).toHaveTextContent('2 / 4 项 · 50%');

    rerender(<PawWorkbench {...baseProps({ pageId: 'overview', planning: { tasks: [] } })} />);
    expect(screen.getByLabelText('当前最需要处理的工作')).toHaveAttribute('data-state', 'empty');
    expect(within(screen.getByLabelText('当前计划状态')).queryByText('已完成')).not.toBeInTheDocument();
  });

  it('keeps repository facts secondary behind an explicit disclosure', () => {
    renderWorkbench({
      pageId: 'overview',
      overview: { project: { name: 'personal-agent-workbench', path: '/work/paw', branch: 'main', revision: 'abc1234' } },
    });

    const disclosure = screen.getByText('仓库与运行事实').closest('details');
    expect(disclosure).not.toBeNull();
    expect(disclosure).not.toHaveAttribute('open');
    expect(within(disclosure as HTMLElement).getByText('main')).toBeInTheDocument();
    expect(within(disclosure as HTMLElement).getByText('abc1234')).toBeInTheDocument();
  });

  it('leads the deck with page purpose and cross-page commands carrying truthful counts', async () => {
    const onNavigate = vi.fn();
    renderWorkbench({
      pageId: 'overview',
      planning: { tasks: [
        { id: 'active-task', title: '推进重构', status: 'active' },
        { id: 'done-task', title: '完成迁移', status: 'done' },
      ] },
      documents: [workDocument()],
      documentTotal: 5,
      onNavigate,
    });

    // Purpose leads; the current page never offers a self-command.
    expect(screen.getByText('先处理最需要处理的事')).toBeInTheDocument();
    const deck = screen.getByRole('group', { name: '项目命令台' });
    expect(within(deck).queryByRole('button', { name: /前往项目概览/ })).not.toBeInTheDocument();
    // Cross-page navigation lives in the deck, not in the now-band.
    expect(within(screen.getByLabelText('当前最需要处理的工作')).queryByRole('button', { name: '任务编排' })).not.toBeInTheDocument();

    await userEvent.click(within(deck).getByRole('button', { name: '前往任务编排：1 项未完成' }));
    expect(onNavigate).toHaveBeenCalledWith('planning');
    await userEvent.click(within(deck).getByRole('button', { name: '前往工作文档：共 5 份' }));
    expect(onNavigate).toHaveBeenCalledWith('documents');
  });

  it('withholds deck counts while resources are unsettled and drops commands without a navigator', () => {
    const { rerender } = renderWorkbench({
      pageId: 'documents',
      onNavigate: vi.fn(),
      resourceStates: { planning: { loading: true }, documents: { loading: true } },
    });

    const deck = screen.getByRole('group', { name: '项目命令台' });
    expect(within(deck).getByRole('button', { name: '前往任务编排' })).toBeInTheDocument();
    expect(within(deck).getByRole('button', { name: '前往项目概览' })).toBeInTheDocument();
    expect(within(deck).queryByRole('button', { name: /前往工作文档/ })).not.toBeInTheDocument();

    rerender(<PawWorkbench {...baseProps({ pageId: 'documents' })} />);
    expect(screen.queryByRole('group', { name: '项目命令台' })).not.toBeInTheDocument();
  });

  it('derives project identity from real planning or WorkDocument fields when overview lacks it', () => {
    renderWorkbench({
      overview: { ok: true },
      planning: { plan: {} },
      documents: [{ documentId: 'doc-1', workspaceRoot: '/Users/example/Projects/personal-agent-workbench' }],
    });

    expect(screen.getAllByText('personal-agent-workbench').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('/Users/example/Projects/personal-agent-workbench').length).toBeGreaterThanOrEqual(1);
  });

  it('leaves App identity in the host while keeping the current project and page inspectable', () => {
    const projectPath = '/Users/example/Projects/a-very-long-personal-agent-workbench-path';
    const { container } = renderWorkbench({
      overview: { project: { name: 'personal-agent-workbench', path: projectPath } },
    });

    expect(container.querySelector('.paw-wb-chrome [data-paw-app-icon]')).toBeNull();
    expect(screen.queryByText('Project Workbench')).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 1, name: '项目概览' })).toBeInTheDocument();
    expect(within(container.querySelector('.paw-wb-chrome') as HTMLElement).getByText(projectPath)).toHaveAttribute('title', projectPath);
  });

  it('draws only declared task dependencies and emits one bounded packet for an active edge', () => {
    const source = { id: 'build', title: '构建校验', status: 'done', owner: '前端', progress: 1 };
    const target = { id: 'visual', title: '视觉回归', status: 'active', owner: '设计', dependencies: ['build'], progress: .53 };
    const independent = { id: 'docs', title: '文档核对', status: 'review' };
    const onOpenTask = vi.fn();
    const { container } = renderWorkbench({ pageId: 'planning', planning: { tasks: [source, target, independent] }, onOpenTask });

    expect(container.querySelectorAll('.paw-wb-graph__edge')).toHaveLength(1);
    expect(container.querySelectorAll('[data-flow-packet]')).toHaveLength(1);
    expect(screen.getByText('3 节点 · 1 条真实依赖')).toBeInTheDocument();

    const graphNode = [...container.querySelectorAll<HTMLButtonElement>('.paw-wb-task-node')]
      .find((node) => node.textContent?.includes('视觉回归'));
    expect(graphNode).toBeDefined();
    fireEvent.click(graphNode as HTMLButtonElement);
    expect(onOpenTask).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '打开任务窗口' }));
    expect(onOpenTask).toHaveBeenLastCalledWith(target);
    expect(screen.getAllByText('53%').length).toBeGreaterThanOrEqual(1);
  });

  it('labels only populated dependency lanes and keeps the graph free of empty columns', () => {
    const { container } = renderWorkbench({
      pageId: 'planning',
      planning: { tasks: [
        { id: 'lane-a', title: '构建校验', status: 'done' },
        { id: 'lane-b', title: '视觉回归', status: 'active' },
      ] },
    });

    const lanes = [...container.querySelectorAll('.paw-wb-graph__lane')];
    expect(lanes.map((lane) => lane.getAttribute('data-lane'))).toEqual(['done', 'active']);
    expect(lanes[0]).toHaveTextContent('已完成');
    expect(lanes[1]).toHaveTextContent('进行中');
    expect(lanes[1]).toHaveTextContent('1');

    // Each populated lane also owns a drawn column, and the selected task's
    // lane is the one the field marks as current.
    const bands = [...container.querySelectorAll<HTMLElement>('.paw-wb-graph__band')];
    expect(bands.map((band) => band.getAttribute('data-lane'))).toEqual(['done', 'active']);
    expect(bands.map((band) => band.style.getPropertyValue('--paw-wb-lane-x')))
      .toEqual(lanes.map((lane) => (lane as HTMLElement).style.getPropertyValue('--paw-wb-lane-x')));
    expect(container.querySelectorAll('.paw-wb-graph__band[data-current]')).toHaveLength(1);
    expect(container.querySelector('.paw-wb-graph__band[data-current]')).toHaveAttribute('data-lane', 'active');
  });

  it('filters the planning outline without hiding graph dependencies', async () => {
    const tasks = [
      ...Array.from({ length: 5 }, (_, index) => ({ id: `routine-${index}`, title: `例行任务 ${index + 1}`, status: 'todo' })),
      { id: 'special', title: '专项验证', status: 'active' },
    ];
    const { container } = renderWorkbench({ pageId: 'planning', planning: { tasks } });

    const filter = screen.getByRole('searchbox', { name: '筛选任务' });
    await userEvent.type(filter, '专项');
    const outline = container.querySelector('.paw-wb-outline') as HTMLElement;
    expect(within(outline).getAllByRole('button', { name: /在任务列表中选择/ })).toHaveLength(1);
    expect(within(outline).getByRole('button', { name: '在任务列表中选择：专项验证' })).toBeInTheDocument();
    expect(container.querySelectorAll('.paw-wb-task-node')).toHaveLength(6);

    await userEvent.clear(filter);
    await userEvent.type(filter, '不存在的任务');
    expect(within(outline).getByText('没有匹配的任务。')).toBeInTheDocument();
    expect(container.querySelectorAll('.paw-wb-task-node')).toHaveLength(6);
  });

  it('keeps a dependency-free dataset free of invented edges', () => {
    const { container } = renderWorkbench({
      pageId: 'planning',
      planning: { tasks: [{ id: 'solo', title: '独立任务', status: 'queued' }] },
    });

    expect(container.querySelectorAll('.paw-wb-graph__edge')).toHaveLength(0);
    expect(container.querySelectorAll('[data-flow-packet]')).toHaveLength(0);
    expect(screen.getByText('1 节点 · 0 条真实依赖')).toBeInTheDocument();
    expect(screen.getByText('该任务记录没有声明依赖。')).toBeInTheDocument();
  });

  it('keeps selection local and exposes explicit task and goal edit actions', async () => {
    const task = { id: 'task-edit', title: '整理 Project 入口', status: 'active' };
    const goal = { id: 'goal-edit', title: '完成 Workbench', status: 'active' };
    const onCreateGoal = vi.fn();
    const onEditGoal = vi.fn();
    const onEditTask = vi.fn();
    const onOpenTask = vi.fn();
    const { container } = renderWorkbench({
      pageId: 'planning',
      planning: { goals: [goal], tasks: [task] },
      onCreateGoal,
      onEditGoal,
      onEditTask,
      onOpenTask,
    });

    fireEvent.click(container.querySelector('.paw-wb-task-node') as HTMLButtonElement);
    expect(onOpenTask).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: '编辑任务' }));
    expect(onEditTask).toHaveBeenCalledWith(task);
    await userEvent.click(screen.getByRole('button', { name: '添加目标' }));
    expect(onCreateGoal).toHaveBeenCalledOnce();
    await userEvent.click(screen.getByRole('button', { name: '编辑目标：完成 Workbench' }));
    expect(onEditGoal).toHaveBeenCalledWith(goal);
  });

  it('follows resolved dependencies, preserves unresolved ids, and resets compact disclosure on selection', async () => {
    const source = { id: 'source-task', title: '准备真实数据', status: 'done', detail: '先完成输入。' };
    const target = {
      id: 'target-task',
      title: '验证最终界面',
      status: 'active',
      detail: '在窄窗口中也必须能查看完整详情。',
      dependencies: ['source-task', 'external-task'],
    };
    const { container } = renderWorkbench({ pageId: 'planning', planning: { tasks: [source, target] } });

    const disclosure = container.querySelector<HTMLButtonElement>('.paw-wb-detail__compact-toggle') as HTMLButtonElement;
    expect(disclosure).toHaveTextContent('展开任务详情');
    expect(disclosure).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(disclosure);
    expect(disclosure).toHaveAttribute('aria-expanded', 'true');
    expect(container.querySelector('.paw-wb-detail')).toHaveAttribute('data-expanded', 'true');

    await userEvent.click(screen.getByRole('button', { name: '查看依赖任务：准备真实数据' }));
    expect(screen.getByRole('heading', { level: 2, name: '准备真实数据' })).toBeInTheDocument();
    expect(container.querySelector('.paw-wb-detail__compact-toggle')).toHaveAttribute('aria-expanded', 'false');
    expect(container.querySelector('.paw-wb-detail')).not.toHaveAttribute('data-expanded');

    await userEvent.click(within(container.querySelector('.paw-wb-outline') as HTMLElement).getByRole('button', { name: /验证最终界面/ }));
    expect(screen.getByText('external-task').closest('.paw-wb-dependencies__unresolved')).toHaveAttribute('title', 'external-task');
    expect(screen.queryByRole('button', { name: /external-task/ })).not.toBeInTheDocument();
  });

  it('preserves the raw WorkDocument open and close handler contracts', async () => {
    const document = workDocument();
    const onOpenDocument = vi.fn();
    const onCloseDocument = vi.fn();
    const { rerender } = renderWorkbench({ pageId: 'documents', documents: [document], onCloseDocument, onOpenDocument });

    await userEvent.click(screen.getByRole('button', { name: /PAWOS 交互重建/ }));
    expect(onOpenDocument).toHaveBeenCalledWith(document);

    rerender(<PawWorkbench {...baseProps({
      pageId: 'documents',
      documents: [document],
      onCloseDocument,
      onOpenDocument,
      selectedDocument: document,
    })} />);
    expect(screen.getByRole('heading', { level: 2, name: 'PAWOS 交互重建' })).toBeInTheDocument();
    expect(screen.getByText('Session Todo')).toBeInTheDocument();
    expect(screen.getByText('/work/paw/docs/active/paw-os.md')).toBeInTheDocument();
    expect(screen.getByText('bbbbbbbbbbbb…bbbb')).toHaveAttribute('title', 'b'.repeat(64));
    await userEvent.click(screen.getByRole('button', { name: '返回文档列表' }));
    expect(onCloseDocument).toHaveBeenCalledOnce();
  });

  it('shows the registered current path for archived documents in the list and reader', () => {
    const document = { ...workDocument(), state: 'archived', path: '/work/paw/docs/archive/paw-os.md' };
    const { rerender } = renderWorkbench({ pageId: 'documents', documentScope: 'history', documents: [document], selectedDocument: document });
    const reader = screen.getByRole('region', { name: '工作文档阅读器' });

    expect.soft(screen.getByRole('button', { name: /PAWOS 交互重建/ }))
      .toHaveAttribute('title', 'PAWOS 交互重建 · /work/paw/docs/archive/paw-os.md');
    expect.soft(within(reader).getByText('当前路径').nextElementSibling)
      .toHaveTextContent('/work/paw/docs/archive/paw-os.md');

    const legacy = { ...workDocument(), path: '' };
    rerender(<PawWorkbench {...baseProps({ pageId: 'documents', documents: [legacy], selectedDocument: legacy })} />);
    expect(screen.getByRole('button', { name: /PAWOS 交互重建/ }))
      .toHaveAttribute('title', 'PAWOS 交互重建 · /work/paw/docs/active/paw-os.md');
    expect(within(reader).getByText('当前路径').nextElementSibling)
      .toHaveTextContent('/work/paw/docs/active/paw-os.md');
  });

  it('filters loaded current documents locally and reports a filter-empty state honestly', async () => {
    const documents = Array.from({ length: 6 }, (_, index) => ({ ...workDocument(), documentId: `doc-${index}`, title: `工作记录 ${index + 1}` }));
    renderWorkbench({ pageId: 'documents', documents });

    const filter = screen.getByRole('searchbox', { name: '筛选当前文档' });
    await userEvent.type(filter, '工作记录 3');
    expect(screen.getByRole('button', { name: /工作记录 3/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /工作记录 1/ })).not.toBeInTheDocument();

    await userEvent.clear(filter);
    await userEvent.type(filter, '不存在的文档');
    expect(screen.getByText('没有匹配的文档')).toBeInTheDocument();
    expect(screen.queryByText('暂无工作文档')).not.toBeInTheDocument();
  });

  it('keeps a current-document filter reachable after a refresh shrinks the catalog', async () => {
    const documents = Array.from({ length: 6 }, (_, index) => ({ ...workDocument(), documentId: `doc-${index}`, title: `工作记录 ${index + 1}` }));
    const { rerender } = renderWorkbench({ pageId: 'documents', documents });
    await userEvent.type(screen.getByRole('searchbox', { name: '筛选当前文档' }), '工作记录 6');
    rerender(<PawWorkbench {...baseProps({ pageId: 'documents', documents: documents.slice(0, 1) })} />);

    expect(screen.getByRole('searchbox', { name: '筛选当前文档' })).toHaveValue('工作记录 6');
    await userEvent.click(screen.getByRole('button', { name: '清除文档筛选' }));
    expect(screen.getByRole('button', { name: /工作记录 1/ })).toBeInTheDocument();
  });

  it('keeps a task filter reachable after a new plan contains fewer tasks', async () => {
    const tasks = Array.from({ length: 6 }, (_, index) => ({ id: `task-${index}`, title: `当天任务 ${index + 1}` }));
    const { rerender } = renderWorkbench({ pageId: 'planning', planning: { tasks } });
    await userEvent.type(screen.getByRole('searchbox', { name: '筛选任务' }), '当天任务 6');
    rerender(<PawWorkbench {...baseProps({ pageId: 'planning', planning: { tasks: tasks.slice(0, 1) } })} />);

    expect(screen.getByRole('searchbox', { name: '筛选任务' })).toHaveValue('当天任务 6');
    await userEvent.click(screen.getByRole('button', { name: '清除任务筛选' }));
    expect(screen.getByRole('button', { name: '在任务列表中选择：当天任务 1' })).toBeInTheDocument();
  });

  it('binds the planning tools to the actual selected task', async () => {
    const tasks = [{ id: 'first', title: '首项任务' }, { id: 'selected', title: '被选择的任务' }];
    const onBreakdown = vi.fn();
    renderWorkbench({
      pageId: 'planning', planning: { tasks },
      planningTools: (task) => <button onClick={() => onBreakdown(task)} type="button">拆解选择</button>,
    });
    await userEvent.click(screen.getByRole('button', { name: '在任务列表中选择：被选择的任务' }));
    await userEvent.click(screen.getByRole('button', { name: '拆解选择' }));
    expect(onBreakdown).toHaveBeenCalledWith(tasks[1]);
  });

  it('withholds all pane counts until their matching resource is available', () => {
    const { container, rerender } = renderWorkbench({ resourceStates: { planning: { loading: true }, documents: { error: '未能读取。' } } });
    expect(container.querySelector('[data-pane="tasks"] .paw-wb-pane__scope')).toHaveTextContent('数量待读取');
    expect(container.querySelector('[data-pane="documents"] .paw-wb-pane__scope')).toHaveTextContent('数量暂不可用');
    rerender(<PawWorkbench {...baseProps({ pageId: 'planning', resourceStates: { planning: { loading: true } } })} />);
    expect(screen.queryByText('0 节点 · 0 条真实依赖')).not.toBeInTheDocument();
    expect(container.querySelector('.paw-wb-outline__project')).toHaveTextContent('数量待读取');
  });

  it('shows recoverable detail loading and errors without asking to choose the same document again', async () => {
    const onCloseDocument = vi.fn();
    const onRefresh = vi.fn();
    const { rerender } = renderWorkbench({ pageId: 'documents', onCloseDocument, onRefresh, resourceStates: { documentDetail: { loading: true } } });
    const reader = screen.getByRole('region', { name: '工作文档阅读器' });
    expect(within(reader).getByRole('status')).toHaveTextContent('正在读取文档详情');
    expect(within(reader).queryByText('选择一份工作文档')).not.toBeInTheDocument();
    rerender(<PawWorkbench {...baseProps({ pageId: 'documents', onCloseDocument, onRefresh, resourceStates: { documentDetail: { error: '文档读取失败。' } } })} />);
    await userEvent.click(within(reader).getByRole('button', { name: '重试' }));
    expect(onRefresh).toHaveBeenCalledWith('documentDetail');
    await userEvent.click(within(reader).getByRole('button', { name: '返回文档列表' }));
    expect(onCloseDocument).toHaveBeenCalledOnce();
  });

  it('moves identity facts into the reader and retires the static truth rail', () => {
    const document = workDocument();
    const { container } = renderWorkbench({ pageId: 'documents', documents: [document], selectedDocument: document });

    expect(screen.queryByText('事实边界')).not.toBeInTheDocument();
    expect(container.querySelector('.paw-wb-document-truth')).toBeNull();
    const facts = screen.getByText('Document ID').closest('dl') as HTMLElement;
    expect(within(facts).getByText(`workdoc_${'a'.repeat(32)}`)).toBeInTheDocument();
    expect(within(facts).getByText('session_todo:session-1:3')).toBeInTheDocument();
  });

  it('keeps the reader title as fixed chrome above one scrolling authority body', () => {
    const document = workDocument();
    const { container, rerender } = renderWorkbench({
      pageId: 'documents',
      documents: [document],
      documentLifecycle: <button type="button">归档到历史</button>,
      selectedDocument: document,
    });

    const reader = container.querySelector('.paw-wb-document-reader') as HTMLElement;
    expect([...reader.children].map((node) => node.tagName.toLowerCase())).toEqual(['header', 'div']);
    const body = reader.querySelector('.paw-wb-document-reader__body') as HTMLElement;
    expect(within(body).getByText('Document ID')).toBeInTheDocument();
    expect(within(body).getByRole('button', { name: '归档到历史' })).toBeInTheDocument();
    // The title, the close affordance and the state badge never scroll away.
    const header = reader.querySelector('header') as HTMLElement;
    expect(within(header).getByRole('heading', { level: 2, name: 'PAWOS 交互重建' })).toBeInTheDocument();
    expect(within(header).getByRole('button', { name: '返回文档列表' })).toBeInTheDocument();

    rerender(<PawWorkbench {...baseProps({ pageId: 'documents', documents: [document] })} />);
    expect(container.querySelector('.paw-wb-document-reader > header')).toBeNull();
    expect(within(container.querySelector('.paw-wb-document-reader__body') as HTMLElement).getByText('选择一份工作文档')).toBeInTheDocument();
  });

  it('makes an overview WorkDocument click enter the reader with that document visible', async () => {
    const document = workDocument();
    const onCloseDocument = vi.fn();

    renderWorkbench({ pageId: 'overview', documents: [document], onCloseDocument });

    await userEvent.click(screen.getByRole('button', { name: /PAWOS 交互重建/ }));

    expect(screen.getByRole('heading', { level: 1, name: '工作文档' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 2, name: 'PAWOS 交互重建' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '返回文档列表' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: '返回文档列表' }));
    expect(screen.getByRole('heading', { level: 1, name: '项目概览' })).toBeInTheDocument();
    expect(onCloseDocument).toHaveBeenCalledOnce();
  });

  it('does not confuse unresolved resource reads with truthful empty collections', () => {
    const loading = renderWorkbench({
      pageId: 'overview',
      resourceStates: { planning: { loading: true }, documents: { error: '工作文档读取失败。' } },
    });

    expect(screen.getByRole('status')).toHaveTextContent('正在读取任务');
    expect(screen.getByRole('alert')).toHaveTextContent('工作文档读取失败。');
    expect(screen.queryByText('暂无真实任务')).not.toBeInTheDocument();
    expect(screen.queryByText('暂无工作文档')).not.toBeInTheDocument();

    loading.rerender(<PawWorkbench {...baseProps({
      pageId: 'planning',
      resourceStates: { planning: { error: '任务编排读取失败。' } },
    })} />);
    expect(screen.getByRole('alert')).toHaveTextContent('任务编排读取失败。');
    expect(screen.queryByText('没有可编排的真实任务')).not.toBeInTheDocument();

    loading.rerender(<PawWorkbench {...baseProps({
      pageId: 'documents',
      resourceStates: { documents: { loading: true } },
    })} />);
    expect(screen.getByRole('status')).toHaveTextContent('正在读取工作文档');
    expect(screen.queryByText('暂无工作文档')).not.toBeInTheDocument();
  });

  it('withholds lifecycle actions when current WorkDocument detail truth failed to load', () => {
    const document = workDocument();
    renderWorkbench({
      pageId: 'documents',
      documents: [document],
      selectedDocument: document,
      documentLifecycle: <button type="button">归档到历史</button>,
      resourceStates: { documentDetail: { error: '详情读取失败。' } },
    });

    expect(screen.getByRole('alert')).toHaveTextContent('详情读取失败。');
    expect(screen.queryByRole('button', { name: '归档到历史' })).not.toBeInTheDocument();
  });

  it('distinguishes task-list and dependency-graph selection names', () => {
    renderWorkbench({
      pageId: 'planning',
      planning: { tasks: [{ id: 'task-1', title: '验证最终界面', status: 'active' }] },
    });

    expect(screen.getByRole('button', { name: '在任务列表中选择：验证最终界面' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '在依赖图中选择：验证最终界面' })).toBeInTheDocument();
  });

  it('keeps overview task and document windows explicit, ordered, and expandable', async () => {
    const onNavigate = vi.fn();
    const tasks = Array.from({ length: 9 }, (_, index) => ({ id: `task-${index}`, title: `任务 ${index + 1}`, status: 'todo' }));
    const documents = Array.from({ length: 7 }, (_, index) => ({ ...workDocument(), documentId: `document-${index}`, title: `文档 ${index + 1}` }));
    renderWorkbench({ documentTotal: 101, documents, onNavigate, planning: { tasks } });

    expect(screen.getByText('当前 8 / 共 9')).toBeInTheDocument();
    expect(screen.getByText('当前 6 / 已加载 7 · 共 101')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /任务 9/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /文档 7/ })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: '显示更多任务：1 项' }));
    await userEvent.click(screen.getByRole('button', { name: '显示更多工作文档：1 项' }));
    expect(screen.getByRole('button', { name: /任务 9/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /文档 7/ })).toBeInTheDocument();
    expect(screen.getByText('当前 9 / 共 9')).toBeInTheDocument();
    expect(screen.getByText('当前 7 / 已加载 7 · 共 101')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '在工作文档中查看其余 94 项' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: '在工作文档中查看其余 94 项' }));
    expect(onNavigate).toHaveBeenCalledWith('documents');
  });

  it('keeps create and retry controls absent when their real handlers are unavailable', () => {
    renderWorkbench({
      pageId: 'overview',
      resourceStates: { overview: { error: '本机服务暂时不可用。' } },
    });

    expect(screen.queryByRole('button', { name: '新任务' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '重试' })).not.toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('本机服务暂时不可用。');
  });
});

function renderWorkbench(overrides: Partial<PawWorkbenchProps> = {}) {
  return render(<PawWorkbench {...baseProps(overrides)} />);
}

function baseProps(overrides: Partial<PawWorkbenchProps> = {}): PawWorkbenchProps {
  return {
    pageId: 'overview',
    overview: { project: { name: 'personal-agent-workbench', path: '/work/paw' } },
    planning: { tasks: [] },
    documents: [],
    selectedDocument: null,
    onOpenTask: vi.fn(),
    onOpenDocument: vi.fn(),
    onCloseDocument: vi.fn(),
    ...overrides,
  };
}

function workDocument(): PawWorkbenchRecord {
  return {
    documentId: `workdoc_${'a'.repeat(32)}`,
    authorityKind: 'session_todo',
    authorityId: 'session-1',
    authorityRevision: 3,
    authorityKey: 'session_todo:session-1:3',
    documentRevision: 2,
    contentSha256: 'b'.repeat(64),
    workspaceRoot: '/work/paw',
    path: '/work/paw/docs/active/paw-os.md',
    activePath: '/work/paw/docs/active/paw-os.md',
    archivePath: '/work/paw/docs/archive/paw-os.md',
    state: 'active',
    title: 'PAWOS 交互重建',
    terminalReceiptId: '',
    error: '',
    createdAtMs: 2,
    updatedAtMs: 3,
  };
}
