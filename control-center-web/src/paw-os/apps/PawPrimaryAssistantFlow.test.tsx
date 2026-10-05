import { forwardRef, type ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { createPreviewTransport } from '@/app/preview-control-transport';
import { TooltipProvider } from '@/components/primitives';
import { useAgentLiveStore } from '@/features/agent/state/live-store';
import { PawAgentApp } from './PawAgentApp';

// Only viewport measurement is synthetic: jsdom has no layout. All domain
// transport, Home, Session, reducers, authority and Stop paths are production.
vi.mock('react-virtuoso', () => ({ Virtuoso: forwardRef(function Virtualizer({ data, itemContent, scrollerRef, components, context }: {
  data: string[]; itemContent: (index: number, item: string) => ReactNode;
  scrollerRef?: (node: HTMLElement | null) => void;
  components?: { Header?: (props: { context?: unknown }) => ReactNode; Footer?: () => ReactNode }; context?: unknown;
}, _ref) { const Header = components?.Header; const Footer = components?.Footer;
  return <div ref={scrollerRef}>{Header ? <Header context={context} /> : null}{data.map((id, index) => <div key={id}>{itemContent(index, id)}</div>)}{Footer ? <Footer /> : null}</div>;
}) }));
afterEach(() => { cleanup(); for (const id of Object.keys(useAgentLiveStore.getState().projections)) useAgentLiveStore.getState().clear(id); });

it('completes the integrated primary conversation, explicit task, Stop, return and result flow', async () => {
  const transport = createPreviewTransport();
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <ControlTransportProvider transport={transport}><TooltipProvider><PawAgentApp initialRoute="/agent" /></TooltipProvider></ControlTransportProvider>
  </QueryClientProvider>);
  await waitFor(() => expect(screen.getByRole('button', { name: /打开对话/ })).toBeEnabled());
  fireEvent.change(screen.getByRole('textbox', { name: '和我的助手聊聊' }), { target: { value: '先说清楚目标' } });
  fireEvent.click(screen.getByRole('button', { name: '发送给我的助手' }));
  await screen.findByText(/我们可以先把目标和顾虑说清楚/, {}, { timeout: 10000 });
  expect(screen.getByText('讨论 · 只读')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '交给助手做' }));
  fireEvent.change(await screen.findByRole('textbox', { name: '和我的助手聊聊' }), { target: { value: '检查主助手入口' } });
  fireEvent.change(screen.getByRole('textbox', { name: '本次工作目录' }), { target: { value: '/work/demo' } });
  fireEvent.click(screen.getByRole('checkbox'));
  await waitFor(() => expect(screen.getByRole('button', { name: '授权并开始任务' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: '授权并开始任务' }));
  const stop = await screen.findByRole('button', { name: '停止本轮' });
  expect(screen.getByText('本次工作区已授权')).toBeVisible();
  fireEvent.click(stop);
  await waitFor(() => expect(screen.queryByRole('button', { name: '停止本轮' })).not.toBeInTheDocument());
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 4200)); });
  expect(screen.queryByText(/这是演示任务的结果/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '返回我的助手' }));
  const summary = await transport.request<{ tasks: unknown[] }>({ pathId: 'agent.primary.ensure', body: {} });
  expect(summary.tasks).toEqual(expect.arrayContaining([expect.objectContaining({ title: '检查主助手入口', goal: expect.objectContaining({ status: 'active' }) })]));
  const taskList = await screen.findByRole('region', { name: '助手的任务' });
  fireEvent.click(await within(taskList).findByRole('button', { name: /检查主助手入口.*任务未完成/ }, { timeout: 5000 }));
  fireEvent.change(await screen.findByRole('textbox', { name: '消息' }), { target: { value: '继续检查，给我结果' } });
  fireEvent.click(screen.getByRole('button', { name: '发送' }));
  await screen.findByText(/这是演示任务的结果/, {}, { timeout: 10000 });
  fireEvent.click(screen.getByRole('button', { name: '返回我的助手' }));
  await screen.findByRole('button', { name: /检查主助手入口.*已完成/ });
  const tasks = transport.requests.filter(({ request }) => request.pathId === 'agent.primary.tasks.create');
  expect(tasks).toHaveLength(1);
  expect(tasks[0].request.body).toMatchObject({ sourceSessionId: expect.any(String), sourceMessageId: expect.any(String), objective: '检查主助手入口', workspaceRoots: ['/work/demo'] });
  const prompts = transport.requests.filter(({ request }) => request.pathId === 'agent.session.prompt');
  expect(prompts).toHaveLength(3);
  expect(prompts[1].request.body).toMatchObject({ clientMessageId: (tasks[0].request.body as Record<string, unknown>).clientRequestId });
});
