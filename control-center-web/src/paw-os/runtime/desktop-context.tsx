import { createContext, useContext, useEffect, useRef, type ReactNode } from 'react';
import { useStore } from 'zustand';
import { createPawDesktopStore, type PawDesktopSnapshot, type PawDesktopState, type PawDesktopStore } from './desktop-store';
import { pawApps, type PawAppId } from './app-registry';
import { isLabExtensionAppId } from '../extensions/registry';

const PawDesktopContext = createContext<PawDesktopStore | null>(null);
const pawDesktopSnapshotKey = 'pawos.desktop.v1';
const pawDesktopPersistDelayMs = 200;
const pawAppIds = new Set<PawAppId>(pawApps.map((app) => app.id));

export function PawDesktopProvider({
  children,
  initialAppId,
  initialRoute,
  storageKey = pawDesktopSnapshotKey,
}: {
  children: ReactNode;
  initialAppId?: PawAppId | null;
  initialRoute?: string;
  /** Team scopes provide an isolated desktop snapshot namespace. */
  storageKey?: string;
}) {
  const storeRef = useRef<PawDesktopStore | null>(null);
  storeRef.current ??= createPawDesktopStore(initialAppId, initialRoute, readPawDesktopSnapshot(storageKey));
  useEffect(() => {
    const store = storeRef.current;
    if (!store) return undefined;
    /* Persistence is a trailing debounce, not a per-mutation write: focus
     * churn, geometry commits and open/close bursts each land as one
     * synchronous localStorage/JSON.stringify instead of one per store set —
     * that serialization was interaction-path jank with many windows open.
     * pagehide/hidden flush the pending snapshot so a reload never loses the
     * last 200ms of desktop state. */
    let timer = 0;
    const write = () => {
      timer = 0;
      const state = store.getState();
      const snapshot: PawDesktopSnapshot = {
        windows: state.windows,
        stack: state.stack,
        activeWindowId: state.activeWindowId,
        dockAppIds: state.dockAppIds,
        wayfinder: state.wayfinder,
        collaborationFocusGroup: state.collaborationFocusGroup,
        collaborationFocusReturnWindowId: state.collaborationFocusReturnWindowId,
        dismissedBackgroundToolIds: state.dismissedBackgroundToolIds,
      };
      try {
        window.localStorage.setItem(storageKey, JSON.stringify(snapshot));
      } catch {
        // Persistence is a convenience boundary, never an interaction gate.
        // Quota/private-mode failures leave the live desktop untouched.
      }
    };
    const unsubscribe = store.subscribe(() => {
      if (!timer) timer = window.setTimeout(write, pawDesktopPersistDelayMs);
    });
    const flush = () => {
      if (!timer) return;
      window.clearTimeout(timer);
      write();
    };
    const flushWhenHidden = () => {
      if (document.visibilityState === 'hidden') flush();
    };
    window.addEventListener('pagehide', flush);
    document.addEventListener('visibilitychange', flushWhenHidden);
    return () => {
      unsubscribe();
      window.removeEventListener('pagehide', flush);
      document.removeEventListener('visibilitychange', flushWhenHidden);
      flush();
    };
  }, [storageKey]);
  return <PawDesktopContext.Provider value={storeRef.current}>{children}</PawDesktopContext.Provider>;
}

function readPawDesktopSnapshot(storageKey: string): PawDesktopSnapshot | undefined {
  if (typeof window === 'undefined') return undefined;
  try {
    const value = window.localStorage.getItem(storageKey);
    if (!value) return undefined;
    return sanitizePawDesktopSnapshot(JSON.parse(value));
  } catch {
    return undefined;
  }
}

function sanitizePawDesktopSnapshot(value: unknown): PawDesktopSnapshot | undefined {
  if (!isRecord(value)) return undefined;
  const rawWindows = isRecord(value.windows) ? value.windows : {};
  const windows = Object.fromEntries(Object.entries(rawWindows).filter(([, node]) => validWindowNode(node))) as PawDesktopSnapshot['windows'];
  const stack = Array.isArray(value.stack)
    ? value.stack.filter((id): id is string => typeof id === 'string' && Boolean(windows[id]))
    : [];
  const activeWindowId = typeof value.activeWindowId === 'string' && windows[value.activeWindowId]
    ? value.activeWindowId
    : null;
  const collaborationFocusGroup = typeof value.collaborationFocusGroup === 'string'
    && hasVisibleRoomMain(windows, value.collaborationFocusGroup)
    ? value.collaborationFocusGroup
    : null;
  const collaborationFocusReturnWindowId = collaborationFocusGroup
    && typeof value.collaborationFocusReturnWindowId === 'string'
    && windows[value.collaborationFocusReturnWindowId]
    && !windows[value.collaborationFocusReturnWindowId].minimized
    ? value.collaborationFocusReturnWindowId
    : null;
  const dockAppIds = Array.isArray(value.dockAppIds)
    ? [...new Set(value.dockAppIds.filter((id): id is PawAppId => typeof id === 'string' && (pawAppIds.has(id as PawAppId) || isLabExtensionAppId(id))))]
    : undefined;
  const rawWayfinder = isRecord(value.wayfinder) ? value.wayfinder : {};
  const rawPositions = isRecord(rawWayfinder.iconPositions) ? rawWayfinder.iconPositions : {};
  const iconPositions = Object.fromEntries(Object.entries(rawPositions).flatMap(([id, position]) => {
    if (!isRecord(position)
      || typeof position.x !== 'number'
      || !Number.isFinite(position.x)
      || typeof position.y !== 'number'
      || !Number.isFinite(position.y)) return [];
    return [[id, { x: position.x, y: position.y }]];
  }));
  const rawAssignments = isRecord(rawWayfinder.projectAssignments) ? rawWayfinder.projectAssignments : {};
  const projectAssignments = Object.fromEntries(Object.entries(rawAssignments).filter((entry): entry is [string, string] => typeof entry[1] === 'string'));
  return {
    windows,
    stack,
    activeWindowId,
    collaborationFocusGroup,
    collaborationFocusReturnWindowId,
    dismissedBackgroundToolIds: Array.isArray(value.dismissedBackgroundToolIds) ? value.dismissedBackgroundToolIds.filter((id): id is string => typeof id === 'string').slice(-512) : [],
    ...(dockAppIds !== undefined ? { dockAppIds } : {}),
    wayfinder: {
      ...(rawWayfinder.layoutVersion === 2 || rawWayfinder.layoutVersion === 3
        ? { layoutVersion: rawWayfinder.layoutVersion as 2 | 3 }
        : {}),
      iconPositions,
      archived: Array.isArray(rawWayfinder.archived) ? rawWayfinder.archived.filter((id): id is string => typeof id === 'string') : [],
      projectAssignments,
    },
  };
}

function hasVisibleRoomMain(
  windows: PawDesktopSnapshot['windows'],
  group: string,
): boolean {
  if (!group.startsWith('room:')) return false;
  const roomId = group.slice('room:'.length);
  return Boolean(roomId) && Object.values(windows).some((node) => (
    !node.minimized
    && node.appId === 'agent'
    && node.target?.kind === 'room'
    && !node.target.panel
    && node.target.id === roomId
  ));
}

function validWindowNode(value: unknown): boolean {
  if (!isRecord(value) || !isRecord(value.bounds)) return false;
  return typeof value.id === 'string'
    && typeof value.appId === 'string'
    && typeof value.title === 'string'
    && typeof value.minimized === 'boolean'
    && typeof value.bounds.x === 'number'
    && Number.isFinite(value.bounds.x)
    && typeof value.bounds.y === 'number'
    && Number.isFinite(value.bounds.y)
    && typeof value.bounds.width === 'number'
    && Number.isFinite(value.bounds.width)
    && typeof value.bounds.height === 'number'
    && Number.isFinite(value.bounds.height);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function usePawDesktopStore<T>(selector: (state: PawDesktopState) => T): T {
  const store = useContext(PawDesktopContext);
  if (!store) throw new Error('usePawDesktopStore must be used inside PawDesktopProvider');
  return useStore(store, selector);
}

export function usePawDesktopApi(): PawDesktopStore {
  const store = useContext(PawDesktopContext);
  if (!store) throw new Error('usePawDesktopApi must be used inside PawDesktopProvider');
  return store;
}
