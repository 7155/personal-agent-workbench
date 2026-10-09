import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { ChatPresentationProvider } from '@/features/conversation-ui/reading/chat-presentation';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TooltipProvider } from '@/components/primitives';
import { AgentComposer } from '@/features/agent/composer/AgentComposer';
import { previewSessions } from '@/features/agent/preview-data';
import { RoomComposer } from '@/features/rooms/composer/RoomComposer';
import type { ComposerFileImporter } from './pasted-text';

afterEach(cleanup);
const text = '完整需求：保留换行与中文。\r\n'.repeat(600);
const participant = { id: 'earth', sessionId: 'session-earth', roleId: 'worker', roleVersion: '1', displayName: 'Earth', ordinal: 0, status: 'active' };

function Harness({ surface, importer, inputOwnerId, busy = false, onSend = vi.fn(), onPick = vi.fn() }: {
  surface: 'Session' | 'Room'; importer: ComposerFileImporter; inputOwnerId?: string; busy?: boolean; onSend?: (...args: string[]) => void; onPick?: () => void;
}) {
  const [draft, setDraft] = useState('请核对附件');
  const common = { draft, onDraftChange: setDraft, sending: false, onPasteImages: importer, onPickAttachments: onPick, onSend, onAttachmentsChange: vi.fn() };
  return <TooltipProvider>{surface === 'Room'
    ? <RoomComposer {...common} room={{ id: 'room-a', status: 'active', participants: [participant] }} attachments={[]} personas={[]} taskBusyState={busy ? 'running' : undefined} onPasteFromClipboard={vi.fn()} />
    : <AgentComposer {...common} inputOwnerId={inputOwnerId} attachments={[]} session={previewSessions[0]} minimal busy={busy} commands={[]} tools={[]} toolCatalogStatus="ready" onToolSelect={vi.fn()} onProductCommand={vi.fn()} onStop={vi.fn()} onPermissionChange={vi.fn()} onWorkspaceRootsChange={vi.fn()} onModelChange={vi.fn()} />}</TooltipProvider>;
}

describe.each(['Session', 'Room'] as const)('%s shared composer', (surface) => {
  it('converts a long paste into one intact UTF-8 file without replacing the draft', async () => {
    const importer = vi.fn().mockResolvedValue(true);
    render(<Harness surface={surface} importer={importer} />);
    const editor = screen.getByRole('textbox');
    expect(fireEvent.paste(editor, { clipboardData: { files: [], items: [], getData: () => text } })).toBe(false);
    await waitFor(() => expect(screen.queryByLabelText('长文本附件')).not.toBeInTheDocument());
    expect(importer).toHaveBeenCalledTimes(1);
    const file = importer.mock.calls[0]![0][0] as File;
    expect(file.name).toMatch(/\.txt$/u);
    expect(file.type).toBe('text/plain');
    const reader = new FileReader();
    const content = new Promise((resolve) => { reader.onload = () => resolve(reader.result); });
    reader.readAsText(file);
    expect(await content).toBe(text);
    expect(editor).toHaveValue('请核对附件');
  });

  it('retains failed text, prevents a partial send and retries the original file', async () => {
    const importer = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    const onSend = vi.fn();
    render(<Harness surface={surface} importer={importer} onSend={onSend} />);
    const editor = screen.getByRole('textbox');
    fireEvent.paste(editor, { clipboardData: { files: [], items: [], getData: () => text } });
    await screen.findByText('文本附件未导入，内容已保留。');
    fireEvent.keyDown(editor, { key: 'Enter' });
    expect(onSend).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('查看保留的原文'));
    expect(screen.getByRole('textbox', { name: '未导入的长文本' })).toHaveValue(text.replace(/\r\n/gu, '\n'));
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    await waitFor(() => expect(screen.queryByLabelText('长文本附件')).not.toBeInTheDocument());
    expect(importer.mock.calls[0]![0][0]).toBe(importer.mock.calls[1]![0][0]);
    expect(editor).toHaveValue('请核对附件');
  });

  it('provides one add trigger, keyboard dismissal and an independent editor expansion', async () => {
    const user = userEvent.setup(); const onPick = vi.fn();
    const { container } = render(<Harness surface={surface} importer={vi.fn()} onPick={onPick} />);
    const controls = container.querySelector('.agent-composer__controls') as HTMLElement;
    expect(within(controls).getAllByRole('button')).toHaveLength(1);
    const add = screen.getByRole('button', { name: '添加内容' });
    await user.click(add);
    await user.click(screen.getByRole('menuitem', { name: /选择附件/ }));
    expect(onPick).toHaveBeenCalledTimes(1);
    await user.click(add); await user.keyboard('{Escape}');
    expect(add).toHaveFocus();
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '展开长文本编辑' }));
    expect(screen.getByRole('textbox')).toHaveFocus();
    expect(screen.getByRole('button', { name: '收起长文本编辑' })).toHaveAttribute('aria-expanded', 'true');
    await user.keyboard('{Escape}');
    expect(screen.getByRole('button', { name: '展开长文本编辑' })).toHaveAttribute('aria-expanded', 'false');
  });
});

function deferredImport() {
  let resolve!: (value: boolean) => void; let reject!: (reason: Error) => void;
  const promise = new Promise<boolean>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function pasteLongText(title: string) {
  fireEvent.paste(screen.getByRole('textbox', { name: '消息' }), {
    clipboardData: { files: [], items: [], getData: () => `${title}\n${'原文保持不变。'.repeat(600)}` },
  });
}

describe('Session long-text input owner', () => {
  it.each(['success', 'failure', 'refused'] as const)('lets a new connection import independently and ignores old %s/finally', async outcome => {
    const old = deferredImport(); const current = deferredImport();
    const oldImporter = vi.fn(() => old.promise); const currentImporter = vi.fn(() => current.promise); const onSend = vi.fn();
    const view = render(<Harness surface="Session" inputOwnerId="transport-a:same-session" importer={oldImporter} onSend={onSend} />);
    pasteLongText('旧连接文本'); expect(oldImporter).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText('长文本附件')).toHaveTextContent('旧连接文本');
    // session.id and the mounted Composer remain unchanged; only the actual
    // transport+Session owner changes, as in Workspace reconnection.
    view.rerender(<Harness surface="Session" inputOwnerId="transport-b:same-session" importer={currentImporter} onSend={onSend} />);
    expect(screen.queryByLabelText('长文本附件')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '发送' })).toBeEnabled();
    pasteLongText('新连接文本'); expect(currentImporter).toHaveBeenCalledTimes(1);
    await act(async () => {
      if (outcome === 'failure') old.reject(new Error('old import failed'));
      else old.resolve(outcome === 'success');
    });
    await waitFor(() => expect(screen.getByLabelText('长文本附件')).toHaveTextContent('新连接文本'));
    expect(screen.getByLabelText('长文本附件')).not.toHaveTextContent('旧连接文本');
    fireEvent.keyDown(screen.getByRole('textbox', { name: '消息' }), { key: 'Enter' });
    expect(onSend).not.toHaveBeenCalled();
    // An obsolete finally must not release the new owner's import lock.
    pasteLongText('重复尝试'); await screen.findByText('文本附件未导入，内容已保留。');
    expect(currentImporter).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: /移除未导入的 重复尝试/ }));
    await act(async () => current.resolve(true));
    await waitFor(() => expect(screen.queryByLabelText('长文本附件')).not.toBeInTheDocument());
    expect(screen.getByRole('textbox', { name: '消息' })).toHaveValue('请核对附件');
    fireEvent.keyDown(screen.getByRole('textbox', { name: '消息' }), { key: 'Enter' });
    expect(onSend).toHaveBeenCalledExactlyOnceWith('prompt', '请核对附件');
  });

  it('does not reuse an earlier import lifetime after navigating away and back to the same owner', async () => {
    const old = deferredImport(); const current = deferredImport();
    const oldImporter = vi.fn(() => old.promise); const currentImporter = vi.fn(() => current.promise);
    const view = render(<Harness surface="Session" inputOwnerId="owner-a" importer={oldImporter} />);
    pasteLongText('早先的文本');
    view.rerender(<Harness surface="Session" inputOwnerId="owner-b" importer={currentImporter} />);
    view.rerender(<Harness surface="Session" inputOwnerId="owner-a" importer={currentImporter} />);
    pasteLongText('返回后文本'); expect(currentImporter).toHaveBeenCalledTimes(1);
    await act(async () => old.resolve(true));
    await waitFor(() => expect(screen.getByLabelText('长文本附件')).toHaveTextContent('返回后文本'));
    pasteLongText('返回后重复尝试'); await screen.findByText('文本附件未导入，内容已保留。');
    expect(currentImporter).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: /移除未导入的 返回后重复尝试/ }));
    await act(async () => current.resolve(true)); await waitFor(() => expect(screen.queryByLabelText('长文本附件')).not.toBeInTheDocument());
  });
});


class EditorResizeObserver {
  static instances: EditorResizeObserver[] = [];
  observed = new Set<Element>();
  disconnected = false;
  constructor(private callback: ResizeObserverCallback) { EditorResizeObserver.instances.push(this); }
  observe(element: Element) { this.disconnected = false; this.observed.add(element); }
  unobserve(element: Element) { this.observed.delete(element); }
  disconnect() { this.disconnected = true; this.observed.clear(); }
  static resize(element: Element) {
    for (const observer of this.instances) if (observer.observed.has(element)) observer.callback([], observer as unknown as ResizeObserver);
  }
}

function BoundedComposer({ surface, version = 'v2', full = true }: { surface: 'Session' | 'Room'; version?: 'v1' | 'v2'; full?: boolean }) {
  const session = surface === 'Session';
  return <ChatPresentationProvider ownerKey={`height-budget:${surface}:${version}:${full}`} defaultVersion={version}>
    <section className={session ? 'paw-session-workspace' : 'paw-room-workspace paw-room-workspace--conversation'}
      data-design={session && full ? 'workbench' : undefined} data-window-chrome={!session && full ? 'portal' : undefined}>
      <div data-testid="editor-host" className={session ? 'paw-session-workspace__primary' : 'paw-room-workspace__main'}>
        <div data-testid="editor-dock" className={session ? 'paw-session-workspace__composer' : 'paw-room-workspace__composer'}>
          <Harness surface={surface} importer={vi.fn()} />
        </div>
      </div>
    </section>
  </ChatPresentationProvider>;
}

function editorGeometryFixture({ shortDock = false } = {}) {
  let available = shortDock ? 180 : 304; let overhead = 116;
  const browserStyle = window.getComputedStyle;
  const minimum = (input: HTMLElement) => Number.parseFloat(input.style.minHeight)
    || (input.closest('[data-expanded]') ? 210 : 64);
  const height = (input: HTMLElement) => Math.max(minimum(input), Number.parseFloat(input.style.height) || minimum(input));
  const inputOf = (node: HTMLElement) => node.querySelector('textarea') as HTMLElement;
  // A constrained two-row versus one-row layout model, not saved short-window
  // BCR: that raw sample was not recorded by the failed browser observer.
  const chrome = (node: HTMLElement) => shortDock ? node.dataset.composerHeight === 'compact' ? 84 : 170 : overhead;
  const isHost = (node: HTMLElement) => node.dataset.testid === 'editor-host';
  const isDock = (node: HTMLElement) => node.dataset.testid === 'editor-dock';
  vi.spyOn(window, 'getComputedStyle').mockImplementation((node, pseudo) => {
    const style = browserStyle(node, pseudo);
    if (node.tagName !== 'TEXTAREA') return style;
    return new Proxy(style, { get(target, key, receiver) {
      if (key === 'minHeight') return node.closest('[data-expanded]') ? '210px' : '64px';
      return Reflect.get(target, key, receiver);
    } });
  });
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(function(this: HTMLElement) {
    return isHost(this) ? available : this.tagName === 'TEXTAREA' ? height(this) : 0;
  });
  vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(function(this: HTMLElement) {
    return isHost(this) ? available : isDock(this) ? height(inputOf(this)) + chrome(this) : this.tagName === 'TEXTAREA' ? height(this) : 64;
  });
  vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockImplementation(function(this: HTMLElement) {
    return isDock(this) ? height(inputOf(this)) + chrome(this) : this.tagName === 'TEXTAREA' ? shortDock && !(this as HTMLTextAreaElement).value ? 0 : 800 : 0;
  });
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function(this: HTMLElement) {
    const h = isHost(this) ? available : isDock(this) ? height(inputOf(this)) + chrome(this) : this.tagName === 'TEXTAREA' ? height(this) : 64;
    const dock = this.closest('[data-testid="editor-dock"]') as HTMLElement | null;
    const y = isHost(this) ? 161 : dock ? 161 + available - height(inputOf(dock)) - chrome(dock) + (this.tagName === 'TEXTAREA' ? 18 : 0) : 0;
    return { x: 15, y, width: 690, height: h, top: y, bottom: y + h, left: 15, right: 705, toJSON: () => ({}) };
  });
  vi.stubGlobal('ResizeObserver', EditorResizeObserver); EditorResizeObserver.instances = [];
  return { resizeHost: (next: number) => { available = next; }, resizeChrome: (next: number) => { overhead = next; } };
}

// jsdom has no browser layout. The fixture models the actual 200% primary/dock
// boxes and CSS minimum precedence; production observers are driven, not mocked.
describe.each(['Session', 'Room'] as const)('%s bounded workbench editor', surface => {
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it('retains a readable empty input and growing long draft in a very short host', () => {
    editorGeometryFixture({ shortDock: true }); render(<BoundedComposer surface={surface} />);
    const input = screen.getByRole('textbox') as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: '' } });
    expect(input.getBoundingClientRect().height).toBe(64);
    // Existing narrow padding is at most24px; at least one26.4px text line.
    expect(input.getBoundingClientRect().height - 24).toBeGreaterThanOrEqual(26.4);
    fireEvent.change(input, { target: { value: '短窗长草稿。\n'.repeat(350) } });
    expect(input.getBoundingClientRect().height).toBeGreaterThan(64);
    expect(input.getBoundingClientRect().height).toBeLessThanOrEqual(156);
    const host = screen.getByTestId('editor-host').getBoundingClientRect();
    expect(screen.getByTestId('editor-dock').getBoundingClientRect().height).toBeLessThan(host.height);
    expect(input.getBoundingClientRect().top).toBeGreaterThanOrEqual(host.top);
  });

  it('resizes the short layout without losing the original input, draft, selection or focus', () => {
    const layout = editorGeometryFixture({ shortDock: true }); const view = render(<BoundedComposer surface={surface} />);
    const input = screen.getByRole('textbox') as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: '原输入节点和中文草稿。\n'.repeat(350) } });
    fireEvent.click(screen.getByRole('button', { name: '展开长文本编辑' }));
    input.focus(); input.setSelectionRange(7, 25, 'backward');
    act(() => { layout.resizeHost(195); EditorResizeObserver.resize(screen.getByTestId('editor-host')); });
    expect(input.getBoundingClientRect().height).toBeGreaterThanOrEqual(64);
    expect(screen.getByRole('textbox')).toBe(input); expect(input).toHaveFocus();
    expect(input.selectionStart).toBe(7); expect(input.selectionEnd).toBe(25); expect(input.selectionDirection).toBe('backward');
    expect(input).toHaveValue('原输入节点和中文草稿。\n'.repeat(350));
    const dock = screen.getByTestId('editor-dock'); expect(dock).toHaveAttribute('data-composer-height', 'compact');
    act(() => { layout.resizeHost(304); EditorResizeObserver.resize(screen.getByTestId('editor-host')); });
    expect(dock).not.toHaveAttribute('data-composer-height');
    view.unmount(); expect(dock).not.toHaveAttribute('data-composer-height');
  });

  it('keeps the expanded editor inside the original 304px host despite its 210px CSS minimum', () => {
    editorGeometryFixture(); render(<BoundedComposer surface={surface} />);
    const input = screen.getByRole('textbox') as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: '公开长草稿。\n'.repeat(350) } });
    fireEvent.click(screen.getByRole('button', { name: '展开长文本编辑' }));
    const host = screen.getByTestId('editor-host').getBoundingClientRect();
    const dock = screen.getByTestId('editor-dock').getBoundingClientRect();
    expect(input.getBoundingClientRect().top).toBeGreaterThanOrEqual(host.top);
    expect(dock.height).toBeLessThan(host.height);
    expect(input.getBoundingClientRect().height).toBeLessThan(210);
    expect(input).toHaveValue('公开长草稿。\n'.repeat(350));
  });

  it('recomputes short-window and toolbar budgets without replacing the input, draft, selection or focus', () => {
    const layout = editorGeometryFixture(); const view = render(<BoundedComposer surface={surface} />);
    const input = screen.getByRole('textbox') as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: '同一输入节点的草稿。\n'.repeat(350) } });
    fireEvent.click(screen.getByRole('button', { name: '展开长文本编辑' }));
    input.focus(); input.setSelectionRange(7, 25, 'backward');
    act(() => { layout.resizeHost(220); EditorResizeObserver.resize(screen.getByTestId('editor-host')); });
    expect(screen.getByRole('textbox')).toBe(input); expect(input).toHaveFocus();
    expect(input.selectionStart).toBe(7); expect(input.selectionEnd).toBe(25); expect(input.selectionDirection).toBe('backward');
    expect(input.getBoundingClientRect().height).toBeLessThanOrEqual(220 - 116 - 8);
    const toolbar = view.container.querySelector('.agent-composer__toolbar')!;
    act(() => { layout.resizeChrome(144); EditorResizeObserver.resize(toolbar); });
    expect(input.getBoundingClientRect().height).toBeLessThanOrEqual(220 - 144 - 8);
    expect(input).toHaveValue('同一输入节点的草稿。\n'.repeat(350));
    const observers = EditorResizeObserver.instances.filter(observer => observer.observed.has(screen.getByTestId('editor-host')));
    expect(observers.length).toBeGreaterThan(0); expect(observers.every(observer => !observer.observed.has(input))).toBe(true);
    view.unmount(); expect(observers.every(observer => observer.disconnected)).toBe(true);
  });

  it('also bounds a collapsed long draft when the actual window is short', () => {
    const layout = editorGeometryFixture(); layout.resizeHost(220); render(<BoundedComposer surface={surface} />);
    const input = screen.getByRole('textbox') as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: '短窗中的未发送长草稿。\n'.repeat(350) } });
    expect(input.getBoundingClientRect().height).toBeLessThanOrEqual(220 - 116 - 8);
    expect(input.getBoundingClientRect().top).toBeGreaterThanOrEqual(161);
    expect(input).toHaveValue('短窗中的未发送长草稿。\n'.repeat(350));
    expect(screen.getByRole('button', { name: '展开长文本编辑' })).toHaveAttribute('aria-expanded', 'false');
  });

  it('does not cancel expansion motion on an unchanged observer delivery', () => {
    const layout = editorGeometryFixture(); layout.resizeHost(900); render(<BoundedComposer surface={surface} />);
    const input = screen.getByRole('textbox') as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: '长草稿。\n'.repeat(350) } });
    fireEvent.click(screen.getByRole('button', { name: '展开长文本编辑' }));
    expect(input.style.transition).toBe('height 220ms cubic-bezier(.2,.8,.2,1)');
    act(() => EditorResizeObserver.resize(screen.getByTestId('editor-host')));
    expect(input.style.transition).toBe('height 220ms cubic-bezier(.2,.8,.2,1)');
    expect(input.style.height).toBe('360px');
  });

  it('keeps the original 156/360px long-draft ranges when the host has room', () => {
    const layout = editorGeometryFixture(); layout.resizeHost(900); render(<BoundedComposer surface={surface} />);
    const input = screen.getByRole('textbox') as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: '长草稿。\n'.repeat(350) } });
    expect(input.style.height).toBe('156px');
    fireEvent.click(screen.getByRole('button', { name: '展开长文本编辑' })); expect(input.style.height).toBe('360px');
    fireEvent.keyDown(input, { key: 'Escape' }); expect(input.style.height).toBe('156px');
  });

  it.each([{ version: 'v1' as const, full: true }, { version: 'v2' as const, full: false }])('retains the unbounded $version/full=$full consumer behavior', ({ version, full }) => {
    editorGeometryFixture(); render(<BoundedComposer surface={surface} version={version} full={full} />);
    const input = screen.getByRole('textbox') as HTMLTextAreaElement;
    fireEvent.click(screen.getByRole('button', { name: '展开长文本编辑' }));
    expect(input.style.height).toBe('360px'); expect(input.style.minHeight).toBe('');
  });
});
