import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import fs from 'node:fs';
import vm from 'node:vm';
import { test } from 'node:test';

// Execute the real entrypoint with Electron/host boundaries substituted. Keep
// readiness and Host startup pending independently to reproduce macOS activate.
function boot() {
  let ready = false;
  let releaseReady;
  let windowCount = 0;
  let hostReady = false;
  const windows = [];
  const app = new EventEmitter();
  Object.assign(app, {
    setName() {}, setPath() {}, commandLine: { appendSwitch() {} },
    requestSingleInstanceLock: () => true,
    whenReady: () => new Promise((resolve) => { releaseReady = () => { ready = true; resolve(); }; }),
    isReady: () => ready,
    exit: (code) => assert.fail(`unexpected app exit ${code}`),
  });
  class BrowserWindow extends EventEmitter {
    static getAllWindows() { return windows; }
    constructor() {
      super();
      windowCount += 1;
      if (!ready) throw new Error('Cannot create BrowserWindow before app is ready');
      if (!hostReady) throw new Error('Window created before Host startup completed');
      this.webContents = new EventEmitter();
      this.webContents.setWindowOpenHandler = () => {};
      windows.push(this);
    }
    loadURL() { return Promise.resolve(); }
  }
  const source = fs.readFileSync(new URL('./main.mjs', import.meta.url), 'utf8')
    .replace(/^import[\s\S]*?;\n/gm, '');
  const context = vm.createContext({
    app, BrowserWindow, process: { argv: [], env: {}, pid: 123, platform: 'darwin' },
    console, crypto: { randomBytes: () => ({ toString: () => 'test-token' }) },
    fs: { readFileSync() { throw new Error('absent fixture'); }, mkdirSync() {}, writeFileSync() {} },
    resolveHostPaths: () => ({ hostPidFile: '/tmp/test-host.pid' }),
    assistantLaunchIntent: () => null,
    browserWindowChrome: () => ({}),
    restoreInterruptedDownloads() {},
    startPawHostServer: () => new Promise(() => {}),
    defaultPawHostPort: 12345,
  });
  vm.runInContext(source, context, { filename: 'main.mjs' });
  return { app, releaseReady, windowCount: () => windowCount,
    completeHost() { hostReady = true; vm.runInContext("hostServer = { origin: 'http://127.0.0.1:12345' }", context); } };
}

test('macOS activate before Electron readiness does not create a BrowserWindow', () => {
  const host = boot();
  assert.doesNotThrow(() => host.app.emit('activate'));
  assert.equal(host.windowCount(), 0);
});

test('macOS activate while Host startup is pending does not create a BrowserWindow', async () => {
  const host = boot();
  host.releaseReady();
  await Promise.resolve();
  assert.doesNotThrow(() => host.app.emit('activate'));
  assert.equal(host.windowCount(), 0);
});


test('activate after readiness reopens one window without duplicating it', async () => {
  const host = boot();
  host.releaseReady();
  await Promise.resolve();
  host.completeHost();
  host.app.emit('activate');
  host.app.emit('activate');
  assert.equal(host.windowCount(), 1);
});
