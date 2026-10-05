import {
  Bot,
  Check,
  ChevronRight,
  CircleDashed,
  FileText,
  FolderKanban,
  GitBranch,
  ListChecks,
  LoaderCircle,
  Paperclip,
  PanelRightClose,
  PanelsTopLeft,
  Radar,
  TriangleAlert,
  type LucideIcon,
} from 'lucide-react';
import { forwardRef, useEffect, useId, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import { useOptionalControlTransport } from '@/app/control-transport';
import { Disclosure, IconButton } from '@/components/primitives';
import { selectRoomParticipantPublicProgress } from '@/contracts/room-reducer';
import type {
  RoomActivityProjection,
  RoomMessageProjection,
  RoomProjectionState,
  RoomTurnProjection,
} from '@/contracts/room-reducer';
import type { RoomParticipantPublicProgressProjection } from '@/contracts/room-reducer';
import type { RoomSummary, RoomWorkItem } from './room-types';
import { roomParticipantPlanetName } from './room-participant-identity';
import { agentSessionAddress, selectAgentProjection, useAgentLiveStore } from '../agent/state/live-store';
import { ROOM_PUBLIC_PROGRESS_KIND_LABELS, roomCollaborationRoleDescription, roomCollaborationRoleLabel, roomParticipantPublicProgressSummary } from './room-copy';
import { roomActivityNeedsSessionAction } from './runtime/room-execution-lanes';
import { roomProjection, useRoomLiveStore } from './state/live-store';
import { publicToolName } from '../agent/tool-presentation';
import { usePawOsDesktop } from '@/features/paw-os/surface-context';
import { roomPlanetObserverWindowRequest } from '@/paw-os/apps/room-satellite-auto-open';
import '../agent/agent.css';

export const RoomStatusPanel = forwardRef<HTMLElement, {
  room?: RoomSummary;
  roomId?: string;
  projection?: RoomProjectionState;
  open: boolean;
  modal?: boolean;
  onClose?: () => void;
}>(function RoomStatusPanel({
  room,
  roomId = '',
  projection: providedProjection,
  open,
  modal = false,
  onClose,
}, ref) {
  const effectiveRoomId = roomId || room?.id || '';
  useRoomLiveStore((state) => (
    open && !providedProjection
      ? state.roomRevisions[effectiveRoomId] ?? 0
      : 0
  ));
  const projection = providedProjection ?? roomProjection(effectiveRoomId);
  const participantProgress = selectRoomParticipantPublicProgress(projection);
  const turn = latestRoomTurn(projection);
  const activities = turn?.activityIds
    .map((id) => projection.activitiesById[id])
    .filter(Boolean)
    .filter((activity) => !roomActivityIsAutomaticPolicyAuthorization(activity)) ?? [];
  const messages = turn?.messageIds.map((id) => projection.messagesById[id]).filter(Boolean) ?? [];
  const attachments = new Set(messages.flatMap((message) => message.message?.attachments ?? []));
  const files = messages.flatMap((message) => message.message?.blocks ?? []).filter((block) => block.type === 'file');
  const deliveredArtifacts = messages.flatMap((message) => message.message?.blocks ?? []).filter((block) => block.type === 'diff' || (block.type === 'file' && Boolean(block.data.mediaId ?? block.data.artifactId ?? block.data.receiptId)));
  const sharedArtifacts = (room?.artifacts ?? []).filter((artifact) => artifact.status === 'active');
  const activeTopics = (room?.topics ?? []).filter((topic) => topic.status === 'active');
  const workItems = room?.workItems ?? [];
  const visibleWorkItems = [...workItems]
    .sort((left, right) => roomWorkPriority(left.state) - roomWorkPriority(right.state));
  const activityGroups = collapseRoomStatusActivities(activities).slice(-8);
  const publicMessageCount = messages.filter((message) => message.projectionKind !== 'execution').length;

  const projectedStatus = roomProjectedStatus(turn, activities, messages);
  return (
    <aside
      ref={ref}
      aria-hidden={!open}
      aria-label="协作进展"
      aria-modal={modal || undefined}
      className="agent-status-panel room-status-panel"
      data-open={open}
      inert={open ? undefined : true}
      role={modal ? 'dialog' : undefined}
      tabIndex={-1}
    >
      <header>
        <span><strong>协作进展</strong><small>{turn ? roomProjectedStatusLabel(projectedStatus) : '等你开始新一轮'}</small></span>
        {onClose ? <IconButton icon={<PanelRightClose size={17} />} label="收起进展面板" onClick={onClose} tooltip /> : null}
      </header>
      <div className="agent-status-panel__body">
        <RoomStatusSection count={turn ? 1 : 0} icon={ListChecks} title="当前回合">
          {turn ? (
            <div className="agent-status-turn" data-state={projectedStatus}>
              <RoomTurnIcon status={projectedStatus} />
              <span>
                <strong>{roomProjectedStatusLabel(projectedStatus)}</strong>
                <small>{publicMessageCount ? `${publicMessageCount} 条公开消息` : '还没有公开消息'}</small>
                <span className="room-status-turn__metrics">
                  <i>{turn.participantIds.length} 位伙伴</i>
                  <i>{activities.length} 个过程步骤</i>
                </span>
              </span>
            </div>
          ) : <RoomStatusEmpty>发出消息后，这里会显示本轮进展</RoomStatusEmpty>}
        </RoomStatusSection>
        <RoomStatusSection
          count={room?.participants.length ?? new Set(
            participantProgress.map((item) => item.participantId || item.sourceSessionId),
          ).size}
          icon={Bot}
          title="每位伙伴的进度"
        >
          <RoomParticipantPublicLanes
            progress={participantProgress}
            room={room}
            workItems={workItems}
          />
        </RoomStatusSection>

        <RoomStatusSection count={workItems.filter((work) => !['done', 'failed', 'cancelled'].includes(work.state)).length} icon={ListChecks} title="任务分工">
          {visibleWorkItems.length
            ? <div className="room-status-work">{visibleWorkItems.map((work) => <RoomWorkRow key={work.id} room={room} work={work} />)}</div>
            : <RoomStatusEmpty>任务开始后，这里会显示谁在做、谁会一起检查</RoomStatusEmpty>}
        </RoomStatusSection>


        <RoomStatusSection
          count={activityGroups.length}
          defaultOpen={activities.some((activity) => ['running', 'waiting', 'failed', 'aborted'].includes(roomActivityStatus(activity)))}
          icon={GitBranch}
          title="关键步骤"
        >
          {activityGroups.length ? <div className="room-status-activities">{activityGroups.map((group) => {
            const activity = group.activity;
            const participant = room?.participants.find((item) => item.id === activity.participantId);
            const presentation = roomStatusActivityPresentation(activity);
            const status = roomActivityStatus(activity);
            return <div className="room-status-activity" data-state={status} key={group.key}>
              <RoomActivityIcon status={status} />
              <span>
                <span className="room-status-activity__heading">
                  <strong>{roomParticipantPlanetName(participant)} · {presentation.title}</strong>
                  <i>{roomActivityStatusLabel(status)}</i>
                </span>
                <small>{presentation.detail}{group.count > 1 ? ` · 合并 ${group.count} 次更新` : ''}</small>
              </span>
            </div>;
          })}</div> : <RoomStatusEmpty>本轮还没有协作步骤</RoomStatusEmpty>}
        </RoomStatusSection>

        <RoomStatusSection count={attachments.size + files.length} defaultOpen={false} icon={Paperclip} title="附件与文件">
          {attachments.size || files.length ? <div className="agent-status-files">{attachments.size ? <RoomStatusRow detail="随协作消息保存在本机" icon={Paperclip} title={`${attachments.size} 个消息附件`} /> : null}{files.map((block) => <RoomStatusRow detail={text(block.data.mimeType) || '文件'} icon={FileText} key={block.id} title={fileName(block.data)} />)}</div> : <RoomStatusEmpty>还没有分享附件或文件</RoomStatusEmpty>}
        </RoomStatusSection>

        <RoomStatusSection count={sharedArtifacts.length + deliveredArtifacts.length} defaultOpen={false} icon={FolderKanban} title="共享资料与产物">
          {sharedArtifacts.length || deliveredArtifacts.length ? <div className="agent-status-files">{sharedArtifacts.map((artifact) => <RoomStatusRow detail={`${artifact.mediaType || '共享文件'} · ${pathName(artifact.path)}`} icon={FileText} key={artifact.id} title={artifact.displayName} />)}{deliveredArtifacts.map((block) => <RoomStatusRow detail={block.type === 'diff' ? '变更产物' : '文件产物'} icon={FolderKanban} key={block.id} title={fileName(block.data)} />)}</div> : <RoomStatusEmpty>还没有共享资料或交付物</RoomStatusEmpty>}
        </RoomStatusSection>

        <RoomStatusSection count={activeTopics.length} defaultOpen={false} icon={GitBranch} title="话题">
          {activeTopics.length ? <div className="agent-status-files">{activeTopics.map((topic) => <RoomStatusRow detail={topic.id === room?.activeTopicId ? '当前话题' : topic.summary || '可切换话题'} icon={GitBranch} key={topic.id} title={topic.title} />)}</div> : <RoomStatusEmpty>还没有单独整理话题</RoomStatusEmpty>}
        </RoomStatusSection>

        <RoomStatusSection count={room?.workspaceRoots?.length ?? 0} defaultOpen={false} icon={FolderKanban} title="工作目录">
          {room?.workspaceRoots?.length ? <div className="agent-status-files">{room.workspaceRoots.map((path) => <RoomStatusRow detail={path} icon={FolderKanban} key={path} title={pathName(path)} />)}</div> : <RoomStatusEmpty>{room?.roomKind === 'roleplay' ? '一起聊聊不会访问项目目录' : '这个协作空间还没有工作目录'}</RoomStatusEmpty>}
        </RoomStatusSection>

        <RoomStatusSection count={room?.participants.filter((participant) => participant.status === 'active').length ?? 0} defaultOpen={false} icon={Bot} title="伙伴状态">
          {room?.participants.some((participant) => participant.status === 'active') ? <div className="room-status-participants">{room.participants.filter((participant) => participant.status === 'active').map((participant) => <RoomParticipantTelemetry key={participant.id} participant={participant} roomId={room.id} />)}</div> : <RoomStatusEmpty>还没有伙伴加入</RoomStatusEmpty>}
        </RoomStatusSection>

        {room ? (
          <a
            className="agent-status-observation-link"
            href={`#/observability?roomId=${encodeURIComponent(room.id)}`}
          >
            <Radar size={16} />
            <span><strong>查看详细运行记录</strong><small>检查消息路由、伙伴通信和任务轨迹</small></span>
            <GitBranch size={15} />
          </a>
        ) : null}
      </div>
    </aside>
  );
});

const ROOM_STATUS_PUBLIC_FEED_THRESHOLD_PX = 32;
const roomStatusTimeFormatter = new Intl.DateTimeFormat('zh-CN', {
  hour: '2-digit',
  minute: '2-digit',
});

const ROOM_STATUS_PARTICIPANT_STATE_LABELS: Record<
  RoomParticipantPublicProgressProjection['status'] | 'idle',
  string
> = {
  running: '执行中',
  waiting: '等待中',
  completed: '已完成',
  failed: '需要关注',
  aborted: '已停止',
  idle: '已加入',
};


function RoomParticipantPublicLanes({
  progress,
  room,
  workItems,
}: {
  progress: RoomParticipantPublicProgressProjection[];
  room?: RoomSummary;
  workItems: RoomWorkItem[];
}) {
  const feedRef = useRef<HTMLDivElement>(null);
  const followsLatestRef = useRef(true);
  const latestProgress = progress.reduce<RoomParticipantPublicProgressProjection | undefined>(
    (latest, item) => !latest || item.updatedAtMs > latest.updatedAtMs ? item : latest,
    undefined,
  );
  const updateSignature = progress
    .map((item) => `${item.rootId}:${item.participantId}:${item.sourceSessionId}:${item.kind}:${item.updatedAtMs}`)
    .join('|');

  useEffect(() => {
    const feed = feedRef.current;
    if (!feed || !followsLatestRef.current) return;
    const latestCard = feed.querySelector<HTMLElement>('[data-latest="true"]');
    if (!latestCard) return;
    const behavior = typeof window.matchMedia === 'function'
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches
      ? 'auto'
      : 'smooth';
    if (typeof latestCard.scrollIntoView === 'function') {
      latestCard.scrollIntoView({ behavior, block: 'nearest' });
    } else if (typeof feed.scrollTo === 'function') {
      feed.scrollTo({
        behavior,
        top: latestCard.offsetTop - feed.offsetTop,
      });
    }
  }, [updateSignature]);

  const participantIds = [...new Set([
    ...(room?.participants.map((participant) => participant.id) ?? []),
    ...progress.map((item) => item.participantId).filter(Boolean),
    ...progress
      .filter((item) => !item.participantId)
      .map((item) => item.sourceSessionId)
      .filter(Boolean),
  ])];
  if (!participantIds.length) {
    return <RoomStatusEmpty>伙伴开始工作后，这里会逐一显示公开进度</RoomStatusEmpty>;
  }

  return <div
    aria-label="工作进展与运行记录"
    aria-live="polite"
    aria-relevant="additions text"
    className="room-status-public-lanes"
    onScroll={(event) => {
      const feed = event.currentTarget;
      const latestCard = feed.querySelector<HTMLElement>('[data-latest="true"]');
      if (!latestCard) {
        followsLatestRef.current = true;
        return;
      }
      const feedBounds = feed.getBoundingClientRect();
      const latestBounds = latestCard.getBoundingClientRect();
      followsLatestRef.current = (
        latestBounds.bottom >= feedBounds.top - ROOM_STATUS_PUBLIC_FEED_THRESHOLD_PX
        && latestBounds.top <= feedBounds.bottom + ROOM_STATUS_PUBLIC_FEED_THRESHOLD_PX
      );
    }}
    ref={feedRef}
    tabIndex={0}
  >
    {participantIds.map((participantId) => {
      const participant = room?.participants.find((item) => (
        item.id === participantId || item.sessionId === participantId
      ));
      const updates = progress
        .filter((item) => (
          item.participantId === participant?.id
          || item.sourceSessionId === participant?.sessionId
          || (!participant && (
            item.participantId === participantId || item.sourceSessionId === participantId
          ))
        ))
        .sort((left, right) => left.updatedAtMs - right.updatedAtMs);
      const latestUpdate = updates.at(-1);
      const isLatest = latestProgress !== undefined && updates.includes(latestProgress);
      const ownedWork = workItems.filter((work) => (
        work.currentOwnerParticipantId === participant?.id
        || work.offeredToParticipantId === participant?.id
      ));
      const state = latestUpdate?.status ?? (participant?.status === 'active' ? 'idle' : 'waiting');
      const role = participant
        ? roomCollaborationRoleLabel(participant.collaborationRole)
        : '角色信息同步中';
      const hasPublicWork = ownedWork.length > 0 || latestUpdate !== undefined;
      const roleSummary = participant
        ? `${role} · ${hasPublicWork
          ? roomCollaborationRoleDescription(participant.collaborationRole)
          : participant.collaborationRole === 'reviewer'
            ? '等待整合完成后开始'
            : '等待本角色分工'}`
        : role;
      return <article
        className="room-status-public-lane"
        data-participant-id={participant?.id ?? participantId}
        data-latest={isLatest || undefined}
        data-state={state}
        key={participantId}
      >
        <header>
          <Bot size={16} />
          <span>
            <strong>{roomParticipantPlanetName(participant)}</strong>
            <small>{roleSummary}</small>
          </span>
          <i>{ROOM_STATUS_PARTICIPANT_STATE_LABELS[state]}</i>
        </header>
        <div className="room-status-public-lane__work">
          <strong>当前分工</strong>
          {ownedWork.length ? <ul>{ownedWork.map((work) => <li key={work.id}>
            <span>{work.objective}</span>
            <small>{roomWorkStateLabel(work.state)}</small>
          </li>)}</ul> : <p>这位伙伴暂时没有单独分到的部分</p>}
        </div>
        <div className="room-status-public-lane__updates">
          {updates.length ? updates.map((update) => <div
            data-kind={update.kind}
            data-state={update.status}
            key={`${update.rootId}:${update.dispatchId}:${update.participantId}:${update.sourceSessionId}:${update.kind}`}
          >
            <header>
              <strong>{ROOM_PUBLIC_PROGRESS_KIND_LABELS[update.kind]}</strong>
              <time dateTime={new Date(update.updatedAtMs).toISOString()}>
                {roomStatusTimeFormatter.format(new Date(update.updatedAtMs))}
              </time>
            </header>
            <p>{roomParticipantPublicProgressSummary(update)}</p>
          </div>) : <p>等待工作摘要、状态或工具进度。</p>}
        </div>
      </article>;
    })}
  </div>;
}

function RoomParticipantTelemetry({ participant, roomId }: { participant: NonNullable<RoomSummary['participants']>[number]; roomId: string }) {
  const pawOsDesktop = usePawOsDesktop();
  const transport = useOptionalControlTransport();
  const address = agentSessionAddress(transport, participant.sessionId);
  const telemetry = useAgentLiveStore((state) => selectAgentProjection(state, address)?.telemetry);
  if (!telemetry) {
    return <article className="room-participant-telemetry room-participant-telemetry--quiet"><header><span><strong>{roomParticipantPlanetName(participant)}</strong><small>{roomCollaborationRoleLabel(participant.collaborationRole)} · {participant.status === 'active' ? '已加入' : '暂未参与'}</small></span>{pawOsDesktop ? <IconButton label={`打开 ${roomParticipantPlanetName(participant)} 伙伴窗口`} icon={<PanelsTopLeft size={14} />} onClick={() => pawOsDesktop.openWindow(roomPlanetObserverWindowRequest(participant, roomId))} tooltip /> : null}</header></article>;
  }
  const context = telemetry.context;
  const cumulative = telemetry.cumulativeUsage;
  const promptTokens = cumulative.input + cumulative.cacheRead + cumulative.cacheWrite;
  const cachePercent = promptTokens > 0 ? Math.round((cumulative.cacheRead / promptTokens) * 100) : 0;
  const percent = context.percent === null ? null : Math.min(100, Math.max(0, context.percent));
  return (
    <article className="room-participant-telemetry" data-compacting={telemetry.isCompacting || undefined}>
      <header>
        <span><strong>{roomParticipantPlanetName(participant)}</strong><small>{telemetry.model.name || telemetry.model.id} · {roomCollaborationRoleLabel(participant.collaborationRole)}</small></span>
        <i data-state={participant.status}>{telemetry.isCompacting ? '整理上下文' : participant.status === 'active' ? '已加入' : '暂未参与'}</i>
        {pawOsDesktop ? <IconButton label={`打开 ${roomParticipantPlanetName(participant)} 伙伴窗口`} icon={<PanelsTopLeft size={14} />} onClick={() => pawOsDesktop.openWindow(roomPlanetObserverWindowRequest(participant, roomId))} tooltip /> : null}
      </header>
      <div className="room-participant-telemetry__numbers">
        <span title="累计提示 Token">{roomTokenCount(promptTokens)} 输入</span>
        <span title="累计输出 Token">{roomTokenCount(cumulative.output)} 输出</span>
        <strong title="累计缓存命中率">缓存 {cachePercent}%</strong>
      </div>
      <div className="room-participant-telemetry__bar" aria-label={percent === null ? '上下文占用待校准' : `上下文已使用 ${Math.round(percent)}%`}>
        <span style={{ '--room-context-progress': (percent ?? 0) / 100 } as CSSProperties} />
        <b>{percent === null ? '待校准' : `${Math.round(percent)}%`}</b>
      </div>
      <p>{context.tokensUntilCompact === null ? '下一轮响应后校准' : `距自动压缩约 ${roomTokenCount(context.tokensUntilCompact)}`}</p>
    </article>
  );
}

function roomTokenCount(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value >= 10_000_000 ? 1 : 2)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(value >= 100_000 ? 0 : 1)}K`;
  return String(Math.max(0, Math.round(value)));
}

function RoomStatusSection({
  icon: Icon,
  title,
  count,
  children,
  defaultOpen = true,
}: {
  icon: LucideIcon;
  title: string;
  count: number;
  children: ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const contentId = useId();
  return <section className="agent-status-section" data-open={open}>
    <header>
      <button
        aria-controls={contentId}
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        <Icon size={15} />
        <strong>{title}</strong>
        {count > 0 ? <span>{count}</span> : null}
        <ChevronRight className="agent-status-section__chevron" size={14} />
      </button>
    </header>
    <div
      aria-hidden={!open}
      className="agent-status-section__content"
      id={contentId}
      inert={!open ? true : undefined}
    >
      <div>{children}</div>
    </div>
  </section>;
}

function RoomStatusRow({ icon: Icon, title, detail }: { icon: LucideIcon; title: string; detail: string }) {
  return <div className="agent-status-row"><Icon size={14} /><span><strong>{title}</strong><small>{detail}</small></span></div>;
}

function RoomWorkRow({ room, work }: { room?: RoomSummary; work: RoomWorkItem }) {
  const ownerId = work.offeredToParticipantId || work.currentOwnerParticipantId;
  const ownerParticipant = room?.participants.find((participant) => participant.id === ownerId);
  const accountableParticipant = room?.participants.find((participant) => participant.id === work.accountableParticipantId);
  const owner = ownerParticipant ? roomParticipantPlanetName(ownerParticipant) : '待接收';
  const accountable = accountableParticipant ? roomParticipantPlanetName(accountableParticipant) : '未指定';
  const blocker = text(work.blocker.reason);
  const showFullObjective = work.objective.trim().length > 180 || work.objective.includes('\n');
  return <article className="room-status-work__item" data-state={work.state}>
    <RoomWorkIcon work={work} />
    <div>
      <header>
        <strong>{work.objective}</strong>
        <em>{roomWorkStateLabel(work.state)}</em>
      </header>
      <div className="room-status-work__owners" aria-label="这部分由谁完成和检查">
        <span><small>{work.state === 'queued' ? '准备接手' : '正在完成'}</small><b>{owner}</b></span>
        <span><small>一起检查</small><b>{accountable}</b></span>
      </div>
      {work.expectedOutput ? <p><span>交付</span><span className="room-status-work__value">{work.expectedOutput}</span></p> : null}
      {work.acceptanceCriteria.length ? <p><span>检查</span><span className="room-status-work__value">{work.acceptanceCriteria.length} 项标准</span></p> : null}
      {work.revision ? <p><span>修订</span><span className="room-status-work__value">第 {work.revision} 次</span></p> : null}
      {blocker ? <p className="room-status-work__blocker"><span>阻塞</span><span className="room-status-work__value">{blocker}</span></p> : null}
      {showFullObjective || work.acceptanceCriteria.length ? (
        <Disclosure
          className="room-status-work__full"
          summary={showFullObjective
            ? work.acceptanceCriteria.length ? '查看完整任务与验收' : '查看完整任务'
            : '查看验收条件'}
        >
          {showFullObjective ? <p>{work.objective}</p> : null}
          {work.acceptanceCriteria.length ? <ul aria-label="验收条件">{work.acceptanceCriteria.map((criterion) => <li key={criterion}>{criterion}</li>)}</ul> : null}
        </Disclosure>
      ) : null}
    </div>
  </article>;
}

function RoomStatusEmpty({ children }: { children: ReactNode }) {
  return <p className="agent-status-empty">{children}</p>;
}

function latestRoomTurn(projection: RoomProjectionState): RoomTurnProjection | undefined {
  return [...projection.turnOrder].reverse().map((id) => projection.turnsById[id]).find(Boolean);
}

type RoomProjectedStatus =
  | 'queued'
  | 'running'
  | 'waiting_approval'
  | 'waiting_review'
  | 'waiting_select'
  | 'waiting_input'
  | 'blocked'
  | 'completed'
  | 'handed_off'
  | 'waiting'
  | 'aborted'
  | 'idle';

function RoomTurnIcon({ status }: { status: RoomProjectedStatus }) {
  if (status === 'queued' || status === 'running') return <LoaderCircle size={15} />;
  if (status === 'blocked') return <TriangleAlert size={15} />;
  if (status === 'handed_off') return <GitBranch size={15} />;
  if (status === 'aborted' || status === 'waiting' || status.startsWith('waiting_') || status === 'idle') return <CircleDashed size={15} />;
  return <Check size={15} />;
}

function RoomActivityIcon({ status }: { status: RoomActivityProjection['status'] }) {
  if (status === 'running') return <LoaderCircle size={14} />;
  if (status === 'waiting' || status === 'aborted') return <CircleDashed size={14} />;
  if (status === 'failed') return <TriangleAlert size={14} />;
  return <Check size={14} />;
}

function RoomWorkIcon({ work }: { work: RoomWorkItem }) {
  if (work.state === 'queued' || work.state === 'active' || work.state === 'review') return <LoaderCircle size={14} />;
  if (work.state === 'blocked' || work.state === 'failed') return <TriangleAlert size={14} />;
  if (work.state === 'cancelled') return <CircleDashed size={14} />;
  return <Check size={14} />;
}

interface RoomStatusActivityGroup {
  key: string;
  activity: RoomActivityProjection;
  count: number;
}

function collapseRoomStatusActivities(
  activities: RoomActivityProjection[],
): RoomStatusActivityGroup[] {
  const groups: RoomStatusActivityGroup[] = [];
  for (const activity of activities) {
    const toolCallId = text(activity.payload.toolCallId);
    const fingerprint = toolCallId
      ? [activity.participantId, 'tool', toolCallId].join(':')
      : [
          activity.participantId,
          activity.kind,
          text(activity.payload.activityKind),
          roomActivitySummary(activity.summary, activity.kind),
        ].join(':');
    const previous = groups.at(-1);
    if (previous?.key.startsWith(`${fingerprint}:`)) {
      previous.activity = activity;
      previous.count += 1;
      continue;
    }
    groups.push({
      key: `${fingerprint}:${groups.length}`,
      activity,
      count: 1,
    });
  }
  return groups;
}

function roomActivityIsAutomaticPolicyAuthorization(
  activity: RoomActivityProjection,
): boolean {
  const sourceEventType = text(activity.payload.sourceEventType);
  return Boolean(text(activity.payload.approvalId))
    && activity.payload.automatic === true
    && text(activity.payload.decisionMode || activity.payload.mode) === 'policy'
    && ['approval_required', 'approval_resolved'].includes(sourceEventType)
    && activity.status !== 'failed';
}

function roomStatusActivityPresentation(
  activity: RoomActivityProjection,
): { title: string; detail: string } {
  const sourceEventType = text(activity.payload.sourceEventType);
  const summary = roomActivitySummary(activity.summary, activity.kind);
  if (sourceEventType.startsWith('tool_')) {
    const tool = publicToolName(
      text(activity.payload.toolName),
      text(activity.payload.displayName),
    );
    const status = roomActivityStatus(activity);
    const stateLabel = {
      running: '正在处理',
      waiting: '等待处理',
      failed: '未完成',
      aborted: '已停止',
      completed: '已返回',
    }[status];
    return {
      title: tool,
      detail: summary === '协作进度已经更新' || summary === tool
        ? `${tool}${stateLabel}`
        : summary,
    };
  }
  if (activity.kind === 'route_decision') {
    return { title: '任务已分派', detail: summary };
  }
  if (text(activity.payload.activityKind) === 'intercom') {
    return { title: '伙伴沟通', detail: summary };
  }
  if (text(activity.payload.approvalId)) {
    return { title: '安全审批', detail: summary };
  }
  if (activity.kind === 'participant_status') {
    return { title: '状态同步', detail: summary };
  }
  return { title: '进展更新', detail: summary };
}

function roomActivityStatus(
  activity: RoomActivityProjection,
): RoomActivityProjection['status'] {
  if (['completed', 'failed', 'aborted'].includes(activity.status)) {
    return activity.status;
  }
  const automatic = activity.payload.automatic === true
    || text(activity.payload.decisionMode) === 'model'
    || text(activity.payload.mode) === 'model';
  const decision = text(activity.payload.decision)
    || text((activity.payload.approvalModelDecision as Record<string, unknown> | undefined)?.decision);
  const state = text(activity.payload.resolutionState || activity.payload.state);
  const decisionMode = text(activity.payload.decisionMode || activity.payload.mode);
  if (automatic && decisionMode === 'policy') return 'completed';
  if (
    automatic
    && !decision
    && !['approved', 'rejected', 'applied', 'resolved', 'cancelled'].includes(state)
  ) return 'running';
  return activity.status;
}

function roomActivityStatusLabel(status: RoomActivityProjection['status']): string {
  return {
    running: '进行中',
    waiting: '等待处理',
    failed: '未完成',
    aborted: '已停止',
    completed: '完成',
  }[status];
}

function roomWorkPriority(state: RoomWorkItem['state']): number {
  return {
    active: 0,
    review: 1,
    blocked: 2,
    queued: 3,
    failed: 4,
    done: 5,
    cancelled: 6,
  }[state];
}

function roomProjectedStatus(
  turn: RoomTurnProjection | undefined,
  activities: RoomActivityProjection[],
  messages: RoomMessageProjection[],
): RoomProjectedStatus {
  if (!turn) return 'idle';
  const pending = [...activities].reverse().find(roomActivityNeedsSessionAction);
  if (pending) {
    const requestKind = text(pending.payload.requestKind);
    if (
      Boolean(text(pending.payload.approvalId))
    ) return 'waiting_approval';
    if (
      requestKind === 'plan_review'
      || requestKind === 'memory_review'
    ) return 'waiting_review';
    if (text(pending.payload.method) === 'select' || Array.isArray(pending.payload.options)) {
      return 'waiting_select';
    }
    return 'waiting_input';
  }
  if (turn.status === 'failed') return 'blocked';
  if (turn.status === 'aborted') return 'aborted';
  if (turn.status === 'queued') return 'queued';
  if (turn.status === 'running') return 'running';
  const outcome = messages.reduce((kind, message) => (
    ['result', 'handoff', 'wait', 'blocked'].includes(message.postKind ?? '')
      ? message.postKind ?? kind
      : kind
  ), '');
  if (outcome === 'result') return 'completed';
  if (outcome === 'handoff') return 'handed_off';
  if (outcome === 'wait') return 'waiting';
  if (outcome === 'blocked') return 'blocked';
  return 'completed';
}

function roomProjectedStatusLabel(status: RoomProjectedStatus): string {
  return {
    queued: '等待协作',
    running: '协作中',
    waiting_approval: '等待审批',
    waiting_review: '等待审阅',
    waiting_select: '等待选择',
    waiting_input: '等待回答',
    blocked: '已阻塞',
    completed: '已完成',
    handed_off: '已转交',
    waiting: '等待继续',
    aborted: '已停止',
    idle: '等待后续',
  }[status];
}

function roomActivitySummary(summary: string, kind: string): string {
  const value = summary.trim();
  if (value && value !== kind && !/\b(?:participant|route|tool|turn)_[a-z_]+\b/i.test(value)) return value;
  if (kind === 'route_decision') return '已确定本轮负责角色';
  if (kind === 'participant_status') return '协作状态已经同步';
  return '协作进度已经更新';
}

function fileName(data: Record<string, unknown>): string {
  return text(data.fileName ?? data.name ?? data.title) || '未命名文件';
}

function text(value: unknown): string { return typeof value === 'string' ? value : ''; }
function pathName(path: string): string { return path.split('/').filter(Boolean).at(-1) ?? path; }
function roomWorkStateLabel(state: RoomWorkItem['state']): string {
  return {
    queued: '待接收',
    active: '执行中',
    review: '等待汇合',
    blocked: '已阻塞',
    done: '已完成',
    failed: '未完成',
    cancelled: '已取消',
  }[state];
}
