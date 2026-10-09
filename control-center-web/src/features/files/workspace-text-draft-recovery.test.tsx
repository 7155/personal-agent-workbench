import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport, type MockRouteHandler } from '@/test/mock-transport';
import { useWorkspaceTextEditor, type EditableWorkspacePreview } from './WorkspaceTextEditor';

const path = '/workspace/public/notes.md';
const revision = `sha256:${'a'.repeat(64)}`;
const nextRevision = `sha256:${'b'.repeat(64)}`;
const identity = { sessionId: 'session-public-a', path, name: 'notes.md' };
function preview(content = 'Original text', resourceRevision = revision, resourcePath = path): EditableWorkspacePreview {
  const byteSize = new TextEncoder().encode(content).length;
  return { path: resourcePath, canonicalPath: resourcePath, content, resourceRevision, byteSize, loadedBytes: byteSize, truncated: false, editability: { editable: true } };
}
const readReply = (snapshot = preview()) => ({ ok: true, ...snapshot, nextOffset: snapshot.loadedBytes });
function transport(connectionIdentity = 'files-draft-public-backend', read: MockRouteHandler = () => readReply(), save: MockRouteHandler = (request: ControlRequest) => ({ ok: true, saved: true, sessionId: request.params?.sessionId, path: (request.body as { path: string }).path, resourceRevision: nextRevision })) {
  const result = new MockControlTransport({ routes: { 'agent.session.workspace.read': read, 'agent.session.workspace.save': save } });
  if (connectionIdentity) Object.defineProperty(result, 'connectionIdentity', { value: connectionIdentity });
  return result;
}
function Editor({ file = identity, snapshot = preview() }: { file?: typeof identity; snapshot?: EditableWorkspacePreview }) {
  const state = useWorkspaceTextEditor(file, snapshot);
  return <>{state.panel}</>;
}
function mount(backend = transport(), file = identity, snapshot = preview()) {
  const view = render(<ControlTransportProvider transport={backend}><Editor file={file} snapshot={snapshot} /></ControlTransportProvider>);
  return { ...view, backend };
}
async function edit(text = 'Unsaved public draft') {
  fireEvent.click(screen.getByRole('button', { name: '编辑文本' }));
  const input = await screen.findByRole('textbox', { name: '编辑 notes.md' });
  fireEvent.change(input, { target: { value: text } });
  return input;
}
const saves = (backend: MockControlTransport) => backend.requests.filter(({ request }) => request.pathId === 'agent.session.workspace.save');
afterEach(() => { cleanup(); vi.restoreAllMocks(); localStorage.clear(); });

describe('Files draft cold recovery', () => {
  it('restores exact unsaved input with a new transport object and requires a read before manual save', async () => {
    const original = mount();
    const text = '未保存的公开草稿\nSecond line';
    await edit(text);
    original.unmount();
    const reopened = mount();
    expect(await screen.findByRole('textbox', { name: '编辑 notes.md' })).toHaveValue(text);
    expect(reopened.backend.requests).toHaveLength(0);
    expect(screen.getByRole('button', { name: '保存文件' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '核对磁盘版本' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '保存文件' })).toBeEnabled());
    expect(reopened.backend.requests[0].request).toMatchObject({ pathId: 'agent.session.workspace.read', params: { sessionId: identity.sessionId }, query: { path } });
    expect(saves(original.backend)).toHaveLength(0);
    expect(saves(reopened.backend)).toHaveLength(0);
  });

  it.each(['connection', 'session', 'path', 'kind'] as const)('isolates the %s binding without deleting the original owner draft', async (scope) => {
    const original = mount();
    await edit('Only original scope');
    original.unmount();
    const otherTransport = transport(scope === 'connection' ? 'other-public-backend' : undefined);
    if (scope === 'kind') Object.defineProperty(otherTransport, 'kind', { value: 'http' });
    const otherFile = { ...identity, ...(scope === 'session' ? { sessionId: 'session-public-b' } : {}), ...(scope === 'path' ? { path: '/workspace/public/other.md' } : {}) };
    const other = mount(otherTransport, otherFile, preview('Other disk', revision, otherFile.path));
    expect(screen.queryByRole('textbox', { name: '编辑 notes.md' })).toBeNull();
    expect(screen.getByRole('button', { name: '编辑文本' })).toBeEnabled();
    expect(other.backend.requests).toHaveLength(0);
    other.unmount();
    mount();
    expect(await screen.findByRole('textbox', { name: '编辑 notes.md' })).toHaveValue('Only original scope');
  });

  it('keeps an anonymous transport usable without sharing unbound cold drafts', async () => {
    const original = mount(transport(''));
    await edit();
    original.unmount();
    const reopened = mount(transport(''));
    expect(screen.queryByRole('textbox', { name: '编辑 notes.md' })).toBeNull();
    expect(localStorage.length).toBe(0);
    expect(reopened.backend.requests).toHaveLength(0);
  });

  it('retains the original draft during a new disk conflict and saves only after explicit version choice', async () => {
    const original = mount();
    await edit('Keep this draft');
    original.unmount();
    const latest = preview('New disk text', nextRevision);
    const reopened = mount(transport(undefined, () => readReply(latest)), identity, latest);
    expect(await screen.findByRole('textbox', { name: '编辑 notes.md' })).toHaveValue('Keep this draft');
    fireEvent.click(screen.getByRole('button', { name: '核对磁盘版本' }));
    expect(await screen.findByText('磁盘当前内容')).toBeInTheDocument();
    expect(screen.getByText('New disk text')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '保存文件' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '保留草稿，基于此版本继续编辑' }));
    fireEvent.click(screen.getByRole('button', { name: '保存文件' }));
    await screen.findByText('已保存到文件。');
    expect(saves(reopened.backend)).toHaveLength(1);
    expect(saves(reopened.backend)[0].request.body).toEqual({ path, content: 'Keep this draft', resourceRevision: nextRevision });
    reopened.unmount();
    mount(transport(), identity, preview('Keep this draft', nextRevision));
    expect(screen.queryByRole('textbox', { name: '编辑 notes.md' })).toBeNull();
    expect(screen.getByRole('button', { name: '编辑文本' })).toBeEnabled();
  });

  it('clears recovery only when the user explicitly abandons the draft', async () => {
    const original = mount();
    const input = await edit('Abandoned draft');
    const discard = screen.getByRole('button', { name: '放弃草稿' });
    discard.focus();
    fireEvent.click(discard);
    expect(input).toHaveValue('Original text');
    expect(input).toHaveFocus();
    expect(saves(original.backend)).toHaveLength(0);
    original.unmount();
    mount();
    expect(screen.queryByRole('textbox', { name: '编辑 notes.md' })).toBeNull();
  });

  it('uses the accepted canonical target after an alias moves during a cold reopen', async () => {
    const alias = '/workspace/public/alias.md';
    const file = { ...identity, path: alias };
    const first = { ...preview(), path: alias, canonicalPath: path };
    const original = mount(transport(), file, first);
    await edit('Canonical draft');
    original.unmount();
    const moved = { ...preview('Another file', nextRevision, '/workspace/public/other.md'), path: alias };
    const reopened = mount(transport(undefined, () => ({ ...readReply(), sessionId: identity.sessionId, requestedPath: path })), file, moved);
    expect(await screen.findByRole('textbox', { name: '编辑 notes.md' })).toHaveValue('Canonical draft');
    fireEvent.click(screen.getByRole('button', { name: '核对磁盘版本' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '保存文件' })).toBeEnabled());
    expect(reopened.backend.requests[0].request.query?.path).toBe(path);
    fireEvent.click(screen.getByRole('button', { name: '保存文件' }));
    await screen.findByText('已保存到文件。');
    expect(saves(reopened.backend)[0].request.body).toEqual({ path, content: 'Canonical draft', resourceRevision: revision });
  });

  it('reconciles an unknown earlier save by original readback without replaying a write', async () => {
    const original = mount(transport(undefined, undefined, () => { throw new Error('Public ACK unknown'); }));
    await edit('Possibly saved text');
    fireEvent.click(screen.getByRole('button', { name: '保存文件' }));
    await screen.findByRole('alert');
    original.unmount();
    const saved = preview('Possibly saved text', nextRevision);
    const reopened = mount(transport(undefined, () => readReply(saved)), identity, saved);
    expect(await screen.findByRole('textbox', { name: '编辑 notes.md' })).toHaveValue('Possibly saved text');
    expect(reopened.backend.requests).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: '核对磁盘版本' }));
    await screen.findByText('磁盘内容与上次提交一致。');
    expect(screen.getByRole('button', { name: '保存文件' })).toBeDisabled();
    expect(saves(original.backend)).toHaveLength(1);
    expect(saves(reopened.backend)).toHaveLength(0);
    reopened.unmount();
    mount(transport(), identity, saved);
    expect(screen.queryByRole('textbox', { name: '编辑 notes.md' })).toBeNull();
  });

  it('does not clear newer input when a held earlier save is acknowledged', async () => {
    let finish!: (value: unknown) => void;
    const original = mount(transport(undefined, undefined, () => new Promise(resolve => { finish = resolve; })));
    const input = await edit('Submitted text');
    fireEvent.click(screen.getByRole('button', { name: '保存文件' }));
    fireEvent.change(input, { target: { value: 'Newer unsaved input' } });
    await act(async () => finish({ ok: true, saved: true, sessionId: identity.sessionId, path, resourceRevision: nextRevision }));
    expect(input).toHaveValue('Newer unsaved input');
    original.unmount();
    mount(transport(), identity, preview('Submitted text', nextRevision));
    expect(await screen.findByRole('textbox', { name: '编辑 notes.md' })).toHaveValue('Newer unsaved input');
    expect(saves(original.backend)).toHaveLength(1);
  });

  it('preserves the current input and focus when local persistence is unavailable', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('Unavailable storage'); });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('Quota fixture'); });
    const original = mount();
    const input = await edit('Current usable input');
    input.focus();
    fireEvent.change(input, { target: { value: 'Still usable after storage failed' } });
    expect(screen.getByRole('textbox', { name: '编辑 notes.md' })).toBe(input);
    expect(input).toHaveFocus();
    expect(input).toHaveValue('Still usable after storage failed');
    expect(screen.getByText(/关闭前请复制/u)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '保存文件' })).toBeEnabled();
    expect(saves(original.backend)).toHaveLength(0);
  });

  it('keeps a reopened owner draft durable after a closed view receives its older save receipt', async () => {
    let finish!: (value: unknown) => void;
    const backend = transport(undefined, undefined, () => new Promise(resolve => { finish = resolve; }));
    const original = mount(backend);
    await edit('Earlier submitted text');
    fireEvent.click(screen.getByRole('button', { name: '保存文件' }));
    original.unmount();
    const reopened = mount(backend);
    const input = await screen.findByRole('textbox', { name: '编辑 notes.md' });
    fireEvent.change(input, { target: { value: 'New input from reopened view' } });
    await act(async () => finish({ ok: true, saved: true, sessionId: identity.sessionId, path, resourceRevision: nextRevision }));
    expect(input).toHaveValue('New input from reopened view');
    reopened.unmount();
    mount(transport(), identity, preview('Earlier submitted text', nextRevision));
    expect(await screen.findByRole('textbox', { name: '编辑 notes.md' })).toHaveValue('New input from reopened view');
    expect(saves(backend)).toHaveLength(1);
  });

  it('retains input when the current canonical read denies writing despite an older writable preview', async () => {
    const original = mount();
    await edit('Retained readonly draft');
    original.unmount();
    const reopened = mount(transport(undefined, () => ({ ...readReply(), editability: { editable: false, reason: 'Current Session is readonly.' } })));
    expect(await screen.findByRole('textbox', { name: '编辑 notes.md' })).toHaveValue('Retained readonly draft');
    fireEvent.click(screen.getByRole('button', { name: '核对磁盘版本' }));
    expect(await screen.findByText('Current Session is readonly.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '保存文件' })).toBeDisabled();
    fireEvent.keyDown(screen.getByRole('textbox', { name: '编辑 notes.md' }), { key: 's', ctrlKey: true });
    expect(saves(reopened.backend)).toHaveLength(0);
    expect(screen.getByRole('textbox', { name: '编辑 notes.md' })).toHaveValue('Retained readonly draft');
  });
});
