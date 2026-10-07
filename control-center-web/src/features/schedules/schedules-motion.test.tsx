import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import { MotionActivityBoundary } from '@/design/motion';
import { MockControlTransport } from '@/test/mock-transport';
import { SchedulesApp } from './SchedulesApp';
import schedulesCss from './schedules.css?raw';

afterEach(() => { cleanup(); document.querySelector('[data-schedule-motion-test]')?.remove(); });

it('keeps the original pending refresh and search draft while quiet, then exposes the original failure and explicit retry', async () => {
  let fail!: (reason: Error) => void;
  let reads = 0;
  const transport = new MockControlTransport({ routes: {
    'agent.wakeSchedules.list': () => { reads += 1; return reads === 1 ? new Promise((_resolve, reject) => { fail = reject; }) : { ok: true, items: [] }; },
    'observability.evalSchedules.list': { schemaVersion: 'rag-ime.eval-schedule-list.v1', ok: true, items: [] },
    'configuration.settings': { settings: {} },
    'agent.memoryMaintenance.run': { ok: true, runs: [] },
  } });
  const query = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrap = (active: boolean) => <QueryClientProvider client={query}><ControlTransportProvider transport={transport}><MemoryRouter><TooltipProvider><MotionActivityBoundary active={active}><SchedulesApp /></MotionActivityBoundary></TooltipProvider></MemoryRouter></ControlTransportProvider></QueryClientProvider>;
  const style = document.createElement('style');
  style.dataset.scheduleMotionTest = ''; style.textContent = schedulesCss; document.head.append(style);
  const view = render(wrap(true));
  const main = view.container.querySelector('.schedule-app__content')!;
  expect(getComputedStyle(main).animation).not.toContain('schedule-view-enter');
  await waitFor(() => expect(reads).toBe(1));
  const search = screen.getByRole('textbox', { name: '搜索定时任务' });
  fireEvent.change(search, { target: { value: '未发送筛选稿' } }); search.focus();
  view.rerender(wrap(false));
  expect(view.container.querySelector('.schedule-app')).toHaveAttribute('data-motion-active', 'false');
  expect(screen.getByRole('button', { name: '刷新所有任务' })).toBeDisabled();
  expect(search).toHaveFocus(); expect(search).toHaveValue('未发送筛选稿'); expect(reads).toBe(1);
  view.rerender(wrap(true));
  expect(view.container.querySelector('.schedule-app__content')).toBe(main);
  expect(getComputedStyle(main).animation).not.toContain('schedule-view-enter');
  expect(search).toHaveFocus(); expect(reads).toBe(1);
  let visibility: DocumentVisibilityState = 'visible';
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => visibility });
  try {
    view.rerender(wrap(true));
    visibility = 'hidden'; fireEvent(document, new Event('visibilitychange'));
    expect(view.container.querySelector('.schedule-app')).toHaveAttribute('data-motion-active', 'false');
    expect(screen.getByRole('button', { name: '刷新所有任务' })).toBeDisabled();
    expect(search).toHaveValue('未发送筛选稿'); expect(reads).toBe(1);
    visibility = 'visible'; fireEvent(document, new Event('visibilitychange'));
    expect(view.container.querySelector('.schedule-app')).toHaveAttribute('data-motion-active', 'true');
  } finally { Reflect.deleteProperty(document, 'visibilityState'); }
  view.rerender(wrap(false));
  await act(async () => fail(new Error('原任务目录读取失败')));
  expect(await screen.findByText('原任务目录读取失败')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '刷新所有任务' })).toBeEnabled();
  fireEvent.click(screen.getByRole('button', { name: '重试' }));
  await waitFor(() => expect(reads).toBe(2));
  await waitFor(() => expect(screen.queryByText('原任务目录读取失败')).toBeNull());
  expect(search).toHaveValue('未发送筛选稿');
});
