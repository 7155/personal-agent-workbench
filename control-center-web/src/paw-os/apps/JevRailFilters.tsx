import type { JevMissionTask } from '@/features/semantic-workspace/jev-mission';

export type JevRailFilter = 'all' | 'active' | 'attention' | 'ended';
const LABELS: Record<JevRailFilter, string> = { all: '全部', active: '进行', attention: '关注', ended: '结束' };
const ACTIVE = new Set(['planning', 'running', 'verifying', 'synthesizing', 'dispatching', 'dispatched', 'submitted', 'review', 'revising', 'reclaiming', 'reassigning']);
const ATTENTION = new Set(['returned', 'unknown', 'failed']);

/** A waiting prerequisite is not automatically a request for human intervention. */
export function matchesJevRailFilter(item: Pick<JevMissionTask, 'stage' | 'lane'>, filter: JevRailFilter): boolean {
  if (filter === 'active') return item.lane !== 'ended' && ACTIVE.has(item.stage);
  if (filter === 'attention') return ATTENTION.has(item.stage);
  if (filter === 'ended') return item.lane === 'ended' && item.stage !== 'superseded';
  return item.stage !== 'superseded';
}
export function JevRailFilters({ items, value, onChange }: {
  items: readonly JevMissionTask[]; value: JevRailFilter; onChange: (filter: JevRailFilter) => void;
}) {
  return <div className="paw-jev-rail-filters" role="group" aria-label="按任务状态筛选">
    {(Object.keys(LABELS) as JevRailFilter[]).map(filter => {
      const count = items.filter(item => matchesJevRailFilter(item, filter)).length;
      return <button type="button" key={filter} aria-pressed={value === filter}
        data-attention={filter === 'attention' && count > 0 || undefined}
        onClick={() => onChange(filter)} aria-label={`${LABELS[filter]}任务 ${count} 项`}>
        <span>{LABELS[filter]}</span><small>{count}</small>
      </button>;
    })}
  </div>;
}
