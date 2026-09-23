import { useId, useMemo, useState } from 'react';
import { ArrowRight, ExternalLink } from 'lucide-react';
import { buildRoomFocusMesh, type RoomFocusMeshRelationState } from './room-focus-mesh';
import { roomFocusStateLabel, type RoomFocusProjection } from './room-focus-projection';

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
  const [selected, setSelected] = useState<string>();
  const [relationId, setRelationId] = useState<string>();
  const partner = focus.partners.find((item) => item.participantId === selected) ?? focus.partners[0];
  const relations = [...mesh.edges, ...mesh.nonDagRelations];
  const relation = relations.find((item) => item.id === relationId);
  const name = (id: string) => mesh.nodes.find((node) => node.id === id)?.label ?? '未知伙伴';
  const tasks = focus.workItems.filter((item) => item.ownerParticipantId === partner?.participantId || item.offeredToParticipantId === partner?.participantId);
  const routes = mesh.edges.filter((edge, index, edges) => edges.findIndex((other) => other.sourceId === edge.sourceId && other.targetId === edge.targetId) === index);
  const height = Math.max(300, mesh.height * 2 + 40);
  return <div className="paw-assignment">
    <div className="paw-assignment__summary"><strong>{focus.goal.title}</strong><span>{focus.partners.length} 位伙伴 · {focus.workItems.length} 项任务 · {mesh.edges.length} 条已确认关系</span></div>
    <div className="paw-assignment__layout">
      <div className="paw-assignment__network">
        <p className="paw-assignment__hint">沿箭头查看分派与交接；点击伙伴查看具体分工。</p>
        <div className="paw-assignment__viewport" tabIndex={0} aria-label="任务关系图，可滚动">
          <div className="paw-assignment__canvas" style={{ height }}>
            <svg viewBox={`0 0 680 ${height}`} width="680" height={height} aria-hidden="true">
              <defs><marker id={arrowId} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" /></marker></defs>
              {routes.map((edge) => {
                const numbers = mesh.edges.flatMap((item, index) => item.sourceId === edge.sourceId && item.targetId === edge.targetId ? [index + 1] : []).join('·');
                const selectedRoute = mesh.edges.some((item) => item.id === relationId && item.sourceId === edge.sourceId && item.targetId === edge.targetId);
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
                return <g key={edge.id} data-selected={selectedRoute}>
                  <path d={path} fill="none" markerEnd={`url(#${arrowId})`} />
                  <rect x={labelX - (numbers.length * 4 + 8)} y={middle - 11} width={numbers.length * 8 + 16} height="22" rx="11" />
                  <text x={labelX} y={middle + 4} textAnchor="middle">{numbers}</text>
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
        {mesh.nonDagRelations.length > 0 && <details className="paw-assignment__pending"><summary>其他关系 / 待确认 · {mesh.nonDagRelations.length}</summary>{mesh.nonDagRelations.map((item) => <button key={item.id} type="button" onClick={() => setRelationId(item.id)}>{name(item.sourceId)} → {name(item.targetId)} · {item.label}<small>{item.reason}</small></button>)}</details>}
      </div>
      <aside className="paw-assignment__detail" aria-label="分工详情" aria-live="polite">
        {relation ? <><span className="paw-assignment__eyebrow">关系详情 · {relation.label}</span><h3>{name(relation.sourceId)} → {name(relation.targetId)}</h3><p className="paw-assignment__eyebrow">{relationStateLabel(relation.state)}</p><p>{relation.summary || '暂无公开说明'}</p>{'reason' in relation && <p>{String(relation.reason)}</p>}<h4>实际记录 · {relation.attempts.length}</h4>{relation.attempts.map((attempt) => <p key={attempt.id}>{relationStateLabel(attempt.state)} · {attempt.summary || relation.label}</p>)}<details><summary>关联任务与证据</summary>{relation.provenance.workItemIds.map((id) => <p key={id}>{focus.workItems.find((item) => item.id === id)?.objective ?? id}</p>)}{relation.provenance.refs.map((ref) => <p key={ref}>{ref}</p>)}{!relation.provenance.workItemIds.length && !relation.provenance.refs.length && <p>此记录没有附加任务或证据。</p>}</details></> : partner ? <>
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
