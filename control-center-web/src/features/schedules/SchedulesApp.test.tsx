import { QueryClient, QueryClientProvider, type QueryClientConfig } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { createPreviewTransport } from '@/app/preview-control-transport';
import { queryClient as productionQueryClient } from '@/app/query-client';
import { TooltipProvider } from '@/components/primitives';
import { SchedulesApp } from './SchedulesApp';
import { memoryScheduleRows } from './schedule-model';
import { isGithubPrUrl } from '@/features/planning/AgentWakeSchedules';
import { memoryQueryKeys } from '@/features/memory/api';
import { MockControlTransport } from '@/test/mock-transport';

afterEach(cleanup);
function setup() {
  const transport = createPreviewTransport();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><ControlTransportProvider transport={transport}><MemoryRouter><TooltipProvider><SchedulesApp /></TooltipProvider></MemoryRouter></ControlTransportProvider></QueryClientProvider>);
  return transport;
}

function setupMemoryStatus(status: () => unknown | Promise<unknown>, defaultOptions: QueryClientConfig['defaultOptions'] = { queries: { retry: false } }) {
  const transport = new MockControlTransport({ routes: {
    'agent.wakeSchedules.list': { ok: true, items: [] },
    'observability.evalSchedules.list': { schemaVersion: 'rag-ime.eval-schedule-list.v1', ok: true, items: [] },
    'configuration.settings': { settings: {} },
    'agent.memoryMaintenance.run': status,
  } });
  const client = new QueryClient({ defaultOptions });
  render(<QueryClientProvider client={client}><ControlTransportProvider transport={transport}><MemoryRouter><TooltipProvider><SchedulesApp /></TooltipProvider></MemoryRouter></ControlTransportProvider></QueryClientProvider>);
  const counts = () => Object.fromEntries(['agent.wakeSchedules.list', 'observability.evalSchedules.list', 'configuration.settings', 'agent.memoryMaintenance.run']
    .map((pathId) => [pathId, transport.requests.filter((call) => call.request.pathId === pathId).length]));
  return { client, counts, transport };
}

it('keeps All pending while only memory status is still reading', async () => {
  let resolve!: (value: unknown) => void;
  const { client, counts } = setupMemoryStatus(() => new Promise((done) => { resolve = done; }));
  await waitFor(() => expect(client.isFetching()).toBe(1));
  expect(Object.values(counts())).toEqual([1, 1, 1, 1]);
  expect(screen.getByRole('status')).toHaveTextContent('正在读取各类安排…');
  expect(screen.getByRole('button', { name: '刷新所有任务' })).toBeDisabled();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(screen.queryByText('还没有任务安排')).not.toBeInTheDocument();
  const search = screen.getByRole('textbox', { name: '搜索定时任务' });
  fireEvent.change(search, { target: { value: '未发送的筛选草稿' } });
  await act(async () => resolve({ ok: true, runs: [] }));
  await waitFor(() => expect(screen.getByRole('button', { name: '刷新所有任务' })).toBeEnabled());
  expect(screen.queryByText('正在读取各类安排…')).not.toBeInTheDocument();
  expect(search).toHaveValue('未发送的筛选草稿');
  expect(Object.values(counts())).toEqual([1, 1, 1, 1]);
  client.clear();
});

it('retains the failed source retry node, loading focus and draft until the original query settles', async () => {
  let reads = 0;
  let resolve!: (value: unknown) => void;
  const { client, counts, transport } = setupMemoryStatus(() => {
    reads += 1;
    if (reads === 1) return Promise.reject(new Error('原后台维护记录读取失败'));
    return new Promise((done) => { resolve = done; });
  });
  const alert = await screen.findByRole('alert');
  expect(alert).toHaveTextContent('后台维护记录暂时无法读取');
  expect(alert).toHaveTextContent('原后台维护记录读取失败');
  expect(Object.values(counts())).toEqual([1, 1, 1, 1]);
  expect(screen.getByRole('button', { name: '刷新所有任务' })).toBeEnabled();
  const search = screen.getByRole('textbox', { name: '搜索定时任务' });
  fireEvent.change(search, { target: { value: '失败后保留的筛选草稿' } });
  const retry = within(alert).getByRole('button', { name: '重试' });
  const label = retry.querySelector('.ui-button__label');
  const user = userEvent.setup();
  retry.focus();
  await user.keyboard('{Enter}');
  await waitFor(() => expect(Object.values(counts())).toEqual([1, 1, 1, 2]));
  expect(retry).toBeInTheDocument();
  expect(screen.getByRole('alert')).toBe(alert);
  expect(within(alert).getByRole('button', { name: '重试' })).toBe(retry);
  expect(retry.querySelector('.ui-button__label')).toBe(label);
  expect(retry).toHaveFocus();
  expect(retry).toHaveAttribute('aria-busy', 'true');
  expect(retry).toHaveAttribute('aria-disabled', 'true');
  expect(retry.querySelector('.ui-button__progress')).toHaveAttribute('aria-hidden', 'true');
  expect(screen.getByRole('button', { name: '刷新所有任务' })).toBeDisabled();
  expect(search).toHaveValue('失败后保留的筛选草稿');
  await user.keyboard('{Enter} ');
  await user.click(retry);
  expect(Object.values(counts())).toEqual([1, 1, 1, 2]);
  for (const call of transport.requests.filter((call) => call.request.pathId === 'agent.memoryMaintenance.run')) {
    expect(call.request.query).toEqual({ limit: 12 });
    expect(call.request.signal).toBeInstanceOf(AbortSignal);
  }
  await act(async () => resolve({ ok: true, runs: [] }));
  await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
  expect(retry).not.toBeInTheDocument();
  expect(search).toHaveFocus();
  expect(screen.getByRole('button', { name: '刷新所有任务' })).toBeEnabled();
  expect(Object.values(counts())).toEqual([1, 1, 1, 2]);
  expect(search).toHaveValue('失败后保留的筛选草稿');
  client.clear();
});

it('keeps the original retry through the production query retry attempt without extra activation', async () => {
  let reads = 0;
  let reject!: (reason: Error) => void;
  let resolve!: (value: unknown) => void;
  const { client, counts } = setupMemoryStatus(() => {
    reads += 1;
    if (reads <= 2) return Promise.reject(new Error('原后台维护记录读取失败'));
    if (reads === 3) return new Promise((_done, fail) => { reject = fail; });
    return new Promise((done) => { resolve = done; });
  }, { queries: { ...productionQueryClient.getDefaultOptions().queries, retryDelay: 0 } });
  const alert = await screen.findByRole('alert');
  const retry = within(alert).getByRole('button', { name: '重试' });
  expect(Object.values(counts())).toEqual([1, 1, 1, 2]);
  const user = userEvent.setup();
  retry.focus();
  await user.keyboard('{Enter}');
  await waitFor(() => expect(Object.values(counts())).toEqual([1, 1, 1, 3]));
  expect(retry).toBeInTheDocument();
  expect(retry).toHaveFocus();
  await act(async () => reject(new Error('本次读取仍失败')));
  await waitFor(() => expect(Object.values(counts())).toEqual([1, 1, 1, 4]));
  expect(screen.getByRole('alert')).toBe(alert);
  expect(alert).toHaveTextContent('原后台维护记录读取失败');
  expect(within(alert).getByRole('button', { name: '重试' })).toBe(retry);
  expect(retry).toHaveFocus();
  expect(retry).toHaveAttribute('aria-busy', 'true');
  expect(retry).toHaveAttribute('aria-disabled', 'true');
  await user.keyboard('{Enter} ');
  await user.click(retry);
  expect(Object.values(counts())).toEqual([1, 1, 1, 4]);
  await act(async () => resolve({ ok: true, runs: [] }));
  await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
  expect(screen.getByRole('textbox', { name: '搜索定时任务' })).toHaveFocus();
  expect(Object.values(counts())).toEqual([1, 1, 1, 4]);
  client.clear();
});

it('keeps the retry usable after failure and preserves moved focus when recovery removes the notice', async () => {
  let reads = 0;
  let reject!: (reason: Error) => void;
  let resolve!: (value: unknown) => void;
  const { client, counts } = setupMemoryStatus(() => {
    reads += 1;
    if (reads === 1) return Promise.reject(new Error('原后台维护记录读取失败'));
    if (reads === 2) return new Promise((_done, fail) => { reject = fail; });
    return new Promise((done) => { resolve = done; });
  });
  const alert = await screen.findByRole('alert');
  const retry = within(alert).getByRole('button', { name: '重试' });
  const user = userEvent.setup();
  retry.focus();
  await user.keyboard('{Enter}');
  await waitFor(() => expect(Object.values(counts())).toEqual([1, 1, 1, 2]));
  expect(retry).toBeInTheDocument();
  await act(async () => reject(new Error('本次读取仍失败')));
  await waitFor(() => expect(alert).toHaveTextContent('本次读取仍失败'));
  expect(within(alert).getByRole('button', { name: '重试' })).toBe(retry);
  expect(retry).toHaveFocus();
  expect(retry).not.toHaveAttribute('aria-busy');
  expect(retry).not.toHaveAttribute('aria-disabled');
  await user.keyboard('{Enter}');
  await waitFor(() => expect(Object.values(counts())).toEqual([1, 1, 1, 3]));
  const search = screen.getByRole('textbox', { name: '搜索定时任务' }) as HTMLInputElement;
  fireEvent.change(search, { target: { value: '仍保留的筛选草稿' } });
  search.setSelectionRange(2, 5);
  const nextAction = screen.getByRole('button', { name: '安排新任务' });
  nextAction.focus();
  await act(async () => resolve({ ok: true, runs: [] }));
  await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
  expect(nextAction).toHaveFocus();
  expect(search).toHaveValue('仍保留的筛选草稿');
  expect([search.selectionStart, search.selectionEnd]).toEqual([2, 5]);
  expect(Object.values(counts())).toEqual([1, 1, 1, 3]);
  client.clear();
});

it('does not cancel or restart the original cached source when retry is activated twice before pending renders', async () => {
  let reads = 0;
  let resolve!: (value: unknown) => void;
  const { client, counts, transport } = setupMemoryStatus(() => {
    reads += 1;
    if (reads === 1) return { ok: true, runs: [] };
    if (reads === 2) return Promise.reject(new Error('已缓存记录重新读取失败'));
    return new Promise((done) => { resolve = done; });
  });
  await waitFor(() => expect(screen.getByRole('button', { name: '刷新所有任务' })).toBeEnabled());
  await act(async () => { await client.refetchQueries({ queryKey: memoryQueryKeys.curationStatus() }); });
  const alert = await screen.findByRole('alert');
  const retry = within(alert).getByRole('button', { name: '重试' });
  retry.focus();
  act(() => { fireEvent.click(retry); fireEvent.click(retry); });
  await waitFor(() => expect(retry).toHaveAttribute('aria-busy', 'true'));
  expect(Object.values(counts())).toEqual([1, 1, 1, 3]);
  expect(retry).toHaveFocus();
  expect(transport.requests.filter((call) => call.request.pathId === 'agent.memoryMaintenance.run')[2].request.signal?.aborted).toBe(false);
  await act(async () => resolve({ ok: true, runs: [] }));
  await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
  expect(screen.getByRole('textbox', { name: '搜索定时任务' })).toHaveFocus();
  expect(Object.values(counts())).toEqual([1, 1, 1, 3]);
  client.clear();
});

it('keeps refresh disabled for a lone cached memory status refresh without extra reads', async () => {
  let reads = 0;
  let resolve!: (value: unknown) => void;
  const { client, counts } = setupMemoryStatus(() => {
    reads += 1;
    return reads === 1 ? { ok: true, runs: [] } : new Promise((done) => { resolve = done; });
  });
  const refresh = screen.getByRole('button', { name: '刷新所有任务' });
  await waitFor(() => expect(refresh).toBeEnabled());
  const search = screen.getByRole('textbox', { name: '搜索定时任务' });
  fireEvent.change(search, { target: { value: '刷新期间筛选草稿' } });
  fireEvent.click(refresh);
  await waitFor(() => expect(client.isFetching()).toBe(1));
  expect(Object.values(counts())).toEqual([2, 2, 2, 2]);
  expect(refresh).toBeDisabled();
  expect(refresh.querySelector('svg')).toHaveClass('ui-spin');
  fireEvent.click(refresh);
  expect(Object.values(counts())).toEqual([2, 2, 2, 2]);
  expect(search).toHaveValue('刷新期间筛选草稿');
  await act(async () => resolve({ ok: true, runs: [] }));
  await waitFor(() => expect(refresh).toBeEnabled());
  expect(refresh.querySelector('svg')).not.toHaveClass('ui-spin');
  expect(search).toHaveValue('刷新期间筛选草稿');
  expect(Object.values(counts())).toEqual([2, 2, 2, 2]);
  client.clear();
});

it('creates a PR schedule, finds it in the unified list, then edits and pauses the same plan', async () => {
  setup(); const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: '安排新任务' }));
  const template = await screen.findByRole('button', { name: '跟进 GitHub PR' });
  await waitFor(() => expect(template).toBeEnabled());
  await user.click(template);
  fireEvent.change(screen.getByLabelText('GitHub PR 地址 *'), { target: { value: 'https://github.com/example/project/pull/42' } });
  fireEvent.change(screen.getByLabelText('安排名称 *'), { target: { value: '检查产品 PR' } });
  await user.click(screen.getByRole('button', { name: '保存安排' }));
  expect(await screen.findByText('检查产品 PR')).toBeVisible();
  await user.click(screen.getByRole('button', { name: /全部任务/ }));
  fireEvent.change(screen.getByRole('textbox', { name: '搜索定时任务' }), { target: { value: '检查产品' } });
  const row = await screen.findByRole('button', { name: /检查产品 PR/ });
  await user.click(row);
  const dialog = await screen.findByRole('dialog');
  expect(within(dialog).getByRole('heading', { level: 2 })).toHaveTextContent('检查产品 PR');
  await user.click(within(dialog).getByRole('button', { name: '完成查看' }));
  await user.click(screen.getByRole('button', { name: '编辑安排' }));
  expect((screen.getByLabelText('到点后做什么 *') as HTMLTextAreaElement).value).toContain('https://github.com/example/project/pull/42');
  fireEvent.change(screen.getByLabelText('安排名称 *'), { target: { value: '复核产品 PR' } });
  await user.click(screen.getByRole('button', { name: '保存安排' }));
  expect(await screen.findByText('复核产品 PR')).toBeVisible();
  await user.click(screen.getByRole('button', { name: '暂停自动执行' }));
  expect(await screen.findByText('已暂停')).toBeVisible();
  await user.click(screen.getByRole('button', { name: '恢复自动执行' }));
  expect(await screen.findByRole('button', { name: '暂停自动执行' })).toBeEnabled();
});

it('manages persisted memory schedules through the settings owner', async () => {
  setup(); const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: /后台维护/ }));
  const control = await screen.findByRole('switch', { name: '启用自动整理记忆' });
  await waitFor(() => expect(control).toBeEnabled());
  await user.click(control);
  fireEvent.change(screen.getByRole('spinbutton', { name: '记忆目录整理频率' }), { target: { value: '14' } });
  await user.click(screen.getByRole('button', { name: '保存维护安排' }));
  expect(await screen.findByText('已保存并重新读取，后续任务会使用新的安排。')).toBeVisible();
  expect(control).not.toBeChecked();
  expect(screen.getByRole('spinbutton', { name: '记忆目录整理频率' })).toHaveValue(14);
});

it('retains unsaved maintenance and evaluation drafts when switching task groups', async () => {
  setup(); const user = userEvent.setup();
  const nav = within(screen.getByRole('navigation', { name: '任务类型' }));
  await user.click(nav.getByRole('button', { name: /后台维护/ }));
  fireEvent.change(await screen.findByRole('spinbutton', { name: '自动整理记忆频率' }), { target: { value: '4' } });
  await user.click(nav.getByRole('button', { name: /全部任务/ }));
  await user.click(nav.getByRole('button', { name: /后台维护/ }));
  expect(await screen.findByRole('spinbutton', { name: '自动整理记忆频率' })).toHaveValue(4);
  await user.click(nav.getByRole('button', { name: /周期评测/ }));
  await user.click(await screen.findByText('新建周期 Eval'));
  fireEvent.change(screen.getByRole('spinbutton', { name: 'Eval 周期间隔' }), { target: { value: '3' } });
  await user.click(nav.getByRole('button', { name: /全部任务/ }));
  await user.click(nav.getByRole('button', { name: /周期评测/ }));
  expect(screen.getByRole('spinbutton', { name: 'Eval 周期间隔' })).toHaveValue(3);
});

it('controls the selected periodic evaluation and reflects its returned state', async () => {
  setup(); const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: /周期评测/ }));
  await user.click(await screen.findByRole('button', { name: '暂停评测计划' }));
  expect(await screen.findByRole('button', { name: '恢复评测计划' })).toBeEnabled();
  await user.click(screen.getByRole('button', { name: '恢复评测计划' }));
  await user.click(await screen.findByRole('button', { name: '取消评测计划' }));
  expect(await screen.findAllByText('已取消')).not.toHaveLength(0);
  expect(screen.queryByRole('button', { name: '恢复评测计划' })).not.toBeInTheDocument();
});

it('does not manufacture enabled memory schedules from an empty response and validates a PR target', () => {
  expect(memoryScheduleRows(undefined, {})).toEqual([]);
  expect(isGithubPrUrl('https://github.com/example/project/pull/42')).toBe(true);
  expect(isGithubPrUrl('https://github.com.evil.invalid/example/project/pull/42')).toBe(false);
  expect(isGithubPrUrl('https://github.com/example/project/issues/42')).toBe(false);
});
