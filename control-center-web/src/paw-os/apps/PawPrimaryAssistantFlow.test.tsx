import { forwardRef, type ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, waitFor, within } from '@testing-library/react';
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
afterEach(() => { cleanup(); vi.restoreAllMocks(); for (const id of Object.keys(useAgentLiveStore.getState().projections)) useAgentLiveStore.getState().clear(id); });

it('completes the integrated primary conversation, explicit task, Stop, return and result flow', async () => {
  // Control only the demo's four-second completion scheduler. Other UI and
  // recovery timers stay real. Run a cancelled callback explicitly to check
  // that it cannot publish a late result; no wall-clock sleep proves that.
  const completions: (() => void)[] = [];
  const realSetTimeout = globalThis.setTimeout;
  vi.spyOn(globalThis, 'setTimeout').mockImplementation(((callback: TimerHandler, delay?: number, ...args: unknown[]) => {
    if (delay === 4000 && typeof callback === 'function') {
      completions.push(() => callback(...args));
      return realSetTimeout(() => undefined, delay);
    }
    return realSetTimeout(callback, delay, ...args);
  }) as typeof setTimeout);
  const transport = createPreviewTransport();
  const view = render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <ControlTransportProvider transport={transport}><TooltipProvider><PawAgentApp initialRoute="/agent" /></TooltipProvider></ControlTransportProvider>
  </QueryClientProvider>);
  const home = () => within(view.container.querySelector<HTMLElement>('.paw-primary-home')!);
  const composer = () => within(view.container.querySelector<HTMLElement>('.paw-session-workspace .agent-composer')!);
  const workspace = () => within(view.container.querySelector<HTMLElement>('.paw-session-workspace')!);
  await waitFor(() => expect(home().getByRole('button', { name: /进入对话/ })).toBeEnabled());
  fireEvent.change(home().getByRole('textbox', { name: '和我的助手聊聊' }), { target: { value: '先说清楚目标' } });
  fireEvent.click(home().getByRole('button', { name: '发送给我的助手' }));
  await within(view.container.querySelector<HTMLElement>('.paw-agent-stage')!).findByText(/我们可以先把目标和顾虑说清楚/, { selector: 'p' }, { timeout: 10000 });
  expect(workspace().getByText('讨论 · 只读')).toBeVisible();
  fireEvent.click(workspace().getByRole('button', { name: '交给助手做' }));
  fireEvent.change(await within(view.container.querySelector<HTMLElement>('.paw-agent-stage')!).findByRole('textbox', { name: '和我的助手聊聊' }), { target: { value: '检查主助手入口' } });
  fireEvent.change(home().getByRole('textbox', { name: '本次工作目录' }), { target: { value: '/work/demo' } });
  fireEvent.click(home().getByRole('checkbox'));
  await waitFor(() => expect(home().getByRole('button', { name: '授权并开始任务' })).toBeEnabled());
  fireEvent.click(home().getByRole('button', { name: '授权并开始任务' }));
  await waitFor(() => expect(composer().getByRole('button', { name: '停止本轮' })).toBeVisible());
  const stop = composer().getByRole('button', { name: '停止本轮' });
  expect(composer().getByText('本次工作区已授权')).toBeVisible();
  fireEvent.click(stop);
  await waitFor(() => expect(composer().queryByRole('button', { name: '停止本轮' })).not.toBeInTheDocument());
  expect(completions).toHaveLength(1);
  await act(async () => completions[0]!());
  expect(workspace().queryByText(/这是演示任务的结果/)).not.toBeInTheDocument();
  fireEvent.click(workspace().getByRole('button', { name: '返回我的助手' }));
  const summary = await transport.request<{ tasks: unknown[] }>({ pathId: 'agent.primary.ensure', body: {} });
  expect(summary.tasks).toEqual(expect.arrayContaining([expect.objectContaining({ title: '检查主助手入口', goal: expect.objectContaining({ status: 'active' }) })]));
  const taskList = await within(view.container.querySelector<HTMLElement>('.paw-agent-stage')!).findByRole('region', { name: '助手的任务' });
  fireEvent.click(within(taskList).getByRole('button', { name: /任务记录/ }));
  fireEvent.click(await within(taskList).findByRole('button', { name: /检查主助手入口.*任务未完成/ }, { timeout: 5000 }));
  fireEvent.change(await within(view.container.querySelector<HTMLElement>('.paw-agent-stage')!).findByRole('textbox', { name: '消息' }), { target: { value: '继续检查，给我结果' } });
  const send = composer().getByRole('button', { name: '发送' });
  await waitFor(() => expect(send).toBeEnabled(), { timeout: 5000 });
  fireEvent.click(send);
  await waitFor(() => expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.prompt')).toHaveLength(3), { timeout: 5000 });
  expect(completions).toHaveLength(2);
  await act(async () => completions[1]!());
  await workspace().findByText(/这是演示任务的结果/, { selector: 'p' });
  fireEvent.click(workspace().getByRole('button', { name: '返回我的助手' }));
  await within(view.container.querySelector<HTMLElement>('.paw-agent-stage')!).findByRole('button', { name: /检查主助手入口.*已完成/ });
  const tasks = transport.requests.filter(({ request }) => request.pathId === 'agent.primary.tasks.create');
  expect(tasks).toHaveLength(1);
  expect(tasks[0].request.body).toMatchObject({ sourceSessionId: expect.any(String), sourceMessageId: expect.any(String), objective: '检查主助手入口', workspaceRoots: ['/work/demo'] });
  const prompts = transport.requests.filter(({ request }) => request.pathId === 'agent.session.prompt');
  expect(prompts).toHaveLength(3);
  expect(prompts[1].request.body).toMatchObject({ clientMessageId: (tasks[0].request.body as Record<string, unknown>).clientRequestId });
});
