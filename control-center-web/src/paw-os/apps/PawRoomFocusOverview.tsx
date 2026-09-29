import { roomPublicAction } from './room-visual-progress';
import { roomWorkStatusLabel, roomPartnerStatusLabel } from './room-work-status';
import {
  ArrowRight,
  CircleHelp,
  ExternalLink,
  FileCheck2,
  FileText,
  GitBranch,
  MessageCircle,
  Route,
  RefreshCw,
  Satellite,
  Send,
  ShieldCheck,
  Waypoints,
} from 'lucide-react';
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Disclosure } from '@/components/primitives';
import { MarkdownBody } from '@/features/agent/timeline/MarkdownRenderer';
import {
  buildRoomFocusMesh,
  type RoomFocusMesh,
  type RoomFocusMeshEdge,
  type RoomFocusMeshNode,
} from './room-focus-mesh';
import {
  roomFocusHasCoordinator,
  roomFocusOriginLabel,
  roomFocusStateLabel,
  type RoomFocusPacket,
  type RoomFocusPacketKind,
  type RoomFocusPartner,
  type RoomFocusProjection,
  type RoomFocusState,
  type RoomFocusWorkItem,
} from './room-focus-projection';
import type { RoomDispatchPlan } from './room-gravity-projection';
import { roomFlowKindLabel, roomFlowStatusLabel, type RoomSatelliteSnapshot, type RoomSatelliteSnapshots } from './room-message-flow';

type FocusSelection =
  | { kind: 'work'; id: string }
  | { kind: 'partner'; id: string }
  | { kind: 'edge'; id: string };

const selectionPriority: RoomFocusState[] = [
  'blocked',
  'failed',
  'review',
  'running',
  'waiting',
  'completed',
  'stopped',
  'idle',
  'disconnected',
];

const FLOW_PACKET_WINDOW = 18;

/**
 * Sol collaboration console — the single Room 态势 surface. Mission chrome,
 * pulse, partner-only relationship graph, flow ledger and inspector all
 * project the same real Room data. The graph deliberately excludes Sol and
 * WorkItems as actors while keeping their task detail available in the
 * inspector (PF-CM-013, UR-023).
 */
export function PawRoomFocusOverview({
  focus,
  hideMission = false,
  onOpenParticipant,
  onSelectParticipant,
  selectedParticipantId,
  satellitesByParticipant = {},
  intercomStatus,
  onRefreshTraffic,
}: {
  focus: RoomFocusProjection;
  hideMission?: boolean;
  onOpenParticipant?: (participantId: string, background?: boolean) => void;
  onSelectParticipant?: (participantId: string) => void;
  selectedParticipantId?: string;
  satellitesByParticipant?: RoomSatelliteSnapshots;
  intercomStatus?: 'loading' | 'ready' | 'error';
  onRefreshTraffic?: () => void;
}) {
  const defaultSelection = useMemo(() => defaultFocusSelection(focus), [focus]);
  const mesh = useMemo(() => buildRoomFocusMesh(focus), [focus]);
  const [selection, setSelection] = useState<FocusSelection>(defaultSelection);

  useEffect(() => {
    const stillExists = selection.kind === 'work'
      ? focus.workItems.some((item) => item.id === selection.id)
      : selection.kind === 'partner'
        ? focus.partners.some((partner) => partner.participantId === selection.id)
        : mesh.edges.some((edge) => edge.id === selection.id);
    if (!stillExists) setSelection(defaultSelection);
  }, [defaultSelection, focus.partners, focus.workItems, mesh.edges, selection]);

  useEffect(() => {
    if (!selectedParticipantId || !focus.partners.some((partner) => partner.participantId === selectedParticipantId)) return;
    setSelection((current) => current.kind === 'partner' && current.id === selectedParticipantId
      ? current
      : { kind: 'partner', id: selectedParticipantId });
  }, [focus.partners, selectedParticipantId]);

  const selectedWork = selection.kind === 'work'
    ? focus.workItems.find((item) => item.id === selection.id)
    : undefined;
  const selectedPartner = selection.kind === 'partner'
    ? focus.partners.find((partner) => partner.participantId === selection.id)
    : selectedWork
      ? focus.partners.find((partner) => partner.participantId === selectedWork.ownerParticipantId)
      : undefined;
  const coordinatorActive = roomFocusHasCoordinator(focus.partners);
  /* Until a connected partner really holds the coordinator role there is no
     Sol to name: the origin every surface still has to refer to is simply the
     shared main Room. */
  const originLabel = roomFocusOriginLabel(coordinatorActive);

  return (
    <section aria-label="Sol 协作态势" className="paw-room-focus-overview" data-coordinator={coordinatorActive || undefined}>
      {!hideMission && coordinatorActive ? <header className="paw-room-focus-overview__mission">
        <span aria-hidden="true" className="paw-room-focus-overview__sol"><i /></span>
        <div>
          <small>Sol · 当前目标</small>
          <strong>{focus.goal.title}</strong>
          {focus.goal.description ? <p>{focus.goal.description}</p> : null}
        </div>
        <div aria-label={`目标状态：${roomFocusStateLabel(focus.goal.state)}`} className="paw-room-focus-overview__mission-state" data-state={focus.goal.state}>
          <i aria-hidden="true" />
          <span>{roomFocusStateLabel(focus.goal.state)}</span>
        </div>
      </header> : !hideMission ? <header className="paw-room-focus-overview__mission paw-room-focus-overview__mission--dormant">
        <div>
          <small>等待主持</small>
          <strong>{focus.goal.title}</strong>
          <p>指定一位伙伴为「主持」后，Sol 协作态势与星空才会点亮。</p>
        </div>
      </header> : null}

      <FocusPulse counts={focus.counts} />

      <div className="paw-room-focus-overview__layout">
      <FocusMeshGraph
        focus={focus}
        mesh={mesh}
        selection={selection}
        onOpenParticipant={onOpenParticipant}
        onSelectParticipant={onSelectParticipant}
        selectedParticipantId={selectedParticipantId}
        satellitesByParticipant={satellitesByParticipant}
        onSelect={setSelection}
      />

      <FocusFlowLedger
        flow={focus.flow}
        originLabel={originLabel}
        partners={focus.partners}
        rootId={focus.goal.rootId}
        workItems={focus.workItems}
        intercomStatus={intercomStatus}
        onOpenParticipant={onOpenParticipant}
        onRefreshTraffic={onRefreshTraffic}
      />

      <FocusInspector
        onOpenParticipant={onOpenParticipant}
        originLabel={originLabel}
        partner={selectedPartner}
        partners={focus.partners}
        work={selectedWork}
        satellites={selectedPartner ? satellitesByParticipant[selectedPartner.participantId] : undefined}
      />
      </div>
    </section>
  );
}

/** 任务脉搏 — the four real counters as one proportional bar plus the exact
 * numbers. Zero work renders a quiet track, never a fake segment. */
function FocusPulse({ counts }: { counts: RoomFocusProjection['counts'] }) {
  const segments = [
    ['active', counts.active, '进行'],
    ['review', counts.review, '复核'],
    ['blocked', counts.blocked, '受阻'],
    ['completed', counts.completed, '完成'],
  ] as const;
  const total = counts.active + counts.review + counts.blocked + counts.completed;
  return (
    <section aria-label="协作摘要" className="paw-room-focus-overview__pulse">
      <div aria-hidden="true" className="paw-room-focus-overview__pulse-bar" data-empty={total === 0 || undefined}>
        {segments.map(([tone, count]) => count > 0
          ? <i data-tone={tone} key={tone} style={{ flexGrow: count }} title={`${count}`} />
          : null)}
      </div>
      <dl className="paw-room-focus-overview__counts">
        {segments.map(([tone, count, label]) => (
          <div data-tone={tone} data-zero={count === 0 || undefined} key={tone}><dt>{label}</dt><dd>{count}</dd></div>
        ))}
      </dl>
    </section>
  );
}

/** Partner-only collaboration graph. Sol remains mission chrome and WorkItems
 * remain inspectable data; neither is drawn as a collaborator node. */
function FocusMeshGraph({
  focus,
  mesh,
  onSelect,
  onOpenParticipant,
  onSelectParticipant,
  selectedParticipantId,
  selection,
  satellitesByParticipant,
}: {
  focus: RoomFocusProjection;
  mesh: RoomFocusMesh;
  onOpenParticipant?: (participantId: string, background?: boolean) => void;
  onSelectParticipant?: (participantId: string) => void;
  onSelect: (selection: FocusSelection) => void;
  selectedParticipantId?: string;
  selection: FocusSelection;
  satellitesByParticipant: RoomSatelliteSnapshots;
}) {
  const nodeLabels = useMemo(() => new Map(mesh.nodes.map((node) => [node.id, node.label])), [mesh.nodes]);
  return <section aria-label="协作网" className="paw-room-focus-overview__section paw-room-focus-overview__mesh">
    <header><span><Waypoints aria-hidden="true" size={14} /><strong>协作行星</strong></span><small>{focus.partners.length} 位伙伴 · Session 卫星</small></header>
    <div aria-label="协作网状图" className="paw-room-focus-overview__mesh-canvas" role="group" data-view="roster">
      {mesh.nodes.map((node) => <FocusMeshNode
        canvasHeight={mesh.height}
        key={node.id}
        node={node}
        satellites={satellitesByParticipant[node.refId]}
        onOpenParticipant={onOpenParticipant}
        onSelectParticipant={onSelectParticipant}
        selected={selectedParticipantId ? selectedParticipantId === node.refId : selection.kind === 'partner' && selection.id === node.refId}
        onSelect={onSelect}
      />)}
      {mesh.edges.length ? <Disclosure className="paw-room-focus-overview__relations" summary={`协作关系 · ${mesh.edges.length} 条已确认`}>
        <div className="paw-room-focus-overview__relation-list">
          {mesh.edges.map((edge) => <button
            aria-label={meshEdgeAriaLabel(edge, nodeLabels)}
            aria-pressed={selection.kind === 'edge' && selection.id === edge.id}
            className="paw-room-focus-overview__mesh-edge-label"
            data-kind={edge.kind}
            key={edge.id}
            onClick={() => onSelect({ kind: 'edge', id: edge.id })}
            type="button"
          ><span>{nodeLabels.get(edge.sourceId)} → {nodeLabels.get(edge.targetId)}</span><small>{edge.label} · {relationStateLabel(edge.state)}</small></button>)}
        </div>
      </Disclosure> : null}
    </div>
    {selection.kind === 'edge' ? <MeshEdgeDetail edge={mesh.edges.find((candidate) => candidate.id === selection.id)} nodeLabels={nodeLabels} onOpenParticipant={onOpenParticipant} nodes={mesh.nodes} /> : null}
    {mesh.nonDagRelations.length ? <Disclosure aria-label="失败、等待与冲突关系" className="paw-room-focus-overview__mesh-disclosure" summary={`未确认关系 · ${mesh.nonDagRelations.length} 条传递中 / 失败 / 冲突`}>
      <ol aria-label="未建立协作关系" className="paw-room-focus-overview__mesh-disclosure-list">
        {mesh.nonDagRelations.map((relation) => <li data-kind={relation.kind} data-state={relation.state} key={relation.id}>
          <div className="paw-room-focus-overview__mesh-disclosure-route"><strong>{nodeLabels.get(relation.sourceId) ?? relation.sourceId}</strong><span aria-hidden="true">→</span><strong>{nodeLabels.get(relation.targetId) ?? relation.targetId}</strong><span>{relation.label} · {relationStateLabel(relation.state)}</span></div>
          <p className="paw-room-focus-overview__mesh-disclosure-reason">{relation.reason}</p>
          <p className="paw-room-focus-overview__mesh-disclosure-summary">{readableActivity(relation.summary, '等待下一条通信回执')}</p>
          <div className="paw-room-focus-overview__mesh-disclosure-provenance">
            {relation.provenance.eventIds.map((value) => <code key={`event:${value}`}>事件 {value}</code>)}
            {relation.provenance.workItemIds.map((value) => <code key={`work:${value}`}>任务 {value}</code>)}
            {relation.provenance.dispatchIds.map((value) => <code key={`dispatch:${value}`}>分派 {value}</code>)}
          </div>
        </li>)}
      </ol>
    </Disclosure> : null}
    {!focus.partners.length ? <p className="paw-room-focus-overview__empty">还没有行星伙伴。添加伙伴后，这里会显示各自的工作与卫星。</p> : null}
  </section>;
}

function relationStateLabel(state: string): string {
  if (state === 'sent') return '已发送';
  if (state === 'delivered') return '已送达';
  if (state === 'received') return '已接收';
  if (state === 'replied') return '已回复';
  if (state === 'offered') return '已提出';
  if (state === 'dispatched') return '已分派';
  if (state === 'accepted') return '已接受';
  if (state === 'confirmed') return '已确认';
  if (state === 'cancelled') return '已取消';
  if (state === 'rejected') return '已拒绝';
  return roomFocusStateLabel(state as RoomFocusState);
}

function meshEdgeAriaLabel(
  edge: RoomFocusMeshEdge,
  nodeLabels: ReadonlyMap<string, string>,
): string {
  return `${nodeLabels.get(edge.sourceId) ?? edge.sourceId} → ${nodeLabels.get(edge.targetId) ?? edge.targetId}，${edge.label}，${relationStateLabel(edge.state)}`;
}

function MeshEdgeDetail({
  edge,
  nodeLabels,
  nodes,
  onOpenParticipant,
}: {
  edge?: RoomFocusMeshEdge;
  nodeLabels: ReadonlyMap<string, string>;
  nodes: readonly RoomFocusMeshNode[];
  onOpenParticipant?: (participantId: string) => void;
}) {
  if (!edge) return null;
  const source = nodes.find((node) => node.id === edge.sourceId);
  const target = nodes.find((node) => node.id === edge.targetId);
  const provenance = [
    ['eventIds', edge.provenance.eventIds],
    ['workItemIds', edge.provenance.workItemIds],
    ['dispatchIds', edge.provenance.dispatchIds],
  ] as const;
  return (
    <section aria-label="协作关系详情" className="paw-room-focus-overview__mesh-detail" role="region">
      <header>
        <span><Waypoints aria-hidden="true" size={13} /><strong>协作关系</strong></span>
        <small>{edge.label} · {relationStateLabel(edge.state)}</small>
      </header>
      <p className="paw-room-focus-overview__mesh-detail-route">
        <strong>{nodeLabels.get(edge.sourceId) ?? edge.sourceId}</strong>
        {' → '}
        <strong>{nodeLabels.get(edge.targetId) ?? edge.targetId}</strong>
      </p>
      {edge.summary ? <p className="paw-room-focus-overview__mesh-detail-summary">{edge.summary}</p> : null}
      {edge.attempts.length ? (
        <div className="paw-room-focus-overview__mesh-detail-attempts">
          <div className="paw-room-focus-overview__mesh-detail-attempts-heading">
            <strong>确认尝试回执</strong>
            <span>{edge.attempts.length} 次</span>
          </div>
          <ol aria-label="确认尝试回执" className="paw-room-focus-overview__mesh-detail-attempt-list">
            {edge.attempts.map((attempt, index) => (
              <li key={attempt.id}>
                <div className="paw-room-focus-overview__mesh-detail-attempt-meta">
                  <strong>第 {index + 1} 次 · {relationStateLabel(attempt.state)}</strong>
                  <span>
                    时间 {attempt.createdAtMs}
                    {attempt.sequence === undefined ? '' : ` · 序列 ${attempt.sequence}`}
                  </span>
                </div>
                <p>{attempt.summary}</p>
                <div className="paw-room-focus-overview__mesh-detail-attempt-provenance">
                  <code>尝试 {attempt.id}</code>
                  {attempt.provenance.eventIds.map((value) => <code key={`event:${value}`}>事件 {value}</code>)}
                  {attempt.provenance.workItemIds.map((value) => <code key={`work:${value}`}>任务 {value}</code>)}
                  {attempt.provenance.dispatchIds.map((value) => <code key={`dispatch:${value}`}>分派 {value}</code>)}
                </div>
              </li>
            ))}
          </ol>
        </div>
      ) : null}
      <dl>
        <div><dt>关系类型</dt><dd>{edge.label}</dd></div>
        <div><dt>当前状态</dt><dd>{relationStateLabel(edge.state)}</dd></div>
        <div><dt>发生时间</dt><dd>{edge.createdAtMs}</dd></div>
        {provenance.map(([label, values]) => values.length ? (
          <div key={label}><dt>{label}</dt><dd>{values.map((value) => <code key={value}>{value}</code>)}</dd></div>
        ) : null)}
      </dl>
      {onOpenParticipant && source && target ? (
        <div className="paw-room-focus-overview__mesh-detail-actions">
          <button onClick={() => onOpenParticipant(source.refId)} type="button">打开 {source.label} 伙伴窗口</button>
          <button onClick={() => onOpenParticipant(target.refId)} type="button">打开 {target.label} 伙伴窗口</button>
        </div>
      ) : null}
    </section>
  );
}

function FocusMeshNode({
  canvasHeight,
  node,
  onOpenParticipant,
  onSelectParticipant,
  onSelect,
  selected,
  satellites,
}: {
  canvasHeight: number;
  node: RoomFocusMeshNode;
  onOpenParticipant?: (participantId: string, background?: boolean) => void;
  onSelectParticipant?: (participantId: string) => void;
  onSelect: (selection: FocusSelection) => void;
  selected: boolean;
  satellites?: RoomSatelliteSnapshot;
}) {
  const position = { left: `${node.x}%`, top: `${Math.round((node.y / canvasHeight) * 10000) / 100}%` };
  const stateLabel = roomFocusStateLabel(node.state);
  return (
    <button
      aria-label={`${node.label}，${node.sublabel}，职责：${node.responsibility}，${stateLabel}`}
      aria-pressed={selected}
      className="paw-room-focus-overview__mesh-node paw-room-focus-overview__mesh-node--partner"
      data-tone={node.tone}
      data-state={node.state}
      onClick={() => {
        onSelect({ kind: 'partner', id: node.refId });
        onSelectParticipant?.(node.refId);
        /* A planet is the direct doorway to its canonical Partner Session.
         * The desktop raises an existing target when present, so
         * foregrounding it never creates a second conversation identity. */
        onOpenParticipant?.(node.refId);
      }}
      style={position}
      title={`${node.label} · ${node.sublabel} · ${node.responsibility}`}
      type="button"
    >
      <i aria-hidden="true" className="paw-room-focus-overview__planet-body" style={planetTexture(node.label)} />
      <span>
        <strong>{node.label}</strong>
        <small>{node.sublabel}</small>
        <em>{readableActivity(node.responsibility, '等待新的工作项')}</em>
        <span className="paw-room-focus-overview__satellite-count" data-known={satellites?.status === 'ready' || undefined}>
          <Satellite aria-hidden="true" size={12} />
          {satellites?.status === 'ready' ? `卫星 ${satellites.satellites.length}` : satellites?.status === 'loading' ? '卫星读取中' : satellites?.status === 'error' ? '卫星暂不可用' : '卫星数量未知'}
        </span>
        {satellites?.status === 'ready' && satellites.satellites.length > 0 ? <span className="paw-room-focus-overview__satellite-breakdown">{satelliteBreakdown(satellites)}</span> : null}
      </span>
    </button>
  );
}

/** Chronological ledger of what really moved between Sol and the planets:
 * public messages, approvals, dispatches, context transfers and WorkItem
 * revisions, in real event order. */
export function FocusFlowLedger({
  flow,
  originLabel,
  partners,
  rootId,
  workItems,
  intercomStatus,
  onOpenParticipant,
  onRefreshTraffic,
}: {
  flow: RoomFocusPacket[];
  originLabel: string;
  partners: RoomFocusPartner[];
  rootId: string;
  workItems: RoomFocusWorkItem[];
  intercomStatus?: 'loading' | 'ready' | 'error';
  onOpenParticipant?: (participantId: string) => void;
  onRefreshTraffic?: () => void;
}) {
  const [selectedPacketId, setSelectedPacketId] = useState('');
  const detailRef = useRef<HTMLDivElement>(null);
  const selectPacket = (id: string) => {
    setSelectedPacketId(id);
    detailRef.current?.scrollIntoView?.({ block: 'start', behavior: 'auto' });
  };
  const [showAllPackets, setShowAllPackets] = useState(false);
  const [filterParticipant, setFilterParticipant] = useState('');
  const [category, setCategory] = useState('communication');
  useEffect(() => {
    setShowAllPackets(false);
    setSelectedPacketId('');
    setFilterParticipant('');
  }, [rootId]);
  const categoryPackets = category === 'all' ? flow : flow.filter((packet) => category === 'communication'
    ? ['request', 'intercom', 'question', 'answer', 'dispatch', 'context'].includes(packet.kind)
    : ['plan', 'document', 'result'].includes(packet.kind));
  const filteredPackets = filterParticipant
    ? categoryPackets.filter((packet) => packet.sourceParticipantId === filterParticipant || packet.targetParticipantIds.includes(filterParticipant))
    : categoryPackets;
  const selectedPacket = filteredPackets.find((packet) => packet.id === selectedPacketId) ?? filteredPackets.at(-1);
  const visiblePackets = showAllPackets ? filteredPackets : filteredPackets.slice(-FLOW_PACKET_WINDOW);
  const actorName = (actorId: string) => actorId === 'root'
    ? originLabel
    : partners.find((partner) => partner.participantId === actorId)?.celestialName ?? actorId;
  const replyTo = selectedPacket?.replyToPacketId ? flow.find((packet) => packet.id === selectedPacket.replyToPacketId) : undefined;
  const replies = selectedPacket ? flow.filter((packet) => packet.replyToPacketId === selectedPacket.id) : [];

  return (
    <section aria-label="往来记录" className="paw-room-focus-overview__section paw-room-focus-overview__flow">
      <header>
        <span><GitBranch aria-hidden="true" size={14} /><strong>消息流转</strong></span>
        <span className="paw-room-focus-overview__flow-window">
          <small>最近 {visiblePackets.length} / 共 {filteredPackets.length} 条</small>
          {filteredPackets.length > visiblePackets.length ? <button onClick={() => setShowAllPackets(true)} type="button">显示全部</button> : null}
        </span>
      </header>
      <div className="paw-room-focus-overview__flow-controls">
        <label>内容<select aria-label="消息类型" onChange={(event) => { setCategory(event.target.value); setSelectedPacketId(''); }} value={category}>
          <option value="communication">协作往返</option><option value="public">公开汇报</option><option value="all">全部事件</option>
        </select></label>
        <label>查看往来<select aria-label="按行星筛选消息" onChange={(event) => { setFilterParticipant(event.target.value); setSelectedPacketId(''); }} value={filterParticipant}>
          <option value="">全部行星</option>
          {partners.map((partner) => <option key={partner.participantId} value={partner.participantId}>{partner.celestialName}</option>)}
        </select></label>
        {onRefreshTraffic ? <button aria-label="刷新消息与卫星" onClick={onRefreshTraffic} type="button"><RefreshCw aria-hidden="true" size={13} />刷新</button> : null}
      </div>
      {intercomStatus ? <p className="paw-room-focus-overview__traffic-source" data-status={intercomStatus}>
        {intercomStatus === 'error' ? '直接通信暂时无法更新，已保留可读取的记录。' : intercomStatus === 'loading' ? '正在读取直接通信记录…' : '本轮公开记录与最近 200 条直接通信'}
      </p> : null}
      {selectedPacket ? (
        <div className="paw-room-focus-overview__selected-message" ref={detailRef}>
        <FocusTrafficRoute actorName={actorName} packet={selectedPacket} onOpenParticipant={onOpenParticipant} />
        <div className="paw-room-focus-overview__packet-detail">
          <header>
            <strong>{packetKindLabel(selectedPacket.kind)}</strong>
            <span data-status={selectedPacket.status}>{flowStatusLabel(selectedPacket.status, selectedPacket)}</span>
          </header>
          <div className="paw-room-focus-overview__message-content"><MarkdownBody
            documentKey={selectedPacket.id}
            sessionId={partners.find((partner) => partner.participantId === selectedPacket.sourceParticipantId)?.sessionId}
            text={packetSummary(selectedPacket)}
          /></div>
          {selectedPacket.error ? <p className="paw-room-focus-overview__delivery-error">{selectedPacket.error}</p> : null}
          {selectedPacket.replyToPacketId ? <div className="paw-room-focus-overview__reply-link">
            <span>回复的消息</span>
            {replyTo ? <button onClick={() => selectPacket(replyTo.id)} type="button">{actorName(replyTo.sourceParticipantId)}：{replyTo.summary}</button>
              : <span>原消息不在已读取的记录中 · {selectedPacket.replyToPacketId.replace(/^intercom:/, '')}</span>}
          </div> : null}
          {replies.length ? <div className="paw-room-focus-overview__reply-link"><span>已收到 {replies.length} 条回复</span>{replies.map((reply) => <button key={reply.id} onClick={() => selectPacket(reply.id)} type="button">查看 {actorName(reply.sourceParticipantId)} 的回复</button>)}</div> : null}
          {selectedPacket.dispatchPlan ? (
            <FocusDispatchPlan
              actorName={actorName}
              packet={selectedPacket}
              plan={selectedPacket.dispatchPlan}
              workItems={workItems}
            />
          ) : null}
          <Disclosure className="paw-room-focus-overview__message-evidence" summary="消息证据">
            <dl>
              <dt>消息</dt><dd>{selectedPacket.intercomId || selectedPacket.id}</dd>
              <dt>发送时间</dt><dd>{packetTimestamp(selectedPacket.createdAtMs)}</dd>
              {selectedPacket.deliveredAtMs ? <><dt>送达时间</dt><dd>{packetTimestamp(selectedPacket.deliveredAtMs)}</dd></> : null}
              {selectedPacket.repliedAtMs ? <><dt>回复时间</dt><dd>{packetTimestamp(selectedPacket.repliedAtMs)}</dd></> : null}
              {selectedPacket.receiptIds?.length ? <><dt>回执事件</dt><dd>{selectedPacket.receiptIds.join('\n')}</dd></> : null}
              {selectedPacket.dispatchId ? <><dt>分派</dt><dd>{selectedPacket.dispatchId}</dd></> : null}
              {selectedPacket.workItemId ? <><dt>任务</dt><dd>{selectedPacket.workItemId}</dd></> : null}
              {selectedPacket.refs.length ? <><dt>上下文 / 文档</dt><dd>{selectedPacket.refs.join('\n')}</dd></> : null}
            </dl>
          </Disclosure>
        </div>
        </div>
      ) : null}
      <div className="paw-room-focus-overview__history-label">消息记录</div>
      {filteredPackets.length ? (
        <ol aria-label="往来事件" className="paw-room-focus-overview__packets">
          {visiblePackets.map((packet, index) => (
            <li data-kind={packet.kind} data-status={packet.status} key={packet.id}>
              <button
                aria-pressed={packet.id === selectedPacket?.id}
                onClick={() => selectPacket(packet.id)}
                type="button"
              >
                <span aria-hidden="true" className="paw-room-focus-overview__packet-icon">{packetIcon(packet.kind)}</span>
                <span className="paw-room-focus-overview__packet-copy">
                  <strong className="paw-room-focus-overview__packet-route">{actorName(packet.sourceParticipantId)} → {packet.targetParticipantIds.map(actorName).join('、') || 'Room'}</strong>
                  <p>{packetSummary(packet)}</p>
                  <small>
                    {packetKindLabel(packet.kind)}
                    {' · '}<span data-status={packet.status}>{flowStatusLabel(packet.status, packet)}</span>
                    {packet.createdAtMs ? ` · ${packetClock(packet.createdAtMs)}` : ''}
                    <span className="paw-room-focus-overview__packet-number"> · #{filteredPackets.length - visiblePackets.length + index + 1}</span>
                  </small>
                </span>
              </button>
            </li>
          ))}
        </ol>
      ) : <p className="paw-room-focus-overview__empty">{intercomStatus === 'loading' ? '正在恢复 Room 的直接通信与消息记录…' : intercomStatus === 'error' ? '消息记录暂时无法读取，点击刷新重试。' : filterParticipant ? '这颗行星在当前筛选下没有往来记录。' : '当前筛选没有可读取的往来。消息发出后，这里会展示发送方、接收方和投递状态。'}</p>}

    </section>
  );
}

/** One selected message has an explicit source and arrow into every actual
 * recipient. This path includes pending/failed messages and reverse replies;
 * it is traffic, independent of the confirmed responsibility graph above. */
function FocusTrafficRoute({ actorName, packet, onOpenParticipant }: {
  actorName: (id: string) => string;
  packet: RoomFocusPacket;
  onOpenParticipant?: (id: string) => void;
}) {
  const actor = (id: string) => <button
    className="paw-room-focus-overview__route-actor"
    disabled={id === 'root' || !onOpenParticipant}
    onClick={() => onOpenParticipant?.(id)}
    type="button"
  ><i aria-hidden="true" style={planetTexture(actorName(id))} /><strong>{actorName(id)}</strong></button>;
  return <div aria-label="选中消息的流转方向" className="paw-room-focus-overview__traffic-route" data-status={packet.status}>
    {actor(packet.sourceParticipantId)}
    <span className="paw-room-focus-overview__route-track"><span>{packetKindLabel(packet.kind)}</span><i aria-hidden="true"><ArrowRight size={16} /></i><strong>{flowStatusLabel(packet.status, packet)}</strong></span>
    <span className="paw-room-focus-overview__route-targets">{packet.targetParticipantIds.map((id) => <span key={id}>{actor(id)}</span>)}</span>
  </div>;
}

/** A route_decision as a readable plan: who exerted the gravity on whom, why,
 * inside which parallel wave, for which real task — plus the scored候选. */
function FocusDispatchPlan({
  actorName,
  packet,
  plan,
  workItems,
}: {
  actorName: (actorId: string) => string;
  packet: RoomFocusPacket;
  plan: RoomDispatchPlan;
  workItems: RoomFocusWorkItem[];
}) {
  const targetName = plan.targetParticipantId ? actorName(plan.targetParticipantId) : '伙伴';
  const objective = workItems.find((item) => item.id === plan.workItemId)?.objective;
  const selectedCandidates = plan.candidates.filter((candidate) => candidate.selected);
  return (
    <div aria-label="分派方案" className="paw-room-focus-overview__dispatch-plan" role="group">
      <span className="paw-room-focus-overview__dispatch-route">
        <b>{actorName(packet.sourceParticipantId)}</b>
        <ArrowRight aria-hidden="true" size={12} />
        <b>{targetName}</b>
      </span>
      <dl>
        <div><dt>方式</dt><dd>{plan.reasonLabel}{plan.routingPolicyLabel ? ` · ${plan.routingPolicyLabel}` : ''}</dd></div>
        {plan.parallelIndex >= 0 && plan.parallelSize > 1 ? (
          <div><dt>并行</dt><dd>轨道 {plan.parallelIndex + 1}/{plan.parallelSize}{plan.phaseName ? ` · ${plan.phaseName}` : ''}</dd></div>
        ) : plan.phaseName ? <div><dt>阶段</dt><dd>{plan.phaseName}</dd></div> : null}
        {objective ? <div><dt>任务</dt><dd className="paw-room-focus-overview__dispatch-objective">{objective}</dd></div> : null}
      </dl>
      {plan.candidates.length ? (
        <Disclosure
          className="paw-room-focus-overview__dispatch-candidates"
          summary={`候选 ${plan.candidates.length} 位 · 选中 ${selectedCandidates.length} 位`}
        >
          <ul>
            {plan.candidates.map((candidate) => (
              <li data-selected={candidate.selected || undefined} key={candidate.participantId}>
                <strong>{actorName(candidate.participantId)}</strong>
                <small>{candidate.selected ? '已选中' : '候选行星'}</small>
                <span>{candidate.signals.length ? candidate.signals.join('、') : '无信号'} · {candidate.score.toFixed(1)}</span>
              </li>
            ))}
          </ul>
        </Disclosure>
      ) : null}
    </div>
  );
}

function FocusInspector({
  onOpenParticipant,
  originLabel,
  partner,
  partners,
  work,
  satellites,
}: {
  onOpenParticipant?: (participantId: string) => void;
  originLabel: string;
  partner?: RoomFocusPartner;
  partners: RoomFocusPartner[];
  work?: RoomFocusWorkItem;
  satellites?: RoomSatelliteSnapshot;
}) {
  const state = work?.state ?? partner?.state ?? 'idle';
  const action = readableActivity(workAction(work) || partner?.currentAction || '', work?.objective || '等待新的工作项');
  const title = work?.objective || partner?.celestialName || originLabel;
  const hasLongInstruction = title.length > 100 || action.length > 180;
  const evidence = work?.evidence ?? [];
  const verifier = work?.review
    ? partners.find((candidate) => candidate.participantId === work.review?.reviewerParticipantId)
    : undefined;
  return (
    <section aria-label="焦点详情" className="paw-room-focus-overview__inspector" data-state={state} key={`${work?.id ?? ''}:${partner?.participantId ?? ''}`} role="region">
      <header>
        <span><FileCheck2 aria-hidden="true" size={14} /><strong>焦点详情</strong></span>
        <span className="paw-room-focus-overview__state"><i aria-hidden="true" />{work ? roomWorkStatusLabel(work) : roomPartnerStatusLabel(state)}</span>
      </header>
      <div className="paw-room-focus-overview__inspector-copy">
        <small>{work ? '当前任务' : '当前伙伴'}</small>
        <strong title={title}>{title}</strong>
        {action !== title ? <p>{action}</p> : null}
        {hasLongInstruction ? <Disclosure summary="完整任务与进展" className="paw-room-focus-overview__full-task">
          <MarkdownBody documentKey={`${work?.id ?? partner?.participantId}:task`} sessionId={partner?.sessionId} text={title} />
          {action !== title ? <MarkdownBody documentKey={`${work?.id ?? partner?.participantId}:action`} sessionId={partner?.sessionId} text={action} /> : null}
        </Disclosure> : null}
      </div>
      {work?.wave ? (
        <p className="paw-room-focus-overview__inspector-wave">
          <Waypoints aria-hidden="true" size={12} />
          并行轨道 {Math.max(work.wave.parallelIndex, 0) + 1}/{Math.max(work.wave.parallelSize, 1)}
          {work.wave.phaseName ? ` · ${work.wave.phaseName}` : ''}
        </p>
      ) : null}
      {partner ? (
        <dl>
          <div><dt>负责人</dt><dd>{partner.celestialName} · {roomPartnerStatusLabel(partner.state)}</dd></div>
          {partner.latestReceipt ? <div><dt>最近回执</dt><dd>{readableActivity(partner.latestReceipt, '已收到工作回执')}</dd></div> : null}
        </dl>
      ) : null}
      {partner ? <section aria-label={`${partner.celestialName} 的 Session 卫星`} className="paw-room-focus-overview__satellites">
        <header><Satellite aria-hidden="true" size={14} /><strong>Session 卫星</strong><span>{satellites?.status === 'ready' ? `${satellites.satellites.length} 个` : satellites?.status === 'loading' ? '正在读取' : satellites?.status === 'error' ? '暂不可用' : '数量未知'}</span></header>
        {satellites?.status === 'ready' && satellites.error ? <p>显示上次读取的卫星状态，暂时无法更新。</p> : null}
        {satellites?.status !== 'ready' ? <p>{satellites?.status === 'error' ? '暂时无法读取，数量未知。刷新后可重试。' : satellites?.status === 'loading' ? '正在读取这个 Session 的卫星记录…' : '尚未取得这个 Session 的卫星记录。'}</p>
          : !satellites.satellites.length ? <p>已读取该 Session，当前没有保留的 Tool Agent 节点。</p>
            : <>
              <p>{satelliteBreakdown(satellites)} · 按节点统计，重试合并到原卫星</p>
              <ul>{satellites.satellites.map((satellite) => <li key={satellite.nodeId || satellite.id} data-state={satellite.state}>
                <Disclosure summary={<><i aria-hidden="true" /><span>{satellite.task}</span><strong>{satellite.stateLabel}</strong></>}>
                  {satellite.result ? <p>{satellite.result}</p> : null}
                  {satellite.error ? <p className="paw-room-focus-overview__delivery-error">{satellite.error}</p> : null}
                  <dl><dt>节点</dt><dd>{satellite.nodeId || satellite.id}</dd><dt>本次执行</dt><dd>{satellite.id}</dd><dt>Session</dt><dd>{satellite.sessionId}</dd><dt>层级</dt><dd>{satellite.depth}</dd></dl>
                </Disclosure>
              </li>)}</ul>
            </>}
      </section> : null}
      {work?.expectedOutput ? (
        <p className="paw-room-focus-overview__expected"><span>期望交付</span>{work.expectedOutput}</p>
      ) : null}
      {work?.acceptanceCriteria.length ? (
        <Disclosure
          className="paw-room-focus-overview__acceptance"
          summary={`验收条件 · ${work.acceptanceCriteria.length}`}
        >
          <ul>{work.acceptanceCriteria.map((criterion) => <li key={criterion}>{criterion}</li>)}</ul>
        </Disclosure>
      ) : null}
      {work?.blocker ? (
        <div className="paw-room-focus-overview__blocker">
          <strong>{work.blocker.reason}</strong>
          {work.blocker.nextStep ? <span>下一步：{work.blocker.nextStep}</span> : null}
        </div>
      ) : null}
      {work?.review ? (
        <div className="paw-room-focus-overview__review" data-verdict={reviewPassed(work) ? 'passed' : 'attention'}>
          <span><ShieldCheck aria-hidden="true" size={13} />独立复核{verifier ? ` · ${verifier.celestialName}` : ''}</span>
          <dl>
            <div><dt>可运行</dt><dd>{reviewVerdictLabel(work.review.operability)}</dd></div>
            <div><dt>符合需求</dt><dd>{reviewVerdictLabel(work.review.requirement)}</dd></div>
          </dl>
          {work.review.reason ? <p>{work.review.reason}</p> : null}
        </div>
      ) : null}
      {work?.latestResult ? <p className="paw-room-focus-overview__result">{work.latestResult}</p> : null}
      {evidence.length ? (
        <ul aria-label="证据与产物" className="paw-room-focus-overview__evidence">
          {evidence.map((item) => <li key={`${item.kind}:${item.ref}`}><span>{item.kind === 'artifact' ? '产物' : '证据'}</span><code>{item.ref}</code></li>)}
        </ul>
      ) : null}
      {partner && onOpenParticipant ? (
        <button className="paw-room-focus-overview__open" onClick={() => onOpenParticipant(partner.participantId)} type="button">
          <ExternalLink aria-hidden="true" size={13} />打开 {partner.celestialName} 伙伴窗口
        </button>
      ) : null}
    </section>
  );
}

function reviewPassed(work: RoomFocusWorkItem): boolean {
  if (!work.review) return false;
  const good = new Set(['passed', 'satisfied', 'pass', 'ok']);
  return good.has(work.review.operability) && good.has(work.review.requirement);
}

function reviewVerdictLabel(verdict: string): string {
  return ({
    passed: '通过',
    pass: '通过',
    satisfied: '满足',
    ok: '通过',
    failed: '未通过',
    unsatisfied: '未满足',
    blocked: '受阻',
  } as Record<string, string>)[verdict] ?? (verdict || '未记录');
}

function packetIcon(kind: RoomFocusPacketKind): ReactNode {
  if (kind === 'approval') return <ShieldCheck size={13} />;
  if (kind === 'review') return <FileCheck2 size={13} />;
  if (kind === 'intercom') return <MessageCircle size={13} />;
  if (kind === 'question') return <CircleHelp size={13} />;
  if (kind === 'plan') return <GitBranch size={13} />;
  if (kind === 'document' || kind === 'context') return <FileText size={13} />;
  if (kind === 'result' || kind === 'answer') return <MessageCircle size={13} />;
  if (kind === 'dispatch') return <Route size={13} />;
  return <Send size={13} />;
}

function packetKindLabel(kind: RoomFocusPacketKind): string {
  return roomFlowKindLabel(kind);
}

function flowStatusLabel(status: string, packet?: RoomFocusPacket): string {
  return roomFlowStatusLabel(status, Boolean(packet?.intercomId));
}

function packetSummary(packet: RoomFocusPacket): string {
  if (packet.intercomId || packet.id.startsWith('message:')) return packet.summary;
  return readableActivity(packet.summary, `${packetKindLabel(packet.kind)} · ${flowStatusLabel(packet.status, packet)}`);
}

function readableActivity(value: string, fallback: string): string {
  return /^(?:participant_activity|approval_[a-z_]+|tool_[a-z_]+|route_decision)$/.test(value.trim()) ? fallback : roomPublicAction(value, fallback);
}

function packetClock(timestamp: number): string {
  return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(new Date(timestamp));
}

function packetTimestamp(timestamp: number): string {
  return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(new Date(timestamp));
}

function planetTexture(name: string) {
  const texture = ['mercury', 'venus', 'earth', 'mars', 'jupiter', 'saturn', 'uranus', 'neptune'].find((planet) => name.toLowerCase() === planet);
  return texture ? { backgroundImage: `url("/paw-media/starfield/${texture}-1k.jpg")` } : undefined;
}

function satelliteBreakdown(snapshot: RoomSatelliteSnapshot): string {
  const labels: Record<string, string> = { queued: '等待', running: '进行', completed: '完成', returned: '已返回', contract_invalid: '合同无效', failed: '失败', aborted: '停止', timed_out: '超时' };
  return Object.entries(labels).flatMap(([state, label]) => {
    const count = snapshot.satellites.filter((satellite) => satellite.state === state).length;
    return count ? [`${label} ${count}`] : [];
  }).join(' · ');
}

function defaultFocusSelection(focus: RoomFocusProjection): FocusSelection {
  for (const state of selectionPriority) {
    const item = focus.workItems.find((candidate) => candidate.state === state);
    if (item) return { kind: 'work', id: item.id };
  }
  const partner = focus.partners.at(0);
  return partner ? { kind: 'partner', id: partner.participantId } : { kind: 'work', id: '' };
}

function workAction(work?: RoomFocusWorkItem): string {
  if (!work) return '';
  if (work.currentAction) return work.currentAction;
  if (work.blocker?.reason) return work.blocker.reason;
  if (work.reviewRequired) return '等待独立复核';
  return work.latestResult || work.expectedOutput || '';
}
