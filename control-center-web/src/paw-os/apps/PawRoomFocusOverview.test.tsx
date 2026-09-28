import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { RoomFocusProjection } from './room-focus-projection';
import { PawRoomFocusOverview } from './PawRoomFocusOverview';

afterEach(cleanup);

const focus: RoomFocusProjection = {
  goal: {
    title: '任务图依赖验证',
    description: '两个实现分支汇合后复核。',
    rootId: 'turn-root',
    state: 'review',
  },
  workItems: [
    {
      id: 'work-root',
      source: 'work-item',
      objective: '整合 Room 任务图',
      expectedOutput: '可复查的整合版本',
      acceptanceCriteria: [],
      ownerParticipantId: 'p-venus',
      accountableParticipantId: 'p-venus',
      verifierParticipantId: 'p-venus',
      state: 'review',
      reviewRequired: true,
      review: { operability: 'passed', requirement: 'satisfied', reviewerParticipantId: 'p-venus' },
      latestResult: '两个实现分支已汇合。',
      evidence: [{ ref: 'test:room-focus', kind: 'evidence' }],
      updatedAtMs: 30,
    },
    {
      id: 'runtime:earth',
      parentId: 'work-root',
      source: 'runtime',
      objective: '实现任务图交互',
      acceptanceCriteria: [],
      ownerParticipantId: 'p-earth',
      state: 'completed',
      currentAction: '任务图交互已通过测试',
      reviewRequired: false,
      wave: { waveId: 'wave-a', phaseName: '并行实现两条支线', parallelIndex: 0, parallelSize: 2 },
      evidence: [],
      dispatchId: 'earth',
      updatedAtMs: 20,
    },
    {
      id: 'runtime:mars',
      parentId: 'work-root',
      source: 'runtime',
      objective: '实现依赖数据投影',
      acceptanceCriteria: [],
      ownerParticipantId: 'p-mars',
      state: 'running',
      currentAction: '正在核对依赖投影',
      reviewRequired: false,
      wave: { waveId: 'wave-a', phaseName: '并行实现两条支线', parallelIndex: 1, parallelSize: 2 },
      evidence: [],
      dispatchId: 'mars',
      updatedAtMs: 21,
    },
  ],
  partners: [
    {
      participantId: 'p-earth',
      sessionId: 'session-earth',
      displayName: 'Agent 1',
      celestialName: 'Earth',
      state: 'completed',
      ownedWorkItemIds: ['runtime:earth'],
      currentAction: '任务图交互已通过测试',
      latestReceipt: '交互实现完成',
      unread: false,
    },
    {
      participantId: 'p-mars',
      sessionId: 'session-mars',
      displayName: 'Agent 2',
      celestialName: 'Mars',
      state: 'running',
      ownedWorkItemIds: ['runtime:mars'],
      currentAction: '正在核对依赖投影',
      unread: false,
    },
    {
      participantId: 'p-venus',
      sessionId: 'session-venus',
      displayName: 'Agent 3',
      celestialName: 'Venus',
      collaborationRole: 'coordinator',
      state: 'review',
      ownedWorkItemIds: ['work-root'],
      currentAction: '等待独立复核',
      unread: false,
    },
  ],
  handoffs: [{
    id: 'handoff-earth-mars',
    sourceParticipantId: 'p-earth',
    targetParticipantId: 'p-mars',
    dispatchId: 'mars',
    task: '交付依赖投影',
    state: 'dispatched',
    createdAtMs: 22,
  }],
  flow: [
    {
      id: 'message:request',
      sourceParticipantId: 'root',
      targetParticipantIds: ['p-earth'],
      kind: 'request',
      summary: '请并行实现任务图交互与依赖投影',
      status: 'completed',
      createdAtMs: 10,
      sequence: 1,
      refs: [],
    },
    {
      id: 'activity:dispatch-mars',
      sourceParticipantId: 'p-earth',
      targetParticipantIds: ['p-mars'],
      kind: 'dispatch',
      summary: '分派依赖投影支线',
      status: 'completed',
      createdAtMs: 22,
      sequence: 2,
      dispatchId: 'mars',
      dispatchPlan: {
        dispatchId: 'mars',
        parentDispatchId: 'earth-root',
        child: true,
        reason: 'partner_delegate',
        reasonLabel: '伙伴委派',
        routingPolicy: 'parallel',
        routingPolicyLabel: '并行协作',
        targetParticipantId: 'p-mars',
        targetDisplayName: 'Agent 2',
        waveId: 'wave-a',
        phaseName: '并行实现两条支线',
        parallelIndex: 1,
        parallelSize: 2,
        workItemId: 'runtime:mars',
        subjectTaskId: '',
        purpose: '',
        workItemState: 'active',
        candidates: [
          { participantId: 'p-earth', displayName: 'Agent 1', score: 0, signals: [], selected: false },
          { participantId: 'p-mars', displayName: 'Agent 2', score: 1, signals: ['explicit_invite'], selected: true },
        ],
      },
      refs: ['context://room/brief'],
    },
    {
      id: 'message:result-earth',
      sourceParticipantId: 'p-earth',
      targetParticipantIds: ['root'],
      kind: 'result',
      summary: '任务图交互已通过测试',
      status: 'completed',
      createdAtMs: 30,
      sequence: 3,
      refs: [],
    },
  ],
  rootEvidence: [{ ref: 'test:room-focus', kind: 'evidence' }],
  counts: { active: 1, review: 1, blocked: 0, completed: 1 },
};

describe('PawRoomFocusOverview', () => {
  it('shows a bilateral reply path and lets the user return to the original message', () => {
    const traffic: RoomFocusProjection = { ...focus, flow: [
      { id: 'intercom:ask', intercomId: 'ask', sourceParticipantId: 'p-earth', targetParticipantIds: ['p-mars'], kind: 'question', summary: '接口是否可用？', status: 'replied', createdAtMs: 100, sequence: 1, refs: [] },
      { id: 'intercom:reply', intercomId: 'reply', replyToPacketId: 'intercom:ask', sourceParticipantId: 'p-mars', targetParticipantIds: ['p-earth'], kind: 'answer', summary: '接口已验证，可以接入 `room_partner`。', status: 'delivered', createdAtMs: 200, deliveredAtMs: 210, sequence: 2, refs: ['test:api'] },
      { id: 'approval', sourceParticipantId: 'p-earth', targetParticipantIds: ['root'], kind: 'approval', summary: 'approval_resolved', status: 'completed', createdAtMs: 300, sequence: 3, refs: [] },
    ] };
    render(<PawRoomFocusOverview focus={traffic} />);
    const packets = screen.getByRole('list', { name: '往来事件' });
    expect(within(packets).getAllByRole('listitem')).toHaveLength(2);
    expect(packets).toHaveTextContent('Earth → Mars');
    expect(packets).toHaveTextContent('Mars → Earth');
    const route = screen.getByLabelText('选中消息的流转方向');
    expect(route.textContent).toMatch(/Mars.*回复.*已送达.*Earth/);
    expect(document.querySelector('.paw-room-focus-overview__message-content code')).toHaveTextContent('room_partner');
    const selectedMessage = route.parentElement as HTMLDivElement;
    const scrollToSelection = vi.fn();
    Object.defineProperty(selectedMessage, 'scrollIntoView', { value: scrollToSelection, configurable: true });
    expect(selectedMessage.compareDocumentPosition(packets) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Earth：接口是否可用？' }));
    expect(scrollToSelection).toHaveBeenCalledWith({ block: 'start', behavior: 'auto' });
    expect(route.textContent).toMatch(/Earth.*询问.*已回复.*Mars/);
    fireEvent.click(screen.getByRole('button', { name: '查看 Mars 的回复' }));
    fireEvent.click(screen.getByText('消息证据'));
    expect(screen.getByText('test:api')).toBeVisible();
    expect(screen.getByText('送达时间')).toBeVisible();
    fireEvent.change(screen.getByRole('combobox', { name: '消息类型' }), { target: { value: 'all' } });
    expect(within(packets).getAllByRole('listitem')).toHaveLength(3);
    expect(packets).not.toHaveTextContent('approval_resolved');
  });

  it('shows per-planet satellite status and unknown separately from a confirmed empty Session', () => {
    render(<PawRoomFocusOverview focus={focus} satellitesByParticipant={{
      'p-earth': { status: 'ready', satellites: [
        { id: 'run-1', nodeId: 'node-1', sessionId: 'child-1', task: '核对传递方向', state: 'running', stateLabel: '进行中', depth: 1, result: '', error: '' },
        { id: 'run-2', nodeId: 'node-2', sessionId: 'child-2', task: '验证消息回执', state: 'returned', stateLabel: '已返回', depth: 1, result: '回执已核对', error: '' },
      ] },
      'p-mars': { status: 'ready', satellites: [] },
      'p-venus': { status: 'error', satellites: [] },
    }} />);
    const mesh = screen.getByRole('group', { name: '协作网状图' });
    const earth = within(mesh).getByRole('button', { name: /^Earth，/ });
    expect(earth).toHaveTextContent('卫星 2');
    expect(earth).toHaveTextContent('进行 1 · 已返回 1');
    expect(within(mesh).getByRole('button', { name: /^Mars，/ })).toHaveTextContent('卫星 0');
    expect(within(mesh).getByRole('button', { name: /^Venus，/ })).toHaveTextContent('卫星暂不可用');
    fireEvent.click(earth);
    const satellites = screen.getByRole('region', { name: 'Earth 的 Session 卫星' });
    fireEvent.click(within(satellites).getByText('验证消息回执'));
    expect(satellites).toHaveTextContent('回执已核对');
    expect(satellites).toHaveTextContent('child-2');
  });

  it('answers partner responsibility, state and handoff without drawing Sol or tasks as partners', () => {
    const { container } = render(<PawRoomFocusOverview focus={focus} onOpenParticipant={vi.fn()} />);

    expect(screen.getByRole('region', { name: 'Sol 协作态势' })).toHaveTextContent('任务图依赖验证');
    const mesh = screen.getByRole('group', { name: '协作网状图' });
    expect(within(mesh).getByRole('button', { name: 'Earth，已完成，职责：实现任务图交互，已完成' })).toBeInTheDocument();
    expect(within(mesh).getByRole('button', { name: 'Mars，进行中，职责：实现依赖数据投影，进行中' })).toBeInTheDocument();
    expect(within(mesh).queryByRole('img', { name: /^Sol，/ })).not.toBeInTheDocument();
    expect(mesh.querySelector('.paw-room-focus-overview__mesh-node--work')).toBeNull();
    // A dispatched handoff is a real attempt, but not yet an established
    // success edge; it stays inspectable in the bounded disclosure.
    expect(container.querySelector('.paw-room-focus-overview__mesh-edge[data-kind="handoff"][data-state="dispatched"]')).toBeNull();
    const attemptSummary = screen.getByText(/未确认关系/).closest('summary');
    expect(attemptSummary).not.toBeNull();
    fireEvent.click(attemptSummary!);
    const attempts = attemptSummary!.closest('details')!;
    expect(attempts).toHaveTextContent(/Earth\s*→\s*Mars/);
    expect(attempts).toHaveTextContent('交接 · 已分派');
    fireEvent.click(within(mesh).getByText(/协作关系 ·/));
    expect(within(mesh).getByRole('button', { name: 'Earth → Mars，分派，已完成' })).toBeInTheDocument();
    expect(within(mesh).queryByRole('list', { name: '协作关系列表' })).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: '焦点详情' })).toHaveTextContent('等待独立复核');
  });

  it('keeps planet responsibility labels aligned on the readable partner grid', () => {
    render(<PawRoomFocusOverview focus={focus} onOpenParticipant={vi.fn()} />);
    const mesh = screen.getByRole('group', { name: '协作网状图' });
    const planets = Array.from(mesh.querySelectorAll<HTMLButtonElement>('.paw-room-focus-overview__mesh-node'));
    expect(planets).toHaveLength(3);
    expect(mesh).toHaveAttribute('data-view', 'roster');
    expect(within(mesh).getByText('实现任务图交互')).toBeInTheDocument();
    expect(within(mesh).getByText('实现依赖数据投影')).toBeInTheDocument();
    expect(within(mesh).getByText('整合 Room 任务图')).toBeInTheDocument();
    expect(document.querySelector('.paw-room-focus-overview__mesh-timespan')).toBeNull();
  });

  it('selects a planet with pointer or keyboard and opens only its real participant target', async () => {
    const user = userEvent.setup();
    const onOpenParticipant = vi.fn();
    render(<PawRoomFocusOverview focus={focus} onOpenParticipant={onOpenParticipant} />);
    const mesh = screen.getByRole('group', { name: '协作网状图' });

    await user.click(within(mesh).getByRole('button', { name: /^Mars，/ }));
    expect(screen.getByRole('region', { name: '焦点详情' })).toHaveTextContent('正在核对依赖投影');
    expect(onOpenParticipant).toHaveBeenNthCalledWith(1, 'p-mars');
    within(mesh).getByRole('button', { name: /^Earth，/ }).focus();
    await user.keyboard('{Enter}');
    expect(onOpenParticipant).toHaveBeenNthCalledWith(2, 'p-earth');
    await user.click(screen.getByRole('button', { name: '打开 Earth 伙伴窗口' }));

    expect(onOpenParticipant).toHaveBeenCalledWith('p-earth');
  });

  it('selects a gravity relation from one visible accessible control, with real provenance and planet actions', async () => {
    const user = userEvent.setup();
    const onOpenParticipant = vi.fn();
    render(<PawRoomFocusOverview focus={focus} onOpenParticipant={onOpenParticipant} />);
    const mesh = screen.getByRole('group', { name: '协作网状图' });

    fireEvent.click(within(mesh).getByText(/协作关系 ·/));
    const relationLabel = mesh.querySelector<HTMLButtonElement>('.paw-room-focus-overview__mesh-edge-label[data-kind="dispatch"]')!;
    await user.click(relationLabel);

    const detail = screen.getByRole('region', { name: '协作关系详情' });
    expect(detail).toHaveTextContent('Earth → Mars');
    expect(detail).toHaveTextContent('关系类型分派');
    expect(detail).toHaveTextContent('当前状态已完成');
    expect(detail).toHaveTextContent('eventIds');
    expect(detail).toHaveTextContent('activity:dispatch-mars');
    expect(detail).toHaveTextContent('dispatchIds');
    expect(detail).toHaveTextContent('mars');

    await user.click(within(detail).getByRole('button', { name: '打开 Earth 伙伴窗口' }));
    await user.click(within(detail).getByRole('button', { name: '打开 Mars 伙伴窗口' }));
    expect(onOpenParticipant.mock.calls).toEqual([['p-earth'], ['p-mars']]);

    expect(relationLabel).toHaveAttribute('aria-pressed', 'true');
    relationLabel.focus();
    await user.keyboard(' ');
    expect(screen.getByRole('region', { name: '协作关系详情' })).toHaveTextContent('activity:dispatch-mars');
  });

  it('keeps failed dispatch attempts out of the DAG and makes their provenance accessible', () => {
    const failedDispatchFocus: RoomFocusProjection = {
      ...focus,
      workItems: [],
      handoffs: [],
      flow: [
        {
          id: 'dispatch-success', sourceParticipantId: 'p-earth', targetParticipantIds: ['p-mars'],
          kind: 'dispatch', summary: '成功分派', status: 'completed', createdAtMs: 1, sequence: 1,
          dispatchId: 'dispatch-shared', refs: [],
        },
        {
          id: 'dispatch-failed', sourceParticipantId: 'p-earth', targetParticipantIds: ['p-mars'],
          kind: 'dispatch', summary: '失败分派重试', status: 'failed', createdAtMs: 2, sequence: 2,
          dispatchId: 'dispatch-shared', refs: [],
        },
      ],
    };
    const { container } = render(<PawRoomFocusOverview focus={failedDispatchFocus} onOpenParticipant={vi.fn()} />);

    const mesh = screen.getByRole('group', { name: '协作网状图' });
    fireEvent.click(within(mesh).getByText(/协作关系 ·/));
    expect(container.querySelectorAll('.paw-room-focus-overview__mesh-edge-label[data-kind="dispatch"]')).toHaveLength(1);
    const summary = screen.getByText(/未确认关系/).closest('summary');
    expect(summary).not.toBeNull();
    expect(summary).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(summary!);

    const disclosure = summary!.closest('details')!;
    expect(disclosure).toHaveTextContent('失败分派重试');
    expect(disclosure).toHaveTextContent('失败尝试');
    expect(disclosure).toHaveTextContent('dispatch-failed');
    expect(disclosure).toHaveTextContent('dispatch-shared');
  });

  it('keeps delivered relations visible as delivered, never as completed', () => {
    const deliveredFocus: RoomFocusProjection = {
      ...focus,
      workItems: [],
      handoffs: [],
      flow: [{
        id: 'receipt-delivered', sourceParticipantId: 'p-earth', targetParticipantIds: ['p-mars'],
        kind: 'answer', summary: '收到任务', status: 'delivered', createdAtMs: 1, sequence: 1, refs: [],
      }],
    };
    render(<PawRoomFocusOverview focus={deliveredFocus} onOpenParticipant={vi.fn()} />);

    const summary = screen.getByText(/未确认关系/).closest('summary')!;
    fireEvent.click(summary);
    const disclosure = summary.closest('details')!;
    expect(disclosure).toHaveTextContent('回执 · 已送达');
    expect(disclosure).not.toHaveTextContent('回执 · 已完成');
  });

  it('shows every confirmed attempt receipt in the selected relation detail', () => {
    const confirmedDispatchFocus: RoomFocusProjection = {
      ...focus,
      workItems: [],
      handoffs: [],
      flow: [
        {
          id: 'dispatch-success-2', sourceParticipantId: 'p-earth', targetParticipantIds: ['p-mars'],
          kind: 'dispatch', summary: '第二次成功分派', status: 'completed', createdAtMs: 20, sequence: 2,
          dispatchId: 'dispatch-shared', refs: [],
        },
        {
          id: 'dispatch-success-1', sourceParticipantId: 'p-earth', targetParticipantIds: ['p-mars'],
          kind: 'dispatch', summary: '第一次成功分派', status: 'completed', createdAtMs: 10, sequence: 1,
          dispatchId: 'dispatch-shared', refs: [],
        },
      ],
    };
    const { container } = render(<PawRoomFocusOverview focus={confirmedDispatchFocus} onOpenParticipant={vi.fn()} />);
    const mesh = screen.getByRole('group', { name: '协作网状图' });
    fireEvent.click(within(mesh).getByText(/协作关系 ·/));
    fireEvent.click(mesh.querySelector<HTMLAnchorElement>('.paw-room-focus-overview__mesh-edge-label[data-kind="dispatch"]')!);

    const detail = screen.getByRole('region', { name: '协作关系详情' });
    const receipts = within(detail).getByRole('list', { name: '确认尝试回执' });
    const receiptItems = within(receipts).getAllByRole('listitem');
    expect(receiptItems).toHaveLength(2);
    expect(receiptItems[0]).toHaveTextContent('第一次成功分派');
    expect(receiptItems[0]).toHaveTextContent('dispatch-success-1');
    expect(receiptItems[1]).toHaveTextContent('第二次成功分派');
    expect(receiptItems[1]).toHaveTextContent('dispatch-success-2');
    expect(container.querySelectorAll('.paw-room-focus-overview__mesh-edge-label[data-kind="dispatch"]')).toHaveLength(1);
  });

  it('keeps WorkItem detail in the inspector without drawing a WorkItem node', () => {
    render(<PawRoomFocusOverview focus={focus} onOpenParticipant={vi.fn()} />);
    const mesh = screen.getByRole('group', { name: '协作网状图' });
    expect(within(mesh).queryByRole('button', { name: '实现任务图交互，已完成' })).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: '焦点详情' })).toHaveTextContent('两个实现分支已汇合');
  });

  it('keeps the chronological flow ledger inside the console with celestial actor names and packet detail', () => {
    render(<PawRoomFocusOverview focus={focus} onOpenParticipant={vi.fn()} />);

    const ledger = screen.getByRole('region', { name: '往来记录' });
    expect(within(ledger).getByText('最近 2 / 共 2 条')).toBeInTheDocument();
    fireEvent.change(within(ledger).getByRole('combobox', { name: '消息类型' }), { target: { value: 'all' } });
    expect(within(ledger).getByText('最近 3 / 共 3 条')).toBeInTheDocument();
    const packets = within(ledger).getByRole('list', { name: '往来事件' });
    expect(packets).toHaveTextContent('Sol → Earth');
    expect(packets).toHaveTextContent('Earth → Mars');

    // The latest packet is pre-selected; picking the dispatch reveals its refs.
    expect(within(ledger).getByRole('button', { name: /任务图交互已通过测试/ })).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(within(ledger).getByRole('button', { name: /分派依赖投影支线/ }));
    const detail = ledger.querySelector('.paw-room-focus-overview__packet-detail')!;
    expect(detail).toHaveTextContent('任务分派');
    fireEvent.click(within(ledger).getByText('消息证据'));
    expect(detail).toHaveTextContent('context://room/brief');
    expect(detail).toHaveTextContent('mars');
  });

  it('renders a selected route decision as a readable dispatch plan, not a dead label', () => {
    render(<PawRoomFocusOverview focus={focus} onOpenParticipant={vi.fn()} />);

    const ledger = screen.getByRole('region', { name: '往来记录' });
    fireEvent.click(within(ledger).getByRole('button', { name: /分派依赖投影支线/ }));
    const plan = within(ledger).getByRole('group', { name: '分派方案' });

    // Route, reason, policy and the parallel track all read as prose.
    expect(plan).toHaveTextContent('Earth');
    expect(plan).toHaveTextContent('Mars');
    expect(plan).toHaveTextContent('伙伴委派 · 并行协作');
    expect(plan).toHaveTextContent('轨道 2/2 · 并行实现两条支线');
    expect(plan).toHaveTextContent('实现依赖数据投影');
    expect(plan).not.toHaveTextContent('Agent 2');

    // Candidate scoring stays reachable behind a disclosure.
    fireEvent.click(within(plan).getByText('候选 2 位 · 选中 1 位'));
    expect(plan).toHaveTextContent('explicit_invite · 1.0');
  });

  it('projects the pulse meter while keeping structural WorkItems out of the gravity mesh', () => {
    const { container } = render(<PawRoomFocusOverview focus={focus} onOpenParticipant={vi.fn()} />);

    // Pulse: proportional segments plus the exact numbers (进行1 复核1 完成1).
    const pulse = screen.getByLabelText('协作摘要');
    expect(pulse.querySelectorAll('.paw-room-focus-overview__pulse-bar > i')).toHaveLength(3);
    expect(pulse).toHaveTextContent('进行');
    expect(pulse).toHaveTextContent('复核');

    const mesh = screen.getByRole('group', { name: '协作网状图' });
    expect(within(mesh).getByText('实现任务图交互')).toBeInTheDocument();
    expect(within(mesh).getByText('实现依赖数据投影')).toBeInTheDocument();
    expect(container.querySelectorAll('.paw-room-focus-overview__mesh-edge[data-kind="dependency"]')).toHaveLength(0);
    expect(container.querySelectorAll('.paw-room-focus-overview__mesh-edge[data-kind="handoff"]')).toHaveLength(0);
    expect(container.querySelectorAll('.paw-room-focus-overview__mesh-edge[data-kind="review"]')).toHaveLength(0);
    expect(container.querySelector('.paw-room-focus-overview__mesh-node--work')).toBeNull();
    expect(screen.queryByRole('list', { name: '关系图例' })).not.toBeInTheDocument();
    expect(within(mesh).getByText(/协作关系 · 1 条已确认/)).toBeInTheDocument();
  });

  it('answers the dual-axis review verdict inside the inspector', () => {
    render(<PawRoomFocusOverview focus={focus} onOpenParticipant={vi.fn()} />);

    // work-root (review state) is the default selection.
    const inspector = screen.getByRole('region', { name: '焦点详情' });
    expect(inspector).toHaveTextContent('独立复核 · Venus');
    expect(inspector).toHaveTextContent('可运行');
    expect(inspector).toHaveTextContent('通过');
    expect(inspector).toHaveTextContent('符合需求');
    expect(inspector).toHaveTextContent('满足');
  });

  it('windows a long ledger to the recent slice until the reader asks for everything', () => {
    const longFlow = Array.from({ length: 21 }, (_, index) => ({
      id: `packet-${index}`,
      sourceParticipantId: 'root',
      targetParticipantIds: ['p-earth'],
      kind: 'dispatch' as const,
      summary: `分派 ${index}`,
      status: 'completed',
      createdAtMs: index,
      sequence: index,
      refs: [],
    }));
    render(<PawRoomFocusOverview focus={{ ...focus, flow: longFlow }} onOpenParticipant={vi.fn()} />);

    expect(screen.getByText('最近 18 / 共 21 条')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '显示全部' }));
    expect(screen.getByText('最近 21 / 共 21 条')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '显示全部' })).not.toBeInTheDocument();
  });

  it('draws no Sol anywhere until a partner really hosts the Room', () => {
    /* Venus is the only coordinator in the fixture; demote her and the whole
       console has to stop naming an origin nobody sits at. */
    const unhosted: RoomFocusProjection = {
      ...focus,
      partners: focus.partners.map((partner) => partner.collaborationRole === 'coordinator'
        ? { ...partner, collaborationRole: 'reviewer' }
        : partner),
    };
    const { container } = render(<PawRoomFocusOverview focus={unhosted} onOpenParticipant={vi.fn()} />);

    // No mission header, no Sol centre body, no origin lifeline.
    expect(container.querySelector('.paw-room-focus-overview__mission--dormant')).not.toBeNull();
    expect(container.querySelector('.paw-room-focus-overview__sol')).toBeNull();
    expect(screen.queryByRole('img', { name: /^Sol，/ })).not.toBeInTheDocument();
    expect(container.querySelector('.paw-room-focus-overview')).not.toHaveAttribute('data-coordinator');
    // The ledger still has to name where a root packet came from — as the
    // shared main Room, not as a star that has not risen.
    const packets = within(screen.getByRole('region', { name: '往来记录' })).getByRole('list', { name: '往来事件' });
    expect(packets).toHaveTextContent('主 Room → Earth');
    expect(packets).not.toHaveTextContent('Sol → Earth');
  });

  it('lights the Sol mission the moment a connected coordinator takes the chair', () => {
    const { container } = render(<PawRoomFocusOverview focus={focus} onOpenParticipant={vi.fn()} />);

    expect(container.querySelector('.paw-room-focus-overview')).toHaveAttribute('data-coordinator', 'true');
    expect(container.querySelector('.paw-room-focus-overview__mission--dormant')).toBeNull();
    expect(container.querySelector('.paw-room-focus-overview__sol')).not.toBeNull();
    expect(within(screen.getByRole('group', { name: '协作网状图' })).queryByRole('img', { name: /^Sol，/ })).not.toBeInTheDocument();
  });

  it('drops Sol again when the only coordinator disconnects', () => {
    const dropped = {
      ...focus,
      partners: focus.partners.map((partner) => partner.collaborationRole === 'coordinator'
        ? { ...partner, state: 'disconnected' as const }
        : partner),
    };
    render(<PawRoomFocusOverview focus={dropped} onOpenParticipant={vi.fn()} />);

    expect(screen.queryByRole('img', { name: /^Sol，/ })).not.toBeInTheDocument();
  });

  it('keeps the full acceptance checklist reachable through the inspector disclosure', () => {
    const acceptance = Array.from({ length: 18 }, (_, index) => `验收项 ${index + 1}`);
    const withAcceptance = {
      ...focus,
      workItems: focus.workItems.map((item) => item.id === 'work-root'
        ? { ...item, acceptanceCriteria: acceptance }
        : item),
    };
    render(<PawRoomFocusOverview focus={withAcceptance} onOpenParticipant={vi.fn()} />);

    const summary = screen.getByText('验收条件 · 18').closest('summary')!;
    expect(summary).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(summary);
    expect(summary).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('验收项 18')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: '焦点详情' })).toHaveTextContent('可复查的整合版本');
  });
});
