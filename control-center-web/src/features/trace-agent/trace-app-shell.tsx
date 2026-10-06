import { BookOpen, Boxes, FolderClock, Plus, Search } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { useState, type ReactNode } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { Button } from '@/components/primitives';
import { publicErrorText } from '@/features/overview/management-ui';
import { usePawOsAppActive } from '@/features/paw-os/surface-context';
import { AppSidebarToggle, useAppSidebar } from '@/paw-os/apps/app-sidebar';

export type TraceAppView = 'workspace' | 'new' | 'knowledge' | 'capabilities' | 'report';
const VIEWS = [
  { id: 'workspace', label: '工作台', icon: FolderClock },
  { id: 'new', label: '新建任务', icon: Plus },
  { id: 'knowledge', label: '经验库', icon: BookOpen },
  { id: 'capabilities', label: '能力库', icon: Boxes },
] as const;

export function TraceAppShell({ view, onNavigate, children }: { view: TraceAppView; onNavigate: (view: TraceAppView) => void; children: ReactNode }) {
  const sidebar = useAppSidebar('trace-agent');
  return <div className="trace-app" data-view={view}><div className="trace-app__layout" data-sidebar-collapsed={sidebar.collapsed}>
    <aside className="trace-app__sidebar"><div className="trace-app__sidebar-content">
      <AppSidebarToggle collapsed={sidebar.collapsed} controlsId={sidebar.controlsId} label="Trace Agent 导航" onToggle={() => sidebar.setCollapsed(!sidebar.collapsed)} toggleRef={sidebar.toggleRef} />
      <div className="trace-app__navigation" {...sidebar.contentProps}><div className="trace-app__identity"><strong>Trace Agent</strong><span>从经历到改进</span></div><nav aria-label="Trace Agent 应用导航">{VIEWS.map(({ id, label, icon: Icon }) => <button aria-current={view === id ? 'page' : undefined} key={id} onClick={() => onNavigate(id)} type="button"><Icon aria-hidden="true" size={17} />{label}</button>)}</nav><p className="trace-app__sidebar-note">对话保留来源，候选留下验证，版本由你选择。</p></div>
    </div></aside>
    <div className="trace-app__content">{view === 'report' ? <button className="trace-app__back" onClick={() => onNavigate('workspace')} type="button">← 返回工作台</button> : null}{children}</div>
  </div></div>;
}

interface PatternSummary { patternId: string; revision: number; title: string; summary: string; status: string; componentRef: string; evidenceCount: number }
interface KnowledgeLibrary {
  ok: boolean;
  projects: { projectId: string; title: string }[];
  projectId: string;
  patterns: PatternSummary[];
  truncated: boolean;
  pattern?: { patternId: string; revision: number; content: Record<string, unknown> };
}
interface CapabilityLibrary {
  ok: boolean;
  items: { id: string; name: string; kind: 'skill' | 'tool' | 'prompt' | 'workflow'; status: 'installed' | 'candidate'; version: string; summary: string }[];
  unavailable: string[];
}

export function TraceKnowledgeLibrary() {
  const transport = useControlTransport();
  const active = usePawOsAppActive() ?? true;
  const [projectId, setProjectId] = useState('');
  const [draftQuery, setDraftQuery] = useState('');
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<PatternSummary | null>(null);
  const library = useQuery({
    enabled: active,
    queryKey: ['trace-agent', 'optimization-library', projectId, query],
    queryFn: ({ signal }) => transport.request<KnowledgeLibrary>({ pathId: 'observability.traceOptimization.library', query: { ...(projectId ? { projectId } : {}), ...(query ? { query } : {}) }, signal }),
    retry: false,
  });
  const detail = useQuery({
    queryKey: ['trace-agent', 'optimization-pattern', projectId || library.data?.projectId, selected?.patternId, selected?.revision],
    enabled: active && Boolean(selected && library.data),
    queryFn: ({ signal }) => transport.request<KnowledgeLibrary>({ pathId: 'observability.traceOptimization.library', query: { projectId: projectId || library.data?.projectId || '', patternId: selected!.patternId, revision: selected!.revision }, signal }),
    retry: false,
  });
  return <section aria-labelledby="trace-library-heading" className="trace-app__page">
    <header className="trace-app__page-heading"><div><h1 id="trace-library-heading">经验库</h1><p>查看项目中有来源的观察、有效方法和被拒绝的尝试。</p></div><Button loading={library.isFetching} onClick={() => void library.refetch()} size="small">刷新</Button></header>
    <form className="trace-app__library-filters" onSubmit={(event) => { event.preventDefault(); setSelected(null); setQuery(draftQuery.trim()); }}>
      <label>项目<select onChange={(event) => { setProjectId(event.target.value); setSelected(null); }} value={projectId || library.data?.projectId || ''}><option value="">选择项目</option>{(library.data?.projects ?? []).map((project) => <option key={project.projectId} value={project.projectId}>{project.title}</option>)}</select></label>
      <label>查找经验<input onChange={(event) => setDraftQuery(event.target.value)} placeholder="组件、症状或方法" type="search" value={draftQuery} /></label><Button leadingIcon={<Search size={15} />} type="submit">查找</Button>
    </form>
    {library.isPending ? <LibraryLoading label="正在读取项目经验" /> : library.error ? <LibraryError error={library.error} onRetry={() => void library.refetch()} /> : <div className="trace-app__library-layout">
      <div>{library.data?.patterns?.length ? <ul className="trace-app__library-list">{library.data.patterns.map((pattern) => <li key={pattern.patternId}><button aria-pressed={selected?.patternId === pattern.patternId} onClick={() => setSelected(pattern)} type="button"><strong>{pattern.title}</strong><p>{pattern.summary}</p><span>{knowledgeStatus(pattern.status)} · {pattern.evidenceCount} 条来源 · 版本 {pattern.revision}</span></button></li>)}</ul> : <LibraryEmpty title={query ? '没有找到匹配经验' : '此项目还没有经验记录'} detail={query ? '换一个组件名或清除查找条件。' : '从工作对话创建沉淀任务，验证后把适用方法与来源保留下来。'} />}{library.data?.truncated ? <p className="trace-app__bounded-note">当前仅显示有界结果，选择项目或查找词可缩小范围。</p> : null}</div>
      {selected ? <article className="trace-app__pattern-detail"><header><h2>{selected.title}</h2><span>冻结版本 {selected.revision}</span></header>{detail.isPending ? <LibraryLoading label="正在读取此版本" /> : detail.error ? <LibraryError error={detail.error} onRetry={() => void detail.refetch()} /> : detail.data?.pattern ? <PatternContent content={detail.data.pattern.content} /> : <p>这个版本没有可读取的正文；保留来源引用，不补写经历。</p>}</article> : null}
    </div>}
  </section>;
}

export function TraceCapabilityLibrary() {
  const transport = useControlTransport();
  const active = usePawOsAppActive() ?? true;
  const [kind, setKind] = useState('all');
  const [status, setStatus] = useState('all');
  const library = useQuery({ enabled: active, queryKey: ['trace-agent', 'optimization-capabilities'], queryFn: ({ signal }) => transport.request<CapabilityLibrary>({ pathId: 'observability.traceOptimization.capabilities', signal }), retry: false });
  const items = (library.data?.items ?? []).filter((item) => (kind === 'all' || item.kind === kind) && (status === 'all' || item.status === status));
  return <section aria-labelledby="trace-capabilities-heading" className="trace-app__page">
    <header className="trace-app__page-heading"><div><h1 id="trace-capabilities-heading">能力库</h1><p>先检查现有能力，再决定更新或创建。候选与已安装版本分开列出。</p></div><Button loading={library.isFetching} onClick={() => void library.refetch()} size="small">刷新</Button></header>
    <div className="trace-app__library-filters"><label>能力类型<select onChange={(event) => setKind(event.target.value)} value={kind}><option value="all">全部</option><option value="skill">Skill</option><option value="tool">工具</option><option value="prompt">提示词</option><option value="workflow">流程</option></select></label><label>版本状态<select onChange={(event) => setStatus(event.target.value)} value={status}><option value="all">全部</option><option value="installed">已安装</option><option value="candidate">候选草稿</option></select></label></div>
    {library.isPending ? <LibraryLoading label="正在读取能力目录" /> : library.error ? <LibraryError error={library.error} onRetry={() => void library.refetch()} /> : <>{items.length ? <ul className="trace-app__capabilities">{items.map((item) => <li key={`${item.id}:${item.version}`}><div><h2>{item.name}</h2><p>{item.summary || '当前目录没有提供能力说明。'}</p><details><summary>能力身份</summary><dl><div><dt>ID</dt><dd>{item.id}</dd></div><div><dt>版本</dt><dd>{item.version || '未记录'}</dd></div></dl></details></div><span>{capabilityKind(item.kind)} · {item.status === 'installed' ? '已安装' : '候选草稿'}</span></li>)}</ul> : <LibraryEmpty title="没有符合条件的能力" detail="调整类型或状态查看已有目录。候选验证与安装操作位于对应任务报告。" />}{library.data?.unavailable?.length ? <div className="trace-app__bounded-note"><strong>部分目录暂不可用</strong><ul>{library.data.unavailable.map((source) => <li key={source}>{source}</li>)}</ul></div> : null}</>}
  </section>;
}

function PatternContent({ content }: { content: Record<string, unknown> }) {
  const scope = objectValue(content.scope);
  return <div className="trace-app__pattern-content">
    {typeof content.summary === 'string' ? <p>{content.summary}</p> : null}
    <section><h3>适用范围</h3><dl>{[['组件', scope.componentRef], ['版本', scope.componentVersion], ['输入类别', scope.inputClass]].map(([label, value]) => <div key={String(label)}><dt>{String(label)}</dt><dd>{typeof value === 'string' && value ? value : '未记录'}</dd></div>)}</dl></section>
    {([['observations', '观察事实'], ['hypotheses', '解释与假设'], ['counterevidence', '反证']] as const).map(([key, label]) => <section key={key}><h3>{label}</h3>{objectRows(content[key]).length ? objectRows(content[key]).map((item, index) => <article className="trace-app__pattern-observation" key={index}><p>{typeof item.statement === 'string' ? item.statement : '未记录内容'}</p>{typeof item.uncertainty === 'string' ? <p className="trace-app__bounded-note">仍不确定：{item.uncertainty}</p> : null}{Array.isArray(item.evidenceIds) && item.evidenceIds.length ? <details><summary>来源证据</summary><StructuredValue value={item.evidenceIds} /></details> : null}</article>) : <p>本版本没有记录{label}。</p>}</section>)}
    {objectRows(content.interventions).length ? <section><h3>已尝试的改法与结果</h3>{objectRows(content.interventions).map((item, index) => <article className="trace-app__pattern-observation" key={index}><strong>{({ supports: '支持这个方法', contradicts: '与这个方法相矛盾', rejected: '已拒绝的尝试', inconclusive: '结果尚不明确' } as Record<string, string>)[String(item.relationship)] ?? '尝试结果'}</strong>{typeof item.note === 'string' ? <p>{item.note}</p> : null}<details><summary>对应结果记录</summary><StructuredValue value={item.outcomeId} /></details></article>)}</section> : null}
    {typeof content.correctionReason === 'string' && content.correctionReason ? <section><h3>为什么修订</h3><p>{content.correctionReason}</p></section> : null}
    <details><summary>完整来源与版本信息</summary><StructuredValue value={Object.fromEntries(Object.entries(content).filter(([key]) => !['summary', 'observations', 'hypotheses', 'counterevidence', 'interventions', 'scope', 'correctionReason'].includes(key)))} /></details>
  </div>;
}
function StructuredValue({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <p>未记录</p>;
  if (Array.isArray(value)) return value.length ? <ul>{value.map((item, index) => <li key={index}><StructuredValue value={item} /></li>)}</ul> : <p>无记录</p>;
  if (typeof value === 'object') return <dl>{Object.entries(value).map(([key, item]) => <div key={key}><dt>{key}</dt><dd><StructuredValue value={item} /></dd></div>)}</dl>;
  return <p>{String(value)}</p>;
}
function LibraryLoading({ label }: { label: string }) { return <div aria-live="polite" className="trace-app__loading"><p>{label}</p><div /><div /><div /></div>; }
function LibraryError({ error, onRetry }: { error: unknown; onRetry: () => void }) { return <div className="trace-app__error" role="alert"><p>{publicErrorText(error, '目录读取失败，请重试。')}</p><Button onClick={onRetry} size="small">重试</Button></div>; }
function LibraryEmpty({ title, detail }: { title: string; detail: string }) { return <div className="trace-app__empty"><h2>{title}</h2><p>{detail}</p></div>; }
function knowledgeStatus(status: string): string { return ({ observed: '观察记录', supported: '有证据支持', contested: '存在争议', superseded: '已被替代' } as Record<string, string>)[status] ?? '状态未记录'; }
function capabilityKind(kind: string): string { return ({ skill: 'Skill', tool: '工具', prompt: '提示词', workflow: '流程' } as Record<string, string>)[kind] ?? kind; }
function objectValue(value: unknown): Record<string, unknown> { return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}; }
function objectRows(value: unknown): Record<string, unknown>[] { return Array.isArray(value) ? value.map(objectValue) : []; }
