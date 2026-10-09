import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from 'react';
import { MotionConfig } from 'motion/react';
import { usePageVisibility } from '@/platform/use-page-visibility';

export const motionTokens = {
  duration: {
    instant: 0.08,
    press: 0.09,
    fast: 0.14,
    enter: 0.18,
    normal: 0.22,
    panel: 0.22,
    slow: 0.32,
    statusPulse: 0.72,
  },
  distance: { xs: 2, enter: 4, sm: 6, md: 12 },
  easing: {
    standard: [0.22, 1, 0.36, 1] as const,
    press: [0.2, 0.8, 0.3, 1] as const,
    exit: [0.4, 0, 1, 1] as const,
  },
  statusPulse: { iterations: 3 },
} as const;

export type MotionPreference = 'system' | 'reduce' | 'full';

type MotionContextValue = {
  preference: MotionPreference;
  reduceMotion: boolean;
  systemReduceMotion: boolean;
  setPreference: (preference: MotionPreference) => void;
};

const STORAGE_KEY = 'rag-ime-control-motion';
const MotionContext = createContext<MotionContextValue | null>(null);
const MotionActivityContext = createContext(true);

/** Host presentation activity is projected here, never inferred from geometry
 * and never used to pause a task, network owner, or runtime execution. */
export function MotionActivityBoundary({ active, children }: { active: boolean; children: ReactNode }) {
  const parentActive = useContext(MotionActivityContext);
  return <MotionActivityContext.Provider value={parentActive && active}>{children}</MotionActivityContext.Provider>;
}

function parseMotionPreference(value: string | null): MotionPreference {
  return value === 'reduce' || value === 'full' || value === 'system' ? value : 'system';
}

function getStoredPreference(): MotionPreference {
  if (typeof window === 'undefined') return 'system';
  return parseMotionPreference(window.localStorage.getItem(STORAGE_KEY));
}

function getSystemPreference(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false;
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function subscribeSystemPreference(listener: () => void): () => void {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return () => {};
  const media = window.matchMedia('(prefers-reduced-motion: reduce)');
  if (media.addEventListener) media.addEventListener('change', listener);
  else media.addListener?.(listener);
  return () => {
    if (media.removeEventListener) media.removeEventListener('change', listener);
    else media.removeListener?.(listener);
  };
}
const noSystemSubscription = () => () => {};

export function resolveReduceMotion(
  preference: MotionPreference,
  systemReduceMotion: boolean,
): boolean {
  // The operating-system preference is an accessibility floor. An in-app
  // request for full motion only applies when the system permits it.
  return systemReduceMotion || preference === 'reduce';
}

export function MotionProvider({ children }: { children: ReactNode }) {
  const [preference, setPreferenceState] = useState<MotionPreference>(getStoredPreference);
  const systemReduceMotion = useSyncExternalStore(subscribeSystemPreference, getSystemPreference, () => false);
  const reduceMotion = resolveReduceMotion(preference, systemReduceMotion);

  useEffect(() => {
    const handleStorage = (event: StorageEvent) => {
      if ((event.key !== STORAGE_KEY && event.key !== null) || event.storageArea !== window.localStorage) return;
      // Same-origin surfaces share the saved preference, not a task or motion
      // lifecycle. No write-back: storage changes must not bounce between windows.
      setPreferenceState(parseMotionPreference(event.newValue));
    };
    window.addEventListener('storage', handleStorage);
    return () => window.removeEventListener('storage', handleStorage);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.reduceMotion = String(reduceMotion);
    document.documentElement.dataset.motionPreference = preference;
  }, [preference, reduceMotion]);

  const setPreference = useCallback((next: MotionPreference) => {
    setPreferenceState(next);
    window.localStorage.setItem(STORAGE_KEY, next);
  }, []);

  const value = useMemo(
    () => ({ preference, reduceMotion, setPreference, systemReduceMotion }),
    [preference, reduceMotion, setPreference, systemReduceMotion],
  );

  return (
    <MotionContext.Provider value={value}>
      <MotionConfig reducedMotion={reduceMotion ? 'always' : 'never'}>{children}</MotionConfig>
    </MotionContext.Provider>
  );
}

export function useMotionPreference(): MotionContextValue {
  const context = useContext(MotionContext);
  if (!context) throw new Error('useMotionPreference must be used inside MotionProvider');
  return context;
}

/** Decorative motion follows the existing preference owner, including in
 * isolated controls/previews, and never runs in a hidden document. */
export function useMotionActivity(): boolean {
  const context = useContext(MotionContext);
  const surfaceActive = useContext(MotionActivityContext);
  // Hosted controls share their provider's subscription. An isolated preview
  // subscribes itself; the animation library's hook only captures initial state.
  const systemReduced = useSyncExternalStore(context ? noSystemSubscription : subscribeSystemPreference, getSystemPreference, () => false);
  const visible = usePageVisibility();
  // The provider is authoritative. Its DOM projection updates after render and
  // must not keep a newly enabled preference stuck on the previous value.
  const reduced = context ? context.reduceMotion : Boolean(systemReduced)
    || typeof document !== 'undefined' && document.documentElement.dataset.reduceMotion === 'true';
  return visible && surfaceActive && !reduced;
}
