import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { appendOptimisticRoomMessage, createRoomProjection, reduceRoomEvent } from '@/contracts/room-reducer';
import { roomEventFixture } from '@/test/fixtures/events';
import { clearConversationScrollMemory } from '@/features/conversation-ui';
import type { RoomSummary } from '@/features/rooms/room-types';
import type { JevSnapshot } from '@/features/semantic-workspace/jev-execution';
import { PawOsDesktopProvider } from '@/features/paw-os/surface-context';
import { parseTraceAgentHandoff } from '@/features/trace-agent/handoff';
import { PawRoomConversation } from './PawRoomWorkspace';

afterEach(() => {
  cleanup();
  clearConversationScrollMemory();
  vi.restoreAllMocks();
});

describe('PawRoomConversation', () => {
  it('scopes a partner observer to the current Root without relabelling older replies', () => {
    const { projection, room } = roomConversation();
    const previous = Object.values(projection.messagesById).find(message => message.role === 'assistant')!;
    projection.messagesById['new-reply'] = { ...previous, id: 'new-reply', turnId: 'new-root', rootId: 'new-root', text: '本轮新答复', status: 'completed' };
    projection.messageOrder.push('new-reply');
    const view = renderRoom({ participantId: 'participant-a', rootId: 'new-root', projection, room, readOnly: true });
    expect(screen.getByRole('region', { name: '行星公开对话' })).toHaveTextContent('本轮新答复');
    expect(screen.queryByText('已接入生产 reducer。')).not.toBeInTheDocument();
    view.rerender(<PawRoomConversation participantId="participant-a" projection={projection} room={room} readOnly />);
    expect(screen.getByRole('region', { name: '行星公开对话' })).toHaveTextContent('已接入生产 reducer。');
  });

  it.each(['session-a', '', 'session-from-other-room'])('binds prose file links only to the proven source Session (%s)', async source => {
    const { projection, room } = roomConversation();
    projection.messagesById['message-agent']!.sourceSessionId = source;
    projection.activitiesById['tool-a']!.sourceSessionId = source;
    projection.activitiesById['approval-a']!.sourceSessionId = source;
    projection.messagesById['message-agent']!.text = '交付文档：[结果](docs/result.md)';
    const routes: string[] = [];
    render(<PawOsDesktopProvider openRoute={route => routes.push(route)} openWindow={() => undefined}>
      <PawRoomConversation projection={projection} room={room} readOnly />
    </PawOsDesktopProvider>);
    const link = await screen.findByRole('link', { name: '打开文件 result.md' });
    await userEvent.setup().click(link);
    const params = new URL(routes[0]!, 'http://localhost').searchParams;
    expect(params.get('path')).toBe('docs/result.md');
    expect(params.get('session')).toBe(source === 'session-a' ? 'session-a' : null);
  });

  it('renders a proven JEV reclaim as a stopped handoff in the real Room conversation', async () => {
    const user = userEvent.setup();
    const { projection, room } = roomConversation();
    const message = projection.messagesById['message-agent']!;
    message.status = 'failed';
    message.text = '模型服务未能生成最终回复。请继续当前对话，或切换模型后继续。';
    message.rootId = 'root-a';
    message.dispatchId = 'dispatch-old';
    message.sourceTurnId = 'turn-old';
    message.message = { blocks: [{ type: 'error', data: { message: 'This operation was aborted' } }] } as unknown as typeof message.message;
    const activity = projection.activitiesById['tool-a']!;
    activity.status = 'failed';
    activity.summary = 'read';
    activity.payload = { sourceEventType: 'tool_finished', toolName: 'read', rootId: 'root-a',
      dispatchId: 'dispatch-old', sourceTurnId: 'turn-old', error: 'This operation was aborted' };
    const graph: JevSnapshot = {
      graphId: 'graph-a', rootId: 'root-a', version: 'v1', phase: 'execute', stopped: false,
      requirementsRevision: 1, edges: [], ready: [], running: ['task-a'], review: [], blocked: [],
      tasks: [{ id: 'task-a', state: 'running', revision: 2, ownerId: 'participant-b', parentId: '',
        objective: '完成任务', expectedOutput: '结果', acceptance: [], result: '', artifacts: [], evidence: [], acceptedTurnId: 'dispatch-new' }],
      events: [], final: null, modelCards: [], planApproval: null,
      effects: [
        { effectId: 'cancel:reclaim-a', operation: 'cancel', state: 'accepted', executionStatus: 'accepted',
          request: { graphId: 'graph-a', rootId: 'root-a', taskId: 'task-a', dispatchId: 'dispatch-old', sessionId: 'session-a', reclaimId: 'reclaim-a' },
          receipt: { state: 'accepted', receiptId: 'cancel-a', taskId: 'task-a', dispatchId: 'dispatch-old', sessionId: 'session-a', reclaimId: 'reclaim-a' } },
        { effectId: 'dispatch-old', operation: 'dispatch', state: 'accepted', executionStatus: 'drained',
          request: { graphId: 'graph-a', roomId: room.id, rootId: 'root-a', taskId: 'task-a', dispatchId: 'dispatch-old', sessionId: 'session-a', ownerId: 'participant-a', purpose: 'execute', taskRevision: 1 },
          receipt: { state: 'accepted', receiptId: 'dispatch-old-receipt', taskId: 'task-a', dispatchId: 'dispatch-old', sessionId: 'session-a', turnId: 'turn-old' } },
        { effectId: 'dispatch-new', operation: 'dispatch', state: 'accepted', executionStatus: 'running',
          request: { graphId: 'graph-a', roomId: room.id, rootId: 'root-a', taskId: 'task-a', dispatchId: 'dispatch-new', sessionId: 'session-b', ownerId: 'participant-b', purpose: 'execute', taskRevision: 2 },
          receipt: { state: 'accepted', receiptId: 'dispatch-new-receipt', taskId: 'task-a', dispatchId: 'dispatch-new', sessionId: 'session-b', turnId: 'turn-new' } },
      ],
    };
    render(<PawOsDesktopProvider openRoute={() => undefined} openWindow={() => undefined}>
      <PawRoomConversation collaborationMode="jev" graph={graph} projection={projection} readOnly room={room} />
    </PawOsDesktopProvider>);
    expect(screen.getByText('旧执行已停止，任务已交接。')).toBeVisible();
    expect(screen.queryByText('模型服务未能生成最终回复。请继续当前对话，或切换模型后继续。')).not.toBeInTheDocument();
    const tool = document.querySelector<HTMLElement>('[data-tool-block="tool:tool-a"]')!;
    expect(tool).toHaveTextContent('旧执行已停止，任务已交接');
    await user.click(within(tool).getByRole('button', { expanded: false }));
    expect(within(tool).getByText('原始停止回执')).toBeVisible();
    expect(within(tool).getByText('This operation was aborted')).toBeVisible();
  });

  it('opens the exact partner Session from the avatar/name by keyboard, including stopped history', async () => {
    const user = userEvent.setup();
    const fixture = roomConversation();
    const routes: string[] = [];
    fixture.projection.turnOrder.push('root-a');
    fixture.projection.turnsById['root-a'] = {
      id: 'root-a', rootId: 'root-a', status: 'aborted', messageIds: ['message-user', 'message-agent'],
      activityIds: ['tool-a', 'approval-a'], participantIds: ['participant-a'],
      createdAtMs: 100, updatedAtMs: 150, failure: '这轮协作已停止。',
    };
    render(<PawOsDesktopProvider openRoute={(route) => routes.push(route)} openWindow={() => undefined}>
      <PawRoomConversation projection={fixture.projection} room={fixture.room} readOnly />
    </PawOsDesktopProvider>);

    const identity = screen.getByRole('link', { name: '打开 Mars 的 Session' });
    expect(identity).toHaveAttribute('href', '#/agent?session=session-a');
    identity.focus();
    await user.keyboard('{Enter}');
    expect(routes).toEqual(['/agent?session=session-a']);
    expect(screen.getByText('这轮协作已停止。')).toBeInTheDocument();
  });

  it('does not guess a partner Session from a missing or conflicting source binding', () => {
    const { projection, room } = roomConversation();
    projection.messagesById['message-agent']!.sourceSessionId = '';
    projection.activitiesById['tool-a']!.sourceSessionId = '';
    projection.activitiesById['approval-a']!.sourceSessionId = '';
    renderRoom({ projection, room });
    expect(screen.queryByRole('link', { name: '打开 Mars 的 Session' })).not.toBeInTheDocument();

    cleanup();
    projection.messagesById['message-agent']!.sourceSessionId = 'session-from-other-room';
    renderRoom({ projection, room });
    expect(screen.queryByRole('link', { name: '打开 Mars 的 Session' })).not.toBeInTheDocument();
  });

  it('opens a dispatch only for its exact target Session and keeps an opaque ID copyable', async () => {
    const user = userEvent.setup();
    const { projection, room } = roomConversation();
    const routes: string[] = [];
    const copy = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: copy } });
    projection.activityOrder.push('dispatch-a');
    projection.activitiesById['dispatch-a'] = {
      id: 'dispatch-a', turnId: 'root-a', participantId: 'participant-a', sourceSessionId: 'session-a',
      kind: 'route_decision', status: 'completed', summary: '已分派',
      payload: { sourceEventType: 'route_decision', reason: 'jev', dispatchId: 'jev-purpose:abc123',
        rootId: 'root-a', targetParticipantId: 'participant-a', targetSessionId: 'session-a' },
      sequence: 5, createdAtMs: 140, updatedAtMs: 140,
    };
    render(<PawOsDesktopProvider openRoute={(route) => routes.push(route)} openWindow={() => undefined}>
      <PawRoomConversation projection={projection} room={room} />
    </PawOsDesktopProvider>);

    const dispatch = document.querySelector<HTMLElement>('[data-tool-block="dispatch:dispatch-a"]')!;
    await user.click(within(dispatch).getByRole('button', { expanded: false }));
    await user.click(within(dispatch).getByText('运行记录'));
    expect(within(dispatch).getByText('jev-purpose:abc123')).toBeInTheDocument();
    expect(within(dispatch).queryByRole('link', { name: /jev-purpose/ })).not.toBeInTheDocument();
    await user.click(within(dispatch).getByRole('button', { name: '复制派遣标识' }));
    expect(copy).toHaveBeenCalledWith('jev-purpose:abc123');
    await user.click(within(dispatch).getByRole('link', { name: '查看伙伴执行：Mars' }));
    expect(routes).toEqual(['/agent?session=session-a']);
  });

  it('does not route a dispatch to a different Room partner or a guessed Session', async () => {
    const user = userEvent.setup();
    const { projection, room } = roomConversation();
    projection.activityOrder.push('dispatch-bad');
    projection.activitiesById['dispatch-bad'] = {
      id: 'dispatch-bad', turnId: 'root-a', participantId: 'participant-a', sourceSessionId: 'session-a',
      kind: 'route_decision', status: 'completed', summary: '已分派',
      payload: { sourceEventType: 'route_decision', dispatchId: 'jev-purpose:bad',
        targetParticipantId: 'foreign-participant', targetSessionId: 'foreign-session' },
      sequence: 5, createdAtMs: 140, updatedAtMs: 140,
    };
    const { container } = renderRoom({ projection, room });
    const dispatch = container.querySelector<HTMLElement>('[data-tool-block="dispatch:dispatch-bad"]')!;
    await user.click(within(dispatch).getByRole('button', { expanded: false }));
    expect(within(dispatch).queryByRole('link', { name: /查看伙伴执行/ })).not.toBeInTheDocument();
    expect(within(dispatch).getByText('jev-purpose:bad')).toBeInTheDocument();
  });

  it.each([false, true])('keeps the Jev plan review between plan and execute dispatches (different partner: %s)', differentPartner => {
    const { projection, room } = roomConversation();
    /* Both route blocks intentionally share one assistant loop. The real Room
     * can emit Jev planning and the first execute dispatch before its public
     * assistant message closes, so a card-footer-only implementation puts the
     * review after execute. */
    projection.messagesById['message-agent']!.sequence = 6;
    projection.activityOrder = ['plan-route', 'plan-tool', 'execute-route'];
    projection.activitiesById['plan-route'] = {
      id: 'plan-route', turnId: 'root-a', participantId: 'participant-a', sourceSessionId: 'session-a',
      kind: 'route_decision', status: 'completed', summary: '方案已形成',
      payload: { sourceEventType: 'route_decision', routingPolicy: 'jev', purpose: 'plan',
        dispatchId: 'plan-dispatch', rootId: 'root-a', targetParticipantId: 'participant-a', targetSessionId: 'session-a' },
      sequence: 2, createdAtMs: 105, updatedAtMs: 105,
    };
    projection.activitiesById['plan-tool'] = {
      id: 'plan-tool', turnId: 'root-a', participantId: 'participant-a', sourceSessionId: 'session-a',
      kind: 'tool', status: 'completed', summary: '已整理方案依赖',
      payload: { sourceEventType: 'tool_finished', toolName: 'read', toolCallId: 'plan-tool-call' },
      sequence: 4, createdAtMs: 120, updatedAtMs: 120,
    };
    projection.activitiesById['execute-route'] = {
      id: 'execute-route', turnId: 'root-a', participantId: 'participant-a', sourceSessionId: 'session-a',
      kind: 'route_decision', status: 'completed', summary: '执行已分派',
      payload: { sourceEventType: 'route_decision', routingPolicy: 'jev', purpose: 'execute',
        dispatchId: 'execute-dispatch', rootId: 'root-a', targetParticipantId: 'participant-a', targetSessionId: 'session-a' },
      sequence: 5, createdAtMs: 135, updatedAtMs: 135,
    };
    if (differentPartner) {
      room.participants.push({ ...room.participants[0]!, id: 'participant-b', sessionId: 'session-b', ordinal: 2 });
      projection.messagesById['message-agent']!.participantId = 'participant-b';
      projection.messagesById['message-agent']!.sourceSessionId = 'session-b';
      const execute = projection.activitiesById['execute-route']!;
      execute.participantId = 'participant-b';
      execute.sourceSessionId = 'session-b';
      execute.payload = { ...execute.payload, targetParticipantId: 'participant-b', targetSessionId: 'session-b' };
    }
    const graph: JevSnapshot = {
      graphId: 'graph-plan', rootId: 'root-a', version: 'v1', phase: 'execute', stopped: false,
      requirementsRevision: 1, edges: [], ready: [], running: [], review: [], blocked: [], tasks: [],
      events: [], final: null, modelCards: [], effects: [],
      planApproval: { status: 'awaiting_approval', planHash: 'plan-hash', requirementsRevision: 1,
        lastActionClientMessageId: '', tasks: [], clarifications: [] },
    };
    const { container } = render(<PawOsDesktopProvider openRoute={() => undefined} openWindow={() => undefined}>
      <PawRoomConversation
        collaborationMode="jev"
        graph={graph}
        planReview={<div data-testid="plan-review">方案确认</div>}
        projection={projection}
        readOnly
        room={room}
      />
    </PawOsDesktopProvider>);

    const planDispatch = container.querySelector<HTMLElement>('[data-tool-block="dispatch:plan-route"]');
    const executeDispatch = container.querySelector<HTMLElement>('[data-tool-block="dispatch:execute-route"]');
    const review = screen.getByTestId('plan-review');
    expect(planDispatch).not.toBeNull();
    expect(executeDispatch).not.toBeNull();
    const planTool = container.querySelector<HTMLElement>('[data-tool-block="tool:plan-tool"]');
    expect(planTool).not.toBeNull();
    expect(planDispatch!.closest('.ccui-assistant-turn') === executeDispatch!.closest('.ccui-assistant-turn')).toBe(!differentPartner);
    expect(planDispatch!.compareDocumentPosition(review) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(planTool!.compareDocumentPosition(review) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(review.compareDocumentPosition(executeDispatch!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('opens a bound task, file, WorkDocument and Trace at their existing owners', async () => {
    const user = userEvent.setup();
    const { projection, room } = roomConversation();
    const routes: string[] = [];
    const dispatchId = 'jev-purpose:task-bound';
    const taskId = 'task-1';
    const workdoc = `workdoc_${'a'.repeat(32)}`;
    projection.activityOrder.push('dispatch-task');
    projection.activitiesById['dispatch-task'] = {
      id: 'dispatch-task', turnId: 'root-a', participantId: 'participant-a', sourceSessionId: 'session-a',
      kind: 'route_decision', status: 'completed', summary: '任务已分派',
      payload: { sourceEventType: 'route_decision', dispatchId, rootId: 'root-a',
        targetParticipantId: 'participant-a', targetSessionId: 'session-a', subjectTaskId: taskId },
      sequence: 5, createdAtMs: 140, updatedAtMs: 140,
    };
    const graph: JevSnapshot = {
      graphId: 'graph-a', rootId: 'root-a', version: 'v1', phase: 'execute', stopped: true,
      requirementsRevision: 1, edges: [], ready: [], running: [], review: [], blocked: [],
      tasks: [{ id: taskId, state: 'done', revision: 2, ownerId: 'participant-a', parentId: '',
        objective: '核对真实源码', expectedOutput: '提交结果', acceptance: [], result: '已核对',
        artifacts: ['src/result.md'], evidence: [`workdoc:${workdoc}@2`, 'trace:turn:runtime-a', 'jev-purpose:opaque'], acceptedTurnId: dispatchId }],
      effects: [{ effectId: dispatchId, operation: 'dispatch', state: 'accepted', executionStatus: 'completed',
        request: { graphId: 'graph-a', roomId: room.id, rootId: 'root-a', dispatchId, ownerId: 'participant-a',
          sessionId: 'session-a', taskId, taskRevision: 2 }, receipt: {} }],
      events: [], final: null, modelCards: [], planApproval: null,
    };
    render(<PawOsDesktopProvider openRoute={(route) => routes.push(route)} openWindow={() => undefined}>
      <PawRoomConversation graph={graph} projection={projection} readOnly room={room} />
    </PawOsDesktopProvider>);

    const dispatch = document.querySelector<HTMLElement>('[data-tool-block="dispatch:dispatch-task"]')!;
    await user.click(within(dispatch).getByRole('button', { expanded: false }));
    await user.click(within(dispatch).getByText('查看任务详情'));
    expect(within(dispatch).getByText('核对真实源码')).toBeVisible();
    const file = within(dispatch).getByRole('link', { name: '打开文件：src/result.md' });
    expect(file).toHaveAttribute('href', '#/files?session=session-a&path=src%2Fresult.md');
    await user.click(file);
    await user.click(within(dispatch).getByRole('link', { name: `打开工作文档：workdoc:${workdoc}@2` }));
    await user.click(within(dispatch).getByRole('link', { name: '打开 Trace：trace:turn:runtime-a' }));
    expect(routes).toEqual([
      '/files?session=session-a&path=src%2Fresult.md',
      `/work-documents?document=${workdoc}`,
      '/observability?traceId=trace%3Aturn%3Aruntime-a',
    ]);
    expect(within(dispatch).queryByRole('link', { name: /jev-purpose:opaque/ })).not.toBeInTheDocument();
    expect(within(dispatch).getByText('jev-purpose:opaque')).toBeInTheDocument();
  });

  it('shows the fixed worker task when a different partner receives its verification dispatch', async () => {
    const user = userEvent.setup();
    const { projection, room } = roomConversation();
    const taskId = 'task-to-verify';
    const dispatchId = 'jev-purpose:verify-bound';
    projection.activityOrder.push('verify-bound');
    projection.activitiesById['verify-bound'] = {
      id: 'verify-bound', turnId: 'root-a', participantId: 'participant-a', sourceSessionId: 'session-a',
      kind: 'route_decision', status: 'completed', summary: '已确定复核伙伴',
      payload: { sourceEventType: 'route_decision', routingPolicy: 'jev', purpose: 'verify', dispatchId,
        rootId: 'root-a', targetParticipantId: 'participant-a', targetSessionId: 'session-a', subjectTaskId: taskId },
      sequence: 5, createdAtMs: 140, updatedAtMs: 140,
    };
    const graph: JevSnapshot = {
      graphId: 'graph-verify', rootId: 'root-a', version: 'v1', phase: 'execute', stopped: false,
      requirementsRevision: 1, edges: [], ready: [], running: [], review: [taskId], blocked: [],
      tasks: [{ id: taskId, state: 'review', revision: 0, ownerId: 'participant-b', parentId: '',
        objective: '核对点击文件是否真的打开', expectedOutput: '实际点击证据', acceptance: [], result: '',
        artifacts: [], evidence: [], acceptedTurnId: 'jev-dispatch:worker' }],
      effects: [{ effectId: dispatchId, operation: 'dispatch', state: 'accepted', executionStatus: 'completed',
        request: { graphId: 'graph-verify', roomId: room.id, rootId: 'root-a', dispatchId,
          ownerId: 'participant-a', sessionId: 'session-a', taskId, taskRevision: 0, purpose: 'verify' }, receipt: {} }],
      events: [], final: null, modelCards: [], planApproval: null,
    };
    renderRoom({ graph, projection, room });
    const dispatch = document.querySelector<HTMLElement>('[data-tool-block="dispatch:verify-bound"]')!;
    await user.click(within(dispatch).getByRole('button', { expanded: false }));
    await user.click(within(dispatch).getByText('查看任务详情'));
    expect(within(dispatch).getByText('核对点击文件是否真的打开')).toBeVisible();
    expect(within(dispatch).getByText(/当前任务状态：/)).toHaveTextContent('复核');
    expect(within(dispatch).getByText('预期交付：实际点击证据')).toBeVisible();
  });

  it('keeps escaped managed text behind a readable summary in a Room tool record', async () => {
    const user = userEvent.setup();
    const { projection, room } = roomConversation();
    const ref = 'media://media_lBpgH2mPPde2OLSM1EIcq4gJ';
    projection.activitiesById['tool-a'] = { ...projection.activitiesById['tool-a']!,
      status: 'completed', summary: `已读取受管资源 ${ref}`,
      payload: { sourceEventType: 'tool_finished', toolName: 'read', dispatchId: 'jev-purpose:review',
        result: { outputPreview: `[resourceRef: ${ref}] [resourceRevision: ${'a'.repeat(64)}] `
          + String.raw`test \u001b[33m 2913\u001b[2mms\u001b[39m\n\u001b[33m✓\u001b[39m\nnext\nlast`, outputTruncated: true } },
    };
    projection.activityOrder.splice(1, 0, 'tool-b');
    projection.activitiesById['tool-b'] = { ...projection.activitiesById['tool-a']!,
      id: 'tool-b', sequence: 2.5, createdAtMs: 115, updatedAtMs: 115,
      summary: '已读取另一文件', payload: { sourceEventType: 'tool_finished', toolName: 'read' },
    };
    renderRoom({ collaborationMode: 'jev', projection, room });
    await user.click(screen.getByRole('button', { name: /工具记录 · 2 项/ }));
    const dialog = screen.getByRole('dialog', { name: /工具记录 · 2 项/ });
    const tool = within(dialog).getByText('读取受管资源').closest<HTMLElement>('[data-tool-block="tool:tool-a"]')!;
    expect(tool).toHaveTextContent('读取受管资源');
    expect(tool).not.toHaveTextContent('\\u001b');
    await user.click(within(tool).getByRole('button', { expanded: false }));
    expect(within(tool).getByText(/已读取受管资源的一个片段/)).toBeVisible();
    await user.click(within(tool).getByText('查看整理后的片段'));
    expect(within(tool).getByText(/2913ms/)).toBeVisible();
    expect(within(tool).queryByText(/\\u001b/)).not.toBeInTheDocument();
  });

  it('does not expose task evidence when a dispatch belongs to another Room or Session', async () => {
    const user = userEvent.setup();
    const { projection, room } = roomConversation();
    projection.activityOrder.push('dispatch-cross-room');
    projection.activitiesById['dispatch-cross-room'] = {
      id: 'dispatch-cross-room', turnId: 'root-a', participantId: 'participant-a', sourceSessionId: 'session-a',
      kind: 'route_decision', status: 'completed', summary: '已分派',
      payload: { sourceEventType: 'route_decision', dispatchId: 'dispatch-cross-room',
        targetParticipantId: 'participant-a', targetSessionId: 'session-a' },
      sequence: 5, createdAtMs: 140, updatedAtMs: 140,
    };
    const graph: JevSnapshot = {
      graphId: 'graph-b', rootId: 'root-a', version: 'v1', phase: 'execute', stopped: false,
      requirementsRevision: 1, edges: [], ready: [], running: [], review: [], blocked: [],
      tasks: [{ id: 'task-foreign', state: 'done', revision: 1, ownerId: 'participant-a', parentId: '',
        objective: '另一 Room 的任务', expectedOutput: '', acceptance: [], result: '', artifacts: ['/private/other.txt'], evidence: [], acceptedTurnId: 'dispatch-cross-room' }],
      effects: [{ effectId: 'dispatch-cross-room', operation: 'dispatch', state: 'accepted', executionStatus: 'completed',
        request: { graphId: 'graph-b', roomId: 'room-foreign', rootId: 'root-a', dispatchId: 'dispatch-cross-room',
          ownerId: 'participant-a', sessionId: 'session-a', taskId: 'task-foreign', taskRevision: 1 }, receipt: {} }],
      events: [], final: null, modelCards: [], planApproval: null,
    };
    const { container } = renderRoom({ graph, projection, room });
    const dispatch = container.querySelector<HTMLElement>('[data-tool-block="dispatch:dispatch-cross-room"]')!;
    await user.click(within(dispatch).getByRole('button', { expanded: false }));
    expect(within(dispatch).queryByText('查看任务详情')).not.toBeInTheDocument();
    expect(within(dispatch).queryByRole('link', { name: /打开文件/ })).not.toBeInTheDocument();

    cleanup();
    renderRoom({ graph, projection, room: { ...room, id: 'room-foreign' } });
    expect(screen.queryByRole('link', { name: '打开 Mars 的 Session' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /查看伙伴执行/ })).not.toBeInTheDocument();
  });

  it('opens a long real Room on a bounded tail window before layout measurement', () => {
    const fixture = longRoomConversation(500);
    const startedAt = performance.now();
    const { container } = renderRoom(fixture);
    const elapsedMs = performance.now() - startedAt;
    const mountedRows = container.querySelectorAll('.ccui-virtual-row').length;

    expect(
      mountedRows,
      `mounted ${mountedRows} transcript rows during first paint in ${elapsedMs.toFixed(2)}ms`,
    ).toBeLessThan(60);
    expect(screen.getByText('最终消息 500')).toBeVisible();
    expect(elapsedMs).toBeLessThan(250);
  });

  it('reads the public chronology on the shared conversation surface', () => {
    const { container } = renderRoom();

    const surface = screen.getByRole('region', { name: 'Room 公开对话' });
    expect(surface).toHaveTextContent('请完成主线迁移');
    expect(surface).toHaveTextContent('已接入生产 reducer。');
    // Messages and activities share one planet identity; the real display
    // name stays reachable as the collaboration role beside it.
    expect(surface).toHaveTextContent('Mars');
    expect(surface).not.toHaveTextContent('Root');
    // One real Runtime loop is one card, and the legacy per-event DOM is gone.
    expect(container.querySelectorAll('.ccui-assistant-turn')).toHaveLength(1);
    expect(container.querySelectorAll('.ccui-user-turn')).toHaveLength(1);
    expect(container.querySelector('.room-turn, .room-agent-lane, .paw-room-chronology')).toBeNull();
  });

  it('renders an exact failed Room final as a compact report with the full source behind disclosure', async () => {
    const user = userEvent.setup();
    const fixture = roomConversation();
    const report = [
      '# 迁移结果',
      '',
      '失败：验收检查仍有一项未通过。',
      '',
      '运行入口：trace:report-final。',
      '',
      '细节证据：完整运行原文仍然保留，展开后可以继续核对。',
    ].join('\n');
    fixture.room.moderatorParticipantId = 'participant-a';
    fixture.projection.moderatorParticipantId = 'participant-a';
    fixture.projection.messagesById['message-agent'] = {
      ...fixture.projection.messagesById['message-agent']!,
      participantId: 'participant-a', rootId: 'root-a', postKind: 'blocked', text: report,
    };
    fixture.projection.turnOrder.push('root-a');
    fixture.projection.turnsById['root-a'] = {
      id: 'root-a', rootId: 'root-a', status: 'completed', messageIds: ['message-user', 'message-agent'],
      activityIds: ['tool-a', 'approval-a'], participantIds: ['participant-a'],
      createdAtMs: 100, updatedAtMs: 150, failure: '验收检查失败。',
    };
    const graph: JevSnapshot = {
      graphId: 'graph-final', roomId: fixture.room.id, rootId: 'root-a', version: 'final', phase: 'final', stopped: false,
      requirementsRevision: 1, tasks: [], edges: [], ready: [], running: [], review: [], blocked: [], effects: [], events: [],
      final: { content: report, status: 'failed', evidence: ['trace:report-final'] }, modelCards: [], planApproval: null,
    };

    const { container } = renderRoom({ collaborationMode: 'jev', graph, projection: fixture.projection, room: fixture.room, readOnly: true });
    const final = screen.getByRole('region', { name: 'Room 最终汇报' });
    expect(final).toHaveAttribute('data-report-layout', 'wide');
    expect(final).toHaveAttribute('data-state', 'failed');
    expect(final).toHaveTextContent('需要处理');
    expect(final).toHaveTextContent('失败：验收检查仍有一项未通过。');
    expect(within(final).queryByText('细节证据：完整运行原文仍然保留，展开后可以继续核对。')).not.toBeInTheDocument();
    expect(container.querySelector('.ccui-assistant-turn:has(.paw-room-conversation__final-report)')).not.toBeNull();

    await user.click(within(final).getByText('查看完整汇报与运行证据'));
    expect(within(final).getByText('细节证据：完整运行原文仍然保留，展开后可以继续核对。')).toBeVisible();
    expect(within(final).getByRole('button', { name: '复制完整汇报' })).toBeVisible();
    expect(within(final).getByRole('region', { name: '报告证据' })).toHaveTextContent('trace:report-final');
  });

  it('does not classify ordinary prose or a result from another Root as the final report', () => {
    const fixture = roomConversation();
    fixture.projection.messagesById['message-agent'] = {
      ...fixture.projection.messagesById['message-agent']!,
      text: '# 迁移结果\n\n这只是过程中的说明。',
    };
    renderRoom({ projection: fixture.projection, room: fixture.room, readOnly: true });
    expect(screen.queryByRole('region', { name: 'Room 最终汇报' })).not.toBeInTheDocument();

    cleanup();
    clearConversationScrollMemory();
    fixture.projection.messagesById['message-agent'] = {
      ...fixture.projection.messagesById['message-agent']!,
      rootId: 'other-root', postKind: 'blocked', text: '另一轮的结果。',
    };
    const graph: JevSnapshot = {
      graphId: 'graph-final', roomId: fixture.room.id, rootId: 'root-a', version: 'final', phase: 'final', stopped: false,
      requirementsRevision: 1, tasks: [], edges: [], ready: [], running: [], review: [], blocked: [], effects: [], events: [],
      final: { content: '另一轮的结果。', status: 'completed', evidence: [] }, modelCards: [], planApproval: null,
    };
    renderRoom({ collaborationMode: 'jev', graph, projection: fixture.projection, room: fixture.room, readOnly: true });
    expect(screen.queryByRole('region', { name: 'Room 最终汇报' })).not.toBeInTheDocument();
    expect(screen.getByText('另一轮的结果。')).toBeVisible();
  });

  it('keeps the previous failed report wide after a new Jev Root starts using its exact terminal publication', () => {
    const fixture = roomConversation();
    fixture.room.moderatorParticipantId = 'participant-a';
    fixture.projection.moderatorParticipantId = 'participant-a';
    fixture.projection.messagesById['message-agent'] = {
      ...fixture.projection.messagesById['message-agent']!,
      rootId: 'root-a', postKind: 'blocked', text: '# 上轮报告\n\n集成仍未通过。',
    };
    const terminal = {
      ...roomEventFixture(1, 'turn_failed', { rootId: 'root-a', finalizationId: 'message-agent', status: 'failed' }),
      roomId: fixture.room.id, turnId: 'root-a', participantId: null, sourceSessionId: '',
    };
    const projection = reduceRoomEvent(fixture.projection, terminal).state;
    const graph: JevSnapshot = {
      graphId: 'graph-new', roomId: fixture.room.id, rootId: 'root-new', version: 'new', phase: 'plan', stopped: false,
      requirementsRevision: 1, tasks: [], edges: [], ready: [], running: [], review: [], blocked: [], effects: [], events: [],
      final: null, modelCards: [], planApproval: null,
    };
    renderRoom({ collaborationMode: 'jev', graph, projection, room: fixture.room, readOnly: true });
    const report = screen.getByRole('region', { name: 'Room 最终汇报' });
    expect(report).toHaveAttribute('data-report-layout', 'wide');
    expect(report).toHaveAttribute('data-state', 'failed');
    expect(report).toHaveTextContent('集成仍未通过。');
  });

  it('does not promote a historical blocked progress post whose terminal names a different publication', () => {
    const fixture = roomConversation();
    fixture.room.moderatorParticipantId = 'participant-a';
    fixture.projection.messagesById['message-agent'] = {
      ...fixture.projection.messagesById['message-agent']!, rootId: 'root-a', postKind: 'blocked', text: '过程受阻，尚未汇报。',
    };
    const terminal = {
      ...roomEventFixture(1, 'turn_failed', { rootId: 'root-a', finalizationId: 'different-report' }),
      roomId: fixture.room.id, turnId: 'root-a', participantId: null, sourceSessionId: '',
    };
    const projection = reduceRoomEvent(fixture.projection, terminal).state;
    renderRoom({ collaborationMode: 'jev', projection, room: fixture.room, readOnly: true });
    expect(screen.queryByRole('region', { name: 'Room 最终汇报' })).not.toBeInTheDocument();
    expect(screen.getByText('过程受阻，尚未汇报。')).toBeVisible();
  });

  it('names a tool by its reader label and keeps the raw call one click away', async () => {
    const user = userEvent.setup();
    const { container } = renderRoom();

    const tool = container.querySelector<HTMLElement>('[data-tool-block="tool:tool-a"]')!;
    // Raw Runtime tool ids map to reader-facing labels (`read` → 读取文件).
    expect(tool).toHaveTextContent('读取文件');
    expect(tool).toHaveTextContent('正在执行');
    // The raw argument blob never leaks into the collapsed reading line.
    expect(screen.queryByText(/Volumes\/private\/workspace/)).not.toBeInTheDocument();

    await user.click(within(tool).getByRole('button', { expanded: false }));
    expect(tool).toHaveTextContent('/Volumes/private/workspace/PawWindowLayer.tsx');
  });

  it('keeps a pending approval decidable without expanding anything', async () => {
    const user = userEvent.setup();
    const decide = vi.fn().mockResolvedValue(undefined);
    renderRoom({ onApprovalDecision: decide });

    await user.click(screen.getByRole('button', { name: '批准并继续' }));
    await waitFor(() => expect(decide).toHaveBeenCalledWith('approval-a', 'approved', 'a'.repeat(64)));
  });

  it('surfaces an approval failure next to the decision it belongs to', async () => {
    const user = userEvent.setup();
    const decide = vi.fn().mockRejectedValue(new Error('approval store unreachable'));
    const routes: string[] = [];
    const fixture = roomConversation();
    render(
      <PawOsDesktopProvider openRoute={(route) => routes.push(route)} openWindow={() => undefined}>
        <PawRoomConversation
          onApprovalDecision={decide}
          onRetryTurn={() => undefined}
          projection={fixture.projection}
          retryingTurn={false}
          room={fixture.room}
        />
      </PawOsDesktopProvider>,
    );

    await user.click(screen.getByRole('button', { name: '拒绝' }));
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
    expect(screen.getByRole('button', { name: '拒绝' })).toBeEnabled();
    await user.click(screen.getByRole('button', { name: '交给 Trace Agent' }));
    const handoff = parseTraceAgentHandoff(routes[0]?.split('?', 2)[1] ?? '');
    expect(handoff).toMatchObject({
      kind: 'room',
      entityId: 'approval-a',
      roomId: 'room-live',
      sessionId: 'session-a',
      sourceRoute: '/rooms?room=room-live',
      refs: {
        approvalId: 'approval-a',
        participantId: 'participant-a',
        turnId: 'root-a',
      },
    });
  });

  it('expands an edit receipt into the shared structured diff reader, not a flat text wall', async () => {
    const user = userEvent.setup();
    const { projection, room } = roomConversation();
    const diff = [
      '--- a/src/example.ts',
      '+++ b/src/example.ts',
      '@@ -1,3 +1,4 @@',
      ' export function greet() {',
      "-  return 'hi';",
      "+  const name = 'PAW';",
      '+  return `hi ${name}`;',
      ' }',
    ].join('\n');
    projection.activityOrder.push('edit-a');
    projection.activitiesById['edit-a'] = {
      id: 'edit-a', turnId: 'root-a', participantId: 'participant-a', sourceSessionId: 'session-a',
      kind: 'tool', status: 'completed', summary: 'edit',
      payload: {
        sourceEventType: 'tool_finished',
        toolCallId: 'call-edit-a',
        toolName: 'edit',
        arguments: { path: 'src/example.ts' },
        result: { details: { ok: true, diff } },
      },
      sequence: 6, createdAtMs: 150, updatedAtMs: 150,
    };

    const { container } = renderRoom({ projection, room });

    // The reader line derives from real evidence, never the machine tool id.
    const card = container.querySelector<HTMLElement>('[data-tool-block="tool:edit-a"]')!;
    expect(card).toHaveTextContent('编辑文件');
    await user.click(within(card).getByRole('button', { expanded: false }));

    const output = within(card).getByLabelText('工具变更差异');
    expect(output.querySelector(':scope > pre')).toBeNull();
    const preview = output.querySelector<HTMLElement>('.agent-diff-preview')!;
    expect(preview).toHaveTextContent('src/example.ts');
    expect(preview.querySelector('tr[data-kind="add"]')).toHaveTextContent("const name = 'PAW';");
    expect(preview.querySelector('tr[data-kind="remove"]')).toHaveTextContent("return 'hi';");
  });

  it('opens a Room background Bash only from an explicit activity action', async () => {
    const user = userEvent.setup();
    const { projection, room } = roomConversation();
    const openProcess = vi.fn();
    projection.activityOrder.push('background-a');
    projection.activitiesById['background-a'] = {
      id: 'background-a', turnId: 'root-a', participantId: 'participant-a', sourceSessionId: 'session-a',
      kind: 'participant_activity', status: 'completed', summary: '后台构建已启动',
      payload: {
        sourceEventType: 'tool_finished',
        toolCallId: 'call-background-a',
        toolName: 'workspace_job',
        arguments: { command: 'pnpm build', cwd: '/workspace' },
        runId: 'bg_0123456789abcdef0123456789abcdef',
      },
      sequence: 5, createdAtMs: 140, updatedAtMs: 140,
    };

    renderRoom({ onOpenProcessActivity: openProcess, projection, room });

    expect(openProcess).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: '查看后台 Bash' }));
    expect(openProcess).toHaveBeenCalledTimes(1);
    expect(openProcess).toHaveBeenCalledWith(projection.activitiesById['background-a']);
  });

  it('offers retry only for the current failed request', async () => {
    const user = userEvent.setup();
    const { projection, room } = roomConversation();
    const retry = vi.fn();
    projection.turnOrder.push('root-a');
    projection.turnsById['root-a'] = {
      id: 'root-a', rootId: 'root-a', status: 'failed',
      messageIds: ['message-user'], activityIds: [], participantIds: [], createdAtMs: 100, updatedAtMs: 145,
      failure: '503 upstream request failed',
    };

    renderRoom({ onRetryTurn: retry, projection, room });

    expect(screen.getByText('503 upstream request failed')).toBeVisible();
    await user.click(screen.getByRole('button', { name: '继续' }));
    expect(retry).toHaveBeenCalledWith('请完成主线迁移', 'root-a');
  });

  it('keeps an old failure as history without reviving retry after newer user input', () => {
    const { projection, room } = roomConversation();
    projection.turnOrder.push('root-a', 'root-b');
    projection.turnsById['root-a'] = {
      id: 'root-a', rootId: 'root-a', status: 'failed',
      messageIds: ['message-user'], activityIds: [], participantIds: [], createdAtMs: 100, updatedAtMs: 145,
      failure: '503 upstream request failed',
    };
    projection.turnsById['root-b'] = {
      id: 'root-b', rootId: 'root-b', status: 'running',
      messageIds: ['message-user-new'], activityIds: [], participantIds: [], createdAtMs: 160, updatedAtMs: 160,
    };
    projection.messageOrder.push('message-user-new');
    projection.messagesById['message-user-new'] = {
      id: 'message-user-new', roomId: room.id, turnId: 'root-b', participantId: null,
      sourceSessionId: '', role: 'user', status: 'completed', text: '改用新的方案继续',
      projectionKind: 'post', sequence: 5, createdAtMs: 160,
    };

    renderRoom({ projection, room });

    expect(screen.getByText('503 upstream request failed')).toBeVisible();
    expect(screen.queryByRole('button', { name: '继续' })).not.toBeInTheDocument();
  });

  it('tells the writer a steer is not delivered yet, then clears the receipt', () => {
    const { projection, room } = roomConversation();
    projection.turnOrder.push('root-a');
    projection.turnsById['root-a'] = {
      id: 'root-a', rootId: 'root-a', status: 'running',
      messageIds: ['message-user', 'message-agent'], activityIds: ['tool-a', 'approval-a'],
      participantIds: ['participant-a'], createdAtMs: 100, updatedAtMs: 130,
    };

    const pending = renderRoom({
      projection: appendOptimisticRoomMessage(projection, {
        clientMessageId: 'client-steer', text: '改成先做迁移脚本', nowMs: 200,
      }),
      room,
    });
    expect(screen.getByText('尚未送达伙伴')).toBeVisible();
    // Nothing is offered that the Room cannot honour: no Runtime contract can
    // recall a published Room post, so the receipt stays read-only.
    expect(pending.container.querySelector('.ccui-steer-receipt button')).toBeNull();

    cleanup();
    clearConversationScrollMemory();
    projection.messageOrder.push('message-steer');
    projection.turnsById['root-a']!.messageIds.push('message-steer');
    projection.messagesById['message-steer'] = {
      id: 'message-steer', roomId: room.id, turnId: 'root-a', participantId: null,
      sourceSessionId: '', role: 'user', status: 'completed', text: '改成先做迁移脚本',
      projectionKind: 'post', sequence: 5, createdAtMs: 200,
    };

    renderRoom({ projection, room });
    expect(screen.getByText('已送达伙伴')).toBeVisible();
    expect(screen.queryByText('尚未送达伙伴')).not.toBeInTheDocument();
  });

  it('scopes a partner satellite to that partner and drops the Room-wide chrome', () => {
    const { projection, room } = roomConversation();
    projection.activityOrder.push('tool-b');
    projection.activitiesById['tool-b'] = {
      id: 'tool-b', turnId: 'root-a', participantId: 'participant-b', sourceSessionId: 'session-b',
      kind: 'tool', status: 'completed', summary: '写入完成',
      payload: { sourceEventType: 'tool_finished', toolName: 'write' },
      sequence: 6, createdAtMs: 150, updatedAtMs: 150,
    };

    const { container } = renderRoom({ participantId: 'participant-a', projection, room });

    expect(screen.getByRole('region', { name: '行星公开对话' })).toBeInTheDocument();
    expect(container.querySelector('[data-tool-block="tool:tool-a"]')).not.toBeNull();
    expect(container.querySelector('[data-tool-block="tool:tool-b"]')).toBeNull();
    expect(container.querySelector('.ccui-conversation-surface')).toHaveAttribute('data-density', 'compact');
  });

  it('keeps a planet observation surface read-only while retaining its public timeline', () => {
    const { projection, room } = roomConversation();

    renderRoom({ participantId: 'participant-a', projection, readOnly: true, room });

    const surface = screen.getByRole('region', { name: '行星公开对话' });
    expect(surface).toHaveTextContent('已接入生产 reducer。');
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '批准并继续' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '拒绝' })).not.toBeInTheDocument();
  });
});

function renderRoom(overrides: Partial<Parameters<typeof PawRoomConversation>[0]> = {}) {
  const fixture = roomConversation();
  return render(<PawRoomConversation
    onApprovalDecision={async () => undefined}
    onRetryTurn={() => undefined}
    projection={fixture.projection}
    retryingTurn={false}
    room={fixture.room}
    {...overrides}
  />);
}

function roomConversation() {
  const room: RoomSummary = {
    id: 'room-live', title: 'PAWOS 完整迁移', status: 'active',
    description: '将候选结构接到真实 Room reducer。', routingPolicy: 'natural',
    moderatorParticipantId: 'participant-root', updatedAtMs: 140,
    participants: [{
      id: 'participant-a', sessionId: 'session-a', roleId: 'implementer', roleVersion: '1',
      displayName: '实现伙伴', collaborationRole: 'implementer', status: 'active', ordinal: 1,
    }],
    workItems: [],
  };
  const projection = createRoomProjection(room.id);
  projection.messageOrder.push('message-user', 'message-agent');
  projection.messagesById['message-user'] = {
    id: 'message-user', roomId: room.id, turnId: 'root-a', participantId: null,
    sourceSessionId: '', role: 'user', status: 'completed', text: '请完成主线迁移',
    projectionKind: 'post', sequence: 1, createdAtMs: 100,
  };
  projection.messagesById['message-agent'] = {
    id: 'message-agent', roomId: room.id, turnId: 'root-a', participantId: 'participant-a',
    sourceSessionId: 'session-a', role: 'assistant', status: 'completed', text: '已接入生产 reducer。',
    projectionKind: 'post', sequence: 4, createdAtMs: 130,
  };
  projection.activityOrder.push('tool-a', 'approval-a');
  projection.activitiesById['tool-a'] = {
    id: 'tool-a', turnId: 'root-a', participantId: 'participant-a', sourceSessionId: 'session-a',
    kind: 'tool', status: 'running',
    summary: '```json\n{"path":"/Volumes/private/workspace/PawWindowLayer.tsx"}\n```',
    payload: { sourceEventType: 'tool_started', toolName: 'read' },
    sequence: 2, createdAtMs: 110, updatedAtMs: 110,
  };
  projection.activitiesById['approval-a'] = {
    id: 'approval-a', turnId: 'root-a', participantId: 'participant-a', sourceSessionId: 'session-a',
    kind: 'approval_required', status: 'waiting', summary: '批准受控迁移操作',
    payload: { sourceEventType: 'approval_required', approvalId: 'approval-a', payloadSha256: 'a'.repeat(64) },
    sequence: 3, createdAtMs: 120, updatedAtMs: 120,
  };
  return { projection, room };
}

function longRoomConversation(turnCount: number) {
  const fixture = roomConversation();
  const projection = createRoomProjection(fixture.room.id);
  for (let index = 1; index <= turnCount; index += 1) {
    const turnId = `root-${index}`;
    const userId = `message-user-${index}`;
    const assistantId = `message-assistant-${index}`;
    projection.messageOrder.push(userId, assistantId);
    projection.messagesById[userId] = {
      id: userId, roomId: fixture.room.id, turnId, participantId: null,
      sourceSessionId: '', role: 'user', status: 'completed',
      text: `真实任务 ${index}`, projectionKind: 'post',
      sequence: index * 2 - 1, createdAtMs: index * 20,
    };
    projection.messagesById[assistantId] = {
      id: assistantId, roomId: fixture.room.id, turnId,
      participantId: 'participant-a', sourceSessionId: 'session-a',
      role: 'assistant', status: 'completed', text: `最终消息 ${index}`,
      projectionKind: 'post', sequence: index * 2, createdAtMs: index * 20 + 10,
      completedAtMs: index * 20 + 10,
    };
    projection.turnOrder.push(turnId);
    projection.turnsById[turnId] = {
      id: turnId, rootId: turnId, status: 'completed',
      messageIds: [userId, assistantId], activityIds: [],
      participantIds: ['participant-a'], terminalParticipantIds: ['participant-a'],
      createdAtMs: index * 20, updatedAtMs: index * 20 + 10,
    };
  }
  return { projection, room: fixture.room };
}
