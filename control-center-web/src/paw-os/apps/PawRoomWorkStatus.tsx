import { useId, useMemo, useRef, useState } from 'react';
import { ArrowUpRight, ChevronDown, ChevronUp, CircleAlert, CircleCheck, Clock3, LoaderCircle, RefreshCw, WifiOff } from 'lucide-react';
import { usePageVisibility } from '@/platform/use-page-visibility';
import { PawRoomProgress } from './PawRoomProgress';
import { buildRoomVisualProgress, roomTextExcerpt, type RoomProgressBucket } from './room-visual-progress';
import { PawRoomAssignmentMap } from './PawRoomAssignmentMap';
import type { RoomFocusProjection } from './room-focus-projection';
import type { RoomWorkStatus } from './room-work-status';
import '../styles/paw-os-room-focus.css';
import '../styles/paw-os-room-progress.css';
import '../styles/paw-os-room-work-status.css';

export interface PawRoomWorkStatusProps {
  focus: RoomFocusProjection;
  status: RoomWorkStatus;
  onOpenParticipant: (participantId: string) => void;
  onRetrySync: () => void;
  onAnswer: () => void;
}

/** In normal composer flow, not fixed over the OS Dock or the send button.
 * The hidden graph stays mounted so folding it preserves the reader's selection. */
export function PawRoomWorkStatus({ focus, status, onOpenParticipant, onRetrySync, onAnswer }: PawRoomWorkStatusProps) {
  const pageVisible = usePageVisibility();
  const animate = status.animate && status.live && pageVisible;
  const progress = useMemo(() => buildRoomVisualProgress(focus), [focus]);
  const [selection, setSelection] = useState<{ rootId: string; bucket?: RoomProgressBucket }>();
  const bucket = selection?.rootId === focus.goal.rootId ? selection.bucket : undefined;
  const [expanded, setExpanded] = useState(false);
  const [everExpanded, setEverExpanded] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const detailId = useId();
  const close = () => { setExpanded(false); trigger.current?.focus({ preventScroll: true }); };
  const toggle = () => { if (expanded) close(); else { setEverExpanded(true); setExpanded(true); } };
  const selectBucket = (next?: RoomProgressBucket) => {
    setSelection({ rootId: focus.goal.rootId, bucket: next });
    setEverExpanded(true);
    setExpanded(true);
  };
  const Icon = status.state === 'offline' ? WifiOff
    : ['blocked', 'failed', 'needs-input'].includes(status.state) ? CircleAlert
      : status.state === 'completed' ? CircleCheck : animate ? LoaderCircle : Clock3;
  const date = status.updatedAtMs > 0 && Number.isFinite(status.updatedAtMs)
    ? new Date(status.updatedAtMs) : undefined;
  const updated = date && Number.isFinite(date.getTime()) ? date : undefined;
  return <section aria-label="协作状态" className="paw-room-work-status" data-state={status.state} data-live={status.live}>
    <div className="paw-room-work-status__bar">
      <Icon aria-hidden="true" size={17} className={animate ? 'paw-room-work-status__spin' : undefined} />
      <div className="paw-room-work-status__copy">
        <strong role="status" aria-live="polite" aria-atomic="true">{status.headline}</strong>
        <span title={status.detail}>{roomTextExcerpt(status.detail, 120)}</span>
      </div>
      <div className="paw-room-work-status__actions">
        {status.action === 'sync' && status.state === 'offline' ? <button type="button" onClick={onRetrySync}><RefreshCw size={14} aria-hidden="true" />重新同步</button> : null}
        {status.action === 'answer' ? <button type="button" onClick={onAnswer}><ArrowUpRight size={14} aria-hidden="true" />回答问题</button> : null}
        <button type="button" ref={trigger} aria-controls={detailId} aria-expanded={expanded} onClick={toggle}>
          {expanded ? <ChevronUp size={15} aria-hidden="true" /> : <ChevronDown size={15} aria-hidden="true" />}
          {expanded ? '收起任务' : '展开任务'}
        </button>
      </div>
    </div>
    <PawRoomProgress progress={progress} live={status.live} executing={animate && progress.leaves.some((task) => task.state === 'running' && Boolean(task.ownerParticipantId && status.executingParticipantIds.includes(task.ownerParticipantId)))} selected={bucket} controlsId={detailId} onSelect={selectBucket} />
    <div id={detailId} role="region" aria-label="协作任务详情" hidden={!expanded} className="paw-room-work-status__detail" onKeyDown={(event) => {
      if (event.key === 'Escape') { event.stopPropagation(); close(); }
    }}>
      {everExpanded ? <PawRoomAssignmentMap key={focus.goal.rootId} focus={focus} live={status.live && expanded} executingParticipantIds={animate ? status.executingParticipantIds : []} progressBucket={bucket} onClearProgressBucket={() => selectBucket(undefined)} freshness={status.live ? 'live' : status.state === 'offline' ? 'offline' : status.state === 'syncing' ? 'recovering' : status.state === 'paused-view' ? 'paused' : 'last-known'} onOpenParticipant={onOpenParticipant} /> : null}
      <div className="paw-room-work-status__meta">
        <span>{progress.aggregates.length ? `另有 ${progress.aggregates.length} 项汇总与收束` : '点击状态筛选实际任务'}</span>
        {updated ? <time dateTime={updated.toISOString()} title={updated.toLocaleString()}>{status.live ? '最近回执 ' : '上次回执 '}{updated.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</time> : <span>尚无回执时间</span>}
      </div>
    </div>
  </section>;
}
