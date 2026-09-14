import { createStore, type StoreApi } from 'zustand/vanilla';
import type { PawOsWindowTarget } from '@/features/paw-os/model/desktop';
import { pawApp, pawDockAppIds, type PawAppId } from './app-registry';
import { isPawExtensionAppId } from '../extensions/registry';
import type { PawExtensionAppId } from '../extensions/types';

export type PawWindowBounds = { x: number; y: number; width: number; height: number };
export type PawWindowPlacement = 'maximized' | 'left' | 'right';
export type PawExtensionAppGateStatus = 'loading' | 'ready' | 'unavailable';
export type PawExtensionAppGate = {
  status: PawExtensionAppGateStatus;
  enabledExtensionIds: ReadonlySet<PawExtensionAppId>;
};

/**
 * PAWOS-only Wayfinder decoration state. These coordinates and buckets are a
 * visual desktop projection; they never rename, move, archive or delete the
 * canonical Session/Room records behind the icons.
 */
export type PawWayfinderIconPosition = { x: number; y: number };
export type PawWayfinderState = {
  layoutVersion: 3;
  iconPositions: Record<string, PawWayfinderIconPosition>;
  archived: string[];
  projectAssignments: Record<string, string>;
};

/* A window is an App container with a floor, not a free-floating page. Below
   these sizes the titlebar can no longer hold its three verbs beside an App's
   own chrome, so no gesture, layout or fit may produce a smaller frame. */
export const PAW_WINDOW_MIN_WIDTH = 280;
export const PAW_WINDOW_MIN_HEIGHT = 210;

/* Ordinary window bounds are `.paw-window-layer` coordinates. The menu bar
   sits above that layer (`--paw-menu-h`). The resident Dock remains visually
   above windows, so an ordinary frame stops before its 62px shelf, 12px floor
   offset and 4px air gap. Maximized and collaboration-focus layouts hide the
   Dock and deliberately keep the complete menu-below plane. */
const PAW_MENU_BAR_HEIGHT = 34;
const PAW_WINDOW_AREA_INSET = 8;
const PAW_DOCK_HEIGHT = 62;
const PAW_DOCK_BOTTOM = 12;
const PAW_DOCK_WINDOW_GAP = 4;
const PAW_DOCK_SAFE_BOTTOM = PAW_DOCK_HEIGHT + PAW_DOCK_BOTTOM + PAW_DOCK_WINDOW_GAP;
const PAW_WINDOW_REACHABLE_GRIP_WIDTH = 120;
const EMPTY_EXTENSION_IDS: ReadonlySet<PawExtensionAppId> = new Set<PawExtensionAppId>();

/** Usable size of the window layer itself, in layer coordinates. */
export function pawWindowLayerSize(): { width: number; height: number } {
  const width = typeof window === 'undefined' ? 1280 : window.innerWidth;
  const height = typeof window === 'undefined' ? 800 : window.innerHeight;
  return {
    width: Math.max(PAW_WINDOW_MIN_WIDTH, width),
    height: Math.max(PAW_WINDOW_MIN_HEIGHT, height - PAW_MENU_BAR_HEIGHT),
  };
}

/**
 * The collaboration layer is the whole usable desktop. Its mode bar is
 * inside that layer and collaboration focus hides the resident Dock.
 */
export function pawFocusWindowLayerSize(): { width: number; height: number } {
  const width = typeof window === 'undefined' ? 1280 : window.innerWidth;
  const height = typeof window === 'undefined' ? 800 : window.innerHeight;
  return {
    width: Math.max(PAW_WINDOW_MIN_WIDTH, width),
    height: Math.max(PAW_WINDOW_MIN_HEIGHT, height - PAW_MENU_BAR_HEIGHT),
  };
}

/** The inset rectangle an ordinary desktop window may occupy without putting
 * its bottom controls under the resident Dock's pointer plane. */
export function pawWindowArea(): PawWindowBounds {
  const layer = pawWindowLayerSize();
  return {
    x: PAW_WINDOW_AREA_INSET,
    y: PAW_WINDOW_AREA_INSET,
    width: Math.max(PAW_WINDOW_MIN_WIDTH, layer.width - PAW_WINDOW_AREA_INSET * 2),
    height: Math.max(PAW_WINDOW_MIN_HEIGHT, layer.height - PAW_WINDOW_AREA_INSET - PAW_DOCK_SAFE_BOTTOM),
  };
}

/** Clamp one frame so the whole window — not just its titlebar — stays reachable. */
export function fitPawWindowBounds(bounds: PawWindowBounds, area: PawWindowBounds = pawWindowArea()): PawWindowBounds {
  const width = Math.min(area.width, Math.max(Math.min(PAW_WINDOW_MIN_WIDTH, area.width), bounds.width));
  const height = Math.min(area.height, Math.max(Math.min(PAW_WINDOW_MIN_HEIGHT, area.height), bounds.height));
  return {
    x: Math.min(Math.max(area.x, bounds.x), area.x + area.width - width),
    y: Math.min(Math.max(area.y, bounds.y), area.y + area.height - height),
    width,
    height,
  };
}

/** Keep an ordinary window recoverable without forcing its full width into
 * the OS canvas. A deliberate horizontal drag may leave content partially
 * outside, but at least one 120px titlebar grip remains reachable. Vertically
 * the complete frame stays in the safe area so its final controls cannot fall
 * behind the Dock. Width/height still shrink with the desktop. */
export function fitReachablePawWindowBounds(
  bounds: PawWindowBounds,
  area: PawWindowBounds = pawWindowArea(),
): PawWindowBounds {
  const width = Math.min(area.width, Math.max(Math.min(PAW_WINDOW_MIN_WIDTH, area.width), bounds.width));
  const height = Math.min(area.height, Math.max(Math.min(PAW_WINDOW_MIN_HEIGHT, area.height), bounds.height));
  const horizontalGrip = Math.min(PAW_WINDOW_REACHABLE_GRIP_WIDTH, width);
  return {
    x: Math.min(
      Math.max(area.x - width + horizontalGrip, bounds.x),
      area.x + area.width - horizontalGrip,
    ),
    y: Math.min(Math.max(area.y, bounds.y), area.y + area.height - height),
    width,
    height,
  };
}
export type PawWindowNode = {
  id: string;
  appId: PawAppId;
  title: string;
  entityId?: string;
  initialRoute?: string;
  target?: PawOsWindowTarget;
  bounds: PawWindowBounds;
  restoreBounds?: PawWindowBounds;
  placement?: PawWindowPlacement;
  minimized: boolean;
};

export type PawDesktopState = {
  windows: Record<string, PawWindowNode>;
  stack: string[];
  activeWindowId: string | null;
  dockAppIds: PawAppId[];
  wayfinder: PawWayfinderState;
  collaborationFocusGroup: string | null;
  collaborationFocusReturnWindowId: string | null;
  dismissedBackgroundToolIds: string[];
  launchpadOpen: boolean;
  overviewOpen: boolean;
  extensionAppGate: PawExtensionAppGate;
  openApp: (appId: PawAppId, options?: { background?: boolean; entityId?: string; initialRoute?: string; title?: string; target?: PawOsWindowTarget }) => string;
  setExtensionAppGate: (status: PawExtensionAppGateStatus, enabledExtensionIds: ReadonlySet<PawExtensionAppId>) => void;
  bindAgentMain: (
    windowId: string,
    target?: Extract<PawOsWindowTarget, { kind: 'session' | 'room' }>,
  ) => void;
  bindRoomMain: (target: Extract<PawOsWindowTarget, { kind: 'room' }>) => void;
  closeWindow: (windowId: string) => void;
  closeAppWindows: (appId: PawAppId) => void;
  closeAllWindows: () => void;
  minimizeWindow: (windowId: string) => void;
  focusWindow: (windowId: string) => void;
  commitBounds: (windowId: string, bounds: PawWindowBounds) => void;
  pinDockApp: (appId: PawAppId) => void;
  unpinDockApp: (appId: PawAppId) => void;
  arrangeWayfinderIcons: () => void;
  setWayfinderIconPositions: (positions: Record<string, PawWayfinderIconPosition>) => void;
  setWayfinderIconPosition: (iconId: string, position: PawWayfinderIconPosition) => void;
  setWayfinderArchived: (iconId: string, archived: boolean) => void;
  setWayfinderProjectAssignment: (iconId: string, projectId: string | null) => void;
  fitWindowsToViewport: () => void;
  snapWindow: (windowId: string, placement: PawWindowPlacement) => void;
  toggleMaximize: (windowId: string) => void;
  showWayfinder: () => void;
  setLaunchpadOpen: (open: boolean) => void;
  setOverviewOpen: (open: boolean) => void;
  setCollaborationFocusGroup: (group: string | null) => void;
};

export type PawDesktopStore = StoreApi<PawDesktopState>;
export type PawPersistedWayfinderState = Omit<PawWayfinderState, 'layoutVersion'> & { layoutVersion?: 2 | 3 };
export type PawDesktopSnapshot = Pick<PawDesktopState, 'windows' | 'stack' | 'activeWindowId'> & {
  dockAppIds?: PawAppId[];
  wayfinder?: PawPersistedWayfinderState;
  collaborationFocusGroup?: string | null;
  collaborationFocusReturnWindowId?: string | null;
  dismissedBackgroundToolIds?: string[];
};
export function createPawDesktopStore(initialAppId?: PawAppId | null, initialRoute?: string, snapshot?: PawDesktopSnapshot): PawDesktopStore {
  const initialWindows = extensionGatedWindows(snapshot?.windows ?? {}, EMPTY_EXTENSION_IDS);
  const initialStack = (snapshot?.stack ?? []).filter((windowId) => Boolean(initialWindows[windowId]));
  const initialActiveWindowId = snapshot?.activeWindowId && initialWindows[snapshot.activeWindowId]
    ? snapshot.activeWindowId
    : null;
  const initialFocusGroup = persistedFocusGroup(initialWindows, snapshot?.collaborationFocusGroup);
  const persistedReturnWindowId = snapshot?.collaborationFocusReturnWindowId;
  const initialFocusReturnWindowId = initialFocusGroup
    && typeof persistedReturnWindowId === 'string'
    && initialWindows[persistedReturnWindowId]
    && !initialWindows[persistedReturnWindowId].minimized
    ? persistedReturnWindowId
    : null;
  const initialDockAppIds = snapshot?.dockAppIds ? [...snapshot.dockAppIds] : [...pawDockAppIds];
  let fittedViewport = pawWindowArea();
  const store = createStore<PawDesktopState>((set, get) => ({
    windows: initialWindows,
    stack: initialStack,
    activeWindowId: initialActiveWindowId,
    dockAppIds: initialDockAppIds,
    wayfinder: {
      layoutVersion: 3,
      iconPositions: snapshot?.wayfinder?.layoutVersion === 2 || snapshot?.wayfinder?.layoutVersion === 3
        ? snapshot.wayfinder.iconPositions ?? {}
        : {},
      archived: snapshot?.wayfinder?.archived ?? [],
      projectAssignments: snapshot?.wayfinder?.projectAssignments ?? {},
    },
    collaborationFocusGroup: initialFocusGroup,
    collaborationFocusReturnWindowId: initialFocusReturnWindowId,
    dismissedBackgroundToolIds: snapshot?.dismissedBackgroundToolIds ?? [],
    launchpadOpen: false,
    overviewOpen: false,
    extensionAppGate: { status: 'unavailable', enabledExtensionIds: EMPTY_EXTENSION_IDS },
    openApp(appId, options = {}) {
      if (!canOpenApp(appId, get().extensionAppGate)) return '';
      const windowId = options.entityId ? `${appId}:${options.entityId}` : appId;
      const current = get().windows[windowId];
      if (current) {
        set((state) => {
          return ({
          windows: current.minimized || options.initialRoute !== undefined || options.target !== undefined || options.title !== undefined
            ? {
                ...state.windows,
                [windowId]: {
                  ...current,
                  ...(options.initialRoute !== undefined ? { initialRoute: options.initialRoute } : {}),
                  ...(options.target !== undefined ? { target: options.target } : {}),
                  ...(options.title !== undefined ? { title: options.title } : {}),
                  minimized: false,
                },
              }
            : state.windows,
          activeWindowId: options.background ? state.activeWindowId : windowId,
          stack: options.background
            ? backgroundStack(state.stack, windowId, state.activeWindowId)
            : [...state.stack.filter((id) => id !== windowId), windowId],
          launchpadOpen: false,
          overviewOpen: false,
          ...focusForOpenedWindow(state, options.target ?? current.target, options.background),
          });
        });
        return windowId;
      }
      const currentState = get();
      const participantTarget = options.target?.kind === 'participant' ? options.target : undefined;
      const roomPanelTarget = options.target?.kind === 'room' && options.target.panel ? options.target : undefined;
      const satelliteTarget = participantTarget
        || roomPanelTarget
        || (options.target?.kind === 'subagent' ? options.target : undefined);
      const satelliteIndex = satelliteTarget
        ? Object.values(currentState.windows).filter((window) => (
            satelliteGroup(window.target) === satelliteGroup(satelliteTarget)
          )).length
        : 0;
      const bounds = satelliteTarget
        ? roomParticipantWindowBounds(satelliteIndex)
        : initialWindowBounds(currentState.stack.length);
      const node: PawWindowNode = {
        id: windowId,
        appId,
        title: options.title ?? pawApp(appId).label,
        entityId: options.entityId,
        initialRoute: options.initialRoute,
        target: options.target,
        bounds,
        minimized: false,
      };
      set((state) => {
        const windows = { ...state.windows, [windowId]: node };
        return {
          windows,
          stack: options.background
            ? backgroundStack(state.stack, windowId, state.activeWindowId)
            : [...state.stack, windowId],
          activeWindowId: options.background ? state.activeWindowId : windowId,
          launchpadOpen: false,
          overviewOpen: false,
          ...focusForOpenedWindow(state, options.target, options.background),
        };
      });
      return windowId;
    },
    setExtensionAppGate(status, enabledExtensionIds) {
      set((state) => {
        /* Inventory polling enters `loading` every time it refreshes. That is
         * not an uninstall receipt and must not tear down a running App. Keep
         * the last authoritative enabled set and mounted windows until the
         * refresh produces either a new ready projection or an explicit
         * unavailable result. */
        if (status === 'loading') {
          if (state.extensionAppGate.status === 'loading') return state;
          return {
            extensionAppGate: {
              status,
              enabledExtensionIds: state.extensionAppGate.enabledExtensionIds,
            },
          };
        }
        const nextEnabledIds = status === 'ready'
          ? new Set([...enabledExtensionIds].filter(isPawExtensionAppId))
          : EMPTY_EXTENSION_IDS;
        const gateUnchanged = state.extensionAppGate.status === status
          && sameExtensionIds(state.extensionAppGate.enabledExtensionIds, nextEnabledIds);
        const windows = extensionGatedWindows(state.windows, nextEnabledIds);
        const dockAppIds = state.dockAppIds.filter((appId) => (
          !isPawExtensionAppId(appId) || nextEnabledIds.has(appId)
        ));
        const windowsUnchanged = Object.keys(windows).length === Object.keys(state.windows).length;
        const dockUnchanged = dockAppIds.length === state.dockAppIds.length;
        if (gateUnchanged && windowsUnchanged && dockUnchanged) return state;
        const cleaned = windowsUnchanged ? state : closeWindowsWhere(state, (node) => (
          isPawExtensionAppId(node.appId) && !nextEnabledIds.has(node.appId)
        ));
        return {
          ...cleaned,
          dockAppIds,
          extensionAppGate: { status, enabledExtensionIds: nextEnabledIds },
        };
      });
    },
    bindAgentMain(windowId, target) {
      set((state) => {
        const mainWindow = state.windows[windowId];
        if (!mainWindow || mainWindow.appId !== 'agent' || isAgentSatellite(mainWindow.target)) return state;
        const title = target?.title ?? pawApp('agent').label;
        const initialRoute = target
          ? `/agent?${target.kind === 'room' ? 'room' : 'session'}=${encodeURIComponent(target.id)}`
          : '/agent';
        if (mainWindow.target?.kind === target?.kind
          && mainWindow.target?.id === target?.id
          && mainWindow.title === title
          && mainWindow.initialRoute === initialRoute) return state;
        const previousRoomId = mainWindow.target?.kind === 'room' ? mainWindow.target.id : '';
        const leavingFocusedRoom = previousRoomId
          && state.collaborationFocusGroup === `room:${previousRoomId}`
          && (target?.kind !== 'room' || target.id !== previousRoomId);
        const rebound: PawDesktopState = {
          ...state,
          windows: {
            ...state.windows,
            [windowId]: {
              ...mainWindow,
              initialRoute,
              target,
              title,
            },
          },
        };
        const leftGroup = satelliteOwnerGroup(mainWindow.target);
        const orphaned = leftGroup && leftGroup !== satelliteOwnerGroup(target)
          ? orphanedSatelliteIds(rebound, leftGroup, windowId)
          : new Set<string>();
        if (!orphaned.size) {
          const focusSurvives = !leavingFocusedRoom
            || (state.collaborationFocusGroup !== null
              && hasVisibleFocusOwner(rebound.windows, state.collaborationFocusGroup));
          return {
            windows: rebound.windows,
            ...(leavingFocusedRoom && !focusSurvives ? {
              collaborationFocusGroup: null,
              collaborationFocusReturnWindowId: null,
            } : {}),
          };
        }
        /* 主窗离开这个 Room 就是父窗离场，卫星跟着走；但导航不是「关掉桌面
           总览」这个动作，overview 保持用户自己留下的状态。 */
        return {
          ...closeWindowsWhere(rebound, (node) => orphaned.has(node.id)),
          overviewOpen: state.overviewOpen,
        };
      });
    },
    bindRoomMain(target) {
      set((state) => {
        const mainWindow = findRoomMainCandidate(state, target.id);
        if (!mainWindow) return state;
        if (mainWindow.target?.kind === 'room'
          && mainWindow.target.id === target.id
          && mainWindow.title === target.title
          && mainWindow.target.subtitle === target.subtitle) return state;
        return {
          windows: {
            ...state.windows,
            [mainWindow.id]: { ...mainWindow, target, title: target.title },
          },
        };
      });
    },
    closeWindow(windowId) {
      set((state) => {
        const closing = state.windows[windowId];
        const orphaned = closing
          ? orphanedSatelliteIds(state, satelliteOwnerGroup(closing.target), windowId)
          : new Set<string>();
        const target = closing?.target;
        const dismiss = (target?.kind === 'process-terminal' || target?.kind === 'browser-target') && target.backgroundObserver;
        return {
          ...closeWindowsWhere(state, (node) => node.id === windowId || orphaned.has(node.id)),
          ...(dismiss ? { dismissedBackgroundToolIds: [...new Set([...state.dismissedBackgroundToolIds, target.id])].slice(-512) } : {}),
        };
      });
    },
    closeAppWindows(appId) {
      set((state) => closeWindowsWhere(state, (node) => node.appId === appId));
    },
    closeAllWindows() {
      set((state) => closeWindowsWhere(state, () => true));
    },
    minimizeWindow(windowId) {
      set((state) => {
        const node = state.windows[windowId];
        if (!node) return state;
        const stack = state.stack.filter((id) => id !== windowId);
        const windows = { ...state.windows, [windowId]: { ...node, minimized: true } };
        const collaborationFocusGroup = state.collaborationFocusGroup
          && hasVisibleFocusOwner(windows, state.collaborationFocusGroup)
          ? state.collaborationFocusGroup
          : null;
        return {
          windows,
          stack,
          activeWindowId: stack.at(-1) ?? null,
          collaborationFocusGroup,
          collaborationFocusReturnWindowId: collaborationFocusGroup
            && state.collaborationFocusReturnWindowId
            && windows[state.collaborationFocusReturnWindowId]
            && !windows[state.collaborationFocusReturnWindowId].minimized
            ? state.collaborationFocusReturnWindowId
            : null,
        };
      });
    },
    focusWindow(windowId) {
      const state = get();
      if (state.activeWindowId === windowId) return;
      const node = state.windows[windowId];
      if (!node) return;
      set({
        windows: node.minimized
          ? { ...state.windows, [windowId]: { ...node, minimized: false } }
          : state.windows,
        stack: [...state.stack.filter((id) => id !== windowId), windowId],
        activeWindowId: windowId,
        overviewOpen: false,
      });
    },
    commitBounds(windowId, bounds) {
      set((state) => {
        const node = state.windows[windowId];
        if (!node || sameBounds(node.bounds, bounds)) return state;
        return { windows: { ...state.windows, [windowId]: { ...node, bounds, restoreBounds: undefined, placement: undefined } } };
      });
    },
    pinDockApp(appId) {
      set((state) => state.dockAppIds.includes(appId)
        ? state
        : { dockAppIds: [...state.dockAppIds, appId] });
    },
    unpinDockApp(appId) {
      set((state) => state.dockAppIds.includes(appId)
        ? { dockAppIds: state.dockAppIds.filter((id) => id !== appId) }
        : state);
    },
    arrangeWayfinderIcons() {
      set((state) => Object.keys(state.wayfinder.iconPositions).length
        ? { wayfinder: { ...state.wayfinder, iconPositions: {} } }
        : state);
    },
    setWayfinderIconPositions(positions) {
      set((state) => sameWayfinderIconPositions(state.wayfinder.iconPositions, positions)
        ? state
        : { wayfinder: { ...state.wayfinder, iconPositions: positions } });
    },
    setWayfinderIconPosition(iconId, position) {
      set((state) => {
        const current = state.wayfinder.iconPositions[iconId];
        if (current?.x === position.x && current.y === position.y) return state;
        return {
          wayfinder: {
            ...state.wayfinder,
            iconPositions: { ...state.wayfinder.iconPositions, [iconId]: position },
          },
        };
      });
    },
    setWayfinderArchived(iconId, archived) {
      set((state) => {
        const hasIcon = state.wayfinder.archived.includes(iconId);
        if (hasIcon === archived) return state;
        return {
          wayfinder: {
            ...state.wayfinder,
            archived: archived
              ? [...state.wayfinder.archived, iconId]
              : state.wayfinder.archived.filter((id) => id !== iconId),
          },
        };
      });
    },
    setWayfinderProjectAssignment(iconId, projectId) {
      set((state) => {
        const assignments = { ...state.wayfinder.projectAssignments };
        if (projectId) assignments[iconId] = projectId;
        else delete assignments[iconId];
        if (assignments[iconId] === state.wayfinder.projectAssignments[iconId]
          && Object.keys(assignments).length === Object.keys(state.wayfinder.projectAssignments).length) return state;
        return { wayfinder: { ...state.wayfinder, projectAssignments: assignments } };
      });
    },
    fitWindowsToViewport() {
      const viewport = pawWindowArea();
      const previousViewport = fittedViewport;
      fittedViewport = viewport;
      const fit = (bounds: PawWindowBounds) => {
        // A smaller desktop must not push previously visible content outside.
        // Keep manual partial placement recoverable instead of undoing the drag.
        const wasInside = bounds.x >= previousViewport.x
          && bounds.x + bounds.width <= previousViewport.x + previousViewport.width;
        return viewport.width < previousViewport.width && wasInside
          ? fitPawWindowBounds(bounds, viewport)
          : fitReachablePawWindowBounds(bounds, viewport);
      };
      set((state) => {
        let changed = false;
        const windows = Object.fromEntries(Object.entries(state.windows).map(([windowId, node]) => {
          const bounds = node.placement
            ? placementBounds(node.placement)
            : fit(node.bounds);
          const restoreBounds = node.restoreBounds
            ? fit(node.restoreBounds)
            : undefined;
          if (sameBounds(bounds, node.bounds)
            && ((!restoreBounds && !node.restoreBounds) || (restoreBounds && node.restoreBounds && sameBounds(restoreBounds, node.restoreBounds)))) {
            return [windowId, node];
          }
          changed = true;
          return [windowId, { ...node, bounds, restoreBounds }];
        }));
        return changed ? { windows } : state;
      });
    },
    snapWindow(windowId, placement) {
      set((state) => {
        const node = state.windows[windowId];
        if (!node) return state;
        const restoreBounds = node.restoreBounds ?? node.bounds;
        return {
          windows: {
            ...state.windows,
            [windowId]: {
              ...node,
              bounds: placementBounds(placement),
              restoreBounds,
              placement,
            },
          },
        };
      });
    },
    toggleMaximize(windowId) {
      set((state) => {
        const node = state.windows[windowId];
        if (!node) return state;
        const maximized = node.placement === 'maximized';
        return {
          windows: {
            ...state.windows,
            [windowId]: maximized
              ? { ...node, bounds: node.restoreBounds ?? node.bounds, restoreBounds: undefined, placement: undefined }
              : { ...node, restoreBounds: node.restoreBounds ?? node.bounds, bounds: placementBounds('maximized'), placement: 'maximized' },
          },
        };
      });
    },
    showWayfinder() {
      set({ activeWindowId: null, launchpadOpen: false, overviewOpen: false });
    },
    setLaunchpadOpen(open) { set({ launchpadOpen: open, overviewOpen: false }); },
    setOverviewOpen(open) {
      set({ overviewOpen: open, launchpadOpen: false });
    },
    setCollaborationFocusGroup(group) {
      set((state) => {
        if (group) {
          return {
            collaborationFocusGroup: group,
            collaborationFocusReturnWindowId: state.collaborationFocusGroup
              ? state.collaborationFocusReturnWindowId
              : state.activeWindowId,
          };
        }
        if (!state.collaborationFocusGroup) {
          return { collaborationFocusGroup: null, collaborationFocusReturnWindowId: null };
        }
        const currentGroup = state.collaborationFocusGroup;
        const main = Object.values(state.windows).find((node) => (
          !node.minimized
          && !satelliteGroup(node.target)
          && (
            (currentGroup.startsWith('room:') && node.target?.kind === 'room' && node.target.id === currentGroup.slice(5))
            || (currentGroup.startsWith('session:') && node.target?.kind === 'session' && node.target.id === currentGroup.slice(8))
          )
        ));
        const returnWindow = state.collaborationFocusReturnWindowId
          ? state.windows[state.collaborationFocusReturnWindowId]
          : undefined;
        const restore = returnWindow && !returnWindow.minimized ? returnWindow : main;
        return {
          collaborationFocusGroup: null,
          collaborationFocusReturnWindowId: null,
          ...(restore ? {
            activeWindowId: restore.id,
            stack: [...state.stack.filter((id) => id !== restore.id), restore.id],
          } : {}),
        };
      });
    },
  }));
  if (initialAppId) store.getState().openApp(initialAppId, { initialRoute });
  store.getState().fitWindowsToViewport();
  return store;
}

function sameWayfinderIconPositions(
  left: Readonly<Record<string, PawWayfinderIconPosition>>,
  right: Readonly<Record<string, PawWayfinderIconPosition>>,
): boolean {
  const leftKeys = Object.keys(left);
  const rightKeys = Object.keys(right);
  return leftKeys.length === rightKeys.length && leftKeys.every((id) => (
    right[id]?.x === left[id]?.x && right[id]?.y === left[id]?.y
  ));
}

function canOpenApp(appId: PawAppId, gate: PawExtensionAppGate): boolean {
  if (!isPawExtensionAppId(appId)) return true;
  // A pending refresh retains the last authoritative enabled set. Initial
  // loading has an empty set; ready removal and unavailable clear it.
  return gate.status !== 'unavailable' && gate.enabledExtensionIds.has(appId);
}

function extensionGatedWindows(
  windows: Record<string, PawWindowNode>,
  enabledExtensionIds: ReadonlySet<PawExtensionAppId>,
): Record<string, PawWindowNode> {
  return Object.fromEntries(Object.entries(windows).filter(([, node]) => (
    !isPawExtensionAppId(node.appId) || enabledExtensionIds.has(node.appId)
  )));
}

function sameExtensionIds(
  left: ReadonlySet<PawExtensionAppId>,
  right: ReadonlySet<PawExtensionAppId>,
): boolean {
  if (left.size !== right.size) return false;
  for (const id of right) if (!left.has(id)) return false;
  return true;
}

function initialWindowBounds(offset: number): PawWindowBounds {
  const viewport = pawWindowArea();
  const inset = Math.min(48, (offset % 5) * 16);
  const width = Math.min(viewport.width, 1280, Math.max(480, viewport.width * 0.82));
  const height = Math.min(viewport.height, 860, Math.max(360, viewport.height * 0.82));
  return fitPawWindowBounds({
    x: viewport.x + (viewport.width - width) / 2 + inset,
    y: viewport.y + (viewport.height - height) / 2 - 12 + inset,
    width,
    height,
  }, viewport);
}

function backgroundStack(stack: string[], windowId: string, activeWindowId: string | null): string[] {
  const next = stack.filter((id) => id !== windowId && id !== activeWindowId);
  next.push(windowId);
  if (activeWindowId) next.push(activeWindowId);
  return next;
}

function hasVisibleFocusOwner(windows: Record<string, PawWindowNode>, group: string): boolean {
  if (group.startsWith('room:')) {
    const roomId = group.slice('room:'.length);
    return Boolean(roomId) && Object.values(windows).some((node) => (
      !node.minimized
      && node.appId === 'agent'
      && node.target?.kind === 'room'
      && !node.target.panel
      && node.target.id === roomId
    ));
  }
  if (group.startsWith('session:')) {
    const sessionId = group.slice('session:'.length);
    return Boolean(sessionId) && Object.values(windows).some((node) => (
      !node.minimized
      && node.appId === 'agent'
      && node.target?.kind === 'session'
      && node.target.id === sessionId
    ));
  }
  return false;
}

function persistedFocusGroup(
  windows: Record<string, PawWindowNode>,
  group: string | null | undefined,
): string | null {
  return typeof group === 'string'
    && group.startsWith('room:')
    && hasVisibleFocusOwner(windows, group)
    ? group
    : null;
}

function closeWindowsWhere(
  state: PawDesktopState,
  shouldClose: (node: PawWindowNode) => boolean,
): PawDesktopState | Partial<PawDesktopState> {
  const closing = new Set(
    Object.values(state.windows).filter(shouldClose).map((node) => node.id),
  );
  if (!closing.size) return state;
  const windows = Object.fromEntries(
    Object.entries(state.windows).filter(([windowId]) => !closing.has(windowId)),
  );
  const stack = state.stack.filter((windowId) => !closing.has(windowId));
  const collaborationFocusGroup = state.collaborationFocusGroup
    && hasVisibleFocusOwner(windows, state.collaborationFocusGroup)
    ? state.collaborationFocusGroup
    : null;
  const focusReturnWindowId = collaborationFocusGroup
    && state.collaborationFocusReturnWindowId
    && windows[state.collaborationFocusReturnWindowId]
    && !windows[state.collaborationFocusReturnWindowId].minimized
    ? state.collaborationFocusReturnWindowId
    : null;
  return {
    windows,
    stack,
    activeWindowId: stack.at(-1) ?? null,
    collaborationFocusGroup,
    collaborationFocusReturnWindowId: focusReturnWindowId,
    overviewOpen: false,
  };
}

export function satelliteGroup(target?: PawOsWindowTarget): string {
  if (target?.kind === 'participant') return `room:${target.roomId}`;
  if (target?.kind === 'room' && target.panel) return `room:${target.id}`;
  if (target?.kind === 'subagent') return `session:${target.sessionId}`;
  if ((target?.kind === 'process-terminal' || target?.kind === 'browser-target') && target.roomId) return `room:${target.roomId}`;
  return '';
}

function focusForOpenedWindow(
  state: PawDesktopState,
  target: PawOsWindowTarget | undefined,
  background = false,
): Pick<PawDesktopState, 'collaborationFocusGroup' | 'collaborationFocusReturnWindowId'> {
  const targetGroup = satelliteGroup(target) || satelliteOwnerGroup(target);
  // Opening another App is an explicit navigation. A retained collaboration
  // layout must not cover it; background work and this group's own windows
  // keep the current focus without closing or cancelling any Session.
  const group = background || targetGroup === state.collaborationFocusGroup
    ? state.collaborationFocusGroup : null;
  return {
    collaborationFocusGroup: group,
    collaborationFocusReturnWindowId: group ? state.collaborationFocusReturnWindowId : null,
  };
}

/**
 * 谁是这组卫星的原点：主 Room 窗与主 Session 窗自己就是恒星，卫星（伙伴、
 * Room 面板、subagent）绕着它转，不会再拥有下一层卫星。返回值与
 * `satelliteGroup` 同一套键，所以「父窗」和「它的卫星」永远说同一种话。
 */
function satelliteOwnerGroup(target?: PawOsWindowTarget): string {
  if (target?.kind === 'room' && !target.panel) return `room:${target.id}`;
  if (target?.kind === 'session') return `session:${target.id}`;
  return '';
}

/**
 * 父窗离场后会被留在桌面上的卫星窗。Designer 的合同是「关掉 Room 就顺带
 * 关掉卫星」：恒星没了，行星不该继续飘着。同一个 Room 还开着另一扇主窗时
 * 卫星仍有原点，这时不做级联；无关的 App 窗口从来不在这组键里。
 */
function orphanedSatelliteIds(state: PawDesktopState, group: string, leavingWindowId: string): Set<string> {
  if (!group) return new Set();
  const anotherOwner = Object.values(state.windows).some((node) => (
    node.id !== leavingWindowId && satelliteOwnerGroup(node.target) === group
  ));
  if (anotherOwner) return new Set();
  return new Set(
    Object.values(state.windows)
      .filter((node) => satelliteGroup(node.target) === group)
      .map((node) => node.id),
  );
}

function isAgentSatellite(target?: PawOsWindowTarget): boolean {
  return target?.kind === 'participant'
    || target?.kind === 'subagent'
    || (target?.kind === 'room' && Boolean(target.panel));
}

function findRoomMainCandidate(state: PawDesktopState, roomId: string): PawWindowNode | undefined {
  const candidates = state.stack
    .map((id) => state.windows[id])
    .filter((window): window is PawWindowNode => Boolean(
      window
      && window.appId === 'agent'
      && window.target?.kind !== 'participant'
      && window.target?.kind !== 'subagent'
      && !(window.target?.kind === 'room' && Boolean(window.target.panel)),
    ));
  return candidates.reduce<PawWindowNode | undefined>((best, candidate) => {
    if (!best || roomMainCandidateScore(candidate, roomId, state.activeWindowId) > roomMainCandidateScore(best, roomId, state.activeWindowId)) return candidate;
    return best;
  }, undefined);
}

function roomMainCandidateScore(window: PawWindowNode, roomId: string, activeWindowId: string | null): number {
  if (window.target?.kind === 'room' && window.target.id === roomId && !window.target.panel) return 3;
  if (window.id === activeWindowId) return 2;
  return 1;
}

function roomParticipantWindowBounds(index: number): PawWindowBounds {
  const viewport = pawWindowArea();
  const width = Math.min(viewport.width, 300, Math.max(PAW_WINDOW_MIN_WIDTH, viewport.width * .22));
  const height = Math.min(viewport.height, 240, Math.max(PAW_WINDOW_MIN_HEIGHT, viewport.height * .27));
  const left = viewport.x;
  const center = viewport.x + (viewport.width - width) / 2;
  const right = viewport.x + viewport.width - width;
  const top = viewport.y;
  const middle = viewport.y + (viewport.height - height) / 2;
  const bottom = viewport.y + viewport.height - height;
  // Room admits eight participants. Keep all eight on distinct perimeter
  // slots so the sixth through eighth windows do not cycle back over the
  // first three. Corners stay first to preserve the familiar small-Room
  // composition; edge centers are only occupied as the Room grows.
  const positions = [
    { x: left, y: top },
    { x: right, y: top },
    { x: left, y: bottom },
    { x: right, y: bottom },
    { x: center, y: top },
    { x: center, y: bottom },
    { x: left, y: middle },
    { x: right, y: middle },
  ];
  const position = positions[index % positions.length]!;
  return fitPawWindowBounds({ ...position, width, height }, viewport);
}

function placementBounds(placement: PawWindowPlacement): PawWindowBounds {
  const viewport = pawWindowArea();
  /* Maximize is the one state that deliberately removes the ordinary 8px
     breathing room. It fills the whole menu-below work plane; the Dock can
     then float above it when revealed, exactly like a macOS overlay. */
  if (placement === 'maximized') {
    const layer = pawWindowLayerSize();
    return { x: 0, y: 0, width: layer.width, height: layer.height };
  }
  const gap = 6;
  const width = (viewport.width - gap) / 2;
  return {
    x: placement === 'left' ? viewport.x : viewport.x + width + gap,
    y: viewport.y,
    width,
    height: viewport.height,
  };
}

function sameBounds(left: PawWindowBounds, right: PawWindowBounds): boolean {
  return left.x === right.x && left.y === right.y
    && left.width === right.width && left.height === right.height;
}
