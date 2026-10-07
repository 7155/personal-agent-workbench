import { CircleAlert, Globe2, LoaderCircle, Plus, X } from 'lucide-react';
import { useEffect, useRef, useState, type KeyboardEvent, type MouseEvent } from 'react';
import { useMotionActivity } from '@/design/motion';
import './browser-chrome.css';

export type BrowserTabItem = {
  id: string;
  title: string;
  active: boolean;
  loading?: boolean;
  favicon?: string;
  failed?: boolean;
};

/** One truthful hover identity per tab: its name plus the real load state. */
export function browserTabTooltip(tab: BrowserTabItem): string {
  const name = tab.title || '新标签页';
  if (tab.loading) return `${name}（正在加载）`;
  if (tab.failed) return `${name}（加载失败）`;
  return name;
}

/**
 * Real Browser tab strip. Each row mirrors an actual guest/tab: spinner while
 * the page loads, the page's own favicon once known, and a failure mark when
 * the last main-frame load did not commit. The selected tab keeps the single
 * always-visible close control; background tabs reveal a named close on
 * hover, close on middle click, and the strip follows the tablist keyboard
 * pattern (arrows/Home/End move selection, Delete closes the focused tab).
 *
 * Only tabs scroll: the new-tab control sits outside the scrolling tablist,
 * so it stays reachable however many guests are open, and the tablist itself
 * contains nothing but tabs. Selecting a tab brings it into view.
 */
export function BrowserTabStrip({
  inWindowChrome,
  newTabDisabled,
  onClose,
  onNewTab,
  onSelect,
  tabs,
}: {
  inWindowChrome?: boolean;
  newTabDisabled?: boolean;
  onClose(tabId: string): void;
  onNewTab(): void;
  onSelect(tabId: string): void;
  tabs: BrowserTabItem[];
}) {
  const motionActive = useMotionActivity();
  const tabButtons = useRef(new Map<string, HTMLButtonElement>());
  const newTabRef = useRef<HTMLButtonElement>(null);
  const closingFocusedTabRef = useRef('');
  const activeTabId = tabs.find((tab) => tab.active)?.id ?? '';

  useEffect(() => {
    const active = activeTabId ? tabButtons.current.get(activeTabId) : undefined;
    if (typeof active?.scrollIntoView === 'function') {
      active.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    }
  }, [activeTabId]);

  useEffect(() => {
    const closingId = closingFocusedTabRef.current;
    if (!closingId || tabs.some((tab) => tab.id === closingId)) return;
    closingFocusedTabRef.current = '';
    // An asynchronous close must not take focus back after the user has
    // already moved elsewhere. Removed focused tabs leave focus on body.
    if (document.activeElement !== document.body) return;
    const nextTabId = activeTabId || tabs[0]?.id;
    const nextTab = nextTabId ? tabButtons.current.get(nextTabId) : undefined;
    (nextTab ?? newTabRef.current)?.focus();
  }, [activeTabId, tabs]);

  const requestClose = (tabId: string) => {
    if (document.activeElement === tabButtons.current.get(tabId)) closingFocusedTabRef.current = tabId;
    onClose(tabId);
  };

  const selectByOffset = (index: number, key: string) => {
    const target = key === 'Home'
      ? tabs[0]
      : key === 'End'
        ? tabs[tabs.length - 1]
        : tabs[key === 'ArrowLeft' ? index - 1 : index + 1];
    if (!target || target.id === tabs[index].id) return;
    onSelect(target.id);
    tabButtons.current.get(target.id)?.focus();
  };

  const tabKeyDown = (index: number) => (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === 'Delete') {
      event.preventDefault();
      requestClose(tabs[index].id);
      return;
    }
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    selectByOffset(index, event.key);
  };

  const middleClickClose = (tabId: string) => (event: MouseEvent<HTMLButtonElement>) => {
    if (event.button !== 1) return;
    event.preventDefault();
    requestClose(tabId);
  };

  return (
    <div className="paw-browser-tabstrip" data-motion-active={motionActive} data-window-chrome={inWindowChrome || undefined}>
      <div aria-label="PAW Browser 标签页" className="paw-browser-tabs-live" role="tablist">
        {tabs.map((tab, index) => (
          <div
            className="paw-browser-tab"
            data-active={tab.active || undefined}
            data-failed={tab.failed || undefined}
            data-loading={tab.loading || undefined}
            key={tab.id}
            role="presentation"
          >
            <button
              aria-busy={tab.loading || undefined}
              aria-keyshortcuts="Delete"
              aria-selected={tab.active}
              className="paw-browser-tab-main"
              onAuxClick={middleClickClose(tab.id)}
              onClick={() => onSelect(tab.id)}
              onKeyDown={tabKeyDown(index)}
              ref={(element) => {
                if (element) tabButtons.current.set(tab.id, element);
                else tabButtons.current.delete(tab.id);
              }}
              role="tab"
              tabIndex={tab.active ? 0 : -1}
              title={browserTabTooltip(tab)}
              type="button"
            >
              <BrowserTabIcon tab={tab} />
              <span>{tab.title || '新标签页'}</span>
            </button>
            <span
              aria-hidden="true"
              className="paw-browser-tab-close"
              data-tab-close-label={tab.active ? '关闭标签页' : tab.title || '新标签页'}
              onClick={(event) => {
                event.stopPropagation();
                requestClose(tab.id);
              }}
              title={tab.active ? '关闭标签页（Delete）' : `关闭标签页：${tab.title || '新标签页'}`}
            >
              <X size={12} />
            </span>
          </div>
        ))}
      </div>
      <button
        aria-label="新建标签页"
        className="paw-browser-new-tab"
        disabled={newTabDisabled}
        onClick={onNewTab}
        ref={newTabRef}
        title="新建标签页"
        type="button"
      >
        <Plus size={14} />
      </button>
    </div>
  );
}

function BrowserTabIcon({ tab }: { tab: BrowserTabItem }) {
  const [brokenFavicon, setBrokenFavicon] = useState('');
  const favicon = tab.favicon && tab.favicon !== brokenFavicon ? tab.favicon : '';
  return (
    <span aria-hidden="true" className="paw-browser-tab-icon">
      {tab.loading ? (
        <LoaderCircle className="ui-spin" size={13} />
      ) : tab.failed ? (
        <CircleAlert size={13} />
      ) : favicon ? (
        <img alt="" onError={() => setBrokenFavicon(favicon)} src={favicon} />
      ) : (
        <Globe2 size={13} />
      )}
    </span>
  );
}
