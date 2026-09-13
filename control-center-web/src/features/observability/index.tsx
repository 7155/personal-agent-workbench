import {
  Activity,
  Bot,
  Brain,
  Braces,
  ChevronDown,
  CircleDotDashed,
  Database,
  FileCheck2,
  FilterX,
  GitBranch,
  KeyRound,
  Link2,
  LockKeyhole,
  MessagesSquare,
  Network,
  Package,
  Radio,
  RefreshCw,
  Search,
  ShieldCheck,
  TimerReset,
  TriangleAlert,
  Wrench,
  type LucideIcon,
} from 'lucide-react';
import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Button, Disclosure, EmptyState, IconButton } from '@/components/primitives';
import type { ObservationEventV1 } from '@/contracts/generated/observation-event.v1';
import type { EvalRunV1 } from '@/contracts/generated/eval-run.v1';
import type { EvalScheduleListV1 } from '@/contracts/generated/eval-schedule-list.v1';
import type { ObservabilityEvidenceEvalRequestV1 } from '@/contracts/generated/observability-evidence-eval-request.v1';
import type { ObservabilityEvalListV1 } from '@/contracts/generated/observability-eval-list.v1';
import type { ObservabilityTraceGetV1 } from '@/contracts/generated/observability-trace-get.v1';
import type { SandboxRunV1 } from '@/contracts/generated/sandbox-run.v1';
import { DebugContextInspector } from '@/features/agent/status/DebugContextInspector';
import {
  InlineNotice,
  ManagementPage,
  ManagementSection,
  QueryState,
  StatusBadge,
  numberValue,
} from '@/features/overview/management-ui';
import {
  useCreateEvalSchedule,
  useEvalScheduleAction,
  useEvalScheduleRuns,
  useEvalSchedules,
  useEvalSuites,
  useObservationFeed,
  useObservationEvals,
  useObservationAiJudge,
  useObservationEvidenceEval,
  useObservationTrace,
  useSandboxRuns,
  type ObservationConnectionState,
  type ObservationFilters,
} from './api';
import {
  projectVerticalAppEval,
  type VerticalAppEvalState,
} from './vertical-app-eval-projection';
import './observability.css';

const CATEGORY_FILTERS = [
  'all',
  'agent',
  'tool',
  'context',
  'retrieval',
  'memory',
  'room',
  'intercom',
  'approval',
  'runtime',
] as const;

type CategoryFilter = (typeof CATEGORY_FILTERS)[number];

export function ObservabilityFeature() {
  const [searchParams, setSearchParams] = useSearchParams();
  const category = observationCategory(searchParams.get('category')) ?? 'all';
  const [needle, setNeedle] = useState('');
  const filters = useMemo<ObservationFilters>(() => ({
    ...(searchParams.get('sessionId') ? { sessionId: searchParams.get('sessionId') ?? '' } : {}),
    ...(searchParams.get('roomId') ? { roomId: searchParams.get('roomId') ?? '' } : {}),
    ...(searchParams.get('traceId') ? { traceId: searchParams.get('traceId') ?? '' } : {}),
    ...(searchParams.get('runId') ? { runId: searchParams.get('runId') ?? '' } : {}),
    ...(category === 'all' ? {} : { category }),
  }), [category, searchParams]);
  const feed = useObservationFeed(filters);
  const scopedItems = useMemo(
    () => feed.items.filter((item) => !filters.runId || item.runId === filters.runId),
    [feed.items, filters.runId],
  );
  const visibleItems = useMemo(
    () => scopedItems.filter((item) => observationMatches(item, needle)),
    [needle, scopedItems],
  );
  const [selectedTraceId, setSelectedTraceId] = useState('');
  const [selectedEventId, setSelectedEventId] = useState('');
  const selectedTrace = useMemo(
    () => scopedItems
      .filter((item) => item.traceId === selectedTraceId)
      .sort((left, right) => left.sequence - right.sequence),
    [scopedItems, selectedTraceId],
  );
  const selectedEvent = scopedItems.find((item) => item.eventId === selectedEventId)
    ?? selectedTrace.at(-1);
  const traceDetail = useObservationTrace(selectedTraceId);
  const scopedTraceId = filters.traceId ?? '';

  useEffect(() => {
    if (!scopedItems.length) {
      setSelectedTraceId(scopedTraceId);
      setSelectedEventId('');
      return;
    }
    if (!visibleItems.length) return;
    if (scopedTraceId && selectedTraceId !== scopedTraceId) {
      setSelectedTraceId(scopedTraceId);
      setSelectedEventId(scopedItems.find((item) => item.traceId === scopedTraceId)?.eventId ?? '');
      return;
    }
    if (!scopedItems.some((item) => item.traceId === selectedTraceId)) {
      setSelectedTraceId(scopedItems[0].traceId);
    }
    if (!scopedItems.some((item) => item.eventId === selectedEventId)) {
      setSelectedEventId(scopedItems[0].eventId);
    }
  }, [scopedItems, scopedTraceId, selectedEventId, selectedTraceId, visibleItems]);

  const runningCount = visibleItems.filter((item) =>
    ['queued', 'running', 'waiting'].includes(item.status),
  ).length;
  const failedCount = visibleItems.filter((item) => item.status === 'failed').length;
  const traceCount = new Set(visibleItems.map((item) => item.traceId)).size;
  const averageDuration = average(
    visibleItems
      .filter((item) => ['completed', 'failed', 'cancelled'].includes(item.status))
      .map((item) => item.durationMs)
      .filter((value): value is number => typeof value === 'number' && value > 0),
  );
  const scoped = Boolean(filters.sessionId || filters.roomId || filters.traceId || filters.runId);
  const snapshotTotal = Math.max(feed.items.length, feed.snapshot?.counts.total ?? 0);
  const snapshotTruncated = Boolean(feed.snapshot?.truncated || snapshotTotal > feed.items.length);
  // The journal's total currently counts the returned page. A truncated page
  // with no larger total does not establish the size of the whole history.
  const snapshotTotalKnown = !feed.snapshot?.truncated || snapshotTotal > feed.items.length;
  const snapshotGeneratedAtMs = feed.snapshot?.generatedAtMs ?? 0;
  const showSnapshotAge = feed.connection !== 'live' && snapshotGeneratedAtMs > 0;
  const timelineCountLabel = needle.trim()
    ? `${visibleItems.length} / 已载入 ${feed.items.length}`
    : snapshotTruncated
      ? snapshotTotalKnown ? `最近 ${visibleItems.length} / 共 ${snapshotTotal}` : `最近 ${visibleItems.length} 条`
      : `${visibleItems.length} 条`;

  function selectCategory(next: CategoryFilter): void {
    const params = new URLSearchParams(searchParams);
    if (next === 'all') params.delete('category');
    else params.set('category', next);
    setSearchParams(params, { replace: true });
  }

  function clearScope(): void {
    const params = new URLSearchParams(searchParams);
    for (const key of ['sessionId', 'roomId', 'traceId', 'runId']) params.delete(key);
    setSearchParams(params, { replace: true });
  }

  function clearViewFilters(): void {
    setNeedle('');
    const params = new URLSearchParams(searchParams);
    params.delete('category');
    setSearchParams(params, { replace: true });
  }

  return (
    <ManagementPage
      actions={(
        <Button
          leadingIcon={<RefreshCw size={15} />}
          loading={feed.isFetching}
          onClick={() => void feed.refresh()}
          size="small"
        >
          刷新
        </Button>
      )}
      description="回看伙伴对话、多人协作、记忆检索和工具操作发生了什么。"
      eyebrow="本机记录"
      routeId="observability"
      title="运行记录"
    >
      <QueryState
        error={feed.snapshot ? null : feed.error}
        isPending={!feed.snapshot && feed.isPending}
        onRetry={() => void feed.refresh()}
      >
        {feed.snapshot && feed.error ? (
          <InlineNotice title="刷新未完成，保留上次记录" tone="warning">
            <p>当前仍显示上次读取的记录；重新读取后再确认最新状态。</p>
            <Button loading={feed.isFetching} onClick={() => void feed.refresh()} size="small">
              重试读取
            </Button>
          </InlineNotice>
        ) : null}
        {snapshotTruncated ? (
          <InlineNotice title={snapshotTotalKnown ? `当前显示最近 ${feed.items.length} / 共 ${snapshotTotal} 条` : `当前显示最近 ${feed.items.length} 条`} tone="warning">
            Runtime 当前只返回最近一段记录；这里明确保留该边界，不把这批结果当作完整历史。
          </InlineNotice>
        ) : null}

        {feed.streamError ? (
          <div className="observation-connection-notice">
            <InlineNotice title="实时连接" tone="warning">{feed.streamError}</InlineNotice>
            <Button onClick={() => void feed.refresh()} size="small">重新连接</Button>
          </div>
        ) : null}

        <section aria-label="运行记录工作台" className="observation-console">
          <header aria-label="事件记录概况" className="observation-pulse" data-connection={feed.connection}>
            <StatusBadge
              label={connectionLabel(feed.connection)}
              tone={connectionTone(feed.connection)}
            />
            {showSnapshotAge ? (
              <span className="observation-pulse__stat" data-role="snapshot-age">
                快照生成于 {formatTime(snapshotGeneratedAtMs)}
              </span>
            ) : null}
            <span className="observation-pulse__stat">
              <GitBranch aria-hidden="true" size={14} />
              {traceCount} 次流程 · {visibleItems.length} 条事件
            </span>
            <span className="observation-pulse__stat" data-tone={runningCount ? 'active' : undefined}>
              <Radio aria-hidden="true" size={14} />
              执行中记录 {runningCount}
            </span>
            <span className="observation-pulse__stat" data-tone={failedCount ? 'danger' : undefined}>
              <TriangleAlert aria-hidden="true" size={14} />
              失败记录 {failedCount}
            </span>
            <span className="observation-pulse__stat">
              <TimerReset aria-hidden="true" size={14} />
              平均耗时 {averageDuration ? formatDuration(averageDuration) : '暂无'}
            </span>
          </header>

          <section aria-label="运行记录筛选" className="observation-controls">
            <div aria-label="事件类型" className="observation-category-tabs" role="group">
              {CATEGORY_FILTERS.map((item) => (
                <button
                  aria-pressed={category === item}
                  data-active={category === item}
                  key={item}
                  onClick={() => selectCategory(item)}
                  type="button"
                >
                  {categoryLabel(item)}
                </button>
              ))}
            </div>
            <label className="observation-search">
              <Search aria-hidden="true" size={15} />
              <input
                aria-label="搜索运行记录"
                onChange={(event) => setNeedle(event.target.value)}
                placeholder="搜索流程、对话、协作或步骤"
                type="search"
                value={needle}
              />
            </label>
            {scoped ? (
              <div className="observation-scope">
                <span>{scopeLabel(filters)}</span>
                <IconButton
                  icon={<FilterX size={15} />}
                  label="清除来源范围"
                  onClick={clearScope}
                  tooltip
                />
              </div>
            ) : null}
          </section>

          <div className="observation-workspace">
            <ManagementSection
              title="事件时间线"
              trailing={<StatusBadge label={timelineCountLabel} tone="neutral" />}
            >
              {visibleItems.length ? (
                <ol className="observation-timeline" aria-label="运行记录事件">
                  {visibleItems.map((item) => (
                    <ObservationRow
                      active={item.eventId === selectedEvent?.eventId}
                      item={item}
                      key={item.eventId}
                      onSelect={() => {
                        setSelectedTraceId(item.traceId);
                        setSelectedEventId(item.eventId);
                      }}
                    />
                  ))}
                </ol>
              ) : (
                <EmptyState
                  action={feed.items.length ? <Button onClick={clearViewFilters} size="small">清除筛选</Button> : <Button onClick={() => void feed.refresh()} size="small">重新检查</Button>}
                  description={feed.items.length ? '当前筛选没有匹配的运行记录。' : '当前范围内还没有结构化运行事件。'}
                  icon={Network}
                  title={feed.items.length ? '没有匹配的记录' : '暂无运行记录'}
                />
              )}
            </ManagementSection>

            <ManagementSection
              title="这次是怎样完成的"
              trailing={selectedTrace.length ? (
                <StatusBadge label={`${selectedTrace.length} 步`} tone="neutral" />
              ) : selectedTraceId ? (
                <StatusBadge
                  label={traceProjectionLabel(
                    traceDetail.data?.trace.traceId === selectedTraceId
                      ? traceDetail.data.projectionSource
                      : undefined,
                  )}
                  tone="neutral"
                />
              ) : undefined}
            >
              {selectedTraceId ? (
                <>
                  <header className="observation-trace-heading">
                    <span><GitBranch size={15} />同一次流程</span>
                    <small>{selectedTrace.length ? traceScope(selectedTrace) : selectedTraceId}</small>
                  </header>
                  {selectedTrace.length ? (
                    <ol className="observation-trace">
                      {selectedTrace.map((item, index) => (
                        <li data-active={item.eventId === selectedEvent?.eventId} data-category={item.category} data-status={item.status} key={`${item.eventId}:trace`}>
                          <article className="observation-trace__card">
                            <button className="observation-trace__summary" onClick={() => setSelectedEventId(item.eventId)} type="button">
                              <span className="observation-trace__index">{index + 1}</span>
                              <span>
                                <strong>{publicObservationSummary(item)}</strong>
                                <small>{categoryLabel(item.category)} · {eventStatusLabel(item.status)}</small>
                              </span>
                              <time dateTime={new Date(item.createdAtMs).toISOString()}>
                                {formatTime(item.createdAtMs)}
                              </time>
                            </button>
                            <ObservationFacts item={item} />
                          </article>
                        </li>
                      ))}
                    </ol>
                  ) : traceDetail.data?.trace.traceId === selectedTraceId ? (
                    <CanonicalTraceOriginNotice source={traceDetail.data.projectionSource} />
                  ) : null}
                  {traceDetail.error ? (
                    <InlineNotice title="这次流程的标准 Trace 暂时不可用" tone="warning">
                      事件时间线仍然保留；标准 spans 和检索证据稍后可以重新读取。
                    </InlineNotice>
                  ) : null}
                  {traceDetail.isPending ? (
                    <div className="observation-canonical-loading" role="status">
                      正在读取这次流程的标准 Trace…
                    </div>
                  ) : traceDetail.data?.trace.traceId === selectedTraceId ? (
                    <CanonicalTraceDetails detail={traceDetail.data} key={selectedTraceId} />
                  ) : null}
                  {selectedEvent?.sessionId ? (
                    <div className="observation-debug-actions">
                      <a href={`#/context-debug?sessionId=${encodeURIComponent(selectedEvent.sessionId)}${selectedEvent.turnId ? `&turnId=${encodeURIComponent(selectedEvent.turnId)}` : ''}`}>
                        <Braces size={15} />
                        <span><strong>查看这轮的完整上下文</strong><small>逐次核对新增内容、模型请求和工具执行</small></span>
                      </a>
                      <Disclosure
                        className="observation-debug-context"
                        summary={<><Database size={15} /><span><strong>在这里快速查看</strong><small>{selectedEvent.turnId ? '读取本轮临时快照' : '读取当前对话的最新临时快照'}</small></span></>}
                        title={selectedEvent.turnId || selectedEvent.sessionId}
                      >
                        <DebugContextInspector sessionId={selectedEvent.sessionId} turnId={selectedEvent.turnId || undefined} embedded />
                      </Disclosure>
                    </div>
                  ) : null}
                </>
              ) : (
                <EmptyState
                  description="从左侧时间线选择一条记录后，这里会按顺序还原同一次工作的前后步骤。"
                  icon={GitBranch}
                  title="选择一条运行记录"
                />
              )}
            </ManagementSection>
          </div>

          <EvalSchedulesPanel />

          <footer className="observation-privacy">
            <LockKeyhole aria-hidden="true" size={14} />
            <p>运行记录只保存状态、耗时、数量和脱敏后的标识。开启“本机上下文快照”后，可以在上下文检查中查看指定目录保存的脱敏记录；未开启时只查看当前运行中的内容。</p>
          </footer>
        </section>
      </QueryState>
    </ManagementPage>
  );
}

export function EvalSchedulesPanel({ initialScheduleId = '' }: { initialScheduleId?: string } = {}) {
  const schedules = useEvalSchedules();
  const suites = useEvalSuites();
  const createSchedule = useCreateEvalSchedule();
  const scheduleAction = useEvalScheduleAction();
  const [selectedScheduleId, setSelectedScheduleId] = useState(initialScheduleId);
  const [suiteId, setSuiteId] = useState('');
  const [recurrenceKind, setRecurrenceKind] = useState<'daily' | 'weekly'>('daily');
  const [recurrenceInterval, setRecurrenceInterval] = useState(1);
  const [maxRuns, setMaxRuns] = useState(30);
  const [nextDueAt, setNextDueAt] = useState(() => localDateTimeValue(Date.now() + 60 * 60 * 1_000));
  const [createdScheduleId, setCreatedScheduleId] = useState('');
  const runs = useEvalScheduleRuns(selectedScheduleId);
  const nextDueAtMs = new Date(nextDueAt).getTime();
  const selectedSuite = suites.data?.items.find((item) => item.suiteId === suiteId);
  const canCreate = Boolean(selectedSuite)
    && Number.isInteger(recurrenceInterval)
    && recurrenceInterval >= 1
    && recurrenceInterval <= 30
    && Number.isInteger(maxRuns)
    && maxRuns >= 1
    && maxRuns <= 100
    && Number.isFinite(nextDueAtMs)
    && nextDueAtMs >= Date.now()
    && !suites.isFetching
    && !createSchedule.isPending;

  useEffect(() => {
    const items = suites.data?.items ?? [];
    if (items.some((item) => item.suiteId === suiteId)) return;
    setSuiteId(items[0]?.suiteId ?? '');
  }, [suiteId, suites.data?.items]);

  useEffect(() => {
    const items = schedules.data?.items ?? [];
    if (items.some((item) => item.id === selectedScheduleId)) return;
    setSelectedScheduleId(createdScheduleId || items.at(-1)?.id || '');
  }, [createdScheduleId, schedules.data?.items, selectedScheduleId]);

  async function submitSchedule(): Promise<void> {
    try {
      const result = await createSchedule.mutateAsync({
        suiteId: selectedSuite?.suiteId ?? '',
        suiteRevision: selectedSuite?.suiteRevision ?? '',
        recurrenceKind,
        recurrenceInterval,
        maxRuns,
        nextDueAtMs,
      });
      setCreatedScheduleId(result.schedule.id);
      setSelectedScheduleId(result.schedule.id);
    } catch {
      // The current schedule list remains usable; the inline receipt owns retry copy.
    }
  }

  const selectedSchedule = schedules.data?.items.find((item) => item.id === selectedScheduleId)
    ?? (createSchedule.data?.schedule.id === selectedScheduleId ? createSchedule.data.schedule : undefined);

  return (
    <section aria-label="周期 Eval" className="observation-schedules">
      <SandboxRunsPanel />
      <header className="observation-schedules__heading">
        <span><TimerReset aria-hidden="true" size={15} /><strong>周期 Eval</strong></span>
        <small>本机计划 · 固定 suite revision · {schedules.data?.items.length ?? 0} 项</small>
      </header>
      <p className="observation-schedules__hint">
        周期计划只调度已注册的确定性 Eval suite；每次执行仍生成独立 EvalRun，不会自动改写 Memory、Knowledge 或索引。
      </p>
      {schedules.error ? (
        <InlineNotice title="周期 Eval 暂时不可用" tone="warning">
          Trace 与已有 Eval 仍可查看；当前只保留本机计划的重试入口。
        </InlineNotice>
      ) : null}
      {suites.error ? (
        <InlineNotice title="Eval suite 目录暂不可用" tone="warning">
          已有周期计划仍可查看；目录恢复前不会显示或伪造新建选项。
        </InlineNotice>
      ) : null}
      <div className="observation-schedules__workspace">
        <div>
          {schedules.isPending ? <div className="observation-eval__loading" role="status">正在读取周期 Eval…</div> : null}
          {!schedules.isPending && !schedules.error && !(schedules.data?.items.length) ? (
            <p className="observation-eval__empty">还没有周期 Eval 计划。</p>
          ) : null}
          {schedules.data?.items.length ? (
            <ul aria-label="周期 Eval 计划" className="observation-schedules__list">
              {schedules.data.items.map((schedule) => (
                <li key={schedule.id}>
                  <button
                    aria-current={schedule.id === selectedScheduleId ? 'true' : undefined}
                    aria-label={`${schedule.suiteId} ${schedule.suiteRevision}，${evalScheduleStatusLabel(schedule.status)}`}
                    data-active={schedule.id === selectedScheduleId || undefined}
                    onClick={() => setSelectedScheduleId(schedule.id)}
                    type="button"
                  >
                    <span><strong>{schedule.suiteId}</strong><small>{schedule.suiteRevision}</small></span>
                    <span>{recurrenceLabel(schedule)}<small>{schedule.runCount} / {schedule.maxRuns} 次</small></span>
                    <StatusBadge label={evalScheduleStatusLabel(schedule.status)} tone={evalScheduleStatusTone(schedule.status)} />
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
        <div className="observation-schedules__runs">
          {selectedSchedule ? (
            <>
              <header>
                <span><strong>{selectedSchedule.suiteId}</strong><small>{selectedSchedule.id}</small></span>
                <span>{selectedSchedule.status === 'scheduled' ? `下次 ${formatDateTime(selectedSchedule.nextDueAtMs)}` : evalScheduleStatusLabel(selectedSchedule.status)}</span>
              </header>
              <div className="schedule-eval-actions">
                {selectedSchedule.status === 'scheduled' ? <Button size="small" disabled={scheduleAction.isPending} onClick={() => scheduleAction.mutate({ scheduleId: selectedSchedule.id, action: 'pause' })}>暂停评测计划</Button> : null}
                {selectedSchedule.status === 'paused' ? <Button size="small" disabled={scheduleAction.isPending} onClick={() => scheduleAction.mutate({ scheduleId: selectedSchedule.id, action: 'resume' })}>恢复评测计划</Button> : null}
                {['scheduled', 'paused', 'failed'].includes(selectedSchedule.status) ? <Button size="small" disabled={scheduleAction.isPending} onClick={() => scheduleAction.mutate({ scheduleId: selectedSchedule.id, action: 'cancel' })}>取消评测计划</Button> : null}
                {['completed', 'failed'].includes(selectedSchedule.status) && selectedSchedule.runCount < 100 ? <Button size="small" disabled={scheduleAction.isPending} onClick={() => scheduleAction.mutate({ scheduleId: selectedSchedule.id, action: 'retry' })}>再评测一次</Button> : null}
              </div>
              {scheduleAction.error && scheduleAction.variables?.scheduleId === selectedSchedule.id ? <InlineNotice title="计划没有更新" tone="danger">请刷新计划，确认当前状态后重试。</InlineNotice> : null}
              {runs.error ? <InlineNotice title="执行记录暂时不可用" tone="warning">计划本身仍然保留。</InlineNotice> : null}
              {runs.isPending ? <div className="observation-eval__loading" role="status">正在读取执行记录…</div> : null}
              {!runs.isPending && !runs.error && !runs.data?.items.length ? (
                <p className="observation-eval__empty">还没有执行回执。</p>
              ) : null}
              {runs.data?.items.length ? (
                <ol aria-label="周期 Eval 执行记录">
                  {runs.data.items.map((run) => (
                    <li data-state={run.state} key={run.id}>
                      <span><strong>第 {run.attempt} 次</strong><small>{formatDateTime(run.dueAtMs)}</small></span>
                      <span>
                        {evalScheduleRunStateLabel(run.state)}
                        <small>{run.evalRunId || run.errorCode || '等待回执'}</small>
                        {run.traceIds.length ? (
                          <span aria-label="关联 Trace">
                            {run.traceIds.map((traceId) => (
                              <a
                                href={`#/observability?traceId=${encodeURIComponent(traceId)}`}
                                key={traceId}
                              >
                                {traceId}
                              </a>
                            ))}
                            {run.traceIdsTruncated ? <small>Trace 已截断</small> : null}
                          </span>
                        ) : null}
                      </span>
                    </li>
                  ))}
                </ol>
              ) : null}
            </>
          ) : <p className="observation-eval__empty">选择一项计划后查看每次执行回执。</p>}
        </div>
      </div>
      <Disclosure
        className="observation-schedules__create"
        summary={<><ShieldCheck aria-hidden="true" size={14} />新建周期 Eval</>}
      >
        <div className="observation-schedules__form">
          <label><span>Eval suite</span>
            <select
              aria-label="Eval suite"
              disabled={suites.isPending || suites.isFetching || Boolean(suites.error) || !suites.data?.items.length}
              onChange={(event) => setSuiteId(event.target.value)}
              value={suiteId}
            >
              {!suites.data?.items.length ? <option value="">暂无已注册 suite</option> : null}
              {suites.data?.items.map((suite) => (
                <option key={suite.suiteId} value={suite.suiteId}>{suite.displayName} · {suite.suiteId}</option>
              ))}
            </select>
          </label>
          <div aria-label="Eval suite revision" className="observation-schedules__suite-revision">
            <span>固定版本</span>
            <strong>{selectedSuite?.suiteRevision ?? (suites.isPending ? '读取中…' : '不可用')}</strong>
            {selectedSuite ? <small>{selectedSuite.fixtureCount} 个 fixture · {selectedSuite.capabilities.join(' · ')}</small> : null}
          </div>
          <label><span>周期</span><select aria-label="Eval 周期" onChange={(event) => setRecurrenceKind(event.target.value as 'daily' | 'weekly')} value={recurrenceKind}><option value="daily">每天</option><option value="weekly">每周</option></select></label>
          <label><span>间隔</span><input aria-label="Eval 周期间隔" max={30} min={1} onChange={(event) => setRecurrenceInterval(Number(event.target.value))} type="number" value={recurrenceInterval} /></label>
          <label><span>最多执行</span><input aria-label="Eval 最大执行次数" max={100} min={1} onChange={(event) => setMaxRuns(Number(event.target.value))} type="number" value={maxRuns} /></label>
          <label><span>首次执行</span><input aria-label="Eval 首次执行时间" onChange={(event) => setNextDueAt(event.target.value)} type="datetime-local" value={nextDueAt} /></label>
          <div className="observation-schedules__create-action">
            <span>创建后由现有 Runtime wake loop 领取，不另起守护进程。</span>
            <Button disabled={!canCreate || Boolean(suites.error)} loading={createSchedule.isPending} onClick={() => void submitSchedule()} size="small">创建计划</Button>
          </div>
          {createSchedule.error ? <InlineNotice title="周期 Eval 未创建" tone="danger">请检查 suite、版本和首次执行时间后重试。</InlineNotice> : null}
          {createdScheduleId ? <div className="observation-eval__receipt" role="status"><strong>周期 Eval 已创建</strong><span>{createdScheduleId}</span></div> : null}
        </div>
      </Disclosure>
    </section>
  );
}

function SandboxRunsPanel() {
  const sandboxRuns = useSandboxRuns();
  const items = useMemo(
    () => [...(sandboxRuns.data?.items ?? [])].sort((left, right) => right.updatedAtMs - left.updatedAtMs),
    [sandboxRuns.data?.items],
  );
  const [selectedRunId, setSelectedRunId] = useState('');
  const selectedRun = items.find((item) => item.sandboxRunId === selectedRunId) ?? items[0];

  useEffect(() => {
    if (!items.length) {
      setSelectedRunId('');
      return;
    }
    if (!items.some((item) => item.sandboxRunId === selectedRunId)) {
      setSelectedRunId(items[0].sandboxRunId);
    }
  }, [items, selectedRunId]);

  function selectRunFromKeyboard(event: ReactKeyboardEvent<HTMLButtonElement>, index: number): void {
    let nextIndex = index;
    if (event.key === 'ArrowDown' || event.key === 'ArrowRight') nextIndex = (index + 1) % items.length;
    else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') nextIndex = (index - 1 + items.length) % items.length;
    else if (event.key === 'Home') nextIndex = 0;
    else if (event.key === 'End') nextIndex = items.length - 1;
    else return;
    event.preventDefault();
    const next = items[nextIndex];
    setSelectedRunId(next.sandboxRunId);
    document.getElementById(sandboxRunTabId(next.sandboxRunId))?.focus();
  }

  return (
    <section aria-label="垂直 Agent SandboxRun 链路" className="observation-sandbox-runs">
      <header className="observation-sandbox-runs__heading">
        <span><ShieldCheck aria-hidden="true" size={15} /><strong>垂直 App 多重 Eval 试验场</strong></span>
        <small>{sandboxRuns.data?.total ?? 0} 次 SandboxRun</small>
      </header>
      <p className="observation-sandbox-runs__hint">
        候选配置 → Host 沙盒测试 → 多重 Eval → Keep / Reject。失败时才进入 Trace、修复与同条件复验；没有 verification / promotion 回执不会晋升。
      </p>
      {sandboxRuns.error ? (
        <InlineNotice title="SandboxRun 暂时不可用" tone="warning">
          Trace 与 Eval 仍可查看；请刷新重试。
        </InlineNotice>
      ) : null}
      {sandboxRuns.isPending ? <div className="observation-sandbox-runs__loading" role="status">正在读取 SandboxRun…</div> : null}
      {!sandboxRuns.isPending && !sandboxRuns.error && !items.length ? (
        <EmptyState
          description="先在 App Center 启用 Vertical Agent Sandbox，再从新 Session 运行 SGG。"
          headingLevel={3}
          icon={ShieldCheck}
          title="还没有 SandboxRun"
        />
      ) : null}
      {items.length ? (
        <div className="observation-sandbox-runs__workspace">
          <div aria-label="垂直 App 实验" className="observation-sandbox-runs__picker" role="tablist">
            {items.map((item, index) => {
              const selected = item.sandboxRunId === selectedRun?.sandboxRunId;
              const tabId = sandboxRunTabId(item.sandboxRunId);
              return (
                <button
                  aria-controls={selected ? `${tabId}-panel` : undefined}
                  aria-selected={selected}
                  id={tabId}
                  key={item.sandboxRunId}
                  onClick={() => setSelectedRunId(item.sandboxRunId)}
                  onKeyDown={(event) => selectRunFromKeyboard(event, index)}
                  role="tab"
                  tabIndex={selected ? 0 : -1}
                  type="button"
                >
                  <span><strong>{item.appId}</strong><small>{item.sandboxRunId}</small></span>
                  <StatusBadge label={sandboxRunStatusLabel(item.status)} tone={sandboxRunStatusTone(item.status)} />
                </button>
              );
            })}
          </div>
          {selectedRun ? <SandboxRunDetail item={selectedRun} key={selectedRun.sandboxRunId} /> : null}
        </div>
      ) : null}
    </section>
  );
}

function SandboxRunDetail({ item }: { item: SandboxRunV1 }) {
  const primaryTraceId = item.traceIds[0] ?? '';
  const evals = useObservationEvals(primaryTraceId);
  const projection = projectVerticalAppEval(item, evals.data?.items ?? []);
  const tabId = sandboxRunTabId(item.sandboxRunId);

  return (
    <article
      aria-labelledby={tabId}
      className="observation-sandbox-runs__row"
      data-status={item.status}
      id={`${tabId}-panel`}
      role="tabpanel"
    >
      <header className="observation-sandbox-runs__identity">
        <span><strong>{item.appId}</strong><code title={item.sandboxRunId}>{item.sandboxRunId}</code></span>
        <StatusBadge label={sandboxRunStatusLabel(item.status)} tone={sandboxRunStatusTone(item.status)} />
      </header>

      <div aria-label={`${item.appId} 候选试验流程`} className="observation-app-eval__flow">
        <section className="observation-app-eval__stage" data-state={projection.candidate.state}>
          <header><span>候选配置</span><StageStateBadge state={projection.candidate.state} /></header>
          <strong>{projection.candidate.label}</strong>
          {projection.candidate.cohortLabel ? <small>{projection.candidate.cohortLabel}</small> : <small>当前 SandboxRun 列表没有冻结配置回执。</small>}
          {projection.candidate.configFingerprint ? <code title={projection.candidate.configFingerprint}>{projection.candidate.configFingerprint}</code> : null}
        </section>

        <section className="observation-app-eval__stage" data-state={projection.sandbox.state}>
          <header><span>Host 沙盒测试</span><StageStateBadge state={projection.sandbox.state} /></header>
          <strong>{projection.sandbox.label}</strong>
          <div className="observation-sandbox-runs__policy" aria-label="SandboxRun 策略">
            <span><code>network</code> {item.policy.network}</span>
            <span><code>mutation</code> {item.policy.mutationMode}</span>
            <span><code>productionWriteBlocked</code> {String(item.policy.productionWriteBlocked)}</span>
          </div>
        </section>

        <section className="observation-app-eval__stage observation-app-eval__stage--evals" data-state={projection.evals.state}>
          <header><span>多重 Eval</span><StageStateBadge state={projection.evals.state} /></header>
          <strong>{projection.evals.receivedCount} / {projection.evals.expectedCount} 份回执</strong>
          {evals.isPending ? <small role="status">正在读取 Eval 回执…</small> : null}
          {evals.error ? <small data-tone="danger">Eval 回执暂时不可用；已保留 SandboxRun。</small> : null}
          {!evals.isPending && !evals.error ? (
            <small>{projection.evals.suiteAlignmentReason}</small>
          ) : null}
          {projection.evals.items.length ? (
            <ul aria-label={`${item.appId} 多重 Eval 回执`} className="observation-app-eval__receipts">
              {projection.evals.items.map((evalItem) => (
                <li data-status={evalItem.status} key={evalItem.evalRunId}>
                  <div>
                    <code title={evalItem.evalRunId}>{evalItem.evalRunId}</code>
                    <span>{evalItem.evaluatorDisplayName} · {evalItem.authority === 'ground_truth' ? '真值' : 'AI 估计'}</span>
                  </div>
                  {evalItem.metrics.length ? (
                    <dl>
                      {evalItem.metrics.map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{formatScore(value)}</dd></div>)}
                    </dl>
                  ) : <small>{evalItem.status === 'completed' ? '回执没有指标' : evalStatusLabel(evalItem.status)}</small>}
                </li>
              ))}
            </ul>
          ) : null}
          {projection.evals.missingEvalRunIds.length ? (
            <div className="observation-app-eval__missing">
              <small>等待 EvalRun</small>
              {projection.evals.missingEvalRunIds.map((evalRunId) => <code key={evalRunId}>{evalRunId}</code>)}
            </div>
          ) : null}
        </section>

        <section className="observation-app-eval__stage" data-state={projection.decision.state}>
          <header><span>Keep / Reject</span><StageStateBadge state={projection.decision.state} /></header>
          <strong>{projection.decision.label}</strong>
          <small>{projection.decision.detail}</small>
        </section>
      </div>

      {projection.failureBranch ? (
        <aside className="observation-app-eval__failure" role="status">
          <span><TriangleAlert aria-hidden="true" size={15} /><strong>候选失败，等待 Trace / 评价回执</strong></span>
          <p>Trace → 修复 → 复验</p>
          <nav aria-label="失败候选 Trace">
            {projection.traceIds.map((traceId) => (
              <a aria-label={`查看 ${traceId}`} href={`#/observability?traceId=${encodeURIComponent(traceId)}`} key={traceId}>{traceId}</a>
            ))}
          </nav>
        </aside>
      ) : projection.traceIds.length ? (
        <nav aria-label="候选 Trace" className="observation-app-eval__trace-links">
          {projection.traceIds.map((traceId) => (
            <a href={`#/observability?traceId=${encodeURIComponent(traceId)}`} key={traceId}>{traceId}</a>
          ))}
        </nav>
      ) : null}
    </article>
  );
}

function sandboxRunTabId(sandboxRunId: string): string {
  return `sandbox-run-${sandboxRunId.replace(/[^a-zA-Z0-9_-]/gu, '-')}`;
}

function StageStateBadge({ state }: { state: VerticalAppEvalState | 'waiting' }) {
  const label = ({ waiting: '等待', active: '进行中', passed: '已完成', failed: '失败' })[state];
  const tone = state === 'passed' ? 'success' : state === 'failed' ? 'danger' : state === 'active' ? 'info' : 'neutral';
  return <StatusBadge label={label} tone={tone} />;
}

function recurrenceLabel(schedule: EvalScheduleListV1['items'][number]): string {
  const unit = schedule.recurrenceKind === 'daily' ? '天' : '周';
  return `每 ${schedule.recurrenceInterval} ${unit}`;
}

function evalScheduleStatusLabel(status: EvalScheduleListV1['items'][number]['status']): string {
  return ({ scheduled: '已计划', running: '执行中', completed: '已完成', failed: '失败', paused: '已暂停', cancelled: '已取消' })[status];
}

function evalScheduleStatusTone(status: EvalScheduleListV1['items'][number]['status']): 'success' | 'danger' | 'info' | 'neutral' {
  if (status === 'completed') return 'success';
  if (status === 'failed') return 'danger';
  if (status === 'running') return 'info';
  return 'neutral';
}

function evalScheduleRunStateLabel(state: 'claimed' | 'succeeded' | 'failed'): string {
  return ({ claimed: '执行中', succeeded: '成功', failed: '失败' })[state];
}

function sandboxRunStatusLabel(status: SandboxRunV1['status']): string {
  return ({
    queued: '排队', running: '运行中', completed: '已完成', failed: '失败', cancelled: '已取消',
  })[status];
}

function sandboxRunStatusTone(status: SandboxRunV1['status']): 'success' | 'danger' | 'info' | 'neutral' {
  if (status === 'completed') return 'success';
  if (status === 'failed') return 'danger';
  if (status === 'running') return 'info';
  return 'neutral';
}

function localDateTimeValue(timestamp: number): string {
  const date = new Date(timestamp);
  const offsetMs = date.getTimezoneOffset() * 60 * 1_000;
  return new Date(timestamp - offsetMs).toISOString().slice(0, 16);
}

function formatDateTime(timestamp: number): string {
  if (!timestamp) return '未安排';
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  }).format(new Date(timestamp));
}

function ObservationRow({
  active,
  item,
  onSelect,
}: {
  active: boolean;
  item: ObservationEventV1;
  onSelect: () => void;
}) {
  const Icon = categoryIcon(item.category);
  return (
    <li data-active={active} data-category={item.category} data-status={item.status}>
      <button aria-current={active ? 'true' : undefined} onClick={onSelect} type="button">
        <span className="observation-row__icon"><Icon size={16} /></span>
        <span className="observation-row__copy">
          <strong>{publicObservationSummary(item)}</strong>
          <small>{categoryLabel(item.category)} · {phaseLabel(item.phase)}</small>
        </span>
        <span className="observation-row__scope">
          <small>{primaryScope(item)}</small>
          <time dateTime={new Date(item.createdAtMs).toISOString()}>
            {formatTime(item.createdAtMs)}
          </time>
        </span>
        <StatusBadge label={eventStatusLabel(item.status)} tone={statusTone(item.status)} />
      </button>
    </li>
  );
}

function ObservationFacts({ item }: { item: ObservationEventV1 }) {
  const [expanded, setExpanded] = useState(false);
  const progress = observationProgress(item);
  const facts = observationFacts(item, new Set(progress?.sourceKeys ?? []));
  const initialFacts = facts.slice(0, 8);
  const additionalFacts = facts.slice(8);
  if (!facts.length && !progress) return null;
  return (
    <div className="observation-facts-shell">
      {progress ? (
        <div className="observation-progress">
          <span><strong>工具进度</strong><small>{progress.current} / {progress.total}</small></span>
          <progress aria-label={`工具进度 ${progress.current} / ${progress.total}`} max={progress.total} value={progress.current} />
        </div>
      ) : null}
      {initialFacts.length ? <ObservationFactList facts={initialFacts} /> : null}
      {additionalFacts.length ? (
        <Disclosure
          className="observation-facts__disclosure"
          onOpenChange={setExpanded}
          revealClassName="observation-facts__reveal"
          summary={<>
            <ChevronDown aria-hidden="true" size={14} />
            {expanded ? `收起其余 ${additionalFacts.length} 项事实` : `查看其余 ${additionalFacts.length} 项事实`}
          </>}
        >
          <ObservationFactList facts={additionalFacts} nested />
        </Disclosure>
      ) : null}
    </div>
  );
}

function CanonicalTraceDetails({ detail }: { detail: ObservabilityTraceGetV1 }) {
  const trace = detail.trace;
  return (
    <section aria-label="标准 Trace 记录" className="observation-canonical">
      <header className="observation-canonical__heading">
        <span><GitBranch aria-hidden="true" size={14} />标准 Trace</span>
        <small>{trace.status} · {trace.traceId}</small>
      </header>
      {detail.truncated ? (
        <InlineNotice title="局部窗口，状态已降级" tone="warning">
          当前只展示观察窗口中的 spans 和证据，不能据此断言完整流程已经结束。
        </InlineNotice>
      ) : null}
      <TraceRelations trace={trace} />
      <Disclosure
        className="observation-canonical__disclosure"
        summary={<><GitBranch aria-hidden="true" size={14} />Canonical spans · {trace.spans.length}</>}
      >
        <ol aria-label="Canonical spans" className="observation-canonical__list">
          {trace.spans.map((span, index) => (
            <li data-recorded={span.recorded} data-status={span.status} key={span.spanId}>
              <span className="observation-canonical__index">{index + 1}</span>
              <span className="observation-canonical__copy">
                <strong>{span.name}</strong>
                <small>{span.spanId}</small>
                {span.parentSpanId ? <small>父阶段 · {span.parentSpanId}</small> : null}
                {!span.recorded ? (
                  <small>未记录：{span.unavailableReason || '原因未提供'}</small>
                ) : null}
                <TraceSpanFacts attributes={span.attributes} metrics={span.metrics} />
              </span>
              <StatusBadge label={statusLabel(span.status)} tone={statusTone(span.status)} />
              <span className="observation-canonical__duration">{spanDurationLabel(span)}</span>
            </li>
          ))}
        </ol>
      </Disclosure>
      <Disclosure
        className="observation-canonical__disclosure"
        summary={<><Database aria-hidden="true" size={14} />证据 · {trace.evidence.length}</>}
      >
        <ul aria-label="Trace 证据" className="observation-canonical__evidence">
          {trace.evidence.map((evidence, index) => (
            <li key={`${evidence.evidenceId}:${evidence.evidenceStage}:${index}`}>
              <div className="observation-canonical__evidence-head">
                <strong>{evidence.evidenceId}</strong>
                <StatusBadge label={evidence.disposition} tone={evidence.disposition === 'included' ? 'success' : 'neutral'} />
              </div>
              <div className="observation-canonical__evidence-meta">
                <span>{evidence.sourceKind} · {evidence.sourceLane || '未标注 lane'}</span>
                <span>阶段 {evidence.evidenceStage}</span>
                <span>排名 {rankChangeLabel(evidence)}</span>
                <span>分数 {scoreLabel(evidence.scores)}</span>
              </div>
              <small>{evidence.sourceRef}</small>
              {evidence.omissionReason ? <small>省略原因：{evidence.omissionReason}</small> : null}
            </li>
          ))}
        </ul>
      </Disclosure>
      <Disclosure
        className="observation-canonical__disclosure"
        summary={<><Package aria-hidden="true" size={14} />产物 · {trace.artifacts.length}</>}
      >
        <ul aria-label="Trace 产物" className="observation-canonical__artifacts">
          {trace.artifacts.map((artifact, index) => (
            <li key={`${artifact.artifactId}:${index}`}>
              <strong>{artifact.artifactId}</strong>
              <span>{artifact.kind} · {artifact.mediaType}</span>
              <small>{formatBytes(artifact.byteSize)} · {artifact.recordCount} 条记录</small>
            </li>
          ))}
        </ul>
      </Disclosure>
      <TraceEvalPanel detail={detail} />
    </section>
  );
}

function CanonicalTraceOriginNotice({
  source,
}: {
  source: ObservabilityTraceGetV1['projectionSource'];
}) {
  if (source === 'trace_store') {
    return (
      <InlineNotice title="这是本机持久化的标准 Trace" tone="info">
        公共事件日志里没有复制这份记录；下方读取的是由原执行边界写入、本机不可变保存的脱敏终态 Trace。
      </InlineNotice>
    );
  }
  if (source === 'source_adapter') {
    return (
      <InlineNotice title="这是来源系统直接提供的 Trace" tone="info">
        公共事件日志里没有复制这份记录；下方直接读取输入或 Browser 等原始权威的脱敏投影。
      </InlineNotice>
    );
  }
  return null;
}

function TraceSpanFacts({
  attributes,
  metrics,
}: {
  attributes: Record<string, unknown>;
  metrics: Record<string, unknown>;
}) {
  const facts = [
    ...Object.entries(attributes).map(([key, value]) => ({ key, value, group: '属性' })),
    ...Object.entries(metrics).map(([key, value]) => ({ key, value, group: '指标' })),
  ];
  if (!facts.length) return null;
  return (
    <dl className="observation-canonical__span-facts">
      {facts.map((fact) => (
        <div key={`${fact.group}:${fact.key}`}>
          <dt>{fact.key}</dt>
          <dd>{traceFactValue(fact.value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function traceFactValue(value: unknown): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'object') return JSON.stringify(value) ?? '未记录';
  return String(value);
}

function traceProjectionLabel(
  source: ObservabilityTraceGetV1['projectionSource'] | undefined,
): string {
  if (source === 'trace_store') return '已保存 Trace';
  if (source === 'source_adapter') return '来源 Trace';
  if (source === 'observation_journal') return '事件 Trace';
  return '标准 Trace';
}

function TraceRelations({ trace }: { trace: ObservabilityTraceGetV1['trace'] }) {
  const bindings = Object.entries(trace.binding).filter((entry): entry is [string, string] => (
    typeof entry[1] === 'string' && Boolean(entry[1])
  ));
  const links = [
    ...(trace.parentTraceId ? [{ traceId: trace.parentTraceId, relation: 'parent' as const }] : []),
    ...(trace.links ?? []),
  ];
  if (!bindings.length && !links.length) return null;
  const sessionId = trace.binding.sessionId;
  const turnId = trace.binding.turnId;
  return (
    <section aria-label="Trace 关联" className="observation-canonical__relations">
      <header><span><Link2 aria-hidden="true" size={14} />对象与 Trace 关联</span></header>
      {bindings.length ? (
        <dl aria-label="Trace 对象绑定">
          {bindings.map(([key, value]) => (
            <div key={key}><dt>{traceBindingLabel(key)}</dt><dd><code>{value}</code></dd></div>
          ))}
        </dl>
      ) : null}
      {links.length ? (
        <ul aria-label="关联 Trace">
          {links.map((link, index) => (
            <li key={`${link.relation}:${link.traceId}:${index}`}>
              <span>{traceRelationLabel(link.relation)}</span>
              <a href={`#/observability?traceId=${encodeURIComponent(link.traceId)}`}>{link.traceId}</a>
            </li>
          ))}
        </ul>
      ) : null}
      {sessionId ? (
        <nav aria-label="Trace 返回入口">
          <a href={`#/agent?session=${encodeURIComponent(sessionId)}`}>返回原 Session</a>
          {turnId ? <a href={`#/context-debug?sessionId=${encodeURIComponent(sessionId)}&turnId=${encodeURIComponent(turnId)}`}>检查原 Turn 上下文</a> : null}
        </nav>
      ) : null}
    </section>
  );
}

function traceBindingLabel(key: string): string {
  return ({
    sessionId: 'Session', turnId: 'Turn', roomId: 'Room', runId: 'Run',
    sourceLoopId: 'Source loop', workItemId: 'WorkItem', caseId: 'Case',
  } as Record<string, string>)[key] ?? key;
}

function traceRelationLabel(relation: 'parent' | 'retry' | 'related'): string {
  return ({ parent: '父 Trace', retry: '重试自', related: '关联' })[relation];
}

function TraceEvalPanel({ detail }: { detail: ObservabilityTraceGetV1 }) {
  const trace = detail.trace;
  const evals = useObservationEvals(trace.traceId);
  const aiJudgeEval = useObservationAiJudge();
  const evidenceEval = useObservationEvidenceEval();
  const activeTraceId = useRef(trace.traceId);
  const [selectedEvidenceIds, setSelectedEvidenceIds] = useState<Set<string>>(() => new Set());
  const [additionalEvidenceIds, setAdditionalEvidenceIds] = useState('');
  const [datasetId, setDatasetId] = useState(() => `manual:${trace.traceId}`);
  const [labelRevision, setLabelRevision] = useState('manual:1');
  const [lastRun, setLastRun] = useState<EvalRunV1 | null>(null);
  const [submissionTraceId, setSubmissionTraceId] = useState('');
  activeTraceId.current = trace.traceId;
  const evidence = uniqueTraceEvidence(trace.evidence);
  const canRun = trace.status === 'completed'
    && !detail.truncated
    && Boolean(datasetId.trim())
    && Boolean(labelRevision.trim())
    && !evidenceEval.isPending
    && !aiJudgeEval.isPending;

  useEffect(() => {
    evidenceEval.reset();
    setSelectedEvidenceIds(new Set());
    setAdditionalEvidenceIds('');
    setDatasetId(`manual:${trace.traceId}`);
    setLabelRevision('manual:1');
    setLastRun(null);
    setSubmissionTraceId('');
    aiJudgeEval.reset();
  }, [trace.traceId]);

  async function submitAiJudge(): Promise<void> {
    const submittedTraceId = trace.traceId;
    setSubmissionTraceId(submittedTraceId);
    try {
      const result = await aiJudgeEval.mutateAsync({ traceId: submittedTraceId });
      if (activeTraceId.current !== submittedTraceId) return;
      setLastRun(result);
      await evals.refetch();
    } catch {
      // Keep the current trace and existing Eval list visible when the run fails.
    }
  }

  async function submitHumanEvidence(): Promise<void> {
    const submittedTraceId = trace.traceId;
    setSubmissionTraceId(submittedTraceId);
    const body: ObservabilityEvidenceEvalRequestV1 = {
      schemaVersion: 'rag-ime.observability-evidence-eval-request.v1',
      traceId: submittedTraceId,
      requiredEvidenceIds: [
        ...new Set([
          ...selectedEvidenceIds,
          ...parseAdditionalEvidenceIds(additionalEvidenceIds),
        ]),
      ],
      datasetId: datasetId.trim(),
      labelRevision: labelRevision.trim(),
      truthKind: 'human',
    };
    try {
      const result = await evidenceEval.mutateAsync(body);
      if (activeTraceId.current !== submittedTraceId) return;
      setLastRun(result);
      if (activeTraceId.current === submittedTraceId) await evals.refetch();
    } catch {
      // Keep the current trace and Eval list visible when the run fails.
    }
  }

  function toggleEvidence(evidenceId: string): void {
    setSelectedEvidenceIds((current) => {
      const next = new Set(current);
      if (next.has(evidenceId)) next.delete(evidenceId);
      else next.add(evidenceId);
      return next;
    });
  }

  return (
    <section aria-label="Trace Eval" className="observation-eval">
      <header className="observation-eval__heading">
        <span><ShieldCheck aria-hidden="true" size={14} />Trace Eval</span>
        <small>只显示已真实记录的评估结果</small>
      </header>
      {evals.error ? (
        <InlineNotice title="Eval 记录暂时不可用" tone="warning">
          当前 Trace 仍然保留；稍后可以重新读取 Eval。
        </InlineNotice>
      ) : null}
      {evals.isPending ? <div className="observation-eval__loading" role="status">正在读取 Eval 记录…</div> : null}
      {evals.data?.truncated ? (
        <InlineNotice title={`当前显示最近 ${evals.data.items.length} / 共 ${evals.data.total} 条 Eval`} tone="warning">
          Eval 列表已截断；这里不把当前窗口当作完整评估历史。
        </InlineNotice>
      ) : null}
      {!evals.isPending && !evals.error ? <EvalSummaryList items={evals.data?.items ?? []} /> : null}
      <div className="observation-eval__actions">
        <span>模型评审：<strong>Luna Max</strong> · 结果标记为 AI 评审估计</span>
        <Button
          disabled={!canRun}
          leadingIcon={<Brain size={15} />}
          loading={aiJudgeEval.isPending}
          onClick={() => void submitAiJudge()}
          size="small"
        >
          运行 Luna Max Eval
        </Button>
      </div>
      {aiJudgeEval.error && submissionTraceId === trace.traceId ? (
        <InlineNotice title="AI Judge 未完成" tone="danger">
          当前 Trace 和 Eval 记录保持不变，请稍后重试。
        </InlineNotice>
      ) : null}
      <Disclosure
        className="observation-eval__disclosure"
        summary={<><FileCheck2 aria-hidden="true" size={14} />人工 evidence-set 标注</>}
      >
        <div className="observation-eval__form">
          <p className="observation-eval__hint">
            勾选正确结果本应依赖的证据。跨检索阶段重复出现的 evidenceId 已合并；默认不会自动勾选。
          </p>
          <div className="observation-eval__fields">
            <label>
              <span>datasetId</span>
              <input
                aria-label="datasetId"
                onChange={(event) => setDatasetId(event.target.value)}
                value={datasetId}
              />
            </label>
            <label>
              <span>labelRevision</span>
              <input
                aria-label="labelRevision"
                onChange={(event) => setLabelRevision(event.target.value)}
                value={labelRevision}
              />
            </label>
          </div>
          <fieldset className="observation-eval__evidence">
            <legend>Required evidence · {selectedEvidenceIds.size} 已选</legend>
            {evidence.length ? evidence.map((item) => (
              <label className="observation-eval__evidence-row" key={item.evidenceId}>
                <input
                  checked={selectedEvidenceIds.has(item.evidenceId)}
                  onChange={() => toggleEvidence(item.evidenceId)}
                  type="checkbox"
                />
                <span>
                  <strong>{item.evidenceId}</strong>
                  <small>{item.sourceRef} · {item.stages.join(' / ')}</small>
                </span>
              </label>
            )) : <p className="observation-eval__empty">这次 Trace 没有可标注的 evidenceId。</p>}
          </fieldset>
          <label className="observation-eval__missing-evidence">
            <span>Trace 未产出、但正确结果仍应包含的 evidenceId</span>
            <textarea
              aria-label="额外 required evidenceId"
              onChange={(event) => setAdditionalEvidenceIds(event.target.value)}
              placeholder="每行一个 evidenceId；用于记录漏检并计算 false negative"
              rows={3}
              value={additionalEvidenceIds}
            />
          </label>
          {detail.truncated || trace.status !== 'completed' ? (
            <InlineNotice title="当前 Trace 不能运行人工 Eval" tone="warning">
              {detail.truncated ? 'Trace 仍是截断窗口，需等完整 Trace 后再标注。' : 'Trace 尚未完成，完成后才能运行。'}
            </InlineNotice>
          ) : null}
          <div className="observation-eval__actions">
            <span>提交 authority：<strong>human</strong></span>
            <Button
              disabled={!canRun}
              leadingIcon={<ShieldCheck size={15} />}
              loading={evidenceEval.isPending}
              onClick={() => void submitHumanEvidence()}
              size="small"
            >
              运行人工 Eval
            </Button>
          </div>
          {evidenceEval.error && submissionTraceId === trace.traceId ? (
            <InlineNotice title="人工 Eval 未完成" tone="danger">
              当前 Trace 和 Eval 记录保持不变，请检查输入后重试。
            </InlineNotice>
          ) : null}
        </div>
      </Disclosure>
      {lastRun?.traceIds.includes(trace.traceId) ? <EvalRunReceipt run={lastRun} /> : null}
    </section>
  );
}

function EvalSummaryList({ items }: { items: ObservabilityEvalListV1['items'] }) {
  if (!items.length) {
    return <p className="observation-eval__empty">这次 Trace 还没有 Eval 记录。</p>;
  }
  return (
    <ul aria-label="Trace Eval 记录" className="observation-eval__list">
      {items.map((item) => (
        <li key={item.evalRunId}>
          <div className="observation-eval__run-head">
            <strong>{evalModeLabel(item.mode)}</strong>
            <StatusBadge label={evalStatusLabel(item.status)} tone={evalStatusTone(item.status)} />
          </div>
          <div className="observation-eval__run-meta">
            {item.suiteBinding ? <span>Suite：{item.suiteBinding.suiteId} · {item.suiteBinding.suiteRevision}</span> : null}
            <span>指标 authority：{metricAuthorityLabel(item.metricAuthority)}</span>
            <span>真值：{truthStatusLabel(item.truthStatus)}</span>
            <span>评估者：{item.evaluatorDisplayName}</span>
            <span>{item.datasetId} · {item.labelRevision}</span>
          </div>
          {Object.keys(item.metrics).length ? (
            <dl className="observation-eval__metrics">
              {Object.entries(item.metrics).map(([key, value]) => (
                <div key={key}><dt>{key}</dt><dd>{formatScore(value)}</dd></div>
              ))}
            </dl>
          ) : <small className="observation-eval__no-metrics">暂无指标</small>}
        </li>
      ))}
    </ul>
  );
}

function EvalRunReceipt({ run }: { run: EvalRunV1 }) {
  const isAiJudge = run.mode === 'ai_judge';
  return (
    <div className="observation-eval__receipt" role="status">
      <strong>{isAiJudge ? 'Luna Max AI Judge 已提交' : '人工 Eval 已提交'}</strong>
      <span>{run.evalRunId} · {evalStatusLabel(run.status)}</span>
      <small>{isAiJudge ? 'AI 评审估计' : '真值 authority'}：{metricAuthorityLabel(run.metricAuthority)} · truthKind：{run.truth.status}</small>
    </div>
  );
}

type UniqueTraceEvidence = TraceEvidence & { stages: string[] };

function uniqueTraceEvidence(items: TraceEvidence[]): UniqueTraceEvidence[] {
  const byId = new Map<string, UniqueTraceEvidence>();
  for (const item of items) {
    const existing = byId.get(item.evidenceId);
    if (existing) {
      if (!existing.stages.includes(item.evidenceStage)) existing.stages.push(item.evidenceStage);
      continue;
    }
    byId.set(item.evidenceId, { ...item, stages: [item.evidenceStage] });
  }
  return [...byId.values()];
}

function parseAdditionalEvidenceIds(value: string): string[] {
  return [...new Set(
    value
      .split(/[\n,]/u)
      .map((item) => item.trim())
      .filter(Boolean),
  )];
}

function evalModeLabel(mode: ObservabilityEvalListV1['items'][number]['mode']): string {
  return mode === 'ground_truth' ? 'Ground truth · 人工/冻结真值' : 'AI Judge · 模型估计';
}

function metricAuthorityLabel(authority: ObservabilityEvalListV1['items'][number]['metricAuthority']): string {
  return authority === 'ground_truth' ? 'ground_truth 真值' : 'ai_judge_estimate AI 估计';
}

function truthStatusLabel(status: ObservabilityEvalListV1['items'][number]['truthStatus']): string {
  return ({ none: '未绑定', human: '人工', frozen: '冻结' })[status];
}

function evalStatusLabel(status: ObservabilityEvalListV1['items'][number]['status']): string {
  return ({ queued: '排队', running: '运行中', completed: '已完成', failed: '失败' })[status];
}

function evalStatusTone(status: ObservabilityEvalListV1['items'][number]['status']): 'success' | 'danger' | 'info' | 'neutral' {
  if (status === 'completed') return 'success';
  if (status === 'failed') return 'danger';
  if (status === 'running') return 'info';
  return 'neutral';
}

type CanonicalTrace = ObservabilityTraceGetV1['trace'];
type TraceSpan = CanonicalTrace['spans'][number];
type TraceEvidence = CanonicalTrace['evidence'][number];

function spanDurationLabel(span: TraceSpan): string {
  if (span.durationMs !== null) return formatDuration(span.durationMs);
  return span.recorded ? '未提供' : '未记录';
}

function rankChangeLabel(evidence: TraceEvidence): string {
  return `${evidence.rankBefore ?? '—'} → ${evidence.rankAfter ?? '—'}`;
}

function scoreLabel(scores: TraceEvidence['scores']): string {
  const entries = Object.entries(scores);
  return entries.length
    ? entries.map(([key, value]) => `${key} ${formatScore(value)}`).join(' · ')
    : '未记录';
}

function formatScore(value: number): string {
  if (!Number.isFinite(value)) return '未记录';
  return Number.isInteger(value) ? String(value) : value.toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
}

function formatBytes(value: number): string {
  if (value < 1_024) return `${value} B`;
  if (value < 1_048_576) return `${(value / 1_024).toFixed(1)} KB`;
  return `${(value / 1_048_576).toFixed(1)} MB`;
}

function ObservationFactList({ facts, nested = false }: { facts: [string, string][]; nested?: boolean }) {
  return (
    <dl className="observation-facts" data-nested={nested || undefined}>
      {facts.map(([label, value], index) => (
        <div key={`${label}:${value}:${index}`}>
          <dt>{label}</dt>
          <dd title={value}>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function observationFacts(item: ObservationEventV1, excludedMetricKeys = new Set<string>()): [string, string][] {
  const facts: [string, string][] = [];
  if (item.durationMs !== null) facts.push(['耗时', formatDuration(item.durationMs)]);
  const model = primitiveAttribute(item.attributes.modelName) || primitiveAttribute(item.attributes.model);
  const provider = primitiveAttribute(item.attributes.provider);
  if (model) facts.push(['模型', model]);
  if (provider) facts.push(['模型服务', provider]);
  for (const [key, value] of Object.entries(item.metrics)) {
    if (excludedMetricKeys.has(key) || value === null || typeof value === 'object') continue;
    facts.push([metricLabel(key), displayMetric(key, value)]);
  }
  if (item.refs.length) facts.push(['引用', String(item.refs.length)]);
  facts.push(['隐私', privacyLabel(item.privacyClass)]);
  return facts;
}

type ObservationProgress = {
  current: number;
  total: number;
  sourceKeys: string[];
};

function observationProgress(item: ObservationEventV1): ObservationProgress | null {
  if (item.category !== 'tool') return null;
  const nested = item.metrics.progress;
  if (nested && typeof nested === 'object' && !Array.isArray(nested)) {
    const current = finiteMetric((nested as Record<string, unknown>).current);
    const total = finiteMetric((nested as Record<string, unknown>).total);
    if (current !== null && total !== null && total > 0) {
      return { current: Math.min(current, total), total, sourceKeys: ['progress'] };
    }
  }
  for (const [currentKey, totalKey] of [
    ['progressCurrent', 'progressTotal'],
    ['completedCount', 'totalCount'],
    ['processedCount', 'totalCount'],
    ['scannedCount', 'totalCount'],
    ['scanned', 'total'],
    ['current', 'total'],
  ] as const) {
    const current = finiteMetric(item.metrics[currentKey]);
    const total = finiteMetric(item.metrics[totalKey]);
    if (current !== null && total !== null && total > 0) {
      return { current: Math.min(current, total), total, sourceKeys: [currentKey, totalKey] };
    }
  }
  return null;
}

function finiteMetric(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

function categoryIcon(category: ObservationEventV1['category']): LucideIcon {
  return {
    context: Brain,
    retrieval: Database,
    memory: Brain,
    tool: Wrench,
    agent: Bot,
    room: MessagesSquare,
    intercom: KeyRound,
    approval: LockKeyhole,
    runtime: Activity,
    system: CircleDotDashed,
  }[category];
}

function observationMatches(item: ObservationEventV1, rawNeedle: string): boolean {
  const needle = rawNeedle.trim().toLocaleLowerCase();
  if (!needle) return true;
  return [
    item.summary,
    item.traceId,
    item.spanId,
    item.sessionId,
    item.roomId,
    item.turnId,
    item.runId,
    item.phase,
    item.name,
  ].some((value) => value.toLocaleLowerCase().includes(needle));
}

function primaryScope(item: ObservationEventV1): string {
  if (item.roomId) return '多人协作';
  if (item.sessionId) return '伙伴对话';
  if (item.runId) return '后台任务';
  return '一次流程';
}

function traceScope(items: ObservationEventV1[]): string {
  const item = items[0];
  return item ? primaryScope(item) : '';
}

function scopeLabel(filters: ObservationFilters): string {
  if (filters.sessionId) return '只看这段对话';
  if (filters.roomId) return '只看这个协作空间';
  if (filters.traceId) return '只看这次流程';
  if (filters.runId) return `只看子 Agent 运行 · ${filters.runId}`;
  return '限定范围';
}

function categoryLabel(value: CategoryFilter | ObservationEventV1['category']): string {
  return {
    all: '全部',
    context: '上下文',
    retrieval: '检索',
    memory: '记忆',
    tool: '工具',
    agent: '伙伴',
    room: '协作',
    intercom: '伙伴消息',
    approval: '确认',
    runtime: '系统',
    system: '系统',
  }[value];
}

function statusLabel(status: ObservationEventV1['status']): string {
  return {
    queued: '排队',
    running: '运行中',
    waiting: '等待',
    completed: '已完成',
    failed: '失败',
    cancelled: '已取消',
    expired: '已过期',
    info: '信息',
  }[status];
}

// Observation events are immutable journal entries, not the current state of
// a Session or job. A later terminal event must not make an old start look live.
function eventStatusLabel(status: ObservationEventV1['status']): string {
  return ['queued', 'running', 'waiting'].includes(status)
    ? `当时${statusLabel(status)}`
    : statusLabel(status);
}

/**
 * Shared PAWOS status language: queued stays neutral, running uses the active
 * blue, waiting alone is amber, completed is quiet green, failed is red.
 * Folding queued/running into amber would overstate how much needs attention.
 */
function statusTone(status: ObservationEventV1['status']): 'success' | 'warning' | 'danger' | 'info' | 'neutral' {
  if (status === 'completed') return 'success';
  if (status === 'failed' || status === 'expired') return 'danger';
  if (status === 'running') return 'info';
  if (status === 'waiting') return 'warning';
  return 'neutral';
}

function connectionLabel(state: ObservationConnectionState): string {
  return {
    connecting: '正在连接',
    live: '实时',
    reconnecting: '正在重连',
    offline: '快照模式',
  }[state];
}

function connectionTone(state: ObservationConnectionState): 'success' | 'warning' | 'neutral' {
  if (state === 'live') return 'success';
  if (state === 'offline' || state === 'reconnecting') return 'warning';
  return 'neutral';
}

function phaseLabel(phase: string): string {
  return ({
    tool_started: '开始',
    tool_progress: '执行中',
    tool_finished: '结束',
    turn_completed: '回合完成',
    turn_failed: '回合失败',
    message_completed: '消息完成',
    compaction_started: '开始压缩',
    compaction_completed: '压缩完成',
    status_changed: '状态更新',
    started: '已捕获',
    retrieval_complete: '检索完成',
    draft_ready: '草案就绪',
    approval_required: '等待确认',
    delivered: '已送达',
    applied: '已应用',
    rolled_back: '已回滚',
  } as Record<string, string>)[phase] ?? '其他步骤';
}

function metricLabel(key: string): string {
  return ({
    argumentFieldCount: '参数字段',
    resultFieldCount: '结果字段',
    evidenceCount: '证据',
    candidateCount: '候选',
    elapsedMs: '累计耗时',
    eventCount: '输入事件',
    changeCount: '变更',
    messageCount: '消息',
    characterCount: '字符',
    pendingEventCount: '待整理',
    pendingDraftCount: '待审草案',
    contextTokens: '上下文用量',
    contextWindowTokens: '窗口',
    contextPercent: '上下文占用',
    remainingTokens: '剩余',
    compactAtTokens: '压缩阈值',
    tokensUntilCompact: '距压缩',
    inputTokens: '输入用量',
    outputTokens: '输出用量',
    cacheReadTokens: '缓存读取',
    cacheWriteTokens: '缓存写入',
    totalTokens: '总用量',
    cacheHitPercent: '缓存命中',
    compactionCount: '压缩次数',
    tokensBefore: '压缩前',
    estimatedTokensAfter: '压缩后约',
  } as Record<string, string>)[key] ?? '其他数据';
}

function displayMetric(key: string, value: unknown): string {
  if (key.endsWith('Percent') && typeof value === 'number') return `${Math.round(value)}%`;
  if (key.toLocaleLowerCase().includes('token') && typeof value === 'number') return formatTokens(value);
  return displayValue(value);
}

function displayValue(value: unknown): string {
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (typeof value === 'number') return String(numberValue(value));
  return String(value);
}

function publicObservationSummary(item: ObservationEventV1): string {
  const value = item.summary.trim()
    .replace(/\bime\.memory\b/giu, '记忆工具')
    .replace(/\bsidecar\b/giu, '本机补全服务')
    .replace(/\bprovider\b/giu, '模型服务')
    .replace(/\bAgent 私信\b/giu, '伙伴消息')
    .replace(/\bAgent 回合\b/giu, '伙伴本轮')
    .replace(/\bAgent\b/giu, '伙伴');
  return value || `${categoryLabel(item.category)}进度已更新`;
}

function primitiveAttribute(value: unknown): string {
  return typeof value === 'string' || typeof value === 'number' ? String(value) : '';
}

function formatTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(value >= 100_000 ? 0 : 1)}K`;
  return String(Math.max(0, Math.round(value)));
}

function privacyLabel(value: ObservationEventV1['privacyClass']): string {
  return {
    metadata: '仅元数据',
    redacted: '已脱敏',
    owner_local: '仅本机所有者',
  }[value];
}

function observationCategory(value: string | null): CategoryFilter | null {
  return CATEGORY_FILTERS.includes(value as CategoryFilter) ? value as CategoryFilter : null;
}

function formatTime(timestampMs: number): string {
  const date = new Date(timestampMs);
  return new Intl.DateTimeFormat('zh-CN', {
    ...(date.getFullYear() !== new Date().getFullYear() ? { year: 'numeric' as const } : {}),
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).format(date);
}

function formatDuration(durationMs: number): string {
  if (durationMs < 1_000) return `${Math.round(durationMs)} ms`;
  if (durationMs < 60_000) return `${(durationMs / 1_000).toFixed(1)} s`;
  return `${Math.floor(durationMs / 60_000)}m ${Math.round((durationMs % 60_000) / 1_000)}s`;
}

function average(values: number[]): number {
  return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : 0;
}
