import type { ControlPathId } from '@/platform/routes';
import type { ControlRequest } from '@/platform/transport';
import type { MockRouteHandler } from '@/test/mock-transport';

const ACTIVE_DOCUMENT_ID = 'workdoc_0123456789abcdef0123456789abcdef';
const ARCHIVED_DOCUMENT_ID = 'workdoc_fedcba9876543210fedcba9876543210';
const ERASE_PAYLOAD_SHA256 = '3'.repeat(64);

type DocumentState = 'active' | 'archived' | 'error';
type PreviewRoutes = Partial<Record<ControlPathId, MockRouteHandler>>;

export function createPreviewWorkDocumentRoutes(): PreviewRoutes {
  let activeState: DocumentState = 'active';
  let activeRevision = 3;
  let activeTerminalReceiptId = '';

  const activeDocument = () => previewWorkDocument(
    ACTIVE_DOCUMENT_ID,
    activeState,
    activeRevision,
    activeTerminalReceiptId,
  );

  return {
    'workDocuments.list': () => {
      const items = activeState === 'archived' ? [] : [activeDocument()];
      return { schemaVersion: 'rag-ime.work-document-list.v1', items, total: items.length };
    },
    'workDocuments.history.search': (request: ControlRequest) => {
      const items = [previewWorkDocument(
        ARCHIVED_DOCUMENT_ID,
        'archived',
        2,
        'terminal-receipt-preview-archived',
      )];
      if (activeState === 'archived') items.push(activeDocument());
      const query = stringValue(record(request.query).query).trim().toLocaleLowerCase();
      const matches = query ? items.filter(document => ['title', 'authorityId', 'authorityKey', 'path', 'activePath', 'archivePath']
        .some(key => stringValue(document[key]).toLocaleLowerCase().includes(query))) : items;
      return { schemaVersion: 'rag-ime.work-document-list.v1', items: matches, total: matches.length };
    },
    'workDocuments.get': (request: ControlRequest) => {
      const documentId = stringValue(record(request.params).documentId);
      const document = documentId === ARCHIVED_DOCUMENT_ID
        ? previewWorkDocument(
            ARCHIVED_DOCUMENT_ID,
            'archived',
            2,
            'terminal-receipt-preview-archived',
          )
        : activeDocument();
      return {
        schemaVersion: 'rag-ime.work-document-detail.v1',
        document,
        reopen: {
          eligible: document.state === 'archived',
          authorityRevision: document.authorityRevision,
          transitionReceiptId: document.terminalReceiptId || '',
          reasonCode: document.state === 'archived' ? 'ready' : 'document_not_archived',
        },
      };
    },
    'workDocuments.register': () => previewWorkDocumentCommand('register', activeDocument()),
    'workDocuments.archive': (request: ControlRequest) => {
      activeState = 'archived';
      activeRevision += 1;
      activeTerminalReceiptId = stringValue(record(request.body).terminalReceiptId);
      return previewWorkDocumentCommand('archive', activeDocument());
    },
    'workDocuments.repair': () => {
      activeState = 'active';
      activeRevision += 1;
      return previewWorkDocumentCommand('repair', activeDocument());
    },
    'workDocuments.reopen': (request: ControlRequest) => {
      activeState = 'active';
      activeRevision += 1;
      activeTerminalReceiptId = stringValue(record(request.body).transitionReceiptId);
      return previewWorkDocumentCommand('reopen', activeDocument());
    },
    'workDocuments.erase.preview': () => ({
      schemaVersion: 'rag-ime.work-document-command.v1',
      ok: true,
      operation: 'erase-preview',
      document: activeDocument(),
      approval: {
        approvalId: 'approval-preview-work-document',
        status: 'approved',
      },
      payloadSha256: ERASE_PAYLOAD_SHA256,
    }),
    'workDocuments.erase': () => previewWorkDocumentCommand('erase', null),
  };
}

function previewWorkDocument(
  documentId: string,
  state: DocumentState,
  revision: number,
  terminalReceiptId: string,
): Record<string, unknown> {
  const archived = state === 'archived';
  const historical = documentId === ARCHIVED_DOCUMENT_ID;
  return {
    documentId,
    authorityKind: historical ? 'session_goal' : 'session_todo',
    authorityId: historical ? 'goal-preview' : 'session-preview',
    authorityRevision: 7,
    authorityKey: historical ? 'goal:preview' : 'todo:preview',
    documentRevision: revision,
    contentSha256: historical ? '5'.repeat(64) : '4'.repeat(64),
    workspaceRoot: '/Users/preview/PersonalAgentWorkbench',
    path: archived ? 'archive/session-preview/todo.md' : 'docs/agent/todo.md',
    activePath: 'docs/agent/todo.md',
    archivePath: 'archive/session-preview/todo.md',
    state,
    title: historical ? '输入法前台验收目标' : '控制中心发布任务',
    terminalReceiptId,
    error: '',
    createdAtMs: Date.now() - 172_800_000,
    updatedAtMs: Date.now() - 3_600_000,
  };
}

function previewWorkDocumentCommand(
  operation: 'register' | 'archive' | 'repair' | 'reopen' | 'erase',
  document: Record<string, unknown> | null,
): Record<string, unknown> {
  const receiptIds = {
    register: '0'.repeat(32),
    archive: '1'.repeat(32),
    repair: '2'.repeat(32),
    reopen: '3'.repeat(32),
    erase: '4'.repeat(32),
  };
  return {
    schemaVersion: 'rag-ime.work-document-command.v1',
    ok: true,
    operation,
    document,
    receipt: {
      receiptId: `workdoc-receipt:${receiptIds[operation]}`,
      operation,
      status: 'applied',
      idempotent: false,
      createdAtMs: Date.now(),
    },
  };
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : '';
}
