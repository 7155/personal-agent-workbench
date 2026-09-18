import { Archive, ArchiveRestore, ArrowUpRight, Bot, Earth, Grid3X3, LayoutGrid, Maximize2, Minus, PanelLeft, PanelRight, PanelsTopLeft, Pin, PinOff, Settings, X } from 'lucide-react';
import { Fragment, memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type DragEvent, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent, type RefObject } from 'react';
import { ConnectionIndicator } from '@/components/feedback';
import { pawApp, pawAppForPath, pawApps, currentPawApps, type PawAppDefinition, type PawAppId } from '../runtime/app-registry';
import { usePawDesktopApi, usePawDesktopStore } from '../runtime/desktop-context';
import { dockMagnetics } from './dock-magnification';
import { PawAppIcon, PawBrandMark } from './PawAppIcon';
import { PawStellarWallpaper } from './PawStellarBackdrop';
import { PawContextMenu, type PawContextMenuCloseReason, type PawContextMenuItem } from './PawContextMenu';
import { pawDesktopGridEntries, pawDesktopGridPosition, pawDesktopMovePosition, pawDesktopOccupiedPositions, pawDesktopResolvePersistedPositions, pawDesktopSnapPosition, usePawDesktopGridLayout } from './desktop-grid';
import { PawFieldLede } from './PawFieldLede';
import { clampWayfinderIconPosition, PawWayfinderWork, spatialWayfinderTarget, WAYFINDER_DRAG_MIME } from './PawWayfinderWork';
import { PawWindowLayer } from './PawWindowLayer';
import { pawBrowserHost } from '../apps/paw-browser-host';
import { PawBackgroundActivity } from './PawBackgroundActivity';
import { PawNotificationCenter } from './PawNotificationCenter';
import { PawWorkDirectoryProvider } from './PawWorkDirectory';
import { isPawExtensionAppId, pawExtensionApp, pawExtensionApps } from '../extensions/registry';
import { PawExtensionInstallationProvider, usePawExtensionInstallation } from '../extensions/installation';
import { warmPawAppProcess } from '../apps/PawApps';
import {
  PAW_BUILD_COMMIT,
  PAW_PRODUCT_BUILD_LABEL,
} from '@/product-identity';

type PawMenuTarget =
  | { kind: 'desktop' }
  | { kind: 'menubar'; menu: 'app' | 'window' }
  | { kind: 'apps'; appIds: PawAppId[]; label: string }
  | { kind: 'work'; iconIds: string[]; label: string }
  | { kind: 'window'; windowId: string; label: string };

/* The archive is grouped the way the machine is actually laid out: where the
 * work happens (工作), where what came out of it is kept (记忆与知识), what the
 * work reaches for (工具), and what runs underneath (系统). Splitting 工作 from
 * Agent left two lone tiles on their own bands and made the archive read as a
 * half-empty shelf; this order reads left to right — Session, its deposits,
 * its instruments, the floor. */
const LAUNCHPAD_GROUP_ORDER = ['work', 'library', 'tool', 'system'] as const;
const LAUNCHPAD_GROUP_LABEL: Record<(typeof LAUNCHPAD_GROUP_ORDER)[number], string> = {
  work: '工作',
  library: '记忆与知识',
  tool: '工具',
  system: '系统',
};
const CORE_WORK_APP_IDS: readonly PawAppId[] = ['agent', 'eval-lab'];
const DESKTOP_APP_IDS: readonly PawAppId[] = [
  ...CORE_WORK_APP_IDS,
  ...LAUNCHPAD_GROUP_ORDER.flatMap((kind) => (
    pawApps.filter((app) => !isPawExtensionAppId(app.id)
      && !CORE_WORK_APP_IDS.includes(app.id)
      && launchpadGroup(app) === kind).map((app) => app.id)
  )),
];
const PAW_APP_IDS = new Set<PawAppId>(pawApps.map((app) => app.id));
const PAW_DOCK_APP_MIME = 'application/x-paw-dock-app';

type PawMenuState = PawMenuTarget & { x: number; y: number };
type PawSelectionRect = { x: number; y: number; width: number; height: number };
type PawArchiveReceipt = { iconIds: readonly string[]; label: string; operation: 'archive' | 'remove' };

const selectMenuSignature = (state: { windows: Record<string, { id: string; appId: PawAppId; placement?: string }> }) => Object
  .values(state.windows)
  .map((node) => `${node.id}\u0001${node.appId}\u0001${node.placement ?? ''}`)
  .sort()
  .join('\u0000');
const selectNoMenuSignature = () => '';

export function PawDesktop() {
  return (
    <PawWorkDirectoryProvider>
      <PawExtensionInstallationProvider>
        <PawDesktopSurface />
      </PawExtensionInstallationProvider>
    </PawWorkDirectoryProvider>
  );
}

function PawDesktopSurface() {
  const api = usePawDesktopApi();
  const desktopRef = useRef<HTMLDivElement>(null);
  const gridLayout = usePawDesktopGridLayout();
  const installation = usePawExtensionInstallation();
  const initialExtensionRouteConsumed = useRef(false);
  const persistedIconPositions = usePawDesktopStore((state) => state.wayfinder.iconPositions);
  const activeWindowId = usePawDesktopStore((state) => state.activeWindowId);
  const activeAppId = usePawDesktopStore((state) => (
    activeWindowId ? state.windows[activeWindowId]?.appId ?? null : null
  ));
  const activeWindowMaximized = usePawDesktopStore((state) => (
    activeWindowId ? state.windows[activeWindowId]?.placement === 'maximized' : false
  ));
  const launchpadOpen = usePawDesktopStore((state) => state.launchpadOpen);
  const overviewOpen = usePawDesktopStore((state) => state.overviewOpen);
  const collaborationFocusGroup = usePawDesktopStore((state) => state.collaborationFocusGroup);
  const collaborationFocus = Boolean(collaborationFocusGroup);
  /* The wallpaper only spends frames when somebody can actually watch it.
   * The picture itself is a one-time raster; this attribute gates the live
   * pulse overlay: a focused App window, the Launchpad veil, the overview
   * plane and a hidden document all mean the field is covered or unseen, so
   * the pulse driver swallows Runtime/audio pulses outright. Collaboration
   * focus and live drag/resize keep their own suspension rules. */
  const documentHidden = useDocumentHidden();
  const ambientPaused = documentHidden || Boolean(activeWindowId) || launchpadOpen || overviewOpen;
  useLayoutEffect(() => {
    const root = desktopRef.current;
    if (!root || Object.keys(persistedIconPositions).length === 0) return;
    api.getState().setWayfinderIconPositions(pawDesktopResolvePersistedPositions(
      pawDesktopGridEntries(root),
      persistedIconPositions,
      gridLayout.columns,
    ));
  }, [api, gridLayout.columns, persistedIconPositions]);
  useEffect(() => {
    if (typeof window.requestIdleCallback !== 'function') return undefined;
    const handle = window.requestIdleCallback(() => warmPawAppProcess('agent'));
    return () => window.cancelIdleCallback?.(handle);
  }, []);
  useEffect(() => {
    const state = api.getState();
    state.setExtensionAppGate(installation.status, installation.enabledExtensionIds);
  }, [api, installation.enabledExtensionIds, installation.status]);
  useEffect(() => {
    if (!installation.ready || initialExtensionRouteConsumed.current) return;
    /* The initial route arrives before Package inventory can authorize an
     * Extension App, so consume that pending deep-link once when inventory
     * first becomes ready. Later ready -> loading -> ready polling cycles are
     * background state changes, not launch intent: a user-closed App must stay
     * closed until an icon, Dock item or new route explicitly opens it. */
    initialExtensionRouteConsumed.current = true;
    const route = window.location.hash.replace(/^#/, '');
    const app = pawAppForPath(route);
    if (!app || !isPawExtensionAppId(app.id) || !installation.enabledExtensionIds.has(app.id)) return;
    api.getState().openApp(app.id, { initialRoute: route, title: app.label });
  }, [api, installation.enabledExtensionIds, installation.ready]);
  /* Desktop selection is one set of icon ids across both families on the
   * plane: App shortcuts (`app:<id>`), project folders (`project:<id>`) and
   * loose conversation files (`session:<id>` / `room:<id>`). The lasso, icon
   * clicks and the context menu all speak these ids so a rubber-band sweep
   * can select a mixed neighbourhood exactly like the macOS desktop. */
  const [selectedIcons, setSelectedIcons] = useState<ReadonlySet<string>>(() => new Set());
  const [contextMenu, setContextMenu] = useState<PawMenuState | null>(null);
  const [lasso, setLasso] = useState<PawSelectionRect | null>(null);
  const [archiveReceipt, setArchiveReceipt] = useState<PawArchiveReceipt | null>(null);
  const [archiveNoticeVisible, setArchiveNoticeVisible] = useState(false);
  const archiveWorkIcons = useCallback((
    iconIds: readonly string[],
    label: string,
    operation: PawArchiveReceipt['operation'] = 'archive',
  ) => {
    const uniqueIconIds = [...new Set(iconIds.filter(Boolean))];
    if (!uniqueIconIds.length) return;
    uniqueIconIds.forEach((iconId) => api.getState().setWayfinderArchived(iconId, true));
    setArchiveReceipt({ iconIds: uniqueIconIds, label, operation });
    setArchiveNoticeVisible(true);
    setSelectedIcons(new Set());
    setContextMenu(null);
    window.setTimeout(() => document.querySelector<HTMLElement>('.paw-desktop-viewport')?.focus(), 0);
  }, [api]);
  const undoArchive = useCallback(() => {
    if (!archiveReceipt) return;
    archiveReceipt.iconIds.forEach((iconId) => api.getState().setWayfinderArchived(iconId, false));
    const firstIconId = archiveReceipt.iconIds[0];
    setArchiveReceipt(null);
    setArchiveNoticeVisible(false);
    window.setTimeout(() => {
      const restored = Array.from(document.querySelectorAll<HTMLElement>('[data-wayfinder-icon], [data-desktop-app]'))
        .find((element) => (
          element.dataset.wayfinderIcon === firstIconId
          || (element.dataset.desktopApp && `app:${element.dataset.desktopApp}` === firstIconId)
        ));
      restored?.focus();
    }, 0);
  }, [api, archiveReceipt]);
  useEffect(() => {
    if (!archiveReceipt) return undefined;
    const timer = window.setTimeout(() => setArchiveNoticeVisible(false), 8_000);
    return () => window.clearTimeout(timer);
  }, [archiveReceipt]);
  /* Menus and their disabled states need only structural window facts:
   * identity, owning App and placement. Nobody reads them until a menu is
   * actually open, so a closed desktop subscribes to a constant — window
   * opens, closes, snaps and runtime title binds then cost the shell nothing,
   * and no bounds commit can re-render the menu bar, Wayfinder or Dock. */
  const menuSignature = usePawDesktopStore(contextMenu ? selectMenuSignature : selectNoMenuSignature);
  const menuWindows = useMemo(() => menuSignature.split('\u0000').filter(Boolean).map((item) => {
    const [id, appId, placement] = item.split('\u0001') as [string, PawAppId, string];
    return { id, appId, placement };
  }), [menuSignature]);
  const viewportRef = useRef<HTMLElement>(null);
  const menuAppRef = useRef<HTMLButtonElement>(null);
  const menuWindowRef = useRef<HTMLButtonElement>(null);
  const contextMenuOpenerRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      const target = event.target;
      const editing = target instanceof HTMLInputElement
        || target instanceof HTMLTextAreaElement
        || (target instanceof HTMLElement && target.isContentEditable);
      if (!editing && archiveReceipt && (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'z') {
        event.preventDefault();
        undoArchive();
      } else if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setContextMenu(null);
        api.getState().setLaunchpadOpen(!api.getState().launchpadOpen);
      } else if ((event.metaKey || event.ctrlKey) && event.key === ',') {
        event.preventDefault();
        setContextMenu(null);
        const state = api.getState();
        const existingWindowId = [...state.stack].reverse().find((windowId) => state.windows[windowId]?.appId === 'system-settings')
          ?? Object.values(state.windows).find((node) => node.appId === 'system-settings')?.id;
        if (existingWindowId) state.focusWindow(existingWindowId);
        else state.openApp('system-settings', { title: pawApp('system-settings').label });
        state.setLaunchpadOpen(false);
      } else if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'h') {
        event.preventDefault();
        const state = api.getState();
        if (state.activeWindowId) state.minimizeWindow(state.activeWindowId);
      } else if (event.key === 'F5') {
        event.preventDefault();
        setContextMenu(null);
        api.getState().setOverviewOpen(!api.getState().overviewOpen);
      } else if (event.key === 'Escape') {
        setContextMenu(null);
        if (api.getState().launchpadOpen) api.getState().setLaunchpadOpen(false);
        else if (api.getState().overviewOpen) api.getState().setOverviewOpen(false);
        else if (api.getState().collaborationFocusGroup) api.getState().setCollaborationFocusGroup(null);
        else setSelectedIcons((current) => (current.size ? new Set() : current));
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [api, archiveReceipt, undoArchive]);
  useEffect(() => {
    const host = pawBrowserHost();
    if (!host) return undefined;
    const openInsidePaw = (url: string, commandId = '') => {
      const state = api.getState();
      const existing = Object.values(state.windows).find((node) => node.appId === 'browser');
      const entityId = existing?.entityId;
      state.openApp('browser', {
        ...(entityId ? { entityId } : {}),
        target: {
          kind: 'browser-target',
          id: entityId || commandId || `shell-url:${Date.now()}`,
          title: 'Browser',
          sessionId: '',
          toolCallId: '',
          targetId: '',
          provisional: true,
          ...(commandId ? { commandId } : {}),
          url,
        },
      });
    };
    const stopCommand = host.onCommand((command) => openInsidePaw(command.url, command.commandId));
    const stopOpenUrl = host.onOpenUrl((url) => openInsidePaw(url));
    return () => {
      stopCommand();
      stopOpenUrl();
    };
  }, [api]);

  /* Every shell callback below reads live state through api.getState()
   * instead of closing over render-time values, so each is created once.
   * That referential stability is what lets Wayfinder and the Dock be memo
   * leaves: focus changes, lasso frames and context menus re-render this
   * component without re-rendering those subtrees. */
  const openApp = useCallback((appId: PawAppId) => {
    const extension = isPawExtensionAppId(appId) ? pawExtensionApp(appId) : null;
    const state = api.getState();
    if (extension && !installation.enabledExtensionIds.has(extension.id)) {
      const route = `/plugins?packageId=${encodeURIComponent(extension.packageId)}`;
      state.openApp('app-center', { initialRoute: route, title: pawApp('app-center').label });
      state.setLaunchpadOpen(false);
      window.history.replaceState(null, '', `${window.location.search}#${route}`);
      return;
    }
    const existingWindowId = [...state.stack].reverse().find((windowId) => state.windows[windowId]?.appId === appId)
      ?? Object.values(state.windows).find((node) => node.appId === appId)?.id;
    if (existingWindowId) state.focusWindow(existingWindowId);
    else state.openApp(appId, { title: pawApp(appId).label });
    state.setLaunchpadOpen(false);
    // Returning to an existing window keeps its page. The reload entry point
    // must follow that page too, rather than silently pointing at the App home.
    const currentRoute = existingWindowId ? api.getState().windows[existingWindowId]?.initialRoute : undefined;
    const route = currentRoute && pawAppForPath(currentRoute)?.id === appId ? currentRoute : pawApp(appId).route;
    window.history.replaceState(window.history.state, '', `${window.location.search}#${route}`);
  }, [api, installation.enabledExtensionIds]);
  const toggleLaunchpad = useCallback(() => {
    const state = api.getState();
    state.setLaunchpadOpen(!state.launchpadOpen);
  }, [api]);
  const closeLaunchpad = useCallback(() => api.getState().setLaunchpadOpen(false), [api]);
  const toggleOverview = useCallback(() => {
    const state = api.getState();
    state.setOverviewOpen(!state.overviewOpen);
  }, [api]);
  const selectIcon = useCallback((iconId: string, additive: boolean) => {
    setSelectedIcons((current) => {
      if (!additive) return new Set([iconId]);
      const next = new Set(current);
      if (next.has(iconId)) next.delete(iconId);
      else next.add(iconId);
      return next;
    });
  }, []);

  const openContextMenu = useCallback((event: ReactMouseEvent<HTMLElement>) => {
    event.preventDefault();
    event.stopPropagation();
    const target = event.target as HTMLElement;
    const focused = document.activeElement instanceof HTMLElement && document.activeElement !== document.body
      ? document.activeElement
      : null;
    contextMenuOpenerRef.current = target.closest<HTMLElement>('button, summary, a[href], input, [tabindex]')
      ?? focused
      ?? viewportRef.current;
    const appElement = target.closest<HTMLElement>('[data-desktop-app]');
    if (appElement) {
      const appId = appElement.dataset.desktopApp as PawAppId;
      const iconId = `app:${appId}`;
      /* A right-click on an unselected icon selects just it (macOS); one on a
       * selected icon keeps the existing multi-selection and acts on the App
       * members of it. */
      const appIds = (selectedIcons.has(iconId) ? [...selectedIcons] : [iconId])
        .filter((id) => id.startsWith('app:'))
        .map((id) => id.slice(4) as PawAppId);
      if (!selectedIcons.has(iconId)) setSelectedIcons(new Set([iconId]));
      const acted = appIds.length ? appIds : [appId];
      setContextMenu({
        kind: 'apps',
        appIds: acted,
        label: acted.length === 1 ? pawApp(acted[0]!).label : `${acted.length} 个 App`,
        x: event.clientX,
        y: event.clientY,
      });
      return;
    }
    const workElement = target.closest<HTMLElement>('[data-wayfinder-icon]');
    if (workElement) {
      const iconId = workElement.dataset.wayfinderIcon ?? '';
      const acted = (iconId && selectedIcons.has(iconId) ? [...selectedIcons] : [iconId])
        .filter((id) => id.startsWith('project:') || id.startsWith('session:') || id.startsWith('room:'));
      if (iconId && !selectedIcons.has(iconId)) setSelectedIcons(new Set([iconId]));
      const title = workElement.getAttribute('title')?.split(' · ', 1)[0]?.trim() || '工作图标';
      setContextMenu({
        kind: 'work',
        iconIds: acted.length ? acted : [iconId],
        label: acted.length > 1 ? `${acted.length} 个工作图标` : title,
        x: event.clientX,
        y: event.clientY,
      });
      return;
    }
    const windowElement = target.closest<HTMLElement>('[data-paw-window-id]');
    if (windowElement) {
      const windowId = windowElement.dataset.pawWindowId ?? '';
      const node = api.getState().windows[windowId];
      if (node) {
        setContextMenu({ kind: 'window', windowId, label: node.title, x: event.clientX, y: event.clientY });
        return;
      }
    }
    setSelectedIcons(new Set());
    setContextMenu({ kind: 'desktop', x: event.clientX, y: event.clientY });
  }, [api, selectedIcons]);

  /* Rubber-band selection, restored for the whole desktop plane. A left
   * press on bare desktop starts the sweep; icons are measured once at the
   * gesture edge, the band appears only past a 4px travel threshold, Escape
   * or a right-click mid-sweep cancels and restores the starting selection,
   * and Shift/Cmd/Ctrl add the sweep to it. Furniture (masthead, archive
   * tray, context sheet, an open folder window) is marked
   * [data-paw-desktop-ui] and never starts a band. */
  const startLasso = useCallback((event: ReactPointerEvent<HTMLElement>) => {
    if (event.button !== 0) return;
    const target = event.target;
    // Body Portals still bubble through the desktop's React tree. Only the
    // viewport's own DOM can start a selection or capture its pointer.
    if (!(target instanceof Element) || !event.currentTarget.contains(target)) return;
    if (target.closest('button, input, textarea, select, a, summary, [role="button"], [role="menu"], [role="menuitem"], [role="menuitemcheckbox"], [role="menuitemradio"], [role="listbox"], [role="option"], [role="combobox"], [role="dialog"], [data-paw-window-id], [data-paw-text-selection], [data-paw-desktop-ui], [data-wayfinder-icon]')) {
      /* Like the macOS desktop, pressing a window or desktop furniture drops
       * the icon selection; icons themselves handle their own clicks. */
      if (!target.closest('[data-desktop-app], [data-wayfinder-icon]')) {
        setSelectedIcons((current) => (current.size ? new Set() : current));
      }
      return;
    }
    event.preventDefault();
    setContextMenu(null);
    const viewport = viewportRef.current;
    if (!viewport) return;
    const viewportBounds = viewport.getBoundingClientRect();
    const origin = { x: event.clientX, y: event.clientY };
    const additive = event.shiftKey || event.metaKey || event.ctrlKey;
    const baseline = additive ? new Set(selectedIcons) : new Set<string>();
    if (!additive) setSelectedIcons(new Set());
    event.currentTarget.setPointerCapture?.(event.pointerId);
    /* Desktop identities cannot move while the lasso is being drawn, so their
     * boxes are measured once at the gesture edge. Re-reading them per frame
     * forced a synchronous layout of the whole desktop on every sample — the
     * single most expensive thing a rubber-band selection could do. */
    const targets = Array.from(viewport.querySelectorAll<HTMLElement>('[data-desktop-app], [data-wayfinder-icon]'))
      .map((element) => ({
        iconId: element.dataset.desktopApp ? `app:${element.dataset.desktopApp}` : element.dataset.wayfinderIcon ?? '',
        rect: element.getBoundingClientRect(),
      }))
      .filter((candidate) => candidate.iconId);
    let frame = 0;
    let latest: PointerEvent | null = null;
    let dragging = false;
    const apply = () => {
      frame = 0;
      const moveEvent = latest;
      if (!moveEvent) return;
      if (!dragging && Math.hypot(moveEvent.clientX - origin.x, moveEvent.clientY - origin.y) < 4) return;
      dragging = true;
      const left = Math.min(origin.x, moveEvent.clientX);
      const top = Math.min(origin.y, moveEvent.clientY);
      const right = Math.max(origin.x, moveEvent.clientX);
      const bottom = Math.max(origin.y, moveEvent.clientY);
      setLasso({
        x: left - viewportBounds.left,
        y: top - viewportBounds.top,
        width: right - left,
        height: bottom - top,
      });
      const next = new Set(baseline);
      for (const { iconId, rect } of targets) {
        if (rect.left < right && rect.right > left && rect.top < bottom && rect.bottom > top) next.add(iconId);
      }
      // Most frames of a drag cross no new identity; keeping the same Set
      // keeps the Wayfinder out of the frame entirely.
      setSelectedIcons((current) => sameIconSelection(current, next) ? current : next);
    };
    const move = (moveEvent: PointerEvent) => {
      latest = moveEvent;
      if (!frame) frame = window.requestAnimationFrame(apply);
    };
    const teardown = () => {
      if (frame) window.cancelAnimationFrame(frame);
      setLasso(null);
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', finish);
      window.removeEventListener('pointercancel', finish);
      window.removeEventListener('keydown', key, true);
      window.removeEventListener('contextmenu', abortMenu, true);
    };
    const finish = () => {
      apply();
      teardown();
    };
    const cancel = () => {
      if (dragging) setSelectedIcons(baseline);
      teardown();
    };
    const key = (keyEvent: KeyboardEvent) => {
      if (keyEvent.key !== 'Escape') return;
      keyEvent.preventDefault();
      keyEvent.stopPropagation();
      cancel();
    };
    const abortMenu = (menuEvent: MouseEvent) => {
      if (!dragging) return;
      menuEvent.preventDefault();
      menuEvent.stopPropagation();
      cancel();
    };
    window.addEventListener('pointermove', move, { passive: true });
    window.addEventListener('pointerup', finish);
    window.addEventListener('pointercancel', finish);
    window.addEventListener('keydown', key, true);
    window.addEventListener('contextmenu', abortMenu, true);
  }, [selectedIcons]);

  const menuItems = useMemo<readonly PawContextMenuItem[]>(() => {
    if (!contextMenu || contextMenu.kind === 'desktop') {
      const archivedCount = api.getState().wayfinder.archived.length;
      return [
        { id: 'new-agent', label: '新建 Agent 工作', icon: <Bot size={15} />, action: () => openApp('agent') },
        { id: 'browser', label: '打开 Browser', icon: <Earth size={15} />, action: () => openApp('browser') },
        { id: 'launchpad', label: '全部 App', icon: <LayoutGrid size={15} />, shortcut: '⌘K', separatorBefore: true, action: () => api.getState().setLaunchpadOpen(true) },
        {
          id: 'arrange-icons',
          label: '整理图标',
          icon: <Grid3X3 size={15} />,
          action: () => {
            api.getState().arrangeWayfinderIcons();
            setSelectedIcons(new Set());
          },
        },
        {
          id: 'restore-archive',
          label: archivedCount ? `恢复 ${archivedCount} 个桌面图标` : '没有已移除的桌面图标',
          icon: <ArchiveRestore size={15} />,
          disabled: archivedCount === 0,
          action: () => api.getState().wayfinder.archived.forEach((iconId) => api.getState().setWayfinderArchived(iconId, false)),
        },
        { id: 'settings', label: '系统设置', icon: <Settings size={15} />, shortcut: '⌘,', action: () => openApp('system-settings') },
        {
          id: 'close-all-windows',
          label: '关闭全部窗口',
          icon: <X size={15} />,
          danger: true,
          separatorBefore: true,
          disabled: menuWindows.length === 0,
          action: () => api.getState().closeAllWindows(),
        },
      ];
    }
    if (contextMenu.kind === 'work') {
      return [{
        id: 'archive-work',
          label: contextMenu.iconIds.length > 1 ? `移到归档 ${contextMenu.iconIds.length} 个工作图标` : `移到归档 ${contextMenu.label}`,
        icon: <Archive size={15} />,
        action: () => archiveWorkIcons(contextMenu.iconIds, contextMenu.label),
      }];
    }
    if (contextMenu.kind === 'menubar') {
      if (contextMenu.menu === 'window') {
        /* The Window menu is always present like on macOS; with no focused
         * window its verbs simply read disabled instead of disappearing. */
        const activeNode = menuWindows.find((candidate) => candidate.id === activeWindowId);
        return [
          { id: 'minimize', label: '最小化', icon: <Minus size={15} />, shortcut: '⌘H', disabled: !activeNode, action: () => { if (activeNode) api.getState().minimizeWindow(activeNode.id); } },
          { id: 'maximize', label: activeNode?.placement === 'maximized' ? '还原窗口' : '最大化', icon: <Maximize2 size={15} />, disabled: !activeNode, action: () => { if (activeNode) api.getState().toggleMaximize(activeNode.id); } },
          { id: 'snap-left', label: '靠左排列', icon: <PanelLeft size={15} />, disabled: !activeNode, action: () => { if (activeNode) api.getState().snapWindow(activeNode.id, 'left'); } },
          { id: 'snap-right', label: '靠右排列', icon: <PanelRight size={15} />, disabled: !activeNode, action: () => { if (activeNode) api.getState().snapWindow(activeNode.id, 'right'); } },
          { id: 'overview', label: '窗口总览', icon: <PanelsTopLeft size={15} />, shortcut: 'F5', separatorBefore: true, action: () => api.getState().setOverviewOpen(true) },
          {
            id: 'close',
            label: '关闭窗口',
            icon: <X size={15} />,
            danger: true,
            separatorBefore: true,
            disabled: !activeNode,
            action: () => { if (activeNode) api.getState().closeWindow(activeNode.id); },
          },
        ];
      }
      if (!activeWindowId || !activeAppId) {
        return [
          { id: 'launchpad', label: '全部 App', icon: <LayoutGrid size={15} />, shortcut: '⌘K', action: () => api.getState().setLaunchpadOpen(true) },
          { id: 'new-agent', label: '新建 Agent 工作', icon: <Bot size={15} />, action: () => openApp('agent') },
          { id: 'settings', label: '系统设置', icon: <Settings size={15} />, shortcut: '⌘,', action: () => openApp('system-settings') },
        ];
      }
      return [
        { id: 'hide', label: '隐藏窗口', icon: <Minus size={15} />, shortcut: '⌘H', action: () => api.getState().minimizeWindow(activeWindowId) },
        { id: 'overview', label: '窗口总览', icon: <PanelsTopLeft size={15} />, shortcut: 'F5', action: () => api.getState().setOverviewOpen(true) },
        {
          id: 'close',
          label: '关闭窗口',
          icon: <X size={15} />,
          danger: true,
          separatorBefore: true,
          action: () => api.getState().closeWindow(activeWindowId),
        },
      ];
    }
    if (contextMenu.kind === 'apps') {
      const openWindowCount = menuWindows.filter((node) => contextMenu.appIds.includes(node.appId)).length;
      const pinned = new Set(api.getState().dockAppIds);
      const eligibleDockAppIds = contextMenu.appIds.filter((appId) => (
        !isPawExtensionAppId(appId) || installation.enabledExtensionIds.has(appId)
      ));
      const unpinnedAppIds = eligibleDockAppIds.filter((appId) => !pinned.has(appId));
      const pinnedAppIds = eligibleDockAppIds.filter((appId) => pinned.has(appId));
      const archived = new Set(api.getState().wayfinder.archived);
      const visibleDesktopAppIds = contextMenu.appIds.filter((appId) => !archived.has(`app:${appId}`));
      const removedDesktopAppIds = contextMenu.appIds.filter((appId) => archived.has(`app:${appId}`));
      return [
        {
          id: 'open-apps',
          label: contextMenu.appIds.length === 1 ? `打开 ${contextMenu.label}` : `打开 ${contextMenu.appIds.length} 个 App`,
          icon: <ArrowUpRight size={15} />,
          action: () => contextMenu.appIds.forEach(openApp),
        },
        ...(unpinnedAppIds.length ? [{
          id: 'pin-dock-apps',
          label: unpinnedAppIds.length === 1 ? `添加 ${pawApp(unpinnedAppIds[0]!).label} 到 Dock` : `添加 ${unpinnedAppIds.length} 个 App 到 Dock`,
          icon: <Pin size={15} />,
          action: () => unpinnedAppIds.forEach((appId) => api.getState().pinDockApp(appId)),
        }] : []),
        ...(pinnedAppIds.length ? [{
          id: 'unpin-dock-apps',
          label: pinnedAppIds.length === 1 ? `从 Dock 移除 ${pawApp(pinnedAppIds[0]!).label}` : `从 Dock 移除 ${pinnedAppIds.length} 个 App`,
          icon: <PinOff size={15} />,
          action: () => pinnedAppIds.forEach((appId) => api.getState().unpinDockApp(appId)),
        }] : []),
        ...(visibleDesktopAppIds.length ? [{
          id: 'remove-desktop-apps',
          label: visibleDesktopAppIds.length === 1
            ? `从桌面移除 ${pawApp(visibleDesktopAppIds[0]!).label}`
            : `从桌面移除 ${visibleDesktopAppIds.length} 个 App`,
          icon: <Archive size={15} />,
          action: () => archiveWorkIcons(
            visibleDesktopAppIds.map((appId) => `app:${appId}`),
            visibleDesktopAppIds.length === 1 ? pawApp(visibleDesktopAppIds[0]!).label : `${visibleDesktopAppIds.length} 个 App`,
            'remove',
          ),
        }] : []),
        ...(removedDesktopAppIds.length ? [{
          id: 'restore-desktop-apps',
          label: removedDesktopAppIds.length === 1
            ? `恢复 ${pawApp(removedDesktopAppIds[0]!).label} 到桌面`
            : `恢复 ${removedDesktopAppIds.length} 个 App 到桌面`,
          icon: <ArchiveRestore size={15} />,
          action: () => removedDesktopAppIds.forEach((appId) => api.getState().setWayfinderArchived(`app:${appId}`, false)),
        }] : []),
        {
          id: 'close-app-windows',
          label: contextMenu.appIds.length === 1
            ? `关闭 ${contextMenu.label} 的全部窗口`
            : `关闭 ${contextMenu.appIds.length} 个 App 的全部窗口`,
          icon: <X size={15} />,
          danger: true,
          separatorBefore: true,
          disabled: openWindowCount === 0,
          action: () => contextMenu.appIds.forEach((appId) => api.getState().closeAppWindows(appId)),
        },
      ];
    }
    const node = menuWindows.find((candidate) => candidate.id === contextMenu.windowId);
    return [
      { id: 'minimize', label: '最小化', icon: <Minus size={15} />, disabled: !node, action: () => api.getState().minimizeWindow(contextMenu.windowId) },
      { id: 'maximize', label: node?.placement === 'maximized' ? '还原窗口' : '最大化', icon: <Maximize2 size={15} />, disabled: !node, action: () => api.getState().toggleMaximize(contextMenu.windowId) },
      { id: 'snap-left', label: '靠左排列', icon: <PanelLeft size={15} />, disabled: !node, action: () => api.getState().snapWindow(contextMenu.windowId, 'left') },
      { id: 'snap-right', label: '靠右排列', icon: <PanelRight size={15} />, disabled: !node, action: () => api.getState().snapWindow(contextMenu.windowId, 'right') },
      {
        id: 'close-app-windows',
        label: node ? `关闭 ${pawApp(node.appId).label} 的全部窗口` : '关闭这个 App 的全部窗口',
        icon: <X size={15} />,
        danger: true,
        separatorBefore: true,
        disabled: !node,
        action: () => {
          if (node) api.getState().closeAppWindows(node.appId);
        },
      },
      {
        id: 'close',
        label: '关闭窗口',
        icon: <X size={15} />,
        danger: true,
        disabled: !node,
        action: () => {
          api.getState().closeWindow(contextMenu.windowId);
        },
      },
    ];
  }, [activeAppId, activeWindowId, api, archiveWorkIcons, contextMenu, installation.enabledExtensionIds, menuWindows]);
  const menuBarLabel = activeAppId ? pawApp(activeAppId).label : '桌面';
  /* macOS menu-bar discipline: a menu opens on click, and while any menu-bar
   * menu is open, hovering the neighbouring title (or pressing ←/→ inside the
   * menu) moves the open menu across the bar instead of requiring a second
   * click. The anchor rect comes from the refs so all three paths agree. */
  const openMenuBarMenu = (menu: 'app' | 'window') => {
    const anchor = menu === 'app' ? menuAppRef.current : menuWindowRef.current;
    if (!anchor) return;
    contextMenuOpenerRef.current = anchor;
    const rect = anchor.getBoundingClientRect();
    setContextMenu({ kind: 'menubar', menu, x: rect.left, y: rect.bottom + 6 });
  };
  const menuBarButtonHandlers = (menu: 'app' | 'window') => ({
    onClick: (event: ReactMouseEvent<HTMLButtonElement>) => {
      event.preventDefault();
      event.stopPropagation();
      if (contextMenu?.kind === 'menubar' && contextMenu.menu === menu) {
        setContextMenu(null);
        return;
      }
      openMenuBarMenu(menu);
    },
    onPointerEnter: (event: ReactPointerEvent<HTMLButtonElement>) => {
      // Touch taps fire pointerenter right before pointerdown; treating that
      // as a hover-switch would make the following click close the menu it
      // just opened. Only a real mouse glide carries the menu across.
      if (event.pointerType !== 'mouse') return;
      if (contextMenu?.kind === 'menubar' && contextMenu.menu !== menu) openMenuBarMenu(menu);
    },
  });
  const closeContextMenu = useCallback((reason: PawContextMenuCloseReason) => {
    setContextMenu(null);
    if (reason !== 'keyboard' && reason !== 'action') return;
    const opener = contextMenuOpenerRef.current;
    /* Item actions may archive their opener. Wait until React and the external
       desktop store have committed, then return to the exact opener when it
       still exists; otherwise leave a stable keyboard landing on the desktop. */
    window.setTimeout(() => {
      if (opener?.isConnected) opener.focus();
      else viewportRef.current?.focus();
    }, 0);
  }, []);
  return (
    <div
      className="paw-desktop"
      data-ambient-paused={ambientPaused || undefined}
      data-collaboration-focus={collaborationFocus || undefined}
      data-overview={overviewOpen || undefined}
      onContextMenu={openContextMenu}
      ref={desktopRef}
    >
      <header className="paw-menu-bar">
        <button aria-label="打开全部 App" className="paw-system-mark" onClick={toggleLaunchpad} type="button">
          <PawBrandMark size={15} />
          <span className="paw-brand-wordmark">PAW</span>
          <span
            className="paw-brand-version"
            data-paw-product-version
            title={`PAW ${PAW_PRODUCT_BUILD_LABEL} · 构建 ${PAW_BUILD_COMMIT}`}
          >{PAW_PRODUCT_BUILD_LABEL}</span>
        </button>
        <div className="paw-menu-menus">
          <button
            aria-expanded={contextMenu?.kind === 'menubar' && contextMenu.menu === 'app'}
            aria-haspopup="menu"
            aria-label={`${menuBarLabel} 菜单`}
            className="paw-menu-app"
            data-app={activeAppId ?? undefined}
            data-idle={activeAppId ? undefined : true}
            ref={menuAppRef}
            type="button"
            {...menuBarButtonHandlers('app')}
          >
            {activeAppId ? <PawAppIcon appId={activeAppId} size={14} /> : null}
            <span>{menuBarLabel}</span>
          </button>
          <button
            aria-expanded={contextMenu?.kind === 'menubar' && contextMenu.menu === 'window'}
            aria-haspopup="menu"
            aria-label="窗口菜单"
            className="paw-menu-extra"
            ref={menuWindowRef}
            type="button"
            {...menuBarButtonHandlers('window')}
          >
            <span>窗口</span>
          </button>
        </div>
        <div className="paw-menu-status">
          <ConnectionIndicator />
          <PawBackgroundActivity />
          <PawNotificationCenter />
          <PawMenuClock />
        </div>
      </header>

      <main
        className="paw-desktop-viewport"
        onDragOver={(event) => {
          if (![...event.dataTransfer.types].includes(PAW_DOCK_APP_MIME)) return;
          event.preventDefault();
          event.dataTransfer.dropEffect = 'move';
        }}
        onDrop={(event) => {
          const appId = readDraggedAppId(event.dataTransfer);
          if (!appId || event.dataTransfer.getData(PAW_DOCK_APP_MIME) !== appId) return;
          event.preventDefault();
          api.getState().unpinDockApp(appId);
        }}
        onPointerDown={startLasso}
        ref={viewportRef}
        tabIndex={-1}
      >
        <Wayfinder onArchive={archiveWorkIcons} onOpen={openApp} onSelectIcon={selectIcon} selectedIcons={selectedIcons} />
        <PawWindowLayer />
        {lasso ? <div className="paw-selection-lasso" data-testid="paw-selection-lasso" style={{ left: lasso.x, top: lasso.y, width: lasso.width, height: lasso.height }} /> : null}
      </main>
      {archiveReceipt && archiveNoticeVisible ? <PawArchiveUndo
        label={archiveReceipt.label}
        operation={archiveReceipt.operation}
        onDismiss={() => setArchiveNoticeVisible(false)}
        onUndo={undoArchive}
      /> : null}
      {collaborationFocus ? <button
        className="paw-collaboration-focus-exit"
        onClick={() => api.getState().setCollaborationFocusGroup(null)}
        type="button"
      ><X size={14} />退出协作聚焦</button> : null}
      {activeWindowMaximized ? null : <PawDock
        activeAppId={activeAppId}
        launchpadOpen={launchpadOpen}
        onLaunchpad={toggleLaunchpad}
        onOpen={openApp}
        onOverview={toggleOverview}
        overviewOpen={overviewOpen}
      />}
      {launchpadOpen ? <PawLaunchpad onClose={closeLaunchpad} onOpen={openApp} /> : null}
      {contextMenu ? (
        <PawContextMenu
          anchor={contextMenu.kind === 'menubar' ? (contextMenu.menu === 'app' ? menuAppRef : menuWindowRef) : undefined}
          ariaLabel={contextMenuAriaLabel(contextMenu, activeAppId)}
          items={menuItems}
          /* A distinct key per menu remounts on a hover/arrow switch, so the
           * entrance replays from the new anchor and focus lands on the new
           * first item instead of dying with the swapped-out list. */
          key={contextMenuKey(contextMenu)}
          onClose={closeContextMenu}
          onHorizontalNavigate={contextMenu.kind === 'menubar'
            ? () => openMenuBarMenu(contextMenu.menu === 'app' ? 'window' : 'app')
            : undefined}
          x={contextMenu.x}
          y={contextMenu.y}
        />
      ) : null}
    </div>
  );
}

/* One subscription to the platform visibility signal: a hidden document can
 * never be watched, so the wallpaper weather pauses with it. */
function useDocumentHidden(): boolean {
  const [hidden, setHidden] = useState(() => typeof document !== 'undefined' && document.visibilityState === 'hidden');
  useEffect(() => {
    const update = () => setHidden(document.visibilityState === 'hidden');
    document.addEventListener('visibilitychange', update);
    return () => document.removeEventListener('visibilitychange', update);
  }, []);
  return hidden;
}

/* The half-minute clock tick lives in its own leaf so it re-renders one
 * <span>, never the whole desktop chrome. */
function PawMenuClock() {
  const [clock, setClock] = useState(() => timeLabel());
  useEffect(() => {
    const timer = window.setInterval(() => setClock(timeLabel()), 30_000);
    return () => window.clearInterval(timer);
  }, []);
  return <span>{clock}</span>;
}

function PawArchiveUndo({ label, onDismiss, onUndo, operation }: {
  label: string;
  onDismiss: () => void;
  onUndo: () => void;
  operation: PawArchiveReceipt['operation'];
}) {
  return (
    <aside aria-label="桌面移除结果" className="paw-archive-undo" data-paw-desktop-ui role="status">
      <ArchiveRestore aria-hidden="true" size={16} />
      <span>{operation === 'remove' ? '已从桌面移除' : '已移到归档'}：<strong>{label}</strong></span>
      <button aria-keyshortcuts="Meta+Z Control+Z" aria-label={`撤销移除 ${label}`} onClick={onUndo} type="button">撤销</button>
      <button aria-label="关闭移除提示" className="paw-archive-undo__close" onClick={onDismiss} type="button"><X aria-hidden="true" size={14} /></button>
    </aside>
  );
}

/* The Wayfinder is the desktop's heaviest resting subtree: the wallpaper, the
 * lede, the identity rail and the recent-work panel. Its props are the two
 * stable callbacks plus the selection set, so clock ticks, menus, focus
 * changes and every lasso frame that crosses no new identity leave it
 * untouched. */
const Wayfinder = memo(function Wayfinder({ onArchive, onOpen, onSelectIcon, selectedIcons }: {
  onArchive: (iconIds: readonly string[], label: string) => void;
  onOpen: (id: PawAppId) => void;
  onSelectIcon: (iconId: string, additive: boolean) => void;
  selectedIcons: ReadonlySet<string>;
}) {
  const shortcutsRef = useRef<HTMLDivElement>(null);
  const api = usePawDesktopApi();
  const installation = usePawExtensionInstallation();
  const wayfinder = usePawDesktopStore((state) => state.wayfinder);
  const running = usePawRunningApps();
  const gridLayout = usePawDesktopGridLayout();
  const desktopAppIds = useMemo(() => [
    ...DESKTOP_APP_IDS,
    ...pawExtensionApps
      .filter((app) => installation.enabledExtensionIds.has(app.id))
      .map((app) => app.id),
  ], [installation.enabledExtensionIds]);
  // Roving arrows walk the shortcut list like a real desktop: focus moves
  // between identities without tabbing out of the Wayfinder, and Enter on the
  // focused identity still opens it (owned by the per-button handler below).
  const walkShortcuts = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (!['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    const host = shortcutsRef.current;
    if (!host) return;
    const buttons = Array.from(host.querySelectorAll<HTMLButtonElement>('button[data-desktop-app]'));
    const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
    if (current === -1) return;
    event.preventDefault();
    if (event.altKey && event.key.startsWith('Arrow')) {
      const button = buttons[current]!;
      const iconId = button.dataset.wayfinderGridPosition;
      const plane = button.closest<HTMLElement>('.paw-wayfinder');
      const entry = plane && iconId
        ? pawDesktopGridEntries(plane).find((candidate) => candidate.id === iconId)
        : undefined;
      if (plane && iconId && entry) {
        api.getState().setWayfinderIconPosition(iconId, pawDesktopMovePosition(
          entry.position,
          event.key as 'ArrowDown' | 'ArrowUp' | 'ArrowLeft' | 'ArrowRight',
          gridLayout.columns,
          pawDesktopOccupiedPositions(plane, iconId),
        ));
      }
      return;
    }
    if (event.key === 'Home' || event.key === 'End') {
      buttons[event.key === 'Home' ? 0 : buttons.length - 1]?.focus();
      return;
    }
    spatialWayfinderTarget(
      buttons,
      buttons[current]!,
      event.key as 'ArrowDown' | 'ArrowUp' | 'ArrowLeft' | 'ArrowRight',
    )?.focus();
  };
  const startAppDrag = useCallback((event: DragEvent<HTMLElement>, appId: PawAppId) => {
    event.stopPropagation();
    writeAppDrag(event.dataTransfer, appId, false);
  }, []);
  const dropOnDesktop = useCallback((event: DragEvent<HTMLElement>) => {
    const iconId = event.dataTransfer.getData(WAYFINDER_DRAG_MIME) || event.dataTransfer.getData('text/plain');
    if (!iconId.startsWith('app:') && !iconId.startsWith('project:') && !iconId.startsWith('session:') && !iconId.startsWith('room:')) return;
    event.preventDefault();
    event.stopPropagation();
    const rect = event.currentTarget.getBoundingClientRect();
    const current = pawDesktopGridEntries(event.currentTarget).find((entry) => entry.id === iconId)?.position;
    /* Keyboard/AT-driven synthetic drops may not carry pointer coordinates.
     * Preserve the icon's current cell instead of serializing NaN. */
    const dropped = Number.isFinite(event.clientX) && Number.isFinite(event.clientY)
      ? { x: event.clientX - rect.left - 48, y: event.clientY - rect.top - 46 }
      : current ?? pawDesktopGridPosition(0, gridLayout.columns, gridLayout.originY);
    const position = clampWayfinderIconPosition(pawDesktopSnapPosition(
      dropped,
      gridLayout.columns,
      pawDesktopOccupiedPositions(event.currentTarget, iconId),
    ), event.currentTarget);
    api.getState().setWayfinderIconPosition(iconId, position);
    const appId = appIdFromDragValue(iconId);
    if (appId) {
      api.getState().setWayfinderArchived(iconId, false);
      if (event.dataTransfer.getData(PAW_DOCK_APP_MIME) === appId) api.getState().unpinDockApp(appId);
    }
    if (iconId.startsWith('session:') || iconId.startsWith('room:')) {
      api.getState().setWayfinderProjectAssignment(iconId, null);
      api.getState().setWayfinderArchived(iconId, false);
    }
  }, [api, gridLayout.columns]);
  return (
    <section className="paw-wayfinder" aria-label="项目场" onDragOver={(event) => { if ([...event.dataTransfer.types].includes(WAYFINDER_DRAG_MIME)) { event.preventDefault(); event.dataTransfer.dropEffect = 'move'; } }} onDrop={dropOnDesktop}>
      <div aria-hidden="true" className="paw-field-media">
        <PawStellarWallpaper />
      </div>
      {/* The first viewport is one composition, not two matching corner cards
          around an empty middle: a single column states what the machine is
          for and indexes the identities directly underneath that sentence,
          and recent work keeps the opposite corner as the one live
          instrument. The column's ground is a feathered opening in the fog,
          so the type belongs to the picture instead of sitting on a card. */}
      <div className="paw-field-stage">
        <PawFieldLede onOpen={onOpen} />
      </div>
      {/* App shortcuts share the Wayfinder's desktop coordinate plane. They
          are still App launchers, but their icon positions now live beside
          project/session icon positions in the same PAWOS snapshot. */}
      <div className="paw-desktop-shortcuts" aria-label="桌面 App" onKeyDown={walkShortcuts} ref={shortcutsRef}>
        {desktopAppIds.filter((id) => !wayfinder.archived.includes(`app:${id}`)).map((id, index) => {
          const open = running.open.has(id);
          const minimizedOnly = open && !running.visible.has(id);
          const iconId = `app:${id}`;
          const position = wayfinder.iconPositions[iconId]
            ?? pawDesktopGridPosition(index, gridLayout.columns, gridLayout.originY);
          return (
            <button
              aria-description={minimizedOnly ? '已最小化，按回车打开' : open ? '运行中' : undefined}
              aria-keyshortcuts="Alt+ArrowUp Alt+ArrowDown Alt+ArrowLeft Alt+ArrowRight"
              aria-pressed={selectedIcons.has(iconId) || undefined}
              data-app={id}
              data-desktop-app={id}
              data-wayfinder-grid-position={iconId}
              data-minimized={minimizedOnly || undefined}
              data-open={open || undefined}
              draggable
              key={id}
              onClick={(event) => onSelectIcon(iconId, event.shiftKey || event.metaKey || event.ctrlKey)}
              onDoubleClick={() => onOpen(id)}
              onDragStart={(event) => startAppDrag(event, id)}
              onFocus={() => warmPawAppProcess(id)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') onOpen(id);
              }}
              onPointerEnter={() => warmPawAppProcess(id)}
              style={{ '--wayfinder-x': `${position.x}px`, '--wayfinder-y': `${position.y}px` } as CSSProperties}
              title={minimizedOnly ? `${pawApp(id).label} · 已最小化` : open ? `${pawApp(id).label} · 运行中` : pawApp(id).label}
              type="button"
            >
              <span><PawAppIcon appId={id} size={48} /></span>
              <strong><span className="paw-desktop-shortcuts__label-ink">{pawApp(id).label}</span></strong>
              <i aria-hidden="true" />
            </button>
          );
        })}
      </div>
      <PawWayfinderWork onArchive={onArchive} onSelectIcon={onSelectIcon} selectedIcons={selectedIcons} />
    </section>
  );
});

/* One projection answers "which Apps are running, which are hidden" for both
 * the Wayfinder list and the Dock. The sorted string signature keeps the
 * store subscription referentially stable, so rows and pills re-render only
 * when an App's running state actually changes. */
function usePawRunningApps(): { open: ReadonlySet<PawAppId>; visible: ReadonlySet<PawAppId> } {
  const signature = usePawDesktopStore((state) => Object.values(state.windows)
    .map((node) => `${node.appId}\u0001${node.minimized ? '1' : '0'}`)
    .sort()
    .join('\u0000'));
  return useMemo(() => {
    const open = new Set<PawAppId>();
    const visible = new Set<PawAppId>();
    for (const item of signature.split('\u0000').filter(Boolean)) {
      const [appId, minimized] = item.split('\u0001') as [PawAppId, string];
      open.add(appId);
      if (minimized === '0') visible.add(appId);
    }
    return { open, visible };
  }, [signature]);
}

/* The shelf answers the pointer directly through dock-magnification's style
 * writes, so React only owns its resting content: which App is current, which
 * are running, whether the overview is open. Everything else on the desktop
 * re-renders without touching it. */
const PawDock = memo(function PawDock({ activeAppId, launchpadOpen, onLaunchpad, onOpen, onOverview, overviewOpen }: {
  activeAppId: PawAppId | null;
  launchpadOpen: boolean;
  onLaunchpad: () => void;
  onOpen: (id: PawAppId) => void;
  onOverview: () => void;
  overviewOpen: boolean;
}) {
  const api = usePawDesktopApi();
  const installation = usePawExtensionInstallation();
  const dockRef = useRef<HTMLElement>(null);
  const [dropTarget, setDropTarget] = useState(false);
  useDockMagnification(dockRef);
  const dockState = usePawRunningApps();
  const dockAppIds = usePawDesktopStore((state) => state.dockAppIds);
  const compact = useCompactDock();
  const visibleDockAppIds = useMemo(() => dockAppIds.filter((appId) => (
    !isPawExtensionAppId(appId) || installation.enabledExtensionIds.has(appId)
  )), [dockAppIds, installation.enabledExtensionIds]);
  const appButtons = visibleDockAppIds.map((appId) => {
    const minimizedOnly = dockState.open.has(appId) && !dockState.visible.has(appId);
    return (
      <button
        aria-current={activeAppId === appId ? 'page' : undefined}
        aria-description={minimizedOnly ? '已最小化，按回车恢复' : dockState.open.has(appId) ? '运行中' : undefined}
        aria-label={pawApp(appId).label}
        data-app={appId}
        data-desktop-app={appId}
        data-minimized={minimizedOnly || undefined}
        data-open={dockState.open.has(appId) || undefined}
        draggable
        key={appId}
        onClick={(event) => {
          // macOS launch feedback: a shelf identity that is not running
          // answers the click with one hop before its window arrives.
          if (!dockState.open.has(appId)) bounceDockIcon(event.currentTarget);
          onOpen(appId);
        }}
        onDragStart={(event) => writeAppDrag(event.dataTransfer, appId, true)}
        onFocus={() => warmPawAppProcess(appId)}
        onPointerEnter={() => warmPawAppProcess(appId)}
        title={minimizedOnly ? `${pawApp(appId).label} 已最小化，点击恢复` : undefined}
        type="button"
      >
        <PawAppIcon appId={appId} size={32} />
        <span aria-hidden="true" className="paw-dock-tip">{minimizedOnly ? `${pawApp(appId).label} · 已最小化` : pawApp(appId).label}</span>
      </button>
    );
  });
  const systemControls = <>
    <button aria-label="窗口总览" aria-pressed={overviewOpen} className="paw-dock-overview" onClick={onOverview} type="button"><PanelsTopLeft size={19} /><span aria-hidden="true" className="paw-dock-tip">窗口总览</span></button>
    <button aria-controls="paw-launchpad-dialog" aria-expanded={launchpadOpen} aria-haspopup="dialog" aria-label="全部 App" className="paw-dock-launchpad" onClick={onLaunchpad} type="button"><Grid3X3 size={19} /><span aria-hidden="true" className="paw-dock-tip">全部 App</span></button>
  </>;
  return (
    <nav
      aria-description="可将桌面或全部 App 中的应用拖入；将 Dock 应用拖回桌面可移除快捷方式。窄窗口时可横向滚动。"
      aria-label="PAWOS 工具架"
      className="paw-dock"
      data-drop-target={dropTarget || undefined}
      onDragLeave={(event) => {
        if (event.relatedTarget instanceof Node && event.currentTarget.contains(event.relatedTarget)) return;
        setDropTarget(false);
      }}
      onDragOver={(event) => {
        if (![...event.dataTransfer.types].some((type) => type === WAYFINDER_DRAG_MIME || type === PAW_DOCK_APP_MIME)) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = 'copy';
        setDropTarget(true);
      }}
      onDrop={(event) => {
        const appId = readDraggedAppId(event.dataTransfer);
        if (!appId) {
          setDropTarget(false);
          return;
        }
        event.preventDefault();
        event.stopPropagation();
        event.dataTransfer.dropEffect = 'copy';
        if (!isPawExtensionAppId(appId) || installation.enabledExtensionIds.has(appId)) {
          api.getState().pinDockApp(appId);
        }
        setDropTarget(false);
      }}
      ref={dockRef}
    >
      {compact ? <>{systemControls}<i aria-hidden="true" />{appButtons}</> : <>{appButtons}<i aria-hidden="true" />{systemControls}</>}
    </nav>
  );
});

function useCompactDock(): boolean {
  const query = '(max-width: 820px)';
  const [compact, setCompact] = useState(() => typeof window.matchMedia === 'function' && window.matchMedia(query).matches);
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return undefined;
    const media = window.matchMedia(query);
    const update = () => setCompact(media.matches);
    update();
    media.addEventListener?.('change', update);
    return () => media.removeEventListener?.('change', update);
  }, []);
  return compact;
}

/* Magnetic Dock conduction. A rAF-throttled pointer stream feeds the pure
 * magnet geometry in dock-magnification.ts (cosine grow, neighbour push and
 * the sine gather toward the pointer) and lands as two custom properties per
 * shelf child, driving transform-only styles. The shelf is measured once per
 * hover on pointerenter — the resting layout cannot change while the pointer
 * conducts, because every response below is a transform — so the per-frame
 * path is pure math plus style writes: no layout reads, no repaints, and
 * dragging a window across the Dock cannot flicker. Coarse pointers, the
 * narrow scrolling Dock, a live window drag/resize, and both reduced-motion
 * signals opt out entirely, leaving the resting shelf untouched. */
function useDockMagnification(dockRef: RefObject<HTMLElement | null>) {
  useEffect(() => {
    const dock = dockRef.current;
    if (!dock) return undefined;
    const finePointer = window.matchMedia('(hover: hover) and (pointer: fine)');
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
    const wideShelf = window.matchMedia('(min-width: 821px)');
    let frame = 0;
    let pointerX = 0;
    let shelf: { items: HTMLElement[]; centers: number[] } | null = null;
    const children = () => Array.from(dock.children).filter((child): child is HTMLElement => child instanceof HTMLElement);
    const measure = () => {
      const items = children();
      const dockLeft = dock.getBoundingClientRect().left;
      shelf = { items, centers: items.map((item) => dockLeft + item.offsetLeft + item.offsetWidth / 2) };
    };
    const rest = () => {
      if (frame) window.cancelAnimationFrame(frame);
      frame = 0;
      shelf = null;
      delete dock.dataset.magnify;
      for (const child of children()) {
        child.style.removeProperty('--paw-dock-mag');
        child.style.removeProperty('--paw-dock-shift');
      }
    };
    const apply = () => {
      frame = 0;
      if (!shelf) measure();
      const { items, centers } = shelf!;
      const { mag, shift } = dockMagnetics(centers, pointerX);
      items.forEach((item, index) => {
        item.style.setProperty('--paw-dock-mag', mag[index].toFixed(4));
        item.style.setProperty('--paw-dock-shift', shift[index].toFixed(2));
      });
    };
    const windowGestureOwnsPointer = () => Boolean(dock.closest<HTMLElement>('.paw-desktop-root')?.dataset.windowInteraction);
    const conducting = () => {
      if (!finePointer.matches || !wideShelf.matches || reducedMotion.matches) return false;
      if (windowGestureOwnsPointer()) return false;
      return document.documentElement.getAttribute('data-reduce-motion') !== 'true';
    };
    const enter = () => {
      if (conducting()) measure();
    };
    const move = (event: PointerEvent) => {
      if (!conducting()) {
        if (dock.dataset.magnify) rest();
        return;
      }
      pointerX = event.clientX;
      dock.dataset.magnify = 'true';
      if (!frame) frame = window.requestAnimationFrame(apply);
    };
    dock.addEventListener('pointerenter', enter);
    dock.addEventListener('pointermove', move);
    dock.addEventListener('pointerleave', rest);
    dock.addEventListener('pointercancel', rest);
    return () => {
      dock.removeEventListener('pointerenter', enter);
      dock.removeEventListener('pointermove', move);
      dock.removeEventListener('pointerleave', rest);
      dock.removeEventListener('pointercancel', rest);
      rest();
    };
  }, [dockRef]);
}

/* The launch hop rides the icon, never the button: the magnification
 * conductor owns the button's transform through custom properties, and a
 * WAAPI transform on the same element would replace it mid-gesture. Both
 * reduced-motion signals (system preference and the in-app setting) opt out,
 * matching the magnification conductor's own gates. */
function bounceDockIcon(button: HTMLElement) {
  if (pawShellReducedMotion()) return;
  const icon = button.querySelector<HTMLElement>('.paw-app-icon');
  if (!icon || typeof icon.animate !== 'function') return;
  icon.animate([
    { transform: 'translate3d(0, 0, 0)' },
    { transform: 'translate3d(0, -16px, 0)' },
    { transform: 'translate3d(0, 0, 0)' },
    { transform: 'translate3d(0, -6px, 0)' },
    { transform: 'translate3d(0, 0, 0)' },
  ], { duration: 540, easing: 'cubic-bezier(.3, .7, .4, 1)' });
}

function pawShellReducedMotion(): boolean {
  if (document.documentElement.getAttribute('data-reduce-motion') === 'true') return true;
  return typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function PawLaunchpad({ onClose, onOpen }: { onClose: () => void; onOpen: (id: PawAppId) => void }) {
  const [query, setQuery] = useState('');
  const searchRef = useRef<HTMLInputElement>(null);
  const installation = usePawExtensionInstallation();
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return currentPawApps().filter((app) => {
      if (!needle) return true;
      return [app.label, app.shortLabel, app.tagline, app.id].some((part) => part.toLowerCase().includes(needle));
    });
  }, [query, installation.enabledExtensionIds]);
  const groups = useMemo(() => {
    // A running index across groups drives the cascade arrival: each group
    // header takes its own beat and its tiles follow, so the archive opens as
    // one choreography that reads in document order — section, then contents.
    let order = 0;
    return LAUNCHPAD_GROUP_ORDER.flatMap((kind) => {
      const apps = filtered.filter((app) => launchpadGroup(app) === kind).sort((left, right) => {
        const priority = (app: PawAppDefinition) => CORE_WORK_APP_IDS.includes(app.id) ? CORE_WORK_APP_IDS.indexOf(app.id) : CORE_WORK_APP_IDS.length;
        return priority(left) - priority(right);
      });
      return apps.length
        ? [{ kind, label: LAUNCHPAD_GROUP_LABEL[kind], order: order++, apps: apps.map((app) => ({ app, order: order++ })) }]
        : [];
    });
  }, [filtered]);
  useEffect(() => {
    const returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    searchRef.current?.focus();
    return () => returnFocus?.focus();
  }, []);
  return (
    <div
      id="paw-launchpad-dialog"
      aria-label="全部 App"
      aria-modal="true"
      className="paw-launchpad"
      onKeyDown={(event) => {
        if (event.key === 'Tab') {
          const focusable = Array.from(event.currentTarget.querySelectorAll<HTMLElement>(
            'input:not(:disabled), button:not(:disabled), a[href], [tabindex]:not([tabindex="-1"])',
          ));
          const first = focusable[0];
          const last = focusable.at(-1);
          if (event.shiftKey && document.activeElement === first && last) {
            event.preventDefault();
            last.focus();
          } else if (!event.shiftKey && document.activeElement === last && first) {
            event.preventDefault();
            first.focus();
          }
          return;
        }
        if (event.key === 'Escape' && query) {
          event.stopPropagation();
          setQuery('');
          return;
        }
        if (event.key === 'Enter' && filtered.length === 1) {
          event.preventDefault();
          onOpen(filtered[0].id);
        }
      }}
      onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}
      role="dialog"
    >
      <section>
        <header>
          <span className="paw-launchpad-title"><PawBrandMark size={16} /><b className="paw-brand-wordmark">PAW</b><span>全部 App</span></span>
          <input
            aria-label="搜索 App"
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索 App"
            ref={searchRef}
            type="search"
            value={query}
          />
          <button onClick={onClose} type="button">完成</button>
        </header>
        <div>
          {groups.length === 0 ? (
            <p className="paw-launchpad-empty">没有匹配的 App</p>
          ) : groups.map((group) => (
            <Fragment key={group.kind}>
              <h2 className="paw-launchpad-group" style={{ '--paw-tile-i': group.order } as CSSProperties}>{group.label}</h2>
              {group.apps.map(({ app, order }) => {
                const extension = isPawExtensionAppId(app.id) ? pawExtensionApp(app.id) : null;
                const installationLabel = extension
                  ? installation.loading
                    ? '检查中…'
                    : installation.unavailable
                      ? '运行时不可用'
                      : installation.isUpdateRequired(app.id)
                        ? '需要更新'
                        : installation.isInstalled(app.id)
                          ? installation.isEnabled(app.id) ? '已安装' : '已安装 · 未启用'
                          : '未安装'
                  : '';
                const installationState = extension
                  ? installationLabel === '已安装' ? 'enabled'
                    : installationLabel === '需要更新' ? 'update-required'
                      : installationLabel === '已安装 · 未启用' ? 'disabled'
                        : installationLabel === '运行时不可用' ? 'runtime-unavailable'
                          : installationLabel === '检查中…' ? 'loading' : 'uninstalled'
                  : undefined;
                return (
                <button
                  data-app={app.id}
                  data-extension-installation={installationState}
                  draggable
                  key={app.id}
                  onClick={() => onOpen(app.id)}
                  onDragStart={(event) => writeAppDrag(event.dataTransfer, app.id, false)}
                  onFocus={() => warmPawAppProcess(app.id)}
                  onPointerEnter={() => warmPawAppProcess(app.id)}
                  style={{ '--paw-tile-i': order } as CSSProperties}
                  type="button"
                >
                  <span><PawAppIcon appId={app.id} size={48} /></span>
                  <strong>{app.label}</strong>
                  <small>{app.tagline}</small>
                  {extension ? <small className="paw-launchpad-installation" data-extension-state={installationLabel}>{installationLabel}</small> : null}
                </button>
                );
              })}
            </Fragment>
          ))}
        </div>
      </section>
    </div>
  );
}

function timeLabel(): string {
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric',
    day: 'numeric',
    weekday: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date());
}

function writeAppDrag(dataTransfer: DataTransfer, appId: PawAppId, fromDock: boolean): void {
  dataTransfer.effectAllowed = fromDock ? 'move' : 'copyMove';
  if (fromDock) dataTransfer.setData(PAW_DOCK_APP_MIME, appId);
  dataTransfer.setData(WAYFINDER_DRAG_MIME, `app:${appId}`);
  dataTransfer.setData('text/plain', `app:${appId}`);
}

function readDraggedAppId(dataTransfer: DataTransfer): PawAppId | null {
  return appIdFromDragValue(
    dataTransfer.getData(PAW_DOCK_APP_MIME)
      || dataTransfer.getData(WAYFINDER_DRAG_MIME)
      || dataTransfer.getData('text/plain'),
  );
}

function appIdFromDragValue(value: string): PawAppId | null {
  const candidate = value.startsWith('app:') ? value.slice(4) : value;
  return PAW_APP_IDS.has(candidate as PawAppId) || isPawExtensionAppId(candidate) ? candidate as PawAppId : null;
}

function sameIconSelection(left: ReadonlySet<string>, right: ReadonlySet<string>): boolean {
  if (left.size !== right.size) return false;
  for (const iconId of right) {
    if (!left.has(iconId)) return false;
  }
  return true;
}

function launchpadGroup(app: PawAppDefinition): (typeof LAUNCHPAD_GROUP_ORDER)[number] {
  if (app.id === 'memory' || app.id === 'knowledge') return 'library';
  if (app.id === 'project-workbench' || CORE_WORK_APP_IDS.includes(app.id) || app.kind === 'agent') return 'work';
  if (app.kind === 'system') return 'system';
  return 'tool';
}

function contextMenuAriaLabel(menu: PawMenuState, activeAppId: PawAppId | null): string {
  if (menu.kind === 'desktop') return '桌面菜单';
  if (menu.kind === 'menubar') {
    if (menu.menu === 'window') return '窗口菜单';
    return activeAppId ? `${pawApp(activeAppId).label} 菜单` : '桌面菜单';
  }
  return `${menu.label} 菜单`;
}

function contextMenuKey(menu: PawMenuState): string {
  if (menu.kind === 'menubar') return `menubar-${menu.menu}`;
  if (menu.kind === 'apps') return `apps-${menu.appIds.join(',')}`;
  if (menu.kind === 'work') return `work-${menu.iconIds.join(',')}`;
  if (menu.kind === 'window') return `window-${menu.windowId}`;
  return 'desktop';
}
