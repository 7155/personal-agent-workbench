import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import { MockControlTransport } from '@/test/mock-transport';
import type { PawExtensionAppManifest } from '@/paw-os/extensions/types';
import type { ControlRequest } from '@/platform/transport';
import App, { GIS_ACCEPTANCE_TASK } from './App';
import manifest from './pawos-app.json';
const { seen, seenMap } = vi.hoisted(() => ({ seen: vi.fn(), seenMap: vi.fn() }));
vi.mock('@/paw-os/apps/PawSessionWorkspace', () => ({ sessionWorkspaceProjectionSlice: () => ({ activeTurnId: '' }), PawSessionWorkspace: (props: { recordId: string }) => { seen(props); return <div data-testid="original-agent">{props.recordId}</div>; } }));
vi.mock('./EarthMap', () => ({ EarthMap: (props: {onSelect:(feature: unknown)=>void; onOpenFile?: (file: { path: string; name: string; kind: 'file' }) => void}) => { seenMap(props); return <><button onClick={()=>props.onSelect({type:'Feature',geometry:{type:'Point',coordinates:[120.1,30.2]},properties:{source:'user_selection'}})}>选择地图地点</button>{props.onOpenFile ? <button onClick={() => props.onOpenFile?.({ path: '/work/report.html', name: 'report.html', kind: 'file' })}>打开 HTML 报告</button> : null}</>; } }));
vi.mock('@/features/agent/file-preview/CodePreview', () => ({ CodePreview: ({ content }: { content: string }) => <pre>{content}</pre> }));
afterEach(() => { cleanup(); seen.mockClear(); seenMap.mockClear(); vi.unstubAllGlobals(); });
function show(transport: MockControlTransport) { render(<ControlTransportProvider transport={transport}><TooltipProvider><App manifest={manifest as PawExtensionAppManifest} /></TooltipProvider></ControlTransportProvider>); }
it('restores only the owning App Session and reuses the original full conversation', async () => {
  const transport = new MockControlTransport({ routes: {
    'agent.sessions.list': { items: [
      { id: 'foreign', mode: 'assistant', title: 'Foreign', updatedAtMs: 9, surfaceKind: 'extension_app', ownerAppId: 'extension:other', surfaceKey: 'analysis' },
      { id: 'ours', mode: 'assistant', title: 'Earth', updatedAtMs: 1, surfaceKind: 'extension_app', ownerAppId: manifest.id, surfaceKey: 'analysis', workspaceRoots: ['/work'] },
    ] },
    'agent.session.workspace.read': () => { throw new Error('No run yet'); },
  } });
  show(transport);
  expect(await screen.findByTestId('original-agent')).toHaveTextContent('ours');
  expect(seen.mock.lastCall?.[0].appearance).not.toBe('embedded');
  expect(transport.requests.some(x => x.request.pathId === 'agent.sessions.create')).toBe(false);
  await userEvent.click(screen.getByRole('button',{name:'选择地图地点'}));
  expect(JSON.parse(seen.mock.lastCall?.[0].composerContext.text).features[0].geometry.coordinates).toEqual([120.1,30.2]);
  expect(screen.getByRole('button',{name:'地图'})).toHaveAttribute('aria-pressed','true');
  expect(transport.requests.some(x=>x.request.pathId==='agent.session.prompt')).toBe(false);
  act(()=>seen.mock.lastCall?.[0].composerContext.onClear());
  expect(seen.mock.lastCall?.[0].composerContext).toBeUndefined();
});
it('creates the exact App-owned Session and freezes the task in its first message', async () => {
  const user = userEvent.setup();
  const transport = new MockControlTransport({ routes: {
    'agent.sessions.list': { items: [] },
    'agent.sessions.create': { session: { id: 'new', title: 'Earth', mode: 'assistant', updatedAtMs: 1, workspaceRoots: ['/work'] } },
    'agent.session.mode.update': { ok: true },
    'agent.session.prompt': { ok: true },
    'agent.session.workspace.read': () => { throw new Error('No run yet'); },
  } });
  show(transport);
  await user.type(await screen.findByRole('textbox', { name: '项目文件夹' }), '/work');
  await user.type(screen.getByRole('textbox', { name: 'Google Cloud 项目' }), 'test-project');
  await user.type(screen.getByRole('textbox', { name: '分析任务' }), '比较两个候选地块');
  await user.click(screen.getByRole('button', { name: '开始分析' }));
  await user.click(screen.getByRole('button', { name: '确认方案并执行' }));
  await waitFor(() => expect(transport.requests.some(x => x.request.pathId === 'agent.session.prompt')).toBe(true));
  expect(transport.requests.find(x => x.request.pathId === 'agent.sessions.create')?.request.body).toMatchObject({ mode: 'coordinator', executionMode: 'workspace_managed', ownerAppId: manifest.id, surfaceKind: 'extension_app', surfaceKey: 'analysis', workspaceRoots: ['/work'] });
  const prompt = transport.requests.find(x => x.request.pathId === 'agent.session.prompt')?.request.body;
  expect(prompt).toMatchObject({ message: expect.stringContaining('比较两个候选地块'), clientMessageId: expect.any(String) });
});

it('offers a short plain-language GIS acceptance task', async () => {
  const transport = new MockControlTransport({ routes: { 'agent.sessions.list': { items: [] } } });
  show(transport);
  await userEvent.click(await screen.findByRole('button', { name: '填入验收任务' }));
  expect(screen.getByRole('textbox', { name: '分析任务' })).toHaveValue(GIS_ACCEPTANCE_TASK);
  expect(GIS_ACCEPTANCE_TASK).toContain('避开河流 200 米');
});

it('disables script execution while a non-script workspace artifact is open', async () => {
  const revision = `sha256:${'0'.repeat(64)}`;
  const run = {
    schemaVersion: 'earth.run.v1', runId: 'run-1', status: 'completed', code: 'print(1);', scriptPath: '/work/analysis.js',
    project: 'earth-test', sourceHash: 'hash', startedAt: '2026-09-19T00:00:00Z', updatedAt: '2026-09-19T00:00:01Z',
    layers: [], console: [],
  };
  const snapshots: Record<string, string> = {
    '/work/.earth/workspace.json': JSON.stringify(run),
    '/work/analysis.js': 'print(1);',
    '/work/report.html': '<h1>Report</h1>',
  };
  const transport = new MockControlTransport({ routes: {
    'agent.sessions.list': { items: [{ id: 'ours', mode: 'assistant', title: 'Earth', updatedAtMs: 1, surfaceKind: 'extension_app', ownerAppId: manifest.id, surfaceKey: 'analysis', workspaceRoots: ['/work'] }] },
    'agent.session.workspace.list': { items: [] },
    'agent.session.workspace.read': (request: { query?: { path?: string } }) => {
      const path = request.query?.path || '';
      const content = snapshots[path];
      if (content === undefined) throw new Error(`missing ${path}`);
      return { ok: true, path, content, byteSize: new TextEncoder().encode(content).length, loadedBytes: new TextEncoder().encode(content).length, nextOffset: new TextEncoder().encode(content).length, truncated: false, resourceRevision: revision, editability: { editable: false } };
    },
  } });
  show(transport);
  await screen.findByTestId('original-agent');
  const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: '代码' }));
  const runButton = await screen.findByRole('button', { name: '运行已保存代码' });
  await waitFor(() => expect(runButton).not.toBeDisabled());
  await user.click(screen.getByRole('button', { name: '地图' }));
  await user.click(screen.getByRole('button', { name: '打开 HTML 报告' }));
  expect(runButton).toBeDisabled();
  await new Promise(resolve=>setTimeout(resolve,2700));
  expect(screen.getByTitle('report.html')).toHaveAttribute('srcdoc','<h1>Report</h1>');
  expect(runButton).toBeDisabled();
  expect(transport.requests.some(item=>item.request.pathId==='agent.session.prompt')).toBe(false);
});

it('opens a local GIS project without Google configuration or a model prompt',async()=>{
 const transport=new MockControlTransport({routes:{'agent.sessions.list':{items:[]},'agent.sessions.create':{session:{id:'local',title:'GIS 项目',mode:'coordinator',updatedAtMs:1,workspaceRoots:['/work']}},'agent.session.workspace.read':()=>{throw new Error('path does not exist in the authorized workspace; nearby entries: .earth/layers');}}});
 show(transport);
 await userEvent.type(await screen.findByRole('textbox',{name:'项目文件夹'}),'/work');
 await userEvent.click(screen.getByRole('button',{name:'新建项目 Agent'}));
 await screen.findByTestId('original-agent');
 await waitFor(()=>expect(screen.queryByText('尚未读到最新结果 · 已保留当前内容')).not.toBeInTheDocument());
 expect(transport.requests.some(item=>item.request.pathId==='agent.sessions.create')).toBe(true);
 expect(transport.requests.some(item=>item.request.pathId==='agent.session.prompt')).toBe(false);
});

it('opens the chosen map workflow without starting analysis or sending a model prompt',async()=>{
 const transport=new MockControlTransport({routes:{'agent.sessions.list':{items:[]}}});
 show(transport);
 await screen.findByRole('textbox',{name:'项目文件夹'});
 await userEvent.click(screen.getByRole('button',{name:'开始任务'}));
 await userEvent.click(screen.getByRole('button',{name:/研究变化/}));
 expect(await screen.findByRole('heading',{name:'查看植被状况'})).toBeVisible();
 expect(transport.requests.some(item=>item.request.pathId==='agent.session.prompt')).toBe(false);
});

const projectAgent = (id: string, path: string, updatedAtMs = 1) => ({ id, title: id, mode: 'coordinator', updatedAtMs, surfaceKind: 'extension_app', ownerAppId: manifest.id, surfaceKey: 'analysis', workspaceRoots: [path] });
const noRun = () => { throw new Error('path does not exist in the authorized workspace'); };

it('uses the existing directory bridge, then enters an existing Agent without changing any binding', async () => {
  const transport = new MockControlTransport({
    pickedFiles: [{ id: 'directory', name: 'second', path: '/gis/second', mimeType: 'inode/directory', byteSize: 0 }],
    routes: {
      'agent.sessions.list': { items: [projectAgent('first', '/gis/first', 2), projectAgent('second', '/gis/second')] },
      'agent.session.workspace.list': { items: [] },
      'agent.session.workspace.read': noRun,
    },
  });
  show(transport);
  expect(await screen.findByTestId('original-agent')).toHaveTextContent('first');
  await userEvent.click(screen.getByRole('button', { name: '选择 GIS 项目与 Agent' }));
  await userEvent.click(screen.getByRole('button', { name: '浏览…' }));
  expect(transport.filePickCalls).toEqual([{ purpose: 'workspace-root', selection: 'directory', multiple: false, maxFiles: 1 }]);
  expect(screen.getByLabelText('当前项目文件夹')).toHaveTextContent('/gis/first');
  expect(screen.getByTestId('original-agent')).toHaveTextContent('first');
  await userEvent.click(screen.getByRole('button', { name: '进入所选 Agent' }));
  expect(screen.getByTestId('original-agent')).toHaveTextContent('second');
  expect(screen.getByLabelText('当前项目文件夹')).toHaveTextContent('/gis/second');
  expect(seenMap.mock.lastCall?.[0].workspaceKey).toBe('/gis/second');
  expect(transport.requests.some(item => ['agent.sessions.create', 'agent.session.prompt', 'agent.session.mode.update', 'agent.session.workspace.update'].includes(item.request.pathId))).toBe(false);
});

it('creates a second Agent in the same directory without a prompt or rebinding the original', async () => {
  const transport = new MockControlTransport({ routes: {
    'agent.sessions.list': { items: [projectAgent('first', '/gis/shared')] },
    'agent.sessions.create': { session: projectAgent('second', '/gis/shared', 2) },
    'agent.session.workspace.list': { items: [] },
    'agent.session.workspace.read': noRun,
  } });
  show(transport);
  await screen.findByTestId('original-agent');
  await userEvent.click(screen.getByRole('button', { name: '选择 GIS 项目与 Agent' }));
  await userEvent.click(screen.getByRole('button', { name: '新建同项目 Agent' }));
  await waitFor(() => expect(screen.getByTestId('original-agent')).toHaveTextContent('second'));
  expect(transport.requests.find(item => item.request.pathId === 'agent.sessions.create')?.request.body).toMatchObject({ title: 'shared · Agent 2', workspaceRoots: ['/gis/shared'], executionMode: 'workspace_managed', surfaceKind: 'extension_app', ownerAppId: manifest.id, surfaceKey: 'analysis' });
  expect(transport.requests.some(item => item.request.pathId === 'agent.session.prompt')).toBe(false);
  await userEvent.click(screen.getByRole('button', { name: '选择 GIS 项目与 Agent' }));
  await userEvent.selectOptions(screen.getByRole('combobox', { name: '项目 Agent' }), 'first');
  await userEvent.click(screen.getByRole('button', { name: '进入所选 Agent' }));
  expect(screen.getByTestId('original-agent')).toHaveTextContent('first');
  expect(screen.getByLabelText('当前项目文件夹')).toHaveTextContent('/gis/shared');
});

it('uses the installed Electron directory picker when transport picking is unavailable', async () => {
  const pickWorkspaceDirectory = vi.fn().mockResolvedValue({ name: 'native', path: '/gis/native' });
  vi.stubGlobal('pawBrowserHost', { kind: 'electron-webview', partition: 'persist:paw-browser', pickWorkspaceDirectory });
  const transport = new MockControlTransport({ routes: { 'agent.sessions.list': { items: [] } } });
  Object.defineProperty(transport, 'pickFiles', { value: undefined });
  show(transport);
  await userEvent.click(await screen.findByRole('button', { name: '浏览…' }));
  await waitFor(() => expect(screen.getByRole('textbox', { name: '项目文件夹' })).toHaveValue('/gis/native'));
  expect(pickWorkspaceDirectory).toHaveBeenCalledOnce();
  expect(transport.requests.some(item => item.request.pathId === 'agent.sessions.create')).toBe(false);
});

it('keeps empty directories and symlinks while reporting partial scans instead of claiming an empty project', async () => {
  const entries = [
    { path: '/gis/shared/empty', name: 'empty', kind: 'directory' },
    { path: '/gis/shared/restricted', name: 'restricted', kind: 'directory' },
    { path: '/gis/shared/reference', name: 'reference', kind: 'symlink' },
    { path: '/gis/shared/parcels.shp', name: 'parcels.shp', kind: 'file', byteSize: 1024 },
  ];
  const transport = new MockControlTransport({ routes: {
    'agent.sessions.list': { items: [projectAgent('first', '/gis/shared')] },
    'agent.session.workspace.read': noRun,
    'agent.session.workspace.list': (request: ControlRequest) => {
      const path = request.query?.path;
      if (path === '/gis/shared') return { items: entries, truncated: true };
      if (path === '/gis/shared/empty') return { items: [] };
      throw new Error('目录不可读取');
    },
  } });
  show(transport);
  await waitFor(() => expect(seenMap.mock.lastCall?.[0].workspaceFilesNotice).toContain('部分目录未能读取'));
  expect(seenMap.mock.lastCall?.[0].workspaceFiles).toEqual(entries);
  expect(seenMap.mock.lastCall?.[0].workspaceFilesIncomplete).toBe(true);
  expect(transport.requests.some(item => item.request.pathId === 'agent.session.workspace.list' && item.request.query?.path === '/gis/shared/reference')).toBe(false);
});

it('ignores a delayed directory listing after switching to another project', async () => {
  let finishFirst!: (value: unknown) => void;
  const delayed = new Promise(resolve => { finishFirst = resolve; });
  const transport = new MockControlTransport({ routes: {
    'agent.sessions.list': { items: [projectAgent('first', '/gis/first', 2), projectAgent('second', '/gis/second')] },
    'agent.session.workspace.read': noRun,
    'agent.session.workspace.list': (request: ControlRequest) => request.query?.path === '/gis/first' ? delayed : { items: [{ path: '/gis/second/new.geojson', name: 'new.geojson', kind: 'file' }] },
  } });
  show(transport);
  await screen.findByTestId('original-agent');
  await waitFor(() => expect(transport.requests.some(item => item.request.pathId === 'agent.session.workspace.list' && item.request.query?.path === '/gis/first')).toBe(true));
  await userEvent.click(screen.getByRole('button', { name: '选择 GIS 项目与 Agent' }));
  await userEvent.selectOptions(screen.getByRole('combobox', { name: '已有 GIS 项目' }), '/gis/second');
  await userEvent.click(screen.getByRole('button', { name: '进入所选 Agent' }));
  await waitFor(() => expect(seenMap.mock.lastCall?.[0].workspaceFiles).toEqual([expect.objectContaining({ path: '/gis/second/new.geojson' })]));
  await act(async () => { finishFirst({ items: [{ path: '/gis/first/old.geojson', name: 'old.geojson', kind: 'file' }] }); await delayed; });
  expect(seenMap.mock.lastCall?.[0].workspaceKey).toBe('/gis/second');
  expect(seenMap.mock.lastCall?.[0].workspaceFiles).toEqual([expect.objectContaining({ path: '/gis/second/new.geojson' })]);
});

it('preserves the original SHP file and conversion run when creating its project layer', async () => {
  const feature = { type: 'Feature', id: 1, properties: {}, geometry: { type: 'Point', coordinates: [120, 30] } };
  const transport = new MockControlTransport({ routes: {
    'agent.sessions.list': { items: [projectAgent('first', '/gis/shared')] },
    'agent.session.workspace.read': noRun,
    'agent.session.workspace.list': { items: [] },
    'agent.session.command.invoke': (request: ControlRequest) => {
      const command = String((request.body as { command?: string })?.command ?? '');
      const name = command.slice(1).split(' ')[0];
      const result = name === 'earth-gis-export' ? { runId: 'import-1', outputs: [{ kind: 'vector', geojson: { type: 'FeatureCollection', features: [feature] } }] }
        : name === 'earth-layer-save' ? { layer: { id: 'parcels', name: 'parcels.shp', path: '.earth/layers/parcels.geojson', format: 'geojson', featureCount: 1, geometryTypes: ['Point'], revision: 1, visible: true, features: [feature] } }
        : { runs: [] };
      return { result: { schemaVersion: 'rag-ime.pi-package-command-result.v1', command: name, result } };
    },
  } });
  show(transport);
  await screen.findByTestId('original-agent');
  await act(async () => { await seenMap.mock.lastCall?.[0].onOpenFile({ path: '/gis/shared/data/parcels.shp', name: 'parcels.shp', kind: 'file' }); });
  const saved = transport.requests.map(item => (item.request.body as { command?: string })?.command).find(command => command?.startsWith('/earth-layer-save '));
  expect(saved).toBeDefined();
  expect(JSON.parse(saved!.slice('/earth-layer-save '.length))).toMatchObject({ source: { kind: 'file', path: 'data/parcels.shp', format: 'shp', runId: 'import-1' } });
});
