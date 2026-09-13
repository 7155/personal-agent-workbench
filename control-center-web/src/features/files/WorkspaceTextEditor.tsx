import { useMemo, useReducer, type ReactNode } from 'react';
import { useControlTransport } from '@/app/control-transport';
import type { ControlTransport } from '@/platform/transport';
import './files-editor.css';

const MAX_TEXT_BYTES = 2 * 1024 * 1024;
const encoder = new TextEncoder();
type FileIdentity = { sessionId: string; path: string; name: string };
export interface EditableWorkspacePreview {
  path: string;
  /** Server-bound regular-file target; path remains the requested UI selection. */
  canonicalPath?: string;
  content: string;
  byteSize: number;
  loadedBytes: number;
  truncated: boolean;
  resourceRevision?: string;
  editability?: { editable: boolean; reason?: string };
}
type Snapshot = EditableWorkspacePreview & { resourceRevision: string };
type Draft = {
  content: string;
  base?: Snapshot;
  editing: boolean;
  busy?: 'reading' | 'saving';
  error?: string;
  notice?: string;
  needsCheck?: boolean;
  disk?: Snapshot;
  lastSubmitted?: string;
};

/** App-lifetime drafts are keyed by the owning Session and file, independent of reader refreshes. */
export function useWorkspaceTextEditor(file: FileIdentity | null, preview: EditableWorkspacePreview | null, onSaved?: (snapshot: EditableWorkspacePreview & { sessionId: string }) => void): { panel: ReactNode; editing: boolean; copyContent: string | null; resourcePath: string | null; draftPreview: EditableWorkspacePreview | null } {
  const transport = useControlTransport();
  const drafts = useMemo(() => new Map<string, Draft>(), [transport]);
  const [, redraw] = useReducer((value: number) => value + 1, 0);
  const key = file ? JSON.stringify([file.sessionId, file.path]) : '';
  const draft = drafts.get(key);
  function update(target: string, patch: Partial<Draft>): void {
    drafts.set(target, { content: '', editing: false, ...drafts.get(target), ...patch });
    redraw();
  }

  async function startEditing(): Promise<void> {
    if (!file || drafts.get(key)?.busy) return;
    if (draft?.base) { update(key, { editing: true }); return; }
    update(key, { busy: 'reading', error: '', notice: '' });
    try {
      const snapshot = await readCompleteFile(transport, file, draft?.error ? undefined : preview ?? undefined);
      if (snapshot.editability?.editable !== true) throw new Error(snapshot.editability?.reason || '当前文件不能编辑。');
      update(key, { content: snapshot.content, base: snapshot, editing: true, busy: undefined });
    } catch (error) {
      update(key, { busy: undefined, error: errorMessage(error) });
    }
  }

  async function save(): Promise<void> {
    const current = drafts.get(key);
    if (!file || !current?.base || current.busy || current.needsCheck || current.disk || current.content === current.base.content) return;
    if ((preview ?? current.base).editability?.editable !== true) return;
    const content = current.content;
    const targetPath = current.base.canonicalPath ?? current.base.path;
    const body = { path: targetPath, content, resourceRevision: current.base.resourceRevision };
    if (encoder.encode(content).length > MAX_TEXT_BYTES || encoder.encode(JSON.stringify(body)).length > 1_990_000) {
      update(key, { error: '文本或请求超过保存上限（文件 2 MiB，请求 2 MB）。草稿已保留，请缩小内容后再保存。' });
      return;
    }
    update(key, { busy: 'saving', lastSubmitted: content, error: '', notice: '' });
    try {
      const response = await transport.request({ pathId: 'agent.session.workspace.save', params: { sessionId: file.sessionId }, body });
      if (!isRecord(response) || response.ok !== true || response.saved !== true || response.sessionId !== file.sessionId || response.path !== targetPath || !validRevision(response.resourceRevision)) {
        throw new Error('未收到与当前文件匹配的保存回执。');
      }
      const byteSize = encoder.encode(content).length;
      const snapshot = { ...current.base, canonicalPath: targetPath, content, resourceRevision: response.resourceRevision, byteSize, loadedBytes: byteSize, truncated: false };
      update(key, {
        base: snapshot,
        busy: undefined, needsCheck: false, notice: '已保存到文件。', disk: undefined,
      });
      onSaved?.({ ...snapshot, sessionId: file.sessionId });
    } catch (error) {
      update(key, { busy: undefined, needsCheck: true, error: `${errorMessage(error)} 草稿已保留，请先核对磁盘版本。` });
    }
  }

  async function reconcile(): Promise<void> {
    const current = drafts.get(key);
    if (!file || !current?.base || current.busy) return;
    update(key, { busy: 'reading', error: '', notice: '' });
    try {
      // Recovery stays on the accepted resource even if its former alias moves.
      const disk = await readCompleteFile(transport, { ...file, path: current.base.canonicalPath ?? current.base.path });
      const latest = drafts.get(key)!;
      if (disk.content === current.lastSubmitted || disk.content === latest.content || disk.resourceRevision === current.base.resourceRevision) {
        update(key, { base: disk, busy: undefined, needsCheck: false, disk: undefined, notice: disk.content === current.lastSubmitted ? '磁盘内容与上次提交一致。' : '已核对磁盘版本，草稿已保留。' });
      } else {
        update(key, { disk, busy: undefined, needsCheck: true, notice: '磁盘文件已变化。请比较两个版本，再选择如何继续。' });
      }
    } catch (error) {
      update(key, { busy: undefined, needsCheck: true, error: `${errorMessage(error)} 草稿仍然保留。` });
    }
  }

  if (!file) return { panel: null, editing: false, copyContent: null, resourcePath: null, draftPreview: null };
  const editing = Boolean(draft?.editing && draft.base);
  const changed = Boolean(draft?.base && draft.content !== draft.base.content);
  const editable = (preview ?? draft?.base)?.editability;
  const canStart = Boolean(preview?.resourceRevision && editable?.editable === true);
  const resourcePath = draft?.base?.canonicalPath ?? preview?.canonicalPath ?? draft?.base?.path ?? preview?.path ?? null;
  const panel = <div className="paw-files-editor">
    <div className="paw-files-editor__toolbar">
      {editing ? <>
        <span role="status">{draft?.busy === 'saving' ? '正在保存…' : changed ? '未保存的更改' : '与已读取版本一致'}</span>
        <button disabled={Boolean(draft?.busy || draft?.needsCheck || draft?.disk || !changed || editable?.editable !== true)} onClick={() => void save()} title="保存文件（⌘S / Ctrl+S）" type="button">{draft?.busy === 'saving' ? '正在保存' : '保存文件'}</button>
        <button onClick={() => update(key, { editing: false })} type="button">预览草稿</button>
      </> : draft?.base ? <>
        <span>{changed ? '此文件有未保存草稿' : '已保留编辑内容'}</span>
        <button onClick={() => void startEditing()} type="button">继续编辑</button>
      </> : canStart ? <>
        <span>{preview?.truncated ? '当前是部分预览，编辑前将读取完整文本。' : '直接编辑工作区文本'}</span>
        <button disabled={Boolean(draft?.busy)} onClick={() => void startEditing()} type="button">{draft?.busy ? '正在读取完整文件…' : preview?.truncated ? '读取完整文件后编辑' : '编辑文本'}</button>
      </> : preview ? <span>{editable?.reason || '当前文件服务未提供可写版本，暂时只能预览。'}</span> : null}
    </div>
    {resourcePath && resourcePath !== file.path ? <p>实际文件：{resourcePath}</p> : null}
    {draft?.base && editable?.editable === false ? <p role="status">{editable.reason || '当前文件为只读，草稿已保留。'}</p> : null}
    {draft?.notice ? <p role="status">{draft.notice}</p> : null}
    {draft?.error ? <p role="alert">{draft.error}</p> : null}
    {draft?.needsCheck ? <button disabled={Boolean(draft.busy)} onClick={() => void reconcile()} type="button">{draft.busy === 'reading' ? '正在核对…' : '核对磁盘版本'}</button> : null}
    {editing ? <textarea
      aria-label={`编辑 ${file.name}`}
      autoFocus
      autoCapitalize="off"
      autoCorrect="off"
      className="paw-files-editor__input"
      onChange={(event) => {
        // Textareas expose LF; retain a CRLF source's convention instead of rewriting every line.
        const source = draft?.base?.content ?? '';
        const usesCrlf = source.includes('\r\n') && !source.replace(/\r\n/g, '').includes('\n');
        update(key, { content: usesCrlf ? event.target.value.replace(/\r?\n/g, '\r\n') : event.target.value, notice: '' });
      }}
      onKeyDown={(event) => {
        if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') { event.preventDefault(); event.stopPropagation(); void save(); }
        if (event.key === 'Escape') event.stopPropagation();
      }}
      spellCheck={false}
      value={draft?.content ?? ''}
    /> : null}
    {draft?.disk ? <div className="paw-files-editor__conflict">
      <strong>磁盘当前内容</strong>
      <pre>{draft.disk.content}</pre>
      <div className="paw-files-editor__toolbar">
        <button disabled={draft.disk.editability?.editable !== true} onClick={() => update(key, { base: draft.disk, disk: undefined, needsCheck: false, notice: '已保留草稿。下次保存将基于刚核对的磁盘版本。' })} type="button">保留草稿，基于此版本继续编辑</button>
        <button onClick={() => update(key, { content: draft.disk!.content, base: draft.disk, disk: undefined, needsCheck: false, notice: '已改用磁盘版本。' })} type="button">改用磁盘版本</button>
      </div>
    </div> : null}
  </div>;
  let draftPreview: EditableWorkspacePreview | null = null;
  if (draft?.base && !editing) {
    const byteSize = encoder.encode(draft.content).length;
    // The reader owns Markdown, HTML, SVG and code presentation. Use the
    // complete local draft without rereading disk or changing save authority.
    draftPreview = { ...draft.base, content: draft.content, byteSize, loadedBytes: byteSize, truncated: false };
  }
  return { panel, editing: Boolean(draft?.base), copyContent: draft?.base ? draft.content : null, resourcePath, draftPreview };
}

export async function readCompleteFile(transport: ControlTransport, file: FileIdentity, seed?: EditableWorkspacePreview): Promise<Snapshot> {
  let snapshot = seed && seed.path === file.path ? { ...seed } : await readChunk(transport, file, 0);
  if (!validRevision(snapshot.resourceRevision)) throw new Error('文件服务未提供读取版本，无法安全保存。');
  if (snapshot.byteSize > MAX_TEXT_BYTES) throw new Error('文本编辑支持最大 2 MiB 的文件。');
  const revision = snapshot.resourceRevision;
  const canonicalPath = snapshot.canonicalPath ?? snapshot.path;
  while (snapshot.truncated) {
    const next = await readChunk(transport, file, snapshot.loadedBytes);
    if ((next.canonicalPath ?? next.path) !== canonicalPath || next.resourceRevision !== revision || next.byteSize !== snapshot.byteSize) throw new Error('读取期间文件已变化，请重新读取完整文件。');
    if (next.loadedBytes <= snapshot.loadedBytes || next.loadedBytes > snapshot.byteSize) throw new Error('文件服务未返回有效的继续读取位置。');
    snapshot = { ...next, content: snapshot.content + next.content };
  }
  if (snapshot.content.includes('\0') || encoder.encode(snapshot.content).length !== snapshot.byteSize || snapshot.loadedBytes !== snapshot.byteSize) throw new Error('未取得完整 UTF-8 文本，暂时不能编辑此文件。');
  return { ...snapshot, resourceRevision: revision };
}

async function readChunk(transport: ControlTransport, file: FileIdentity, offset: number): Promise<EditableWorkspacePreview> {
  const value = await transport.request({ pathId: 'agent.session.workspace.read', params: { sessionId: file.sessionId }, query: { path: file.path, offset, limit: 65_536 } });
  if (!isRecord(value) || value.ok !== true || typeof value.content !== 'string' || typeof value.byteSize !== 'number' || typeof value.nextOffset !== 'number' || (value.offset !== undefined && value.offset !== offset)) throw new Error('文件服务返回了无法识别的数据。');
  const canonicalPath = canonicalWorkspaceReadPath(value, file.path, file.sessionId);
  return { path: file.path, canonicalPath, content: value.content, byteSize: value.byteSize, loadedBytes: value.nextOffset, truncated: value.truncated === true, resourceRevision: validRevision(value.resourceRevision) ? value.resourceRevision : undefined, editability: isRecord(value.editability) ? { editable: value.editability.editable === true, reason: typeof value.editability.reason === 'string' ? value.editability.reason : undefined } : undefined };
}
/** Legacy exact-path replies remain valid. A different path needs explicit owner-bound alias evidence. */
export function canonicalWorkspaceReadPath(value: Record<string, unknown>, requestedPath: string, sessionId: string): string {
  if (typeof value.path !== 'string' || !value.path.startsWith('/') || value.path.includes('\0')
    || (value.sessionId !== undefined && value.sessionId !== sessionId)
    || (value.requestedPath !== undefined && value.requestedPath !== requestedPath)
    || (value.path !== requestedPath && (value.requestedPath !== requestedPath || value.sessionId !== sessionId || !validRevision(value.resourceRevision)))) {
    throw new Error('文件服务返回了无法识别的数据。');
  }
  return value.path;
}
function validRevision(value: unknown): value is string { return typeof value === 'string' && /^sha256:[a-f0-9]{64}$/.test(value); }
function isRecord(value: unknown): value is Record<string, unknown> { return value !== null && typeof value === 'object' && !Array.isArray(value); }
function errorMessage(error: unknown): string { return error instanceof Error ? error.message : '文件操作未完成。'; }
