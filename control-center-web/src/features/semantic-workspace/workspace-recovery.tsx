import { useCallback, useEffect, useMemo, useState, useSyncExternalStore, type Dispatch, type SetStateAction } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { observeRequest } from './organization-request';
import type { ControlRequest, ControlTransport } from '@/platform/transport';

type Attachment = { id?: string; mediaId?: string; sha256?: string; [key: string]: unknown };
type Saved<T> = { draft: string; attachments: T[]; savedAtMs: number };
const PREFIX = 'paw.workspace.draft.v1:';
export function recoveryScope(transport: Pick<ControlTransport, 'kind' | 'connectionIdentity'>, spaceKey: string) {
  // Native PAW is a single local principal. Anonymous test/preview transports
  // do not share or persist state with another unknown backend.
  const connection = transport.connectionIdentity || (transport.kind === 'native' ? 'native-local' : '');
  return connection ? PREFIX + encodeURIComponent(connection) + ':' + encodeURIComponent(spaceKey) : '';
}
function load<T>(key: string): Saved<T> | null {
  try {
    const raw = key && localStorage.getItem(key);
    if (!raw) return null;
    const value = JSON.parse(raw) as Saved<T>;
    return typeof value.draft === 'string' && Array.isArray(value.attachments) && value.attachments.length <= 8 && value.attachments.every(item => item !== null && typeof item === 'object') ? value : null;
  } catch { return null; }
}
function serializable<T>(attachments: T[]): T[] {
  return attachments.map(item => {
    const value = { ...item } as Record<string, unknown>;
    delete value.previewFile;
    return value as T;
  });
}
type Input<T> = Pick<Saved<T>, 'draft' | 'attachments'>;
type RecoverySnapshot<T> = Saved<T> & { warning: string; restored: boolean; restoredIds: ReadonlySet<string> };
type RecoveryOwner<T> = {
  key: string; id: string; snapshot: RecoverySnapshot<T>; listeners: Set<() => void>;
  persistedValue?: string;
};
// Mounted composers share the existing draft owner. Pending callbacks retain
// their original owner, but resolve a reopened owner before restoring input.
const owners = new Map<string, RecoveryOwner<object>>();
function attachmentIds<T>(attachments: T[]): Set<string> {
  return new Set(attachments.map(raw => { const item = raw as Attachment; return item.id || item.mediaId || ''; }));
}
function createOwner<T extends object>(key: string, initialDraft: string, initialAttachments: T[]): RecoveryOwner<T> {
  const existing = key && owners.get(key);
  if (existing) return existing as RecoveryOwner<T>;
  const saved = load<T>(key);
  const owner: RecoveryOwner<T> = { key, id: key || `anonymous-draft:${crypto.randomUUID()}`, listeners: new Set(), persistedValue: saved ? JSON.stringify(saved) : undefined, snapshot: {
    ...(saved ?? { draft: '', attachments: [], savedAtMs: 0 }),
    ...(initialDraft ? { draft: initialDraft } : {}), ...(initialAttachments.length ? { attachments: initialAttachments } : {}),
    warning: '', restored: Boolean(saved), restoredIds: attachmentIds(saved?.attachments ?? []),
  } };
  if (key) owners.set(key, owner as RecoveryOwner<object>);
  return owner;
}
function persist<T>(owner: RecoveryOwner<T>): void {
  if (!owner.key) return;
  let warning = owner.snapshot.warning;
  try {
    const previous = localStorage.getItem(owner.key);
    if (previous && !load<T>(owner.key)) {
      if (!owner.snapshot.draft && !owner.snapshot.attachments.length) {
        owner.snapshot = { ...owner.snapshot, warning: '本机草稿记录无法读取，原记录已保留；可以重新输入，旧记录会另存。' };
        return;
      }
      localStorage.setItem(owner.key + ':unreadable', previous);
      warning = '旧草稿记录无法读取，已另存原记录；当前草稿继续保存在本机。';
    }
    const { draft, attachments } = owner.snapshot;
    const value = JSON.stringify({ draft, attachments: serializable(attachments), savedAtMs: Date.now() });
    localStorage.setItem(owner.key, value);
    owner.persistedValue = value;
  } catch { warning = '草稿暂时无法保存到本机，请在关闭窗口前复制保留。'; }
  if (warning !== owner.snapshot.warning) owner.snapshot = { ...owner.snapshot, warning };
}
function updateOwner<T extends object>(owner: RecoveryOwner<T>, update: (input: Input<T>) => Input<T>, recovering = false): void {
  const target = (owner.key && owners.get(owner.key) || owner) as RecoveryOwner<T>;
  // A closed view's snapshot can predate edits made by a later, now closed
  // view. Disk is the current owner when there is no mounted composer.
  if (owner.key && !owners.has(owner.key)) {
    const saved = load<T>(owner.key);
    if (saved) {
      target.snapshot = { ...target.snapshot, ...saved };
      target.persistedValue = JSON.stringify(saved);
    }
  }
  const previous = target.snapshot;
  const next = update(previous);
  target.snapshot = { ...previous, ...next,
    ...(recovering ? { restored: true, restoredIds: new Set([
      ...previous.restoredIds,
      ...attachmentIds(next.attachments.filter(item => !previous.attachments.includes(item))),
    ]) } : {}),
  };
  // Persist outside React state updaters: cleanup and late HTTP rejection must
  // save even when their originating component can no longer render.
  persist(target);
  target.listeners.forEach(listener => listener());
}
export function useWorkspaceRecovery<T extends object>(spaceKey: string, initialDraft = '', initialAttachments: T[] = []) {
  const transport = useControlTransport();
  const key = recoveryScope(transport, spaceKey);
  const owner = useMemo(() => createOwner<T>(key, initialDraft, initialAttachments), [key, spaceKey, key ? null : transport]);
  const lifetime = useMemo(() => ({ active: false, subscribed: false, owner }), [owner]);
  const subscribe = useCallback((listener: () => void) => {
    // Effects may reconnect without recreating this hook. A later view may
    // already own the same draft, so never register this view's stale owner.
    const currentOwner = (key && owners.get(key) || lifetime.owner) as RecoveryOwner<T>;
    if (key && !owners.has(key) && lifetime.subscribed) {
      const saved = load<T>(key);
      // StrictMode replay of our own write must preserve live attachment
      // previews and unsaved input when storage failed. Only changed disk
      // content is a new recovery source; initial seeds never run again.
      if (saved && JSON.stringify(saved) !== currentOwner.persistedValue) {
        currentOwner.snapshot = { ...currentOwner.snapshot, ...saved,
          restored: true, restoredIds: attachmentIds(saved.attachments) };
        currentOwner.persistedValue = JSON.stringify(saved);
      }
    }
    lifetime.owner = currentOwner;
    lifetime.subscribed = true;
    lifetime.active = true;
    if (key) owners.set(key, currentOwner as RecoveryOwner<object>);
    currentOwner.listeners.add(listener);
    persist(currentOwner);
    return () => {
      lifetime.active = false;
      currentOwner.listeners.delete(listener);
      if (key && !currentOwner.listeners.size && owners.get(key) === currentOwner) owners.delete(key);
    };
  }, [key, lifetime]);
  const current = useSyncExternalStore(subscribe, () => lifetime.owner.snapshot, () => lifetime.owner.snapshot);
  // Ordinary completion callbacks belong to their mounted view. Only the
  // explicit recovery path below may restore input after that view is gone.
  const setDraft: Dispatch<SetStateAction<string>> = useCallback(value => {
    if (lifetime.active) updateOwner(lifetime.owner, current => ({
      ...current, draft: typeof value === 'function' ? value(current.draft) : value,
    }));
  }, [lifetime]);
  const setAttachments: Dispatch<SetStateAction<T[]>> = useCallback(value => {
    if (lifetime.active) updateOwner(lifetime.owner, current => ({
      ...current, attachments: typeof value === 'function' ? value(current.attachments) : value,
    }));
  }, [lifetime]);
  const recoverInput = useCallback((update: (input: Input<T>) => Input<T>) => {
    updateOwner(lifetime.owner, update, !lifetime.active);
  }, [lifetime]);
  const [issues, setIssues] = useState<string[]>([]);
  const [checking, setChecking] = useState(current.savedAtMs > 0 && current.attachments.length > 0);
  // A just-imported attachment already has an authoritative owner-bound
  // receipt. Only references restored from disk need continuity verification.
  const attachmentSignature = JSON.stringify(current.attachments.map(raw => {
    const item = raw as Attachment;
    return { id: item.id || item.mediaId || '', sha256: item.sha256 || '' };
  }).filter(item => current.restoredIds.has(item.id)));
  useEffect(() => {
    if (attachmentSignature === '[]' || !key) { setIssues([]); setChecking(false); return; }
    const controller = new AbortController();
    setChecking(true);
    void observeRequest<ControlRequest, { ok: boolean; items: { id: string; available: boolean }[] }>(input => transport.request(input), {
      pathId: 'agent.continuity.media', body: { spaceKey, attachments: JSON.parse(attachmentSignature) }, signal: controller.signal,
    }).then(result => {
      if (controller.signal.aborted) return;
      if (!result.ok || !Array.isArray(result.items)) throw new Error('Unavailable');
      const expected = JSON.parse(attachmentSignature) as { id: string }[];
      setIssues(expected.filter(ref => !result.items.some(item => item.id === ref.id && item.available === true)).map(item => item.id));
    }).catch(() => {
      if (!controller.signal.aborted) setIssues(JSON.parse(attachmentSignature).map((a: { id: string }) => a.id));
    }).finally(() => { if (!controller.signal.aborted) setChecking(false); });
    return () => controller.abort();
  }, [transport, key, spaceKey, attachmentSignature]);
  const removeUnavailable = () => setAttachments(items => items.filter(raw => {
    const item = raw as Attachment;
    return !issues.includes(String(item.id || item.mediaId || ''));
  }));
  return { draft: current.draft, setDraft, attachments: current.attachments, setAttachments,
    recoverInput, ownerId: lifetime.owner.id, warning: current.warning, checking, issues, removeUnavailable, restored: current.restored };
}

export function WorkspaceRecoveryNotice({ recovery }: { recovery: {
  warning: string; checking: boolean; issues: string[]; removeUnavailable: () => void;
} }) {
  if (!recovery.warning && !recovery.checking && !recovery.issues.length) return null;
  return <div className="workspace-recovery-notice" role="status">
    {recovery.warning || (recovery.checking ? '正在核实恢复的附件…' : `${recovery.issues.length} 项附件已失效或暂时无法核实，引用仍保留。`)}
    {recovery.issues.length ? <button type="button" onClick={recovery.removeUnavailable}>移除这些附件引用</button> : null}
  </div>;
}
