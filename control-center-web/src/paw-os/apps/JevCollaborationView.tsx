import { Component, type KeyboardEvent, type ReactNode } from 'react';
import {
  COLLABORATION_NODE_HEIGHT as NODE_H, COLLABORATION_NODE_WIDTH as NODE_W,
  collaborationNeighborhood, collaborationPurpose, collaborationRunLabel,
  isCurrentCollaborationRun, layoutJevCollaboration, shortCollaborationText,
  type CollaborationModel, type CollaborationNode, type CollaborationPerson, type CollaborationPosition,
} from './jev-collaboration-model';
import './jev-collaboration.css';

export type CollaborationViewKind = 'map' | 'team' | 'handoffs';
export interface JevCollaborationViewProps {
  model: CollaborationModel;
  historical?: boolean;
  active?: boolean;
  motionAllowed?: boolean;
  freshKeys?: ReadonlySet<string>;
  renderAvatar: (person: CollaborationPerson, size: number, running: boolean) => ReactNode;
  onInspectTask: (node: CollaborationNode) => void;
  onOpenParticipant?: (id: string) => void;
}
interface ViewState {
  view: CollaborationViewKind; selected: string; query: string;
  focusOnly: boolean; context: boolean; zoom: number | null; width: number; height: number;
  filter: 'all' | 'active' | 'attention'; visible: number;
}
const VIEW_LABELS: Record<CollaborationViewKind, string> = { map: '任务关系', team: '伙伴分工', handoffs: '交接与复核' };
const ATTENTION = new Set(['failed', 'unknown', 'returned']);
const CURRENT = new Set(['running', 'planning', 'verifying', 'synthesizing', 'submitted', 'review']);
const EMPTY_SET: ReadonlySet<string> = new Set();

/** Small original line icons, local to this view. No icon/font/network dependency. */
export function CollaborationIcon({ name, size = 16 }: { name: string; size?: number }) {
  const paths: Record<string, string> = {
    map: 'M5 4h5v5H5z M14 15h5v5h-5z M5 15h5v5H5z M7.5 9v6 M10 6.5h6.5V15',
    team: 'M8 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6 M2 20v-2a6 6 0 0 1 12 0v2 M16 5a3 3 0 0 1 0 6 M17 14a5 5 0 0 1 5 5v1',
    arrow: 'M5 12h14 M14 7l5 5-5 5', chevron: 'M9 5l7 7-7 7',
    expand: 'M9 3H3v6 M15 3h6v6 M3 15v6h6 M21 15v6h-6',
    search: 'M10.5 17a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13 M16 16l5 5',
    plus: 'M12 5v14 M5 12h14', minus: 'M5 12h14',
    check: 'M5 12l4 4L19 6', clock: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18 M12 7v5l3 2',
    shield: 'M12 2l8 4v6c0 5-8 10-8 10S4 17 4 12V6z M8 12l3 3 5-6',
    handoffs: 'M3 8h16 M15 4l4 4-4 4 M21 16H5 M9 12l-4 4 4 4',
    alert: 'M12 3L2 21h20z M12 9v5 M12 17v.2',
    close: 'M6 6l12 12 M18 6L6 18',
    file: 'M6 3h8l5 5v13H6z M14 3v6h5 M9 13h7 M9 17h7',
    focus: 'M8 3H3v5 M16 3h5v5 M21 16v5h-5 M8 21H3v-5 M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8',
    open: 'M14 3h7v7 M21 3L11 13 M10 5H3v16h16v-7',
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name] || paths.file} /></svg>;
}
function Status({ node, quiet = false }: { node: CollaborationNode; quiet?: boolean }) {
  return <span className="jcv-status" data-tone={node.tone} data-quiet={quiet || undefined}><span className="jcv-status__mark" aria-hidden="true">{node.stage === 'done' ? <CollaborationIcon name="check" size={11} /> : ATTENTION.has(node.stage) ? '!' : null}</span><span className="jcv-status__text" title={node.label}>{node.label}</span></span>;
}
function effectCaption(node: CollaborationNode): string {
  const verify = node.runs.filter(run => run.purpose === 'verify' && isCurrentCollaborationRun(run));
  if (verify.length) return `复核 · ${verify.map(run => run.person?.name || '伙伴待同步').join('、')}`;
  const execute = node.runs.filter(run => run.purpose === 'execute' && isCurrentCollaborationRun(run));
  if (execute.length) return `执行 · ${execute.map(run => run.person?.name || '伙伴待同步').join('、')}`;
  return node.waitingOn.length ? `等待 ${node.waitingOn.length} 项前置` : node.plan ? '确认方案后执行' : node.stage === 'done' ? '结果与依据已保留' : '等待下一条回执';
}
function isRunning(node: CollaborationNode): boolean {
  return node.runs.some(run => run.state === 'running') && !['done', 'failed', 'cancelled', 'superseded'].includes(node.stage);
}

/**
 * UI-only state: selected task, viewport, tabs and filters. Receipts always come
 * from props. Class form also lets the offline preview render this exact view.
 */
export class JevCollaborationView extends Component<JevCollaborationViewProps, ViewState> {
  state: ViewState = { view: 'map', selected: '', query: '', focusOnly: false, context: true, zoom: null, width: 0, height: 0, filter: 'all', visible: 40 };
  private root: HTMLDivElement | null = null;
  private viewport: HTMLDivElement | null = null;
  private observer?: ResizeObserver;
  private nodes = new Map<string, HTMLButtonElement>();
  private scroll: Partial<Record<CollaborationViewKind, { top: number; left: number }>> = {};
  private restoreFrame = 0;
  private detailHeading: HTMLElement | null = null;
  private selectionTrigger: HTMLElement | null = null;
  private detailAction: HTMLButtonElement | null = null;
  private returnToDetail = false;
  private uid = `jcv-${++collaborationInstance}`;
  componentDidMount() {
    if (typeof ResizeObserver !== 'undefined') {
      this.observer = new ResizeObserver(this.measure);
      if (this.root) this.observer.observe(this.root);
    }
    window.addEventListener('resize', this.measure);
    this.measure();
  }
  componentDidUpdate(prev: JevCollaborationViewProps, previousState: ViewState) {
    if (this.state.width > 0 && this.state.width < 720 && this.state.selected !== previousState.selected) {
      if (this.state.selected) this.detailHeading?.focus({ preventScroll: true });
      else if (previousState.selected) {
        const trigger = this.selectionTrigger?.isConnected ? this.selectionTrigger : this.nodes.get(previousState.selected);
        trigger?.focus({ preventScroll: true });
      }
    }
    if (prev.model.graphId !== this.props.model.graphId || prev.model.planned !== this.props.model.planned) {
      this.scroll = {};
      this.setState({ selected: '', focusOnly: false, query: '', zoom: null, filter: 'all', visible: 40 });
    } else if (this.state.selected && !this.props.model.nodes.some(node => node.id === this.state.selected)) {
      this.setState({ selected: '', focusOnly: false });
    }
    if (!prev.active && this.props.active && this.returnToDetail) {
      this.returnToDetail = false; this.detailAction?.focus({ preventScroll: true });
    }
    this.measure();
  }
  componentWillUnmount() {
    this.observer?.disconnect();
    window.removeEventListener('resize', this.measure);
    cancelAnimationFrame(this.restoreFrame);
  }
  private measure = () => {
    const width = this.root?.clientWidth ?? 0;
    const height = this.viewport?.clientHeight ?? 0;
    if (width > 0 && (width !== this.state.width || height !== this.state.height)) this.setState({ width, height });
  };
  private changeView = (view: CollaborationViewKind) => {
    if (this.viewport) this.scroll[this.state.view] = { top: this.viewport.scrollTop, left: this.viewport.scrollLeft };
    this.setState({ view }, () => {
      cancelAnimationFrame(this.restoreFrame);
      this.restoreFrame = requestAnimationFrame(() => {
        if (!this.viewport) return;
        const last = this.scroll[view];
        this.viewport.scrollTop = last?.top ?? 0;
        this.viewport.scrollLeft = last?.left ?? 0;
      });
    });
  };
  private select = (id: string) => {
    if (!this.state.selected && document.activeElement instanceof HTMLElement) this.selectionTrigger = document.activeElement;
    this.setState({ selected: this.state.selected === id ? '' : id });
  };
  private matching = (node: CollaborationNode) => {
    const query = this.state.query.trim().toLocaleLowerCase();
    if (query && ![node.objective, node.owner?.name, node.expected, ...node.runs.map(run => run.person?.name)].filter(Boolean).join(' ').toLocaleLowerCase().includes(query)) return false;
    return this.state.filter === 'all' || (this.state.filter === 'attention' ? ATTENTION.has(node.stage) : CURRENT.has(node.stage));
  };
  private animate = () => Boolean(this.props.active && this.props.motionAllowed && !this.props.historical && !this.props.model.stopped && !this.props.model.final);
  private keyboardNode = (event: KeyboardEvent<HTMLButtonElement>, current: CollaborationPosition, positions: CollaborationPosition[]) => {
    if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return;
    event.preventDefault();
    const horizontal = event.key === 'ArrowLeft' || event.key === 'ArrowRight';
    const positive = event.key === 'ArrowRight' || event.key === 'ArrowDown';
    const candidates = positions.filter(position => position.id !== current.id && (horizontal ? (positive ? position.x > current.x : position.x < current.x) : (positive ? position.y > current.y : position.y < current.y)));
    candidates.sort((a, b) => Math.hypot(a.x - current.x, a.y - current.y) - Math.hypot(b.x - current.x, b.y - current.y));
    if (candidates[0]) this.nodes.get(candidates[0].id)?.focus({ preventScroll: false });
  };
  private renderNode = (node: CollaborationNode, selected: boolean, options: { position?: CollaborationPosition; positions?: CollaborationPosition[]; dimmed?: boolean } = {}) => {
    const fresh = this.animate() && (this.props.freshKeys ?? EMPTY_SET).has(`${node.id}:${node.revision}:${node.stage}`);
    return <button type="button" key={node.id} ref={element => { if (element) this.nodes.set(node.id, element); else this.nodes.delete(node.id); }}
      className={`jcv-node${options.position ? ' jcv-node--positioned' : ''}`}
      style={options.position ? { left: options.position.x, top: options.position.y, width: NODE_W, height: NODE_H } : undefined}
      data-tone={node.tone} data-unresolved={options.position?.unresolved || undefined} data-selected={selected || undefined} data-dimmed={options.dimmed || undefined}
      data-running={this.animate() && isRunning(node) || undefined} data-fresh={fresh || undefined}
      aria-pressed={selected} aria-label={`任务 ${node.index + 1}：${node.objective}，${node.label}。查看关系与详情`}
      onClick={() => this.select(node.id)} onKeyDown={event => options.position && this.keyboardNode(event, options.position, options.positions ?? [])}>
      <span className="jcv-node__top"><span className="jcv-node__number">{String(node.index + 1).padStart(2, '0')}</span><Status node={node} /></span>
      <strong className="jcv-node__title" title={node.objective}>{node.title}</strong>
      <span className="jcv-node__owner">{node.owner ? this.props.renderAvatar(node.owner, 24, false) : <CollaborationIcon name="team" size={20} />}<span>{node.owner?.name || '负责人待分配'}</span>
        {node.runs.some(run => run.purpose === 'verify' && isCurrentCollaborationRun(run)) ? <span className="jcv-node__handoff" title={effectCaption(node)}><CollaborationIcon name="arrow" size={12} /><CollaborationIcon name="shield" size={12} />{node.runs.filter(run => run.purpose === 'verify' && isCurrentCollaborationRun(run)).map(run => run.person?.name || '待同步').join('、')}</span>
          : <small>{node.waitingOn.length ? `等 ${node.waitingOn.length} 项前置` : node.stage === 'done' ? `${node.refs.length} 条引用` : `r${node.revision}`}</small>}
      </span>
    </button>;
  };
  private renderMap = (compact: boolean) => {
    const { model } = this.props;
    const nearby = collaborationNeighborhood(model, this.state.selected);
    const selected = this.state.selected;
    if (compact || model.nodes.length > 60) {
      const shown = model.nodes.filter(node => this.matching(node) && (!this.state.focusOnly || !selected || nearby.has(node.id)));
      return <div className="jcv-mobile-map" ref={element => { this.viewport = element; }}>
        <div className="jcv-view-note"><CollaborationIcon name="map" /><span>{model.nodes.length > 60 ? '任务较多，使用完整关系列表。' : '窄窗口使用关系列表，保留字号与完整入口。'}</span></div>
        {shown.slice(0, this.state.visible).map(node => <div key={node.id} className="jcv-mobile-map__item">
          {this.renderNode(node, node.id === selected)}
          <div className="jcv-mobile-map__relations">{this.relationLinks(node.id, 'in')}</div>
        </div>)}
        {!shown.length ? this.emptyFilter() : null}
        {shown.length > this.state.visible ? <button className="jcv-button" type="button" onClick={() => this.setState({ visible: this.state.visible + 40 })}>再显示 40 项（{this.state.visible}/{shown.length}）</button> : null}
      </div>;
    }
    const layout = layoutJevCollaboration(model);
    const byId = new Map(model.nodes.map(node => [node.id, node]));
    const positions = new Map(layout.positions.map(position => [position.id, position]));
    const available = Math.max(250, this.state.width - (selected && this.state.width >= 720 ? 308 : 0) - 48);
    const fit = Math.min(1, available / layout.width, this.state.height > 0 ? (this.state.height - 24) / layout.height : 1);
    const zoom = this.state.zoom ?? Math.max(.72, fit);
    const visiblePositions = layout.positions.filter(position => !this.state.focusOnly || !selected || nearby.has(position.id));
    const rendered = new Set(visiblePositions.map(position => position.id));
    return <div className="jcv-map-shell">
      <div className="jcv-map-controls"><span>连线表示依赖，不代表消息正在传输</span><div>
        <button type="button" title="缩小" aria-label="缩小任务图" disabled={zoom <= .55} onClick={() => this.setState({ zoom: Math.max(.55, zoom - .1) })}><CollaborationIcon name="minus" /></button>
        <button className="jcv-map-zoom" type="button" title="适应视口" onClick={() => this.setState({ zoom: null })}>{Math.round(zoom * 100)}%</button>
        <button type="button" title="放大" aria-label="放大任务图" disabled={zoom >= 1.5} onClick={() => this.setState({ zoom: Math.min(1.5, zoom + .1) })}><CollaborationIcon name="plus" /></button>
        <button type="button" title="适应视口" aria-label="适应任务图视口" onClick={() => this.setState({ zoom: null })}><CollaborationIcon name="expand" /></button>
      </div></div>
      {layout.unresolved.length ? <p className="jcv-notice" role="status"><CollaborationIcon name="alert" />有 {layout.unresolved.length} 项无法拓扑排序，可能包含循环或受其影响。保留节点，不伪造执行顺序。</p> : null}
      <div className="jcv-map-scroll" ref={element => { this.viewport = element; }} tabIndex={0} role="region" aria-label="任务依赖图，可横向滚动">
        <div style={{ width: layout.width * zoom, height: layout.height * zoom, position: 'relative', marginInline: 'auto' }}>
          <div className="jcv-map-canvas" style={{ width: layout.width, height: layout.height, transform: `scale(${zoom})`, transformOrigin: 'top left' }}>
            {Array.from({ length: layout.levels }, (_, index) => <span className="jcv-depth-label" key={index} style={{ left: 24 + index * (NODE_W + 64), top: 20 }}>{layout.unresolved.length && index === layout.levels - 1 ? '关系待核实' : index === 0 ? '起始任务' : `依赖层 ${String(index).padStart(2, '0')}`} </span>)}
            <svg className="jcv-edges" width={layout.width} height={layout.height} aria-hidden="true">
              <defs><marker id={`${this.uid}-arrow`} markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M1 1l5 2.5-5 2.5" fill="none" stroke="context-stroke" strokeWidth="1.2" /></marker></defs>
              {model.edges.filter(edge => rendered.has(edge.from) && rendered.has(edge.to) && (!edge.context || this.state.context)).map(edge => {
                const from = positions.get(edge.from)!; const to = positions.get(edge.to)!;
                const x1 = from.x + NODE_W, y1 = from.y + NODE_H / 2, x2 = to.x, y2 = to.y + NODE_H / 2;
                const dx = Math.max(32, Math.abs(x2 - x1) * .5);
                const d = from.depth === to.depth
                  ? `M${from.x},${y1}C${from.x - 26},${y1} ${to.x - 26},${y2} ${to.x - 3},${y2}`
                  : x2 >= x1 ? `M${x1},${y1}C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2 - 3},${y2}`
                  : `M${from.x + NODE_W / 2},${from.y}C${from.x + NODE_W / 2},${Math.min(from.y, to.y) - 32} ${to.x + NODE_W / 2},${Math.min(from.y, to.y) - 32} ${to.x + NODE_W / 2},${to.y - 4}`;
                return <path key={edge.id} d={d} className="jcv-edge" data-context={edge.context || undefined}
                  data-related={selected && nearby.has(edge.from) && nearby.has(edge.to) || undefined}
                  data-satisfied={!model.planned && byId.get(edge.from)?.stage === 'done' || undefined}
                  markerEnd={`url(#${this.uid}-arrow)`} />;
              })}
            </svg>
            {visiblePositions.map(position => { const node = byId.get(position.id)!; return this.renderNode(node, node.id === selected, {
              position, positions: visiblePositions, dimmed: !this.matching(node) || Boolean(selected && !nearby.has(node.id)),
            }); })}
          </div>
        </div>
      </div>
      <div className="jcv-map-legend"><span><i />依赖前置</span><span><i className="context" />参考上下文</span><span><i className="satisfied" />前置已验收</span><small>同一层不代表整批等待</small></div>
    </div>;
  };
  private relationLinks = (id: string, direction: 'in' | 'out') => {
    const relations = this.props.model.edges.filter(edge => (direction === 'in' ? edge.to : edge.from) === id && (this.state.context || !edge.context));
    return relations.length ? relations.map(edge => {
      const targetId = direction === 'in' ? edge.from : edge.to;
      const target = this.props.model.nodes.find(node => node.id === targetId);
      const text = edge.context ? '参考' : direction === 'in' ? '前置' : '后续';
      return target ? <button key={edge.id} type="button" className="jcv-relation" data-context={edge.context || undefined} onClick={() => this.select(target.id)}>
        <span>{text}</span><strong>{target.title}</strong><small>{target.stage === 'done' ? '已验收' : target.label}</small><CollaborationIcon name="chevron" size={12} />
      </button> : <div key={edge.id} className="jcv-relation jcv-relation--missing"><span>{text}</span><code>{targetId}</code><small>不在本视图</small></div>;
    }) : <p className="jcv-muted">{direction === 'in' ? '没有记录图内前置关系。是否可派发仍由 Jev 决定。' : '没有记录后续依赖。'}</p>;
  };
  private renderTeam = () => {
    const { model } = this.props;
    const knownOwners = new Set(model.people.map(person => person.id));
    const orphaned = model.nodes.filter(node => !node.owner || !knownOwners.has(node.owner.id));
    return <div className="jcv-team-list" ref={element => { this.viewport = element; }}>
      <p className="jcv-view-note">按伙伴查看本轮职责。执行者、当前负责人和复核者分别标明，不替换彼此。</p>
      {model.people.map(person => {
        const jobs = model.nodes.filter(node => this.matching(node) && (node.owner?.id === person.id || node.runs.some(run => run.person?.id === person.id && isCurrentCollaborationRun(run))));
        const running = model.runs.filter(run => run.person?.id === person.id && run.state === 'running');
        const topRuns = model.runs.filter(run => run.person?.id === person.id && isCurrentCollaborationRun(run));
        const knownTaskIds = new Set(model.nodes.map(node => node.id));
        const rootRuns = topRuns.filter(run => !knownTaskIds.has(run.taskId));
        if ((this.state.query || this.state.filter !== 'all') && !jobs.length && !rootRuns.length) return null;
        return <section className="jcv-person" key={person.id} data-working={this.animate() && running.length > 0 || undefined}>
          <header>{this.props.renderAvatar(person, 42, this.animate() && running.length > 0)}<div><strong>{person.name}</strong><small>{!person.available ? '当前名单中已停用' : this.props.historical || !this.props.active || model.stopped || model.final ? '所选快照记录' : running.length ? `${running.length} 个执行回执进行中` : topRuns.length ? '等待执行回执' : '本轮暂无正在执行的派发'}</small></div>
            {this.props.onOpenParticipant && person.available ? <button className="jcv-icon-button" type="button" aria-label={`打开 ${person.name} Session`} onClick={() => this.props.onOpenParticipant?.(person.id)}><CollaborationIcon name="open" /></button> : null}</header>
          <div className="jcv-person__jobs">{rootRuns.map(run => <div className="jcv-person__root" key={run.id}><CollaborationIcon name="focus" /><span><strong>{collaborationPurpose(run.purpose)} · 主目标</strong><small>{collaborationRunLabel(run.state)}{run.model ? ` · ${run.model}` : ''}</small></span></div>)}
            {jobs.map(node => {
              const mine = node.runs.filter(run => run.person?.id === person.id && isCurrentCollaborationRun(run));
              const latest = mine.find(run => run.latest)?.latest;
              return <button className="jcv-person__job" type="button" key={node.id} onClick={() => this.select(node.id)} data-selected={this.state.selected === node.id || undefined}>
                <span className="jcv-node__number">{String(node.index + 1).padStart(2, '0')}</span><span><strong>{node.title}</strong><small>{mine.length ? [...new Set(mine.map(run => collaborationPurpose(run.purpose)))].join(' / ') : '任务负责人'}{mine.find(run => run.model)?.model ? ` · ${mine.find(run => run.model)!.model}` : ''}</small>{latest ? <span className="jcv-job-action">{shortCollaborationText(latest, 88)}</span> : null}</span><Status node={node} /><CollaborationIcon name="chevron" size={14} />
              </button>;
            })}{!jobs.length && !rootRuns.length ? <p className="jcv-person__empty">没有该伙伴负责的当前任务，不补充虚构分工。</p> : null}</div>
        </section>;
      })}
      {orphaned.length ? <section className="jcv-person"><header><CollaborationIcon name="team" /><strong>负责人待同步 / 待分配</strong></header>{orphaned.filter(this.matching).map(node => this.renderNode(node, node.id === this.state.selected))}</section> : null}
    </div>;
  };
  private renderHandoffs = () => {
    const items = this.props.model.nodes.filter(this.matching);
    return <div className="jcv-handoffs" ref={element => { this.viewport = element; }}>
      <p className="jcv-view-note">依据当前任务修订与派发回执展示交接。没有时间戳时不绘制耗时轴，执行结束不等于验收通过。</p>
      {items.slice(0, this.state.visible).map(node => {
        const executes = node.runs.filter(run => run.purpose === 'execute' && run.admitted);
        const verifies = node.runs.filter(run => run.purpose === 'verify' && run.admitted);
        const producers = [...new Map(executes.filter(run => run.person).map(run => [run.person!.id, run.person!])).values()];
        const reviewers = [...new Map(verifies.filter(run => run.person).map(run => [run.person!.id, run.person!])).values()];
        const personList = (people: CollaborationPerson[], empty: string) => people.length ? <div className="jcv-handoff__people">{people.map(person => <span key={person.id}>{this.props.renderAvatar(person, 26, false)}{person.name}</span>)}</div> : <span className="jcv-muted">{empty}</span>;
        return <section className="jcv-handoff" key={node.id} data-selected={node.id === this.state.selected || undefined}>
          <button className="jcv-handoff__title" type="button" onClick={() => this.select(node.id)}><span className="jcv-node__number">{String(node.index + 1).padStart(2, '0')}</span><strong>{node.title}</strong><small>修订 {node.revision}</small><CollaborationIcon name="chevron" size={14} /></button>
          <div className="jcv-handoff__track">
            <div><small>执行派发</small>{personList(producers, node.plan ? '方案尚未执行' : '未提供执行者回执')}<span>{executes.length ? executes.map(run => collaborationRunLabel(run.state)).join(' · ') : '不以负责人代替派发记录'}</span></div>
            <CollaborationIcon name="arrow" size={20} />
            <div><small>复核派发</small>{personList(reviewers, node.stage === 'done' ? '未提供复核者回执' : '等待复核派发')}<span>{verifies.length ? verifies.map(run => collaborationRunLabel(run.state)).join(' · ') : node.stage === 'done' ? '不推断独立复核者' : '不提前指定复核伙伴'}</span></div>
            <CollaborationIcon name="arrow" size={20} />
            <div><small>任务结果</small><Status node={node} /><span>{node.refs.length ? `${node.refs.length} 条产物 / 证据引用` : node.stage === 'done' ? '当前未提供产物引用' : '等待任务结果'}</span></div>
          </div>
          {producers.some(person => reviewers.some(reviewer => reviewer.id === person.id)) ? <p className="jcv-handoff__note">回执中存在同一伙伴执行与复核；不标为独立复核。</p> : null}
        </section>;
      })}
      {!items.length ? this.emptyFilter() : null}
      {items.length > this.state.visible ? <button className="jcv-button" type="button" onClick={() => this.setState({ visible: this.state.visible + 40 })}>加载更多交接记录</button> : null}
    </div>;
  };
  private emptyFilter = () => <div className="jcv-empty"><CollaborationIcon name="search" size={28} /><strong>没有匹配的任务</strong><p>筛选不影响正在执行的工作。</p><button className="jcv-button" type="button" onClick={() => this.setState({ query: '', filter: 'all', focusOnly: false })}>显示全部</button></div>;
  private renderDetail = (node: CollaborationNode) => <aside className="jcv-detail" aria-label="选中任务的关系与摘要">
    <header ref={element => { this.detailHeading = element; }} tabIndex={-1}><span>任务 {String(node.index + 1).padStart(2, '0')} <small>· 修订 {node.revision}</small></span><button type="button" className="jcv-icon-button" aria-label="取消选中任务" onClick={() => this.setState({ selected: '', focusOnly: false })}><CollaborationIcon name="close" /></button></header>
    <div className="jcv-detail__body"><Status node={node} /><h3>{node.objective}</h3>
      {node.owner ? <div className="jcv-detail__owner">{this.props.renderAvatar(node.owner, 30, false)}<span><small>任务负责人</small><strong>{node.owner.name}</strong></span></div> : null}
      {node.runs.filter(isCurrentCollaborationRun).length ? <section><h4>当前派发</h4>{node.runs.filter(isCurrentCollaborationRun).map(run => <div className="jcv-detail__run" key={run.id}><CollaborationIcon name={run.purpose === 'verify' ? 'shield' : 'arrow'} size={14} /><span><strong>{collaborationPurpose(run.purpose)} · {run.person?.name || '伙伴待同步'}</strong><small>{collaborationRunLabel(run.state)}{run.model ? ` · ${run.model}` : ''}</small>{run.latest ? <p>{run.latest}</p> : null}</span></div>)}</section> : null}
      {node.reasons.length ? <div className="jcv-detail__why"><CollaborationIcon name="clock" size={15} /><p>{node.reasons.join('；')}</p></div> : null}
      <section><h4>前置与参考</h4>{this.relationLinks(node.id, 'in')}</section>
      <section><h4>后续依赖</h4>{this.relationLinks(node.id, 'out')}</section>
      {node.expected ? <section><h4>预期产出</h4><p>{node.expected}</p></section> : null}
      {node.acceptance.length ? <section><h4>验收要求 <small>不是已通过清单</small></h4><ul>{node.acceptance.map((criterion, index) => <li key={index}>{criterion}</li>)}</ul></section> : null}
      {node.result ? <section><h4>结果记录</h4><p>{node.result}</p></section> : null}
      {node.refs.length ? <section><h4>产物与证据</h4><ul className="jcv-detail__refs">{node.refs.map(ref => <li key={ref}><CollaborationIcon name="file" size={13} /><code>{ref}</code></li>)}</ul></section> : null}
    </div>
    <footer><button type="button" className="jcv-button jcv-button--primary" ref={element => { this.detailAction = element; }} onClick={() => { this.returnToDetail = true; this.props.onInspectTask(node); }}>完整任务与操作<CollaborationIcon name="open" size={14} /></button><small>改派、返修、文件等沿用原任务入口</small></footer>
  </aside>;
  render() {
    const { model, historical, active } = this.props;
    const selected = model.nodes.find(node => node.id === this.state.selected);
    const compact = this.state.width > 0 && this.state.width < 720;
    const matching = model.nodes.filter(this.matching).length;
    const scope = historical ? '历史任务快照' : model.stopped ? model.runs.some(run => isCurrentCollaborationRun(run)) ? '停止已记录 · 执行回执待确认' : '已停止 · 保留执行回执' : model.final ? '本轮已结束' : !active ? '保留快照 · 动态暂停' : model.planned ? '待确认方案 · 尚未执行' : '当前任务快照';
    return <div className="jcv" ref={element => { this.root = element; }} data-view={this.state.view} data-detail={Boolean(selected)} data-compact={compact || undefined} data-motion={this.animate() ? 'active' : 'paused'}>
      <div className="jcv-scope"><span><i data-live={this.animate() || undefined} />{scope}</span><span>需求修订 {model.revision}</span><span className="jcv-scope__accepted">{model.planned ? `${model.nodes.length} 项拟执行任务` : `已验收 ${model.counts.accepted} / ${model.nodes.length}`}</span></div>
      <nav className="jcv-tabs" aria-label="协作可视化视图" role="tablist" onKeyDown={event => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        const tabs: CollaborationViewKind[] = ['map', 'team', 'handoffs'];
        event.preventDefault();
        const index = event.key === 'Home' ? 0 : event.key === 'End' ? 2 : (tabs.indexOf(this.state.view) + (event.key === 'ArrowRight' ? 1 : 2)) % 3;
        this.changeView(tabs[index]!);
        event.currentTarget.querySelector<HTMLButtonElement>(`[data-tab="${tabs[index]}"]`)?.focus();
      }}>
        {(['map', 'team', 'handoffs'] as const).map(view => <button key={view} data-tab={view} type="button" role="tab" aria-selected={this.state.view === view} aria-controls={`${this.uid}-content`} id={`${this.uid}-${view}`} tabIndex={this.state.view === view ? 0 : -1} onClick={() => this.changeView(view)}><CollaborationIcon name={view} size={17} />{VIEW_LABELS[view]}</button>)}
        <span className="jcv-tabs__hint">同一份回执，三个观察角度</span>
      </nav>
      <div className="jcv-toolbar"><label className="jcv-search"><CollaborationIcon name="search" size={15} /><input aria-label="查找任务或伙伴" placeholder="查找任务、伙伴或产出" type="search" value={this.state.query} onChange={event => this.setState({ query: event.target.value })} />{this.state.query ? <button type="button" aria-label="清除任务查找" onClick={() => this.setState({ query: '' })}><CollaborationIcon name="close" size={13} /></button> : null}</label>
        <div className="jcv-filters" role="group" aria-label="协作任务筛选">{(['all', 'active', 'attention'] as const).map(filter => <button type="button" key={filter} aria-pressed={this.state.filter === filter} onClick={() => this.setState({ filter })}>{filter === 'all' ? '全部' : filter === 'active' ? '进行与复核' : '需要关注'}{filter === 'attention' && model.counts.attention ? <span>{model.counts.attention}</span> : null}</button>)}</div>
        {this.state.view === 'map' ? <div className="jcv-map-options"><button type="button" aria-pressed={this.state.context} onClick={() => this.setState({ context: !this.state.context })}>参考连线</button><button type="button" disabled={!selected} aria-pressed={this.state.focusOnly} onClick={() => this.setState({ focusOnly: !this.state.focusOnly })}><CollaborationIcon name="focus" size={13} />关联路径</button></div> : null}
        {this.state.query || this.state.filter !== 'all' ? <span className="jcv-match-count" role="status">匹配 {matching}/{model.nodes.length}</span> : null}
      </div>
      {model.notices.length ? <details className="jcv-notices"><summary><CollaborationIcon name="alert" size={13} />关系与数据说明 {model.notices.length}</summary>{model.notices.map(note => <p key={note}>{note}</p>)}</details> : null}
      <div className="jcv-body">
        <div className="jcv-content" id={`${this.uid}-content`} role="tabpanel" aria-labelledby={`${this.uid}-${this.state.view}`}>
          {!model.nodes.length ? <div className="jcv-empty"><CollaborationIcon name="map" size={32} /><strong>{model.planned ? '方案尚未包含任务' : '等待可展示的任务'}</strong><p>规划、普通对话和简单问答不会凭空生成一张任务图。</p>{model.runs.filter(isCurrentCollaborationRun).map(run => <p key={run.id}>{run.person?.name ?? '伙伴待同步'} · {collaborationPurpose(run.purpose)} · {collaborationRunLabel(run.state)}</p>)}</div>
            : this.state.view === 'map' ? this.renderMap(compact) : this.state.view === 'team' ? this.renderTeam() : this.renderHandoffs()}
        </div>
        {selected ? this.renderDetail(selected) : null}
      </div>
      <footer className="jcv-footer"><span><CollaborationIcon name="shield" size={13} />状态来自任务与执行回执</span><span>选择节点查看依赖和验收依据</span></footer>
    </div>;
  }
}
let collaborationInstance = 0;
