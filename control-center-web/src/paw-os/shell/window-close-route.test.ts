import { afterEach, describe, expect, it } from 'vitest';
import { createPawDesktopStore } from '../runtime/desktop-store';
import { closePawWindows } from './window-close-route';

const initialUrl = window.location.href;
afterEach(() => window.history.replaceState(null, '', initialUrl));

describe('intentional close reload route', () => {
  it.each([
    ['room', '/agent'], ['session', '/agent'], ['room', '/rooms'],
  ] as const)('clears a generic %s entry after the main window binds a target and closes', (kind, route) => {
    const store = createPawDesktopStore('agent', route);
    window.history.replaceState(null, '', `#${route}`);
    store.getState().bindAgentMain('agent', { kind, id: 'selected-work', title: '继续工作' });
    closePawWindows(store, () => store.getState().closeWindow('agent'));
    expect(Object.keys(store.getState().windows)).toEqual([]);
    expect(window.location.hash).toBe('#/project-field');
  });

  it.each([
    ['room', 'room/a', '/rooms?room=room%2Fa&view=history'],
    ['session', 'session/a', '/agent?sessionId=session%2Fa&view=files'],
  ] as const)('keeps exact %s binding and its open subpage after Settings closes', (kind, id, route) => {
    const store = createPawDesktopStore('agent');
    store.getState().bindAgentMain('agent', { kind, id, title: '当前工作' });
    store.getState().openApp('agent', { initialRoute: route });
    store.getState().openApp('system-settings', { initialRoute: '/appearance' });
    window.history.replaceState(null, '', '#/appearance');
    closePawWindows(store, () => store.getState().closeWindow('system-settings'));
    expect(window.location.hash).toBe(`#${route}`);
  });

  it('uses the current binding rather than an obsolete Room initial route', () => {
    const store = createPawDesktopStore('agent');
    store.getState().bindAgentMain('agent', { kind: 'room', id: 'current-room', title: 'Room' });
    store.getState().openApp('agent', { initialRoute: '/rooms?room=old-room' });
    store.getState().openApp('system-settings');
    window.history.replaceState(null, '', '#/appearance');
    closePawWindows(store, () => store.getState().closeWindow('system-settings'));
    expect(window.location.hash).toBe('#/agent?room=current-room');
  });

  it('preserves an open Lab subpage and query as the next reload destination', () => {
    const store = createPawDesktopStore('eval-lab', '/eval-lab?suite=regression&view=results');
    store.getState().openApp('system-settings');
    window.history.replaceState(null, '', '#/appearance');
    closePawWindows(store, () => store.getState().closeWindow('system-settings'));
    expect(window.location.hash).toBe('#/eval-lab?suite=regression&view=results');
  });

  it('does not confuse two Agent targets or redirect an unrelated close', () => {
    const store = createPawDesktopStore('agent');
    store.getState().bindAgentMain('agent', { kind: 'room', id: 'room-live', title: 'Room' });
    const other = store.getState().openApp('agent', { entityId: 'other', target: { kind: 'session', id: 'other', title: 'Other' } });
    window.history.replaceState(null, '', '#/rooms?room=room-live&view=history');
    closePawWindows(store, () => store.getState().closeWindow(other));
    expect(window.location.hash).toBe('#/rooms?room=room-live&view=history');
  });

  it('does not treat a Room panel as the main Room route owner', () => {
    const store = createPawDesktopStore('agent');
    store.getState().bindAgentMain('agent', { kind: 'room', id: 'room-live', title: 'Room' });
    const panel = store.getState().openApp('agent', { entityId: 'focus', target: { kind: 'room', id: 'room-live', panel: 'focus', title: 'Focus' } });
    window.history.replaceState(null, '', '#/rooms?room=room-live');
    closePawWindows(store, () => store.getState().closeWindow(panel));
    expect(window.location.hash).toBe('#/rooms?room=room-live');
  });

  it.each(['all', 'app'] as const)('clears the reload route when %s close removes its last owner', kind => {
    const store = createPawDesktopStore('system-settings', '/appearance');
    window.history.replaceState(null, '', '#/appearance');
    closePawWindows(store, () => kind === 'all' ? store.getState().closeAllWindows() : store.getState().closeAppWindows('system-settings'));
    expect(window.location.hash).toBe('#/project-field');
    expect(Object.keys(store.getState().windows)).toEqual([]);
  });
});
