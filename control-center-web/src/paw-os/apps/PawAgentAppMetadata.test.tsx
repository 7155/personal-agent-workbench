import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { forwardRef, type ReactNode } from 'react';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import type { SessionSummary } from '@/features/agent/types';
import { agentProjection, agentSessionAddress } from '@/features/agent/state/live-store';
import { agentModeStore } from '@/features/semantic-workspace/agent-mode-store';
import type { PawOsWindowTarget } from '@/features/paw-os/model/desktop';
import type { ControlRequest } from '@/platform/transport';
import { controlRoute } from '@/platform/routes';
import { MockControlTransport } from '@/test/mock-transport';
import { PawAgentApp } from './PawAgentApp';
import { PawSessionWorkspace } from './PawSessionWorkspace';

// jsdom has no row measurements. Keep real message renderers while exposing
// their virtual rows; this is not a browser/layout check.
vi.mock('react-virtuoso', () => ({ Virtuoso: forwardRef(function Rows({ data, itemContent }: {
  data: string[]; itemContent: (index: number, item: string) => ReactNode;
}, _ref) { return <div>{data.map((item, index) => <div key={item}>{itemContent(index, item)}</div>)}</div>; }) }));

beforeEach(() => agentModeStore.select('traditional'));
afterEach(cleanup);

function deferred() {
  let resolve!: (value: unknown) => void;
  const promise = new Promise<unknown>((settle) => { resolve = settle; });
  return { promise, resolve };
}
function session(id: string, evaluationSnapshot = false): SessionSummary {
  return { id, title: `原记录 ${id}`, mode: 'assistant', status: 'idle', updatedAtMs: 1,
    roleId: '', roleVersion: '', roleBookRevisionId: '', workspaceRoots: [], evaluationSnapshot };
}
function page(index: number, hasMore = true) {
  const items = Array.from({ length: 100 }, (_, offset) => ({ ...session(`recent-${index * 100 + offset}`), updatedAtMs: 20_000 - index * 100 - offset }));
  const last = items.at(-1)!;
  return { schemaVersion: 'rag-ime.agent-session-list.v1', ok: true, items, hasMore,
    nextBeforeUpdatedAtMs: last.updatedAtMs, nextBeforeId: last.id,
    nextCursor: { beforeUpdatedAtMs: last.updatedAtMs, beforeId: last.id } };
}
function savedSnapshot(request: ControlRequest) {
  return { schemaVersion: 'rag-ime.agent-message-list.v1', ok: true,
    sessionId: request.params?.sessionId, items: [{ schemaVersion: 'rag-ime.agent-message.v1', id: `${request.params?.sessionId}:saved`,
      sessionId: request.params?.sessionId, turnId: 'saved-original-turn', role: 'user', status: 'completed',
      blocks: [{ id: 'saved-text', type: 'text', status: 'completed', presentationKind: 'markdown', data: { text: '已保存的原任务说明：核对原始记录。' } }],
      attachments: [], citations: [], createdAtMs: 1, completedAtMs: 1 }], status: 'idle', liveEvents: [] as unknown[], lastSequence: 0, resumeToken: '' };
}
function transportWith(read: (request: ControlRequest) => unknown, snapshot: (request: ControlRequest) => unknown = savedSnapshot) {
  return new MockControlTransport({ routes: {
    'agent.sessions.list': read,
    'agent.rooms.list': { ok: true, items: [] }, 'agent.roles.list': { items: [] },
    'agent.session.snapshot': snapshot,
    'agent.session.models': { providers: [], selected: {} }, 'agent.session.commands': { items: [] },
    'agent.tools.list': { items: [] }, 'agent.runtime.get': { capabilities: {} },
  } });
}
function tree(transport: MockControlTransport, target: PawOsWindowTarget, client: QueryClient) {
  return <QueryClientProvider client={client}><ControlTransportProvider transport={transport}><TooltipProvider>
    <PawAgentApp target={target} />
  </TooltipProvider></ControlTransportProvider></QueryClientProvider>;
}
function open(transport: MockControlTransport, id: string, subtitle?: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const target: PawOsWindowTarget = { kind: 'session', id, title: `目标 ${id}`, subtitle };
  return { client, view: render(tree(transport, target, client)) };
}
function metadataReads(transport: MockControlTransport) {
  return transport.requests.filter(({ request }) => request.pathId === 'agent.sessions.list');
}
function expectOnlyReads(transport: MockControlTransport) {
  expect(transport.requests.every(({ request }) => controlRoute(request.pathId).method === 'GET' && !request.body)).toBe(true);
}

describe('canonical metadata before mounting a directly targeted Session', () => {
  it('finds the older evaluation row beyond 1000 newer ordinary Sessions and never mounts writable controls', async () => {
    const id = 'agent:older-evaluation';
    const older = deferred();
    let reads = 0;
    const transport = transportWith(() => ++reads <= 10 ? page(reads - 1) : older.promise);
    const { client } = open(transport, id);
    await waitFor(() => expect(metadataReads(transport)).toHaveLength(11));
    expect(screen.getByRole('status', { name: 'Session 工作记录状态' })).toHaveTextContent(id);
    expect(document.querySelector('.paw-session-workspace')).toBeNull();
    expect(screen.queryByRole('textbox', { name: '消息' })).toBeNull();
    expect(transport.requests.map(({ request }) => request.pathId)).toEqual(Array(11).fill('agent.sessions.list'));
    await act(async () => { older.resolve({ items: [session(id, true)], hasMore: false }); });
    expect(await screen.findByText('评测记录，只读')).toBeInTheDocument();
    expect(await screen.findByText('已保存的原任务说明：核对原始记录。')).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: '消息' })).toBeNull();
    expect(screen.queryByRole('button', { name: '继续' })).toBeNull();
    expect(screen.queryByRole('button', { name: '对话工具' })).toBeNull();
    await waitFor(() => expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.snapshot')).toHaveLength(1));
    const snapshot = transport.requests.find(({ request }) => request.pathId === 'agent.session.snapshot')!.request;
    expect(snapshot.params).toEqual({ sessionId: id });
    // The existing snapshot API omits view for its default full history read.
    expect(snapshot.query?.view ?? 'full').toBe('full');
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.models')).toBe(false);
    expect(transport.subscriptionCalls).toHaveLength(0);
    expect(metadataReads(transport).map(({ request }) => request.query?.includeArchived)).toEqual(Array(11).fill(true));
    expect(metadataReads(transport)[1]!.request.query).toMatchObject({ beforeUpdatedAtMs: page(0).nextBeforeUpdatedAtMs, beforeId: page(0).nextBeforeId });
    expectOnlyReads(transport); client.clear();
  });

  it('keeps an ordinary older Session editable despite a read-only subtitle and preserves its draft while the rail reloads', async () => {
    const id = 'agent:older-ordinary'; const later = deferred(); let reads = 0;
    const transport = transportWith(() => {
      reads += 1;
      if (reads === 1 || reads === 3) return page(0);
      if (reads === 2) return { ...page(1), items: [session(id)] };
      return later.promise;
    });
    const { client } = open(transport, id, '评测记录 · 只读');
    const composer = await screen.findByRole('textbox', { name: '消息' });
    expect(metadataReads(transport)).toHaveLength(2);
    expect(screen.queryByText('评测记录，只读')).toBeNull();
    await userEvent.setup().type(composer, '原 Session 未提交草稿');
    await userEvent.setup().click(screen.getByRole('button', { name: '打开工作记录' }));
    await waitFor(() => expect(metadataReads(transport)).toHaveLength(4));
    expect(screen.getByRole('textbox', { name: '消息' })).toBe(composer);
    expect(composer).toHaveValue('原 Session 未提交草稿');
    await act(async () => { later.resolve({ items: [session(id)], hasMore: false }); });
    expect(screen.getByRole('textbox', { name: '消息' })).toBe(composer);
    expect(composer).toHaveValue('原 Session 未提交草稿');
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.snapshot')).toHaveLength(1);
    expectOnlyReads(transport); client.clear();
  });

  it('aborts a superseded target and does not let its late evaluation row affect the newer ordinary Session', async () => {
    const old = deferred(); let reads = 0;
    const transport = transportWith(() => ++reads === 1 ? old.promise : { items: [session('agent:new-target')], hasMore: false });
    const { client, view } = open(transport, 'agent:old-target');
    await waitFor(() => expect(metadataReads(transport)).toHaveLength(1));
    const first = metadataReads(transport)[0]!.request;
    expect(document.querySelector('.paw-session-workspace')).toBeNull();
    view.rerender(tree(transport, { kind: 'session', id: 'agent:new-target', title: '新目标' }, client));
    const composer = await screen.findByRole('textbox', { name: '消息' });
    await userEvent.setup().type(composer, '新目标草稿');
    expect(first.signal?.aborted).toBe(true);
    await act(async () => { old.resolve({ ...page(0), items: [session('agent:old-target', true)] }); });
    expect(screen.getByRole('textbox', { name: '消息' })).toBe(composer);
    expect(composer).toHaveValue('新目标草稿');
    expect(screen.queryByText('评测记录，只读')).toBeNull();
    expect(metadataReads(transport)).toHaveLength(2);
    expect(transport.requests.filter(({ request }) => request.pathId.startsWith('agent.session.')).every(({ request }) => request.params?.sessionId === 'agent:new-target')).toBe(true);
    expectOnlyReads(transport); client.clear();
  });

  it.each(['network', 'missing', 'cursor'] as const)('retains the original target and retries its metadata once after %s failure', async (failure) => {
    const id = `agent:recover-${failure}`; const recovery = deferred(); let reads = 0;
    const transport = transportWith(() => {
      reads += 1;
      if (failure === 'cursor' && reads <= 2) return page(0);
      if (reads === 1) { if (failure === 'network') throw new Error('connection unavailable'); return { items: [], hasMore: false }; }
      return recovery.promise;
    });
    const { client } = open(transport, id);
    const retry = await screen.findByRole('button', { name: '重新读取 Session' });
    const notice = screen.getByRole('alert', { name: 'Session 工作记录状态' });
    const label = retry.textContent;
    const errorText = notice.querySelector('span')!.textContent;
    const buttonClass = retry.className;
    const buttonSize = retry.getAttribute('data-size');
    expect(notice).toHaveTextContent(id);
    expect(document.querySelector('.paw-session-workspace')).toBeNull();
    expect(transport.requests.every(({ request }) => request.pathId === 'agent.sessions.list')).toBe(true);
    const before = reads;
    retry.focus(); await userEvent.setup().keyboard('{Enter}');
    await waitFor(() => expect(retry).toHaveAttribute('aria-busy', 'true'));
    expect(screen.getByRole('button', { name: '重新读取 Session' })).toBe(retry);
    expect(screen.getByRole('alert', { name: 'Session 工作记录状态' })).toBe(notice);
    expect(retry.textContent).toBe(label);
    expect(retry.className).toBe(buttonClass);
    expect(retry.getAttribute('data-size')).toBe(buttonSize);
    expect(notice.querySelector('span')).toHaveTextContent(errorText!);
    expect(screen.queryByText('正在读取 Session 工作记录…')).toBeNull();
    expect(document.querySelector('.paw-session-workspace')).toBeNull();
    expect(retry).toHaveAttribute('aria-disabled', 'true');
    expect(retry).not.toBeDisabled();
    expect(retry).toHaveFocus();
    await userEvent.setup().keyboard('{Enter}'); await userEvent.setup().click(retry);
    expect(reads).toBe(before + 1);
    await act(async () => { recovery.resolve({ items: [session(id, true)], hasMore: false }); });
    expect(await screen.findByText('评测记录，只读')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '重新读取 Session' })).toBeNull();
    expect(screen.queryByRole('textbox', { name: '消息' })).toBeNull();
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.models')).toBe(false);
    expectOnlyReads(transport); client.clear();
  });

  it('reopens the same original evaluation ID with its canonical read-only flag on a fresh App mount', async () => {
    const id = 'agent:reopened-eval';
    const transport = transportWith(() => ({ items: [session(id, true)], hasMore: false }));
    const first = open(transport, id);
    expect(await screen.findByText('评测记录，只读')).toBeInTheDocument();
    first.view.unmount(); first.client.clear();
    const second = open(transport, id);
    expect(await screen.findByText('评测记录，只读')).toBeInTheDocument();
    expect(metadataReads(transport)).toHaveLength(2);
    expect(screen.queryByRole('textbox', { name: '消息' })).toBeNull();
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.models')).toBe(false);
    expectOnlyReads(transport); second.client.clear();
  });

  it.each(['failed', 'awaiting-approval'] as const)('keeps saved %s evidence readable without any original Runtime action', async (state) => {
    const id = `agent:saved-${state}`;
    const payload = state === 'failed' ? { error: '已保存的评测失败原因' }
      : { approvalId: 'saved-approval', toolCallId: 'saved-tool', payloadSha256: 'a'.repeat(64), operation: '已保存的批准前检查' };
    const transport = transportWith(() => ({ items: [session(id, true)], hasMore: false }), (request) => ({
      ...savedSnapshot(request), status: state === 'failed' ? 'failed' : 'waiting', lastSequence: 1, resumeToken: `${id}:1`,
      liveEvents: [{ schemaVersion: 'rag-ime.agent-event.v1', eventId: `${id}:1`, sessionId: id, turnId: 'saved-original-turn',
        sequence: 1, createdAtMs: 2, eventType: state === 'failed' ? 'turn_failed' : 'approval_required', payload, resumeToken: `${id}:1` }],
    }));
    const { client } = open(transport, id);
    expect(await screen.findByText('已保存的原任务说明：核对原始记录。')).toBeInTheDocument();
    expect(screen.getByText('评测记录，只读')).toBeInTheDocument();
    const projection = agentProjection(agentSessionAddress(transport, id));
    expect(projection.turnsById['saved-original-turn'].status).toBe(state === 'failed' ? 'failed' : 'waiting');
    if (state === 'awaiting-approval') {
      expect(projection.activitiesById['saved-tool']).toMatchObject({ kind: 'approval_required', status: 'waiting' });
      expect(screen.getByLabelText(/^审批状态：/)).toBeInTheDocument();
    }
    expect(screen.queryAllByRole('button', { name: /^(继续|继续对话|重试本轮|切换模型|批准|拒绝|去审批|请求权限)$/ })).toHaveLength(0);
    expect(screen.queryByRole('textbox', { name: '消息' })).toBeNull();
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.models')).toBe(false);
    expectOnlyReads(transport); client.clear();
  });

  it('does not admit an initial submission attached to an already canonical evaluation snapshot', async () => {
    const id = 'agent:readonly-initial-submission';
    const transport = transportWith(() => ({ items: [session(id, true)], hasMore: false }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><ControlTransportProvider transport={transport}><TooltipProvider>
      <PawSessionWorkspace record={session(id, true)} recordId={id} initialSubmission={{ clientMessageId: 'original-callback', message: '不得把旧提交重新执行' }}
        onNewWork={vi.fn()} onSessionCreated={vi.fn()} onSessionUpdated={vi.fn()} />
    </TooltipProvider></ControlTransportProvider></QueryClientProvider>);
    expect((await screen.findAllByText('已保存的原任务说明：核对原始记录。')).some((element) => element.tagName === 'P')).toBe(true);
    expect(screen.getByText('评测记录，只读')).toBeInTheDocument();
    expectOnlyReads(transport);
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.snapshot')).toHaveLength(1);
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.models')).toBe(false);
    expect(screen.queryAllByText('不得把旧提交重新执行')).toHaveLength(0);
    expectOnlyReads(transport); client.clear();
  });
});
