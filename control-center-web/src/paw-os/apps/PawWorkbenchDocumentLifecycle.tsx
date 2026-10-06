import {
  Archive,
  CheckCircle2,
  RotateCcw,
  Trash2,
  Wrench,
} from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
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
} from '@/components/primitives';
import type {
  WorkDocumentCommandV1,
  WorkDocumentDetailV1,
  WorkDocumentErasePreviewV1,
  WorkDocumentV1,
} from '@/contracts/work-documents';
import { sessionItems, type AgentSessionListResponse } from '@/features/agent/types';
import { InlineNotice, publicErrorText } from '@/features/overview/management-ui';
import {
  requestWorkDocumentCommand,
  requestWorkDocumentErasePreview,
  type WorkDocumentAccess,
  type WorkDocumentCommandInput,
} from '@/features/work-documents/api';
import type { ControlTransport } from '@/platform/transport';

const ERASE_CONFIRMATION = '永久清除';
type RepairableState = Extract<WorkDocumentV1['state'], 'archive_pending' | 'reopen_pending' | 'error'>;

/**
 * The lifecycle reader is intentionally independent from the legacy management
 * page. Workbench can mount it in a document reader or a satellite window while
 * keeping the Runtime-owned command and approval contracts unchanged.
 */
export type PawWorkbenchDocumentCurrent = WorkDocumentV1 | WorkDocumentDetailV1;

export interface PawWorkbenchDocumentLifecycleProps {
  /** A full WorkDocument detail or its document projection. */
  current: PawWorkbenchDocumentCurrent | null;
  /** Optional fresh reopen context when the host keeps it separate from current. */
  reopen?: WorkDocumentDetailV1['reopen'];
  access: WorkDocumentAccess;
  transport: ControlTransport;
  onChanged?: (result: WorkDocumentCommandV1) => void;
  onErased?: (result: WorkDocumentCommandV1) => void;
}

interface SessionOption {
  value: string;
  label: string;
}

interface PreviewState {
  fence: string;
  documentId: string;
  sessionId: string;
  result: WorkDocumentErasePreviewV1;
}

export function PawWorkbenchDocumentLifecycle({
  access,
  current,
  onChanged,
  onErased,
  reopen: reopenOverride,
  transport,
}: PawWorkbenchDocumentLifecycleProps) {
  const document = resolveDocument(current);
  const reopen = reopenOverride ?? resolveReopen(current);
  const fence = document ? documentFence(document, reopen) : 'empty';
  const fenceRef = useRef(fence);
  const mountedRef = useRef(true);
  fenceRef.current = fence;

  const [terminalReceiptId, setTerminalReceiptId] = useState(document?.terminalReceiptId ?? '');
  const [pendingOperation, setPendingOperation] = useState<WorkDocumentCommandInput['operation'] | null>(null);
  const [receipt, setReceipt] = useState<WorkDocumentCommandV1['receipt'] | null>(null);
  const [commandError, setCommandError] = useState<unknown>(null);

  const [eraseOpen, setEraseOpen] = useState(false);
  const [sessionOptions, setSessionOptions] = useState<SessionOption[]>([]);
  const [sessionId, setSessionId] = useState('');
  const [sessionsPending, setSessionsPending] = useState(false);
  const [sessionsError, setSessionsError] = useState<unknown>(null);
  const sessionReadRef = useRef(0);
  const [previewPending, setPreviewPending] = useState(false);
  const [previewError, setPreviewError] = useState<unknown>(null);
  const [previewState, setPreviewState] = useState<PreviewState | null>(null);
  const [eraseConfirmation, setEraseConfirmation] = useState('');

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    setTerminalReceiptId(document?.terminalReceiptId ?? '');
    setReceipt(null);
    setCommandError(null);
    setPendingOperation(null);
    setEraseOpen(false);
    setSessionOptions([]);
    setSessionId('');
    setSessionsPending(false);
    setSessionsError(null);
    sessionReadRef.current += 1;
    setPreviewPending(false);
    setPreviewError(null);
    setPreviewState(null);
    setEraseConfirmation('');
  }, [document?.documentId, document?.documentRevision, fence]);

  const isCurrent = (capturedFence: string) => mountedRef.current && fenceRef.current === capturedFence;
  const runCommand = async (input: WorkDocumentCommandInput) => {
    if (!document) return;
    const capturedFence = fenceRef.current;
    setPendingOperation(input.operation);
    setCommandError(null);
    setReceipt(null);
    try {
      const result = await requestWorkDocumentCommand(transport, input);
      if (!isCurrent(capturedFence)) return;
      setReceipt(result.receipt ?? null);
      onChanged?.(result);
      if (input.operation === 'erase' && result.receipt?.status === 'applied') {
        onErased?.(result);
        closeErase(false);
      }
    } catch (error) {
      if (isCurrent(capturedFence)) setCommandError(error);
    } finally {
      if (isCurrent(capturedFence)) setPendingOperation(null);
    }
  };

  const readEraseSessions = () => {
    if (!document || !access.erase) return;
    const readId = ++sessionReadRef.current;
    setSessionOptions([]);
    setSessionId('');
    setSessionsError(null);
    setPreviewError(null);
    setPreviewState(null);
    const capturedFence = fenceRef.current;
    setSessionsPending(true);
    void transport.request<AgentSessionListResponse>({
      pathId: 'agent.sessions.list',
      query: { limit: 200 },
    }).then((response) => {
      if (!isCurrent(capturedFence) || sessionReadRef.current !== readId) return;
      const options = conversationOptions(sessionItems(response).filter((item) => item.status !== 'archived'));
      setSessionOptions(options);
      setSessionId(options[0]?.value ?? '');
    }).catch((error: unknown) => {
      if (isCurrent(capturedFence) && sessionReadRef.current === readId) setSessionsError(error);
    }).finally(() => {
      if (isCurrent(capturedFence) && sessionReadRef.current === readId) setSessionsPending(false);
    });
  };

  const openErase = () => {
    if (!document || !access.erase) return;
    setEraseOpen(true);
    setEraseConfirmation('');
    readEraseSessions();
  };

  const closeErase = (open: boolean) => {
    setEraseOpen(open);
    if (open) return;
    sessionReadRef.current += 1;
    setSessionsPending(false);
    setSessionOptions([]);
    setSessionId('');
    setSessionsError(null);
    setPreviewPending(false);
    setPreviewError(null);
    setPreviewState(null);
    setEraseConfirmation('');
  };

  const prepareErase = () => {
    if (!document || !sessionId.trim() || sessionsPending || sessionsError) return;
    const capturedFence = fenceRef.current;
    const capturedDocumentId = document.documentId;
    const capturedSessionId = sessionId.trim();
    setPreviewPending(true);
    setPreviewError(null);
    setPreviewState(null);
    void requestWorkDocumentErasePreview(transport, capturedDocumentId, capturedSessionId)
      .then((result) => {
        if (!isCurrent(capturedFence)) return;
        setPreviewState({
          documentId: capturedDocumentId,
          fence: capturedFence,
          result,
          sessionId: capturedSessionId,
        });
      })
      .catch((error: unknown) => {
        if (isCurrent(capturedFence)) setPreviewError(error);
      })
      .finally(() => {
        if (isCurrent(capturedFence)) setPreviewPending(false);
      });
  };

  const approvedPreview = useMemo(() => (
    previewState
    && previewState.fence === fence
    && previewState.documentId === document?.documentId
    && previewState.sessionId === sessionId.trim()
      ? previewState.result
      : null
  ), [document?.documentId, fence, previewState, sessionId]);
  const approvalId = approvedPreview?.approval.approvalId ?? '';
  const payloadSha256 = approvedPreview?.payloadSha256 ?? '';
  const repairable = document ? isRepairableState(document.state) : false;

  if (!document) {
    return (
      <section aria-label="工作文档生命周期" className="paw-wb-document-lifecycle paw-wb-document-lifecycle--empty">
        <strong>选择一份工作文档</strong>
        <p>选择后才能查看归档、修复、恢复或清除操作。</p>
      </section>
    );
  }

  return (
    <section aria-label="工作文档生命周期" className="paw-wb-document-lifecycle" data-state={document.state}>
      <header className="paw-wb-document-lifecycle__header">
        <div>
          <h2>归档与恢复</h2>
          <p>每个动作都绑定当前文档、来源版本和 Runtime 收据。</p>
        </div>
        <span className="paw-wb-document-lifecycle__state">{stateLabel(document.state)}</span>
      </header>

      {commandError ? (
        <InlineNotice title="文档操作未完成" tone="danger">
          {publicErrorText(commandError)} 状态重新同步前，不会假定文件已经移动或清除。
        </InlineNotice>
      ) : null}
      {document.state === 'active' ? (
        <div className="paw-wb-document-lifecycle__action">
          <Field
            description="归档前需要提供已完成的终端收据；归档不会清除内容。"
            htmlFor="paw-wb-document-terminal-receipt"
            label="完成依据"
            required
          >
            <Input
              id="paw-wb-document-terminal-receipt"
              onChange={(event) => setTerminalReceiptId(event.target.value)}
              value={terminalReceiptId}
            />
          </Field>
          <Button
            disabled={!access.archive || !terminalReceiptId.trim()}
            leadingIcon={<Archive size={16} />}
            loading={pendingOperation === 'archive'}
            onClick={() => void runCommand({
              documentId: document.documentId,
              operation: 'archive',
              terminalReceiptId: terminalReceiptId.trim(),
            })}
          >
            {access.archive ? '归档到历史' : '当前宿主不支持归档'}
          </Button>
          {!access.archive ? <p className="paw-wb-document-lifecycle__unavailable">当前 Runtime 未公开归档路由，页面不会发送请求。</p> : null}
        </div>
      ) : null}

      {repairable ? (
        <div className="paw-wb-document-lifecycle__action paw-wb-document-lifecycle__action--compact">
          <div>
            <strong>重新检查状态</strong>
            <p>异常或过渡状态仍会保留在列表中；修复只重新核对 Runtime 状态。</p>
          </div>
          <Button
            disabled={!access.repair}
            leadingIcon={<Wrench size={16} />}
            loading={pendingOperation === 'repair'}
            onClick={() => void runCommand({ documentId: document.documentId, operation: 'repair' })}
          >
            {access.repair ? '重新检查状态' : '当前宿主不支持重新检查'}
          </Button>
          {!access.repair ? <p className="paw-wb-document-lifecycle__unavailable">当前 Runtime 未公开修复路由，页面不会发送请求。</p> : null}
        </div>
      ) : null}

      {document.state === 'archived' ? (
        <div className="paw-wb-document-lifecycle__action paw-wb-document-lifecycle__action--compact">
          <div>
            <strong>重新打开到活跃区</strong>
            <p>{reopenGuidance(reopen)}</p>
          </div>
          <Button
            disabled={!access.reopen || !reopen?.eligible}
            leadingIcon={<RotateCcw size={16} />}
            loading={pendingOperation === 'reopen'}
            onClick={() => {
              if (!reopen?.eligible) return;
              void runCommand({
                authorityRevision: reopen.authorityRevision,
                documentId: document.documentId,
                operation: 'reopen',
                transitionReceiptId: reopen.transitionReceiptId,
              });
            }}
          >
            {access.reopen ? '重新打开到活跃区' : '当前宿主不支持重新打开'}
          </Button>
          {!access.reopen ? <p className="paw-wb-document-lifecycle__unavailable">当前 Runtime 未公开恢复路由，页面不会发送请求。</p> : null}
        </div>
      ) : null}

      <section className="paw-wb-document-lifecycle__danger" aria-labelledby="paw-wb-document-erase-heading">
        <div>
          <h3 id="paw-wb-document-erase-heading">永久清除</h3>
          <p>清除与归档不同：它会删除受管记录，且不能恢复。需要审批和精确确认词。</p>
        </div>
        <Button
          disabled={!access.erase || pendingOperation !== null}
          leadingIcon={<Trash2 size={16} />}
          onClick={openErase}
          variant="danger"
        >
          {access.erase ? '永久清除…' : '当前宿主不支持永久清除'}
        </Button>
        {!access.erase ? <p className="paw-wb-document-lifecycle__unavailable">当前 Runtime 未同时公开清除预览和执行路由，页面不会发送请求。</p> : null}
      </section>

      {receipt ? <CommandReceipt receipt={receipt} /> : null}

      <Dialog onOpenChange={closeErase} open={eraseOpen}>
        <DialogContent className="paw-wb-document-lifecycle__erase-dialog">
          <DialogHeader>
            <DialogTitle>永久清除工作文档</DialogTitle>
            <DialogDescription>
              这不是归档：批准后会清除“{document.title || '这份工作文档'}”及其受管记录。
            </DialogDescription>
          </DialogHeader>
          <div className="paw-wb-document-lifecycle__erase-form">
            {sessionsError ? (
              <InlineNotice title="暂时无法读取可用对话" tone="danger"><p>重新读取后可继续选择接收审批的对话。</p><Button onClick={readEraseSessions} size="small" variant="quiet">重新读取可用对话</Button></InlineNotice>
            ) : sessionOptions.length ? (
              <Field
                description="审批请求会绑定到所选 Agent Session。已归档对话不会出现在这里。"
                htmlFor="paw-wb-document-erase-session"
                label="接收审批的对话"
                required
              >
                <Select
                  aria-label="接收审批的对话"
                  id="paw-wb-document-erase-session"
                  onValueChange={setSessionId}
                  options={sessionOptions}
                  value={sessionId}
                />
              </Field>
            ) : sessionsPending ? (
              <InlineNotice title="正在读取可用对话" tone="info">读取完成后即可准备清除审批。</InlineNotice>
            ) : (
              <InlineNotice title="没有可用于审批的对话" tone="warning">先打开一段 Agent 对话，再回来继续永久清除。</InlineNotice>
            )}
            <Button
              disabled={sessionsPending || Boolean(sessionsError) || !sessionId.trim()}
              loading={previewPending}
              onClick={prepareErase}
            >
              准备永久清除
            </Button>
            {previewError ? <InlineNotice title="无法获取清除审批" tone="danger">{publicErrorText(previewError)}</InlineNotice> : null}
            {approvalId && payloadSha256 ? (
              <div aria-label="清除审批已就绪" className="paw-wb-document-lifecycle__approval" role="status">
                <CheckCircle2 aria-hidden size={18} />
                <div><strong>清除审批已就绪</strong><span>审批已绑定到当前文档和所选 Agent Session。</span></div>
              </div>
            ) : null}
            <Field
              description={`输入“${ERASE_CONFIRMATION}”确认。系统审批仍会独立生效。`}
              htmlFor="paw-wb-document-erase-confirmation"
              label="永久清除确认"
              required
            >
              <Input
                autoComplete="off"
                id="paw-wb-document-erase-confirmation"
                onChange={(event) => setEraseConfirmation(event.target.value)}
                value={eraseConfirmation}
              />
            </Field>
          </div>
          <DialogFooter>
            <Button onClick={() => closeErase(false)} variant="quiet">取消</Button>
            <Button
              disabled={!approvalId || !payloadSha256 || eraseConfirmation !== ERASE_CONFIRMATION || pendingOperation !== null}
              loading={pendingOperation === 'erase'}
              onClick={() => {
                if (!approvalId || !payloadSha256 || !document) return;
                void runCommand({
                  approvalId,
                  documentId: document.documentId,
                  operation: 'erase',
                  payloadSha256,
                  sessionId: sessionId.trim(),
                });
              }}
              variant="danger"
            >
              永久清除，不是归档
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}

function resolveDocument(current: PawWorkbenchDocumentCurrent | null): WorkDocumentV1 | null {
  if (!current) return null;
  return 'document' in current ? current.document : current;
}

function resolveReopen(current: PawWorkbenchDocumentCurrent | null): WorkDocumentDetailV1['reopen'] | undefined {
  if (!current || !('document' in current)) return undefined;
  return current.reopen;
}

function documentFence(document: WorkDocumentV1, reopen: WorkDocumentDetailV1['reopen'] | undefined): string {
  return [
    document.documentId,
    document.authorityId,
    document.authorityKey,
    document.authorityRevision,
    document.documentRevision,
    reopen?.authorityRevision ?? '',
    reopen?.transitionReceiptId ?? '',
  ].join(':');
}

function isRepairableState(value: WorkDocumentV1['state']): value is RepairableState {
  return value === 'archive_pending' || value === 'reopen_pending' || value === 'error';
}

function conversationOptions(
  sessions: ReturnType<typeof sessionItems>,
): SessionOption[] {
  const titleCounts = new Map<string, number>();
  sessions.forEach((session) => {
    const title = session.title.trim() || '未命名对话';
    titleCounts.set(title, (titleCounts.get(title) ?? 0) + 1);
  });
  return sessions.map((session) => {
    const title = session.title.trim() || '未命名对话';
    return {
      label: (titleCounts.get(title) ?? 0) > 1 ? `${title} · ${session.id}` : title,
      value: session.id,
    };
  });
}

function stateLabel(value: WorkDocumentV1['state']): string {
  return {
    active: '进行中',
    archive_pending: '正在归档',
    archived: '已归档',
    reopen_pending: '正在重新打开',
    error: '需要修复',
  }[value];
}

function reopenGuidance(reopen: WorkDocumentDetailV1['reopen'] | undefined): string {
  switch (reopen?.reasonCode) {
    case 'ready':
      return '来源已经可以继续；重新打开会按当前状态恢复文档，并保留历史记录。';
    case 'authority_terminal':
      return '来源仍处于已完成或已取消状态。请先恢复来源，再刷新此页。';
    case 'authority_not_advanced':
      return '来源尚未产生新的可继续版本。请先恢复来源并刷新此页。';
    case 'authority_unavailable':
      return '暂时无法核对来源状态；核对完成前不会重新打开。';
    case 'document_not_archived':
      return '只有已归档文档可以重新打开。';
    default:
      return '正在核对来源状态；核对完成前不会重新打开。';
  }
}

function CommandReceipt({ receipt }: { receipt: NonNullable<WorkDocumentCommandV1['receipt']> }) {
  const status = receipt.status === 'applied'
    ? '已应用'
    : receipt.status === 'failed'
      ? '未完成'
      : '已接受，等待完成';
  return (
    <div aria-live="polite" className="paw-wb-document-lifecycle__receipt" role="status">
      <strong>操作结果 · {status}</strong>
      <span>{operationLabel(receipt.operation)}{receipt.idempotent ? ' · 已避免重复执行' : ''}</span>
    </div>
  );
}

function operationLabel(value: string): string {
  return {
    archive: '归档文档',
    repair: '重新检查文档',
    reopen: '重新打开文档',
    erase: '永久清除文档',
  }[value] ?? '文档操作';
}
