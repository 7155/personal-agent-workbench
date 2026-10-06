import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { StrictMode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type {
  WorkDocumentCommandV1,
  WorkDocumentDetailV1,
  WorkDocumentErasePreviewV1,
  WorkDocumentV1,
} from '@/contracts/work-documents';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport } from '@/test/mock-transport';
import {
  PawWorkbenchDocumentLifecycle,
  type PawWorkbenchDocumentLifecycleProps,
} from './PawWorkbenchDocumentLifecycle';

afterEach(cleanup);

const DOCUMENT_ID = `workdoc_${'a'.repeat(32)}`;
const AUTHORITY_ID = 'session-authority-1';

describe('PawWorkbenchDocumentLifecycle', () => {
  it('accepts an approval-session read after effects reconnect', async () => {
    const transport = lifecycleTransport({ 'agent.sessions.list': { ok: true, items: [{ id: 'approval-session', title: '审批对话', status: 'active', updatedAtMs: 2 }] } });
    render(<StrictMode><PawWorkbenchDocumentLifecycle {...baseProps({ current: workDocument(), transport })} /></StrictMode>);
    await userEvent.click(screen.getByRole('button', { name: '永久清除…' }));
    expect(await screen.findByRole('combobox', { name: '接收审批的对话' })).toBeEnabled();
    expect(screen.getByRole('button', { name: '永久清除，不是归档' })).toBeDisabled();
    expect(transport.requests.every(({ request }) => request.pathId === 'agent.sessions.list')).toBe(true);
  });
  it('retries the approval-session read in place without preparing or applying erase', async () => {
    let attempts = 0;
    const transport = lifecycleTransport({ 'agent.sessions.list': () => {
      attempts += 1;
      if (attempts === 1) throw new Error('sessions unavailable');
      return { ok: true, items: [{ id: 'approval-session', title: '审批对话', status: 'active', updatedAtMs: 2 }] };
    } });
    renderLifecycle({ current: workDocument({ state: 'archived' }), transport });
    await userEvent.click(screen.getByRole('button', { name: '永久清除…' }));
    await userEvent.click(await screen.findByRole('button', { name: '重新读取可用对话' }));
    expect(await screen.findByText('审批对话')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '永久清除，不是归档' })).toBeDisabled();
    expect(transport.requests.every(({ request }) => request.pathId === 'agent.sessions.list')).toBe(true);
  });

  it('requires a terminal receipt before archiving and reports the typed receipt', async () => {
    const user = userEvent.setup();
    const changed = vi.fn();
    const document = workDocument({ state: 'active', terminalReceiptId: '' });
    const transport = lifecycleTransport({
      'workDocuments.archive': (request: ControlRequest) => commandResponse('archive', {
        ...document,
        state: 'archive_pending',
        terminalReceiptId: String(
          request.body && typeof request.body === 'object' && !Array.isArray(request.body) && 'terminalReceiptId' in request.body
            ? request.body.terminalReceiptId
            : '',
        ),
      }),
    });

    renderLifecycle({ current: document, onChanged: changed, transport });

    const archive = screen.getByRole('button', { name: '归档到历史' });
    expect(archive).toBeDisabled();
    await user.type(screen.getByRole('textbox', { name: '完成依据' }), 'terminal-receipt-1');
    expect(archive).toBeEnabled();
    await user.click(archive);

    expect(await screen.findByText('操作结果 · 已应用')).toBeInTheDocument();
    expect(changed).toHaveBeenCalledWith(expect.objectContaining({ operation: 'archive' }));
    expect(transport.requests.find(({ request }) => request.pathId === 'workDocuments.archive')?.request).toEqual(
      expect.objectContaining({
        params: { documentId: DOCUMENT_ID },
        body: { terminalReceiptId: 'terminal-receipt-1' },
      }),
    );
  });

  it('only offers repair for repairable states and keeps unsupported repair truthful', async () => {
    const user = userEvent.setup();
    const active = renderLifecycle({ current: workDocument({ state: 'active' }) });
    expect(screen.queryByRole('button', { name: '重新检查状态' })).not.toBeInTheDocument();
    active.unmount();

    const repair = vi.fn(() => commandResponse('repair', workDocument({ state: 'active' })));
    const transport = lifecycleTransport({ 'workDocuments.repair': repair });
    renderLifecycle({
      access: { repair: false },
      current: workDocument({ state: 'error', error: 'archive index failed' }),
      transport,
    });

    const repairButton = screen.getByRole('button', { name: '当前宿主不支持重新检查' });
    expect(repairButton).toBeDisabled();
    expect(screen.getByText(/当前 Runtime 未公开修复路由/)).toBeInTheDocument();
    expect(repair).not.toHaveBeenCalled();

    active.unmount();
    renderLifecycle({ current: workDocument({ state: 'reopen_pending' }), transport });
    await user.click(screen.getByRole('button', { name: '重新检查状态' }));
    expect(repair).toHaveBeenCalledTimes(1);
  });

  it('gates reopen on the backend-projected context and sends its revision receipt', async () => {
    const user = userEvent.setup();
    const reopen = vi.fn(() => commandResponse('reopen', workDocument({ state: 'reopen_pending' })));
    const transport = lifecycleTransport({ 'workDocuments.reopen': reopen });
    const archived = workDocument({ state: 'archived' });

    const view = renderLifecycle({
      current: detail(archived, reopenContext({ eligible: false, reasonCode: 'authority_terminal' })),
      transport,
    });

    const button = screen.getByRole('button', { name: '重新打开到活跃区' });
    expect(button).toBeDisabled();
    expect(screen.getByText(/来源仍处于已完成或已取消状态/)).toBeInTheDocument();
    expect(reopen).not.toHaveBeenCalled();

    const eligible = reopenContext({ eligible: true, reasonCode: 'ready', authorityRevision: 11, transitionReceiptId: 'transition-11' });
    view.rerender(<PawWorkbenchDocumentLifecycle {...baseProps({ current: detail(archived, eligible), transport })} />);
    await user.click(screen.getByRole('button', { name: '重新打开到活跃区' }));

    expect(reopen).toHaveBeenCalledWith(expect.objectContaining({
      params: { documentId: DOCUMENT_ID },
      body: { authorityRevision: 11, transitionReceiptId: 'transition-11' },
    }));
  });

  it('loads eligible Agent Sessions, previews erase, and requires the exact confirmation before erase', async () => {
    const user = userEvent.setup();
    const erased = vi.fn();
    const document = workDocument({ state: 'archived', title: '需要清除的记录' });
    const preview = erasePreview(document);
    const erase = vi.fn(() => commandResponse('erase', null));
    const transport = lifecycleTransport({
      'agent.sessions.list': {
        ok: true,
        items: [
          { id: 'session-approval', title: '审批对话', updatedAtMs: 2, status: 'active' },
          { id: 'session-archived', title: '旧对话', updatedAtMs: 1, status: 'archived' },
        ],
      },
      'workDocuments.erase.preview': preview,
      'workDocuments.erase': erase,
    });

    renderLifecycle({
      access: { erase: true },
      current: detail(document, reopenContext({ eligible: false })),
      onErased: erased,
      transport,
    });

    await user.click(screen.getByRole('button', { name: '永久清除…' }));
    expect(await screen.findByText('审批对话')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '准备永久清除' }));
    expect(await screen.findByText('清除审批已就绪')).toBeInTheDocument();

    const finalButton = screen.getByRole('button', { name: '永久清除，不是归档' });
    expect(finalButton).toBeDisabled();
    await user.type(screen.getByRole('textbox', { name: '永久清除确认' }), '永久清除');
    expect(finalButton).toBeEnabled();
    await user.click(finalButton);

    expect(erased).toHaveBeenCalledWith(expect.objectContaining({ operation: 'erase' }));
    expect(erase).toHaveBeenCalledWith(expect.objectContaining({
      params: { documentId: DOCUMENT_ID },
      body: {
        sessionId: 'session-approval',
        approvalId: 'approval-1',
        payloadSha256: 'f'.repeat(64),
      },
    }));
  });

  it('fences a late command receipt when the same document advances revision', async () => {
    const user = userEvent.setup();
    let resolveArchive: ((value: WorkDocumentCommandV1) => void) | undefined;
    const archive = vi.fn(() => new Promise<WorkDocumentCommandV1>((resolve) => {
      resolveArchive = resolve;
    }));
    const transport = lifecycleTransport({ 'workDocuments.archive': archive });
    const first = workDocument({ state: 'active', documentRevision: 3 });
    const second = { ...first, documentRevision: 4 };
    const changed = vi.fn();
    const view = renderLifecycle({ current: first, onChanged: changed, transport });

    await user.type(screen.getByRole('textbox', { name: '完成依据' }), 'terminal-receipt-1');
    await user.click(screen.getByRole('button', { name: '归档到历史' }));
    view.rerender(<PawWorkbenchDocumentLifecycle {...baseProps({ current: second, onChanged: changed, transport })} />);
    resolveArchive?.(commandResponse('archive', { ...first, state: 'archive_pending' }));

    await waitFor(() => expect(archive).toHaveBeenCalledTimes(1));
    expect(screen.queryByText('操作结果 · 已应用')).not.toBeInTheDocument();
    expect(changed).not.toHaveBeenCalled();
  });
});

type LifecycleOverrides = Omit<Partial<PawWorkbenchDocumentLifecycleProps>, 'access'> & {
  access?: Partial<PawWorkbenchDocumentLifecycleProps['access']>;
};

function renderLifecycle(overrides: LifecycleOverrides = {}) {
  return render(<PawWorkbenchDocumentLifecycle {...baseProps(overrides)} />);
}

function baseProps(overrides: LifecycleOverrides = {}): PawWorkbenchDocumentLifecycleProps {
  return {
    current: workDocument({ state: 'active' }),
    onChanged: undefined,
    onErased: undefined,
    reopen: undefined,
    transport: lifecycleTransport(),
    ...overrides,
    access: {
      read: true,
      history: true,
      register: true,
      archive: true,
      repair: true,
      reopen: true,
      erase: true,
      missingReadRoutes: [],
      ...overrides.access,
    },
  };
}

function detail(document: WorkDocumentV1, reopen: WorkDocumentDetailV1['reopen']): WorkDocumentDetailV1 {
  return {
    schemaVersion: 'rag-ime.work-document-detail.v1',
    document,
    reopen,
  };
}

function lifecycleTransport(routes: Record<string, unknown> = {}) {
  return new MockControlTransport({ routes: routes as never });
}

function commandResponse(operation: WorkDocumentCommandV1['operation'], document: WorkDocumentV1 | null): WorkDocumentCommandV1 {
  return {
    schemaVersion: 'rag-ime.work-document-command.v1',
    ok: true,
    operation,
    document,
    receipt: {
      receiptId: `workdoc-receipt:${'a'.repeat(32)}`,
      operation: operation === 'erase-preview' ? 'erase' : operation,
      status: 'applied',
      idempotent: false,
      createdAtMs: 3,
    },
  };
}

function erasePreview(document: WorkDocumentV1) {
  return {
    schemaVersion: 'rag-ime.work-document-command.v1',
    ok: true,
    operation: 'erase-preview',
    document,
    approval: { approvalId: 'approval-1' },
    payloadSha256: 'f'.repeat(64),
  } satisfies WorkDocumentErasePreviewV1;
}

function workDocument(overrides: Partial<WorkDocumentV1> = {}): WorkDocumentV1 {
  return {
    documentId: DOCUMENT_ID,
    authorityKind: 'session_todo',
    authorityId: AUTHORITY_ID,
    authorityRevision: 7,
    authorityKey: `session_todo:${AUTHORITY_ID}`,
    documentRevision: 3,
    contentSha256: 'c'.repeat(64),
    workspaceRoot: '/work/paw',
    path: 'docs/active/work.md',
    activePath: 'docs/active/work.md',
    archivePath: 'docs/archive/work.md',
    state: 'active',
    title: 'Lifecycle record',
    terminalReceiptId: 'terminal-receipt-existing',
    error: '',
    createdAtMs: 1,
    updatedAtMs: 2,
    ...overrides,
  };
}

function reopenContext(overrides: Partial<WorkDocumentDetailV1['reopen']> = {}): WorkDocumentDetailV1['reopen'] {
  return {
    eligible: false,
    authorityRevision: 7,
    transitionReceiptId: 'transition-7',
    reasonCode: 'document_not_archived',
    ...overrides,
  };
}
