import type { RefObject } from 'react';
import { isTeamDeployment } from '@/features/team/deployment';

export const PAW_BROWSER_PARTITION = 'persist:paw-browser';

export type PawBrowserHost = {
  kind: 'electron-webview';
  partition: string;
  activate(tab: { title: string; url: string; webContentsId: number }): void;
  clearBrowsingData(action: 'cache' | 'site-data'): Promise<PawBrowserMaintenanceReceipt>;
  clearHistory(): Promise<PawBrowserHistoryEntry[]>;
  /** Optional while older Electron hosts are still in circulation. */
  getBookmarks?(): Promise<PawBrowserBookmark[]>;
  /** Optional while older Electron hosts are still in circulation. */
  addBookmark?(bookmark: { title: string; url: string }): Promise<PawBrowserBookmark[]>;
  /** Optional while older Electron hosts are still in circulation. */
  removeBookmark?(bookmarkId: string): Promise<PawBrowserBookmark[]>;
  /** Optional while older Electron hosts are still in circulation. */
  getDownloads?(): Promise<PawBrowserDownload[]>;
  /** Optional while older Electron hosts are still in circulation. */
  openDownload?(downloadId: string): Promise<PawBrowserDownload & { opened: boolean }>;
  /** Optional while older Electron hosts are still in circulation. */
  revealDownload?(downloadId: string): Promise<PawBrowserDownload & { revealed: boolean }>;
  /** Optional while older Electron hosts are still in circulation. */
  cancelDownload?(downloadId: string): Promise<PawBrowserDownload>;
  getHistory(): Promise<PawBrowserHistoryEntry[]>;
  getSettings(): Promise<PawBrowserSettings>;
  listExtensions(): Promise<PawBrowserExtension[]>;
  loadUnpackedExtension(): Promise<PawBrowserExtension | null>;
  openExtensionsFolder(): Promise<{ opened: boolean; path: string }>;
  openDownloads(): Promise<{ opened: boolean; path: string }>;
  removeExtension(extensionId: string): Promise<PawBrowserExtension[]>;
  pickWorkspaceDirectory?(): Promise<PawWorkspaceDirectoryReceipt | null>;
  register(tab: { commandId?: string; tabId: string; webContentsId: number }): void;
  removeHistoryEntry(entryId: string): Promise<PawBrowserHistoryEntry[]>;
  setStartPage(startPage: string): Promise<{ startPage: string }>;
  takeScreenshot(webContentsId: number): Promise<{ path: string; saved: boolean }>;
  onCommand(listener: (command: { action: 'new_tab'; commandId: string; url: string }) => void): () => void;
  /** Optional while older Electron hosts are still in circulation. */
  onBookmarksChanged?(listener: (bookmarks: PawBrowserBookmark[]) => void): () => void;
  /** Optional while older Electron hosts are still in circulation. */
  onDownloadsChanged?(listener: (downloads: PawBrowserDownload[]) => void): () => void;
  onGuestClosed(listener: (tabId: string) => void): () => void;
  onHistoryChanged(listener: (history: PawBrowserHistoryEntry[]) => void): () => void;
  onOpenUrl(listener: (url: string) => void): () => void;
  onSelectTab(listener: (tabId: string) => void): () => void;
};

export type PawWorkspaceDirectoryReceipt = {
  name: string;
  path: string;
};

export type PawBrowserHistoryEntry = {
  id: string;
  title: string;
  url: string;
  visitedAt: number;
};

export type PawBrowserBookmark = {
  id: string;
  title: string;
  url: string;
  createdAt: number;
  updatedAt: number;
};

export type PawBrowserDownloadState =
  | 'pending'
  | 'progressing'
  | 'completed'
  | 'cancelled'
  | 'interrupted'
  | 'failed';

export type PawBrowserDownload = {
  id: string;
  filename: string;
  url: string;
  path: string;
  state: PawBrowserDownloadState;
  receivedBytes: number;
  totalBytes: number;
  startedAt: number;
  updatedAt: number;
  completedAt?: number;
};

export type PawBrowserExtension = {
  id: string;
  name: string;
  path: string;
  version: string;
};

export type PawBrowserSettings = {
  cacheBytes: number;
  cookieCount: number;
  downloadPath: string;
  extensionCount: number;
  extensionsPath: string;
  partition: typeof PAW_BROWSER_PARTITION;
  permissionMode: 'site-request';
  startPage: string;
};

export type PawBrowserMaintenanceReceipt = {
  action: 'cache' | 'site-data';
  after: number;
  before: number;
  completedAt: number;
};

export type PawBrowserWebview = HTMLElement & {
  canGoBack(): boolean;
  canGoForward(): boolean;
  getTitle(): string;
  getURL(): string;
  getWebContentsId(): number;
  getZoomFactor(): number;
  goBack(): void;
  goForward(): void;
  isCrashed(): boolean;
  isLoading(): boolean;
  findInPage(text: string, options?: { findNext?: boolean; forward?: boolean }): number;
  loadURL(url: string): Promise<void>;
  print(): void;
  reload(): void;
  setZoomFactor(value: number): void;
  stop(): void;
  stopFindInPage(action: 'clearSelection' | 'keepSelection' | 'activateSelection'): void;
};

export type PawBrowserGuestFailLoadEvent = Event & {
  errorCode: number;
  errorDescription: string;
  validatedURL: string;
  isMainFrame: boolean;
};

export type PawBrowserGuestFoundInPageEvent = Event & {
  result?: { activeMatchOrdinal?: number; matches?: number; finalUpdate?: boolean };
};

export type PawBrowserGuestProcessGoneEvent = Event & { reason?: string };

export type PawBrowserGuestFaviconEvent = Event & { favicons?: string[] };

export function pawBrowserHost(): PawBrowserHost | null {
  // Electron's persistent guest partition is host-scoped. Team spaces use
  // the HTTP Browser surface until the native host supplies an account-safe
  // partition, so cookies/history/downloads cannot cross identities.
  if (isTeamDeployment()) return null;
  const host = window.pawBrowserHost;
  return host?.kind === 'electron-webview' && host.partition === PAW_BROWSER_PARTITION
    ? host
    : null;
}

export function loadPawBrowserUrl(
  ref: RefObject<PawBrowserWebview | null>,
  rawUrl: string,
): boolean {
  const webview = ref.current;
  if (!webview) return false;
  void webview.loadURL(rawUrl);
  return true;
}

/**
 * Reads real history availability from the guest. The `<webview>` element only
 * gains its navigation methods once Electron attaches the guest, so this reads
 * defensively instead of trusting the type at mount time.
 */
export function guestNavigationState(
  webview: PawBrowserWebview | null,
): { canGoBack: boolean; canGoForward: boolean } {
  return {
    canGoBack: typeof webview?.canGoBack === 'function' ? webview.canGoBack() : false,
    canGoForward: typeof webview?.canGoForward === 'function' ? webview.canGoForward() : false,
  };
}

/**
 * Electron exposes the zoom methods on the custom element before its guest is
 * attached. Calling them during that short mount/unmount window throws instead
 * of returning an unavailable value, so keep lifecycle absence local to the
 * Browser surface rather than letting it trip the app-wide error boundary.
 */
export function guestZoomFactor(webview: PawBrowserWebview | null): number | null {
  if (typeof webview?.getZoomFactor !== 'function') return null;
  try {
    const factor = webview.getZoomFactor();
    return Number.isFinite(factor) ? factor : null;
  } catch {
    return null;
  }
}
