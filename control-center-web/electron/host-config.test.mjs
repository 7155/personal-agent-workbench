import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';
import {
  browserPartition,
  computeFrontendDistDigest,
  defaultPawHostPort,
  isBrowserGuestUrl,
  resolveHostPaths,
  validateProductionFrontend,
} from './host-config.mjs';
import { startPawHostServer } from './local-server.mjs';
import {
  appendBrowserHistory,
  clearBrowserHistory,
  clearBrowserSessionData,
  listBrowserExtensions,
  readBrowserHistory,
  readBrowserSessionSettings,
  removeBrowserHistoryEntry,
} from './browser-session.mjs';
import { browserWindowChrome } from './window-chrome.mjs';

function computeTestDistDigest(root) {
  const digest = crypto.createHash('sha256');
  const visit = (directory, relativeDirectory) => {
    const entries = fs.readdirSync(directory, { withFileTypes: true })
      .sort((left, right) => left.name < right.name ? -1 : left.name > right.name ? 1 : 0);
    for (const entry of entries) {
      const relative = relativeDirectory
        ? `${relativeDirectory}/${entry.name}`
        : entry.name;
      if (relative === 'rag-ime-control-web-build.json') continue;
      const target = path.join(directory, entry.name);
      if (entry.isDirectory()) {
        visit(target, relative);
      } else if (entry.isFile()) {
        digest.update(relative);
        digest.update('\0');
        digest.update(fs.readFileSync(target));
        digest.update('\0');
      }
    }
  };
  visit(root, '');
  return digest.digest('hex');
}

test('uses one fixed persistent PAW Browser profile and the built PAWOS entry', () => {
  const paths = resolveHostPaths({
    HOME: '/Users/example',
    RAG_IME_APP_SUPPORT_DIR: '/tmp/paw-app-support',
  });

  assert.equal(browserPartition, 'persist:paw-browser');
  assert.equal(defaultPawHostPort, 8770);
  assert.equal(paths.profilePath, path.resolve('/tmp/paw-app-support/Browser/runtime-profile'));
  assert.match(paths.frontendEntry, /control-center-web\/dist\/index\.html$/);
  assert.equal(paths.hostPidFile, path.join(paths.profilePath, 'PAWBrowserHost.pid'));
  assert.equal(paths.browserHistoryFile, path.join(paths.profilePath, 'PAWBrowserHost.history.json'));
  assert.equal(paths.browserBookmarksFile, path.join(paths.profilePath, 'PAWBrowserHost.bookmarks.json'));
  assert.equal(paths.browserDownloadsFile, path.join(paths.profilePath, 'PAWBrowserHost.downloads.json'));
  assert.equal(paths.browserExtensionsDir, path.join(paths.profilePath, 'Extensions'));
});

test('development host mode is explicit and permits a local frontend override', () => {
  const paths = resolveHostPaths({
    HOME: '/Users/example',
    PAW_FRONTEND_ENTRY: '/tmp/paw-dev/index.html',
    PAW_HOST_MODE: 'development',
    RAG_IME_APP_SUPPORT_DIR: '/tmp/paw-app-support',
  });

  assert.equal(paths.hostMode, 'development');
  assert.equal(paths.production, false);
  assert.equal(paths.frontendEntry, path.resolve('/tmp/paw-dev/index.html'));
});

test('production host rejects overrides and missing, legacy, or stale frontend markers', () => {
  assert.throws(
    () => resolveHostPaths({ PAW_FRONTEND_ENTRY: '/tmp/override/index.html', PAW_HOST_MODE: 'production' }),
    /PAW_FRONTEND_ENTRY is not allowed for production Electron hosts/,
  );

  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'paw-production-marker-'));
  const entry = path.join(root, 'index.html');
  const assets = path.join(root, 'assets');
  fs.mkdirSync(assets);
  fs.writeFileSync(entry, '<!doctype html><title>PAWOS</title>', 'utf8');
  fs.writeFileSync(path.join(assets, 'main.js'), 'console.log("paw-os");', 'utf8');
  const markerPath = path.join(root, 'rag-ime-control-web-build.json');
  const marker = {
    buildChannel: 'production',
    frontendProduct: 'paw-os',
    schemaVersion: 'rag-ime.control-web-build.v1',
    sourceCommit: 'a'.repeat(40),
    transport: 'http',
    distTreeDigest: computeTestDistDigest(root),
  };
  try {
    assert.throws(
      () => validateProductionFrontend(path.join(root, 'missing-index.html')),
      /frontend entry is missing/,
    );
    fs.writeFileSync(markerPath, JSON.stringify({ ...marker, frontendProduct: 'legacy' }), 'utf8');
    assert.throws(() => validateProductionFrontend(entry), /legacy frontend product/);
    fs.writeFileSync(markerPath, JSON.stringify({ ...marker, distTreeDigest: '0'.repeat(64) }), 'utf8');
    assert.throws(() => validateProductionFrontend(entry), /frontend marker is stale/);
    fs.writeFileSync(markerPath, JSON.stringify(marker), 'utf8');
    assert.equal(validateProductionFrontend(entry).frontendProduct, 'paw-os');
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('same-window guests accept browser pages but not local host files', () => {
  assert.equal(isBrowserGuestUrl('https://example.com/docs'), true);
  assert.equal(isBrowserGuestUrl('about:blank'), true);
  assert.equal(isBrowserGuestUrl('chrome://history/'), false);
  assert.equal(isBrowserGuestUrl('file:///tmp/index.html'), false);
  assert.equal(isBrowserGuestUrl('javascript:alert(1)'), false);
});

test('macOS merges native traffic lights into the one draggable PAW topbar', () => {
  assert.deepEqual(browserWindowChrome('darwin'), {
    titleBarStyle: 'hidden',
    trafficLightPosition: { x: 14, y: 11 },
  });
  assert.deepEqual(browserWindowChrome('linux'), { titleBarStyle: 'default' });

  const css = fs.readFileSync(path.resolve(import.meta.dirname, '../src/paw-os/styles/paw-os.css'), 'utf8');
  const main = fs.readFileSync(path.resolve(import.meta.dirname, 'main.mjs'), 'utf8');
  assert.match(main, /\.\.\.browserWindowChrome\(\)/);
  assert.doesNotMatch(main, /\bframe:\s*false/);
  assert.match(css, /html\[data-paw-native-host='macos'\] \.paw-menu-bar/);
  assert.match(css, /grid-template-columns: 68px max-content minmax\(0, 1fr\) auto/);
  assert.match(css, /html\[data-paw-native-host='macos'\] \.paw-menu-bar\s*\{[^}]*-webkit-app-region:\s*drag;/s);
  assert.match(css, /html\[data-paw-native-host='macos'\] \.paw-menu-bar > :is\(button, \.paw-menu-menus, \.paw-menu-status\)\s*\{[^}]*-webkit-app-region:\s*no-drag;/s);
});

test('preload marks only a macOS Electron document for the PAWOS native-host seam', () => {
  const preload = fs.readFileSync(path.resolve(import.meta.dirname, 'preload.cjs'), 'utf8');
  const macos = runPreload(preload, 'darwin', 'loading');
  const linux = runPreload(preload, 'linux');
  assert.equal(macos.browserHost.nativeHost, 'macos');
  assert.equal(macos.dataset.pawNativeHost, 'macos');
  assert.equal(macos.terminalHost, undefined);
  assert.equal(linux.browserHost.nativeHost, 'electron');
  assert.equal(linux.dataset.pawNativeHost, undefined);
});

test('preload reads and mutates Browser History through the host-owned IPC seam', async () => {
  const preload = fs.readFileSync(path.resolve(import.meta.dirname, 'preload.cjs'), 'utf8');
  const host = runPreload(preload, 'darwin');

  await host.browserHost.getHistory();
  await host.browserHost.removeHistoryEntry('history-entry');
  await host.browserHost.clearHistory();
  const unsubscribe = host.browserHost.onHistoryChanged(() => undefined);
  unsubscribe();

  assert.deepEqual(host.ipcCalls, [
    ['paw-browser:get-history'],
    ['paw-browser:remove-history-entry', 'history-entry'],
    ['paw-browser:clear-history'],
  ]);
  assert.deepEqual(host.ipcListeners.filter(([, channel]) => channel.startsWith('paw-browser:')), [
    ['on', 'paw-browser:history-updated'],
    ['off', 'paw-browser:history-updated'],
  ]);
});

test('preload exposes host-owned bookmarks and download operations through validated IPC IDs', async () => {
  const preload = fs.readFileSync(path.resolve(import.meta.dirname, 'preload.cjs'), 'utf8');
  const host = runPreload(preload, 'darwin');

  await host.browserHost.getBookmarks();
  await host.browserHost.addBookmark({ title: 'PAW docs', url: 'https://example.com/docs' });
  await host.browserHost.removeBookmark('bookmark-1');
  await host.browserHost.getDownloads();
  await host.browserHost.openDownload('download-1');
  await host.browserHost.revealDownload('download-1');
  await host.browserHost.cancelDownload('download-1');
  const unsubscribeBookmarks = host.browserHost.onBookmarksChanged(() => undefined);
  const unsubscribeDownloads = host.browserHost.onDownloadsChanged(() => undefined);
  unsubscribeBookmarks();
  unsubscribeDownloads();

  assert.deepEqual(host.ipcCalls, [
    ['paw-browser:get-bookmarks'],
    ['paw-browser:add-bookmark', { title: 'PAW docs', url: 'https://example.com/docs' }],
    ['paw-browser:remove-bookmark', 'bookmark-1'],
    ['paw-browser:get-downloads'],
    ['paw-browser:open-download', 'download-1'],
    ['paw-browser:reveal-download', 'download-1'],
    ['paw-browser:cancel-download', 'download-1'],
  ]);
  assert.deepEqual(host.ipcListeners.filter(([, channel]) => channel.startsWith('paw-browser:')), [
    ['on', 'paw-browser:bookmarks-updated'],
    ['on', 'paw-browser:downloads-updated'],
    ['off', 'paw-browser:bookmarks-updated'],
    ['off', 'paw-browser:downloads-updated'],
  ]);
});

test('preload exposes the Electron-owned workspace directory picker', async () => {
  const preload = fs.readFileSync(path.resolve(import.meta.dirname, 'preload.cjs'), 'utf8');
  const host = runPreload(preload, 'darwin');

  await host.browserHost.pickWorkspaceDirectory();

  assert.deepEqual(host.ipcCalls, [
    ['paw-host:pick-workspace-directory'],
  ]);
});

test('Electron keeps workspace selection inside the installed PAW window', () => {
  const main = fs.readFileSync(path.resolve(import.meta.dirname, 'main.mjs'), 'utf8');
  assert.match(main, /ipcMain\.handle\('paw-host:pick-workspace-directory'/);
  assert.match(main, /dialog\.showOpenDialog\(mainWindow/);
  assert.match(main, /properties:\s*\['openDirectory', 'createDirectory'\]/);
});

test('Electron sends Agent writes to the execution-owning Gateway by default', () => {
  const main = fs.readFileSync(path.resolve(import.meta.dirname, 'main.mjs'), 'utf8');
  const localServer = fs.readFileSync(path.resolve(import.meta.dirname, 'local-server.mjs'), 'utf8');

  assert.match(main, /PAW_CONTROL_ORIGIN \|\| 'http:\/\/127\.0\.0\.1:8768'/);
  assert.match(localServer, /controlOrigin = 'http:\/\/127\.0\.0\.1:8768'/);
});

test('Electron records History from guest navigation events rather than renderer claims', () => {
  const main = fs.readFileSync(path.resolve(import.meta.dirname, 'main.mjs'), 'utf8');
  assert.match(main, /guestContents\.on\('did-navigate'/);
  assert.match(main, /guestContents\.on\('did-navigate-in-page'/);
  assert.doesNotMatch(main, /paw-browser:record-history/);
});

test('Electron persists Browser bookmarks and tracks downloads through the main-owned partition', () => {
  const main = fs.readFileSync(path.resolve(import.meta.dirname, 'main.mjs'), 'utf8');
  assert.match(main, /session\.fromPartition\(browserPartition\)/);
  assert.match(main, /persistentBrowserSession\.on\('will-download'/);
  assert.match(main, /trackDownload\(paths\.browserDownloadsFile/);
  assert.match(main, /onError: \(stage\) => console\.error\('Could not persist PAW Browser Download metadata', stage\)/);
  assert.match(main, /paw-browser:get-bookmarks/);
  assert.match(main, /paw-browser:add-bookmark/);
  assert.match(main, /paw-browser:remove-bookmark/);
  assert.match(main, /paw-browser:get-downloads/);
  assert.match(main, /paw-browser:open-download/);
  assert.match(main, /paw-browser:reveal-download/);
  assert.match(main, /paw-browser:cancel-download/);
  assert.match(main, /event\.sender !== mainWindow\?\.webContents/);
});

test('Electron exposes no external terminal launcher; Terminal remains a PAWOS PTY', () => {
  const preload = fs.readFileSync(path.resolve(import.meta.dirname, 'preload.cjs'), 'utf8');
  const main = fs.readFileSync(path.resolve(import.meta.dirname, 'main.mjs'), 'utf8');
  assert.doesNotMatch(preload, /pawTerminalHost|paw-terminal:/);
  assert.doesNotMatch(main, /terminal-host|paw-terminal:|Ghostty|Terminal\.app/);
});

test('host serves PAWOS and proxies GET, POST, and event streams through one origin', async () => {
  const requests = [];
  const upstream = http.createServer((request, response) => {
    let body = '';
    request.setEncoding('utf8');
    request.on('data', (chunk) => { body += chunk; });
    request.on('end', () => {
      requests.push({ body, headers: request.headers, method: request.method, url: request.url });
      if (request.url === '/api/events') {
        response.writeHead(200, { 'Content-Type': 'text/event-stream' });
        response.end('event: ready\ndata: {"ok":true}\n\n');
        return;
      }
      if (request.url === '/__paw_html_preview') {
        response.writeHead(200, { 'Content-Type': 'text/html', 'Content-Security-Policy': "sandbox allow-scripts allow-downloads" });
        response.end('<!doctype html><title>PAW HTML Preview</title>');
        return;
      }
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ body, method: request.method, ok: true }));
    });
  });
  await listen(upstream);
  const upstreamAddress = upstream.address();
  assert.ok(upstreamAddress && typeof upstreamAddress !== 'string');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'paw-electron-host-'));
  const frontendEntry = path.join(root, 'index.html');
  fs.writeFileSync(frontendEntry, '<!doctype html><title>PAWOS</title>', 'utf8');
  const host = await startPawHostServer({
    browserBridge: {
      activateTarget: async (targetId) => ({ ok: true, targetId }),
      createTab: async (url) => ({ ok: true, targetId: `target:${url}` }),
      token: 'host-token',
    },
    frontendEntry,
    controlOrigin: `http://127.0.0.1:${upstreamAddress.port}`,
    port: 0,
  });
  try {
    assert.match(await (await fetch(`${host.origin}/`)).text(), /PAWOS/);
    assert.equal((await (await fetch(`${host.origin}/api/health`)).json()).method, 'GET');
    const posted = await fetch(`${host.origin}/api/probe`, {
      body: '{"check":true}',
      headers: { 'Content-Type': 'application/json', Origin: host.origin },
      method: 'POST',
    });
    assert.equal((await posted.json()).body, '{"check":true}');
    const rejected = await fetch(`${host.origin}/api/probe`, {
      body: '{}',
      headers: { 'Content-Type': 'application/json', Origin: 'http://evil.invalid' },
      method: 'POST',
    });
    assert.equal(rejected.status, 403);
    const created = await fetch(`${host.origin}/__paw_browser/tabs`, {
      body: '{"url":"https://example.com"}',
      headers: { 'Content-Type': 'application/json', 'X-Paw-Browser-Token': 'host-token' },
      method: 'POST',
    });
    assert.equal((await created.json()).targetId, 'target:https://example.com');
    assert.match(await (await fetch(`${host.origin}/api/events`)).text(), /event: ready/);
    assert.equal(requests[1].headers.origin, `http://127.0.0.1:${upstreamAddress.port}`);
    assert.equal(requests[1].headers.host, `127.0.0.1:${upstreamAddress.port}`);
    for (const method of ['GET', 'HEAD']) {
      const preview = await fetch(`${host.origin}/__paw_html_preview#private-html`, { method });
      assert.equal(preview.headers.get('content-security-policy'), 'sandbox allow-scripts allow-downloads');
      assert.equal(requests.at(-1).url, '/__paw_html_preview');
      if (method === 'GET') assert.match(await preview.text(), /PAW HTML Preview/);
    }
  } finally {
    await host.close();
    await new Promise((resolve) => upstream.close(resolve));
    fs.rmSync(root, { recursive: true });
  }
});

test('host falls back to an ephemeral port when its preferred port is occupied', async () => {
  const occupied = http.createServer((_request, response) => {
    response.writeHead(200, { 'Content-Type': 'text/plain' });
    response.end('occupied');
  });
  await listen(occupied);
  const occupiedAddress = occupied.address();
  assert.ok(occupiedAddress && typeof occupiedAddress !== 'string');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'paw-electron-host-fallback-'));
  const frontendEntry = path.join(root, 'index.html');
  fs.writeFileSync(frontendEntry, '<!doctype html><title>PAWOS fallback</title>', 'utf8');
  let host = null;
  try {
    host = await startPawHostServer({
      fallbackToEphemeralPort: true,
      frontendEntry,
      hostMode: 'development',
      port: occupiedAddress.port,
    });
    assert.notEqual(Number(new URL(host.origin).port), occupiedAddress.port);
    assert.match(await (await fetch(`${host.origin}/`)).text(), /PAWOS fallback/);
  } finally {
    await host?.close();
    await new Promise((resolve) => occupied.close(resolve));
    fs.rmSync(root, { recursive: true });
  }
});

test('Browser settings report and clear the fixed persistent session', async () => {
  let cacheBytes = 8192;
  let cookieCount = 3;
  const electronSession = {
    clearCache: async () => { cacheBytes = 0; },
    clearStorageData: async () => { cookieCount = 0; },
    cookies: { get: async () => Array.from({ length: cookieCount }, () => ({})) },
    extensions: { getAllExtensions: () => new Map() },
    flushStorageData: () => undefined,
    getCacheSize: async () => cacheBytes,
  };

  assert.deepEqual(await readBrowserSessionSettings({
    downloadsPath: '/Users/example/Downloads',
    electronSession,
    extensionsPath: '/Users/example/Extensions',
    startPage: 'about:blank',
  }), {
    cacheBytes: 8192,
    cookieCount: 3,
    downloadPath: '/Users/example/Downloads',
    extensionCount: 0,
    extensionsPath: '/Users/example/Extensions',
    partition: 'persist:paw-browser',
    permissionMode: 'site-request',
    startPage: 'about:blank',
  });
  assert.deepEqual(await clearBrowserSessionData(electronSession, 'cache'), {
    action: 'cache',
    before: 8192,
    after: 0,
  });
  assert.deepEqual(await clearBrowserSessionData(electronSession, 'site-data'), {
    action: 'site-data',
    before: 3,
    after: 0,
  });
});

test('Browser settings report the extensions the persistent session actually loaded', async () => {
  const extensions = new Map([
    ['abc', { id: 'abc', name: '广告拦截', path: '/Users/example/Extensions/abc', version: '1.4.0', manifest: {} }],
    ['def', { id: 'def', name: '取色器', path: '/Users/example/Extensions/def', version: '0.9.2', manifest: {} }],
  ]);
  const electronSession = {
    cookies: { get: async () => [] },
    extensions: { getAllExtensions: () => extensions },
    getCacheSize: async () => 0,
  };

  assert.deepEqual(listBrowserExtensions(electronSession), [
    { id: 'abc', name: '广告拦截', path: '/Users/example/Extensions/abc', version: '1.4.0' },
    { id: 'def', name: '取色器', path: '/Users/example/Extensions/def', version: '0.9.2' },
  ]);
  const settings = await readBrowserSessionSettings({
    downloadsPath: '/Users/example/Downloads',
    electronSession,
    extensionsPath: '/Users/example/Extensions',
    startPage: 'about:blank',
  });
  assert.equal(settings.extensionCount, 2);
  assert.equal(settings.extensionsPath, '/Users/example/Extensions');
});

test('Browser History is host-owned, persisted, bounded, and contains only real web pages', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'paw-browser-history-'));
  const historyFile = path.join(root, 'PAWBrowserHost.history.json');
  try {
    const first = appendBrowserHistory(historyFile, {
      title: 'PAW docs',
      url: 'https://example.com/paw',
      visitedAt: 10,
    });
    assert.equal(first.length, 1);
    assert.match(first[0].id, /^history-/);
    assert.deepEqual(readBrowserHistory(historyFile), first);

    const deduplicated = appendBrowserHistory(historyFile, {
      title: 'PAW docs updated',
      url: 'https://example.com/paw',
      visitedAt: 20,
    });
    assert.equal(deduplicated.length, 1);
    assert.equal(deduplicated[0].title, 'PAW docs updated');
    assert.equal(deduplicated[0].visitedAt, 20);

    assert.deepEqual(appendBrowserHistory(historyFile, {
      title: 'Blank',
      url: 'about:blank',
      visitedAt: 30,
    }), deduplicated);

    const second = appendBrowserHistory(historyFile, {
      title: 'Reference',
      url: 'https://reference.example/',
      visitedAt: 40,
    });
    assert.deepEqual(removeBrowserHistoryEntry(historyFile, second[1].id), [second[0]]);
    assert.deepEqual(clearBrowserHistory(historyFile), []);
    assert.deepEqual(readBrowserHistory(historyFile), []);
  } finally {
    fs.rmSync(root, { recursive: true });
  }
});

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
}

function runPreload(source, platform, readyState = 'complete') {
  let onReady;
  const document = {
    documentElement: { dataset: {} },
    readyState,
    addEventListener: (_event, listener) => { onReady = listener; },
  };
  const exposed = {};
  const ipcCalls = [];
  const ipcListeners = [];
  vm.runInNewContext(source, {
    document,
    Object,
    process: { platform },
    require: () => ({
      contextBridge: { exposeInMainWorld: (name, value) => { exposed[name] = value; } },
      ipcRenderer: {
        invoke: (...args) => {
          ipcCalls.push(args);
          return Promise.resolve(undefined);
        },
        off: (channel) => { ipcListeners.push(['off', channel]); },
        on: (channel) => { ipcListeners.push(['on', channel]); },
      },
    }),
  });
  onReady?.();
  return {
    browserHost: exposed.pawBrowserHost,
    dataset: document.documentElement.dataset,
    ipcCalls,
    ipcListeners,
    terminalHost: exposed.pawTerminalHost,
  };
}

import { EventEmitter } from 'node:events';

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
