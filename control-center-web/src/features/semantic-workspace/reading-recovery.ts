import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { recoveryScope } from './workspace-recovery';

/** UI-only preferences: no messages, work payloads or transport credentials. */
export type RoomRoundReadingPreferences = {
  followingLatest: boolean;
  historicalRoundIds: string[];
  processDisclosure: [string, boolean][];
  expandedRowIds: string[];
};
export type RoomRoundReading = RoomRoundReadingPreferences & {
  rowId?: string;
  offset: number;
  scrollTop: number;
};
const roundReadingKey = (scope: string) => `${scope}:reading:rounds:v2`;
const validId = (value: unknown): value is string => typeof value === 'string' && value.length > 0 && value.length <= 512;
const validIds = (value: unknown): value is string[] => Array.isArray(value) && value.length <= 400 && value.every(validId);

export function readRoomRoundReading(scope?: string): RoomRoundReading | null {
  if (!scope) return null;
  try {
    const raw = localStorage.getItem(roundReadingKey(scope));
    if (!raw) return null;
    const saved = JSON.parse(raw);
    if (!saved || (saved.rowId !== undefined && !validId(saved.rowId))
      || !Number.isFinite(saved.offset) || Math.abs(saved.offset) > 1e7
      || !Number.isFinite(saved.scrollTop) || saved.scrollTop < 0 || saved.scrollTop > 1e8
      || typeof saved.followingLatest !== 'boolean'
      || !validIds(saved.historicalRoundIds) || !validIds(saved.expandedRowIds)
      || !Array.isArray(saved.processDisclosure) || saved.processDisclosure.length > 400
      || !saved.processDisclosure.every((item: unknown) => Array.isArray(item) && item.length === 2 && validId(item[0]) && typeof item[1] === 'boolean')) return null;
    return saved;
  } catch { return null; }
}

/** Restore once on the scoped surface; coalesce scroll writes and flush while its DOM still exists. */
export function useRoomRoundReadingRecovery(
  ref: RefObject<HTMLElement | null>, scope: string | undefined, ready: boolean,
  initial: RoomRoundReading | null, preferences: () => RoomRoundReadingPreferences,
) {
  const currentPreferences = useRef(preferences);
  const hydrated = useRef(false);
  useLayoutEffect(() => { currentPreferences.current = preferences; });
  useLayoutEffect(() => {
    const node = ref.current;
    if (!scope || !ready || !node) return;
    const clamp = (top: number) => Math.max(0, Math.min(top, Math.max(0, node.scrollHeight - node.clientHeight)));
    if (!hydrated.current && initial) {
      const row = Array.from(node.querySelectorAll<HTMLElement>('[data-round-id]')).find(el => el.dataset.roundId === initial.rowId);
      node.scrollTop = initial.followingLatest ? clamp(node.scrollHeight)
        : row ? clamp(node.scrollTop + row.getBoundingClientRect().top - node.getBoundingClientRect().top - initial.offset)
          : clamp(initial.scrollTop);
    }
    hydrated.current = true;
    let timer: number | undefined;
    const save = () => {
      if (timer !== undefined) { window.clearTimeout(timer); timer = undefined; }
      const top = node.getBoundingClientRect().top;
      const row = Array.from(node.querySelectorAll<HTMLElement>('[data-round-id]')).find(el => el.getBoundingClientRect().bottom > top);
      const prefs = currentPreferences.current();
      try {
        localStorage.setItem(roundReadingKey(scope), JSON.stringify({
          rowId: row?.dataset.roundId, offset: row ? row.getBoundingClientRect().top - top : 0,
          scrollTop: node.scrollTop, ...prefs,
          historicalRoundIds: prefs.historicalRoundIds.slice(-400),
          processDisclosure: prefs.processDisclosure.slice(-400), expandedRowIds: prefs.expandedRowIds.slice(-400),
        }));
      } catch { /* Reading and navigation remain available without storage. */ }
    };
    const schedule = () => { if (timer === undefined) timer = window.setTimeout(save, 250); };
    node.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('pagehide', save);
    return () => { save(); node.removeEventListener('scroll', schedule); window.removeEventListener('pagehide', save); };
    // The initial snapshot belongs to this mount, not a later Root/resize.
  }, [ref, scope, ready, initial]);
  return useCallback(() => { hydrated.current = true; }, []);
}

/** The row ID survives inserted messages; pixels are only a fallback. */
export function useRoomReadingRecovery(ref: RefObject<HTMLDivElement | null>, spaceKey: string, ready: boolean, view: string) {
  const transport = useControlTransport();
  const scope = recoveryScope(transport, spaceKey);
  useLayoutEffect(() => {
    const node = ref.current;
    if (!scope || !ready || !node) return;
    const key = `${scope}:reading:${view}`;
    try {
      const raw = localStorage.getItem(key);
      if (raw) {
        const saved = JSON.parse(raw);
        const row = Array.from(node.querySelectorAll<HTMLElement>('[data-room-message-id]')).find(el => el.dataset.roomMessageId === saved.rowId);
        if (row && Number.isFinite(saved.offset)) node.scrollTop += row.getBoundingClientRect().top - node.getBoundingClientRect().top - saved.offset;
        else if (Number.isFinite(saved.scrollTop)) node.scrollTop = Math.max(0, saved.scrollTop);
      }
    } catch { /* Reading remains available when persistence is unavailable. */ }
    const save = () => {
      const top = node.getBoundingClientRect().top;
      const row = Array.from(node.querySelectorAll<HTMLElement>('[data-room-message-id]')).find(el => el.getBoundingClientRect().bottom > top);
      try { localStorage.setItem(key, JSON.stringify({ rowId: row?.dataset.roomMessageId, offset: row ? row.getBoundingClientRect().top - top : 0, scrollTop: node.scrollTop })); } catch { /* Draft notice covers blocked local storage. */ }
    };
    node.addEventListener('scroll', save, { passive: true });
    window.addEventListener('pagehide', save);
    return () => { save(); node.removeEventListener('scroll', save); window.removeEventListener('pagehide', save); };
  }, [ref, scope, ready, view]);
}


type RoomView = 'rounds' | 'conversation' | 'messages' | 'tasks' | 'starfield' | 'timeline';
const validRoomView = (value: string | null | false): value is RoomView =>
  value === 'rounds' || value === 'conversation' || value === 'messages' || value === 'tasks' || value === 'starfield' || value === 'timeline';
const readView = (key: string): RoomView => {
  try {
    const current = key && localStorage.getItem(key + ':view:v2');
    if (validRoomView(current)) return current;
    // v1 wrote "rounds" on first mount even without a user choice. Migrate that
    // automatic presentation to continuous reading; explicit v2 inspection
    // remains recoverable. Execution and reading anchors are untouched.
    const legacy = key && localStorage.getItem(key + ':view');
    if (validRoomView(legacy) && legacy !== 'rounds') return legacy;
  } catch { /* A default view remains usable. */ }
  return 'conversation';
};
export function useRoomViewRecovery(spaceKey: string) {
  const transport = useControlTransport();
  const key = recoveryScope(transport, spaceKey);
  const [state, setState] = useState(() => ({ key, value: readView(key) }));
  const view = state.key === key ? state.value : readView(key);
  const setView = useCallback((value: RoomView) => setState({ key, value }), [key]);
  useEffect(() => {
    if (!key || state.key !== key) return;
    try { localStorage.setItem(key + ':view:v2', state.value); localStorage.setItem(key + ':view', state.value === 'tasks' ? 'timeline' : state.value); } catch { /* No execution depends on this preference. */ }
  }, [key, state]);
  return [view, setView] as const;
}
