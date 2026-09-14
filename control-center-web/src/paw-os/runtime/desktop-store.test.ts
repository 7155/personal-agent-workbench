import { describe, expect, it } from 'vitest';
import { pawDockAppIds } from './app-registry';
import { pawExtensionApps } from '../extensions/registry';
import { createPawDesktopStore, pawFocusWindowLayerSize, pawWindowArea, pawWindowLayerSize } from './desktop-store';

describe('PAWOS desktop store', () => {
  it('keeps previously visible windows fully inside a narrower viewport', () => {
    const width = window.innerWidth;
    const height = window.innerHeight;
    try {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1280 });
      Object.defineProperty(window, 'innerHeight', { configurable: true, value: 900 });
      const store = createPawDesktopStore('app-center');
      store.getState().commitBounds('app-center', { x: 109, y: 80, width: 775, height: 500 });
      store.getState().openApp('files');
      store.getState().commitBounds('files', { x: 31, y: 70, width: 930, height: 500 });
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: 840 });
      store.getState().fitWindowsToViewport();
      expect(store.getState().windows['app-center']!.bounds).toEqual({ x: 57, y: 80, width: 775, height: 500 });
      expect(store.getState().windows.files!.bounds).toEqual({ x: 8, y: 70, width: 824, height: 500 });
      const state = store.getState();
      store.getState().fitWindowsToViewport();
      expect(store.getState()).toBe(state);
    } finally {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: width });
      Object.defineProperty(window, 'innerHeight', { configurable: true, value: height });
    }
  });

  it('preserves intentionally partial windows while fitting maximized restore bounds on shrink', () => {
    const width = window.innerWidth;
    const height = window.innerHeight;
    try {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1280 });
      Object.defineProperty(window, 'innerHeight', { configurable: true, value: 900 });
      const store = createPawDesktopStore('app-center');
      store.getState().commitBounds('app-center', { x: 109, y: 80, width: 775, height: 500 });
      store.getState().toggleMaximize('app-center');
      store.getState().openApp('files');
      store.getState().commitBounds('files', { x: -240, y: 70, width: 600, height: 500 });
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: 840 });
      store.getState().fitWindowsToViewport();
      expect(store.getState().windows.files!.bounds.x).toBe(-240);
      expect(store.getState().windows['app-center']!.placement).toBe('maximized');
      store.getState().toggleMaximize('app-center');
      expect(store.getState().windows['app-center']!.bounds).toEqual({ x: 57, y: 80, width: 775, height: 500 });
      // Fitting an unchanged viewport must not undo a deliberate subsequent drag.
      store.getState().commitBounds('app-center', { x: 700, y: 80, width: 775, height: 500 });
      store.getState().fitWindowsToViewport();
      expect(store.getState().windows['app-center']!.bounds.x).toBe(700);
    } finally {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: width });
      Object.defineProperty(window, 'innerHeight', { configurable: true, value: height });
    }
  });

  it('pins and unpins Dock Apps without changing their window lifecycle', () => {
    const store = createPawDesktopStore('agent');

    expect(store.getState().dockAppIds).toEqual(pawDockAppIds);
    store.getState().pinDockApp('system-settings');
    store.getState().pinDockApp('system-settings');
    expect(store.getState().dockAppIds).toEqual([...pawDockAppIds, 'system-settings']);

    store.getState().unpinDockApp('agent');
    expect(store.getState().dockAppIds).not.toContain('agent');
    expect(store.getState().windows.agent).toBeDefined();
  });

  it('gates Extension App openApp calls until enabled and closes them when the gate is lost', () => {
    const extension = pawExtensionApps[0]!;
    const store = createPawDesktopStore();

    expect(store.getState().openApp(extension.id)).toBe('');
    expect(store.getState().windows[extension.id]).toBeUndefined();

    store.getState().setExtensionAppGate('ready', new Set([extension.id]));
    expect(store.getState().openApp(extension.id)).toBe(extension.id);
    expect(store.getState().windows[extension.id]).toBeDefined();

    store.getState().setExtensionAppGate('unavailable', new Set([extension.id]));
    expect(store.getState().windows[extension.id]).toBeUndefined();
    expect(store.getState().openApp(extension.id)).toBe('');
  });

  it('opens only previously enabled Extensions during refresh and revokes them on a new inventory', () => {
    const extension = pawExtensionApps[0]!;
    const store = createPawDesktopStore('eval-lab', '/eval-lab?project=project-1');
    const lab = store.getState().windows['eval-lab'];
    store.getState().setExtensionAppGate('loading', new Set([extension.id]));
    expect(store.getState().openApp(extension.id)).toBe('');
    store.getState().setExtensionAppGate('ready', new Set([extension.id]));
    store.getState().setExtensionAppGate('loading', new Set());
    expect(store.getState().openApp(extension.id)).toBe(extension.id);
    expect(store.getState().windows['eval-lab']).toBe(lab);
    store.getState().setExtensionAppGate('ready', new Set());
    expect(store.getState().windows[extension.id]).toBeUndefined();
    expect(store.getState().openApp(extension.id)).toBe('');
    store.getState().setExtensionAppGate('loading', new Set([extension.id]));
    expect(store.getState().openApp(extension.id)).toBe('');
  });

  it('keeps a running Extension App mounted while its installation inventory refreshes', () => {
    const extension = pawExtensionApps[0]!;
    const store = createPawDesktopStore();

    store.getState().setExtensionAppGate('ready', new Set([extension.id]));
    const windowId = store.getState().openApp(extension.id);
    const runningWindow = store.getState().windows[windowId];

    store.getState().setExtensionAppGate('loading', new Set());

    expect(store.getState().windows[windowId]).toBe(runningWindow);
    expect(store.getState().extensionAppGate.enabledExtensionIds.has(extension.id)).toBe(true);
    expect(store.getState().extensionAppGate.status).toBe('loading');

    store.getState().setExtensionAppGate('ready', new Set([extension.id]));

    expect(store.getState().windows[windowId]).toBe(runningWindow);
  });

  it('preserves v2 coordinates for the view-layer snap migration and keeps filing choices', () => {
    const store = createPawDesktopStore(undefined, undefined, {
      windows: {},
      stack: [],
      activeWindowId: null,
      wayfinder: {
        layoutVersion: 2,
        iconPositions: { 'project:/work/paw': { x: 731, y: 418 } },
        archived: ['session:old'],
        projectAssignments: { 'session:moved': '/work/paw' },
      },
    });

    expect(store.getState().wayfinder).toEqual({
      layoutVersion: 3,
      iconPositions: { 'project:/work/paw': { x: 731, y: 418 } },
      archived: ['session:old'],
      projectAssignments: { 'session:moved': '/work/paw' },
    });
  });

  it('can arrange dragged icons again without changing project assignments', () => {
    const store = createPawDesktopStore();
    store.getState().setWayfinderIconPosition('project:/work/paw', { x: 731, y: 418 });
    store.getState().setWayfinderProjectAssignment('session:moved', '/work/paw');

    store.getState().arrangeWayfinderIcons();

    expect(store.getState().wayfinder.iconPositions).toEqual({});
    expect(store.getState().wayfinder.projectAssignments).toEqual({ 'session:moved': '/work/paw' });
  });

  it('keeps App, project, Session and Room removal in visual desktop state only', () => {
    const store = createPawDesktopStore('agent');
    const windows = store.getState().windows;
    const iconIds = ['app:agent', 'project:/work/paw', 'session:s1', 'room:r1'];

    iconIds.forEach((iconId) => store.getState().setWayfinderArchived(iconId, true));

    expect(store.getState().wayfinder.archived).toEqual(iconIds);
    expect(store.getState().windows).toBe(windows);
    expect(store.getState().windows.agent).toBeDefined();

    iconIds.forEach((iconId) => store.getState().setWayfinderArchived(iconId, false));
    expect(store.getState().wayfinder.archived).toEqual([]);
  });

  it('keeps one primary window per App and restores it without creating a second process', () => {
    const store = createPawDesktopStore();

    const first = store.getState().openApp('agent');
    store.getState().minimizeWindow(first);
    const reopened = store.getState().openApp('agent');

    expect(reopened).toBe(first);
    expect(Object.keys(store.getState().windows)).toEqual(['agent']);
    expect(store.getState().windows.agent?.minimized).toBe(false);
    expect(store.getState().activeWindowId).toBe('agent');
  });

  it('allows entity windows to coexist without duplicating the App runtime owner', () => {
    const store = createPawDesktopStore();

    store.getState().openApp('agent');
    const sessionWindow = store.getState().openApp('agent', { entityId: 'session-42' });

    expect(sessionWindow).toBe('agent:session-42');
    expect(Object.values(store.getState().windows).map((node) => node.appId)).toEqual(['agent', 'agent']);
    expect(store.getState().windows[sessionWindow]?.entityId).toBe('session-42');
  });

  it('retains the entity target needed by the full feature surface', () => {
    const store = createPawDesktopStore();
    const target = { kind: 'room', id: 'room-42', title: '迁移协作' } as const;

    const windowId = store.getState().openApp('agent', {
      entityId: target.id,
      target,
      title: target.title,
    });

    expect(store.getState().windows[windowId]?.target).toEqual(target);
    expect(store.getState().windows[windowId]?.title).toBe('迁移协作');
  });

  it('routes an already open App to the requested deep link', () => {
    const store = createPawDesktopStore('agent');

    const windowId = store.getState().openApp('agent', { initialRoute: '/roles' });

    expect(windowId).toBe('agent');
    expect(store.getState().windows.agent?.initialRoute).toBe('/roles');
  });

  it('opens the initial App at the requested deep link', () => {
    const store = createPawDesktopStore('agent', '/agent?subagents=open');

    expect(store.getState().windows.agent?.initialRoute).toBe('/agent?subagents=open');
    expect(store.getState().windows.agent?.title).toBe('Agent');
  });

  it('does not publish a new window snapshot when bounds are unchanged', () => {
    const store = createPawDesktopStore('agent');
    const before = store.getState().windows.agent;

    store.getState().commitBounds('agent', { ...before!.bounds });

    expect(store.getState().windows.agent).toBe(before);
  });

  it('keeps ordinary window controls above the resident Dock hit plane', () => {
    const originalWidth = window.innerWidth;
    const originalHeight = window.innerHeight;
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 });
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 900 });
    try {
      expect(pawWindowLayerSize()).toEqual({ width: 1440, height: 866 });
      expect(pawFocusWindowLayerSize()).toEqual({ width: 1440, height: 866 });
      expect(pawWindowArea()).toEqual({ x: 8, y: 8, width: 1424, height: 780 });

      const store = createPawDesktopStore(undefined, undefined, {
        windows: {
          agent: {
            id: 'agent',
            appId: 'agent',
            title: 'Agent',
            bounds: { x: 120, y: 620, width: 980, height: 540 },
            minimized: false,
          },
        },
        stack: ['agent'],
        activeWindowId: 'agent',
      });
      const fitted = store.getState().windows.agent!.bounds;
      expect(fitted.y + fitted.height).toBeLessThanOrEqual(788);
    } finally {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalWidth });
      Object.defineProperty(window, 'innerHeight', { configurable: true, value: originalHeight });
    }
  });

  it('maximizes every App to the full menu-below plane and restores its prior frame', () => {
    const originalWidth = window.innerWidth;
    const originalHeight = window.innerHeight;
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 });
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 900 });
    try {
      const store = createPawDesktopStore();
      const windowId = store.getState().openApp('files');
      const before = store.getState().windows[windowId]!.bounds;

      store.getState().toggleMaximize(windowId);

      expect(store.getState().windows[windowId]).toMatchObject({
        placement: 'maximized',
        restoreBounds: before,
        bounds: { x: 0, y: 0, width: 1440, height: 866 },
      });

      store.getState().toggleMaximize(windowId);

      expect(store.getState().windows[windowId]).toMatchObject({
        bounds: before,
        placement: undefined,
        restoreBounds: undefined,
      });
    } finally {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalWidth });
      Object.defineProperty(window, 'innerHeight', { configurable: true, value: originalHeight });
    }
  });

  it('keeps the Room main window focused while opening participant Sessions without entering collaboration mode', () => {
    const store = createPawDesktopStore();
    const room = { kind: 'room', id: 'room-7', title: '迁移协作' } as const;
    const mainId = store.getState().openApp('agent', { entityId: room.id, target: room, title: room.title });
    const firstId = store.getState().openApp('agent', {
      background: true,
      target: { kind: 'participant', id: 'participant-1', roomId: room.id, title: '实现伙伴' },
    });
    const secondId = store.getState().openApp('agent', {
      background: true,
      target: { kind: 'participant', id: 'participant-2', roomId: room.id, title: '复核伙伴' },
    });
    const state = store.getState();

    expect(state.activeWindowId).toBe(mainId);
    expect(state.windows[firstId]?.target?.kind).toBe('participant');
    expect(state.windows[secondId]?.target?.kind).toBe('participant');
    expect(state.windows[firstId]?.bounds.width).toBeGreaterThan(0);
    expect(state.windows[secondId]?.bounds.width).toBeGreaterThan(0);
    expect(state.stack.at(-1)).toBe(mainId);
    expect(state.collaborationFocusGroup).toBeNull();

    store.getState().setCollaborationFocusGroup(`room:${room.id}`);
    expect(store.getState().collaborationFocusGroup).toBe(`room:${room.id}`);
  });

  it('gives all eight supported Room participants distinct non-overlapping satellite frames', () => {
    const store = createPawDesktopStore();
    const room = { kind: 'room', id: 'room-eight', title: '八人协作' } as const;
    store.getState().openApp('agent', { entityId: room.id, target: room, title: room.title });

    const participantIds = Array.from({ length: 8 }, (_, index) => store.getState().openApp('agent', {
      background: true,
      entityId: `participant-${index + 1}`,
      target: {
        kind: 'participant',
        id: `participant-${index + 1}`,
        roomId: room.id,
        title: `伙伴 ${index + 1}`,
      },
    }));
    const bounds = participantIds.map((id) => store.getState().windows[id]!.bounds);

    expect(new Set(bounds.map((frame) => `${frame.x}:${frame.y}:${frame.width}:${frame.height}`)).size).toBe(8);
    for (let leftIndex = 0; leftIndex < bounds.length; leftIndex += 1) {
      for (let rightIndex = leftIndex + 1; rightIndex < bounds.length; rightIndex += 1) {
        const left = bounds[leftIndex]!;
        const right = bounds[rightIndex]!;
        const overlaps = left.x < right.x + right.width
          && left.x + left.width > right.x
          && left.y < right.y + right.height
          && left.y + left.height > right.y;
        expect(overlaps, `participant ${leftIndex + 1} overlaps participant ${rightIndex + 1}`).toBe(false);
      }
    }
  });

  it('preserves Room focus for background work and exits it when another App is opened', () => {
    const store = createPawDesktopStore('agent');
    store.getState().bindAgentMain('agent', { kind: 'room', id: 'room-reading', title: '研究' });
    store.getState().setCollaborationFocusGroup('room:room-reading');

    store.getState().openApp('files', { background: true });
    expect(store.getState().activeWindowId).toBe('agent');
    expect(store.getState().collaborationFocusGroup).toBe('room:room-reading');
    store.getState().openApp('agent');
    expect(store.getState().collaborationFocusGroup).toBe('room:room-reading');

    store.getState().openApp('files');
    expect(store.getState().activeWindowId).toBe('files');
    expect(store.getState().collaborationFocusGroup).toBeNull();
    expect(store.getState().collaborationFocusReturnWindowId).toBeNull();
    expect(store.getState().windows.agent?.target?.id).toBe('room-reading');
  });

  it('binds an existing Agent window to the Room target without creating another main window', () => {
    const store = createPawDesktopStore();
    const mainId = store.getState().openApp('agent');
    store.getState().bindRoomMain({ kind: 'room', id: 'room-8', title: '研究协作' });

    expect(Object.keys(store.getState().windows)).toEqual([mainId]);
    expect(store.getState().windows[mainId]?.target).toEqual({ kind: 'room', id: 'room-8', title: '研究协作' });
  });

  it('keeps the exact Agent window title, icon target, and route aligned while moving between Room and Session', () => {
    const store = createPawDesktopStore();
    const mainId = store.getState().openApp('agent');

    store.getState().bindAgentMain(mainId, { kind: 'room', id: 'room-8', title: '研究协作' });
    store.getState().setCollaborationFocusGroup('room:room-8');
    store.getState().bindAgentMain(mainId, { kind: 'session', id: 'session-8', title: '发布检查' });

    expect(store.getState().windows[mainId]).toMatchObject({
      title: '发布检查',
      initialRoute: '/agent?session=session-8',
      target: { kind: 'session', id: 'session-8', title: '发布检查' },
    });
    expect(store.getState().collaborationFocusGroup).toBeNull();

    store.getState().bindAgentMain(mainId);
    expect(store.getState().windows[mainId]).toMatchObject({ title: 'Agent', initialRoute: '/agent' });
    expect(store.getState().windows[mainId]?.target).toBeUndefined();
  });

  it('does not let a main-window identity update overwrite a participant satellite', () => {
    const store = createPawDesktopStore();
    const satelliteId = store.getState().openApp('agent', {
      target: { kind: 'participant', id: 'participant-8', roomId: 'room-8', title: 'Mercury' },
    });
    const before = store.getState().windows[satelliteId];

    store.getState().bindAgentMain(satelliteId, { kind: 'session', id: 'session-8', title: '发布检查' });

    expect(store.getState().windows[satelliteId]).toBe(before);
  });

  it('keeps Room participant Sessions attached to the matching Room main when other Agent windows are open', () => {
    const store = createPawDesktopStore();
    const sessionId = store.getState().openApp('agent', { entityId: 'session-1', target: { kind: 'session', id: 'session-1', title: '普通 Session' } });
    const room = { kind: 'room', id: 'room-9', title: '并行协作' } as const;
    const roomId = store.getState().openApp('agent', { entityId: room.id, target: room, title: room.title });
    const roomBoundsBeforeSatellite = store.getState().windows[roomId]?.bounds;
    store.getState().focusWindow(sessionId);

    const participantId = store.getState().openApp('agent', {
      background: true,
      target: { kind: 'participant', id: 'participant-9', roomId: room.id, title: '实现伙伴' },
    });
    const state = store.getState();
    const roomWindow = Object.values(state.windows).find((window) => window.target?.kind === 'room' && window.target.id === room.id);

    expect(roomWindow?.bounds.width).toBeGreaterThan(0);
    expect(roomWindow?.bounds).toEqual(roomBoundsBeforeSatellite);
    expect(roomWindow?.target).toEqual(room);
    expect(state.collaborationFocusGroup).toBeNull();
    expect(state.windows[sessionId]?.target?.kind).toBe('session');
    expect(state.windows[participantId]?.target?.kind).toBe('participant');
  });

  it('keeps ordinary subagent open and focus actions out of collaboration mode', () => {
    const store = createPawDesktopStore();
    const returnId = store.getState().openApp('project-workbench');
    const subagentId = store.getState().openApp('agent', {
      entityId: 'subagent-ordinary',
      target: { kind: 'subagent', id: 'subagent-ordinary', sessionId: 'session-ordinary', title: '子 Agent' },
    });

    expect(store.getState().collaborationFocusGroup).toBeNull();
    store.getState().focusWindow(returnId);
    store.getState().focusWindow(subagentId);
    expect(store.getState().activeWindowId).toBe(subagentId);
    expect(store.getState().collaborationFocusGroup).toBeNull();
  });

  it('binds the matching Room main even when another Agent window is focused', () => {
    const store = createPawDesktopStore();
    const sessionId = store.getState().openApp('agent', { entityId: 'session-2', target: { kind: 'session', id: 'session-2', title: '普通 Session' } });
    store.getState().openApp('agent', { entityId: 'room-10', target: { kind: 'room', id: 'room-10', title: '治理 Room' } });
    store.getState().focusWindow(sessionId);

    store.getState().bindRoomMain({ kind: 'room', id: 'room-10', title: '治理 Room', subtitle: '更新后的 Room' });

    expect(Object.values(store.getState().windows).find((window) => window.target?.kind === 'room' && window.target.id === 'room-10')?.target).toEqual({ kind: 'room', id: 'room-10', title: '治理 Room', subtitle: '更新后的 Room' });
    expect(store.getState().windows[sessionId]?.target?.kind).toBe('session');
  });

  it('takes the satellites with it when the Room main window closes', () => {
    const store = createPawDesktopStore();
    const filesId = store.getState().openApp('files');
    const otherRoomId = store.getState().openApp('agent', {
      entityId: 'room-other',
      target: { kind: 'room', id: 'room-other', title: '别的 Room' },
    });
    const otherPlanetId = store.getState().openApp('agent', {
      background: true,
      entityId: 'participant-other',
      target: { kind: 'participant', id: 'participant-other', roomId: 'room-other', title: '别的伙伴' },
    });
    const roomId = store.getState().openApp('agent', {
      entityId: 'room-sol',
      target: { kind: 'room', id: 'room-sol', title: 'Sol 协作' },
    });
    const firstPlanetId = store.getState().openApp('agent', {
      background: true,
      entityId: 'participant-1',
      target: { kind: 'participant', id: 'participant-1', roomId: 'room-sol', title: '实现伙伴' },
    });
    const secondPlanetId = store.getState().openApp('agent', {
      background: true,
      entityId: 'participant-2',
      target: { kind: 'participant', id: 'participant-2', roomId: 'room-sol', title: '复核伙伴' },
    });
    const panelId = store.getState().openApp('agent', {
      background: true,
      entityId: 'room-sol:focus',
      target: { kind: 'room', id: 'room-sol', title: 'Sol 态势', panel: 'focus' },
    });

    store.getState().closeWindow(roomId);

    const state = store.getState();
    expect(state.windows[firstPlanetId]).toBeUndefined();
    expect(state.windows[secondPlanetId]).toBeUndefined();
    expect(state.windows[panelId]).toBeUndefined();
    expect(state.stack).not.toContain(firstPlanetId);
    expect(Object.keys(state.windows).sort())
      .toEqual([filesId, otherRoomId, otherPlanetId].sort());
    expect(state.collaborationFocusGroup).toBeNull();
  });

  it('closes one dismissed planet without touching the Room it orbits', () => {
    const store = createPawDesktopStore();
    const roomId = store.getState().openApp('agent', {
      entityId: 'room-sol',
      target: { kind: 'room', id: 'room-sol', title: 'Sol 协作' },
    });
    const firstPlanetId = store.getState().openApp('agent', {
      background: true,
      entityId: 'participant-1',
      target: { kind: 'participant', id: 'participant-1', roomId: 'room-sol', title: '实现伙伴' },
    });
    const secondPlanetId = store.getState().openApp('agent', {
      background: true,
      entityId: 'participant-2',
      target: { kind: 'participant', id: 'participant-2', roomId: 'room-sol', title: '复核伙伴' },
    });

    store.getState().closeWindow(firstPlanetId);

    const state = store.getState();
    expect(Object.keys(state.windows).sort()).toEqual([roomId, secondPlanetId].sort());
    expect(state.windows[roomId]?.target).toMatchObject({ kind: 'room', id: 'room-sol' });
  });

  it('drops the Room satellites when the main window navigates away from that Room', () => {
    const store = createPawDesktopStore();
    const mainId = store.getState().openApp('agent');
    store.getState().bindAgentMain(mainId, { kind: 'room', id: 'room-sol', title: 'Sol 协作' });
    const planetId = store.getState().openApp('agent', {
      background: true,
      entityId: 'participant-1',
      target: { kind: 'participant', id: 'participant-1', roomId: 'room-sol', title: '实现伙伴' },
    });
    const filesId = store.getState().openApp('files');
    store.getState().focusWindow(mainId);
    store.getState().setCollaborationFocusGroup('room:room-sol');

    store.getState().bindAgentMain(mainId, { kind: 'session', id: 'session-8', title: '发布检查' });

    const state = store.getState();
    expect(state.windows[planetId]).toBeUndefined();
    expect(state.stack).not.toContain(planetId);
    expect(Object.keys(state.windows).sort()).toEqual([mainId, filesId].sort());
    expect(state.activeWindowId).toBe(mainId);
    expect(state.collaborationFocusGroup).toBeNull();
    expect(state.windows[mainId]?.target).toMatchObject({ kind: 'session', id: 'session-8' });
  });

  it('keeps Room satellites alive while another main window still holds that Room open', () => {
    const store = createPawDesktopStore();
    const duplicateMainId = store.getState().openApp('agent', {
      entityId: 'room-sol',
      target: { kind: 'room', id: 'room-sol', title: 'Sol 协作' },
    });
    const navigatingMainId = store.getState().openApp('agent');
    store.getState().bindAgentMain(navigatingMainId, { kind: 'room', id: 'room-sol', title: 'Sol 协作' });
    const planetId = store.getState().openApp('agent', {
      background: true,
      entityId: 'participant-1',
      target: { kind: 'participant', id: 'participant-1', roomId: 'room-sol', title: '实现伙伴' },
    });

    store.getState().closeWindow(navigatingMainId);

    expect(Object.keys(store.getState().windows).sort()).toEqual([duplicateMainId, planetId].sort());
  });

  it('closes the subagent satellites with their Session main window', () => {
    const store = createPawDesktopStore();
    const sessionId = store.getState().openApp('agent', {
      entityId: 'session-8',
      target: { kind: 'session', id: 'session-8', title: '发布检查' },
    });
    const subagentId = store.getState().openApp('agent', {
      background: true,
      entityId: 'subagent-1',
      target: { kind: 'subagent', id: 'subagent-1', sessionId: 'session-8', title: '子 Agent' },
    });
    const terminalId = store.getState().openApp('terminal', {
      entityId: 'bg_run',
      target: {
        kind: 'process-terminal',
        id: 'bg_run',
        title: 'pnpm test',
        sessionId: 'session-8',
        toolCallId: 'call-1',
        command: 'pnpm test',
      },
    });

    store.getState().closeWindow(sessionId);

    const state = store.getState();
    expect(state.windows[subagentId]).toBeUndefined();
    /* 进程终端窗是 Runtime 自己的投影，不在这组卫星键里，Session 窗关闭
       不代表它该消失。 */
    expect(Object.keys(state.windows)).toEqual([terminalId]);
  });

  it('closes every window for one App atomically while preserving other Apps and clearing stale focus state', () => {
    const store = createPawDesktopStore();
    const roomId = store.getState().openApp('agent', {
      entityId: 'room-close',
      target: { kind: 'room', id: 'room-close', title: '关闭范围' },
    });
    store.getState().openApp('agent', {
      background: true,
      entityId: 'participant-close',
      target: { kind: 'participant', id: 'participant-close', roomId: 'room-close', title: '伙伴' },
    });
    const browserId = store.getState().openApp('browser', { title: 'Browser' });
    store.getState().setCollaborationFocusGroup('room:room-close');
    store.getState().focusWindow(roomId);

    store.getState().closeAppWindows('agent');

    const state = store.getState();
    expect(Object.keys(state.windows)).toEqual([browserId]);
    expect(state.stack).toEqual([browserId]);
    expect(state.activeWindowId).toBe(browserId);
    expect(state.collaborationFocusGroup).toBeNull();
    expect(state.collaborationFocusReturnWindowId).toBeNull();
    expect(state.overviewOpen).toBe(false);
  });

  it('closes every PAWOS projection without stopping or mutating an underlying Runtime target', () => {
    const store = createPawDesktopStore();
    store.getState().openApp('agent');
    store.getState().openApp('terminal', {
      entityId: 'bg_same_run',
      target: {
        kind: 'process-terminal',
        id: 'bg_same_run',
        title: 'pnpm test',
        sessionId: 'session-1',
        toolCallId: 'call-1',
        runId: 'bg_same_run',
        command: 'pnpm test',
      },
    });
    store.getState().setOverviewOpen(true);

    store.getState().closeAllWindows();

    expect(store.getState()).toMatchObject({
      windows: {},
      stack: [],
      activeWindowId: null,
      collaborationFocusGroup: null,
      collaborationFocusReturnWindowId: null,
      overviewOpen: false,
    });
  });
});
