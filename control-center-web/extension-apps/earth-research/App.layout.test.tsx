import { PawOsDesktopProvider } from '@/features/paw-os/surface-context';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import { MockControlTransport } from '@/test/mock-transport';
import type { PawExtensionAppManifest } from '@/paw-os/extensions/types';
import type { ControlRequest } from '@/platform/transport';
import App from './App';
import manifest from './pawos-app.json';

vi.mock('@/paw-os/apps/PawSessionWorkspace', () => ({
  sessionWorkspaceProjectionSlice: () => ({ activeTurnId: '' }),
  PawSessionWorkspace: () => <textarea aria-label="Agent 草稿" defaultValue="保留的分析目标" />,
}));
vi.mock('./EarthMap', () => ({ EarthMap: (props: {
  onSelect: (feature: unknown) => void;
  onOpenFile: (file: { path: string; name: string; kind: 'file' }) => void;
}) => <div aria-label="地图画布">
  <button onClick={() => props.onSelect({ type: 'Feature', geometry: { type: 'Point', coordinates: [120, 30] }, properties: {} })}>选择地图对象</button>
  <button onClick={() => props.onOpenFile({ path: '/work/report.html', name: 'report.html', kind: 'file' })}>打开成果报告</button>
</div> }));
vi.mock('@/features/agent/file-preview/CodePreview', () => ({ CodePreview: ({ content }: { content: string }) => <pre>{content}</pre> }));

afterEach(cleanup);

async function showWorkspace() {
  const transport = new MockControlTransport({ routes: {
    'agent.sessions.list': { items: [{ id: 'earth-layout', mode: 'assistant', title: 'GIS 项目', updatedAtMs: 1, surfaceKind: 'extension_app', ownerAppId: manifest.id, surfaceKey: 'analysis', workspaceRoots: ['/work'] }] },
    'agent.session.workspace.list': { items: [] },
    'agent.session.workspace.read': (request: ControlRequest) => {
      const path = request.query?.path;
      if (path !== '/work/report.html') throw new Error('path does not exist in the authorized workspace');
      const content = '<h1>验证后的分析成果</h1>';
      const byteSize = new TextEncoder().encode(content).length;
      return { ok: true, path, content, byteSize, loadedBytes: byteSize, nextOffset: byteSize, truncated: false, resourceRevision: `sha256:${'0'.repeat(64)}`, editability: { editable: false } };
    },
  } });
  render(<ControlTransportProvider transport={transport}><TooltipProvider><App manifest={manifest as PawExtensionAppManifest} /></TooltipProvider></ControlTransportProvider>);
  await waitFor(() => expect(screen.getByLabelText('当前项目文件夹')).toHaveAttribute('data-path', '/work'));
  return transport;
}

it('starts with the map and preserves the chosen split while selecting map objects', async () => {
  await showWorkspace();
  const user = userEvent.setup();
  expect(document.querySelector('.earth-panels')).toHaveAttribute('data-view', 'map');
  expect(screen.queryByRole('region', { name: 'Earth Engine JavaScript' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '更多' }));
  await user.click(screen.getByRole('button', { name: '地图＋代码' }));
  await user.click(screen.getByRole('button', { name: '选择地图对象' }));
  expect(screen.getByRole('region', { name: 'Earth Engine JavaScript' })).toBeVisible();
  expect(document.querySelector('.earth-panels')).toHaveAttribute('data-view', 'split');
  expect(screen.getByLabelText('地图画布')).toBeVisible();
});

it('opens a report beside the map, allows focused reading, then restores or closes the preview', async () => {
  await showWorkspace();
  const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: '打开成果报告' }));
  expect(await screen.findByTitle('report.html')).toHaveAttribute('srcdoc', '<h1>验证后的分析成果</h1>');
  expect(screen.getByLabelText('地图画布')).toBeVisible();
  expect(document.querySelector('.earth-panels')).toHaveAttribute('data-view', 'split');
  await user.click(screen.getByRole('button', { name: '最大化文件预览' }));
  expect(screen.getByLabelText('地图画布')).not.toBeVisible();
  await user.click(screen.getByRole('button', { name: '恢复地图与文件分屏' }));
  expect(screen.getByLabelText('地图画布')).toBeVisible();
  await user.click(screen.getByRole('button', { name: '关闭文件预览' }));
  expect(screen.queryByRole('region', { name: 'Earth Engine JavaScript' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '更多' }));
  await user.click(screen.getByRole('button', { name: '地图＋代码' }));
  expect(screen.getByTitle('report.html')).toBeVisible();
});

it('collapses the Agent without discarding its in-progress draft', async () => {
  const transport = await showWorkspace();
  const user = userEvent.setup();
  expect(screen.queryByRole('region', { name: 'Agent 工作栏' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Agent' }));
  const draft = screen.getByRole('textbox', { name: 'Agent 草稿' });
  await user.type(draft, '，避开河流');
  expect(screen.getByRole('button', { name: 'Agent' })).toHaveAttribute('aria-expanded', 'true');
  await user.click(screen.getByRole('button', { name: 'Agent' }));
  await user.click(screen.getByRole('button', { name: 'Agent' }));
  expect(screen.getByRole('textbox', { name: 'Agent 草稿' })).toHaveValue('保留的分析目标，避开河流');
  expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.prompt')).toBe(false);
});


it('resizes the Agent with keyboard, remembers width, and resets on double click', async () => {
  localStorage.removeItem('paw-earth-agent-width');
  await showWorkspace();
  await userEvent.click(screen.getByRole('button',{name:'Agent'}));
  const handle=screen.getByRole('separator',{name:'调整 Agent 宽度'});
  fireEvent.keyDown(handle,{key:'ArrowRight',shiftKey:true});
  expect(handle).toHaveAttribute('aria-valuenow','452');
  expect(localStorage.getItem('paw-earth-agent-width')).toBe('452');
  fireEvent.doubleClick(handle);
  expect(handle).toHaveAttribute('aria-valuenow','420');
  await userEvent.click(screen.getByRole('button',{name:'Agent'}));
  expect(screen.queryByRole('separator',{name:'调整 Agent 宽度'})).not.toBeInTheDocument();
  localStorage.removeItem('paw-earth-agent-width');
});

it('opens the project HTML report inside the OS result window',async()=>{
 const openWindow=vi.fn();const html='<!doctype html><title>地理分析报告</title><h1>真实成果</h1>';
 const transport=new MockControlTransport({routes:{
  'agent.sessions.list':{items:[{id:'earth-report',title:'GIS 项目',mode:'assistant',updatedAtMs:1,surfaceKind:'extension_app',ownerAppId:manifest.id,surfaceKey:'analysis',workspaceRoots:['/work']}]},
  'agent.session.workspace.list':{items:[{path:'/work/report.html',name:'report.html',kind:'file'}]},
  'agent.session.workspace.read':(request:ControlRequest)=>{if(request.query?.path!=='/work/report.html')throw new Error('not found');const size=new TextEncoder().encode(html).length;return{ok:true,path:'/work/report.html',content:html,byteSize:size,loadedBytes:size,nextOffset:size,truncated:false,resourceRevision:`sha256:${'0'.repeat(64)}`,editability:{editable:false}};},
 }});
 render(<PawOsDesktopProvider openWindow={openWindow}><ControlTransportProvider transport={transport}><TooltipProvider><App manifest={manifest as PawExtensionAppManifest}/></TooltipProvider></ControlTransportProvider></PawOsDesktopProvider>);
 await waitFor(()=>expect(screen.getByLabelText('当前项目文件夹')).toHaveAttribute('data-path','/work'));
 await userEvent.click(screen.getByRole('button',{name:'报告'}));
 await userEvent.click(await screen.findByRole('button',{name:/report.html/}));
 await waitFor(()=>expect(openWindow).toHaveBeenCalledWith(expect.objectContaining({appId:manifest.id,target:expect.objectContaining({kind:'result',resultKind:'html',title:'地理分析报告',content:html})})));
});
