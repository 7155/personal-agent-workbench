import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { createPreviewTransport } from '@/app/preview-control-transport';
import { previewSessions } from '@/features/agent/preview-data';
import { RoomCapabilityControls } from './RoomCapabilityControls';

afterEach(cleanup);

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
