import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport, type MockControlTransportOptions, type MockRouteHandler } from '@/test/mock-transport';
import { PawOsFilesApp } from './PawOsFilesApp';

afterEach(cleanup);
const firstRevision = `sha256:${'a'.repeat(64)}`;
const nextRevision = `sha256:${'b'.repeat(64)}`;
const filePath = '/workspace/paw/notes.md';
function readResult(content = 'original', revision = firstRevision, path = filePath) {
  const byteSize = new TextEncoder().encode(content).length;
  return { ok: true, path, content, resourceRevision: revision, byteSize, nextOffset: byteSize, truncated: false, editability: { editable: true, maxBytes: 2 * 1024 * 1024 } };
}
function setup(read: MockRouteHandler = () => readResult(), save: MockRouteHandler = () => ({ ok: true, saved: true, sessionId: 'session-a', path: filePath, resourceRevision: nextRevision }), options: { initialRoute?: string; routes?: MockControlTransportOptions['routes'] } = {}) {
  const routes = {
    'agent.sessions.list': { ok: true, activeSessionId: 'session-a', items: ['a', 'b'].map((id) => ({ id: `session-${id}`, title: `Session ${id.toUpperCase()}`, updatedAtMs: 1, workspaceRoots: ['/workspace/paw'], status: 'idle' })) },
    'agent.session.workspace.list': { ok: true, items: ['notes.md', 'other.txt'].map((name) => ({ path: `/workspace/paw/${name}`, name, kind: 'file' })) },
    'agent.session.workspace.read': read,
    'agent.session.workspace.save': save,
    ...options.routes,
  };
  // Preview/list now use independent local routes; the complete editor and saves
  // retain their Session routes and canonical ownership checks.
  const transport = new MockControlTransport({ routes: { ...routes,
    'files.list': async (request: ControlRequest) => {
      if (!request.query?.path) return { ok: true, path: '/home/qa', homePath: '/home/qa', items: [] };
      const handler = routes['agent.session.workspace.list'];
      const response = typeof handler === 'function' ? await handler(request) : handler;
      const path = String(request.query.path);
      const file = /\.(md|txt|html)$/u.test(path);
      return { ...(response as object), path: file ? '/workspace/paw' : path, selectedPath: file ? path : '' };
    },
    'files.read': (request: ControlRequest) => typeof read === 'function' ? read({ ...request, params: { sessionId: String(request.query?.sessionId ?? '') } }) : read,
  } });
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><ControlTransportProvider transport={transport}><TooltipProvider><PawOsFilesApp initialRoute={options.initialRoute} /></TooltipProvider></ControlTransportProvider></QueryClientProvider>);
  return transport;
}
async function openEditor() {
  const user = userEvent.setup();
  await user.click(await screen.findByRole('treeitem', { name: '打开文件 notes.md' }));
  await user.click(await screen.findByRole('button', { name: '编辑文本' }));
  return { user, editor: await screen.findByRole('textbox', { name: '编辑 notes.md' }) };
}

describe('Files text editing', () => {
  it('keeps low-frequency file details collapsed while the original editor draft and save remain available', async () => {
    const transport = setup();
    const { user, editor } = await openEditor();
    fireEvent.change(editor, { target: { value: 'visible editing draft' } });
    const location = screen.getByText('文件夹位置').closest('details')!;
    const identity = screen.getByRole('heading', { name: 'notes.md', level: 2 }).closest('details')!;
    expect(location).not.toHaveAttribute('open');
    expect(identity).not.toHaveAttribute('open');
    expect(screen.getByRole('button', { name: '保存文件' })).toBeEnabled();
    // Native disclosure state may change without replacing the editor owner.
    location.open = true;
    identity.open = true;
    fireEvent(location, new Event('toggle'));
    fireEvent(identity, new Event('toggle'));
    expect(screen.getByRole('textbox', { name: '文件或文件夹路径' })).toBeVisible();
    expect(screen.getByRole('textbox', { name: '编辑 notes.md' })).toBe(editor);
    expect(editor).toHaveValue('visible editing draft');
    await user.click(screen.getByRole('button', { name: '协作与访问' }));
    expect(screen.getByRole('textbox', { name: '编辑 notes.md' })).toBe(editor);
    expect(editor).toHaveValue('visible editing draft');
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.workspace.save')).toHaveLength(0);
  });

  it('renders the unsaved Markdown draft as a reading page and keeps it when editing resumes', async () => {
    const transport = setup(() => readResult('# 磁盘标题\n\n原文。'));
    const { user, editor } = await openEditor();
    const draft = '# 未保存的标题\n\n- 第一项\n- 第二项\n\n继续完善这份文档。';
    fireEvent.change(editor, { target: { value: draft } });
    const reads = transport.requests.filter(({ request }) => request.pathId === 'agent.session.workspace.read').length;
    await user.click(screen.getByRole('button', { name: '预览草稿' }));
    expect(await screen.findByRole('heading', { name: '未保存的标题', level: 1 })).toBeInTheDocument();
    expect(screen.getByText('第一项').closest('li')).not.toBeNull();
    expect(screen.queryByRole('heading', { name: '磁盘标题' })).not.toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: '编辑 notes.md' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '继续编辑' }));
    expect(await screen.findByRole('textbox', { name: '编辑 notes.md' })).toHaveValue(draft);
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.workspace.read')).toHaveLength(reads);
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.workspace.save')).toHaveLength(0);
  });

  it('renders the unsaved HTML draft through the existing report preview and resumes its exact source', async () => {
    const htmlPath = '/workspace/paw/report.html';
    const transport = setup(() => readResult('<h1>磁盘报告</h1>', firstRevision, htmlPath), undefined, {
      routes: { 'agent.session.workspace.list': { ok: true, items: [{ path: htmlPath, name: 'report.html', kind: 'file' }] } },
    });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 report.html' }));
    await user.click(await screen.findByRole('button', { name: '编辑文本' }));
    const draft = '<h1>未保存的报告</h1><p>报告正文</p><button>报告交互</button>';
    fireEvent.change(await screen.findByRole('textbox', { name: '编辑 report.html' }), { target: { value: draft } });
    const reads = transport.requests.filter(({ request }) => request.pathId === 'agent.session.workspace.read').length;
    await user.click(screen.getByRole('button', { name: '预览草稿' }));
    const frame = await screen.findByTitle('report.html 交互预览');
    const source = frame.getAttribute('src') ?? '';
    expect(source).toMatch(/^\/__paw_html_preview#/);
    const encoded = source.split('#')[1].replaceAll('-', '+').replaceAll('_', '/');
    const rendered = new DOMParser().parseFromString(new TextDecoder().decode(Uint8Array.from(atob(encoded), (value) => value.charCodeAt(0))), 'text/html');
    expect(rendered.querySelector('h1')?.textContent).toBe('未保存的报告');
    expect(rendered.querySelector('button')?.textContent).toBe('报告交互');
    expect(rendered.body.textContent).not.toContain('磁盘报告');
    await user.click(screen.getByRole('button', { name: '继续编辑' }));
    expect(await screen.findByRole('textbox', { name: '编辑 report.html' })).toHaveValue(draft);
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.workspace.read')).toHaveLength(reads);
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.workspace.save')).toHaveLength(0);
  });

  it.each(['tree alias', 'route alias', 'normalized route'])('opens %s with its bound canonical identity and saves and finds relations for the real target', async (entry) => {
    const alias = entry === 'normalized route' ? '/workspace/paw/./notes.md' : '/workspace/paw/alias.md';
    const name = entry === 'normalized route' ? 'notes.md' : 'alias.md';
    const transport = setup((request: ControlRequest) => ({ ...readResult(), requestedPath: request.query?.path, sessionId: request.params?.sessionId }), undefined, {
      initialRoute: entry === 'tree alias' ? undefined : `/files?session=session-a&path=${encodeURIComponent(alias)}`,
      routes: {
        'agent.session.workspace.list': { ok: true, items: [{ path: alias, name, kind: entry === 'normalized route' ? 'file' : 'symlink' }] },
        'workDocuments.list': { schemaVersion: 'rag-ime.work-document-list.v1', total: 1, items: [{
          documentId: `workdoc_${'a'.repeat(32)}`, workspaceRoot: '/workspace/paw', path: 'notes.md', activePath: 'notes.md', archivePath: 'archive/notes.md',
          title: '真实目标文档', state: 'active', authorityKind: 'session_todo', authorityId: 'session-a', authorityKey: 'session_todo:session-a',
          authorityRevision: 1, documentRevision: 1, contentSha256: 'a'.repeat(64), terminalReceiptId: '', error: '', createdAtMs: 1, updatedAtMs: 1,
        }] },
      },
    });
    const user = userEvent.setup();
    if (entry === 'tree alias') await user.click(await screen.findByRole('treeitem', { name: `打开符号链接 ${name}` }));
    await user.click(await screen.findByRole('button', { name: '编辑文本' }));
    const editor = await screen.findByRole('textbox', { name: `编辑 ${name}` });
    expect(screen.getByText(`实际文件：${filePath}`)).toBeInTheDocument();
    fireEvent.change(editor, { target: { value: 'canonical update' } });
    await user.click(screen.getByRole('button', { name: '保存文件' }));
    expect(await screen.findByText('已保存到文件。')).toBeInTheDocument();
    expect(transport.requests.find(({ request }) => request.pathId === 'agent.session.workspace.save')?.request.body).toEqual({ path: filePath, content: 'canonical update', resourceRevision: firstRevision });
    await user.click(screen.getByRole('button', { name: '协作与访问' }));
    expect(await screen.findByText('真实目标文档')).toBeInTheDocument();
  });

  it.each(['missing', 'wrong request', 'wrong Session'])('rejects a canonical response with %s owner binding', async (binding) => {
    const alias = '/workspace/paw/alias.md';
    setup(() => ({ ...readResult(), ...(binding === 'missing' ? {} : { requestedPath: binding === 'wrong request' ? '/workspace/paw/other-alias.md' : alias, sessionId: binding === 'wrong Session' ? 'session-b' : 'session-a' }) }), undefined, { initialRoute: `/files?session=session-a&path=${encodeURIComponent(alias)}` });
    expect(await screen.findByText('文件服务返回了无法识别的数据。')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '编辑文本' })).not.toBeInTheDocument();
  });

  it('rejects an alias retargeted to an equal-content different resource during complete reading', async () => {
    const alias = '/workspace/paw/alias.md';
    setup((request: ControlRequest) => Number(request.query?.offset) === 0
      ? { ...readResult('first\n'), requestedPath: alias, sessionId: 'session-a', byteSize: 13, nextOffset: 6, truncated: true }
      : { ...readResult('second\n', firstRevision, '/workspace/paw/other.txt'), requestedPath: alias, sessionId: 'session-a', byteSize: 13, offset: 6, nextOffset: 13 }, undefined, { initialRoute: `/files?session=session-a&path=${encodeURIComponent(alias)}` });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: '读取完整文件后编辑' }));
    expect(await screen.findByText(/读取期间文件已变化/)).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: '编辑 alias.md' })).not.toBeInTheDocument();
  });

  it('reconciles an uncertain alias save against its original canonical target after the alias moves', async () => {
    const alias = '/workspace/paw/alias.md';
    let aliasMoved = false;
    const transport = setup((request: ControlRequest) => ({
      ...readResult(aliasMoved ? 'saved draft' : 'original', aliasMoved ? nextRevision : firstRevision,
        aliasMoved && request.query?.path === alias ? '/workspace/paw/other.txt' : filePath),
      requestedPath: request.query?.path, sessionId: request.params?.sessionId,
    }), () => { aliasMoved = true; throw new Error('lost response'); }, { initialRoute: `/files?session=session-a&path=${encodeURIComponent(alias)}` });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: '编辑文本' }));
    const editor = await screen.findByRole('textbox', { name: '编辑 alias.md' });
    fireEvent.change(editor, { target: { value: 'saved draft' } });
    await user.click(screen.getByRole('button', { name: '保存文件' }));
    await user.click(await screen.findByRole('button', { name: '核对磁盘版本' }));
    expect(await screen.findByText('磁盘内容与上次提交一致。')).toBeInTheDocument();
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.workspace.read').at(-1)?.request.query?.path).toBe(filePath);
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.workspace.save')).toHaveLength(1);
    expect(screen.getByText(`实际文件：${filePath}`)).toBeInTheDocument();
  });

  it('saves the authorized file snapshot and keeps newer typing until its own receipt', async () => {
    let finish!: (value: unknown) => void;
    const transport = setup(undefined, () => new Promise((resolve) => { finish = resolve; }));
    const { user, editor } = await openEditor();
    fireEvent.change(editor, { target: { value: 'my edit' } });
    await user.click(screen.getByRole('button', { name: '保存文件' }));
    expect(editor).toHaveValue('my edit');
    expect(screen.getByRole('button', { name: '保存文件' })).toHaveAttribute('aria-busy', 'true');
    fireEvent.change(editor, { target: { value: 'newer typing' } });
    await act(async () => finish({ ok: true, saved: true, sessionId: 'session-a', path: filePath, resourceRevision: nextRevision }));
    expect(editor).toHaveValue('newer typing');
    expect(screen.getByRole('button', { name: '保存文件' })).toBeEnabled();
    fireEvent.keyDown(editor, { key: 's', ctrlKey: true });
    const saves = transport.requests.filter(({ request }) => request.pathId === 'agent.session.workspace.save');
    await waitFor(() => expect(saves).toHaveLength(2));
    expect(saves.map(({ request }) => request.body)).toEqual([
      { path: filePath, content: 'my edit', resourceRevision: firstRevision },
      { path: filePath, content: 'newer typing', resourceRevision: nextRevision },
    ]);
    expect(saves.every(({ request }) => request.params?.sessionId === 'session-a')).toBe(true);
    await act(async () => finish({ ok: true, saved: true, sessionId: 'session-a', path: filePath, resourceRevision: firstRevision }));
  });

  it('keeps the original clicked save action focused and named while one request is held', async () => {
    let finish!: (value: unknown) => void;
    const transport = setup(undefined, () => new Promise((resolve) => { finish = resolve; }));
    const { user, editor } = await openEditor();
    fireEvent.change(editor, { target: { value: 'held original draft' } });
    const save = screen.getByRole('button', { name: '保存文件' });
    await user.click(save);
    expect(save).toBeEnabled();
    expect(save).toHaveFocus();
    expect(save).toHaveAccessibleName('保存文件');
    expect(screen.getByRole('button', { name: '保存文件' })).toBe(save);
    expect(save).toHaveAttribute('aria-disabled', 'true');
    expect(save).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByRole('status')).toHaveTextContent('正在保存…');
    await user.click(save);
    await user.keyboard('{Enter} ');
    expect(save).toHaveFocus();
    await user.click(editor);
    await user.keyboard('{Meta>}s{/Meta}{Control>}s{/Control}');
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.workspace.save')).toHaveLength(1);
    await act(async () => finish({ ok: true, saved: true, sessionId: 'session-a', path: filePath, resourceRevision: nextRevision }));
    expect(save).not.toHaveAttribute('aria-busy');
    expect(save).not.toHaveAttribute('aria-disabled');
    expect(save).toBeDisabled();
    expect(editor).toHaveValue('held original draft');
  });

  it('retains a visible draft across file and Session switches even when the reread fails', async () => {
    let readFails = false;
    const transport = setup((request: ControlRequest) => {
      if (readFails && request.query?.path === filePath) throw new Error('read offline');
      return readResult('original', firstRevision, String(request.query?.path));
    });
    const { user, editor } = await openEditor();
    fireEvent.change(editor, { target: { value: 'recover this draft' } });
    await user.click(screen.getByRole('treeitem', { name: '打开文件 other.txt' }));
    await user.selectOptions(screen.getByRole('combobox', { name: /Session/ }), 'session-b');
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 notes.md' }));
    expect(screen.queryByDisplayValue('recover this draft')).not.toBeInTheDocument();
    readFails = true;
    await user.selectOptions(screen.getByRole('combobox', { name: /Session/ }), 'session-a');
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 notes.md' }));
    expect(await screen.findByRole('textbox', { name: '编辑 notes.md' })).toHaveValue('recover this draft');
    expect(await screen.findByText('read offline')).toBeInTheDocument();
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.workspace.save')).toHaveLength(0);
  });

  it('shows an external version alongside the draft and rebases only after an explicit choice', async () => {
    let disk = 'original';
    let revision = firstRevision;
    const transport = setup(() => readResult(disk, revision), () => { throw Object.assign(new Error('文件已被其他任务修改'), { status: 409, payload: { errorCode: 'stale_snapshot' } }); });
    const { user, editor } = await openEditor();
    fireEvent.change(editor, { target: { value: 'my draft' } });
    disk = 'external edit'; revision = nextRevision;
    await user.click(screen.getByRole('button', { name: '保存文件' }));
    expect(editor).toHaveValue('my draft');
    await user.click(await screen.findByRole('button', { name: '核对磁盘版本' }));
    expect(await screen.findByText('external edit')).toBeInTheDocument();
    expect(editor).toHaveValue('my draft');
    expect(screen.getByRole('button', { name: '保存文件' })).toBeDisabled();
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.workspace.save')).toHaveLength(1);
    await user.click(screen.getByRole('button', { name: '保留草稿，基于此版本继续编辑' }));
    await user.click(screen.getByRole('button', { name: '保存文件' }));
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.workspace.save').at(-1)?.request.body).toEqual({ path: filePath, content: 'my draft', resourceRevision: nextRevision });
  });

  it('reconciles a lost save response by reading the file without automatically saving again', async () => {
    let disk = 'original';
    const transport = setup(() => readResult(disk, disk === 'original' ? firstRevision : nextRevision), (request: ControlRequest) => {
      disk = String((request.body as { content: string }).content);
      throw new Error('connection lost');
    });
    const { user, editor } = await openEditor();
    fireEvent.change(editor, { target: { value: 'actually written' } });
    await user.click(screen.getByRole('button', { name: '保存文件' }));
    expect(editor).toHaveValue('actually written');
    expect(screen.getByRole('button', { name: '保存文件' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: '保存文件' }));
    fireEvent.keyDown(editor, { key: 's', metaKey: true });
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.workspace.save')).toHaveLength(1);
    await user.click(await screen.findByRole('button', { name: '核对磁盘版本' }));
    expect(await screen.findByText('磁盘内容与上次提交一致。')).toBeInTheDocument();
    expect(editor).toHaveValue('actually written');
    expect(screen.getByRole('button', { name: '保存文件' })).toBeDisabled();
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.workspace.save')).toHaveLength(1);
  });

  it('reads all remaining chunks before enabling editing and rejects mixed revisions', async () => {
    let stable = false;
    const transport = setup((request: ControlRequest) => Number(request.query?.offset) === 0
      ? { ...readResult('part'), byteSize: 9, nextOffset: 4, truncated: true }
      : { ...readResult(' tail', stable ? firstRevision : nextRevision), byteSize: 9, offset: 4, nextOffset: 9 });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 notes.md' }));
    await user.click(await screen.findByRole('button', { name: '读取完整文件后编辑' }));
    expect(await screen.findByText(/读取期间文件已变化/)).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: '编辑 notes.md' })).not.toBeInTheDocument();
    stable = true;
    await user.click(screen.getByRole('button', { name: '读取完整文件后编辑' }));
    expect(await screen.findByRole('textbox', { name: '编辑 notes.md' })).toHaveValue('part tail');
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.workspace.save')).toHaveLength(0);
  });

  it('explains a read-only file without presenting a save action', async () => {
    setup(() => ({ ...readResult(), editability: { editable: false, reason: '当前 Session 只读。', maxBytes: 2 * 1024 * 1024 } }));
    const user = userEvent.setup();
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 notes.md' }));
    expect(await screen.findByText('当前 Session 只读。')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '编辑文本' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '保存文件' })).not.toBeInTheDocument();
  });

  it('keeps a late save receipt in its original Session while another Session edits the same path', async () => {
    let finish!: (value: unknown) => void;
    setup(undefined, () => new Promise((resolve) => { finish = resolve; }));
    const { user, editor } = await openEditor();
    fireEvent.change(editor, { target: { value: 'Session A draft' } });
    await user.click(screen.getByRole('button', { name: '保存文件' }));
    await user.selectOptions(screen.getByRole('combobox', { name: /Session/ }), 'session-b');
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 notes.md' }));
    await user.click(await screen.findByRole('button', { name: '编辑文本' }));
    const secondEditor = await screen.findByRole('textbox', { name: '编辑 notes.md' });
    fireEvent.change(secondEditor, { target: { value: 'Session B draft' } });
    await act(async () => finish({ ok: true, saved: true, sessionId: 'session-a', path: filePath, resourceRevision: nextRevision }));
    expect(secondEditor).toHaveValue('Session B draft');
    expect(screen.queryByText('已保存到文件。')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '保存文件' })).toBeEnabled();
    await user.selectOptions(screen.getByRole('combobox', { name: /Session/ }), 'session-a');
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 notes.md' }));
    expect(await screen.findByRole('textbox', { name: '编辑 notes.md' })).toHaveValue('Session A draft');
    expect(screen.getByRole('button', { name: '保存文件' })).toBeDisabled();
  });

  it('does not replace the draft with success from a receipt for another file', async () => {
    const transport = setup(undefined, () => ({ ok: true, saved: true, sessionId: 'session-a', path: '/workspace/paw/other.txt', resourceRevision: nextRevision }));
    const { user, editor } = await openEditor();
    fireEvent.change(editor, { target: { value: 'keep this exact draft' } });
    await user.click(screen.getByRole('button', { name: '保存文件' }));
    expect(await screen.findByRole('button', { name: '核对磁盘版本' })).toBeInTheDocument();
    expect(editor).toHaveValue('keep this exact draft');
    expect(screen.queryByText('已保存到文件。')).not.toBeInTheDocument();
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.workspace.save')).toHaveLength(1);
  });

  it('preserves CRLF line endings when the textarea normalizes the edited value', async () => {
    const transport = setup(() => readResult('first\r\nsecond\r\n'));
    const { user, editor } = await openEditor();
    fireEvent.change(editor, { target: { value: 'first\nchanged\n' } });
    await user.click(screen.getByRole('button', { name: '保存文件' }));
    expect(transport.requests.find(({ request }) => request.pathId === 'agent.session.workspace.save')?.request.body).toEqual({ path: filePath, content: 'first\r\nchanged\r\n', resourceRevision: firstRevision });
  });

  it('keeps the draft readable and blocks keyboard saving after refreshed permissions become read-only', async () => {
    let writable = true;
    const transport = setup(() => ({ ...readResult(), editability: { editable: writable, reason: writable ? '' : '当前 Session 只读。' } }));
    const { user, editor } = await openEditor();
    fireEvent.change(editor, { target: { value: 'draft before permission change' } });
    writable = false;
    await user.click(screen.getByRole('button', { name: '刷新文件' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '保存文件' })).toBeDisabled());
    fireEvent.keyDown(editor, { key: 's', ctrlKey: true });
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.workspace.save')).toHaveLength(0);
    expect(screen.getByText('当前 Session 只读。')).toBeInTheDocument();
    expect(editor).toHaveValue('draft before permission change');
  });

  it('counts the complete editor text instead of retaining the partial-preview line count', async () => {
    setup((request: ControlRequest) => Number(request.query?.offset) === 0
      ? { ...readResult('first\n'), byteSize: 19, nextOffset: 6, truncated: true }
      : { ...readResult('second\nthird\n'), byteSize: 19, offset: 6, nextOffset: 19 });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 notes.md' }));
    expect(await screen.findByText('已载 1 行')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '读取完整文件后编辑' }));
    const editor = await screen.findByRole('textbox', { name: '编辑 notes.md' });
    expect(editor).toHaveValue('first\nsecond\nthird\n');
    expect(screen.queryByText('已载 1 行')).not.toBeInTheDocument();
    expect(screen.getByText('3 行')).toBeInTheDocument();
    fireEvent.change(editor, { target: { value: 'first\nsecond\nthird\nfourth\n' } });
    expect(screen.getByText('4 行')).toBeInTheDocument();
  });

  it('exposes collapsed collaboration for the selected file without eagerly loading Session activity', async () => {
    const transport = setup((request: ControlRequest) => readResult('original', firstRevision, String(request.query?.path)));
    const user = userEvent.setup();
    await user.click(await screen.findByRole('treeitem', { name: '打开文件 notes.md' }));
    expect(await screen.findByRole('region', { name: 'notes.md 协作与访问' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '协作与访问' })).toHaveAttribute('aria-expanded', 'false');
    await user.click(screen.getByRole('treeitem', { name: '打开文件 other.txt' }));
    expect(await screen.findByRole('region', { name: 'other.txt 协作与访问' })).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'notes.md 协作与访问' })).not.toBeInTheDocument();
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.snapshot')).toBe(false);
  });
});
