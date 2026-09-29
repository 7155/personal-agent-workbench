import { Component } from 'react';
import { createRoot } from 'react-dom/client';
import { RoomPlanetAvatar } from '../../src/features/rooms/RoomPlanetAvatar';
import { roomPlanetName } from '../../src/features/rooms/room-copy';
import { JevCollaborationView, CollaborationIcon } from '../../src/paw-os/apps/JevCollaborationView';
import { buildJevCollaboration, type CollaborationNode, type CollaborationPerson } from '../../src/paw-os/apps/jev-collaboration-model';
import { collaborationScenes, createCollaborationDemo, DEMO_LABELS, type CollaborationScene } from './jev-collaboration-v4-data';
import './jev-collaboration-v4.css';

class CollaborationDemo extends Component<{}, { scene: CollaborationScene; step: number; theme: string; motion: boolean; narrow: boolean; page: 'map' | 'chat'; detail: CollaborationNode | null; notice: string; fresh: ReadonlySet<string> }> {
  state = { scene: 'mixed' as CollaborationScene, step: 0, theme: 'light', motion: true, narrow: false, page: 'map' as 'map' | 'chat', detail: null as CollaborationNode | null, notice: '', fresh: new Set<string>() as ReadonlySet<string> };
  private timer = 0;
  private reduced?: MediaQueryList;
  componentDidMount() {
    this.reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
    this.setState({ motion: !this.reduced.matches });
    this.reduced.addEventListener?.('change', this.syncMotion);
  }
  componentWillUnmount() { window.clearTimeout(this.timer); this.reduced?.removeEventListener?.('change', this.syncMotion); }
  private syncMotion = () => this.setState({ motion: !this.reduced?.matches });
  private advance = () => {
    const next = Math.min(3, this.state.step + 1);
    const fixture = createCollaborationDemo(this.state.scene, next);
    const last = createCollaborationDemo(this.state.scene, this.state.step);
    const changed = fixture.mission.filter(item => item.stage !== last.mission.find(previous => previous.task.id === item.task.id)?.stage).map(item => `${item.task.id}:${item.task.revision}:${item.stage}`);
    this.setState({ step: next, fresh: new Set(changed), notice: ['','已注入：阅读恢复验收回执','已注入：文件读取验收回执','已注入：联调任务执行回执'][next]! });
    window.clearTimeout(this.timer); this.timer = window.setTimeout(() => this.setState({ fresh: new Set(), notice: '' }), 1400);
  };
  private avatar = (person: CollaborationPerson, size: number, running: boolean) => <RoomPlanetAvatar ordinal={person.ordinal} size={size} decorative activity={running && this.state.motion ? 'working' : 'static'} />;
  render() {
    const fixture = createCollaborationDemo(this.state.scene, this.state.step);
    const model = buildJevCollaboration(fixture.graph, fixture.room, fixture.mission, DEMO_LABELS, roomPlanetName);
    const inUse = model.people.filter(person => model.nodes.some(node => node.owner?.id === person.id || node.runs.some(run => run.person?.id === person.id)));
    const active = !['disconnected', 'history'].includes(this.state.scene);
    const summary = model.planned ? '这是尚未执行的方案。任务、负责人和依赖已经列出，确认与调整仍走原方案入口。'
      : !model.nodes.length ? '当前还没有拆分出的任务。普通对话不需要为了展示而创建任务图。'
      : model.stopped ? '本轮已记录停止。已有结果保留，是否完全结束仍以每条执行回执为准。'
      : model.final ? `本轮已结束，当前记录中有 ${model.counts.accepted} 项已验收结果。成果与依据仍可回到原任务核对。`
      : `当前有 ${model.nodes.length} 项任务：${model.counts.running} 项执行中，${model.counts.reviewing} 项提交或复核中，${model.counts.accepted} 项已验收。${model.counts.attention ? '还有需关注的任务，具体原因保留在详情中。' : '后续任务等待自己的前置结果，不必等待整批任务。'}`;
    return <div className="collab-demo" data-theme={this.state.theme} data-narrow={this.state.narrow || undefined}>
      <header className="collab-demo-top"><div className="collab-demo-brand"><span><CollaborationIcon name="map" size={19} /></span><strong>PAW</strong><small>COLLABORATION / 04</small></div>
        <nav aria-label="设计样例页面"><button type="button" aria-pressed={this.state.page === 'chat'} onClick={() => this.setState({ page: 'chat' })}>对话与进展</button><button type="button" aria-pressed={this.state.page === 'map'} onClick={() => this.setState({ page: 'map' })}>协作全景</button></nav>
        <span className="collab-demo-label">交互样例 · 不连接模型</span></header>
      <main className="collab-demo-window">
        <div className="collab-demo-chrome"><span className="collab-demo-dots"><i /><i /><i /></span><span>Jev · 文件预览与阅读恢复</span><small>所见状态均为合成输入</small></div>
        <div className="collab-demo-heading"><div><span className="collab-demo-eyebrow">一个目标，分别推进</span><h1>{model.objective}</h1><p>分工、依赖与交接都回到同一份任务记录。</p></div><div className="collab-demo-people">{inUse.slice(0, 8).map(person => <span title={person.name} key={person.id}>{this.avatar(person, 34, false)}</span>)}<small>{inUse.length} 位伙伴</small></div></div>
        <div className="collab-demo-controls"><label>场景<select aria-label="切换协作样例" value={this.state.scene} onChange={event => { window.clearTimeout(this.timer); this.setState({ scene: event.target.value as CollaborationScene, step: 0, detail: null, fresh: new Set(), notice: '' }); }}>{Object.entries(collaborationScenes).map(([key, label]) => <option value={key} key={key}>{label}</option>)}</select></label>
          <button type="button" className="collab-demo-receipt" onClick={this.advance} disabled={this.state.scene !== 'mixed' || this.state.step >= 3}><CollaborationIcon name="plus" size={13} />推进一条样例回执</button>
          <div className="collab-demo-controls-right"><button type="button" aria-pressed={!this.state.motion} onClick={() => this.setState({ motion: !this.state.motion })}>减少动态</button><button type="button" aria-pressed={this.state.narrow} onClick={() => this.setState({ narrow: !this.state.narrow })}>窄窗口</button><button type="button" onClick={() => this.setState({ theme: this.state.theme === 'light' ? 'dark' : 'light' })}>{this.state.theme === 'light' ? '深色' : '浅色'}</button></div>
        </div>
        {this.state.page === 'map' ? <div className="collab-demo-main">
          <div className="collab-demo-real-view" hidden={Boolean(this.state.detail)}>
            <JevCollaborationView key={this.state.scene} model={model} active={active && !this.state.detail} historical={this.state.scene === 'history'} motionAllowed={this.state.motion} freshKeys={this.state.fresh} renderAvatar={this.avatar}
              onInspectTask={node => this.setState({ detail: node })} onOpenParticipant={id => this.setState({ notice: `${model.people.find(person => person.id === id)?.name}：完整工程中由原 Session 入口打开；本页不会连接你的会话。` })} />
          </div>
          {this.state.detail ? <section className="collab-demo-detail"><button type="button" onClick={() => this.setState({ detail: null })}>← 返回协作全景</button><span>完整任务 · 仅本地样例</span><h2>{this.state.detail.objective}</h2><p>{this.state.detail.expected}</p><h3>验收要求</h3><ul>{this.state.detail.acceptance.map((line, i) => <li key={i}>{line}</li>)}</ul><h3>原始引用</h3><pre>{this.state.detail.refs.join('\n') || '当前未提供产物引用。'}</pre><p>正式入口保留现有任务改派、返修、停止与证据能力。这里不模拟服务器操作成功。</p></section> : null}
        </div> : <div className="collab-demo-chat">
          <section className="collab-demo-conversation"><div className="collab-demo-user">恢复后重新核实文件回执，保留阅读位置和未提交内容。各项改动完成后，再做联调。</div><article><header>{this.avatar(model.people[0]!, 30, false)}<strong>Earth</strong><small>本轮协作 · 样例内容</small></header><p>{summary}</p><div className="collab-demo-inline"><CollaborationIcon name="map" size={18} /><span><strong>分工已展开，依赖关系已保留</strong><small>{model.nodes.length} 项任务 · 已验收 {model.counts.accepted} 项</small></span><button type="button" onClick={() => this.setState({ page: 'map' })}>查看协作全景 →</button></div><p>不需要逐条翻工具记录。打开全景，可以直接查看哪项结果在阻挡后续任务，以及当前由谁复核。</p><div className="collab-demo-quiet"><CollaborationIcon name="shield" size={15} />进行中不等于完成，提交不等于验收。</div></article><div className="collab-demo-composer"><span>继续补充目标，或查看伙伴的任务…</span><small>这是只读设计样例。正式输入框与队列逻辑保持不变。</small></div></section>
          <aside className="collab-demo-rail"><header><strong>本轮工作</strong><small>任务与成果</small></header><button type="button" className="collab-demo-peek" onClick={() => this.setState({ page: 'map' })}><span>{inUse.slice(0, 5).map(person => <span key={person.id}>{this.avatar(person, 28, false)}</span>)}</span><strong>协作全景<CollaborationIcon name="expand" size={14} /></strong><small>谁在执行，谁在等待，结果如何交接</small></button>{model.nodes.map(node => <button className="collab-demo-task" key={node.id} type="button" onClick={() => this.setState({ page: 'map', detail: node })}><span>{node.owner ? this.avatar(node.owner, 30, false) : null}</span><span><strong>{node.title}</strong><small>{node.owner?.name} · {node.label}</small></span><CollaborationIcon name="chevron" size={13} /></button>)}</aside>
        </div>}
      </main>
      <footer className="collab-demo-foot"><span>PAW · 一份目标，多位伙伴</span><span>实际可视化组件 / 合成任务与回执</span></footer>
      {this.state.notice ? <div className="collab-demo-toast" role="status"><CollaborationIcon name="check" size={15} />{this.state.notice}<button type="button" aria-label="关闭样例提示" onClick={() => this.setState({ notice: '' })}>×</button></div> : null}
    </div>;
  }
}
const root = document.getElementById('root');
if (root) createRoot(root).render(<CollaborationDemo />);
