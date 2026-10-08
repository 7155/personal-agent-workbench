import { useMutation, useQuery } from '@tanstack/react-query';
import { FilePlus2, Flag, ListChecks } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { useControlTransport } from '@/app/control-transport';
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Field,
  Input,
  Select,
  TextArea,
} from '@/components/primitives';
import {
  ManagementMutationWorkflow,
  parseManagementWorkPreview,
  parseManagementWorkReceipt,
} from '@/features/overview/management-mutation';
import { InlineNotice, publicErrorText, StatusBadge } from '@/features/overview/management-ui';
import { planningMutationPathIds, usePlanningMutationBoundary } from '@/features/planning/api';
import {
  requestWorkDocumentCommand,
  type WorkDocumentCommandInput,
} from '@/features/work-documents/api';
import type { JsonValue } from '@/platform/transport';
import type { PawWorkbenchRecord } from './PawWorkbench';

export function PawWorkbenchTaskDialog({
  onChanged,
  onOpenChange,
  open,
  planning,
  selectedTask = null,
}: {
  onChanged: () => void;
  onOpenChange: (open: boolean) => void;
  open: boolean;
  planning: PawWorkbenchRecord;
  /** Optional live task selected by the Workbench outline. Null keeps the create flow. */
  selectedTask?: PawWorkbenchRecord | null;
}) {
  const boundary = usePlanningMutationBoundary();
  const [title, setTitle] = useState('');
  const [detail, setDetail] = useState('');
  const [titleTouched, setTitleTouched] = useState(false);
  const selectedTaskId = entityId(selectedTask);
  const plan = record(planning.plan);
  const runtimeRevision = finiteNumber(planning.runtimeRevision);
  const project = text(plan.project) || text(planning.project) || 'personal-agent-workbench';
  const date = text(planning.date) || localDate(new Date());
  const taskStatus = text(selectedTask?.status, 'todo');
  const taskAction = isTaskDone(taskStatus) ? 'reopen' : 'complete';
  const draft = useMemo<Record<string, JsonValue>>(() => ({
    date,
    detail: detail.trim(),
    project,
    title: title.trim(),
    ...(selectedTaskId ? { taskId: selectedTaskId } : {}),
  }), [date, detail, project, selectedTaskId, title]);
  const actionDraft = useMemo<Record<string, JsonValue>>(() => ({
    action: taskAction,
    taskId: selectedTaskId,
  }), [selectedTaskId, taskAction]);
  const revisionBlock = runtimeRevision === null ? '当前规划状态尚未同步，请刷新后重试。' : '';
  const availability = boundary.availability(
    [planningMutationPathIds.preview, planningMutationPathIds.taskSave, planningMutationPathIds.rollback],
    revisionBlock || (!title.trim() ? '请输入任务标题。' : ''),
  );
  const actionAvailability = boundary.availability(
    [planningMutationPathIds.preview, planningMutationPathIds.taskAction, planningMutationPathIds.taskEventUndo],
    revisionBlock || (!selectedTaskId ? '当前任务缺少可验证的 ID，不能更新状态。' : ''),
  );

  useEffect(() => {
    if (!open) return;
    setTitle(text(selectedTask?.title));
    setDetail(text(selectedTask?.detail) || text(selectedTask?.description));
    setTitleTouched(false);
  }, [open, selectedTask]);

  return (
    <Dialog onOpenChange={onOpenChange} open={open}>
      <DialogContent className="paw-wb-operation-dialog paw-wb-operation-dialog--task">
        <DialogHeader>
          <div className="paw-wb-operation-dialog__heading"><ListChecks aria-hidden size={19} /></div>
          <DialogTitle>{selectedTaskId ? '编辑任务' : '添加任务'}</DialogTitle>
          <DialogDescription>{selectedTaskId ? '修改内容后保存，也可以单独更新完成状态。' : '写下一个可执行、完成后能核对结果的下一步。'}</DialogDescription>
        </DialogHeader>
        <div className="paw-wb-operation-dialog__form">
          <Field
            error={titleTouched && !title.trim() ? '请输入任务标题。' : undefined}
            htmlFor="paw-wb-task-title"
            label="任务标题"
            required
          >
            <Input
              id="paw-wb-task-title"
              onBlur={() => setTitleTouched(true)}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="例如：整理今天的工作清单"
              value={title}
            />
          </Field>
          <Field htmlFor="paw-wb-task-detail" label="完成说明">
            <TextArea
              id="paw-wb-task-detail"
              onChange={(event) => setDetail(event.target.value)}
              placeholder="范围、完成标准或需要交接的上下文"
              rows={4}
              value={detail}
            />
          </Field>
          <dl className="paw-wb-task-context">
            <div><dt>项目</dt><dd>{project}</dd></div>
            <div><dt>日期</dt><dd>{date}</dd></div>
            {selectedTaskId ? <div><dt>当前状态</dt><dd><StatusBadge label={taskStatusLabel(taskStatus)} tone={taskStatusTone(taskStatus)} /></dd></div> : null}
          </dl>
          <ManagementMutationWorkflow
            availability={availability}
            description="保存后可以撤销本次更改。"
            disabled={!title.trim()}
            draftKey={JSON.stringify(draft)}
            mutationKey={['paw-workbench', 'planning', 'task-save']}
            onApply={async (preview) => parseManagementWorkReceipt(
              await boundary.request({
                pathId: planningMutationPathIds.taskSave,
                body: {
                  ...preview.context,
                  confirmText: preview.requiredConfirm,
                  expectedRuntimeRevision: preview.expectedRuntimeRevision,
                  payloadSha256: preview.payloadSha256,
                  previewToken: preview.previewToken,
                },
              }),
              planningMutationPathIds.taskSave,
              preview.payloadSha256,
            )}
            onApplied={onChanged}
            onPreview={async () => parseManagementWorkPreview(
              await boundary.request({
                pathId: planningMutationPathIds.preview,
                body: {
                  expectedRuntimeRevision: runtimeRevision ?? 0,
                  kind: 'task.save',
                  payload: draft,
                },
              }),
              planningMutationPathIds.taskSave,
              draft,
            )}
            onRollback={async (receipt, preview) => parseManagementWorkReceipt(
              await boundary.request({
                pathId: planningMutationPathIds.rollback,
                body: {
                  confirmText: 'rollback',
                  payloadSha256: receipt.payloadSha256,
                  receiptId: receipt.receiptId,
                  rollbackToken: receipt.rollbackToken,
                },
              }),
              planningMutationPathIds.rollback,
              preview.payloadSha256,
            )}
            onRolledBack={onChanged}
            risk="R1"
            title={selectedTaskId ? '保存任务修改' : '创建任务'}
          />
          {selectedTaskId ? (
            <ManagementMutationWorkflow
              availability={actionAvailability}
              description={taskAction === 'reopen' ? '重新打开所选任务；完成后仍可撤销。' : '将所选任务标记完成；完成后仍可撤销。'}
              draftKey={JSON.stringify(actionDraft)}
              mutationKey={['paw-workbench', 'planning', 'task-action', selectedTaskId]}
              onApply={async (preview) => parseManagementWorkReceipt(
                await boundary.request({
                  pathId: planningMutationPathIds.taskAction,
                  body: {
                    ...preview.context,
                    confirmText: preview.requiredConfirm,
                    expectedRuntimeRevision: preview.expectedRuntimeRevision,
                    payloadSha256: preview.payloadSha256,
                    previewToken: preview.previewToken,
                  },
                }),
                planningMutationPathIds.taskAction,
                preview.payloadSha256,
              )}
              onApplied={onChanged}
              onPreview={async () => parseManagementWorkPreview(
                await boundary.request({
                  pathId: planningMutationPathIds.preview,
                  body: {
                    expectedRuntimeRevision: runtimeRevision ?? 0,
                    kind: 'task.action',
                    payload: actionDraft,
                  },
                }),
                planningMutationPathIds.taskAction,
                actionDraft,
              )}
              onRollback={async (receipt, preview) => {
                const eventId = text(receipt.raw.eventId);
                if (!eventId) throw new Error('无法验证这次任务变更，不能安全撤销。');
                return parseManagementWorkReceipt(
                  await boundary.request({
                    pathId: planningMutationPathIds.taskEventUndo,
                    body: {
                      confirmText: 'undo',
                      eventId,
                      payloadSha256: receipt.payloadSha256,
                      receiptId: receipt.receiptId,
                      rollbackToken: receipt.rollbackToken,
                    },
                  }),
                  planningMutationPathIds.taskEventUndo,
                  preview.payloadSha256,
                );
              }}
              onRolledBack={onChanged}
              risk="R1"
              title={taskAction === 'reopen' ? '重新打开所选任务' : '完成所选任务'}
              triggerVariant="secondary"
            />
          ) : null}
        </div>
      </DialogContent>
    </Dialog>
  );
}

export function PawWorkbenchGoalDialog({
  onChanged,
  onOpenChange,
  open,
  planning,
  selectedGoal = null,
}: {
  onChanged: () => void;
  onOpenChange: (open: boolean) => void;
  open: boolean;
  planning: PawWorkbenchRecord;
  /** Optional live goal selected by the Workbench outline. Null starts a new goal. */
  selectedGoal?: PawWorkbenchRecord | null;
}) {
  const boundary = usePlanningMutationBoundary();
  const [title, setTitle] = useState('');
  const [detail, setDetail] = useState('');
  const [horizon, setHorizon] = useState('long_term');
  const [status, setStatus] = useState('active');
  const [priority, setPriority] = useState(1);
  const [targetDate, setTargetDate] = useState('');
  const [titleTouched, setTitleTouched] = useState(false);
  const selectedGoalId = entityId(selectedGoal);
  const plan = record(planning.plan);
  const runtimeRevision = finiteNumber(planning.runtimeRevision);
  const project = text(plan.project) || text(planning.project) || 'personal-agent-workbench';
  const draft = useMemo<Record<string, JsonValue>>(() => ({
    detail: detail.trim(),
    horizon,
    priority,
    project,
    status,
    targetDate,
    title: title.trim(),
    ...(selectedGoalId ? { goalId: selectedGoalId } : {}),
  }), [detail, horizon, priority, project, selectedGoalId, status, targetDate, title]);
  const revisionBlock = runtimeRevision === null ? '当前规划状态尚未同步，请刷新后重试。' : '';
  const availability = boundary.availability(
    [planningMutationPathIds.preview, planningMutationPathIds.goalSave, planningMutationPathIds.rollback],
    revisionBlock || (!title.trim() ? '请输入目标标题。' : ''),
  );

  useEffect(() => {
    if (!open) return;
    setTitle(text(selectedGoal?.title));
    setDetail(text(selectedGoal?.detail) || text(selectedGoal?.description));
    setHorizon(text(selectedGoal?.horizon, 'long_term'));
    setStatus(text(selectedGoal?.status, 'active'));
    setPriority(finiteNumber(selectedGoal?.priority) ?? 1);
    setTargetDate(text(selectedGoal?.targetDate));
    setTitleTouched(false);
  }, [open, selectedGoal]);

  return (
    <Dialog onOpenChange={onOpenChange} open={open}>
      <DialogContent className="paw-wb-operation-dialog paw-wb-operation-dialog--goal">
        <DialogHeader>
          <div className="paw-wb-operation-dialog__heading"><Flag aria-hidden size={19} /></div>
          <DialogTitle>{selectedGoalId ? '编辑目标' : '添加目标'}</DialogTitle>
          <DialogDescription>{selectedGoalId ? '保存周期、状态和优先级，完成后仍可撤销。' : '把一个有边界、能回看的结果放进目标层级。'}</DialogDescription>
        </DialogHeader>
        <div className="paw-wb-operation-dialog__form">
          <Field
            error={titleTouched && !title.trim() ? '请输入目标标题。' : undefined}
            htmlFor="paw-wb-goal-title"
            label="目标标题"
            required
          >
            <Input
              id="paw-wb-goal-title"
              onBlur={() => setTitleTouched(true)}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="例如：完成控制中心迁移"
              value={title}
            />
          </Field>
          <Field htmlFor="paw-wb-goal-detail" label="说明">
            <TextArea id="paw-wb-goal-detail" onChange={(event) => setDetail(event.target.value)} rows={4} value={detail} />
          </Field>
          <div className="paw-wb-operation-dialog__grid">
            <Field htmlFor="paw-wb-goal-horizon" label="时间范围">
              <Select
                id="paw-wb-goal-horizon"
                onValueChange={setHorizon}
                options={[
                  { label: '今天', value: 'today' },
                  { label: '近期', value: 'short_term' },
                  { label: '阶段目标', value: 'medium_term' },
                  { label: '长期目标', value: 'long_term' },
                ]}
                value={horizon}
              />
            </Field>
            <Field htmlFor="paw-wb-goal-status" label="状态">
              <Select
                id="paw-wb-goal-status"
                onValueChange={setStatus}
                options={[
                  { label: '进行中', value: 'active' },
                  { label: '已完成', value: 'completed' },
                  { label: '已归档', value: 'archived' },
                ]}
                value={status}
              />
            </Field>
          </div>
          <div className="paw-wb-operation-dialog__grid">
            <Field htmlFor="paw-wb-goal-priority" label="优先级">
              <Select
                id="paw-wb-goal-priority"
                onValueChange={(value) => setPriority(Number(value))}
                options={[
                  { label: '低', value: '0' },
                  { label: '普通', value: '1' },
                  { label: '高', value: '2' },
                  { label: '最高', value: '3' },
                ]}
                value={String(priority)}
              />
            </Field>
            <Field htmlFor="paw-wb-goal-target-date" label="目标日期">
              <Input id="paw-wb-goal-target-date" onChange={(event) => setTargetDate(event.target.value)} type="date" value={targetDate} />
            </Field>
          </div>
          <dl className="paw-wb-operation-dialog__context">
            <div><dt>项目</dt><dd>{project}</dd></div>
            <div><dt>Runtime revision</dt><dd>{runtimeRevision ?? '尚未同步'}</dd></div>
          </dl>
          <ManagementMutationWorkflow
            availability={availability}
            description="先由 Runtime 核对当前 revision，再保存并返回可撤销收据。"
            disabled={!title.trim()}
            draftKey={JSON.stringify(draft)}
            mutationKey={['paw-workbench', 'planning', 'goal-save', selectedGoalId || 'new']}
            onApply={async (preview) => parseManagementWorkReceipt(
              await boundary.request({
                pathId: planningMutationPathIds.goalSave,
                body: {
                  ...preview.context,
                  confirmText: preview.requiredConfirm,
                  expectedRuntimeRevision: preview.expectedRuntimeRevision,
                  payloadSha256: preview.payloadSha256,
                  previewToken: preview.previewToken,
                },
              }),
              planningMutationPathIds.goalSave,
              preview.payloadSha256,
            )}
            onApplied={onChanged}
            onPreview={async () => parseManagementWorkPreview(
              await boundary.request({
                pathId: planningMutationPathIds.preview,
                body: {
                  expectedRuntimeRevision: runtimeRevision ?? 0,
                  kind: 'goal.save',
                  payload: draft,
                },
              }),
              planningMutationPathIds.goalSave,
              draft,
            )}
            onRollback={async (receipt, preview) => parseManagementWorkReceipt(
              await boundary.request({
                pathId: planningMutationPathIds.rollback,
                body: {
                  confirmText: 'rollback',
                  payloadSha256: receipt.payloadSha256,
                  receiptId: receipt.receiptId,
                  rollbackToken: receipt.rollbackToken,
                },
              }),
              planningMutationPathIds.rollback,
              preview.payloadSha256,
            )}
            onRolledBack={onChanged}
            risk="R1"
            title={selectedGoalId ? '保存目标修改' : '创建目标'}
          />
        </div>
      </DialogContent>
    </Dialog>
  );
}

export function PawWorkbenchDocumentRegisterDialog({
  onChanged,
  onOpenChange,
  open,
}: {
  onChanged: () => void;
  onOpenChange: (open: boolean) => void;
  open: boolean;
}) {
  const transport = useControlTransport();
  const [authorityKind, setAuthorityKind] = useState<'session_todo' | 'session_goal' | 'room_work_item'>('session_todo');
  const [authorityId, setAuthorityId] = useState('');
  const [authorityRevision, setAuthorityRevision] = useState('');
  const [workspaceRoot, setWorkspaceRoot] = useState('');
  const [sourcePath, setSourcePath] = useState('');
  const [title, setTitle] = useState('');
  const capabilities = useQuery({
    enabled: open,
    queryKey: ['paw-workbench', 'work-documents', 'capabilities'],
    queryFn: () => transport.capabilities(),
    staleTime: 30_000,
  });
  const routeIds = new Set(capabilities.data?.routeIds ?? []);
  const supported = routeIds.has('workDocuments.register');
  const registrationReady = Boolean(
    authorityId.trim()
      && /^\d+$/.test(authorityRevision)
      && workspaceRoot.trim()
      && sourcePath.trim().startsWith('docs/')
      && sourcePath.trim().endsWith('.md'),
  );
  const mutation = useMutation({
    mutationKey: ['paw-workbench', 'work-documents', 'register'],
    mutationFn: (input: Extract<WorkDocumentCommandInput, { operation: 'register' }>) => requestWorkDocumentCommand(transport, input),
    onSuccess: () => onChanged(),
  });

  useEffect(() => {
    if (!open) return;
    setAuthorityKind('session_todo');
    setAuthorityId('');
    setAuthorityRevision('');
    setWorkspaceRoot('');
    setSourcePath('');
    setTitle('');
    mutation.reset();
  // The mutation object is intentionally excluded: reset only when the dialog opens.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  return (
    <Dialog onOpenChange={onOpenChange} open={open}>
      <DialogContent className="paw-wb-operation-dialog paw-wb-operation-dialog--document">
        <DialogHeader>
          <div className="paw-wb-operation-dialog__heading"><FilePlus2 aria-hidden size={19} /></div>
          <DialogTitle>登记工作文档</DialogTitle>
          <DialogDescription>把已有 Markdown 绑定到真实 Session 目标、任务或 Room WorkItem；不会创建第二套运行状态。</DialogDescription>
        </DialogHeader>
        <form
          className="paw-wb-operation-dialog__form"
          onSubmit={(event) => {
            event.preventDefault();
            if (!registrationReady || !supported) return;
            mutation.mutate({
              operation: 'register',
              authorityId: authorityId.trim(),
              authorityKind,
              authorityRevision: Number(authorityRevision),
              sourcePath: sourcePath.trim(),
              title: title.trim(),
              workspaceRoot: workspaceRoot.trim(),
            });
          }}
        >
          {capabilities.isPending ? <InlineNotice title="正在确认能力" tone="info">正在读取当前 Runtime 可用操作。</InlineNotice> : null}
          {capabilities.error ? <InlineNotice title="无法确认登记能力" tone="danger">{publicErrorText(capabilities.error)}</InlineNotice> : null}
          {!capabilities.isPending && !capabilities.error && !supported ? <InlineNotice title="当前不能登记" tone="warning">当前 Runtime 未公开 `workDocuments.register`，没有请求会被发送。</InlineNotice> : null}
          <Field htmlFor="paw-wb-document-kind" label="来源类型" required>
            <Select
              aria-label="来源类型"
              id="paw-wb-document-kind"
              onValueChange={(value) => setAuthorityKind(value as typeof authorityKind)}
              options={[
                { label: '对话任务', value: 'session_todo' },
                { label: '对话目标', value: 'session_goal' },
                { label: 'Room WorkItem', value: 'room_work_item' },
              ]}
              value={authorityKind}
            />
          </Field>
          <div className="paw-wb-operation-dialog__grid">
            <Field htmlFor="paw-wb-document-authority" label="来源编号" required>
              <Input id="paw-wb-document-authority" onChange={(event) => setAuthorityId(event.target.value)} value={authorityId} />
            </Field>
            <Field htmlFor="paw-wb-document-revision" label="来源版本" required>
              <Input id="paw-wb-document-revision" min="0" onChange={(event) => setAuthorityRevision(event.target.value)} type="number" value={authorityRevision} />
            </Field>
          </div>
          <Field htmlFor="paw-wb-document-root" label="工作区根目录" required>
            <Input id="paw-wb-document-root" onChange={(event) => setWorkspaceRoot(event.target.value)} placeholder="/path/to/project" value={workspaceRoot} />
          </Field>
          <Field description="必须是工作区内 docs/ 下已存在的 Markdown 文件。" htmlFor="paw-wb-document-source" label="Markdown 来源路径" required>
            <Input id="paw-wb-document-source" onChange={(event) => setSourcePath(event.target.value)} placeholder="docs/active/project.md" value={sourcePath} />
          </Field>
          <Field htmlFor="paw-wb-document-title" label="标题（可选）">
            <Input id="paw-wb-document-title" onChange={(event) => setTitle(event.target.value)} value={title} />
          </Field>
          {mutation.error ? <InlineNotice title="登记未完成" tone="danger">{publicErrorText(mutation.error)} 列表保持原状，可以修正来源后重试。</InlineNotice> : null}
          {mutation.data?.receipt?.status === 'applied' ? <InlineNotice title="登记完成" tone="success">Registry 已接受这份工作文档，列表正在同步。</InlineNotice> : null}
          {mutation.data?.receipt?.status === 'accepted' ? <InlineNotice title="登记已接受" tone="info">请求已进入 Registry，列表正在同步。</InlineNotice> : null}
          {mutation.data?.receipt?.status === 'failed' ? <InlineNotice title="登记未完成" tone="danger">Registry 返回失败收据，请核对来源与版本。</InlineNotice> : null}
          <DialogFooter>
            <Button onClick={() => onOpenChange(false)} type="button">取消</Button>
            <Button disabled={!registrationReady || !supported || mutation.isPending} loading={mutation.isPending} type="submit" variant="primary">确认登记</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function record(value: unknown): PawWorkbenchRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as PawWorkbenchRecord : {};
}

function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function entityId(value: PawWorkbenchRecord | null | undefined): string {
  return text(value?.id) || text(value?.taskId) || text(value?.goalId);
}

function isTaskDone(status: string): boolean {
  return status === 'done' || status === 'completed';
}

function taskStatusLabel(status: string): string {
  return ({
    active: '进行中',
    cancelled: '已取消',
    completed: '已完成',
    done: '已完成',
    in_progress: '进行中',
    todo: '待开始',
  } as Record<string, string>)[status] ?? '待开始';
}

function taskStatusTone(status: string): 'success' | 'warning' | 'danger' | 'info' | 'neutral' {
  if (isTaskDone(status)) return 'success';
  if (status === 'cancelled') return 'warning';
  if (status === 'in_progress' || status === 'active') return 'info';
  return 'neutral';
}

function finiteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function localDate(value: Date): string {
  const offset = value.getTimezoneOffset() * 60_000;
  return new Date(value.getTime() - offset).toISOString().slice(0, 10);
}
