import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { test } from 'node:test';
import fs from 'node:fs';
import vm from 'node:vm';
import { clampPetPosition, installDesktopPet } from './desktop-pet.mjs';

function harness({ deferred = false, autoReady = true } = {}) {
  const handlers = new Map(); const created = []; const visibility = []; const openedIds = [];
  const source = Object.assign(new EventEmitter(), { mainFrame: {}, isDestroyed: () => false, getURL: () => 'http://127.0.0.1:7777/' });
  let opened = 0; let voiceOpened = 0; let cursor = { x: 500, y: 500 };
  let area = { x: 0, y: 0, width: 1200, height: 800 };
  class Window extends EventEmitter {
    constructor(options) {
      super(); this.options = options; this.position = [options.x, options.y]; this.size = [options.width, options.height]; this.destroyed = false; this.visible = false;
      this.sent = [];
      this.webContents = Object.assign(new EventEmitter(), { mainFrame: {}, getURL: () => this.url, send: (...args) => this.sent.push(args),
        setWindowOpenHandler: (handler) => { this.openHandler = handler; } });
      created.push(this);
    }
    isDestroyed() { return this.destroyed; }
    isVisible() { return this.visible; }
    destroy() { this.destroyed = true; this.visible = false; this.emit('closed'); }
    getPosition() { return this.position; }
    getSize() { return this.size; }
    setSize(width, height) { this.size = [width, height]; }
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
  const manager = installDesktopPet({ app, BrowserWindow: Window, ipcMain: { handle: (name, handler) => handlers.set(name, handler), removeHandler: (name) => handlers.delete(name) },
    screen, origin: 'http://127.0.0.1:7777', preload: '/test/desktop-pet-preload.cjs',
    getSource: () => source, openAssistant: (id) => { opened += 1; openedIds.push(id); }, openVoiceSettings: () => { voiceOpened += 1; }, onVisibilityChanged: (value) => visibility.push(value) });
  return { manager, created, app, screen, visibility, source, openedIds, opened: () => opened, voiceOpened: () => voiceOpened,
    cursor: (point) => { cursor = point; }, area: (value) => { area = value; },
    invoke: (name, value, sender = created.at(-1)?.webContents, senderFrame = sender?.mainFrame) => handlers.get(`paw-pet:${name}`)({ sender, senderFrame }, value),
    publish: (name, value) => handlers.get(`paw-pet-state:${name}`)({ sender: source, senderFrame: source.mainFrame }, value),
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
  const h = harness({ autoReady: false }); const pending = h.manager.show(); await Promise.resolve();
  const window = h.created[0]; assert.equal(window.visible, false);
  assert.equal(h.manager.isVisible(), false); assert.deepEqual(h.visibility, [false]);
  h.invoke('ready'); assert.equal(await pending, true); assert.equal(window.visible, true);
  assert.deepEqual(h.visibility, [false, true]);
  h.manager.hide();
  assert.throws(() => h.invoke('ready', undefined, window.webContents), /rejected/);
});

test('missing renderer readiness fails closed and permits a fresh presentation', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const h = harness({ autoReady: false }); const pending = h.manager.show(); await Promise.resolve();
  assert.equal(h.manager.isVisible(), false);
  t.mock.timers.tick(10_000);
  assert.equal(await pending, false); assert.equal(h.created[0].destroyed, true);
  assert.ok(h.visibility.every(value => value === false));
  const retry = h.manager.show(); await Promise.resolve();
  h.invoke('ready'); assert.equal(await retry, true);
  h.manager.hide();
});

test('keyboard movement is bounded by the same host-owned work area', async () => {
  const h = harness(); await h.manager.show(); const window = h.created[0];
  window.setPosition(4, 4); h.invoke('move', 'left'); h.invoke('move', 'up');
  assert.deepEqual(window.position, [0, 0]);
  h.invoke('move', 'right'); h.invoke('move', 'down');
  assert.deepEqual(window.position, [16, 16]);
  assert.throws(() => h.invoke('move', { x: 5000 }), /Invalid/);
  assert.throws(() => h.invoke('move', 'left', {}), /rejected/);
  h.manager.hide();
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
  h.created[1].emit('ready-to-show'); h.created[1].resolve(); assert.equal(await next, true);
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
  assert.deepEqual(clampPetPosition({ x: 10, y: 99 }, { x: -1200, y: -800, width: 1200, height: 800 }), { x: -160, y: -240 });
  assert.deepEqual(clampPetPosition({ x: 99, y: 99 }, { x: 5, y: 10, width: 100, height: 100 }), { x: 5, y: 10 });
});

test('replays retained conversations on ready, opens only those ids and clears on source loss', async () => {
  const h = harness();
  const { producerEpoch } = h.publish('begin', { schemaVersion: 1, sourceId: 'work-directory', scopeId: 'scope' });
  h.publish('publish', { schemaVersion: 1, producerEpoch, revision: 1, freshness: 'synced',
    counts: { running: 1, attention: 0, error: 0, paused: 0, idle: 0, terminal: 0, unknown: 0 },
    conversations: [{ id: 'session-one', label: 'One', state: 'running' }] });
  await h.manager.show();
  assert.equal(h.invoke('ready').conversations[0].id, 'session-one');
  const target = { id: 'session-one', producerEpoch, sourceId: 'work-directory', scopeId: 'scope' };
  h.invoke('open-conversation', target); assert.deepEqual(h.openedIds, ['session-one']);
  assert.throws(() => h.invoke('open-conversation', { ...target, id: 'other' }), /rejected/);
  h.source.emit('destroyed');
  assert.equal(h.created[0].sent.at(-1)[1].freshness, 'unavailable');
  assert.throws(() => h.invoke('open-conversation', target), /rejected/);
});

test('expansion uses fixed host bounds and expanded dragging stays inside the work area', async () => {
  const h = harness(); await h.manager.show(); const window = h.created[0];
  h.invoke('expand', true); assert.deepEqual(window.size, [320, 440]);
  assert.ok(window.position[0] <= 880); assert.ok(window.position[1] <= 360);
  h.invoke('drag', 'start'); h.cursor({ x: 10000, y: 10000 }); h.invoke('drag', 'end');
  assert.ok(window.position[0] <= 880); assert.ok(window.position[1] <= 360);
  h.invoke('expand', false); assert.deepEqual(window.size, [160, 240]);
  assert.throws(() => h.invoke('expand', { width: 10000 }), /Invalid/);
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
  assert.deepEqual(Object.keys(exposed.pawDesktopPet), ['ready', 'onSnapshot', 'hide', 'openAssistant', 'openVoiceSettings', 'openConversation', 'setExpanded', 'drag', 'move']);
  assert.equal(Object.isFrozen(exposed.pawDesktopPet), true);
  await exposed.pawDesktopPet.ready(); await exposed.pawDesktopPet.hide(); await exposed.pawDesktopPet.openAssistant(); await exposed.pawDesktopPet.drag('cancel'); await exposed.pawDesktopPet.move('right');
  assert.deepEqual(calls, [['paw-pet:ready'], ['paw-pet:hide'], ['paw-pet:open-assistant'], ['paw-pet:drag', 'cancel'], ['paw-pet:move', 'right']]);
});


test('only the first-party workbench can show the companion; its actions reuse existing App entries', async () => {
  const h = harness();
  assert.throws(() => h.invoke('show-from-workbench', undefined, h.source, {}), /rejected/);
  assert.throws(() => h.invoke('show-from-workbench', undefined, { getURL: () => 'http://127.0.0.1:7777/' }), /rejected/);
  assert.equal(h.created.length, 0);
  assert.equal(await h.invoke('show-from-workbench', undefined, h.source), true);
  h.invoke('open-assistant'); h.invoke('voice-settings');
  assert.equal(h.opened(), 1); assert.equal(h.voiceOpened(), 1);
  assert.throws(() => h.invoke('hide-from-workbench'), /rejected/);
  assert.equal(h.manager.isVisible(), true);
  h.invoke('hide-from-workbench', undefined, h.source);
  assert.equal(h.manager.isVisible(), false);
});
