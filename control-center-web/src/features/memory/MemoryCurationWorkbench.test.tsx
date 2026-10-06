import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport, type MockRouteHandler } from '@/test/mock-transport';
import { MemoryCurationWorkbench } from './MemoryCurationWorkbench';
import { MemoryLibraryNavigation } from './MemoryLibraryNavigation';
import { memoryQueryKeys } from './api';

afterEach(cleanup);

function status(owner: Record<string, unknown> = {}) {
  return { ok: true, policy: 'auto_governed', autoApply: true, runs: [], ownerCuration: owner };
}

function renderWorkbench(run: MockRouteHandler, lifecycle: MockRouteHandler = { counts: {} }, prepareClient?: (client: QueryClient) => void) {
  const transport = new MockControlTransport({ routes: {
    'memory.summary': {},
    'agent.memoryMaintenance.run': run,
    'agent.memoryMaintenance.trigger': { jobId: 'curation-test', state: 'queued' },
    'memory.lifecycle.status': lifecycle,
  } });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  prepareClient?.(client);
  render(<ControlTransportProvider transport={transport}><QueryClientProvider client={client}>
    <MemoryCurationWorkbench enabled onOpenTimeline={vi.fn()} />
  </QueryClientProvider></ControlTransportProvider>);
  return transport;
}

describe('Memory curation progress and recovery', () => {
  it('does not call missing counters zero or claim curation is complete', async () => {
    renderWorkbench(status({ scopes: [] }));
    expect(await screen.findByText('整理进度待确认')).toBeVisible();
    expect(screen.queryByText('已经整理到今天')).not.toBeInTheDocument();
    expect(screen.queryByText('100%')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '开始整理' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '重新读取整理状态' })).toBeEnabled();
  });

  it('shows the known backlog without inventing a denominator or empty facets', async () => {
    renderWorkbench(status({ pendingSourceCount: 8 }));
    expect(await screen.findByText('8 条来源待整理；日期和应用分布尚未提供。')).toBeVisible();
    const progress = screen.getByRole('progressbar', { name: '记忆来源整理进度待确认' });
    expect(progress).not.toHaveAttribute('aria-valuenow');
    expect(screen.queryByText('已处理 0 / 8')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '开始整理' })).toBeEnabled();
  });

  it('keeps the last completed source count visible when a batch fails', async () => {
    const user = userEvent.setup();
    const transport = renderWorkbench((request: ControlRequest) => request.query?.jobId
      ? { jobId: 'curation-test', state: 'failed', error: '模型请求超时', progress: {
        phase: 'owner_memory_curation', processedSourceCount: 6, completedSourceCount: 6, totalSourceCount: 10, pendingSourceCount: 4,
      } }
      : status({ pendingSourceCount: 10, scopes: [{ totalSourceCount: 10 }] }));
    await user.click(await screen.findByRole('button', { name: '开始整理' }));
    expect(await screen.findByText('本轮已处理 6 条 · 回执剩余 4 条')).toBeVisible();
    expect(screen.getByRole('button', { name: '继续整理' })).toBeEnabled();
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.memoryMaintenance.trigger')).toHaveLength(1);
  });

  it('rereads a lost progress receipt without submitting another curation job', async () => {
    const user = userEvent.setup();
    let reads = 0;
    const transport = renderWorkbench((request: ControlRequest) => {
      if (request.query?.jobId) {
        if (++reads === 1) throw new Error('Failed to fetch');
        return { jobId: 'curation-test', state: 'completed', progress: { phase: 'owner_memory_curation', processedSourceCount: 6, pendingSourceCount: 2 } };
      }
      return status({ pendingSourceCount: 8, scopes: [{ totalSourceCount: 10 }] });
    });
    await user.click(await screen.findByRole('button', { name: '开始整理' }));
    expect(await screen.findByText('暂时无法读取任务进度')).toBeVisible();
    expect(screen.queryByText('本轮没有完成')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '重新读取任务进度' }));
    expect(await screen.findByText('本轮处理完成')).toBeVisible();
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.memoryMaintenance.trigger')).toHaveLength(1);
  });

  it('restores an active server job on a direct organize entry without triggering it', async () => {
    const transport = renderWorkbench((request: ControlRequest) => {
      if (request.query?.projectionOnly) return { job: { jobId: 'existing-job', state: 'running' } };
      if (request.query?.jobId) return { jobId: 'existing-job', state: 'running', progress: {
        phase: 'owner_memory_curation', processedSourceCount: 6, pendingSourceCount: 4, totalSourceCount: 10, completedSourceCount: 6,
      } };
      return status({ pendingSourceCount: 10, scopes: [{ totalSourceCount: 10 }] });
    });
    expect(await screen.findByRole('button', { name: '正在整理' })).toBeDisabled();
    await waitFor(() => expect(transport.requests.some(({ request }) => request.query?.jobId === 'existing-job')).toBe(true));
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.memoryMaintenance.trigger')).toBe(false);
  });

  it('keeps unavailable lifecycle counts unknown behind supporting details', async () => {
    const user = userEvent.setup();
    renderWorkbench(status({ pendingSourceCount: 8 }), () => { throw new Error('offline'); });
    await screen.findByRole('button', { name: '开始整理' });
    expect(screen.queryByRole('heading', { name: '记忆生命周期' })).not.toBeInTheDocument();
    await user.click(screen.getByText('日报与检索维护', { selector: 'summary' }));
    expect(await screen.findByText('暂不可用')).toBeVisible();
    expect(screen.queryByText('已接入')).not.toBeInTheDocument();
  });

  it('directs a paused batch to its blocking draft instead of a disabled continue action', async () => {
    renderWorkbench((request: ControlRequest) => request.query?.runId
      ? { run: { runId: 'draft-review', status: 'draft', changes: [] }, canApply: true }
      : { ...status({ pendingSourceCount: 2, scopes: [{ status: 'backoff', lastError: 'fetch failed' }] }), autoApply: false, runs: [{ runId: 'draft-review', status: 'draft' }] });
    expect(await screen.findByRole('button', { name: '先审核本批' })).toBeDisabled();
    expect(screen.getByText(/请先审核下方本批草案，再继续处理来源。/)).toBeVisible();
    expect(screen.queryByText(/可以使用上方“继续整理”/)).not.toBeInTheDocument();
    expect(screen.getByText('当前版本还不能安全应用记忆整理草案；没有请求被发送。')).toBeVisible();
  });

  it('discovers a newer running job after an earlier tracked job has completed', async () => {
    const transport = renderWorkbench((request: ControlRequest) => {
      if (request.query?.projectionOnly) return { job: { jobId: 'new-running-job', state: 'running' } };
      if (request.query?.jobId === 'old-completed-job') return { jobId: 'old-completed-job', state: 'completed' };
      if (request.query?.jobId === 'new-running-job') return { jobId: 'new-running-job', state: 'running', progress: {
        phase: 'owner_memory_curation', processedSourceCount: 2, totalSourceCount: 10, pendingSourceCount: 3,
      } };
      return status({ pendingSourceCount: 5, scopes: [{ totalSourceCount: 10 }] });
    }, { counts: {} }, (client) => {
      client.setQueryData(memoryQueryKeys.curationTrackedJob(), 'old-completed-job');
      client.setQueryData(memoryQueryKeys.curationJob('old-completed-job'), { jobId: 'old-completed-job', state: 'completed' });
    });
    expect(await screen.findByRole('button', { name: '正在整理' })).toBeDisabled();
    expect(await screen.findByText('本轮已处理 2 条 · 回执剩余 3 条')).toBeVisible();
    expect(screen.queryByText('本轮处理完成')).not.toBeInTheDocument();
    expect(transport.requests.some(({ request }) => request.query?.jobId === 'new-running-job')).toBe(true);
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.memoryMaintenance.trigger')).toBe(false);
  });
});

it('keeps an organization entry available even when the known backlog is empty', async () => {
  const organize = vi.fn();
  render(<MemoryLibraryNavigation activeLayer="evidence" onOpenLayer={vi.fn()} onOpenOrganize={organize} onRetry={vi.fn()} summary={{ pendingGovernedEvidenceCount: 0 }} summaryState="ready" />);
  expect(screen.getByRole('button', { name: '整理记忆' })).toBeVisible();
  await userEvent.setup().click(screen.getByRole('button', { name: '整理状态摘要' }));
  expect(screen.getByText('来源是原始记录，不代表已经保存为记忆。')).toBeVisible();
  await userEvent.setup().click(screen.getByRole('button', { name: '整理记忆' }));
  expect(organize).toHaveBeenCalledOnce();
});
