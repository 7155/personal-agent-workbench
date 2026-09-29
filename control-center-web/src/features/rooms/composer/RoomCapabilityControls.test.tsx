import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { createPreviewTransport } from '@/app/preview-control-transport';
import { previewSessions } from '@/features/agent/preview-data';
import { previewRoomSnapshot } from '@/app/preview-room-data';
import type { RoomSummary } from '../room-types';
import type { ControlRequest } from '@/platform/transport';
import { RoomCapabilityControls } from './RoomCapabilityControls';

afterEach(cleanup);

it('keeps a visible memory switch bound to the selected partner and never updates Room permissions', async () => {
  const transport = createPreviewTransport();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><ControlTransportProvider transport={transport}>
    <RoomCapabilityControls participants={[
      { id: 'earth', sessionId: previewSessions[0].id, ordinal: 0, displayName: 'Agent 1', status: 'active' },
      { id: 'mars', sessionId: previewSessions[1].id, ordinal: 1, displayName: 'Agent 2', status: 'active' },
    ]} showSessionSettings busy={false} disabled={false} onSelectTool={() => undefined} />
  </ControlTransportProvider></QueryClientProvider>);
  const user = userEvent.setup();
  const memory = await screen.findByRole('switch', { name: 'Earth 的记忆' });
  await waitFor(() => expect(memory).toBeEnabled());
  expect(memory).toHaveAttribute('aria-checked', 'false');
  await user.click(memory);
  await waitFor(() => expect(memory).toHaveAttribute('aria-checked', 'true'));
  expect(transport.requests.filter(call => call.request.pathId === 'agent.session.capability-policy.update').at(-1)?.request.params?.sessionId).toBe(previewSessions[0].id);
  await user.click(screen.getByRole('combobox', { name: '选择要设置记忆和插件的伙伴' }));
  await user.click(screen.getByRole('option', { name: 'Mars' }));
  await waitFor(() => expect(screen.getByRole('switch', { name: 'Mars 的记忆' })).toHaveAttribute('aria-checked', 'false'));
  expect(transport.requests.some(call => call.request.pathId === 'agent.room.archive' || call.request.pathId === 'agent.session.mode.update')).toBe(false);
});

it('reads the exact partner from a paginated Session directory without upgrading it to the Room policy', async () => {
  const transport = createPreviewTransport();
  const original = transport.request.bind(transport);
  const mutations: string[] = [];
  const reads: ControlRequest[] = [];
  transport.request = async <Response,>(request: ControlRequest): Promise<Response> => {
    reads.push(request);
    if (request.pathId === 'agent.sessions.list') return (request.query?.beforeId
      ? { schemaVersion: 'rag-ime.agent-session-list.v1', ok: true, items: [{ id: previewSessions[0].id, executionMode: 'read_only', toolProfileVersion: 'subagent-readonly-v1' }], hasMore: false }
      : { schemaVersion: 'rag-ime.agent-session-list.v1', ok: true, items: [{ id: 'another-session', executionMode: 'full_trust', toolProfileVersion: 'control-center-auto-approve-v1' }], hasMore: true, nextBeforeUpdatedAtMs: 10, nextBeforeId: 'another-session' }) as Response;
    if (request.pathId === 'agent.room.archive' || request.pathId === 'agent.session.mode.update') mutations.push(request.pathId);
    return original<Response>(request);
  };
  const room = previewRoomSnapshot('permission-fixture').room as unknown as RoomSummary;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><ControlTransportProvider transport={transport}>
    <RoomCapabilityControls participants={[{ id: 'earth', sessionId: previewSessions[0].id, ordinal: 0, displayName: 'Agent 1', status: 'active' }]} room={room} showSessionSettings automaticModels busy={false} disabled={false} onSelectTool={() => undefined} />
  </ControlTransportProvider></QueryClientProvider>);
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: 'Earth 当前权限：只读（沙箱）' }));
  expect(screen.getByText(/当前值来自这位伙伴的 Session/)).toBeVisible();
  expect(mutations).toEqual([]);
  expect(reads.some(request => request.pathId === 'agent.session.snapshot')).toBe(false);
  expect(reads.some(request => request.pathId === 'agent.sessions.list' && request.query?.beforeId === 'another-session')).toBe(true);
  expect(screen.getByText('执行时自动路由')).toBeVisible();
});

it('starts memory off and updates only the selected partner through the Session policy API', async () => {
  const transport = createPreviewTransport();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><ControlTransportProvider transport={transport}>
    <RoomCapabilityControls participants={[
      { id: 'earth', sessionId: previewSessions[0].id, ordinal: 0, displayName: 'Agent 1', status: 'active' },
      { id: 'mars', sessionId: previewSessions[1].id, ordinal: 1, displayName: 'Agent 2', status: 'active' },
    ]} busy={false} disabled={false} onSelectTool={() => undefined} />
  </ControlTransportProvider></QueryClientProvider>);
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: /对话功能：/ }));
  await user.click(await screen.findByRole('button', { name: '当前对话记忆已关闭，打开记忆开关' }));
  await user.click(screen.getByRole('combobox', { name: '记忆召回的当前对话使用' }));
  await user.click(screen.getByRole('option', { name: '当前对话启用' }));
  await screen.findByRole('button', { name: '当前对话记忆已开启，打开记忆开关' });
  await user.keyboard('{Escape}');
  await user.click(screen.getByRole('combobox', { name: '选择要设置记忆和插件的伙伴' }));
  await user.click(screen.getByRole('option', { name: 'Mars' }));
  await waitFor(() => expect(screen.getByRole('button', { name: /对话功能：/ })).toBeEnabled());
  await user.click(screen.getByRole('button', { name: /对话功能：/ }));
  await waitFor(() => expect(screen.getByRole('button', { name: '当前对话记忆已关闭，打开记忆开关' })).toBeVisible());
});

it('changes model reasoning on the selected partner and rereads that Session', async () => {
  const transport = createPreviewTransport();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><ControlTransportProvider transport={transport}>
    <RoomCapabilityControls participants={[
      { id: 'earth', sessionId: previewSessions[0].id, ordinal: 0, displayName: 'Agent 1', status: 'active' },
      { id: 'mars', sessionId: previewSessions[1].id, ordinal: 1, displayName: 'Agent 2', status: 'active' },
    ]} busy={false} disabled={false} onSelectTool={() => undefined} />
  </ControlTransportProvider></QueryClientProvider>);
  const user = userEvent.setup();
  await user.click(screen.getByRole('combobox', { name: '选择要设置记忆和插件的伙伴' }));
  await user.click(screen.getByRole('option', { name: 'Mars' }));
  const model = await screen.findByRole('button', { name: /模型与推理：/ });
  await waitFor(() => expect(model).toBeEnabled());
  await user.click(model);
  await user.click(screen.getByRole('radio', { name: '不启用推理' }));
  await waitFor(() => expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.thinking.select').at(-1)?.request).toMatchObject({ params: { sessionId: previewSessions[1].id }, body: { level: 'off' } }));
  expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.model.select').every(({ request }) => request.params?.sessionId === previewSessions[1].id)).toBe(true);
});
