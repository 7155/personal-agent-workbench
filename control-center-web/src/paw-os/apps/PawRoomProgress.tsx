import { useId } from 'react';
import { Circle, CircleCheck, CircleHelp, CirclePause, CircleX, Clock3, LoaderCircle, ShieldCheck, TriangleAlert } from 'lucide-react';
import { usePageVisibility } from '@/platform/use-page-visibility';
import type { RoomProgressBucket, RoomVisualProgress } from './room-visual-progress';

export function RoomTaskStateIcon({ bucket, spinning = false }: { bucket: RoomProgressBucket; spinning?: boolean }) {
  const Icon = { completed: CircleCheck, review: ShieldCheck, working: spinning ? LoaderCircle : Clock3,
    blocked: TriangleAlert, failed: CircleX, stopped: CirclePause, pending: Circle, unknown: CircleHelp }[bucket];
  return <Icon size={14} aria-hidden="true" className={spinning ? 'paw-room-task-spin' : undefined} />;
}

export interface PawRoomProgressProps {
  progress: RoomVisualProgress;
  live: boolean;
  /** Only a current Runtime dispatch can animate a working task. */
  executing?: boolean;
  selected?: RoomProgressBucket;
  controlsId?: string;
  onSelect: (bucket: RoomProgressBucket | undefined) => void;
}

/** A small count and keyboard-operable status filters, not an estimated time bar. */
export function PawRoomProgress({ progress, live, executing = false, selected, controlsId, onSelect }: PawRoomProgressProps) {
  const descriptionId = useId();
  const pageVisible = usePageVisibility();
  const valueText = `${progress.completed} / ${progress.total} 个执行项完成；${progress.segments.filter((part) => part.count).map((part) => `${part.label} ${part.count}`).join('，')}。不是耗时进度。`;
  return <div className="paw-room-progress" data-live={live}>
    <div className="paw-room-progress__summary" title={`${progress.explanation} ${progress.accepted} 项已验收。`}>
      {progress.fraction !== null ? <span role="progressbar" aria-label="当前执行项完成数量"
        aria-valuemin={0} aria-valuemax={progress.total} aria-valuenow={progress.completed}
        aria-valuetext={valueText} aria-describedby={descriptionId}>
        执行项完成 {progress.completed} / {progress.total}
      </span> : <span>{progress.total ? '任务待补全' : '暂无工作项'}</span>}
      {!live ? <small>上次记录</small> : null}
    </div>
    <div className="paw-room-progress__legend" aria-label="按执行项状态筛选">
      {progress.segments.filter((part) => part.count || selected === part.key).map((part) => <button key={part.key} type="button"
        aria-controls={controlsId} aria-pressed={selected === part.key} data-bucket={part.key} onClick={() => onSelect(selected === part.key ? undefined : part.key)}
        aria-label={`查看${part.label}执行项，${part.count} 项`}>
        <RoomTaskStateIcon bucket={part.key} spinning={part.key === 'working' && part.count > 0 && executing && live && pageVisible} />
        {part.label}<b>{part.count}</b>
      </button>)}
      {selected ? <button type="button" onClick={() => onSelect(undefined)}>全部任务</button> : null}
    </div>
    <p id={descriptionId} className={progress.incomplete ? 'paw-room-progress__explanation' : 'paw-room-progress__sr-only'}>{progress.explanation}</p>
  </div>;
}
