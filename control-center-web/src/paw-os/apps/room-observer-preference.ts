import { useCallback, useEffect, useState } from 'react';

function readPreference(key: string): boolean {
  try { return window.localStorage.getItem(key) !== 'off'; }
  catch { return true; }
}

/** Presentation preference only. Does not stop, send, or change Room execution. */
function useRoomPresentationPreference(key: string, changeEvent: string) {
  const [enabled, setEnabled] = useState(() => readPreference(key));
  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key === key || event.key === null) setEnabled(readPreference(key));
    };
    const onChange = (event: Event) => {
      const next = (event as CustomEvent<boolean>).detail;
      if (typeof next === 'boolean') setEnabled(next);
    };
    window.addEventListener('storage', onStorage);
    window.addEventListener(changeEvent, onChange);
    return () => {
      window.removeEventListener('storage', onStorage);
      window.removeEventListener(changeEvent, onChange);
    };
  }, [key, changeEvent]);
  const update = useCallback((next: boolean) => {
    setEnabled(next);
    try { window.localStorage.setItem(key, next ? 'on' : 'off'); }
    catch { /* The current windows remain configurable without persistent storage. */ }
    window.dispatchEvent(new CustomEvent(changeEvent, { detail: next }));
  }, [key, changeEvent]);
  return [enabled, update] as const;
}

export function useRoomObserverAutoOpen() {
  return useRoomPresentationPreference('pawos.room-observer-auto-open.v1', 'pawos:room-observer-auto-open');
}

export function useRoomWorkStatusVisible() {
  return useRoomPresentationPreference('pawos.room-work-status-visible.v1', 'pawos:room-work-status-visible');
}
