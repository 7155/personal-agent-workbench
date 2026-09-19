import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import { MockControlTransport } from '@/test/mock-transport';
import type { PawExtensionAppManifest } from '@/paw-os/extensions/types';
import App, { GIS_ACCEPTANCE_TASK } from './App';
import manifest from './pawos-app.json';
const { seen } = vi.hoisted(() => ({ seen: vi.fn() }));
vi.mock('@/paw-os/apps/PawSessionWorkspace', () => ({ sessionWorkspaceProjectionSlice: () => ({ activeTurnId: '' }), PawSessionWorkspace: (props: { recordId: string }) => { seen(props); return <div data-testid="original-agent">{props.recordId}</div>; } }));
vi.mock('./EarthMap', () => ({ EarthMap: ({onSelect}: {onSelect:(feature: unknown)=>void}) => <button onClick={()=>onSelect({type:'Feature',geometry:{type:'Point',coordinates:[120.1,30.2]},properties:{source:'user_selection'}})}>选择地图地点</button> }));
vi.mock('@/features/agent/file-preview/CodePreview', () => ({ CodePreview: ({ content }: { content: string }) => <pre>{content}</pre> }));
afterEach(() => { cleanup(); seen.mockClear(); });
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
