import { FitAddon } from '@xterm/addon-fit';
import { SearchAddon } from '@xterm/addon-search';
import { Terminal as Xterm } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowDown, ArrowUp, ChevronDown, Folder, List, LoaderCircle, Plus, Search, TriangleAlert, X } from 'lucide-react';
import { type KeyboardEvent, useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { usePawOsAppActive } from '@/features/paw-os/surface-context';
import { PawWindowChromePortal, usePawWindowChromeTarget } from '@/paw-os/shell/PawWindowChrome';
import { writeClipboardText } from '@/platform/clipboard';
import { usePageVisibility } from '@/platform/use-page-visibility';
import './paw-os-terminal-app.css';

type TerminalState = 'running' | 'exited' | 'closed';

interface TerminalSession {
  terminalId: string;
  title: string;
  cwd: string;
  shell: string;
  pid: number;
  cols: number;
  rows: number;
  status: TerminalState;
  exitCode: number | null;
  baseCursor: number;
  nextCursor: number;
  createdAtMs: number;
}

interface TerminalListResponse {
  schemaVersion: 'rag-ime.system-terminal.v1';
  ok: boolean;
  items: TerminalSession[];
}

interface TerminalReadResponse {
  schemaVersion: 'rag-ime.system-terminal.v1';
  ok: boolean;
  terminal: TerminalSession;
  cursor: number;
  nextCursor: number;
  truncated: boolean;
  text: string;
}

const terminalKeys = { root: ['system-terminal'] as const };
const emptySessions: TerminalSession[] = [];

// Highlight colours mirror the xterm theme below so search decorations stay
// readable on the dark terminal surface. Backgrounds must be #RRGGBB.
const searchDecorations = {
  matchBackground: '#314036',
  matchOverviewRuler: '#79c56e',
  activeMatchBackground: '#79c56e',
  activeMatchColorOverviewRuler: '#f4f4f5',
};

type ScrollbackSearchResult = { resultIndex: number; resultCount: number };

function searchCountText(result: ScrollbackSearchResult | null): string {
  if (!result) return '';
  if (!result.resultCount) return '无匹配';
  // resultIndex is -1 when matches exceed the highlight limit.
  if (result.resultIndex < 0) return `${result.resultCount}+ 项`;
  return `${result.resultIndex + 1}/${result.resultCount}`;
}

function terminalStateText(session: TerminalSession): string {
  if (session.status === 'running') return '运行中';
  if (session.status === 'closed') return '已关闭';
  return session.exitCode !== null ? `已退出（退出码 ${session.exitCode}）` : '已退出';
}

// Tab-sized state text. A tab that is no longer running says so in words as
// well as colour, but it cannot afford the parenthesised long form.
function terminalStateTag(session: TerminalSession): string {
  if (session.status === 'running') return '';
  if (session.status === 'closed') return '已关闭';
  return session.exitCode !== null ? `已退出 ${session.exitCode}` : '已退出';
}

// A non-zero exit is the one state that earns the danger colour; a clean exit
// and a deliberate close are quiet, not alarming.
function exitFailed(session: TerminalSession): boolean {
  return session.status === 'exited' && session.exitCode !== null && session.exitCode !== 0;
}

// The working-directory basename is the strongest per-tab identity signal the
// backend actually knows; the full path stays in the tooltip and status bar.
function cwdBasename(cwd: string): string {
  return cwd.split('/').filter(Boolean).at(-1) ?? '';
}

interface TabStripOverflow {
  overflowing: boolean;
  atStart: boolean;
  atEnd: boolean;
}

const settledTabStrip: TabStripOverflow = { overflowing: false, atStart: true, atEnd: true };

// PAWOS appearance preference plus the OS media query; xterm's blinking cursor
// is JS-driven, so CSS reduced-motion rules alone cannot silence it.
function prefersReducedMotion(): boolean {
  if (document.documentElement.dataset.reduceMotion === 'true') return true;
  return typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

export function PawOsTerminalApp() {
  const transport = useControlTransport();
  const surfaceActive = usePawOsAppActive() ?? true;
  const pageVisible = usePageVisibility();
  const pollingEnabled = surfaceActive && pageVisible;
  const windowChromeTarget = usePawWindowChromeTarget();
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState('');
  const [cursor, setCursor] = useState(0);
  const [interactionError, setInteractionError] = useState('');
  const [showSearch, setShowSearch] = useState(false);
  const [searchDraft, setSearchDraft] = useState('');
  const [searchResult, setSearchResult] = useState<ScrollbackSearchResult | null>(null);
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [cwdDraft, setCwdDraft] = useState('');
  const [cwdCopied, setCwdCopied] = useState(false);
  const [showTabSwitcher, setShowTabSwitcher] = useState(false);
  const [tabStrip, setTabStrip] = useState<TabStripOverflow>(settledTabStrip);
  const terminalTabsId = useId();
  const terminalHostRef = useRef<HTMLDivElement | null>(null);
  const terminalRef = useRef<Xterm | null>(null);
  const searchAddonRef = useRef<SearchAddon | null>(null);
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const cwdInputRef = useRef<HTMLInputElement | null>(null);
  const cwdCreateToggleRef = useRef<HTMLButtonElement | null>(null);
  const terminalTabRefs = useRef(new Map<string, HTMLButtonElement>());
  const tabStripRef = useRef<HTMLDivElement | null>(null);
  const tabSwitcherToggleRef = useRef<HTMLButtonElement | null>(null);
  const tabSwitcherFirstItemRef = useRef<HTMLButtonElement | null>(null);
  const emptyCreateRef = useRef<HTMLButtonElement | null>(null);
  const initialLoadHandled = useRef(false);
  const restoreTabFocusRef = useRef(false);
  const restoreCwdCreateFocusRef = useRef(false);
  const selectedStatusRef = useRef<TerminalState>('running');
  const createShortcutRef = useRef<() => void>(() => undefined);
  const ordinalShortcutRef = useRef<(ordinal: number) => void>(() => undefined);
  const cwdCopyTimerRef = useRef(0);

  const sessionsQuery = useQuery({
    queryKey: terminalKeys.root,
    enabled: pollingEnabled,
    queryFn: ({ signal }) => transport.request<TerminalListResponse>({ pathId: 'terminal.sessions.list', signal }),
    refetchInterval: pollingEnabled ? 2_000 : false,
  });
  const sessions = sessionsQuery.data?.items ?? emptySessions;
  const selected = sessions.find((item) => item.terminalId === selectedId) ?? null;
  const invalidate = async () => queryClient.invalidateQueries({ queryKey: terminalKeys.root });

  // Sessions created through this App all share the backend title "Terminal";
  // number repeated titles in list order so every tab keeps a distinct identity.
  const tabLabels = useMemo(() => {
    const totals = new Map<string, number>();
    for (const session of sessions) {
      const title = session.title.trim() || '终端';
      totals.set(title, (totals.get(title) ?? 0) + 1);
    }
    const ordinals = new Map<string, number>();
    return new Map(sessions.map((session) => {
      const title = session.title.trim() || '终端';
      const ordinal = (ordinals.get(title) ?? 0) + 1;
      ordinals.set(title, ordinal);
      return [session.terminalId, (totals.get(title) ?? 1) > 1 ? `${title} ${ordinal}` : title];
    }));
  }, [sessions]);

  const create = useMutation({
    mutationFn: (options: { cwd?: string }) => transport.request<{ terminal: TerminalSession }>({
      pathId: 'terminal.session.create',
      body: { title: 'Terminal', cols: 120, rows: 35, ...(options.cwd ? { cwd: options.cwd } : {}) },
    }),
    onSuccess: async (value) => {
      await invalidate();
      setSelectedId(value.terminal.terminalId);
    },
  });

  const close = useMutation({
    mutationFn: (terminalId: string) => transport.request({
      pathId: 'terminal.session.close',
      body: { terminalId },
    }),
    onSuccess: invalidate,
  });

  // The keyboard shortcut runs from inside the xterm key handler, which lives
  // in a mount-scoped effect; route it through a ref so it always reaches the
  // current mutation without re-creating the terminal instance.
  createShortcutRef.current = () => {
    if (!create.isPending) create.mutate({});
  };

  // The numbered chord matches the ordinal every tab shows once the strip is
  // too narrow for labels; an ordinal with no session behind it does nothing.
  ordinalShortcutRef.current = (ordinal: number) => {
    const target = sessions[ordinal - 1];
    if (target) setSelectedId(target.terminalId);
  };

  // A shell is created only when the very first successful load finds no
  // sessions at all. A failed list read proves nothing about existing sessions,
  // and refetches, reconnects, and tab closes never invent a new identity.
  useEffect(() => {
    if (initialLoadHandled.current || !sessionsQuery.data) return;
    initialLoadHandled.current = true;
    if (!sessionsQuery.data.items.length) create.mutate({});
  }, [create, sessionsQuery.data]);

  useEffect(() => {
    setSelectedId((current) => sessions.some((item) => item.terminalId === current) ? current : sessions.at(-1)?.terminalId ?? '');
  }, [sessions]);

  useEffect(() => {
    selectedStatusRef.current = selected?.status ?? 'running';
  }, [selected?.status]);

  // Closing a tab unmounts the focused control; hand focus to the surviving
  // selected tab, or to the empty-state create action when none survive.
  useEffect(() => {
    if (!restoreTabFocusRef.current) return;
    const nextFocus = sessions.length
      ? terminalTabRefs.current.get(selectedId) ?? terminalTabRefs.current.get(sessions.at(-1)!.terminalId)
      : emptyCreateRef.current;
    if (nextFocus) {
      nextFocus.focus();
      restoreTabFocusRef.current = false;
    }
  }, [selectedId, sessions]);

  useEffect(() => {
    setCursor(0);
    setInteractionError('');
    setShowSearch(false);
    setSearchDraft('');
    setSearchResult(null);
    setCwdCopied(false);
    setShowTabSwitcher(false);
  }, [selectedId]);

  // The strip only reports what it can prove: scroll metrics of its own box.
  // Hidden-tab affordances (edge fades, the session switcher) appear exactly
  // while tabs overflow locally and disappear when everything fits again.
  const measureTabStrip = useCallback(() => {
    const strip = tabStripRef.current;
    if (!strip) return;
    const maxScroll = strip.scrollWidth - strip.clientWidth;
    const next: TabStripOverflow = maxScroll > 1
      ? { overflowing: true, atStart: strip.scrollLeft <= 1, atEnd: strip.scrollLeft >= maxScroll - 1 }
      : settledTabStrip;
    setTabStrip((current) =>
      current.overflowing === next.overflowing && current.atStart === next.atStart && current.atEnd === next.atEnd
        ? current
        : next);
  }, []);

  useEffect(() => {
    const strip = tabStripRef.current;
    if (!strip) return;
    measureTabStrip();
    const observer = new ResizeObserver(measureTabStrip);
    observer.observe(strip);
    return () => observer.disconnect();
  }, [measureTabStrip, sessions.length]);

  useEffect(() => {
    if (!tabStrip.overflowing) setShowTabSwitcher(false);
  }, [tabStrip.overflowing]);

  useEffect(() => {
    if (showTabSwitcher) tabSwitcherFirstItemRef.current?.focus();
  }, [showTabSwitcher]);

  useEffect(() => () => window.clearTimeout(cwdCopyTimerRef.current), []);

  useEffect(() => {
    if (showSearch) searchInputRef.current?.focus();
  }, [showSearch]);

  useEffect(() => {
    if (showCreateForm) cwdInputRef.current?.focus();
  }, [showCreateForm]);

  useEffect(() => {
    if (showCreateForm || create.isPending || !restoreCwdCreateFocusRef.current) return;
    cwdCreateToggleRef.current?.focus();
    let settleFrame = 0;
    const focusFrame = window.requestAnimationFrame(() => {
      settleFrame = window.requestAnimationFrame(() => {
        cwdCreateToggleRef.current?.focus();
        restoreCwdCreateFocusRef.current = false;
      });
    });
    return () => {
      window.cancelAnimationFrame(focusFrame);
      if (settleFrame) window.cancelAnimationFrame(settleFrame);
    };
  }, [create.isPending, showCreateForm]);

  // The tab strip scrolls locally; keep the selected identity visible even when
  // selection changes through keyboard navigation or session-list updates.
  useEffect(() => {
    if (!selectedId) return;
    const tab = terminalTabRefs.current.get(selectedId);
    if (tab && typeof tab.scrollIntoView === 'function') tab.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }, [selectedId, sessions.length]);

  useEffect(() => {
    const host = terminalHostRef.current;
    if (!host || !selectedId) return;
    host.replaceChildren();
    const terminal = new Xterm({
      // SearchAddon uses registerDecoration for highlights and match counts.
      // This enables its local renderer API; PTY transport authority is separate.
      allowProposedApi: true,
      convertEol: true,
      cursorBlink: !prefersReducedMotion(),
      cursorStyle: 'bar',
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
      fontSize: 13,
      lineHeight: 1.2,
      scrollback: 20_000,
      theme: {
        background: '#101216', foreground: '#d7dbe2', cursor: '#f4f4f5', selectionBackground: '#79c56e3d',
        black: '#27272a', red: '#f87171', green: '#34d399', yellow: '#fbbf24', blue: '#60a5fa', magenta: '#c084fc', cyan: '#22d3ee', white: '#f4f4f5',
        brightBlack: '#71717a', brightRed: '#fca5a5', brightGreen: '#6ee7b7', brightYellow: '#fde68a', brightBlue: '#93c5fd', brightMagenta: '#d8b4fe', brightCyan: '#67e8f9', brightWhite: '#ffffff',
      },
    });
    const fit = new FitAddon();
    terminal.loadAddon(fit);
    const search = new SearchAddon();
    terminal.loadAddon(search);
    const searchSubscription = search.onDidChangeResults(setSearchResult);
    searchAddonRef.current = search;
    // Ctrl/Cmd combos that must stay in PAWOS instead of reaching the PTY:
    // copy the current selection, open the scrollback search, create a
    // sibling terminal, and jump to a numbered tab without leaving the
    // keyboard.
    terminal.attachCustomKeyEventHandler((event) => {
      if (event.type !== 'keydown') return true;
      const withShell = (event.ctrlKey && event.shiftKey) || (event.metaKey && !event.ctrlKey && !event.altKey);
      if (!withShell) return true;
      if (event.code === 'KeyC' && terminal.hasSelection()) {
        void writeClipboardText(terminal.getSelection()).catch(() => undefined);
        return false;
      }
      if (event.code === 'KeyF') {
        setShowCreateForm(false);
        setShowSearch(true);
        return false;
      }
      if (event.code === 'KeyT') {
        createShortcutRef.current();
        return false;
      }
      const ordinal = /^Digit([1-9])$/.exec(event.code)?.[1];
      if (ordinal) {
        ordinalShortcutRef.current(Number(ordinal));
        return false;
      }
      return true;
    });
    terminal.open(host);
    terminalRef.current = terminal;
    let current = true;
    let pendingInput = '';
    let inputTimer = 0;
    let lastSize = '';

    const flushInput = () => {
      inputTimer = 0;
      const text = pendingInput;
      pendingInput = '';
      if (!text) return;
      if (selectedStatusRef.current !== 'running') {
        if (current) setInteractionError('这个终端已退出，输入没有发送。');
        return;
      }
      void transport.request({ pathId: 'terminal.session.write', body: { terminalId: selectedId, text } })
        .then(() => { if (current) setInteractionError(''); })
        .catch((error: unknown) => { if (current) setInteractionError(publicError(error)); });
    };
    const dataSubscription = terminal.onData((data) => {
      pendingInput += data;
      if (!inputTimer) inputTimer = window.setTimeout(flushInput, 12);
    });
    const fitTerminal = () => {
      fit.fit();
      const size = `${terminal.cols}:${terminal.rows}`;
      if (size === lastSize) return;
      lastSize = size;
      void transport.request({ pathId: 'terminal.session.resize', body: { terminalId: selectedId, cols: terminal.cols, rows: terminal.rows } })
        .catch((error: unknown) => { if (current) setInteractionError(publicError(error)); });
    };
    const observer = new ResizeObserver(fitTerminal);
    observer.observe(host);
    const frame = window.requestAnimationFrame(() => {
      fitTerminal();
      if (restoreCwdCreateFocusRef.current) cwdCreateToggleRef.current?.focus();
      else terminal.focus();
    });

    return () => {
      // Input belongs to its original PTY. Its late write/resize outcome must
      // not replace an error or create a warning on the next selected tab.
      current = false;
      if (inputTimer) window.clearTimeout(inputTimer);
      flushInput();
      window.cancelAnimationFrame(frame);
      observer.disconnect();
      dataSubscription.dispose();
      searchSubscription.dispose();
      terminal.dispose();
      if (searchAddonRef.current === search) searchAddonRef.current = null;
      if (terminalRef.current === terminal) terminalRef.current = null;
    };
  }, [selectedId, transport]);

  const readQuery = useQuery({
    queryKey: [...terminalKeys.root, 'read', selectedId, cursor],
    enabled: pollingEnabled && Boolean(selectedId),
    queryFn: ({ signal }) => transport.request<TerminalReadResponse>({
      pathId: 'terminal.session.read',
      body: { terminalId: selectedId, cursor, maxBytes: 262_144 },
      signal,
    }),
    refetchInterval: pollingEnabled ? (selected?.status === 'running' ? 120 : 1_000) : false,
    retry: false,
  });

  useEffect(() => {
    const chunk = readQuery.data;
    if (!chunk) return;
    const receipt = chunk.terminal;
    selectedStatusRef.current = receipt.status;
    queryClient.setQueryData<TerminalListResponse>(terminalKeys.root, (current) => {
      if (!current) return current;
      const existing = current.items.find((item) => item.terminalId === receipt.terminalId);
      if (
        !existing
        || (existing.status === receipt.status
          && existing.exitCode === receipt.exitCode
          && existing.baseCursor === receipt.baseCursor
          && existing.nextCursor === receipt.nextCursor)
      ) return current;
      return {
        ...current,
        items: current.items.map((item) => item.terminalId === receipt.terminalId
          ? {
              ...item,
              status: receipt.status,
              exitCode: receipt.exitCode,
              baseCursor: receipt.baseCursor,
              nextCursor: receipt.nextCursor,
            }
          : item),
      };
    });
    const terminal = terminalRef.current;
    if (!terminal) return;
    if (chunk.truncated) terminal.reset();
    if (chunk.text) terminal.write(chunk.text);
    if (chunk.nextCursor !== cursor) setCursor(chunk.nextCursor);
  }, [cursor, queryClient, readQuery.data]);

  // One error surface, but each source keeps a truthful recovery: interaction
  // and mutation failures are dismissible (reset), polled query failures offer
  // an immediate retry and clear themselves on the next successful poll.
  const errorNotice: { text: string; dismiss?: () => void; retry?: () => void } | null = interactionError
    ? { text: interactionError, dismiss: () => setInteractionError('') }
    : create.error && !showCreateForm
      ? { text: `新建终端失败：${publicError(create.error)}`, dismiss: () => create.reset() }
      : close.error
        ? { text: `结束终端会话失败：${publicError(close.error)}`, dismiss: () => close.reset() }
        : sessionsQuery.error
          ? { text: `读取终端会话失败：${publicError(sessionsQuery.error)}`, retry: () => void sessionsQuery.refetch() }
          : readQuery.error
            ? { text: `读取终端输出失败：${publicError(readQuery.error)}`, retry: () => void readQuery.refetch() }
            : null;
  const terminalPanelId = `${terminalTabsId}-panel`;
  const selectedTabId = selected ? `${terminalTabsId}-tab-${selected.terminalId}` : undefined;

  const selectTerminalTab = (terminalId: string, focus = false) => {
    setSelectedId(terminalId);
    if (focus) terminalTabRefs.current.get(terminalId)?.focus();
  };

  const runScrollbackSearch = (term: string, direction: 'next' | 'previous', incremental = false) => {
    const addon = searchAddonRef.current;
    if (!addon) return;
    if (!term) {
      addon.clearDecorations();
      setSearchResult(null);
      return;
    }
    const options = { decorations: searchDecorations, ...(direction === 'next' && incremental ? { incremental: true } : {}) };
    if (direction === 'next') addon.findNext(term, options);
    else addon.findPrevious(term, options);
  };

  const closeSearch = () => {
    searchAddonRef.current?.clearDecorations();
    setShowSearch(false);
    setSearchDraft('');
    setSearchResult(null);
    terminalRef.current?.focus();
  };

  const copySelectedCwd = () => {
    if (!selected) return;
    void writeClipboardText(selected.cwd)
      .then(() => {
        setCwdCopied(true);
        window.clearTimeout(cwdCopyTimerRef.current);
        cwdCopyTimerRef.current = window.setTimeout(() => setCwdCopied(false), 1_600);
      })
      .catch(() => setInteractionError('无法访问剪贴板，路径没有复制。'));
  };

  const cwdInvalid = cwdDraft.trim() !== '' && !cwdDraft.trim().startsWith('/');

  const closeCreateForm = () => {
    restoreCwdCreateFocusRef.current = true;
    setShowCreateForm(false);
    setCwdDraft('');
  };

  const submitCreateWithCwd = () => {
    const cwd = cwdDraft.trim();
    if (cwdInvalid || create.isPending) return;
    create.mutate(cwd ? { cwd } : {}, {
      onSuccess: closeCreateForm,
      onError: () => cwdInputRef.current?.focus(),
    });
  };

  const onTerminalTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let nextIndex: number | undefined;
    if (event.key === 'ArrowLeft') nextIndex = (index - 1 + sessions.length) % sessions.length;
    if (event.key === 'ArrowRight') nextIndex = (index + 1) % sessions.length;
    if (event.key === 'Home') nextIndex = 0;
    if (event.key === 'End') nextIndex = sessions.length - 1;
    if (nextIndex === undefined) return;
    event.preventDefault();
    const nextTerminal = sessions[nextIndex];
    if (nextTerminal) selectTerminalTab(nextTerminal.terminalId, true);
  };

  const terminalTabs = (
    <div className="paw-terminal-app__toolbar" data-window-chrome={windowChromeTarget ? true : undefined}>
      <div
        aria-label="PAWOS 终端"
        aria-orientation="horizontal"
        className="paw-terminal-tabs"
        data-at-end={tabStrip.atEnd || undefined}
        data-at-start={tabStrip.atStart || undefined}
        data-overflow={tabStrip.overflowing || undefined}
        onScroll={measureTabStrip}
        onWheel={(event) => {
          if (!event.deltaY || event.deltaX) return;
          event.currentTarget.scrollLeft += event.deltaY;
        }}
        ref={tabStripRef}
        role="tablist"
      >
        {sessions.map((terminal, index) => {
          const active = terminal.terminalId === selectedId;
          const label = tabLabels.get(terminal.terminalId) ?? `终端 ${index + 1}`;
          const stateText = terminalStateText(terminal);
          const running = terminal.status === 'running';
          const tabId = `${terminalTabsId}-tab-${terminal.terminalId}`;
          // Visible text collapses to the leading ordinal once the strip is too
          // narrow, so the accessible name carries the identity and the state
          // instead of depending on a rendered label.
          const tabName = running ? label : `${label} ${stateText}`;
          // A running tab shows where it runs; an ended one shows that it ended.
          const folder = running ? cwdBasename(terminal.cwd) : '';
          return (
            <div
              className="paw-terminal-tab"
              data-exit-failure={exitFailed(terminal) || undefined}
              data-selected={active || undefined}
              data-state={terminal.status}
              key={terminal.terminalId}
              role="presentation"
            >
              <button
                aria-controls={terminalPanelId}
                aria-label={tabName}
                aria-selected={active}
                className="paw-terminal-tab-main"
                id={tabId}
                onClick={() => selectTerminalTab(terminal.terminalId)}
                onKeyDown={(event) => onTerminalTabKeyDown(event, index)}
                ref={(node) => {
                  if (node) terminalTabRefs.current.set(terminal.terminalId, node);
                  else terminalTabRefs.current.delete(terminal.terminalId);
                }}
                role="tab"
                tabIndex={active ? 0 : -1}
                title={`${label} · ${terminal.shell || '/bin/zsh'} · ${terminal.cwd}${running ? '' : ` · ${stateText}`}`}
                type="button"
              >
                <i data-exit-failure={exitFailed(terminal) || undefined} data-state={terminal.status} />
                <b aria-hidden className="paw-terminal-tab-ordinal">{index + 1}</b>
                <span className="paw-terminal-tab-label">{label}</span>
                {folder ? <small aria-hidden className="paw-terminal-tab-cwd">{folder}</small> : null}
                {running ? null : <small aria-hidden className="paw-terminal-tab-exit">{terminalStateTag(terminal)}</small>}
              </button>
              <button
                aria-busy={close.isPending && close.variables === terminal.terminalId ? true : undefined}
                aria-label={`结束终端会话 ${label}`}
                className="paw-tab-close"
                disabled={close.isPending}
                onClick={() => {
                  restoreTabFocusRef.current = true;
                  close.mutate(terminal.terminalId);
                }}
                title={`结束终端会话 ${label}`}
                type="button"
              >
                <X size={11} />
              </button>
            </div>
          );
        })}
      </div>
      {sessions.length ? (
        <div className="paw-terminal-new-group">
          {tabStrip.overflowing ? (
            <button
              aria-expanded={showTabSwitcher}
              aria-haspopup="true"
              aria-label={`列出全部终端（${sessions.length} 个）`}
              className="paw-terminal-tab-new paw-terminal-tabs-overflow"
              onClick={() => {
                setShowSearch(false);
                setShowCreateForm(false);
                setShowTabSwitcher((value) => !value);
              }}
              ref={tabSwitcherToggleRef}
              title="列出全部终端"
              type="button"
            >
              <List size={13} />
            </button>
          ) : null}
          <button aria-busy={create.isPending || undefined} aria-label="新建终端" className="paw-terminal-tab-new" disabled={create.isPending} onClick={() => create.mutate({})} title="新建终端（⌘T / Ctrl+Shift+T）" type="button">
            {create.isPending ? <LoaderCircle className="ui-spin" size={13} /> : <Plus size={13} />}
          </button>
          <button
            aria-expanded={showCreateForm}
            aria-label="在指定目录新建终端"
            className="paw-terminal-tab-new paw-terminal-tab-new--cwd"
            disabled={create.isPending}
            onClick={() => {
              setShowSearch(false);
              setShowCreateForm((value) => !value);
            }}
            ref={cwdCreateToggleRef}
            title="在指定目录新建终端"
            type="button"
          >
            <ChevronDown size={12} />
          </button>
        </div>
      ) : null}
    </div>
  );

  return (
    <>
      {windowChromeTarget ? <PawWindowChromePortal>{terminalTabs}</PawWindowChromePortal> : null}
      <section className="paw-terminal-app" data-error={errorNotice ? true : undefined}>
        <h1 className="sr-only">Terminal</h1>
        {windowChromeTarget ? null : terminalTabs}
        <div className="paw-terminal-app__workspace">
          {errorNotice ? (
            <div className="paw-terminal-error" role="alert">
              <TriangleAlert size={15} />
              <span>{errorNotice.text}</span>
              {errorNotice.retry ? <button className="paw-terminal-error__retry" onClick={errorNotice.retry} type="button">重试</button> : null}
              {errorNotice.dismiss ? (
                <button
                  aria-label="关闭错误提示"
                  onClick={() => {
                    errorNotice.dismiss?.();
                    terminalRef.current?.focus();
                  }}
                  type="button"
                >
                  <X size={13} />
                </button>
              ) : null}
            </div>
          ) : null}
          <section
            aria-label="终端输出"
            aria-labelledby={selectedTabId}
            className="paw-terminal-console"
            data-ended={selected && selected.status !== 'running' ? true : undefined}
            data-search={selected && showSearch ? true : undefined}
            data-session={selected ? true : undefined}
            id={terminalPanelId}
            role={selected ? 'tabpanel' : 'region'}
          >
            {selected && showSearch ? (
              <div className="paw-terminal-search" role="search" onKeyDown={(event) => {
                if (event.key !== 'Escape') return;
                event.preventDefault();
                event.stopPropagation();
                closeSearch();
              }}>
                <Search aria-hidden="true" className="paw-terminal-search__glyph" size={13} />
                <input
                  aria-label="搜索终端输出"
                  onChange={(event) => {
                    setSearchDraft(event.target.value);
                    runScrollbackSearch(event.target.value, 'next', true);
                  }}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') runScrollbackSearch(searchDraft, event.shiftKey ? 'previous' : 'next');
                  }}
                  placeholder="搜索输出"
                  ref={searchInputRef}
                  spellCheck={false}
                  value={searchDraft}
                />
                <span aria-live="polite" className="paw-terminal-search__count">{searchDraft ? searchCountText(searchResult) : ''}</span>
                <button aria-label="上一个匹配" disabled={!searchDraft} onClick={() => runScrollbackSearch(searchDraft, 'previous')} type="button"><ArrowUp size={12} /></button>
                <button aria-label="下一个匹配" disabled={!searchDraft} onClick={() => runScrollbackSearch(searchDraft, 'next')} type="button"><ArrowDown size={12} /></button>
                <button aria-label="关闭搜索" onClick={closeSearch} type="button"><X size={12} /></button>
              </div>
            ) : null}
            {showCreateForm ? (
              <form
                aria-label="在指定目录新建终端"
                className="paw-terminal-create"
                onSubmit={(event) => {
                  event.preventDefault();
                  submitCreateWithCwd();
                }}
              >
                <label>
                  <span>新终端工作目录</span>
                  <input
                    aria-invalid={cwdInvalid || undefined}
                    aria-label="新终端工作目录"
                    onChange={(event) => setCwdDraft(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === 'Escape') closeCreateForm();
                    }}
                    placeholder={selected?.cwd || '/绝对路径，留空使用默认目录'}
                    ref={cwdInputRef}
                    spellCheck={false}
                    value={cwdDraft}
                  />
                </label>
                {cwdInvalid ? <p className="paw-terminal-create__hint" role="alert">请输入以 / 开头的绝对路径。</p> : null}
                {create.error ? <p className="paw-terminal-create__hint" role="alert">新建终端失败：{publicError(create.error)} 输入已保留，请修改目录后重试。</p> : null}
                <div className="paw-terminal-create__actions">
                  <button disabled={create.isPending || cwdInvalid} type="submit">{create.isPending ? '正在创建' : '新建终端'}</button>
                  <button onClick={closeCreateForm} type="button">取消</button>
                </div>
              </form>
            ) : null}
            {showTabSwitcher && sessions.length ? (
              <div
                aria-label="全部终端会话"
                className="paw-terminal-switcher"
                onKeyDown={(event) => {
                  if (event.key !== 'Escape') return;
                  event.stopPropagation();
                  setShowTabSwitcher(false);
                  tabSwitcherToggleRef.current?.focus();
                }}
                role="group"
              >
                <header>全部终端<em>{sessions.length}</em></header>
                <ol>
                  {sessions.map((terminal, index) => {
                    const active = terminal.terminalId === selectedId;
                    const label = tabLabels.get(terminal.terminalId) ?? `终端 ${index + 1}`;
                    return (
                      <li key={terminal.terminalId}>
                        <button
                          aria-current={active || undefined}
                          onClick={() => {
                            setShowTabSwitcher(false);
                            selectTerminalTab(terminal.terminalId, true);
                          }}
                          ref={active || (!selectedId && index === 0) ? tabSwitcherFirstItemRef : undefined}
                          title={`${label} · ${terminal.cwd}`}
                          type="button"
                        >
                          <i data-exit-failure={exitFailed(terminal) || undefined} data-state={terminal.status} />
                          <b aria-hidden className="paw-terminal-switcher__ordinal">{index + 1}</b>
                          <span><strong>{label}</strong><small>{terminal.cwd}</small></span>
                          {terminal.status === 'running' ? null : <em>{terminalStateText(terminal)}</em>}
                        </button>
                      </li>
                    );
                  })}
                </ol>
              </div>
            ) : null}
            {sessionsQuery.isPending ? (
              <div className="paw-terminal-console__empty" data-loading role="status"><LoaderCircle className="ui-spin" size={15} /><p>正在读取终端会话…</p></div>
            ) : sessionsQuery.error && !sessionsQuery.data ? (
              <div className="paw-terminal-console__empty">
                <TriangleAlert aria-hidden="true" size={24} />
                <p>终端会话暂时不可用</p>
                <p className="paw-terminal-console__empty-hint">使用上方的重试读取已有会话，再继续查看输出。</p>
              </div>
            ) : selected ? (
              <div aria-label="终端输入输出" className="paw-terminal-xterm" onClick={() => terminalRef.current?.focus()} ref={terminalHostRef} />
            ) : (
              <div className="paw-terminal-console__empty">
                <span aria-hidden className="paw-terminal-empty-glyph">❯<i /></span>
                <p>还没有终端会话</p>
                <p className="paw-terminal-console__empty-hint">新建一个 PAWOS 内嵌 PTY，直接在这里运行项目命令。</p>
                <button aria-busy={create.isPending || undefined} disabled={create.isPending} onClick={() => create.mutate({})} ref={emptyCreateRef} type="button">{create.isPending ? <LoaderCircle className="ui-spin" size={14} /> : <Plus size={14} />}{create.isPending ? '正在创建' : '新建终端'}</button>
              </div>
            )}
            {selected && selected.status !== 'running' ? (
              <div className="paw-terminal-ended" data-failure={exitFailed(selected) || undefined} role="status">
                <span className="paw-terminal-ended__text">这个终端会话{terminalStateText(selected)}。输出仍可回看，输入不会再发送。</span>
                <span className="paw-terminal-ended__actions">
                  <button aria-busy={create.isPending || undefined} disabled={create.isPending} onClick={() => create.mutate({})} type="button">新建终端</button>
                  <button
                    aria-busy={close.isPending && close.variables === selected.terminalId ? true : undefined}
                    disabled={close.isPending}
                    onClick={() => {
                      restoreTabFocusRef.current = true;
                      close.mutate(selected.terminalId);
                    }}
                    type="button"
                  >
                    关闭此标签页
                  </button>
                </span>
              </div>
            ) : null}
            {selected ? (
              // The status band reads left to right in order of how much the
              // truth costs to be wrong: live state, where the shell runs,
              // then the process facts that only ever confirm it.
              <footer
                className="paw-terminal-statusbar"
                data-failure={exitFailed(selected) || undefined}
                data-state={selected.status}
              >
                <span className="paw-terminal-state-tag">
                  {selected.status === 'running'
                    ? <span className="paw-terminal-running-badge"><i aria-hidden="true" />运行中</span>
                    : <span className="paw-terminal-exited-badge">{selected.status === 'closed' ? '已关闭' : `已退出${selected.exitCode !== null ? ` (${selected.exitCode})` : ''}`}</span>}
                </span>
                <button
                  aria-label={`复制工作目录 ${selected.cwd}`}
                  className="paw-terminal-cwd"
                  data-copied={cwdCopied || undefined}
                  onClick={copySelectedCwd}
                  title={`${selected.cwd}（点击复制路径）`}
                  type="button"
                >
                  <Folder size={11} />
                  <span>{cwdCopied ? '已复制路径' : selected.cwd}</span>
                </button>
                <span className="paw-terminal-statusbar__facts">
                  <strong className="paw-terminal-shell" title={selected.shell || '/bin/zsh'}>{selected.shell || '/bin/zsh'}</strong>
                  <i aria-hidden="true" />
                  <span>pid {selected.pid}</span>
                  <i aria-hidden="true" />
                  <span>UTF-8</span>
                  <i aria-hidden="true" />
                  <span>{selected.cols}×{selected.rows}</span>
                </span>
                <button
                  aria-label="搜索终端输出"
                  className="paw-terminal-statusbar__search"
                  data-active={showSearch || undefined}
                  onClick={() => {
                    setShowCreateForm(false);
                    setShowSearch(true);
                  }}
                  title="搜索终端输出（Ctrl+Shift+F / ⌘F）"
                  type="button"
                >
                  <Search size={12} />
                </button>
              </footer>
            ) : null}
          </section>
        </div>
      </section>
    </>
  );
}

function publicError(error: unknown): string {
  return error instanceof Error && error.message.trim() ? error.message : '系统终端暂时不可用。';
}
