import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { createPreviewTransport } from '@/app/preview-control-transport';
import { TooltipProvider } from '@/components/primitives';
import { SchedulesApp } from './SchedulesApp';
import { memoryScheduleRows } from './schedule-model';
import { isGithubPrUrl } from '@/features/planning/AgentWakeSchedules';

afterEach(cleanup);
function setup() {
  const transport = createPreviewTransport();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><ControlTransportProvider transport={transport}><MemoryRouter><TooltipProvider><SchedulesApp /></TooltipProvider></MemoryRouter></ControlTransportProvider></QueryClientProvider>);
  return transport;
}

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
