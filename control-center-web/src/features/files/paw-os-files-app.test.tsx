import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport, type MockRouteHandler } from '@/test/mock-transport';
import { PawOsFilesApp } from './PawOsFilesApp';
import { MotionActivityBoundary } from '@/design/motion';
import { PawWindowFrame } from '@/paw-os/shell/PawWindowLayer';
import filesCss from './paw-os-files-app.css?raw';

afterEach(() => { cleanup(); window.localStorage.clear(); });

describe('PawOsFilesApp', () => {
  it('opens an absolute file outside workspaces when the linked Session is missing', async () => {
    const path = '/Volumes/Field notes/observations.md';
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': { ok: true, items: [] },
      'files.list': (request: ControlRequest) => ({ ok: true, scope: 'local', path: request.query?.path ? '/Volumes/Field notes' : '/home/qa', homePath: '/home/qa', selectedPath: request.query?.path === path ? path : '', items: [{ path, name: 'observations.md', kind: 'file' }] }),
      'files.read': { ok: true, scope: 'local', requestedPath: path, path, content: '# Field observations', byteSize: 20, nextOffset: 20, editability: { editable: false, reason: '本机预览' } },
    } });
    renderApp(transport, <PawOsFilesApp initialRoute={`/files?session=deleted-session&path=${encodeURIComponent(path)}`} />);
    expect(await screen.findByRole('heading', { name: 'Field observations' })).toBeInTheDocument();
    expect(screen.queryByText('工作区暂时不可用')).not.toBeInTheDocument();
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.workspace.read')).toBe(false);
    expect(screen.getByRole('textbox', { name: '文件或文件夹路径' })).toHaveValue('/Volumes/Field notes');
  });

  it('navigates home, arbitrary folders and the filesystem root without a Session', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': { ok: true, items: [] },
      'files.list': (request: ControlRequest) => ({ ok: true, scope: 'local', path: request.query?.path || '/home/qa', homePath: '/home/qa', items: [] }),
    } });
    renderApp(transport, <PawOsFilesApp />);
    const address = screen.getByRole('textbox', { name: '文件或文件夹路径' });
    await waitFor(() => expect(address).toHaveValue('/home/qa'));
    await user.clear(address); await user.type(address, '/Volumes'); await user.click(screen.getByRole('button', { name: '打开路径' }));
    await waitFor(() => expect(screen.getByText('/Volumes')).toBeInTheDocument());
    await user.click(screen.getByRole('button', { name: '上一级文件夹' }));
    await waitFor(() => expect(address).toHaveValue('/'));
    expect(screen.getByRole('button', { name: '上一级文件夹' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: '打开主目录' }));
    await waitFor(() => expect(address).toHaveValue('/home/qa'));
    expect(transport.requests.filter(({ request }) => request.pathId === 'files.list').every(({ request }) => !request.query?.sessionId)).toBe(true);
  });

  it('ignores a slow folder result after the user returns home', async () => {
    const user = userEvent.setup(); let settle!: (response: unknown) => void;
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': { ok: true, items: [] },
      'files.list': (request: ControlRequest) => request.query?.path === '/slow' ? new Promise((resolve) => { settle = resolve; }) : { ok: true, scope: 'local', path: '/home/qa', homePath: '/home/qa', items: [] },
    } });
    renderApp(transport, <PawOsFilesApp />);
    const address = screen.getByRole('textbox', { name: '文件或文件夹路径' });
    await waitFor(() => expect(address).toHaveValue('/home/qa'));
    await user.clear(address); await user.type(address, '/slow'); await user.click(screen.getByRole('button', { name: '打开路径' }));
    await user.click(screen.getByRole('button', { name: '打开主目录' }));
    await waitFor(() => expect(address).toHaveValue('/home/qa'));
    await act(async () => settle({ ok: true, scope: 'local', path: '/slow', items: [] }));
    expect(address).toHaveValue('/home/qa');
    expect(screen.queryByText('/slow')).not.toBeInTheDocument();
  });

  it('loads further directory pages and retains existing entries without duplicates', async () => {
    const user = userEvent.setup();
    const first = { path: '/home/qa/first.txt', name: 'first.txt', kind: 'file' };
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': { ok: true, items: [] },
      'files.list': (request: ControlRequest) => ({ ok: true, scope: 'local', path: '/home/qa', homePath: '/home/qa', items: request.query?.offset ? [first, { path: '/home/qa/last.txt', name: 'last.txt', kind: 'file' }] : [first], truncated: !request.query?.offset, nextOffset: request.query?.offset ? null : 240 }),
    } });
    renderApp(transport, <PawOsFilesApp />);
    await user.click(await screen.findByRole('button', { name: '继续加载目录' }));
    expect(await screen.findByRole('treeitem', { name: '打开文件 last.txt' })).toBeInTheDocument();
    expect(screen.getAllByRole('treeitem', { name: '打开文件 first.txt' })).toHaveLength(1);
    expect(screen.queryByRole('button', { name: '继续加载目录' })).not.toBeInTheDocument();
    expect(transport.requests.some(({ request }) => request.query?.offset === 240)).toBe(true);
  });

  it.each([false, true])('keeps the selected file and directory state through manual collapse (narrow: %s)', async (narrowWindow) => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': { ok: true, activeSessionId: 'session-work', items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }] },
      'files.list': { ok: true, path: '/workspace/paw', items: [{ path: '/workspace/paw/guide.md', name: 'guide.md', kind: 'file' }] },
      'files.read': { ok: true, path: '/workspace/paw/guide.md', content: '# Saved preview', byteSize: 15, truncated: false },
    } });
    renderApp(transport, <PawOsFilesApp />);
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 guide.md' }));
    expect(await screen.findByRole('heading', { name: 'Saved preview' })).toBeInTheDocument();
    const reads = transport.requests.filter(({ request }) => request.pathId === 'files.read').length;
    const narrow = narrowWindow ? applyNarrowLayout() : null;
    try {
      if (narrow) {
        act(() => { window.dispatchEvent(new Event('resize')); });
        await user.click(screen.getByRole('button', { name: '展开文件目录' }));
        expect(screen.getByRole('treeitem', { name: '打开文件 guide.md' })).toHaveAttribute('data-selected', 'true');
      }
      await user.click(screen.getByRole('button', { name: '收起文件目录' }));
      expect(screen.queryByRole('tree', { name: '项目文件' })).not.toBeInTheDocument();
      expect(screen.getByRole('heading', { name: 'Saved preview' })).toBeVisible();
      const toggle = screen.getByRole('button', { name: '展开文件目录' });
      expect(toggle).toHaveAttribute('aria-expanded', 'false');
      expect(document.getElementById(toggle.getAttribute('aria-controls')!)).toHaveAttribute('hidden');
      await user.click(toggle);
      expect(screen.getByRole('treeitem', { name: '打开文件 guide.md' })).toHaveAttribute('data-selected', 'true');
      expect(transport.requests.filter(({ request }) => request.pathId === 'files.read')).toHaveLength(reads);
      if (narrow) {
        await user.click(screen.getByRole('treeitem', { name: '打开文件 guide.md' }));
        expect(screen.getByRole('heading', { name: 'Saved preview' })).toBeVisible();
      }
    } finally { narrow?.remove(); }
  });
  it('keeps independent browsing available when the Session catalog fails', async () => {
    const user = userEvent.setup(); let offline = true;
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': () => { if (offline) throw new Error('offline'); return { ok: true, items: [] }; },
      'files.list': { ok: true, scope: 'local', path: '/home', items: [{ path: '/home/readme.md', name: 'readme.md', kind: 'file' }] },
      'files.read': { ok: true, scope: 'local', requestedPath: '/home/readme.md', path: '/home/readme.md', content: '# Local file', byteSize: 12, nextOffset: 12 },
    } });
    renderApp(transport, <PawOsFilesApp />);
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 readme.md' }));
    expect(await screen.findByRole('heading', { name: 'Local file' })).toBeInTheDocument();
    expect(screen.getByText('Session 快捷入口暂时无法读取，本机文件仍可浏览。')).toBeInTheDocument();
    offline = false; await user.click(screen.getByRole('button', { name: '重试 Session 列表' }));
    await waitFor(() => expect(screen.queryByText('Session 快捷入口暂时无法读取，本机文件仍可浏览。')).not.toBeInTheDocument());
    expect(screen.getByRole('heading', { name: 'Local file' })).toBeInTheDocument();
  });
  it('keeps its accessible title out of the workspace grid at every window width', () => {
    const transport = new MockControlTransport({ routes: { 'agent.sessions.list': { ok: true, items: [] } } });
    renderApp(transport, <PawOsFilesApp />);

    expect(screen.getByRole('heading', { name: '文件', level: 1 })).toHaveClass('paw-files-app__title');
    expect(filesCss).toMatch(/\.paw-files-app__title\s*\{[^}]*position:\s*absolute;/s);
    expect(filesCss).not.toMatch(/transition:\s*width/);
  });

  it('frames itself as an App window: fixed chrome bands around one scrolling workspace', async () => {
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }],
        },
        'files.list': { ok: true, path: '/workspace/paw', items: [] },
      },
    });

    const { container } = renderApp(transport, <PawOsFilesApp />);
    const app = container.querySelector('.paw-files-app') as HTMLElement;
    await screen.findByRole('tree', { name: '项目文件' });

    // Scope bar, workspace, status bar — no extra wrapper between the window
    // and the band that has to absorb the resize.
    expect([...app.children].map((child) => child.className)).toEqual([
      'paw-files-app__title',
      'paw-files-app__toolbar',
      'paw-files-location-disclosure',
      'paw-files-app__workspace',
      'paw-files-statusbar',
    ]);
    // The chrome bands keep their own height and the workspace takes the
    // rest, with no row template restated per band combination.
    expect(filesCss).toMatch(/\.paw-files-app\s*\{[^}]*display:\s*flex;[^}]*flex-direction:\s*column;/s);
    expect(filesCss).toMatch(/\.paw-files-app > :is\([^)]*\)\s*\{[^}]*flex:\s*0 0 auto;/s);
    expect(filesCss).toMatch(/\.paw-files-app__workspace\s*\{[^}]*flex:\s*1 1 auto;[^}]*min-height:\s*0;/s);
    expect(filesCss).not.toMatch(/grid-template-rows:\s*[^;]*\b\d+px\b[^;]*;/);
    // Both panes own a local scroll instead of growing the window.
    expect(filesCss).toMatch(/\.paw-files-tree__scroll\s*\{[^}]*overflow:\s*auto;/s);
    expect(filesCss).toMatch(/\.paw-files-preview__body\s*\{[^}]*overflow:\s*auto;/s);
  });

  it('keeps every scope-bar and reader control on the shared 32px control height', async () => {
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }],
        },
        'files.list': { ok: true, path: '/workspace/paw', items: [] },
      },
    });

    renderApp(transport, <PawOsFilesApp />);
    await screen.findByRole('tree', { name: '项目文件' });

    // The shared control language owns the scale; Files only re-points it,
    // including on the scope bar, which window chrome portals out of the App.
    expect(filesCss).toMatch(/\.paw-files-app,\s*\n\.paw-files-app__toolbar \{[^}]*--paw-files-control-h: var\(--paw-control-h, 32px\);/s);
    for (const declaration of [
      /\.paw-files-scope,\s*\n\.paw-files-filter \{[^}]*height: var\(--paw-files-control-h\);/s,
      /\.paw-files-preview__action\s*\{[^}]*width: var\(--paw-files-control-h\);[^}]*height: var\(--paw-files-control-h\);/s,
      /\.paw-files-preview__back\s*\{[^}]*height: var\(--paw-files-control-h\);/s,
    ]) expect(filesCss).toMatch(declaration);
    // Control surfaces stay opaque so their ink never lands on an unknown backdrop.
    expect(filesCss).toContain('--paw-files-control-bg: var(--paw-control-bg, #fff);');
    expect(filesCss).not.toMatch(/(?:min-)?height:\s*30px/);

    // The refresh action keeps an accessible name for the width where its
    // visible label retires, so the icon-only state is never anonymous.
    expect(screen.getByRole('button', { name: '刷新文件' })).toBeInTheDocument();
    expect(filesCss).toMatch(/@container paw-files-tools \(max-width: 420px\)[\s\S]*?\.paw-files-refresh > span\s*\{\s*display:\s*none;/s);
  });

  it('keeps the open file, its selection, and expanded directories across a layout swap', async () => {
    // jsdom cannot evaluate @container queries, so this drives the exact
    // <=620px rules from paw-os-files-app.css on and off the way a
    // continuous window drag-resize would cross the breakpoint.
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }],
        },
        'files.list': (request: ControlRequest) => {
          const path = String(request.query?.path ?? '');
          if (path === '/workspace/paw/docs') return {
            ok: true,
            path,
            items: [{ path: `${path}/guide.md`, name: 'guide.md', kind: 'file', byteSize: 42 }],
          };
          return { ok: true, path, items: [{ path: '/workspace/paw/docs', name: 'docs', kind: 'directory' }] };
        },
        'files.read': (request: ControlRequest) => ({
          ok: true,
          path: request.query?.path,
          content: '# Guide',
          byteSize: 7,
          truncated: false,
        }),
      },
    });

    const { container } = renderApp(transport, <PawOsFilesApp />);
    await user.click(await screen.findByRole('treeitem', { name: '展开目录 docs' }));
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 guide.md' }));
    expect(await screen.findByRole('heading', { name: 'guide.md', level: 2 })).toBeInTheDocument();

    const narrow = applyNarrowLayout();
    try {
      // The window observer that notices the crossing is stubbed out in
      // jsdom; a viewport resize reaches the same handler, because the shell
      // re-clamps every window against the viewport.
      window.dispatchEvent(new Event('resize'));

      // Crossing into the single-pane layout hides the rail with CSS only —
      // it stays mounted, so nothing about the selection is discarded.
      expect(screen.getByRole('heading', { name: 'guide.md', level: 2 })).toBeInTheDocument();
      expect(container.querySelector('.paw-files-tree')).not.toBeNull();
      expect(container.querySelector('.paw-files-tree__row[data-selected]')).toHaveAttribute('title', '/workspace/paw/docs/guide.md');
      // Keyboard focus follows the layout instead of falling out of the App
      // with the rail that used to hold it.
      await waitFor(() => expect(screen.getByRole('button', { name: '返回文件列表' })).toHaveFocus());
    } finally {
      narrow.remove();
    }

    // Widening back to the split layout restores the rail beside the very
    // same open file, still expanded and still marked as the selection.
    expect(screen.getByRole('heading', { name: 'guide.md', level: 2 })).toBeInTheDocument();
    expect(screen.getByRole('treeitem', { name: '收起目录 docs' })).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('treeitem', { name: '打开文件 guide.md' })).toHaveAttribute('data-selected', 'true');
    expect(screen.getByRole('button', { name: '复制文件内容' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '复制文件路径' })).toBeInTheDocument();
  });

  it('uses a Session workspace as a shortcut and previews through independent file reading', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [
            {
              id: 'session-work',
              title: 'PAWOS 迁移',
              updatedAtMs: 1,
              workspaceRoots: ['/workspace/paw'],
              status: 'idle',
            },
            {
              id: 'session-empty',
              title: '无项目对话',
              updatedAtMs: 0,
              workspaceRoots: [],
              status: 'idle',
            },
          ],
        },
        'files.list': {
          ok: true,
          path: '/workspace/paw',
          items: [
            { path: '/workspace/paw/docs', name: 'docs', kind: 'directory' },
            { path: '/workspace/paw/AGENTS.md', name: 'AGENTS.md', kind: 'file', byteSize: 128 },
          ],
        },
        'files.read': {
          ok: true,
          path: '/workspace/paw/AGENTS.md',
          content: '# Project guide\nRead docs before work.',
          byteSize: 38,
          truncated: false,
        },
      },
    });

    renderApp(transport, <PawOsFilesApp />);

    expect(await screen.findByRole('heading', { name: '文件', level: 1 })).toBeInTheDocument();
    expect(screen.getByText('/workspace/paw')).toBeInTheDocument();
    expect(screen.queryByText('/Users')).not.toBeInTheDocument();
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 AGENTS.md' }));
    expect(await screen.findByRole('heading', { name: 'AGENTS.md', level: 2 })).toBeInTheDocument();
    expect(screen.getByText('Project guide')).toBeInTheDocument();
    expect(screen.getByText(/已选 AGENTS\.md · 38 B/)).toBeInTheDocument();
    expect(screen.getByText('Session 工作区快捷入口 · 本机读取')).toBeInTheDocument();

    await waitFor(() => expect(transport.requests.some((call) => (
      call.request.pathId === 'files.read'
      && call.request.query?.sessionId === 'session-work'
      && call.request.query?.path === '/workspace/paw/AGENTS.md'
    ))).toBe(true));
  });

  it('opens a root-relative file path from a Session evidence link', async () => {
    const relativePath = 'docs/room-runtime-handoff.md';
    const absolutePath = `/workspace/paw/${relativePath}`;
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }],
        },
        'files.list': (request: ControlRequest) => {
          const path = String(request.query?.path ?? '');
          if (path === '/workspace/paw/docs') {
            return { ok: true, path, items: [{ path: absolutePath, name: 'room-runtime-handoff.md', kind: 'file', byteSize: 32 }] };
          }
          return { ok: true, path, items: [{ path: '/workspace/paw/docs', name: 'docs', kind: 'directory' }] };
        },
        'files.read': (request: ControlRequest) => ({
          ok: true,
          path: request.query?.path,
          content: '# Room handoff',
          byteSize: 14,
          truncated: false,
        }),
      },
    });

    renderApp(transport, <PawOsFilesApp initialRoute={`/files?session=session-work&path=${encodeURIComponent(relativePath)}`} />);

    expect(await screen.findByRole('heading', { name: 'room-runtime-handoff.md', level: 2 })).toBeInTheDocument();
    expect(await screen.findByText('Room handoff')).toBeInTheDocument();
    expect(transport.requests.some((call) => (
      call.request.pathId === 'files.read'
      && call.request.query?.sessionId === 'session-work'
      && call.request.query?.path === absolutePath
    ))).toBe(true);
  });

  it('binds a new explicit deep link to its read-only Session before any file read', async () => {
    let completeList!: (value: unknown) => void;
    let lists = 0;
    const transport = scopedFilesTransport(() => ++lists === 1 ? scopedSessions() : new Promise((resolve) => { completeList = resolve; }));
    const view = renderApp(transport, <PawOsFilesApp initialRoute={filesRoute('writer', 'notes.md')} />);
    expect(await screen.findByRole('button', { name: '编辑文本' })).toBeInTheDocument();
    view.rerender(<PawOsFilesApp initialRoute={filesRoute('reader', 'docs/plan.md')} />);
    await waitFor(() => expect(lists).toBe(2));
    expect(screen.queryByRole('button', { name: '编辑文本' })).not.toBeInTheDocument();
    expect(transport.requests.some(({ request }) => request.pathId === 'files.read'
      && request.query?.path === '/workspace/paw/docs/plan.md' && request.query?.sessionId !== 'reader')).toBe(false);
    await act(async () => completeList(scopedSessions()));
    expect(await screen.findByRole('heading', { name: 'reader plan.md', level: 1 })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: '选择文件所属 Session' })).toHaveValue('reader');
    expect(screen.queryByRole('button', { name: '编辑文本' })).not.toBeInTheDocument();
    expect(screen.getByText('此 Session 只读')).toBeInTheDocument();
    expect(transport.requests.filter(({ request }) => request.pathId === 'files.read' && request.query?.path === '/workspace/paw/docs/plan.md')
      .every(({ request }) => request.query?.sessionId === 'reader')).toBe(true);
  });

  it.each(['missing', 'unbound'])('never borrows the previous Session for an unavailable %s deep link', async (sessionId) => {
    const user = userEvent.setup();
    const transport = scopedFilesTransport();
    const view = renderApp(transport, <PawOsFilesApp initialRoute={filesRoute('writer', 'notes.md')} />);
    expect(await screen.findByRole('button', { name: '编辑文本' })).toBeInTheDocument();
    view.rerender(<PawOsFilesApp initialRoute={filesRoute(sessionId, 'docs/plan.md')} />);
    await waitFor(() => expect(transport.requests.filter(({ request }) => request.pathId === 'agent.sessions.list')).toHaveLength(2));
    await waitFor(() => expect(screen.getByRole('combobox', { name: '选择文件所属 Session' })).toBeEnabled());
    expect(transport.requests.some(({ request }) => request.pathId === 'files.read' && request.query?.path === '/workspace/paw/docs/plan.md')).toBe(false);
    expect(screen.getByRole('combobox', { name: '选择文件所属 Session' })).toHaveValue(sessionId === 'missing' ? '' : 'unbound');
    expect(screen.queryByRole('button', { name: '编辑文本' })).not.toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '文件或文件夹路径' })).toBeEnabled();
    expect(screen.getByRole('button', { name: '打开主目录' })).toBeEnabled();
    await user.selectOptions(screen.getByRole('combobox', { name: '选择文件所属 Session' }), 'writer');
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 notes.md' }));
    expect(await screen.findByRole('heading', { name: 'writer notes.md', level: 1 })).toBeInTheDocument();
    expect(transport.requests.some(({ request }) => request.pathId === 'files.read' && request.query?.path === '/workspace/paw/docs/plan.md')).toBe(false);
  });

  it('preserves a manual Session through refresh and applies the next explicit file intent even when its Session id is unchanged', async () => {
    const user = userEvent.setup();
    const transport = scopedFilesTransport();
    const view = renderApp(transport, <PawOsFilesApp initialRoute={filesRoute('writer', 'notes.md')} />);
    expect(await screen.findByRole('heading', { name: 'writer notes.md', level: 1 })).toBeInTheDocument();
    await user.selectOptions(screen.getByRole('combobox', { name: '选择文件所属 Session' }), 'reader');
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 notes.md' }));
    expect(await screen.findByRole('heading', { name: 'reader notes.md', level: 1 })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: '刷新文件' })).toBeEnabled());
    await user.click(screen.getByRole('button', { name: '刷新文件' }));
    expect(await screen.findByRole('heading', { name: 'reader notes.md', level: 1 })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: '选择文件所属 Session' })).toHaveValue('reader');
    view.rerender(<PawOsFilesApp initialRoute={filesRoute('writer', 'docs/next.md')} />);
    expect(await screen.findByRole('heading', { name: 'writer next.md', level: 1 })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: '选择文件所属 Session' })).toHaveValue('writer');
    expect(transport.requests.filter(({ request }) => request.pathId === 'files.read' && request.query?.path === '/workspace/paw/docs/next.md')
      .every(({ request }) => request.query?.sessionId === 'writer')).toBe(true);
  });

  it('ignores an older Session catalog response after a newer deep link has resolved', async () => {
    let finishOld!: (value: unknown) => void;
    let lists = 0;
    const transport = scopedFilesTransport(() => ++lists === 2 ? new Promise((resolve) => { finishOld = resolve; }) : scopedSessions());
    const view = renderApp(transport, <PawOsFilesApp initialRoute={filesRoute('writer', 'notes.md')} />);
    expect(await screen.findByRole('heading', { name: 'writer notes.md', level: 1 })).toBeInTheDocument();
    view.rerender(<PawOsFilesApp initialRoute={filesRoute('reader', 'docs/older.md')} />);
    await waitFor(() => expect(lists).toBe(2));
    view.rerender(<PawOsFilesApp initialRoute={filesRoute('writer', 'docs/latest.md')} />);
    expect(await screen.findByRole('heading', { name: 'writer latest.md', level: 1 })).toBeInTheDocument();
    await act(async () => finishOld({ ok: true, activeSessionId: 'reader', items: scopedSessions().items.filter((item) => item.id === 'reader') }));
    expect(screen.getByRole('combobox', { name: '选择文件所属 Session' })).toHaveValue('writer');
    expect(screen.getByRole('heading', { name: 'writer latest.md', level: 1 })).toBeInTheDocument();
  });

  it('moves editing location and collaboration into one file-tools popover without replacing the editor', async () => {
    const user = userEvent.setup();
    const style = applyNarrowLayout();
    try {
      const transport = savedMetadataTransport(() => savedMetadataReceipt());
      renderApp(transport, <PawOsFilesApp />);
      await user.click(await screen.findByRole('treeitem', { name: '打开文件 notes.md' }));
      expect(screen.queryByRole('button', { name: '文件工具' })).not.toBeInTheDocument();
      await user.click(await screen.findByRole('button', { name: '编辑文本' }));
      const input = await screen.findByRole('textbox', { name: '编辑 notes.md' }) as HTMLTextAreaElement;
      fireEvent.change(input, { target: { value: 'unsaved tools draft' } });
      input.setSelectionRange(3, 7); input.scrollTop = 120;
      const body = input.closest('.paw-files-preview__body')!;
      expect(body.querySelector('.paw-files-location')).toBeNull();
      expect(body.querySelector('.paw-files-collaboration')).toBeNull();
      const trigger = screen.getByRole('button', { name: '文件工具' });
      await user.click(trigger);
      const panel = await screen.findByRole('dialog', { name: '文件工具' });
      const location = within(panel).getByRole('textbox', { name: '文件或文件夹路径' });
      expect(location).toHaveFocus();
      expect(within(panel).getByRole('button', { name: '协作与访问' })).toBeInTheDocument();
      expect(within(panel).getByRole('button', { name: '复制编辑内容' })).toBeInTheDocument();
      expect(within(panel).getByRole('button', { name: '复制文件路径' })).toBeInTheDocument();
      expect(screen.getAllByRole('button', { name: '复制文件路径' })).toHaveLength(1);
      expect(panel.closest('.paw-files-preview__body')).toBeNull();
      expect(screen.getByRole('textbox', { name: '编辑 notes.md' })).toBe(input);
      expect(input).toHaveValue('unsaved tools draft');
      expect([input.selectionStart, input.selectionEnd, input.scrollTop]).toEqual([3, 7, 120]);
      expect(screen.getByRole('button', { name: '保存文件' })).toBeEnabled();
      await user.keyboard('{Escape}');
      await waitFor(() => expect(screen.queryByRole('dialog', { name: '文件工具' })).not.toBeInTheDocument());
      await waitFor(() => expect(trigger).toHaveFocus());
      expect(screen.getByRole('textbox', { name: '编辑 notes.md' })).toBe(input);
      await user.click(trigger);
      await screen.findByRole('dialog', { name: '文件工具' });
      await user.click(document.body);
      await waitFor(() => expect(screen.queryByRole('dialog', { name: '文件工具' })).not.toBeInTheDocument());
      await waitFor(() => expect(trigger).toHaveFocus());
      expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.workspace.save')).toBe(false);
    } finally { style.remove(); }
  });

  it.each(['neutral-content', 'own-shell'])('returns tools focus after %s clicks within both actual negative-tabindex desktop and shell boundaries', async (target) => {
    const user = userEvent.setup();
    const transport = savedMetadataTransport(() => savedMetadataReceipt());
    renderApp(transport, <main className="paw-desktop-viewport" tabIndex={-1}><section className="paw-window-shell" tabIndex={-1} aria-label="Files test window"><PawOsFilesApp /><div data-testid="neutral-content">Neutral content</div></section></main>);
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 notes.md' }));
    await user.click(await screen.findByRole('button', { name: '编辑文本' }));
    const editor = await screen.findByRole('textbox', { name: '编辑 notes.md' });
    fireEvent.change(editor, { target: { value: 'shell boundary draft' } });
    const trigger = screen.getByRole('button', { name: '文件工具' });
    await user.click(trigger);
    await screen.findByRole('dialog', { name: '文件工具' });
    await user.click(target === 'own-shell' ? screen.getByRole('region', { name: 'Files test window' }) : screen.getByTestId('neutral-content'));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '文件工具' })).not.toBeInTheDocument());
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(screen.getByRole('textbox', { name: '编辑 notes.md' })).toBe(editor);
    expect(editor).toHaveValue('shell boundary draft');
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.workspace.save')).toBe(false);
  });

  it('preserves explicit local negative-tabindex and other-window focus intent', async () => {
    const user = userEvent.setup();
    const transport = savedMetadataTransport(() => savedMetadataReceipt());
    renderApp(transport, <main className="paw-desktop-viewport" tabIndex={-1}>
      <section className="paw-window-shell" tabIndex={-1} aria-label="Files test window"><PawOsFilesApp /><div tabIndex={-1} data-testid="local-focus-target" onClick={(event) => event.currentTarget.focus()}><span>Local focus surface</span></div></section>
      <section className="paw-window-shell" tabIndex={-1} aria-label="Other test window" onPointerDown={(event) => event.currentTarget.focus()}><div data-testid="other-neutral">Other neutral content</div></section>
      <button type="button" onClick={(event) => event.currentTarget.focus()}>Desktop control</button>
    </main>);
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 notes.md' }));
    await user.click(await screen.findByRole('button', { name: '编辑文本' }));
    const trigger = screen.getByRole('button', { name: '文件工具' });
    await user.click(trigger);
    await screen.findByRole('dialog', { name: '文件工具' });
    const local = screen.getByTestId('local-focus-target');
    await user.click(screen.getByText('Local focus surface'));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '文件工具' })).not.toBeInTheDocument());
    expect(local).toHaveFocus();
    await user.click(trigger);
    await screen.findByRole('dialog', { name: '文件工具' });
    await user.click(screen.getByTestId('other-neutral'));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '文件工具' })).not.toBeInTheDocument());
    expect(screen.getByRole('region', { name: 'Other test window' })).toHaveFocus();
    await user.click(trigger);
    await screen.findByRole('dialog', { name: '文件工具' });
    const desktopControl = screen.getByRole('button', { name: 'Desktop control' });
    await user.click(desktopControl);
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '文件工具' })).not.toBeInTheDocument());
    expect(desktopControl).toHaveFocus();
  });

  it('closes tools on a file switch, preserves the draft, and does not focus a replacement opener', async () => {
    const user = userEvent.setup();
    const transport = savedMetadataTransport(() => savedMetadataReceipt());
    renderApp(transport, <PawOsFilesApp />);
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 notes.md' }));
    await user.click(await screen.findByRole('button', { name: '编辑文本' }));
    fireEvent.change(await screen.findByRole('textbox', { name: '编辑 notes.md' }), { target: { value: 'keep my notes' } });
    await user.click(screen.getByRole('button', { name: '文件工具' }));
    await screen.findByRole('dialog', { name: '文件工具' });
    const alias = screen.getByRole('treeitem', { name: '打开符号链接 alias.md' });
    await user.click(alias);
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '文件工具' })).not.toBeInTheDocument());
    expect(alias).toHaveFocus();
    await user.click(screen.getByRole('treeitem', { name: '打开文件 notes.md' }));
    expect(await screen.findByRole('textbox', { name: '编辑 notes.md' })).toHaveValue('keep my notes');
    expect(screen.queryByRole('dialog', { name: '文件工具' })).not.toBeInTheDocument();
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.workspace.save')).toBe(false);
  });

  it('closes tools on an external file intent and does not reopen them when the original draft returns', async () => {
    const user = userEvent.setup();
    const transport = savedMetadataTransport(() => savedMetadataReceipt());
    const view = renderApp(transport, <PawOsFilesApp initialRoute={filesRoute('writer', 'notes.md')} />);
    await user.click(await screen.findByRole('button', { name: '编辑文本' }));
    fireEvent.change(await screen.findByRole('textbox', { name: '编辑 notes.md' }), { target: { value: 'retain route draft' } });
    await user.click(screen.getByRole('button', { name: '文件工具' }));
    await screen.findByRole('dialog', { name: '文件工具' });
    view.rerender(<PawOsFilesApp initialRoute={filesRoute('writer', 'other.txt')} />);
    await screen.findByRole('heading', { name: 'other.txt', level: 2 });
    expect(screen.queryByRole('dialog', { name: '文件工具' })).not.toBeInTheDocument();
    view.rerender(<PawOsFilesApp initialRoute={filesRoute('writer', 'notes.md')} />);
    expect(await screen.findByRole('textbox', { name: '编辑 notes.md' })).toHaveValue('retain route draft');
    expect(screen.queryByRole('dialog', { name: '文件工具' })).not.toBeInTheDocument();
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.workspace.save')).toBe(false);
  });

  it('keeps inactive tools interactive and lets discard retain its original editor focus', async () => {
    const user = userEvent.setup();
    const transport = savedMetadataTransport(() => savedMetadataReceipt());
    renderApp(transport, <MotionActivityBoundary active={false}><PawOsFilesApp /></MotionActivityBoundary>);
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 notes.md' }));
    await user.click(await screen.findByRole('button', { name: '编辑文本' }));
    const input = await screen.findByRole('textbox', { name: '编辑 notes.md' });
    fireEvent.change(input, { target: { value: 'discard this draft explicitly' } });
    await user.click(screen.getByRole('button', { name: '文件工具' }));
    expect(await screen.findByRole('dialog', { name: '文件工具' })).toHaveAttribute('data-motion-active', 'false');
    await user.click(screen.getByRole('button', { name: '放弃草稿' }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '文件工具' })).not.toBeInTheDocument());
    expect(input).toHaveFocus();
    expect(input).toHaveValue('disk');
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.workspace.save')).toBe(false);
  });

  it('lets keyboard users inspect file details without replacing or submitting the live draft', async () => {
    const user = userEvent.setup();
    const transport = savedMetadataTransport(() => savedMetadataReceipt());
    renderApp(transport, <PawOsFilesApp />);
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 notes.md' }));
    await user.click(await screen.findByRole('button', { name: '编辑文本' }));
    const input = await screen.findByRole('textbox', { name: '编辑 notes.md' }) as HTMLTextAreaElement;
    const draft = Array.from({ length: 350 }, (_, i) => `line ${i} — unsaved`).join('\n');
    fireEvent.change(input, { target: { value: draft } });
    input.setSelectionRange(17, 29);
    input.scrollTop = 120;
    const summary = screen.getByRole('heading', { name: 'notes.md', level: 2 }).closest('summary')!;
    const disclosure = summary.closest('details')!;
    await user.click(summary);
    expect(disclosure).toHaveAttribute('open');
    const metadata = screen.getByRole('group', { name: '文件路径与属性' });
    expect(metadata).toHaveAttribute('tabindex', '0');
    metadata.focus();
    expect(metadata).toHaveFocus();
    await user.tab();
    expect(within(metadata).getAllByRole('button')[0]).toHaveFocus();
    expect(within(metadata).getByText('4 B')).toBeInTheDocument();
    expect(within(metadata).getByRole('button', { name: '返回文件列表' })).toBeInTheDocument();
    expect(within(metadata).getByRole('button', { name: '复制编辑内容' })).toBeInTheDocument();
    expect(within(metadata).getByRole('button', { name: '复制文件路径' })).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: '复制文件路径' })).toHaveLength(1);
    await user.click(within(metadata).getByRole('button', { name: '复制编辑内容' }));
    expect(await navigator.clipboard.readText()).toBe(draft);
    expect(within(metadata).getByRole('button', { name: '已复制文件内容' })).toHaveFocus();
    expect(within(metadata).getAllByRole('button').length).toBeGreaterThan(0);
    await user.click(summary);
    expect(disclosure).not.toHaveAttribute('open');
    await user.click(screen.getByRole('button', { name: '文件工具' }));
    const tools = await screen.findByRole('dialog', { name: '文件工具' });
    expect(screen.getByRole('button', { name: '复制文件路径' }).closest('.paw-files-tools-popover')).toBe(tools);
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('button', { name: '复制文件路径' })).not.toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '编辑 notes.md' })).toBe(input);
    expect(input).toHaveValue(draft);
    expect([input.selectionStart, input.selectionEnd, input.scrollTop]).toEqual([17, 29, 120]);
    expect(screen.getByRole('button', { name: '保存文件' })).toBeEnabled();
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.workspace.save')).toBe(false);
    input.focus();
    expect(input).toHaveFocus();
  });

  it.each(['notes.md', 'alias.md'])('syncs the saved byte count for %s without refreshing or counting a newer draft', async (name) => {
    const user = userEvent.setup();
    let finishSave!: (value: unknown) => void;
    const transport = savedMetadataTransport(() => new Promise((resolve) => { finishSave = resolve; }));
    renderApp(transport, <PawOsFilesApp />);
    const treeLabel = `${name === 'alias.md' ? '打开符号链接' : '打开文件'} ${name}`;
    await user.click(await screen.findByRole('treeitem', { name: treeLabel }));
    await user.click(await screen.findByRole('button', { name: '编辑文本' }));
    const editor = await screen.findByRole('textbox', { name: `编辑 ${name}` });
    const submitted = 'saved 文档\n';
    const byteSize = new TextEncoder().encode(submitted).length;
    fireEvent.change(editor, { target: { value: submitted } });
    await user.click(screen.getByRole('button', { name: '保存文件' }));
    expect(within(screen.getByRole('treeitem', { name: treeLabel })).getByText('4 B')).toBeInTheDocument();
    fireEvent.change(editor, { target: { value: 'a newer unsaved draft' } });
    await act(async () => finishSave(savedMetadataReceipt()));
    expect(await screen.findByText('已保存到文件。')).toBeInTheDocument();
    expect(within(screen.getByRole('treeitem', { name: treeLabel })).getByText(`${byteSize} B`)).toBeInTheDocument();
    expect(within(screen.getByRole('treeitem', { name: '打开文件 notes.md' })).getByText(`${byteSize} B`)).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: '文件预览' })).getByText(`${byteSize} B`)).toBeInTheDocument();
    expect(screen.getByText(`已选 ${name} · ${byteSize} B`)).toBeInTheDocument();
    expect(editor).toHaveValue('a newer unsaved draft');
    expect(transport.requests.filter(({ request }) => request.pathId === 'files.read')).toHaveLength(1);
    expect(transport.requests.filter(({ request }) => request.pathId === 'files.list' && request.query?.path)).toHaveLength(1);
  });

  it('applies a late save only to its loaded file while another file is selected', async () => {
    const user = userEvent.setup();
    let finishSave!: (value: unknown) => void;
    const transport = savedMetadataTransport(() => new Promise((resolve) => { finishSave = resolve; }));
    renderApp(transport, <PawOsFilesApp />);
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 notes.md' }));
    await user.click(await screen.findByRole('button', { name: '编辑文本' }));
    fireEvent.change(await screen.findByRole('textbox', { name: '编辑 notes.md' }), { target: { value: 'saved result' } });
    await user.click(screen.getByRole('button', { name: '保存文件' }));
    await user.click(screen.getByRole('treeitem', { name: '打开文件 other.txt' }));
    expect(await screen.findByText('other text')).toBeInTheDocument();
    await act(async () => finishSave(savedMetadataReceipt()));
    expect(within(screen.getByRole('treeitem', { name: '打开文件 notes.md' })).getByText('12 B')).toBeInTheDocument();
    expect(screen.getByText('已选 other.txt · 10 B')).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: '文件预览' })).getByText('10 B')).toBeInTheDocument();
    expect(screen.getByText('other text')).toBeInTheDocument();
    expect(transport.requests.filter(({ request }) => request.pathId === 'files.read')).toHaveLength(2);
    expect(transport.requests.filter(({ request }) => request.pathId === 'files.list' && request.query?.path)).toHaveLength(1);
  });

  it('ignores a late save from the previous Session when the new Session has the same file path', async () => {
    const user = userEvent.setup();
    let finishSave!: (value: unknown) => void;
    const transport = savedMetadataTransport(() => new Promise((resolve) => { finishSave = resolve; }));
    renderApp(transport, <PawOsFilesApp />);
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 notes.md' }));
    await user.click(await screen.findByRole('button', { name: '编辑文本' }));
    fireEvent.change(await screen.findByRole('textbox', { name: '编辑 notes.md' }), { target: { value: 'saved result' } });
    await user.click(screen.getByRole('button', { name: '保存文件' }));
    await user.selectOptions(screen.getByRole('combobox', { name: '选择文件所属 Session' }), 'reader');
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 notes.md' }));
    expect(await screen.findByText('reader disk')).toBeInTheDocument();
    await act(async () => finishSave(savedMetadataReceipt()));
    expect(within(screen.getByRole('treeitem', { name: '打开文件 notes.md' })).getByText('11 B')).toBeInTheDocument();
    expect(screen.getByText('已选 notes.md · 11 B')).toBeInTheDocument();
    expect(screen.getByText('reader disk')).toBeInTheDocument();
    expect(screen.queryByText('已保存到文件。')).not.toBeInTheDocument();
    expect(transport.requests.filter(({ request }) => request.pathId === 'files.read')).toHaveLength(2);
  });

  it('reveals a workspace root in the tree when the deep link names a directory', async () => {
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }],
        },
        'files.list': (request: ControlRequest) => ({
          ok: true,
          path: request.query?.path,
          items: [{ path: '/workspace/paw/AGENTS.md', name: 'AGENTS.md', kind: 'file', byteSize: 128 }],
        }),
        'files.read': (request: ControlRequest) => ({
          ok: true,
          path: request.query?.path,
          content: '# Project guide',
          byteSize: 15,
          truncated: false,
        }),
      },
    });

    renderApp(transport, <PawOsFilesApp initialRoute={`/files?session=session-work&path=${encodeURIComponent('/workspace/paw')}`} />);

    // A directory is revealed, not read: the tree focuses the root, its
    // listing loads, and the reader stays on its empty state.
    const rootItem = await screen.findByRole('treeitem', { name: /paw/ });
    await waitFor(() => expect(rootItem).toHaveFocus());
    expect(await screen.findByRole('treeitem', { name: '打开文件 AGENTS.md' })).toBeInTheDocument();
    expect(screen.getByText('选择要检查的文件')).toBeInTheDocument();
    expect(transport.requests.some((call) => call.request.pathId === 'files.read')).toBe(false);
  });

  it('projects its live Session selector and refresh action into window chrome', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-one',
          items: [
            { id: 'session-one', title: 'Project One', updatedAtMs: 2, workspaceRoots: ['/workspace/one'], status: 'idle' },
            { id: 'session-two', title: 'Project Two', updatedAtMs: 1, workspaceRoots: ['/workspace/two'], status: 'idle' },
          ],
        },
        'files.list': (request: ControlRequest) => ({ ok: true, path: request.query?.path, items: [] }),
      },
    });

    renderApp(transport, (
      <PawWindowFrame
        active
        appId="files"
        bounds={{ x: 0, y: 0, width: 900, height: 640 }}
        onBoundsCommit={() => undefined}
        onClose={() => undefined}
        onFocus={() => undefined}
        onMinimize={() => undefined}
        onToggleMaximize={() => undefined}
        title="Files"
        windowChrome="files-tools"
        windowId="files"
        zIndex={10}
      >
        <PawOsFilesApp />
      </PawWindowFrame>
    ));

    const titlebar = screen.getByText('Files').closest('.paw-window-titlebar') as HTMLElement;
    const selector = await within(titlebar).findByRole('combobox', { name: '选择文件所属 Session' });
    expect(document.querySelector('.paw-window-body .paw-files-app__toolbar')).toBeNull();
    await user.selectOptions(selector, 'session-two');
    await waitFor(() => expect(transport.requests.some((call) => (
      call.request.pathId === 'files.list'
      && call.request.query?.path === '/workspace/two'
    ))).toBe(true));
    const requestsBeforeRefresh = transport.requests.filter((call) => call.request.pathId === 'files.list').length;
    await user.click(within(titlebar).getByRole('button', { name: '刷新文件' }));
    await waitFor(() => expect(transport.requests.filter((call) => call.request.pathId === 'files.list')).toHaveLength(requestsBeforeRefresh + 1));
  });

  it('does not let a late directory response cross a Session shortcut change', async () => {
    const user = userEvent.setup();
    let resolveFirst: ((value: unknown) => void) | undefined;
    const firstListing = new Promise<unknown>((resolve) => { resolveFirst = resolve; });
    let directoryLoads = 0;
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-one',
          items: [
            { id: 'session-one', title: 'Project One', updatedAtMs: 2, workspaceRoots: ['/workspace/shared'], status: 'idle' },
            { id: 'session-two', title: 'Project Two', updatedAtMs: 1, workspaceRoots: ['/workspace/shared'], status: 'idle' },
          ],
        },
        'files.list': (request: ControlRequest) => {
          if (!request.query?.path) return { ok: true, path: '/home/qa', homePath: '/home/qa', items: [] };
          if (++directoryLoads === 1) return firstListing;
          return {
            ok: true,
            path: '/workspace/shared',
            items: [{ path: '/workspace/shared/two.txt', name: 'two.txt', kind: 'file', byteSize: 2 }],
          };
        },
      },
    });

    renderApp(transport, <PawOsFilesApp />);
    const selector = await screen.findByRole('combobox', { name: '选择文件所属 Session' });
    await waitFor(() => expect(transport.requests.some((call) => (
      call.request.pathId === 'files.list'
      && call.request.query?.path === '/workspace/shared'
    ))).toBe(true));

    await user.selectOptions(selector, 'session-two');
    await act(async () => {
      resolveFirst?.({
        ok: true,
        path: '/workspace/shared',
        items: [{ path: '/workspace/shared/one.txt', name: 'one.txt', kind: 'file', byteSize: 1 }],
      });
      await Promise.resolve();
    });

    expect(await screen.findByRole('treeitem', { name: '打开文件 two.txt' })).toBeInTheDocument();
    expect(screen.queryByRole('treeitem', { name: '打开文件 one.txt' })).not.toBeInTheDocument();
    expect(directoryLoads).toBe(2);
  });

  it('uses one roving keyboard tree and refreshes every expanded directory', async () => {
    const user = userEvent.setup();
    const directoryReads: string[] = [];
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }],
        },
        'files.list': (request: ControlRequest) => {
          const path = String(request.query?.path ?? '');
          directoryReads.push(path);
          if (path === '/workspace/paw/docs') return {
            ok: true,
            path,
            items: [{ path: `${path}/guide.md`, name: 'guide.md', kind: 'file', byteSize: 42 }],
          };
          return {
            ok: true,
            path,
            items: [
              { path: '/workspace/paw/docs', name: 'docs', kind: 'directory' },
              { path: '/workspace/paw/AGENTS.md', name: 'AGENTS.md', kind: 'file', byteSize: 128 },
            ],
          };
        },
        'files.read': (request: ControlRequest) => ({
          ok: true,
          path: request.query?.path,
          content: '# Guide',
          byteSize: 7,
          truncated: false,
        }),
      },
    });

    renderApp(transport, <PawOsFilesApp />);

    const tree = await screen.findByRole('tree', { name: '项目文件' });
    const root = await within(tree).findByRole('treeitem', { name: /收起工作区 paw/ });
    const docs = await within(tree).findByRole('treeitem', { name: '展开目录 docs' });
    expect(within(tree).getAllByRole('treeitem').filter((item) => item.tabIndex === 0)).toEqual([root]);

    root.focus();
    await user.keyboard('{ArrowDown}');
    expect(docs).toHaveFocus();
    await user.keyboard('{ArrowRight}');
    const guide = await within(tree).findByRole('treeitem', { name: '打开文件 guide.md' });
    expect(docs).toHaveAttribute('aria-expanded', 'true');
    await user.keyboard('{ArrowRight}');
    expect(guide).toHaveFocus();
    await user.keyboard('{End}');
    expect(within(tree).getByRole('treeitem', { name: '打开文件 AGENTS.md' })).toHaveFocus();
    await user.keyboard('{Home}');
    expect(root).toHaveFocus();
    guide.focus();
    await user.keyboard('{ArrowLeft}');
    expect(docs).toHaveFocus();
    await user.keyboard('{ArrowLeft}');
    expect(docs).toHaveAttribute('aria-expanded', 'false');
    expect(within(tree).queryByRole('treeitem', { name: '打开文件 guide.md' })).not.toBeInTheDocument();

    await user.click(docs);
    await within(tree).findByRole('treeitem', { name: '打开文件 guide.md' });
    const nestedReadsBeforeRefresh = directoryReads.filter((path) => path === '/workspace/paw/docs').length;
    await user.click(screen.getByRole('button', { name: '刷新文件' }));
    await waitFor(() => expect(directoryReads.filter((path) => path === '/workspace/paw/docs').length).toBeGreaterThan(nestedReadsBeforeRefresh));
    expect(await within(tree).findByRole('treeitem', { name: '打开文件 guide.md' })).toBeInTheDocument();
  });

  it('starts keyboard focus on the tree once the first listing lands', async () => {
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }],
        },
        'files.list': {
          ok: true,
          path: '/workspace/paw',
          items: [{ path: '/workspace/paw/AGENTS.md', name: 'AGENTS.md', kind: 'file', byteSize: 128 }],
        },
      },
    });

    renderApp(transport, <PawOsFilesApp />);

    const tree = await screen.findByRole('tree', { name: '项目文件' });
    const root = await within(tree).findByRole('treeitem', { name: /收起工作区 paw/ });
    await waitFor(() => expect(root).toHaveFocus());
  });

  it('never steals initial focus from a field the person is already typing in', async () => {
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }],
        },
        'files.list': {
          ok: true,
          path: '/workspace/paw',
          items: [{ path: '/workspace/paw/AGENTS.md', name: 'AGENTS.md', kind: 'file', byteSize: 128 }],
        },
      },
    });

    renderApp(transport, (
      <>
        <input aria-label="外部输入" />
        <PawOsFilesApp />
      </>
    ));
    const field = screen.getByRole('textbox', { name: '外部输入' });
    field.focus();

    const tree = await screen.findByRole('tree', { name: '项目文件' });
    await within(tree).findByRole('treeitem', { name: /收起工作区 paw/ });
    expect(field).toHaveFocus();
  });

  it('renders binary payloads as an explicit state and preserves full truncated labels', async () => {
    const user = userEvent.setup();
    const binaryPath = '/workspace/paw/assets/a-very-long-preview-file-name.png';
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }],
        },
        'files.list': {
          ok: true,
          path: '/workspace/paw',
          items: [{ path: binaryPath, name: 'a-very-long-preview-file-name.png', kind: 'file', byteSize: 82_304 }],
        },
        'files.read': {
          ok: true,
          path: binaryPath,
          content: '\u0000PNG\r\n\u001a\n',
          byteSize: 82_304,
          truncated: false,
        },
      },
    });

    const { container } = renderApp(transport, <PawOsFilesApp />);
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 a-very-long-preview-file-name.png' }));

    expect(await screen.findByText('二进制文件不能作为文本预览。')).toBeInTheDocument();
    // The reader names its active lens and points at the real escape hatch.
    expect(screen.getByText('二进制')).toBeInTheDocument();
    expect(screen.getByText('可从上方复制完整路径，用 Terminal 或 Agent 工具检查原始内容。')).toBeInTheDocument();
    const heading = screen.getByRole('heading', { name: 'a-very-long-preview-file-name.png', level: 2 });
    expect(heading).toHaveAttribute('title', 'a-very-long-preview-file-name.png');
    expect(heading.closest('details')?.querySelector('small')).toHaveAttribute('title', binaryPath);
    expect(container.querySelector('.paw-files-statusbar__selection')).toHaveAttribute('title', `${binaryPath} · 80 KB`);
    expect(container.querySelector('.agent-file-code')).toBeNull();
  });

  it('leaves the shared code reader on its --color-code-bg/--color-code-text pair', async () => {
    // Regression: Files once repainted only the code-surface background with a
    // light colour, leaving the shared light code text unreadable in the plain
    // fallback reader. The Files owner must not restyle the code surface paint.
    expect(filesCss).not.toMatch(/\.agent-file-code[^{}]*\{[^}]*background/s);
    // The Files header owns the filename and the copy actions, so the shared
    // reader's duplicate filename strip stays hidden inside this App.
    expect(filesCss).toMatch(/\.agent-file-code figcaption[^{]*\{[^}]*display:\s*none/s);

    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }],
        },
        'files.list': {
          ok: true,
          path: '/workspace/paw',
          items: [{ path: '/workspace/paw/notes.txt', name: 'notes.txt', kind: 'file', byteSize: 27 }],
        },
        'files.read': {
          ok: true,
          path: '/workspace/paw/notes.txt',
          content: 'plain text stays readable\n',
          byteSize: 27,
          truncated: false,
        },
      },
    });

    const { container } = renderApp(transport, <PawOsFilesApp />);
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 notes.txt' }));

    expect(await screen.findByText('plain text stays readable')).toBeInTheDocument();
    expect(container.querySelector('.agent-file-code')).not.toBeNull();
  });

  it('shows an explicit empty-file state instead of a blank code reader', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }],
        },
        'files.list': {
          ok: true,
          path: '/workspace/paw',
          items: [{ path: '/workspace/paw/todo.md', name: 'todo.md', kind: 'file', byteSize: 0 }],
        },
        'files.read': {
          ok: true,
          path: '/workspace/paw/todo.md',
          content: '',
          byteSize: 0,
          truncated: false,
        },
      },
    });

    const { container } = renderApp(transport, <PawOsFilesApp />);
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 todo.md' }));

    expect(await screen.findByText('这个文件是空的。')).toBeInTheDocument();
    expect(container.querySelector('.agent-file-code')).toBeNull();
    expect(container.querySelector('.agent-file-markdown')).toBeNull();
  });

  it('labels a truncated preview and counts loaded entries truthfully', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }],
        },
        'files.list': {
          ok: true,
          path: '/workspace/paw',
          items: [{ path: '/workspace/paw/big.log', name: 'big.log', kind: 'file', byteSize: 131_072 }],
        },
        'files.read': {
          ok: true,
          path: '/workspace/paw/big.log',
          content: 'line 1\nline 2\n',
          byteSize: 131_072,
          offset: 0,
          nextOffset: 65_536,
          truncated: true,
        },
      },
    });

    renderApp(transport, <PawOsFilesApp />);
    expect(await screen.findByText('已加载 1 项')).toBeInTheDocument();
    await user.click(screen.getByRole('treeitem', { name: '打开文件 big.log' }));

    expect(await screen.findByText('已显示前 64 KB · 共 128 KB')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /继续读取/ })).toBeInTheDocument();
    expect(screen.getByText(/已选 big\.log · 128 KB/)).toBeInTheDocument();
    // Loaded-line and renderer readouts stay honest about the partial window.
    expect(screen.getByText('已载 2 行')).toBeInTheDocument();
    expect(screen.getByText('纯文本')).toBeInTheDocument();
  });

  it('continues a bounded read from the served nextOffset and retires the bar when complete', async () => {
    const user = userEvent.setup();
    const readOffsets: number[] = [];
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }],
        },
        'files.list': {
          ok: true,
          path: '/workspace/paw',
          items: [{ path: '/workspace/paw/big.log', name: 'big.log', kind: 'file', byteSize: 131_072 }],
        },
        'files.read': (request: ControlRequest) => {
          const offset = Number(request.query?.offset ?? 0);
          readOffsets.push(offset);
          if (offset === 0) return {
            ok: true,
            path: '/workspace/paw/big.log',
            content: 'chunk one\n',
            byteSize: 131_072,
            offset: 0,
            nextOffset: 65_536,
            truncated: true,
          };
          return {
            ok: true,
            path: '/workspace/paw/big.log',
            content: 'chunk two\n',
            byteSize: 131_072,
            offset,
            nextOffset: 131_072,
            truncated: false,
          };
        },
      },
    });

    renderApp(transport, <PawOsFilesApp />);
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 big.log' }));

    expect(await screen.findByText('已显示前 64 KB · 共 128 KB')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /继续读取/ }));

    expect(await screen.findByText(/chunk two/)).toBeInTheDocument();
    expect(screen.getByText(/chunk one/)).toBeInTheDocument();
    expect(screen.queryByText(/已显示前/)).not.toBeInTheDocument();
    expect(readOffsets).toEqual([0, 65_536]);
  });

  it.each(['file', 'session'] as const)('releases the old preview read lock when switching %s and keeps the new read independently pending', async (switchKind) => {
    const user = userEvent.setup();
    let resolveOld!: (value: unknown) => void;
    let resolveNew!: (value: unknown) => void;
    const oldRead = new Promise((resolve) => { resolveOld = resolve; });
    const newRead = new Promise((resolve) => { resolveNew = resolve; });
    const chunk = (path: string, content: string, truncated: boolean) => ({
      ok: true, path, content, byteSize: 131_072,
      offset: truncated ? 0 : 65_536, nextOffset: truncated ? 65_536 : 131_072, truncated,
    });
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': {
        ok: true, activeSessionId: 'session-one', items: [
          { id: 'session-one', title: 'Project One', updatedAtMs: 2, workspaceRoots: ['/workspace/paw'], status: 'idle' },
          { id: 'session-two', title: 'Project Two', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' },
        ],
      },
      'files.list': {
        ok: true, path: '/workspace/paw', items: ['a.log', 'b.log'].map((name) => ({ path: `/workspace/paw/${name}`, name, kind: 'file', byteSize: 131_072 })),
      },
      'files.read': (request: ControlRequest) => {
        const path = String(request.query?.path);
        if (!Number(request.query?.offset)) return chunk(path, path.endsWith('a.log') ? '原文件首段\n' : '新文件首段\n', true);
        return path.endsWith('a.log') ? oldRead : newRead;
      },
    } });
    renderApp(transport, <PawOsFilesApp />);
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 a.log' }));
    await user.click(await screen.findByRole('button', { name: /继续读取/ }));
    if (switchKind === 'session') await user.selectOptions(screen.getByRole('combobox', { name: '选择文件所属 Session' }), 'session-two');
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 b.log' }));
    await screen.findByText('新文件首段');
    expect(screen.getByRole('button', { name: /继续读取/ })).toBeEnabled();
    await user.click(screen.getByRole('button', { name: /继续读取/ }));
    await act(async () => resolveOld(chunk('/workspace/paw/a.log', '旧续读不应串入\n', false)));
    expect(screen.getByRole('button', { name: /继续读取/ })).toBeDisabled();
    expect(screen.queryByText('旧续读不应串入')).not.toBeInTheDocument();
    await act(async () => resolveNew(chunk('/workspace/paw/b.log', '新文件后段\n', false)));
    expect(await screen.findByText(/新文件后段/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /继续读取/ })).not.toBeInTheDocument();
    const reads = transport.requests.filter(({ request }) => request.pathId === 'files.read').map(({ request }) => request);
    expect(reads.map((request) => Number(request.query?.offset))).toEqual([0, 65_536, 0, 65_536]);
    expect(reads[3].query?.sessionId).toBe(switchKind === 'session' ? 'session-two' : 'session-one');
  });

  it('marks the 512 KB reading cap on the gauge scale of an oversized file', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }],
        },
        'files.list': {
          ok: true,
          path: '/workspace/paw',
          items: [{ path: '/workspace/paw/trace.log', name: 'trace.log', kind: 'file', byteSize: 2_097_152 }],
        },
        'files.read': {
          ok: true,
          path: '/workspace/paw/trace.log',
          content: 'first window\n',
          byteSize: 2_097_152,
          offset: 0,
          nextOffset: 65_536,
          truncated: true,
        },
      },
    });

    renderApp(transport, <PawOsFilesApp />);
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 trace.log' }));

    const gauge = await screen.findByRole('progressbar', { name: '已读取 64 KB，共 2 MB' });
    // The printed percentage never claims more than the bytes on screen.
    expect(gauge).toHaveAttribute('aria-valuenow', '3');
    expect(screen.getByText('3%')).toBeInTheDocument();
    // 64 KB ticks would blur into noise on a 2 MB scale, so they retire.
    expect(gauge.style.getPropertyValue('--paw-files-tick')).toBe('');
    // The in-App reading cap sits at its true position on this file's scale.
    expect(gauge.style.getPropertyValue('--paw-files-cap')).toBe('25%');
    expect(gauge.querySelector('b')).toHaveAttribute('title', '512 KB 预览上限');
  });

  it('reads out the active renderer, loaded lines, and the rail scan coverage', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }],
        },
        'files.list': {
          ok: true,
          path: '/workspace/paw',
          items: [
            { path: '/workspace/paw/docs', name: 'docs', kind: 'directory' },
            { path: '/workspace/paw/AGENTS.md', name: 'AGENTS.md', kind: 'file', byteSize: 38 },
          ],
        },
        'files.read': {
          ok: true,
          path: '/workspace/paw/AGENTS.md',
          content: '# Project guide\nRead docs before work.',
          byteSize: 38,
          truncated: false,
        },
      },
    });

    renderApp(transport, <PawOsFilesApp />);

    // The rail head reports exactly what has been scanned: one listed
    // directory (the root) holding two entries.
    expect(await screen.findByText('1 目录 · 2 项')).toBeInTheDocument();

    await user.click(screen.getByRole('treeitem', { name: '打开文件 AGENTS.md' }));
    expect(await screen.findByRole('heading', { name: 'AGENTS.md', level: 2 })).toBeInTheDocument();
    // A complete two-line Markdown file names its lens and its full extent.
    expect(await screen.findByText('Markdown')).toBeInTheDocument();
    expect(screen.getByText('2 行')).toBeInTheDocument();
  });

  it('teaches the real keyboard path on the empty reading desk', async () => {
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }],
        },
        'files.list': { ok: true, path: '/workspace/paw', items: [] },
      },
    });

    renderApp(transport, <PawOsFilesApp />);

    expect(await screen.findByText('选择要检查的文件')).toBeInTheDocument();
    expect(screen.getByText('Enter')).toBeInTheDocument();
    expect(screen.getByText('打开')).toBeInTheDocument();
    expect(screen.getByText('展开目录')).toBeInTheDocument();
  });

  it('reports an honest display limit when a directory listing is truncated by the route', async () => {
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }],
        },
        'files.list': {
          ok: true,
          path: '/workspace/paw',
          truncated: true,
          items: [
            { path: '/workspace/paw/a.txt', name: 'a.txt', kind: 'file', byteSize: 1 },
            { path: '/workspace/paw/b.txt', name: 'b.txt', kind: 'file', byteSize: 2 },
          ],
        },
      },
    });

    renderApp(transport, <PawOsFilesApp />);
    expect(await screen.findByText('目录条目已达显示上限。')).toBeInTheDocument();
  });

  it('renders a complete SVG as a safe image with a source toggle', async () => {
    const user = userEvent.setup();
    const svgContent = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10"/></svg>';
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }],
        },
        'files.list': {
          ok: true,
          path: '/workspace/paw',
          items: [{ path: '/workspace/paw/logo.svg', name: 'logo.svg', kind: 'file', byteSize: 104 }],
        },
        'files.read': {
          ok: true,
          path: '/workspace/paw/logo.svg',
          content: svgContent,
          byteSize: 104,
          truncated: false,
        },
      },
    });

    const { container } = renderApp(transport, <PawOsFilesApp />);
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 logo.svg' }));

    const image = await screen.findByRole('img', { name: 'logo.svg 矢量图预览' });
    expect(image).toHaveAttribute('src', `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svgContent)}`);
    expect(container.querySelector('.agent-file-code')).toBeNull();

    await user.click(screen.getByRole('button', { name: '源码' }));
    expect(container.querySelector('.agent-file-code')).not.toBeNull();
    expect(screen.queryByRole('img', { name: 'logo.svg 矢量图预览' })).not.toBeInTheDocument();
  });

  it('recovers a failed file read through the in-place retry action', async () => {
    const user = userEvent.setup();
    let readAttempts = 0;
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }],
        },
        'files.list': {
          ok: true,
          path: '/workspace/paw',
          items: [{ path: '/workspace/paw/notes.txt', name: 'notes.txt', kind: 'file', byteSize: 20 }],
        },
        'files.read': (request: ControlRequest) => {
          readAttempts += 1;
          if (readAttempts === 1) throw new Error('读取超时。');
          return { ok: true, path: request.query?.path, content: 'recovered content\n', byteSize: 20, truncated: false };
        },
      },
    });

    renderApp(transport, <PawOsFilesApp />);
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 notes.txt' }));

    const failure = await screen.findByRole('alert');
    expect(failure).toHaveTextContent('读取超时。');
    await user.click(within(failure).getByRole('button', { name: '重试' }));
    expect(await screen.findByText('recovered content')).toBeInTheDocument();
    expect(readAttempts).toBe(2);
  });

  it('returns from the narrow reader to the tree with expansion and focus preserved', async () => {
    const narrowEmulation = applyNarrowLayout();

    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }],
        },
        'files.list': (request: ControlRequest) => {
          const path = String(request.query?.path ?? '');
          if (path === '/workspace/paw/docs') return {
            ok: true,
            path,
            items: [{ path: `${path}/guide.md`, name: 'guide.md', kind: 'file', byteSize: 42 }],
          };
          return {
            ok: true,
            path,
            items: [{ path: '/workspace/paw/docs', name: 'docs', kind: 'directory' }],
          };
        },
        'files.read': (request: ControlRequest) => ({
          ok: true,
          path: request.query?.path,
          content: '# Guide',
          byteSize: 7,
          truncated: false,
        }),
      },
    });

    try {
      renderApp(transport, <PawOsFilesApp />);
      await user.click(await screen.findByRole('treeitem', { name: '展开目录 docs' }));
      await user.click(await screen.findByRole('treeitem', { name: '打开文件 guide.md' }));
      expect(await screen.findByRole('heading', { name: 'guide.md', level: 2 })).toBeInTheDocument();

      const back = screen.getByRole('button', { name: '返回文件列表' });
      // The narrow return path is a labelled pill, not a bare icon.
      expect(back).toHaveTextContent('返回');
      await waitFor(() => expect(back).toHaveFocus());
      await user.click(back);

      expect(screen.queryByRole('heading', { name: 'guide.md', level: 2 })).not.toBeInTheDocument();
      const reopened = screen.getByRole('treeitem', { name: '打开文件 guide.md' });
      expect(reopened).toHaveFocus();
      expect(screen.getByRole('treeitem', { name: '收起目录 docs' })).toHaveAttribute('aria-expanded', 'true');
    } finally {
      narrowEmulation.remove();
    }
  });

  it('presents directories first with natural name order regardless of service order', async () => {
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }],
        },
        'files.list': {
          ok: true,
          path: '/workspace/paw',
          items: [
            { path: '/workspace/paw/zeta.md', name: 'zeta.md', kind: 'file', byteSize: 1 },
            { path: '/workspace/paw/beta', name: 'beta', kind: 'directory' },
            { path: '/workspace/paw/alpha10.ts', name: 'alpha10.ts', kind: 'file', byteSize: 1 },
            { path: '/workspace/paw/alpha2.ts', name: 'alpha2.ts', kind: 'file', byteSize: 1 },
            { path: '/workspace/paw/Alpha', name: 'Alpha', kind: 'directory' },
          ],
        },
      },
    });

    renderApp(transport, <PawOsFilesApp />);

    const tree = await screen.findByRole('tree', { name: '项目文件' });
    await within(tree).findByRole('treeitem', { name: '打开文件 zeta.md' });
    const names = within(tree).getAllByRole('treeitem').slice(1).map((item) => item.querySelector('span:nth-of-type(2), span > span')?.textContent ?? item.textContent);
    expect(names.map((name) => name?.trim())).toEqual(['Alpha', 'beta', 'alpha2.ts', 'alpha10.ts', 'zeta.md']);
  });

  it('filters loaded entries with truthful coverage and opens a match directly', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }],
        },
        'files.list': (request: ControlRequest) => {
          const path = String(request.query?.path ?? '');
          if (path === '/workspace/paw/docs') return {
            ok: true,
            path,
            items: [{ path: `${path}/guide.md`, name: 'guide.md', kind: 'file', byteSize: 42 }],
          };
          return {
            ok: true,
            path,
            items: [
              { path: '/workspace/paw/docs', name: 'docs', kind: 'directory' },
              { path: '/workspace/paw/AGENTS.md', name: 'AGENTS.md', kind: 'file', byteSize: 128 },
            ],
          };
        },
        'files.read': (request: ControlRequest) => ({
          ok: true,
          path: request.query?.path,
          content: '# Guide',
          byteSize: 7,
          truncated: false,
        }),
      },
    });

    renderApp(transport, <PawOsFilesApp />);

    await user.click(await screen.findByRole('treeitem', { name: '展开目录 docs' }));
    await screen.findByRole('treeitem', { name: '打开文件 guide.md' });

    const filter = screen.getByRole('searchbox', { name: '筛选已加载的文件' });
    await user.type(filter, 'gui');
    // The filter names its coverage: it only searches already-loaded entries.
    expect(screen.getByText('在已加载的 3 项中匹配 1 项')).toBeInTheDocument();
    expect(screen.queryByRole('tree', { name: '项目文件' })).not.toBeInTheDocument();

    await user.click(within(screen.getByRole('list', { name: '筛选结果' })).getByRole('button', { name: '打开文件 guide.md' }));
    expect(await screen.findByRole('heading', { name: 'guide.md', level: 2 })).toBeInTheDocument();

    await user.clear(filter);
    await user.type(filter, 'no-such-entry');
    expect(screen.getByText('在已加载的 3 项中匹配 0 项')).toBeInTheDocument();
    expect(screen.getByText('没有匹配已加载的条目。')).toBeInTheDocument();

    // Escape clears the filter and restores the tree.
    await user.keyboard('{Escape}');
    expect(await screen.findByRole('tree', { name: '项目文件' })).toBeInTheDocument();
  });

  it('returns from a narrow filtered file preview to the filter match that opened it', async () => {
    const narrowEmulation = applyNarrowLayout();
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }],
        },
        'files.list': {
          ok: true,
          path: '/workspace/paw',
          items: [{ path: '/workspace/paw/guide.md', name: 'guide.md', kind: 'file', byteSize: 42 }],
        },
        'files.read': (request: ControlRequest) => ({
          ok: true,
          path: request.query?.path,
          content: '# Guide',
          byteSize: 7,
          truncated: false,
        }),
      },
    });

    try {
      renderApp(transport, <PawOsFilesApp />);
      const filter = await screen.findByRole('searchbox', { name: '筛选已加载的文件' });
      await user.type(filter, 'guide');
      const match = within(screen.getByRole('list', { name: '筛选结果' })).getByRole('button', { name: '打开文件 guide.md' });
      await user.click(match);
      expect(await screen.findByRole('heading', { name: 'guide.md', level: 2 })).toBeInTheDocument();

      const back = screen.getByRole('button', { name: '返回文件列表' });
      await waitFor(() => expect(back).toHaveFocus());
      await user.click(back);

      const restoredMatch = within(screen.getByRole('list', { name: '筛选结果' })).getByRole('button', { name: '打开文件 guide.md' });
      expect(restoredMatch).toHaveFocus();
    } finally {
      narrowEmulation.remove();
    }
  });

  it('locates a crumb directory in the tree from the reader header', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }],
        },
        'files.list': (request: ControlRequest) => {
          const path = String(request.query?.path ?? '');
          if (path === '/workspace/paw/docs') return {
            ok: true,
            path,
            items: [{ path: `${path}/guide.md`, name: 'guide.md', kind: 'file', byteSize: 42 }],
          };
          return {
            ok: true,
            path,
            items: [{ path: '/workspace/paw/docs', name: 'docs', kind: 'directory' }],
          };
        },
        'files.read': (request: ControlRequest) => ({
          ok: true,
          path: request.query?.path,
          content: '# Guide',
          byteSize: 7,
          truncated: false,
        }),
      },
    });

    renderApp(transport, <PawOsFilesApp />);
    await user.click(await screen.findByRole('treeitem', { name: '展开目录 docs' }));
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 guide.md' }));
    expect(await screen.findByRole('heading', { name: 'guide.md', level: 2 })).toBeInTheDocument();

    // The crumb line names the root-relative chain: workspace root, then docs.
    await user.click(screen.getByRole('button', { name: '在目录树中定位 docs' }));

    // The wide layout keeps the file open beside the located directory row.
    expect(screen.getByRole('heading', { name: 'guide.md', level: 2 })).toBeInTheDocument();
    const docs = screen.getByRole('treeitem', { name: '收起目录 docs' });
    expect(docs).toHaveAttribute('aria-expanded', 'true');
    await waitFor(() => expect(docs).toHaveFocus());

    await user.click(screen.getByRole('button', { name: '在目录树中定位 paw' }));
    await waitFor(() => expect(screen.getByRole('treeitem', { name: /收起工作区 paw/ })).toHaveFocus());
  });

  it('reports directory child counts, the filter match count, and the honest loaded share', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }],
        },
        'files.list': (request: ControlRequest) => {
          const path = String(request.query?.path ?? '');
          if (path === '/workspace/paw/docs') return {
            ok: true,
            path,
            items: [{ path: `${path}/guide.md`, name: 'guide.md', kind: 'file', byteSize: 42 }],
          };
          return {
            ok: true,
            path,
            items: [
              { path: '/workspace/paw/docs', name: 'docs', kind: 'directory' },
              { path: '/workspace/paw/big.log', name: 'big.log', kind: 'file', byteSize: 131_072 },
            ],
          };
        },
        'files.read': {
          ok: true,
          path: '/workspace/paw/big.log',
          content: 'line 1\nline 2\n',
          byteSize: 131_072,
          offset: 0,
          nextOffset: 65_536,
          truncated: true,
        },
      },
    });

    const { container } = renderApp(transport, <PawOsFilesApp />);

    // A loaded directory states how many entries it holds; unread ones claim nothing.
    const docs = await screen.findByRole('treeitem', { name: '展开目录 docs' });
    expect(docs).not.toHaveTextContent('项');
    await user.click(docs);
    await screen.findByRole('treeitem', { name: '打开文件 guide.md' });
    expect(screen.getByRole('treeitem', { name: '收起目录 docs' })).toHaveTextContent('1 项');

    // The bounded-read meter mirrors the stated byte range: 64 KB of 128 KB,
    // with one gauge tick per 64 KB request and no cap marker below 512 KB.
    await user.click(screen.getByRole('treeitem', { name: '打开文件 big.log' }));
    expect(await screen.findByText('已显示前 64 KB · 共 128 KB')).toBeInTheDocument();
    const meter = container.querySelector('.paw-files-preview__meter') as HTMLElement | null;
    expect(meter?.style.getPropertyValue('--paw-files-loaded')).toBe('50%');
    expect(meter?.style.getPropertyValue('--paw-files-tick')).toBe('50%');
    expect(meter?.querySelector('b')).toBeNull();
    expect(screen.getByText('50%')).toBeInTheDocument();

    // The filter chip repeats the truthful match count beside the query.
    await user.type(screen.getByRole('searchbox', { name: '筛选已加载的文件' }), 'guide');
    expect(container.querySelector('.paw-files-filter__count')?.textContent).toBe('1');
  });

  it('copies loaded file content and states the truncation boundary truthfully', async () => {
    const user = userEvent.setup();
    const writeText = vi.fn(async () => undefined);
    const hadClipboard = 'clipboard' in navigator;
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }],
        },
        'files.list': {
          ok: true,
          path: '/workspace/paw',
          items: [{ path: '/workspace/paw/big.log', name: 'big.log', kind: 'file', byteSize: 131_072 }],
        },
        'files.read': {
          ok: true,
          path: '/workspace/paw/big.log',
          content: 'line 1\nline 2\n',
          byteSize: 131_072,
          offset: 0,
          nextOffset: 65_536,
          truncated: true,
        },
      },
    });

    try {
      renderApp(transport, <PawOsFilesApp />);
      await user.click(await screen.findByRole('treeitem', { name: '打开文件 big.log' }));
      const copyContent = await screen.findByRole('button', { name: '复制文件内容' });
      expect(copyContent).toHaveAttribute('title', '复制已加载的前 64 KB 内容');
      await user.click(copyContent);
      await waitFor(() => expect(writeText).toHaveBeenCalledWith('line 1\nline 2\n'));
      expect(await screen.findByRole('button', { name: '已复制文件内容' })).toBeInTheDocument();
    } finally {
      if (!hadClipboard) delete (navigator as { clipboard?: unknown }).clipboard;
    }
  });

  it('announces clipboard failures instead of silently claiming no result', async () => {
    const user = userEvent.setup();
    const writeText = vi.fn(async () => { throw new Error('clipboard denied'); });
    const hadClipboard = 'clipboard' in navigator;
    const originalClipboard = navigator.clipboard;
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }],
        },
        'files.list': {
          ok: true,
          path: '/workspace/paw',
          items: [{ path: '/workspace/paw/notes.md', name: 'notes.md', kind: 'file', byteSize: 8 }],
        },
        'files.read': {
          ok: true,
          path: '/workspace/paw/notes.md',
          content: '# notes',
          byteSize: 8,
          truncated: false,
        },
      },
    });

    try {
      renderApp(transport, <PawOsFilesApp />);
      await user.click(await screen.findByRole('treeitem', { name: '打开文件 notes.md' }));
      await user.click(await screen.findByRole('button', { name: '复制文件路径' }));

      expect(await screen.findByRole('alert')).toHaveTextContent('无法访问剪贴板，文件路径没有复制');
      expect(screen.getByRole('button', { name: '复制文件路径' })).toBeInTheDocument();
    } finally {
      Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: hadClipboard ? originalClipboard : undefined,
      });
    }
  });

  it('refuses to copy binary payloads as text', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'agent.sessions.list': {
          ok: true,
          activeSessionId: 'session-work',
          items: [{ id: 'session-work', title: 'PAWOS', updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' }],
        },
        'files.list': {
          ok: true,
          path: '/workspace/paw',
          items: [{ path: '/workspace/paw/image.png', name: 'image.png', kind: 'file', byteSize: 900 }],
        },
        'files.read': {
          ok: true,
          path: '/workspace/paw/image.png',
          content: '\u0000PNG\r\n\u001a\n',
          byteSize: 900,
          truncated: false,
        },
      },
    });

    renderApp(transport, <PawOsFilesApp />);
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 image.png' }));

    expect(await screen.findByText('二进制文件不能作为文本预览。')).toBeInTheDocument();
    const copyContent = screen.getByRole('button', { name: '复制文件内容' });
    expect(copyContent).toBeDisabled();
    expect(copyContent).toHaveAttribute('title', '二进制内容不能复制为文本');
  });

  it.each([{ items: [] }, { items: [{ id: 'empty', title: '无项目对话', workspaceRoots: [], updatedAtMs: 0 }] }])('opens local folders regardless of available Session roots: %j', async ({ items }) => {
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': { ok: true, items },
      'files.list': { ok: true, scope: 'local', path: '/home', items: [{ name: 'Documents', path: '/home/Documents', kind: 'directory' }] },
    } });
    renderApp(transport, <PawOsFilesApp />);
    expect(await screen.findByRole('treeitem', { name: '展开目录 Documents' })).toBeInTheDocument();
    expect(screen.queryByText('这个 Session 尚未选择工作目录')).not.toBeInTheDocument();
    expect(screen.getByText('本机文件 · 无需 Session')).toBeInTheDocument();
  });

});

/** jsdom cannot evaluate @container queries, so the single-pane layout is
    driven by hand. These are the exact `@container paw-files (max-width:
    620px)` rules from paw-os-files-app.css that change behaviour: an open
    file replaces the rail, and the labelled Back pill appears. */
function applyNarrowLayout(): HTMLStyleElement {
  const emulation = document.createElement('style');
  emulation.textContent = `
    .paw-files-app__workspace:not([data-file-open]) .paw-files-preview { display: none; }
    .paw-files-app__workspace[data-file-open] .paw-files-tree { display: none; }
    .paw-files-app__workspace[data-tree-revealed]:not([data-sidebar-collapsed='true']) .paw-files-tree { display: flex; }
    .paw-files-app__workspace[data-tree-revealed]:not([data-sidebar-collapsed='true']) .paw-files-preview { display: none; }
    .paw-files-app__workspace[data-sidebar-collapsed='true'] .paw-files-preview { display: flex; }
    .paw-files-preview__back { display: inline-flex; }
  `;
  document.head.append(emulation);
  return emulation;
}

function renderApp(transport: MockControlTransport, child: React.ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrap = (content: React.ReactNode) => (
    <QueryClientProvider client={queryClient}>
      <ControlTransportProvider transport={transport}>
        <TooltipProvider>{content}</TooltipProvider>
      </ControlTransportProvider>
    </QueryClientProvider>
  );
  const view = render(wrap(child));
  return { ...view, rerender: (content: React.ReactNode) => view.rerender(wrap(content)) };
}

function scopedSessions() {
  return { ok: true, activeSessionId: 'writer', items: ['writer', 'reader', 'unbound'].map((id) => ({
    id, title: id, updatedAtMs: 1, status: 'idle', workspaceRoots: id === 'unbound' ? [] : ['/workspace/paw'],
  })) };
}
function filesRoute(sessionId: string, path: string) { return `/files?${new URLSearchParams({ session: sessionId, path })}`; }
function savedMetadataReceipt() {
  return { ok: true, saved: true, sessionId: 'writer', path: '/workspace/paw/notes.md', resourceRevision: `sha256:${'b'.repeat(64)}` };
}
function savedMetadataTransport(save: MockRouteHandler) {
  return new MockControlTransport({ routes: {
    'agent.sessions.list': scopedSessions(),
    'files.list': (request: ControlRequest) => ({ ok: true, path: request.query?.path, items: ['notes.md', 'alias.md', 'other.txt'].map((name) => ({
      path: `/workspace/paw/${name}`, name, kind: name === 'alias.md' ? 'symlink' : 'file',
      byteSize: name === 'other.txt' ? 10 : request.query?.sessionId === 'reader' ? 11 : 4,
    })) }),
    'files.read': (request: ControlRequest) => {
      const requestedPath = String(request.query?.path);
      const content = requestedPath.endsWith('other.txt') ? 'other text' : request.query?.sessionId === 'reader' ? 'reader disk' : 'disk';
      return { ok: true, sessionId: request.query?.sessionId, requestedPath, path: requestedPath.endsWith('alias.md') ? '/workspace/paw/notes.md' : requestedPath,
        content, byteSize: content.length, nextOffset: content.length, truncated: false,
        resourceRevision: `sha256:${'a'.repeat(64)}`, editability: { editable: true } };
    },
    'agent.session.workspace.save': save,
  } });
}
function scopedFilesTransport(sessionList: MockRouteHandler = scopedSessions()) {
  return new MockControlTransport({ routes: {
    'agent.sessions.list': sessionList,
    'files.list': (request: ControlRequest) => ({ ok: true, path: request.query?.path, items: request.query?.path === '/workspace/paw' ? [
      { path: '/workspace/paw/notes.md', name: 'notes.md', kind: 'file' },
      { path: '/workspace/paw/docs', name: 'docs', kind: 'directory' },
    ] : [] }),
    'files.read': (request: ControlRequest) => {
      const content = `# ${request.query?.sessionId} ${String(request.query?.path).split('/').at(-1)}`;
      return { ok: true, sessionId: request.query?.sessionId, path: request.query?.path, content,
        byteSize: content.length, nextOffset: content.length, truncated: false, resourceRevision: `sha256:${'a'.repeat(64)}`,
        editability: { editable: request.query?.sessionId === 'writer', reason: request.query?.sessionId === 'writer' ? '' : '此 Session 只读' } };
    },
  } });
}
