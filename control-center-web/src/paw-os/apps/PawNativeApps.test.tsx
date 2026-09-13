import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import { MotionProvider } from '@/design/motion';
import { PawOsAppearanceProvider } from '@/design/paw-os-themes';
import { ThemeProvider } from '@/design/themes';
import { PawOsAppSurfaceProvider, PawOsDesktopProvider, type PawOsWindowRequest } from '@/features/paw-os/surface-context';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport } from '@/test/mock-transport';
import { PawNativeApp } from './PawNativeApps';

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  delete document.documentElement.dataset.theme;
  document.documentElement.style.removeProperty('color-scheme');
});

const NATIVE_DOCUMENT_ID = `workdoc_${'a'.repeat(32)}`;

describe('PAWOS native Apps', () => {
  it('leaves App identity to the host window and keeps only page navigation in the native rail', async () => {
    const view = renderNative('project-workbench', nativeTransport());

    expect(await screen.findByRole('navigation', { name: '项目工作台页面' })).toBeInTheDocument();
    expect(view.container.querySelector('.paw-native-nav > header')).toBeNull();
  });

  it.each([
    ['project-workbench', '/overview', '概览'],
    ['project-workbench', '/planning', '任务'],
    ['project-workbench', '/work-documents', '工作文档'],
    ['memory', '/memory?view=preferences', '记忆偏好'],
    ['input-studio', '/history', '输入记录'],
    ['app-center', '/plugins?view=catalog', '目录'],
    ['system-monitor', '/context-debug', '上下文'],
    ['system-settings', '/approvals', '审批'],
  ] as const)('keeps the %s %s navigation target named when narrow labels are hidden', async (appId, route, label) => {
    renderNative(appId, nativeTransport(), { initialRoute: route, width: 420 });

    const navigation = screen.getByRole('navigation');
    const target = within(navigation).getByRole('button', { name: label });
    expect(target).toHaveAttribute('aria-label', label);
    expect(target).toHaveAttribute('aria-current', 'page');
  });

  it.each(['project-workbench', 'memory'] as const)('keeps %s page navigation collapsible without remounting the page', async (appId) => {
    const user = userEvent.setup();
    const view = renderNative(appId, nativeTransport());
    const toggle = screen.getByRole('button', { name: /(?:展开|收起).*导航$/ });
    if (toggle.getAttribute('aria-expanded') === 'false') await user.click(toggle);
    const page = view.container.querySelector('.paw-native-page');
    const navigation = screen.getByRole('navigation');
    expect(toggle).toHaveAttribute('aria-controls', navigation.id);
    await user.click(toggle);
    expect(navigation).not.toBeVisible();
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(view.container.querySelector('.paw-native-page')).toBe(page);
    await user.click(toggle);
    expect(navigation).toBeVisible();
  });

  it('lets Lab own its navigation without a redundant one-page rail', async () => {
    const view = renderNative('eval-lab', nativeTransport());
    expect(view.container.querySelector('.paw-native-nav')).toBeNull();
    expect(view.container.querySelector('[data-owns-navigation]')).toHaveAttribute('data-owns-navigation', 'true');
    expect(view.container.querySelector('.paw-native-page')).not.toBeNull();
  });

  it('recomposes Project routes as one OS workspace instead of rendering legacy pages', async () => {
    const transport = nativeTransport();
    const user = userEvent.setup();
    renderNative('project-workbench', transport);

    expect(await screen.findByRole('heading', { level: 1, name: '项目概览' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '任务' }));
    expect(await screen.findByRole('heading', { level: 1, name: '任务' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '工作文档' }));
    expect(await screen.findByText('PAWOS 交互重建')).toBeInTheDocument();
    expect(transport.requests.map(({ request }) => request.pathId)).toEqual(
      expect.arrayContaining(['overview.get', 'planning.dashboard', 'workDocuments.list']),
    );
  });

  it.each([
    ['/overview', 'overview'],
    ['/planning', 'planning'],
    ['/work-documents', 'documents'],
  ] as const)('keeps the Project %s route on the migrated Workbench owner', async (route, pageId) => {
    const view = renderNative('project-workbench', nativeTransport(), { initialRoute: route });

    expect(await screen.findByRole('heading', { level: 1 })).toBeInTheDocument();
    expect(view.container.querySelector(`.paw-workbench-migrated[data-page-id="${pageId}"]`)).not.toBeNull();
    expect(view.container.querySelector('.mgmt-page')).toBeNull();
  });

  it('takes the Project overview into the production Planning preview/apply/rollback path', async () => {
    const transport = nativeTransport();
    const user = userEvent.setup();
    renderNative('project-workbench', transport);

    await user.click(await screen.findByRole('button', { name: '查看任务' }));
    expect(await screen.findByRole('heading', { level: 1, name: '任务' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '添加任务' }));
    await user.type(screen.getByPlaceholderText('例如：整理今天的工作清单'), '闭合 Project 写入');
    await user.click(screen.getByRole('button', { name: '创建任务' }));

    expect(await screen.findByText('已保存')).toBeInTheDocument();
    expect(transport.requests.map(({ request }) => request.pathId)).toEqual(expect.arrayContaining([
      'planning.mutation.preview',
      'planning.task.save',
    ]));

    await user.click(screen.getByRole('button', { name: '撤销' }));
    expect(await screen.findByText('已恢复到更改前')).toBeInTheDocument();
    expect(transport.requests.map(({ request }) => request.pathId)).toContain('planning.mutation.rollback');
  });

  it('keeps a rejected Project Planning apply visible without inventing success', async () => {
    const transport = nativeTransport({ failTaskSave: true });
    const user = userEvent.setup();
    renderNative('project-workbench', transport, { initialRoute: '/planning' });

    await user.click(await screen.findByRole('button', { name: '添加任务' }));
    await user.type(screen.getByPlaceholderText('例如：整理今天的工作清单'), '触发 Project 版本冲突');
    await user.click(screen.getByRole('button', { name: '创建任务' }));

    expect(await screen.findByText('页面内容已经变化，请重新预览。')).toBeInTheDocument();
    expect(screen.queryByText('已保存')).not.toBeInTheDocument();
    expect(transport.requests.map(({ request }) => request.pathId)).not.toContain('planning.mutation.rollback');
  });

  it('opens explicit editors for the selected task and live goal without treating selection as a window launch', async () => {
    const transport = nativeTransport();
    const openWindow = vi.fn();
    const user = userEvent.setup();
    renderNative('project-workbench', transport, { initialRoute: '/planning', openWindow });

    await user.click(await screen.findByRole('button', { name: '编辑任务' }));
    expect(await screen.findByRole('heading', { name: '编辑任务' })).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '任务标题' })).toHaveValue('统一 Agent 入口');
    expect(openWindow).not.toHaveBeenCalled();
    await user.keyboard('{Escape}');

    await user.click(screen.getByRole('button', { name: '编辑目标：完成 PAWOS 前端迁移' }));
    expect(await screen.findByRole('heading', { name: '编辑目标' })).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '目标标题' })).toHaveValue('完成 PAWOS 前端迁移');
  });

  it('keeps day navigation, Agent handoff, and real wake schedules in the migrated Planning surface', async () => {
    const transport = nativeTransport();
    const openApp = vi.fn();
    const user = userEvent.setup();
    renderNative('project-workbench', transport, { initialRoute: '/planning', openApp });

    const date = await screen.findByLabelText('规划日期');
    expect(date).toHaveAttribute('type', 'date');
    await user.click(screen.getByRole('button', { name: '交给 Agent 安排' }));
    expect(openApp).toHaveBeenCalledWith('agent', expect.stringMatching(/^\/agent\?draft=/));
    const handoffRoute = openApp.mock.calls[0]?.[1] ?? '';
    const handoffDraft = new URLSearchParams(handoffRoute.split('?', 2)[1] ?? '').get('draft') ?? '';
    expect(handoffDraft).toContain('项目：personal-agent-workbench');
    expect(handoffDraft).toContain('工作区：/work/paw');
    expect(handoffDraft).toContain('目标：完成 PAWOS 前端迁移（goal-1）');
    expect(handoffDraft).toContain('任务：统一 Agent 入口（task-1）');

    await user.click(screen.getByRole('button', { name: '定时安排' }));
    expect(openApp).toHaveBeenLastCalledWith('schedules', '/schedules?view=agent');
  });

  it('registers a WorkDocument through the disclosed production route and refreshes the list', async () => {
    const transport = nativeTransport();
    const user = userEvent.setup();
    renderNative('project-workbench', transport, { initialRoute: `/work-documents?document=${NATIVE_DOCUMENT_ID}` });

    await user.click(await screen.findByRole('button', { name: '登记工作文档' }));
    expect(screen.getByRole('button', { name: '确认登记' })).toBeDisabled();
    await user.type(screen.getByRole('textbox', { name: '来源编号' }), 'session-1');
    await user.type(screen.getByRole('spinbutton', { name: '来源版本' }), '3');
    await user.type(screen.getByRole('textbox', { name: '工作区根目录' }), '/work/paw');
    await user.type(screen.getByRole('textbox', { name: 'Markdown 来源路径' }), 'docs/active/project.md');
    await user.type(screen.getByRole('textbox', { name: '标题（可选）' }), 'Project 闭环');
    await user.click(screen.getByRole('button', { name: '确认登记' }));

    expect(await screen.findByText('登记完成')).toBeInTheDocument();
    const registration = transport.requests.find(({ request }) => request.pathId === 'workDocuments.register')?.request;
    expect(registration?.body).toEqual({
      authorityKind: 'session_todo',
      authorityId: 'session-1',
      authorityRevision: 3,
      workspaceRoot: '/work/paw',
      sourcePath: 'docs/active/project.md',
      title: 'Project 闭环',
    });
    await waitFor(() => expect(
      transport.requests.filter(({ request }) => request.pathId === 'workDocuments.list'),
    ).toHaveLength(2));
  });

  it('searches Runtime history and archives the selected WorkDocument from the same reader', async () => {
    const transport = nativeTransport();
    const user = userEvent.setup();
    renderNative('project-workbench', transport, { initialRoute: `/work-documents?document=${NATIVE_DOCUMENT_ID}` });

    expect(await screen.findByRole('region', { name: '工作文档生命周期' })).toBeInTheDocument();
    await user.type(screen.getByRole('textbox', { name: '完成依据' }), 'terminal-project-1');
    await user.click(screen.getByRole('button', { name: '归档到历史' }));
    await waitFor(() => expect(transport.requests.map(({ request }) => request.pathId)).toContain('workDocuments.archive'));

    await user.click(screen.getByRole('button', { name: '返回文档列表' }));
    await user.click(screen.getByRole('button', { name: '历史' }));
    expect(await screen.findByRole('searchbox', { name: '筛选历史' })).toBeInTheDocument();
    await waitFor(() => expect(transport.requests.map(({ request }) => request.pathId)).toContain('workDocuments.history.search'));
  });

  it('routes an overview WorkDocument into the reader and can open its independent window', async () => {
    const transport = nativeTransport();
    const openWindow = vi.fn();
    const user = userEvent.setup();
    renderNative('project-workbench', transport, { openWindow });

    await user.click(await screen.findByRole(
      'button',
      { name: /PAWOS 交互重建/ },
      { timeout: 5_000 },
    ));

    expect(await screen.findByRole('heading', { level: 1, name: '工作文档' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '工作文档' })).toHaveAttribute('aria-current', 'page');
    expect(await screen.findByRole('heading', { level: 2, name: 'PAWOS 交互重建' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '在独立窗口打开文档' }));
    expect(openWindow).toHaveBeenCalledWith(expect.objectContaining({
      appId: 'project-workbench',
      target: expect.objectContaining({ id: NATIVE_DOCUMENT_ID, kind: 'work-document' }),
    }));
  });

  it('presents Knowledge as a library with real knowledge-base objects', async () => {
    const transport = nativeTransport();
    const user = userEvent.setup();
    renderNative('knowledge', transport);

    expect(await screen.findByRole('heading', { level: 1, name: '知识库' })).toBeInTheDocument();

    // A window this wide reads its selection from the rail, so the band states
    // the library once and the workspace starts right below it — no second
    // header sheet repeating the same name and counts.
    const band = await screen.findByRole('region', { name: '切换文档知识库' });
    expect(within(band).getByText('产品资料库')).toBeInTheDocument();
    expect(within(band).getByLabelText('12 个文件，0 个段落')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { level: 2, name: '产品资料库' })).toBeNull();

    // The rail is the wide-window selector; its rows are virtualised, so only
    // its index header is measurable here.
    const rail = await screen.findByRole('complementary', { name: '文档知识库' });
    expect(document.querySelector('.paw-native-nav')).toBeNull();
    expect(within(rail).getByText('1 个独立库')).toBeInTheDocument();
    expect(within(rail).queryByRole('button', { name: '刷新知识库' })).toBeNull();
    expect(transport.requests.map(({ request }) => request.pathId)).toContain('knowledgeBases.get');
    await user.click(screen.getByRole('button', { name: '收起知识库目录' }));
    expect(rail).not.toBeVisible();
    await user.click(screen.getByRole('button', { name: '展开知识库目录' }));
    expect(rail).toBeVisible();
  });

  it('keeps Appearance inside a native System Settings split view', async () => {
    const transport = nativeTransport();
    renderNative('system-settings', transport);

    expect(await screen.findByRole('heading', { name: '外观' })).toBeInTheDocument();
    const themes = screen.getByRole('radiogroup', { name: 'PAWOS 主题' });
    expect(within(themes).getByDisplayValue('system')).toBeChecked();
    expect(screen.getByRole('navigation', { name: 'System Settings页面' })).toBeInTheDocument();
    await waitFor(() => expect(transport.requests.map(({ request }) => request.pathId)).toEqual(['agent.approvals.list']));
  });

  it.each([
    ['memory', '我的记忆', [
      'memory.summary',
      'memory.pages',
    ]],
    ['input-studio', '输入法', ['input.source.get']],
    ['app-center', '插件管理', ['agent.extensions.list', 'agent.extensions.catalog', 'agent.extensions.proposals']],
    ['system-monitor', '运行记录', ['observability.snapshot']],
  ] as const)('opens %s on its native transport-backed surface', async (appId, heading, pathIds) => {
    const transport = nativeTransport();
    renderNative(appId, transport);

    expect(await screen.findByRole('heading', { name: heading })).toBeInTheDocument();
    await waitFor(() => expect(transport.requests.map(({ request }) => request.pathId)).toEqual(
      expect.arrayContaining([...pathIds]),
    ));
  });

  it('preserves the explicit Memory timeline entry after making the catalog the App home', async () => {
    const transport = nativeTransport();
    renderNative('memory', transport, { initialRoute: '/memory?view=timeline' });
    await waitFor(() => expect(transport.requests.map(({ request }) => request.pathId)).toEqual(
      expect.arrayContaining(['memory.activityTimeline.get', 'memory.activityTimeline.calendar']),
    ));
  });
});

function renderNative(
  appId: Parameters<typeof PawNativeApp>[0]['appId'],
  transport = nativeTransport(),
  options: {
    initialRoute?: string;
    openApp?: (appId: PawOsWindowRequest['appId'], initialRoute?: string) => void;
    openWindow?: (request: PawOsWindowRequest) => void;
    width?: number;
  } = {},
) {
  return render(<NativeHarness appId={appId} initialRoute={options.initialRoute} openApp={options.openApp} openWindow={options.openWindow} transport={transport} width={options.width} />);
}

function NativeHarness({
  appId,
  initialRoute = '',
  openApp,
  openWindow = () => undefined,
  transport,
  width = 1_080,
}: {
  appId: Parameters<typeof PawNativeApp>[0]['appId'];
  initialRoute?: string;
  openApp?: (appId: PawOsWindowRequest['appId'], initialRoute?: string) => void;
  openWindow?: (request: PawOsWindowRequest) => void;
  transport: MockControlTransport;
  width?: number;
}) {
  const [route, setRoute] = useState(initialRoute);
  const [client] = useState(() => new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  }));
  return (
    <TooltipProvider delayDuration={0}>
      <QueryClientProvider client={client}>
        <ControlTransportProvider transport={transport}>
          <ThemeProvider>
            <PawOsAppearanceProvider>
              <MotionProvider>
                <PawOsDesktopProvider openApp={openApp} openRoute={setRoute} openWindow={openWindow}>
                  <PawOsAppSurfaceProvider appId={appId} height={720} width={width}>
                    <PawNativeApp appId={appId} initialRoute={route} />
                  </PawOsAppSurfaceProvider>
                </PawOsDesktopProvider>
              </MotionProvider>
            </PawOsAppearanceProvider>
          </ThemeProvider>
        </ControlTransportProvider>
      </QueryClientProvider>
    </TooltipProvider>
  );
}

function nativeTransport(options: { failTaskSave?: boolean } = {}) {
  const workDocument = {
    documentId: NATIVE_DOCUMENT_ID,
    authorityKind: 'session_todo' as const,
    authorityId: 'session-1',
    authorityRevision: 3,
    authorityKey: 'session_todo:session-1',
    documentRevision: 2,
    contentSha256: 'b'.repeat(64),
    workspaceRoot: '/work/paw',
    path: '/work/paw/docs/agent/work/active/session_todo/project.md',
    activePath: '/work/paw/docs/agent/work/active/session_todo/project.md',
    archivePath: '/work/paw/docs/agent/work/archive/session_todo/project.md',
    state: 'active' as const,
    title: 'PAWOS 交互重建',
    terminalReceiptId: '',
    error: '',
    createdAtMs: 2,
    updatedAtMs: 3,
  };
  return new MockControlTransport({
    capabilities: {
      features: {
        managementWorkContract: true,
        planningWorkContract: true,
        workDocuments: true,
      },
    },
    routes: {
    'overview.get': { ok: true, project: { name: 'personal-agent-workbench', path: '/work/paw' }, metrics: { activeSessions: 3, openTasks: 4, documents: 7 } },
    'planning.dashboard': {
      ok: true,
      runtimeRevision: 7,
      plan: { project: 'PAWOS' },
      goals: [{ id: 'goal-1', title: '完成 PAWOS 前端迁移', detail: '逐 App 收束', status: 'active', priority: 2 }],
      tasks: [{ id: 'task-1', title: '统一 Agent 入口', status: 'active', project: 'PAWOS' }],
    },
    'planning.mutation.preview': (request: ControlRequest) => {
      const body = request.body as Record<string, unknown>;
      const kind = String(body.kind ?? '');
      const pathId = kind === 'goal.save'
        ? 'planning.goal.save'
        : kind === 'task.action'
          ? 'planning.task.action'
          : 'planning.task.save';
      const suffix = kind === 'goal.save' ? 'goal' : kind === 'task.action' ? 'action' : 'task';
      return {
        schemaVersion: 'rag-ime.management-work-preview.v1',
        ok: true,
        pathId,
        previewToken: `preview-project-${suffix}`,
        payloadSha256: `sha256:${suffix}`,
        expectedRevision: { runtimeRevision: 7 },
        expiresAtMs: Date.now() + 60_000,
        requiredConfirm: 'apply',
        summary: { title: '保存规划变更', items: ['写入当前规划'], risk: 'R1' },
      };
    },
    'planning.task.save': options.failTaskSave ? {
      ok: false,
      error: '运行版本已变化，请重新预览。',
      code: 'stale_revision',
    } : {
      ok: true,
      pathId: 'planning.task.save',
      receiptId: 'receipt-project-task',
      payloadSha256: 'sha256:task',
      appliedAtMs: 3,
      auditId: 7,
      rollbackAvailable: true,
      rollbackToken: 'rollback-project-task',
      restartComponents: [],
    },
    'planning.goal.save': (request: ControlRequest) => planningReceipt('planning.goal.save', request, 'receipt-project-goal'),
    'planning.task.action': (request: ControlRequest) => planningReceipt('planning.task.action', request, 'receipt-project-action', { eventId: 'task-event:project-1' }),
    'planning.taskEvent.undo': (request: ControlRequest) => planningReceipt('planning.taskEvent.undo', request, 'receipt-project-undo', { rollbackAvailable: false, rollbackToken: '' }),
    'planning.mutation.rollback': (request: ControlRequest) => ({
      ok: true,
      pathId: 'planning.mutation.rollback',
      receiptId: 'receipt-project-rollback',
      payloadSha256: String((request.body as Record<string, unknown>).payloadSha256 ?? ''),
      appliedAtMs: 4,
      auditId: 8,
      rollbackAvailable: false,
      rollbackToken: '',
      restartComponents: [],
    }),
    'workDocuments.list': {
      schemaVersion: 'rag-ime.work-document-list.v1',
      items: [workDocument],
      total: 1,
    },
    'workDocuments.get': {
      schemaVersion: 'rag-ime.work-document-detail.v1',
      document: workDocument,
      reopen: { eligible: false, authorityRevision: 3, transitionReceiptId: '', reasonCode: 'document_not_archived' },
    },
    'workDocuments.history.search': {
      schemaVersion: 'rag-ime.work-document-list.v1',
      items: [{ ...workDocument, state: 'archived', title: 'PAWOS 交互重建 · 历史' }],
      total: 1,
    },
    'workDocuments.archive': {
      schemaVersion: 'rag-ime.work-document-command.v1',
      ok: true,
      operation: 'archive',
      document: { ...workDocument, state: 'archive_pending', terminalReceiptId: 'terminal-project-1' },
      receipt: {
        receiptId: `workdoc-receipt:${'e'.repeat(32)}`,
        operation: 'archive',
        status: 'applied',
        idempotent: false,
        createdAtMs: 5,
      },
    },
    'workDocuments.register': {
      schemaVersion: 'rag-ime.work-document-command.v1',
      ok: true,
      operation: 'register',
      document: { ...workDocument, title: 'Project 闭环', documentRevision: 3 },
      receipt: {
        receiptId: `workdoc-receipt:${'d'.repeat(32)}`,
        operation: 'register',
        status: 'applied',
        idempotent: false,
        createdAtMs: 4,
      },
    },
    'agent.sessions.list': { ok: true, items: [{ id: 'session-1', title: 'Project 协作', status: 'active', updatedAtMs: 3 }] },
    'agent.roles.list': { ok: true, items: [] },
    'agent.wakeSchedules.list': { ok: true, items: [] },
    'memory.summary': { ok: true, total: 9, pending: 1 },
    'memory.pages': { ok: true, items: [] },
    'knowledgeBases.list': { ok: true, items: [{ id: 'kb-1', name: '产品资料库', documentCount: 12, status: 'ready', updatedAtMs: 2 }] },
    'knowledgeBases.get': { ok: true, base: { id: 'kb-1', name: '产品资料库', documentCount: 12, status: 'ready', updatedAtMs: 2, description: 'PAWOS 产品资料' } },
    'knowledge.status': { ok: true, ready: true },
    'input.source.get': { ok: true, source: 'squirrel', enabled: true },
    'input.lexicon.review': { ok: true, entries: [] },
    'history.page': { ok: true, items: [] },
    'agent.extensions.list': { ok: true, items: [] },
    'agent.extensions.catalog': { ok: true, items: [] },
    'agent.extensions.proposals': { ok: true, items: [] },
    'observability.snapshot': { ok: true, healthy: true, sessions: 3 },
    'observability.events': { ok: true, items: [] },
    'diagnostics.runtime': { ok: true, status: 'healthy' },
    'configuration.settings': { ok: true, revision: 3 },
    'agent.governance.read': { ok: true, mode: 'governed' },
    'agent.approvals.list': { ok: true, items: [] },
  } });
}

function planningReceipt(
  pathId: string,
  request: ControlRequest,
  receiptId: string,
  extra: Record<string, unknown> = {},
) {
  const body = request.body as Record<string, unknown>;
  return {
    ok: true,
    pathId,
    receiptId,
    payloadSha256: String(body.payloadSha256 ?? ''),
    appliedAtMs: 4,
    auditId: 8,
    rollbackAvailable: true,
    rollbackToken: `rollback-${receiptId}`,
    restartComponents: [],
    ...extra,
  };
}
