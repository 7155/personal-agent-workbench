import { createContext, useContext, useMemo, type ReactNode } from 'react';
import type { PawOsDesktopAppId } from './model/app-registry';
import type { PawOsWindowTarget } from './model/desktop';
import { MotionActivityBoundary } from '@/design/motion';

export type PawOsAppSurface = {
  appId: PawOsDesktopAppId;
  active: boolean;
  windowId?: string;
  width: number;
  height: number;
  compact: boolean;
};

export type PawOsAppSurfaceIdentity = Pick<PawOsAppSurface, 'appId' | 'windowId'>;

const PawOsAppSurfaceContext = createContext<PawOsAppSurface | null>(null);
/* Window bounds change for every resize frame, while most PAWOS Apps need
 * only one stable fact. Keep those facts in independent contexts so a live
 * width/height update cannot re-render polling, routing or chrome consumers. */
const PawOsAppIdentityContext = createContext<PawOsAppSurfaceIdentity | null>(null);
const PawOsAppActivityContext = createContext<boolean | null>(null);
const PawOsAppCompactContext = createContext<boolean | null>(null);

export type PawOsWindowRequest = {
  appId: PawOsDesktopAppId;
  target: PawOsWindowTarget;
  background?: boolean;
};

export type PawOsDesktopControls = {
  openWindow: (request: PawOsWindowRequest) => void;
  openApp?: (appId: PawOsDesktopAppId, initialRoute?: string) => void;
  openRoute?: (route: string) => void;
  bindAgentMain?: (
    windowId: string,
    target?: Extract<PawOsWindowTarget, { kind: 'session' | 'room' }>,
  ) => void;
  bindRoomMain?: (target: Extract<PawOsWindowTarget, { kind: 'room' }>) => void;
  /** Desktop-owned focus state; App surfaces may observe but never own it. */
  readonly collaborationFocusGroup?: string | null;
  setCollaborationFocusGroup?: (group: string | null) => void;
  closeWindow?: (windowId: string) => void;
};

const PawOsDesktopContext = createContext<PawOsDesktopControls | null>(null);

export function PawOsDesktopProvider({
  children,
  bindAgentMain,
  bindRoomMain,
  collaborationFocusGroup,
  setCollaborationFocusGroup,
  closeWindow,
  openApp,
  openRoute,
  openWindow,
}: {
  children: ReactNode;
  bindAgentMain?: PawOsDesktopControls['bindAgentMain'];
  bindRoomMain?: PawOsDesktopControls['bindRoomMain'];
  collaborationFocusGroup?: PawOsDesktopControls['collaborationFocusGroup'];
  setCollaborationFocusGroup?: PawOsDesktopControls['setCollaborationFocusGroup'];
  closeWindow?: PawOsDesktopControls['closeWindow'];
  openApp?: PawOsDesktopControls['openApp'];
  openRoute?: PawOsDesktopControls['openRoute'];
  openWindow: PawOsDesktopControls['openWindow'];
}) {
  const value = useMemo(() => ({ bindAgentMain, bindRoomMain, collaborationFocusGroup, setCollaborationFocusGroup, closeWindow, openApp, openRoute, openWindow }), [bindAgentMain, bindRoomMain, collaborationFocusGroup, setCollaborationFocusGroup, closeWindow, openApp, openRoute, openWindow]);
  return <PawOsDesktopContext.Provider value={value}>{children}</PawOsDesktopContext.Provider>;
}

export function PawOsAppSurfaceProvider({
  active = true,
  appId,
  children,
  height,
  windowId,
  width,
}: {
  active?: boolean;
  appId: PawOsDesktopAppId;
  children: ReactNode;
  height: number;
  windowId?: string;
  width: number;
}) {
  const identity = useMemo<PawOsAppSurfaceIdentity>(() => ({ appId, windowId }), [appId, windowId]);
  const compact = width <= 760;
  const value = useMemo<PawOsAppSurface>(() => ({
    active,
    appId,
    windowId,
    width,
    height,
    compact,
  }), [active, appId, compact, height, width, windowId]);

  return (
    <PawOsAppSurfaceContext.Provider value={value}>
      <PawOsAppIdentityContext.Provider value={identity}>
        <PawOsAppActivityContext.Provider value={active}>
          <MotionActivityBoundary active={active}>
            <PawOsAppCompactContext.Provider value={compact}>{children}</PawOsAppCompactContext.Provider>
          </MotionActivityBoundary>
        </PawOsAppActivityContext.Provider>
      </PawOsAppIdentityContext.Provider>
    </PawOsAppSurfaceContext.Provider>
  );
}

export function usePawOsAppSurface(): PawOsAppSurface | null {
  return useContext(PawOsAppSurfaceContext);
}

export function usePawOsAppIdentity(): PawOsAppSurfaceIdentity | null {
  return useContext(PawOsAppIdentityContext);
}

export function usePawOsAppActive(): boolean | null {
  return useContext(PawOsAppActivityContext);
}

/** Keep a visited page's input owner while pausing its reads and motion. */
export function PawOsAppActivityProvider({ active, children }: { active: boolean; children: ReactNode }) {
  const parentActive = usePawOsAppActive();
  const effectiveActive = active && (parentActive ?? true);
  return <PawOsAppActivityContext.Provider value={effectiveActive}>
    <MotionActivityBoundary active={effectiveActive}>{children}</MotionActivityBoundary>
  </PawOsAppActivityContext.Provider>;
}

export function usePawOsAppCompact(): boolean | null {
  return useContext(PawOsAppCompactContext);
}

export function usePawOsDesktop(): PawOsDesktopControls | null {
  return useContext(PawOsDesktopContext);
}

export function openPawOsRoute(desktop: PawOsDesktopControls | null, route: string): void {
  if (desktop?.openRoute) {
    desktop.openRoute(route);
    return;
  }
  window.location.hash = route;
}
