import { ChevronDown, FolderOpen, Plus, X } from 'lucide-react';
import { useEffect, useId, useMemo, useRef, useState } from 'react';
import type { SessionSummary } from '@/features/agent/types';

export interface ProjectWorkspacePickerProps {
  sessions: SessionSummary[];
  session?: SessionSummary;
  draftRoot: string;
  open: boolean;
  disabled?: boolean;
  loading?: boolean;
  onOpenChange(open: boolean): void;
  onDraftRootChange(path: string): void;
  onSelectSession(session: SessionSummary): void;
  onCreateSession(path: string): Promise<void>;
  onPickDirectory?: () => Promise<string | null>;
}

// Compare directory spellings without pretending to resolve symlinks on the client.
export function normalizeWorkspaceRoot(path: string): string {
  const trimmed = path.trim();
  return trimmed.startsWith('/') ? trimmed.replace(/\/+$/, '') || '/' : trimmed;
}

function projectName(path: string): string {
  return path.split('/').filter(Boolean).at(-1) || path || '选择 GIS 项目';
}

export function ProjectWorkspacePicker({ sessions, session, draftRoot, open, disabled = false, loading = false, onOpenChange, onDraftRootChange, onSelectSession, onCreateSession, onPickDirectory }: ProjectWorkspacePickerProps) {
  const panelId = useId();
  const hintId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const [selectedSessionId, setSelectedSessionId] = useState('');
  const [working, setWorking] = useState<'pick' | 'create' | null>(null);
  const [error, setError] = useState('');
  const workingRef = useRef(false);
  const currentRoot = normalizeWorkspaceRoot(session?.workspaceRoots?.[0] ?? '');
  const candidateRoot = normalizeWorkspaceRoot(draftRoot);
  const validRoot = candidateRoot.startsWith('/');
  const blocked = disabled || loading || working !== null;
  const projects = useMemo(() => {
    const groups = new Map<string, SessionSummary[]>();
    for (const item of sessions) {
      const path = normalizeWorkspaceRoot(item.workspaceRoots?.[0] ?? '');
      if (!path.startsWith('/')) continue;
      groups.set(path, [...(groups.get(path) ?? []), item]);
    }
    return [...groups].map(([path, agents]) => ({ path, agents: agents.sort((a, b) => b.updatedAtMs - a.updatedAtMs) }))
      .sort((a, b) => b.agents[0].updatedAtMs - a.agents[0].updatedAtMs);
  }, [sessions]);
  const matching = projects.find(item => item.path === candidateRoot)?.agents ?? [];
  const selected = matching.find(item => item.id === selectedSessionId)
    ?? matching.find(item => item.id === session?.id)
    ?? matching[0];
  const visibleRoot = currentRoot || candidateRoot;

  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) onOpenChange(false);
    };
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [open, onOpenChange]);

  function changeRoot(path: string) {
    setError('');
    setSelectedSessionId('');
    onDraftRootChange(path);
  }

  async function pickDirectory() {
    if (!onPickDirectory || workingRef.current) return;
    workingRef.current = true; setWorking('pick'); setError('');
    try {
      const path = await onPickDirectory();
      if (path) changeRoot(path);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally { workingRef.current = false; setWorking(null); }
  }

  async function createSession() {
    if (!validRoot || blocked || workingRef.current) return;
    workingRef.current = true; setWorking('create'); setError('');
    try {
      await onCreateSession(candidateRoot);
      onOpenChange(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally { workingRef.current = false; setWorking(null); }
  }

  return <div className="earth-project-picker" ref={rootRef} onKeyDown={event => {
    if (event.key === 'Escape' && open) { event.stopPropagation(); onOpenChange(false); triggerRef.current?.focus(); }
  }}>
    <button ref={triggerRef} className="earth-project-picker__trigger" type="button" aria-label="选择 GIS 项目与 Agent" aria-expanded={open} aria-controls={panelId} onClick={() => onOpenChange(!open)}>
      <FolderOpen size={17} aria-hidden="true" />
      <span className="earth-project-picker__current"><strong>{projectName(visibleRoot)}</strong><span aria-label={currentRoot ? '当前项目文件夹' : '待打开项目文件夹'} title={visibleRoot}>{visibleRoot || '选择文件夹或已有项目'}</span></span>
      <span className="earth-project-picker__agent" title={session?.title}>{session ? session.title : loading ? '正在读取…' : '未进入 Agent'}</span>
      <ChevronDown size={13} aria-hidden="true" />
    </button>
    {open ? <section id={panelId} className="earth-project-picker__panel" aria-label="项目与 Agent 选择" aria-busy={working !== null}>
      <header><strong>项目与 Agent</strong><button type="button" aria-label="关闭项目选择" onClick={() => onOpenChange(false)}><X size={15} aria-hidden="true" /></button></header>
      {projects.length ? <label>已有 GIS 项目<select aria-label="已有 GIS 项目" value={projects.some(item => item.path === candidateRoot) ? candidateRoot : ''} disabled={blocked} onChange={event => changeRoot(event.target.value)}><option value="" disabled>选择项目或浏览其他文件夹</option>{projects.map(item => <option key={item.path} value={item.path}>{projectName(item.path)} · {item.agents.length} 个 Agent · {item.path}</option>)}</select></label> : null}
      <label>项目文件夹<span className="earth-project-picker__path"><input aria-label="项目文件夹" aria-describedby={hintId} value={draftRoot} disabled={blocked} placeholder="输入绝对路径，例如 /data/gis-project" onChange={event => changeRoot(event.target.value)} />{onPickDirectory ? <button type="button" disabled={blocked} onClick={() => void pickDirectory()}><FolderOpen size={14} aria-hidden="true" />{working === 'pick' ? '选择中…' : '浏览…'}</button> : null}</span></label>
      <p id={hintId} className="earth-project-picker__hint">{currentRoot && candidateRoot !== currentRoot ? '待打开的项目；当前 Agent 仍在原文件夹工作。' : '选择文件夹后，进入已有 Agent 或明确新建一个。'}</p>
      {matching.length ? <label>项目 Agent<select aria-label="项目 Agent" value={selected?.id ?? ''} disabled={blocked} onChange={event => setSelectedSessionId(event.target.value)}>{matching.map(item => <option key={item.id} value={item.id}>{item.title} · {item.id.slice(0, 8)}{item.id === session?.id ? ' · 当前' : ''}</option>)}</select></label> : <p className="earth-project-picker__empty">{validRoot ? '当前列表未找到此文件夹的 GIS Agent。新建后即可读取其中的地理文件。' : '先选择一个项目文件夹。'}</p>}
      <div className="earth-project-picker__actions">
        {selected ? <button type="button" className="earth-project-picker__primary" disabled={blocked || selected.id === session?.id} onClick={() => { onSelectSession(selected); onOpenChange(false); }}>{selected.id === session?.id ? '当前 Agent' : '进入所选 Agent'}</button> : null}
        <button type="button" className={!selected ? 'earth-project-picker__primary' : undefined} disabled={blocked || !validRoot} onClick={() => void createSession()}><Plus size={14} aria-hidden="true" />{working === 'create' ? '正在创建…' : matching.length ? '新建同项目 Agent' : '新建项目 Agent'}</button>
      </div>
      {error ? <p className="earth-project-picker__error" role="alert">{error}</p> : null}
      <p className="earth-project-picker__boundary">同目录的 Agent 共享图层和文件。图层保存冲突会提示重新读取，已有版本保留。</p>
    </section> : null}
  </div>;
}
