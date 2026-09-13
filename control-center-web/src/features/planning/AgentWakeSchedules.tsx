import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  CalendarClock,
  CirclePlay,
  History,
  Pencil,
  GitPullRequest,
  ExternalLink,
  Pause,
  Plus,
  RefreshCw,
  RotateCcw,
  X,
} from 'lucide-react';
import { useMemo, useRef, useState, type ReactNode } from 'react';
import { useControlTransport } from '@/app/control-transport';
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  EmptyState,
  Field,
  IconButton,
  Input,
  Select,
  TextArea,
} from '@/components/primitives';
import { roleItems, sessionItems } from '@/features/agent/types';
import { openPawOsRoute, usePawOsDesktop } from '@/features/paw-os/surface-context';
import {
  InlineNotice,
  ManagementSection,
  QueryState,
  StatusBadge,
  arrayRecords,
  asRecord,
  numberValue,
  publicErrorText,
  stringValue,
  type JsonRecord,
} from '@/features/overview/management-ui';
import './planning.css';

type TargetType = 'session' | 'role';
type RecurrenceKind = 'once' | 'daily' | 'weekly';
type ScheduleAction = 'pause' | 'resume' | 'cancel' | 'retry';

const scheduleQueryKey = ['planning', 'agent-wake-schedules'] as const;

export function AgentWakeSchedules({ embedded = false, tasks, search = '', statusFilter = 'all', initialHistoryId = '' }: { embedded?: boolean; tasks: readonly JsonRecord[]; search?: string; statusFilter?: string; initialHistoryId?: string }) {
  const transport = useControlTransport();
  const desktop = usePawOsDesktop();
  const queryClient = useQueryClient();
  const createTriggerRef = useRef<HTMLButtonElement>(null);
  const editTriggerRef = useRef<HTMLButtonElement | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [editingId, setEditingId] = useState('');
  const [template, setTemplate] = useState<'custom' | 'github'>('custom');
  const [prUrl, setPrUrl] = useState('');
  const [historyId, setHistoryId] = useState(initialHistoryId);
  const [title, setTitle] = useState('');
  const [instruction, setInstruction] = useState('');
  const [targetType, setTargetType] = useState<TargetType>('session');
  const [targetId, setTargetId] = useState('');
  const [wakeAt, setWakeAt] = useState(() => futureLocalDateTime(30));
  const [recurrence, setRecurrence] = useState<RecurrenceKind>('once');
  const [recurrenceInterval, setRecurrenceInterval] = useState(1);
  const [maxRuns, setMaxRuns] = useState(7);
  const [planningTaskId, setPlanningTaskId] = useState('');
  const localTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai';
  const [timezone, setTimezone] = useState(localTimezone);

  const catalog = useQuery({
    queryKey: ['planning', 'agent-wake-catalog'],
    queryFn: async ({ signal }) => {
      const [sessionsResponse, rolesResponse] = await Promise.all([
        transport.request({ pathId: 'agent.sessions.list', query: { limit: 200 }, signal }),
        transport.request({ pathId: 'agent.roles.list', signal }),
      ]);
      return {
        sessions: sessionItems(sessionsResponse).filter((item) => item.status !== 'archived'),
        roles: roleItems(rolesResponse).filter((item) => item.selectableModes.includes('assistant')),
      };
    },
  });
  const schedules = useQuery({
    queryKey: scheduleQueryKey,
    queryFn: ({ signal }) => transport.request({
      pathId: 'agent.wakeSchedules.list',
      query: { limit: 500 },
      signal,
    }),
    refetchInterval: 15_000,
  });
  const history = useQuery({
    enabled: Boolean(historyId),
    queryKey: [...scheduleQueryKey, historyId, 'runs'],
    queryFn: ({ signal }) => transport.request({
      pathId: 'agent.wakeSchedule.runs',
      params: { scheduleId: historyId },
      query: { limit: 100 },
      signal,
    }),
  });
  const createSchedule = useMutation({
    mutationKey: [...scheduleQueryKey, 'create'],
    mutationFn: () => {
      const role = catalog.data?.roles.find((item) => item.roleId === targetId);
      const original = arrayRecords(asRecord(schedules.data).items).find((item) => item.id === editingId);
      const definition = {
          title: title.trim(),
          instruction: template === 'github' ? githubPrInstruction(prUrl, instruction) : instruction.trim(),
          targetType,
          targetSessionId: targetType === 'session' ? targetId : '',
          targetRoleId: targetType === 'role' ? targetId : '',
          targetRoleVersion: targetType === 'role' ? (original?.targetRoleId === targetId ? stringValue(original.targetRoleVersion, role?.version ?? '1') : role?.version ?? '1') : '',
          wakeAtMs: new Date(wakeAt).getTime(),
          timezone,
          recurrenceKind: recurrence,
          recurrenceInterval,
          maxRuns: recurrence === 'once' ? 1 : maxRuns,
          planningTaskId,
        };
      return transport.request(editingId ? { pathId: 'agent.wakeSchedule.action', params: { scheduleId: editingId }, body: { action: 'edit', confirmText: 'apply', schedule: definition } } : { pathId: 'agent.wakeSchedules.create', body: { ...definition, confirmText: 'schedule' } });
    },
    onSuccess: async () => {
      setCreateOpen(false);
      await queryClient.invalidateQueries({ queryKey: scheduleQueryKey });
    },
  });
  const changeSchedule = useMutation({
    mutationKey: [...scheduleQueryKey, 'action'],
    mutationFn: ({ action, scheduleId }: { action: ScheduleAction; scheduleId: string }) => transport.request({
      pathId: 'agent.wakeSchedule.action',
      params: { scheduleId },
      body: { action, confirmText: 'apply' },
    }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: scheduleQueryKey });
    },
  });

  const allItems = arrayRecords(asRecord(schedules.data).items).filter((item) => stringValue(asRecord(item.metadata).kind) !== 'room_partner_completion');
  const items = allItems.filter((item) => (statusFilter === 'all' || stringValue(item.status) === statusFilter) && `${stringValue(item.title)} ${stringValue(item.instruction)}`.toLocaleLowerCase().includes(search.toLocaleLowerCase()));
  const targetOptions = targetType === 'session'
    ? (catalog.data?.sessions ?? []).map((item) => ({ value: item.id, label: item.title }))
    : (catalog.data?.roles ?? []).map((item) => ({ value: item.roleId, label: item.displayName }));
  const taskOptions = useMemo(() => [
    { value: '', label: '不关联任务' },
    ...tasks
      .filter((task) => !['done', 'completed', 'cancelled'].includes(stringValue(task.status)))
      .map((task) => ({ value: stringValue(task.id), label: stringValue(task.title, '未命名任务') })),
  ], [tasks]);
  const selectedHistory = allItems.find((item) => stringValue(item.id) === historyId);
  const createBlocked = (template !== 'github' && !instruction.trim())
    || (template === 'github' && !isGithubPrUrl(prUrl))
    || !title.trim()
    || !targetId
    || !wakeAt
    || !Number.isInteger(maxRuns) || maxRuns < 1 || maxRuns > 100
    || !Number.isInteger(recurrenceInterval) || recurrenceInterval < 1 || recurrenceInterval > 30
    || Boolean(editingId && maxRuns <= numberValue(allItems.find((item) => item.id === editingId)?.runCount))
    || !Number.isFinite(new Date(wakeAt).getTime())
    || new Date(wakeAt).getTime() < Date.now() + 1_000;

  function beginCreate(kind: 'custom' | 'github' = 'custom') {
    const defaultSession = catalog.data?.sessions[0]?.id ?? '';
    const defaultRole = catalog.data?.roles[0]?.roleId ?? '';
    setEditingId(''); setTemplate(kind); setPrUrl('');
    setTimezone(localTimezone);
    setTitle(kind === 'github' ? '跟进 GitHub PR' : '');
    setInstruction('');
    setTargetType(defaultSession ? 'session' : 'role');
    setTargetId(defaultSession || defaultRole);
    setWakeAt(futureLocalDateTime(30));
    setRecurrence(kind === 'github' ? 'daily' : 'once');
    setRecurrenceInterval(1);
    setMaxRuns(7);
    setPlanningTaskId('');
    createSchedule.reset();
    setCreateOpen(true);
  }

  function beginEdit(schedule: JsonRecord) {
    setEditingId(stringValue(schedule.id)); setTemplate('custom'); setPrUrl('');
    setTitle(stringValue(schedule.title)); setInstruction(stringValue(schedule.instruction));
    setTimezone(stringValue(schedule.timezone, localTimezone));
    setTargetType(stringValue(schedule.targetType) as TargetType);
    setTargetId(stringValue(schedule.targetType) === 'role' ? stringValue(schedule.targetRoleId) : stringValue(schedule.targetSessionId));
    setWakeAt(localDateTime(Math.max(numberValue(schedule.nextWakeAtMs), Date.now() + 60_000)));
    setRecurrence(stringValue(schedule.recurrenceKind) as RecurrenceKind); setRecurrenceInterval(numberValue(schedule.recurrenceInterval, 1));
    setMaxRuns(numberValue(schedule.maxRuns, 1)); setPlanningTaskId(stringValue(schedule.planningTaskId));
    createSchedule.reset(); setCreateOpen(true);
  }

  function openSession(id: string, name: string) {
    if (desktop) desktop.openWindow({ appId: 'agent', target: { kind: 'session', id, title: name } });
    else openPawOsRoute(null, `/agent?sessionId=${encodeURIComponent(id)}`);
  }

  function selectTargetType(next: string) {
    const value = next as TargetType;
    setTargetType(value);
    setTargetId(value === 'session'
      ? catalog.data?.sessions[0]?.id ?? ''
      : catalog.data?.roles[0]?.roleId ?? '');
  }

  function selectTask(taskId: string) {
    setPlanningTaskId(taskId);
    const task = tasks.find((item) => stringValue(item.id) === taskId);
    if (!task) return;
    const taskTitle = stringValue(task.title);
    const taskDetail = stringValue(task.detail);
    if (!title.trim()) setTitle(taskTitle);
    if (!instruction.trim()) setInstruction(taskDetail || `完成任务《${taskTitle}》，并说明结果。`);
  }

  function requestAction(action: ScheduleAction, scheduleId: string) {
    changeSchedule.mutate({ action, scheduleId });
  }

  return (
    <PlanningWakeSurface
      embedded={embedded}
      createAction={(
        <div className="planning-wake-create-actions"><Button size="small" leadingIcon={<GitPullRequest size={15} />} disabled={!catalog.data} onClick={() => beginCreate('github')}>跟进 GitHub PR</Button><Button
          ref={createTriggerRef}
          disabled={catalog.isPending || Boolean(catalog.error)}
          leadingIcon={<Plus size={15} />}
          onClick={() => beginCreate()}
          size="small"
          variant="primary"
        >
          添加安排
        </Button></div>
      )}
    >
      {asRecord(schedules.data).schedulerActive === false ? <InlineNotice title="调度器未运行" tone="warning">安排已保存；本机 Agent 服务运行后才能按时执行。</InlineNotice> : null}
      {catalog.isPending ? (
        <InlineNotice title="正在读取可安排的对话与伙伴" tone="info">
          已有安排仍可查看和管理；读取完成后即可添加新的安排。
        </InlineNotice>
      ) : null}
      {catalog.error ? (
        <div className="planning-wake-catalog-state">
          <InlineNotice title="暂时无法读取可安排的对话与伙伴" tone="danger">
            已有安排仍可查看和管理；重新读取后才能添加新的安排。
          </InlineNotice>
          <Button
            loading={catalog.isFetching}
            onClick={() => void catalog.refetch()}
          >
            重新读取对象
          </Button>
        </div>
      ) : null}
      <QueryState
        error={schedules.error as Error | null}
        headingLevel={3}
        isPending={schedules.isPending}
        onRetry={() => void schedules.refetch()}
      >
        {items.length ? (
          <div className="planning-wake-list">
            {items.map((schedule) => {
              const id = stringValue(schedule.id);
              const status = stringValue(schedule.status);
              const scheduleTitle = stringValue(schedule.title, '未命名安排');
              const latestRun = asRecord(schedule.latestRun);
              return (
                <article className="planning-wake-row" key={id}>
                  <div className="planning-wake-row__copy">
                    <div className="planning-wake-row__title">
                      <strong>{scheduleTitle}</strong>
                      <StatusBadge label={scheduleStatusLabel(status)} tone={scheduleStatusTone(status)} />
                    </div>
                    <details className="planning-wake-instruction"><summary>{stringValue(schedule.instruction).split('\n')[0]}</summary><p>{stringValue(schedule.instruction)}</p></details>
                    <span>
                      {targetLabel(schedule, catalog.data?.sessions ?? [], catalog.data?.roles ?? [])}
                      {' · '}{recurrenceLabel(schedule)}
                      {' · '}{nextWakeLabel(schedule)}
                    </span>
                    {stringValue(schedule.lastError) ? <em>{stringValue(schedule.lastError)}</em> : null}
                    {stringValue(latestRun.sessionId) ? <span>最近执行：{runStateLabel(stringValue(latestRun.state))}</span> : null}
                  </div>
                  <div className="planning-wake-row__actions">
                    {['scheduled', 'paused'].includes(status) ? <IconButton icon={<Pencil size={16} />} label="编辑安排" onClick={(event) => { editTriggerRef.current = event.currentTarget; beginEdit(schedule); }} size="small" tooltip /> : null}
                    <IconButton icon={<History size={16} />} label="查看执行记录" onClick={() => setHistoryId(id)} size="small" tooltip />
                    {status === 'scheduled' ? <IconButton disabled={changeSchedule.isPending} icon={<Pause size={16} />} label="暂停自动执行" onClick={() => requestAction('pause', id)} size="small" tooltip /> : null}
                    {status === 'paused' ? <IconButton disabled={changeSchedule.isPending} icon={<CirclePlay size={16} />} label="恢复自动执行" onClick={() => requestAction('resume', id)} size="small" tooltip /> : null}
                    {['failed', 'completed'].includes(status) ? <IconButton disabled={changeSchedule.isPending} icon={<RotateCcw size={16} />} label="再做一次" onClick={() => requestAction('retry', id)} size="small" tooltip /> : null}
                    {['scheduled', 'paused', 'failed'].includes(status) ? <IconButton disabled={changeSchedule.isPending} icon={<X size={16} />} label="取消这项安排" onClick={() => requestAction('cancel', id)} size="small" tooltip /> : null}
                  </div>
                </article>
              );
            })}
          </div>
        ) : <EmptyState description={allItems.length ? '试试其他关键词或状态。' : '设好时间后，伙伴会自动继续，并留下每次的执行结果。'} headingLevel={3} icon={CalendarClock} title={allItems.length ? '没有符合条件的安排' : '还没有定时安排'} />}
        {changeSchedule.error ? <InlineNotice title="安排未更新" tone="danger">{publicErrorText(changeSchedule.error)}</InlineNotice> : null}
      </QueryState>

      <Dialog open={createOpen} onOpenChange={(open) => { if (!createSchedule.isPending) setCreateOpen(open); }}>
        <DialogContent
          className="planning-dialog planning-wake-dialog"
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            (editingId ? editTriggerRef.current : createTriggerRef.current)?.focus();
          }}
        >
          <DialogHeader>
            <DialogTitle>{editingId ? '编辑定时安排' : template === 'github' ? '定时跟进 GitHub PR' : '添加定时安排'}</DialogTitle>
            <DialogDescription>写下要做的事、在哪里继续以及开始时间。保存后可以暂停、取消或查看每次结果。</DialogDescription>
          </DialogHeader>
          <div className="planning-wake-form">
            {template === 'github' ? <Field className="planning-wake-form__wide" htmlFor="planning-wake-pr" label="GitHub PR 地址" required><Input id="planning-wake-pr" type="url" placeholder="https://github.com/owner/repository/pull/123" value={prUrl} onChange={(event) => setPrUrl(event.target.value)} /><small>检查 CI、Review 和合并状态；只在有变化或需要你处理时报告。</small></Field> : null}
            <Field htmlFor="planning-wake-title" label="安排名称" required>
              <Input autoFocus id="planning-wake-title" maxLength={120} onChange={(event) => setTitle(event.target.value)} placeholder="例如：明早整理本周任务" value={title} />
            </Field>
            <Field htmlFor="planning-wake-task" label="关联已有任务">
              <Select id="planning-wake-task" onValueChange={selectTask} options={taskOptions} value={planningTaskId} />
            </Field>
            <Field className="planning-wake-form__wide" htmlFor="planning-wake-instruction" label={template === 'github' ? '额外关注点（可选）' : '到点后做什么'} required={template !== 'github'}>
              <TextArea id="planning-wake-instruction" maxLength={8_000} onChange={(event) => setInstruction(event.target.value)} placeholder="写清任务、完成标准，以及失败时要报告什么。" rows={5} value={instruction} />
            </Field>
            <Field htmlFor="planning-wake-target-type" label="在哪里继续">
              <Select id="planning-wake-target-type" onValueChange={selectTargetType} options={[
                { value: 'session', label: '在现有对话里继续' },
                { value: 'role', label: '请一位伙伴开始' },
              ]} value={targetType} />
            </Field>
            <Field htmlFor="planning-wake-target" label={targetType === 'session' ? '继续哪段对话' : '请哪位伙伴'} required>
              <Select id="planning-wake-target" onValueChange={setTargetId} options={targetOptions} placeholder={targetType === 'session' ? '选择一段对话' : '选择一位伙伴'} value={targetId} />
            </Field>
            <Field description={timezone === localTimezone ? `使用 ${localTimezone} 本地时间。` : `开始时间按本机 ${localTimezone} 输入；重复周期保留原时区 ${timezone}。`} htmlFor="planning-wake-at" label="开始时间" required>
              <Input id="planning-wake-at" min={futureLocalDateTime(1)} onChange={(event) => setWakeAt(event.target.value)} type="datetime-local" value={wakeAt} />
            </Field>
            <Field htmlFor="planning-wake-recurrence" label="多久做一次">
              <Select id="planning-wake-recurrence" onValueChange={(value) => setRecurrence(value as RecurrenceKind)} options={[
                { value: 'once', label: '只执行一次' },
                { value: 'daily', label: '每天同一时间' },
                { value: 'weekly', label: '每周同一时间' },
              ]} value={recurrence} />
            </Field>
            {recurrence !== 'once' ? (
              <Field htmlFor="planning-wake-interval" label={recurrence === 'daily' ? '每隔几天' : '每隔几周'}><Input id="planning-wake-interval" max={30} min={1} type="number" value={recurrenceInterval} onChange={(event) => setRecurrenceInterval(Number(event.target.value))} /></Field>
            ) : null}
            {recurrence !== 'once' ? (
              <Field description={editingId ? `累计已执行 ${numberValue(allItems.find((item) => item.id === editingId)?.runCount)} 次；总次数需更大，最多 100 次。` : '达到次数后自动结束，最多 100 次。'} htmlFor="planning-wake-max-runs" label="最多执行次数">
                <Input id="planning-wake-max-runs" max={100} min={1} onChange={(event) => setMaxRuns(Number(event.target.value))} type="number" value={maxRuns} />
              </Field>
            ) : null}
          </div>
          <InlineNotice title="保存后会发生什么" tone="warning">
            到点后会自动发起一轮真实对话。若选中的对话正在忙，本次安排会顺延一分钟，不会打断正在进行的工作。
          </InlineNotice>
          {createSchedule.error ? <InlineNotice title="安排未保存" tone="danger">{publicErrorText(createSchedule.error)}</InlineNotice> : null}
          <DialogFooter>
            <Button disabled={createSchedule.isPending} onClick={() => setCreateOpen(false)} variant="quiet">先不安排</Button>
            <Button disabled={createBlocked} leadingIcon={<CalendarClock size={16} />} loading={createSchedule.isPending} onClick={() => createSchedule.mutate()} variant="primary">保存安排</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={Boolean(historyId)} onOpenChange={(open) => { if (!open) setHistoryId(''); }}>
        <DialogContent className="planning-dialog planning-wake-history-dialog">
          <DialogHeader>
            <DialogTitle>执行记录 · {stringValue(selectedHistory?.title, '定时安排')}</DialogTitle>
            <DialogDescription>每次开始、接手、完成、失败或顺延都会保留在这里。</DialogDescription>
          </DialogHeader>
          <QueryState error={history.error as Error | null} headingLevel={3} isPending={history.isPending} onRetry={() => void history.refetch()}>
            {arrayRecords(asRecord(history.data).items).length ? (
              <div className="planning-wake-history">
                {arrayRecords(asRecord(history.data).items).map((run) => (
                  <div className="planning-wake-history__row" key={stringValue(run.id)}>
                    <div>
                      <strong>第 {numberValue(run.attempt)} 次 · {runStateLabel(stringValue(run.state))}</strong>
                      <span>计划时间：{formatTimestamp(numberValue(run.dueAtMs))}</span>
                      {numberValue(run.finishedAtMs) ? <span>结束时间：{formatTimestamp(numberValue(run.finishedAtMs))}</span> : null}
                      {stringValue(run.error) ? <em>{stringValue(run.error)}</em> : null}
                    </div>
                    <StatusBadge label={runStateLabel(stringValue(run.state))} tone={runStateTone(stringValue(run.state))} />
                    {stringValue(run.sessionId) ? <Button size="small" leadingIcon={<ExternalLink size={14} />} onClick={() => openSession(stringValue(run.sessionId), stringValue(selectedHistory?.title, '定时任务结果'))}>打开执行对话</Button> : null}
                  </div>
                ))}
              </div>
            ) : <EmptyState description="这项安排开始执行后，过程和结果会留在这里。" headingLevel={3} icon={History} title="还没有执行记录" />}
          </QueryState>
          <DialogFooter>
            <Button leadingIcon={<RefreshCw size={15} />} onClick={() => void history.refetch()}>刷新</Button>
            <Button onClick={() => setHistoryId('')} variant="primary">完成查看</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

    </PlanningWakeSurface>
  );
}

function PlanningWakeSurface({
  children,
  createAction,
  embedded,
}: {
  children: ReactNode;
  createAction: ReactNode;
  embedded: boolean;
}) {
  if (embedded) {
    return (
      <section className="planning-wake-embedded">
        <header>
          <div><h2>定时安排</h2><p>让一段对话或一位伙伴在指定时间继续做事；每次执行都会留下结果。</p></div>
          {createAction}
        </header>
        {children}
      </section>
    );
  }
  return (
    <ManagementSection
      description="让一段对话或一位伙伴在指定时间继续做事；每次执行都会留下结果。"
      title="定时安排"
      trailing={createAction}
    >
      {children}
    </ManagementSection>
  );
}

function futureLocalDateTime(minutes: number): string {
  return localDateTime(Date.now() + minutes * 60_000);
}

function localDateTime(timestamp: number): string {
  const value = new Date(timestamp);
  const local = new Date(value.getTime() - value.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

export function isGithubPrUrl(value: string): boolean {
  try {
    const url = new URL(value.trim());
    return url.protocol === 'https:' && url.hostname === 'github.com' && !url.username && !url.password && /^\/[^/]+\/[^/]+\/pull\/[1-9]\d*\/?$/u.test(url.pathname);
  } catch { return false; }
}

export function githubPrInstruction(url: string, extra: string): string {
  return `定时检查 GitHub PR：${url.trim()}\n检查 CI/checks、Review 结论、新增评论、冲突和合并状态，并与该任务上次执行记录比较。首次记录基线，之后仅在状态变化、检查失败、需要我处理或已合并/关闭时报告；没有变化时保持安静。每次执行留下可追溯的检查结果与 PR 链接，后续执行先读取上次结果；读取失败要说明原因，不推断成功。仅查看，不发布评论、不合并、不修改分支。${extra.trim() ? `\n额外关注点：${extra.trim()}` : ''}`;
}

function formatTimestamp(timestamp: number): string {
  if (!timestamp) return '暂无';
  return new Date(timestamp).toLocaleString('zh-CN', { hour12: false });
}

function scheduleStatusLabel(status: string): string {
  return ({ scheduled: '等待中', paused: '已暂停', running: '执行中', completed: '已完成', failed: '失败', cancelled: '已取消' } as Record<string, string>)[status] ?? '未知';
}

function scheduleStatusTone(status: string): 'success' | 'warning' | 'danger' | 'info' | 'neutral' {
  if (status === 'completed') return 'success';
  if (status === 'failed') return 'danger';
  if (status === 'running' || status === 'scheduled') return 'info';
  if (status === 'paused') return 'warning';
  return 'neutral';
}

function runStateLabel(status: string): string {
  return ({ claimed: '正在启动', accepted: '伙伴已接手', completed: '已完成', failed: '失败', deferred: '已顺延' } as Record<string, string>)[status] ?? '尚未运行';
}

function runStateTone(status: string): 'success' | 'warning' | 'danger' | 'info' | 'neutral' {
  if (status === 'completed') return 'success';
  if (status === 'failed') return 'danger';
  if (status === 'deferred') return 'warning';
  if (status === 'claimed' || status === 'accepted') return 'info';
  return 'neutral';
}

function recurrenceLabel(schedule: JsonRecord): string {
  const kind = stringValue(schedule.recurrenceKind);
  const runs = numberValue(schedule.runCount);
  const maxRuns = numberValue(schedule.maxRuns);
  const interval = numberValue(schedule.recurrenceInterval, 1);
  if (kind === 'daily') return `每 ${interval} 天 · ${runs}/${maxRuns} 次`;
  if (kind === 'weekly') return `每 ${interval} 周 · ${runs}/${maxRuns} 次`;
  return runs ? '单次 · 已运行' : '单次';
}

function nextWakeLabel(schedule: JsonRecord): string {
  const status = stringValue(schedule.status);
  if (status === 'paused') return '已暂停，恢复后继续';
  if (status === 'running') return '正在等待执行结果';
  if (['failed', 'completed', 'cancelled'].includes(status)) return '本次安排已结束';
  const next = numberValue(schedule.nextWakeAtMs);
  return next ? `下次 ${formatTimestamp(next)}` : '没有下次安排';
}

function targetLabel(
  schedule: JsonRecord,
  sessions: readonly { id: string; title: string }[],
  roles: readonly { roleId: string; displayName: string }[],
): string {
  if (stringValue(schedule.targetType) === 'role') {
    const roleId = stringValue(schedule.targetRoleId);
    return `伙伴：${roles.find((item) => item.roleId === roleId)?.displayName ?? '未命名伙伴'}`;
  }
  const sessionId = stringValue(schedule.targetSessionId);
  return `对话：${sessions.find((item) => item.id === sessionId)?.title || '原对话已不在列表中'}`;
}
