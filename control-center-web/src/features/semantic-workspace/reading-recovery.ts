import { useCallback, useEffect, useLayoutEffect, useState, type RefObject } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { recoveryScope } from './workspace-recovery';

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


type RoomView = 'rounds' | 'conversation' | 'messages' | 'starfield' | 'timeline';
const readView = (key: string): RoomView => {
  try {
    const value = key && localStorage.getItem(key + ':view');
    if (value === 'conversation' || value === 'messages' || value === 'starfield' || value === 'timeline') return value;
  } catch { /* A default view remains usable. */ }
  return 'rounds';
};
export function useRoomViewRecovery(spaceKey: string) {
  const transport = useControlTransport();
  const key = recoveryScope(transport, spaceKey);
  const [state, setState] = useState(() => ({ key, value: readView(key) }));
  const view = state.key === key ? state.value : readView(key);
  const setView = useCallback((value: RoomView) => setState({ key, value }), [key]);
  useEffect(() => {
    if (!key || state.key !== key) return;
    try { localStorage.setItem(key + ':view', state.value); } catch { /* No execution depends on this preference. */ }
  }, [key, state]);
  return [view, setView] as const;
}
