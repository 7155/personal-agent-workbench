import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useState } from 'react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { PawOsAppSurfaceProvider } from '@/features/paw-os/surface-context';
import { ControlTransportProvider } from '@/app/control-transport';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport } from '@/test/mock-transport';
import { EvalLabFeature } from '../index';
import { LabProjectWorkbench } from './LabProjectWorkbench';
import { messageWithWorkspaceContext, type WorkspaceComposerContext } from '@/paw-os/apps/workspace-draft';
import { ArtifactSurface } from './ArtifactSurface';
import type { LabArtifact, LabProject, ProjectCommand } from './types';

vi.mock('./ProjectGuide', async () => {
  const actual = await vi.importActual<typeof import('./ProjectGuide')>('./ProjectGuide');
  return { ...actual, ProjectGuide: ({ project, draftRequest, viewContext, onProjectActivity }: { project: LabProject; draftRequest?: { text: string }; viewContext?: Omit<WorkspaceComposerContext, 'onClear'>; onProjectActivity: () => void }) => <section aria-label="项目 Agent"><p>{project.guideSessionId || '项目对话'}</p><output aria-label="Agent 将收到的上下文">{viewContext ? messageWithWorkspaceContext('修改这个结果', { ...viewContext, onClear: () => undefined }) : ''}</output><button onClick={onProjectActivity}>模拟 Agent 结束</button>{draftRequest ? <textarea aria-label="Agent 输入草稿" value={draftRequest.text} readOnly /> : null}</section> };
});
const clients: QueryClient[] = [];
afterEach(() => { cleanup(); clients.splice(0).forEach((client) => client.clear()); sessionStorage.clear(); localStorage.clear(); window.history.replaceState(null, '', '/'); vi.restoreAllMocks(); });
const artifact = (patch: Partial<LabArtifact> = {}): LabArtifact => ({ artifactId: 'artifact-1', revision: 1, title: '项目发现', kind: 'investigation', view: 'markdown', content: '这是一份当前项目的观察。', summary: '', templateRef: null, actions: [], createdAtMs: 1, updatedAtMs: 1, ...patch });
const project = (patch: Partial<LabProject> = {}): LabProject => ({
  schemaVersion: 'rag-ime.agent-lab-project.v1', projectId: 'project-1', revision: 1, title: '售后助手', description: '让客服依据新的规则完成任务', briefVersion: 1,
  materialCount: 0, artifactCount: 0, guideSessionId: '', createdAtMs: 1, updatedAtMs: 1, materialSetId: '',
  materialSet: { materialSetId: '', version: 0, materials: [], createdAtMs: null }, materialVersions: [],
  intake: { state: 'needs_materials', requestedPath: '', resolvedPath: '', readCount: 0, readBytes: 0, skippedCount: 0, partial: false, issues: [], checkedAtMs: null },
  artifacts: [], bindings: [], workspace: { artifactOrder: [], primaryArtifactId: '', layout: 'split' }, workspaceBinding: null, ...patch,
});
function withArtifact(item: LabArtifact, patch: Partial<LabProject> = {}) {
  const { content: _content, ...summary } = item;
  return project({ artifacts: [summary], artifactCount: 1, workspace: { artifactOrder: [item.artifactId], primaryArtifactId: item.artifactId, layout: 'split' }, ...patch });
}
const views = ['markdown', 'table', 'form', 'code', 'html', 'json'];
function read(current: LabProject | null, items: LabProject[], item?: LabArtifact) { return { ok: true, items, project: current, supportedViews: views, ...(item ? { artifact: item } : {}) }; }
function RouteMarker() { const location = useLocation(); return <output aria-label="当前项目路由">{location.pathname}{location.search}</output>; }
function mount(transport: MockControlTransport, props: { initialProjectId?: string; root?: boolean; route?: string; activeSurface?: boolean } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } }); clients.push(client);
  const body = <QueryClientProvider client={client}><ControlTransportProvider transport={transport}>{props.root ? <EvalLabFeature /> : <LabProjectWorkbench initialProjectId={props.initialProjectId} />}</ControlTransportProvider></QueryClientProvider>;
  const surface = props.activeSurface ? <PawOsAppSurfaceProvider appId="eval-lab" active width={1200} height={800}>{body}</PawOsAppSurfaceProvider> : body;
  return render(props.route ? <MemoryRouter initialEntries={[props.route]}>{surface}<RouteMarker /></MemoryRouter> : surface);
}

describe('Agent-led Lab project container', () => {
  it('routes a completed App graph node to its exact version and call while retaining the Guide', async () => {
    const appId = 'extension:lab-11111111111111111111111111111111';
    const current = project({ guideSessionId: 'guide-one', workflow: { schemaVersion: 'paw.lab-project-workflow.v1', observedAtMs: 1,
      nodes: [{ id: 'app-original-call', kind: 'job', title: '原研究调用', status: 'completed', summary: '执行已完成', dependencies: [], ref: { kind: 'application_call', id: 'original-call', version: 1 }, evidenceRefs: [{ kind: 'application', id: appId, version: 1 }], source: 'runtime' }],
      edges: [], counts: { running: 0, queued: 0, completed: 1, failed: 0 }, currentNodeId: 'app-original-call' } });
    const application = { appId, projectId: current.projectId, title: '研究应用', description: '', revision: 2, latestVersion: 2, activeVersion: null, createdAtMs: 1, updatedAtMs: 2 };
    const version = { appId, version: 1, html: '<h1>旧版本</h1>', contentHash: 'a'.repeat(64), fileCount: 1, byteSize: 10, sourceFiles: [], spec: { title: '研究应用', model: { model: '原模型' }, actions: [{ id: 'answer', title: '研究', prompt: '原方法' }] } };
    const transport = new MockControlTransport({ routes: {
      'agent.eval-lab.projects.get': read(current, [current]),
      'agent.eval-lab.apps.get': ({ query }: ControlRequest) => query?.callId ? { ok: true, items: [application], app: application, version, versions: [version], calls: [], call: { appId, callId: 'original-call', version: 1, actionId: 'answer', state: 'completed', sessionId: 'original-session', input: {}, result: { text: '原调用研究结果' }, error: '' } } : { ok: true, items: [application], app: null },
    } });
    mount(transport, { initialProjectId: current.projectId });
    fireEvent.click(await screen.findByRole('button', { name: '项目总览' }));
    const guide = screen.getByRole('region', { name: '项目 Agent' });
    fireEvent.click(screen.getByRole('button', { name: '查看结果' }));
    expect(await screen.findByText('原调用研究结果')).toBeVisible();
    expect(screen.getByRole('region', { name: '项目 Agent' })).toBe(guide);
    expect(screen.queryByTitle('研究应用 · 应用预览')).not.toBeInTheDocument();
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.eval-lab.apps.get' && request.query?.appId === appId && request.query?.version === 1 && request.query?.callId === 'original-call')).toBe(true);
    expect(transport.requests.every(({ request }) => request.pathId.endsWith('.get'))).toBe(true);
  });
  it('opens paired Session and Turn sources inside the project while preserving the Guide element', async () => {
    const current = project({ guideSessionId: 'guide-one', workflow: { schemaVersion: 'paw.lab-project-workflow.v1', observedAtMs: 1,
      nodes: [{ id: 'experiment-one', kind: 'experiment', title: '原实验', status: 'completed', summary: '原结果', dependencies: [], ref: { kind: 'golden_job', id: 'experiment-one' }, source: 'runtime', evidenceRefs: [{ kind: 'runtime_session', id: 'source-session' }, { kind: 'runtime_turn', id: 'source-turn' }] }],
      edges: [], counts: { running: 0, queued: 0, completed: 1, failed: 0 }, currentNodeId: 'experiment-one' } });
    const transport = new MockControlTransport({ routes: {
      'agent.eval-lab.projects.get': read(current, [current]),
      'agent.session.snapshot': { schemaVersion: 'rag-ime.agent-message-list.v1', ok: true, sessionId: 'source-session', items: [], liveEvents: [], status: 'idle' },
    } });
    mount(transport, { initialProjectId: current.projectId });
    fireEvent.click(await screen.findByRole('button', { name: '项目总览' }));
    const guide = screen.getByRole('region', { name: '项目 Agent' });
    fireEvent.click(screen.getByRole('button', { name: '查看原回合' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('指定回合未出现在原 Session');
    expect(screen.getByRole('region', { name: '项目 Agent' })).toBe(guide);
    expect(screen.getByRole('heading', { name: '项目工作流' })).toBeVisible();
    expect(transport.requests.find(({ request }) => request.pathId === 'agent.session.snapshot')?.request.params).toEqual({ sessionId: 'source-session' });
    expect(transport.requests.every(({ request }) => ['agent.session.snapshot', 'agent.eval-lab.projects.get'].includes(request.pathId))).toBe(true);
  });
  it('opens an exact Knowledge graph receipt beside the current work without another run', async () => {
    const current = project({ workflow: { schemaVersion: 'paw.lab-project-workflow.v1', observedAtMs: 1,
      nodes: [{ id: 'old-knowledge-job', kind: 'experiment', title: '原检索对照', status: 'completed', summary: '原结果', dependencies: [], ref: { kind: 'knowledge_job', id: 'old-knowledge-job' }, source: 'runtime' }],
      edges: [], counts: { running: 0, queued: 0, completed: 1, failed: 0 }, currentNodeId: 'old-knowledge-job' } });
    const transport = new MockControlTransport({ routes: {
      'agent.eval-lab.projects.get': read(current, [current]),
      'agent.eval-lab.trials.get': { schemaVersion: 'rag-ime.agent-lab-trial.v1', job: { jobId: 'old-knowledge-job', sceneId: 'knowledge-resource', state: 'completed', publicSpec: { projectId: current.projectId, operation: 'search' }, result: { query: '原问题', hits: [{ title: '原来源', content: '原始检索证据', sourceId: 'source', chunkId: 'chunk' }] } } },
    } });
    mount(transport, { initialProjectId: current.projectId });
    fireEvent.click(await screen.findByRole('button', { name: '项目总览' }));
    fireEvent.click(screen.getByRole('button', { name: '查看结果' }));
    expect(await screen.findByText('原始检索证据')).toBeVisible();
    expect(screen.getByRole('heading', { name: '项目工作流' })).toBeVisible();
    expect(screen.getByRole('region', { name: '项目 Agent' })).toBeVisible();
    expect(transport.requests.find(({ request }) => request.pathId === 'agent.eval-lab.trials.get')?.request.query).toEqual({ jobId: 'old-knowledge-job' });
    expect(transport.requests.every(({ request }) => request.pathId.endsWith('.get'))).toBe(true);
  });
  it('does not describe an ordinary pending Guide message as preparing an App', async () => {
    const current = project({ guideSessionId: 'guide-1' }); let settle!: (value: unknown) => void;
    const transport = new MockControlTransport({ routes: {
      'agent.eval-lab.projects.get': read(current, [current]),
      'agent.eval-lab.apps.get': { ok: true, items: [], app: null },
      'agent.session.prompt': () => new Promise((resolve) => { settle = resolve; }),
    } });
    mount(transport, { initialProjectId: current.projectId });
    fireEvent.click(await screen.findByRole('button', { name: '继续下一步' }));
    await waitFor(() => expect(settle).toBeDefined());
    fireEvent.click(screen.getByRole('button', { name: '应用交付' }));
    expect(await screen.findByText('还没有准备好的应用版本')).toBeVisible();
    expect(screen.queryByText(/正在冻结资料并准备应用版本/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '准备应用版本' })).toBeDisabled();
    await act(async () => settle({ accepted: true }));
  });
  it('defaults to the result workspace and opens a completed graph result without another execution', async () => {
    const item = artifact(); const current = withArtifact(item, { workflow: {
      schemaVersion: 'paw.lab-project-workflow.v1', observedAtMs: 1,
      nodes: [{ id: 'saved-result', kind: 'artifact', title: '已保存的实验结果', status: 'completed', summary: '保留原始结果', dependencies: [], ref: { kind: 'artifact', id: item.artifactId }, source: 'artifact' }],
      edges: [], counts: { running: 0, queued: 0, completed: 0, failed: 0 }, currentNodeId: null,
    } });
    const transport = new MockControlTransport({ routes: { 'agent.eval-lab.projects.get': (request: ControlRequest) => read(request.query?.projectId ? current : null, [current], request.query?.artifactId ? item : undefined) } });
    mount(transport, { initialProjectId: current.projectId });
    expect(await screen.findByRole('region', { name: '项目成果工作面' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '项目总览' }));
    expect(await screen.findByRole('heading', { name: '项目工作流' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '查看结果' }));
    expect(await screen.findByText('这是一份当前项目的观察。')).toBeVisible();
    expect(transport.requests.every(({ request }) => request.pathId === 'agent.eval-lab.projects.get')).toBe(true);
  });
  it('opens the exact latest artifact selected on the home page even when a different artifact is primary', async () => {
    const primary = artifact({ artifactId: 'primary', title: '原主成果' });
    const latest = artifact({ artifactId: 'latest', title: '最新实验', content: '这是首页所指的最新实验。' });
    const current = withArtifact(primary, { artifactCount: 2, artifacts: [primary, latest],
      workspace: { artifactOrder: ['primary', 'latest'], primaryArtifactId: 'primary', layout: 'focus' },
      latestRecord: { kind: 'artifact', status: 'available', title: latest.title, artifactId: latest.artifactId, updatedAtMs: 2 } });
    const transport = new MockControlTransport({ routes: { 'agent.eval-lab.projects.get': (request: ControlRequest) => read(request.query?.projectId ? current : null, [current], request.query?.artifactId === 'latest' ? latest : request.query?.artifactId === 'primary' ? primary : undefined) } });
    mount(transport);
    fireEvent.click(await screen.findByRole('button', { name: /最新实验 查看成果/ }, { timeout: 5000 }));
    expect(await screen.findByText('这是首页所指的最新实验。')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '最新实验' })).toHaveAttribute('aria-current', 'page');
  });
  it('keeps an ordinary comparison table visible when it is not an experiment table', () => {
    const item = artifact({ view: 'table', content: { columns: ['metric', 'baseline', 'candidate'].map((key) => ({ key, label: key })), rows: [{ metric: '业务设置', baseline: '以前', candidate: '现在' }] } });
    render(<ArtifactSurface artifact={item} busy={false} onDraft={vi.fn()} onSave={async () => true} onAction={vi.fn()} />);
    expect(screen.getByRole('table')).toBeVisible();
    expect(screen.queryByText('查看数据表')).not.toBeInTheDocument();
  });
  it('continues through the owning Agent only on user action and suppresses duplicate sends', async () => {
    const current = project({ guideSessionId: 'guide-1' });
    let settle!: (value: unknown) => void;
    const messages: ControlRequest[] = [];
    const transport = new MockControlTransport({ routes: {
      'agent.eval-lab.projects.get': read(current, [current]),
      'agent.session.prompt': (request: ControlRequest) => { messages.push(request); return new Promise((resolve) => { settle = resolve; }); },
    } });
    mount(transport, { initialProjectId: current.projectId });
    await screen.findByRole('button', { name: '实验闭环' });
    expect(messages).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: '实验闭环' }));
    fireEvent.click(screen.getByRole('button', { name: '自动推进优化' }));
    fireEvent.click(screen.getByRole('button', { name: '自动推进优化' }));
    await waitFor(() => expect(messages).toHaveLength(1));
    expect(messages[0]?.params).toEqual({ sessionId: 'guide-1' });
    expect(messages[0]?.body).toMatchObject({ message: expect.stringContaining('在已有授权和预算内连续完成') });
    expect(screen.getByRole('button', { name: '带我逐步完成' })).toBeDisabled();
    await act(async () => settle({ accepted: true }));
    await waitFor(() => expect(screen.getByRole('button', { name: '带我逐步完成' })).toBeEnabled());
  });
  it('saves materials before resuming the same project, and lets the user opt out', async () => {
    let current = project({ guideSessionId: 'guide-1' });
    const order: string[] = [];
    const transport = new MockControlTransport({ routes: {
      'agent.eval-lab.projects.get': () => read(current, [current]),
      'agent.eval-lab.projects.command': (request: ControlRequest) => {
        const command = request.body as ProjectCommand; order.push(command.action);
        current = { ...current, revision: current.revision + 1 };
        return { ok: true, project: current, clientRequestId: command.clientRequestId, replayed: false };
      },
      'agent.session.prompt': () => { order.push('continue'); return { accepted: true }; },
    } });
    mount(transport, { initialProjectId: current.projectId });
    fireEvent.click(await screen.findByRole('button', { name: '添加材料' }));
    fireEvent.change(screen.getByRole('textbox', { name: '粘贴材料标题' }), { target: { value: '规则' } });
    fireEvent.change(screen.getByRole('textbox', { name: '材料正文' }), { target: { value: '已确认的规则' } });
    fireEvent.click(screen.getByRole('button', { name: '保存文本材料' }));
    await waitFor(() => expect(order).toEqual(['import_materials', 'continue']));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    fireEvent.click(screen.getAllByRole('button', { name: '添加材料' })[0]!);
    fireEvent.click(screen.getByRole('checkbox', { name: '材料接入后，让 Agent 继续推进' }));
    fireEvent.change(screen.getByRole('textbox', { name: '粘贴材料标题' }), { target: { value: '补充规则' } });
    fireEvent.change(screen.getByRole('textbox', { name: '材料正文' }), { target: { value: '只保存这一份' } });
    fireEvent.click(screen.getByRole('button', { name: '保存文本材料' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(order).toEqual(['import_materials', 'continue', 'import_materials']);
  });
  it('imports an existing scene as a project without starting its Agent or any trial', async () => {
    let current: LabProject | null = null; const commands: ProjectCommand[] = [];
    const historyCollections = [{ sceneId: 'cloudops', title: '云上事故诊断', sourceHash: 'frozen-source', experimentCount: 9,
      datasetIds: ['incident-validation'], latestEvidenceAtMs: 1 }];
    const transport = new MockControlTransport({ routes: {
      'agent.eval-lab.projects.get': (request: ControlRequest) => ({ ...read(request.query?.projectId ? current : null, current ? [current] : []), historyCollections }),
      'agent.eval-lab.projects.command': (request: ControlRequest) => { const command = request.body as ProjectCommand; commands.push(command);
        current = project({ title: '云上事故诊断', workspace: { artifactOrder: [], primaryArtifactId: '', layout: 'focus' },
          historyOrigin: { sceneId: 'cloudops', sourceHash: 'frozen-source', experimentCount: 9, importedAtMs: 1, snapshotArtifactId: 'snapshot', snapshotArtifactRevision: 1 } });
        return { ok: true, project: current, clientRequestId: command.clientRequestId, replayed: false }; },
    } });
    mount(transport);
    fireEvent.click(await screen.findByRole('button', { name: '导入已有实验' }));
    fireEvent.click(await screen.findByRole('button', { name: '导入为项目' }));
    expect(await screen.findByRole('heading', { name: '云上事故诊断', level: 1 })).toBeVisible();
    expect(commands).toHaveLength(1);
    expect(commands[0]).toMatchObject({ action: 'import_history', expectedRevision: 0, input: { sceneId: 'cloudops', sourceHash: 'frozen-source' } });
    expect(transport.requests.every(({ request }) => ['agent.eval-lab.projects.get', 'agent.eval-lab.projects.command'].includes(request.pathId))).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: '返回 Lab 项目' }));
    fireEvent.click(screen.getByRole('button', { name: '导入已有实验' }));
    fireEvent.click(await screen.findByRole('button', { name: '打开已导入项目' }));
    expect(commands).toHaveLength(1);
  });

  it('recovers a failed project read at the same revision without replaying commands or losing the draft', async () => {
    let offline = false; const current = project();
    const transport = new MockControlTransport({ routes: { 'agent.eval-lab.projects.get': (request: ControlRequest) => {
      if (request.query?.projectId && offline) throw new Error('Failed to fetch');
      return read(request.query?.projectId ? current : null, [current]);
    } } });
    mount(transport, { initialProjectId: current.projectId });
    fireEvent.click(await screen.findByRole('button', { name: '材料 0' }));
    fireEvent.click(screen.getAllByRole('button', { name: '添加材料' })[0]!);
    const field = await screen.findByRole('textbox', { name: '粘贴材料标题' });
    fireEvent.change(field, { target: { value: '尚未保存的材料' } });
    offline = true;
    await act(async () => { await clients[0]!.invalidateQueries({ queryKey: ['lab-projects'] }); });
    await screen.findByText('项目服务暂时不可用，请重新读取。');
    offline = false;
    await waitFor(() => expect(screen.queryByText('项目服务暂时不可用，请重新读取。')).not.toBeInTheDocument(), { timeout: 4500 });
    expect(field).toHaveValue('尚未保存的材料');
    expect(transport.requests.every(({ request }) => request.pathId === 'agent.eval-lab.projects.get')).toBe(true);
  });

  it('starts with a description and real projects, without a universal business form or fixed Golden journey', async () => {
    const transport = new MockControlTransport({ routes: { 'agent.eval-lab.projects.get': read(null, []) } });
    mount(transport, { root: true });
    expect(await screen.findByRole('heading', { name: '让第一个项目开始工作' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '新建项目' }));
    expect(await screen.findByRole('textbox', { name: '描述你的项目' })).toBeVisible();
    expect(screen.queryByRole('spinbutton', { name: '计划题数' })).not.toBeInTheDocument();
    expect(screen.queryByText('退货期限')).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: '校准评审' })).not.toBeInTheDocument();
    expect(transport.requests.every(({ request }) => request.pathId === 'agent.eval-lab.projects.get')).toBe(true);
  });

  it('turns the returning home into a continuation workbench with truthful next actions', async () => {
    const item = project({ projectId: 'history-1', title: '南极论文资料库', materialCount: 0, artifactCount: 1,
      historyOrigin: { sceneId: 'antarctic', sourceHash: 'source-1', experimentCount: 4, importedAtMs: 4, snapshotArtifactId: 'snapshot', snapshotArtifactRevision: 1 } });
    const transport = new MockControlTransport({ routes: { 'agent.eval-lab.projects.get': read(null, [item]) } });
    mount(transport);
    expect(await screen.findByRole('heading', { name: '需要处理' })).toBeVisible();
    expect(screen.getByRole('button', { name: /南极论文资料库，历史结果/u })).toBeVisible();
    expect(screen.getAllByText('准备复跑').length).toBeGreaterThan(0);
    expect(screen.queryByRole('textbox', { name: '描述你的项目' })).not.toBeInTheDocument();
    fireEvent.click((await screen.findAllByRole('button', { name: '新建项目' }))[0]!);
    expect(await screen.findByRole('dialog', { name: '新建项目' })).toBeVisible();
  });

  it('creates a description-only project and starts the exact App-owned Session', async () => {
    let current: LabProject | null = null; const commands: ProjectCommand[] = [];
    const transport = new MockControlTransport({ routes: {
      'agent.eval-lab.projects.get': (request: ControlRequest) => read(request.query?.projectId ? current : null, current ? [current] : []),
      'agent.eval-lab.projects.command': (request: ControlRequest) => { const command = request.body as ProjectCommand; commands.push(command); current = project({ description: String(command.input.description), guideSessionId: 'project-guide-1' }); return { ok: true, project: current, clientRequestId: command.clientRequestId, replayed: false }; },
      'agent.session.prompt': { ok: true, accepted: true },
    } });
    mount(transport);
    fireEvent.click(await screen.findByRole('button', { name: '新建项目' }));
    fireEvent.change(await screen.findByRole('textbox', { name: '描述你的项目' }), { target: { value: '诊断支付服务故障，不是资料问答' } });
    fireEvent.click(screen.getByRole('button', { name: '创建并开始' }));
    await screen.findByText('project-guide-1');
    await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.prompt')).toBe(true));
    expect(commands).toHaveLength(1);
    expect(commands[0]!.input).toEqual({ description: '诊断支付服务故障，不是资料问答' });
    const prompt = transport.requests.find(({ request }) => request.pathId === 'agent.session.prompt')!.request;
    expect(prompt.params).toEqual({ sessionId: 'project-guide-1' });
    expect(prompt.body).toMatchObject({ clientMessageId: 'lab-project-start:project-1', delivery: 'prompt' });
  });

  it('preserves the new-project description and source path when the page is reopened', async () => {
    const transport = new MockControlTransport({ routes: { 'agent.eval-lab.projects.get': read(null, []) } });
    const first = mount(transport);
    fireEvent.click(await screen.findByRole('button', { name: '新建项目' }));
    fireEvent.change(await screen.findByRole('textbox', { name: '描述你的项目' }), { target: { value: '还在整理的故障排查项目' } });
    fireEvent.change(screen.getByRole('textbox', { name: '连接执行器上的材料路径' }), { target: { value: '/workspace/incident' } });
    first.unmount(); mount(transport);
    fireEvent.click(await screen.findByRole('button', { name: '新建项目' }));
    expect(await screen.findByRole('textbox', { name: '描述你的项目' })).toHaveValue('还在整理的故障排查项目');
    expect(screen.getByRole('textbox', { name: '连接执行器上的材料路径' })).toHaveValue('/workspace/incident');
    expect(transport.requests.every(({ request }) => request.pathId === 'agent.eval-lab.projects.get')).toBe(true);
  });

  it('restores the selected resource and collapsed conversation without running anything', async () => {
    const current = project();
    const transport = new MockControlTransport({ routes:{'agent.eval-lab.projects.get':read(current,[current])} });
    const first = mount(transport,{initialProjectId:current.projectId});
    fireEvent.click(await screen.findByRole('button',{name:'材料 0'}));
    fireEvent.click(screen.getByRole('button',{name:'收起项目 Agent'}));
    first.unmount(); mount(transport,{initialProjectId:current.projectId});
    expect(await screen.findByRole('heading',{name:'项目实际材料'})).toBeVisible();
    expect(screen.queryByRole('region',{name:'项目 Agent'})).not.toBeInTheDocument();
    expect(screen.getByRole('button',{name:'展开项目 Agent'})).toBeEnabled();
    expect(transport.requests.every(({request}) => request.pathId==='agent.eval-lab.projects.get')).toBe(true);
  });

  it('honors a project focus layout until the user chooses to show the conversation', async () => {
    const current = project({workspace:{artifactOrder:[],primaryArtifactId:'',layout:'focus'}});
    const transport = new MockControlTransport({routes:{'agent.eval-lab.projects.get':read(current,[current])}});
    const first = mount(transport,{initialProjectId:current.projectId});
    expect(await screen.findByRole('button',{name:'展开项目 Agent'})).toBeVisible();
    expect(screen.queryByRole('region',{name:'项目 Agent'})).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button',{name:'材料 0'}));
    expect(screen.queryByRole('region',{name:'项目 Agent'})).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button',{name:'展开项目 Agent'}));
    expect(screen.getByRole('region',{name:'项目 Agent'})).toBeVisible();
    first.unmount();mount(transport,{initialProjectId:current.projectId});
    expect(await screen.findByRole('region',{name:'项目 Agent'})).toBeVisible();
  });

  it('keeps the project route aligned when returning home, changing projects and reopening', async () => {
    const items = [project(),project({projectId:'project-2',title:'服务拓扑'})];
    const transport = new MockControlTransport({ routes:{'agent.eval-lab.projects.get':(request: ControlRequest) =>
      read(items.find((item) => item.projectId === request.query?.projectId) ?? null,items)} });
    const first = mount(transport,{root:true,route:'/eval-lab?project=project-1',activeSurface:true});
    expect(await screen.findByRole('heading',{name:'售后助手'})).toBeVisible();
    fireEvent.click(screen.getByRole('button',{name:'返回 Lab 项目'}));
    fireEvent.click((await screen.findAllByRole('button',{name:'新建项目'}))[0]!);
    await screen.findByRole('textbox',{name:'描述你的项目'});
    fireEvent.click(screen.getByRole('button',{name:'关闭'}));
    expect(screen.getByLabelText('当前项目路由')).toHaveTextContent(/^\/eval-lab$/u);
    expect(window.location.hash).toBe('#/eval-lab');
    fireEvent.click(screen.getByRole('button',{name:/服务拓扑，/u}));
    expect(await screen.findByRole('heading',{name:'服务拓扑'})).toBeVisible();
    const route = screen.getByLabelText('当前项目路由').textContent!;
    expect(route).toBe('/eval-lab?project=project-2');
    expect(window.location.hash).toBe('#/eval-lab?project=project-2');
    first.unmount();mount(transport,{root:true,route});
    expect(await screen.findByRole('heading',{name:'服务拓扑'})).toBeVisible();
  });

  it('restores an unknown first project message after reopening without creating or prompting again automatically', async () => {
    let current: LabProject | null = null; const prompts: unknown[] = [];
    const transport = new MockControlTransport({ routes: {
      'agent.eval-lab.projects.get': (request: ControlRequest) => read(request.query?.projectId ? current : null, current ? [current] : []),
      'agent.eval-lab.projects.command': ({ body }: ControlRequest) => { const command = body as ProjectCommand; current = project({ guideSessionId: 'guide-unknown-1' }); return { ok: true, project: current, clientRequestId: command.clientRequestId, replayed: false }; },
      'agent.session.prompt': ({ body }: ControlRequest) => { prompts.push(body); if (prompts.length === 1) throw new TypeError('Response lost'); return { ok: true, accepted: true }; },
    } });
    const first = mount(transport);
    fireEvent.click(await screen.findByRole('button', { name: '新建项目' }));
    fireEvent.change(await screen.findByRole('textbox', { name: '描述你的项目' }), { target: { value: '第一次消息待核对的项目' } });
    fireEvent.click(screen.getByRole('button', { name: '创建并开始' }));
    await screen.findByRole('button', { name: '核对原消息' }); first.unmount(); mount(transport);
    fireEvent.click(await screen.findByRole('button', { name: '核对原消息' }));
    await waitFor(() => expect(prompts).toHaveLength(2)); expect(prompts[1]).toEqual(prompts[0]);
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.eval-lab.projects.command')).toHaveLength(1);
    await waitFor(() => expect(screen.queryByRole('button', { name: '核对原消息' })).not.toBeInTheDocument());
  });

  it('renders project-defined input fields, preserves an edit across views, and saves its real revision', async () => {
    let currentArtifact = artifact({ title: '售后条件', kind: 'support_rules', view: 'form', content: {
      fields: [{ key: 'days', label: '退货期限', type: 'number' }, { key: 'used', label: '允许已使用商品', type: 'boolean' }], values: { days: 7, used: false },
    } });
    let current = withArtifact(currentArtifact); const commands: ProjectCommand[] = [];
    const transport = new MockControlTransport({ routes: {
      'agent.eval-lab.projects.get': (request: ControlRequest) => read(request.query?.projectId ? current : null, [current], request.query?.artifactId ? currentArtifact : undefined),
      'agent.eval-lab.projects.command': (request: ControlRequest) => { const command = request.body as ProjectCommand; commands.push(command); currentArtifact = { ...currentArtifact, content: command.input.content!, revision: 2, updatedAtMs: 2 }; current = withArtifact(currentArtifact, { revision: 2, updatedAtMs: 2 }); return { ok: true, project: current, artifact: currentArtifact, replayed: false, clientRequestId: command.clientRequestId }; },
    } });
    mount(transport, { initialProjectId: 'project-1' });
    fireEvent.click(await screen.findByRole('button', { name: '成果 1' }));
    fireEvent.change(await screen.findByRole('spinbutton', { name: '退货期限' }), { target: { value: '14' } });
    fireEvent.click(screen.getByRole('button', { name: '材料 0' }));
    await screen.findByText('尚未读取材料。可以上传文件、粘贴正文或连接执行器路径。');
    fireEvent.click(screen.getByRole('button', { name: '成果 1' }));
    fireEvent.click(screen.getByRole('button', { name: '售后条件' }));
    expect(await screen.findByRole('spinbutton', { name: '退货期限' })).toHaveValue(14);
    fireEvent.click(screen.getByRole('button', { name: '保存新版本' }));
    await waitFor(() => expect(commands).toHaveLength(1));
    expect(commands[0]).toMatchObject({ action: 'publish_artifact', projectId: 'project-1', expectedRevision: 1,
      input: { artifactId: 'artifact-1', expectedArtifactRevision: 1, content: { values: { days: 14, used: false } } } });
    await waitFor(() => expect(screen.queryByRole('button', { name: '保存新版本' })).not.toBeInTheDocument());
  });

  it('uses different columns and interaction types for another project without adding a page branch', async () => {
    const item = artifact({ title: '故障时间线', kind: 'incident_timeline', view: 'table', content: {
      columns: [{ key: 'at', label: '事件时间' }, { key: 'service', label: '服务' }, { key: 'observed', label: '实际观测' }],
      rows: [{ at: '10:01', service: '支付', observed: '连接超时' }],
    } }); const current = withArtifact(item, { title: '支付故障排查' });
    const transport = new MockControlTransport({ routes: { 'agent.eval-lab.projects.get': (request: ControlRequest) => read(request.query?.projectId ? current : null, [current], request.query?.artifactId ? item : undefined) } });
    mount(transport, { initialProjectId: 'project-1' });
    fireEvent.click(await screen.findByRole('button', { name: '成果 1' }));
    expect(await screen.findByRole('columnheader', { name: '事件时间' })).toBeVisible();
    expect(screen.getByRole('cell', { name: '连接超时' })).toBeVisible();
    expect(screen.queryByRole('spinbutton', { name: '退货期限' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '运行' })).toBeEnabled();
  });

  it('restores an unsaved artifact draft after remounting the project', async () => {
    const item = artifact({ view: 'form', title: '环境观察', content: { fields: [{ key: 'service', label: '受影响服务', type: 'text' }], values: { service: '原值' } } });
    const current = withArtifact(item);
    const transport = new MockControlTransport({ routes: { 'agent.eval-lab.projects.get': (request: ControlRequest) => read(request.query?.projectId ? current : null, [current], request.query?.artifactId ? item : undefined) } });
    const first = mount(transport, { initialProjectId: 'project-1' });
    fireEvent.click(await screen.findByRole('button', { name: '成果 1' }));
    fireEvent.change(await screen.findByRole('textbox', { name: '受影响服务' }), { target: { value: '支付服务，仍待确认' } });
    first.unmount();
    mount(transport, { initialProjectId: 'project-1' });
    expect(await screen.findByRole('textbox', { name: '受影响服务' })).toHaveValue('支付服务，仍待确认');
    expect(screen.getByRole('button', { name: '保存新版本' })).toBeVisible();
    expect(transport.requests.every(({ request }) => request.pathId === 'agent.eval-lab.projects.get')).toBe(true);
  });

  it('reconciles an uncertain create with the same command instead of creating another project', async () => {
    let current: LabProject | null = null; let accepted: Record<string, unknown>; const commands: ProjectCommand[] = [];
    const transport = new MockControlTransport({ routes: {
      'agent.eval-lab.projects.get': (request: ControlRequest) => read(request.query?.projectId ? current : null, current ? [current] : []),
      'agent.eval-lab.projects.command': (request: ControlRequest) => { const command = request.body as ProjectCommand; commands.push(command); if (commands.length === 1) { current = project(); accepted = { ok: true, project: current, replayed: false, clientRequestId: command.clientRequestId }; throw new Error('连接中断'); } return { ...accepted, replayed: true }; },
    } });
    mount(transport);
    fireEvent.click(await screen.findByRole('button', { name: '新建项目' }));
    fireEvent.change(await screen.findByRole('textbox', { name: '描述你的项目' }), { target: { value: '业务任务' } });
    fireEvent.click(screen.getByRole('button', { name: '创建并开始' }));
    fireEvent.click(await screen.findByRole('button', { name: '核对原操作' }));
    await screen.findByRole('heading', { name: '售后助手' });
    expect(commands).toHaveLength(2); expect(commands[1]).toEqual(commands[0]);
    expect(screen.queryByRole('button', { name: '核对原操作' })).not.toBeInTheDocument();
  });

  it('isolates custom HTML and stages only declared interactions without automatically prompting the Agent', async () => {
    const item = artifact({ title: '服务拓扑', view: 'html', kind: 'service_graph', content: '<button>查看服务</button>', actions: [{ actionId: 'inspect', label: '排查选定服务', prompt: '排查所选服务' }] });
    const current = withArtifact(item, { guideSessionId: 'guide-1' });
    const transport = new MockControlTransport({ routes: { 'agent.eval-lab.projects.get': (request: ControlRequest) => read(request.query?.projectId ? current : null, [current], request.query?.artifactId ? item : undefined) } });
    mount(transport, { initialProjectId: 'project-1' });
    fireEvent.click(await screen.findByRole('button', { name: '成果 1' }));
    const frame = await waitFor(() => {
      const target = document.querySelector<HTMLIFrameElement>('iframe[title="服务拓扑"]');
      expect(target).not.toBeNull(); return target!;
    });
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts');
    expect(frame.getAttribute('srcdoc')).toBeNull();
    expect(frame.getAttribute('src')).toMatch(/^\/__paw_html_preview#/u);
    expect(atob(frame.getAttribute('src')!.split('#')[1]!.replaceAll('-', '+').replaceAll('_', '/'))).toContain("connect-src 'none'");
    fireEvent(window, new MessageEvent('message', { source: window, data: { type: 'paw.lab.artifact.action', actionId: 'inspect' } }));
    expect(screen.queryByRole('button', { name: '带入对话' })).not.toBeInTheDocument();
    fireEvent(window, new MessageEvent('message', { source: frame.contentWindow, data: { type: 'paw.lab.artifact.action', actionId: 'inspect', values: { service: 'payments' } } }));
    fireEvent.click(await screen.findByRole('button', { name: '带入对话' }));
    expect((screen.getByRole('textbox', { name: 'Agent 输入草稿' }) as HTMLTextAreaElement).value).toContain('payments');
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.prompt')).toBe(false);
  });
});

describe('artifact editing', () => {
  it('retains a malformed structured-data draft after returning to its preview', async () => {
    const item = artifact({ view: 'json', content: { observed: 1 } }); const save = vi.fn();
    function Editor() { const [draft, setDraft] = useState<import('./ArtifactSurface').ArtifactDraft>(); return <ArtifactSurface artifact={item} draft={draft} busy={false} onDraft={setDraft} onSave={save} onAction={vi.fn()} />; }
    render(<Editor />);
    fireEvent.click(screen.getByRole('button', { name: '编辑内容' }));
    fireEvent.change(screen.getByRole('textbox', { name: '成果内容草稿' }), { target: { value: '{invalid' } });
    fireEvent.click(screen.getByRole('button', { name: '查看成果' }));
    fireEvent.click(screen.getByRole('button', { name: '保存新版本' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('JSON 格式还不完整');
    expect(save).not.toHaveBeenCalled();
  });
});


it('links selected artifact contents and refreshed revisions to the Guide without sending on selection', async () => {
  let first = artifact({ title: '第一份结果', content: 'FIRST_BODY' });
  const second = artifact({ artifactId: 'artifact-2', title: '第二份结果', content: 'SECOND_BODY' });
  let current = withArtifact(first, { guideSessionId: 'guide-1', artifacts: [first, second], artifactCount: 2 });
  const transport = new MockControlTransport({ routes: {
    'agent.eval-lab.projects.get': ({ query }: ControlRequest) => read(current, [current], query?.artifactId === second.artifactId ? second : first),
  } });
  mount(transport, { initialProjectId: current.projectId });
  const context = await screen.findByLabelText('Agent 将收到的上下文');
  await waitFor(() => expect(context).toHaveTextContent('FIRST_BODY'));
  const guide = screen.getByRole('region', { name: '项目 Agent' });
  fireEvent.click(screen.getByRole('button', { name: /第二份结果/ }));
  await waitFor(() => expect(context).toHaveTextContent('SECOND_BODY'));
  expect(context).not.toHaveTextContent('FIRST_BODY');
  fireEvent.click(screen.getByRole('button', { name: /第一份结果/ }));
  first = { ...first, revision: 2, content: 'UPDATED_BODY' };
  current = { ...current, revision: 2, artifacts: [first, second] };
  fireEvent.click(screen.getByRole('button', { name: '模拟 Agent 结束' }));
  await waitFor(() => expect(context).toHaveTextContent('UPDATED_BODY'));
  expect(context).not.toHaveTextContent('FIRST_BODY');
  expect(context).toHaveTextContent('v2');
  expect(screen.getByRole('region', { name: '项目 Agent' })).toBe(guide);
  expect(transport.requests.every(({ request }) => request.pathId.endsWith('.get'))).toBe(true);
});

it('links selected experiment receipts and restores artifact context when returning', async () => {
  const item = artifact();
  const node = { id: 'experiment-context', kind: 'experiment' as const, title: '本轮实验', status: 'failed' as const, summary: 'EXPERIMENT_FAILURE', dependencies: [], ref: { kind: 'golden_job', id: 'job-context' }, source: 'runtime' as const };
  const current = withArtifact(item, { workflow: { schemaVersion: 'paw.lab-project-workflow.v1', observedAtMs: 1, nodes: [node], edges: [], counts: { running: 0, queued: 0, completed: 0, failed: 1 }, currentNodeId: null } });
  const transport = new MockControlTransport({ routes: { 'agent.eval-lab.projects.get': read(current, [current], item) } });
  mount(transport, { initialProjectId: current.projectId });
  fireEvent.click(await screen.findByRole('button', { name: /本轮实验/ }));
  const context = screen.getByLabelText('Agent 将收到的上下文');
  expect(context).toHaveTextContent('EXPERIMENT_FAILURE');
  expect(context).toHaveTextContent('job-context');
  expect(context).not.toHaveTextContent(item.content as string);
  fireEvent.click(screen.getByRole('button', { name: '返回当前成果' }));
  await waitFor(() => expect(context).toHaveTextContent(item.content as string));
  expect(context).not.toHaveTextContent('EXPERIMENT_FAILURE');
});
