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
  await waitFor(() => expect(screen.getByRole('button', { name: '展开 Agent' })).toBeEnabled());
  return transport;
}

it('starts with the map and preserves the chosen split while selecting map objects', async () => {
  await showWorkspace();
  const user = userEvent.setup();
  expect(screen.getByRole('button', { name: /^地图$/ })).toHaveAttribute('aria-pressed', 'true');
  expect(screen.queryByRole('region', { name: 'Earth Engine JavaScript' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '地图＋代码' }));
  await user.click(screen.getByRole('button', { name: '选择地图对象' }));
  expect(screen.getByRole('region', { name: 'Earth Engine JavaScript' })).toBeVisible();
  expect(screen.getByRole('button', { name: '地图＋代码' })).toHaveAttribute('aria-pressed', 'true');
  expect(screen.getByLabelText('地图画布')).toBeVisible();
});

it('opens a report beside the map, allows focused reading, then restores or closes the preview', async () => {
  await showWorkspace();
  const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: '打开成果报告' }));
  expect(await screen.findByTitle('report.html')).toHaveAttribute('srcdoc', '<h1>验证后的分析成果</h1>');
  expect(screen.getByLabelText('地图画布')).toBeVisible();
  expect(screen.getByRole('button', { name: '地图＋代码' })).toHaveAttribute('aria-pressed', 'true');
  await user.click(screen.getByRole('button', { name: '最大化文件预览' }));
  expect(screen.getByLabelText('地图画布')).not.toBeVisible();
  await user.click(screen.getByRole('button', { name: '恢复地图与文件分屏' }));
  expect(screen.getByLabelText('地图画布')).toBeVisible();
  await user.click(screen.getByRole('button', { name: '关闭文件预览' }));
  expect(screen.queryByRole('region', { name: 'Earth Engine JavaScript' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '地图＋代码' }));
  expect(screen.getByTitle('report.html')).toBeVisible();
});

it('collapses the Agent without discarding its in-progress draft', async () => {
  const transport = await showWorkspace();
  const user = userEvent.setup();
  expect(screen.queryByRole('region', { name: 'Agent 工作栏' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '展开 Agent' }));
  const draft = screen.getByRole('textbox', { name: 'Agent 草稿' });
  await user.type(draft, '，避开河流');
  expect(screen.getByRole('button', { name: '收起 Agent' })).toHaveAttribute('aria-expanded', 'true');
  await user.click(screen.getByRole('button', { name: '收起 Agent' }));
  await user.click(screen.getByRole('button', { name: '展开 Agent' }));
  expect(screen.getByRole('textbox', { name: 'Agent 草稿' })).toHaveValue('保留的分析目标，避开河流');
  expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.prompt')).toBe(false);
});


it('resizes the Agent with keyboard, remembers width, and resets on double click', async () => {
  localStorage.removeItem('paw-earth-agent-width');
  await showWorkspace();
  await userEvent.click(screen.getByRole('button',{name:'展开 Agent'}));
  const handle=screen.getByRole('separator',{name:'调整 Agent 宽度'});
  fireEvent.keyDown(handle,{key:'ArrowRight',shiftKey:true});
  expect(handle).toHaveAttribute('aria-valuenow','392');
  expect(localStorage.getItem('paw-earth-agent-width')).toBe('392');
  fireEvent.doubleClick(handle);
  expect(handle).toHaveAttribute('aria-valuenow','360');
  await userEvent.click(screen.getByRole('button',{name:'收起 Agent'}));
  expect(screen.queryByRole('separator',{name:'调整 Agent 宽度'})).not.toBeInTheDocument();
  localStorage.removeItem('paw-earth-agent-width');
});
