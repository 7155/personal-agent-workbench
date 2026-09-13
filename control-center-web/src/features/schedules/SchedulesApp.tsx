import { useQuery } from '@tanstack/react-query';
import { Bot, Brain, CalendarClock, ChevronRight, FlaskConical, Layers, RefreshCw, Search } from 'lucide-react';
import { lazy, Suspense, useEffect, useState } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { Button, EmptyState, IconButton, Input, Select } from '@/components/primitives';
import { useEvalSchedules } from '@/features/observability/api';
import { AgentWakeSchedules } from '@/features/planning/AgentWakeSchedules';
import { InlineNotice, publicErrorText } from '@/features/overview/management-ui';
import { MemorySchedules, useMemoryScheduleSources } from './MemorySchedules';
import { agentScheduleRows, evalScheduleRows, memoryScheduleRows, scheduleGroupLabels, scheduleStatusLabels, scheduleTime, type ScheduleGroup } from './schedule-model';
import './schedules.css';

const EvalSchedules = lazy(async () => ({ default: (await import('@/features/observability')).EvalSchedulesPanel }));
const groups = [ { id: 'all', label: '全部任务', icon: Layers }, { id: 'agent', label: 'Agent 安排', icon: Bot }, { id: 'eval', label: '周期评测', icon: FlaskConical }, { id: 'memory', label: '后台维护', icon: Brain } ] as const;
const icons = { agent: Bot, eval: FlaskConical, memory: Brain };
const emptyTasks: readonly Record<string, unknown>[] = [];

export function SchedulesApp({ initialRoute = '' }: { initialRoute?: string }) {
  const transport = useControlTransport();
  const [group, setGroup] = useState<ScheduleGroup | 'all'>(() => initialRoute.includes('view=agent') ? 'agent' : 'all');
  const [selectedId, setSelectedId] = useState('');
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState('all');
  const agent = useQuery({ queryKey: ['planning', 'agent-wake-schedules'], queryFn: ({ signal }) => transport.request({ pathId: 'agent.wakeSchedules.list', query: { limit: 500 }, signal }), refetchInterval: 15_000 });
  const evaluation = useEvalSchedules();
  const memory = useMemoryScheduleSources();
  useEffect(() => { if (initialRoute.includes('view=agent')) setGroup('agent'); }, [initialRoute]);
  const rows = [...agentScheduleRows(agent.data), ...evalScheduleRows(evaluation.data), ...memoryScheduleRows(memory.settings.data, memory.status.data)];
  const visible = rows.filter((item) => (statusFilter === 'all' || item.status === statusFilter) && `${item.title} ${item.detail} ${scheduleGroupLabels[item.group]}`.toLocaleLowerCase().includes(search.toLocaleLowerCase()))
    .sort((a, b) => (a.nextAt || Number.MAX_SAFE_INTEGER) - (b.nextAt || Number.MAX_SAFE_INTEGER));
  const sources = [ { title: 'Agent 安排', query: agent }, { title: '周期评测', query: evaluation }, { title: '后台维护设置', query: memory.settings } ];
  const pending = sources.some((source) => source.query.isPending);
  const refreshing = sources.some((source) => source.query.isFetching);
  function selectGroup(value: ScheduleGroup | 'all') { setGroup(value); setSelectedId(''); setSearch(''); setStatusFilter('all'); }
  return <div className="schedule-app" data-app-id="schedules">
    <header className="schedule-app__header"><span className="schedule-app__identity"><CalendarClock size={23} /><span><h1>定时任务</h1><p>把需要惦记的事，交给下一次执行。</p></span></span><IconButton label="刷新所有任务" icon={<RefreshCw size={17} className={refreshing ? 'ui-spin' : ''} />} disabled={refreshing} onClick={() => { void agent.refetch(); void evaluation.refetch(); void memory.settings.refetch(); void memory.status.refetch(); }} tooltip /></header>
    <nav className="schedule-app__nav" aria-label="任务类型">{groups.map((item) => <button key={item.id} aria-pressed={group === item.id} onClick={() => selectGroup(item.id)} type="button"><item.icon size={16} /><span>{item.label}</span><small>{pending ? '·' : item.id === 'all' ? rows.length : rows.filter((row) => row.group === item.id).length}</small></button>)}</nav>
    {group === 'all' || group === 'agent' ? <div className="schedule-app__filters"><label><Search size={16} /><Input aria-label="搜索定时任务" placeholder="搜索任务、PR 或关注点" value={search} onChange={(event) => setSearch(event.target.value)} /></label><Select aria-label="任务状态" value={statusFilter} onValueChange={setStatusFilter} options={[{ value: 'all', label: '所有状态' }, ...Object.entries(scheduleStatusLabels).map(([value, label]) => ({ value, label }))]} /></div> : null}
    <main className="schedule-app__content" key={group}>
      {group === 'all' ? <>
        <div className="schedule-app__overview"><span><strong>{rows.filter((row) => row.status === 'running').length}</strong> 正在执行<span className="schedule-app__separator">/</span><strong>{rows.filter((row) => row.status === 'scheduled').length}</strong> 等待执行</span><Button size="small" variant="primary" onClick={() => selectGroup('agent')}>安排新任务</Button></div>
        {sources.map((source) => source.query.error ? <InlineNotice key={source.title} title={`${source.title}暂时无法读取`} tone="danger">{publicErrorText(source.query.error)}<Button size="small" onClick={() => void source.query.refetch()}>重试</Button></InlineNotice> : null)}
        {pending ? <p className="schedule-muted" role="status">正在读取各类安排…</p> : null}
        {visible.length ? <div className="schedule-app__list" aria-label="统一定时任务列表">{visible.map((item) => {
          const Icon = icons[item.group];
          const terminal = ['completed', 'cancelled', 'paused', 'failed'].includes(item.status);
          return <button className="schedule-app__row" key={`${item.group}:${item.id}`} type="button" onClick={() => { setGroup(item.group); setSelectedId(item.id); setSearch(''); setStatusFilter('all'); }}>
            <span className="schedule-app__row-icon" data-group={item.group}><Icon size={19} /></span><span className="schedule-app__row-copy"><strong>{item.title}</strong><small>{scheduleGroupLabels[item.group]} · {item.cadence}</small>{item.error ? <em>{item.error}</em> : null}</span>
            <span className="schedule-app__row-time"><span className="schedule-state" data-state={item.status}><i />{scheduleStatusLabels[item.status] ?? '状态待确认'}</span><small>{item.status === 'running' ? '正在等待执行结果' : !terminal && item.nextAt ? `下次 ${scheduleTime(item.nextAt)}` : !terminal && item.group === 'memory' ? '到期且满足条件时执行' : '执行记录已保留'}</small></span><ChevronRight size={15} />
          </button>;
        })}</div> : !pending ? <EmptyState icon={CalendarClock} title={rows.length ? '没有匹配的任务' : '还没有任务安排'} description={rows.length ? '换个关键词或状态试试。' : '可以安排一次提醒、一段定时对话，或定期跟进 GitHub PR。'} /> : null}
        {rows.length >= 500 ? <p className="schedule-muted">Agent 列表显示最近 500 项，周期评测显示最近 100 项。</p> : null}
      </> : group === 'agent' ? <AgentWakeSchedules key={selectedId} embedded tasks={emptyTasks} search={search} statusFilter={statusFilter} initialHistoryId={selectedId} /> : group === 'eval' ? <Suspense fallback={<p role="status">正在打开周期评测…</p>}><EvalSchedules key={selectedId} initialScheduleId={selectedId} /></Suspense> : <MemorySchedules />}
    </main>
    <footer className="schedule-app__footer">本机服务运行时按计划执行 · 结果会留在任务记录中</footer>
  </div>;
}
