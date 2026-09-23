import { useId, useMemo, useState } from 'react';
import { ArrowRight, ExternalLink } from 'lucide-react';
import { buildRoomFocusMesh, type RoomFocusMeshRelationState } from './room-focus-mesh';
import { roomFocusStateLabel, type RoomFocusProjection, type RoomFocusWorkItem } from './room-focus-projection';

function relationStateLabel(state: RoomFocusMeshRelationState): string {
  const labels: Partial<Record<RoomFocusMeshRelationState, string>> = { offered: '待接收', dispatched: '已分派', sent: '已发送', delivered: '已送达', received: '已接收', replied: '已回复', accepted: '已接受', confirmed: '已确认', cancelled: '已取消' };
  return labels[state] ?? roomFocusStateLabel(state as Parameters<typeof roomFocusStateLabel>[0]);
}

/** A read-only projection of Room assignments. Opening a node never dispatches work. */
export function PawRoomAssignmentMap({ focus, onOpenParticipant }: {
  focus: RoomFocusProjection;
  onOpenParticipant: (id: string) => void;
}) {
  const mesh = useMemo(() => buildRoomFocusMesh(focus), [focus]);
  const arrowId = useId().replaceAll(':', '');
  const [view, setView] = useState<'tasks' | 'relations'>('tasks');
  const [taskId, setTaskId] = useState<string>();
  const [selected, setSelected] = useState<string>();
  const [relationId, setRelationId] = useState<string>();
  const partner = focus.partners.find((item) => item.participantId === selected) ?? focus.partners[0];
  const relations = [...mesh.edges, ...mesh.nonDagRelations];
  const relation = relations.find((item) => item.id === relationId);
  const selectedTask = focus.workItems.find((item) => item.id === taskId)
    ?? focus.workItems.find((item) => ['blocked', 'failed', 'running', 'review'].includes(item.state))
    ?? focus.workItems[0];
  const partnerName = (id?: string) => focus.partners.find((item) => item.participantId === id)?.celestialName ?? '未指定';
  const name = (id: string) => mesh.nodes.find((node) => node.id === id)?.label ?? '未知伙伴';
  const tasks = focus.workItems.filter((item) => item.ownerParticipantId === partner?.participantId || item.offeredToParticipantId === partner?.participantId);
  const routes = relations.filter((edge, index, edges) => edges.findIndex((other) => other.sourceId === edge.sourceId && other.targetId === edge.targetId) === index);
  const height = Math.max(300, mesh.height * 2 + 40);
  return <div className="paw-assignment">
    <div className="paw-assignment__summary"><strong>{focus.goal.title}</strong><span>{focus.partners.length} 位伙伴 · {focus.workItems.length} 项任务 · {focus.workItems.filter((item) => item.state === 'completed').length} 项已完成</span></div>
    <div className="paw-assignment__views" role="group" aria-label="关系视图">
      <button type="button" aria-pressed={view === 'tasks'} onClick={() => setView('tasks')}>任务分派 <span>{focus.workItems.length}</span></button>
      <button type="button" aria-pressed={view === 'relations'} onClick={() => setView('relations')}>协作往来 <span>{relations.length}</span></button>
    </div>
    <div className="paw-assignment__layout">
      <div className="paw-assignment__network">
        {view === 'tasks' ? <>
          <p className="paw-assignment__hint">谁负责、谁执行、谁复核。点击任务查看交付要求和结果。</p>
          <div className="paw-assignment__task-map" aria-label="任务分派图">
            <div className="paw-assignment__task-columns" aria-hidden="true"><span>统筹</span><span>执行任务</span><span>复核</span></div>
            {focus.workItems.map((task) => {
              const owner = task.ownerParticipantId || task.offeredToParticipantId;
              const offered = !task.ownerParticipantId && Boolean(task.offeredToParticipantId);
              return <div className="paw-assignment__task-route" key={task.id} data-state={task.state} data-pending={offered || undefined}>
                <div className="paw-assignment__actor"><span className="paw-assignment__role">统筹</span><button type="button" disabled={!task.accountableParticipantId} onClick={() => task.accountableParticipantId && onOpenParticipant(task.accountableParticipantId)}>{partnerName(task.accountableParticipantId)}</button></div>
                <span className="paw-assignment__connector" data-known={Boolean(task.accountableParticipantId && owner)}><ArrowRight size={18} aria-hidden="true" /></span>
                <button className="paw-assignment__task" type="button" aria-pressed={selectedTask?.id === task.id} onClick={() => setTaskId(task.id)}>
                  <span><strong>{owner ? partnerName(owner) : '等待分派'}</strong><small data-state={task.state}>{offered ? '待接收' : roomFocusStateLabel(task.state)}</small></span>
                  <span className="paw-assignment__task-title">{task.objective}</span>
                  {task.blocker && <span className="paw-assignment__blocker">阻塞：{task.blocker.reason}</span>}
                  {task.parentId && <small>上级任务：{focus.workItems.find((item) => item.id === task.parentId)?.objective ?? '等待同步'}</small>}
                  {task.wave && <small>并行组 · {task.wave.phaseName || task.wave.parallelSize + ' 项任务'}</small>}
                </button>
                <span className="paw-assignment__connector" data-known={Boolean(task.verifierParticipantId && owner)}><ArrowRight size={18} aria-hidden="true" /></span>
                <div className="paw-assignment__actor"><span className="paw-assignment__role">复核</span>{task.verifierParticipantId ? <button type="button" onClick={() => onOpenParticipant(task.verifierParticipantId!)}>{partnerName(task.verifierParticipantId)}</button> : <span>{task.reviewRequired ? '待指定' : '未要求'}</span>}</div>
              </div>;
            })}
            {!focus.workItems.length && <p className="paw-assignment__hint">暂未记录任务分派。可切换“协作往来”查看已经发生的消息与交接。</p>}
          </div>
          <p className="paw-assignment__hint">连线表示任务中的责任关系；实时交接记录见“协作往来”。</p>
        </> : <>
        <p className="paw-assignment__hint">实线：已确认 · 虚线：待确认或未完成 · 红色：失败或停止</p>
        <div className="paw-assignment__viewport" tabIndex={0} aria-label="任务关系图，可滚动">
          <div className="paw-assignment__canvas" style={{ height }}>
            <svg viewBox={`0 0 680 ${height}`} width="680" height={height} aria-hidden="true">
              <defs><marker id={arrowId} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" /></marker></defs>
              {routes.map((edge) => {
                const sameRoute = relations.filter((item) => item.sourceId === edge.sourceId && item.targetId === edge.targetId);
                const selectedRoute = sameRoute.some((item) => item.id === relationId);
                const displayedEdge = sameRoute.find((item) => item.id === relationId) ?? edge;
                const label = [...new Set(sameRoute.map((item) => item.label))].slice(0, 2).join(' · ');
                const pending = 'reason' in displayedEdge;
                const failed = ['failed', 'stopped', 'cancelled'].includes(displayedEdge.state);
                const source = mesh.nodes.find((node) => node.id === edge.sourceId)!;
                const target = mesh.nodes.find((node) => node.id === edge.targetId)!;
                const down = target.y >= source.y;
                const sameRow = source.y === target.y;
                const direction = target.x > source.x ? 1 : -1;
                const x1 = source.x * 6.8 + (sameRow ? 96 * direction : 0);
                const x2 = target.x * 6.8 - (sameRow ? 100 * direction : 0);
                const y1 = source.y * 2 + 24 + (sameRow ? 0 : down ? 48 : -48);
                const y2 = target.y * 2 + 24 + (sameRow ? 0 : down ? -52 : 52);
                const middle = (y1 + y2) / 2;
                const skipsRow = Math.abs(target.y - source.y) > 70;
                const laneX = 28;
                const path = skipsRow
                  ? `M ${source.x * 6.8 - 96} ${source.y * 2 + 24} C ${laneX} ${source.y * 2 + 24}, ${laneX} ${target.y * 2 + 24}, ${target.x * 6.8 - 100} ${target.y * 2 + 24}`
                  : `M ${x1} ${y1} C ${x1} ${middle}, ${x2} ${middle}, ${x2} ${y2}`;
                const labelX = skipsRow ? laneX + 30 : (x1 + x2) / 2;
                return <g key={edge.id} data-selected={selectedRoute} data-pending={pending} data-failed={failed}>
                  <path d={path} fill="none" markerEnd={`url(#${arrowId})`} />
                  <rect x={labelX - (label.length * 6 + 8)} y={middle - 11} width={label.length * 12 + 16} height="22" rx="11" />
                  <text x={labelX} y={middle + 4} textAnchor="middle">{label}</text>
                </g>;
              })}
            </svg>
            {mesh.nodes.map((node) => <button key={node.id} type="button" className="paw-assignment__node" aria-pressed={partner?.participantId === node.refId} style={{ left: node.x * 6.8, top: node.y * 2 + 24 }} onClick={() => { setSelected(node.refId); setRelationId(undefined); }}>
              <span><strong>{node.label}</strong><small data-state={node.state}>{roomFocusStateLabel(node.state)}</small></span>
              <span className="paw-assignment__responsibility">{node.responsibility === 'participant_activity' ? '查看任务与执行记录' : node.responsibility || node.sublabel}</span>
            </button>)}
            {!mesh.nodes.length && <p>伙伴加入后，将显示真实分工与关系。</p>}
          </div>
        </div>
        <div className="paw-assignment__relations" aria-label="已确认协作关系">
          {mesh.edges.map((edge, index) => <button type="button" aria-pressed={edge.id === relationId} key={edge.id} onClick={() => setRelationId(edge.id)}><b>{index + 1}</b><span>{name(edge.sourceId)} <ArrowRight size={13} aria-hidden="true" /> {name(edge.targetId)}</span><strong>{edge.label}</strong></button>)}
          {!mesh.edges.length && <p>尚无已确认的伙伴间关系。不会根据窗口位置推测分派。</p>}
        </div>
        {mesh.nonDagRelations.length > 0 && <section className="paw-assignment__pending" aria-label="其他关系与待确认"><h4>其他关系 / 待确认 · {mesh.nonDagRelations.length}</h4>{mesh.nonDagRelations.map((item) => <button key={item.id} type="button" aria-pressed={item.id === relationId} onClick={() => setRelationId(item.id)}>{name(item.sourceId)} → {name(item.targetId)} · {item.label} · {relationStateLabel(item.state)}<small>{item.reason}</small></button>)}</section>}
        </>}
      </div>
      <aside className="paw-assignment__detail" aria-label="分工详情" aria-live="polite">
        {view === 'tasks' ? selectedTask ? <TaskDetail task={selectedTask} partnerName={partnerName} onOpenParticipant={onOpenParticipant} /> : <p>任务建立后，在这里查看负责人、验收要求和结果。</p> : relation ? <><h3>{name(relation.sourceId)} → {name(relation.targetId)}</h3><p className="paw-assignment__eyebrow">{relation.label} · {relationStateLabel(relation.state)}</p><p>{relation.summary || '暂无公开说明'}</p>{'reason' in relation && <p>{String(relation.reason)}</p>}<h4>实际记录 · {relation.attempts.length}</h4>{relation.attempts.map((attempt) => <p key={attempt.id}>{relationStateLabel(attempt.state)} · {attempt.summary || relation.label}</p>)}<details><summary>关联任务与证据</summary>{relation.provenance.workItemIds.map((id) => <p key={id}>{focus.workItems.find((item) => item.id === id)?.objective ?? id}</p>)}{relation.provenance.refs.map((ref) => <p key={ref}>{ref}</p>)}{!relation.provenance.workItemIds.length && !relation.provenance.refs.length && <p>此记录没有附加任务或证据。</p>}</details></> : partner ? <>
          <span className="paw-assignment__eyebrow">{partner.collaborationRole === 'coordinator' ? '统筹与汇总' : '任务负责人'}</span><h3>{partner.celestialName} <small>{roomFocusStateLabel(partner.state)}</small></h3><p>{partner.currentAction === 'participant_activity' ? '最近有执行记录，打开会话查看详情' : partner.currentAction}</p>
          <button type="button" className="paw-assignment__open" onClick={() => onOpenParticipant(partner.participantId)}>查看实际会话与调用 <ExternalLink size={14} aria-hidden="true" /></button>
          <h4>负责的任务 · {tasks.length}</h4>
          {tasks.map((task) => <article key={task.id}><span className="paw-assignment__eyebrow">{task.ownerParticipantId === partner.participantId ? roomFocusStateLabel(task.state) : '待接收'}</span><h4>{task.objective.length > 150 ? `${task.objective.slice(0, 150)}…` : task.objective}</h4>{task.objective.length > 150 && <details><summary>查看完整任务</summary><p>{task.objective}</p></details>}{task.verifierParticipantId && <p>复核：{focus.partners.find((item) => item.participantId === task.verifierParticipantId)?.celestialName ?? '负责人待同步'}</p>}{task.expectedOutput && <p>交付：{task.expectedOutput}</p>}{task.acceptanceCriteria.length > 0 && <><strong>验收要求</strong><ul>{task.acceptanceCriteria.map((item, index) => <li key={index}>{item}</li>)}</ul></>}{task.blocker && <p>阻塞：{task.blocker.reason}</p>}{task.evidence.map((evidence) => <p key={`${evidence.kind}:${evidence.ref}`}>证据：{evidence.ref}</p>)}</article>)}
          {!tasks.length && <p className="paw-assignment__hint">尚无归属该伙伴的任务记录。可打开会话查看实际调用。</p>}
        </> : <p>等待伙伴加入。</p>}
      </aside>
    </div>
  </div>;
}

function TaskDetail({ task, partnerName, onOpenParticipant }: {
  task: RoomFocusWorkItem;
  partnerName: (id?: string) => string;
  onOpenParticipant: (id: string) => void;
}) {
  const owner = task.ownerParticipantId || task.offeredToParticipantId;
  return <>
    <h3>{owner ? partnerName(owner) : '等待分派'} <small>{roomFocusStateLabel(task.state)}</small></h3>
    <p>{task.objective.length > 150 ? `${task.objective.slice(0, 150)}…` : task.objective}</p>
    {task.objective.length > 150 && <details><summary>查看完整任务</summary><p>{task.objective}</p></details>}
    <dl className="paw-assignment__ownership"><div><dt>统筹</dt><dd>{partnerName(task.accountableParticipantId)}</dd></div><div><dt>{task.ownerParticipantId ? '执行' : '待接收'}</dt><dd>{owner ? partnerName(owner) : '未分派'}</dd></div><div><dt>复核</dt><dd>{task.verifierParticipantId ? partnerName(task.verifierParticipantId) : task.reviewRequired ? '待指定' : '未要求'}</dd></div></dl>
    {owner && <button type="button" className="paw-assignment__open" onClick={() => onOpenParticipant(owner)}>查看实际会话与调用 <ExternalLink size={14} aria-hidden="true" /></button>}
    {task.blocker && <p className="paw-assignment__blocker">阻塞：{task.blocker.reason}</p>}
    {task.expectedOutput && <><h4>交付要求</h4><p>{task.expectedOutput}</p></>}
    {task.acceptanceCriteria.length > 0 && <><h4>验收要求</h4><ul>{task.acceptanceCriteria.map((item, index) => <li key={index}>{item}</li>)}</ul></>}
    {task.latestResult && <details className="paw-assignment__result"><summary>查看已提交结果</summary><p>{task.latestResult}</p></details>}
    {task.evidence.length > 0 && <details className="paw-assignment__result"><summary>查看证据 · {task.evidence.length}</summary>{task.evidence.map((item) => <p key={`${item.kind}:${item.ref}`}>{item.ref}</p>)}</details>}
  </>;
}
