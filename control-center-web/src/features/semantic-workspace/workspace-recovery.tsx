import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react';
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
export function useWorkspaceRecovery<T extends object>(spaceKey: string, initialDraft = '', initialAttachments: T[] = []) {
  const transport = useControlTransport();
  const key = recoveryScope(transport, spaceKey);
  const [state, setState] = useState(() => ({ key, ...(load<T>(key) ?? { draft: initialDraft, attachments: initialAttachments, savedAtMs: 0 }),
    ...(initialDraft ? { draft: initialDraft } : {}), ...(initialAttachments.length ? { attachments: initialAttachments } : {}) }));
  const current = state.key === key ? state : { key, ...(load<T>(key) ?? { draft: initialDraft, attachments: initialAttachments, savedAtMs: 0 }) };
  const [warning, setWarning] = useState('');
  const [issues, setIssues] = useState<string[]>([]);
  const [checking, setChecking] = useState(current.savedAtMs > 0 && current.attachments.length > 0);
  const restored = useRef({ key, saved: current.savedAtMs > 0, ids: new Set(current.savedAtMs > 0 ? current.attachments.map(raw => {
    const item = raw as Attachment;
    return item.id || item.mediaId || '';
  }) : []) });
  if (restored.current.key !== key) restored.current = { key, saved: current.savedAtMs > 0, ids: new Set(current.savedAtMs > 0 ? current.attachments.map(raw => {
    const item = raw as Attachment;
    return item.id || item.mediaId || '';
  }) : []) };
  const setDraft: Dispatch<SetStateAction<string>> = useCallback(value => setState(previous => {
    const base = previous.key === key ? previous : { key, ...(load<T>(key) ?? { draft: '', attachments: [], savedAtMs: 0 }) };
    return { ...base, draft: typeof value === 'function' ? value(base.draft) : value };
  }), [key]);
  const setAttachments: Dispatch<SetStateAction<T[]>> = useCallback(value => setState(previous => {
    const base = previous.key === key ? previous : { key, ...(load<T>(key) ?? { draft: '', attachments: [], savedAtMs: 0 }) };
    return { ...base, attachments: typeof value === 'function' ? value(base.attachments) : value };
  }), [key]);
  useEffect(() => {
    if (state.key !== key) { setState(current); return; }
    if (!key) return;
    try {
      const previous = localStorage.getItem(key);
      if (previous && !load<T>(key)) {
        // Never replace an unreadable record with the empty mount fallback.
        if (!state.draft && !state.attachments.length) {
          setWarning('本机草稿记录无法读取，原记录已保留；可以重新输入，旧记录会另存。');
          return;
        }
        localStorage.setItem(key + ':unreadable', previous);
        setWarning('旧草稿记录无法读取，已另存原记录；当前草稿继续保存在本机。');
      }
      const value = { draft: state.draft, attachments: serializable(state.attachments), savedAtMs: Date.now() };
      localStorage.setItem(key, JSON.stringify(value));
    } catch { setWarning('草稿暂时无法保存到本机，请在关闭窗口前复制保留。'); }
  }, [key, state]);
  // A just-imported attachment already has an authoritative owner-bound
  // receipt. Only references restored from disk need continuity verification.
  const attachmentSignature = JSON.stringify(current.attachments.map(raw => {
    const item = raw as Attachment;
    return { id: item.id || item.mediaId || '', sha256: item.sha256 || '' };
  }).filter(item => restored.current.ids.has(item.id)));
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
    warning, checking, issues, removeUnavailable, restored: restored.current.saved };
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
