import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import type { ControlRequest, ControlTransport, FrontendCapabilities } from '@/platform/transport';
import { PawWorkbenchGoalDialog, PawWorkbenchTaskDialog } from './PawWorkbenchOperations';

afterEach(cleanup);

describe('PAWOS Workbench Planning operations', () => {
  it('shows read-only project and date while keeping the hidden revision bound to task creation', async () => {
    const user = userEvent.setup();
    const transport = renderOperations({
      planning: { runtimeRevision: 7, date: '2026-10-08', plan: { project: 'PAWOS' } },
    });
    const taskDialog = await screen.findByRole('dialog', { name: '添加任务' });
    expect(within(taskDialog).getByText('项目', { selector: 'dt' })).toBeInTheDocument();
    expect(within(taskDialog).getByText('PAWOS', { selector: 'dd' })).toBeInTheDocument();
    expect(within(taskDialog).getByText('日期', { selector: 'dt' })).toBeInTheDocument();
    expect(within(taskDialog).getByText('2026-10-08', { selector: 'dd' })).toBeInTheDocument();
    expect(taskDialog).not.toHaveTextContent(/Runtime|revision/);
    expect(within(taskDialog).getByText('保存后可以撤销本次更改。')).toBeInTheDocument();
    await user.type(within(taskDialog).getByRole('textbox', { name: '任务标题' }), '公开测试任务');
    await user.type(within(taskDialog).getByRole('textbox', { name: '完成说明' }), '核对公开结果');
    await user.click(within(taskDialog).getByRole('button', { name: '创建任务' }));
    await waitFor(() => expect(requestFor(transport, 'planning.mutation.preview')).toMatchObject({
      body: { kind: 'task.save', expectedRuntimeRevision: 7, payload: { project: 'PAWOS', date: '2026-10-08', title: '公开测试任务', detail: '核对公开结果' } },
    }));
    await waitFor(() => expect(requestFor(transport, 'planning.task.save')).toMatchObject({
      body: { project: 'PAWOS', date: '2026-10-08', title: '公开测试任务', detail: '核对公开结果', expectedRuntimeRevision: 7, previewToken: 'preview-task-save', payloadSha256: 'sha256:task' },
    }));
    expect(await within(taskDialog).findByText('已保存')).toBeInTheDocument();
  });

  it('completes a selected task and undoes only through the receipt event id', async () => {
    const user = userEvent.setup();
    const transport = renderOperations({
      planning: {
        runtimeRevision: 7,
        plan: { project: 'PAWOS' },
      },
      selectedTask: { id: 'task-7', title: '整理任务', detail: '核对结果', status: 'in_progress' },
    });

    expect(await screen.findByRole('heading', { name: '编辑任务' })).toBeInTheDocument();
    const action = screen.getByText('完成所选任务', { selector: 'strong' }).closest('.mgmt-workflow');
    expect(action).not.toBeNull();
    await user.click(within(action as HTMLElement).getByRole('button', { name: '完成所选任务' }));

    await waitFor(() => expect(requestFor(transport, 'planning.task.action')).toMatchObject({
      body: {
        taskId: 'task-7',
        action: 'complete',
        expectedRuntimeRevision: 7,
        previewToken: 'preview-task-action',
        payloadSha256: 'sha256:action',
      },
    }));
    expect(await within(action as HTMLElement).findByText('已保存')).toBeInTheDocument();

    await user.click(within(action as HTMLElement).getByRole('button', { name: '撤销' }));
    expect(await within(action as HTMLElement).findByText('已恢复到更改前')).toBeInTheDocument();
    expect(requestFor(transport, 'planning.taskEvent.undo')).toMatchObject({
      body: {
        eventId: 'task-event:7',
        receiptId: 'receipt-task-action',
        rollbackToken: 'rollback-task-action',
        payloadSha256: 'sha256:action',
        confirmText: 'undo',
      },
    });
  });

  it('reopens a completed selected task through the same typed action path', async () => {
    const user = userEvent.setup();
    const transport = renderOperations({
      planning: { runtimeRevision: 7, plan: { project: 'PAWOS' } },
      selectedTask: { id: 'task-8', title: '已完成任务', status: 'completed' },
    });

    const action = screen.getByText('重新打开所选任务', { selector: 'strong' }).closest('.mgmt-workflow');
    expect(action).not.toBeNull();
    await user.click(within(action as HTMLElement).getByRole('button', { name: '重新打开所选任务' }));
    await waitFor(() => expect(requestFor(transport, 'planning.task.action')).toMatchObject({
      body: { taskId: 'task-8', action: 'reopen', expectedRuntimeRevision: 7 },
    }));
  });

  it('saves the complete goal draft and rolls it back with the save receipt', async () => {
    const user = userEvent.setup();
    const transport = renderOperations({
      planning: {
        runtimeRevision: 11,
        plan: { project: 'PAWOS' },
      },
      selectedGoal: {
        id: 'goal-4',
        title: '整理 Workbench',
        detail: '收束规划写入',
        horizon: 'long_term',
        status: 'active',
        priority: 2,
        targetDate: '2026-09-01',
      },
      goal: true,
    });

    expect(await screen.findByRole('heading', { name: '编辑目标' })).toBeInTheDocument();
    const title = screen.getByRole('textbox', { name: '目标标题' });
    await user.clear(title);
    await user.type(title, '完成 Planning App');
    await user.click(screen.getByRole('combobox', { name: '时间范围' }));
    await user.click(await screen.findByRole('option', { name: '阶段目标' }));
    await user.click(screen.getByRole('combobox', { name: '优先级' }));
    await user.click(await screen.findByRole('option', { name: '最高' }));

    const workflow = screen.getByText('保存目标修改', { selector: 'strong' }).closest('.mgmt-workflow');
    expect(workflow).not.toBeNull();
    await user.click(within(workflow as HTMLElement).getByRole('button', { name: '保存目标修改' }));

    await waitFor(() => expect(requestFor(transport, 'planning.mutation.preview')).toMatchObject({
      body: {
        kind: 'goal.save',
        expectedRuntimeRevision: 11,
        payload: expect.objectContaining({
          goalId: 'goal-4',
          title: '完成 Planning App',
          detail: '收束规划写入',
          horizon: 'medium_term',
          status: 'active',
          priority: 3,
          targetDate: '2026-09-01',
          project: 'PAWOS',
        }),
      },
    }));
    expect(await within(workflow as HTMLElement).findByText('已保存')).toBeInTheDocument();
    expect(requestFor(transport, 'planning.goal.save')).toMatchObject({
      body: {
        goalId: 'goal-4',
        title: '完成 Planning App',
        expectedRuntimeRevision: 11,
        previewToken: 'preview-goal-save',
        payloadSha256: 'sha256:goal',
        confirmText: 'apply',
      },
    });

    await user.click(within(workflow as HTMLElement).getByRole('button', { name: '撤销' }));
    expect(await within(workflow as HTMLElement).findByText('已恢复到更改前')).toBeInTheDocument();
    expect(requestFor(transport, 'planning.mutation.rollback')).toMatchObject({
      body: {
        receiptId: 'receipt-goal-save',
        rollbackToken: 'rollback-goal-save',
        payloadSha256: 'sha256:goal',
        confirmText: 'rollback',
      },
    });
  });

  it('does not expose a mutation trigger when the live revision is unavailable', async () => {
    renderOperations({ planning: { plan: { project: 'PAWOS' } }, selectedTask: { id: 'task-7', status: 'in_progress' } });
    expect(await screen.findByRole('heading', { name: '编辑任务' })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: '完成所选任务' })).toBeDisabled());
    expect(screen.getAllByText('当前规划状态尚未同步，请刷新后重试。')).toHaveLength(2);
  });
});

function renderOperations({
  goal = false,
  planning,
  selectedGoal,
  selectedTask,
}: {
  goal?: boolean;
  planning: Record<string, unknown>;
  selectedGoal?: Record<string, unknown>;
  selectedTask?: Record<string, unknown>;
}) {
  const transport = new OperationsTransport();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <TooltipProvider delayDuration={0}>
      <ControlTransportProvider transport={transport}>
        <QueryClientProvider client={client}>
          {goal ? (
            <PawWorkbenchGoalDialog
              onChanged={() => undefined}
              onOpenChange={() => undefined}
              open
              planning={planning}
              selectedGoal={selectedGoal}
            />
          ) : (
            <PawWorkbenchTaskDialog
              onChanged={() => undefined}
              onOpenChange={() => undefined}
              open
              planning={planning}
              selectedTask={selectedTask}
            />
          )}
        </QueryClientProvider>
      </ControlTransportProvider>
    </TooltipProvider>,
  );
  return transport;
}

class OperationsTransport implements ControlTransport {
  readonly kind = 'mock' as const;
  readonly requests: ControlRequest[] = [];

  async capabilities(): Promise<FrontendCapabilities> {
    return {
      schemaVersion: 'rag-ime.control-frontend-capabilities.v1',
      transport: 'mock',
      routeIds: [
        'planning.mutation.preview',
        'planning.task.save',
        'planning.goal.save',
        'planning.task.action',
        'planning.taskEvent.undo',
        'planning.mutation.rollback',
      ],
      features: { managementWorkContract: true, planningWorkContract: true },
      native: { pickFiles: false, managedAgentImageImport: false, revealPath: false, approvedExternalActions: false, keychain: false, tcc: false },
    };
  }

  async request<Response = unknown>(request: ControlRequest): Promise<Response> {
    this.requests.push(request);
    const pathId = String(request.pathId);
    if (pathId === 'planning.mutation.preview') {
      const kind = String((request.body as Record<string, unknown> | undefined)?.kind ?? '');
      const isGoal = kind === 'goal.save';
      return preview(isGoal ? 'planning.goal.save' : kind === 'task.action' ? 'planning.task.action' : 'planning.task.save', isGoal ? 'preview-goal-save' : kind === 'task.action' ? 'preview-task-action' : 'preview-task-save', isGoal ? 'sha256:goal' : kind === 'task.action' ? 'sha256:action' : 'sha256:task') as Response;
    }
    if (pathId === 'planning.task.action') return receipt(pathId, 'receipt-task-action', 'rollback-task-action', 'sha256:action', { eventId: 'task-event:7' }) as Response;
    if (pathId === 'planning.taskEvent.undo') return receipt(pathId, 'receipt-task-undo', '', 'sha256:action', { rollbackAvailable: false }) as Response;
    if (pathId === 'planning.goal.save') return receipt(pathId, 'receipt-goal-save', 'rollback-goal-save', 'sha256:goal') as Response;
    if (pathId === 'planning.task.save') return receipt(pathId, 'receipt-task-save', 'rollback-task-save', 'sha256:task') as Response;
    if (pathId === 'planning.mutation.rollback') return receipt(pathId, 'receipt-goal-rollback', '', 'sha256:goal', { rollbackAvailable: false }) as Response;
    throw new Error(`Unexpected request: ${pathId}`);
  }

  subscribe(): () => void { return () => undefined; }
}

function requestFor(transport: OperationsTransport, pathId: string): ControlRequest | undefined {
  return transport.requests.find((request) => String(request.pathId) === pathId);
}

function preview(pathId: string, previewToken: string, payloadSha256: string) {
  return {
    ok: true,
    pathId,
    previewToken,
    payloadSha256,
    expectedRevision: { runtimeRevision: pathId === 'planning.goal.save' ? 11 : 7 },
    expiresAtMs: Date.now() + 60_000,
    requiredConfirm: 'apply',
    summary: { title: '确认本次变更', items: ['核对字段并保存'], risk: 'R1' },
  };
}

function receipt(pathId: string, receiptId: string, rollbackToken: string, payloadSha256: string, extra: Record<string, unknown> = {}) {
  return {
    ok: true,
    pathId,
    receiptId,
    rollbackToken,
    payloadSha256,
    appliedAtMs: Date.now(),
    rollbackAvailable: extra.rollbackAvailable ?? Boolean(rollbackToken),
    ...extra,
  };
}
