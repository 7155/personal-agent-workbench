import { createContext, useContext, useMemo, useSyncExternalStore, type ReactNode } from 'react';

export const CHAT_PRESENTATION_VERSIONS = ['v1', 'v2'] as const;
export type ChatPresentationVersion = typeof CHAT_PRESENTATION_VERSIONS[number];
export const CHAT_PRESENTATION_STORAGE_KEY = 'paw:chat-presentation:v1';
export type ChatPresentationOptions = {
  ownerKey?: string;
  defaultVersion?: ChatPresentationVersion;
  availableVersions?: readonly ChatPresentationVersion[];
};
type Selection = Readonly<{ version: ChatPresentationVersion; previousVersion?: ChatPresentationVersion }>;
type Selections = ReadonlyMap<string, Selection>;
const EMPTY: Selections = new Map();
let snapshot: Selections = EMPTY;
const listeners = new Set<() => void>();

function isVersion(value: unknown): value is ChatPresentationVersion {
  return value === 'v1' || value === 'v2';
}
function normalize(value: unknown): Selections {
  const next = new Map<string, Selection>();
  if (!value || typeof value !== 'object' || Array.isArray(value)) return next;
  for (const [owner, raw] of Object.entries(value)) {
    if (!owner.trim() || owner.length > 240 || !raw || typeof raw !== 'object' || Array.isArray(raw)) continue;
    const input = raw as Record<string, unknown>;
    if (!isVersion(input.version)) continue;
    next.set(owner, { version: input.version,
      ...(isVersion(input.previousVersion) && input.previousVersion !== input.version ? { previousVersion: input.previousVersion } : {}) });
  }
  return next;
}
function publish(next: Selections) {
  if (snapshot.size === next.size && [...next].every(([owner, value]) => {
    const previous = snapshot.get(owner);
    return previous?.version === value.version && previous?.previousVersion === value.previousVersion;
  })) return;
  snapshot = next;
  listeners.forEach(listener => listener());
}
function hydrate() {
  if (typeof window === 'undefined') return;
  try { publish(normalize(JSON.parse(window.localStorage.getItem(CHAT_PRESENTATION_STORAGE_KEY) || 'null'))); }
  catch { /* Storage denial retains the current in-memory selection. */ }
}
function onStorage(event: StorageEvent) {
  if (event.key === CHAT_PRESENTATION_STORAGE_KEY || event.key === null) hydrate();
}
function subscribe(listener: () => void) {
  listeners.add(listener);
  if (listeners.size === 1 && typeof window !== 'undefined') {
    window.addEventListener('storage', onStorage);
    hydrate();
  }
  return () => {
    listeners.delete(listener);
    if (!listeners.size && typeof window !== 'undefined') window.removeEventListener('storage', onStorage);
  };
}
function selectVersion(ownerKey: string, nextVersion: ChatPresentationVersion, currentVersion: ChatPresentationVersion) {
  if (!isVersion(nextVersion) || nextVersion === currentVersion) return;
  const next = new Map(snapshot);
  next.set(ownerKey, { version: nextVersion, previousVersion: currentVersion });
  publish(next);
  try { window.localStorage.setItem(CHAT_PRESENTATION_STORAGE_KEY, JSON.stringify(Object.fromEntries(next))); }
  catch { /* Display changes remain usable in sandboxed or private views. */ }
}

export type ChatPresentation = {
  ownerKey: string;
  version: ChatPresentationVersion;
  defaultVersion: ChatPresentationVersion;
  previousVersion?: ChatPresentationVersion;
  availableVersions: readonly ChatPresentationVersion[];
  scope: 'app' | 'session';
  setVersion(version: ChatPresentationVersion): void;
  rollback(): void;
};
const Context = createContext<ChatPresentation | undefined>(undefined);

/** Presentation only: never stores messages, drafts, model selection or Runtime authority. */
export function ChatPresentationProvider({ ownerKey, defaultVersion = 'v1', availableVersions = CHAT_PRESENTATION_VERSIONS, scope = 'app', children }: {
  ownerKey: string;
  defaultVersion?: ChatPresentationVersion;
  availableVersions?: readonly ChatPresentationVersion[];
  scope?: 'app' | 'session';
  children: ReactNode;
}) {
  if (!ownerKey.trim() || ownerKey.length > 240) throw new Error('聊天显示设置需要有效的 App 或会话标识。');
  if (!isVersion(defaultVersion) || !Array.isArray(availableVersions) || !availableVersions.length
    || availableVersions.some(version => !isVersion(version)) || !availableVersions.includes(defaultVersion)) {
    throw new Error('聊天显示版本配置无效。');
  }
  const selections = useSyncExternalStore(subscribe, () => snapshot, () => EMPTY);
  const selection = selections.get(ownerKey);
  const version = selection && availableVersions.includes(selection.version) ? selection.version : defaultVersion;
  const previousVersion = selection?.previousVersion && availableVersions.includes(selection.previousVersion)
    && selection.previousVersion !== version ? selection.previousVersion : undefined;
  const value = useMemo<ChatPresentation>(() => ({ ownerKey, version, defaultVersion, previousVersion, availableVersions, scope,
    setVersion(nextVersion) {
      if (isVersion(nextVersion) && availableVersions.includes(nextVersion)) selectVersion(ownerKey, nextVersion, version);
    },
    rollback() { if (previousVersion) selectVersion(ownerKey, previousVersion, version); },
  }), [ownerKey, version, defaultVersion, previousVersion, availableVersions, scope]);
  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useChatPresentation(): ChatPresentation | undefined {
  return useContext(Context);
}
