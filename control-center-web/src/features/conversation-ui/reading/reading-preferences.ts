import { useSyncExternalStore } from 'react';
import { useMotionActivity } from '@/design/motion';

export type ReadingPreferences = Readonly<{
  size: 'standard' | 'large';
  spacing: 'comfortable' | 'compact';
  motion: 'system' | 'reduced';
}>;

const STORAGE_KEY = 'paw:conversation-reading:v1';
const DEFAULTS: ReadingPreferences = Object.freeze({ size: 'standard', spacing: 'comfortable', motion: 'system' });
let snapshot: ReadingPreferences = DEFAULTS;
const listeners = new Set<() => void>();

function normalize(value: unknown): ReadingPreferences {
  const input = value && typeof value === 'object' ? value as Record<string, unknown> : {};
  return {
    size: input.size === 'large' ? 'large' : 'standard',
    spacing: input.spacing === 'compact' ? 'compact' : 'comfortable',
    motion: input.motion === 'reduced' ? 'reduced' : 'system',
  };
}
function publish(next: ReadingPreferences) {
  if (snapshot.size === next.size && snapshot.spacing === next.spacing && snapshot.motion === next.motion) return;
  snapshot = next;
  listeners.forEach(listener => listener());
}
function hydrate() {
  if (typeof window === 'undefined') return;
  try { publish(normalize(JSON.parse(window.localStorage.getItem(STORAGE_KEY) || 'null'))); }
  catch { /* Sandboxed previews retain preferences in memory. */ }
}
function onStorage(event: StorageEvent) {
  if (event.key === STORAGE_KEY || event.key === null) hydrate();
}
function subscribe(listener: () => void) {
  listeners.add(listener);
  if (listeners.size === 1 && typeof window !== 'undefined') {
    window.addEventListener('storage', onStorage);
    hydrate();
  }
  return () => {
    listeners.delete(listener);
    if (!listeners.size && typeof window !== 'undefined') window.removeEventListener('storage', onStorage);
  };
}

/** One presentation preference owner. No messages, drafts or model settings are stored here. */
export function useReadingPreferences(): ReadingPreferences {
  return useSyncExternalStore(subscribe, () => snapshot, () => DEFAULTS);
}
export function updateReadingPreferences(change: Partial<ReadingPreferences>) {
  const next = normalize({ ...snapshot, ...change });
  publish(next);
  try { window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next)); }
  catch { /* A storage denial must not disable reading controls. */ }
}

/** This gates decoration only. It must never pause a Runtime or change status labels. */
export function usePresentationMotion(active = true): boolean {
  const preferences = useReadingPreferences();
  const motionActive = useMotionActivity();
  return active && motionActive && preferences.motion !== 'reduced';
}
