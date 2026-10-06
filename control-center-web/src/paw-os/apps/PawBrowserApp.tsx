import {
  Activity,
  ArrowLeft,
  ArrowRight,
  Bookmark,
  Bot,
  Check,
  CircleAlert,
  CircleX,
  Camera,
  Download,
  EllipsisVertical,
  Globe2,
  History,
  Home,
  PanelRightClose,
  PanelRightOpen,
  Printer,
  Puzzle,
  RefreshCw,
  Search,
  Settings,
  Sparkles,
  Square,
  X,
  ZoomIn,
  ZoomOut,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type WheelEvent } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { BrowserFindBar, type BrowserFindMatch } from '@/features/browser/BrowserFindBar';
import { BrowserLibraryPanel, type BrowserLibraryView } from '@/features/browser/BrowserLibraryPanel';
import { BrowserOmnibox } from '@/features/browser/BrowserOmnibox';
import { BrowserPageStatus } from '@/features/browser/BrowserPageStatus';
import { BrowserTabStrip, type BrowserTabItem } from '@/features/browser/BrowserTabStrip';
import {
  PAW_BROWSER_PARTITION,
  guestNavigationState,
  guestZoomFactor,
  loadPawBrowserUrl,
  pawBrowserHost,
  type PawBrowserGuestFailLoadEvent,
  type PawBrowserGuestFaviconEvent,
  type PawBrowserGuestFoundInPageEvent,
  type PawBrowserGuestProcessGoneEvent,
  type PawBrowserBookmark,
  type PawBrowserDownload,
  type PawBrowserHistoryEntry,
  type PawBrowserWebview,
  type PawBrowserExtension,
  type PawBrowserSettings,
} from './paw-browser-host';
import {
  browserActionLabel,
  browserElement,
  errorText,
  formatBytes,
  historyClock,
  historyDateTime,
  historyDayKey,
  historyDayLabel,
  hostTab,
  initialHostTab,
  isTextEntry,
  knownCount,
  normalizedAddress,
  number,
  record,
  rows,
  text,
  type BrowserElement,
  type BrowserRecord,
  type HostBrowserTab,
} from './paw-browser-model';
import { PawWindowChromePortal, usePawWindowChromeTarget } from '../shell/PawWindowChrome';
import type { PawOsWindowTarget } from '@/features/paw-os/model/desktop';
import { usePawOsAppActive } from '@/features/paw-os/surface-context';
import { usePageVisibility } from '@/platform/use-page-visibility';

export function PawBrowserApp({ target }: { target?: Extract<PawOsWindowTarget, { kind: 'browser-target' }> } = {}) {
  const transport = useControlTransport();
  const electronHost = pawBrowserHost();
  const surfaceActive = usePawOsAppActive() ?? true;
  const pageVisible = usePageVisibility();
  const windowChromeTarget = usePawWindowChromeTarget();
  const [tabs, setTabs] = useState<BrowserRecord[]>([]);
  const [traces, setTraces] = useState<BrowserRecord[]>([]);
  const [snapshot, setSnapshot] = useState<BrowserRecord>({});
  const [selectedTabId, setSelectedTabId] = useState(0);
  const [cdpUrl, setCdpUrl] = useState('');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [showTrace, setShowTrace] = useState(false);
  const [editingElement, setEditingElement] = useState<BrowserElement | null>(null);
  const [elementDraft, setElementDraft] = useState('');
  const [hostTabs, setHostTabs] = useState<HostBrowserTab[]>([initialHostTab()]);
  const [selectedHostTabId, setSelectedHostTabId] = useState('paw-tab-1');
  const [history, setHistory] = useState<PawBrowserHistoryEntry[]>([]);
  const [showHistory, setShowHistory] = useState(false);
  const [historyQuery, setHistoryQuery] = useState('');
  const [libraryView, setLibraryView] = useState<BrowserLibraryView | ''>('');
  const [bookmarks, setBookmarks] = useState<PawBrowserBookmark[]>([]);
  const [downloads, setDownloads] = useState<PawBrowserDownload[]>([]);
  const [showSettings, setShowSettings] = useState(false);
  const [browserSettings, setBrowserSettings] = useState<PawBrowserSettings | null>(null);
  const [browserExtensions, setBrowserExtensions] = useState<PawBrowserExtension[]>([]);
  const [settingsReceipt, setSettingsReceipt] = useState('');
  const [startPageDraft, setStartPageDraft] = useState('about:blank');
  const [showBrowserMenu, setShowBrowserMenu] = useState(false);
  const [showFind, setShowFind] = useState(false);
  const [findDraft, setFindDraft] = useState('');
  const [findMatch, setFindMatch] = useState<BrowserFindMatch>(null);
  const [zoomPercent, setZoomPercent] = useState(100);
  const [browserActionReceipt, setBrowserActionReceipt] = useState('');
  const [confirmingClear, setConfirmingClear] = useState<'' | 'history' | 'cache' | 'site-data'>('');
  const bookmarksRevision = useRef(0);
  const downloadsRevision = useRef(0);
  const acceptBookmarks = useCallback((next: PawBrowserBookmark[]) => {
    bookmarksRevision.current += 1;
    setBookmarks(next);
  }, []);
  const acceptDownloads = useCallback((next: PawBrowserDownload[]) => {
    downloadsRevision.current += 1;
    setDownloads(next);
  }, []);
  const hostWebviews = useRef(new Map<string, PawBrowserWebview>());
  const started = useRef(false);
  const openedTargetCommand = useRef('');
  const appliedTargetSelection = useRef('');
  const selectedTab = tabs.find((tab) => number(tab.tabId) === selectedTabId) ?? tabs[0];
  const selectedHostTab = hostTabs.find((tab) => tab.id === selectedHostTabId) ?? hostTabs[0];
  const selectedTabUrl = electronHost ? selectedHostTab?.url ?? 'about:blank' : text(selectedTab?.url);
  // One committed truth for the address model: the real guest URL owns the
  // omnibox rest state; the human draft lives inside BrowserOmnibox.
  const currentUrl = electronHost ? selectedHostTab?.url || 'about:blank' : cdpUrl;
  const omniboxTabKey = electronHost ? selectedHostTabId : String(selectedTabId);

  const refreshShell = useCallback(async (signal?: AbortSignal) => {
    const [tabsValue, tracesValue] = await Promise.all([
      transport.request({ pathId: 'browser.tabs', signal }),
      transport.request({ pathId: 'browser.traces', query: { limit: 30 }, signal }),
    ]);
    if (signal?.aborted) return;
    const nextTabs = rows(record(tabsValue).items);
    const nextTraces = rows(record(tracesValue).items);
    /* The shell polls every second while the window is open; keeping the
     * previous array identity when nothing changed lets an idle poll cost
     * zero renders instead of re-rendering the whole Browser App. */
    setTabs((current) => sameRecords(current, nextTabs) ? current : nextTabs);
    setTraces((current) => sameRecords(current, nextTraces) ? current : nextTraces);
    const selectedStillExists = selectedTabId > 0
      && nextTabs.some((tab) => number(tab.tabId) === selectedTabId);
    const nextTabId = selectedStillExists ? selectedTabId : number(nextTabs[0]?.tabId);
    const nextTab = nextTabs.find((tab) => number(tab.tabId) === nextTabId) ?? nextTabs[0];
    if (nextTabId && nextTabId !== selectedTabId) setSelectedTabId(nextTabId);
    if (nextTabId && text(nextTab?.url) && text(nextTab?.url) !== 'about:blank' && nextTraces.some((trace) => (
      text(trace.sourceKind) === 'agent'
        && ['queued', 'claimed'].includes(text(trace.status))
    ))) {
      const liveSnapshot = record(await transport.request({
        pathId: 'browser.snapshot.latest',
        query: { deviceId: 'paw-browser', tabId: nextTabId, includeMarkdown: true },
        signal,
      }));
      if (signal?.aborted) return;
      if (liveSnapshot.snapshotId) {
        setSnapshot((current) => current.snapshotId === liveSnapshot.snapshotId ? current : liveSnapshot);
      }
    }
  }, [selectedTabId, transport]);

  const captureSnapshot = useCallback(async (tabId: number) => {
    if (!tabId) return;
    try {
      const captured = record(await transport.request({
        pathId: 'browser.command',
        body: { action: 'screenshot', deviceId: 'paw-browser', tabId },
      }));
      if (captured.ok === false) throw new Error(text(captured.summary) || '页面没有打开');
      const value = record(await transport.request({
        pathId: 'browser.snapshot.latest',
        query: { deviceId: 'paw-browser', tabId, includeMarkdown: true },
      }));
      setSnapshot(value);
      const url = text(value.url);
      if (url) setCdpUrl(url);
      setError('');
    } catch (requestError) {
      setError(errorText(requestError));
    }
  }, [transport]);

  useEffect(() => {
    if (!surfaceActive || !pageVisible) return undefined;
    let active = true;
    const controller = new AbortController();
    async function boot() {
      try {
        if (!started.current) {
          started.current = true;
          if (!electronHost) await transport.request({ pathId: 'browser.managed.start', body: {} });
        }
        if (active) await refreshShell(controller.signal);
      } catch (requestError) {
        if (active) setError(errorText(requestError));
      }
    }
    void boot();
    const timer = window.setInterval(() => { if (active) void refreshShell(controller.signal).catch(() => undefined); }, 1_000);
    return () => { active = false; controller.abort(); window.clearInterval(timer); };
  }, [electronHost, pageVisible, refreshShell, surfaceActive, transport]);

  useEffect(() => {
    const url = target?.url?.trim() ?? '';
    const key = `${target?.commandId ?? ''}\n${url}`;
    if (!electronHost || !url || openedTargetCommand.current === key) return;
    openedTargetCommand.current = key;
    const tab = hostTab(url, target?.commandId);
    setHostTabs((current) => [...current, tab]);
    setSelectedHostTabId(tab.id);
  }, [electronHost, target?.commandId, target?.url]);

  useEffect(() => electronHost?.onGuestClosed((tabId) => closeHostTab(tabId)), [electronHost, selectedHostTabId]);
  useEffect(() => electronHost?.onHistoryChanged(setHistory), [electronHost]);
  useEffect(() => electronHost?.onBookmarksChanged?.(acceptBookmarks), [acceptBookmarks, electronHost]);
  useEffect(() => electronHost?.onDownloadsChanged?.(acceptDownloads), [acceptDownloads, electronHost]);
  useEffect(() => electronHost?.onSelectTab((tabId) => setSelectedHostTabId(tabId)), [electronHost]);

  useEffect(() => {
    if (!target) return;
    const targetKey = [
      target.commandId ?? '',
      target.targetId,
      target.tabId ?? '',
      target.url ?? '',
    ].join('\n');
    if (appliedTargetSelection.current === targetKey) return;
    if (electronHost) {
      const hostTab = hostTabs.find((tab) => (
        (target.commandId && tab.commandId === target.commandId)
        || tab.id === target.targetId
      ));
      if (!hostTab) return;
      appliedTargetSelection.current = targetKey;
      if (hostTab.id !== selectedHostTabId) setSelectedHostTabId(hostTab.id);
      return;
    }
    const tab = tabs.find((candidate) => (
      text(candidate.targetId) === target.targetId
      || (target.tabId !== undefined && number(candidate.tabId) === target.tabId)
    ));
    const tabId = number(tab?.tabId);
    if (!tabId) return;
    appliedTargetSelection.current = targetKey;
    if (tabId !== selectedTabId) setSelectedTabId(tabId);
  }, [electronHost, hostTabs, selectedHostTabId, selectedTabId, tabs, target]);

  useEffect(() => {
    if (!electronHost || !selectedHostTab?.webContentsId) return;
    electronHost.activate({
      title: selectedHostTab.title,
      url: selectedHostTab.url || 'about:blank',
      webContentsId: selectedHostTab.webContentsId,
    });
  }, [electronHost, selectedHostTab]);

  // Find state and zoom are per-guest: switching tabs drops the stale query
  // and reads the real zoom factor of the newly selected guest.
  useEffect(() => {
    if (!electronHost) return;
    setShowFind(false);
    setFindDraft('');
    setFindMatch(null);
    const webview = hostWebviews.current.get(selectedHostTabId);
    const factor = guestZoomFactor(webview ?? null);
    setZoomPercent(typeof factor === 'number' && Number.isFinite(factor) ? Math.round(factor * 100) : 100);
  }, [electronHost, selectedHostTabId]);

  useEffect(() => {
    if (!browserActionReceipt) return;
    const timer = window.setTimeout(() => setBrowserActionReceipt(''), 6_000);
    return () => window.clearTimeout(timer);
  }, [browserActionReceipt]);

  // A pending destructive confirmation never outlives its surface.
  useEffect(() => {
    if (!showHistory && !showSettings && !libraryView) setConfirmingClear('');
  }, [libraryView, showHistory, showSettings]);

  const refreshBrowserHistory = useCallback(async () => {
    if (!electronHost) return;
    setHistory(await electronHost.getHistory());
  }, [electronHost]);

  const refreshBrowserSettings = useCallback(async () => {
    if (!electronHost) return;
    const [value, extensions] = await Promise.all([
      electronHost.getSettings(),
      electronHost.listExtensions(),
    ]);
    setBrowserSettings(value);
    setBrowserExtensions(extensions);
    setStartPageDraft(value.startPage);
  }, [electronHost]);

  const refreshBrowserLibrary = useCallback(async () => {
    if (!electronHost) return;
    // Each collection updates independently. A newer read, mutation, or host
    // event invalidates an older snapshot before it can replace live state.
    const bookmarkRead = ++bookmarksRevision.current;
    const downloadRead = ++downloadsRevision.current;
    await Promise.all([
      (electronHost.getBookmarks ? electronHost.getBookmarks() : Promise.resolve([])).then((next) => {
        if (bookmarkRead === bookmarksRevision.current) setBookmarks(next);
      }),
      (electronHost.getDownloads ? electronHost.getDownloads() : Promise.resolve([])).then((next) => {
        if (downloadRead === downloadsRevision.current) setDownloads(next);
      }),
    ]);
  }, [electronHost]);

  useEffect(() => {
    if (!electronHost || !surfaceActive || !pageVisible) return;
    void refreshBrowserSettings().then(() => undefined, (requestError) => setError(errorText(requestError)));
    void refreshBrowserHistory().then(() => undefined, (requestError) => setError(errorText(requestError)));
    void refreshBrowserLibrary().then(() => undefined, (requestError) => setError(errorText(requestError)));
  }, [electronHost, pageVisible, refreshBrowserHistory, refreshBrowserLibrary, refreshBrowserSettings, surfaceActive]);

  useEffect(() => {
    if (!electronHost || !browserSettings || browserSettings.startPage === 'about:blank') return;
    const firstTab = hostTabs.length === 1 ? hostTabs[0] : null;
    if (!firstTab || firstTab.url !== 'about:blank') return;
    loadPawBrowserUrl({ current: hostWebviews.current.get(firstTab.id) ?? null }, browserSettings.startPage);
    updateHostTab(firstTab.id, { title: browserSettings.startPage, url: browserSettings.startPage });
  }, [browserSettings, electronHost, hostTabs]);

  useEffect(() => {
    if (electronHost || !surfaceActive || !pageVisible) return;
    if (!selectedTabId) return;
    if (!selectedTabUrl || selectedTabUrl === 'about:blank') {
      setSnapshot({});
      setCdpUrl('about:blank');
      setError('');
      return;
    }
    setCdpUrl(selectedTabUrl);
    void captureSnapshot(selectedTabId);
  }, [captureSnapshot, electronHost, pageVisible, selectedTabId, selectedTabUrl, surfaceActive]);

  const run = useCallback(async (action: string, extra: BrowserRecord = {}) => {
    setBusy(action);
    setError('');
    try {
      const value = record(await transport.request({
        pathId: 'browser.command',
        body: {
          action,
          deviceId: 'paw-browser',
          ...(selectedTabId ? { tabId: selectedTabId } : {}),
          ...extra,
        },
      }));
      if (value.ok === false) throw new Error(text(value.summary) || '浏览器操作未完成');
      await refreshShell();
      const result = record(value.result);
      const nextTabId = number(result.tabId) || selectedTabId;
      if (action === 'new_tab' && nextTabId) {
        setSelectedTabId(nextTabId);
        setCdpUrl(text(extra.url) || 'about:blank');
      } else if (nextTabId && action !== 'close_tab' && text(result.url) !== 'about:blank') {
        await captureSnapshot(nextTabId);
      }
      return value;
    } catch (requestError) {
      // Every caller fire-and-forgets; the surfaced error state is the
      // failure channel, so rethrowing would only leak unhandled rejections.
      setError(errorText(requestError));
      return null;
    } finally {
      setBusy('');
    }
  }, [captureSnapshot, refreshShell, selectedTabId, transport]);

  const navigateTo = (rawUrl: string) => {
    const url = normalizedAddress(rawUrl);
    if (!url) return;
    if (electronHost && selectedHostTab) {
      loadPawBrowserUrl(
        { current: hostWebviews.current.get(selectedHostTab.id) ?? null },
        url,
      );
      updateHostTab(selectedHostTab.id, { url });
      return;
    }
    setCdpUrl(url);
    if (selectedTabId && !busy) {
      void run('navigate', { url });
    }
  };

  const updateHostTab = (tabId: string, update: Partial<HostBrowserTab>) => {
    setHostTabs((current) => current.map((tab) => tab.id === tabId ? { ...tab, ...update } : tab));
  };

  const addHostTab = (url = 'about:blank') => {
    const tab = hostTab(url);
    setHostTabs((current) => [...current, tab]);
    setSelectedHostTabId(tab.id);
  };

  const closeHostTab = (tabId: string) => {
    setHostTabs((current) => {
      const index = current.findIndex((tab) => tab.id === tabId);
      const next = current.filter((tab) => tab.id !== tabId);
      if (next.length === 0) {
        const blank = initialHostTab();
        setSelectedHostTabId(blank.id);
        return [blank];
      }
      if (tabId === selectedHostTabId) {
        setSelectedHostTabId(next[Math.min(Math.max(index, 0), next.length - 1)].id);
      }
      return next;
    });
  };

  const runNavigation = (action: 'back' | 'forward' | 'reload') => {
    const webview = selectedHostTab ? hostWebviews.current.get(selectedHostTab.id) : null;
    if (electronHost && webview) {
      if (action === 'back' && webview.canGoBack()) webview.goBack();
      if (action === 'forward' && webview.canGoForward()) webview.goForward();
      if (action === 'reload') webview.reload();
      return;
    }
    void run(action);
  };

  const isStartPage = !currentUrl || currentUrl === 'about:blank' || currentUrl === 'paw://home';
  const snapshotId = text(snapshot.snapshotId);
  const snapshotImageUrl = snapshotId && snapshot.hasScreenshot && transport.browserSnapshotImageUrl
    ? transport.browserSnapshotImageUrl(snapshotId)
    : '';
  const viewport = record(snapshot.viewport);
  const viewportWidth = number(viewport.width);
  const viewportHeight = number(viewport.height);
  const elements = rows(viewport.elements).map(browserElement).filter((item): item is BrowserElement => Boolean(item));
  const activeAgentTrace = traces.find((trace) => (
    text(trace.sourceKind) === 'agent'
      && ['queued', 'claimed'].includes(text(trace.status))
  ));
  const activeAgentSteps = rows(activeAgentTrace?.steps);
  const activeAgentStep = activeAgentSteps.at(-1);
  const activeAgentAction = text(activeAgentStep?.action) || text(activeAgentTrace?.action);
  const activeAgentTask = text(activeAgentTrace?.target)
    || text(record(activeAgentTrace?.result).summary)
    || text(selectedTab?.title)
    || selectedHostTab?.title
    || '当前页面';
  const activeAgentTarget = text(activeAgentStep?.target) || activeAgentTask;
  const agentExecutionState = activeAgentTrace ? 'active' : '';
  const agentTargetRefId = text(activeAgentTrace?.targetRefId);
  const activeAgentTraceId = text(activeAgentTrace?.commandId);
  const activateElement = (element: BrowserElement) => {
    if (isTextEntry(element)) {
      setEditingElement(element);
      setElementDraft('');
      return;
    }
    void run('click', { refId: element.refId });
  };

  const submitElementDraft = () => {
    if (!editingElement || busy) return;
    const target = editingElement;
    const value = elementDraft;
    setEditingElement(null);
    setElementDraft('');
    void run('type', { refId: target.refId, text: value, clear: true, submit: true });
  };

  const scrollPage = (event: WheelEvent<HTMLDivElement>) => {
    if (busy || Math.abs(event.deltaY) < 8) return;
    event.preventDefault();
    void run('scroll', {
      direction: event.deltaY < 0 ? 'up' : 'down',
      amount: Math.min(1_200, Math.max(160, Math.round(Math.abs(event.deltaY) * 2))),
    });
  };

  const stopAgentBrowserWork = async () => {
    setBusy('stop');
    try {
      await transport.request({ pathId: 'browser.stop', body: {} });
      await refreshShell();
    } catch (requestError) {
      setError(errorText(requestError));
    } finally {
      setBusy('');
    }
  };

  const clearBrowserData = async (action: 'cache' | 'site-data') => {
    if (!electronHost) return;
    setConfirmingClear('');
    setBusy(`clear-${action}`);
    setSettingsReceipt('');
    try {
      const receipt = await electronHost.clearBrowsingData(action);
      setSettingsReceipt(action === 'cache'
        ? `缓存已清除（${formatBytes(receipt.before)} → ${formatBytes(receipt.after)}）`
        : `Cookie 与站点数据已清除（${receipt.before} → ${receipt.after}）`);
      await refreshBrowserSettings();
    } catch (requestError) {
      setSettingsReceipt(`清理失败：${errorText(requestError)}`);
    } finally {
      setBusy('');
    }
  };

  const saveStartPage = async () => {
    if (!electronHost) return;
    setBusy('save-start-page');
    setSettingsReceipt('');
    try {
      const saved = await electronHost.setStartPage(normalizedAddress(startPageDraft) || 'about:blank');
      setStartPageDraft(saved.startPage);
      setSettingsReceipt('启动页已保存');
      await refreshBrowserSettings();
    } catch (requestError) {
      setSettingsReceipt(`保存失败：${errorText(requestError)}`);
    } finally {
      setBusy('');
    }
  };

  const selectedWebview = () => selectedHostTab ? hostWebviews.current.get(selectedHostTab.id) ?? null : null;

  useEffect(() => {
    if (!activeAgentTraceId) return;
    setShowTrace(true);
  }, [activeAgentTraceId]);

  const loadUnpackedExtension = async () => {
    if (!electronHost) return;
    setBusy('load-extension');
    setSettingsReceipt('');
    try {
      const loaded = await electronHost.loadUnpackedExtension();
      if (loaded) setSettingsReceipt(`已加载扩展程序 ${loaded.name}`);
      await refreshBrowserSettings();
    } catch (requestError) {
      setSettingsReceipt(`加载扩展程序失败：${errorText(requestError)}`);
    } finally {
      setBusy('');
    }
  };

  const openExtensionsFolder = async () => {
    if (!electronHost) return;
    setBusy('open-extensions');
    setSettingsReceipt('');
    try {
      const receipt = await electronHost.openExtensionsFolder();
      setSettingsReceipt(receipt.opened ? `已打开扩展程序目录 ${receipt.path}` : '扩展程序目录未打开');
    } catch (requestError) {
      setSettingsReceipt(`打开扩展程序目录失败：${errorText(requestError)}`);
    } finally {
      setBusy('');
    }
  };

  const removeBrowserExtension = async (extensionId: string) => {
    if (!electronHost) return;
    setBusy('remove-extension');
    setSettingsReceipt('');
    try {
      setBrowserExtensions(await electronHost.removeExtension(extensionId));
      setSettingsReceipt('扩展程序已移除');
      await refreshBrowserSettings();
    } catch (requestError) {
      setSettingsReceipt(`移除扩展程序失败：${errorText(requestError)}`);
    } finally {
      setBusy('');
    }
  };

  const takeOverBrowserWork = async () => {
    await stopAgentBrowserWork();
    setShowTrace(false);
    selectedWebview()?.focus();
  };

  const findOnPage = (value: string, findNext = false, forward = true) => {
    setFindDraft(value);
    if (value !== findDraft) setFindMatch(null);
    if (!value) {
      selectedWebview()?.stopFindInPage('clearSelection');
      setFindMatch(null);
      return;
    }
    selectedWebview()?.findInPage(value, { findNext, forward });
  };

  const closeFind = () => {
    selectedWebview()?.stopFindInPage('clearSelection');
    setFindDraft('');
    setFindMatch(null);
    setShowFind(false);
    selectedWebview()?.focus();
  };

  const applyZoomPercent = (next: number) => {
    const webview = selectedWebview();
    if (!webview) return;
    webview.setZoomFactor(next / 100);
    setZoomPercent(next);
  };

  const changeZoom = (step: number) => {
    applyZoomPercent(Math.min(300, Math.max(25, zoomPercent + step)));
  };

  const takeScreenshot = async () => {
    if (!electronHost) return;
    const webContentsId = selectedHostTab?.webContentsId;
    if (!webContentsId) {
      setShowBrowserMenu(false);
      setBrowserActionReceipt('页面还没有就绪，无法截图');
      return;
    }
    setBusy('screenshot');
    try {
      const receipt = await electronHost.takeScreenshot(webContentsId);
      setBrowserActionReceipt(receipt.saved ? `截图已保存到 ${receipt.path}` : '截图未保存');
    } catch (requestError) {
      setBrowserActionReceipt(`截图失败：${errorText(requestError)}`);
    } finally {
      setBusy('');
      setShowBrowserMenu(false);
    }
  };

  const openDownloads = async () => {
    if (!electronHost) return;
    try {
      const receipt = await electronHost.openDownloads();
      setBrowserActionReceipt(receipt.opened ? `已打开下载目录 ${receipt.path}` : '下载目录未打开');
    } catch (requestError) {
      setBrowserActionReceipt(`打开下载目录失败：${errorText(requestError)}`);
    } finally {
      setShowBrowserMenu(false);
    }
  };

  const addCurrentBookmark = async () => {
    if (!electronHost?.addBookmark || !selectedHostTab) {
      setBrowserActionReceipt('当前 Browser 宿主还不支持书签存储');
      return;
    }
    const url = selectedHostTab.url.trim();
    if (!/^https?:\/\//i.test(url)) {
      setBrowserActionReceipt('只有 http(s) 页面可以收藏');
      return;
    }
    setBusy('add-bookmark');
    try {
      acceptBookmarks(await electronHost.addBookmark({ title: selectedHostTab.title || url, url }));
      setBrowserActionReceipt('当前页面已加入书签');
    } catch (requestError) {
      setBrowserActionReceipt(`添加书签失败：${errorText(requestError)}`);
    } finally {
      setBusy('');
    }
  };

  const removeBookmark = async (bookmarkId: string) => {
    if (!electronHost?.removeBookmark) {
      setBrowserActionReceipt('当前 Browser 宿主还不支持书签存储');
      return;
    }
    setBusy('remove-bookmark');
    try {
      acceptBookmarks(await electronHost.removeBookmark(bookmarkId));
    } catch (requestError) {
      setBrowserActionReceipt(`删除书签失败：${errorText(requestError)}`);
    } finally {
      setBusy('');
    }
  };

  const openDownload = async (downloadId: string) => {
    if (!electronHost?.openDownload) {
      setBrowserActionReceipt('当前 Browser 宿主还不支持打开下载文件');
      return;
    }
    setBusy('open-download');
    try {
      const receipt = await electronHost.openDownload(downloadId);
      setBrowserActionReceipt(receipt.opened ? `已打开 ${receipt.filename}` : `未打开 ${receipt.filename}`);
    } catch (requestError) {
      setBrowserActionReceipt(`打开下载失败：${errorText(requestError)}`);
    } finally {
      setBusy('');
    }
  };

  const revealDownload = async (downloadId: string) => {
    if (!electronHost?.revealDownload) {
      setBrowserActionReceipt('当前 Browser 宿主还不支持显示下载位置');
      return;
    }
    setBusy('reveal-download');
    try {
      const receipt = await electronHost.revealDownload(downloadId);
      setBrowserActionReceipt(receipt.revealed ? `已定位 ${receipt.filename}` : `未定位 ${receipt.filename}`);
    } catch (requestError) {
      setBrowserActionReceipt(`定位下载失败：${errorText(requestError)}`);
    } finally {
      setBusy('');
    }
  };

  const cancelDownload = async (downloadId: string) => {
    if (!electronHost?.cancelDownload) {
      setBrowserActionReceipt('当前 Browser 宿主还不支持取消下载');
      return;
    }
    setBusy('cancel-download');
    const cancellationRevision = downloadsRevision.current;
    try {
      const receipt = await electronHost.cancelDownload(downloadId);
      // A lifecycle event can finish the item while this IPC reply is in
      // flight. Keep that newer state instead of replaying the older receipt.
      if (cancellationRevision !== downloadsRevision.current) return;
      downloadsRevision.current += 1;
      setDownloads((current) => current.map((entry) => entry.id === receipt.id ? receipt : entry));
      setBrowserActionReceipt(receipt.state === 'cancelled'
        ? '下载已取消'
        : receipt.state === 'completed'
          ? '下载已完成，未取消'
          : receipt.state === 'pending' || receipt.state === 'progressing'
            ? '已请求取消下载，等待宿主确认'
            : '下载已停止');
    } catch (requestError) {
      setBrowserActionReceipt(`取消下载失败：${errorText(requestError)}`);
    } finally {
      setBusy('');
    }
  };

  const clearHistory = async () => {
    if (!electronHost) return;
    setConfirmingClear('');
    setBusy('clear-history');
    try {
      setHistory(await electronHost.clearHistory());
      setBrowserActionReceipt('浏览历史已清空');
    } catch (requestError) {
      setBrowserActionReceipt(`清空浏览历史失败：${errorText(requestError)}`);
    } finally {
      setBusy('');
    }
  };

  const removeHistoryEntry = async (entryId: string) => {
    if (!electronHost) return;
    try {
      setHistory(await electronHost.removeHistoryEntry(entryId));
    } catch (requestError) {
      setBrowserActionReceipt(`删除浏览记录失败：${errorText(requestError)}`);
    }
  };

  const retryPage = () => {
    const webview = selectedWebview();
    const tab = selectedHostTab;
    if (!webview || !tab) return;
    const failedUrl = tab.failure?.url;
    updateHostTab(tab.id, { crashedReason: undefined, failure: null, loading: true });
    if (failedUrl) void webview.loadURL(failedUrl);
    else webview.reload();
  };

  const visibleHistory = useMemo(() => history.filter((entry) => {
    const query = historyQuery.trim().toLocaleLowerCase();
    return !query || entry.title.toLocaleLowerCase().includes(query) || entry.url.toLocaleLowerCase().includes(query);
  }), [history, historyQuery]);

  // History reads like a timeline: consecutive visits of the same calendar day
  // share one day heading, and rows only repeat the clock time.
  const historyDayGroups = useMemo(() => {
    const groups: { key: string; label: string; entries: PawBrowserHistoryEntry[] }[] = [];
    for (const entry of visibleHistory) {
      const key = historyDayKey(entry.visitedAt);
      const lastGroup = groups.at(-1);
      if (lastGroup && lastGroup.key === key) lastGroup.entries.push(entry);
      else groups.push({ key, label: historyDayLabel(entry.visitedAt), entries: [entry] });
    }
    return groups;
  }, [visibleHistory]);

  const openLibrary = (view: BrowserLibraryView) => {
    setShowHistory(false);
    setShowSettings(false);
    setLibraryView(view);
    void refreshBrowserLibrary().catch((requestError) => setError(errorText(requestError)));
  };

  const openBookmark = (bookmark: PawBrowserBookmark) => {
    setLibraryView('');
    navigateTo(bookmark.url);
  };

  const bookmarksAvailable = Boolean(electronHost?.getBookmarks && electronHost?.addBookmark && electronHost?.removeBookmark);
  const downloadsAvailable = Boolean(electronHost?.getDownloads && electronHost?.openDownload && electronHost?.revealDownload && electronHost?.cancelDownload);

  const homePage = electronHost ? (browserSettings?.startPage || 'about:blank') : 'about:blank';

  const selectedTabLoading = Boolean(electronHost && selectedHostTab?.loading);
  // The load hairline mirrors only what the selected guest actually reported.
  const selectedGuestLoading = electronHost ? Boolean(selectedHostTab?.loading) : Boolean(selectedTab?.loading);
  const selectedPageFailure = electronHost ? selectedHostTab?.failure ?? null : null;
  const selectedPageCrash = electronHost ? selectedHostTab?.crashedReason : undefined;
  const tabItems: BrowserTabItem[] = electronHost
    ? hostTabs.map((tab) => ({
      id: tab.id,
      title: tab.title || '新标签页',
      active: tab.id === selectedHostTabId,
      loading: tab.loading,
      favicon: tab.favicon,
      failed: Boolean(tab.failure || tab.crashedReason),
    }))
    : tabs.map((tab) => ({
      id: String(number(tab.tabId)),
      title: text(tab.title) || '新标签页',
      active: number(tab.tabId) === selectedTabId,
      loading: Boolean(tab.loading) || undefined,
      favicon: text(tab.favicon) || undefined,
    }));

  const browserTabs = (
    <BrowserTabStrip
      inWindowChrome={Boolean(windowChromeTarget)}
      newTabDisabled={Boolean(busy)}
      onClose={(tabId) => {
        // Close exactly the clicked tab, never whichever tab happens to be
        // selected: the managed runtime is told the precise tab id.
        if (electronHost) closeHostTab(tabId);
        else void run('close_tab', { tabId: Number(tabId) });
      }}
      onNewTab={() => {
        if (electronHost) addHostTab();
        else void run('new_tab', { url: 'about:blank' });
      }}
      onSelect={(tabId) => {
        if (electronHost) setSelectedHostTabId(tabId);
        else setSelectedTabId(Number(tabId));
      }}
      tabs={tabItems}
    />
  );

  return (
    <>
      {windowChromeTarget ? <PawWindowChromePortal>{browserTabs}</PawWindowChromePortal> : null}
      <section
        aria-label="Browser"
        className="paw-direct-browser"
        data-route-id="browser"
        data-tabs-in-window-chrome={windowChromeTarget ? true : undefined}
        role="region"
      >
        {windowChromeTarget ? null : browserTabs}

      <section className="paw-browser-toolbar">
        <div className="paw-nav-buttons">
          <button
            aria-label="后退"
            disabled={electronHost ? !selectedHostTab?.canGoBack : !selectedTabId || Boolean(busy)}
            onClick={() => runNavigation('back')}
            type="button"
          >
            <ArrowLeft size={15} />
          </button>
          <button
            aria-label="前进"
            disabled={electronHost ? !selectedHostTab?.canGoForward : !selectedTabId || Boolean(busy)}
            onClick={() => runNavigation('forward')}
            type="button"
          >
            <ArrowRight size={15} />
          </button>
          <button
            aria-label={selectedTabLoading ? '停止加载' : '刷新网页'}
            disabled={electronHost ? !selectedHostTab : !selectedTabId || Boolean(busy)}
            onClick={() => {
              if (selectedTabLoading) selectedWebview()?.stop();
              else runNavigation('reload');
            }}
            type="button"
          >
            {selectedTabLoading ? <X size={14} /> : <RefreshCw className={busy === 'reload' ? 'ui-spin' : ''} size={14} />}
          </button>
          <button
            aria-label={homePage === 'about:blank' ? '打开空白页' : '打开启动页'}
            className="paw-browser-home"
            onClick={() => navigateTo(homePage)}
            title={homePage === 'about:blank' ? '打开空白页' : `打开启动页 ${homePage}`}
            type="button"
          >
            <Home size={14} />
          </button>
        </div>

        <BrowserOmnibox
          committedUrl={currentUrl}
          onNavigate={navigateTo}
          tabKey={omniboxTabKey}
        />

        <div className="paw-toolbar-actions">
          <button
            aria-label={showTrace ? '隐藏 Agent 浏览器轨迹' : '显示 Agent 浏览器轨迹'}
            aria-pressed={showTrace}
            data-active={showTrace || undefined}
            onClick={() => setShowTrace((value) => !value)}
            title="Agent 浏览器轨迹"
            type="button"
          >
            {showTrace ? <PanelRightClose size={14} /> : <PanelRightOpen size={14} />}
          </button>
          {electronHost ? (
            <button
              aria-label="浏览历史"
              className="paw-browser-history-toggle"
              disabled={Boolean(busy)}
              onClick={() => { setShowSettings(false); setLibraryView(''); setShowHistory((value) => !value); }}
              title="浏览历史"
              type="button"
            >
              <History size={14} />
            </button>
          ) : null}
          {electronHost ? (
            <button
              aria-label="书签"
              aria-pressed={libraryView === 'bookmarks'}
              className="paw-browser-bookmarks-toggle"
              data-active={libraryView === 'bookmarks' || undefined}
              disabled={Boolean(busy)}
              onClick={() => libraryView === 'bookmarks' ? setLibraryView('') : openLibrary('bookmarks')}
              title="书签"
              type="button"
            >
              <Bookmark size={14} />
            </button>
          ) : null}
          {electronHost ? (
            <button
              aria-label="下载记录"
              aria-pressed={libraryView === 'downloads'}
              className="paw-browser-downloads-toggle"
              data-active={libraryView === 'downloads' || undefined}
              disabled={Boolean(busy)}
              onClick={() => libraryView === 'downloads' ? setLibraryView('') : openLibrary('downloads')}
              title="下载记录"
              type="button"
            >
              <Download size={14} />
            </button>
          ) : null}
          {electronHost ? (
            <button
              aria-label="Browser 设置"
              className="paw-browser-settings-toggle"
              disabled={Boolean(busy)}
              onClick={() => { setShowHistory(false); setLibraryView(''); setShowSettings((value) => !value); if (!showSettings) void refreshBrowserSettings(); }}
              title="Browser 设置"
              type="button"
            >
              <Settings size={14} />
            </button>
          ) : null}
          {electronHost ? (
            <button
              aria-expanded={showBrowserMenu}
              aria-haspopup="menu"
              aria-label="Browser 菜单"
              disabled={Boolean(busy)}
              onClick={() => {
                const next = !showBrowserMenu;
                if (next) {
                  const factor = guestZoomFactor(selectedWebview());
                  if (typeof factor === 'number') setZoomPercent(Math.round(factor * 100));
                }
                setShowBrowserMenu(next);
              }}
              title="Browser 菜单"
              type="button"
            >
              <EllipsisVertical size={14} />
            </button>
          ) : null}
          {electronHost && showBrowserMenu ? (
            <div className="paw-browser-menu paw-menu" role="menu">
              <button onClick={() => { setShowFind(true); setShowBrowserMenu(false); }} role="menuitem" type="button"><Search size={13} />页内查找</button>
              <button onClick={() => { selectedWebview()?.print(); setShowBrowserMenu(false); }} role="menuitem" type="button"><Printer size={13} />打印</button>
              <div className="paw-browser-menu-zoom">
                <span>缩放</span>
                <button aria-label="缩小网页" onClick={() => changeZoom(-10)} type="button"><ZoomOut size={13} /></button>
                <button
                  aria-label="恢复默认缩放"
                  className="paw-browser-menu-zoom-reset"
                  disabled={zoomPercent === 100}
                  onClick={() => applyZoomPercent(100)}
                  title={zoomPercent === 100 ? '当前为默认缩放' : '恢复 100%'}
                  type="button"
                >
                  {zoomPercent}%
                </button>
                <button aria-label="放大网页" onClick={() => changeZoom(10)} type="button"><ZoomIn size={13} /></button>
              </div>
              <button onClick={() => void takeScreenshot()} role="menuitem" type="button"><Camera size={13} />截图</button>
              <button onClick={() => void openDownloads()} role="menuitem" type="button"><Download size={13} />下载</button>
              <button onClick={() => { openLibrary('bookmarks'); setShowBrowserMenu(false); }} role="menuitem" type="button"><Bookmark size={13} />书签</button>
              <button onClick={() => { openLibrary('downloads'); setShowBrowserMenu(false); }} role="menuitem" type="button"><Download size={13} />下载记录</button>
              <button className="paw-browser-menu-narrow-only" onClick={() => { setShowSettings(false); setLibraryView(''); setShowHistory(true); setShowBrowserMenu(false); void refreshBrowserHistory(); }} role="menuitem" type="button"><History size={13} />浏览历史</button>
              <button className="paw-browser-menu-narrow-only" onClick={() => { setShowHistory(false); setLibraryView(''); setShowSettings(true); setShowBrowserMenu(false); void refreshBrowserSettings(); }} role="menuitem" type="button"><Settings size={13} />浏览器设置</button>
            </div>
          ) : null}
        </div>
      </section>

      {/* The grid column follows the rendered sidebar, not the Agent: an
        * on-demand trace opened while nobody is executing still needs its
        * own column instead of stacking under the page. */}
      <section className="paw-browser-workspace" data-show-agent={showTrace || undefined}>
        <div className="paw-browser-viewport" data-agent-state={agentExecutionState || undefined}>
          {selectedGuestLoading ? (
            <span aria-hidden="true" className="paw-browser-loadbar" />
          ) : null}
          {!electronHost ? (
            <div aria-label="Browser 宿主能力说明" className="paw-browser-host-notice" role="note">
              <Globe2 aria-hidden="true" size={16} />
              <span>
                <strong>完整浏览器请在 PAW 桌面应用中使用</strong>
                <small>这里是网页控制视图。历史、书签和下载记录保存在桌面应用中。</small>
              </span>
            </div>
          ) : null}
          {electronHost && showFind ? (
            <BrowserFindBar
              match={findMatch}
              onChange={(value) => findOnPage(value)}
              onClose={closeFind}
              onNext={() => findOnPage(findDraft, true, true)}
              onPrevious={() => findOnPage(findDraft, true, false)}
              value={findDraft}
            />
          ) : null}
          {browserActionReceipt ? (
            <div className="paw-browser-action-receipt">
              <output role="status">{browserActionReceipt}</output>
              <button aria-label="关闭操作提示" onClick={() => setBrowserActionReceipt('')} type="button"><X size={12} /></button>
            </div>
          ) : null}
          {showHistory ? (
            <section aria-label="浏览历史" className="paw-browser-history">
              <div className="paw-browser-surface-card">
                <header>
                  <div><History size={16} /><strong>浏览历史</strong></div>
                  <div>
                    {confirmingClear === 'history' ? (
                      <span aria-label="确认清空浏览历史" className="paw-browser-confirm" role="group">
                        <em>清空全部浏览历史？</em>
                        <button data-danger disabled={Boolean(busy)} onClick={() => void clearHistory()} type="button">确认清空</button>
                        <button onClick={() => setConfirmingClear('')} type="button">取消</button>
                      </span>
                    ) : (
                      <button disabled={!history.length || Boolean(busy)} onClick={() => setConfirmingClear('history')} type="button">清空</button>
                    )}
                    <button aria-label="关闭浏览历史" onClick={() => setShowHistory(false)} type="button"><X size={14} /></button>
                  </div>
                </header>
                <label className="paw-browser-history-search"><Search size={13} /><input aria-label="搜索浏览历史" onChange={(event) => setHistoryQuery(event.target.value)} placeholder="搜索标题或网址" type="search" value={historyQuery} /></label>
                {historyDayGroups.length ? (
                  <div className="paw-browser-history-days">
                    {historyDayGroups.map((group) => (
                      <section className="paw-browser-history-day" key={group.key}>
                        <h3>{group.label}</h3>
                        <ol>
                          {group.entries.map((entry) => (
                            <li key={`${entry.visitedAt}-${entry.url}`}>
                              <button onClick={() => { setShowHistory(false); navigateTo(entry.url); }} type="button">
                                <Globe2 size={14} />
                                <span><strong>{entry.title || entry.url}</strong><small>{entry.url}</small></span>
                                <time dateTime={historyDateTime(entry.visitedAt)}>{historyClock(entry.visitedAt)}</time>
                              </button>
                              <button aria-label={`删除 ${entry.title || entry.url}`} onClick={() => void removeHistoryEntry(entry.id)} type="button"><X size={13} /></button>
                            </li>
                          ))}
                        </ol>
                      </section>
                    ))}
                  </div>
                ) : <p>{history.length ? '没有匹配的浏览记录' : '还没有浏览记录'}</p>}
              </div>
            </section>
          ) : null}
          {electronHost && libraryView ? (
            <BrowserLibraryPanel
              bookmarks={bookmarks}
              bookmarksAvailable={bookmarksAvailable}
              busy={busy}
              currentPage={selectedHostTab ? { title: selectedHostTab.title, url: selectedHostTab.url } : undefined}
              downloads={downloads}
              downloadsAvailable={downloadsAvailable}
              onAddBookmark={() => void addCurrentBookmark()}
              onCancelDownload={(downloadId) => void cancelDownload(downloadId)}
              onClose={() => setLibraryView('')}
              onOpenBookmark={openBookmark}
              onOpenDownload={(downloadId) => void openDownload(downloadId)}
              onOpenDownloadsFolder={() => void openDownloads()}
              onRemoveBookmark={(bookmarkId) => void removeBookmark(bookmarkId)}
              onRevealDownload={(downloadId) => void revealDownload(downloadId)}
              view={libraryView}
            />
          ) : null}
          {electronHost && showSettings ? (
            <section aria-label="Browser 设置" className="paw-browser-settings">
              <div className="paw-browser-surface-card">
                <header><div><Settings size={16} /><strong>Browser 设置</strong></div><button aria-label="关闭 Browser 设置" onClick={() => setShowSettings(false)} type="button"><X size={14} /></button></header>
                <div className="paw-browser-settings-body">
                  <section><h3>启动页</h3><div className="paw-browser-setting-row"><input aria-label="Browser 启动页" onChange={(event) => setStartPageDraft(event.target.value)} value={startPageDraft} /><button disabled={Boolean(busy)} onClick={() => void saveStartPage()} type="button">保存</button></div></section>
                  <section><h3>下载</h3><p>下载位置</p><code>{browserSettings?.downloadPath || '读取中…'}</code></section>
                  <section>
                    <h3>浏览数据</h3>
                    <div className="paw-browser-setting-row">
                      <span>缓存 · {formatBytes(browserSettings?.cacheBytes ?? 0)}</span>
                      {confirmingClear === 'cache' ? (
                        <span aria-label="确认清除缓存" className="paw-browser-confirm" role="group">
                          <button data-danger disabled={Boolean(busy)} onClick={() => void clearBrowserData('cache')} type="button">确认清除</button>
                          <button onClick={() => setConfirmingClear('')} type="button">取消</button>
                        </span>
                      ) : (
                        <button disabled={Boolean(busy)} onClick={() => setConfirmingClear('cache')} type="button">清除缓存</button>
                      )}
                    </div>
                    <div className="paw-browser-setting-row">
                      <span>Cookie 与站点数据 · {browserSettings?.cookieCount ?? 0} 个 Cookie</span>
                      {confirmingClear === 'site-data' ? (
                        <span aria-label="确认清除 Cookie 与站点数据" className="paw-browser-confirm" role="group">
                          <button data-danger disabled={Boolean(busy)} onClick={() => void clearBrowserData('site-data')} type="button">确认清除</button>
                          <button onClick={() => setConfirmingClear('')} type="button">取消</button>
                        </span>
                      ) : (
                        <button disabled={Boolean(busy)} onClick={() => setConfirmingClear('site-data')} type="button">清除 Cookie 与站点数据</button>
                      )}
                    </div>
                  </section>
                  <section><h3>网站权限</h3><p>网站在需要时请求权限，由当前隔离 Browser Session 处理。</p></section>
                  <section>
                    <h3>扩展程序</h3>
                    <p>已安装 {browserSettings?.extensionCount ?? browserExtensions.length} 个扩展程序，与 Chrome 一样可在隔离 Browser Session 中加载。</p>
                    <code>{browserSettings?.extensionsPath || '读取中…'}</code>
                    {browserExtensions.length ? (
                      <ul className="paw-browser-extension-list">
                        {browserExtensions.map((extension) => (
                          <li key={extension.id}>
                            <span><strong>{extension.name}</strong><small>{extension.version}</small></span>
                            <button aria-label={`移除扩展程序 ${extension.name}`} disabled={Boolean(busy)} onClick={() => void removeBrowserExtension(extension.id)} type="button">移除</button>
                          </li>
                        ))}
                      </ul>
                    ) : <p className="paw-browser-extension-empty">还没有安装扩展程序</p>}
                    <div className="paw-browser-setting-row paw-browser-extension-actions">
                      <button disabled={Boolean(busy)} onClick={() => void loadUnpackedExtension()} type="button"><Puzzle size={13} />加载已解压的扩展程序</button>
                      <button disabled={Boolean(busy)} onClick={() => void openExtensionsFolder()} type="button">打开扩展程序目录</button>
                    </div>
                  </section>
                  {settingsReceipt ? <output role="status">{settingsReceipt}</output> : null}
                </div>
              </div>
            </section>
          ) : null}
          {electronHost ? hostTabs.map((tab) => (
            <NativeBrowserWebview
              active={tab.id === selectedHostTabId}
              key={tab.id}
              onChange={({ title, url }) => {
                // Guest identity flows into the tab record; the activate
                // effect and the omnibox read from that single source.
                updateHostTab(tab.id, { title, url });
              }}
              onFoundInPage={(match) => {
                if (tab.id === selectedHostTabId) setFindMatch(match);
              }}
              onGuestState={(update) => updateHostTab(tab.id, update)}
              onIdentity={(webContentsId) => {
                updateHostTab(tab.id, { webContentsId });
                electronHost.register({ commandId: tab.commandId, tabId: tab.id, webContentsId });
              }}
              onWebview={(element) => {
                if (element) hostWebviews.current.set(tab.id, element);
                else hostWebviews.current.delete(tab.id);
              }}
              tab={tab}
            />
          )) : null}
          {electronHost && isStartPage && !selectedPageFailure && !selectedPageCrash ? (
            <div aria-hidden="true" className="paw-browser-start" data-live={activeAgentTrace ? true : undefined}>
              <span className="paw-browser-start-logo"><Globe2 size={28} /></span>
              <span className="paw-browser-start-halo"><Sparkles size={26} /></span>
              <strong>新标签页</strong>
              <span className="paw-browser-start-hint">在地址栏输入网址或搜索内容</span>
            </div>
          ) : null}
          {electronHost && (selectedPageFailure || selectedPageCrash) && !showHistory && !showSettings ? (
            <BrowserPageStatus
              crashedReason={selectedPageCrash}
              failure={selectedPageFailure}
              onRetry={retryPage}
            />
          ) : null}
          {!electronHost && error && !isStartPage ? (
            <div className="paw-browser-error" role="alert">
              <CircleAlert size={16} />
              <span>
                <strong>页面没有打开</strong>
                <small>{error}</small>
              </span>
              <button disabled={!selectedTabId || Boolean(busy)} onClick={() => void captureSnapshot(selectedTabId)} type="button">重试</button>
            </div>
          ) : null}

          {!electronHost && isStartPage ? (
            <div aria-label="空白页面" className="paw-browser-blank-page" data-live={activeAgentTrace ? true : undefined} />
          ) : !electronHost && snapshotImageUrl && viewportWidth && viewportHeight ? (
            <div className="paw-browser-live-view" onWheel={scrollPage}>
              <div className="paw-browser-live-canvas" style={{ aspectRatio: `${viewportWidth} / ${viewportHeight}` }}>
                <img alt={text(snapshot.title) || '网页视图'} draggable={false} src={snapshotImageUrl} />
                {elements.map((element) => (
                  <button
                    aria-label={element.label || `${element.role} ${element.refId}`}
                    className="paw-browser-hit-target"
                    data-agent-target={agentTargetRefId === element.refId || undefined}
                    key={element.refId}
                    onClick={() => activateElement(element)}
                    style={{
                      height: `${element.height / viewportHeight * 100}%`,
                      left: `${element.x / viewportWidth * 100}%`,
                      top: `${element.y / viewportHeight * 100}%`,
                      width: `${element.width / viewportWidth * 100}%`,
                    }}
                    type="button"
                  />
                ))}
                {editingElement ? (
                  <form
                    className="paw-browser-inline-entry"
                    onSubmit={(event) => { event.preventDefault(); submitElementDraft(); }}
                    style={{
                      left: `${Math.max(0, editingElement.x) / viewportWidth * 100}%`,
                      top: `${Math.max(0, editingElement.y) / viewportHeight * 100}%`,
                      width: `${Math.max(180, editingElement.width) / viewportWidth * 100}%`,
                    }}
                  >
                    <input
                      autoFocus
                      onChange={(event) => setElementDraft(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === 'Escape') {
                          setEditingElement(null);
                          setElementDraft('');
                        }
                      }}
                      placeholder={editingElement.label || '输入'}
                      value={elementDraft}
                    />
                  </form>
                ) : null}
              </div>
            </div>
          ) : !electronHost && !error ? (
            /* Outside the desktop host there is no guest to embed, so the page
               is whatever Runtime last captured. A bare reload glyph left the
               reader staring at a blank sheet with nothing explaining it. */
            <div className="paw-browser-awaiting" role="status">
              <RefreshCw aria-hidden="true" className={busy ? 'ui-spin' : ''} size={18} />
              <span>
                <strong>{busy ? '正在获取页面画面' : '还没有这一页的画面'}</strong>
                <small>当前环境不能直接内嵌网页，显示的是 Runtime 抓取的页面画面。</small>
              </span>
              <button disabled={Boolean(busy)} onClick={() => void captureSnapshot(selectedTabId)} type="button">重新载入</button>
            </div>
          ) : null}

          {activeAgentTrace && !showHistory && !showSettings ? (
            <div className="paw-browser-agent-field" data-state="active">
              <span aria-hidden="true" className="paw-browser-agent-signal" />
              <span aria-hidden="true" className="paw-browser-agent-counter"><Sparkles size={13} />1</span>
              <section aria-label="Agent 浏览器任务" className="paw-browser-agent-capsule">
                <span aria-hidden="true" className="paw-browser-agent-mark"><Sparkles size={17} /></span>
                <output aria-label="Agent 浏览器任务状态" role="status">
                  <strong>{activeAgentTask}</strong>
                  <span><b>Agent 正在浏览</b><small><b>{browserActionLabel(activeAgentAction) || '处理页面'}</b><span>{activeAgentTarget}</span></small></span>
                </output>
                <div className="paw-browser-agent-capsule-actions">
                  <button aria-label="接管浏览器" onClick={() => void takeOverBrowserWork()} type="button">接管</button>
                  <button aria-label="停止 Agent 浏览器操作" data-danger onClick={() => void stopAgentBrowserWork()} type="button"><Square size={10} />停止</button>
                </div>
              </section>
            </div>
          ) : null}
        </div>
        {showTrace ? (
          <aside className="paw-browser-agent-stream" aria-label="Agent 浏览器轨迹">
            <header>
              <span><Activity size={13} />Agent 轨迹</span>
              <div>
                {activeAgentTrace ? (
                  <button aria-label="停止 Agent 浏览器操作" onClick={() => void stopAgentBrowserWork()} type="button">
                    <Square size={11} /> 停止
                  </button>
                ) : null}
                <button aria-label="隐藏 Agent 浏览器轨迹" onClick={() => setShowTrace(false)} type="button"><X size={13} /></button>
              </div>
            </header>
            <div>
              {traces.length ? traces.map((trace) => (
                <BrowserTraceRow key={text(trace.commandId)} trace={trace} />
              )) : (
                <div className="paw-browser-no-trace">
                  <Bot size={18} />
                  <strong>暂无操作</strong>
                </div>
              )}
            </div>
            <footer>
              <span data-active={Boolean(activeAgentTrace) || undefined} />
              {activeAgentTrace ? 'Agent 正在操作当前浏览器' : '人和 Agent 共用当前页面'}
            </footer>
          </aside>
        ) : null}
      </section>
      </section>
    </>
  );
}

/* Poll payloads are small (≤30 rows); a serialized compare is far cheaper
 * than the App re-render a fresh array identity would force every second. */
function sameRecords(current: BrowserRecord[], next: BrowserRecord[]): boolean {
  return current.length === next.length && JSON.stringify(current) === JSON.stringify(next);
}

function BrowserTraceRow({ trace }: { trace: BrowserRecord }) {
  const [showAllSteps, setShowAllSteps] = useState(false);
  const action = text(trace.action);
  const status = text(trace.status);
  const sourceKind = text(trace.sourceKind);
  const target = text(trace.target);
  const result = record(trace.result);
  const steps = rows(trace.steps);
  const latestStep = steps.at(-1);
  const stepPreviewLimit = 4;
  const visibleSteps = showAllSteps ? steps : steps.slice(-stepPreviewLimit);
  const progress = record(trace.progress);
  const totalSteps = knownCount(trace.totalSteps, trace.stepTotal, progress.totalSteps, progress.total);
  const recordedSteps = knownCount(trace.currentStep, trace.completedSteps, progress.currentStep, progress.completed) ?? steps.length;
  const running = ['queued', 'claimed', 'running'].includes(status);
  const displayedAction = text(latestStep?.action) || action;
  const summary = text(latestStep?.error)
    || text(latestStep?.target)
    || text(result.summary)
    || target
    || (sourceKind === 'agent' ? 'PAW Browser' : '手动操作');
  return (
    <article data-source={sourceKind} data-status={status}>
      <span data-status={status}>
        {status === 'completed' ? <Check size={12} /> : status === 'failed' || status === 'cancelled' ? <CircleX size={12} /> : <Bot size={12} />}
      </span>
      <div>
        <strong>{sourceKind === 'agent' ? `Agent · ${browserActionLabel(displayedAction)}` : `你 · ${browserActionLabel(displayedAction)}`}</strong>
        <small>{summary}</small>
        {visibleSteps.length ? (
          <div className="paw-browser-trace-step-window">
            <div className="paw-browser-trace-step-window__meta">
              <span>{showAllSteps ? `已显示 ${steps.length} / 已加载 ${steps.length} 个步骤` : `最近 ${visibleSteps.length} / 已加载 ${steps.length} 个步骤`}</span>
              {running && totalSteps !== null && totalSteps > 0 ? (
                <span className="paw-browser-trace-progress">
                  <progress aria-label="浏览器执行进度" max={totalSteps} value={Math.min(recordedSteps, totalSteps)} />
                  已记录 {Math.min(recordedSteps, totalSteps)} / 共 {totalSteps} 步
                </span>
              ) : running ? <span>总步数未提供</span> : null}
            </div>
            <ol className="paw-browser-trace-steps">
              {visibleSteps.map((step, index) => (
                <li data-event={text(step.event)} key={`${number(step.atMs)}-${index}`}>
                  <span />
                  <b>{browserActionLabel(text(step.action))}</b>
                  {text(step.target) ? <em>{text(step.target)}</em> : null}
                </li>
              ))}
            </ol>
            {steps.length > stepPreviewLimit ? (
              <button
                aria-label={showAllSteps ? `收起步骤到最近 ${stepPreviewLimit} 项` : `显示全部步骤：${steps.length} 项`}
                className="paw-browser-trace-steps-toggle"
                onClick={() => setShowAllSteps((value) => !value)}
                type="button"
              >
                {showAllSteps ? `收起到最近 ${stepPreviewLimit} 项` : `显示全部 ${steps.length} 项`}
              </button>
            ) : null}
          </div>
        ) : null}
      </div>
    </article>
  );
}

function NativeBrowserWebview({
  active,
  onChange,
  onFoundInPage,
  onGuestState,
  onIdentity,
  onWebview,
  tab,
}: {
  active: boolean;
  onChange(value: { title: string; url: string; webContentsId: number }): void;
  onFoundInPage(match: BrowserFindMatch): void;
  onGuestState(update: Partial<HostBrowserTab>): void;
  onIdentity(webContentsId: number): void;
  onWebview(value: PawBrowserWebview | null): void;
  tab: HostBrowserTab;
}) {
  const ref = useRef<PawBrowserWebview | null>(null);
  const captureWebview = useCallback((webview: PawBrowserWebview | null) => {
    ref.current = webview;
    webview?.setAttribute('allowpopups', 'true');
  }, []);

  useEffect(() => {
    const webview = ref.current;
    if (!webview) return;
    onWebview(webview);
    const publish = () => {
      onChange({
        title: webview.getTitle() || (webview.getURL() === 'about:blank' ? '新标签页' : tab.title),
        url: webview.getURL() || tab.url,
        webContentsId: webview.getWebContentsId(),
      });
      onGuestState(guestNavigationState(webview));
    };
    const ready = () => {
      onIdentity(webview.getWebContentsId());
      publish();
    };
    const startLoading = () => onGuestState({ crashedReason: undefined, failure: null, loading: true });
    const stopLoading = () => onGuestState({ loading: false, ...guestNavigationState(webview) });
    const failLoad = (event: Event) => {
      const failure = event as PawBrowserGuestFailLoadEvent;
      // -3 is ERR_ABORTED: the person or a newer navigation cancelled the load.
      if (!failure.isMainFrame || failure.errorCode === -3) return;
      onGuestState({
        failure: {
          code: failure.errorCode,
          description: failure.errorDescription ?? '',
          url: failure.validatedURL ?? '',
        },
        loading: false,
      });
    };
    const processGone = (event: Event) => onGuestState({
      crashedReason: (event as PawBrowserGuestProcessGoneEvent).reason || 'crashed',
      loading: false,
    });
    const faviconUpdated = (event: Event) => {
      const favicon = (event as PawBrowserGuestFaviconEvent).favicons?.[0];
      if (favicon) onGuestState({ favicon });
    };
    const foundInPage = (event: Event) => {
      const result = (event as PawBrowserGuestFoundInPageEvent).result;
      if (!result) return;
      onFoundInPage({
        activeMatchOrdinal: typeof result.activeMatchOrdinal === 'number' ? result.activeMatchOrdinal : 0,
        matches: typeof result.matches === 'number' ? result.matches : 0,
      });
    };
    webview.addEventListener('dom-ready', ready);
    webview.addEventListener('did-navigate', publish);
    webview.addEventListener('did-navigate-in-page', publish);
    webview.addEventListener('page-title-updated', publish);
    webview.addEventListener('did-start-loading', startLoading);
    webview.addEventListener('did-stop-loading', stopLoading);
    webview.addEventListener('did-fail-load', failLoad);
    webview.addEventListener('render-process-gone', processGone);
    webview.addEventListener('page-favicon-updated', faviconUpdated);
    webview.addEventListener('found-in-page', foundInPage);
    return () => {
      webview.removeEventListener('dom-ready', ready);
      webview.removeEventListener('did-navigate', publish);
      webview.removeEventListener('did-navigate-in-page', publish);
      webview.removeEventListener('page-title-updated', publish);
      webview.removeEventListener('did-start-loading', startLoading);
      webview.removeEventListener('did-stop-loading', stopLoading);
      webview.removeEventListener('did-fail-load', failLoad);
      webview.removeEventListener('render-process-gone', processGone);
      webview.removeEventListener('page-favicon-updated', faviconUpdated);
      webview.removeEventListener('found-in-page', foundInPage);
      onWebview(null);
    };
  }, [onChange, onFoundInPage, onGuestState, onIdentity, onWebview, tab.title, tab.url]);

  return (
    <webview
      aria-label={tab.title || '网页内容'}
      className="paw-browser-native-webview"
      data-active={active || undefined}
      partition={PAW_BROWSER_PARTITION}
      ref={captureWebview}
      src={tab.url}
    />
  );
}
