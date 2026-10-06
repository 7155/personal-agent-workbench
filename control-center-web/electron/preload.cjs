const { contextBridge, ipcRenderer } = require('electron');

// First-party view summaries only; main verifies the exact source window/frame.
contextBridge.exposeInMainWorld('pawDesktopPetState', Object.freeze({
  begin: (identity) => ipcRenderer.invoke('paw-pet-state:begin', identity),
  publish: (snapshot) => ipcRenderer.invoke('paw-pet-state:publish', snapshot),
  release: (lease) => ipcRenderer.invoke('paw-pet-state:release', lease),
}));

ipcRenderer.on('paw-host:navigate', (_event, route) => {
  if (typeof route === 'string' && /^\/agent(?:\?session=[^\s#]*)?$/.test(route)) window.location.hash = route;
});

contextBridge.exposeInMainWorld('pawScreenAssistant', Object.freeze({
  getCapture: () => ipcRenderer.invoke('paw-screen:context'),
  getConversation: () => ipcRenderer.invoke('paw-screen:conversation'),
  rememberConversation: (conversation) => ipcRenderer.invoke('paw-screen:remember-conversation', conversation),
  capture: () => ipcRenderer.invoke('paw-screen:capture'),
  openSession: (sessionId) => ipcRenderer.invoke('paw-screen:open-session', sessionId),
  saveNote: (note) => ipcRenderer.invoke('paw-screen:save-note', note),
}));

const nativeHost = process.platform === 'darwin' ? 'macos' : 'electron';
if (nativeHost === 'macos') {
  contextBridge.exposeInMainWorld('pawVoiceHost', Object.freeze({
    status: () => ipcRenderer.invoke('paw-voice:status'),
    credentialStatus: (provider) => ipcRenderer.invoke('paw-voice:credentials', provider),
    saveCredentials: (request) => ipcRenderer.invoke('paw-voice:save', request),
    action: (action) => ipcRenderer.invoke('paw-voice:action', action),
  }));
  const markNativeHost = () => { document.documentElement.dataset.pawNativeHost = nativeHost; };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', markNativeHost, { once: true });
  else markNativeHost();
}

contextBridge.exposeInMainWorld('pawBrowserHost', Object.freeze({
  kind: 'electron-webview',
  nativeHost,
  partition: 'persist:paw-browser',
  activate(tab) {
    ipcRenderer.send('paw-browser:activate', tab);
  },
  clearBrowsingData(action) {
    return ipcRenderer.invoke('paw-browser:clear-browsing-data', action);
  },
  clearHistory() {
    return ipcRenderer.invoke('paw-browser:clear-history');
  },
  addBookmark(bookmark) {
    return ipcRenderer.invoke('paw-browser:add-bookmark', bookmark);
  },
  getBookmarks() {
    return ipcRenderer.invoke('paw-browser:get-bookmarks');
  },
  getDownloads() {
    return ipcRenderer.invoke('paw-browser:get-downloads');
  },
  getHistory() {
    return ipcRenderer.invoke('paw-browser:get-history');
  },
  getSettings() {
    return ipcRenderer.invoke('paw-browser:get-settings');
  },
  listExtensions() {
    return ipcRenderer.invoke('paw-browser:list-extensions');
  },
  loadUnpackedExtension() {
    return ipcRenderer.invoke('paw-browser:load-extension');
  },
  openExtensionsFolder() {
    return ipcRenderer.invoke('paw-browser:open-extensions-folder');
  },
  openDownloads() {
    return ipcRenderer.invoke('paw-browser:open-downloads');
  },
  openDownload(downloadId) {
    return ipcRenderer.invoke('paw-browser:open-download', downloadId);
  },
  revealDownload(downloadId) {
    return ipcRenderer.invoke('paw-browser:reveal-download', downloadId);
  },
  pickWorkspaceDirectory() {
    return ipcRenderer.invoke('paw-host:pick-workspace-directory');
  },
  register(tab) {
    ipcRenderer.send('paw-browser:register', tab);
  },
  removeHistoryEntry(entryId) {
    return ipcRenderer.invoke('paw-browser:remove-history-entry', entryId);
  },
  removeBookmark(bookmarkId) {
    return ipcRenderer.invoke('paw-browser:remove-bookmark', bookmarkId);
  },
  cancelDownload(downloadId) {
    return ipcRenderer.invoke('paw-browser:cancel-download', downloadId);
  },
  removeExtension(extensionId) {
    return ipcRenderer.invoke('paw-browser:remove-extension', extensionId);
  },
  setStartPage(startPage) {
    return ipcRenderer.invoke('paw-browser:set-start-page', startPage);
  },
  takeScreenshot(webContentsId) {
    return ipcRenderer.invoke('paw-browser:take-screenshot', webContentsId);
  },
  onCommand(listener) {
    const handler = (_event, command) => listener(command);
    ipcRenderer.on('paw-browser:command', handler);
    return () => ipcRenderer.off('paw-browser:command', handler);
  },
  onGuestClosed(listener) {
    const handler = (_event, tabId) => listener(tabId);
    ipcRenderer.on('paw-browser:guest-closed', handler);
    return () => ipcRenderer.off('paw-browser:guest-closed', handler);
  },
  onHistoryChanged(listener) {
    const handler = (_event, history) => listener(history);
    ipcRenderer.on('paw-browser:history-updated', handler);
    return () => ipcRenderer.off('paw-browser:history-updated', handler);
  },
  onBookmarksChanged(listener) {
    const handler = (_event, bookmarks) => listener(bookmarks);
    ipcRenderer.on('paw-browser:bookmarks-updated', handler);
    return () => ipcRenderer.off('paw-browser:bookmarks-updated', handler);
  },
  onDownloadsChanged(listener) {
    const handler = (_event, downloads) => listener(downloads);
    ipcRenderer.on('paw-browser:downloads-updated', handler);
    return () => ipcRenderer.off('paw-browser:downloads-updated', handler);
  },
  onOpenUrl(listener) {
    const handler = (_event, url) => listener(url);
    ipcRenderer.on('paw-browser:open-url', handler);
    return () => ipcRenderer.off('paw-browser:open-url', handler);
  },
  onSelectTab(listener) {
    const handler = (_event, tabId) => listener(tabId);
    ipcRenderer.on('paw-browser:select-tab', handler);
    return () => ipcRenderer.off('paw-browser:select-tab', handler);
  },
}));
