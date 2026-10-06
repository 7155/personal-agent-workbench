import { CornerDownLeft, Globe2, Info, LockKeyhole, Search, X } from 'lucide-react';
import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import {
  normalizedAddress,
  omniboxIconKind,
  omniboxIconTitle,
} from '@/paw-os/apps/paw-browser-model';
import './browser-chrome.css';

const GOOGLE_SEARCH_PREFIX = 'https://www.google.com/search?q=';

export type OmniboxCommitPreview =
  | { kind: 'open'; url: string }
  | { kind: 'search'; query: string; url: string };

/**
 * States exactly what pressing Enter will do with the current draft: open one
 * normalized URL or run one web search. Derived from the same normalization
 * the App uses to navigate, so the preview can never differ from the commit.
 */
export function omniboxCommitPreview(address: string, currentUrl: string): OmniboxCommitPreview | null {
  const committed = currentUrl === 'about:blank' ? '' : currentUrl;
  const draft = address.trim();
  if (!draft || draft === committed) return null;
  const url = normalizedAddress(draft);
  if (!url) return null;
  if (url.startsWith(GOOGLE_SEARCH_PREFIX)) return { kind: 'search', query: draft, url };
  return { kind: 'open', url };
}

export type CommittedUrlParts = { scheme: string; host: string; rest: string };

/**
 * Splits a committed http(s) URL for the resting display so the host can be
 * emphasized. Returns null unless the parts reassemble to the exact committed
 * string, which keeps the styled display equal to the real address.
 */
export function committedUrlParts(url: string): CommittedUrlParts | null {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') return null;
  const scheme = `${parsed.protocol}//`;
  const rest = `${parsed.pathname}${parsed.search}${parsed.hash}`;
  if (`${scheme}${parsed.host}${rest}` !== url) return null;
  return { scheme, host: parsed.host, rest };
}

/**
 * Real omnibox with one clear ownership split: the App owns the committed
 * guest URL, this field owns the human draft. While no draft exists the field
 * simply presents the committed address, so guest events (loading, favicon,
 * title, or an Agent navigation) can never clobber half-typed input. The
 * draft drops only on tab switch, Escape, or commit.
 *
 * The leading icon states the truth about the committed page: a lock only for
 * HTTPS, an unencrypted mark for HTTP, and search while a draft differs from
 * the committed URL or nothing is committed yet. While editing, one action
 * row previews the exact URL or search Enter commits; Escape restores the
 * committed address. At rest the committed URL is shown with the host
 * emphasized, without changing the underlying field value. When a tab with no
 * committed page becomes current the caret lands here automatically, the same
 * hand-off a desktop browser makes on a new tab.
 */
export function BrowserOmnibox({
  committedUrl,
  onNavigate,
  tabKey,
}: {
  committedUrl: string;
  onNavigate(rawAddress: string): void;
  tabKey: string;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const selectAllOnMouseUp = useRef(false);
  const [focused, setFocused] = useState(false);
  // null = no human draft: present the committed URL and follow it truthfully.
  const [draft, setDraft] = useState<string | null>(null);
  const lastTabKey = useRef(tabKey);
  if (lastTabKey.current !== tabKey) {
    lastTabKey.current = tabKey;
    setDraft(null);
  }

  const committed = committedUrl === 'about:blank' ? '' : committedUrl;
  const value = draft ?? committed;
  const kind = omniboxIconKind(value, committedUrl);
  const preview = focused ? omniboxCommitPreview(value, committedUrl) : null;
  const restingParts = !focused && value && value === committed ? committedUrlParts(committed) : null;

  useEffect(() => {
    if (!focused && draft !== null && draft === committed) setDraft(null);
  }, [committed, draft, focused]);

  // Real-browser caret hand-off: whenever a tab with no committed page becomes
  // current — first open or a fresh new tab — typing starts here immediately.
  // Tabs already showing a page keep focus on the page, and focus is never
  // pulled out of another window or a field the person is typing in.
  const autoFocusedTabKey = useRef<string | null>(null);
  useEffect(() => {
    if (autoFocusedTabKey.current === tabKey) return;
    autoFocusedTabKey.current = tabKey;
    if (committed) return;
    const input = inputRef.current;
    if (!input) return;
    const active = document.activeElement;
    if (active instanceof HTMLElement) {
      const ownShell = input.closest('.paw-window-shell');
      const activeShell = active.closest('.paw-window-shell');
      if (activeShell && activeShell !== ownShell) return;
      if (active.matches('input, textarea, select, [contenteditable="true"]')) return;
      if (active.matches('[role="tab"]')) return;
    }
    input.focus();
  }, [committed, tabKey]);

  const commit = () => {
    onNavigate(value);
    setDraft(null);
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    commit();
  };

  const keyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key !== 'Escape') return;
    event.preventDefault();
    if (draft === null || draft === committed) {
      inputRef.current?.blur();
      return;
    }
    setDraft(null);
  };

  // First pointer click selects the whole address; a second click keeps the
  // caret where the person placed it. Keyboard focus always selects all.
  const pointerDown = () => {
    if (document.activeElement !== inputRef.current) selectAllOnMouseUp.current = true;
  };
  const focus = () => {
    setFocused(true);
    if (!selectAllOnMouseUp.current) inputRef.current?.select();
  };
  const mouseUp = () => {
    if (!selectAllOnMouseUp.current) return;
    selectAllOnMouseUp.current = false;
    const input = inputRef.current;
    if (input && input.selectionStart === input.selectionEnd) input.select();
  };
  const blur = () => {
    setFocused(false);
    selectAllOnMouseUp.current = false;
  };

  return (
    <form className="paw-omnibox-form" onSubmit={submit}>
      <span
        className={kind === 'lock' ? 'paw-omnibox-icon paw-lock-icon' : 'paw-omnibox-icon'}
        data-kind={kind}
        title={omniboxIconTitle(kind)}
      >
        {kind === 'lock' ? <LockKeyhole size={13} /> : kind === 'info' ? <Info size={13} /> : <Search size={13} />}
      </span>
      <span className="paw-omnibox-field" data-presenting={restingParts ? true : undefined}>
        <input
          aria-label="页面地址"
          onBlur={blur}
          onChange={(event) => setDraft(event.target.value)}
          onFocus={focus}
          onKeyDown={keyDown}
          onMouseUp={mouseUp}
          onPointerDown={pointerDown}
          placeholder="输入网址或搜索内容…"
          ref={inputRef}
          spellCheck={false}
          value={value}
        />
        {restingParts ? (
          <span aria-hidden="true" className="paw-omnibox-presentation">
            <span className="paw-omnibox-url-scheme">{restingParts.scheme}</span>
            <span className="paw-omnibox-url-host">{restingParts.host}</span>
            <span className="paw-omnibox-url-rest">{restingParts.rest}</span>
          </span>
        ) : null}
      </span>
      {value ? (
        <button
          aria-label="清除地址"
          className="paw-omnibox-clear"
          onClick={() => {
            setDraft('');
            inputRef.current?.focus();
          }}
          onMouseDown={(event) => event.preventDefault()}
          type="button"
        >
          <X size={12} />
        </button>
      ) : null}
      {preview ? (
        <div className="paw-omnibox-commit-hint">
          <button
            aria-label={preview.kind === 'search' ? `搜索 ${preview.query}` : `打开 ${preview.url}`}
            onClick={commit}
            onMouseDown={(event) => event.preventDefault()}
            type="button"
          >
            <span aria-hidden="true" className="paw-omnibox-commit-icon">
              {preview.kind === 'search' ? <Search size={13} /> : <Globe2 size={13} />}
            </span>
            <span className="paw-omnibox-commit-text">
              <b>{preview.kind === 'search' ? '搜索' : '打开'}</b>
              <span>{preview.kind === 'search' ? preview.query : preview.url}</span>
              {preview.kind === 'search' ? <small>Google</small> : null}
            </span>
            <kbd aria-hidden="true"><CornerDownLeft size={11} />回车</kbd>
          </button>
        </div>
      ) : null}
    </form>
  );
}
