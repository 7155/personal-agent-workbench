import { afterEach, describe, expect, it } from 'vitest';
import { pawExtensionApps } from './extensions/registry';
import { createPawDesktopStore } from './runtime/desktop-store';
import { syncPawOsRoute } from './PawOsApp';

afterEach(() => {
  window.location.hash = '';
});

describe('PAWOS route bridge', () => {
  it('still honors a fresh Settings appearance deep link after an intentionally closed desktop snapshot', () => {
    const old = createPawDesktopStore('system-settings', '/appearance');
    old.getState().closeWindow('system-settings');
    window.history.replaceState(null, '', '#/appearance');
    const restored = createPawDesktopStore(undefined, undefined, old.getState());
    syncPawOsRoute(restored);
    expect(restored.getState().windows['system-settings']?.initialRoute).toBe('/appearance');
  });

  it('opens a Lab deep link above a restored Room focus without closing its work', () => {
    const store = createPawDesktopStore('agent', '/agent?room=room-open');
    store.getState().bindAgentMain('agent', { kind: 'room', id: 'room-open', title: '正在协作' });
    store.getState().setCollaborationFocusGroup('room:room-open');
    window.location.hash = '#/eval-lab';

    syncPawOsRoute(store);

    expect(store.getState().activeWindowId).toBe('eval-lab');
    expect(store.getState().collaborationFocusGroup).toBeNull();
    expect(store.getState().collaborationFocusReturnWindowId).toBeNull();
    expect(store.getState().windows.agent?.target).toMatchObject({ kind: 'room', id: 'room-open' });
  });

  it('reconciles an existing App window to the current hash on mount', () => {
    const store = createPawDesktopStore('input-studio', '/input');
    window.location.hash = '#/input?view=lexicon';

    syncPawOsRoute(store);

    expect(store.getState().windows['input-studio']).toMatchObject({
      initialRoute: '/input?view=lexicon',
    });
  });

  it('lets a Room deep link replace a previously bound Session target', () => {
    const store = createPawDesktopStore('agent', '/agent?session=session-old');
    store.getState().bindAgentMain('agent', {
      kind: 'session',
      id: 'session-old',
      title: '旧 Session',
    });
    window.location.hash = '#/rooms?room=room-preview';

    syncPawOsRoute(store);

    expect(store.getState().windows.agent).toMatchObject({
      appId: 'agent',
      initialRoute: '/rooms?room=room-preview',
      title: 'Session 与 Room',
    });
    expect(store.getState().windows.agent?.target).toBeUndefined();
  });

  it('fails closed for an Extension App initial hash until its Package is enabled', () => {
    const extension = pawExtensionApps[0]!;
    window.location.hash = extension.route;
    const store = createPawDesktopStore(extension.id, extension.route);

    expect(store.getState().windows[extension.id]).toBeUndefined();

    store.getState().setExtensionAppGate('ready', new Set([extension.id]));
    syncPawOsRoute(store);

    expect(store.getState().windows[extension.id]).toMatchObject({
      appId: extension.id,
      initialRoute: extension.route,
    });
  });

  it('does not reopen an Extension App through the RouteBridge while Runtime is unavailable', () => {
    const extension = pawExtensionApps[0]!;
    window.location.hash = extension.route;
    const store = createPawDesktopStore();

    store.getState().setExtensionAppGate('unavailable', new Set([extension.id]));
    syncPawOsRoute(store);

    expect(store.getState().windows[extension.id]).toBeUndefined();
  });
});
