import fs from 'node:fs';
import crypto from 'node:crypto';
import path from 'node:path';
import { app, BrowserWindow, dialog, globalShortcut, ipcMain, Menu, MenuItem, session, shell, systemPreferences } from 'electron';
import { browserPartition, defaultPawHostPort, isBrowserGuestUrl, resolveHostPaths } from './host-config.mjs';
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
import {
  addBookmark,
  cancelDownload as cancelBrowserDownload,
  getBookmarks,
  getDownloads,
  openDownload as openBrowserDownload,
  removeBookmark,
  restoreInterruptedDownloads,
  revealDownload as revealBrowserDownload,
  trackDownload,
} from './browser-library.mjs';
import { browserWindowChrome } from './window-chrome.mjs';
import { assistantLaunchIntent, assistantSessionRoute } from './assistant-launch.mjs';
import { installScreenAssistant } from './screen-assistant.mjs';

import { createVoiceControl, trustedVoiceSender } from './voice-control.mjs';

const paths = resolveHostPaths();

app.setName('Personal Agent Workbench');
app.setPath('userData', paths.profilePath);
app.commandLine.appendSwitch('remote-debugging-address', '127.0.0.1');
app.commandLine.appendSwitch('remote-debugging-port', process.env.PAW_REMOTE_DEBUGGING_PORT || '0');

let mainWindow = null;
let screenAssistant = null;
let pendingAssistantIntent = assistantLaunchIntent(process.argv);
let hostServer = null;
const guestRegistry = new Map();
const pendingTabs = new Map();
const activeDownloads = new Map();
const hostToken = crypto.randomBytes(24).toString('hex');
let browserStartPage = readBrowserStartPage();

function readBrowserStartPage() {
  try {
    const value = JSON.parse(fs.readFileSync(paths.browserSettingsFile, 'utf8'));
    return normalizedStartPage(value?.startPage);
  } catch {
    return 'about:blank';
  }
}

function normalizedStartPage(value) {
  const text = String(value || '').trim();
  if (text === 'about:blank') return text;
  try {
    const url = new URL(text);
    if (url.protocol === 'http:' || url.protocol === 'https:') return url.toString();
  } catch {
    // Invalid values are rejected below.
  }
  throw new Error('启动页必须是 http(s) 地址或 about:blank');
}

function sendToMainWindow(channel, payload) {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  mainWindow.webContents.send(channel, payload);
}

function createWindow(initialRoute, showWhenReady = true) {
  const window = new BrowserWindow({
    width: 1440,
    height: 940,
    minWidth: 960,
    minHeight: 640,
    backgroundColor: '#f4f1e9',
    title: 'Personal Agent Workbench',
    ...browserWindowChrome(),
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: paths.preloadEntry,
      sandbox: true,
      webviewTag: true,
    },
  });

  window.webContents.on('will-attach-webview', (_event, webPreferences, params) => {
    webPreferences.nodeIntegration = false;
    webPreferences.contextIsolation = true;
    webPreferences.sandbox = true;
    delete webPreferences.preload;
    params.partition = browserPartition;
  });
  window.webContents.on('did-attach-webview', (_event, guestContents) => {
    guestRegistry.set(guestContents.id, { contents: guestContents, tabId: '', targetId: '' });
    const recordGuestHistory = () => {
      if (guestContents.isDestroyed()) return;
      try {
        const history = appendBrowserHistory(paths.browserHistoryFile, {
          title: guestContents.getTitle(),
          url: guestContents.getURL(),
          visitedAt: Date.now(),
        });
        if (!window.isDestroyed()) window.webContents.send('paw-browser:history-updated', history);
      } catch (error) {
        console.error('Could not persist PAW Browser History', error);
      }
    };
    guestContents.on('did-navigate', recordGuestHistory);
    guestContents.on('did-navigate-in-page', recordGuestHistory);
    guestContents.on('page-title-updated', recordGuestHistory);
    guestContents.setWindowOpenHandler(({ url }) => {
      if (isBrowserGuestUrl(url)) {
        window.webContents.send('paw-browser:open-url', url);
      }
      return { action: 'deny' };
    });
    guestContents.once('destroyed', () => {
      const entry = guestRegistry.get(guestContents.id);
      guestRegistry.delete(guestContents.id);
      if (entry?.tabId) window.webContents.send('paw-browser:guest-closed', entry.tabId);
    });
  });
  window.webContents.setWindowOpenHandler(({ url }) => {
    if (isBrowserGuestUrl(url)) window.webContents.send('paw-browser:open-url', url);
    return { action: 'deny' };
  });

  const route = initialRoute || process.env.PAW_INITIAL_ROUTE || '/project-field';
  void window.loadURL(`${hostServer.origin}/?frontend=paw-os&pawHost=electron#${route}`);
  window.once('ready-to-show', () => { if (showWhenReady) window.show(); });
  window.on('closed', () => {
    if (mainWindow === window) mainWindow = null;
  });
  return window;
}

async function registerGuest(sender, tab) {
  if (sender !== mainWindow?.webContents) return;
  const webContentsId = Number(tab?.webContentsId || 0);
  const entry = guestRegistry.get(webContentsId);
  if (!entry) return;
  entry.tabId = String(tab?.tabId || '');
  entry.targetId = await guestTargetId(entry.contents);
  const pending = pendingTabs.get(String(tab?.commandId || ''));
  if (pending) {
    clearTimeout(pending.timer);
    pendingTabs.delete(String(tab.commandId));
    pending.resolve({ ok: true, targetId: entry.targetId, webContentsId });
  }
}

async function guestTargetId(contents) {
  const attached = contents.debugger.isAttached();
  if (!attached) contents.debugger.attach('1.3');
  try {
    const result = await contents.debugger.sendCommand('Target.getTargetInfo');
    const targetId = String(result?.targetInfo?.targetId || '');
    if (!targetId) throw new Error('Electron guest has no CDP target identity');
    return targetId;
  } finally {
    if (!attached && contents.debugger.isAttached()) contents.debugger.detach();
  }
}

function createVisibleTab(url) {
  if (!mainWindow || mainWindow.isDestroyed()) throw new Error('PAW Browser renderer is unavailable');
  const commandId = crypto.randomUUID();
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pendingTabs.delete(commandId);
      reject(new Error('PAW Browser guest did not attach'));
    }, 8_000);
    pendingTabs.set(commandId, { reject, resolve, timer });
    mainWindow.webContents.send('paw-browser:command', { action: 'new_tab', commandId, url });
  });
}

function activateVisibleTarget(targetId) {
  const entry = [...guestRegistry.values()].find((guest) => guest.targetId === targetId);
  if (!entry?.tabId || !mainWindow || mainWindow.isDestroyed()) {
    throw new Error('PAW Browser target is not a visible guest');
  }
  mainWindow.webContents.send('paw-browser:select-tab', entry.tabId);
  return { ok: true, targetId };
}

const primaryInstance = app.requestSingleInstanceLock({ profilePath: paths.profilePath });
if (!primaryInstance) {
  app.quit();
} else {
  app.on('second-instance', (_event, argv) => {
    const intent = assistantLaunchIntent(argv);
    if (intent) {
      if (!screenAssistant) pendingAssistantIntent = intent;
      else handleAssistantIntent(intent);
      return;
    }
    if (mainWindow?.isMinimized()) mainWindow.restore();
    mainWindow?.show();
    mainWindow?.focus();
  });
  void startPrimaryInstance().catch((error) => {
    console.error(error);
    app.exit(1);
  });
}

async function startPrimaryInstance() {
  await app.whenReady();
  fs.mkdirSync(paths.profilePath, { recursive: true });
  fs.mkdirSync(paths.browserExtensionsDir, { recursive: true });
  try {
    restoreInterruptedDownloads(paths.browserDownloadsFile);
  } catch {
    console.error('Could not restore PAW Browser Downloads');
  }
  fs.writeFileSync(paths.hostPidFile, `${process.pid}\n`, { encoding: 'utf8', mode: 0o600 });
  fs.writeFileSync(paths.hostPidFile.replace(/\.pid$/, '.token'), `${hostToken}\n`, { encoding: 'utf8', mode: 0o600 });
  const configuredFrontendPort = String(process.env.PAW_FRONTEND_PORT || '').trim();
  hostServer = await startPawHostServer({
    browserBridge: { activateTarget: activateVisibleTarget, createTab: createVisibleTab, token: hostToken },
    fallbackToEphemeralPort: configuredFrontendPort === '',
    frontendEntry: paths.frontendEntry,
    controlOrigin: process.env.PAW_CONTROL_ORIGIN || 'http://127.0.0.1:8768',
    port: Number(configuredFrontendPort || defaultPawHostPort),
  });
  fs.writeFileSync(
    paths.hostPidFile.replace(/\.pid$/, '.origin'),
    `${hostServer.origin}\n`,
    { encoding: 'utf8', mode: 0o600 },
  );
  if (process.platform === 'darwin') {
    const voice = createVoiceControl();
    for (const [channel, handler] of Object.entries({
      'paw-voice:status': () => voice.status(),
      'paw-voice:credentials': (provider) => voice.credentialStatus(provider),
      'paw-voice:save': (request) => voice.saveCredentials(request),
      'paw-voice:action': (action) => voice.action(action),
    })) {
      ipcMain.handle(channel, (event, argument) => {
        if (!trustedVoiceSender(event, mainWindow, hostServer.origin)) throw new Error('Voice sender rejected');
        return handler(argument);
      });
    }
  }
  ipcMain.on('paw-browser:register', (event, tab) => { void registerGuest(event.sender, tab); });
  const persistentBrowserSession = session.fromPartition(browserPartition);
  persistentBrowserSession.on('will-download', (_event, item) => {
    try {
      trackDownload(paths.browserDownloadsFile, item, {
        activeDownloads,
        downloadsPath: app.getPath('downloads'),
        onChange: (downloads) => sendToMainWindow('paw-browser:downloads-updated', downloads),
        onError: (stage) => console.error('Could not persist PAW Browser Download metadata', stage),
      });
    } catch {
      console.error('Could not register PAW Browser Download');
    }
  });
  ipcMain.handle('paw-browser:get-settings', (event) => {
    if (event.sender !== mainWindow?.webContents) throw new Error('Browser settings sender rejected');
    return readBrowserSessionSettings({
      downloadsPath: app.getPath('downloads'),
      electronSession: persistentBrowserSession,
      extensionsPath: paths.browserExtensionsDir,
      startPage: browserStartPage,
    });
  });
  ipcMain.handle('paw-browser:list-extensions', (event) => {
    if (event.sender !== mainWindow?.webContents) throw new Error('Browser settings sender rejected');
    return listBrowserExtensions(persistentBrowserSession);
  });
  ipcMain.handle('paw-browser:load-extension', async (event) => {
    if (event.sender !== mainWindow?.webContents) throw new Error('Browser settings sender rejected');
    const selection = await dialog.showOpenDialog(mainWindow, {
      title: '选择扩展程序目录',
      properties: ['openDirectory'],
    });
    const extensionPath = selection.filePaths[0];
    if (selection.canceled || !extensionPath) return null;
    const resolvedPath = path.resolve(extensionPath);
    const extension = await persistentBrowserSession.extensions.loadExtension(resolvedPath);
    return {
      id: extension.id,
      name: extension.name,
      path: extension.path,
      version: extension.version,
    };
  });
  ipcMain.handle('paw-browser:remove-extension', (event, extensionId) => {
    if (event.sender !== mainWindow?.webContents) throw new Error('Browser settings sender rejected');
    persistentBrowserSession.extensions.removeExtension(String(extensionId || ''));
    return listBrowserExtensions(persistentBrowserSession);
  });
  ipcMain.handle('paw-browser:open-extensions-folder', async (event) => {
    if (event.sender !== mainWindow?.webContents) throw new Error('Browser settings sender rejected');
    fs.mkdirSync(paths.browserExtensionsDir, { recursive: true });
    const error = await shell.openPath(paths.browserExtensionsDir);
    if (error) throw new Error(error);
    return { opened: true, path: paths.browserExtensionsDir };
  });
  ipcMain.handle('paw-browser:get-history', (event) => {
    if (event.sender !== mainWindow?.webContents) throw new Error('Browser History sender rejected');
    return readBrowserHistory(paths.browserHistoryFile);
  });
  ipcMain.handle('paw-browser:get-bookmarks', (event) => {
    if (event.sender !== mainWindow?.webContents) throw new Error('Browser Bookmarks sender rejected');
    return getBookmarks(paths.browserBookmarksFile);
  });
  ipcMain.handle('paw-browser:add-bookmark', (event, bookmark) => {
    if (event.sender !== mainWindow?.webContents) throw new Error('Browser Bookmarks sender rejected');
    const bookmarks = addBookmark(paths.browserBookmarksFile, bookmark);
    sendToMainWindow('paw-browser:bookmarks-updated', bookmarks);
    return bookmarks;
  });
  ipcMain.handle('paw-browser:remove-bookmark', (event, bookmarkId) => {
    if (event.sender !== mainWindow?.webContents) throw new Error('Browser Bookmarks sender rejected');
    const bookmarks = removeBookmark(paths.browserBookmarksFile, bookmarkId);
    sendToMainWindow('paw-browser:bookmarks-updated', bookmarks);
    return bookmarks;
  });
  ipcMain.handle('paw-browser:get-downloads', (event) => {
    if (event.sender !== mainWindow?.webContents) throw new Error('Browser Downloads sender rejected');
    return getDownloads(paths.browserDownloadsFile);
  });
  ipcMain.handle('paw-browser:open-download', async (event, downloadId) => {
    if (event.sender !== mainWindow?.webContents) throw new Error('Browser Downloads sender rejected');
    const record = await openBrowserDownload(paths.browserDownloadsFile, downloadId, {
      downloadsPath: app.getPath('downloads'),
      openPath: (filePath) => shell.openPath(filePath),
    });
    return { ...record, opened: true };
  });
  ipcMain.handle('paw-browser:reveal-download', (event, downloadId) => {
    if (event.sender !== mainWindow?.webContents) throw new Error('Browser Downloads sender rejected');
    const record = revealBrowserDownload(paths.browserDownloadsFile, downloadId, {
      downloadsPath: app.getPath('downloads'),
      showItemInFolder: (filePath) => shell.showItemInFolder(filePath),
    });
    return { ...record, revealed: true };
  });
  ipcMain.handle('paw-browser:cancel-download', (event, downloadId) => {
    if (event.sender !== mainWindow?.webContents) throw new Error('Browser Downloads sender rejected');
    const record = cancelBrowserDownload(paths.browserDownloadsFile, downloadId, { activeDownloads });
    // DownloadItem lifecycle events publish the authoritative cancellation state.
    return record;
  });
  ipcMain.handle('paw-browser:remove-history-entry', (event, entryId) => {
    if (event.sender !== mainWindow?.webContents) throw new Error('Browser History sender rejected');
    return removeBrowserHistoryEntry(paths.browserHistoryFile, entryId);
  });
  ipcMain.handle('paw-browser:clear-history', (event) => {
    if (event.sender !== mainWindow?.webContents) throw new Error('Browser History sender rejected');
    return clearBrowserHistory(paths.browserHistoryFile);
  });
  ipcMain.handle('paw-browser:open-downloads', async (event) => {
    if (event.sender !== mainWindow?.webContents) throw new Error('Browser settings sender rejected');
    const downloadsPath = app.getPath('downloads');
    const error = await shell.openPath(downloadsPath);
    if (error) throw new Error(error);
    return { opened: true, path: downloadsPath };
  });
  ipcMain.handle('paw-host:pick-workspace-directory', async (event) => {
    if (event.sender !== mainWindow?.webContents) throw new Error('Workspace picker sender rejected');
    const selection = await dialog.showOpenDialog(mainWindow, {
      title: '选择工作目录',
      properties: ['openDirectory', 'createDirectory'],
    });
    const selectedPath = selection.filePaths[0];
    if (selection.canceled || !selectedPath) return null;
    const resolvedPath = path.resolve(selectedPath);
    if (!fs.statSync(resolvedPath).isDirectory()) throw new Error('选择的工作目录不可用');
    return { name: path.basename(resolvedPath), path: resolvedPath };
  });
  ipcMain.handle('paw-browser:take-screenshot', async (event, rawWebContentsId) => {
    if (event.sender !== mainWindow?.webContents) throw new Error('Browser screenshot sender rejected');
    const entry = guestRegistry.get(Number(rawWebContentsId || 0));
    if (!entry || entry.contents.isDestroyed()) throw new Error('当前网页不可用');
    const image = await entry.contents.capturePage();
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    const outputPath = path.join(app.getPath('downloads'), `PAW-Browser-${stamp}.png`);
    fs.writeFileSync(outputPath, image.toPNG());
    return { path: outputPath, saved: true };
  });
  ipcMain.handle('paw-browser:clear-browsing-data', async (event, action) => {
    if (event.sender !== mainWindow?.webContents) throw new Error('Browser settings sender rejected');
    const result = await clearBrowserSessionData(persistentBrowserSession, action);
    return { ...result, completedAt: Date.now() };
  });
  ipcMain.handle('paw-browser:set-start-page', (event, value) => {
    if (event.sender !== mainWindow?.webContents) throw new Error('Browser settings sender rejected');
    browserStartPage = normalizedStartPage(value);
    fs.writeFileSync(paths.browserSettingsFile, `${JSON.stringify({ startPage: browserStartPage })}\n`, { encoding: 'utf8', mode: 0o600 });
    return { startPage: browserStartPage };
  });
  ipcMain.on('paw-browser:activate', (event, tab) => {
    if (event.sender !== mainWindow?.webContents || !tab || typeof tab !== 'object') return;
    const webContentsId = Number(tab.webContentsId || 0);
    const entry = guestRegistry.get(webContentsId);
    if (!entry?.targetId) return;
    const target = {
      tabId: entry.targetId,
      targetId: entry.targetId,
      rendererTabId: entry.tabId,
      webContentsId,
      title: String(tab.title || '').slice(0, 500),
      url: String(tab.url || '').slice(0, 8_000),
    };
    fs.writeFileSync(
      paths.hostPidFile.replace(/\.pid$/, '.active-target.json'),
      `${JSON.stringify(target)}\n`,
      { encoding: 'utf8', mode: 0o600 },
    );
  });
  screenAssistant = installScreenAssistant({
    app, BrowserWindow, ipcMain, dialog, systemPreferences,
    origin: hostServer.origin, preload: paths.preloadEntry,
    getMainWindow: () => mainWindow, openSession: openAssistantSession,
  });
  // This is the system-wide entry point: the picker owns the foreground
  // selection, then hands the managed image to the same PAW Session popup.
  // Keep registration in the Electron host so it works while another app is
  // frontmost; the App UI also mirrors the shortcut when PAW has focus.
  const capsuleShortcut = 'CommandOrControl+Shift+Space';
  if (!globalShortcut.register(capsuleShortcut, () => { void screenAssistant.startCapture(); })) {
    console.warn(`Could not register Agent Capsule shortcut: ${capsuleShortcut}`);
  }
  app.once('will-quit', () => globalShortcut.unregister(capsuleShortcut));
  const initialIntent = pendingAssistantIntent;
  pendingAssistantIntent = null;
  mainWindow = createWindow(initialIntent?.kind === 'session' ? assistantSessionRoute(initialIntent.sessionId) : undefined, initialIntent?.kind !== 'capture');
  if (initialIntent?.kind === 'capture') void screenAssistant.startCapture(initialIntent.sourceAppBundleId);
  const menu = Menu.getApplicationMenu();
  if (menu) {
    const item = new MenuItem({ label: '框选屏幕与 PAW 对话…', click: () => { void screenAssistant.startCapture(); } });
    // Electron's macOS default menu may expose the File item by label rather
    // than role. Resolve that submenu explicitly so the managed App remains
    // reachable even when the host did not install a custom menu template.
    const fileMenu = menu.items.find((entry) => entry.role === 'fileMenu' || entry.label === 'File')?.submenu;
    if (fileMenu && !fileMenu.items.some((entry) => entry.label === item.label)) {
      fileMenu.append(item);
      Menu.setApplicationMenu(menu);
    }
  }
}

function openAssistantSession(sessionId) {
  const route = assistantSessionRoute(sessionId);
  if (!mainWindow || mainWindow.isDestroyed()) mainWindow = createWindow(route);
  else {
    const contents = mainWindow.webContents;
    const navigate = () => { if (!contents.isDestroyed()) contents.send('paw-host:navigate', route); };
    if (contents.isLoadingMainFrame()) contents.once('did-finish-load', navigate);
    else navigate();
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show(); mainWindow.focus();
  }
}

function handleAssistantIntent(intent) {
  if (intent.kind === 'capture') void screenAssistant.startCapture(intent.sourceAppBundleId);
  else openAssistantSession(intent.sessionId);
}

app.on('activate', () => {
  // Startup owns the first window, including cold-launch activation.
  if (!primaryInstance || !app.isReady() || !hostServer) return;
  if (BrowserWindow.getAllWindows().length === 0) mainWindow = createWindow();
});
app.on('before-quit', () => {
  void hostServer?.close();
  try {
    if (fs.readFileSync(paths.hostPidFile, 'utf8').trim() === String(process.pid)) {
      fs.unlinkSync(paths.hostPidFile);
    }
  } catch {
    // The host file is only a live-process handoff; an absent/stale file is harmless.
  }
});
app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
