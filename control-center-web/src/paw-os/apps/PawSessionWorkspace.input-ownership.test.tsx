import { forwardRef, type Key, type ReactNode } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import { parseAgentEvent } from '@/contracts/validators';
import { useAgentLiveStore } from '@/features/agent/state/live-store';
import type { ModelCatalog, SessionSummary } from '@/features/agent/types';
import type { ControlRequest } from '@/platform/transport';
import { StubControlTransport } from '@/test/stub-control-transport';
import { PawSessionWorkspace } from './PawSessionWorkspace';

// Only replace zero-height jsdom virtualization; the Workspace, Composer,
// recovery owner, timeline messages and transport admission remain real.
vi.mock('react-virtuoso', () => ({
  Virtuoso: forwardRef(function TestVirtuoso({ components, computeItemKey, context, data, itemContent, scrollerRef }: {
    components?: { Header?: (props: { context?: unknown }) => ReactNode; Footer?: () => ReactNode };
    computeItemKey?: (index: number, item: string) => Key;
    context?: unknown; data: string[]; itemContent: (index: number, item: string) => ReactNode;
    scrollerRef?: (scroller: HTMLElement | Window | null) => void;
  }, _ref) {
    const Header = components?.Header; const Footer = components?.Footer;
    return <div ref={node => scrollerRef?.(node)}>{Header ? <Header context={context} /> : null}
      {data.map((item, index) => <div key={computeItemKey?.(index, item) ?? index}>{itemContent(index, item)}</div>)}
      {Footer ? <Footer /> : null}</div>;
  }),
}));

afterEach(() => { cleanup(); useAgentLiveStore.setState({ projections: {} }); });

const history = ['第一条公开请求', '第二条公开请求'];
function deferred<T = unknown>() {
  let resolve!: (value: T) => void; let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function anchors() {
  return { items: history.map((text, index) => ({ entryId: `entry-${index}`, role: 'user', text, createdAtMs: index + 1 })) };
}
function sessionSnapshot(sessionId: string) {
  return { status: 'idle', liveEvents: [], lastSequence: 0, resumeToken: '',
    messages: history.flatMap((text, index) => ['user', 'assistant'].map(role => ({
      schemaVersion: 'rag-ime.agent-message.v1', id: role === 'user' ? `message-${index}` : `reply-${index}`, sessionId,
      turnId: `turn-${index}`, role, status: 'completed',
      blocks: [{ id: `${role}-text-${index}`, type: 'text', status: 'completed', presentationKind: 'plain_text', data: { text: role === 'user' ? text : `已处理请求 ${index + 1}` } }],
      attachments: [], citations: [], createdAtMs: index + 1, completedAtMs: index + 1,
    }))) };
}
function transportFor(sessionId: string, routes: ConstructorParameters<typeof StubControlTransport>[1] = {}) {
  return new StubControlTransport('mock', {
    'agent.session.snapshot': (request: ControlRequest) => sessionSnapshot(request.params?.sessionId ?? sessionId),
    'agent.session.models': {}, 'agent.session.commands': {}, 'agent.tools.list': {},
    'agent.runtime.get': { capabilities: { conversationRewrite: true } },
    'agent.session.forks.list': anchors(), 'agent.session.rewrite': { ok: true },
    'agent.session.prompt': new Promise(() => undefined), ...routes,
  });
}
function sessionRecord(id: string): SessionSummary {
  return { id, title: '输入归属', mode: 'coordinator', status: 'active', roleId: 'builder', roleVersion: '1',
    roleBookRevisionId: '', updatedAtMs: 1, workspaceRoots: ['/workspace/example'], executionMode: 'per_action', modelProfile: 'test/model' };
}
function workspace(transport: StubControlTransport, id: string, onSessionUpdated = vi.fn()) {
  const record = sessionRecord(id);
  return <ControlTransportProvider transport={transport}><TooltipProvider><PawSessionWorkspace record={record} recordId={id}
    onNewWork={vi.fn()} onSessionCreated={vi.fn()} onSessionUpdated={onSessionUpdated} /></TooltipProvider></ControlTransportProvider>;
}
async function edit(index = 0) { fireEvent.click((await screen.findAllByRole('button', { name: '修改这条消息' }, { timeout: 10_000 }))[index]!); }
function input() { return screen.getByRole('textbox', { name: '消息' }); }
function type(text: string) { fireEvent.change(input(), { target: { value: text } }); }
function enter() { fireEvent.keyDown(input(), { key: 'Enter' }); }
function prompts(transport: StubControlTransport) { return transport.requests.filter(request => request.pathId === 'agent.session.prompt'); }
const receipt = { id: 'media_input_attachment', name: 'input.txt', mimeType: 'text/plain', byteSize: 4 };
function paste() { fireEvent.paste(input(), { clipboardData: { files: [new File(['data'], receipt.name, { type: receipt.mimeType })], items: [], getData: () => '' } }); }
async function pick() {
  const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: '添加内容' }));
  await user.click(screen.getByRole('menuitem', { name: /选择附件/ }));
}

describe('Session composer asynchronous input ownership', () => {
  it.each(['primaryAssistant', 'primaryTask'] as const)('keeps %s directory controls visibly locked without sending a permission change', async kind => {
    const id = `locked-roots-${kind}`; const transport = transportFor(id, { 'agent.session.workspace.list': { items: [] } });
    const record = { ...sessionRecord(id), metadata: { [kind]: true } };
    render(<ControlTransportProvider transport={transport}><TooltipProvider><PawSessionWorkspace record={record} recordId={id}
      onNewWork={vi.fn()} onSessionCreated={vi.fn()} onSessionUpdated={vi.fn()} /></TooltipProvider></ControlTransportProvider>);
    await screen.findByRole('textbox', { name: '消息' });
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '对话工具' }));
    await user.click(screen.getByRole('menuitem', { name: '文件' }));
    const manage = screen.getByRole('button', { name: '管理工作区目录' });
    expect(manage).toBeDisabled();
    expect(manage).toHaveAccessibleDescription('工作区已在开始时确定。要使用其他目录，请返回入口新建工作。');
    await user.click(manage);
    expect(transport.requests.some(request => request.pathId === 'agent.session.mode.update')).toBe(false);
  });

  it.each([false, true])('retires the file tree when the same Session ID moves to another transport (pending refresh: %s)', async pendingRefresh => {
    const id = 'file-scope'; const root = '/workspace/example';
    const oldRefresh = deferred(); const newListing = deferred(); let oldReads = 0;
    const listing = (name: string) => ({ items: [{ name, path: `${root}/${name}`, kind: 'file', byteSize: 1 }] });
    const first = transportFor(id, { 'agent.session.workspace.list': () => ++oldReads === 1 ? listing('old.md') : oldRefresh.promise });
    const second = transportFor(id, { 'agent.session.workspace.list': () => newListing.promise });
    const user = userEvent.setup(); const view = render(workspace(first, id));
    await screen.findByRole('textbox', { name: '消息' });
    await user.click(screen.getByRole('button', { name: '对话工具' }));
    await user.click(screen.getByRole('menuitem', { name: '文件' }));
    await screen.findByRole('treeitem', { name: '预览文件 old.md' });
    if (pendingRefresh) {
      await user.click(screen.getByRole('button', { name: '刷新文件目录' }));
      await waitFor(() => expect(oldReads).toBe(2));
    }
    view.rerender(workspace(second, id));
    expect(screen.queryByRole('treeitem', { name: '预览文件 old.md' })).not.toBeInTheDocument();
    if (pendingRefresh) expect(first.requests.filter(request => request.pathId === 'agent.session.workspace.list')[1]?.signal?.aborted).toBe(true);
    await waitFor(() => expect(second.requests.filter(request => request.pathId === 'agent.session.workspace.list')).toHaveLength(1));
    await act(async () => newListing.resolve(listing('current.md')));
    await screen.findByRole('treeitem', { name: '预览文件 current.md' });
    if (pendingRefresh) await act(async () => oldRefresh.resolve(listing('late-old.md')));
    expect(screen.queryByRole('treeitem', { name: '预览文件 late-old.md' })).not.toBeInTheDocument();
  });

  it.each(['success', 'failure'] as const)('does not revive a cancelled edit after late anchor %s', async outcome => {
    const pending = deferred(); const transport = transportFor(`cancel-${outcome}`, { 'agent.session.forks.list': () => pending.promise });
    render(workspace(transport, `cancel-${outcome}`)); await edit();
    fireEvent.click(screen.getByRole('button', { name: '取消修改' })); type('取消后新写的草稿');
    await act(async () => outcome === 'success' ? pending.resolve(anchors()) : pending.reject(new Error('old anchor failed')));
    expect(input()).toHaveValue('取消后新写的草稿');
    expect(screen.queryByRole('button', { name: '取消修改' })).not.toBeInTheDocument();
    expect(screen.queryByText('old anchor failed')).not.toBeInTheDocument();
  });

  it.each(['success', 'failure'] as const)('keeps the newer edit after an older anchor %s', async outcome => {
    const first = deferred(); let requests = 0;
    const transport = transportFor(`new-edit-${outcome}`, { 'agent.session.forks.list': () => ++requests === 1 ? first.promise : anchors() });
    render(workspace(transport, `new-edit-${outcome}`)); await edit(0); await edit(1);
    await screen.findByText('发送后将从这里重新生成后续对话');
    await act(async () => outcome === 'success' ? first.resolve(anchors()) : first.reject(new Error('stale anchor')));
    expect(input()).toHaveValue(history[1]); enter();
    await waitFor(() => expect(transport.requests.find(request => request.pathId === 'agent.session.rewrite')?.body)
      .toMatchObject({ entryId: 'entry-1', message: history[1] }));
  });

  it.each(['record', 'transport'] as const)('fences edit completion when the %s scope changes', async scope => {
    const pending = deferred(); const transport = transportFor('old-owner', { 'agent.session.forks.list': () => pending.promise });
    const view = render(workspace(transport, 'old-owner')); await edit();
    const nextId = scope === 'record' ? 'new-owner' : 'old-owner';
    view.rerender(workspace(scope === 'transport' ? transportFor(nextId) : transport, nextId));
    type('新工作区草稿'); await act(async () => pending.resolve(anchors()));
    expect(input()).toHaveValue('新工作区草稿');
    expect(screen.queryByRole('button', { name: '取消修改' })).not.toBeInTheDocument();
  });

  it('preserves edits on Enter while the anchor is loading, then sends the resolved target exactly once', async () => {
    const pending = deferred(); const transport = transportFor('anchor-send', { 'agent.session.forks.list': () => pending.promise });
    render(workspace(transport, 'anchor-send')); await edit(); type('已经改好的请求');
    enter(); expect(input()).toHaveValue('已经改好的请求');
    expect(screen.getByRole('button', { name: /发送.*正在定位历史消息/ })).toBeDisabled();
    expect(transport.requests.some(request => request.pathId === 'agent.session.rewrite')).toBe(false);
    await act(async () => pending.resolve(anchors())); enter();
    await waitFor(() => expect(transport.requests.filter(request => request.pathId === 'agent.session.rewrite')).toHaveLength(1));
    expect(transport.requests.find(request => request.pathId === 'agent.session.rewrite')?.body).toMatchObject({ entryId: 'entry-0', message: '已经改好的请求' });
  });

  it.each([false, true])('restores a rejected rewrite only while its input is still empty (new draft: %s)', async fresh => {
    const pending = deferred(); const transport = transportFor(`rewrite-${fresh}`, { 'agent.session.rewrite': () => pending.promise });
    Object.assign(transport, { pasteImages: async () => [receipt] });
    render(workspace(transport, `rewrite-${fresh}`)); await edit(); await screen.findByText('发送后将从这里重新生成后续对话');
    paste(); await screen.findByText(receipt.name);
    type('待重试的改写'); enter(); expect(input()).toHaveValue('');
    if (fresh) type('下一条独立草稿');
    await act(async () => pending.reject(new Error('rewrite rejected')));
    await waitFor(() => expect(input()).toHaveValue(fresh ? '下一条独立草稿' : '待重试的改写'));
    expect(Boolean(screen.queryByRole('button', { name: '取消修改' }))).toBe(!fresh);
    expect(Boolean(screen.queryByText(receipt.name))).toBe(!fresh);
  });

  it.each(['picker', 'paste', 'drop'] as const)('holds %s input until the attachment receipt, then sends one complete prompt', async source => {
    const pending = deferred<typeof receipt[]>(); const transport = transportFor(`import-${source}`);
    Object.assign(transport, { pickFiles: () => pending.promise, pasteImages: () => pending.promise });
    render(workspace(transport, `import-${source}`)); await screen.findByRole('textbox', { name: '消息' }); type('与附件一起发送');
    if (source === 'picker') await pick();
    else if (source === 'paste') paste();
    else fireEvent.drop(input().closest('.agent-composer-wrap')!, { dataTransfer: { types: ['Files'], files: [new File(['data'], receipt.name)] } });
    enter(); expect(prompts(transport)).toHaveLength(0); expect(input()).toHaveValue('与附件一起发送');
    expect(screen.getByRole('button', { name: /发送.*正在导入附件/ })).toBeDisabled();
    await act(async () => pending.resolve([receipt])); await screen.findByText(receipt.name); enter();
    await waitFor(() => expect(prompts(transport)).toHaveLength(1));
    expect(prompts(transport)[0]?.body).toMatchObject({ message: '与附件一起发送', attachments: [receipt.id] });
  });

  it('does not queue partial text while an attachment is pending during a running turn', async () => {
    const pending = deferred<typeof receipt[]>();
    let running = false;
    const event = parseAgentEvent({ schemaVersion: 'rag-ime.agent-event.v1',
      eventId: 'busy-import:1', sessionId: 'busy-import', turnId: 'running', sequence: 1, createdAtMs: 5,
      eventType: 'text_delta', payload: { messageId: 'running:assistant', blockId: 'running:text', delta: '处理中' }, resumeToken: 'busy-import:1' });
    const transport = transportFor('busy-import', { 'agent.session.snapshot': () => ({ ...sessionSnapshot('busy-import'),
      ...(running ? { status: 'busy', liveEvents: [event], lastSequence: 1, resumeToken: 'busy-import:1' } : {}) }) });
    Object.assign(transport, { pasteImages: () => pending.promise }); render(workspace(transport, 'busy-import'));
    await waitFor(() => expect(transport.subscriptionCount('agent.session.events')).toBe(1));
    type('接下来分析附件'); paste();
    act(() => { running = true; transport.emit('agent.session.events', event); });
    expect(await screen.findByRole('button', { name: '停止本轮' }, { timeout: 10_000 })).toBeVisible();
    enter();
    expect(input()).toHaveValue('接下来分析附件'); expect(prompts(transport)).toHaveLength(0);
    expect(screen.queryByRole('status', { name: '等待当前执行完成后发送的消息' })).not.toBeInTheDocument();
    await act(async () => pending.resolve([receipt])); enter();
    await waitFor(() => expect(prompts(transport)).toHaveLength(1));
    expect(prompts(transport)[0]?.body).toMatchObject({ attachments: [receipt.id], delivery: 'followUp' });
  });

  it.each([false, true])('holds all repeated imports through picker completion/cancellation and later failure (cancel: %s)', async cancelled => {
    const first = deferred<typeof receipt[]>(); const second = deferred<typeof receipt[]>();
    const transport = transportFor('repeat-import'); Object.assign(transport, { pickFiles: () => first.promise, pasteImages: () => second.promise });
    render(workspace(transport, 'repeat-import')); await screen.findByRole('textbox', { name: '消息' }); type('保留文字'); await pick(); paste();
    await act(async () => first.resolve(cancelled ? [] : [receipt])); enter(); expect(prompts(transport)).toHaveLength(0);
    await act(async () => second.reject(new Error('attachment import failed')));
    expect(input()).toHaveValue('保留文字'); expect(await screen.findByRole('alert')).toHaveTextContent('附件');
    enter(); await waitFor(() => expect(prompts(transport)).toHaveLength(1));
    expect(prompts(transport)[0]?.body).toMatchObject({ message: '保留文字', attachments: cancelled ? [] : [receipt.id] });
  });

  it('releases the old transport import gate without attaching its late receipt to the next owner', async () => {
    const pending = deferred<typeof receipt[]>(); const old = transportFor('same-record'); Object.assign(old, { pasteImages: () => pending.promise });
    const next = transportFor('same-record'); const view = render(workspace(old, 'same-record'));
    await screen.findByRole('textbox', { name: '消息' }); type('旧输入'); paste();
    view.rerender(workspace(next, 'same-record')); type('新输入');
    await act(async () => pending.resolve([receipt])); expect(screen.queryByText(receipt.name)).not.toBeInTheDocument();
    enter(); await waitFor(() => expect(prompts(next)).toHaveLength(1));
    expect(prompts(next)[0]?.body).toMatchObject({ message: '新输入', attachments: [] });
  });

  it('keeps the replacement import locked when a cancelled edit import returns late', async () => {
    const old = deferred<typeof receipt[]>(); const current = deferred<typeof receipt[]>(); let calls = 0;
    const transport = transportFor('cancel-import'); Object.assign(transport, { pasteImages: () => ++calls === 1 ? old.promise : current.promise });
    render(workspace(transport, 'cancel-import')); await edit(); await screen.findByText('发送后将从这里重新生成后续对话'); paste();
    fireEvent.click(screen.getByRole('button', { name: '取消修改' })); type('新输入与新附件'); paste();
    await act(async () => old.resolve([receipt])); enter();
    expect(prompts(transport)).toHaveLength(0); expect(input()).toHaveValue('新输入与新附件');
    expect(screen.queryByText(receipt.name)).not.toBeInTheDocument();
    const next = { ...receipt, id: 'media_replacement', name: 'replacement.txt' };
    await act(async () => current.resolve([next])); enter();
    await waitFor(() => expect(prompts(transport)).toHaveLength(1));
    expect(prompts(transport)[0]?.body).toMatchObject({ message: '新输入与新附件', attachments: [next.id] });
  });
});

function modelCatalog(sessionId: string, owner: string, next = false): ModelCatalog {
  return { schemaVersion: 'rag-ime.agent-model-catalog.v1', ok: true, sessionId, thinkingLevel: 'max',
    selected: { provider: 'test', id: next ? 'next' : 'first', modelId: next ? 'next' : 'first', name: `${owner} ${next ? 'next' : 'first'}` },
    providers: [{ id: 'test', displayName: 'Test', models: ['first', 'next'].map(id => ({
      provider: 'test', id, name: `${owner} ${id}`, api: 'responses', reasoning: true,
      thinkingLevels: ['max'] as ['max'], supportsImages: false, contextWindow: 1000, maxTokens: 100,
    })) }],
  };
}
async function chooseModel(owner: string) {
  fireEvent.click(await screen.findByRole('button', { name: new RegExp(`模型与推理：${owner} first`) }, { timeout: 10_000 }));
  fireEvent.click(screen.getByRole('button', { name: /更换模型/ }));
  fireEvent.click(screen.getByRole('option', { name: `选择模型 ${owner} next` }));
}
async function chooseSettings(action: 'permission' | 'roots') {
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: /^对话权限：/ }, { timeout: 10_000 }));
  if (action === 'permission') await user.click(screen.getByRole('radio', { name: /^工作区托管/ }));
  else { await user.click(screen.getByRole('button', { name: '更改目录' })); await user.keyboard('{Escape}'); }
}

describe('Session setting mutation ownership', () => {
  it.each(['session', 'transport'] as const)('releases a previous model admission gate when the %s owner changes', async scope => {
    const pending = deferred(); const oldId = `model-gate-${scope}`; const nextId = scope === 'session' ? `${oldId}-next` : oldId;
    const old = transportFor(oldId, {
      'agent.session.models': (request: ControlRequest) => modelCatalog(request.params?.sessionId ?? oldId, request.params?.sessionId === oldId ? 'Old' : 'Current'),
      'agent.session.model.select': () => pending.promise, 'agent.session.thinking.select': { ok: true },
    });
    const next = scope === 'transport' ? transportFor(nextId, { 'agent.session.models': modelCatalog(nextId, 'Current') }) : old;
    const view = render(workspace(old, oldId)); await chooseModel('Old');
    view.rerender(workspace(next, nextId)); await screen.findByRole('button', { name: /模型与推理：Current first/ }, { timeout: 10_000 }); type('新工作区草稿');
    expect(screen.getByRole('button', { name: /^发送/ })).toBeEnabled();
    await act(async () => pending.resolve({ ok: true }));
    expect(old.requests.find(request => request.pathId === 'agent.session.thinking.select')?.params).toEqual({ sessionId: oldId });
    expect(input()).toHaveValue('新工作区草稿');
  });

  it.each(['success', 'failure'] as const)('does not let old model %s overwrite the next owner catalog or release its new model gate', async outcome => {
    const oldPending = deferred(); const currentPending = deferred(); let oldSelected = false; let currentSelected = false;
    const old = transportFor('model-chain', {
      'agent.session.models': () => modelCatalog('model-chain', 'Old', oldSelected),
      'agent.session.model.select': () => { oldSelected = true; return oldPending.promise; }, 'agent.session.thinking.select': { ok: true },
    });
    const current = transportFor('model-chain', {
      'agent.session.models': () => modelCatalog('model-chain', 'Current', currentSelected),
      'agent.session.model.select': () => { currentSelected = true; return currentPending.promise; }, 'agent.session.thinking.select': { ok: true },
    });
    const view = render(workspace(old, 'model-chain')); await chooseModel('Old');
    view.rerender(workspace(current, 'model-chain')); await chooseModel('Current'); type('当前连接草稿');
    await act(async () => outcome === 'success' ? oldPending.resolve({ ok: true }) : oldPending.reject(new Error('旧连接模型修改失败')));
    expect(screen.getByRole('button', { name: /^模型与推理：/ })).toHaveAccessibleName(/Current first/);
    expect(screen.getByRole('button', { name: /^模型与推理：/ })).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByRole('button', { name: /发送.*正在切换模型/ })).toBeDisabled();
    expect(screen.queryByText('旧连接模型修改失败')).not.toBeInTheDocument();
    if (outcome === 'success') expect(old.requests.find(request => request.pathId === 'agent.session.thinking.select')?.params).toEqual({ sessionId: 'model-chain' });
    await act(async () => currentPending.resolve({ ok: true }));
    await screen.findByRole('button', { name: /模型与推理：Current next/ }, { timeout: 10_000 });
    expect(screen.getByRole('button', { name: '发送' })).toBeEnabled(); expect(input()).toHaveValue('当前连接草稿');
  });

  describe.each(['session', 'transport'] as const)('%s scope changes', scope => {
    it.each([
      ['permission', 'success'], ['permission', 'failure'], ['roots', 'success'], ['roots', 'failure'],
    ] as const)('keeps a late %s %s on its original target without publishing into the new owner', async (action, outcome) => {
      const pending = deferred(); const oldId = `settings-${scope}-${action}-${outcome}`;
      const nextId = scope === 'session' ? `${oldId}-next` : oldId; const onUpdated = vi.fn();
      const old = transportFor(oldId, {
        'agent.session.mode.update': (request: ControlRequest) => {
          if (request.params?.sessionId !== oldId) throw new Error('当前连接的权限未修改');
          return pending.promise;
        },
      });
      Object.assign(old, { pickFiles: async () => [{ id: 'directory', name: 'selected', path: '/workspace/selected', mimeType: 'application/x-directory', byteSize: 0 }] });
      const next = scope === 'transport' ? transportFor(nextId, { 'agent.session.mode.update': () => { throw new Error('当前连接的权限未修改'); } }) : old;
      const view = render(workspace(old, oldId, onUpdated)); await chooseSettings(action);
      await waitFor(() => expect(old.requests.filter(request => request.pathId === 'agent.session.mode.update')).toHaveLength(1));
      view.rerender(workspace(next, nextId, onUpdated)); type('当前工作区输入');
      await chooseSettings('permission'); await screen.findByText('当前连接的权限未修改');
      await act(async () => {
        if (outcome === 'failure') pending.reject(new Error('旧连接设置修改失败'));
        else pending.resolve({ ok: true, session: sessionRecord(oldId) });
      });
      expect(screen.getByText('当前连接的权限未修改')).toBeVisible();
      expect(screen.queryByText('旧连接设置修改失败')).not.toBeInTheDocument();
      expect(onUpdated).not.toHaveBeenCalled(); expect(input()).toHaveValue('当前工作区输入');
      if (outcome === 'success') expect(old.requests.filter(request => request.pathId === 'agent.session.mode.update' && request.params?.sessionId === oldId)).toHaveLength(1);
    });

    it.each(['success', 'failure'] as const)('discards an unsubmitted directory picker %s after its owner changes', async outcome => {
      const picker = deferred(); const oldId = `picker-${scope}-${outcome}`; const nextId = scope === 'session' ? `${oldId}-next` : oldId;
      const onUpdated = vi.fn();
      const old = transportFor(oldId, { 'agent.session.mode.update': { ok: true, session: sessionRecord(oldId) } });
      Object.assign(old, { pickFiles: () => picker.promise });
      const next = scope === 'transport' ? transportFor(nextId) : old;
      const view = render(workspace(old, oldId, onUpdated)); await chooseSettings('roots');
      view.rerender(workspace(next, nextId, onUpdated)); type('保留新工作区输入');
      // Initial snapshot recovery legitimately clears errors. Let the new
      // owner finish that before testing an obsolete picker completion.
      await screen.findAllByText('已处理请求 2', {}, { timeout: 10_000 });
      await act(async () => {
        if (outcome === 'failure') picker.reject(new Error('旧目录选择失败'));
        else picker.resolve([{ id: 'directory', name: 'late', path: '/workspace/late', mimeType: 'application/x-directory', byteSize: 0 }]);
      });
      expect(old.requests.some(request => request.pathId === 'agent.session.mode.update')).toBe(false);
      expect(next.requests.some(request => request.pathId === 'agent.session.mode.update')).toBe(false);
      expect(onUpdated).not.toHaveBeenCalled(); expect(input()).toHaveValue('保留新工作区输入');
      expect(screen.queryByText('旧目录选择失败')).not.toBeInTheDocument();
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    });
  });
});
