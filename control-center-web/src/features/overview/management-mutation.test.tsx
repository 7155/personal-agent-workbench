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
  it.each(['standard', 'action'] as const)('runs a reversible %s operation directly, prevents duplicate requests, and announces completion', async (presentation) => {
    const user = userEvent.setup();
    const pendingPreview = deferred<ManagementWorkPreview<TestContext>>();
    const pendingApply = deferred<ManagementWorkReceipt>();
    const pendingRollback = deferred<ManagementWorkReceipt>();
    const boundPreview = previewFixture('R2');
    const appliedReceipt = receiptFixture(true);
    const onPreview = vi.fn(() => pendingPreview.promise);
    const onApply = vi.fn(() => pendingApply.promise);
    const onRollback = vi.fn(() => pendingRollback.promise);

    renderWorkflow({ onApply, onPreview, onRollback, risk: 'R2', title: '保存设置', presentation });

    if (presentation === 'action') {
      const action = screen.getByRole('group', { name: '保存设置' });
      expect(within(action).queryByText('保存设置', { selector: 'strong' })).not.toBeInTheDocument();
      expect(within(action).queryByText('保存当前页面中的更改。')).not.toBeInTheDocument();
    }
    const trigger = screen.getByRole('button', { name: '保存设置' });
    trigger.focus();
    await user.keyboard('{Enter}');
    expect(trigger).toBeEnabled();
    expect(trigger).toHaveFocus();
    expect(trigger).toHaveAttribute('aria-disabled', 'true');
    expect(trigger).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByText('正在准备更改')).toBeInTheDocument();

    await user.keyboard('{Enter} ');
    await user.click(trigger);
    expect(onPreview).toHaveBeenCalledTimes(1);

    await act(async () => pendingPreview.resolve(boundPreview));
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(1));
    expect(screen.getByRole('button', { name: '保存设置' })).toBe(trigger);
    expect(trigger).toHaveFocus();
    expect(trigger).toHaveAttribute('aria-disabled', 'true');
    expect(screen.queryByText('继续确认')).not.toBeInTheDocument();
    expect(screen.getByText('正在保存更改')).toBeInTheDocument();
    await user.keyboard('{Enter} ');
    await user.click(trigger);
    expect(onPreview).toHaveBeenCalledTimes(1);
    expect(onApply).toHaveBeenCalledTimes(1);

    await act(async () => pendingApply.resolve(appliedReceipt));
    const receipt = await screen.findByRole('status');
    expect(receipt).toHaveTextContent('已保存');
    expect(receipt).toHaveFocus();
    const undo = within(receipt).getByRole('button', { name: '撤销' });
    undo.focus();
    await user.keyboard('{Enter}');
    expect(undo).toBeEnabled();
    expect(undo).toHaveFocus();
    expect(undo).toHaveAttribute('aria-disabled', 'true');
    expect(undo).toHaveAttribute('aria-busy', 'true');
    expect(within(receipt).getByRole('button', { name: '撤销' })).toBe(undo);
    expect(within(receipt).getByRole('button', { name: '完成' })).toBeDisabled();
    await user.keyboard('{Enter} ');
    await user.click(undo);
    expect(onRollback).toHaveBeenCalledTimes(1);
    expect(onRollback).toHaveBeenCalledWith(appliedReceipt, boundPreview);
    await act(async () => pendingRollback.resolve(receiptFixture()));
    const restored = await screen.findByRole('status');
    expect(restored).toHaveTextContent('已恢复到更改前');
    expect(restored).toHaveFocus();
    await user.click(within(restored).getByRole('button', { name: '完成' }));
    expect(screen.getByRole('button', { name: '保存设置' })).toHaveFocus();
  });

  it.each([
    ['创建任务', 'primary'],
    ['完成所选任务', 'primary'],
    ['重新打开所选任务', 'primary'],
    ['完成所选任务', 'secondary'],
    ['重新打开所选任务', 'secondary'],
  ] as const)('keeps the original %s %s target focused through direct preview and apply', async (title, triggerVariant) => {
    const user = userEvent.setup();
    const pendingPreview = deferred<ManagementWorkPreview<TestContext>>();
    const pendingApply = deferred<ManagementWorkReceipt>();
    const onPreview = vi.fn(() => pendingPreview.promise);
    const onApply = vi.fn(() => pendingApply.promise);
    renderWorkflow({ onApply, onPreview, risk: 'R1', title, triggerVariant });

    const trigger = screen.getByRole('button', { name: title });
    expect(trigger).toHaveAttribute('data-variant', triggerVariant);
    trigger.focus();
    await user.keyboard('{Enter}');
    expect(trigger).toBeEnabled();
    expect(trigger).toHaveFocus();
    expect(trigger).toHaveAttribute('aria-disabled', 'true');
    expect(screen.getByRole('button', { name: title })).toBe(trigger);

    await act(async () => pendingPreview.resolve(previewFixture('R1')));
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(1));
    expect(trigger).toHaveFocus();
    expect(trigger).toHaveAttribute('aria-busy', 'true');
    expect(trigger).toHaveAttribute('data-variant', triggerVariant);
    expect(screen.getByRole('button', { name: title })).toBe(trigger);
    await user.keyboard('{Enter} ');
    await user.click(trigger);
    expect(onPreview).toHaveBeenCalledTimes(1);
    expect(onApply).toHaveBeenCalledTimes(1);

    await act(async () => pendingApply.resolve(receiptFixture()));
    expect(await screen.findByRole('status')).toHaveFocus();
  });

  it.each(['standard', 'action'] as const)('uses the server preview as the authority and confirms an R3 escalation for %s presentation', async (presentation) => {
    const user = userEvent.setup();
    const onApply = vi.fn(async () => receiptFixture());
    const onPreview = vi.fn(async () => previewFixture('R3'));

    renderWorkflow({ presentation, onApply, onPreview, risk: 'R1', title: '保存设置' });
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

  it.each(['standard', 'action'] as const)('locks navigation while applying, focuses a recoverable error, and returns focus to retry for %s presentation', async (presentation) => {
    const user = userEvent.setup();
    const pendingApply = deferred<ManagementWorkReceipt>();
    const onApply = vi.fn(() => pendingApply.promise);

    renderWorkflow({ presentation,
      onApply,
      onPreview: async () => previewFixture('R3'),
      risk: 'R3',
      title: '永久清除',
      triggerVariant: 'secondary',
    });

    expect(screen.getByRole('button', { name: '查看影响' })).toHaveAttribute('data-variant', 'danger');
    await user.click(screen.getByRole('button', { name: '查看影响' }));
    await user.click(await screen.findByRole('button', { name: '继续确认' }));
    const checkbox = screen.getByRole('checkbox');
    await user.click(checkbox);
    const applyButton = screen.getByRole('button', { name: '确认执行' });
    applyButton.focus();
    await user.keyboard('{Enter}');

    expect(checkbox).toBeDisabled();
    expect(screen.getByRole('button', { name: '返回查看' })).toBeDisabled();
    expect(applyButton).toBeEnabled();
    expect(applyButton).toHaveFocus();
    expect(applyButton).toHaveAttribute('aria-disabled', 'true');
    expect(applyButton).toHaveAttribute('aria-busy', 'true');
    await user.keyboard('{Enter} ');
    await user.click(applyButton);
    expect(onApply).toHaveBeenCalledTimes(1);

    await act(async () => pendingApply.reject(new Error('网络暂时不可用，请稍后重试。')));
    const alert = await screen.findByRole('alert');
    const errorPanel = alert.closest('.mgmt-workflow__panel');
    expect(errorPanel).toHaveFocus();
    expect(within(errorPanel as HTMLElement).getByRole('button', { name: '重新查看影响' })).toBeEnabled();

    await user.click(within(errorPanel as HTMLElement).getByRole('button', { name: '重新查看影响' }));
    expect(screen.getByRole('button', { name: '查看影响' })).toHaveFocus();
  });

  it.each(['standard', 'action'] as const)('keeps the original retry name and focus through held preview and apply after a sanitized failure for %s presentation', async (presentation) => {
    const user = userEvent.setup();
    const pendingRetryPreview = deferred<ManagementWorkPreview<TestContext>>();
    const pendingRetryApply = deferred<ManagementWorkReceipt>();
    const onPreview = vi.fn()
      .mockRejectedValueOnce(new Error('POST /api/internal payloadSha256=secret'))
      .mockImplementationOnce(() => pendingRetryPreview.promise);
    const onApply = vi.fn(() => pendingRetryApply.promise);

    renderWorkflow({ presentation, onApply, onPreview, risk: 'R1', title: '保存设置' });
    const trigger = screen.getByRole('button', { name: '保存设置' });
    await user.click(trigger);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('预览失败');
    expect(alert).toHaveTextContent('暂时无法预览，请稍后重试。');
    expect(alert).not.toHaveTextContent('/api/internal');
    expect(within(alert).getByRole('button', { name: '交给 Trace Agent' })).toBeInTheDocument();
    expect(alert.parentElement).toHaveFocus();
    const errorBody = alert.querySelector('.mgmt-notice__body');
    const errorBodyText = errorBody?.textContent;

    const retry = screen.getByRole('button', { name: '重新尝试' });
    const retryLabel = retry.querySelector('.ui-button__label');
    expect(retry).toBe(trigger);
    retry.focus();
    await user.keyboard('{Enter}');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toBe(alert);
    expect(alert).toHaveTextContent('正在重试');
    expect(alert.querySelector('.mgmt-notice__body')).toBe(errorBody);
    expect(errorBody).toHaveTextContent(errorBodyText!);
    expect(retry).toHaveAccessibleName('重新尝试');
    expect(screen.getByRole('button', { name: '重新尝试' })).toBe(retry);
    expect(retry.querySelector('.ui-button__label')).toBe(retryLabel);
    expect(retry).toHaveFocus();
    expect(retry).toBeEnabled();
    expect(retry).toHaveAttribute('aria-disabled', 'true');
    expect(retry).toHaveAttribute('aria-busy', 'true');
    await user.keyboard('{Enter} ');
    await user.click(retry);
    expect(onPreview).toHaveBeenCalledTimes(2);
    expect(onApply).not.toHaveBeenCalled();

    await act(async () => pendingRetryPreview.resolve(previewFixture('R1')));
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(1));
    expect(screen.getByRole('status')).toBe(alert);
    expect(alert).toHaveTextContent('正在保存');
    expect(alert.querySelector('.mgmt-notice__body')).toBe(errorBody);
    expect(errorBody).toHaveTextContent(errorBodyText!);
    expect(retry).toHaveAccessibleName('重新尝试');
    expect(screen.getByRole('button', { name: '重新尝试' })).toBe(retry);
    expect(retry.querySelector('.ui-button__label')).toBe(retryLabel);
    expect(retry).toHaveFocus();
    expect(retry).toHaveAttribute('aria-disabled', 'true');
    expect(retry).toHaveAttribute('aria-busy', 'true');
    await user.keyboard('{Enter} ');
    await user.click(retry);
    await act(async () => pendingRetryApply.resolve(receiptFixture()));
    const receipt = await screen.findByRole('status');
    expect(receipt).toHaveTextContent('已保存');
    expect(receipt).toHaveFocus();
    expect(alert).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '重新尝试' })).not.toBeInTheDocument();
    expect(onPreview).toHaveBeenCalledTimes(2);
    expect(onApply).toHaveBeenCalledTimes(1);
  });

  it.each(['standard', 'action'] as const)('reports the new retry failure and clears the old feedback at the authoritative next stage for %s presentation', async (presentation) => {
    const user = userEvent.setup();
    const firstRetry = deferred<ManagementWorkPreview<TestContext>>();
    const nextRetry = deferred<ManagementWorkPreview<TestContext>>();
    const onPreview = vi.fn()
      .mockRejectedValueOnce(new Error('第一次未完成。'))
      .mockImplementationOnce(() => firstRetry.promise)
      .mockImplementationOnce(() => nextRetry.promise);
    const onApply = vi.fn(async () => receiptFixture());
    renderWorkflow({ presentation, onApply, onPreview, risk: 'R1', title: '保存设置' });
    await user.click(screen.getByRole('button', { name: '保存设置' }));
    const notice = await screen.findByRole('alert');
    expect(notice).toHaveTextContent('第一次未完成。');
    const retry = screen.getByRole('button', { name: '重新尝试' });
    await user.click(retry);
    expect(screen.getByRole('status')).toBe(notice);
    expect(notice).toHaveTextContent('正在重试');
    expect(notice).toHaveTextContent('第一次未完成。');

    await act(async () => firstRetry.reject(new Error('第二次未完成。')));
    expect(await screen.findByRole('alert')).toBe(notice);
    expect(notice).toHaveTextContent('预览失败');
    expect(notice).toHaveTextContent('第二次未完成。');
    expect(notice).not.toHaveTextContent('第一次未完成。');
    expect(notice.parentElement).toHaveFocus();
    await user.click(retry);
    expect(screen.getByRole('status')).toBe(notice);
    expect(notice).toHaveTextContent('第二次未完成。');

    await act(async () => nextRetry.resolve(previewFixture('R3')));
    const previewPanel = (await screen.findByText('这次更改会永久移除内容')).closest('.mgmt-workflow__panel');
    expect(previewPanel).toHaveFocus();
    expect(notice).not.toBeInTheDocument();
    expect(onPreview).toHaveBeenCalledTimes(3);
    expect(onApply).not.toHaveBeenCalled();
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
  triggerVariant,
  presentation = 'standard',
}: {
  onApply: (preview: ManagementWorkPreview<TestContext>) => Promise<ManagementWorkReceipt>;
  onPreview: () => Promise<ManagementWorkPreview<TestContext>>;
  onRollback?: (
    receipt: ManagementWorkReceipt,
    preview: ManagementWorkPreview<TestContext>,
  ) => Promise<ManagementWorkReceipt>;
  risk: 'R1' | 'R2' | 'R3';
  title: string;
  triggerVariant?: 'primary' | 'secondary';
  presentation?: 'standard' | 'action';
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
        triggerVariant={triggerVariant}
        presentation={presentation}
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
