const PET_PATH = '/desktop-pet';
const SIZE = { width: 160, height: 190 };
const READY_TIMEOUT_MS = 10_000;

export function clampPetPosition(position, area, size = SIZE) {
  return {
    x: Math.round(Math.max(area.x, Math.min(position.x, area.x + Math.max(0, area.width - size.width)))),
    y: Math.round(Math.max(area.y, Math.min(position.y, area.y + Math.max(0, area.height - size.height)))),
  };
}

// An optional launcher, not a second assistant runtime. No capture or model calls.
export function installDesktopPet({ app, BrowserWindow, ipcMain, screen, origin, preload, openAssistant, getSource = () => null, onVisibilityChanged = () => {} }) {
  let window = null;
  let presentationEpoch = 0;
  let loading = null;
  let drag = null;
  let enabled = false;
  let disposed = false;
  let nativeReady = false;
  let surfaceReady = false;
  let finishLoading = null;
  let readinessTimer = null;
  const positions = new Map();
  const url = `${origin}${PET_PATH}`;
  const state = installDesktopPetState({ ipcMain, getSource, origin, onSnapshot: (snapshot) => {
    if (surfaceReady && window && !window.isDestroyed()) window.webContents.send('paw-pet:state', snapshot);
  } });
  const settlePresentation = (visible) => {
    clearTimeout(readinessTimer); readinessTimer = null;
    const finish = finishLoading; finishLoading = null; loading = null;
    finish?.(visible);
  };
  const present = (target, epoch) => {
    if (!nativeReady || !surfaceReady || !enabled || epoch !== presentationEpoch || window !== target || target.isDestroyed()) return;
    if (target.isVisible()) { settlePresentation(true); return; }
    try {
      target.showInactive();
      onVisibilityChanged(true);
      settlePresentation(true);
    } catch { hide(); }
  };
  const validUrl = (value) => {
    try { const target = new URL(value); return target.origin === origin && target.pathname === PET_PATH && !target.search && !target.hash; }
    catch { return false; }
  };
  const owned = (event) => {
    if (!window || window.isDestroyed() || event.sender !== window.webContents || !validUrl(event.sender.getURL())
      || (event.senderFrame && event.senderFrame !== window.webContents.mainFrame)) throw new Error('Desktop pet sender rejected');
    return window;
  };
  const place = (target, position) => {
    const display = screen.getDisplayNearestPoint(position);
    const [width, height] = target.getSize();
    const next = clampPetPosition(position, display.workArea, { width, height });
    target.setPosition(next.x, next.y);
    positions.set(display.id, next);
  };
  const hide = () => {
    enabled = false;
    presentationEpoch += 1;
    drag = null;
    settlePresentation(false);
    const previous = window;
    window = null;
    if (previous && !previous.isDestroyed()) previous.destroy();
    onVisibilityChanged(false);
  };
  const show = () => {
    if (disposed) return Promise.resolve(false);
    if (window && !window.isDestroyed()) return loading || Promise.resolve(window.isVisible());
    enabled = true;
    nativeReady = false;
    surfaceReady = false;
    // A native checkbox toggles before its click handler. Opening is pending,
    // not visible; only the two-ready presentation below can check it again.
    onVisibilityChanged(false);
    const epoch = ++presentationEpoch;
    const display = screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
    const position = clampPetPosition(positions.get(display.id) || {
      x: display.workArea.x + display.workArea.width - SIZE.width - 24,
      y: display.workArea.y + display.workArea.height - SIZE.height - 24,
    }, display.workArea);
    const current = new BrowserWindow({
      ...SIZE, ...position, title: 'PAW 桌面伙伴', show: false, transparent: true,
      frame: false, resizable: false, maximizable: false, fullscreenable: false,
      alwaysOnTop: true, skipTaskbar: true, backgroundColor: '#00000000',
      webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true, preload },
    });
    window = current;
    current.on('blur', () => { drag = null; });
    current.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    current.webContents.on('will-navigate', (event, target) => { if (!validUrl(target)) event.preventDefault(); });
    current.webContents.on('will-redirect', (event, target) => { if (!validUrl(target)) event.preventDefault(); });
    current.once('closed', () => {
      if (window === current) { window = null; enabled = false; drag = null; settlePresentation(false); onVisibilityChanged(false); }
    });
    current.once('ready-to-show', () => {
      if (window !== current || epoch !== presentationEpoch) return;
      nativeReady = true;
      present(current, epoch);
    });
    loading = new Promise((resolve) => { finishLoading = resolve; });
    const pending = loading;
    readinessTimer = setTimeout(() => {
      if (window === current && epoch === presentationEpoch) {
        console.warn('Desktop pet did not become ready; close the hidden window and allow retry');
        hide();
      }
    }, READY_TIMEOUT_MS);
    readinessTimer.unref?.();
    void Promise.resolve().then(() => current.loadURL(url)).catch(() => {
      if (window === current) hide();
    });
    return pending;
  };
  ipcMain.handle('paw-pet:ready', (event) => {
    const target = owned(event);
    surfaceReady = true;
    present(target, presentationEpoch);
    return state.snapshot();
  });
  ipcMain.handle('paw-pet:hide', (event) => { owned(event); hide(); });
  ipcMain.handle('paw-pet:open-assistant', (event) => { owned(event); openAssistant(); });
  ipcMain.handle('paw-pet:open-conversation', (event, target) => {
    owned(event);
    if (!state.canOpenConversation(target)) throw new Error('Desktop pet conversation rejected');
    openAssistant(target.id);
  });
  ipcMain.handle('paw-pet:expand', (event, expanded) => {
    const target = owned(event);
    if (typeof expanded !== 'boolean') throw new Error('Invalid desktop pet expansion');
    drag = null;
    const [x, y] = target.getPosition();
    const display = screen.getDisplayNearestPoint({ x, y });
    const size = expanded ? { width: 320, height: 400 } : SIZE;
    target.setSize(Math.min(size.width, display.workArea.width), Math.min(size.height, display.workArea.height));
    place(target, { x, y });
  });
  ipcMain.handle('paw-pet:drag', (event, phase) => {
    const target = owned(event);
    if (!['start', 'move', 'end', 'cancel'].includes(phase)) throw new Error('Invalid desktop pet drag');
    if (phase === 'start') {
      const [x, y] = target.getPosition();
      drag = { pointer: screen.getCursorScreenPoint(), x, y, moved: false };
    } else if ((phase === 'move' || (phase === 'end' && drag?.moved)) && drag) {
      const cursor = screen.getCursorScreenPoint();
      place(target, { x: drag.x + cursor.x - drag.pointer.x, y: drag.y + cursor.y - drag.pointer.y });
      drag.moved = true;
    }
    if (phase === 'end' || phase === 'cancel') drag = null;
  });
  ipcMain.handle('paw-pet:move', (event, direction) => {
    const target = owned(event);
    if (!['left', 'right', 'up', 'down'].includes(direction)) throw new Error('Invalid desktop pet direction');
    drag = null;
    const [x, y] = target.getPosition();
    const [dx, dy] = { left: [-16, 0], right: [16, 0], up: [0, -16], down: [0, 16] }[direction];
    place(target, { x: x + dx, y: y + dy });
  });
  const keepVisible = () => {
    if (window && !window.isDestroyed()) {
      drag = null;
      const [x, y] = window.getPosition();
      place(window, { x, y });
    }
  };
  screen.on('display-removed', keepVisible);
  screen.on('display-metrics-changed', keepVisible);
  app.once('before-quit', () => {
    disposed = true;
    hide();
    state.dispose();
    screen.removeListener('display-removed', keepVisible);
    screen.removeListener('display-metrics-changed', keepVisible);
  });
  return { show, hide, isVisible: () => Boolean(window && !window.isDestroyed() && window.isVisible()) };
}
import { installDesktopPetState } from './desktop-pet-state.mjs';
