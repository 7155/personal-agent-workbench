import type { ControlTransport } from '@/platform/transport';
import { recoveryScope } from '@/features/semantic-workspace/workspace-recovery';
import type { EditableWorkspacePreview } from './WorkspaceTextEditor';

type FileScope = { sessionId: string; path: string };
export type WorkspaceTextDraft = {
  content: string;
  base: EditableWorkspacePreview & { resourceRevision: string };
  editing: boolean;
  lastSubmitted?: string;
};
type RecordV1 = WorkspaceTextDraft & FileScope & { version: 1; savedAtMs: number };
const encoder = new TextEncoder();
const storageWarning = '草稿暂时无法保存在本机，当前输入仍保留；关闭前请复制。';
const unreadableWarning = '本机草稿记录无法读取，原记录仍保留；当前文件可以重新编辑。';
const keyFor = (transport: ControlTransport, file: FileScope) => recoveryScope(transport, `file:${JSON.stringify([transport.kind, file.sessionId, file.path])}`);
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value);

function decode(raw: string, file: FileScope, maxBytes: number): WorkspaceTextDraft | null {
  const value: unknown = JSON.parse(raw);
  if (!object(value) || value.version !== 1 || value.sessionId !== file.sessionId || value.path !== file.path
    || typeof value.content !== 'string' || typeof value.editing !== 'boolean'
    || (value.lastSubmitted !== undefined && typeof value.lastSubmitted !== 'string') || !object(value.base)) return null;
  const base = value.base;
  if (base.path !== file.path || typeof base.content !== 'string' || base.content.includes('\0')
    || typeof base.resourceRevision !== 'string' || !/^sha256:[a-f0-9]{64}$/u.test(base.resourceRevision)
    || base.truncated !== false || !Number.isSafeInteger(base.byteSize) || Number(base.byteSize) < 0 || Number(base.byteSize) > maxBytes
    || base.loadedBytes !== base.byteSize || encoder.encode(base.content).length !== base.byteSize
    || (base.canonicalPath !== undefined && (typeof base.canonicalPath !== 'string' || !base.canonicalPath.startsWith('/') || base.canonicalPath.includes('\0')))) return null;
  // Stored text/revision is evidence, never a recovered permission grant.
  return { content: value.content, editing: value.editing, lastSubmitted: value.lastSubmitted as string | undefined,
    base: { path: file.path, canonicalPath: base.canonicalPath as string | undefined, content: base.content,
      resourceRevision: base.resourceRevision, byteSize: Number(base.byteSize), loadedBytes: Number(base.loadedBytes), truncated: false } };
}

type StorageResult = { persistedValue?: string | null; warning?: string };
export function readWorkspaceTextDraft(transport: ControlTransport, file: FileScope, maxBytes: number): StorageResult & { draft: WorkspaceTextDraft | null } {
  const key = keyFor(transport, file);
  if (!key) return { draft: null, persistedValue: null };
  let raw: string | null;
  try { raw = localStorage.getItem(key); } catch { return { draft: null, warning: storageWarning }; }
  if (!raw) return { draft: null, persistedValue: null };
  try {
    const draft = decode(raw, file, maxBytes);
    return draft ? { draft, persistedValue: raw } : { draft: null, persistedValue: raw, warning: unreadableWarning };
  } catch { return { draft: null, persistedValue: raw, warning: unreadableWarning }; }
}

export function writeWorkspaceTextDraft(transport: ControlTransport, file: FileScope, draft: WorkspaceTextDraft | null, maxBytes: number, expectedValue: string | null | undefined): StorageResult {
  const key = keyFor(transport, file);
  if (!key) return { persistedValue: null };
  try {
    const old = localStorage.getItem(key);
    // A closed view's late receipt must not clear a reopened view's newer
    // input. Compare the persisted value as workspace recovery does when a
    // later view can own a newer record; do not replace that record here.
    if (old !== (expectedValue ?? null)) return { persistedValue: expectedValue,
      warning: '本机已有更新的草稿，当前输入仍保留；关闭前请复制。' };
    if (old) {
      let valid = false;
      try { valid = Boolean(decode(old, file, maxBytes)); } catch { /* Preserve an unreadable record before replacing it. */ }
      if (!valid) localStorage.setItem(key + ':unreadable', old);
    }
    if (!draft) { localStorage.removeItem(key); return { persistedValue: null }; }
    const { base } = draft;
    const value: RecordV1 = { version: 1, sessionId: file.sessionId, path: file.path, savedAtMs: Date.now(),
      content: draft.content, editing: draft.editing, lastSubmitted: draft.lastSubmitted,
      base: { path: file.path, canonicalPath: base.canonicalPath ?? base.path, content: base.content, resourceRevision: base.resourceRevision,
        byteSize: base.byteSize, loadedBytes: base.loadedBytes, truncated: false } };
    const persistedValue = JSON.stringify(value);
    localStorage.setItem(key, persistedValue);
    return { persistedValue };
  } catch { return { persistedValue: expectedValue, warning: storageWarning }; }
}
