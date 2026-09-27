import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { createPreviewTransport } from '@/app/preview-control-transport';
import { previewRoomSnapshot } from '@/app/preview-room-data';
import { TooltipProvider } from '@/components/primitives';
import { PawOsDesktopProvider } from '@/features/paw-os/surface-context';
import type { ControlRequest } from '@/platform/transport';
import type { RoomSummary } from '@/features/rooms/room-types';
import { useRoomLiveStore } from '@/features/rooms/state/live-store';
import { createJevWork } from '@/features/semantic-workspace/jev-execution';
import { PawRoomWorkspace } from './PawRoomWorkspace';

afterEach(() => { cleanup(); useRoomLiveStore.getState().reset(); });

describe('Jev room-backed conversation flow', () => {
  it('opens a delivered file in Files with its producing Session without sending work or losing the draft', async () => {
    const user = userEvent.setup();
    const room = previewRoomSnapshot('jev-file-window').room as unknown as RoomSummary;
    const transport = createPreviewTransport(); const original = transport.request.bind(transport);
    const requests: ControlRequest[] = [];
    transport.request = async <Response,>(request: ControlRequest): Promise<Response> => {
      requests.push(request);
      if (request.pathId === 'agent.room.get') return { ok: true, room } as Response;
      if (request.pathId === 'agent.room.snapshot') return previewRoomSnapshot(room.id) as Response;
      if (request.pathId === 'agent.jev.get') return (request.query?.graphId
        ? { ok: true, mode: 'jev', graphId: 'files-graph', rootId: 'files-root', snapshotVersion: 'one', phase: 'execute',
          tasks: [{ id: 'report', parent_id: 'goal', revision: 1, state: 'done', owner_id: room.participants[0].id, accepted_turn_id: 'report-worker', artifacts: ['docs/验收报告.md sha256:' + 'a'.repeat(64)] }],
          effects: [{ effectId: 'report-worker', operation: 'dispatch', state: 'accepted', executionStatus: 'completed', request: { taskId: 'report', taskRevision: 1, purpose: 'execute', sessionId: 'agent:original-file-owner' } }],
        } : { ok: true, mode: 'jev', items: [{ graph_id: 'files-graph', room_id: room.id, phase: 'execute' }] }) as Response;
      return original<Response>(request);
    };
    const openRoute = vi.fn();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><ControlTransportProvider transport={transport}><TooltipProvider><PawOsDesktopProvider openWindow={vi.fn()} openRoute={openRoute}>
      <PawRoomWorkspace interfaceMode="jev" personas={[]} record={room} recordId={room.id} onRoomUpdated={vi.fn()} />
    </PawOsDesktopProvider></TooltipProvider></ControlTransportProvider></QueryClientProvider>);
    const input = await screen.findByRole('textbox', { name: '协作消息' });
    await user.type(input, '还要核对报告');
    await user.click(await screen.findByRole('button', { name: '打开文件 验收报告.md' }));
    expect(openRoute).toHaveBeenCalledWith(`/files?session=agent%3Aoriginal-file-owner&path=${encodeURIComponent('docs/验收报告.md')}`);
    expect(screen.getByRole('textbox', { name: '协作消息' })).toBe(input);
    expect(input).toHaveValue('还要核对报告');
    expect(requests.filter(request => ['agent.jev.command', 'agent.room.message', 'agent.session.workspace.read'].includes(request.pathId))).toEqual([]);
  });
  it('loads partner capability settings on demand and preserves the central draft', async () => {
    const user = userEvent.setup();
    const room = previewRoomSnapshot('jev-settings-popup').room as unknown as RoomSummary;
    const transport = createPreviewTransport(); const original = transport.request.bind(transport);
    const reads: string[] = [];
    transport.request = async <Response,>(request: ControlRequest): Promise<Response> => {
      reads.push(request.pathId);
      if (request.pathId === 'agent.room.get') return { ok: true, room } as Response;
      if (request.pathId === 'agent.room.snapshot') return previewRoomSnapshot(room.id) as Response;
      if (request.pathId === 'agent.jev.get') return { ok: true, mode: 'jev', items: [] } as Response;
      return original<Response>(request);
    };
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><ControlTransportProvider transport={transport}><TooltipProvider><PawRoomWorkspace interfaceMode="jev" personas={[]} record={room} recordId={room.id} onRoomUpdated={vi.fn()} /></TooltipProvider></ControlTransportProvider></QueryClientProvider>);
    const input = await screen.findByRole('textbox', { name: '协作消息' });
    await user.type(input, '保留草稿');
    expect(reads).not.toContain('agent.session.models');
    expect(reads).not.toContain('agent.tools.list');
    const opener = screen.getByRole('button', { name: '伙伴工具与记忆' });
    await user.click(opener);
    const dialog = screen.getByRole('dialog', { name: '伙伴设置' });
    expect(within(dialog).getByLabelText('选择要设置记忆和插件的伙伴')).toBeVisible();
    await waitFor(() => expect(reads).toContain('agent.session.models'));
    expect(reads).toContain('agent.tools.list');
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(input).toHaveValue('保留草稿');
    expect(opener).toHaveFocus();
    expect(reads).not.toContain('agent.session.model.select');
  });
  it('renders the canonical Runtime final in the central conversation after planner drain', async () => {
    const base = previewRoomSnapshot('jev-final-visible');
    const room = base.room as unknown as RoomSummary;
    const rootId = 'jev-root:visible'; const finalId = 'jev-final:graph';
    const actor = room.participants[0];
    const events = [
      { eventType: 'user_message', participantId: null, sourceSessionId: '', payload: { mode: 'jev', graphId: 'graph', text: '计算并核对结果' } },
      { eventType: 'route_decision', participantId: actor.id, sourceSessionId: actor.sessionId, payload: { rootId, dispatchId: 'plan', targetParticipantId: actor.id } },
      { eventType: 'participant_activity', participantId: actor.id, sourceSessionId: actor.sessionId, payload: { rootId, dispatchId: 'plan', activityKind: 'child', phase: 'completed', sourceEventId: 'pi-event', sourceEventType: 'turn_completed', sourceTurnId: 'pi-plan' } },
      { eventType: 'room_post', participantId: actor.id, sourceSessionId: '', payload: { post: { schemaVersion: 'wisdom-weasel.room-post.v2', postId: finalId, roomId: room.id, rootId, generation: 1, dispatchId: finalId, authorActorRef: actor.id, kind: 'result', visibility: 'room', content: 'A=15，B=12，C=A+B=27。', idempotencyKey: finalId, publicationSource: { kind: 'runtime_projection', ref: finalId }, createdAtMs: 40 } } },
      { eventType: 'turn_completed', participantId: null, sourceSessionId: '', payload: { rootId, finalizationId: finalId, status: 'completed' } },
    ].map((event, index) => ({ ...base.events[0], ...event, turnId: rootId, eventId: `${room.id}:${index + 1}`, sequence: index + 1, resumeToken: `${room.id}:${index + 1}`, createdAtMs: (index + 1) * 10 }));
    const transport = createPreviewTransport(); const original = transport.request.bind(transport);
    transport.request = async <Response,>(request: ControlRequest): Promise<Response> => {
      if (request.pathId === 'agent.room.get') return { ok: true, room: { ...room, lastEventSequence: events.length } } as Response;
      if (request.pathId === 'agent.room.conversationSnapshot') throw new Error('Use complete Room events in this fixture');
      if (request.pathId === 'agent.room.snapshot') return { ...base, room: { ...room, lastEventSequence: events.length }, events, lastSequence: events.length, resumeToken: `${room.id}:${events.length}` } as Response;
      if (request.pathId === 'agent.jev.get') return (request.query?.graphId
        ? { ok: true, mode: 'jev', graphId: 'graph', rootId, snapshotVersion: 'final', phase: 'final', tasks: [], final: { status: 'completed', content: 'A=15，B=12，C=A+B=27。' } }
        : { ok: true, mode: 'jev', items: [{ graph_id: 'graph', room_id: room.id, phase: 'final' }] }) as Response;
      return original<Response>(request);
    };
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><ControlTransportProvider transport={transport}><TooltipProvider><PawRoomWorkspace interfaceMode="jev" personas={[]} record={room} recordId={room.id} onRoomUpdated={vi.fn()} /></TooltipProvider></ControlTransportProvider></QueryClientProvider>);
    const central = await screen.findByRole('log', { name: 'Room 公开对话时间线' });
    expect(await within(central).findByText('A=15，B=12，C=A+B=27。')).toBeVisible();
    expect(within(central).getAllByText('A=15，B=12，C=A+B=27。')).toHaveLength(1);
    expect(screen.queryByRole('button', { name: '停止 Jev 执行' })).not.toBeInTheDocument();
  });
  it('keeps the central composer and draft through explicit plan approval without creating another graph', async () => {
    const user = userEvent.setup();
    const room = previewRoomSnapshot('jev-plan-workspace').room as unknown as RoomSummary;
    const transport = createPreviewTransport(); const original = transport.request.bind(transport);
    let approved = false;
    const commands: Record<string, unknown>[] = [];
    transport.request = async <Response,>(request: ControlRequest): Promise<Response> => {
      if (request.pathId === 'agent.room.get') return { ok: true, room } as Response;
      if (request.pathId === 'agent.room.snapshot') return previewRoomSnapshot(room.id) as Response;
      if (request.pathId === 'agent.jev.get') return (request.query?.graphId
        ? { ok: true, mode: 'jev', graphId: 'planned', rootId: 'planned-root', snapshotVersion: String(approved), phase: approved ? 'execute' : 'awaiting_approval', tasks: [{ id: 'goal', objective: '核对恢复', state: 'queued' }], planApproval: { status: approved ? 'approved' : 'awaiting_approval', planHash: 'exact-plan', requirementsRevision: 1, proposal: { tasks: [{ key: 'task', objective: '核对附件读取', expectedOutput: '验证结果', acceptanceCriteria: ['附件正确读取'] }] } } }
        : { ok: true, mode: 'jev', items: [{ graph_id: 'planned', room_id: room.id, phase: approved ? 'execute' : 'awaiting_approval' }] }) as Response;
      if (request.pathId === 'agent.jev.command') { commands.push(request.body as Record<string, unknown>); approved = true; return { ok: true, graphId: 'planned', rootId: 'planned-root' } as Response; }
      return original<Response>(request);
    };
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><ControlTransportProvider transport={transport}><TooltipProvider><PawRoomWorkspace interfaceMode="jev" personas={[]} record={room} recordId={room.id} onRoomUpdated={vi.fn()} /></TooltipProvider></ControlTransportProvider></QueryClientProvider>);
    const input = await screen.findByRole('textbox', { name: '协作消息' });
    await user.type(input, '稍后讨论另一个问题');
    expect(commands).toEqual([]);
    expect(screen.queryByRole('button', { name: '停止 Jev 执行' })).not.toBeInTheDocument();
    await user.click(await screen.findByRole('button', { name: '开始执行' }));
    await screen.findByText('执行方案已确认 · 版本 1');
    expect(commands).toEqual([expect.objectContaining({ action: 'approve_plan', graphId: 'planned', rootId: 'planned-root', planHash: 'exact-plan' })]);
    expect(screen.getByRole('textbox', { name: '协作消息' })).toBe(input);
    expect(input).toHaveValue('稍后讨论另一个问题');
  });
  it('retries a Home admission with its original attachments while preserving a newly edited Room draft', async () => {
    const user = userEvent.setup();
    const room = previewRoomSnapshot('jev-home-recovery').room as unknown as RoomSummary;
    const transport = createPreviewTransport();
    const original = transport.request.bind(transport);
    const commands: Record<string, unknown>[] = [];
    transport.request = async <Response,>(request: ControlRequest): Promise<Response> => {
      if (request.pathId === 'agent.room.get') return { ok: true, room } as Response;
      if (request.pathId === 'agent.room.snapshot') return previewRoomSnapshot(room.id) as Response;
      if (request.pathId === 'agent.jev.get') return (request.query?.graphId
        ? { ok: true, mode: 'jev', graphId: 'recovered-graph', rootId: 'recovered-root', snapshotVersion: 'v1', phase: 'execute', tasks: [] }
        : { ok: true, mode: 'jev', items: commands.length > 1 ? [{ graph_id: 'recovered-graph', room_id: room.id, phase: 'execute' }] : [] }) as Response;
      if (request.pathId === 'agent.jev.command') {
        commands.push(request.body as Record<string, unknown>);
        if (commands.length === 1) throw new TypeError('Failed to fetch');
        return { ok: true, accepted: true, graphId: 'recovered-graph', rootId: 'recovered-root' } as Response;
      }
      return original<Response>(request);
    };
    await expect(createJevWork(transport, room.id, { message: '阅读原附件', attachmentIds: ['managed-home-attachment'] })).rejects.toThrow();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><ControlTransportProvider transport={transport}><TooltipProvider>
      <PawRoomWorkspace interfaceMode="jev" initialDraft="阅读原附件" initialError="发送尚未确认" personas={[]} record={room} recordId={room.id} onRoomUpdated={vi.fn()} />
    </TooltipProvider></ControlTransportProvider></QueryClientProvider>);
    const input = await screen.findByRole('textbox', { name: '协作消息' });
    await user.clear(input); await user.type(input, '继续保留的新草稿');
    await user.click(await screen.findByRole('button', { name: '重试上次发送' }));
    await waitFor(() => expect(commands).toHaveLength(2));
    expect(commands[1]).toEqual(commands[0]);
    expect(commands[1].attachmentIds).toEqual(['managed-home-attachment']);
    await waitFor(() => expect(screen.queryByRole('button', { name: '重试上次发送' })).not.toBeInTheDocument());
    expect(input).toHaveValue('继续保留的新草稿');
  });
  it('submits real graphs, queues follow-ups until final, and stops through the Jev owner', async () => {
    const user = userEvent.setup();
    const room = previewRoomSnapshot('jev-workspace-fixture').room as unknown as RoomSummary;
    const transport = createPreviewTransport();
    const original = transport.request.bind(transport);
    const commands: Record<string, unknown>[] = [];
    const oldCommands: string[] = [];
    const graphs: { id: string; done: boolean; stopped: boolean; message: string; abstained: boolean }[] = [];
    transport.request = async <Response,>(request: ControlRequest): Promise<Response> => {
      if (request.pathId === 'agent.room.get') return { ok: true, room } as Response;
      if (request.pathId === 'agent.room.snapshot') return previewRoomSnapshot(room.id) as Response;
      if (request.pathId === 'agent.jev.get') {
        const graph = graphs.find(item => item.id === request.query?.graphId);
        return (request.query?.graphId ? { ok: true, mode: 'jev', graphId: graph!.id, rootId: `root:${graph!.id}`,
          snapshotVersion: `${graph!.id}:${graph!.done}:${graph!.stopped}`, phase: graph!.done ? 'final' : 'execute', stopped: graph!.stopped,
          tasks: [], edges: [], effects: [], running: [], review: [], ready: [], blocked: [], events: graph!.abstained ? [{ source_id: 'latest', state: 'done', result_json: { status: 'abstained' } }] : [],
          final: graph!.done ? { content: `完成：${graph!.message}`, status: 'completed' } : {},
        } : { ok: true, mode: 'jev', items: graphs.map(item => ({ graph_id: item.id, root_turn_id: `root:${item.id}`, room_id: room.id, objective: item.message, phase: item.done ? 'final' : 'execute', stopped: item.stopped })) }) as Response;
      }
      if (request.pathId === 'agent.jev.command') {
        const body = request.body as Record<string, unknown>; commands.push(body);
        if (body.action === 'stop') { graphs.find(item => item.id === body.graphId)!.stopped = true; return { ok: true } as Response; }
        const id = `graph-${graphs.length + 1}`;
        graphs.unshift({ id, done: false, stopped: false, message: String(body.message), abstained: false });
        return { ok: true, accepted: true, graphId: id, rootId: `root:${id}` } as Response;
      }
      if (['agent.room.message', 'agent.room.participant.steer', 'agent.room.abort'].includes(request.pathId)) oldCommands.push(request.pathId);
      return original<Response>(request);
    };
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><ControlTransportProvider transport={transport}><TooltipProvider>
      <PawRoomWorkspace interfaceMode="jev" personas={[]} record={room} recordId={room.id} onRoomUpdated={vi.fn()} />
    </TooltipProvider></ControlTransportProvider></QueryClientProvider>);
    const input = await screen.findByRole('textbox', { name: '协作消息' });
    await waitFor(() => expect(screen.getByRole('button', { name: '同步 Jev 任务' })).toBeEnabled());
    await user.click(screen.getAllByRole('button', { name: 'Jev 模型与工具设置' })[0]);
    await user.click(within(screen.getByRole('menu')).getByRole('menuitemradio', { name: '始终由其他伙伴复核' }));
    await user.click(screen.getAllByRole('button', { name: 'Jev 模型与工具设置' })[0]);
    expect(within(screen.getByRole('menu')).getByRole('menuitemradio', { name: '始终由其他伙伴复核' })).toHaveAttribute('aria-checked', 'true');
    await user.keyboard('{Escape}');
    await user.type(input, '完成第一项');
    await user.click(screen.getByRole('button', { name: '发送消息' }));
    await within(screen.getByRole('complementary', { name: 'Jev 任务进展' })).findByText('推进任务与复核');
    expect(commands).toHaveLength(1);
    expect(commands[0]).toMatchObject({ action: 'create', verificationMode: 'independent' });
    graphs[0].abstained = true;
    await user.click(screen.getByRole('button', { name: '同步 Jev 任务' }));
    await within(screen.getByRole('complementary', { name: 'Jev 任务进展' })).findByText('暂未选出下一步');
    expect(screen.getByLabelText('Room 窗口控制')).not.toHaveAttribute('data-status', 'busy');
    expect(screen.getByRole('button', { name: '停止 Jev 执行' })).toBeEnabled();
    await user.type(input, '接着核对第二项');
    await user.click(screen.getByRole('button', { name: '排入下一轮任务' }));
    expect(commands).toHaveLength(1);
    graphs[0].done = true;
    await user.click(screen.getByRole('button', { name: '同步 Jev 任务' }));
    await waitFor(() => expect(commands).toHaveLength(2));
    expect(commands[1]).toMatchObject({ action: 'create', message: '接着核对第二项', previousRootId: 'root:graph-1' });
    await waitFor(() => expect(screen.getByRole('button', { name: '停止当前任务' })).toBeEnabled());
    await user.click(screen.getByRole('button', { name: '停止当前任务' }));
    await waitFor(() => expect(commands.at(-1)).toMatchObject({ action: 'stop', graphId: 'graph-2' }));
    expect(oldCommands).toEqual([]);
  });
});
