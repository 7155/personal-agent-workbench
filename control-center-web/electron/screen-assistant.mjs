import { captureScreenRegion } from './screen-capture.mjs';
import { assistantLaunchIntent } from './assistant-launch.mjs';
import fs from 'node:fs/promises';
import path from 'node:path';
import { randomUUID } from 'node:crypto';

const AGENT_CAPSULE_CAPTURE_PATH = '/agent-capsule';
const AGENT_CAPSULE_CAPTURE_QUERY = '?frontend=paw-os&pawHost=electron&surface=capture';

export function installScreenAssistant({ app, BrowserWindow, ipcMain, dialog, systemPreferences, origin, preload, getMainWindow, openSession, capture = captureScreenRegion }) {
  const windows = new Map();
  let capturing = false;
  let pendingCapture;
  const owned = (sender) => {
    const entry = windows.get(sender.id);
    if (!entry || entry.window.isDestroyed() || new URL(sender.getURL()).origin !== origin) throw new Error('Screen assistant sender rejected');
    return entry;
  };
  ipcMain.handle('paw-screen:context', (event) => owned(event.sender).capture);
  ipcMain.handle('paw-screen:conversation', (event) => owned(event.sender).conversation || {});
  ipcMain.handle('paw-screen:remember-conversation', (event, value) => {
    const entry = owned(event.sender);
    const intent = assistantLaunchIntent([`--paw-session=${String(value?.session?.id || '')}`]);
    if (!intent?.sessionId || (entry.conversation && entry.conversation.session.id !== intent.sessionId)) throw new Error('Invalid Session recovery identity');
    if (Buffer.byteLength(JSON.stringify(value)) > 64 * 1024) throw new Error('Conversation recovery receipt is too large');
    const conversation = { ...entry.conversation, session: JSON.parse(JSON.stringify(value.session)) };
    if (value.attachments !== undefined) {
      if (!Array.isArray(value.attachments) || value.attachments.length !== 1) throw new Error('Expected one screen attachment');
      const attachment = value.attachments[0];
      if (!/^media_[A-Za-z0-9_-]{12,80}$/.test(attachment?.id) || attachment.mimeType !== 'image/png') throw new Error('Invalid screen attachment receipt');
      if (entry.conversation?.attachments && entry.conversation.attachments[0].id !== attachment.id) throw new Error('Screen attachment is already bound');
      conversation.attachments = [{ id: attachment.id, name: '屏幕选区.png', mimeType: 'image/png', byteSize: Number(attachment.byteSize) || 0, source: 'picker' }];
    }
    entry.conversation = conversation;
  });
  ipcMain.handle('paw-screen:open-session', (event, sessionId) => {
    owned(event.sender);
    const intent = assistantLaunchIntent([`--paw-session=${String(sessionId || '')}`]);
    if (!intent) throw new Error('Invalid Session identity');
    openSession(intent.sessionId);
  });
  ipcMain.handle('paw-screen:save-note', async (event, note) => {
    const entry = owned(event.sender);
    if (typeof note?.body !== 'string' || !note.body.trim() || Buffer.byteLength(note.body) > 2 * 1024 * 1024) throw new Error('笔记内容为空或过长。');
    const result = await dialog.showSaveDialog(entry.window, {
      title: '保存选区笔记', defaultPath: '选区笔记.md', filters: [{ name: 'Markdown', extensions: ['md'] }],
    });
    if (result.canceled || !result.filePath) return { saved: false };
    await fs.writeFile(result.filePath, note.body, { encoding: 'utf8', mode: 0o600 });
    return { saved: true, name: path.basename(result.filePath) };
  });
  ipcMain.handle('paw-screen:capture', (event, options) => {
    if (event.senderFrame && event.senderFrame !== event.sender.mainFrame) throw new Error('Screen assistant frame rejected');
    if (event.sender !== getMainWindow()?.webContents) owned(event.sender);
    else if (new URL(event.sender.getURL()).origin !== origin) throw new Error('Screen assistant sender rejected');
    if (options !== undefined && (!options || typeof options !== 'object' || Array.isArray(options)
      || Object.keys(options).some((key) => key !== 'sourceSessionId'))) throw new Error('Invalid screen assistant options');
    const sourceSessionId = options?.sourceSessionId;
    if (sourceSessionId !== undefined && (typeof sourceSessionId !== 'string' || !sourceSessionId
      || !assistantLaunchIntent([`--paw-session=${sourceSessionId}`]))) throw new Error('Invalid source Session identity');
    return startCapture('', { sourceSessionId });
  });

  async function startCapture(sourceAppBundleId = '', { sourceSessionId } = {}) {
    if (capturing) return false;
    capturing = true;
    const controller = new AbortController();
    pendingCapture = controller;
    const visible = [getMainWindow(), ...[...windows.values()].map((entry) => entry.window)]
      .filter((window) => window && !window.isDestroyed() && window.isVisible());
    try {
      if (systemPreferences.getMediaAccessStatus('screen') === 'denied') {
        throw new Error('请在系统设置 → 隐私与安全性 → 屏幕与系统音频录制中允许 PAW，然后重新框选。');
      }
      visible.forEach((window) => window.hide());
      const context = await capture({ sourceAppBundleId, signal: controller.signal });
      visible.forEach((window) => { if (!window.isDestroyed()) window.showInactive(); });
      if (!context || controller.signal.aborted) return false;
      const window = new BrowserWindow({
        width: 640, height: 760, minWidth: 400, minHeight: 500,
        title: '星伴 · 屏幕对话', backgroundColor: '#f7f8fa', show: false,
        alwaysOnTop: true, visibleOnAllWorkspaces: true, skipTaskbar: true,
        webPreferences: { contextIsolation: true, nodeIntegration: false, preload, sandbox: true },
      });
      windows.set(window.webContents.id, { window, capture: {
        ...context, ...(sourceSessionId ? { sourceSessionId } : {}),
        creationRequestId: `screen-capture:${randomUUID()}`,
      } });
      window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
      window.webContents.on('will-navigate', (event, url) => {
        const target = new URL(url);
        if (
          target.origin !== origin
          || target.pathname !== AGENT_CAPSULE_CAPTURE_PATH
          || target.searchParams.get('surface') !== 'capture'
        ) event.preventDefault();
      });
      const senderId = window.webContents.id;
      window.on('closed', () => windows.delete(senderId));
      window.once('ready-to-show', () => window.show());
      await window.loadURL(`${origin}${AGENT_CAPSULE_CAPTURE_PATH}${AGENT_CAPSULE_CAPTURE_QUERY}`);
      return true;
    } catch (error) {
      visible.forEach((window) => { if (!window.isDestroyed()) window.showInactive(); });
      if (!controller.signal.aborted) await dialog.showMessageBox({ type: 'error', title: '框选未完成', message: String(error?.message || '请重试。') });
      return false;
    } finally { capturing = false; pendingCapture = undefined; }
  }
  app.on('before-quit', () => pendingCapture?.abort());
  return { startCapture };
}
