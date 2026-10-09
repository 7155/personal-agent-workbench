import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport } from '@/test/mock-transport';
import { PawOsAppSurfaceProvider } from '@/features/paw-os/surface-context';
import { PawOsTerminalApp } from './PawOsTerminalApp';
import { PawWindowFrame } from '@/paw-os/shell/PawWindowLayer';
import terminalCss from './paw-os-terminal-app.css?raw';

const xtermConstructorOptions = vi.hoisted(() => [] as Record<string, unknown>[]);
const clipboardWrites = vi.hoisted(() => [] as string[]);
const searchAddonState = vi.hoisted(() => ({
  calls: [] as { kind: 'next' | 'previous' | 'clear'; term?: string; options?: Record<string, unknown> }[],
  listeners: [] as ((event: { resultIndex: number; resultCount: number }) => void)[],
}));

vi.mock('@/platform/clipboard', () => ({
  writeClipboardText: async (value: string) => { clipboardWrites.push(value); },
}));

vi.mock('@xterm/addon-fit', () => ({ FitAddon: class { fit() {} } }));
vi.mock('@xterm/addon-search', () => ({
  SearchAddon: class {
    private terminal?: { options: Record<string, unknown> };
    activate(terminal: { options: Record<string, unknown> }) { this.terminal = terminal; }
    dispose() {}
    clearDecorations() { searchAddonState.calls.push({ kind: 'clear' }); }
    private checkDecorations(options?: Record<string, unknown>) {
      // Installed SearchAddon creates xterm decorations before emitting match
      // results. Preserve that dependency contract in the UI test double.
      if (options?.decorations && !this.terminal?.options.allowProposedApi) {
        throw new Error('You must set the allowProposedApi option to true to use proposed API');
      }
    }
    findNext(term: string, options?: Record<string, unknown>) {
      this.checkDecorations(options);
      searchAddonState.calls.push({ kind: 'next', term, options });
      return true;
    }
    findPrevious(term: string, options?: Record<string, unknown>) {
      this.checkDecorations(options);
      searchAddonState.calls.push({ kind: 'previous', term, options });
      return true;
    }
    onDidChangeResults(listener: (event: { resultIndex: number; resultCount: number }) => void) {
      searchAddonState.listeners.push(listener);
      return { dispose() {} };
    }
  },
}));
vi.mock('@xterm/xterm', () => ({
  Terminal: class {
    cols = 104;
    rows = 30;
    private host?: HTMLElement;
    private renderedElements: HTMLElement[] = [];
    private onDataCallback: (data: string) => void = () => undefined;
    private customKeyHandler: (event: KeyboardEvent) => boolean = () => true;

    constructor(readonly options: Record<string, unknown>) {
      xtermConstructorOptions.push(options);
    }

    loadAddon(addon: { activate?: (terminal: { options: Record<string, unknown> }) => void }) { addon.activate?.(this); }
    attachCustomKeyEventHandler(handler: (event: KeyboardEvent) => boolean) { this.customKeyHandler = handler; }
    hasSelection() { return false; }
    getSelection() { return ''; }
    open(host: HTMLElement) {
      this.host = host;
      const input = document.createElement('textarea');
      this.renderedElements.push(input);
      input.setAttribute('aria-label', '终端输入');
      input.addEventListener('keydown', (event) => {
        // Real xterm consults the custom handler before treating a key as PTY input.
        if (!this.customKeyHandler(event)) return;
        if (event.key === 'Enter') this.onDataCallback('\r');
        else if (event.key.length === 1) this.onDataCallback(event.key);
      });
      host.append(input);
    }
    onData(callback: (data: string) => void) {
      this.onDataCallback = callback;
      return { dispose() {} };
    }
    focus() { this.host?.querySelector<HTMLTextAreaElement>('textarea')?.focus(); }
    reset() { this.host?.replaceChildren(); }
    write(text: string) {
      const output = document.createElement('pre');
      this.renderedElements.push(output);
      output.textContent = text;
      this.host?.append(output);
    }
    dispose() { for (const element of this.renderedElements) element.remove(); }
  },
}));

afterEach(() => {
  cleanup();
  xtermConstructorOptions.length = 0;
  clipboardWrites.length = 0;
  searchAddonState.calls.length = 0;
  searchAddonState.listeners.length = 0;
  delete document.documentElement.dataset.reduceMotion;
  delete (window as Window & { pawTerminalHost?: unknown }).pawTerminalHost;
});

describe('PawOsTerminalApp', () => {
  it('keeps the loading state visible until the session list settles', async () => {
    let resolveList!: (value: unknown) => void;
    const listPending = new Promise((resolve) => { resolveList = resolve; });
    const transport = new MockControlTransport({
      routes: {
        'terminal.sessions.list': () => listPending,
      },
    });

    renderTerminal(transport, <PawOsTerminalApp />);
    expect(await screen.findByText('正在读取终端会话…')).toBeInTheDocument();
    expect(screen.queryByText('还没有终端会话')).not.toBeInTheDocument();

    await act(async () => {
      resolveList({ schemaVersion: 'rag-ime.system-terminal.v1', ok: true, items: [] });
      await listPending;
    });
    expect(await screen.findByText('还没有终端会话')).toBeInTheDocument();
  });

  it('does not start list or output polling while its PAWOS window is inactive', async () => {
    const terminal = terminalSession('terminal-one', 'Terminal');
    const transport = new MockControlTransport({
      routes: {
        'terminal.sessions.list': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, items: [terminal] },
        'terminal.session.read': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminal, cursor: 0, nextCursor: 0, truncated: false, text: '' },
      },
    });

    renderTerminal(transport, (
      <PawOsAppSurfaceProvider active={false} appId="terminal" height={700} width={900}>
        <PawOsTerminalApp />
      </PawOsAppSurfaceProvider>
    ));
    await act(async () => { await Promise.resolve(); });

    expect(transport.requests.some((call) => call.request.pathId === 'terminal.sessions.list')).toBe(false);
    expect(transport.requests.some((call) => call.request.pathId === 'terminal.session.read')).toBe(false);
  });

  it('writes commands to a real system terminal route without wrapping them in an Agent prompt', async () => {
    const user = userEvent.setup();
    let output = '';
    const terminal = {
      terminalId: 'terminal-system',
      title: 'System Terminal',
      cwd: '/workspace/paw',
      shell: '/bin/zsh',
      pid: 4242,
      cols: 104,
      rows: 30,
      status: 'running',
      exitCode: null,
      baseCursor: 0,
      nextCursor: 0,
      createdAtMs: 1,
    };
    const transport = new MockControlTransport({
      routes: {
        'terminal.sessions.list': () => ({
          schemaVersion: 'rag-ime.system-terminal.v1',
          ok: true,
          items: [{ ...terminal, nextCursor: output.length }],
        }),
        'terminal.session.read': (request: ControlRequest) => {
          const body = asRecord(request.body);
          const cursor = Number(body.cursor ?? 0);
          return {
            schemaVersion: 'rag-ime.system-terminal.v1',
            ok: true,
            terminal: { ...terminal, nextCursor: output.length },
            cursor,
            nextCursor: output.length,
            truncated: false,
            text: output.slice(cursor),
          };
        },
        'terminal.session.write': (request: ControlRequest) => {
          output += String(asRecord(request.body).text ?? '');
          return { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminalId: terminal.terminalId, bytesWritten: output.length };
        },
        'terminal.session.resize': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminalId: terminal.terminalId },
        'terminal.session.create': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminal },
        'terminal.session.close': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminal: { ...terminal, status: 'closed' } },
      },
    });

    renderApp(transport, <PawOsTerminalApp />);

    expect(await screen.findByRole('heading', { name: 'Terminal', level: 1 })).toBeInTheDocument();
    expect(await screen.findByText('/workspace/paw')).toBeInTheDocument();
    expect(screen.getByText('pid 4242')).toBeInTheDocument();
    expect(screen.getByText('UTF-8')).toBeInTheDocument();
    await user.type(await screen.findByRole('textbox', { name: '终端输入' }), 'printf PAW_PTY_OK{Enter}');

    await waitFor(() => expect(transport.requests
      .filter((call) => call.request.pathId === 'terminal.session.write')
      .map((call) => String(asRecord(call.request.body).text ?? ''))
      .join(''))
      .toBe('printf PAW_PTY_OK\r'));
    expect(output).toBe('printf PAW_PTY_OK\r');
    expect(transport.requests.some((call) => call.request.pathId === 'agent.session.prompt')).toBe(false);
  });

  it('keeps live terminal tabs, creation, selection, and close in window chrome', async () => {
    const user = userEvent.setup();
    const terminal = terminalSession('terminal-one', 'Terminal One');
    let terminals = [terminal];
    const transport = new MockControlTransport({
      routes: {
        'terminal.sessions.list': () => ({ schemaVersion: 'rag-ime.system-terminal.v1', ok: true, items: terminals }),
        'terminal.session.read': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminal, cursor: 0, nextCursor: 0, truncated: false, text: '' },
        'terminal.session.resize': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminalId: terminal.terminalId },
        'terminal.session.create': () => {
          const created = terminalSession('terminal-two', 'Terminal Two');
          terminals = [...terminals, created];
          return { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminal: created };
        },
        'terminal.session.close': (request: ControlRequest) => {
          const terminalId = String(asRecord(request.body).terminalId ?? '');
          terminals = terminals.filter((item) => item.terminalId !== terminalId);
          return { schemaVersion: 'rag-ime.system-terminal.v1', ok: true };
        },
      },
    });

    renderApp(transport, (
      <PawWindowFrame
        active
        appId="terminal"
        bounds={{ x: 0, y: 0, width: 900, height: 640 }}
        onBoundsCommit={() => undefined}
        onClose={() => undefined}
        onFocus={() => undefined}
        onMinimize={() => undefined}
        onToggleMaximize={() => undefined}
        title="Terminal"
        windowChrome="terminal-tabs"
        windowId="terminal"
        zIndex={10}
      >
        <PawOsTerminalApp />
      </PawWindowFrame>
    ));

    const titlebar = screen.getByLabelText('Terminal窗口').querySelector('.paw-window-titlebar') as HTMLElement;
    const tablist = await within(titlebar).findByRole('tablist', { name: 'PAWOS 终端' });
    const firstTab = await within(tablist).findByRole('tab', { name: /Terminal One/ });
    const terminalPanel = screen.getByRole('tabpanel', { name: /Terminal One/ });
    expect(firstTab.querySelector(':scope > svg')).toBeNull();
    expect(firstTab).toHaveAttribute('aria-controls', terminalPanel.id);
    expect(firstTab).toHaveAttribute('tabindex', '0');
    expect(terminalPanel).toHaveAttribute('aria-labelledby', firstTab.id);
    expect(document.querySelector('.paw-window-body .paw-terminal-app__toolbar')).toBeNull();
    expect(within(titlebar).getByRole('button', { name: '关闭窗口' })).toBeInTheDocument();
    expect(within(tablist).getByRole('button', { name: '结束终端会话 Terminal One' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '关闭当前终端' })).not.toBeInTheDocument();
    // The new-terminal action must not scroll away with an overflowing tab strip.
    const newTerminalButton = within(titlebar).getByRole('button', { name: '新建终端' });
    expect(tablist.contains(newTerminalButton)).toBe(false);
    await user.click(newTerminalButton);
    await waitFor(() => expect(within(tablist).getAllByRole('tab')).toHaveLength(2));
    const secondTab = within(tablist).getByRole('tab', { name: /Terminal Two/ });
    expect(secondTab).toHaveAttribute('aria-selected', 'true');
    expect(secondTab).toHaveAttribute('tabindex', '0');
    expect(firstTab).toHaveAttribute('tabindex', '-1');

    await act(async () => { await new Promise<void>(resolve => window.requestAnimationFrame(() => resolve())); });
    expect(screen.getByRole('textbox', { name: '终端输入' })).toHaveFocus();

    secondTab.focus();
    await user.keyboard('{ArrowLeft}');
    expect(firstTab).toHaveAttribute('aria-selected', 'true');
    expect(firstTab).toHaveFocus();
    await act(async () => { await new Promise<void>(resolve => window.requestAnimationFrame(() => resolve())); });
    expect(firstTab).toHaveFocus();
    await user.keyboard('{End}');
    expect(secondTab).toHaveAttribute('aria-selected', 'true');
    expect(secondTab).toHaveFocus();
    await user.keyboard('{Home}');
    expect(firstTab).toHaveAttribute('aria-selected', 'true');
    expect(firstTab).toHaveFocus();
    await user.keyboard('{ArrowLeft}');
    expect(secondTab).toHaveAttribute('aria-selected', 'true');
    expect(secondTab).toHaveFocus();

    await user.click(within(tablist).getByRole('button', { name: '结束终端会话 Terminal Two' }));
    await waitFor(() => expect(within(tablist).getAllByRole('tab')).toHaveLength(1));
  });

  it('promotes unchanged owning window shell focus to the initial automatically created PTY', async () => {
    const terminal = terminalSession('terminal-auto', 'Terminal');
    let sessions: ReturnType<typeof terminalSession>[] = [];
    const transport = new MockControlTransport({ routes: {
      'terminal.sessions.list': () => ({ ok: true, items: sessions }),
      'terminal.session.create': () => { sessions = [terminal]; return { ok: true, terminal }; },
      'terminal.session.read': { ok: true, terminal, cursor: 0, nextCursor: 0, text: '' },
      'terminal.session.resize': { ok: true },
    } });
    renderTerminal(transport, terminalFrame(<PawOsTerminalApp />));
    // The actual Frame layout effect sets the neutral keyboard context before
    // the App's passive mount work; this is not a user control focus intent.
    expect(screen.getByLabelText('Terminal窗口')).toHaveFocus();
    const input = await screen.findByRole('textbox', { name: '终端输入' });
    await act(async () => { await new Promise<void>(resolve => window.requestAnimationFrame(() => window.requestAnimationFrame(() => resolve()))); });
    expect(input).toHaveFocus();
    expect(transport.requests.filter(call => call.request.pathId === 'terminal.session.create')).toHaveLength(1);
    expect(transport.requests.some(call => call.request.pathId === 'terminal.session.write')).toBe(false);
  });

  it('preserves another window shell already focused when the terminal mounts', async () => {
    const terminal = terminalSession('terminal-one', 'Terminal');
    let resolveList!: (value: unknown) => void;
    const listPending = new Promise(resolve => { resolveList = resolve; });
    const transport = new MockControlTransport({ routes: {
      'terminal.sessions.list': () => listPending,
      'terminal.session.read': { ok: true, terminal, cursor: 0, nextCursor: 0, text: '' },
      'terminal.session.resize': { ok: true },
    } });
    renderTerminal(transport, <>{terminalFrame(<PawOsTerminalApp />)}{terminalFrame(<span>Other content</span>, 'other', false)}</>);
    const otherShell = screen.getByLabelText('other窗口');
    otherShell.focus();
    await act(async () => { resolveList({ ok: true, items: [terminal] }); await listPending; });
    await screen.findByRole('textbox', { name: '终端输入' });
    await act(async () => { await new Promise<void>(resolve => window.requestAnimationFrame(() => window.requestAnimationFrame(() => resolve()))); });
    expect(otherShell).toHaveFocus();
    expect(transport.requests.some(call => ['terminal.session.create', 'terminal.session.write'].includes(call.request.pathId))).toBe(false);
  });

  it.each(['empty', 'survivor'] as const)('waits for accepted close removal before restoring stable %s focus across query cache updates', async (destination) => {
    const user = userEvent.setup();
    const survivor = terminalSession('terminal-survivor', 'Survivor');
    const closing = terminalSession('terminal-closing', 'Closing');
    let sessions = destination === 'empty' ? [closing] : [survivor, closing];
    let resolveClose!: (value: unknown) => void;
    const closePending = new Promise((resolve) => { resolveClose = resolve; });
    const transport = new MockControlTransport({ routes: {
      'terminal.sessions.list': () => ({ schemaVersion: 'rag-ime.system-terminal.v1', ok: true, items: sessions }),
      'terminal.session.read': (request: ControlRequest) => ({
        schemaVersion: 'rag-ime.system-terminal.v1', ok: true,
        terminal: sessions.find(item => item.terminalId === asRecord(request.body).terminalId) ?? closing,
        cursor: 0, nextCursor: 0, truncated: false, text: '',
      }),
      'terminal.session.resize': { ok: true },
      'terminal.session.close': () => closePending,
    } });
    const { queryClient } = renderTerminal(transport, <PawOsTerminalApp />);
    await screen.findByRole('tab', { name: 'Closing' });
    await screen.findByRole('textbox', { name: '终端输入' });
    await user.click(screen.getByRole('button', { name: '结束终端会话 Closing' }));
    expect(screen.getByRole('button', { name: '结束终端会话 Closing' })).toBeDisabled();

    // A normal list/read projection changes array identity while the original
    // close is pending. It must not consume return focus on the closing tab.
    await act(async () => {
      queryClient.setQueryData(['system-terminal'], {
        schemaVersion: 'rag-ime.system-terminal.v1', ok: true,
        items: sessions.map(item => item.terminalId === closing.terminalId
          ? { ...item, status: 'exited', exitCode: 0, nextCursor: 1 }
          : item),
      });
    });
    // Await the actual cache projection commit before settling the close:
    // the shell may exit while its owning close request is still pending.
    await screen.findByRole('tab', { name: 'Closing 已退出（退出码 0）' });
    await act(async () => { await new Promise<void>(resolve => window.requestAnimationFrame(() => resolve())); });
    await act(async () => {
      sessions = destination === 'empty' ? [] : [survivor];
      resolveClose({ schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminal: { ...closing, status: 'closed' } });
      await closePending;
    });
    const target = destination === 'empty'
      ? await screen.findByRole('button', { name: '新建终端' })
      : await screen.findByRole('tab', { name: 'Survivor' });
    await waitFor(() => expect(target).toHaveFocus());
    // Flush the actual mount frame: a surviving PTY must not take the return
    // focus back, nor may an equal later list projection replay that focus.
    await act(async () => { await new Promise<void>(resolve => window.requestAnimationFrame(() => resolve())); });
    expect(target).toHaveFocus();
    await act(async () => {
      queryClient.setQueryData(['system-terminal'], {
        schemaVersion: 'rag-ime.system-terminal.v1', ok: true,
        items: sessions.map(item => ({ ...item, nextCursor: 2 })),
      });
    });
    expect(target).toHaveFocus();
    expect(transport.requests.filter(call => call.request.pathId === 'terminal.session.close')).toHaveLength(1);
    expect(transport.requests.some(call => ['terminal.session.create', 'terminal.session.write'].includes(call.request.pathId))).toBe(false);
  });

  it('reveals the entire selected tab including its original close hit target', async () => {
    const user = userEvent.setup();
    const scrollIntoView = vi.spyOn(HTMLElement.prototype, 'scrollIntoView').mockImplementation(() => undefined);
    const first = terminalSession('terminal-one', 'Terminal');
    const second = terminalSession('terminal-two', 'Terminal');
    const transport = new MockControlTransport({ routes: {
      'terminal.sessions.list': { ok: true, items: [first, second] },
      'terminal.session.read': (request: ControlRequest) => ({ ok: true, terminal: asRecord(request.body).terminalId === first.terminalId ? first : second, cursor: 0, nextCursor: 0, text: '' }),
      'terminal.session.resize': { ok: true },
    } });
    try {
      renderTerminal(transport, <PawOsTerminalApp />);
      const firstTab = await screen.findByRole('tab', { name: 'Terminal 1' });
      await screen.findByRole('tab', { name: 'Terminal 2' });
      scrollIntoView.mockClear();
      await user.click(firstTab);
      const wrapper = firstTab.closest('.paw-terminal-tab')!;
      const originalClose = screen.getByRole('button', { name: '结束终端会话 Terminal 1' });
      expect(wrapper).toContainElement(originalClose);
      await waitFor(() => expect(scrollIntoView.mock.contexts).toContain(wrapper));
      expect(scrollIntoView.mock.contexts).not.toContain(firstTab);
    } finally { scrollIntoView.mockRestore(); }
  });

  it('does not replay a failed close focus handoff on a later independent list update', async () => {
    const user = userEvent.setup();
    const terminal = terminalSession('terminal-one', 'Terminal');
    const transport = new MockControlTransport({ routes: {
      'terminal.sessions.list': { ok: true, items: [terminal] },
      'terminal.session.read': { ok: true, terminal, cursor: 0, nextCursor: 0, text: '' },
      'terminal.session.resize': { ok: true },
      'terminal.session.close': () => { throw new Error('close unavailable'); },
    } });
    const { queryClient } = renderTerminal(transport, <><button type="button">Other action</button><PawOsTerminalApp /></>);
    await user.click(await screen.findByRole('button', { name: '结束终端会话 Terminal' }));
    await screen.findByText('结束终端会话失败：close unavailable');
    expect(screen.getByRole('tab', { name: 'Terminal' })).toHaveAttribute('aria-selected', 'true');
    const otherAction = screen.getByRole('button', { name: 'Other action' });
    await user.click(otherAction);
    await act(async () => { queryClient.setQueryData(['system-terminal'], { ok: true, items: [] }); });
    await screen.findByText('还没有终端会话');
    expect(otherAction).toHaveFocus();
    expect(transport.requests.filter(call => call.request.pathId === 'terminal.session.close')).toHaveLength(1);
    expect(transport.requests.some(call => call.request.pathId === 'terminal.session.create')).toBe(false);
  });

  it('retains the original manual create name and draft while its request is pending', async () => {
    const user = userEvent.setup();
    const terminal = terminalSession('terminal-one', 'Terminal');
    const transport = new MockControlTransport({ routes: {
      'terminal.sessions.list': { ok: true, items: [terminal] },
      'terminal.session.read': { ok: true, terminal, cursor: 0, nextCursor: 0, text: '' },
      'terminal.session.resize': { ok: true },
      'terminal.session.create': () => new Promise(() => undefined),
    } });
    renderTerminal(transport, <PawOsTerminalApp />);
    await user.click(await screen.findByRole('button', { name: '在指定目录新建终端' }));
    const form = screen.getByRole('form', { name: '在指定目录新建终端' });
    const input = within(form).getByRole('textbox', { name: '新终端工作目录' });
    await user.type(input, '/workspace/own');
    const submit = within(form).getByRole('button', { name: '新建终端' });
    await user.click(submit);
    expect(submit).toHaveAccessibleName('新建终端');
    expect(submit).toHaveAttribute('aria-busy', 'true');
    expect(submit).toBeDisabled();
    expect(input).toHaveValue('/workspace/own');
    await user.click(submit);
    expect(transport.requests.filter(call => call.request.pathId === 'terminal.session.create')).toHaveLength(1);
  });

  it('uses an action-oriented empty state without repeating Terminal App identity', async () => {
    const pendingCreate = new Promise<never>(() => undefined);
    const transport = new MockControlTransport({
      routes: {
        'terminal.sessions.list': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, items: [] },
        'terminal.session.create': () => pendingCreate,
      },
    });

    const { container } = renderTerminal(transport, <PawOsTerminalApp />);

    expect(await screen.findByText('还没有终端会话')).toBeInTheDocument();
    expect(container.querySelector('.paw-terminal-console__empty > svg')).toBeNull();
    expect(screen.queryByText('PAWOS 终端')).not.toBeInTheDocument();
    expect(screen.getAllByRole('button')).toHaveLength(1);
    const createButton = screen.getByRole('button', { name: '新建终端' });
    await waitFor(() => expect(createButton).toHaveAttribute('aria-busy', 'true'));
    expect(createButton).toBeDisabled();
    expect(createButton).toHaveAccessibleName('新建终端');
  });

  it('always creates the PAWOS embedded PTY and ignores a legacy external-terminal bridge', async () => {
    const calls: string[] = [];
    const terminal = terminalSession('terminal-embedded', 'Terminal');
    let terminals: ReturnType<typeof terminalSession>[] = [];
    (window as Window & { pawTerminalHost?: unknown }).pawTerminalHost = {
      kind: 'electron-terminal-host',
      closeSurface: async () => { calls.push('close'); return { ok: true }; },
      focusSurface: async () => { calls.push('focus'); return { ok: false }; },
      getStatus: async () => ({ ok: true, external: true }),
      listSurfaces: async () => ({ ok: true, external: true, items: [] }),
      openBackgroundJob: async () => { calls.push('background'); return { ok: false }; },
      openShell: async () => { calls.push('open'); return { ok: false }; },
    };
    const transport = new MockControlTransport({
      routes: {
        'terminal.sessions.list': () => ({ schemaVersion: 'rag-ime.system-terminal.v1', ok: true, items: terminals }),
        'terminal.session.create': () => {
          terminals = [terminal];
          return { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminal };
        },
        'terminal.session.read': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminal, cursor: 0, nextCursor: 0, truncated: false, text: '' },
        'terminal.session.resize': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminalId: terminal.terminalId },
      },
    });

    renderApp(transport, <PawOsTerminalApp />);

    expect(await screen.findByLabelText('终端输入输出')).toBeInTheDocument();
    const terminalInput = await screen.findByRole('textbox', { name: '终端输入' });
    await act(async () => { await new Promise<void>(resolve => window.requestAnimationFrame(() => resolve())); });
    expect(terminalInput).toHaveFocus();
    expect(transport.requests.some((call) => call.request.pathId === 'terminal.session.create')).toBe(true);
    expect(calls).toEqual([]);
    expect(screen.queryByText('外部终端')).not.toBeInTheDocument();
  });

  it('numbers repeated session titles so every tab keeps a distinct identity and scrolls the selection into view', async () => {
    const user = userEvent.setup();
    const scrollIntoView = vi.spyOn(HTMLElement.prototype, 'scrollIntoView').mockImplementation(() => undefined);
    const first = terminalSession('terminal-one', 'Terminal');
    const second = terminalSession('terminal-two', 'Terminal');
    const transport = new MockControlTransport({
      routes: {
        'terminal.sessions.list': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, items: [first, second] },
        'terminal.session.read': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminal: second, cursor: 0, nextCursor: 0, truncated: false, text: '' },
        'terminal.session.resize': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminalId: second.terminalId },
      },
    });

    try {
      renderApp(transport, <PawOsTerminalApp />);

      const firstTab = await screen.findByRole('tab', { name: 'Terminal 1' });
      const secondTab = screen.getByRole('tab', { name: 'Terminal 2' });
      expect(firstTab).toHaveAttribute('title', 'Terminal 1 · /bin/zsh · /workspace/paw');
      expect(secondTab).toHaveAttribute('aria-selected', 'true');
      // The cwd basename is a visible identity hint that stays out of the
      // accessible tab name, which the exact name queries above already prove.
      const cwdHint = firstTab.querySelector('.paw-terminal-tab-cwd');
      expect(cwdHint).toHaveTextContent('paw');
      expect(cwdHint).toHaveAttribute('aria-hidden', 'true');
      expect(screen.getByRole('button', { name: '结束终端会话 Terminal 1' })).toBeInTheDocument();
      await waitFor(() => expect(scrollIntoView).toHaveBeenCalled());

      scrollIntoView.mockClear();
      await user.click(firstTab);
      expect(firstTab).toHaveAttribute('aria-selected', 'true');
      await waitFor(() => expect(scrollIntoView).toHaveBeenCalled());
    } finally {
      scrollIntoView.mockRestore();
    }
  });

  it('surfaces hidden tabs through edge fades and an all-sessions switcher only while the strip overflows', async () => {
    const user = userEvent.setup();
    const first = terminalSession('terminal-one', 'Terminal');
    const second = { ...terminalSession('terminal-two', 'Terminal'), cwd: '/workspace/other' };
    const transport = new MockControlTransport({
      routes: {
        'terminal.sessions.list': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, items: [first, second] },
        'terminal.session.read': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminal: second, cursor: 0, nextCursor: 0, truncated: false, text: '' },
        'terminal.session.resize': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminalId: second.terminalId },
      },
    });

    renderApp(transport, <PawOsTerminalApp />);

    const tablist = await screen.findByRole('tablist', { name: 'PAWOS 终端' });
    await screen.findByRole('tab', { name: 'Terminal 1' });
    // While every tab fits, no overflow affordance may exist.
    expect(tablist).not.toHaveAttribute('data-overflow');
    expect(screen.queryByRole('button', { name: /列出全部终端/ })).not.toBeInTheDocument();

    // The strip measures its own scroll box; simulate a real overflow.
    Object.defineProperties(tablist, {
      scrollWidth: { configurable: true, value: 640 },
      clientWidth: { configurable: true, value: 240 },
    });
    fireEvent.scroll(tablist);
    expect(tablist).toHaveAttribute('data-overflow');
    expect(tablist).toHaveAttribute('data-at-start');
    expect(tablist).not.toHaveAttribute('data-at-end');

    const toggle = screen.getByRole('button', { name: '列出全部终端（2 个）' });
    await user.click(toggle);
    const switcher = screen.getByRole('group', { name: '全部终端会话' });
    const rows = within(switcher).getAllByRole('button');
    expect(rows).toHaveLength(2);
    expect(rows[1]).toHaveAttribute('aria-current', 'true');
    expect(within(switcher).getByText('/workspace/other')).toBeInTheDocument();

    // Escape closes the switcher and hands focus back to its toggle.
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('group', { name: '全部终端会话' })).not.toBeInTheDocument();
    expect(toggle).toHaveFocus();

    await user.click(toggle);
    await user.click(within(screen.getByRole('group', { name: '全部终端会话' })).getByRole('button', { name: /Terminal 1/ }));
    expect(screen.queryByRole('group', { name: '全部终端会话' })).not.toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Terminal 1' })).toHaveAttribute('aria-selected', 'true');
  });

  it('keeps an ended session readable, states the exit truthfully, and never reruns or forwards input', async () => {
    const user = userEvent.setup();
    const exited = { ...terminalSession('terminal-exited', 'Terminal'), status: 'exited' as const, exitCode: 1 };
    let terminals = [exited];
    const transport = new MockControlTransport({
      routes: {
        'terminal.sessions.list': () => ({ schemaVersion: 'rag-ime.system-terminal.v1', ok: true, items: terminals }),
        'terminal.session.read': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminal: exited, cursor: 0, nextCursor: 0, truncated: false, text: '' },
        'terminal.session.resize': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminalId: exited.terminalId },
        'terminal.session.close': () => {
          terminals = [];
          return { schemaVersion: 'rag-ime.system-terminal.v1', ok: true };
        },
      },
    });

    renderApp(transport, <PawOsTerminalApp />);

    // Viewing an existing exited session must not invent a new one.
    const tab = await screen.findByRole('tab', { name: /Terminal.*已退出（退出码 1）/ });
    expect(transport.requests.some((call) => call.request.pathId === 'terminal.session.create')).toBe(false);
    expect(tab.querySelector('i[data-state="exited"][data-exit-failure]')).not.toBeNull();

    // The exit is stated in words on the tab, not only in the dot colour, and
    // the whole tab carries the state so the strip can dim what is over.
    expect(tab.closest('.paw-terminal-tab')).toHaveAttribute('data-state', 'exited');
    expect(tab.closest('.paw-terminal-tab')).toHaveAttribute('data-exit-failure');
    expect(tab.querySelector('.paw-terminal-tab-exit')).toHaveTextContent('已退出 1');
    expect(tab.querySelector('.paw-terminal-tab-cwd')).toBeNull();

    // The status band tells the same truth, and a non-zero exit is the one
    // state that earns the danger tone.
    const statusbar = document.querySelector('.paw-terminal-statusbar') as HTMLElement;
    expect(statusbar).toHaveAttribute('data-state', 'exited');
    expect(statusbar).toHaveAttribute('data-failure');
    expect(statusbar.querySelector('.paw-terminal-exited-badge')).toHaveTextContent('已退出 (1)');
    expect(statusbar.querySelector('.paw-terminal-running-badge')).toBeNull();

    const notice = screen.getByRole('status');
    expect(notice).toHaveTextContent('这个终端会话已退出（退出码 1）。输出仍可回看，输入不会再发送。');
    expect(within(notice).getByRole('button', { name: '新建终端' })).toBeInTheDocument();

    // Typed input is refused locally with a dismissible explanation.
    await user.type(await screen.findByRole('textbox', { name: '终端输入' }), 'x');
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('这个终端已退出，输入没有发送。');
    expect(transport.requests.some((call) => call.request.pathId === 'terminal.session.write')).toBe(false);
    await user.click(within(alert).getByRole('button', { name: '关闭错误提示' }));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();

    // The notice can end the tab identity explicitly.
    await user.click(within(notice).getByRole('button', { name: '关闭此标签页' }));
    expect(await screen.findByText('还没有终端会话')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: '新建终端' })).toHaveFocus());
    expect(screen.queryByRole('tab')).not.toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: '终端输入' })).not.toBeInTheDocument();
    expect(transport.requests.filter((call) => call.request.pathId === 'terminal.session.close')).toHaveLength(1);
    expect(transport.requests.some((call) => call.request.pathId === 'terminal.session.create')).toBe(false);
  });

  it('applies an exited status from the authoritative read receipt immediately', async () => {
    const user = userEvent.setup();
    const running = terminalSession('terminal-fast-exit', 'Terminal');
    const exited = { ...running, status: 'exited' as const, exitCode: 0 };
    const transport = new MockControlTransport({
      routes: {
        'terminal.sessions.list': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, items: [running] },
        'terminal.session.read': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminal: exited, cursor: 0, nextCursor: 0, truncated: false, text: '' },
        'terminal.session.resize': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminalId: running.terminalId },
        'terminal.session.write': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminalId: running.terminalId, bytesWritten: 1 },
      },
    });

    renderApp(transport, <PawOsTerminalApp />);

    expect(await screen.findByRole('tab', { name: /Terminal.*已退出（退出码 0）/ })).toBeInTheDocument();
    await user.type(await screen.findByRole('textbox', { name: '终端输入' }), 'x');
    await new Promise((resolve) => window.setTimeout(resolve, 20));
    expect(transport.requests.some((call) => call.request.pathId === 'terminal.session.write')).toBe(false);
  });

  it('creates a terminal in a chosen working directory and refuses relative paths locally', async () => {
    const user = userEvent.setup();
    const terminal = terminalSession('terminal-one', 'Terminal');
    let terminals = [terminal];
    const createBodies: Record<string, unknown>[] = [];
    const transport = new MockControlTransport({
      routes: {
        'terminal.sessions.list': () => ({ schemaVersion: 'rag-ime.system-terminal.v1', ok: true, items: terminals }),
        'terminal.session.read': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminal, cursor: 0, nextCursor: 0, truncated: false, text: '' },
        'terminal.session.resize': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminalId: terminal.terminalId },
        'terminal.session.create': (request: ControlRequest) => {
          const body = asRecord(request.body);
          createBodies.push(body);
          const created = { ...terminalSession(`terminal-${createBodies.length + 1}`, 'Terminal'), cwd: String(body.cwd ?? '/workspace/paw') };
          terminals = [...terminals, created];
          return { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminal: created };
        },
      },
    });

    renderApp(transport, <PawOsTerminalApp />);

    await user.click(await screen.findByRole('button', { name: '在指定目录新建终端' }));
    const form = screen.getByRole('form', { name: '在指定目录新建终端' });
    const cwdInput = within(form).getByRole('textbox', { name: '新终端工作目录' });
    expect(cwdInput).toHaveFocus();

    // A relative path is refused locally with a truthful hint; nothing is sent.
    await user.type(cwdInput, 'relative/path');
    expect(within(form).getByRole('alert')).toHaveTextContent('请输入以 / 开头的绝对路径。');
    expect(within(form).getByRole('button', { name: '新建终端' })).toBeDisabled();
    expect(createBodies).toHaveLength(0);

    await user.clear(cwdInput);
    await user.type(cwdInput, '/workspace/other');
    await user.click(within(form).getByRole('button', { name: '新建终端' }));
    await waitFor(() => expect(createBodies).toHaveLength(1));
    expect(createBodies[0].cwd).toBe('/workspace/other');
    expect(screen.queryByRole('form', { name: '在指定目录新建终端' })).not.toBeInTheDocument();
    const cwdToggle = screen.getByRole('button', { name: '在指定目录新建终端' });
    expect(cwdToggle).toHaveFocus();

    await user.click(cwdToggle);
    const escapeInput = screen.getByRole('textbox', { name: '新终端工作目录' });
    await user.type(escapeInput, '/tmp{Escape}');
    expect(screen.queryByRole('form', { name: '在指定目录新建终端' })).not.toBeInTheDocument();
    expect(cwdToggle).toHaveFocus();

    await user.click(cwdToggle);
    await user.click(within(screen.getByRole('form', { name: '在指定目录新建终端' })).getByRole('button', { name: '取消' }));
    expect(cwdToggle).toHaveFocus();

    // The plain new-terminal action keeps the backend default directory.
    await user.click(screen.getByRole('button', { name: '新建终端' }));
    await waitFor(() => expect(createBodies).toHaveLength(2));
    expect('cwd' in createBodies[1]).toBe(false);
  });

  it('preserves the directory draft after failed creation and until the corrected creation is accepted', async () => {
    const user = userEvent.setup();
    const first = terminalSession('terminal-one', 'Terminal');
    const created = { ...terminalSession('terminal-two', 'Terminal'), cwd: '/workspace/retry' };
    let terminals = [first];
    let attempts = 0;
    let accept!: () => void;
    const transport = new MockControlTransport({ routes: {
      'terminal.sessions.list': () => ({ ok: true, items: terminals }),
      'terminal.session.read': (request: ControlRequest) => ({ ok: true, terminal: asRecord(request.body).terminalId === first.terminalId ? first : created, cursor: 0, nextCursor: 0, truncated: false, text: '' }),
      'terminal.session.resize': { ok: true },
      'terminal.session.create': () => {
        attempts += 1;
        if (attempts === 1) throw new Error('路径不存在');
        return new Promise((resolve) => { accept = () => { terminals = [first, created]; resolve({ ok: true, terminal: created }); }; });
      },
    } });
    renderApp(transport, <PawOsTerminalApp />);
    await user.click(await screen.findByRole('button', { name: '在指定目录新建终端' }));
    const form = screen.getByRole('form', { name: '在指定目录新建终端' });
    const input = within(form).getByRole('textbox', { name: '新终端工作目录' });
    await user.type(input, '/workspace/missing');
    await user.click(within(form).getByRole('button', { name: '新建终端' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('路径不存在');
    expect(form).toBeInTheDocument();
    expect(input).toHaveValue('/workspace/missing');
    await user.clear(input);
    await user.type(input, created.cwd);
    await user.click(within(form).getByRole('button', { name: '新建终端' }));
    await waitFor(() => expect(accept).toBeTypeOf('function'));
    expect(form).toBeInTheDocument();
    expect(input).toHaveValue(created.cwd);
    await act(async () => { accept(); });
    await waitFor(() => expect(screen.queryByRole('form', { name: '在指定目录新建终端' })).not.toBeInTheDocument());
    expect(screen.getByRole('tab', { name: 'Terminal 2' })).toHaveAttribute('aria-selected', 'true');
    expect(attempts).toBe(2);
  });

  it('searches the scrollback with a live match position and clears decorations on close', async () => {
    const user = userEvent.setup();
    const terminal = terminalSession('terminal-one', 'Terminal');
    const transport = new MockControlTransport({
      routes: {
        'terminal.sessions.list': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, items: [terminal] },
        'terminal.session.read': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminal, cursor: 0, nextCursor: 0, truncated: false, text: '' },
        'terminal.session.resize': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminalId: terminal.terminalId },
      },
    });

    renderApp(transport, <PawOsTerminalApp />);

    expect(await screen.findByLabelText('终端输入输出')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '搜索终端输出' }));
    const searchInput = screen.getByRole('textbox', { name: '搜索终端输出' });
    expect(searchInput).toHaveFocus();

    await user.type(searchInput, 'PAW');
    const nextCalls = () => searchAddonState.calls.filter((call) => call.kind === 'next');
    await waitFor(() => expect(nextCalls().length).toBeGreaterThan(0));
    expect(nextCalls().at(-1)?.term).toBe('PAW');
    // Decorations are requested so the reported match position is real.
    expect(nextCalls().at(-1)?.options).toMatchObject({ decorations: expect.any(Object) });

    act(() => { for (const listener of searchAddonState.listeners) listener({ resultIndex: 1, resultCount: 5 }); });
    expect(screen.getByText('2/5')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '上一个匹配' }));
    expect(searchAddonState.calls.at(-1)).toMatchObject({ kind: 'previous', term: 'PAW' });

    act(() => { for (const listener of searchAddonState.listeners) listener({ resultIndex: -1, resultCount: 0 }); });
    expect(screen.getByText('无匹配')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '关闭搜索' }));
    expect(screen.queryByRole('textbox', { name: '搜索终端输出' })).not.toBeInTheDocument();
    expect(searchAddonState.calls.at(-1)).toMatchObject({ kind: 'clear' });
    await user.click(screen.getByRole('button', { name: '搜索终端输出' }));
    await user.type(screen.getByRole('textbox', { name: '搜索终端输出' }), 'PAW');
    await user.click(screen.getByRole('button', { name: '下一个匹配' }));
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('search')).not.toBeInTheDocument();
  });

  it('opens scrollback search as a console band instead of a panel over the output it reports', async () => {
    const user = userEvent.setup();
    const terminal = terminalSession('terminal-one', 'Terminal');
    const transport = new MockControlTransport({
      routes: {
        'terminal.sessions.list': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, items: [terminal] },
        'terminal.session.read': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminal, cursor: 0, nextCursor: 0, truncated: false, text: '' },
        'terminal.session.resize': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminalId: terminal.terminalId },
      },
    });

    const { container } = renderTerminal(transport, <PawOsTerminalApp />);

    const surface = await screen.findByLabelText('终端输入输出');
    const consoleBands = container.querySelector('.paw-terminal-console') as HTMLElement;
    expect(consoleBands).not.toHaveAttribute('data-search');

    await user.click(screen.getByRole('button', { name: '搜索终端输出' }));
    const band = screen.getByRole('search');
    // The band is a real console row: it is a child of the band column and it
    // sits above the surface, so it can never cover a reported match.
    expect(consoleBands).toHaveAttribute('data-search');
    expect(band.parentElement).toBe(consoleBands);
    expect(band.compareDocumentPosition(surface) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // Taking a row must not cost the live session: the PTY is refit, not rebuilt.
    expect(xtermConstructorOptions).toHaveLength(1);
    expect(screen.getByLabelText('终端输入输出')).toBe(surface);

    await user.keyboard('{Escape}');
    expect(consoleBands).not.toHaveAttribute('data-search');
    expect(screen.queryByRole('search')).not.toBeInTheDocument();
  });

  it.each(['search', 'cwd', 'tab', 'other', 'other-window'] as const)('preserves explicit %s focus acquired before the initial xterm mount frame', async (control) => {
    const terminal = terminalSession('terminal-one', 'Terminal');
    const transport = new MockControlTransport({ routes: {
      'terminal.sessions.list': { ok: true, items: [terminal] },
      'terminal.session.read': { ok: true, terminal, cursor: 0, nextCursor: 0, text: '' },
      'terminal.session.resize': { ok: true },
    } });
    const nativeFrame = window.requestAnimationFrame.bind(window);
    const pendingFrames: FrameRequestCallback[] = [];
    // Retain the actual scheduled mount work until the user has acquired a
    // control, then execute it on a real animation frame; no timing guess.
    const schedule = vi.spyOn(window, 'requestAnimationFrame').mockImplementation(callback => {
      pendingFrames.push(callback);
      return nativeFrame(() => undefined);
    });
    try {
      renderTerminal(transport, <><button type="button">Other action</button>{terminalFrame(<PawOsTerminalApp />)}{terminalFrame(<span>Other content</span>, 'other', false)}</>);
      const terminalInput = await screen.findByRole('textbox', { name: '终端输入' });
      expect(pendingFrames).toHaveLength(1);
      let target: HTMLElement;
      if (control === 'search') {
        fireEvent.click(screen.getByRole('button', { name: '搜索终端输出' }));
        target = screen.getByRole('textbox', { name: '搜索终端输出' });
      } else if (control === 'cwd') {
        fireEvent.click(screen.getByRole('button', { name: '在指定目录新建终端' }));
        target = screen.getByRole('textbox', { name: '新终端工作目录' });
      } else if (control === 'tab') {
        target = screen.getByRole('tab', { name: 'Terminal' });
        target.focus();
      } else if (control === 'other-window') {
        target = screen.getByLabelText('other窗口');
        target.focus();
      } else {
        target = screen.getByRole('button', { name: 'Other action' });
        target.focus();
      }
      expect(target).toHaveFocus();
      await act(async () => { await new Promise<void>(resolve => nativeFrame(timestamp => {
        pendingFrames.splice(0).forEach(callback => callback(timestamp));
        resolve();
      })); });
      expect(target).toHaveFocus();
      if (control === 'search') {
        await userEvent.setup().keyboard('{Escape}');
        expect(screen.queryByRole('search')).not.toBeInTheDocument();
        expect(terminalInput).toHaveFocus();
      }
      expect(xtermConstructorOptions).toHaveLength(1);
      expect(transport.requests.some(call => call.request.pathId === 'terminal.session.write')).toBe(false);
    } finally { schedule.mockRestore(); }
  });

  it('keeps the status band ordered by truth and never lets it leave the frame', async () => {
    const terminal = terminalSession('terminal-one', 'Terminal');
    const transport = new MockControlTransport({
      routes: {
        'terminal.sessions.list': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, items: [terminal] },
        'terminal.session.read': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminal, cursor: 0, nextCursor: 0, truncated: false, text: '' },
        'terminal.session.resize': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminalId: terminal.terminalId },
      },
    });

    const { container } = renderTerminal(transport, <PawOsTerminalApp />);

    await screen.findByLabelText('终端输入输出');
    const statusbar = container.querySelector('.paw-terminal-statusbar') as HTMLElement;
    // Live state leads, the working directory follows, and the process facts
    // that only ever confirm them are the group narrow windows may retire.
    expect(statusbar.firstElementChild).toHaveClass('paw-terminal-state-tag');
    expect(statusbar).toHaveAttribute('data-state', 'running');
    expect(statusbar).not.toHaveAttribute('data-failure');
    expect(statusbar.querySelector('.paw-terminal-running-badge')).toHaveTextContent('运行中');
    const facts = statusbar.querySelector('.paw-terminal-statusbar__facts') as HTMLElement;
    expect(within(facts).getByText('pid 4242')).toBeInTheDocument();
    expect(within(facts).getByText('UTF-8')).toBeInTheDocument();
    expect(within(facts).getByText('104×30')).toBeInTheDocument();
    // The band belongs to the console column, not to the scrolling surface.
    expect(statusbar.parentElement).toBe(container.querySelector('.paw-terminal-console'));
  });

  it('keeps every tab addressable by ordinal when the strip collapses to icon-first', async () => {
    const first = terminalSession('terminal-one', 'Terminal');
    const second = terminalSession('terminal-two', 'Terminal');
    const transport = new MockControlTransport({
      routes: {
        'terminal.sessions.list': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, items: [first, second] },
        'terminal.session.read': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminal: second, cursor: 0, nextCursor: 0, truncated: false, text: '' },
        'terminal.session.resize': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminalId: second.terminalId },
        'terminal.session.write': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminalId: second.terminalId, bytesWritten: 0 },
      },
    });

    renderApp(transport, <PawOsTerminalApp />);

    const firstTab = await screen.findByRole('tab', { name: 'Terminal 1' });
    const secondTab = screen.getByRole('tab', { name: 'Terminal 2' });
    expect(secondTab).toHaveAttribute('aria-selected', 'true');

    // The visible ordinal survives an icon-first strip; the full identity
    // stays in the accessible name, which no longer depends on visible text.
    const ordinal = firstTab.querySelector('.paw-terminal-tab-ordinal');
    expect(ordinal).toHaveTextContent('1');
    expect(ordinal).toHaveAttribute('aria-hidden', 'true');
    expect(firstTab).toHaveAttribute('aria-label', 'Terminal 1');
    expect(firstTab.querySelector('.paw-terminal-tab-label')).toHaveTextContent('Terminal 1');

    // The chord selects the same ordinal and never reaches the shell.
    const ptyInput = await screen.findByRole('textbox', { name: '终端输入' });
    fireEvent.keyDown(ptyInput, { code: 'Digit1', key: '1', ctrlKey: true, shiftKey: true });
    await waitFor(() => expect(firstTab).toHaveAttribute('aria-selected', 'true'));
    fireEvent.keyDown(ptyInput, { code: 'Digit9', key: '9', ctrlKey: true, shiftKey: true });
    expect(firstTab).toHaveAttribute('aria-selected', 'true');
    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'terminal.session.write')).toBe(false));
  });

  it('keeps PAWOS keyboard chords out of the PTY: search opens and a sibling terminal is created', async () => {
    const created = terminalSession('terminal-two', 'Terminal Two');
    const first = terminalSession('terminal-one', 'Terminal One');
    let terminals = [first];
    const transport = new MockControlTransport({
      routes: {
        'terminal.sessions.list': () => ({ schemaVersion: 'rag-ime.system-terminal.v1', ok: true, items: terminals }),
        'terminal.session.read': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminal: first, cursor: 0, nextCursor: 0, truncated: false, text: '' },
        'terminal.session.resize': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminalId: first.terminalId },
        'terminal.session.write': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminalId: first.terminalId, bytesWritten: 0 },
        'terminal.session.create': () => {
          terminals = [...terminals, created];
          return { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminal: created };
        },
      },
    });

    renderApp(transport, <PawOsTerminalApp />);

    const ptyInput = await screen.findByRole('textbox', { name: '终端输入' });
    fireEvent.keyDown(ptyInput, { code: 'KeyF', key: 'F', ctrlKey: true, shiftKey: true });
    expect(await screen.findByRole('textbox', { name: '搜索终端输出' })).toBeInTheDocument();

    fireEvent.keyDown(ptyInput, { code: 'KeyT', key: 'T', ctrlKey: true, shiftKey: true });
    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'terminal.session.create')).toBe(true));
    expect(await screen.findByRole('tab', { name: /Terminal Two/ })).toHaveAttribute('aria-selected', 'true');

    // Neither chord may leak into the shell as typed input.
    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === 'terminal.session.write')).toBe(false));
  });

  it('ignores a late resize failure from the closed terminal after selecting the surviving tab', async () => {
    const user = userEvent.setup();
    const first = terminalSession('terminal-one', 'Terminal One');
    const second = terminalSession('terminal-two', 'Terminal Two');
    let sessions = [first, second];
    let rejectResize!: (reason: Error) => void;
    const transport = new MockControlTransport({ routes: {
      'terminal.sessions.list': () => ({ ok: true, items: sessions }),
      'terminal.session.read': (request: ControlRequest) => ({ ok: true, terminal: asRecord(request.body).terminalId === first.terminalId ? first : second, cursor: 0, nextCursor: 0, truncated: false, text: '' }),
      'terminal.session.resize': (request: ControlRequest) => asRecord(request.body).terminalId === second.terminalId
        ? new Promise((_resolve, reject) => { rejectResize = reject; }) : { ok: true },
      'terminal.session.close': () => { sessions = [first]; return { ok: true, terminal: { ...second, status: 'closed' } }; },
    } });
    renderApp(transport, <PawOsTerminalApp />);
    await waitFor(() => expect(rejectResize).toBeTypeOf('function'));
    await user.click(screen.getByRole('button', { name: '结束终端会话 Terminal Two' }));
    await waitFor(() => expect(screen.getByRole('tab', { name: 'Terminal One' })).toHaveAttribute('aria-selected', 'true'));
    await act(async () => rejectResize(new Error('terminal session was not found')));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Terminal Two' })).not.toBeInTheDocument();
    expect(transport.requests.filter(({ request }) => request.pathId === 'terminal.session.create')).toHaveLength(0);
  });

  it.each(['success', 'failure'] as const)('keeps the selected terminal error when the previous tab write settles with %s', async (outcome) => {
    const user = userEvent.setup();
    const first = terminalSession('terminal-one', 'Terminal One');
    const second = terminalSession('terminal-two', 'Terminal Two');
    let resolveWrite!: (value: unknown) => void;
    let rejectWrite!: (reason: Error) => void;
    const transport = new MockControlTransport({ routes: {
      'terminal.sessions.list': { ok: true, items: [first, second] },
      'terminal.session.read': (request: ControlRequest) => ({ ok: true, terminal: asRecord(request.body).terminalId === first.terminalId ? first : second, cursor: 0, nextCursor: 0, truncated: false, text: '' }),
      'terminal.session.resize': (request: ControlRequest) => {
        if (asRecord(request.body).terminalId === first.terminalId) throw new Error('selected terminal resize failed');
        return { ok: true };
      },
      'terminal.session.write': () => new Promise((resolve, reject) => { resolveWrite = resolve; rejectWrite = reject; }),
    } });
    renderApp(transport, <PawOsTerminalApp />);
    fireEvent.keyDown(await screen.findByRole('textbox', { name: '终端输入' }), { key: 'x', code: 'KeyX' });
    await waitFor(() => expect(resolveWrite).toBeTypeOf('function'));
    await user.click(screen.getByRole('tab', { name: 'Terminal One' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('selected terminal resize failed');
    await act(async () => {
      if (outcome === 'success') resolveWrite({ ok: true });
      else rejectWrite(new Error('previous terminal write failed'));
    });
    expect(screen.getByRole('alert')).toHaveTextContent('selected terminal resize failed');
    expect(screen.getByRole('alert')).not.toHaveTextContent('previous terminal write failed');
  });

  it('copies the working directory from the status bar with a temporary truthful receipt', async () => {
    const user = userEvent.setup();
    const terminal = terminalSession('terminal-one', 'Terminal');
    const transport = new MockControlTransport({
      routes: {
        'terminal.sessions.list': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, items: [terminal] },
        'terminal.session.read': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminal, cursor: 0, nextCursor: 0, truncated: false, text: '' },
        'terminal.session.resize': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminalId: terminal.terminalId },
      },
    });

    renderApp(transport, <PawOsTerminalApp />);

    const copyButton = await screen.findByRole('button', { name: '复制工作目录 /workspace/paw' });
    expect(copyButton).toHaveTextContent('/workspace/paw');
    await user.click(copyButton);
    await waitFor(() => expect(clipboardWrites).toEqual(['/workspace/paw']));
    expect(copyButton).toHaveTextContent('已复制路径');
    expect(copyButton).toHaveAttribute('data-copied');
    // The receipt is temporary; the truthful path returns on its own.
    await waitFor(() => expect(copyButton).toHaveTextContent('/workspace/paw'), { timeout: 3_000 });
    expect(copyButton).not.toHaveAttribute('data-copied');
  });

  it('lets a failed create be dismissed instead of leaving a stuck banner', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'terminal.sessions.list': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, items: [] },
        'terminal.session.create': () => {
          throw new Error('后端拒绝了新终端');
        },
      },
    });

    renderApp(transport, <PawOsTerminalApp />);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('新建终端失败：后端拒绝了新终端');
    // The notice floats inside the workspace instead of taking a layout row.
    expect(alert.closest('.paw-terminal-app__workspace')).not.toBeNull();
    await user.click(within(alert).getByRole('button', { name: '关闭错误提示' }));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '新建终端' })).toBeInTheDocument();
  });

  it('offers an immediate retry when the session list cannot be read', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'terminal.sessions.list': () => {
          throw new Error('系统终端服务未连接');
        },
      },
    });

    renderApp(transport, <PawOsTerminalApp />);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('读取终端会话失败：系统终端服务未连接');
    expect(screen.getByText('终端会话暂时不可用')).toBeInTheDocument();
    expect(screen.queryByText('还没有终端会话')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '新建终端' })).not.toBeInTheDocument();
    // A failed list read proves nothing about existing sessions; it must not
    // fall through to the first-load auto-create.
    expect(transport.requests.some((call) => call.request.pathId === 'terminal.session.create')).toBe(false);
    const listCalls = () => transport.requests.filter((call) => call.request.pathId === 'terminal.sessions.list').length;
    const before = listCalls();
    await user.click(within(alert).getByRole('button', { name: '重试' }));
    await waitFor(() => expect(listCalls()).toBeGreaterThan(before));
  });

  it('disables the blinking cursor when PAWOS reduced motion is active', async () => {
    document.documentElement.dataset.reduceMotion = 'true';
    const terminal = terminalSession('terminal-calm', 'Terminal');
    const transport = new MockControlTransport({
      routes: {
        'terminal.sessions.list': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, items: [terminal] },
        'terminal.session.read': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminal, cursor: 0, nextCursor: 0, truncated: false, text: '' },
        'terminal.session.resize': { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminalId: terminal.terminalId },
      },
    });

    renderApp(transport, <PawOsTerminalApp />);

    expect(await screen.findByLabelText('终端输入输出')).toBeInTheDocument();
    await waitFor(() => expect(xtermConstructorOptions.length).toBeGreaterThan(0));
    expect(xtermConstructorOptions.at(-1)).toMatchObject({ cursorBlink: false });
  });
});

describe('paw-os-terminal-app.css contracts', () => {
  it('builds the App as fixed chrome bands around one flexible surface', () => {
    // Window skeleton, not a web page: the App is a column, the tab strip is
    // an intrinsic band, and exactly one band absorbs the window.
    expect(terminalCss).toMatch(/\.paw-terminal-app\s*\{[^}]*flex-direction:\s*column;/s);
    expect(terminalCss).toMatch(/\.paw-terminal-app > \.paw-terminal-app__toolbar\s*\{[^}]*flex:\s*0 0 auto;/s);
    expect(terminalCss).toMatch(/\.paw-terminal-app__workspace\s*\{[^}]*flex:\s*1 1 auto;/s);
    expect(terminalCss).toMatch(/\.paw-terminal-app__workspace\s*\{[^}]*min-height:\s*0;/s);
  });

  it('gives the console one row template per real band combination', () => {
    expect(terminalCss).toMatch(/\.paw-terminal-console\[data-session\]\s*\{[\s\S]*?grid-template-rows:\s*minmax\(0, 1fr\) var\(--paw-terminal-status-height\);/s);
    expect(terminalCss).toMatch(/\.paw-terminal-console\[data-session\]\[data-search\]\s*\{[\s\S]*?grid-template-rows:\s*var\(--paw-terminal-search-height\) minmax\(0, 1fr\) var\(--paw-terminal-status-height\);/s);
    expect(terminalCss).toMatch(/\.paw-terminal-console\[data-session\]\[data-ended\]\s*\{[\s\S]*?grid-template-rows:\s*minmax\(0, 1fr\) auto var\(--paw-terminal-status-height\);/s);
    expect(terminalCss).toMatch(/\.paw-terminal-console\[data-session\]\[data-search\]\[data-ended\]\s*\{[\s\S]*?grid-template-rows:\s*var\(--paw-terminal-search-height\) minmax\(0, 1fr\) auto var\(--paw-terminal-status-height\);/s);
  });

  it('keeps search a band and keeps the menus that must not resize the PTY floating', () => {
    expect(terminalCss).not.toMatch(/\.paw-terminal-search\s*\{[^}]*position:\s*absolute;/s);
    expect(terminalCss).toMatch(/\.paw-terminal-switcher\s*\{[^}]*position:\s*absolute;/s);
    expect(terminalCss).toMatch(/\.paw-terminal-error\s*\{[^}]*position:\s*absolute;/s);
  });

  it('answers narrow widths with the container that owns the width, down to icon-first tabs', () => {
    // The strip is portalled into the titlebar, so it must query its own box
    // rather than the App body it no longer lives in.
    expect(terminalCss).toMatch(/\.paw-terminal-app__toolbar\s*\{[^}]*container:\s*paw-terminal-bar \/ inline-size;/s);
    expect(terminalCss).toMatch(/\.paw-terminal-app\s*\{[^}]*container:\s*paw-terminal \/ inline-size;/s);
    expect(terminalCss).not.toMatch(/@media[^{]*\(max-width/);

    const iconFirst = terminalCss.slice(terminalCss.indexOf('@container paw-terminal-bar (max-width: 300px)'));
    expect(iconFirst).toMatch(/\.paw-terminal-tab-label,[\s\S]*?\.paw-terminal-tab-exit\s*\{\s*display:\s*none;\s*\}/);
    expect(iconFirst).toMatch(/\.paw-terminal-tab-ordinal\s*\{\s*display:\s*block;\s*\}/);

    // The status band retires its confirming facts, never the live state or
    // the path, and the search band never retires a hit target.
    const narrowApp = terminalCss.slice(terminalCss.indexOf('@container paw-terminal (max-width: 560px)'));
    expect(narrowApp).toMatch(/\.paw-terminal-statusbar__facts\s*\{\s*display:\s*none;\s*\}/);
    expect(narrowApp).not.toMatch(/\.paw-terminal-state-tag\s*\{\s*display:\s*none;/);
    expect(narrowApp).not.toMatch(/\.paw-terminal-cwd\s*\{\s*display:\s*none;/);
    expect(narrowApp).not.toMatch(/\.paw-terminal-search button\s*\{\s*display:\s*none;/);
  });

  it('carries exactly the three deliberate motions and silences all of them', () => {
    // 1: the tab-switch beam. 2: the search band unfolding. 3: the PTY focus edge.
    expect(terminalCss).toMatch(/\.paw-terminal-tab::before\s*\{[^}]*transform:\s*scaleX\(\.3\);/s);
    expect(terminalCss).toMatch(/\.paw-terminal-tab\[data-selected\]::before\s*\{[^}]*transform:\s*scaleX\(1\);/s);
    expect(terminalCss).toMatch(/\.paw-terminal-search\s*\{[^}]*animation:\s*paw-terminal-band-open/s);
    expect(terminalCss).toMatch(/@keyframes paw-terminal-band-open/);
    expect(terminalCss).toMatch(/\.paw-terminal-xterm:focus-within::before\s*\{[^}]*border-top-color:/s);

    expect(terminalCss).toMatch(/@media \(prefers-reduced-motion: reduce\)[\s\S]*?\.paw-terminal-tab\[data-selected\]::before\s*\{\s*transform:\s*none;/);
    expect(terminalCss).toMatch(/:root\[data-reduce-motion='true'\] \.paw-terminal-tab\[data-selected\]::before\s*\{\s*transform:\s*none;/);
    // The focus edge is colour and shadow only, so it survives reduced motion
    // without ever moving anything.
    expect(terminalCss).toMatch(/\.paw-terminal-xterm::before\s*\{[^}]*transition:\s*border-color[^}]*box-shadow[^}]*\}/s);
    expect(terminalCss).not.toMatch(/\.paw-terminal-xterm::before\s*\{[^}]*transition:[^};]*transform/s);
  });

  it('keeps the tab strip locally scrollable while the new-terminal action stays outside it', () => {
    expect(terminalCss).toMatch(/\.paw-terminal-tabs\s*\{[^}]*overflow-x:\s*auto;[^}]*\}/s);
    expect(terminalCss).toMatch(/\.paw-terminal-tab-new\s*\{[^}]*flex:\s*0 0 auto;/s);
  });

  it('fades only the strip edges that truly hide tabs', () => {
    expect(terminalCss).toMatch(/\.paw-terminal-tabs\[data-overflow\]\s*\{[^}]*mask-image:/s);
    expect(terminalCss).toMatch(/\.paw-terminal-tabs\[data-overflow\]\[data-at-start\]\s*\{[^}]*mask-image:/s);
    expect(terminalCss).toMatch(/\.paw-terminal-tabs\[data-overflow\]\[data-at-end\]\s*\{[^}]*mask-image:/s);
  });

  it('floats the error notice over the console instead of granting it a grid row', () => {
    expect(terminalCss).toMatch(/\.paw-terminal-error\s*\{[^}]*position:\s*absolute;/s);
    expect(terminalCss).not.toMatch(/\[data-error\][^{]*\{[^}]*grid-template-rows/s);
    expect(terminalCss).toMatch(/\.paw-terminal-app__workspace\s*\{[^}]*position:\s*relative;/s);
  });

  it('silences terminal motion for both the OS media query and the PAWOS preference', () => {
    expect(terminalCss).toMatch(/@media \(prefers-reduced-motion: reduce\)[\s\S]*?\.paw-terminal-ended,[\s\S]*?animation:\s*none;/s);
    expect(terminalCss).toMatch(/:root\[data-reduce-motion='true'\] \.paw-terminal-tab-main i\[data-state="running"\]::after\s*\{\s*animation:\s*none;/s);
  });

  it('keeps the ghost-prompt caret and the cwd copy affordance in the feature owner, with motion silenced', () => {
    expect(terminalCss).toMatch(/\.paw-terminal-empty-glyph > i\s*\{[^}]*animation:\s*paw-terminal-caret/s);
    expect(terminalCss).toMatch(/@media \(prefers-reduced-motion: reduce\)[\s\S]*?\.paw-terminal-empty-glyph > i,[\s\S]*?animation:\s*none;/s);
    expect(terminalCss).toMatch(/:root\[data-reduce-motion='true'\] \.paw-terminal-empty-glyph > i,[\s\S]*?animation:\s*none;/s);
    expect(terminalCss).toMatch(/\.paw-terminal-cwd\s*\{[^}]*cursor:\s*copy;/s);
  });
});

function terminalSession(terminalId: string, title: string) {
  return {
    terminalId,
    title,
    cwd: '/workspace/paw',
    shell: '/bin/zsh',
    pid: 4242,
    cols: 104,
    rows: 30,
    status: 'running' as const,
    exitCode: null,
    baseCursor: 0,
    nextCursor: 0,
    createdAtMs: 1,
  };
}

function terminalFrame(child: React.ReactNode, windowId = 'terminal', active = true) {
  return <PawWindowFrame active={active} appId="terminal" bounds={{ x: 0, y: 0, width: 900, height: 640 }} onBoundsCommit={() => undefined} onClose={() => undefined} onFocus={() => undefined} onMinimize={() => undefined} onToggleMaximize={() => undefined} title={windowId === 'terminal' ? 'Terminal' : windowId} windowChrome="terminal-tabs" windowId={windowId} zIndex={10}>{child}</PawWindowFrame>;
}

function renderApp(transport: MockControlTransport, child: React.ReactNode): void {
  renderTerminal(transport, child);
}

function renderTerminal(transport: MockControlTransport, child: React.ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return { ...render(
    <QueryClientProvider client={queryClient}>
      <ControlTransportProvider transport={transport}>{child}</ControlTransportProvider>
    </QueryClientProvider>,
  ), queryClient };
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}
