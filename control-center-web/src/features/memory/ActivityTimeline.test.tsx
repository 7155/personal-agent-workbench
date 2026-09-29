import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import type {
  ControlRequest,
  ControlSubscription,
  ControlTransport,
} from '@/platform/transport';
import { ActivityTimeline } from './ActivityTimeline';
import { memoryQueryKeys } from './api';
import { PawOsDesktopProvider } from '@/features/paw-os/surface-context';
import { parseTraceAgentHandoff } from '@/features/trace-agent/handoff';

vi.mock('./MemorySteward', () => ({
  MemorySteward: () => <section aria-label="Memory 管家" />,
}));

afterEach(cleanup);

describe('ActivityTimeline activity projection', () => {
  it('previews the authoritative pending range before submitting catch-up once', async () => {
    const user = userEvent.setup();
    const requests: ControlRequest[] = [];
    renderTimeline(semanticTimeline(), { requests });

    const organize = await screen.findByRole('button', { name: '整理本月' });
    await user.click(organize);

    expect(requests.filter((request) => request.pathId === 'memory.activityTimeline.build')).toHaveLength(0);
    const preview = await screen.findByRole('dialog', { name: '整理本月' });
    expect(preview).toHaveTextContent('1 天待整理');
    expect(preview).toHaveTextContent('12 条来源');
    expect(preview).toHaveTextContent(/待整理日期示例/u);

    const confirm = within(preview).getByRole('button', { name: '确认整理本月' });
    await user.dblClick(confirm);
    await waitFor(() => expect(
      requests.filter((request) => request.pathId === 'memory.activityTimeline.build'),
    ).toHaveLength(1));
    expect(requests.find((request) => request.pathId === 'memory.activityTimeline.build')?.body).toEqual({
      date: localDateForTest(),
      rangeStartDate: `${localDateForTest().slice(0, 7)}-01`,
      throughToday: true,
    });
  });

  it('previews and submits only the selected old month instead of implying a cross-month catch-up', async () => {
    const user = userEvent.setup();
    const requests: ControlRequest[] = [];
    const oldMonth = shiftMonthForTest(localDateForTest().slice(0, 7), -2);
    const oldMonthEnd = lastDayOfMonthForTest(oldMonth);
    renderTimeline(semanticTimeline(), {
      initialDate: `${oldMonth}-01`,
      requests,
    });

    await user.click(await screen.findByRole('button', { name: '整理本月' }));
    const preview = await screen.findByRole('dialog', { name: '整理本月' });
    expect(preview).toHaveTextContent(`范围：${oldMonth}-01 至 ${oldMonthEnd}`);
    expect(preview).not.toHaveTextContent(formatDateForTest(localDateForTest()));

    await user.click(within(preview).getByRole('button', { name: '确认整理本月' }));
    await waitFor(() => expect(
      requests.find((request) => request.pathId === 'memory.activityTimeline.build')?.body,
    ).toEqual({
      date: oldMonthEnd,
      rangeStartDate: `${oldMonth}-01`,
      throughToday: true,
    }));
  });

  it('recovers the authoritative active job identity and disables duplicate organization', async () => {
    const requests: ControlRequest[] = [];
    renderTimeline(semanticTimeline(), {
      requests,
      calendarAutomation: {
        state: 'running',
        job: {
          jobId: 'memory-maintenance:existing-timeline',
          state: 'running',
          mode: 'manual_catch_up',
          progress: {
            phase: 'activity_timeline_catch_up',
            throughDate: localDateForTest(),
            completedDayCount: 2,
            totalDayCount: 5,
            remainingDayCount: 3,
            currentDate: `${localDateForTest().slice(0, 7)}-01`,
          },
        },
      },
      jobResult: {
        jobId: 'memory-maintenance:existing-timeline',
        state: 'running',
        progress: {
          phase: 'activity_timeline_catch_up',
          throughDate: localDateForTest(),
          completedDayCount: 2,
          totalDayCount: 5,
          remainingDayCount: 3,
          currentDate: `${localDateForTest().slice(0, 7)}-01`,
        },
      },
    });

    expect(await screen.findByRole(
      'button',
      { name: '正在整理' },
      { timeout: 3_000 },
    )).toBeDisabled();
    const status = screen.getByRole('status', { name: '历史日记整理进度' });
    expect(status).toHaveTextContent('已完成 2 / 5 天');
    expect(status).toHaveTextContent('任务 memory-maintenance:existing-timeline');
    expect(requests.filter((request) => request.pathId === 'memory.activityTimeline.build')).toHaveLength(0);
    expect(requests).toContainEqual(expect.objectContaining({
      pathId: 'agent.memoryMaintenance.run',
      query: { jobId: 'memory-maintenance:existing-timeline' },
    }));
  });

  it('replaces an old failed local job when the calendar reports a newer completed job', async () => {
    const user = userEvent.setup();
    const requests: ControlRequest[] = [];
    const today = localDateForTest();
    let latestJob: Record<string, unknown> = {};
    const view = renderTimeline(semanticTimeline(), {
      requests,
      calendarAutomation: () => ({ state: 'scheduled', job: latestJob }),
      jobResult: (jobId) => jobId === 'memory-maintenance:test'
        ? {
            jobId,
            state: 'failed',
            createdAtMs: 100,
            error: 'old job failed',
          }
        : {
            jobId,
            state: 'completed',
            createdAtMs: 200,
            progress: { currentDate: today, completedDayCount: 1, totalDayCount: 1 },
            result: { ok: true },
          },
    });

    await user.click(await screen.findByRole('button', { name: '整理本月' }));
    await user.click(within(await screen.findByRole('dialog', { name: '整理本月' }))
      .getByRole('button', { name: '确认整理本月' }));
    expect(await screen.findByRole('alert', { name: '历史日记整理进度' })).toHaveTextContent('任务 memory-maintenance:test');

    latestJob = {
      jobId: 'memory-maintenance:newer-completed',
      state: 'completed',
      mode: 'single_day',
      createdAtMs: 200,
      progress: { currentDate: today, completedDayCount: 1, totalDayCount: 1 },
      result: { ok: true },
    };
    await act(async () => {
      await view.client.invalidateQueries({ queryKey: memoryQueryKeys.activityTimelineCalendar(today.slice(0, 7)) });
    });

    const receipt = await screen.findByRole('status', { name: '历史日记整理进度' });
    expect(receipt).toHaveTextContent('任务 memory-maintenance:newer-completed');
    expect(screen.queryByRole('alert', { name: '历史日记整理进度' })).not.toBeInTheDocument();
    expect(requests).toContainEqual(expect.objectContaining({
      pathId: 'agent.memoryMaintenance.run',
      query: { jobId: 'memory-maintenance:newer-completed' },
    }));
  });

  it('shows a running catch-up spanning the selected older month', async () => {
    const oldMonth = shiftMonthForTest(localDateForTest().slice(0, 7), -2);
    const oldMonthStart = `${oldMonth}-01`;
    const jobId = 'memory-maintenance:cross-month-catch-up';
    renderTimeline(semanticTimeline(), {
      initialDate: oldMonthStart,
      calendarAutomation: {
        state: 'scheduled',
        job: {
          jobId,
          state: 'running',
          mode: 'manual_catch_up',
          progress: {
            phase: 'activity_timeline_catch_up',
            throughDate: localDateForTest(),
            currentDate: oldMonthStart,
            completedDayCount: 1,
            totalDayCount: 41,
          },
        },
      },
      jobResult: {
        jobId,
        state: 'running',
        progress: {
          phase: 'activity_timeline_catch_up',
          throughDate: localDateForTest(),
          currentDate: oldMonthStart,
          completedDayCount: 1,
          totalDayCount: 41,
        },
      },
    });

    const status = await screen.findByRole('status', { name: '历史日记整理进度' });
    expect(status).toHaveTextContent('已完成 1 / 41 天');
    expect(status).toHaveTextContent(`任务 ${jobId}`);
    expect(screen.getByRole('button', { name: '正在整理' })).toBeDisabled();
  });

  it('shows a traceable completed receipt and a failed-job recovery action', async () => {
    const completed = renderTimeline(semanticTimeline(), {
      calendarAutomation: {
        state: 'caught_up',
        job: {
          jobId: 'memory-maintenance:completed-timeline',
          state: 'completed',
          mode: 'manual_catch_up',
          progress: { throughDate: localDateForTest(), completedDayCount: 3, totalDayCount: 3 },
          result: { ok: true, completedDayCount: 3, remainingDayCount: 0 },
        },
      },
      jobResult: {
        jobId: 'memory-maintenance:completed-timeline',
        state: 'completed',
        progress: { throughDate: localDateForTest(), completedDayCount: 3, totalDayCount: 3 },
        result: { ok: true, completedDayCount: 3, remainingDayCount: 0 },
      },
    });

    const receipt = await screen.findByRole('status', { name: '历史日记整理进度' });
    expect(receipt).toHaveTextContent('整理完成：已完成 3 / 3 天，剩余 0 天');
    expect(receipt).toHaveTextContent('任务 memory-maintenance:completed-timeline');
    completed.unmount();

    const user = userEvent.setup();
    const routes: string[] = [];
    renderTimeline(semanticTimeline(), {
      routes,
      calendarAutomation: {
        state: 'retry_scheduled',
        job: {
          jobId: 'memory-maintenance:failed-timeline',
          state: 'failed',
          mode: 'manual_catch_up',
          progress: { throughDate: localDateForTest(), completedDayCount: 1, totalDayCount: 3 },
          error: 'selected memory model request failed: Session already has an active turn',
        },
      },
      jobResult: {
        jobId: 'memory-maintenance:failed-timeline',
        state: 'failed',
        traceId: 'trace:memory:memory-maintenance:failed-timeline',
        runId: 'memory-maintenance:failed-timeline',
        progress: { throughDate: localDateForTest(), completedDayCount: 1, totalDayCount: 3 },
        error: 'selected memory model request failed: Session already has an active turn',
      },
    });

    const failure = await screen.findByRole('alert', { name: '历史日记整理进度' });
    expect(failure).toHaveTextContent('任务 memory-maintenance:failed-timeline');
    expect(failure).toHaveTextContent('内部 Session 仍被上一回合占用');
    await user.click(within(failure).getByRole('button', { name: '交给 Trace Agent' }));
    const handoff = parseTraceAgentHandoff(routes.at(-1)?.split('?', 2)[1] ?? '');
    expect(handoff).toMatchObject({
      kind: 'memory',
      entityId: 'memory-maintenance:failed-timeline',
      traceId: 'trace:memory:memory-maintenance:failed-timeline',
      runId: 'memory-maintenance:failed-timeline',
      failureRef: 'memory-maintenance:failed-timeline',
      refs: { jobId: 'memory-maintenance:failed-timeline', state: 'failed' },
    });
    await user.click(within(failure).getByRole('button', { name: '重新检查范围' }));
    expect(await screen.findByRole('dialog', { name: '整理本月' })).toBeInTheDocument();
  });

  it('explains that a post-restart job expired and offers a fresh retry', async () => {
    const user = userEvent.setup();
    const jobId = 'memory-maintenance:after-gateway-restart';
    renderTimeline(semanticTimeline(), {
      calendarAutomation: {
        state: 'expired',
        job: {
          jobId,
          state: 'expired',
          mode: 'manual_catch_up',
          progress: { throughDate: localDateForTest() },
          errorCode: 'memory_maintenance_job_expired',
          error: 'Gateway restarted before this process-local Memory maintenance job could be read; the old job cannot be recovered.',
          recovery: {
            recoverable: false,
            retryable: true,
            action: 'trigger_new_job',
            reason: 'process_local_job_registry_lost',
          },
        },
      },
      jobResult: {
        jobId,
        state: 'expired',
        errorCode: 'memory_maintenance_job_expired',
        error: 'Gateway restarted before this process-local Memory maintenance job could be read; the old job cannot be recovered.',
        recovery: {
          recoverable: false,
          retryable: true,
          action: 'trigger_new_job',
          reason: 'process_local_job_registry_lost',
        },
      },
    });

    const expired = await screen.findByRole('alert', { name: '历史日记整理进度' });
    expect(expired).toHaveTextContent('Gateway 重启后旧的记忆整理任务已过期，无法恢复');
    expect(expired).toHaveTextContent(`任务 ${jobId}`);
    await user.click(within(expired).getByRole('button', { name: '重新检查范围' }));
    expect(await screen.findByRole('dialog', { name: '整理本月' })).toBeInTheDocument();
  });

  it('names the configured model mismatch in a failed month job', async () => {
    const error = 'memory curation requires the canonical live model openai-codex/gpt-5.6-luna; received openai-codex/gpt-6-luna';
    renderTimeline(semanticTimeline(), {
      calendarAutomation: {
        state: 'failed',
        job: {
          jobId: 'memory-maintenance:model-mismatch',
          state: 'failed',
          mode: 'manual_catch_up',
          error,
        },
      },
      jobResult: {
        jobId: 'memory-maintenance:model-mismatch',
        state: 'failed',
        error,
      },
    });

    const failure = await screen.findByRole('alert', { name: '历史日记整理进度' });
    expect(failure).toHaveTextContent('整理模型不匹配：本次使用 gpt-6-luna，执行器当时仅接受 gpt-5.6-luna');
    expect(failure).not.toHaveTextContent('读取或保存失败');
  });

  it('keeps semantic verification failures as a visible warning instead of a job error', async () => {
    renderTimeline(semanticTimeline(), {
      calendarAutomation: {
        state: 'caught_up',
        job: {
          jobId: 'memory-maintenance:semantic-warning',
          state: 'completed',
          mode: 'single_day',
          progress: { currentDate: localDateForTest(), completedDayCount: 1, totalDayCount: 1 },
          result: {
            ok: true,
            semanticOrganization: {
              status: 'warning',
              reviewRequired: true,
              error: 'Activity organization did not pass independent semantic verification',
            },
          },
        },
      },
      jobResult: {
        jobId: 'memory-maintenance:semantic-warning',
        state: 'completed',
        progress: { currentDate: localDateForTest(), completedDayCount: 1, totalDayCount: 1 },
        result: {
          ok: true,
          semanticOrganization: {
            status: 'warning',
            reviewRequired: true,
            error: 'Activity organization did not pass independent semantic verification',
          },
        },
      },
    });

    expect(await screen.findByText('语义整理待检查')).toBeInTheDocument();
    expect(screen.getByText(/Activity organization did not pass independent semantic verification/u)).toBeInTheDocument();
    expect(screen.queryByRole('alert', { name: '历史日记整理进度' })).not.toBeInTheDocument();
  });

  it('keeps the daily journal as the first-screen main pane with the calendar in the rail', async () => {
    const user = userEvent.setup();
    renderTimeline(semanticTimeline());

    const calendarControl = await screen.findByRole('button', { name: '上个月' });
    expect(calendarControl.closest('.activity-timeline__rail')).not.toBeNull();
    const journal = screen.getByRole('heading', { name: /的每日日记$/ }).closest('.daily-journal');
    expect(journal?.parentElement).toHaveClass('activity-timeline__main');
    expect(journal?.parentElement?.firstElementChild).toBe(journal);
    expect(await screen.findByText('上午完成账号切换与连续开发，下午验证记忆召回。')).toBeInTheDocument();
    expect(screen.getByText('当天应用')).not.toBeVisible();
    await user.click(screen.getByText('日记摘要与应用来源'));
    expect(screen.getByText('当天应用')).toBeVisible();

    await user.click(screen.getByRole('button', { name: '查看日记条目：验证三条记忆消费路径' }));
    expect(await screen.findByRole('dialog', { name: '验证三条记忆消费路径' })).toBeInTheDocument();
  });

  it('uses the month calendar as the entry point to a daily timeline', async () => {
    const user = userEvent.setup();
    renderTimeline(semanticTimeline());

    const calendar = (await screen.findByRole('button', { name: '上个月' })).closest('.activity-calendar');
    expect(calendar).not.toBeNull();
    await waitFor(() => {
      expect(calendar).toHaveTextContent('2 天有活动');
      expect(calendar).toHaveTextContent('1 天已整理');
    });
    const waitingDay = screen.getByRole('button', { name: /待整理，12 条来源/ });
    await user.click(waitingDay);
    expect(waitingDay).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText('点击日期，直接查看当天时间线和来源。')).toBeInTheDocument();
  });

  it('does not present a heuristic approved timeline as a semantic journal', async () => {
    renderTimeline(semanticTimeline(), { organized: false });

    expect(await screen.findByText('这一天还没有整理成日记')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '整理这一天' })).toBeInTheDocument();
    expect(screen.getByText('待整理成日记')).toBeInTheDocument();
    expect(screen.queryByText('上午完成账号切换与连续开发，下午验证记忆召回。')).not.toBeInTheDocument();
  });

  it('keeps a cross-app morning activity together and reveals its evidence chain', async () => {
    const user = userEvent.setup();
    renderTimeline(semanticTimeline());

    expect(await screen.findByText('2 项活动')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '上午' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '下午' })).toBeInTheDocument();

    const task = screen.getByRole('button', { name: '查看任务：切换 Codex 账号并继续开发' });
    expect(within(task).getByText('Terminal')).toBeInTheDocument();
    expect(within(task).getByText('Codex')).toBeInTheDocument();
    expect(within(task).getByText('8 条来源')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: /查看任务：/ })).toHaveLength(2);

    await user.click(task);
    const dialog = await screen.findByRole('dialog', { name: '切换 Codex 账号并继续开发' });
    expect(within(dialog).getAllByText('通过 cas 切换到工作账号。')).toHaveLength(1);

    const eventSummary = within(dialog).getByText('事件 1').closest('summary');
    expect(eventSummary).not.toBeNull();
    await user.click(eventSummary!);
    const eventDetails = eventSummary!.closest('details');
    expect(eventDetails).not.toBeNull();
    expect(within(eventDetails!).getAllByText('通过 cas 切换到工作账号。')).toHaveLength(2);

    const referenceSummary = within(eventDetails!).getByText('相关来源 · 1').closest('summary');
    expect(referenceSummary).not.toBeNull();
    await user.click(referenceSummary!);
    expect(within(eventDetails!).getByText(/input_events\/101/)).toBeVisible();
    expect(within(eventDetails!).getByText('Terminal 命令记录')).toBeVisible();

    await user.click(within(eventDetails!).getByRole('button', { name: /Terminal 命令记录/ }));
    const referenceDialog = await screen.findByRole('dialog', { name: 'Terminal 命令记录' });
    expect(within(referenceDialog).getByText('通过 cas 切换到工作账号。')).toBeVisible();
    expect(within(referenceDialog).getByRole('region', { name: '整理使用的输入上下文' })).toBeVisible();
    expect(within(referenceDialog).getByText('准备切换工作账号')).toBeVisible();
  });

  it('uses a compact 24-hour density track with separate readable summaries', async () => {
    const user = userEvent.setup();
    const timeline = semanticTimeline();
    const tasks = timeline.semanticTasks as Array<Record<string, unknown>>;
    const [year, month, day] = localDateForTest().split('-').map(Number);
    const dayStart = new Date(year, month - 1, day, 0, 0).getTime();
    tasks[0]!.startMs = dayStart + 9 * 3_600_000;
    tasks[0]!.endMs = dayStart + 11 * 3_600_000;
    tasks[1]!.startMs = dayStart + 14 * 3_600_000;
    tasks[1]!.endMs = dayStart + 15.5 * 3_600_000;
    tasks.push({
      segmentId: 'semantic-task:unclassified',
      title: '补充未归类来源',
      period: 'day',
      startMs: dayStart,
      endMs: dayStart + 24 * 3_600_000,
      activityKind: 'unclassified_activity',
      summary: '来源尚未形成可靠的时间归类。',
      apps: [],
      eventCount: 1,
      evidenceCount: 1,
    });
    renderTimeline(timeline);

    const map = await screen.findByRole('region', { name: `${localDateForTest()} 活动分布` });
    const density = within(map).getByRole('list', { name: '24 小时活动密度轨道' });
    const summary = within(map).getByRole('list', { name: '活动摘要' });
    const morningMarker = within(density).getByRole('button', { name: /查看活动：切换 Codex 账号并继续开发/u });
    const unclassifiedMarker = within(density).getByRole('button', { name: /查看活动：补充未归类来源/u });

    expect(morningMarker).toBeEmptyDOMElement();
    expect(unclassifiedMarker).toHaveAttribute('data-unclassified', 'true');
    expect(within(summary).getByText('切换 Codex 账号并继续开发')).toBeVisible();
    expect(within(summary).getByText('补充未归类来源')).toBeVisible();

    await user.click(morningMarker);
    expect(await screen.findByRole('dialog', { name: '切换 Codex 账号并继续开发' })).toBeInTheDocument();
  });

  it('keeps a long daily journal bounded until the reader explicitly expands it', async () => {
    const user = userEvent.setup();
    const timeline = semanticTimeline();
    const fullStory = [
      '上午完成账号切换与连续开发，下午验证记忆召回。',
      '这段补充说明用于确认时间线日记不会把长篇原始整理内容直接撑满首屏。',
      '完整内容仍应在用户选择展开后可读，并且当天来源入口始终保留。',
    ].join(' ').repeat(5);
    timeline.summary = fullStory;
    renderTimeline(timeline);

    const story = await screen.findByTestId('daily-journal-story');
    const expand = screen.getByRole('button', { name: '展开完整日记' });
    expect(story).toHaveAttribute('data-expanded', 'false');
    expect(expand).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByRole('button', { name: '查看日记与当天来源' })).toBeVisible();

    await user.click(expand);
    expect(story).toHaveAttribute('data-expanded', 'true');
    expect(screen.getByRole('button', { name: '收起完整日记' })).toHaveAttribute('aria-expanded', 'true');
    expect(story).toHaveTextContent(fullStory);
  });

  it('shows source-bound insights after the journal and opens conversation on request', async () => {
    const user = userEvent.setup();
    const timeline = semanticTimeline();
    const tasks = timeline.semanticTasks as Array<Record<string, unknown>>;
    tasks[0]!.title = '继续完成记忆召回方案';
    tasks[0]!.summary = '当前实现仍待继续完成，先核对来源后再安排下一步。';
    tasks[0]!.evidenceCount = 8;
    renderTimeline(timeline);

    const canvas = await screen.findByRole('region', { name: '当天的记忆线索' });
    const journal = screen.getByRole('region', { name: /每日日记/u });
    expect(screen.getByRole('region', { name: 'Memory 管家' })).not.toBeVisible();
    await user.click(screen.getByText('与记忆管家聊这一天'));
    const steward = screen.getByRole('region', { name: 'Memory 管家' });
    expect(steward).toBeVisible();

    expect(canvas).toHaveTextContent('值得回看的线索');
    expect(await within(canvas).findByText('未完成')).toBeVisible();
    expect(within(canvas).getByText('继续完成记忆召回方案')).toBeVisible();
    expect(within(canvas).getByRole('button', { name: '查看未完成线索：继续完成记忆召回方案' })).toHaveTextContent('8 条来源');
    expect(within(canvas).getByText('线索来自当天已整理活动，点击可核对原文。')).toBeVisible();
    expect(journal.compareDocumentPosition(canvas) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(journal.compareDocumentPosition(steward) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    await user.click(within(canvas).getByRole('button', { name: /查看未完成线索：继续完成记忆召回方案/u }));
    expect(await screen.findByRole('dialog', { name: '继续完成记忆召回方案' })).toBeInTheDocument();
  });

  it('does not invent a memory insight when organized activities have no explicit signal', async () => {
    renderTimeline(semanticTimeline());

    expect(await screen.findByText('上午完成账号切换与连续开发，下午验证记忆召回。')).toBeVisible();
    expect(screen.queryByRole('region', { name: '当天的记忆线索' })).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: '记忆画布' })).not.toBeInTheDocument();
  });

  it('distinguishes an empty day from a populated library and offers browsing without starting organization', async () => {
    const user = userEvent.setup();
    const requests: ControlRequest[] = [];
    const onBrowseMemories = vi.fn();
    const onOpenOrganize = vi.fn();
    const recentDate = `${shiftMonthForTest(localDateForTest().slice(0, 7), -1)}-14`;
    renderTimeline({}, {
      requests,
      library: { memories: 259, topics: 76, pendingSources: 882 },
      latestTimelineDate: recentDate,
      onBrowseMemories,
      onOpenOrganize,
    });

    expect(await screen.findByText('这一天还没有日记')).toBeVisible();
    expect(screen.getByText('记忆库中已有 259 条记忆、76 个主题，可以继续查找。')).toBeVisible();
    expect(screen.getByText('882 条来源等待整理')).toBeVisible();
    expect(screen.queryByRole('region', { name: '记忆画布' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '浏览全部记忆' }));
    expect(onBrowseMemories).toHaveBeenCalledOnce();
    await user.click(screen.getByRole('button', { name: '查看整理与审核' }));
    expect(onOpenOrganize).toHaveBeenCalledOnce();
    await user.click(screen.getByRole('button', { name: `回看 ${recentDate.slice(5)}` }));
    await waitFor(() => expect(requests).toContainEqual(expect.objectContaining({
      pathId: 'memory.activityTimeline.get', query: { date: recentDate },
    })));
    expect(requests.some((request) => request.pathId === 'memory.activityTimeline.build')).toBe(false);
  });

  it('keeps nested timeline details mounted through their closing transition', async () => {
    const user = userEvent.setup();
    renderTimeline(semanticTimeline());

    await user.click(await screen.findByRole('button', { name: '查看任务：切换 Codex 账号并继续开发' }));
    const dialog = await screen.findByRole('dialog', { name: '切换 Codex 账号并继续开发' });
    const eventSummary = within(dialog).getByText('事件 1').closest('summary');
    expect(eventSummary).not.toBeNull();
    await user.click(eventSummary!);
    const eventDetails = eventSummary!.closest('details');
    expect(eventDetails).toHaveAttribute('open');

    const sourceSummary = within(eventDetails!).getByText('相关来源 · 1').closest('summary');
    expect(sourceSummary).not.toBeNull();
    await user.click(sourceSummary!);
    const sourceDetails = sourceSummary!.closest('details');
    expect(sourceDetails).toHaveAttribute('open');
    expect(within(sourceDetails!).getByText('Terminal 命令记录')).toBeVisible();

    await user.click(sourceSummary!);
    expect(sourceSummary).toHaveAttribute('aria-expanded', 'false');
    expect(sourceDetails).toHaveAttribute('open');
    expect(within(sourceDetails!).getByText('Terminal 命令记录')).toBeInTheDocument();
    await waitFor(() => {
      expect(within(eventDetails!).queryByText('Terminal 命令记录')).not.toBeInTheDocument();
      expect(sourceDetails).not.toHaveAttribute('open');
    });

    await user.click(eventSummary!);
    expect(eventSummary).toHaveAttribute('aria-expanded', 'false');
    expect(eventDetails).toHaveAttribute('open');
    expect(eventDetails!.querySelector('.activity-timeline__event-body > p')).toBeInTheDocument();
    await waitFor(() => {
      expect(eventDetails!.querySelector('.activity-timeline__event-body > p')).toBeNull();
      expect(eventDetails).not.toHaveAttribute('open');
    });
  });

  it('falls back to legacy segments and preserves expandable source event identities', async () => {
    const user = userEvent.setup();
    renderTimeline(legacyTimeline());

    expect(await screen.findByText('1 项活动')).toBeInTheDocument();
    expect(screen.queryByText('1 个时段')).not.toBeInTheDocument();
    const task = screen.getByRole('button', { name: /查看任务：实现旧时间线兼容/ });
    await user.click(task);

    const dialog = await screen.findByRole('dialog', { name: '实现旧时间线兼容' });
    const eventSummary = within(dialog).getByText('事件 1').closest('summary');
    expect(eventSummary).not.toBeNull();
    await user.click(eventSummary!);
    expect(within(dialog).getByText('记录 #201')).toBeInTheDocument();
    expect(within(dialog).getByText('这条记录暂时没有可打开的来源。')).toBeVisible();
    expect(dialog).toHaveTextContent('分类未标注');
  });

  it('projects a minute-scale production fragment as ordinary activity with reference-only evidence', async () => {
    const user = userEvent.setup();
    renderTimeline(evidenceRefTimeline());

    expect(await screen.findByText('1 项活动')).toBeInTheDocument();
    const task = screen.getByRole('button', { name: '查看任务：切换账号并继续记忆系统开发' });
    expect(within(task).getByText('Terminal')).toBeInTheDocument();
    expect(within(task).getByText('Codex')).toBeInTheDocument();
    expect(within(task).getByText('2 条来源')).toBeInTheDocument();
    expect(within(task).getAllByText('普通活动').length).toBeGreaterThan(0);
    expect(within(task).queryByText('长时聚合')).not.toBeInTheDocument();

    await user.click(task);
    const dialog = await screen.findByRole('dialog', { name: '切换账号并继续记忆系统开发' });
    const firstEvent = within(dialog).getByText('事件 1').closest('summary');
    expect(firstEvent).not.toBeNull();
    await user.click(firstEvent!);
    const firstEventDetails = firstEvent!.closest('details');
    expect(firstEventDetails).not.toBeNull();
    expect(within(firstEventDetails!).getByText('这条记录暂时没有可展示的摘要。')).toBeVisible();
    expect(within(firstEventDetails!).getByRole('button', { name: '打开来源记录' })).toBeVisible();
  });

  it('keeps every event reachable when a task has more than the initial evidence window', async () => {
    const user = userEvent.setup();
    const timeline = semanticTimeline();
    const tasks = timeline.semanticTasks as Array<Record<string, unknown>>;
    const baseEvent = (tasks[0]!.events as Array<Record<string, unknown>>)[0]!;
    tasks[0]!.events = Array.from({ length: 30 }, (_, index) => ({
      ...baseEvent,
      eventId: 1_000 + index,
      summary: `批量事件 ${index + 1}`,
      sourceRefs: [],
    }));
    tasks[0]!.eventCount = 30;
    renderTimeline(timeline);

    await user.click(await screen.findByRole('button', { name: '查看任务：切换 Codex 账号并继续开发' }));
    const dialog = await screen.findByRole('dialog', { name: '切换 Codex 账号并继续开发' });
    expect(within(dialog).getByText('事件 24')).toBeInTheDocument();
    expect(within(dialog).queryByText('事件 25')).not.toBeInTheDocument();
    const more = within(dialog).getByRole('button', { name: '再显示 6 条事件，当前 24 / 30' });
    await user.click(more);
    expect(within(dialog).getByText('事件 30')).toBeInTheDocument();
    expect(within(dialog).queryByRole('button', { name: /再显示/ })).not.toBeInTheDocument();
  });

  it('groups a legacy task crossing noon as all-day and labels duration as a span', async () => {
    renderTimeline(crossPeriodTimeline());

    expect(await screen.findByRole('heading', { name: '全天' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '上午' })).not.toBeInTheDocument();
    expect(screen.getByText('记录时间范围合计')).toBeInTheDocument();
    expect(screen.queryByText('活跃时长')).not.toBeInTheDocument();
    const task = screen.getByRole('button', { name: '查看任务：持续优化输入法记忆召回' });
    expect(within(task).getByText('长时聚合 · 记录时间范围 6 小时')).toBeInTheDocument();
  });
});

function renderTimeline(
  timeline: Record<string, unknown>,
  options: TimelineTransportOptions = {},
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const view = render(
    <PawOsDesktopProvider openRoute={(route) => options.routes?.push(route)} openWindow={() => undefined}>
      <TooltipProvider delayDuration={0}>
        <ControlTransportProvider transport={timelineTransport(timeline, options)}>
          <QueryClientProvider client={client}>
            <ActivityTimeline
              initialDate={options.initialDate}
              library={options.library}
              latestTimelineDate={options.latestTimelineDate}
              onBrowseMemories={options.onBrowseMemories}
              onOpenOrganize={options.onOpenOrganize}
            />
          </QueryClientProvider>
        </ControlTransportProvider>
      </TooltipProvider>
    </PawOsDesktopProvider>,
  );
  return { ...view, client };
}

function timelineTransport(
  timeline: Record<string, unknown>,
  options: TimelineTransportOptions = {},
): ControlTransport {
  return {
    kind: 'mock',
    capabilities: async () => ({
      schemaVersion: 'rag-ime.control-frontend-capabilities.v1',
      transport: 'mock',
      routeIds: [
        'memory.activityTimeline.get',
        'memory.activityTimeline.calendar',
        'memory.activityTimeline.build',
        'memory.activityTimeline.approve',
        'memory.activityTimeline.reject',
        'memory.reference.get',
        'agent.memoryMaintenance.run',
      ],
      features: {},
      native: {},
    }),
    request: async <Response,>(request: ControlRequest) => {
      options.requests?.push(request);
      if (request.pathId === 'agent.sessions.list') {
        return { ok: true, items: [] } as Response;
      }
      if (request.pathId === 'memory.activityTimeline.get') {
        return { ok: true, timeline } as Response;
      }
      if (request.pathId === 'memory.activityTimeline.calendar') {
        const month = String(request.query?.month ?? new Date().toISOString().slice(0, 7));
        const today = localDateForTest();
        const waitingDate = `${month}-01`;
        return {
          schemaVersion: 'rag-ime.activity-timeline-calendar.v1',
          ok: true,
          month,
          summary: {
            sourceEventCount: 24,
            activityDayCount: 2,
            organizedDayCount: 1,
            approvedDayCount: 1,
            draftDayCount: 0,
            waitingDayCount: 1,
            outdatedDayCount: 0,
          },
          ...(options.calendarAutomation ? { automation: typeof options.calendarAutomation === 'function'
            ? options.calendarAutomation()
            : options.calendarAutomation } : {}),
          days: [
            { date: today, status: 'approved', organized: options.organized ?? true, modelOrganized: options.organized ?? true, needsRefresh: false, sourceEventCount: 12, segmentCount: 2 },
            { date: waitingDate, status: 'none', organized: false, modelOrganized: false, needsRefresh: false, sourceEventCount: 12, segmentCount: 0 },
          ],
        } as Response;
      }
      if (request.pathId === 'memory.reference.get') {
        return {
          ok: true,
          item: {
            id: String(request.params?.referenceId ?? ''),
            title: 'Terminal 命令记录',
            text: '通过 cas 切换到工作账号。',
            status: 'active',
            occurredAtMs: new Date(2026, 6, 18, 9, 0).getTime(),
            sourceContextAvailable: true,
            sourceContext: {
              recentContext: '准备切换工作账号',
              preedit: 'cas codex',
              redacted: false,
              scopeProject: 'wisdom-weasel-rag-ime',
              usedFor: ['source_fingerprint', 'semantic_grouping'],
            },
          },
          source: { kind: 'input_event', id: String(request.params?.referenceId ?? '') },
          evidenceRefs: [],
        } as Response;
      }
      if (request.pathId === 'memory.activityTimeline.build') {
        return {
          schemaVersion: 'rag-ime.gateway-memory-maintenance-job.v1',
          ok: true,
          jobId: 'memory-maintenance:test',
          state: 'queued',
          createdAtMs: 100,
        } as Response;
      }
      if (request.pathId === 'agent.memoryMaintenance.run') {
        return (typeof options.jobResult === 'function'
          ? options.jobResult(String(request.query?.jobId ?? ''))
          : options.jobResult ?? {
          schemaVersion: 'rag-ime.gateway-memory-maintenance-job.v1',
          ok: true,
          jobId: 'memory-maintenance:test',
          state: 'completed',
          result: { ok: true },
        }) as Response;
      }
      throw new Error(`Unexpected request: ${request.pathId}`);
    },
    subscribe: (_request: ControlSubscription) => () => undefined,
  } as unknown as ControlTransport;
}

interface TimelineTransportOptions {
  library?: { memories: number; topics: number; pendingSources: number };
  latestTimelineDate?: string;
  onBrowseMemories?: () => void;
  onOpenOrganize?: () => void;
  initialDate?: string;
  organized?: boolean;
  calendarAutomation?: Record<string, unknown> | (() => Record<string, unknown>);
  jobResult?: Record<string, unknown> | ((jobId: string) => Record<string, unknown>);
  requests?: ControlRequest[];
  routes?: string[];
}

function shiftMonthForTest(monthValue: string, offset: number): string {
  const [year, month] = monthValue.split('-').map(Number);
  const value = new Date(year, month - 1 + offset, 1);
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}`;
}

function lastDayOfMonthForTest(monthValue: string): string {
  const [year, month] = monthValue.split('-').map(Number);
  const day = new Date(year, month, 0).getDate();
  return `${monthValue}-${String(day).padStart(2, '0')}`;
}

function formatDateForTest(value: string): string {
  const [year, month, day] = value.split('-').map(Number);
  return new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: 'long', day: 'numeric' })
    .format(new Date(year, month - 1, day));
}

function localDateForTest() {
  const value = new Date();
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`;
}

function semanticTimeline(): Record<string, unknown> {
  const start = new Date(2026, 6, 18, 9, 0).getTime();
  return {
    timelineId: 'timeline:semantic',
    date: '2026-07-18',
    timezone: 'Asia/Shanghai',
    status: 'draft',
    sourceEventHash: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    summary: '上午完成账号切换与连续开发，下午验证记忆召回。',
    eventCount: 12,
    segmentCount: 2,
    ordinaryActivityCount: 0,
    consolidatedActivityCount: 2,
    updatedAtMs: start + 8 * 3_600_000,
    semanticTasks: [
      {
        segmentId: 'semantic-task:account-and-codex',
        title: '切换 Codex 账号并继续开发',
        period: 'morning',
        startMs: start,
        endMs: start + 2 * 3_600_000,
        activityKind: 'consolidated_activity',
        summary: '在 Terminal 通过 cas 切换 Codex 账号，随后回到 Codex 延续同一项开发工作。',
        apps: [
          { bundleId: 'com.apple.Terminal', name: 'Terminal', eventCount: 2 },
          { bundleId: 'com.openai.codex', name: 'Codex', eventCount: 6 },
        ],
        eventCount: 8,
        evidenceCount: 8,
        sourceEventIds: [101, 102],
        sourceKinds: ['terminal', 'pi_agent'],
        contextGroupIds: ['group:memory-v2'],
        redactedEventCount: 0,
        events: [
          {
            eventId: 101,
            occurredAtMs: start,
            app: { bundleId: 'com.apple.Terminal', name: 'Terminal' },
            sourceKind: 'terminal',
            summary: '通过 cas 切换到工作账号。',
            redacted: false,
            sourceRefs: [
              {
                refId: 'input-event:101',
                kind: 'input_event',
                label: 'Terminal 命令记录',
                locator: 'input_events/101',
              },
            ],
          },
          {
            eventId: 102,
            occurredAtMs: start + 20 * 60_000,
            app: { bundleId: 'com.openai.codex', name: 'Codex' },
            sourceKind: 'pi_agent',
            summary: '继续完成语义时间线方案。',
            sourceRefs: [],
          },
        ],
      },
      {
        segmentId: 'semantic-task:verification',
        title: '验证三条记忆消费路径',
        period: 'afternoon',
        startMs: start + 5 * 3_600_000,
        endMs: start + 6.5 * 3_600_000,
        summary: '验证生成、会话首次注入和 Agent Tools 的召回。',
        activityKind: 'consolidated_activity',
        apps: [{ bundleId: 'com.mitchellh.ghostty', name: 'Ghostty', eventCount: 4 }],
        eventCount: 4,
        evidenceCount: 4,
        sourceEventIds: [201],
        redactedEventCount: 0,
        events: [],
      },
    ],
  };
}

function evidenceRefTimeline(): Record<string, unknown> {
  const start = new Date(2026, 6, 18, 9, 0).getTime();
  return {
    timelineId: 'timeline:evidence-refs',
    date: '2026-07-18',
    timezone: 'Asia/Shanghai',
    status: 'draft',
    sourceEventHash: 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
    summary: '上午围绕同一项记忆系统工作，在 Terminal 和 Codex 之间切换。',
    eventCount: 2,
    segmentCount: 1,
    ordinaryActivityCount: 1,
    consolidatedActivityCount: 0,
    updatedAtMs: start + 2 * 3_600_000,
    segments: [
      {
        segmentId: 'semantic-task:production-evidence-refs',
        title: '切换账号并继续记忆系统开发',
        startMs: start,
        endMs: start + 20 * 60_000,
        activityKind: 'ordinary_activity',
        summary: '通过 cas 切换账号后继续完成记忆系统开发。',
        apps: ['com.apple.Terminal', 'com.openai.codex'],
        eventCount: 2,
        evidenceRefs: [
          {
            sourceType: 'input_event',
            sourceId: 'event:501',
            eventId: 501,
            app: 'com.apple.Terminal',
            sourceKind: 'terminal',
            occurredAtMs: start,
            redacted: false,
          },
          {
            sourceType: 'input_event',
            sourceId: 'event:502',
            eventId: 502,
            app: 'com.openai.codex',
            sourceKind: 'pi_agent',
            occurredAtMs: start + 20 * 60_000,
            redacted: false,
          },
        ],
      },
    ],
  };
}

function crossPeriodTimeline(): Record<string, unknown> {
  const start = new Date(2026, 6, 18, 9, 0).getTime();
  return {
    timelineId: 'timeline:cross-period',
    date: '2026-07-18',
    timezone: 'Asia/Shanghai',
    status: 'draft',
    sourceEventHash: 'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
    summary: '上午到下午持续优化输入法记忆召回。',
    eventCount: 2,
    segmentCount: 1,
    ordinaryActivityCount: 0,
    consolidatedActivityCount: 1,
    updatedAtMs: start + 6 * 3_600_000,
    segments: [
      {
        segmentId: 'semantic-task:cross-period',
        title: '持续优化输入法记忆召回',
        startMs: start,
        endMs: start + 6 * 3_600_000,
        activityKind: 'consolidated_activity',
        summary: '上午开始排查，下午完成验证。',
        apps: ['com.openai.codex'],
        eventCount: 2,
        sourceEventIds: [601, 602],
        redactedEventCount: 0,
      },
    ],
  };
}

function legacyTimeline(): Record<string, unknown> {
  const start = new Date(2026, 6, 18, 10, 0).getTime();
  return {
    timelineId: 'timeline:legacy',
    date: '2026-07-18',
    timezone: 'Asia/Shanghai',
    status: 'approved',
    sourceEventHash: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    summary: '旧时间线载荷仍然可以阅读。',
    eventCount: 1,
    segmentCount: 1,
    ordinaryActivityCount: 0,
    consolidatedActivityCount: 0,
    updatedAtMs: start,
    segments: [
      {
        segmentId: 'segment:legacy',
        app: 'com.openai.codex',
        sourceKinds: ['pi_agent'],
        contextGroupIds: ['group:compatibility'],
        startMs: start,
        endMs: start + 30 * 60_000,
        eventCount: 1,
        sourceEventIds: [201],
        summary: '实现旧时间线兼容。',
        redactedEventCount: 0,
      },
    ],
  };
}
