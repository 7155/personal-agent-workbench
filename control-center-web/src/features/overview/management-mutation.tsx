import { useMutation } from '@tanstack/react-query';
import { Check, CircleDashed, RotateCcw, ShieldCheck } from 'lucide-react';
import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from 'react';
import { Button } from '@/components/primitives';
import { TraceAgentHandoffButton } from '@/features/trace-agent/handoff';
import {
  InlineNotice,
  StatusBadge,
  asRecord,
  booleanValue,
  numberValue,
  publicErrorText,
  stringValue,
} from './management-ui';

export type MutationAvailability = {
  state: 'checking' | 'available' | 'blocked' | 'unsupported';
  reason?: string;
};

export type ManagementWorkPreview<Context> = {
  context: Context;
  expectedRuntimeRevision: number;
  expiresAtMs: number;
  pathId: string;
  payloadSha256: string;
  previewToken: string;
  requiredConfirm: string;
  summary: {
    title: string;
    items: readonly string[];
    risk: 'R1' | 'R2' | 'R3';
  };
};

export type ManagementWorkReceipt = {
  appliedAtMs: number;
  pathId: string;
  payloadSha256: string;
  receiptId: string;
  rollbackAvailable: boolean;
  rollbackToken: string;
  raw: Record<string, unknown>;
};

export function ManagementMutationWorkflow<Context>({
  availability,
  description,
  disabled = false,
  draftKey,
  mutationKey,
  onApply,
  onApplied,
  onPreview,
  onRollback,
  onRolledBack,
  risk,
  title,
}: {
  availability: MutationAvailability;
  description: string;
  disabled?: boolean;
  /** @deprecated Confirmation follows the authoritative R3 server preview. */
  explicitConfirmation?: boolean;
  draftKey: string;
  mutationKey: readonly unknown[];
  onApply: (preview: ManagementWorkPreview<Context>) => Promise<ManagementWorkReceipt>;
  onApplied?: (receipt: ManagementWorkReceipt) => void;
  onPreview: () => Promise<ManagementWorkPreview<Context>>;
  onRollback?: (
    receipt: ManagementWorkReceipt,
    preview: ManagementWorkPreview<Context>,
  ) => Promise<ManagementWorkReceipt>;
  onRolledBack?: (receipt: ManagementWorkReceipt) => void;
  risk: 'R1' | 'R2' | 'R3';
  title: string;
}) {
  const [stage, setStage] = useState<'idle' | 'preview' | 'approval' | 'receipt' | 'rolled-back'>('idle');
  const [pendingTriggerLabel, setPendingTriggerLabel] = useState<string | null>(null);
  const [pendingPreviewError, setPendingPreviewError] = useState<unknown>(null);
  const [approved, setApproved] = useState(false);
  const [preview, setPreview] = useState<ManagementWorkPreview<Context> | null>(null);
  const [receipt, setReceipt] = useState<ManagementWorkReceipt | null>(null);
  const [rollbackReceipt, setRollbackReceipt] = useState<ManagementWorkReceipt | null>(null);
  const panelTitleId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const previewPanelRef = useRef<HTMLDivElement>(null);
  const approvalCheckboxRef = useRef<HTMLInputElement>(null);
  const receiptRef = useRef<HTMLDivElement>(null);
  const previewErrorRef = useRef<HTMLDivElement>(null);
  const applyErrorRef = useRef<HTMLDivElement>(null);
  const rollbackErrorRef = useRef<HTMLDivElement>(null);
  const previousStageRef = useRef(stage);
  const declaredDangerous = risk === 'R3';
  // The server preview is authoritative. A route may underestimate the risk,
  // but a preview promoted to R3 must never fall through the direct path.
  const effectiveRisk = preview?.summary.risk ?? risk;
  const requiresConfirmation = effectiveRisk === 'R3';

  const applyMutation = useMutation({
    mutationKey: [...mutationKey, 'apply'],
    mutationFn: async (boundPreview: ManagementWorkPreview<Context>) => onApply(boundPreview),
    onSuccess: (nextReceipt) => {
      setReceipt(nextReceipt);
      setStage('receipt');
      onApplied?.(nextReceipt);
    },
  });
  const previewMutation = useMutation({
    mutationKey: [...mutationKey, 'preview'],
    mutationFn: onPreview,
    onSuccess: (nextPreview) => {
      setPreview(nextPreview);
      setApproved(false);
      if (nextPreview.summary.risk === 'R3') setStage('preview');
      else applyMutation.mutate(nextPreview);
    },
  });
  const rollbackMutation = useMutation({
    mutationKey: [...mutationKey, 'rollback'],
    mutationFn: async ({ applied, boundPreview }: {
      applied: ManagementWorkReceipt;
      boundPreview: ManagementWorkPreview<Context>;
    }) => {
      if (!onRollback) throw new Error('这次操作当前不能撤销。');
      return onRollback(applied, boundPreview);
    },
    onSuccess: (nextReceipt) => {
      setRollbackReceipt(nextReceipt);
      setStage('rolled-back');
      onRolledBack?.(nextReceipt);
    },
  });

  useEffect(() => {
    if (stage !== 'preview' && stage !== 'approval') return;
    if (applyMutation.isPending) return;
    setPreview(null);
    setApproved(false);
    setStage('idle');
  // draftKey intentionally invalidates a server preview when editable input changes.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftKey]);

  useEffect(() => {
    const previousStage = previousStageRef.current;
    previousStageRef.current = stage;
    if (stage === 'preview') previewPanelRef.current?.focus();
    else if (stage === 'approval') approvalCheckboxRef.current?.focus();
    else if (stage === 'receipt' || stage === 'rolled-back') receiptRef.current?.focus();
    else if (stage === 'idle' && previousStage !== 'idle') triggerRef.current?.focus();
  }, [stage]);

  useEffect(() => {
    if (previewMutation.isError) previewErrorRef.current?.focus();
  }, [previewMutation.isError, previewMutation.failureCount]);

  useEffect(() => {
    if (applyMutation.isError) applyErrorRef.current?.focus();
  }, [applyMutation.isError, applyMutation.failureCount]);

  useEffect(() => {
    if (rollbackMutation.isError) rollbackErrorRef.current?.focus();
  }, [rollbackMutation.isError, rollbackMutation.failureCount]);

  const steps = useMemo(() => (
    requiresConfirmation
      ? [
          { id: 'preview', label: '查看影响' },
          { id: 'approval', label: '确认' },
          { id: 'receipt', label: '完成' },
          { id: 'rolled-back', label: '撤销' },
        ] as const
      : [
          { id: 'preview', label: '查看影响' },
          { id: 'receipt', label: '完成' },
          { id: 'rolled-back', label: '撤销' },
        ] as const
  ), [requiresConfirmation]);
  const stageIndex = stage === 'idle' ? -1 : steps.findIndex((step) => step.id === stage);
  const actionable = availability.state === 'available' && !disabled;
  const previewExpired = Boolean(preview && preview.expiresAtMs <= Date.now());
  const isWorking = previewMutation.isPending || applyMutation.isPending || rollbackMutation.isPending;
  const retryingPreview = stage === 'idle' && isWorking && pendingPreviewError != null;
  const displayedPreviewError = previewMutation.error ?? (retryingPreview ? pendingPreviewError : null);
  const triggerLabel = previewMutation.isError ? '重新尝试' : declaredDangerous ? '查看影响' : title;
  const liveStatus = previewMutation.isPending
    ? declaredDangerous ? '正在准备影响说明' : '正在准备更改'
    : applyMutation.isPending
      ? '正在保存更改'
      : rollbackMutation.isPending
        ? '正在撤销更改'
        : '';

  return (
    <div
      className="mgmt-workflow"
      data-availability={availability.state}
      data-confirmation={requiresConfirmation ? 'dangerous' : 'direct'}
      data-stage={stage}
    >
      <span aria-atomic="true" aria-live="polite" className="mgmt-sr-only">
        {liveStatus}
      </span>
      <div className="mgmt-workflow__heading">
        <div>
          {requiresConfirmation ? <span className="mgmt-workflow__risk">{riskLabel(effectiveRisk)}</span> : null}
          <strong>{title}</strong>
          <p>{description}</p>
        </div>
        {stage === 'idle' && (availability.state === 'available' || availability.state === 'checking') ? (
          <Button
            ref={triggerRef}
            disabled={!actionable}
            leadingIcon={declaredDangerous ? <ShieldCheck size={15} /> : <Check size={15} />}
            loading={previewMutation.isPending || applyMutation.isPending || availability.state === 'checking'}
            onClick={() => {
              // Keep the last failure's layout until this retry reaches its next stage.
              setPendingTriggerLabel(triggerLabel);
              setPendingPreviewError(previewMutation.error);
              previewMutation.mutate();
            }}
            preserveFocusWhileLoading
            size="small"
            variant={declaredDangerous ? 'danger' : 'primary'}
          >
            {isWorking ? pendingTriggerLabel ?? triggerLabel : triggerLabel}
          </Button>
        ) : null}
      </div>

      {availability.state !== 'available' && availability.state !== 'checking' && availability.reason ? (
        availability.state === 'unsupported' || requiresConfirmation ? (
          <InlineNotice title={availability.state === 'unsupported' ? '这项功能暂不可用' : '暂时无法执行'} tone="warning">
            {availability.reason}
          </InlineNotice>
        ) : <p className="mgmt-workflow__hint">{availability.reason}</p>
      ) : null}

      {displayedPreviewError ? (
        <div ref={previewErrorRef} className="mgmt-workflow__feedback" tabIndex={-1}>
          <InlineNotice
            title={retryingPreview ? applyMutation.isPending ? '正在保存本次更改' : '正在重试上次操作' : declaredDangerous ? '暂时无法查看影响' : '预览失败'}
            tone={retryingPreview ? 'info' : 'danger'}
          >
            {publicErrorText(displayedPreviewError, declaredDangerous ? '暂时无法查看影响，请稍后重试。' : '暂时无法预览，请稍后重试。')}
            <TraceAgentHandoffButton handoff={{
              kind: 'runtime',
              entityId: `${draftKey}:preview`,
              title: `${title}预检失败`,
              summary: publicErrorText(displayedPreviewError, '操作预检失败。'),
              error: displayedPreviewError instanceof Error ? displayedPreviewError.message : String(displayedPreviewError),
              refs: { stage: 'preview', risk: effectiveRisk },
            }} />
          </InlineNotice>
        </div>
      ) : null}

      {requiresConfirmation && stage !== 'idle' ? (
        <ol className="mgmt-workflow__steps" aria-label="操作进度">
          {steps.map((step, index) => (
            <li
              aria-current={index === stageIndex ? 'step' : undefined}
              data-state={index < stageIndex ? 'complete' : index === stageIndex ? 'current' : 'pending'}
              key={step.id}
            >
              {index < stageIndex ? <Check aria-hidden="true" size={13} /> : index === stageIndex ? <CircleDashed aria-hidden="true" size={13} /> : <i aria-hidden="true" />}
              {step.label}
            </li>
          ))}
        </ol>
      ) : null}

      {requiresConfirmation && stage === 'preview' && preview ? (
        <div
          ref={previewPanelRef}
          aria-labelledby={panelTitleId}
          className="mgmt-workflow__panel"
          tabIndex={-1}
        >
          <strong id={panelTitleId}>{preview.summary.title}</strong>
          <ul>{preview.summary.items.map((line) => <li key={line}>{line}</li>)}</ul>
          <div className="mgmt-workflow__binding">
            <span>已核对当前状态</span>
            <span>{previewExpired ? '影响说明已过期' : `有效至 ${formatTimestamp(preview.expiresAtMs)}`}</span>
          </div>
          {previewExpired ? <InlineNotice title="请重新查看影响" tone="warning">页面状态已经变化，旧的影响说明不会继续执行。</InlineNotice> : null}
          <div className="mgmt-workflow__buttons">
            <Button disabled={isWorking} onClick={() => reset()} size="small" variant="quiet">先不更改</Button>
            <Button
              disabled={previewExpired || !actionable || isWorking}
              onClick={() => setStage('approval')}
              size="small"
              variant="primary"
            >
              继续确认
            </Button>
          </div>
        </div>
      ) : null}

      {requiresConfirmation && stage === 'approval' && preview ? (
        <div className="mgmt-workflow__panel">
          <strong>请确认你已看过上方影响</strong>
          <label className="mgmt-workflow__confirm">
            <input
              ref={approvalCheckboxRef}
              checked={approved}
              disabled={!actionable || applyMutation.isPending || applyMutation.isError}
              onChange={(event) => setApproved(event.target.checked)}
              type="checkbox"
            />
            <span>我确认只执行上方列出的更改</span>
          </label>
          <div className="mgmt-workflow__buttons">
            <Button disabled={applyMutation.isPending} onClick={() => setStage('preview')} size="small" variant="quiet">返回查看</Button>
            <Button
              disabled={!approved || previewExpired || !actionable || applyMutation.isError}
              loading={applyMutation.isPending}
              onClick={() => applyMutation.mutate(preview)}
              preserveFocusWhileLoading
              size="small"
              variant={preview.summary.risk === 'R3' ? 'danger' : 'primary'}
            >
              确认执行
            </Button>
          </div>
        </div>
      ) : null}

      {applyMutation.error ? (
        <div ref={applyErrorRef} className="mgmt-workflow__panel" tabIndex={-1}>
          <InlineNotice title="更改未完成" tone="danger">
            {publicErrorText(applyMutation.error, '暂时无法保存，请稍后重试。')}
            <TraceAgentHandoffButton handoff={{
              kind: 'runtime',
              entityId: preview?.pathId || `${draftKey}:apply`,
              title: `${title}执行失败`,
              summary: publicErrorText(applyMutation.error, '暂时无法保存，请稍后重试。'),
              error: applyMutation.error instanceof Error ? applyMutation.error.message : String(applyMutation.error),
              failureRef: preview?.payloadSha256,
              refs: { stage: 'apply', pathId: preview?.pathId, risk: effectiveRisk },
            }} />
          </InlineNotice>
          <div className="mgmt-workflow__buttons">
            <Button onClick={() => reset()} size="small" variant="quiet">{requiresConfirmation ? '重新查看影响' : '返回'}</Button>
          </div>
        </div>
      ) : null}

      {stage === 'receipt' && receipt && preview ? (
        <WorkReceipt compact={!requiresConfirmation} focusRef={receiptRef} receipt={receipt} rolledBack={false}>
          {receipt.rollbackAvailable && onRollback ? (
            <div className="mgmt-workflow__receipt-actions">
              <Button disabled={rollbackMutation.isPending} onClick={() => reset()} size="small" variant="quiet">
                完成
              </Button>
              <Button
                leadingIcon={<RotateCcw size={14} />}
                loading={rollbackMutation.isPending}
                onClick={() => rollbackMutation.mutate({ applied: receipt, boundPreview: preview })}
                preserveFocusWhileLoading
                size="small"
              >
                {requiresConfirmation ? '撤销这次更改' : '撤销'}
              </Button>
            </div>
          ) : (
            <Button onClick={() => reset()} size="small" variant="quiet">完成</Button>
          )}
        </WorkReceipt>
      ) : null}

      {rollbackMutation.error ? (
        <div ref={rollbackErrorRef} className="mgmt-workflow__feedback" tabIndex={-1}>
          <InlineNotice title="撤销失败" tone="danger">
            {publicErrorText(rollbackMutation.error)}
            <TraceAgentHandoffButton handoff={{
              kind: 'runtime',
              entityId: receipt?.receiptId || `${draftKey}:rollback`,
              title: `${title}撤销失败`,
              summary: publicErrorText(rollbackMutation.error),
              error: rollbackMutation.error instanceof Error ? rollbackMutation.error.message : String(rollbackMutation.error),
              failureRef: receipt?.receiptId,
              refs: { stage: 'rollback', pathId: receipt?.pathId, risk: effectiveRisk },
            }} />
          </InlineNotice>
        </div>
      ) : null}

      {stage === 'rolled-back' && rollbackReceipt ? (
        <WorkReceipt compact={!requiresConfirmation} focusRef={receiptRef} receipt={rollbackReceipt} rolledBack>
          <Button onClick={() => reset()} size="small" variant="quiet">完成</Button>
        </WorkReceipt>
      ) : null}
    </div>
  );

  function reset() {
    setStage('idle');
    setPendingPreviewError(null);
    setApproved(false);
    setPreview(null);
    setReceipt(null);
    setRollbackReceipt(null);
    previewMutation.reset();
    applyMutation.reset();
    rollbackMutation.reset();
  }
}

export function UnsupportedWorkflow({
  description,
  reason,
  risk,
  title,
}: {
  description: string;
  reason: string;
  risk: 'R1' | 'R2' | 'R3';
  title: string;
}) {
  const dangerous = risk === 'R3';
  return (
    <div
      className="mgmt-workflow"
      data-availability="unsupported"
      data-confirmation={dangerous ? 'dangerous' : 'direct'}
      data-stage="idle"
    >
      <div className="mgmt-workflow__heading">
        <div>
          {dangerous ? <span className="mgmt-workflow__risk">{riskLabel(risk)}</span> : null}
          <strong>{title}</strong>
          <p>{description}</p>
        </div>
      </div>
      {dangerous ? (
        <InlineNotice title="暂时无法执行" tone="warning">{reason}</InlineNotice>
      ) : <p className="mgmt-workflow__hint">{reason}</p>}
    </div>
  );
}

export function parseManagementWorkPreview<Context>(
  value: unknown,
  expectedPathId: string,
  context: Context,
): ManagementWorkPreview<Context> {
  const payload = asRecord(value);
  const summary = asRecord(payload.summary);
  const expectedRevision = asRecord(payload.expectedRevision);
  const previewToken = stringValue(payload.previewToken);
  const payloadSha256 = stringValue(payload.payloadSha256);
  const pathId = stringValue(payload.pathId);
  const requiredConfirm = stringValue(payload.requiredConfirm);
  const expiresAtMs = numberValue(payload.expiresAtMs);
  const expectedRuntimeRevision = numberValue(expectedRevision.runtimeRevision);
  const items = Array.isArray(summary.items)
    ? summary.items.filter((item): item is string => typeof item === 'string' && item.length > 0)
    : [];
  const risk = stringValue(summary.risk);
  if (payload.ok !== true) throw new Error(stringValue(payload.message, stringValue(payload.error, '暂时无法准备这次更改。')));
  if (!previewToken || !payloadSha256 || pathId !== expectedPathId || requiredConfirm !== 'apply' || !expiresAtMs) {
    throw new Error('这次更改的信息不完整，请刷新后重试。');
  }
  if (!Number.isInteger(expectedRuntimeRevision) || expectedRuntimeRevision < 0) {
    throw new Error('页面内容已经变化，请刷新后重试。');
  }
  if (!stringValue(summary.title) || items.length === 0 || !['R1', 'R2', 'R3'].includes(risk)) {
    throw new Error('暂时无法核对这次更改，请重试。');
  }
  return {
    context,
    expectedRuntimeRevision,
    expiresAtMs,
    pathId,
    payloadSha256,
    previewToken,
    requiredConfirm,
    summary: {
      title: publicWorkflowText(stringValue(summary.title), '确认本次变更'),
      items: publicWorkflowItems(items),
      risk: risk as 'R1' | 'R2' | 'R3',
    },
  };
}

const internalWorkflowText = /(?:pathId|operation(?:Id)?|receipt(?:Id)?|rollbackToken|payloadSha|runtimeRevision|previewToken|expectedRevision|schema|policy(?:Id)?|profile(?:Id|Version)?|\bID\b|sha256:|https?:\/\/|\/api\/)/i;

function publicWorkflowText(value: string, fallback: string): string {
  const text = value.trim();
  if (!text || text.length > 220 || internalWorkflowText.test(text)) return fallback;
  return text;
}

function publicWorkflowItems(items: readonly string[]): string[] {
  const visible = items
    .map((item) => publicWorkflowText(item, ''))
    .filter(Boolean);
  return visible.length ? visible : ['只会应用你在页面中确认的内容。'];
}

export function parseManagementWorkReceipt(
  value: unknown,
  expectedPathId: string,
  expectedPayloadSha256: string,
): ManagementWorkReceipt {
  const payload = asRecord(value);
  const receiptId = stringValue(payload.receiptId);
  const pathId = stringValue(payload.pathId);
  const payloadSha256 = stringValue(payload.payloadSha256);
  const rollbackAvailable = booleanValue(payload.rollbackAvailable);
  const rollbackToken = stringValue(payload.rollbackToken);
  const appliedAtMs = numberValue(payload.appliedAtMs);
  if (payload.ok !== true) throw new Error(stringValue(payload.message, stringValue(payload.error, '服务拒绝了这次操作。')));
  if (!receiptId || pathId !== expectedPathId || payloadSha256 !== expectedPayloadSha256 || !appliedAtMs) {
    throw new Error('这次操作结果暂时无法确认，请刷新后重试。');
  }
  if (rollbackAvailable && !rollbackToken) throw new Error('这次操作暂时无法安全撤销，请刷新后重试。');
  return {
    appliedAtMs,
    pathId,
    payloadSha256,
    receiptId,
    rollbackAvailable,
    rollbackToken,
    raw: payload,
  };
}

function WorkReceipt({
  children,
  compact,
  focusRef,
  receipt,
  rolledBack,
}: {
  children: React.ReactNode;
  compact?: boolean;
  focusRef: RefObject<HTMLDivElement | null>;
  receipt: ManagementWorkReceipt;
  rolledBack: boolean;
}) {
  return (
    <div
      ref={focusRef}
      aria-atomic="true"
      aria-live="polite"
      className="mgmt-workflow__receipt"
      data-compact={compact || undefined}
      role="status"
      tabIndex={-1}
    >
      <div>
        <StatusBadge label={rolledBack ? '已撤销' : '已完成'} tone={rolledBack ? 'info' : 'success'} />
        <strong>{rolledBack ? '已恢复到更改前' : compact ? '已保存' : '更改已记录'}</strong>
        <span>{rolledBack ? '原来的更改不再生效' : receipt.rollbackAvailable ? '仍可以撤销' : '这次更改不可撤销'}</span>
        <time>{formatTimestamp(receipt.appliedAtMs)}</time>
      </div>
      {children}
    </div>
  );
}

function formatTimestamp(value: number): string {
  return new Date(value).toLocaleString('zh-CN', { hour12: false });
}

function riskLabel(value: 'R1' | 'R2' | 'R3'): string {
  return ({ R1: '操作确认', R2: '重要更改', R3: '高风险' } as const)[value];
}
