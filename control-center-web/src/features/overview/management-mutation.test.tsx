import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  ManagementMutationWorkflow,
  type ManagementWorkPreview,
  type ManagementWorkReceipt,
} from './management-mutation';
import { QueryState } from './management-ui';
import { PawOsDesktopProvider } from '@/features/paw-os/surface-context';
import { parseTraceAgentHandoff } from '@/features/trace-agent/handoff';

type TestContext = { value: string };

afterEach(cleanup);

describe('ManagementMutationWorkflow feedback and confirmation', () => {
  it('runs a reversible operation directly, prevents duplicate requests, and announces completion', async () => {
    const user = userEvent.setup();
    const pendingPreview = deferred<ManagementWorkPreview<TestContext>>();
    const pendingApply = deferred<ManagementWorkReceipt>();
    const onPreview = vi.fn(() => pendingPreview.promise);
    const onApply = vi.fn(() => pendingApply.promise);
    const onRollback = vi.fn(async () => receiptFixture());

    renderWorkflow({ onApply, onPreview, onRollback, risk: 'R2', title: '保存设置' });

    const trigger = screen.getByRole('button', { name: '保存设置' });
    await user.click(trigger);
    expect(trigger).toBeDisabled();
    expect(trigger).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByText('正在准备更改')).toBeInTheDocument();

    await user.click(trigger);
    expect(onPreview).toHaveBeenCalledTimes(1);

    await act(async () => pendingPreview.resolve(previewFixture('R2')));
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(1));
    expect(screen.queryByText('继续确认')).not.toBeInTheDocument();
    expect(screen.getByText('正在保存更改')).toBeInTheDocument();

    await act(async () => pendingApply.resolve(receiptFixture(true)));
    const receipt = await screen.findByRole('status');
    expect(receipt).toHaveTextContent('已保存');
    expect(receipt).toHaveFocus();
    expect(within(receipt).getByRole('button', { name: '撤销' })).toBeEnabled();
    await user.click(within(receipt).getByRole('button', { name: '完成' }));
    expect(screen.getByRole('button', { name: '保存设置' })).toHaveFocus();
  });

  it('uses the server preview as the authority and confirms an R3 escalation', async () => {
    const user = userEvent.setup();
    const onApply = vi.fn(async () => receiptFixture());
    const onPreview = vi.fn(async () => previewFixture('R3'));

    renderWorkflow({ onApply, onPreview, risk: 'R1', title: '保存设置' });
    await user.click(screen.getByRole('button', { name: '保存设置' }));

    const previewPanel = (await screen.findByText('这次更改会永久移除内容')).closest('.mgmt-workflow__panel');
    expect(previewPanel).toHaveFocus();
    expect(screen.getByText('高风险')).toBeInTheDocument();
    expect(onApply).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: '继续确认' }));
    const checkbox = screen.getByRole('checkbox', { name: '我确认只执行上方列出的更改' });
    expect(checkbox).toHaveFocus();
    await user.click(checkbox);
    await user.click(screen.getByRole('button', { name: '确认执行' }));

    const receipt = await screen.findByRole('status');
    expect(onApply).toHaveBeenCalledTimes(1);
    expect(receipt).toHaveTextContent('更改已记录');
  });

  it('locks navigation while applying, focuses a recoverable error, and returns focus to retry', async () => {
    const user = userEvent.setup();
    const pendingApply = deferred<ManagementWorkReceipt>();
    const onApply = vi.fn(() => pendingApply.promise);

    renderWorkflow({
      onApply,
      onPreview: async () => previewFixture('R3'),
      risk: 'R3',
      title: '永久清除',
    });

    await user.click(screen.getByRole('button', { name: '查看影响' }));
    await user.click(await screen.findByRole('button', { name: '继续确认' }));
    const checkbox = screen.getByRole('checkbox');
    await user.click(checkbox);
    const applyButton = screen.getByRole('button', { name: '确认执行' });
    await user.click(applyButton);

    expect(checkbox).toBeDisabled();
    expect(screen.getByRole('button', { name: '返回查看' })).toBeDisabled();
    expect(applyButton).toBeDisabled();
    expect(applyButton).toHaveAttribute('aria-busy', 'true');

    await act(async () => pendingApply.reject(new Error('网络暂时不可用，请稍后重试。')));
    const alert = await screen.findByRole('alert');
    const errorPanel = alert.closest('.mgmt-workflow__panel');
    expect(errorPanel).toHaveFocus();
    expect(within(errorPanel as HTMLElement).getByRole('button', { name: '重新查看影响' })).toBeEnabled();

    await user.click(within(errorPanel as HTMLElement).getByRole('button', { name: '重新查看影响' }));
    expect(screen.getByRole('button', { name: '查看影响' })).toHaveFocus();
  });

  it('offers a visible retry after preview failure without exposing technical details', async () => {
    const user = userEvent.setup();
    const onPreview = vi.fn()
      .mockRejectedValueOnce(new Error('POST /api/internal payloadSha256=secret'))
      .mockResolvedValueOnce(previewFixture('R1'));
    const onApply = vi.fn(async () => receiptFixture());

    renderWorkflow({ onApply, onPreview, risk: 'R1', title: '保存设置' });
    await user.click(screen.getByRole('button', { name: '保存设置' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('预览失败');
    expect(alert).toHaveTextContent('暂时无法预览，请稍后重试。');
    expect(alert).not.toHaveTextContent('/api/internal');
    expect(within(alert).getByRole('button', { name: '交给 Trace Agent' })).toBeInTheDocument();
    expect(alert.parentElement).toHaveFocus();

    await user.click(screen.getByRole('button', { name: '重新尝试' }));
    expect(await screen.findByRole('status')).toHaveTextContent('已保存');
    expect(onPreview).toHaveBeenCalledTimes(2);
    expect(onApply).toHaveBeenCalledTimes(1);
  });
});

describe('shared management query feedback', () => {
  it('announces a sanitized read error and provides a real retry action', async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();
    const routes: string[] = [];
    render(
      <PawOsDesktopProvider openRoute={(route) => routes.push(route)} openWindow={() => undefined}>
        <QueryState
          error={new Error('GET /api/private runtimeRevision=10')}
          isPending={false}
          onRetry={onRetry}
        >
          loaded
        </QueryState>
      </PawOsDesktopProvider>,
    );

    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('暂时无法读取这部分内容，请稍后重试。');
    expect(alert).not.toHaveTextContent('/api/private');
    await user.click(within(alert).getByRole('button', { name: '重试' }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    await user.click(within(alert).getByRole('button', { name: '交给 Trace Agent' }));
    const handoff = parseTraceAgentHandoff(routes[0].split('?', 2)[1] ?? '');
    expect(handoff).toMatchObject({
      kind: 'generic',
      title: '读取失败',
      error: 'GET[path redacted] runtimeRevision=10',
    });
    expect(handoff?.error).not.toContain('/api/private');
  });
});

function renderWorkflow({
  onApply,
  onPreview,
  onRollback,
  risk,
  title,
}: {
  onApply: (preview: ManagementWorkPreview<TestContext>) => Promise<ManagementWorkReceipt>;
  onPreview: () => Promise<ManagementWorkPreview<TestContext>>;
  onRollback?: (
    receipt: ManagementWorkReceipt,
    preview: ManagementWorkPreview<TestContext>,
  ) => Promise<ManagementWorkReceipt>;
  risk: 'R1' | 'R2' | 'R3';
  title: string;
}) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ManagementMutationWorkflow
        availability={{ state: 'available' }}
        description="保存当前页面中的更改。"
        draftKey="draft-a"
        mutationKey={['test', title]}
        onApply={onApply}
        onPreview={onPreview}
        onRollback={onRollback}
        risk={risk}
        title={title}
      />
    </QueryClientProvider>,
  );
}

function previewFixture(risk: 'R1' | 'R2' | 'R3'): ManagementWorkPreview<TestContext> {
  return {
    context: { value: 'next' },
    expectedRuntimeRevision: 1,
    expiresAtMs: Date.now() + 60_000,
    pathId: 'test.apply',
    payloadSha256: 'payload',
    previewToken: 'preview',
    requiredConfirm: 'apply',
    summary: {
      title: risk === 'R3' ? '这次更改会永久移除内容' : '保存当前设置',
      items: ['只更改当前页面中的内容。'],
      risk,
    },
  };
}

function receiptFixture(rollbackAvailable = false): ManagementWorkReceipt {
  return {
    appliedAtMs: Date.now(),
    pathId: 'test.apply',
    payloadSha256: 'payload',
    receiptId: 'receipt',
    rollbackAvailable,
    rollbackToken: rollbackAvailable ? 'rollback' : '',
    raw: { ok: true },
  };
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}
