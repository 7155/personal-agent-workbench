import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport } from '@/test/mock-transport';
import { previewPersonas } from '@/features/agent/preview-data';
import { PawOsAppSurfaceProvider, PawOsDesktopProvider } from '@/features/paw-os/surface-context';
import { MemoryFeature } from './index';

afterEach(() => {
  cleanup();
  window.history.replaceState(null, '', '/');
});

describe('MemoryFeature relations', () => {
  it('keeps the organize title independent from a stale summary backlog', async () => {
    const transport = new MockControlTransport({ routes: {
      'memory.summary': { pendingCompileEvents: 0, pendingGovernedEvidenceCount: 0 },
      'agent.memoryMaintenance.run': { ok: true, policy: 'auto_governed', autoApply: true, ownerCuration: { pendingSourceCount: 310 }, runs: [] },
    } });
    renderMemory(transport, '/memory?view=organize');
    expect(await screen.findByText('310 条待整理')).toBeInTheDocument();
    expect(screen.getByText('核对来源、继续整理和查看本批结果')).toBeInTheDocument();
    expect(screen.queryByText('0 条待整理')).not.toBeInTheDocument();
  });

  it('opens the topic library on active topics and keeps historical filters available', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({ routes: {
      'memory.summary': { memoryBookCount: 87 },
      'memory.pages': { items: [], nextCursor: '' },
    } });
    renderMemory(transport, '/memory?layer=books');
    await waitFor(() => expect(transport.requests.some(({ request }) => (
      request.pathId === 'memory.pages' && request.params?.kind === 'books' && request.query?.status === 'active'
    ))).toBe(true));
    await user.click(screen.getByRole('combobox', { name: '状态' }));
    await user.click(await screen.findByRole('option', { name: '已归档' }));
    await waitFor(() => expect(transport.requests.some(({ request }) => (
      request.pathId === 'memory.pages' && request.query?.status === 'archived'
    ))).toBe(true));
  });

  it('consolidates the existing catalog independently from pending source curation', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({ routes: {
      'memory.summary': { memoryBookCount: 87 },
      'memory.pages': { items: [], nextCursor: '' },
      'agent.memoryMaintenance.run': (request: ControlRequest) => request.query?.jobId
        ? { jobId: 'catalog-job', state: 'completed', catalogOnly: true, result: { ok: true, catalogOnly: true } }
        : { ok: true, policy: 'auto_governed', autoApply: true, runs: [], ownerCuration: { pendingSourceCount: 906 }, catalogConsolidation: { enabled: true } },
      'agent.memoryMaintenance.trigger': { ok: true, jobId: 'catalog-job', state: 'queued', catalogOnly: true },
    } });
    renderMemory(transport, '/memory?view=organize');
    await user.click(await screen.findByRole('button', { name: '整理已有主题' }));
    const calls = transport.requests.filter(({ request }) => request.pathId === 'agent.memoryMaintenance.trigger');
    expect(calls).toHaveLength(1);
    expect(calls[0].request.body).toMatchObject({ catalogOnly: true, manual: true });
    expect(calls[0].request.body).not.toHaveProperty('maxSources');
    expect(await screen.findByText('已有主题整理完成')).toBeInTheDocument();
  });

  it('opens the existing catalog from an empty day and searches without creating a model task', async () => {
    const user = userEvent.setup();
    const date = localCalendarDate();
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': {
          ok: true,
          currentAtomCount: 259,
          memoryBookCount: 76,
          memoryEvidenceCount: 143,
          pendingGovernedEvidenceCount: 882,
        },
        'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
        'memory.activityTimeline.get': { ok: true, timeline: null },
        'memory.activityTimeline.calendar': { ok: true, month: date.slice(0, 7), days: [], summary: {} },
        'agent.sessions.list': { ok: true, items: [] },
      },
    });
    renderMemory(transport, '/memory?view=timeline', true);

    expect(await screen.findByText('这一天还没有日记')).toBeVisible();
    expect(screen.getByText('记忆库中已有 259 条记忆、76 个主题，可以继续查找。')).toBeVisible();
    expect(screen.queryByRole('combobox', { name: '记忆页面' })).not.toBeInTheDocument();
    expect(screen.queryByRole('list', { name: '记忆内容分类' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '浏览全部记忆' }));
    const catalog = await screen.findByRole('list', { name: '记忆内容分类' });
    expect(within(catalog).getByRole('button', { name: '记忆 · 已整理记忆 · 259 项' })).toHaveAttribute('aria-current', 'page');
    await user.type(screen.getByRole('textbox', { name: '搜索' }), '偏好');
    await waitFor(() => expect(transport.requests.some(({ request }) => (
      request.pathId === 'memory.pages' && request.params?.kind === 'atoms' && request.query?.query === '偏好'
    ))).toBe(true));
    expect(transport.requests.some(({ request }) => [
      'agent.sessions.surface.ensure', 'agent.session.prompt', 'memory.activityTimeline.build', 'agent.memoryMaintenance.trigger',
    ].includes(request.pathId))).toBe(false);
  });

  it('shows automatic timeline publication and keeps an immediate manual control', async () => {
    const user = userEvent.setup();
    const date = localCalendarDate();
    let status = 'draft';
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': {
          ok: true,
          memoryBookCount: 3,
          memoryAtomCount: 8,
          activityTimelineCounts: { draft: 1 },
          roleBookRevisionCounts: { active: 1 },
          projection: { fresh: true, retrievalDocuments: 12, backlog: 0 },
        },
        'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
        'memory.activityTimeline.get': () => ({ ok: true, timeline: activityTimeline(status, date) }),
        'memory.activityTimeline.calendar': () => ({
          ok: true,
          month: date.slice(0, 7),
          days: [{
            date,
            status,
            organized: true,
            modelOrganized: true,
            needsRefresh: false,
            sourceEventCount: 8,
            segmentCount: 2,
          }],
          summary: {
            activityDayCount: 1,
            organizedDayCount: 1,
            waitingDayCount: 0,
            sourceEventCount: 8,
          },
        }),
        'memory.activityTimeline.approve': (request: ControlRequest) => {
          expect(request.body).toEqual({
            timelineId: `timeline:${date}`,
            expectedSourceEventHash: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            confirmText: 'approve',
          });
          status = 'approved';
          return { ok: true, decision: 'accepted', timeline: activityTimeline(status, date) };
        },
        'memory.activityTimeline.build': (request: ControlRequest) => {
          expect(request.body).toEqual({ date, throughToday: false });
          status = 'draft';
          return { ok: true, timeline: activityTimeline(status, date) };
        },
        'memory.activityTimeline.reject': (request: ControlRequest) => {
          expect(request.body).toEqual({
            timelineId: `timeline:${date}`,
            reason: '时段划分需要调整',
            confirmText: 'reject',
          });
          status = 'rejected';
          return { ok: true, decision: 'rejected', timeline: activityTimeline(status, date) };
        },
      },
    });
    renderMemory(transport);

    expect(await screen.findByRole('button', { name: /来源 · 来源记录/ })).toBeInTheDocument();
    await user.click(await screen.findByRole('tab', { name: '时间线' }));
    expect(await screen.findByText('实现最终输入框捕获并核对三条记忆消费路径。')).toBeInTheDocument();
    expect(screen.getByText('2 项活动')).toBeInTheDocument();
    expect(screen.getByText('整理完成后会自动更新；仅在时间相关问题中按需使用')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '立即发布' }));

    expect(await screen.findByText('已发布')).toBeInTheDocument();
    expect(screen.getByText('已整理到活动时间线')).toBeInTheDocument();
    expect(transport.requests.filter((call) => call.request.pathId === 'memory.activityTimeline.approve')).toHaveLength(1);

    await user.click(screen.getByRole('button', { name: '重新整理' }));
    expect(await screen.findAllByText('待发布')).not.toHaveLength(0);
    expect(transport.requests.filter((call) => call.request.pathId === 'memory.activityTimeline.build')).toHaveLength(1);

    await user.click(screen.getByRole('button', { name: '驳回' }));
    const rejectDialog = await screen.findByRole('dialog', { name: '驳回当天整理' });
    await user.type(within(rejectDialog).getByLabelText('原因'), '时段划分需要调整');
    await user.click(within(rejectDialog).getByRole('button', { name: '确认驳回' }));

    expect(await screen.findAllByText('已驳回')).not.toHaveLength(0);
    expect(screen.getByText('本次整理未进入长期上下文')).toBeInTheDocument();
    expect(transport.requests.filter((call) => call.request.pathId === 'memory.activityTimeline.reject')).toHaveLength(1);
  });

  it('shows immediate in-place feedback while historical organization is being accepted', async () => {
    const user = userEvent.setup();
    const date = localCalendarDate();
    let acceptBuild: ((payload: unknown) => void) | undefined;
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, memoryBookCount: 1, memoryAtomCount: 1 },
        'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
        'memory.activityTimeline.get': { ok: true, timeline: activityTimeline('approved', date) },
        'memory.activityTimeline.calendar': {
          ok: true,
          month: date.slice(0, 7),
          days: [{
            date,
            status: 'waiting',
            organized: false,
            modelOrganized: false,
            needsRefresh: false,
            sourceEventCount: 8,
            segmentCount: 0,
          }],
          summary: {
            activityDayCount: 1,
            organizedDayCount: 0,
            waitingDayCount: 1,
            sourceEventCount: 8,
          },
        },
        'memory.activityTimeline.build': (request: ControlRequest) => {
          expect(request.body).toEqual({
            date,
            rangeStartDate: `${date.slice(0, 7)}-01`,
            throughToday: true,
          });
          return new Promise((resolve) => { acceptBuild = resolve; });
        },
        'agent.memoryMaintenance.run': {
          ok: true,
          jobId: 'memory-maintenance:timeline',
          state: 'running',
          progress: {
            phase: 'activity_timeline_catch_up',
            completedDayCount: 2,
            totalDayCount: 19,
            currentDate: '2026-07-13',
          },
        },
      },
    });
    renderMemory(transport);

    await user.click(await screen.findByRole('tab', { name: '时间线' }));
    const organizeButton = await screen.findByRole('button', { name: '整理本月' });
    await user.click(organizeButton);
    const preview = await screen.findByRole('dialog', { name: '整理本月' });
    expect(preview).toHaveTextContent('1 天待整理 · 8 条来源');
    expect(transport.requests.filter((call) => call.request.pathId === 'memory.activityTimeline.build')).toHaveLength(0);
    await user.click(within(preview).getByRole('button', { name: '确认整理本月' }));

    expect(transport.requests.filter((call) => call.request.pathId === 'memory.activityTimeline.build')).toHaveLength(1);
    expect(organizeButton).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByRole('status', { name: '历史日记整理进度' })).toHaveTextContent('正在提交整理任务');

    await act(async () => acceptBuild?.({
      ok: true,
      jobId: 'memory-maintenance:timeline',
      state: 'queued',
    }));
    expect(await screen.findByRole('button', { name: '正在整理' })).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByRole('status', { name: '历史日记整理进度' })).toHaveTextContent(
      '正在整理历史日记：已完成 2 / 19 天，当前 2026-07-13。',
    );
  });

  it('fails closed without the activity timeline read capability', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      capabilities: { routeIds: ['memory.summary', 'memory.pages'] },
      routes: {
        'memory.summary': { ok: true, memoryBookCount: 1, memoryAtomCount: 2 },
        'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
      },
    });
    renderMemory(transport);

    await user.click(await screen.findByRole('tab', { name: '时间线' }));
    expect(await screen.findByText('当前服务未开放每日活动读取')).toBeInTheDocument();
    expect(screen.queryByText('正在读取当天活动')).not.toBeInTheDocument();
    expect(screen.queryByText('尚未生成时间线')).not.toBeInTheDocument();
    expect(transport.requests.some((call) => call.request.pathId === 'memory.activityTimeline.get')).toBe(false);
  });

  it('keeps Agent memory curation under the 治理 tab instead of knowledge tasks', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, eventCount: 3, memoryItemCount: 2, memoryBookCount: 1, memoryAtomCount: 1, pendingCompileEvents: 0 },
        'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
        'agent.memoryMaintenance.run': (request: ControlRequest) => request.query?.runId
          ? memoryCurationRun()
          : memoryCurationStatus(),
      },
    });
    renderMemory(transport);

    expect(await screen.findByRole('heading', { name: '我的记忆', level: 1 })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '整理到今天' })).not.toBeInTheDocument();
    await user.click(await screen.findByRole('tab', { name: '让Agent整理' }));
    expect(await screen.findByRole('heading', { name: '整理到今天', level: 2 })).toBeInTheDocument();
    expect(await screen.findByRole('list', { name: '记忆整理建议' })).toBeInTheDocument();
    expect(screen.queryByPlaceholderText('输入一个明确的知识任务')).not.toBeInTheDocument();
    expect(transport.requests.some((call) => call.request.pathId === 'agent.memoryMaintenance.run')).toBe(true);
    expect(transport.requests.some((call) => call.request.pathId === 'knowledge.routeStatus')).toBe(false);

    await user.click(screen.getByRole('button', { name: '补充整理要求' }));
    const handoffDraft = new URLSearchParams(window.location.hash.split('?')[1]).get('draft') ?? '';
    expect(handoffDraft).toContain('只准备一份可逐项审核的草案');
    expect(handoffDraft).not.toMatch(/memory Tool|curation_prepare|conservative|Atom-first|runId/);
  });

  it('uses governed pending sources in every Memory surface instead of the legacy compile backlog', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': {
          ok: true,
          memoryBookCount: 66,
          memoryAtomCount: 225,
          memoryEvidenceCount: 125,
          pendingCompileEvents: 2219,
          pendingGovernedEvidenceCount: 777,
          governedNeedsReviewEvidenceCount: 10,
        },
        'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
        'agent.memoryMaintenance.run': {
          ...memoryCurationStatus(),
          ownerCuration: { pendingSourceCount: 777, needsReviewSourceCount: 10, scopes: [] },
        },
      },
    });
    renderMemory(transport);

    expect(await screen.findByText('777 条来源待整理')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '查看待整理来源' }));
    expect(await screen.findByText('777 条待整理')).toBeInTheDocument();
    expect(screen.queryByText('2219 条待整理')).not.toBeInTheDocument();
  });

  it('does not block the next batch on a legacy draft when governed auto-apply is enabled', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': {
          ok: true,
          memoryBookCount: 1,
          memoryAtomCount: 1,
          pendingGovernedEvidenceCount: 4,
        },
        'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
        'agent.memoryMaintenance.run': (request: ControlRequest) => request.query?.runId
          ? memoryCurationRun()
          : {
            ...memoryCurationStatus(),
            autoApply: true,
            ownerCuration: {
              pendingSourceCount: 4,
              needsReviewSourceCount: 0,
              backlog: { pendingSourceCount: 4, pendingDayCount: 1, days: [] },
              scopes: [{ status: 'waiting_review', totalSourceCount: 5 }],
            },
          },
        'agent.memoryMaintenance.trigger': (request: ControlRequest) => {
          expect(request.body).toMatchObject({
            maxSources: 4,
            instruction: expect.stringContaining('通过治理校验后自动应用'),
          });
          return { ok: true, jobId: 'memory-maintenance:auto', state: 'queued' };
        },
      },
    });
    renderMemory(transport);

    await user.click(await screen.findByRole('tab', { name: '让Agent整理' }));
    const start = await screen.findByRole('button', { name: '开始整理' });
    expect(start).toBeEnabled();
    expect(screen.queryByRole('button', { name: '先审核本批' })).not.toBeInTheDocument();
    expect(screen.getByText(/通过治理校验后自动应用/)).toBeInTheDocument();
    expect(screen.queryByText(/等待审核/)).not.toBeInTheDocument();
    expect(screen.queryByText(/逐项审核/)).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '补充整理要求' }));
    const autoApplyPrompt = new URLSearchParams(window.location.hash.split('?')[1]).get('draft') ?? '';
    expect(autoApplyPrompt).toContain('通过治理校验后自动应用');
    expect(autoApplyPrompt).not.toContain('逐项审核');
    await user.click(start);
    expect(transport.requests.some((call) => call.request.pathId === 'agent.memoryMaintenance.trigger')).toBe(true);
  });

  it('uses governed Evidence and resumable model state instead of legacy compile counters', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, memoryBookCount: 1, memoryAtomCount: 1 },
        'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
        'agent.memoryMaintenance.run': {
          ok: true,
          autoApply: false,
          scheduledDraftOnly: true,
          due: true,
          pendingDraftCount: 0,
          compileState: { pendingEventCount: 3714, undraftedEventCount: 1686 },
          runs: [],
          ownerCuration: {
            pendingSourceCount: 244,
            needsReviewSourceCount: 10,
            scopes: [{
              status: 'backoff',
              consecutiveFailures: 14,
              lastError: 'managed memory model request failed: fetch failed',
            }],
          },
          modelCuration: {
            stateCounts: { running: 2, resumable: 11, completed: 5, cancelled: 6 },
            runs: [{ state: 'resumable', lastError: 'Session already has an active turn' }],
          },
          bookProjection: { inSync: true, unbookedAtomCount: 1 },
          projection: {
            running: true,
            freshness: { fresh: true, backlog: 0, retrievalDocuments: 51, vectorCoverage: 0 },
          },
        },
      },
    });
    renderMemory(transport);

    await user.click(await screen.findByRole('tab', { name: '让Agent整理' }));

    expect(await screen.findByText('244 条来源待整理；日期和应用分布尚未提供。')).toBeInTheDocument();
    expect(screen.getByText('原始输入不会被改写；每次只处理一批，形成草案后停下来等你审核。')).toBeInTheDocument();
    const technicalSummary = screen.getByText('运行与索引详情').closest('summary');
    expect(technicalSummary).not.toBeNull();
    await user.click(technicalSummary!);
    expect(technicalSummary).toHaveAttribute('aria-expanded', 'true');
    expect(await screen.findByText('待整理来源')).toBeInTheDocument();
    expect(screen.getByText('244')).toBeInTheDocument();
    expect(screen.getByText('可继续')).toBeInTheDocument();
    expect(screen.getByText('11')).toBeInTheDocument();
    expect(screen.getByText(/上一次模型会话仍有活动轮次/)).toBeInTheDocument();
    expect(screen.queryByText(/1686 条新证据尚未生成草案/)).not.toBeInTheDocument();
  });

  it('starts a bounded review-only batch from the saved cursor and polls the real job', async () => {
    const user = userEvent.setup();
    const today = localCalendarDate();
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, memoryBookCount: 1, memoryAtomCount: 1 },
        'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
        'agent.memoryMaintenance.run': (request: ControlRequest) => request.query?.jobId
          ? {
            ok: true,
            jobId: 'memory-maintenance:test',
            state: 'completed',
            result: { ok: true, requestedSourceCount: 6 },
          }
          : {
            ok: true,
            policy: 'auto_governed',
            autoApply: false,
            scheduledDraftOnly: true,
            runs: [],
            ownerCuration: {
              pendingSourceCount: 12,
              needsReviewSourceCount: 1,
              backlog: {
                pendingSourceCount: 12,
                pendingDayCount: 2,
                coveredThroughDate: '2026-08-09',
                targetDate: today,
                caughtUpThroughToday: false,
                days: [
                  { date: '2026-08-10', pendingSourceCount: 8, needsReviewSourceCount: 1, applications: [{ name: 'Codex', count: 8 }] },
                  { date: today, pendingSourceCount: 4, needsReviewSourceCount: 0, applications: [{ name: 'Chrome', count: 4 }] },
                ],
                applications: [
                  { name: 'Codex', count: 8, lastSourceAtMs: Date.now() - 60_000 },
                  { name: 'Chrome', count: 4, lastSourceAtMs: Date.now() },
                ],
              },
              scopes: [{ status: 'idle', totalSourceCount: 40 }],
            },
            modelCuration: { stateCounts: {} },
            bookProjection: { inSync: true, unbookedAtomCount: 0 },
            projection: { freshness: { fresh: true, retrievalDocuments: 4 } },
          },
        'agent.memoryMaintenance.trigger': (request: ControlRequest) => {
          expect(request.body).toEqual({
            ownerKind: 'user',
            ownerId: 'default',
            manual: true,
            maxSources: 6,
            instruction: '从当前已保存的整理位置继续，按时间顺序处理下一批个人记忆来源；只生成可审核草案，不直接保存。',
          });
          return { ok: true, jobId: 'memory-maintenance:test', state: 'queued' };
        },
      },
    });
    renderMemory(transport);

    await user.click(await screen.findByRole('tab', { name: '让Agent整理' }));
    expect(await screen.findByText('来源应用')).toBeInTheDocument();
    expect(screen.getAllByText('Codex')).not.toHaveLength(0);
    expect(screen.getAllByText('Chrome')).not.toHaveLength(0);
    await user.click(screen.getByRole('button', { name: '开始整理' }));

    await waitFor(() => expect(
      transport.requests.filter((call) => call.request.pathId === 'agent.memoryMaintenance.trigger'),
    ).toHaveLength(1));
    await waitFor(() => expect(
      transport.requests.some((call) => call.request.pathId === 'agent.memoryMaintenance.run' && call.request.query?.jobId === 'memory-maintenance:test'),
    ).toBe(true));
    expect(await screen.findByText('本轮处理完成')).toBeInTheDocument();
  });

  it('surfaces an expired curation job as retryable after a Gateway restart', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, memoryBookCount: 1, memoryAtomCount: 1 },
        'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
        'agent.memoryMaintenance.run': (request: ControlRequest) => request.query?.jobId
          ? {
            ok: false,
            jobId: 'memory-maintenance:after-gateway-restart',
            state: 'expired',
            errorCode: 'memory_maintenance_job_expired',
            error: 'Gateway restarted before this process-local Memory maintenance job could be read; the old job cannot be recovered.',
            recovery: {
              recoverable: false,
              retryable: true,
              action: 'trigger_new_job',
              reason: 'process_local_job_registry_lost',
            },
          }
          : {
            ok: true,
            policy: 'auto_governed',
            runs: [],
            ownerCuration: {
              pendingSourceCount: 4,
              needsReviewSourceCount: 0,
              backlog: {
                pendingSourceCount: 4,
                pendingDayCount: 1,
                coveredThroughDate: '2026-08-09',
                targetDate: localCalendarDate(),
                caughtUpThroughToday: false,
                days: [{ date: localCalendarDate(), pendingSourceCount: 4, applications: [] }],
                applications: [],
              },
              scopes: [{ status: 'idle', totalSourceCount: 4 }],
            },
            modelCuration: { stateCounts: {} },
            bookProjection: { inSync: true },
            projection: { freshness: { fresh: true } },
          },
        'agent.memoryMaintenance.trigger': { ok: true, jobId: 'memory-maintenance:after-gateway-restart', state: 'queued' },
      },
    });
    renderMemory(transport);

    await user.click(await screen.findByRole('tab', { name: '让Agent整理' }));
    await user.click(await screen.findByRole('button', { name: '开始整理' }));
    expect(await screen.findByText('整理任务已过期')).toBeInTheDocument();
    expect(screen.getByText('没有找到这个任务的可恢复回执；已有记忆仍保留，可以重新整理。')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '继续整理' })).not.toBeDisabled();
  });

  it('opens a backlog day in the matching activity timeline', async () => {
    const user = userEvent.setup();
    const targetDate = '2026-08-10';
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, memoryBookCount: 1, memoryAtomCount: 1 },
        'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
        'agent.memoryMaintenance.run': {
          ok: true,
          policy: 'auto_governed',
          runs: [],
          ownerCuration: {
            pendingSourceCount: 4,
            backlog: {
              pendingSourceCount: 4,
              pendingDayCount: 1,
              coveredThroughDate: '2026-08-09',
              targetDate: localCalendarDate(),
              days: [{
                date: targetDate,
                pendingSourceCount: 4,
                applications: [{ name: 'com.openai.codex', count: 4 }],
              }],
              applications: [{ name: 'com.openai.codex', count: 4, lastSourceAtMs: Date.now() }],
            },
            scopes: [{ status: 'idle', totalSourceCount: 10 }],
          },
          modelCuration: { stateCounts: {} },
          bookProjection: { inSync: true },
          projection: { freshness: { fresh: true } },
        },
        'memory.activityTimeline.get': { ok: true, timeline: activityTimeline('draft', targetDate) },
        'memory.activityTimeline.calendar': { ok: true, month: '2026-08', days: [], summary: {} },
      },
    });
    renderMemory(transport);

    await user.click(await screen.findByRole('tab', { name: '让Agent整理' }));
    await user.click(await screen.findByRole('button', { name: `查看 ${targetDate} 时间线` }));

    expect(await screen.findByRole('tab', { name: '时间线', selected: true })).toBeInTheDocument();
    expect(screen.getByLabelText('时间线日期')).toHaveValue(targetDate);
    await waitFor(() => expect(transport.requests.some((call) => (
      call.request.pathId === 'memory.activityTimeline.get'
      && call.request.query?.date === targetDate
    ))).toBe(true));
  });

  it('updates only the selected Agent draft item before any database apply', async () => {
    const user = userEvent.setup();
    let secondSelected = false;
    const transport = new MockControlTransport({
      capabilities: { features: { managementWorkContract: true, knowledgeDatabaseWorkContract: true } },
      routes: {
        'memory.summary': { ok: true, appCount: 1, completeInputCount: 3, memoryBookCount: 1, memoryAtomCount: 1, pendingCompileEvents: 0 },
        'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
        'agent.memoryMaintenance.run': (request: ControlRequest) => {
          if (!request.query?.runId) return memoryCurationStatus();
          const result = memoryCurationRun();
          const changes = ((result.run as Record<string, unknown>).changes as Record<string, unknown>[]);
          changes[1] = { ...changes[1], selected: secondSelected, status: secondSelected ? 'approved' : 'rejected' };
          return result;
        },
        'knowledge.database.draft.edit': (request: ControlRequest) => {
          expect(request.body).toEqual({ runId: 'memory_book_test', diffId: 2, selected: true });
          secondSelected = true;
          return { ok: true, runId: 'memory_book_test', diffId: 2, selected: true };
        },
      },
    });
    renderMemory(transport);

    await user.click(await screen.findByRole('tab', { name: '让Agent整理' }));
    const checkbox = await screen.findByRole('checkbox', { name: '选择 合并输入法同义标签' });
    expect(checkbox).not.toBeChecked();
    await user.click(checkbox);

    await waitFor(() => expect(checkbox).toBeChecked());
    expect(screen.getByText('2 / 2 已选择')).toBeInTheDocument();
    expect(transport.requests.filter((call) => call.request.pathId === 'knowledge.database.draft.edit')).toHaveLength(1);
    expect(transport.requests.some((call) => call.request.pathId === 'knowledge.database.apply')).toBe(false);
  });

  it('keeps memories, topics and source records as distinct catalog destinations', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': {
          ok: true,
          evidenceSourceCount: 18,
          agentEvidenceCount: 6,
          memoryBookCount: 4,
          memoryAtomCount: 10,
          memoryAtomTotalCount: 14,
          memoryAtomArchivedCount: 4,
          pendingCompileEvents: 2,
        },
        'memory.pages': (request: ControlRequest) => memoryPage(String(request.params?.kind ?? '')),
        'memory.graph.get': (request: ControlRequest) => memoryGraph(String(request.query?.plane ?? '')),
        'memory.entity.get': (request: ControlRequest) => memoryEntity(
          String(request.params?.kind ?? ''),
          String(request.params?.entityId ?? ''),
        ),
      },
    });
    renderMemory(transport);

    const architecture = await screen.findByRole('list', { name: '记忆内容分类' });
    await waitFor(() => (
      expect(within(architecture).getByRole('button', { name: /来源/ })).toHaveTextContent('24')
    ));
    expect(within(architecture).getByRole('button', { name: /记忆/ })).toHaveTextContent('10');
    expect(within(architecture).getByRole('button', { name: /主题/ })).toHaveTextContent('4');

    const stageLabels = within(architecture).getAllByRole('button').map((stage) => (
      within(stage).getByText(/^(?:记忆|主题|来源)$/).textContent
    ));
    expect(stageLabels).toEqual(['主题', '记忆', '来源']);
    expect(within(architecture).queryByRole('button', { name: /应用/ })).not.toBeInTheDocument();
    expect(within(architecture).queryByRole('button', { name: /标签/ })).not.toBeInTheDocument();

    await user.click(screen.getByRole('tab', { name: '关系图' }));
    expect(await screen.findByRole('heading', { name: '记忆关系', level: 2 })).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: /Agent Runtime，11 条记忆/ })).toBeInTheDocument();
  });

  it('gives compact Memory a reversible list-to-detail disclosure', async () => {
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, memoryBookCount: 1, memoryAtomCount: 1 },
        'memory.pages': (request: ControlRequest) => memoryPage(String(request.params?.kind ?? '')),
      },
    });
    renderMemory(transport);

    fireEvent.click(await screen.findByRole('button', { name: /控制中心迁移/ }));
    const workspace = document.querySelector('.memory-layer-workspace');
    expect(workspace).toHaveAttribute('data-detail-open', 'true');
    const back = document.querySelector('.memory-layer-detail__back') as HTMLButtonElement;
    expect(back).toHaveAttribute('aria-label', '返回记忆目录');
    fireEvent.click(back);
    expect(workspace).not.toHaveAttribute('data-detail-open');
  });

  it('keeps the row summary compact but reveals the complete memory body after selection', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, memoryAtomCount: 1 },
        'memory.pages': {
          ok: true,
          items: [{
            id: 'atom:complete-body',
            title: '输入段封口规则',
            summary: '先结束输入，再保存。',
            text: 'Backspace 只修改当前缓冲区；Enter、切换 App 或明确提交后，才形成可治理的完整输入段。',
            status: 'current',
            source: { type: 'memory_atom', id: 'atom:complete-body' },
            ref: { type: 'atom', id: 'atom:complete-body' },
          }],
          nextCursor: '',
          limit: 50,
        },
      },
    });
    renderMemory(transport);

    const row = await screen.findByRole('button', { name: /输入段封口规则/ });
    expect(within(row).getByText('先结束输入，再保存。')).toBeInTheDocument();
    await user.click(row);

    const detail = screen.getByLabelText('输入段封口规则 详情');
    expect(within(detail).getByText('记忆正文')).toBeInTheDocument();
    expect(within(detail).getByText('Backspace 只修改当前缓冲区；Enter、切换 App 或明确提交后，才形成可治理的完整输入段。')).toBeInTheDocument();
    expect(within(detail).queryByText('先结束输入，再保存。')).not.toBeInTheDocument();
  });

  it('keeps every catalog tag reachable beyond the compact first row', async () => {
    const user = userEvent.setup();
    const tags = Array.from({ length: 10 }, (_, index) => `标签 ${index + 1}`);
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, memoryAtomCount: 1 },
        'memory.pages': {
          ok: true,
          items: [{
            id: 'atom:many-tags',
            title: '多标签记忆',
            text: '完整记忆正文',
            status: 'current',
            tags,
          }],
          nextCursor: '',
          limit: 50,
        },
      },
    });
    renderMemory(transport);

    await user.click(await screen.findByRole('button', { name: /多标签记忆/ }));
    const detail = screen.getByLabelText('多标签记忆 详情');
    expect(within(detail).getByText('标签 8')).toBeInTheDocument();
    expect(within(detail).queryByText('标签 9')).not.toBeInTheDocument();
    const moreSummary = within(detail).getByText('查看其余 2 项标签').closest('summary');
    expect(moreSummary).not.toBeNull();
    await user.click(moreSummary!);
    expect(within(detail).getByText('标签 9')).toBeInTheDocument();
    expect(within(detail).getByText('标签 10')).toBeInTheDocument();
  });

  it('labels linked Agent evidence as an intentional Agent capture', async () => {
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, memoryEvidenceCount: 1, memoryBookCount: 1, memoryAtomCount: 1 },
        'memory.pages': (request: ControlRequest) => ({
          ok: true,
          items: request.params?.kind === 'evidence' ? [{
            id: 'evidence:agent-capture',
            title: '回答默认先给结论，再补依据。',
            detail: 'Agent 主动记录',
            type: 'session_digest',
            sourceChannel: 'agent_capture',
            status: 'active',
            source: { kind: 'agent_memory_evidence', id: 'evidence:agent-capture' },
            ownerKind: 'agent',
            ownerId: 'companion-present-v1',
          }] : [],
          nextCursor: '',
          limit: 50,
        }),
      },
    });
    renderMemory(transport, '/memory?layer=evidence');

    expect(await screen.findByText('已引用')).toBeInTheDocument();
    expect(screen.getByText(/Agent 主动记录/u)).toBeInTheDocument();
    expect(screen.queryByText('对话审计')).not.toBeInTheDocument();
  });

  it('uses product language for memory references without exposing owner identifiers', async () => {
    const user = userEvent.setup();
    const ownerId = 'companion-internal-7f4a';
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': {
          ok: true,
          memoryBookCount: 1,
          memoryAtomCount: 1,
          owners: [{ ownerKind: 'agent', ownerId, ownerDisplayName: '澄', itemCount: 1 }],
        },
        'memory.pages': {
          ok: true,
          items: [{
            id: 'book:memory-language',
            title: '时间线必须审批后参与召回',
            detail: '审批后才会召回这部分内容。',
            status: 'active',
            ownerKind: 'agent',
            ownerId,
            ownerDisplayName: '澄',
            evidenceRefs: [{ kind: 'atom', referenceId: 'atom:timeline', title: '时间线召回规则' }],
          }],
          nextCursor: '',
          limit: 50,
        },
      },
    });
    renderMemory(transport, '/memory?layer=books');

    const row = await screen.findByRole('button', { name: /时间线必须审批后用于联想/ });
    await user.click(row);
    const topic = screen.getByRole('region', { name: '时间线必须审批后用于联想 主题页' });
    await user.click(within(topic).getByText('查看已有摘要', { selector: 'summary' }));
    expect(topic).toHaveTextContent('审批后才会联想这部分内容。');
    expect(screen.getByText('伙伴记忆 · 澄')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /时间线联想规则/ })).toBeInTheDocument();
    await user.click(screen.getByRole('combobox', { name: '归属' }));
    expect(await screen.findByRole('option', { name: '伙伴记忆 · 澄 · 1 项' })).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(ownerId);
    expect(document.body).not.toHaveTextContent('召回');
  });

  it('starts from current memory and keeps preserved history inspectable', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, memoryBookCount: 10, memoryAtomCount: 103, pendingCompileEvents: 0 },
        'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
      },
    });
    renderMemory(transport);

    await waitFor(() => {
      const request = transport.requests.find((call) => call.request.pathId === 'memory.pages');
      expect(request?.request.query?.status).toBe('current');
    });
    await user.click(await screen.findByRole('combobox', { name: '状态' }));
    expect(await screen.findByRole('option', { name: '历史保留' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: '已合并' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: '来源归档' })).toBeInTheDocument();
  });

  it('normalizes structured source and sourceType/sourceId evidence references', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, memoryBookCount: 1, memoryAtomCount: 1 },
        'memory.pages': {
          ok: true,
          items: [{
            atomId: 'atom:structured-source',
            text: '用户长期居住在上海',
            type: 'personal_fact',
            status: 'active',
            source: { type: 'input_event', id: 'input-memory:42' },
            ref: { kind: 'atom', referenceId: 'atom:structured-source' },
            evidenceRefs: [{ sourceType: 'input_event', sourceId: 'input-memory:42', textPreview: '用户明确说明常住上海' }],
          }],
          nextCursor: '',
          limit: 50,
        },
        'memory.reference.get': (request: ControlRequest) => {
          expect(request.params).toEqual({ kind: 'event', referenceId: 'input-memory:42' });
          return {
            schemaVersion: 'rag-ime.memory-reference.v1',
            settingsRevision: 'settings:test',
            runtimeRevision: 1,
            ok: true,
            kind: 'event',
            referenceId: 'input-memory:42',
            ref: {
              kind: 'event',
              id: 'input-memory:42',
              referenceKind: 'event',
              referenceId: 'input-memory:42',
            },
            source: { kind: 'ax_focused_value', id: 'input-memory:42' },
            item: {
              id: 'input-memory:42',
              title: '用户明确说明常住上海',
              status: 'active',
              text: '用户长期居住在上海',
            },
            evidenceRefs: [],
          };
        },
      },
    });
    renderMemory(transport);

    await user.click(await screen.findByRole('button', { name: /用户长期居住在上海/ }));
    const detail = screen.getByRole('region', { name: '用户长期居住在上海 详情' });
    expect(within(detail).getByText('输入记录')).toBeInTheDocument();
    await user.click(within(detail).getByRole('button', { name: /用户明确说明常住上海/ }));
    const dialog = await screen.findByRole('dialog', { name: '用户明确说明常住上海' });
    await user.click(within(dialog).getByText('高级：引用详情', { selector: 'summary' }));
    expect(within(dialog).getByText('ax_focused_value')).toBeInTheDocument();
    expect(within(
      within(dialog).getByRole('navigation', { name: '记忆来源路径' }),
    ).getByText('来源')).toBeInTheDocument();
  });

  it('does not hide Atom kinds behind a category-only frontend filter', async () => {
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, memoryAtomCount: 3 },
        'memory.pages': {
          ok: true,
          items: [
            {
              id: 'atom:preference',
              type: 'durable_preference',
              text: '用户偏好先给结论。',
              status: 'active',
              updated_at_ms: 10,
            },
            {
              id: 'atom:principle',
              type: 'personal_principle',
              text: '用户要求工程判断基于真实运行证据。',
              status: 'active',
              updated_at_ms: 11,
            },
            {
              id: 'atom:project-requirement',
              type: 'project_requirement',
              text: '项目要求保留 Evidence 到 Book 的完整链路。',
              status: 'active',
              updated_at_ms: 12,
            },
          ],
          nextCursor: '',
          limit: 50,
        },
      },
    });
    renderMemory(transport);

    expect(await screen.findByRole('button', { name: /用户偏好先给结论/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /用户要求工程判断基于真实运行证据/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /项目要求保留 Evidence 到 Book 的完整链路/ })).toBeInTheDocument();
    expect(screen.getByText('项目要求 · 0 条来源')).toBeInTheDocument();
    expect(screen.queryByRole('radiogroup', { name: '个人记忆类型' })).not.toBeInTheDocument();
  });

  it('keeps App counts as source-index statistics instead of a peer memory layer', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': {
          ok: true,
          appCount: 1,
          completeInputCount: 8,
          blockedFragmentCount: 44,
          memoryBookCount: 1,
          memoryAtomCount: 2,
          memoryAtomTotalCount: 8,
          memoryAtomArchivedCount: 4,
          memoryAtomSourceArchiveCount: 2,
          pendingCompileEvents: 0,
        },
        'memory.pages': (request: ControlRequest) => memoryPage(String(request.params?.kind ?? '')),
      },
    });
    renderMemory(transport);

    const pipeline = await screen.findByRole('list', { name: '记忆内容分类' });
    await user.click(screen.getByRole('button', { name: '整理状态摘要' }));
    expect(await screen.findByText('全部 8 · 历史 4')).toBeInTheDocument();
    expect(within(pipeline).queryByRole('button', { name: /应用/ })).not.toBeInTheDocument();
    expect(transport.requests.some((call) => call.request.params?.kind === 'apps')).toBe(false);
  });

  it('opens every architecture layer while keeping Role Book separate', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, eventCount: 18, memoryItemCount: 14, memoryBookCount: 4, memoryAtomCount: 10, pendingCompileEvents: 2 },
        'memory.pages': (request: ControlRequest) => memoryPage(String(request.params?.kind ?? '')),
        'agent.roles.list': { ok: true, items: [] },
      },
    });
    renderMemory(transport);

    const architecture = await screen.findByRole('list', { name: '记忆内容分类' });
    await user.click(within(architecture).getByRole('button', { name: /来源/ }));
    await waitFor(() => expect(transport.requests.some((call) => (
      call.request.pathId === 'memory.pages' && call.request.params?.kind === 'evidence'
    ))).toBe(true));
    await user.click(within(architecture).getByRole('button', { name: /记忆/ }));
    await waitFor(() => expect(transport.requests.some((call) => (
      call.request.pathId === 'memory.pages' && call.request.params?.kind === 'atoms'
    ))).toBe(true));
    await user.click(within(architecture).getByRole('button', { name: /主题/ }));
    await waitFor(() => expect(transport.requests.some((call) => (
      call.request.pathId === 'memory.pages' && call.request.params?.kind === 'books'
    ))).toBe(true));
    const bookRequest = transport.requests.find((call) => (
      call.request.pathId === 'memory.pages' && call.request.params?.kind === 'books'
    ));
    expect(bookRequest?.request.params?.status).toBeUndefined();
    await user.click(screen.getByRole('button', { name: '整理状态摘要' }));
    expect(screen.getByText('按主题持续查找')).toBeInTheDocument();

    await user.click(screen.getByRole('tab', { name: '伙伴记忆' }));
    expect(await screen.findByRole('heading', { name: '伙伴记忆', level: 3 })).toBeInTheDocument();
    expect(transport.requests.some((call) => call.request.pathId === 'agent.roles.list')).toBe(true);
  });

  it('opens role-book entries and their evidence without mixing them into personal facts', async () => {
    const user = userEvent.setup();
    const persona = previewPersonas[0]!;
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, memoryBookCount: 1, memoryAtomCount: 2 },
        'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
        'agent.roles.list': { ok: true, items: [persona] },
        'agent.roleBook.get': {
          ok: true,
          active: {
            revisionId: 'revision:role-current',
            revisionNumber: 3,
            displayName: persona.displayName,
            mission: '保持角色工作连续性',
            status: 'active',
            sections: {
              personality: [],
              capabilities: [],
              recentWork: [{
                itemId: 'recent-work:1',
                text: '完成记忆事实链重构',
                evidenceIds: ['evidence:role-work'],
                provenance: { sourceType: 'daily_session_summary', sourceId: 'session:1' },
              }],
              lessonsAndLimits: [],
              activeCommitments: [],
            },
          },
          history: [],
          dailyDrafts: [],
        },
        'memory.reference.get': () => ({
          schemaVersion: 'rag-ime.memory-reference.v1',
          settingsRevision: 'settings:test',
          runtimeRevision: 1,
          ok: true,
          kind: 'evidence',
          referenceId: 'evidence:role-work',
          ref: {
            kind: 'evidence',
            id: 'evidence:role-work',
            referenceKind: 'evidence',
            referenceId: 'evidence:role-work',
          },
          source: { kind: 'agent_memory_evidence', id: 'evidence:role-work' },
          item: {
            id: 'evidence:role-work',
            title: '角色工作证据',
            status: 'active',
            text: '已完成事实链重构并通过测试。',
          },
          evidenceRefs: [],
        }),
      },
    });
    renderMemory(transport);

    await user.click(await screen.findByRole('tab', { name: '伙伴记忆' }));
    expect(await screen.findByRole('heading', { name: persona.displayName, level: 3 })).toBeInTheDocument();
    const entry = screen.getByRole('button', { name: /完成记忆事实链重构/ });
    expect(entry).toHaveAttribute('aria-expanded', 'false');
    const disclosureId = entry.getAttribute('aria-controls');
    expect(disclosureId).toBeTruthy();

    await user.click(entry);
    expect(entry).toHaveAttribute('aria-expanded', 'true');
    const detail = screen.getByLabelText('近期工作条目详情');
    const disclosure = document.getElementById(disclosureId!);
    expect(entry.nextElementSibling).toBe(disclosure);
    expect(disclosure).not.toHaveAttribute('inert');
    expect(within(detail).getByText('对话总结')).toBeInTheDocument();

    await user.click(entry);
    expect(entry).toHaveAttribute('aria-expanded', 'false');
    expect(disclosure).toHaveAttribute('inert');
    await user.click(entry);
    await user.click(within(detail).getByRole('button', { name: '来源 1' }));
    expect(await screen.findByRole('dialog', { name: '角色工作来源' })).toBeInTheDocument();
    expect(transport.requests.some((call) => (
      call.request.pathId === 'memory.reference.get'
      && call.request.params?.kind === 'evidence'
      && call.request.params?.referenceId === 'evidence:role-work'
    ))).toBe(true);
  });

  it('keeps the partner governance action synchronized with the Memory route owner', async () => {
    const user = userEvent.setup();
    const persona = previewPersonas[0]!;
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, roleBookCount: 1 },
        'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
        'agent.roles.list': { ok: true, items: [persona] },
        'agent.roleBook.get': { ok: true, active: {}, history: [], dailyDrafts: [] },
      },
    });
    renderMemory(transport, '/memory?view=roleBooks');

    await user.click(await screen.findByRole('button', { name: '查看待确认内容' }));
    expect(screen.getByTestId('memory-location')).toHaveTextContent('/memory?view=organize');
  });

  it.each([
    { layer: 'atoms', id: 'atom:deep-link', kind: 'atom', tab: '记忆' },
    { layer: 'books', id: 'book:deep-link', kind: 'book', tab: '记忆' },
    { layer: 'evidence', id: 'evidence:deep-link', kind: 'evidence', tab: '记忆' },
    { layer: 'timelines', id: 'timeline:2026-07-18', kind: 'timeline', tab: '时间线' },
    { layer: 'role-books', id: 'revision:deep-link', kind: 'role_book_revision', tab: '伙伴记忆' },
  ])('opens the $layer deep link and selects its stable reference', async ({ id, kind, layer, tab }) => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, memoryBookCount: 1, memoryAtomCount: 1 },
        'memory.pages': (request: ControlRequest) => memoryPage(String(request.params?.kind ?? '')),
        'memory.reference.get': (request: ControlRequest) => {
          expect(request.params).toEqual({ kind, referenceId: id });
          return {
            schemaVersion: 'rag-ime.memory-reference.v1',
            settingsRevision: 'settings:test',
            runtimeRevision: 1,
            ok: true,
            kind,
            referenceId: id,
            ref: { kind, id, referenceKind: kind, referenceId: id },
            source: { kind: `memory_${kind}`, id },
            item: { id, title: `深链 ${layer}`, status: 'active', text: '公开详情' },
            evidenceRefs: [],
          };
        },
        'agent.roles.list': { ok: true, items: [] },
      },
    });
    renderMemory(transport, `/memory?layer=${layer}&id=${encodeURIComponent(id)}`);

    expect(await screen.findByRole('tab', { name: tab, hidden: true })).toHaveAttribute('aria-selected', 'true');
    if (layer === 'books') {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      await user.click(screen.getByRole('button', { name: '查看主题已有来源' }));
    }
    expect(await screen.findByRole('dialog', { name: layer === 'evidence' ? '深链 来源' : `深链 ${layer}` })).toBeInTheDocument();
    expect(transport.requests.some((call) => call.request.pathId === 'memory.reference.get')).toBe(true);
  });

  it('traces a Book through its Atom to immutable Evidence', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, memoryBookCount: 1, memoryAtomCount: 1, evidenceSourceCount: 1 },
        'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
        'memory.reference.get': (request: ControlRequest) => {
          const kind = String(request.params?.kind ?? '');
          const referenceId = String(request.params?.referenceId ?? '');
          if (kind === 'book') {
            return {
              schemaVersion: 'rag-ime.memory-reference.v1',
              settingsRevision: 'settings:test',
              runtimeRevision: 1,
              ok: true,
              kind: 'book',
              referenceId,
              item: { id: referenceId, title: '输入法记忆 Book', status: 'active', summary: '只组织 Atom。' },
              source: { kind: 'memory_book', id: referenceId },
              ref: { kind: 'book', id: referenceId, referenceKind: 'book', referenceId },
              evidenceRefs: [{
                kind: 'atom',
                id: 'atom:input-boundary',
                referenceKind: 'atom',
                referenceId: 'atom:input-boundary',
                label: '输入边界 Atom',
              }],
            };
          }
          if (kind === 'atom') {
            return {
              schemaVersion: 'rag-ime.memory-reference.v1',
              settingsRevision: 'settings:test',
              runtimeRevision: 1,
              ok: true,
              kind: 'atom',
              referenceId,
              item: { id: referenceId, title: '输入边界 Atom', status: 'active', text: '提交后才形成完整输入。' },
              source: { kind: 'memory_atom', id: referenceId },
              ref: { kind: 'atom', id: referenceId, referenceKind: 'atom', referenceId },
              evidenceRefs: [{
                kind: 'evidence',
                id: 'evidence:input-boundary',
                referenceKind: 'evidence',
                referenceId: 'evidence:input-boundary',
                label: '完整输入 Evidence',
              }],
            };
          }
          return {
            schemaVersion: 'rag-ime.memory-reference.v1',
            settingsRevision: 'settings:test',
            runtimeRevision: 1,
            ok: true,
            kind: 'evidence',
            referenceId,
            item: { id: referenceId, title: '完整输入 Evidence', status: 'active', text: '用户提交的不可变来源。' },
            source: { kind: 'agent_memory_evidence', id: referenceId },
            ref: { kind: 'evidence', id: referenceId, referenceKind: 'evidence', referenceId },
            evidenceRefs: [],
          };
        },
      },
    });
    renderMemory(transport, '/memory?layer=books&id=book%3Ainput-memory');

    await user.click(await screen.findByRole('button', { name: '查看主题已有来源' }));
    const dialog = await screen.findByRole('dialog', { name: '输入法记忆 主题' });
    let path = within(dialog).getByRole('navigation', { name: '记忆来源路径' });
    expect(within(path).getByText('主题')).toBeInTheDocument();

    await user.click(within(dialog).getByRole('button', { name: /输入边界 记忆/ }));
    expect(await within(dialog).findByRole('heading', { name: '输入边界 记忆' })).toBeInTheDocument();
    path = within(dialog).getByRole('navigation', { name: '记忆来源路径' });
    expect(within(path).getByText('主题')).toBeInTheDocument();
    expect(within(path).getByText('记忆')).toBeInTheDocument();

    await user.click(within(dialog).getByRole('button', { name: /完整输入 来源/ }));
    expect(await within(dialog).findByRole('heading', { name: '完整输入 来源' })).toBeInTheDocument();
    path = within(dialog).getByRole('navigation', { name: '记忆来源路径' });
    expect(within(path).getByText('主题')).toBeInTheDocument();
    expect(within(path).getByText('记忆')).toBeInTheDocument();
    expect(within(path).getByText('来源')).toBeInTheDocument();
    expect(transport.requests.filter((call) => call.request.pathId === 'memory.reference.get').map((call) => (
      call.request.params?.kind
    ))).toEqual(['book', 'atom', 'evidence']);
  });

  it('recursively opens evidence references, blocks cycles, and explains redaction', async () => {
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, memoryBookCount: 1, memoryAtomCount: 1 },
        'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
        'memory.reference.get': (request: ControlRequest) => {
          if (request.params?.kind === 'event') {
            return {
              schemaVersion: 'rag-ime.memory-reference.v1',
              settingsRevision: 'settings:test',
              runtimeRevision: 1,
              ok: true,
              kind: 'event',
              referenceId: 'event:1',
              item: { id: 'event:1', title: '原始事件', status: 'archived' },
              source: { kind: 'input_event', id: 'event:1' },
              ref: { kind: 'event', id: 'event:1', referenceKind: 'event', referenceId: 'event:1' },
              evidenceRefs: [{
                kind: 'evidence',
                id: 'evidence:redacted',
                referenceKind: 'evidence',
                referenceId: 'evidence:redacted',
                label: '返回根节点',
              }],
            };
          }
          return {
            schemaVersion: 'rag-ime.memory-reference.v1',
            settingsRevision: 'settings:test',
            runtimeRevision: 1,
            ok: true,
            kind: 'evidence',
            referenceId: 'evidence:redacted',
            item: {
              id: 'evidence:redacted',
              title: '已脱敏证据',
              status: 'not_for_memory',
              sensitive: true,
              text: 'SECRET SHOULD NEVER RENDER',
            },
            source: { kind: 'agent_memory_evidence', id: 'evidence:redacted' },
            ref: {
              kind: 'evidence',
              id: 'evidence:redacted',
              referenceKind: 'evidence',
              referenceId: 'evidence:redacted',
            },
            evidenceRefs: [{
              kind: 'event',
              id: 'event:1',
              referenceKind: 'event',
              referenceId: 'event:1',
              label: '原始输入',
            }],
          };
        },
      },
    });
    renderMemory(transport, '/memory?layer=evidence&id=evidence%3Aredacted');

    const dialog = await screen.findByRole('dialog', { name: '已脱敏来源' });
    expect(within(dialog).getByText('内容已脱敏')).toBeInTheDocument();
    expect(within(dialog).getByText('正文不会在控制中心显示；来源类别和处理状态仍会保留，方便核对。')).toBeInTheDocument();
    expect(within(dialog).queryByText('SECRET SHOULD NEVER RENDER')).not.toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole('button', { name: /原始输入/ }));
    expect(await within(dialog).findByRole('heading', { name: '原始事件' })).toBeInTheDocument();
    expect(within(dialog).getByText('已在路径中')).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: /返回根节点/ })).toBeDisabled();
  });

  it('keeps a failed evidence reference actionable and retries the typed response', async () => {
    const user = userEvent.setup();
    let attempts = 0;
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, memoryBookCount: 1, memoryAtomCount: 1 },
        'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
        'memory.reference.get': () => {
          attempts += 1;
          if (attempts === 1) throw new Error('temporary reference failure');
          return {
            schemaVersion: 'rag-ime.memory-reference.v1',
            settingsRevision: 'settings:test',
            runtimeRevision: 1,
            ok: true,
            kind: 'evidence',
            referenceId: 'evidence:retry',
            item: {
              id: 'evidence:retry',
              title: '重试后的证据',
              status: 'active',
              text: '引用详情已恢复。',
            },
            source: { kind: 'agent_memory_evidence', id: 'evidence:retry' },
            ref: {
              kind: 'evidence',
              id: 'evidence:retry',
              referenceKind: 'evidence',
              referenceId: 'evidence:retry',
            },
            evidenceRefs: [],
          };
        },
      },
    });
    renderMemory(transport, '/memory?layer=evidence&id=evidence%3Aretry');

    const retry = await screen.findByRole('button', { name: '重试读取' });
    expect(screen.getByText('引用暂时无法读取')).toBeInTheDocument();
    await user.click(retry);

    expect(await screen.findByRole('dialog', { name: '重试后的来源' })).toBeInTheDocument();
    expect(screen.getByText('引用详情已恢复。')).toBeInTheDocument();
    expect(attempts).toBe(2);
  });

  it('edits the selected topic-book stable id directly', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, runtimeRevision: 7, eventCount: 18, memoryItemCount: 14, memoryBookCount: 4, memoryAtomCount: 10, pendingCompileEvents: 2 },
        'memory.pages': (request: ControlRequest) => memoryPage(String(request.params?.kind ?? '')),
        'memory.edit': (request: ControlRequest) => {
          expect(request.body).toEqual({
            kind: 'books',
            id: 'book-1',
            title: '控制中心真实迁移',
            summary: 'React 页面与本机事务',
            tags: ['控制中心', '真实环境'],
          });
          return { schemaVersion: 'rag-ime.memory-edit.v1', ok: true, kind: 'books', id: 'book-1' };
        },
      },
    });
    renderMemory(transport);

    const pipelineStages = await screen.findByRole('list', { name: '记忆内容分类' });
    await user.click(within(pipelineStages).getByRole('button', { name: /主题/ }));
    await user.click(await screen.findByRole('button', { name: /控制中心迁移/ }));
    const editWorkflow = screen.getByText('编辑内容').closest('.mgmt-workflow');
    expect(editWorkflow).not.toBeNull();
    await user.click(within(editWorkflow as HTMLElement).getByRole('button', { name: '编辑' }));
    const dialog = await screen.findByRole('dialog', { name: '编辑 长期主题' });
    const title = within(dialog).getByRole('textbox', { name: '标题' });
    const summary = within(dialog).getByRole('textbox', { name: '摘要' });
    const tags = within(dialog).getByRole('textbox', { name: '标签' });
    expect(title).toHaveValue('控制中心迁移');
    expect(summary).toHaveValue('React 页面与受控接口');
    await user.clear(title);
    await user.type(title, '控制中心真实迁移');
    await user.clear(summary);
    await user.type(summary, 'React 页面与本机事务');
    await user.clear(tags);
    await user.type(tags, '控制中心，真实环境');
    await user.click(within(dialog).getByRole('button', { name: '保存' }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '编辑 长期主题' })).not.toBeInTheDocument());
    expect(transport.requests.filter((call) => call.request.pathId === 'memory.edit')).toHaveLength(1);
    expect(screen.getByRole('main')).not.toHaveTextContent('book-1');
  });

  it('keeps a failed edit draft and replaces internal errors with friendly copy', async () => {
    const user = userEvent.setup();
    const internal = '/Users/private/memory.sqlite pathId=memory.edit schemaVersion rawJson';
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, runtimeRevision: 7, eventCount: 18, memoryItemCount: 14, memoryBookCount: 4, memoryAtomCount: 10, pendingCompileEvents: 2 },
        'memory.pages': (request: ControlRequest) => memoryPage(String(request.params?.kind ?? '')),
        'memory.edit': () => Promise.reject(new Error(internal)),
      },
    });
    renderMemory(transport);

    const pipelineStages = await screen.findByRole('list', { name: '记忆内容分类' });
    await user.click(within(pipelineStages).getByRole('button', { name: /主题/ }));
    await user.click(await screen.findByRole('button', { name: /控制中心迁移/ }));
    await user.click(within(screen.getByText('编辑内容').closest('.mgmt-workflow') as HTMLElement).getByRole('button', { name: '编辑' }));
    const dialog = await screen.findByRole('dialog', { name: '编辑 长期主题' });
    const summary = within(dialog).getByRole('textbox', { name: '摘要' });
    await user.clear(summary);
    await user.type(summary, '失败后仍应保留的草稿');
    await user.click(within(dialog).getByRole('button', { name: '保存' }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent('保存失败。草稿已保留');
    expect(summary).toHaveValue('失败后仍应保留的草稿');
    expect(document.body).not.toHaveTextContent(internal);
    expect(document.body).not.toHaveTextContent('pathId');
    expect(document.body).not.toHaveTextContent('schemaVersion');
    expect(document.body).not.toHaveTextContent('rawJson');
  });

  it('keeps non-durable audit states out of the Evidence catalog', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': {
          ok: true,
          memoryEvidenceCount: 1,
          evidenceSourceCount: 1,
          forgottenSourceCount: 5_705,
          memoryBookCount: 4,
          memoryAtomCount: 10,
        },
        'memory.pages': (request: ControlRequest) => ({
          ok: true,
          items: request.params?.kind === 'evidence' ? [{
            id: 'source:durable-input',
            title: '回答时默认先给结论。',
            detail: '已整理进长期记忆',
            status: 'consolidated',
            disposition: 'consolidated',
            sourceChannel: 'input_method',
            type: 'user_final',
            ownerKind: 'user',
            ownerId: 'default',
          }] : [],
          nextCursor: '',
          limit: 50,
        }),
      },
    });
    renderMemory(transport, '/memory?layer=evidence');

    const evidenceRow = await screen.findByRole('button', { name: /回答时默认先给结论/ });
    expect(evidenceRow).toHaveTextContent('输入法');
    expect(document.body).not.toHaveTextContent('reviewed_non_durable_source');
    expect(document.body).not.toHaveTextContent('命令执行完成');
    expect(screen.queryByText('恢复证据')).not.toBeInTheDocument();
    await user.click(screen.getByRole('combobox', { name: '状态' }));
    expect(screen.queryByRole('option', { name: '已遗忘' })).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: '已过期' })).not.toBeInTheDocument();
  });

  it('loads bounded tag/group graphs and links keyboard node selection to accessible tables', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, eventCount: 18, memoryItemCount: 14, memoryBookCount: 4, memoryAtomCount: 10, pendingCompileEvents: 2 },
        'memory.pages': (request: ControlRequest) => memoryPage(String(request.params?.kind ?? '')),
        'memory.graph.get': (request: ControlRequest) => {
          const plane = String(request.query?.plane ?? '');
          const query = String(request.query?.query ?? '');
          if (plane === 'tags' && query === 'Memory') {
            return graphEnvelope(
              'tags',
              [graphNode('tag:memory', 'tag', 'Memory', 5, '记忆组织与检索')],
              [],
              false,
            );
          }
          if (plane === 'groups' && query) return graphEnvelope('groups', [], [], false);
          return memoryGraph(plane);
        },
        'memory.entity.get': (request: ControlRequest) => memoryEntity(
          String(request.params?.kind ?? ''),
          String(request.params?.entityId ?? ''),
        ),
      },
    });
    renderMemory(transport);

    expect(await screen.findByRole('heading', { name: '我的记忆', level: 1 })).toBeInTheDocument();
    await user.click(await screen.findByRole('tab', { name: '关系图' }));
    expect(await screen.findByText(/共 2 项 · 1 条关系/)).toBeInTheDocument();
    expect(screen.getByText('还有更多关系未显示')).toBeInTheDocument();
    expect(document.querySelector('.memory-relation-canvas')).toHaveAttribute('data-layout', 'clustered-force-network');

    await waitFor(() => {
      const graphRequests = transport.requests.filter((call) =>
        call.request.pathId === 'memory.graph.get');
      expect(graphRequests).toHaveLength(2);
      for (const call of graphRequests) {
        expect(call.request.query).toMatchObject({ depth: 1, minWeight: 0 });
      }
    });

    const memoryNode = screen.getByRole('button', { name: /Memory，5 条记忆/ });
    fireEvent.keyDown(memoryNode, { key: 'Enter' });
    expect(screen.getByRole('heading', { name: 'Memory', level: 3 })).toBeInTheDocument();
    await waitFor(() => {
      expect(transport.requests.some((call) =>
        call.request.pathId === 'memory.graph.get'
        && call.request.query?.plane === 'tags'
        && call.request.query?.focusId === 'memory')).toBe(true);
    });
    expect(within(screen.getByRole('complementary', { name: '标签节点列表' })).queryByText('Group only')).not.toBeInTheDocument();
    const memoryDetails = screen.getByRole('region', { name: 'Memory 详情' });
    expect(await within(memoryDetails).findByText('已存关系')).toBeInTheDocument();
    expect(await within(memoryDetails).findByRole('button', { name: 'Agent Runtime' })).toBeInTheDocument();
    expect(within(memoryDetails).getAllByText('本地记忆').length).toBeGreaterThan(0);

    await user.type(screen.getByRole('textbox', { name: '筛选分组或标签' }), 'Memory');
    await waitFor(() => {
      expect(within(screen.getByRole('complementary', { name: '标签节点列表' })).getAllByRole('button')).toHaveLength(1);
      expect(transport.requests.some((call) =>
        call.request.pathId === 'memory.graph.get'
        && call.request.query?.plane === 'tags'
        && call.request.query?.query === 'Memory'
        && !call.request.query?.focusId)).toBe(true);
    });
    await user.clear(screen.getByRole('textbox', { name: '筛选分组或标签' }));

    await user.click(screen.getByRole('radio', { name: '分组 / 标签' }));
    expect(within(screen.getByRole('complementary', { name: '分组与标签列表' })).getByText('Group only')).toBeInTheDocument();
    const groupNode = screen.getByRole('button', { name: /分组 Agent 工程，12 个成员/ });
    await user.click(groupNode);
    await waitFor(() => {
      expect(transport.requests.some((call) =>
        call.request.pathId === 'memory.graph.get'
        && call.request.query?.plane === 'groups'
        && call.request.query?.focusId === 'agent')).toBe(true);
    });
    const groupDetails = screen.getByRole('region', { name: 'Agent 工程 详情' });
    expect(await within(groupDetails).findByText(/成员 · 当前 2 \/ 12/)).toBeInTheDocument();
    expect(within(groupDetails).getAllByRole('button', { name: 'Agent Runtime' }).length).toBeGreaterThan(0);

    await user.click(screen.getByRole('radio', { name: '分组 / 长期主题' }));
    const bookList = screen.getByRole('complementary', { name: '分组与长期主题列表' });
    expect(within(bookList).getByText('输入法知识册')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /长期主题 输入法知识册，6 条记忆/ }));
    const bookDetails = screen.getByRole('region', { name: '输入法知识册 详情' });
    expect(await within(bookDetails).findByText('候选与上下文')).toBeInTheDocument();
    expect(within(bookDetails).getByText('为保护原始记忆内容，此处只显示成员计数。')).toBeInTheDocument();
    const relationFilter = screen.getByRole('textbox', { name: '筛选分组或标签' });
    await user.type(relationFilter, '不存在的主题');
    expect(await screen.findByText('当前筛选没有匹配分组或长期主题。')).toBeInTheDocument();
    await user.clear(relationFilter);

    await waitFor(() => {
      const entityRequests = transport.requests.filter((call) => call.request.pathId === 'memory.entity.get');
      expect(entityRequests.length).toBeGreaterThanOrEqual(2);
      const bookRequest = entityRequests.find((call) =>
        call.request.params?.kind === 'book' && call.request.params?.entityId === 'book:input');
      expect(bookRequest?.request.query).toMatchObject({ connectionsLimit: 40, membersLimit: 40 });
    });
  });

  it('searches the full backend graph instead of filtering only the first response', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, eventCount: 2, memoryItemCount: 2, memoryBookCount: 0, memoryAtomCount: 0, pendingCompileEvents: 0 },
        'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
        'memory.graph.get': (request: ControlRequest) => {
          const plane = String(request.query?.plane ?? '');
          const query = String(request.query?.query ?? '');
          if (plane === 'tags') {
            const node = query
              ? graphNode('tag:remote', 'tag', '远端标签', 7, '不在首批响应中')
              : graphNode('tag:first', 'tag', '首批标签', 1, '首批响应');
            return graphEnvelope('tags', [node], [], false);
          }
          const group = query
            ? graphNode('group:remote', 'group', '远端分组', 9, '不在首批响应中')
            : graphNode('group:first', 'group', '首批分组', 1, '首批响应');
          return graphEnvelope('groups', [group], [], false);
        },
        'memory.entity.get': (request: ControlRequest) => memoryEntity(
          String(request.params?.kind ?? ''),
          String(request.params?.entityId ?? ''),
        ),
      },
    });
    renderMemory(transport);

    await user.click(await screen.findByRole('tab', { name: '关系图' }));
    expect(await screen.findByRole('button', { name: /首批标签，1 条记忆/ })).toBeInTheDocument();
    const search = screen.getByRole('textbox', { name: '筛选分组或标签' });
    await user.type(search, '远端标签');
    expect(await screen.findByRole('button', { name: /远端标签，7 条记忆/ })).toBeInTheDocument();
    await waitFor(() => {
      expect(transport.requests.some((call) =>
        call.request.pathId === 'memory.graph.get'
        && call.request.query?.plane === 'tags'
        && call.request.query?.query === '远端标签')).toBe(true);
    });

    await user.clear(search);
    await user.click(screen.getByRole('radio', { name: '分组 / 标签' }));
    await user.type(search, '远端分组');
    expect(await screen.findByRole('button', { name: /分组 远端分组，9 个成员/ })).toBeInTheDocument();
    await waitFor(() => {
      expect(transport.requests.some((call) =>
        call.request.pathId === 'memory.graph.get'
        && call.request.query?.plane === 'groups'
        && call.request.query?.query === '远端分组')).toBe(true);
    });
  });

  it('isolates tag and group failures and never exposes a transport error', async () => {
    const user = userEvent.setup();
    const internal = '/Users/private/memory.sqlite GET /api/memory/graph traceback';
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, eventCount: 1, memoryItemCount: 1, memoryBookCount: 0, memoryAtomCount: 0, pendingCompileEvents: 0 },
        'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
        'memory.graph.get': (request: ControlRequest) => {
          if (request.query?.plane === 'tags') return Promise.reject(new Error(internal));
          return memoryGraph('groups');
        },
        'memory.entity.get': (request: ControlRequest) => memoryEntity(
          String(request.params?.kind ?? ''),
          String(request.params?.entityId ?? ''),
        ),
      },
    });
    renderMemory(transport);

    await user.click(await screen.findByRole('tab', { name: '关系图' }));
    expect(await screen.findByText('读取失败')).toBeInTheDocument();
    expect(screen.getByText('当前关系读取失败，请稍后重试。')).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(internal);

    await user.click(screen.getByRole('radio', { name: '分组 / 标签' }));
    expect(await screen.findByRole('button', { name: /分组 Agent 工程，12 个成员/ })).toBeInTheDocument();
    expect(screen.queryByText('读取失败')).not.toBeInTheDocument();
  });

  it('keeps tag relations usable when the group route fails', async () => {
    const user = userEvent.setup();
    const internal = 'sqlite traceback /api/memory/graph groups';
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, eventCount: 1, memoryItemCount: 1, memoryBookCount: 0, memoryAtomCount: 0, pendingCompileEvents: 0 },
        'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
        'memory.graph.get': (request: ControlRequest) => {
          if (request.query?.plane === 'groups') return Promise.reject(new Error(internal));
          return graphEnvelope(
            'tags',
            [graphNode('tag:solo', 'tag', '可用标签', 1, '标签路由正常')],
            [],
            false,
          );
        },
        'memory.entity.get': () => memoryEntity('tag', 'solo'),
      },
    });
    renderMemory(transport);

    await user.click(await screen.findByRole('tab', { name: '关系图' }));
    expect(await screen.findByRole('button', { name: /可用标签，1 条记忆/ })).toBeInTheDocument();
    expect(screen.queryByText('读取失败')).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(internal);

    await user.click(screen.getByRole('radio', { name: '分组 / 标签' }));
    expect(await screen.findByText('读取失败')).toBeInTheDocument();
    expect(screen.getByText('当前关系读取失败，请稍后重试。')).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(internal);
  });

  it('loads additional connection and member pages from their real cursors', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, eventCount: 1, memoryItemCount: 5, memoryBookCount: 0, memoryAtomCount: 0, pendingCompileEvents: 0 },
        'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
        'memory.graph.get': (request: ControlRequest) => request.query?.plane === 'tags'
          ? graphEnvelope('tags', [graphNode('tag:agent', 'tag', 'Agent Runtime', 5, 'Agent 生命周期与工具边界')], [], false)
          : graphEnvelope('groups', [], [], false),
        'memory.entity.get': pagedMemoryEntity,
      },
    });
    renderMemory(transport);

    await user.click(await screen.findByRole('tab', { name: '关系图' }));
    const details = await screen.findByRole('region', { name: 'Agent Runtime 详情' });
    const connectionSection = within(details).getByRole('heading', { name: '已存关系' }).closest('section');
    expect(connectionSection).not.toBeNull();
    await user.click(within(connectionSection as HTMLElement).getByRole('button', { name: '加载更多' }));
    expect(await within(connectionSection as HTMLElement).findByText('深入关系')).toBeInTheDocument();

    const memberSection = within(details).getByRole('heading', { name: /成员 · 当前 1 \/ 5/ }).closest('section');
    expect(memberSection).not.toBeNull();
    await user.click(within(memberSection as HTMLElement).getByRole('button', { name: '加载更多' }));
    expect(await within(memberSection as HTMLElement).findByText('成员二')).toBeInTheDocument();

    expect(transport.requests.some((call) =>
      call.request.pathId === 'memory.entity.get'
      && call.request.query?.connectionsCursor === '40')).toBe(true);
    expect(transport.requests.some((call) =>
      call.request.pathId === 'memory.entity.get'
      && call.request.query?.membersCursor === '40')).toBe(true);
  });

  it('shows isolated nodes without inventing an edge', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, eventCount: 1, memoryItemCount: 1, memoryBookCount: 0, memoryAtomCount: 0, pendingCompileEvents: 0 },
        'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
        'memory.graph.get': (request: ControlRequest) => {
          const plane = String(request.query?.plane ?? '');
          return plane === 'tags'
            ? graphEnvelope('tags', [graphNode('tag:solo', 'tag', '孤立标签', 1, '没有边')], [], false)
            : graphEnvelope('groups', [], [], false);
        },
        'memory.entity.get': () => memoryEntity('tag', 'solo'),
      },
    });
    renderMemory(transport);
    await user.click(await screen.findByRole('tab', { name: '关系图' }));
    expect(await screen.findByText('1 项记忆目前没有已记录关系。')).toBeInTheDocument();
    expect(document.querySelectorAll('.memory-graph__edge')).toHaveLength(0);
  });

  it('keeps the readable relationship network separate from unconnected tags', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, eventCount: 3, memoryItemCount: 3, memoryBookCount: 0, memoryAtomCount: 0, pendingCompileEvents: 0 },
        'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
        'memory.graph.get': (request: ControlRequest) => {
          const plane = String(request.query?.plane ?? '');
          if (plane !== 'tags') return graphEnvelope('groups', [], [], false);
          return graphEnvelope(
            'tags',
            [
              graphNode('tag:agent', 'tag', 'Agent', 5, 'Agent 运行'),
              graphNode('tag:memory', 'tag', '记忆', 8, '长期记忆'),
              graphNode('tag:isolated', 'tag', '未连接标签', 2, '尚未形成证据关系'),
            ],
            [{
              id: 'edge:agent-memory',
              kind: 'tagRelation',
              sourceId: 'tag:agent',
              targetId: 'tag:memory',
              sourceKind: 'tag',
              targetKind: 'tag',
              relation: 'related_to',
              weight: .9,
              directionBias: 0,
              evidenceCount: 3,
              source: 'sqlite',
              updatedAtMs: 1,
            }],
            false,
          );
        },
        'memory.entity.get': (request: ControlRequest) => memoryEntity(
          String(request.params?.kind ?? ''),
          String(request.params?.entityId ?? ''),
        ),
      },
    });
    renderMemory(transport);

    await user.click(await screen.findByRole('tab', { name: '关系图' }));
    const canvas = document.querySelector('.memory-relation-canvas');
    expect(canvas).toHaveAttribute('data-node-count', '2');
    expect(canvas).toHaveAttribute('data-cluster-count', '1');
    expect(screen.getByRole('button', { name: '显示 1 个未连接节点' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /未连接标签，2 条记忆/ })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '显示 1 个未连接节点' }));
    expect(document.querySelector('.memory-relation-canvas')).toHaveAttribute('data-node-count', '3');
    expect(document.querySelector('.memory-relation-canvas')).toHaveAttribute('data-cluster-count', '2');
    expect(screen.getByRole('button', { name: '隐藏 1 个未连接节点' })).toBeInTheDocument();
  });

  it('fails with public error copy and never renders internal memory fields', async () => {
    const user = userEvent.setup();
    const internal = '/Users/private/memory.sqlite SELECT prompt reasoning rawJson';
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': { ok: true, eventCount: 1, memoryItemCount: 1, memoryBookCount: 0, memoryAtomCount: 0, pendingCompileEvents: 0 },
        'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
        'memory.graph.get': (request: ControlRequest) => {
          const plane = String(request.query?.plane ?? '');
          if (plane === 'groups') return graphEnvelope('groups', [], [], false);
          return graphEnvelope('tags', [
            graphNode('tag:solo', 'tag', '孤立标签', 1, '公开摘要'),
          ], [], false);
        },
        'memory.entity.get': () => Promise.reject(new Error(internal)),
      },
    });
    renderMemory(transport);

    await user.click(await screen.findByRole('tab', { name: '关系图' }));
    expect(await screen.findByRole('heading', { name: '孤立标签', level: 3 })).toBeInTheDocument();
    expect(await screen.findByRole('alert')).toHaveTextContent('实体详情读取失败，请稍后重试。');
    expect(document.body).not.toHaveTextContent(internal);
    expect(document.body).not.toHaveTextContent('reasoning');
    expect(document.body).not.toHaveTextContent('rawJson');
  });

  it('archives and rolls back a real topic book through the bound work contract', async () => {
    const user = userEvent.setup();
    const payloadSha256 = `sha256:${'c'.repeat(64)}`;
    const transport = new MockControlTransport({
      routes: {
        'memory.summary': {
          ok: true,
          runtimeRevision: 7,
          eventCount: 18,
          memoryItemCount: 14,
          memoryBookCount: 4,
          memoryAtomCount: 10,
          pendingCompileEvents: 2,
        },
        'memory.pages': (request: ControlRequest) => memoryPage(String(request.params?.kind ?? '')),
        'memory.book.archive.preview': (request: ControlRequest) => {
          expect(request.body).toEqual({
            bookId: 'book-1',
            archived: true,
            reason: 'control_center_archive',
            expectedRuntimeRevision: 7,
          });
          return {
            schemaVersion: 'rag-ime.management-work-preview.v1',
            ok: true,
            previewToken: 'preview-memory-book',
            pathId: 'memory.book.archive.apply',
            payloadSha256,
            expectedRevision: { runtimeRevision: 7, subjectRevision: 'sha256:book-before' },
            expiresAtMs: Date.now() + 60_000,
            requiredConfirm: 'apply',
            summary: {
              title: '归档主题记忆',
              items: ['主题：控制中心迁移', '归档后不再自动参考。'],
              risk: 'R2',
            },
          };
        },
        'memory.book.archive.apply': (request: ControlRequest) => {
          expect(request.body).toMatchObject({
            bookId: 'book-1',
            archived: true,
            reason: 'control_center_archive',
            expectedRuntimeRevision: 7,
            previewToken: 'preview-memory-book',
            payloadSha256,
            confirmText: 'apply',
          });
          return workReceipt('memory.book.archive.apply', payloadSha256, true);
        },
        'memory.book.archive.rollback': (request: ControlRequest) => {
          expect(request.body).toEqual({
            receiptId: 'receipt-memory-book',
            rollbackToken: 'rollback-memory-book',
            payloadSha256,
            confirmText: 'rollback',
          });
          return workReceipt('memory.book.archive.rollback', payloadSha256, false);
        },
      },
    });
    renderMemory(transport);

    const pipelineStages = await screen.findByRole('list', { name: '记忆内容分类' });
    await user.click(within(pipelineStages).getByRole('button', { name: /主题/ }));
    const book = await screen.findByRole('button', { name: /控制中心迁移/ });
    await user.click(book);
    const details = screen.getByRole('region', { name: '控制中心迁移 主题页' });
    await user.click(within(details).getByText('查看已有摘要', { selector: 'summary' }));
    expect(within(details).getByText('React 页面与受控接口')).toBeInTheDocument();
    expect(screen.getByText('归档长期主题，以后不再自动参考；事实和来源关系仍会保留。')).toBeInTheDocument();
    expect(screen.queryByText('book-1')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '归档长期主题' }));
    expect(await screen.findByText('已保存')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '撤销' }));
    expect(await screen.findByText('已撤销')).toBeInTheDocument();

    expect(transport.requests.filter((call) => call.request.pathId === 'memory.book.archive.preview')).toHaveLength(1);
    expect(transport.requests.filter((call) => call.request.pathId === 'memory.book.archive.apply')).toHaveLength(1);
    expect(transport.requests.filter((call) => call.request.pathId === 'memory.book.archive.rollback')).toHaveLength(1);
  });

  it('fails closed without archive routes and does not show an implementation placeholder', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      capabilities: {
        routeIds: ['memory.summary', 'memory.pages', 'memory.graph.get', 'memory.entity.get'],
      },
      routes: {
        'memory.summary': {
          ok: true,
          runtimeRevision: 7,
          eventCount: 18,
          memoryItemCount: 14,
          memoryBookCount: 4,
          memoryAtomCount: 10,
          pendingCompileEvents: 2,
        },
        'memory.pages': (request: ControlRequest) => memoryPage(String(request.params?.kind ?? '')),
      },
    });
    renderMemory(transport);

    const pipelineStages = await screen.findByRole('list', { name: '记忆内容分类' });
    await user.click(within(pipelineStages).getByRole('button', { name: /主题/ }));
    await user.click(await screen.findByRole('button', { name: /控制中心迁移/ }));
    expect(await screen.findByText('管理长期主题')).toBeInTheDocument();
    expect(screen.getByText('当前不可用')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /归档长期主题|恢复长期主题|暂未开放/ })).not.toBeInTheDocument();
    expect(screen.queryByText('后端暂不支持')).not.toBeInTheDocument();
    expect(transport.requests.some((call) => call.request.pathId.startsWith('memory.book.archive.'))).toBe(false);
  });
});

describe('MemoryFeature preferences', () => {
  it('lets PAWOS own Memory navigation without repeating the legacy view tabs', async () => {
    const transport = new MockControlTransport({
      capabilities: { features: { managementWorkContract: true, configurationSettingsWorkContract: true } },
      routes: {
        'memory.summary': { ok: true, total: 0 },
        'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
        'configuration.settings': {
          ok: true,
          settings: {
            memory: {
              enabled: false,
              timeDecay: { temporaryHalfLifeDays: 14, stablePreferenceHalfLifeDays: 365 },
              recall: { detailLevel: 'balanced' },
              automaticOrganization: { includeAgentDialogue: true },
            },
          },
          runtimeRevision: 4,
        },
      },
    });
    renderMemory(transport, '/memory?view=preferences', true);

    expect(await screen.findByRole('heading', { name: '记忆偏好' })).toBeInTheDocument();
    expect(await screen.findByRole('switch', { name: '启用记忆增强' })).not.toBeChecked();
    expect(screen.queryByRole('tablist', { name: '记忆视图' })).not.toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: '记忆页面' })).not.toBeInTheDocument();
    expect(screen.queryByRole('list', { name: '记忆内容分类' })).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: '我的记忆' })).toHaveAttribute('data-paw-os-app', 'memory');
    expect(document.querySelector('.mgmt-page__header')).not.toBeInTheDocument();
  });

  it('saves second-brain preferences through the governed settings authority and reloads them', async () => {
    const user = userEvent.setup();
    let memoryEnabled = true;
    let stablePreferenceHalfLifeDays = 365;
    let includeAgentDialogue = true;
    let timelineEnabled = true;
    let timelineMaxItems = 2;
    let settingsReads = 0;
    const payloadSha256 = 'a'.repeat(64);
    const transport = new MockControlTransport({
      capabilities: {
        features: {
          managementWorkContract: true,
          configurationSettingsWorkContract: true,
        },
      },
      routes: {
        'memory.summary': { ok: true, memoryBookCount: 2, memoryAtomCount: 8 },
        'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
        'configuration.settings': () => {
          settingsReads += 1;
          return {
            ok: true,
            settings: {
              memory: {
                enabled: memoryEnabled,
                timeDecay: { temporaryHalfLifeDays: 14, stablePreferenceHalfLifeDays },
                automaticOrganization: { includeAgentDialogue },
                recall: { detailLevel: 'compact', timelineEnabled, timelineMaxItems },
              },
            },
            runtimeConfig: { runtimeRevision: 12 },
          };
        },
        'configuration.settings.preview': (request: ControlRequest) => {
          expect(request.body).toEqual({
            changes: {
              'memory.enabled': false,
              'memory.automaticOrganization.includeAgentDialogue': false,
              'memory.recall.timelineEnabled': false,
              'memory.recall.timelineMaxItems': 4,
              'memory.timeDecay.stablePreferenceHalfLifeDays': 730,
            },
            expectedRuntimeRevision: 12,
          });
          return {
            schemaVersion: 'rag-ime.management-work-preview.v1',
            ok: true,
            previewToken: 'memory-preferences-preview',
            pathId: 'configuration.settings.apply',
            payloadSha256,
            expectedRevision: { runtimeRevision: 12, subjectRevision: 'settings:12' },
            expiresAtMs: Date.now() + 60_000,
            requiredConfirm: 'apply',
            summary: { title: '保存记忆偏好', items: ['更新两项记忆偏好'], risk: 'R1' },
          };
        },
        'configuration.settings.apply': (request: ControlRequest) => {
          const body = request.body as Record<string, unknown>;
          expect(body).toMatchObject({
            changes: {
              'memory.enabled': false,
              'memory.automaticOrganization.includeAgentDialogue': false,
              'memory.recall.timelineEnabled': false,
              'memory.recall.timelineMaxItems': 4,
              'memory.timeDecay.stablePreferenceHalfLifeDays': 730,
            },
            expectedRuntimeRevision: 12,
            previewToken: 'memory-preferences-preview',
            payloadSha256,
            confirmText: 'apply',
          });
          stablePreferenceHalfLifeDays = 730;
          memoryEnabled = false;
          includeAgentDialogue = false;
          timelineEnabled = false;
          timelineMaxItems = 4;
          return {
            schemaVersion: 'rag-ime.management-work-receipt.v1',
            ok: true,
            receiptId: 'memory-preferences-receipt',
            pathId: 'configuration.settings.apply',
            payloadSha256,
            appliedAtMs: Date.now(),
            rollbackAvailable: true,
            rollbackToken: 'memory-preferences-rollback',
          };
        },
      },
    });
    renderMemory(transport, '/memory?view=preferences');

    expect(await screen.findByRole('heading', { name: '记忆偏好' })).toBeInTheDocument();
    expect(await screen.findByRole('combobox', { name: '稳定偏好' })).toHaveTextContent('正常记住');
    await user.click(await screen.findByRole('switch', { name: '启用记忆增强' }));
    await user.click(screen.getByRole('combobox', { name: '稳定偏好' }));
    await user.click(await screen.findByRole('option', { name: '优先记住' }));
    await user.click(screen.getByRole('switch', { name: '让 Agent 对话摘要参与整理' }));
    await user.click(screen.getByRole('switch', { name: '按需召回时间线' }));
    await user.click(screen.getByRole('combobox', { name: '时间线召回上限' }));
    await user.click(await screen.findByRole('option', { name: '4 条' }));
    await user.click(screen.getByRole('button', { name: '保存记忆偏好' }));

    expect(await screen.findByText('记忆偏好已保存')).toBeInTheDocument();
    expect(settingsReads).toBeGreaterThanOrEqual(2);
    expect(screen.getByRole('combobox', { name: '稳定偏好' })).toHaveTextContent('优先记住');
    expect(screen.getByRole('switch', { name: '启用记忆增强' })).not.toBeChecked();
    expect(screen.getByRole('switch', { name: '让 Agent 对话摘要参与整理' })).not.toBeChecked();
    expect(screen.getByRole('switch', { name: '按需召回时间线' })).not.toBeChecked();
    expect(screen.getByRole('combobox', { name: '时间线召回上限' })).toHaveTextContent('4 条');
    expect(screen.getByText('每个压缩周期最多调用一次')).toBeInTheDocument();
  });

  it('keeps unsaved choices visible and reports an authority write failure', async () => {
    const user = userEvent.setup();
    const payloadSha256 = 'b'.repeat(64);
    const transport = new MockControlTransport({
      capabilities: {
        features: { managementWorkContract: true, configurationSettingsWorkContract: true },
      },
      routes: {
        'memory.summary': { ok: true, memoryBookCount: 0, memoryAtomCount: 0 },
        'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
        'configuration.settings': {
          ok: true,
          settings: {
            memory: {
              timeDecay: { temporaryHalfLifeDays: 14, stablePreferenceHalfLifeDays: 365 },
              automaticOrganization: { includeAgentDialogue: true },
              recall: { detailLevel: 'compact' },
            },
          },
          runtimeConfig: { runtimeRevision: 4 },
        },
        'configuration.settings.preview': {
          schemaVersion: 'rag-ime.management-work-preview.v1',
          ok: true,
          previewToken: 'memory-preferences-preview-failure',
          pathId: 'configuration.settings.apply',
          payloadSha256,
          expectedRevision: { runtimeRevision: 4, subjectRevision: 'settings:4' },
          expiresAtMs: Date.now() + 60_000,
          requiredConfirm: 'apply',
          summary: { title: '保存记忆偏好', items: ['更新临时事项偏好'], risk: 'R1' },
        },
        'configuration.settings.apply': () => { throw new Error('本机设置刚刚发生变化，请重新读取后重试。'); },
      },
    });
    renderMemory(transport, '/memory?view=preferences');

    await user.click(await screen.findByRole('combobox', { name: '临时事项' }));
    await user.click(await screen.findByRole('option', { name: '尽快忘记' }));
    await user.click(screen.getByRole('button', { name: '保存记忆偏好' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('记忆偏好没有保存');
    expect(screen.getByRole('alert')).toHaveTextContent('本机设置刚刚发生变化');
    expect(screen.getByRole('combobox', { name: '临时事项' })).toHaveTextContent('尽快忘记');
    expect(screen.queryByText('记忆偏好已保存')).not.toBeInTheDocument();
  });
});

describe('MemoryFeature catalog cold lead', () => {
  it('cold-open explains the settle path and hands 交给 Agent 一件事 to the desktop route', async () => {
    const openRoute = vi.fn();
    const user = userEvent.setup();
    renderMemoryOnDesktop(emptyCatalogTransport(), openRoute);

    expect(await screen.findByText('记忆从工作回执沉淀')).toBeInTheDocument();
    expect(screen.getByText(
      '交给 Agent 一件事，工作留下来源记录，分批审核后沉淀成这里的记忆；每条记忆之后被哪些 Session 装配，都能在它的详情里看到。',
    )).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '交给 Agent 一件事' }));
    expect(openRoute).toHaveBeenCalledWith('/agent');
  });

  it('keeps the filter hint for filtered misses and never renders an entry it cannot open', async () => {
    const user = userEvent.setup();
    renderMemory(emptyCatalogTransport());

    // 脱离桌面时冷启动引导仍然讲链路，但不渲染打不开的 Agent 入口。
    expect(await screen.findByText('记忆从工作回执沉淀')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '交给 Agent 一件事' })).not.toBeInTheDocument();

    await user.type(screen.getByPlaceholderText('标题、正文或标签，输入即筛选'), '没有这条记忆');
    expect(await screen.findByText('没有匹配结果')).toBeInTheDocument();
    expect(screen.queryByText('记忆从工作回执沉淀')).not.toBeInTheDocument();
  });

  it('gives the evidence layer its own receipt-first lead', async () => {
    const user = userEvent.setup();
    renderMemory(emptyCatalogTransport());

    await user.click(await screen.findByRole('button', { name: '来源 · 来源记录 · 0 项' }));
    expect(await screen.findByText('还没有来源记录')).toBeInTheDocument();
    expect(screen.getByText(
      '来源记录是工作留下的回执：输入法、语音与伙伴主动记录先落在这里，经分批审核后才沉淀为记忆。',
    )).toBeInTheDocument();
  });
});

function emptyCatalogTransport(): MockControlTransport {
  return new MockControlTransport({
    routes: {
      'memory.summary': { ok: true, memoryBookCount: 0, memoryAtomCount: 0 },
      'memory.pages': { ok: true, items: [], nextCursor: '', limit: 50 },
    },
  });
}

function renderMemoryOnDesktop(transport: MockControlTransport, openRoute: (route: string) => void) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <MemoryRouter initialEntries={['/memory?layer=atoms']}>
      <MemoryLocationProbe />
      <TooltipProvider delayDuration={0}>
        <ControlTransportProvider transport={transport}>
          <PawOsDesktopProvider openRoute={openRoute} openWindow={() => {}}>
            <PawOsAppSurfaceProvider appId="memory" height={720} width={1_080}>
              <QueryClientProvider client={client}><MemoryFeature /></QueryClientProvider>
            </PawOsAppSurfaceProvider>
          </PawOsDesktopProvider>
        </ControlTransportProvider>
      </TooltipProvider>
    </MemoryRouter>,
  );
}

function renderMemory(transport: MockControlTransport, initialEntry = '/memory?layer=atoms', pawOs = false) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <MemoryLocationProbe />
      <TooltipProvider delayDuration={0}>
        <ControlTransportProvider transport={transport}>
          {pawOs ? (
            <PawOsAppSurfaceProvider appId="memory" height={720} width={1_080}>
              <QueryClientProvider client={client}><MemoryFeature /></QueryClientProvider>
            </PawOsAppSurfaceProvider>
          ) : <QueryClientProvider client={client}><MemoryFeature /></QueryClientProvider>}
        </ControlTransportProvider>
      </TooltipProvider>
    </MemoryRouter>,
  );
}

function MemoryLocationProbe() {
  const location = useLocation();
  return <output data-testid="memory-location" hidden>{`${location.pathname}${location.search}`}</output>;
}

function activityTimeline(status: string, date: string): Record<string, unknown> {
  const [year, month, day] = date.split('-').map(Number);
  const start = new Date(year, month - 1, day, 9, 0).getTime();
  const segment = (position: number, app: string, summary: string) => ({
    segmentId: `segment:${position}`,
    position,
    app,
    sourceKinds: ['squirrel_input_segment', 'pi_agent'],
    contextGroupIds: ['group:personal-context'],
    startMs: start + position * 3_600_000,
    endMs: start + (position + 1) * 3_600_000,
    activityKind: 'consolidated_activity',
    spanSemantics: 'first_to_last_source_event',
    eventCount: 4,
    sourceEventIds: [position + 1],
    sourceEventHash: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    summary,
    redactedEventCount: 0,
  });
  return {
    schemaVersion: 'rag-ime.daily-activity-timeline.v1',
    timelineId: `timeline:${date}`,
    project: 'wisdom-weasel-rag-ime',
    date,
    timezone: 'Asia/Shanghai',
    status,
    sourceEventIds: [1, 2],
    sourceEventHash: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    segments: [
      segment(0, 'com.openai.codex', '实现最终输入框捕获并核对三条记忆消费路径。'),
      segment(1, 'com.mitchellh.ghostty', '运行后端、Web 与输入法验证。'),
    ],
    summary: '当天完成个人上下文主链改造与验证。',
    eventCount: 8,
    segmentCount: 2,
    observedStartMs: start,
    observedEndMs: start + 2 * 3_600_000,
    spanSemantics: 'first_to_last_source_event',
    ordinaryActivityCount: 0,
    consolidatedActivityCount: 2,
    approvedBookId: '',
    approvedBy: status === 'approved' ? 'control-center-user' : '',
    approvedAtMs: status === 'approved' ? start + 10_000 : 0,
    createdAtMs: start,
    updatedAtMs: start + 20_000,
    policy: {
      derivedFromInputEvents: true,
      longTermFact: false,
      automaticPromotion: true,
      explicitApprovalRequired: false,
      minimumConsolidatedSpanMs: 30 * 60_000,
    },
  };
}

function localCalendarDate(): string {
  const now = new Date();
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

function memoryCurationStatus(): Record<string, unknown> {
  return {
    ok: true,
    policy: 'review',
    autoApply: false,
    pendingDraftCount: 1,
    runs: [{ runId: 'memory_book_test', status: 'draft', diffCount: 2, createdAtMs: 1_784_006_400_000 }],
  };
}

function memoryCurationRun(): Record<string, unknown> {
  return {
    ok: true,
    stale: false,
    canApply: true,
    canRollback: false,
    run: {
      runId: 'memory_book_test',
      status: 'draft',
      createdAtMs: 1_784_006_400_000,
      diffCount: 2,
      pendingDiffCount: 1,
      changes: [
        {
          diffId: 1,
          operation: 'upsert_memory_atom',
          operationLabel: '更新记忆条目',
          status: 'pending',
          selected: true,
          title: '输入封口边界',
          detail: 'Backspace 修改缓冲区，Enter 后才写入完整段落。',
          sourceCount: 3,
        },
        {
          diffId: 2,
          operation: 'merge_semantic_tag',
          operationLabel: '合并标签',
          status: 'rejected',
          selected: false,
          title: '合并输入法同义标签',
          detail: '保留用户命名作为别名。',
          sourceCount: 2,
        },
      ],
    },
  };
}

function memoryGraph(plane: string): Record<string, unknown> {
  const tagAgent = graphNode('tag:agent', 'tag', 'Agent Runtime', 11, 'Agent 生命周期与工具边界');
  const tagMemory = graphNode('tag:memory', 'tag', 'Memory', 5, '记忆组织与检索');
  if (plane === 'tags') {
    return graphEnvelope('tags', [tagAgent, tagMemory], [{
      id: 'edge:agent-memory',
      kind: 'tagRelation',
      sourceId: 'tag:agent',
      targetId: 'tag:memory',
      sourceKind: 'tag',
      targetKind: 'tag',
      relation: 'related_to',
      weight: 0.9,
      directionBias: 0,
      evidenceCount: 6,
      source: 'sqlite',
      updatedAtMs: 1,
    }], true);
  }
  const groupOnly = graphNode('tag:group-only', 'tag', 'Group only', 3, '只由 Group plane 返回');
  const book = graphNode('book:input', 'book', '输入法知识册', 6, '候选与上下文');
  return graphEnvelope(
    'groups',
    [graphNode('group:agent', 'group', 'Agent 工程', 12, 'Agent 陪伴与恢复'), tagAgent, tagMemory, groupOnly, book],
    [...[tagAgent, tagMemory, groupOnly].map((tag) => ({
      id: `membership:${String(tag.id)}`,
      kind: 'groupMember',
      sourceId: 'group:agent',
      targetId: tag.id,
      sourceKind: 'group',
      targetKind: 'tag',
      relation: 'contains',
      weight: 1,
      directionBias: 0,
      evidenceCount: 1,
      source: 'sqlite',
      updatedAtMs: 1,
    })), {
      id: 'membership:book:input',
      kind: 'groupMember',
      sourceId: 'group:agent',
      targetId: book.id,
      sourceKind: 'group',
      targetKind: 'book',
      relation: 'contains',
      weight: .8,
      directionBias: 1,
      evidenceCount: 1,
      source: 'sqlite',
      updatedAtMs: 1,
    }],
    false,
  );
}

function graphEnvelope(
  plane: 'tags' | 'groups',
  nodes: Record<string, unknown>[],
  edges: Record<string, unknown>[],
  truncated: boolean,
): Record<string, unknown> {
  return {
      schemaVersion: 'rag-ime.memory-graph.v1',
    ok: true,
    settingsRevision: 'settings:test',
    runtimeRevision: 1,
    graphRevision: `sha256:${'a'.repeat(64)}`,
    plane,
    project: 'wisdom-weasel-rag-ime',
    filters: { status: 'active', query: '', focusId: '', minWeight: 0 },
    nodes,
    edges,
    truncated: { nodes: truncated, edges: false },
    limits: { nodeLimit: 80, edgeLimit: 160, depth: 1 },
  };
}

function graphNode(
  id: string,
  kind: 'tag' | 'group' | 'book',
  label: string,
  memberCount: number,
  description: string,
): Record<string, unknown> {
  return {
    id,
    entityId: kind === 'book' ? id : id.split(':').at(-1),
    kind,
    label,
    description,
    color: kind === 'group' ? 'blue' : 'teal',
    status: 'active',
    source: 'sqlite',
    project: 'wisdom-weasel-rag-ime',
    qualityScore: 1,
    memberCount,
    edgeCount: 1,
    updatedAtMs: 1,
  };
}

function memoryPage(kind: string): Record<string, unknown> {
  if (kind === 'tags') {
    return {
      ok: true,
      items: [
        {
          id: 'tag-agent',
          tag: 'Agent Runtime',
          description: 'Agent 生命周期与工具边界',
          item_count: 11,
          edge_count: 1,
          color_token: 'teal',
          connections: [{ id: 'tag-memory', tag: 'Memory', type: 'related_to', weight: 0.9, evidenceCount: 6 }],
        },
        {
          id: 'tag-memory',
          tag: 'Memory',
          description: '记忆组织与检索',
          item_count: 5,
          edge_count: 1,
          color_token: 'green',
          connections: [{ id: 'tag-agent', tag: 'Agent Runtime', type: 'related_to', weight: 0.9, evidenceCount: 6 }],
        },
      ],
      nextCursor: 'tag-next',
      limit: 50,
    };
  }
  if (kind === 'groups') {
    return {
      ok: true,
      items: [
        {
          id: 'group:agent',
          title: 'Agent 工程',
          note: 'Agent 陪伴与恢复',
          tags: ['Agent Runtime', 'Memory'],
          event_count: 12,
          color_token: 'blue',
        },
      ],
      nextCursor: '',
      limit: 50,
    };
  }
  return {
    ok: true,
    items: [{
      id: 'book-1',
      type: 'topic',
      title: '控制中心迁移',
      summary: 'React 页面与受控接口',
      status: 'active',
      source: 'dsv4',
      tags: ['控制中心', '迁移'],
      updatedAtMs: 1_900_000_100_020,
    }],
    nextCursor: '',
    limit: 50,
  };
}

function workReceipt(pathId: string, payloadSha256: string, rollbackAvailable: boolean) {
  return {
    schemaVersion: 'rag-ime.management-work-receipt.v1',
    ok: true,
    receiptId: pathId.endsWith('rollback') ? 'receipt-memory-book-rollback' : 'receipt-memory-book',
    pathId,
    payloadSha256,
    appliedAtMs: Date.now(),
    auditId: 11,
    rollbackAvailable,
    rollbackToken: rollbackAvailable ? 'rollback-memory-book' : '',
    rollbackAuthority: { bookId: 'book-1' },
    restartComponents: [],
    result: { status: pathId.endsWith('rollback') ? 'active' : 'archived' },
  };
}

function memoryEntity(kind: string, entityId: string): Record<string, unknown> {
  const isGroup = kind === 'group';
  const isBook = kind === 'book';
  const label = isGroup ? 'Agent 工程' : isBook ? '输入法知识册' : entityId === 'memory' ? 'Memory' : entityId === 'solo' ? '孤立标签' : 'Agent Runtime';
  const node = graphNode(`${kind}:${entityId}`, isGroup ? 'group' : isBook ? 'book' : 'tag', label, isGroup ? 12 : isBook ? 6 : 5, isBook ? '候选与上下文' : `${label} detail`);
  const relatedTag = graphNode('tag:agent', 'tag', 'Agent Runtime', 11, 'Agent 生命周期与工具边界');
  const relatedMemory = graphNode('tag:memory', 'tag', 'Memory', 5, '记忆组织与检索');
  const connections = isGroup || entityId === 'solo' ? [] : isBook ? [{
    node: graphNode('group:agent', 'group', 'Agent 工程', 12, 'Agent 陪伴与恢复'),
    edge: entityEdge('group-member:book', 'groupMember', 'group:agent', 'book:book:input', 'contains'),
  }] : [{
    node: entityId === 'memory' ? relatedTag : relatedMemory,
    edge: entityEdge('tag-relation:test', 'tagRelation', `tag:${entityId}`, entityId === 'memory' ? 'tag:agent' : 'tag:memory', 'related_to'),
  }];
  const members = isGroup ? [relatedTag, relatedMemory].map((member) => ({
    node: member,
    edge: entityEdge(`group-member:${String(member.id)}`, 'groupMember', 'group:agent', String(member.id), 'contains'),
  })) : [];
  return {
    schemaVersion: 'rag-ime.memory-entity.v1',
    ok: true,
    settingsRevision: 'settings:test',
    runtimeRevision: 1,
    kind,
    entityId,
    entityRevision: `sha256:${'b'.repeat(64)}`,
    project: 'wisdom-weasel-rag-ime',
    entity: node,
    attributes: { type: isGroup ? 'semantic' : isBook ? 'topic' : 'concept', aliases: isGroup || isBook ? [] : ['记忆'], tags: isBook ? ['输入法'] : [] },
    connections: { items: connections, nextCursor: '', limit: 40, hasMore: false },
    members: { items: members, nextCursor: '', limit: 40, hasMore: false },
    limits: { connectionsLimit: 40, membersLimit: 40 },
  };
}

function pagedMemoryEntity(request: ControlRequest): Record<string, unknown> {
  const kind = String(request.params?.kind ?? 'tag');
  const entityId = String(request.params?.entityId ?? 'agent');
  const payload = memoryEntity(kind, entityId);
  const connectionsCursor = String(request.query?.connectionsCursor ?? '');
  const membersCursor = String(request.query?.membersCursor ?? '');
  const relatedMemory = graphNode('tag:memory', 'tag', 'Memory', 5, '记忆组织与检索');
  const deepRelation = graphNode('tag:deep', 'tag', '深入关系', 3, '第二页关系');
  const memberOne = graphNode('tag:member-one', 'tag', '成员一', 1, '第一页成员');
  const memberTwo = graphNode('tag:member-two', 'tag', '成员二', 1, '第二页成员');

  if (connectionsCursor) {
    payload.connections = {
      items: [{
        node: deepRelation,
        edge: entityEdge('tag-relation:deep', 'tagRelation', 'tag:agent', 'tag:deep', 'related_to'),
      }],
      nextCursor: '',
      limit: 40,
      hasMore: false,
    };
    payload.members = { items: [], nextCursor: '', limit: 1, hasMore: false };
    return payload;
  }
  if (membersCursor) {
    payload.connections = { items: [], nextCursor: '', limit: 1, hasMore: false };
    payload.members = {
      items: [{
        node: memberTwo,
        edge: entityEdge('tag-member:two', 'groupMember', 'group:test', 'tag:member-two', 'contains'),
      }],
      nextCursor: '',
      limit: 40,
      hasMore: false,
    };
    return payload;
  }

  payload.connections = {
    items: [{
      node: relatedMemory,
      edge: entityEdge('tag-relation:memory', 'tagRelation', 'tag:agent', 'tag:memory', 'related_to'),
    }],
    nextCursor: '40',
    limit: 40,
    hasMore: true,
  };
  payload.members = {
    items: [{
      node: memberOne,
      edge: entityEdge('tag-member:one', 'groupMember', 'group:test', 'tag:member-one', 'contains'),
    }],
    nextCursor: '40',
    limit: 40,
    hasMore: true,
  };
  return payload;
}

function entityEdge(id: string, kind: 'tagRelation' | 'groupMember', sourceId: string, targetId: string, relation: string) {
  return {
    id,
    kind,
    sourceId,
    targetId,
    sourceKind: kind === 'groupMember' ? 'group' : 'tag',
    targetKind: 'tag',
    relation,
    weight: .9,
    directionBias: 0,
    evidenceCount: 2,
    source: 'sqlite',
    updatedAtMs: 1,
  };
}
