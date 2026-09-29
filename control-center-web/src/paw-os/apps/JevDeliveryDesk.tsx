import { Component, type ReactNode } from 'react';
import { ArrowLeft, ArrowUpRight, Clipboard, FileText, FolderOpen, LayoutGrid, List, Pin, Search, X } from 'lucide-react';
import { writeClipboardText } from '@/platform/clipboard';
import {
  DELIVERY_KINDS, deliveryExcerpt, deliveryKind, deliveryReferenceList, deliverySuffix, filterDeliveries,
  type DeliveryDeskState, type DeliveryKind, type JevDeliveryFile,
} from './jev-delivery-desk-model';
import './jev-delivery-desk.css';

export interface JevDeliveryDeskProps {
  files: readonly JevDeliveryFile[]; state: DeliveryDeskState;
  onState: (change: Partial<DeliveryDeskState>) => void;
  onInspect: (file: JevDeliveryFile) => void;
  onOpen?: (file: JevDeliveryFile) => void;
  stageLabel: (file: JevDeliveryFile) => string;
  renderOwner?: (file: JevDeliveryFile) => ReactNode;
  freshKeys?: ReadonlySet<string>; active?: boolean; motion?: boolean;
  historical?: boolean; paused?: boolean;
  attachments?: readonly { mediaId: string; fileName: string }[];
}

/** Display state belongs to the calling Room, so closing this view never loses a pin.
 * Files and task results always remain owned by the original graph projection.
 */
export class JevDeliveryDesk extends Component<JevDeliveryDeskProps, { copy: 'idle' | 'pending' | 'done' | 'error' }> {
  state: { copy: 'idle' | 'pending' | 'done' | 'error' } = { copy: 'idle' };
  private root: HTMLElement | null = null;
  private frame = 0;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private generation = 0;
  private live = true;
  componentDidMount() { this.live = true; }
  componentWillUnmount() {
    this.live = false; this.generation += 1; clearTimeout(this.timer); cancelAnimationFrame(this.frame);
  }
  componentDidUpdate(previous: JevDeliveryDeskProps) {
    const selectedChanged = previous.state.selectedKey !== this.props.state.selectedKey;
    if (selectedChanged || previous.state.scope !== this.props.state.scope) {
      this.generation += 1; clearTimeout(this.timer);
      if (this.state.copy !== 'idle') this.setState({ copy: 'idle' });
    }
    if ((!previous.active && this.props.active) || selectedChanged) {
      cancelAnimationFrame(this.frame);
      this.frame = requestAnimationFrame(() => {
        if (!this.live || this.props.active === false) return;
        const selected = this.props.files.find(file => file.key === this.props.state.selectedKey);
        if (selected) this.root?.querySelector<HTMLElement>('.jdd-detail h3')?.focus({ preventScroll: true });
        else if (previous.state.selectedKey) {
          const card = Array.from(this.root?.querySelectorAll<HTMLButtonElement>('[data-delivery-select]') ?? [])
            .find(node => node.dataset.deliverySelect === previous.state.selectedKey);
          (card ?? this.root?.querySelector<HTMLButtonElement>('.jdd-filter button'))?.focus({ preventScroll: true });
        }
      });
    }
  }
  private copy = async (files: readonly JevDeliveryFile[]) => {
    const text = deliveryReferenceList(files); if (!text) return;
    const generation = ++this.generation; clearTimeout(this.timer); this.setState({ copy: 'pending' });
    try {
      await writeClipboardText(text);
      if (this.live && generation === this.generation) this.setState({ copy: 'done' });
    } catch {
      if (this.live && generation === this.generation) this.setState({ copy: 'error' });
    }
    if (this.live && generation === this.generation) this.timer = setTimeout(() => this.live && this.setState({ copy: 'idle' }), 2400);
  };
  private select = (key: string) => this.props.onState({ selectedKey: key });
  private pin = (key: string) => {
    const pins = new Set(this.props.state.pins);
    if (pins.has(key)) pins.delete(key); else pins.add(key);
    this.props.onState({ pins: [...pins] });
  };
  render() {
    const p = this.props, s = p.state;
    const filtered = filterDeliveries(p.files, s, Boolean(p.onOpen));
    const shown = filtered.slice(0, s.limit); const selected = p.files.find(file => file.key === s.selectedKey);
    const pinned = p.files.filter(file => s.pins.includes(file.key));
    const tasks = new Set(p.files.map(file => file.task.id));
    const kinds = DELIVERY_KINDS.filter(kind => p.files.some(file => deliveryKind(file.name) === kind.id));
    const activeKind = DELIVERY_KINDS.find(kind => kind.id === s.kind);
    return <section className="jdd" data-detail={Boolean(selected)} data-layout={s.layout} data-motion={p.motion !== false && p.active !== false}
      ref={node => { this.root = node; }} aria-label="成果桌">
      <header className="jdd-head"><div><span className="jdd-eyebrow">{p.historical ? '保留的交付记录' : '本轮成果'}</span><h2>把结果，放到眼前<span>.</span></h2>
        <p>{p.files.length} 项文件记录<span> / </span>{tasks.size} 项来源任务</p></div>
        <span className="jdd-head-art" aria-hidden="true"><i/><i/><span><FolderOpen size={28}/></span></span>
      </header>
      {p.historical || p.paused ? <p className="jdd-history" role="status">{p.historical ? '正在查看历史记录，不代表当前执行。' : '当前静态展示已有记录；任务是否仍在运行，以执行回执为准。'}</p> : null}
      <div className="jdd-controls"><label className="jdd-search"><Search size={16} aria-hidden/><input aria-label="搜索成果文件" placeholder="找文件、伙伴，或来源任务…" value={s.query}
        onChange={event => p.onState({ query: event.currentTarget.value, limit: 36 })}/>{s.query ? <button type="button" aria-label="清空成果搜索" onClick={() => p.onState({ query: '', limit: 36 })}><X size={14}/></button> : null}</label>
        <div className="jdd-layout" role="group" aria-label="成果展示方式"><button type="button" aria-label="卡片浏览" aria-pressed={s.layout === 'gallery'} onClick={() => p.onState({ layout: 'gallery' })}><LayoutGrid size={16}/></button><button type="button" aria-label="列表浏览" aria-pressed={s.layout === 'list'} onClick={() => p.onState({ layout: 'list' })}><List size={16}/></button></div>
      </div>
      <div className="jdd-filter" role="group" aria-label="成果类型"><button type="button" aria-pressed={s.kind === 'all'} onClick={() => p.onState({ kind: 'all', limit: 36 })}>全部 <small>{p.files.length}</small></button>
        {kinds.map(kind => <button type="button" key={kind.id} aria-pressed={s.kind === kind.id} onClick={() => p.onState({ kind: kind.id, limit: 36 })}>{kind.label}<small>{p.files.filter(file => deliveryKind(file.name) === kind.id).length}</small></button>)}
      </div>
      <div className="jdd-body"><div className="jdd-collection">
        <div className="jdd-collection-head"><div role="group" aria-label="成果范围">{([
          ['all', '本轮'], ['pinned', `重点 ${pinned.length}`], ['openable', '可打开'],
        ] as const).map(([filter, label]) => <button key={filter} type="button" aria-pressed={s.filter === filter} onClick={() => p.onState({ filter, limit: 36 })}>{filter === 'pinned' ? <Pin size={12} aria-hidden/> : null}{label}</button>)}</div>
          <small>{filtered.length} 项{s.filter === 'pinned' ? ' · 仅本窗口整理' : ''}</small>
        </div>
        <div className="jdd-cards" role="list" aria-label={activeKind ? `${activeKind.label}成果` : '文件成果'}>
          {shown.map((file, index) => { const kind = deliveryKind(file.name), isPinned = s.pins.includes(file.key); return <article role="listitem" key={file.key} className="jdd-card" data-kind={kind} data-selected={selected?.key === file.key} data-fresh={p.freshKeys?.has(file.key) || undefined}>
            <button type="button" data-delivery-select={file.key} className="jdd-card-main" aria-pressed={selected?.key === file.key} aria-label={`查看成果 ${file.name}`} onClick={() => this.select(file.key)}>
              <DeliveryCover file={file} kind={kind} number={index + 1}/>
              <span className="jdd-card-text"><strong title={file.name}>{file.name}</strong><span>{deliveryExcerpt(file.task.objective, 60)}</span>
                <small>{p.renderOwner?.(file)}{file.ownerName || '负责人未提供'}<i>·</i>{p.stageLabel(file)}</small>
              </span><ArrowUpRight className="jdd-card-arrow" size={16} aria-hidden/>
            </button>
            <button type="button" className="jdd-pin" aria-label={`${isPinned ? '取消重点' : '设为重点'} ${file.name}`} aria-pressed={isPinned} title={isPinned ? '取消重点' : '留在本窗口的重点区'} onClick={() => this.pin(file.key)}><Pin size={14}/></button>
          </article>; })}
        </div>
        {!filtered.length ? <div className="jdd-empty" role="status"><FolderOpen size={32} aria-hidden/><h3>{!p.files.length ? '还没有文件记录' : s.filter === 'pinned' ? '把重要的留在这里' : '没有匹配的成果'}</h3>
          <p>{!p.files.length ? '实际文件回执出现后再展示，不把预期交付当成已经完成。' : s.filter === 'pinned' ? '点文件上的图钉。只整理本窗口，不改文件、不写入记忆。' : '可以按文件名、任务目标或伙伴查找。'}</p>
          {p.files.length ? <button type="button" onClick={() => p.onState({ kind: 'all', filter: 'all', query: '', limit: 36 })}>查看本轮全部</button> : null}</div> : null}
        {filtered.length > s.limit ? <button type="button" className="jdd-load" onClick={() => p.onState({ limit: s.limit + 36 })}>再显示 {Math.min(36, filtered.length - s.limit)} 项<span>已显示 {shown.length}/{filtered.length}</span></button> : null}
        {p.attachments?.length ? <details className="jdd-inputs"><summary>原始附件 · {p.attachments.length} 项<span>与交付分开</span></summary><ul>{p.attachments.map(file => <li key={file.mediaId}><FileText size={14} aria-hidden/>{file.fileName}</li>)}</ul></details> : null}
      </div>
      {selected ? <aside className="jdd-detail" aria-label={`${selected.name}的来源与结果`}>
        <header><span>成果 / 来源</span><button type="button" aria-label="返回成果集合" onClick={() => this.select('')}><ArrowLeft size={15}/><span>返回</span></button></header>
        <div className="jdd-detail-scroll"><h3 tabIndex={-1}>{selected.name}</h3><p className="jdd-detail-path">{selected.path}</p>
        {!filtered.some(file => file.key === selected.key) ? <p className="jdd-outside">当前选择不在筛选结果中。阅读保留，返回后可重新筛选。</p> : null}
        <div className="jdd-provenance"><span className="jdd-provenance-owner">{p.renderOwner?.(selected)}<span>{selected.ownerName || '负责人未提供'}<small>任务负责人</small></span></span><i aria-hidden/><span className="jdd-revision">r{selected.task.revision}<small>任务修订</small></span></div>
        <section className="jdd-detail-section"><h4>来自这项任务</h4><button type="button" className="jdd-task-link" onClick={() => p.onInspect(selected)}><span>{selected.task.objective}</span><ArrowUpRight size={15} aria-hidden/></button><span className="jdd-stage" data-stage={selected.task.state}>{p.stageLabel(selected)}</span></section>
        <section className="jdd-detail-section"><h4>记录的结果</h4><p className="jdd-result">{selected.task.result || '尚未提供结果文字。文件记录仍可查看。'}</p></section>
        {selected.task.expectedOutput ? <details className="jdd-requirements"><summary>预期产出与验收要求</summary><p>{selected.task.expectedOutput}</p>{selected.task.acceptance.length ? <ul>{selected.task.acceptance.map((item, index) => <li key={index}>{item}</li>)}</ul> : null}<small>这里是要求，不是逐项通过证明。</small></details> : null}
        <details className="jdd-reference"><summary>原始引用</summary><code>{selected.ref}</code></details>
        </div>
        <div className="jdd-detail-actions">{p.onOpen && selected.sessionId ? <button type="button" className="jdd-primary" onClick={() => p.onOpen?.(selected)}><FolderOpen size={16} aria-hidden/>打开当前文件<ArrowUpRight size={14} aria-hidden/></button> : <p>当前没有可用的文件打开入口，可查看原任务与证据。</p>}
          <button type="button" onClick={() => p.onInspect(selected)}>完整任务与证据<ArrowUpRight size={14} aria-hidden/></button>
          <button type="button" disabled={this.state.copy === 'pending'} onClick={() => void this.copy([selected])}><Clipboard size={14} aria-hidden/>复制文件引用</button>
          <small>打开的是工作区当前文件，不是交付时的冻结副本。</small>
        </div>

      </aside> : null}
      </div>
      <footer className="jdd-foot"><span><span className="jdd-foot-dot" aria-hidden/>展示已有记录，不重新执行任务</span><span role="status">{this.state.copy === 'pending' ? '正在复制…' : this.state.copy === 'done' ? '文件引用已复制' : this.state.copy === 'error' ? '未能复制，可展开原始引用手动选择。' : ''}</span>
        {!selected && shown.length ? <button type="button" disabled={this.state.copy === 'pending'} onClick={() => void this.copy(shown)}><Clipboard size={13} aria-hidden/>复制已展示的 {shown.length} 项引用</button> : null}
      </footer>
    </section>;
  }
}

/** A format cover, not a counterfeit thumbnail. No bytes are loaded from a path. */
function DeliveryCover({ file, kind, number }: { file: JevDeliveryFile; kind: DeliveryKind; number: number }) {
  const type = DELIVERY_KINDS.find(value => value.id === kind)!;
  return <span className="jdd-cover" aria-hidden="true"><span className="jdd-cover-index">{String(number).padStart(2, '0')}</span>
    <span className="jdd-cover-sheet"><span className="jdd-cover-mark">{type.mark}</span><i/><i/><i/></span>
    <span className="jdd-cover-bottom"><span>{type.label}文件</span><b>{deliverySuffix(file.name)}</b></span>
  </span>;
}

export function JevDeliveryDeskLaunch({ count, onClick }: { count: number; onClick: () => void }) {
  return <button type="button" className="jdd-launch" onClick={onClick}><span className="jdd-launch-stack" aria-hidden><i/><i/><FileText size={21}/></span><span><strong>打开成果桌<ArrowUpRight size={13} aria-hidden/></strong><small>{count} 项文件记录 · 浏览、标记与追溯</small></span></button>;
}
