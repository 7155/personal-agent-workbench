import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport } from '@/test/mock-transport';
import { PawOsAppSurfaceProvider } from '@/features/paw-os/surface-context';
import { PawBrowserApp } from './PawBrowserApp';
import type { PawBrowserDownload } from './paw-browser-host';
import { PawWindowFrame } from '../shell/PawWindowLayer';

beforeEach(() => {
  localStorage.clear();
  delete window.pawBrowserHost;
});

afterEach(() => {
  cleanup();
  delete window.pawBrowserHost;
});

describe('PAW Browser App', () => {
  it('does not poll Browser tabs while its owning window is inactive', async () => {
    const transport = browserTransport();
    render(
      <ControlTransportProvider transport={transport}>
        <PawOsAppSurfaceProvider active={false} appId="browser" height={720} width={1_080} windowId="browser">
          <PawBrowserApp />
        </PawOsAppSurfaceProvider>
      </ControlTransportProvider>,
    );

    await act(async () => { await Promise.resolve(); });
    expect(transport.requests.some(({ request }) => request.pathId === 'browser.tabs')).toBe(false);
    expect(transport.requests.some(({ request }) => request.pathId === 'browser.traces')).toBe(false);
  });

  it('aborts Browser polling while the document is hidden and refreshes once when visible again', async () => {
    let visibility: DocumentVisibilityState = 'visible';
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => visibility });
    try {
      const transport = browserTransport();
      render(
        <ControlTransportProvider transport={transport}>
          <PawBrowserApp />
        </ControlTransportProvider>,
      );

      await waitFor(() => expect(
        transport.requests.filter(({ request }) => request.pathId === 'browser.tabs').at(-1)?.request.signal?.aborted,
      ).toBe(false));
      const firstTabsRequest = transport.requests.filter(
        ({ request }) => request.pathId === 'browser.tabs',
      ).at(-1)?.request;

      visibility = 'hidden';
      fireEvent(document, new Event('visibilitychange'));
      expect(firstTabsRequest?.signal?.aborted).toBe(true);
      const hiddenCount = transport.requests.filter(({ request }) => request.pathId === 'browser.tabs').length;

      visibility = 'visible';
      fireEvent(document, new Event('visibilitychange'));
      await waitFor(() => expect(
        transport.requests.filter(({ request }) => request.pathId === 'browser.tabs').length,
      ).toBe(hiddenCount + 1));
    } finally {
      delete (document as unknown as Record<string, unknown>).visibilityState;
    }
  });

  it('starts and controls the isolated browser directly without pairing or permission UI', async () => {
    const user = userEvent.setup();
    const transport = browserTransport();
    render(
      <ControlTransportProvider transport={transport}>
        <PawBrowserApp />
      </ControlTransportProvider>,
    );

    expect(await screen.findByRole('textbox', { name: '页面地址' })).toBeInTheDocument();
    expect(document.querySelector('section[data-route-id="browser"]')).toBeInTheDocument();
    expect(screen.queryByText('CDP 直连')).not.toBeInTheDocument();
    expect(screen.queryByText('Agent 拥有完整控制权')).not.toBeInTheDocument();
    expect(screen.queryByText(/配对/)).not.toBeInTheDocument();
    expect(screen.queryByText(/站点权限/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '显示 Agent 浏览器轨迹' })).toBeInTheDocument();
    expect(screen.queryByRole('complementary', { name: 'Agent 浏览器轨迹' })).not.toBeInTheDocument();
    const address = screen.getByRole('textbox', { name: '页面地址' });
    await waitFor(() => expect(address).toHaveValue('https://example.com'));
    await user.clear(address);
    await user.type(address, 'https://example.com/next');
    await user.keyboard('{Enter}');

    await waitFor(() => expect(transport.requests.some(({ request }) => (
      request.pathId === 'browser.command'
      && record(request.body).action === 'navigate'
      && record(request.body).url === 'https://example.com/next'
    ))).toBe(true));
    expect(transport.requests.some(({ request }) => request.pathId.includes('pairing'))).toBe(false);
    expect(transport.requests.some(({ request }) => request.pathId.includes('permission'))).toBe(false);
  });

  it('keeps tab creation and the observable Agent trajectory in one Browser window', async () => {
    const user = userEvent.setup();
    const transport = browserTransport();
    render(
      <ControlTransportProvider transport={transport}>
        <PawBrowserApp />
      </ControlTransportProvider>,
    );

    await user.click(await screen.findByRole('button', { name: '新建标签页' }));
    await waitFor(() => expect(transport.requests.some(({ request }) => (
      request.pathId === 'browser.command' && record(request.body).action === 'new_tab'
    ))).toBe(true));
    expect(screen.getByRole('button', { name: '显示 Agent 浏览器轨迹' })).toBeInTheDocument();
    expect(screen.queryByRole('complementary', { name: 'Agent 浏览器轨迹' })).not.toBeInTheDocument();
    expect(screen.queryByText(/completed/)).not.toBeInTheDocument();
  });

  it('keeps the page dominant and projects a real Agent trace as an Ego-style execution field', async () => {
    const user = userEvent.setup();
    const transport = browserTransport({
      traces: {
        ok: true,
        items: [{
          commandId: 'cmd-active',
          action: 'run',
          sourceKind: 'agent',
          status: 'claimed',
          target: '完成账户设置',
          createdAtMs: Date.now(),
          steps: [{ event: 'started', action: 'click', target: '继续按钮', atMs: Date.now() }],
        }],
      },
    });
    render(
      <ControlTransportProvider transport={transport}>
        <PawBrowserApp />
      </ControlTransportProvider>,
    );

    await waitFor(() => expect(document.querySelector('.paw-browser-viewport')).toHaveAttribute('data-agent-state', 'active'));
    const task = screen.getByRole('region', { name: 'Agent 浏览器任务' });
    expect(within(task).getByRole('status', { name: 'Agent 浏览器任务状态' })).toHaveTextContent('Agent 正在浏览');
    expect(within(task).getByText('点击')).toBeInTheDocument();
    expect(within(task).getByText('继续按钮')).toBeInTheDocument();
    expect(await screen.findByRole('complementary', { name: 'Agent 浏览器轨迹' })).toBeInTheDocument();

    await user.click(within(task).getByRole('button', { name: '接管浏览器' }));
    await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId === 'browser.stop')).toBe(true));

    await user.click(screen.getByRole('button', { name: '显示 Agent 浏览器轨迹' }));
    expect(await screen.findByRole('complementary', { name: 'Agent 浏览器轨迹' })).toBeInTheDocument();
    expect(screen.getByText('总步数未提供')).toBeInTheDocument();
    expect(screen.queryByRole('progressbar', { name: '浏览器执行进度' })).not.toBeInTheDocument();
  });

  it('keeps Browser execution steps in an explicit loaded window and shows known running progress', async () => {
    const user = userEvent.setup();
    const transport = browserTransport({
      traces: {
        ok: true,
        items: [{
          commandId: 'cmd-progress',
          action: 'run',
          sourceKind: 'agent',
          status: 'claimed',
          target: '整理页面结构',
          totalSteps: 8,
          currentStep: 5,
          createdAtMs: Date.now(),
          steps: Array.from({ length: 6 }, (_, index) => ({
            event: index === 5 ? 'started' : 'completed',
            action: 'click',
            target: `步骤 ${index + 1}`,
            atMs: Date.now() + index,
          })),
        }],
      },
    });
    render(
      <ControlTransportProvider transport={transport}>
        <PawBrowserApp />
      </ControlTransportProvider>,
    );

    await waitFor(() => expect(screen.getByRole('complementary', { name: 'Agent 浏览器轨迹' })).toBeInTheDocument());
    expect(await screen.findByText('最近 4 / 已加载 6 个步骤')).toBeInTheDocument();
    expect(screen.getByText('已记录 5 / 共 8 步')).toBeInTheDocument();
    expect(screen.getByRole('progressbar', { name: '浏览器执行进度' })).toHaveAttribute('value', '5');
    expect(screen.queryByText('步骤 1')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '显示全部步骤：6 项' }));
    expect(screen.getByText('已显示 6 / 已加载 6 个步骤')).toBeInTheDocument();
    expect(screen.getByText('步骤 1')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '收起步骤到最近 4 项' })).toBeInTheDocument();
  });

  it('keeps host-only toolbar commands absent instead of disabled without the desktop host', async () => {
    render(
      <ControlTransportProvider transport={browserTransport()}>
        <PawBrowserApp />
      </ControlTransportProvider>,
    );

    await screen.findByRole('textbox', { name: '页面地址' });
    // No PAWOS desktop host: History, Settings, and the page-tools menu would
    // be theatre, so they are absent rather than permanently disabled.
    expect(screen.queryByRole('button', { name: '浏览历史' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Browser 设置' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Browser 菜单' })).toBeNull();
    expect(screen.getByRole('note', { name: 'Browser 宿主能力说明' })).toHaveTextContent('完整浏览器请在 PAW 桌面应用中使用');
    expect(screen.getByRole('note', { name: 'Browser 宿主能力说明' })).toHaveTextContent('历史、书签和下载记录保存在桌面应用中');
    // Navigation and the Agent trace remain fully real in this mode.
    expect(screen.getByRole('button', { name: '后退' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '显示 Agent 浏览器轨迹' })).toBeInTheDocument();
  });

  it('closes exactly the clicked managed tab, never the selected one', async () => {
    const user = userEvent.setup();
    const transport = browserTransport({
      tabs: () => ({ ok: true, items: [
        { deviceId: 'paw-browser', tabId: 1, title: 'One', url: 'https://one.example', active: true },
        { deviceId: 'paw-browser', tabId: 2, title: 'Two', url: 'https://two.example', active: false },
      ] }),
      snapshot: () => browserSnapshot('https://one.example', 1, 'One'),
    });
    render(
      <ControlTransportProvider transport={transport}>
        <PawBrowserApp />
      </ControlTransportProvider>,
    );

    await screen.findByRole('tab', { name: 'Two' });
    await user.click(document.querySelector('[data-tab-close-label="Two"]') as HTMLElement);
    await waitFor(() => expect(transport.requests.some(({ request }) => (
      request.pathId === 'browser.command'
      && record(request.body).action === 'close_tab'
      && record(request.body).tabId === 2
    ))).toBe(true));
    expect(transport.requests.some(({ request }) => (
      record(request.body).action === 'close_tab' && record(request.body).tabId === 1
    ))).toBe(false);
  });

  it('shows the real transport failure beside the retry action', async () => {
    const user = userEvent.setup();
    const transport = browserTransport({
      command: (request) => record(request.body).action === 'navigate'
        ? { ok: false, summary: '浏览器网关超时' }
        : { ok: true, status: 'completed' },
    });
    render(
      <ControlTransportProvider transport={transport}>
        <PawBrowserApp />
      </ControlTransportProvider>,
    );

    const address = await screen.findByRole('textbox', { name: '页面地址' });
    await waitFor(() => expect(address).toHaveValue('https://example.com'));
    await user.clear(address);
    await user.type(address, 'https://blocked.example/{Enter}');

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('页面没有打开');
    expect(alert).toHaveTextContent('浏览器网关超时');
    expect(within(alert).getByRole('button', { name: '重试' })).toBeInTheDocument();
    expect(screen.queryByText('还没有这一页的画面')).not.toBeInTheDocument();
    const screenshots = transport.requests.filter(({ request }) => record(request.body).action === 'screenshot').length;
    await user.click(within(alert).getByRole('button', { name: '重试' }));
    await waitFor(() => expect(transport.requests.filter(({ request }) => record(request.body).action === 'screenshot').length).toBeGreaterThan(screenshots));
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
  });

  it('selects the exact Agent target without creating another Browser tab', async () => {
    const transport = browserTransport({
      tabs: () => ({ ok: true, items: [
        { deviceId: 'paw-browser', targetId: 'target-one', tabId: 1, title: 'One', url: 'https://one.example', active: true },
        { deviceId: 'paw-browser', targetId: 'target-two', tabId: 2, title: 'Two', url: 'https://two.example', active: false },
      ] }),
      snapshot: (request) => record(request.query).tabId === 2
        ? browserSnapshot('https://two.example', 2, 'Two')
        : browserSnapshot('https://one.example', 1, 'One'),
    });
    render(
      <ControlTransportProvider transport={transport}>
        <PawBrowserApp target={{
          kind: 'browser-target', id: 'target-two', targetId: 'target-two', tabId: 2,
          title: 'Browser', sessionId: 'session-1', toolCallId: 'call-browser-1',
        }} />
      </ControlTransportProvider>,
    );

    await waitFor(() => expect(screen.getByRole('textbox', { name: '页面地址' })).toHaveValue('https://two.example'));
    expect(transport.requests.some(({ request }) => (
      request.pathId === 'browser.command' && record(request.body).action === 'new_tab'
    ))).toBe(false);
  });

  it('keeps the real Browser tab lifecycle in the PAWOS window titlebar', async () => {
    const user = userEvent.setup();
    window.pawBrowserHost = electronBrowserHost();
    render(
      <ControlTransportProvider transport={browserTransport()}>
        <PawWindowFrame
          active
          appId="browser"
          bounds={{ x: 0, y: 0, width: 900, height: 640 }}
          onBoundsCommit={() => undefined}
          onClose={() => undefined}
          onFocus={() => undefined}
          onMinimize={() => undefined}
          onToggleMaximize={() => undefined}
          title="Browser"
          windowChrome="browser-tabs"
          windowId="browser"
          zIndex={10}
        >
          <PawBrowserApp />
        </PawWindowFrame>
      </ControlTransportProvider>,
    );

    const titlebar = screen.getByText('Browser').closest('.paw-window-titlebar') as HTMLElement;
    const tablist = await within(titlebar).findByRole('tablist', { name: 'PAW Browser 标签页' });
    expect(document.querySelector('webview')).toHaveAttribute('allowpopups', 'true');
    expect(document.querySelector('.paw-window-body .paw-browser-tabstrip')).toBeNull();
    // The tablist scrolls tabs only; new-tab sits beside it and stays reachable.
    expect(within(tablist).queryByRole('button', { name: '新建标签页' })).toBeNull();
    await user.click(within(titlebar).getByRole('button', { name: '新建标签页' }));
    expect(within(tablist).getAllByRole('tab')).toHaveLength(2);
    await user.click(within(tablist).getAllByRole('tab')[0]);
    expect(within(tablist).getAllByRole('tab')[0]).toHaveAttribute('aria-selected', 'true');
    await user.click(document.querySelector('.paw-browser-tab-close') as HTMLElement);
    expect(within(tablist).getAllByRole('tab')).toHaveLength(1);
  });

  it('opens a Shell-routed URL inside the mounted PAW Browser', async () => {
    window.pawBrowserHost = electronBrowserHost();
    render(
      <ControlTransportProvider transport={browserTransport()}>
        <PawBrowserApp target={{
          kind: 'browser-target',
          id: 'shell-url-1',
          title: 'Browser',
          sessionId: '',
          toolCallId: '',
          targetId: '',
          provisional: true,
          url: 'https://inside.example/path',
        }} />
      </ControlTransportProvider>,
    );

    await waitFor(() => expect(screen.getByRole('textbox', { name: '页面地址' }))
      .toHaveValue('https://inside.example/path'));
    expect([...document.querySelectorAll('webview')].some((guest) => (
      guest.getAttribute('src') === 'https://inside.example/path'
    ))).toBe(true);
  });

  it('keeps the selected host tab, omnibox, and activated guest synchronized', async () => {
    const user = userEvent.setup();
    const activate = vi.fn();
    window.pawBrowserHost = {
      ...electronBrowserHost(),
      activate,
    };
    render(
      <ControlTransportProvider transport={browserTransport()}>
        <PawBrowserApp target={{
          kind: 'browser-target', id: 'shell-url-2', title: 'Browser',
          sessionId: '', toolCallId: '', targetId: '', provisional: true,
          commandId: 'command-second',
          url: 'https://second.example/path',
        }} />
      </ControlTransportProvider>,
    );

    await screen.findByRole('textbox', { name: '页面地址' });
    await waitFor(() => expect(screen.getByRole('textbox', { name: '页面地址' })).toHaveValue('https://second.example/path'));

    const guests = document.querySelectorAll('webview');
    expect(guests).toHaveLength(2);
    mockGuestIdentity(guests[0], { title: '新标签页', url: 'about:blank', webContentsId: 101 });
    mockGuestIdentity(guests[1], { title: 'Second', url: 'https://second.example/path', webContentsId: 202 });
    fireEvent(guests[0], new Event('dom-ready'));
    fireEvent(guests[1], new Event('dom-ready'));
    await waitFor(() => expect(activate).toHaveBeenCalledWith({
      title: 'Second',
      url: 'https://second.example/path',
      webContentsId: 202,
    }));

    activate.mockClear();
    await user.click(screen.getAllByRole('tab')[0]);
    await waitFor(() => expect(screen.getByRole('textbox', { name: '页面地址' })).toHaveValue(''));
    await act(async () => { await new Promise((resolve) => window.setTimeout(resolve, 20)); });
    expect(screen.getAllByRole('tab')[0]).toHaveAttribute('aria-selected', 'true');
    expect(screen.getAllByRole('tab')[1]).toHaveAttribute('aria-selected', 'false');
    expect(screen.getByRole('textbox', { name: '页面地址' })).toHaveValue('');
    expect(activate).toHaveBeenCalledWith({ title: '新标签页', url: 'about:blank', webContentsId: 101 });
    expect(document.querySelectorAll('webview')).toHaveLength(2);
  });

  it('selects a fresh tab when the previously selected target disappears', async () => {
    const snapshot = browserSnapshot('https://example.net', 42, 'Example Net');
    let tabCalls = 0;
    const transport = browserTransport({
      tabs: () => {
        tabCalls += 1;
        return {
          ok: true,
          items: tabCalls === 1
            ? [{ deviceId: 'paw-browser', tabId: 41, title: 'Example', url: 'https://example.com', active: true }]
            : [{ deviceId: 'paw-browser', tabId: 42, title: 'Example Net', url: 'https://example.net', active: true }],
        };
      },
      snapshot: (request) => record(request.query).tabId === 42 ? snapshot : browserSnapshot('https://example.com', 41, 'Example'),
    });
    render(
      <ControlTransportProvider transport={transport}>
        <PawBrowserApp />
      </ControlTransportProvider>,
    );

    await waitFor(() => expect(screen.getByRole('textbox', { name: '页面地址' })).toHaveValue('https://example.net'), { timeout: 3_000 });
    expect(screen.getByText('Example Net')).toBeInTheDocument();
  });

  it('uses built-in searchable History and real persistent-session Settings', async () => {
    const user = userEvent.setup();
    const history = [
      { id: 'history-paw', title: 'PAW docs', url: 'https://example.com/paw', visitedAt: 2 },
      { id: 'history-other', title: 'Other', url: 'https://other.test', visitedAt: 1 },
    ];
    const clearBrowsingData = vi.fn(async (action: 'cache' | 'site-data') => ({ action, after: 0, before: action === 'cache' ? 4096 : 2, completedAt: 3 }));
    const clearHistory = vi.fn(async () => []);
    const getHistory = vi.fn(async () => history);
    const removeHistoryEntry = vi.fn(async (entryId: string) => history.filter((entry) => entry.id !== entryId));
    window.pawBrowserHost = {
      kind: 'electron-webview',
      partition: 'persist:paw-browser',
      activate: () => undefined,
      clearBrowsingData,
      clearHistory,
      getHistory,
      getSettings: async () => ({ cacheBytes: 4096, cookieCount: 2, downloadPath: '/Users/example/Downloads', extensionCount: 0, extensionsPath: '/Users/example/Extensions', partition: 'persist:paw-browser', permissionMode: 'site-request', startPage: 'about:blank' }),
      listExtensions: async () => [],
      loadUnpackedExtension: async () => null,
      openExtensionsFolder: async () => ({ opened: true, path: '/Users/example/Extensions' }),
      openDownloads: async () => ({ opened: true, path: '/Users/example/Downloads' }),
      register: () => undefined,
      removeExtension: async () => [],
      removeHistoryEntry,
      setStartPage: async (startPage) => ({ startPage }),
      takeScreenshot: async () => ({ path: '/Users/example/Downloads/page.png', saved: true }),
      onCommand: () => () => undefined,
      onGuestClosed: () => () => undefined,
      onHistoryChanged: () => () => undefined,
      onOpenUrl: () => () => undefined,
      onSelectTab: () => () => undefined,
    };
    const confirmSpy = vi.spyOn(window, 'confirm');
    const transport = browserTransport();
    render(<ControlTransportProvider transport={transport}><PawBrowserApp /></ControlTransportProvider>);
    await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId === 'browser.tabs')).toBe(true));
    expect(transport.requests.some(({ request }) => request.pathId === 'browser.managed.start')).toBe(false);
    await user.click(await screen.findByRole('button', { name: '浏览历史' }));
    await waitFor(() => expect(getHistory).toHaveBeenCalled());
    expect(screen.getByRole('region', { name: '浏览历史' }).querySelector('.paw-browser-surface-card')).not.toBeNull();
    await user.type(screen.getByRole('searchbox', { name: '搜索浏览历史' }), 'paw');
    expect(screen.getByText('PAW docs')).toBeInTheDocument();
    expect(screen.queryByText('Other')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '删除 PAW docs' }));
    await waitFor(() => expect(removeHistoryEntry).toHaveBeenCalledWith('history-paw'));
    expect(screen.queryByText('PAW docs')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Browser 设置' }));
    expect(await screen.findByText('/Users/example/Downloads')).toBeInTheDocument();
    // Destructive clears confirm in place instead of a blocking dialog.
    await user.click(screen.getByRole('button', { name: '清除缓存' }));
    expect(clearBrowsingData).not.toHaveBeenCalled();
    await user.click(within(screen.getByRole('group', { name: '确认清除缓存' })).getByRole('button', { name: '确认清除' }));
    await waitFor(() => expect(clearBrowsingData).toHaveBeenCalledWith('cache'));
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(await screen.findByText(/缓存已清除/)).toBeInTheDocument();
    expect(screen.queryByText('Ego 轨迹')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '显示 Agent 浏览器轨迹' })).toBeInTheDocument();
    expect(screen.queryByRole('complementary', { name: 'Agent 浏览器轨迹' })).not.toBeInTheDocument();
    expect(localStorage.getItem('paw.browser.history.v1')).toBeNull();
  });

  it('saves a normalized start page through the host and opens it in the blank guest', async () => {
    const user = userEvent.setup();
    let startPage = 'about:blank';
    const setStartPage = vi.fn(async (value: string) => { startPage = value; return { startPage }; });
    window.pawBrowserHost = {
      ...electronBrowserHost(),
      getSettings: async () => ({ cacheBytes: 0, cookieCount: 0, downloadPath: '/tmp', extensionCount: 0, extensionsPath: '/tmp/Extensions', partition: 'persist:paw-browser', permissionMode: 'site-request', startPage }),
      setStartPage,
    };
    render(<ControlTransportProvider transport={browserTransport()}><PawBrowserApp /></ControlTransportProvider>);
    const guest = document.querySelector('webview') as Element & Record<string, unknown>;
    const loadURL = vi.fn(async () => undefined);
    Object.assign(guest, { loadURL });

    await user.click(await screen.findByRole('button', { name: 'Browser 设置' }));
    const field = await screen.findByRole('textbox', { name: 'Browser 启动页' });
    await user.clear(field);
    await user.type(field, 'start.example/home');
    await user.click(screen.getByRole('button', { name: '保存' }));

    // A bare host is committed as the same absolute URL the omnibox would open.
    await waitFor(() => expect(setStartPage).toHaveBeenCalledWith('https://start.example/home'));
    expect(await screen.findByText('启动页已保存')).toBeInTheDocument();
    expect(field).toHaveValue('https://start.example/home');
    await waitFor(() => expect(loadURL).toHaveBeenCalledWith('https://start.example/home'));
    expect(screen.getByRole('button', { name: '打开启动页' }))
      .toHaveAttribute('title', '打开启动页 https://start.example/home');
  });

  it('manages real Electron session extensions from Browser settings', async () => {
    const user = userEvent.setup();
    const installed = [
      { id: 'ext-one', name: '广告拦截', path: '/Users/example/Extensions/one', version: '1.4.0' },
      { id: 'ext-two', name: '取色器', path: '/Users/example/Extensions/two', version: '0.9.2' },
    ];
    const listExtensions = vi.fn(async () => installed);
    const loadUnpackedExtension = vi.fn(async () => installed[0]);
    const openExtensionsFolder = vi.fn(async () => ({ opened: true, path: '/Users/example/Extensions' }));
    const removeExtension = vi.fn(async (extensionId: string) => installed.filter((item) => item.id !== extensionId));
    window.pawBrowserHost = {
      ...electronBrowserHost(),
      getSettings: async () => ({ cacheBytes: 0, cookieCount: 0, downloadPath: '/tmp', extensionCount: installed.length, extensionsPath: '/Users/example/Extensions', partition: 'persist:paw-browser', permissionMode: 'site-request', startPage: 'about:blank' }),
      listExtensions,
      loadUnpackedExtension,
      openExtensionsFolder,
      removeExtension,
    };
    render(<ControlTransportProvider transport={browserTransport()}><PawBrowserApp /></ControlTransportProvider>);

    await user.click(await screen.findByRole('button', { name: 'Browser 设置' }));
    const settings = await screen.findByRole('region', { name: 'Browser 设置' });
    expect(within(settings).getByText('已安装 2 个扩展程序，与 Chrome 一样可在隔离 Browser Session 中加载。')).toBeInTheDocument();
    expect(within(settings).getByText('/Users/example/Extensions')).toBeInTheDocument();
    expect(within(settings).getByText('广告拦截')).toBeInTheDocument();
    expect(within(settings).getByText('1.4.0')).toBeInTheDocument();
    expect(within(settings).queryByText('还没有安装扩展程序')).toBeNull();

    await user.click(within(settings).getByRole('button', { name: '移除扩展程序 广告拦截' }));
    await waitFor(() => expect(removeExtension).toHaveBeenCalledWith('ext-one'));
    expect(await within(settings).findByText('扩展程序已移除')).toBeInTheDocument();

    await user.click(within(settings).getByRole('button', { name: '加载已解压的扩展程序' }));
    await waitFor(() => expect(loadUnpackedExtension).toHaveBeenCalledTimes(1));
    expect(await within(settings).findByText('已加载扩展程序 广告拦截')).toBeInTheDocument();

    await user.click(within(settings).getByRole('button', { name: '打开扩展程序目录' }));
    await waitFor(() => expect(openExtensionsFolder).toHaveBeenCalledTimes(1));
    expect(await within(settings).findByText('已打开扩展程序目录 /Users/example/Extensions')).toBeInTheDocument();
  });

  it('reports a cancelled extension pick and a failed load instead of claiming success', async () => {
    const user = userEvent.setup();
    window.pawBrowserHost = {
      ...electronBrowserHost(),
      loadUnpackedExtension: vi.fn()
        .mockResolvedValueOnce(null)
        .mockRejectedValueOnce(new Error('manifest.json 缺失')),
    };
    render(<ControlTransportProvider transport={browserTransport()}><PawBrowserApp /></ControlTransportProvider>);

    await user.click(await screen.findByRole('button', { name: 'Browser 设置' }));
    const settings = await screen.findByRole('region', { name: 'Browser 设置' });
    expect(within(settings).getByText('还没有安装扩展程序')).toBeInTheDocument();

    // A cancelled directory pick loaded nothing, so no receipt is invented.
    await user.click(within(settings).getByRole('button', { name: '加载已解压的扩展程序' }));
    await waitFor(() => expect(within(settings).queryByRole('status')).toBeNull());

    await user.click(within(settings).getByRole('button', { name: '加载已解压的扩展程序' }));
    expect(await within(settings).findByText('加载扩展程序失败：manifest.json 缺失')).toBeInTheDocument();
  });

  it('opens a clean new-tab page and lights it only while an Agent is executing', async () => {
    window.pawBrowserHost = electronBrowserHost();
    const { unmount } = render(
      <ControlTransportProvider transport={browserTransport()}><PawBrowserApp /></ControlTransportProvider>,
    );

    await screen.findByRole('textbox', { name: '页面地址' });
    const start = document.querySelector('.paw-browser-start') as HTMLElement;
    expect(start).not.toBeNull();
    expect(start).toHaveTextContent('新标签页');
    expect(start).toHaveTextContent('在地址栏输入网址或搜索内容');
    expect(start.querySelector('.paw-browser-start-logo')).not.toBeNull();
    expect(start).not.toHaveAttribute('data-live');
    expect(document.querySelector('.paw-browser-agent-field')).toBeNull();
    unmount();

    render(
      <ControlTransportProvider transport={browserTransport({
        traces: {
          ok: true,
          items: [{
            commandId: 'cmd-ntp', action: 'run', sourceKind: 'agent', status: 'claimed',
            target: '读取新闻', createdAtMs: Date.now(),
          }],
        },
      })}>
        <PawBrowserApp />
      </ControlTransportProvider>,
    );
    await waitFor(() => expect(document.querySelector('.paw-browser-start')).toHaveAttribute('data-live', 'true'));
    expect(document.querySelector('.paw-browser-agent-field')).not.toBeNull();
  });

  it('runs print, screenshot, and downloads against the real guest and host', async () => {
    const user = userEvent.setup();
    const openDownloads = vi.fn(async () => ({ opened: true, path: '/Users/example/Downloads' }));
    const takeScreenshot = vi.fn(async () => ({ path: '/Users/example/Downloads/page.png', saved: true }));
    window.pawBrowserHost = { ...electronBrowserHost(), openDownloads, takeScreenshot };
    render(<ControlTransportProvider transport={browserTransport()}><PawBrowserApp /></ControlTransportProvider>);

    const guest = document.querySelector('webview') as Element & Record<string, unknown>;
    const print = vi.fn();
    Object.assign(guest, { print });

    await user.click(await screen.findByRole('button', { name: 'Browser 菜单' }));
    await user.click(screen.getByRole('menuitem', { name: '打印' }));
    expect(print).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('menu')).toBeNull();

    // The guest has not reported its webContents id yet, so screenshot says so
    // rather than silently doing nothing.
    await user.click(screen.getByRole('button', { name: 'Browser 菜单' }));
    await user.click(screen.getByRole('menuitem', { name: '截图' }));
    expect(takeScreenshot).not.toHaveBeenCalled();
    expect(await screen.findByText('页面还没有就绪，无法截图')).toBeInTheDocument();

    mockGuestIdentity(guest, { title: '新标签页', url: 'about:blank', webContentsId: 512 });
    fireEvent(guest, new Event('dom-ready'));
    await user.click(screen.getByRole('button', { name: 'Browser 菜单' }));
    await user.click(screen.getByRole('menuitem', { name: '截图' }));
    await waitFor(() => expect(takeScreenshot).toHaveBeenCalledWith(512));
    expect(await screen.findByText('截图已保存到 /Users/example/Downloads/page.png')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Browser 菜单' }));
    await user.click(screen.getByRole('menuitem', { name: '下载' }));
    await waitFor(() => expect(openDownloads).toHaveBeenCalledTimes(1));
    expect(await screen.findByText('已打开下载目录 /Users/example/Downloads')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '关闭操作提示' }));
    expect(screen.queryByText('已打开下载目录 /Users/example/Downloads')).toBeNull();
  });

  it('keeps History and Settings menu fallbacks narrow-only instead of duplicating wide toolbar commands', async () => {
    const user = userEvent.setup();
    window.pawBrowserHost = electronBrowserHost();
    render(<ControlTransportProvider transport={browserTransport()}><PawBrowserApp /></ControlTransportProvider>);

    await user.click(await screen.findByRole('button', { name: 'Browser 菜单' }));
    const menu = screen.getByRole('menu');
    expect(within(menu).getByRole('menuitem', { name: '浏览历史' })).toHaveClass('paw-browser-menu-narrow-only');
    expect(within(menu).getByRole('menuitem', { name: '浏览器设置' })).toHaveClass('paw-browser-menu-narrow-only');
    // The settings surface has exactly one wide entry point; the old
    // "清除浏览数据" item opened the same surface and is gone.
    expect(within(menu).queryByRole('menuitem', { name: '清除浏览数据' })).toBeNull();
    expect(screen.getByRole('button', { name: '浏览历史' })).toHaveClass('paw-browser-history-toggle');
    expect(screen.getByRole('button', { name: 'Browser 设置' })).toHaveClass('paw-browser-settings-toggle');
  });

  it('uses the persistent host for current-page bookmarks and reopens a stored bookmark in the same guest', async () => {
    const user = userEvent.setup();
    const addBookmark = vi.fn(async () => [{ id: 'bookmark-current', title: 'Current page', url: 'https://current.example/', createdAt: 1, updatedAt: 1 }]);
    const getBookmarks = vi.fn(async () => []);
    const loadURL = vi.fn(async () => undefined);
    window.pawBrowserHost = {
      ...electronBrowserHost(),
      addBookmark,
      getBookmarks,
      removeBookmark: async () => [],
      getHistory: async () => [],
    };
    render(<ControlTransportProvider transport={browserTransport()}><PawBrowserApp /></ControlTransportProvider>);

    const guest = document.querySelector('webview') as Element & Record<string, unknown>;
    Object.assign(guest, { loadURL });
    mockGuestIdentity(guest, { title: 'Current page', url: 'https://current.example/', webContentsId: 314 });
    fireEvent(guest, new Event('dom-ready'));

    await user.click(await screen.findByRole('button', { name: '书签' }));
    const library = await screen.findByRole('region', { name: '浏览书签' });
    await user.click(within(library).getByRole('button', { name: '收藏当前页面' }));
    await waitFor(() => expect(addBookmark).toHaveBeenCalledWith({ title: 'Current page', url: 'https://current.example/' }));
    expect(within(library).getAllByText('Current page').length).toBeGreaterThanOrEqual(2);
  });

  it('renders host download jobs and sends file actions to the matching host job ID', async () => {
    const user = userEvent.setup();
    const download = { id: 'download-report', filename: 'report.pdf', url: 'https://example.com/report.pdf', path: '/tmp/report.pdf', state: 'completed' as const, receivedBytes: 10, totalBytes: 10, startedAt: 1, updatedAt: 2, completedAt: 2 };
    const openDownload = vi.fn(async () => ({ ...download, opened: true }));
    const revealDownload = vi.fn(async () => ({ ...download, revealed: true }));
    const cancelDownload = vi.fn(async () => ({ ...download, state: 'cancelled' as const }));
    window.pawBrowserHost = {
      ...electronBrowserHost(),
      getDownloads: async () => [download],
      openDownload,
      revealDownload,
      cancelDownload,
    };
    render(<ControlTransportProvider transport={browserTransport()}><PawBrowserApp /></ControlTransportProvider>);

    await user.click(await screen.findByRole('button', { name: '下载记录' }));
    const library = await screen.findByRole('region', { name: '下载记录' });
    expect(within(library).getByText('report.pdf')).toBeInTheDocument();
    await user.click(within(library).getByRole('button', { name: '打开下载 report.pdf' }));
    await user.click(within(library).getByRole('button', { name: '显示下载 report.pdf' }));
    await waitFor(() => expect(openDownload).toHaveBeenCalledWith('download-report'));
    expect(revealDownload).toHaveBeenCalledWith('download-report');
    expect(cancelDownload).not.toHaveBeenCalled();
  });

  it('keeps live download updates when an older library read finishes later', async () => {
    const user = userEvent.setup();
    const download: PawBrowserDownload = { id: 'download-race', filename: 'race.pdf', url: 'https://example.com/race.pdf', path: '/tmp/race.pdf', state: 'progressing', receivedBytes: 1, totalBytes: 10, startedAt: 1, updatedAt: 1 };
    let resolveRead!: (downloads: PawBrowserDownload[]) => void;
    const pendingRead = new Promise<PawBrowserDownload[]>((resolve) => { resolveRead = resolve; });
    let notifyDownloads!: (downloads: PawBrowserDownload[]) => void;
    window.pawBrowserHost = {
      ...electronBrowserHost(),
      getDownloads: () => pendingRead,
      openDownload: async () => ({ ...download, opened: true }),
      revealDownload: async () => ({ ...download, revealed: true }),
      cancelDownload: async () => download,
      onDownloadsChanged: (listener) => { notifyDownloads = listener; return () => undefined; },
    };
    render(<ControlTransportProvider transport={browserTransport()}><PawBrowserApp /></ControlTransportProvider>);
    await user.click(await screen.findByRole('button', { name: '下载记录' }));
    act(() => notifyDownloads([{ ...download, state: 'completed', receivedBytes: 10, updatedAt: 2 }]));
    const library = screen.getByRole('region', { name: '下载记录' });
    expect(within(library).getByText('已完成')).toBeInTheDocument();
    await act(async () => resolveRead([download]));
    expect(within(library).getByText('已完成')).toBeInTheDocument();
    expect(within(library).queryByText('下载中')).toBeNull();
  });

  it('keeps a terminal host event when an earlier cancellation receipt arrives late', async () => {
    const user = userEvent.setup();
    const download: PawBrowserDownload = { id: 'download-race', filename: 'race.pdf', url: 'https://example.com/race.pdf', path: '/tmp/race.pdf', state: 'progressing', receivedBytes: 1, totalBytes: 10, startedAt: 1, updatedAt: 1 };
    let resolveCancel!: (value: PawBrowserDownload) => void;
    const pendingCancel = new Promise<PawBrowserDownload>((resolve) => { resolveCancel = resolve; });
    let notifyDownloads!: (downloads: PawBrowserDownload[]) => void;
    window.pawBrowserHost = {
      ...electronBrowserHost(),
      getDownloads: async () => [download],
      openDownload: async () => ({ ...download, opened: true }),
      revealDownload: async () => ({ ...download, revealed: true }),
      cancelDownload: () => pendingCancel,
      onDownloadsChanged: (listener) => { notifyDownloads = listener; return () => undefined; },
    };
    render(<ControlTransportProvider transport={browserTransport()}><PawBrowserApp /></ControlTransportProvider>);
    await user.click(await screen.findByRole('button', { name: '下载记录' }));
    await user.click(await screen.findByRole('button', { name: '取消下载 race.pdf' }));
    act(() => notifyDownloads([{ ...download, state: 'cancelled', updatedAt: 2 }]));
    await act(async () => resolveCancel(download));
    const library = screen.getByRole('region', { name: '下载记录' });
    expect(within(library).getByText('已取消')).toBeInTheDocument();
    expect(within(library).queryByText('下载中')).toBeNull();
  });

  it('does not claim cancellation when the host reports the download already completed', async () => {
    const user = userEvent.setup();
    const download: PawBrowserDownload = { id: 'download-race', filename: 'race.pdf', url: 'https://example.com/race.pdf', path: '/tmp/race.pdf', state: 'progressing', receivedBytes: 1, totalBytes: 10, startedAt: 1, updatedAt: 1 };
    const completed = { ...download, state: 'completed' as const, receivedBytes: 10, updatedAt: 2 };
    let current = download;
    window.pawBrowserHost = {
      ...electronBrowserHost(),
      getDownloads: async () => [current],
      openDownload: async () => ({ ...current, opened: true }),
      revealDownload: async () => ({ ...current, revealed: true }),
      cancelDownload: async () => { current = completed; return completed; },
    };
    render(<ControlTransportProvider transport={browserTransport()}><PawBrowserApp /></ControlTransportProvider>);
    await user.click(await screen.findByRole('button', { name: '下载记录' }));
    await user.click(await screen.findByRole('button', { name: '取消下载 race.pdf' }));
    expect(await screen.findByText('下载已完成，未取消')).toBeInTheDocument();
    expect(screen.queryByText('下载已取消')).toBeNull();
  });

  it('reports the real find-in-page match position from the guest and clears on close', async () => {
    const user = userEvent.setup();
    window.pawBrowserHost = electronBrowserHost();
    render(<ControlTransportProvider transport={browserTransport()}><PawBrowserApp /></ControlTransportProvider>);

    const guest = document.querySelector('webview') as Element & Record<string, unknown>;
    const findInPage = vi.fn(() => 1);
    const stopFindInPage = vi.fn();
    Object.assign(guest, { findInPage, stopFindInPage });

    await user.click(await screen.findByRole('button', { name: 'Browser 菜单' }));
    await user.click(screen.getByRole('menuitem', { name: '页内查找' }));
    const findInput = await screen.findByRole('textbox', { name: '页内查找' });
    expect(findInput).toHaveFocus();

    await user.type(findInput, 'paw');
    expect(findInPage).toHaveBeenLastCalledWith('paw', { findNext: false, forward: true });
    // No count is invented before the guest reports one.
    expect(screen.queryByText(/^\d+\/\d+$/)).toBeNull();

    fireEvent(guest, Object.assign(new Event('found-in-page'), {
      result: { activeMatchOrdinal: 2, matches: 8, finalUpdate: true },
    }));
    expect(await screen.findByText('2/8')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '下一个匹配项' }));
    expect(findInPage).toHaveBeenLastCalledWith('paw', { findNext: true, forward: true });
    await user.click(screen.getByRole('button', { name: '上一个匹配项' }));
    expect(findInPage).toHaveBeenLastCalledWith('paw', { findNext: true, forward: false });

    fireEvent(guest, Object.assign(new Event('found-in-page'), {
      result: { activeMatchOrdinal: 0, matches: 0, finalUpdate: true },
    }));
    expect(await screen.findByText('无匹配')).toBeInTheDocument();

    await user.type(findInput, ' workspace');
    expect(findInPage).toHaveBeenLastCalledWith('paw workspace', { findNext: false, forward: true });
    expect(screen.queryByText('无匹配')).not.toBeInTheDocument();
    expect(screen.queryByText('2/8')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '关闭页内查找' }));
    expect(stopFindInPage).toHaveBeenCalledWith('clearSelection');
    expect(screen.queryByRole('textbox', { name: '页内查找' })).toBeNull();
  });

  it('reads the real guest zoom and resets it through the percent readout', async () => {
    const user = userEvent.setup();
    window.pawBrowserHost = electronBrowserHost();
    render(<ControlTransportProvider transport={browserTransport()}><PawBrowserApp /></ControlTransportProvider>);

    const guest = document.querySelector('webview') as Element & Record<string, unknown>;
    let zoomFactor = 1.2;
    const setZoomFactor = vi.fn((value: number) => { zoomFactor = value; });
    Object.assign(guest, { getZoomFactor: () => zoomFactor, setZoomFactor });

    await user.click(await screen.findByRole('button', { name: 'Browser 菜单' }));
    const reset = screen.getByRole('button', { name: '恢复默认缩放' });
    expect(reset).toHaveTextContent('120%');
    expect(reset).toBeEnabled();

    await user.click(reset);
    expect(setZoomFactor).toHaveBeenCalledWith(1);
    expect(reset).toHaveTextContent('100%');
    expect(reset).toBeDisabled();

    await user.click(screen.getByRole('button', { name: '放大网页' }));
    expect(setZoomFactor).toHaveBeenLastCalledWith(1.1);
    expect(reset).toHaveTextContent('110%');
  });

  it('navigates Home to the configured start page instead of a hardcoded blank page', async () => {
    const user = userEvent.setup();
    window.pawBrowserHost = {
      ...electronBrowserHost(),
      getSettings: async () => ({ cacheBytes: 0, cookieCount: 0, downloadPath: '/tmp', extensionCount: 0, extensionsPath: '/tmp/Extensions', partition: 'persist:paw-browser', permissionMode: 'site-request', startPage: 'https://start.example/' }),
    };
    render(<ControlTransportProvider transport={browserTransport()}><PawBrowserApp /></ControlTransportProvider>);
    const guest = document.querySelector('webview') as Element & Record<string, unknown>;
    const loadURL = vi.fn(async () => undefined);
    Object.assign(guest, { loadURL });

    const home = await screen.findByRole('button', { name: '打开启动页' });
    expect(home).toHaveAttribute('title', '打开启动页 https://start.example/');
    loadURL.mockClear();
    await user.click(home);
    expect(loadURL).toHaveBeenCalledWith('https://start.example/');
  });

  it('groups browsing history by day with truthful day headings', async () => {
    const user = userEvent.setup();
    const now = Date.now();
    window.pawBrowserHost = {
      ...electronBrowserHost(),
      getHistory: async () => [
        { id: 'h-today', title: 'Today doc', url: 'https://today.example/', visitedAt: now },
        { id: 'h-yesterday', title: 'Yesterday doc', url: 'https://yesterday.example/', visitedAt: now - 86_400_000 },
      ],
    };
    render(<ControlTransportProvider transport={browserTransport()}><PawBrowserApp /></ControlTransportProvider>);

    await user.click(await screen.findByRole('button', { name: '浏览历史' }));
    const historyRegion = await screen.findByRole('region', { name: '浏览历史' });
    await within(historyRegion).findByText('Today doc');
    const headings = within(historyRegion).getAllByRole('heading', { level: 3 });
    expect(headings.map((heading) => heading.textContent)).toEqual(['今天', '昨天']);
    expect(within(historyRegion).getByText('Yesterday doc')).toBeInTheDocument();
  });

  it('reopens a history entry in the same guest and leaves History behind', async () => {
    const user = userEvent.setup();
    window.pawBrowserHost = {
      ...electronBrowserHost(),
      getHistory: async () => [
        { id: 'h-one', title: 'Yesterday doc', url: 'https://yesterday.example/read', visitedAt: 1 },
      ],
    };
    render(<ControlTransportProvider transport={browserTransport()}><PawBrowserApp /></ControlTransportProvider>);
    const guest = document.querySelector('webview') as Element & Record<string, unknown>;
    const loadURL = vi.fn(async () => undefined);
    Object.assign(guest, { loadURL });

    await user.click(await screen.findByRole('button', { name: '浏览历史' }));
    const historyRegion = await screen.findByRole('region', { name: '浏览历史' });
    await user.click(within(historyRegion).getByRole('button', { name: /^Yesterday doc/ }));

    expect(loadURL).toHaveBeenCalledWith('https://yesterday.example/read');
    expect(screen.queryByRole('region', { name: '浏览历史' })).toBeNull();
    await waitFor(() => expect(screen.getByRole('textbox', { name: '页面地址' }))
      .toHaveValue('https://yesterday.example/read'));
    // Reopening reuses the one visible guest instead of spawning a tab.
    expect(document.querySelectorAll('webview')).toHaveLength(1);
  });

  it('clears History only through the persistent Browser host authority after an in-App confirmation', async () => {
    const user = userEvent.setup();
    const clearHistory = vi.fn(async () => []);
    window.pawBrowserHost = {
      ...electronBrowserHost(),
      clearHistory,
      getHistory: async () => [{ id: 'history-one', title: 'Example', url: 'https://example.com/', visitedAt: 1 }],
    };
    const confirmSpy = vi.spyOn(window, 'confirm');

    render(<ControlTransportProvider transport={browserTransport()}><PawBrowserApp /></ControlTransportProvider>);
    await user.click(await screen.findByRole('button', { name: '浏览历史' }));
    const historyRegion = await screen.findByRole('region', { name: '浏览历史' });
    expect(within(historyRegion).getByText('Example')).toBeInTheDocument();
    await user.click(within(historyRegion).getByRole('button', { name: '清空' }));
    // The first activation only arms the confirmation; nothing is cleared yet.
    expect(clearHistory).not.toHaveBeenCalled();
    const confirmGroup = within(historyRegion).getByRole('group', { name: '确认清空浏览历史' });
    expect(within(confirmGroup).getByText('清空全部浏览历史？')).toBeInTheDocument();

    // Cancelling disarms without touching the host.
    await user.click(within(confirmGroup).getByRole('button', { name: '取消' }));
    expect(clearHistory).not.toHaveBeenCalled();
    expect(within(historyRegion).queryByRole('group', { name: '确认清空浏览历史' })).toBeNull();

    await user.click(within(historyRegion).getByRole('button', { name: '清空' }));
    await user.click(within(historyRegion).getByRole('button', { name: '确认清空' }));
    await waitFor(() => expect(clearHistory).toHaveBeenCalledTimes(1));
    expect(screen.getByText('还没有浏览记录')).toBeInTheDocument();
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(localStorage.getItem('paw.browser.history.v1')).toBeNull();
  });

  it('says why the page area is empty when there is no guest and no captured picture', async () => {
    render(
      <ControlTransportProvider transport={browserTransport({ snapshot: () => ({ ok: true }) })}>
        <PawBrowserApp />
      </ControlTransportProvider>,
    );

    const awaiting = await screen.findByRole('status');
    expect(within(awaiting).getByText('还没有这一页的画面')).toBeInTheDocument();
    expect(within(awaiting).getByText(/当前环境不能直接内嵌网页/)).toBeInTheDocument();
    expect(within(awaiting).getByRole('button', { name: '重新载入' })).toBeEnabled();
  });

});

function browserTransport(overrides: {
  tabs?: (request: { query?: unknown }) => unknown;
  snapshot?: (request: { query?: unknown }) => unknown;
  traces?: unknown;
  command?: (request: { body?: unknown }) => unknown;
} = {}) {
  const snapshot = browserSnapshot('https://example.com', 41, 'Example');
  const tabs = overrides.tabs ?? (() => ({ ok: true, items: [{ deviceId: 'paw-browser', tabId: 41, title: 'Example', url: 'https://example.com', active: true }] }));
  const snapshotRoute = overrides.snapshot ?? (() => snapshot);
  const commandRoute = overrides.command ?? (() => ({ ok: true, status: 'completed' }));
  return new MockControlTransport({
    browserSnapshotImageUrl: () => 'blob:paw-browser-snapshot',
    routes: {
      'browser.status': {
        ok: true,
        connected: true,
        managedBrowser: {
          running: true,
          connected: true,
          controlProtocol: 'ego-browser',
          browserTransport: 'cdp',
          profilePath: '/tmp/paw-browser',
          egoBrowser: { available: true, hostRunning: true, secondBrowserProcess: false },
        },
        latestSnapshot: snapshot,
      },
      'browser.managed.start': { ok: true, running: true, connected: true, controlProtocol: 'ego-browser', browserTransport: 'cdp' },
      'browser.tabs': (request: ControlRequest) => tabs({ query: request.query }),
      'browser.snapshot.latest': (request: ControlRequest) => snapshotRoute({ query: request.query }),
      'browser.traces': overrides.traces ?? { ok: true, items: [{ commandId: 'cmd-1', action: 'navigate', sourceKind: 'agent', status: 'completed', createdAtMs: Date.now(), completedAtMs: Date.now(), durationMs: 140, result: { summary: 'Example' } }] },
      'browser.command': (request: ControlRequest) => commandRoute({ body: request.body }),
      'browser.stop': { ok: true, cancelled: 0 },
    },
  });
}

function browserSnapshot(url: string, tabId: number, title: string) {
  return {
    ok: true,
    snapshotId: 'snap-direct',
    deviceId: 'paw-browser',
    tabId,
    url,
    title,
    markdown: '# Example\n- [0:e1] button "Continue"',
    interactiveCount: 1,
    hasScreenshot: true,
    imagePath: '/api/browser/snapshots/snap-direct/image',
    createdAtMs: Date.now(),
  };
}

function electronBrowserHost(): NonNullable<typeof window.pawBrowserHost> {
  return {
    kind: 'electron-webview',
    partition: 'persist:paw-browser',
    activate: () => undefined,
    clearBrowsingData: async (action) => ({ action, after: 0, before: 0, completedAt: 3 }),
    clearHistory: async () => [],
    getHistory: async () => [],
    getSettings: async () => ({ cacheBytes: 0, cookieCount: 0, downloadPath: '/tmp', extensionCount: 0, extensionsPath: '/tmp/Extensions', partition: 'persist:paw-browser', permissionMode: 'site-request', startPage: 'about:blank' }),
    listExtensions: async () => [],
    loadUnpackedExtension: async () => null,
    openExtensionsFolder: async () => ({ opened: true, path: '/tmp/Extensions' }),
    openDownloads: async () => ({ opened: true, path: '/tmp' }),
    register: () => undefined,
    removeExtension: async () => [],
    removeHistoryEntry: async () => [],
    setStartPage: async (startPage) => ({ startPage }),
    takeScreenshot: async () => ({ path: '/tmp/page.png', saved: true }),
    onCommand: () => () => undefined,
    onGuestClosed: () => () => undefined,
    onHistoryChanged: () => () => undefined,
    onOpenUrl: () => () => undefined,
    onSelectTab: () => () => undefined,
  };
}

function mockGuestIdentity(element: Element, identity: { title: string; url: string; webContentsId: number }) {
  Object.assign(element, {
    getTitle: () => identity.title,
    getURL: () => identity.url,
    getWebContentsId: () => identity.webContentsId,
  });
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
