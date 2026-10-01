import { ArrowUpRight, BookOpen, BrainCircuit, ChevronRight, Package, Plug, Puzzle, Search, ShieldCheck, SlidersHorizontal, Wrench, X } from 'lucide-react';
import type { ReactNode, Ref } from 'react';
import type { CapabilityPreference } from '@/features/plugins/capability-policy';
import type { CodemodeMode } from '../types';
import { filterCapabilityRows, type CapabilityDisplayRow, type CapabilityFilter, type CapabilitySection } from './capability-display';
import './pi-capabilities.css';

export interface PiCapabilityBrowserProps {
  rows: readonly CapabilityDisplayRow[]; query: string; section: CapabilitySection; filter: CapabilityFilter;
  selectedKey: string; status: 'loading' | 'ready' | 'failed'; locked: boolean; pending: boolean;
  /** Omitted until the Session snapshot confirms the native Pi setting. */
  codemodeMode?: CodemodeMode; codemodeModePending?: boolean;
  titleId?: string; searchRef?: Ref<HTMLInputElement>; motion?: boolean;
  onQuery: (value: string) => void; onSection: (value: CapabilitySection) => void;
  onFilter: (value: CapabilityFilter) => void; onSelect: (key: string) => void;
  onPreference: (key: string, preference: CapabilityPreference) => void;
  onCodemodeModeChange?: (mode: CodemodeMode) => void;
  onInsert: (row: CapabilityDisplayRow) => void; onClose?: () => void; onManage?: () => void;
  mcpPanel?: ReactNode;
}
const sections: Array<{ id: CapabilitySection; label: string; icon: typeof Wrench }> = [
  { id: 'all', label: '全部', icon: SlidersHorizontal }, { id: 'tool', label: '工具', icon: Wrench },
  { id: 'skill', label: '技能', icon: BookOpen }, { id: 'extension', label: '扩展', icon: Puzzle },
  { id: 'mcp', label: 'MCP', icon: Plug },
];
const kindLabels = { tool: '工具', skill: '技能', extension: '扩展' };
export function PiCapabilityBrowser(props: PiCapabilityBrowserProps) {
  const { rows, query, section, filter, selectedKey, status, locked, pending, codemodeMode, codemodeModePending } = props;
  const visible = filterCapabilityRows(rows, section, filter, query);
  const selected = status === 'ready' ? visible.find(row => row.key === selectedKey) : undefined;
  const memory = status === 'ready' && section === 'memory' ? rows.find(row => row.id === 'memory') : undefined;
  const usable = rows.filter(row => row.kind === 'tool' && row.state === 'usable').length;
  return <section className="pi-capabilities" data-status={status} data-detail={Boolean(selected)} data-motion={props.motion !== false} aria-label="当前对话能力">
    <header className="pi-capabilities__head">
      <span className="pi-capabilities__brand"><Puzzle size={19} aria-hidden /></span>
      <div><h2 id={props.titleId}>当前对话的能力</h2><p>{status === 'ready' ? `${usable} 个工具可用 · 技能与扩展按需使用` : status === 'failed' ? '未能读取当前对话的能力' : '正在读取当前对话的能力'}</p></div>
      {props.onClose ? <button type="button" className="pi-capabilities__icon" aria-label="关闭对话功能" onClick={props.onClose}><X size={18} /></button> : null}
    </header>
    <div className="pi-capabilities__categories" role="group" aria-label="功能类别">
      {sections.map(({ id, label, icon: Icon }) => <button key={id} type="button" aria-pressed={section === id}
        onClick={() => props.onSection(id)}><Icon size={14} aria-hidden /><span>{label}</span>{id !== 'mcp' ? <small>{id === 'all' ? rows.length : rows.filter(row => row.kind === id).length}</small> : null}</button>)}
      {rows.some(row => row.id === 'memory') ? <button type="button" aria-label="查看记忆召回设置" aria-pressed={section === 'memory'} onClick={() => props.onSection('memory')}><BrainCircuit size={14} aria-hidden /><span>记忆</span></button> : null}
    </div>
    <div className="pi-capabilities__search"><Search size={15} aria-hidden /><input ref={props.searchRef} value={query} autoComplete="off"
      placeholder="按名称、用途或来源查找" aria-label="搜索当前对话功能" onChange={event => props.onQuery(event.currentTarget.value)} />
      {query ? <button type="button" className="pi-capabilities__icon" aria-label="清空功能搜索" onClick={() => props.onQuery('')}><X size={14} /></button> : null}
      <select value={filter} aria-label="筛选功能状态" onChange={event => props.onFilter(event.currentTarget.value as CapabilityFilter)}>
        <option value="all">所有状态</option><option value="usable">当前可用</option><option value="unavailable">未开启 / 不可用</option>
      </select>
    </div>
    {memory ? <div className="pi-capabilities__memory-switch">
      <div><strong>本对话记忆召回</strong><small>{memory.stateLabel} · {memory.scope}</small></div>
      <label>此对话如何使用<select aria-label="本对话记忆召回" value={memory.preference} disabled={locked || pending || !memory.configurable} onChange={event => props.onPreference(memory.key, event.currentTarget.value as CapabilityPreference)}>
        <option value="inherit">跟随默认设置</option><option value="enabled">在此对话启用</option><option value="disabled">在此对话关闭</option>
      </select></label>
      <small role="status">{pending ? '正在保存…' : locked ? '本轮结束后可调整，从下一轮生效' : '从下一轮生效；不会重新处理历史消息'}</small>
    </div> : null}
    {codemodeMode !== undefined ? <div className="pi-capabilities__codemode-switch">
      <div><strong>代码执行编排</strong><small>Pi 原生 codemode · 从下一轮生效</small></div>
      <label>调用方式<select aria-label="代码执行编排方式" value={codemodeMode}
        disabled={locked || pending || codemodeModePending || !props.onCodemodeModeChange}
        onChange={event => {
          const mode = event.currentTarget.value;
          if (mode === 'on' || mode === 'only' || mode === 'off') props.onCodemodeModeChange?.(mode);
        }}>
        <option value="on">保留直接工具</option><option value="only">仅通过代码执行</option><option value="off">关闭代码执行</option>
      </select></label>
      <small role="status">{codemodeModePending ? '正在保存…' : locked ? '本轮结束后可调整' : '只影响后续调用；历史回执保持不变'}</small>
    </div> : null}
    {locked ? <p className="pi-capabilities__notice"><ShieldCheck size={15} aria-hidden />当前暂不能调整功能；可以继续查看，现有任务不受影响。</p> : null}
    <div className="pi-capabilities__body" data-detail={Boolean(selected)}>
      {section === 'mcp' ? props.mcpPanel : status !== 'ready' ? <div className="pi-capabilities__empty" role="status"><Package size={27} aria-hidden /><strong>{status === 'failed' ? '能力目录暂不可用' : '正在读取能力目录'}</strong><p>{status === 'failed' ? '请通过原设置入口重新读取。不会用旧目录代替当前伙伴。' : '工具、技能与扩展会按当前 Session 的配置显示。'}</p></div>
        : visible.length ? <div className="pi-capabilities__list" aria-label="功能列表">
          <p className="pi-capabilities__count">{visible.length} 项{query ? '匹配' : '功能'}<span>点击查看来源与使用范围</span></p>
          {visible.map(row => <button type="button" key={row.key} className="pi-capabilities__row" data-capability-key={row.key} data-selected={selected?.key === row.key}
            aria-pressed={selected?.key === row.key} onClick={() => props.onSelect(selected?.key === row.key ? '' : row.key)}>
            <span className="pi-capabilities__type" data-kind={row.kind}>{row.id === 'memory' ? <BrainCircuit size={17} /> : row.kind === 'skill' ? <BookOpen size={17} /> : row.kind === 'extension' ? <Puzzle size={17} /> : <Wrench size={17} />}</span>
            <span className="pi-capabilities__row-main"><span className="pi-capabilities__row-top"><strong>{row.name}</strong><span className="pi-capabilities__status" data-state={row.state}>{row.stateLabel}</span></span>
              <span className="pi-capabilities__description">{row.description || '未提供用途说明'}</span><small>{kindLabels[row.kind]}<i aria-hidden>·</i>{row.source}<i aria-hidden>·</i>{row.resourceLabel}</small></span><ChevronRight size={15} aria-hidden />
          </button>)}
        </div> : <div className="pi-capabilities__empty" role="status"><Search size={25} aria-hidden /><strong>没有匹配的功能</strong><p>可以换个名称，或者查看所有功能。</p><button type="button" onClick={() => { props.onQuery(''); props.onSection('all'); props.onFilter('all'); }}>清空筛选</button></div>}
      {selected ? <aside className="pi-capabilities__detail" aria-label={`${selected.name}的功能详情`}>
        <header><small>{kindLabels[selected.kind]} / 详情</small><button type="button" className="pi-capabilities__icon" aria-label="返回功能列表" onClick={() => props.onSelect('')}><X size={16} /></button></header>
        <h3 tabIndex={-1}>{selected.name}</h3><p>{selected.description || '未提供用途说明'}</p>
        <dl><div><dt>资源</dt><dd>{selected.resourceLabel}</dd></div><div><dt>本对话</dt><dd data-state={selected.state}>{selected.stateLabel}</dd></div><div><dt>披露</dt><dd>{selected.disclosure}</dd></div><div><dt>授权</dt><dd>{selected.authorization}</dd></div><div><dt>作用范围</dt><dd>{selected.scope}</dd></div><div><dt>来源</dt><dd>{selected.source}</dd></div></dl>
        {selected.reasons.length ? <details className="pi-capabilities__reason" open={selected.state !== 'usable'}><summary>状态原因</summary>{selected.reasons.map((reason, index) => <p key={index}>{reason}</p>)}</details> : null}
        <details className="pi-capabilities__reason"><summary>权限与标识</summary><p>{selected.risk}</p>{selected.permissions.length ? <p>{selected.permissions.join(' · ')}</p> : <p>未声明额外权限项</p>}<code>{selected.key}</code>{selected.revision ? <small>目录修订：{selected.revision}</small> : null}</details>
        <div className="pi-capabilities__setting"><label htmlFor={`${props.titleId || 'pi-capability'}-preference`}>此对话如何使用</label>
          <select id={`${props.titleId || 'pi-capability'}-preference`} value={selected.preference} disabled={locked || pending || !selected.configurable}
            onChange={event => props.onPreference(selected.key, event.currentTarget.value as CapabilityPreference)}>
            <option value="inherit">跟随默认设置</option><option value="enabled">在此对话启用</option><option value="disabled">在此对话关闭</option>
          </select><small role="status">{pending ? '设置正在提交，以返回的目录为准' : !selected.configurable ? '当前目录未提供可修改的 Session 设置' : '从下一轮生效，不改变已经发出的调用'}</small>
        </div>
        {selected.tool ? <button className="pi-capabilities__insert" type="button" disabled={locked || selected.state !== 'usable'} onClick={() => props.onInsert(selected)}><ArrowUpRight size={15} aria-hidden />加入消息</button>
          : <p className="pi-capabilities__footnote">{selected.kind === 'skill' ? '技能提供工作说明。已提供给对话，不代表本轮已加载或使用。' : '扩展由 Pi 执行；是否有网页界面由已有 App 宿主决定。'}</p>}
        {selected.tool ? <p className="pi-capabilities__footnote">只把工具加入输入框，不立即执行。</p> : null}
      </aside> : null}
    </div>
    <footer className="pi-capabilities__footer"><span>配置说明可用范围，调用回执才说明实际使用。</span>{props.onManage ? <button type="button" onClick={props.onManage}>管理功能与默认设置<ArrowUpRight size={14} aria-hidden /></button> : null}</footer>
  </section>;
}
