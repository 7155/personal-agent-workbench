import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { test } from 'node:test';
import fs from 'node:fs';
import vm from 'node:vm';
import { clampPetPosition, installDesktopPet } from './desktop-pet.mjs';

function harness({ deferred = false, autoReady = true } = {}) {
  const handlers = new Map(); const created = []; const visibility = [];
  let opened = 0; let cursor = { x: 500, y: 500 };
  let area = { x: 0, y: 0, width: 1200, height: 800 };
  class Window extends EventEmitter {
    constructor(options) {
      super(); this.options = options; this.position = [options.x, options.y]; this.destroyed = false; this.visible = false;
      this.webContents = Object.assign(new EventEmitter(), { mainFrame: {}, getURL: () => this.url,
        setWindowOpenHandler: (handler) => { this.openHandler = handler; } });
      created.push(this);
    }
    isDestroyed() { return this.destroyed; }
    destroy() { this.destroyed = true; this.visible = false; this.emit('closed'); }
    getPosition() { return this.position; }
    setPosition(x, y) { this.position = [x, y]; }
    showInactive() { this.visible = true; }
    loadURL(url) {
      this.url = url;
      if (autoReady) handlers.get('paw-pet:ready')({ sender: this.webContents, senderFrame: this.webContents.mainFrame });
      if (deferred) return new Promise((resolve, reject) => { this.resolve = resolve; this.reject = reject; });
      this.emit('ready-to-show'); return Promise.resolve();
    }
  }
  const app = new EventEmitter();
  const screen = Object.assign(new EventEmitter(), { getCursorScreenPoint: () => cursor,
    getDisplayNearestPoint: () => ({ id: 1, workArea: area }) });
  const manager = installDesktopPet({ app, BrowserWindow: Window, ipcMain: { handle: (name, handler) => handlers.set(name, handler) },
    screen, origin: 'http://127.0.0.1:7777', preload: '/test/desktop-pet-preload.cjs',
    openAssistant: () => { opened += 1; }, onVisibilityChanged: (value) => visibility.push(value) });
  return { manager, created, app, screen, visibility, opened: () => opened,
    cursor: (point) => { cursor = point; }, area: (value) => { area = value; },
    invoke: (name, value, sender = created.at(-1)?.webContents, senderFrame = sender?.mainFrame) => handlers.get(`paw-pet:${name}`)({ sender, senderFrame }, value),
  };
}

test('pet is opt-in, opens one sandboxed surface, and reuses the assistant entry', async () => {
  const h = harness();
  assert.equal(h.created.length, 0);
  assert.equal(h.manager.isVisible(), false);
  const first = h.manager.show(); const second = h.manager.show();
  assert.equal(first, second);
  assert.equal(await first, true);
  const window = h.created[0];
  assert.equal(h.created.length, 1);
  assert.equal(window.url, 'http://127.0.0.1:7777/desktop-pet');
  assert.equal(window.visible, true);
  assert.deepEqual(window.options.webPreferences, { sandbox: true, nodeIntegration: false, contextIsolation: true, preload: '/test/desktop-pet-preload.cjs' });
  assert.deepEqual(window.openHandler(), { action: 'deny' });
  h.invoke('open-assistant'); assert.equal(h.opened(), 1);
  h.invoke('hide'); assert.equal(window.destroyed, true); assert.equal(h.manager.isVisible(), false);
});

test('hide invalidates pending loading and stale ready events without hiding a newer pet', async () => {
  const h = harness({ deferred: true });
  const first = h.manager.show(); await Promise.resolve();
  const old = h.created[0]; h.manager.hide();
  const second = h.manager.show(); await Promise.resolve();
  const current = h.created[1];
  old.emit('ready-to-show'); old.resolve();
  assert.equal(await first, false); assert.equal(old.visible, false);
  current.emit('ready-to-show'); current.resolve();
  assert.equal(await second, true); assert.equal(current.visible, true);
  assert.equal(h.manager.isVisible(), true);
});

test('the shared boot screen stays hidden until the pet renderer acknowledges readiness', async () => {
  const h = harness({ autoReady: false }); await h.manager.show();
  const window = h.created[0]; assert.equal(window.visible, false);
  h.invoke('ready'); assert.equal(window.visible, true);
  h.manager.hide();
  assert.throws(() => h.invoke('ready', undefined, window.webContents), /rejected/);
});

test('quit cancels pending presentation and removes display observers', async () => {
  const h = harness({ deferred: true }); const pending = h.manager.show(); await Promise.resolve();
  const window = h.created[0]; h.app.emit('before-quit');
  window.emit('ready-to-show'); window.resolve();
  assert.equal(await pending, false); assert.equal(window.visible, false);
  assert.equal(h.screen.listenerCount('display-removed'), 0);
  assert.equal(h.screen.listenerCount('display-metrics-changed'), 0);
  assert.equal(await h.manager.show(), false);
  assert.equal(h.created.length, 1);
});

test('failed loads clear the toggle and permit a new show', async () => {
  const h = harness({ deferred: true }); const first = h.manager.show(); await Promise.resolve();
  h.created[0].reject(new Error('load failed'));
  assert.equal(await first, false); assert.equal(h.manager.isVisible(), false);
  const next = h.manager.show(); await Promise.resolve();
  h.created[1].resolve(); assert.equal(await next, true);
});

test('IPC rejects foreign windows, subframes, navigated senders and invalid phases', async () => {
  const h = harness(); await h.manager.show(); const window = h.created[0];
  assert.throws(() => h.invoke('hide', undefined, { getURL: () => window.url }), /rejected/);
  assert.throws(() => h.invoke('hide', undefined, window.webContents, {}), /rejected/);
  assert.throws(() => h.invoke('drag', { x: 100 }), /Invalid/);
  window.url = 'https://example.com/desktop-pet';
  assert.throws(() => h.invoke('open-assistant'), /rejected/);
  assert.equal(h.opened(), 0);
});

test('external navigation, redirects, queries and sibling routes are blocked', async () => {
  const h = harness(); await h.manager.show(); const window = h.created[0];
  for (const eventName of ['will-navigate', 'will-redirect']) {
    for (const url of ['https://example.com', 'http://127.0.0.1:7777/agent', `${window.url}?other=1`, 'invalid']) {
      let prevented = false;
      window.webContents.emit(eventName, { preventDefault: () => { prevented = true; } }, url);
      assert.equal(prevented, true);
    }
  }
});

test('drag uses host cursor, clamps to work area, cancels and restores last position', async () => {
  const h = harness(); await h.manager.show(); const window = h.created[0];
  h.invoke('drag', 'start'); h.cursor({ x: -3000, y: -3000 }); h.invoke('drag', 'move');
  assert.deepEqual(window.position, [0, 0]);
  h.invoke('drag', 'cancel'); h.cursor({ x: 9999, y: 9999 }); h.invoke('drag', 'move');
  assert.deepEqual(window.position, [0, 0]);
  h.manager.hide(); await h.manager.show(); assert.deepEqual(h.created[1].position, [0, 0]);
  h.area({ x: 200, y: 100, width: 300, height: 300 }); h.screen.emit('display-removed');
  assert.deepEqual(h.created[1].position, [200, 100]);
});

test('negative coordinates and undersized work areas have bounded origins', () => {
  assert.deepEqual(clampPetPosition({ x: 10, y: 99 }, { x: -1200, y: -800, width: 1200, height: 800 }), { x: -160, y: -190 });
  assert.deepEqual(clampPetPosition({ x: 99, y: 99 }, { x: 5, y: 10, width: 100, height: 100 }), { x: 5, y: 10 });
});

test('drag end samples the final cursor after coalesced moves and blur cancels dragging', async () => {
  const h = harness(); await h.manager.show(); const window = h.created[0];
  window.setPosition(300, 300); h.cursor({ x: 300, y: 300 }); h.invoke('drag', 'start');
  h.cursor({ x: 310, y: 310 }); h.invoke('drag', 'move');
  h.cursor({ x: 330, y: 340 }); h.invoke('drag', 'end');
  assert.deepEqual(window.position, [330, 340]);
  h.invoke('drag', 'start'); window.emit('blur');
  h.cursor({ x: 400, y: 400 }); h.invoke('drag', 'move');
  assert.deepEqual(window.position, [330, 340]);
});

test('pet preload exposes only readiness and its three bounded host actions', async () => {
  const exposed = {}; const calls = [];
  vm.runInNewContext(fs.readFileSync(new URL('./desktop-pet-preload.cjs', import.meta.url), 'utf8'), {
    require: () => ({ contextBridge: { exposeInMainWorld: (key, value) => { exposed[key] = value; } },
      ipcRenderer: { invoke: (...args) => { calls.push(args); return Promise.resolve(); } } }),
  });
  assert.deepEqual(Object.keys(exposed), ['pawDesktopPet']);
  assert.deepEqual(Object.keys(exposed.pawDesktopPet), ['ready', 'hide', 'openAssistant', 'drag']);
  assert.equal(Object.isFrozen(exposed.pawDesktopPet), true);
  await exposed.pawDesktopPet.ready(); await exposed.pawDesktopPet.hide(); await exposed.pawDesktopPet.openAssistant(); await exposed.pawDesktopPet.drag('cancel');
  assert.deepEqual(calls, [['paw-pet:ready'], ['paw-pet:hide'], ['paw-pet:open-assistant'], ['paw-pet:drag', 'cancel']]);
});
