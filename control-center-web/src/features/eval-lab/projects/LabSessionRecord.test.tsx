import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { useAgentLiveStore } from '@/features/agent/state/live-store';
import { MockControlTransport } from '@/test/mock-transport';
import type { ControlRequest } from '@/platform/transport';
import { LabSessionRecord } from './LabSessionRecord';

const clients: QueryClient[] = [];
afterEach(() => { cleanup(); clients.splice(0).forEach((client) => client.clear()); useAgentLiveStore.getState().clear('guide-session'); });
const message = (id: string, turnId: string, text: string) => ({ schemaVersion: 'rag-ime.agent-message.v1', id, sessionId: 'source-session', turnId, role: 'assistant', status: 'completed',
  blocks: [{ id: `${id}:text`, type: 'text', status: 'completed', presentationKind: 'markdown', data: { text } }], attachments: [], citations: [], createdAtMs: 1, completedAtMs: 2 });
function snapshot() { return { schemaVersion: 'rag-ime.agent-message-list.v1', ok: true, sessionId: 'source-session', status: 'idle', items: [message('one', 'turn-one', '第一轮完整回答'), message('two', 'turn-two', '第二轮完整回答')], liveEvents: [], lastSequence: 0 }; }
function mount(raw: Record<string, unknown> = snapshot(), turnId?: string) {
  // Match debug_server's messages route: only omitted view or a single `recent`
  // is supported. A real Gateway GET with view=full returns HTTP 400.
  const transport = new MockControlTransport({ routes: { 'agent.session.snapshot': ({ query }: ControlRequest) => {
    if (query?.view !== undefined && query.view !== 'recent') throw new Error('unsupported Session snapshot view');
    return raw;
  } } });
  const client = new QueryClient(); clients.push(client); const close = vi.fn();
  render(<QueryClientProvider client={client}><ControlTransportProvider transport={transport}><LabSessionRecord sessionId="source-session" turnId={turnId} onClose={close} /></ControlTransportProvider></QueryClientProvider>);
  return { transport, close };
}
it('renders the exact Session with shared message blocks without touching the Guide live store', async () => {
  useAgentLiveStore.getState().ensure('guide-session');
  const before = useAgentLiveStore.getState().projections;
  const { transport, close } = mount();
  expect(await screen.findByText('第一轮完整回答')).toBeVisible();
  expect(screen.getByText('第二轮完整回答')).toBeVisible();
  expect(transport.requests[0]?.request).toMatchObject({ pathId: 'agent.session.snapshot', params: { sessionId: 'source-session' } });
  expect(transport.requests[0]?.request.query?.view).toBeUndefined();
  expect(useAgentLiveStore.getState().projections).toBe(before);
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /重试|继续运行|批准/ })).not.toBeInTheDocument();
  fireEvent.keyDown(screen.getByRole('complementary', { name: '原 Session 记录阅读' }), { key: 'Escape' });
  expect(close).toHaveBeenCalledOnce();
  expect(transport.requests).toHaveLength(1);
});
it('uses the default public snapshot accepted by the real Session owner rather than an unsupported full view', async () => {
  const { transport } = mount();
  expect(await screen.findByText('第一轮完整回答')).toBeVisible();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(transport.requests[0]?.request.query).toBeUndefined();
});
it('filters by the exact stored Turn identity rather than showing another round', async () => {
  mount(snapshot(), 'turn-two');
  expect(await screen.findByText('第二轮完整回答')).toBeVisible();
  expect(screen.queryByText('第一轮完整回答')).not.toBeInTheDocument();
});
it('reports a missing Turn without falling back to another completion', async () => {
  mount(snapshot(), 'missing-turn');
  expect(await screen.findByRole('alert')).toHaveTextContent('未跳转到其他回合');
  expect(screen.queryByText('第一轮完整回答')).not.toBeInTheDocument();
});
it('rejects a foreign Session response without rendering its transcript', async () => {
  mount({ ...snapshot(), sessionId: 'foreign-session' });
  expect(await screen.findByRole('alert')).toHaveTextContent('读取身份不匹配');
  expect(screen.queryByText('第一轮完整回答')).not.toBeInTheDocument();
});
it('preserves the selected Turn tool arguments and result for explicit full-record reading', async () => {
  const raw = { ...snapshot(), lastSequence: 1, liveEvents: [{ schemaVersion: 'rag-ime.agent-event.v1', eventId: 'source-session:1', sessionId: 'source-session', turnId: 'turn-two', sequence: 1, createdAtMs: 1, eventType: 'tool_finished', resumeToken: 'source-session:1',
    payload: { toolCallId: 'read-one', toolName: 'workspace_read', summary: '原工具读取结果', args: { path: 'evidence.txt' }, result: { text: '这是原始工具输出' }, status: 'completed' } }] };
  const { transport } = mount(raw, 'turn-two');
  fireEvent.click(await screen.findByText(/原工具读取结果/, { selector: 'summary' }));
  expect(screen.getByText(/"path": "evidence.txt"/)).toBeVisible();
  expect(screen.getByText(/这是原始工具输出/)).toBeVisible();
  fireEvent.click(screen.getByText('查看本次返回的完整公开消息与事件'));
  expect(await screen.findByText(/"eventType": "tool_finished"/)).toBeVisible();
  expect(transport.requests.every(({ request }) => request.pathId === 'agent.session.snapshot')).toBe(true);
});
