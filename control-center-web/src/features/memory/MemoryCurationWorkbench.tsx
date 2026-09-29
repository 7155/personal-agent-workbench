import {
  AppWindow,
  CalendarRange,
  CheckCircle2,
  CircleDotDashed,
  ListChecks,
  Play,
  RefreshCw,
  ShieldCheck,
  Sparkles,
} from 'lucide-react';
import { useState } from 'react';
import { Button, Disclosure, EmptyState } from '@/components/primitives';
import { useProductIdentity } from '@/features/identity/product-identity';
import { openPawOsRoute, usePawOsDesktop } from '@/features/paw-os/surface-context';
import {
  ManagementMutationWorkflow,
  parseManagementWorkPreview,
  parseManagementWorkReceipt,
} from '@/features/overview/management-mutation';
import {
  InlineNotice,
  ManagementSection,
  MetricStrip,
  QueryState,
  StatusBadge,
  arrayRecords,
  asRecord,
  booleanValue,
  numberValue,
  publicErrorText,
  stringValue,
} from '@/features/overview/management-ui';
import { isModelQuotaError } from '@/features/agent/public-error';
import type { JsonValue } from '@/platform/transport';
import { useMemoryCurationQueries, useMemoryLifecycleQueries } from './api';
import { knowledgeMutationPathIds, useKnowledgeMutationBoundary } from './knowledge-workbench-api';

export function MemoryCurationWorkbench({
  enabled,
  onOpenTimeline,
}: {
  enabled: boolean;
  onOpenTimeline: (date: string) => void;
}) {
  const identity = useProductIdentity();
  const desktop = usePawOsDesktop();
  const queries = useMemoryCurationQueries(enabled);
  const lifecycle = useMemoryLifecycleQueries(enabled);
  const mutationBoundary = useKnowledgeMutationBoundary();
  const [editingDiffId, setEditingDiffId] = useState(0);
  const [editError, setEditError] = useState('');
  const [startError, setStartError] = useState('');
  const batchSize = 6;
  const statusPayload = asRecord(queries.status.data);
  const compileState = asRecord(statusPayload.compileState);
  const ownerCuration = asRecord(statusPayload.ownerCuration);
  const modelCuration = asRecord(statusPayload.modelCuration);
  const modelStateCounts = asRecord(modelCuration.stateCounts);
  const ownerScopes = arrayRecords(ownerCuration.scopes);
  const backlog = asRecord(ownerCuration.backlog);
  const backlogDays = arrayRecords(backlog.days);
  const backlogApplications = arrayRecords(backlog.applications);
  const hasBacklogFacets = Array.isArray(backlog.days) && Array.isArray(backlog.applications);
  const backlogFacetsReady = !queries.status.isPlaceholderData
    && hasBacklogFacets && booleanValue(backlog.facetsReady, true);
  const latestModelRun = arrayRecords(modelCuration.runs)[0] ?? {};
  const bookProjection = asRecord(statusPayload.bookProjection);
  const memoryProjection = asRecord(statusPayload.projection);
  const projectionFreshness = asRecord(
    memoryProjection.freshness ?? asRecord(memoryProjection.lastReport).freshness,
  );
  const hasGovernedStatus = Object.keys(ownerCuration).length > 0;
  const pendingKnown = knownCount(ownerCuration.pendingSourceCount) !== null;
  const governedPending = numberValue(ownerCuration.pendingSourceCount);
  const governedNeedsReview = numberValue(ownerCuration.needsReviewSourceCount);
  const modelRunning = numberValue(modelStateCounts.running);
  const modelResumable = numberValue(modelStateCounts.resumable);
  const vectorCoverage = numberValue(projectionFreshness.vectorCoverage);
  const vectorFingerprint = stringValue(projectionFreshness.providerFingerprint);
  const automaticOrganizationEnabled = !queries.status.isPending && !queries.status.isPlaceholderData
    && Object.keys(statusPayload).length > 0 && stringValue(statusPayload.policy) !== 'disabled';
  const automaticOrganizationAutoApply = booleanValue(statusPayload.autoApply);
  const failedOwnerScope = ownerScopes.find((scope) => (
    stringValue(scope.status) === 'backoff' || Boolean(stringValue(scope.lastError))
  ));
  const payload = asRecord(queries.run.data);
  const run = asRecord(payload.run);
  const changes = arrayRecords(run.changes);
  const runId = stringValue(run.runId, queries.runId);
  const runStatus = stringValue(run.status);
  const stale = booleanValue(payload.stale);
  const selectedCount = changes.filter((change) => booleanValue(change.selected)).length;
  const rejectedCount = changes.length - selectedCount;
  const error = (!queries.status.data ? queries.status.error : null) as Error | null;
  const pending = queries.status.isPending || (Boolean(queries.runId) && queries.run.isPending);
  const jobPayload = asRecord(queries.trigger.isPending ? undefined : queries.job.data);
  const jobProgress = asRecord(jobPayload.progress);
  const jobState = stringValue(jobPayload.state, queries.jobState);
  const catalogJob = jobPayload.catalogOnly === true
    || (queries.trigger.isPending && queries.trigger.variables?.catalogOnly === true);
  const jobActive = jobState === 'queued' || jobState === 'running' || queries.trigger.isPending;
  const jobExpired = jobState === 'expired';
  const jobFailed = jobState === 'failed' || jobExpired;
  const jobReadError = queries.job.error;
  const lifecycleJobs = arrayRecords(asRecord(lifecycle.status.data).jobs);
  const lifecycleFailedJob = lifecycleJobs.find((job) => (
    ['paused', 'stale', 'retry_wait'].includes(stringValue(job.state))
    && Boolean(stringValue(job.error))
  ));
  const scopeTotals = ownerScopes.map((scope) => knownCount(scope.totalSourceCount));
  const totalSourceCount = scopeTotals.length && scopeTotals.every((value) => value !== null)
    ? scopeTotals.reduce<number>((total, value) => total + (value ?? 0), 0) : null;
  const sourceProgress = !catalogJob && stringValue(jobProgress.phase) === 'owner_memory_curation';
  const liveProgress = jobActive && sourceProgress;
  const useJobProgress = sourceProgress && (jobActive || queries.status.dataUpdatedAt <= queries.job.dataUpdatedAt);
  const displayTotalSourceCount = useJobProgress ? knownCount(jobProgress.totalSourceCount) ?? totalSourceCount : totalSourceCount;
  const displayPendingSourceCount = useJobProgress ? knownCount(jobProgress.pendingSourceCount) ?? (pendingKnown ? governedPending : null) : pendingKnown ? governedPending : null;
  const displayOrganizedSourceCount = displayTotalSourceCount !== null && displayPendingSourceCount !== null && displayTotalSourceCount >= displayPendingSourceCount
    ? displayTotalSourceCount - displayPendingSourceCount : null;
  const progressPercent = displayTotalSourceCount && displayOrganizedSourceCount !== null
    ? Math.round(displayOrganizedSourceCount / displayTotalSourceCount * 100) : null;
  const receiptProcessed = sourceProgress ? knownCount(jobProgress.processedSourceCount) : null;
  const receiptPending = sourceProgress ? knownCount(jobProgress.pendingSourceCount) : null;
  const coveredThroughDate = stringValue(backlog.coveredThroughDate);
  const targetDate = stringValue(backlog.targetDate, localToday());
  const caughtUp = !queries.status.isPlaceholderData && pendingKnown && governedPending === 0
    && backlog.caughtUpThroughToday !== false && !jobActive;
  const hasDraft = Boolean(runId) && runStatus === 'draft';
  const blockingDraft = hasDraft && !automaticOrganizationAutoApply;
  const awaitingJobReceipt = Boolean(queries.jobId) && !jobState;
  const startBlocked = !automaticOrganizationEnabled || !pendingKnown || queries.status.isPlaceholderData
    || caughtUp || blockingDraft || jobActive || awaitingJobReceipt;
  const draftKey = `${runId}:${changes.map((change) => `${numberValue(change.diffId)}:${booleanValue(change.selected)}`).join(',')}`;
  const applyBlockedReason = !runId
    ? `等待自动整理，或让${identity.assistantName}现在准备一份草案。`
    : stale
      ? '这份草案基于旧数据，请重新生成后再应用。'
      : runStatus !== 'draft'
        ? '当前整理批次已经结束，不能重复应用。'
        : !booleanValue(payload.canApply)
          ? '当前草案不能完成审核，请刷新后重试。'
          : '';
  const refresh = () => void Promise.all([
    queries.status.refetch(),
    ...(queries.jobId ? [queries.job.refetch()] : [queries.latestJob.refetch()]),
    ...(queries.runId ? [queries.run.refetch()] : []),
  ]);

  return (
    <div className="memory-curation">
      <div className="memory-curation__header">
        <div>
          <h2>整理到今天</h2>
          <span>{automaticOrganizationAutoApply
            ? '从上次位置继续推进；按日期和应用核对来源，治理通过后自动应用。'
            : '从上次位置继续推进；按日期和应用核对来源，再审核本轮形成的记忆草案。'}</span>
        </div>
        <div className="memory-curation__actions">
          <Button
            disabled={jobActive || !automaticOrganizationEnabled || asRecord(statusPayload.catalogConsolidation).enabled === false}
            onClick={() => void startCatalogConsolidation()}
            size="small"
          >整理已有主题</Button>
          <Button leadingIcon={<Sparkles size={15} />} onClick={handoffToAgent} size="small" variant="quiet">补充整理要求</Button>
        </div>
      </div>
      <QueryState error={error} isPending={pending} onRetry={refresh}>
        {queries.status.error && queries.status.data ? <InlineNotice title="整理概览暂未更新" tone="warning">
          先保留上次读取的来源数量，任务回执会单独更新。
          <Button onClick={refresh} size="small" variant="quiet">重新读取整理状态</Button>
        </InlineNotice> : null}
        <ManagementSection
          title="整理进度"
          description={automaticOrganizationAutoApply
            ? '原始输入不会被改写；每次只处理一批，通过治理校验后自动应用并保留回滚回执。'
            : '原始输入不会被改写；每次只处理一批，形成草案后停下来等你审核。'}
          trailing={<StatusBadge label={!pendingKnown ? '状态待确认' : caughtUp ? '已到今天' : `${governedPending} 条待整理`} tone={!pendingKnown ? 'neutral' : caughtUp ? 'success' : 'warning'} />}
        >
          {hasGovernedStatus ? (
            <>
              <div className="memory-curation__progress-card">
                <div className="memory-curation__progress-copy">
                  <strong>{!pendingKnown ? '整理进度待确认' : caughtUp ? '已经整理到今天' : coveredThroughDate ? `${formatCalendarDate(coveredThroughDate)} → ${formatCalendarDate(targetDate)}` : '从已有来源继续整理'}</strong>
                    <p>{!pendingKnown
                      ? '服务尚未提供来源数量；读取状态后再继续。'
                      : caughtUp
                      ? '目前没有新的候选来源等待整理。'
                      : !backlogFacetsReady
                        ? queries.status.isPlaceholderData
                          ? `${displayPendingSourceCount} 条来源待整理，正在读取日期和应用分布。`
                          : `${displayPendingSourceCount} 条来源待整理；日期和应用分布尚未提供。`
                        : `${displayPendingSourceCount} 条来源分布在 ${numberValue(backlog.pendingDayCount)} 天、${backlogApplications.length} 个应用中${jobActive ? '，进度会随当前批次更新' : '。'}`}</p>
                </div>
                <div className="memory-curation__progress-meter">
                  <div className="memory-curation__progress-label">
                    <span>{displayOrganizedSourceCount !== null ? `已处理 ${displayOrganizedSourceCount} / ${displayTotalSourceCount}` : '总量尚未确认'}</span>
                    <strong>{progressPercent === null ? '—' : `${progressPercent}%`}</strong>
                  </div>
                  <div aria-label={progressPercent === null ? '记忆来源整理进度待确认' : `记忆来源整理进度 ${progressPercent}%`} aria-valuemax={100} aria-valuemin={0} aria-valuenow={progressPercent ?? undefined} className="memory-curation__progress-track" data-unknown={progressPercent === null || undefined} role="progressbar">
                    {progressPercent !== null ? <span style={{ width: `${progressPercent}%` }} /> : null}
                  </div>
                  <div className="memory-curation__progress-dates">
                    <span><CheckCircle2 size={14} /> {coveredThroughDate ? `已整理到 ${formatCalendarDate(coveredThroughDate)}` : '覆盖日期尚未确认'}</span>
                    <span><CalendarRange size={14} /> 目标 {formatCalendarDate(targetDate)}</span>
                  </div>
                </div>
                <div className="memory-curation__runner">
                  <label>
                    <span>每轮整理上限</span>
                    <strong className="memory-curation__batch-bound">每批最多 {batchSize} 条来源 · 本次最多 8 批</strong>
                  </label>
                  <Button
                    disabled={startBlocked}
                    leadingIcon={caughtUp ? <CheckCircle2 size={16} /> : <Play size={16} />}
                    loading={jobActive}
                    onClick={() => void startCuration()}
                  >
                    {jobActive ? '正在整理' : awaitingJobReceipt ? '等待任务回执' : blockingDraft ? '先审核本批' : caughtUp ? '已整理到今天' : failedOwnerScope || jobFailed ? '继续整理' : '开始整理'}
                  </Button>
                  {!pendingKnown ? <Button onClick={refresh} size="small" variant="quiet">重新读取整理状态</Button> : null}
                  {!automaticOrganizationEnabled ? <small>{queries.status.isPlaceholderData ? '正在读取整理设置…' : '自动整理已关闭，可在记忆偏好中开启。'}</small> : null}
                </div>
              </div>

              <p className="memory-curation__source-note">原始来源用于核对；整理后才形成可复用的记忆和主题。{governedNeedsReview > 0 ? `另有 ${governedNeedsReview} 条来源需要判断，不会自动当作记忆使用。` : ''}</p>
              {receiptProcessed !== null ? <p className="memory-curation__receipt" role="status">本轮已处理 {receiptProcessed} 条{receiptPending !== null ? ` · 回执剩余 ${receiptPending} 条` : ''}</p> : null}

              {jobReadError ? <InlineNotice title="暂时无法读取任务进度" tone="warning">
                尚不能判断任务结果；保留上次回执，重新读取不会再启动整理。
                <Button loading={queries.job.isFetching} onClick={() => void queries.job.refetch()} size="small" variant="quiet">重新读取任务进度</Button>
              </InlineNotice> : null}

              {startError || jobFailed ? (
                <InlineNotice title={jobExpired ? '整理任务已过期' : startError ? '整理请求未能确认' : '本轮没有完成'} tone={jobExpired ? 'warning' : 'danger'}>
                  {startError || (jobExpired
                    ? '没有找到这个任务的可恢复回执；已有记忆仍保留，可以重新整理。'
                    : publicErrorText(jobPayload.error, '整理任务没有完成；进度已经保留，可以重试。'))}
                </InlineNotice>
              ) : jobState === 'completed' ? (
                <InlineNotice title={catalogJob ? '已有主题整理完成' : '本轮处理完成'} tone="success">{receiptPending !== null && receiptPending > 0 ? `回执还有 ${receiptPending} 条来源待处理，可继续下一轮。` : '本轮任务已结束。'}{automaticOrganizationAutoApply ? '已保存的结果以记忆库为准。' : '如果产生了草案，请在下方逐项审核。'}</InlineNotice>
              ) : jobActive ? (
                <InlineNotice title={catalogJob ? '正在核对已有主题' : liveProgress ? '正在读取并整理本轮来源' : '后台记忆任务正在运行'} tone="info">{catalogJob
                  ? '正在检查主题和当前成员；不会推进新来源整理进度。'
                  : liveProgress && receiptProcessed !== null
                    ? `当前批次已处理 ${receiptProcessed} 条${receiptPending !== null ? `，剩余 ${receiptPending} 条` : ''}；完成后会刷新来源和日期分布。`
                    : '你可以留在此页，完成后会自动刷新；原始来源会保留。'}</InlineNotice>
              ) : null}

              {failedOwnerScope && !jobActive ? (
                <InlineNotice title="上次自动整理已暂停" tone="warning">
                  {knownCount(failedOwnerScope.consecutiveFailures) !== null ? `连续失败 ${numberValue(failedOwnerScope.consecutiveFailures)} 次；` : ''}{ownerRunErrorLabel(stringValue(failedOwnerScope.lastError))} {blockingDraft
                    ? '请先审核下方本批草案，再继续处理来源。'
                    : caughtUp ? '当前没有待整理来源，已有记忆可以继续查阅。'
                      : !automaticOrganizationEnabled ? '请先在记忆偏好中开启自动整理。'
                        : !pendingKnown ? '请先重新读取整理状态，再继续处理来源。'
                          : '可以使用上方“继续整理”从保留位置恢复。'}
                </InlineNotice>
              ) : null}

              <div className="memory-curation__backlog-grid">
                <section aria-labelledby="memory-curation-days-title" className="memory-curation__days">
                  <header>
                    <div>
                      <CalendarRange size={17} />
                      <strong id="memory-curation-days-title">按日期推进</strong>
                    </div>
                    <span>{backlogFacetsReady ? `${backlogDays.length} 天` : queries.status.isPlaceholderData ? '读取中' : '未提供'}</span>
                  </header>
                  {backlogDays.length ? (
                    <ol aria-label="待整理日期">
                      {backlogDays.map((day) => {
                        const apps = arrayRecords(day.applications);
                        const date = stringValue(day.date);
                        return (
                          <li key={date}>
                            <button aria-label={`查看 ${date} 时间线`} onClick={() => onOpenTimeline(date)} type="button">
                              <time dateTime={date}>{formatDayLabel(date)}</time>
                              <span className="memory-curation__day-apps">{apps.slice(0, 3).map((app) => applicationLabel(stringValue(app.name))).filter(Boolean).join(' · ') || '未知应用'}</span>
                              <strong>{numberValue(day.pendingSourceCount)} 条</strong>
                              <span className="memory-curation__day-state">
                                <StatusBadge label={numberValue(day.needsReviewSourceCount) ? `${numberValue(day.needsReviewSourceCount)} 条需判断` : '待整理'} tone={numberValue(day.needsReviewSourceCount) ? 'warning' : 'neutral'} />
                              </span>
                            </button>
                          </li>
                        );
                      })}
                    </ol>
                  ) : <p className="memory-curation__empty-copy">{backlogFacetsReady ? '没有待整理日期。' : queries.status.isPlaceholderData ? '正在读取日期分布。' : '服务尚未提供日期分布。'}</p>}
                </section>

                <section aria-labelledby="memory-curation-apps-title" className="memory-curation__apps">
                  <header>
                    <div>
                      <AppWindow size={17} />
                      <strong id="memory-curation-apps-title">来源应用</strong>
                    </div>
                    <span>{backlogFacetsReady ? `${backlogApplications.length} 个` : queries.status.isPlaceholderData ? '读取中' : '未提供'}</span>
                  </header>
                  {backlogApplications.length ? (
                    <ol aria-label="待整理来源应用">
                      {backlogApplications.map((application) => {
                        const count = numberValue(application.count);
                        const share = governedPending ? Math.min(100, Math.max(0, Math.round((count / governedPending) * 100))) : 0;
                        return (
                          <li key={stringValue(application.name)}>
                            <div><strong>{applicationLabel(stringValue(application.name))}</strong><span>{count} 条</span></div>
                            <div aria-hidden="true" className="memory-curation__app-meter"><span style={{ width: `${share}%` }} /></div>
                            <small>最近 {formatRelativeDate(numberValue(application.lastSourceAtMs))}</small>
                          </li>
                        );
                      })}
                    </ol>
                  ) : <p className="memory-curation__empty-copy">{backlogFacetsReady ? '尚无应用分布。' : queries.status.isPlaceholderData ? '正在读取应用分布。' : '服务尚未提供应用分布。'}</p>}
                </section>
              </div>

              <Disclosure className="memory-curation__technical" summary="运行与索引详情">
                <MetricStrip items={[
                  { label: '待整理来源', value: pendingKnown ? governedPending : '—', detail: '等待分批整理', icon: RefreshCw, tone: governedPending ? 'warning' : 'neutral' },
                  { label: '需人工判定', value: knownCount(ownerCuration.needsReviewSourceCount) ?? '—', detail: '不会交给模型猜测', icon: ShieldCheck, tone: governedNeedsReview ? 'warning' : 'neutral' },
                  { label: '正在运行', value: knownCount(modelStateCounts.running) ?? '—', detail: '独立记忆会话', icon: Sparkles, tone: modelRunning ? 'info' : 'neutral' },
                  { label: '可继续', value: knownCount(modelStateCounts.resumable) ?? '—', detail: '输入与进度已保留', icon: RefreshCw, tone: modelResumable ? 'warning' : 'neutral' },
                  { label: '主题整理', value: booleanValue(bookProjection.inSync) ? '已同步' : '需检查', detail: `${numberValue(bookProjection.unbookedAtomCount)} 条记忆尚未归入主题`, icon: ListChecks, tone: booleanValue(bookProjection.inSync) ? 'success' : 'warning' },
                  { label: '检索索引', value: booleanValue(projectionFreshness.fresh) ? '已同步' : '有积压', detail: `${numberValue(projectionFreshness.retrievalDocuments)} 个文档 · ${vectorProjectionLabel(vectorFingerprint, vectorCoverage)}`, icon: ShieldCheck, tone: booleanValue(projectionFreshness.fresh) ? 'info' : 'warning' },
                ]} />
                {stringValue(latestModelRun.lastError) ? (
                  <InlineNotice title="模型会话可继续" tone="warning">{modelRunErrorLabel(stringValue(latestModelRun.lastError))}；冻结输入和进度仍然保留。</InlineNotice>
                ) : null}
              </Disclosure>
            </>
          ) : (
            <InlineNotice title="可用整理范围" tone={booleanValue(statusPayload.due) ? 'warning' : 'info'}>
              {knownCount(compileState.undraftedEventCount) !== null ? `当前服务确认还有 ${numberValue(compileState.undraftedEventCount)} 条来源尚未整理；更细的分批进度暂不可用。` : '服务尚未提供待整理来源数量，请重新读取状态。'}
              <Button onClick={refresh} size="small" variant="quiet">重新读取整理状态</Button>
            </InlineNotice>
          )}
        </ManagementSection>

        <ManagementSection title={automaticOrganizationAutoApply ? '自动整理结果' : '本批草案'} description={automaticOrganizationAutoApply
          ? '通过 Evidence、Atom-first 与计划校验的结果会自动保存；原始来源和回滚回执继续保留。'
          : '按内容判断是否保留；不同应用的原始来源继续分开保存。'} trailing={!automaticOrganizationAutoApply && runId ? <StatusBadge label={`${selectedCount} / ${changes.length} 已选择`} tone={selectedCount ? 'success' : 'warning'} /> : undefined}>
          {queries.run.error ? <InlineNotice title="草案暂时无法读取" tone="warning">保留当前整理进度；重新读取后再核对本批内容。<Button onClick={() => void queries.run.refetch()} size="small" variant="quiet">重新读取草案</Button></InlineNotice> : null}
          {runId && !automaticOrganizationAutoApply ? (
            <>
              <MetricStrip items={[
                { label: '批次状态', value: curationStatusLabel(runStatus, stale), detail: formatCreatedAt(numberValue(run.createdAtMs)), icon: ShieldCheck, tone: runStatus === 'draft' && !stale ? 'warning' : 'success' },
                { label: '建议更新', value: changes.length, detail: `${identity.assistantName}整理结果`, icon: ListChecks },
                { label: '已选择', value: selectedCount, detail: '准备保存', icon: ShieldCheck, tone: selectedCount ? 'success' : 'warning' },
                { label: '已排除', value: rejectedCount, detail: '不会写入', icon: ListChecks, tone: 'neutral' },
              ]} />
              <InlineNotice title={stale ? '草案已过期' : '只保存勾选内容'} tone={stale ? 'warning' : 'info'}>
                {stale ? '记忆库在草案生成后已有变化，请重新整理。' : '勾选只修改草案；下方确认保存后才会更新正式记忆。'}
              </InlineNotice>
            {editError ? <InlineNotice title="草案更新失败" tone="danger">{editError}</InlineNotice> : null}
            <div className="memory-curation__list" role="list" aria-label="记忆整理建议">
              {changes.map((change) => {
                const diffId = numberValue(change.diffId);
                const selected = booleanValue(change.selected);
                const disabled = stale || runStatus !== 'draft' || editingDiffId === diffId;
                return (
                  <label className="memory-curation__row" data-selected={selected || undefined} key={diffId} role="listitem">
                    <input
                      aria-label={`选择 ${stringValue(change.title, '未命名更新')}`}
                      checked={selected}
                      disabled={disabled}
                      onChange={(event) => void updateSelection(diffId, event.target.checked)}
                      type="checkbox"
                    />
                    <span className="memory-curation__copy">
                      <span className="memory-curation__title">
                        <strong>{stringValue(change.title, '未命名更新')}</strong>
                        <StatusBadge label={diffStatusLabel(stringValue(change.status))} tone={selected ? 'success' : 'neutral'} />
                      </span>
                      <span>{stringValue(change.detail, '这项建议没有补充说明。')}</span>
                      <small>{stringValue(change.operationLabel, operationLabel(stringValue(change.operation)))} · {numberValue(change.sourceCount)} 条来源</small>
                    </span>
                  </label>
                );
              })}
            </div>
            </>
          ) : runId ? (
            <InlineNotice title="自动应用已启用" tone="info">当前 Gateway 会在治理校验通过后自动应用；历史草案不会阻塞下一批整理。</InlineNotice>
          ) : (
            <EmptyState description={caughtUp ? '新的输入出现后会继续从今天向后整理。' : '点击上方开始或继续整理；形成草案后会停在这里等待审核。'} icon={caughtUp ? CheckCircle2 : CircleDotDashed} title={caughtUp ? '已经整理到今天' : '当前没有待审核草案'} />
          )}
        </ManagementSection>

        {runId && !automaticOrganizationAutoApply ? <ManagementSection title="完成本批审核" description={selectedCount
          ? '保存所选建议并更新本机检索；完成后可以立即撤销本批次。'
          : '全部排除时只结束本批审核，不会改动正式记忆。'}>
          <ManagementMutationWorkflow
            availability={mutationBoundary.databaseAvailability(applyBlockedReason)}
            description={selectedCount
              ? '只保存上方已选择的整理建议，并更新本机检索索引。'
              : '不保存任何建议，只结束本批审核。'}
            draftKey={draftKey}
            mutationKey={['memory', 'curation', runId]}
            onApply={async (preview) => parseManagementWorkReceipt(
              await mutationBoundary.request({
                pathId: knowledgeMutationPathIds.databaseApply,
                body: {
                  runId: preview.context.runId,
                  confirm: 'apply',
                  previewToken: preview.previewToken,
                  payloadSha256: preview.payloadSha256,
                  expectedRuntimeRevision: preview.expectedRuntimeRevision,
                },
              }),
              knowledgeMutationPathIds.databaseApply,
              preview.payloadSha256,
            )}
            onApplied={refresh}
            onPreview={async () => {
              const context: Record<string, JsonValue> = { runId };
              return parseManagementWorkPreview(
                await mutationBoundary.request({
                  pathId: knowledgeMutationPathIds.databaseApplyPreview,
                  body: context,
                }),
                knowledgeMutationPathIds.databaseApply,
                context,
              );
            }}
            onRollback={async (receipt, preview) => parseManagementWorkReceipt(
              await mutationBoundary.request({
                pathId: knowledgeMutationPathIds.databaseRollback,
                body: {
                  runId: preview.context.runId,
                  confirm: 'rollback',
                  receiptId: receipt.receiptId,
                  rollbackToken: receipt.rollbackToken,
                  payloadSha256: receipt.payloadSha256,
                },
              }),
              knowledgeMutationPathIds.databaseRollback,
              preview.payloadSha256,
            )}
            onRolledBack={refresh}
            risk="R2"
            title={selectedCount ? '保存到记忆' : '排除本批建议'}
          />
        </ManagementSection> : null}
      </QueryState>
      <Disclosure className="memory-curation__support" summary="日报与检索维护">
      <ManagementSection
        title="记忆生命周期"
        description="日报使用已整理的记忆；检索维护只更新查找索引。"
        trailing={<StatusBadge label={lifecycle.status.isPending ? '读取中' : lifecycle.status.isError ? '暂不可用' : '已读取'} tone={lifecycle.status.isError ? 'warning' : lifecycle.status.isPending ? 'neutral' : 'success'} />}
      >
        <MetricStrip items={[
          { label: '来源', value: lifecycleCount(lifecycle.status.data, 'sources'), detail: '当前项目来源' },
          { label: '来源证据', value: lifecycleCount(lifecycle.status.data, 'evidence'), detail: '保留来源链' },
          { label: '已整理记忆', value: lifecycleCount(lifecycle.status.data, 'atoms'), detail: '经过治理校验' },
          { label: '刷新任务', value: Array.isArray(asRecord(lifecycle.status.data).jobs) ? (asRecord(lifecycle.status.data).jobs as unknown[]).length : '—', detail: '持久化任务' },
        ]} />
        <div className="memory-curation__actions">
          <Button
            loading={lifecycle.refresh.isPending}
            onClick={() => lifecycle.refresh.mutate({ operation: 'daily_report', date: localToday(), timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC' })}
          >生成今日日报任务</Button>
          <Button
            loading={lifecycle.refresh.isPending}
            onClick={() => lifecycle.refresh.mutate({ operation: 'retrieval_projection' })}
            variant="quiet"
          >刷新检索投影</Button>
        </div>
        {lifecycle.status.error ? <InlineNotice title="生命周期状态暂不可用" tone="warning">{publicErrorText(lifecycle.status.error, '稍后会自动重试。')}<Button onClick={() => void lifecycle.status.refetch()} size="small" variant="quiet">重新读取维护状态</Button></InlineNotice> : null}
        {lifecycle.refresh.error ? <InlineNotice title="刷新任务提交失败" tone="danger">{publicErrorText(lifecycle.refresh.error, '没有改变已有记忆；请稍后重试。')}</InlineNotice> : null}
        {lifecycleFailedJob ? <InlineNotice title="上次刷新没有完成" tone="warning">{stringValue(lifecycleFailedJob.error, '任务已暂停；输入和已有记忆保持不变。')}</InlineNotice> : null}
      </ManagementSection>
      </Disclosure>
    </div>
  );

  async function updateSelection(diffId: number, selected: boolean) {
    setEditingDiffId(diffId);
    setEditError('');
    try {
      await mutationBoundary.request({
        pathId: knowledgeMutationPathIds.databaseDraftEdit,
        body: { runId, diffId, selected },
      });
      await queries.run.refetch();
    } catch (cause) {
      setEditError(publicErrorText(cause, '未能保存这项选择，正式记忆没有变化。'));
    } finally {
      setEditingDiffId(0);
    }
  }

  async function startCuration() {
    setStartError('');
    try {
      await queries.trigger.mutateAsync({
        maxSources: batchSize,
        instruction: automaticOrganizationAutoApply
          ? '从当前已保存的整理位置继续，按时间顺序处理下一批个人记忆来源；通过治理校验后自动应用，并保留回滚回执。'
          : '从当前已保存的整理位置继续，按时间顺序处理下一批个人记忆来源；只生成可审核草案，不直接保存。',
      });
    } catch (cause) {
      setStartError(publicErrorText(cause, '未能启动本轮整理；已有进度没有变化。'));
    }
  }

  async function startCatalogConsolidation() {
    setStartError('');
    try {
      await queries.trigger.mutateAsync({
        catalogOnly: true,
        instruction: '核对已有主题目录，合并同一稳定对象与同一问题轴的重复主题；保留当前记忆成员、来源、旧引用及回滚回执。',
      });
    } catch (cause) {
      setStartError(publicErrorText(cause, '未能启动已有主题整理；可以重试。'));
    }
  }

  function handoffToAgent() {
    const prompt = automaticOrganizationAutoApply
      ? '请帮我稳妥地增量整理当前记忆：按 Evidence、Atom-first 与计划校验推进，通过治理校验后自动应用并保留回滚回执，保留原始来源，不要展示内部执行记录。完成后告诉我本批处理结果。'
      : '请帮我稳妥地增量整理当前记忆：只准备一份可逐项审核的草案，保留原始来源，不要直接保存，也不要展示内部执行记录。完成后请告诉我可以回来审核。';
    openPawOsRoute(desktop, `/agent?draft=${encodeURIComponent(prompt)}`);
  }
}

function knownCount(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

function lifecycleCount(value: unknown, key: string): number | string {
  return knownCount(asRecord(asRecord(value).counts)[key]) ?? '—';
}

function curationStatusLabel(status: string, stale: boolean): string {
  if (stale) return '需要重建';
  return {
    draft: '等待审核',
    applied: '已应用',
    partial: '部分应用',
    rolled_back: '已撤销',
    superseded: '已被替代',
    dismissed: '已全部排除',
    empty: '无需更新',
  }[status] ?? '暂无状态';
}

function diffStatusLabel(status: string): string {
  return {
    pending: '待审核',
    approved: '已选择',
    rejected: '已排除',
    applied: '已应用',
    rolled_back: '已撤销',
  }[status] ?? '未知';
}

function operationLabel(operation: string): string {
  return {
    upsert_semantic_group: '更新分组',
    upsert_semantic_tag: '更新标签',
    upsert_memory_book: '更新长期主题',
    upsert_memory_atom: '更新一条事实',
    upsert_tag_edge: '更新标签关系',
    merge_semantic_tag: '合并标签',
    add_phrase_candidate: '添加短语候选',
    add_negative_phrase: '添加负反馈',
    supersede_memory: '替代旧记忆',
  }[operation] ?? '整理记忆';
}

function formatCreatedAt(value: number): string {
  if (!value) return '尚未生成';
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(value);
}

function localToday(): string {
  const now = new Date();
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

function formatCalendarDate(value: string): string {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return '尚未开始';
  const [year, month, day] = value.split('-').map(Number);
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'long',
    day: 'numeric',
    ...(year !== new Date().getFullYear() ? { year: 'numeric' } : {}),
  }).format(new Date(year, month - 1, day));
}

function formatDayLabel(value: string): string {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return '日期未知';
  const [year, month, day] = value.split('-').map(Number);
  const date = new Date(year, month - 1, day);
  const label = new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    weekday: 'short',
  }).format(date);
  return value === localToday() ? `${label} · 今天` : label;
}

function applicationLabel(value: string): string {
  if (!value) return '未知应用';
  const knownLabels: Record<string, string> = {
    'com.apple.Terminal': '终端',
    'com.apple.Safari': 'Safari',
    'com.google.Chrome': 'Chrome',
    'com.mitchellh.ghostty': 'Ghostty',
    'com.microsoft.edgemac': 'Microsoft Edge',
    'com.microsoft.VSCode': 'VS Code',
    'com.openai.codex': 'Codex',
    'com.tencent.qq': 'QQ',
    'com.tencent.xinWeChat': '微信',
    'dev.kiro.desktop': 'Kiro',
  };
  if (knownLabels[value]) return knownLabels[value];
  if (!value.includes('.')) return value;
  const tail = value.split('.').filter(Boolean).at(-1) ?? value;
  return tail.replace(/[-_]+/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatRelativeDate(value: number): string {
  if (!value) return '时间未知';
  const difference = Math.max(0, Date.now() - value);
  if (difference < 60_000) return '刚刚';
  if (difference < 3_600_000) return `${Math.max(1, Math.round(difference / 60_000))} 分钟前`;
  if (difference < 86_400_000) return `${Math.max(1, Math.round(difference / 3_600_000))} 小时前`;
  return `${Math.max(1, Math.round(difference / 86_400_000))} 天前`;
}

function formatCoverage(value: number): string {
  const percentage = value <= 1 ? value * 100 : value;
  return `${Math.round(percentage)}%`;
}

function vectorProjectionLabel(fingerprint: string, coverage: number): string {
  if (!fingerprint || fingerprint === 'none') return '向量未启用（覆盖 0%）';
  return `向量覆盖 ${formatCoverage(coverage)} · ${fingerprint.replace(/^sha256:/, '').slice(0, 12)}`;
}

function modelRunErrorLabel(value: string): string {
  if (isModelQuotaError(value)) return '模型服务额度暂时用尽；本轮没有改动记忆，稍后可继续整理。';
  if (/memory\s+curation\s+packet.*(?:input\s+limit|too\s+long|exceed)/i.test(value)) return '本轮输入上下文过大；冻结输入已保留，下一轮会从保留位置继续。';
  if (/prompt-acceptance proof/i.test(value)) return '上一次续跑缺少精确接收回执，模型没有被重复调用';
  if (/active turn/i.test(value)) return '上一次模型会话仍有活动轮次';
  if (/timeout/i.test(value)) return '上一次模型请求超时';
  if (/request.failed|fetch failed/i.test(value)) return '上一次模型请求未完成';
  return '上一次模型批次没有完成';
}

function ownerRunErrorLabel(value: string): string {
  if (isModelQuotaError(value)) return '模型服务额度暂时用尽，本轮未改动记忆；稍后可以继续整理。';
  if (/memory\s+curation\s+packet.*(?:input\s+limit|too\s+long|exceed)/i.test(value)) return '本轮输入上下文过大，系统已保留进度，下一轮会从保留位置继续。';
  if (/prompt-acceptance proof/i.test(value)) return '上次续跑缺少精确接收回执，系统拒绝重复调用模型。';
  if (/active turn/i.test(value)) return '等待已有活动轮次结束后再续跑。';
  if (/fetch failed|request failed/i.test(value)) return '模型请求未完成，系统会稍后重试。';
  if (/timeout/i.test(value)) return '模型请求超时，系统会从保留的进度继续。';
  return '请查看最近模型批次的具体失败原因。';
}
