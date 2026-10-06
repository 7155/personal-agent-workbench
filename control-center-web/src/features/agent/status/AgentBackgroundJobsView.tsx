import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Check,
  ChevronRight,
  CircleDashed,
  CloudOff,
  LoaderCircle,
  RefreshCw,
  Square,
  TriangleAlert,
} from 'lucide-react';
import { useEffect, useState } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { Button } from '@/components/primitives';
import type { AgentBackgroundJobV1 } from '@/contracts/generated/agent-background-job.v1';
import { agentProjectionKey, agentSessionAddress, selectAgentProjection, useAgentLiveStore, type AgentSessionTarget } from '../state/live-store';

const BACKGROUND_JOB_LIST_LIMIT = 100;
const BACKGROUND_JOB_LOG_LIMIT_BYTES = 131_072;

type BackgroundJobListResponse = {
  schemaVersion: 'rag-ime.agent-background-job-list.v1';
  ok: boolean;
  sessionId: string;
  items: AgentBackgroundJobV1[];
  activeCount: number;
};

type BackgroundJobLogResponse = {
  schemaVersion: 'rag-ime.agent-background-job-log.v1';
  ok: boolean;
  jobId: string;
  sessionId: string;
  cursor: number;
  nextCursor: number;
  logStartCursor: number;
  truncatedBeforeCursor: boolean;
  hasMore: boolean;
  text: string;
};

const ACTIVE_BACKGROUND_JOB_STATUSES: Readonly<
  Record<AgentBackgroundJobV1['status'], boolean>
> = {
  queued: true,
  running: true,
  cancelling: true,
  completed: false,
  failed: false,
  cancelled: false,
  orphaned: false,
};

const BACKGROUND_JOB_STATUS_LABELS: Readonly<
  Record<AgentBackgroundJobV1['status'], string>
> = {
  queued: '等待启动',
  running: '运行中',
  cancelling: '正在停止',
  completed: '已完成',
  failed: '失败',
  cancelled: '已停止',
  orphaned: '宿主已断开',
};

const BACKGROUND_JOB_STATUS_PRECEDENCE: Readonly<
  Record<AgentBackgroundJobV1['status'], number>
> = {
  queued: 0,
  running: 1,
  cancelling: 2,
  completed: 3,
  failed: 3,
  cancelled: 3,
  orphaned: 3,
};

const BACKGROUND_JOB_SORT_ORDER: Readonly<
  Record<AgentBackgroundJobV1['status'], number>
> = {
  running: 0,
  cancelling: 1,
  queued: 2,
  completed: 3,
  failed: 3,
  cancelled: 3,
  orphaned: 3,
};

export function AgentBackgroundJobsView({
  active = true,
  sessionId,
  jobs: snapshotJobs,
  onOpenJob,
}: {
  active?: boolean;
  sessionId: string;
  jobs: AgentBackgroundJobV1[];
  onOpenJob?: (job: AgentBackgroundJobV1) => void;
}) {
  const transport = useControlTransport();
  const address = agentSessionAddress(transport, sessionId);
  const listing = useQuery({
    queryKey: backgroundJobListQueryKey(address),
    queryFn: async ({ signal }) => {
      const response = await transport.request<BackgroundJobListResponse>({
        pathId: 'agent.session.backgroundJobs.list',
        params: { sessionId },
        query: { limit: BACKGROUND_JOB_LIST_LIMIT },
        signal,
      });
      if (
        response.schemaVersion !== 'rag-ime.agent-background-job-list.v1'
        || response.ok !== true
        || response.sessionId !== sessionId
        || !Array.isArray(response.items)
        || response.items.some((job) => job.sessionId !== sessionId)
      ) {
        throw new Error('后台任务列表回执与当前会话不匹配');
      }
      return response;
    },
    enabled: active && Boolean(sessionId),
    refetchInterval: active ? (query) => (
      mergeBackgroundJobItems(
        backgroundJobItems(query.state.data, sessionId),
        snapshotJobs,
        sessionId,
      ).some((job) => ACTIVE_BACKGROUND_JOB_STATUSES[job.status])
        ? 1_000
        : 5_000
    ) : false,
    retry: false,
  });
  const jobs = listing.data
    ? mergeBackgroundJobItems(listing.data.items, snapshotJobs, sessionId)
    : snapshotJobs;

  if (listing.isPending && jobs.length === 0) {
    return (
      <div className="agent-background-jobs__state" role="status">
        <LoaderCircle aria-hidden="true" className="ui-spin" size={14} />
        <span>正在读取后台任务</span>
      </div>
    );
  }

  if (listing.error && jobs.length === 0) {
    return (
      <div className="agent-background-jobs__state" data-tone="danger">
        <TriangleAlert aria-hidden="true" size={14} />
        <span role="alert">后台任务暂时不可用；不会猜测任务是否仍在运行。</span>
        <Button
          loading={listing.isFetching}
          onClick={() => void listing.refetch()}
          size="small"
          variant="quiet"
        >
          重新读取
        </Button>
      </div>
    );
  }

  return (
    <div className="agent-background-jobs">
      <div className="agent-background-jobs__toolbar">
        <span aria-live="polite">
          {listing.isFetching
            ? '正在刷新 Runtime 状态…'
            : `${jobs.length} 个任务 · 最多显示 ${BACKGROUND_JOB_LIST_LIMIT} 个`}
        </span>
        <Button
          aria-label="刷新后台任务"
          leadingIcon={<RefreshCw size={13} />}
          loading={listing.isFetching}
          onClick={() => void listing.refetch()}
          size="small"
          variant="quiet"
        >
          刷新
        </Button>
      </div>
      {listing.error ? (
        <div className="agent-background-jobs__notice" data-tone="danger">
          <TriangleAlert aria-hidden="true" size={13} />
          <span role="alert">刷新失败，当前显示上一次由服务器确认的状态。</span>
          <Button
            loading={listing.isFetching}
            onClick={() => void listing.refetch()}
            size="small"
            variant="quiet"
          >
            重试
          </Button>
        </div>
      ) : null}
      {jobs.length ? (
        <div className="agent-background-jobs__list">
          {jobs.map((job) => (
            <BackgroundJobRow
              surfaceActive={active}
              key={`${agentProjectionKey(address)}:${job.jobId}`}
              job={job}
              sessionId={sessionId}
              onOpenJob={onOpenJob}
            />
          ))}
        </div>
      ) : (
        <p className="agent-status-empty">当前会话没有后台任务；后台运行命令后会显示在这里。</p>
      )}
    </div>
  );
}

function BackgroundJobRow({
  job,
  sessionId,
  surfaceActive,
  onOpenJob,
}: {
  job: AgentBackgroundJobV1;
  sessionId: string;
  surfaceActive: boolean;
  onOpenJob?: (job: AgentBackgroundJobV1) => void;
}) {
  const transport = useControlTransport();
  const address = agentSessionAddress(transport, sessionId);
  const queryClient = useQueryClient();
  const [expanded, setExpanded] = useState(false);
  const [confirmingCancel, setConfirmingCancel] = useState(false);
  const [cancelError, setCancelError] = useState('');
  const [cancelNotice, setCancelNotice] = useState('');
  const [cancelling, setCancelling] = useState(false);
  const jobActive = ACTIVE_BACKGROUND_JOB_STATUSES[job.status];
  const sessionCancellable = (
    (job.status === 'queued' || job.status === 'running')
    && !job.causalMetadata.roomBound
  );
  const cancellationManagedByRoom = job.causalMetadata.roomBound && jobActive;
  const elapsed = useJobElapsed(job, surfaceActive);
  const logCursor = Math.max(
    job.logStartCursor,
    job.outputBytes - BACKGROUND_JOB_LOG_LIMIT_BYTES,
  );
  const logs = useQuery({
    queryKey: ['agent', 'background-job-logs', agentProjectionKey(address), job.jobId, logCursor],
    queryFn: ({ signal }) => transport.request<BackgroundJobLogResponse>({
      pathId: 'agent.session.backgroundJob.logs',
      params: { sessionId, jobId: job.jobId },
      query: { cursor: logCursor, limitBytes: BACKGROUND_JOB_LOG_LIMIT_BYTES },
      signal,
    }),
    enabled: surfaceActive && expanded,
    refetchInterval: surfaceActive && expanded && jobActive ? 1_000 : false,
    retry: false,
  });
  const log = backgroundJobLog(logs.data, sessionId, job.jobId);
  const logNotice = logWindowNotice(job, log, logCursor);

  useEffect(() => {
    if (sessionCancellable) return;
    setConfirmingCancel(false);
    setCancelling(false);
  }, [sessionCancellable]);

  async function cancel(): Promise<void> {
    if (!sessionCancellable || cancelling) return;
    if (!confirmingCancel) {
      setConfirmingCancel(true);
      return;
    }
    setCancelling(true);
    setCancelError('');
    setCancelNotice('');
    const requestAddress = address;
    try {
      const receipt = await transport.request<unknown>({
        pathId: 'agent.session.backgroundJob.cancel',
        params: { sessionId, jobId: job.jobId },
        body: { reason: 'control_center_requested' },
      });
      const metadata = backgroundJobReceiptMetadata(receipt, sessionId, job.jobId);
      if (!metadata) throw new Error('后台任务停止回执无效，请重新读取会话状态');

      const store = useAgentLiveStore.getState();
      store.applyBackgroundJobReceipt(requestAddress, receipt);
      const authoritativeJob = selectAgentProjection(useAgentLiveStore.getState(), requestAddress)
        ?.backgroundJobsById[job.jobId];
      if (!authoritativeJob || authoritativeJob.updatedAtMs < metadata.updatedAtMs) {
        throw new Error('后台任务停止回执未能更新会话状态，请重新读取');
      }

      queryClient.setQueryData<BackgroundJobListResponse>(
        backgroundJobListQueryKey(requestAddress),
        (current) => current ? upsertListedJob(current, authoritativeJob) : current,
      );
      setCancelNotice(metadata.summary);
      setConfirmingCancel(false);
      setCancelling(false);
    } catch (error) {
      setCancelError(publicError(error));
      setCancelling(false);
    }
  }

  return (
    <article className="agent-background-job" data-state={job.status}>
      <button
        aria-expanded={expanded}
        className="agent-background-job__summary"
        onClick={() => setExpanded((value) => !value)}
        type="button"
      >
        <span className="agent-background-job__state"><JobStateIcon status={job.status} /></span>
        <span className="agent-background-job__identity">
          <strong>{job.label}</strong>
          <small>{BACKGROUND_JOB_STATUS_LABELS[job.status]} · {elapsed} · {formatBytes(job.outputBytes)}</small>
        </span>
        <ChevronRight aria-hidden="true" className="agent-background-job__chevron" size={14} />
      </button>
      {expanded ? (
        <div className="agent-background-job__details">
          <code className="agent-background-job__command">{job.command}</code>
          <dl>
            <div><dt>任务</dt><dd title={job.jobId}>{job.jobId}</dd></div>
            <div><dt>目录</dt><dd title={job.cwd}>{job.cwd}</dd></div>
            <div><dt>退出码</dt><dd>{job.exitCode ?? '—'}</dd></div>
          </dl>
          <div className="agent-background-job__log-header">
            <strong>任务输出</strong>
            <Button
              aria-label={`刷新${job.label}的日志`}
              leadingIcon={<RefreshCw size={12} />}
              loading={logs.isFetching}
              onClick={() => void logs.refetch()}
              size="small"
              variant="quiet"
            >
              刷新日志
            </Button>
          </div>
          {logNotice ? <p className="agent-background-job__log-note">{logNotice}</p> : null}
          <div className="agent-background-job__log" aria-label={`${job.label}的后台任务日志`}>
            {logs.isPending ? <span role="status">正在读取日志…</span> : null}
            {logs.error ? <span data-tone="danger" role="alert">日志暂时不可用</span> : null}
            {!logs.isPending && !logs.error ? (
              <pre tabIndex={0}>{log?.text || '暂无输出'}</pre>
            ) : null}
          </div>
          {logs.error ? (
            <Button
              loading={logs.isFetching}
              onClick={() => void logs.refetch()}
              size="small"
              variant="quiet"
            >
              重新读取日志
            </Button>
          ) : null}
          {job.error ? (
            <p
              className={
                job.status === 'failed' || job.status === 'orphaned'
                  ? 'agent-background-job__error'
                  : 'agent-background-job__server-detail'
              }
              role={job.status === 'failed' || job.status === 'orphaned' ? 'alert' : undefined}
            >
              {job.error}
            </p>
          ) : null}
          {sessionCancellable ? (
            <div className="agent-background-job__actions">
              {onOpenJob ? (
                <Button onClick={() => onOpenJob(job)} size="small" variant="secondary">
                  在窗口查看
                </Button>
              ) : null}
              <Button
                disabled={cancelling}
                leadingIcon={<Square size={12} />}
                loading={cancelling}
                onClick={() => void cancel()}
                size="small"
                variant={confirmingCancel ? 'danger' : 'secondary'}
              >
                {cancelling ? '正在停止…' : confirmingCancel ? '确认停止' : '停止任务'}
              </Button>
              {confirmingCancel ? (
                <Button
                  disabled={cancelling}
                  onClick={() => setConfirmingCancel(false)}
                  size="small"
                  variant="quiet"
                >
                  继续运行
                </Button>
              ) : null}
            </div>
          ) : onOpenJob ? (
            <div className="agent-background-job__actions">
              <Button onClick={() => onOpenJob(job)} size="small" variant="secondary">
                在窗口查看
              </Button>
            </div>
          ) : null}
          {cancellationManagedByRoom ? (
            <p className="agent-background-job__server-detail">
              此任务属于 Room；请在对应 Room 中停止。
            </p>
          ) : null}
          {cancelNotice ? (
            <p className="agent-background-job__cancel-notice" role="status">
              <Check aria-hidden="true" size={13} />{cancelNotice}
            </p>
          ) : null}
          {cancelError ? <p className="agent-background-job__error" role="alert">{cancelError}</p> : null}
        </div>
      ) : null}
    </article>
  );
}

function JobStateIcon({ status }: { status: AgentBackgroundJobV1['status'] }) {
  if (status === 'queued') return <CircleDashed aria-hidden="true" size={14} />;
  if (status === 'running' || status === 'cancelling') {
    return <LoaderCircle aria-hidden="true" size={14} />;
  }
  if (status === 'completed') return <Check aria-hidden="true" size={14} />;
  if (status === 'cancelled') return <Square aria-hidden="true" size={13} />;
  if (status === 'orphaned') return <CloudOff aria-hidden="true" size={14} />;
  return <TriangleAlert aria-hidden="true" size={14} />;
}

function useJobElapsed(job: AgentBackgroundJobV1, surfaceActive: boolean): string {
  const active = surfaceActive && ACTIVE_BACKGROUND_JOB_STATUSES[job.status];
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [active]);
  const startedAt = job.startedAtMs || job.createdAtMs;
  const endedAt = job.endedAtMs || now;
  const seconds = Math.max(0, Math.floor((endedAt - startedAt) / 1_000));
  const minutes = Math.floor(seconds / 60);
  return minutes ? `${minutes}分${String(seconds % 60).padStart(2, '0')}秒` : `${seconds}秒`;
}

function backgroundJobListQueryKey(address: AgentSessionTarget) {
  return ['agent', 'background-jobs', agentProjectionKey(address)] as const;
}

function mergeBackgroundJobItems(
  listedJobs: readonly AgentBackgroundJobV1[],
  liveJobs: readonly AgentBackgroundJobV1[],
  sessionId: string,
): AgentBackgroundJobV1[] {
  const byId = new Map<string, AgentBackgroundJobV1>();
  for (const job of listedJobs) {
    if (job.sessionId === sessionId) byId.set(job.jobId, job);
  }
  for (const job of liveJobs) {
    if (job.sessionId !== sessionId) continue;
    const listed = byId.get(job.jobId);
    if (listed === undefined || isLiveBackgroundJobNewer(job, listed)) {
      byId.set(job.jobId, job);
    }
  }
  return [...byId.values()]
    .sort(compareBackgroundJobs)
    .slice(0, BACKGROUND_JOB_LIST_LIMIT);
}

function isLiveBackgroundJobNewer(
  live: AgentBackgroundJobV1,
  listed: AgentBackgroundJobV1,
): boolean {
  if (live.updatedAtMs !== listed.updatedAtMs) {
    return live.updatedAtMs > listed.updatedAtMs;
  }
  return (
    BACKGROUND_JOB_STATUS_PRECEDENCE[live.status]
    > BACKGROUND_JOB_STATUS_PRECEDENCE[listed.status]
  );
}

function compareBackgroundJobs(
  left: AgentBackgroundJobV1,
  right: AgentBackgroundJobV1,
): number {
  return (
    BACKGROUND_JOB_SORT_ORDER[left.status] - BACKGROUND_JOB_SORT_ORDER[right.status]
    || right.updatedAtMs - left.updatedAtMs
    || right.createdAtMs - left.createdAtMs
    || left.jobId.localeCompare(right.jobId)
  );

}

function backgroundJobItems(
  value: BackgroundJobListResponse | undefined,
  sessionId: string,
): AgentBackgroundJobV1[] {
  if (!value || value.sessionId !== sessionId || !Array.isArray(value.items)) return [];
  return value.items;
}

function backgroundJobLog(
  value: BackgroundJobLogResponse | undefined,
  sessionId: string,
  jobId: string,
): BackgroundJobLogResponse | undefined {
  if (
    !value
    || value.schemaVersion !== 'rag-ime.agent-background-job-log.v1'
    || value.ok !== true
    || value.sessionId !== sessionId
    || value.jobId !== jobId
    || value.cursor < 0
    || value.nextCursor < value.cursor
    || typeof value.text !== 'string'
  ) return undefined;
  return value;
}

function backgroundJobReceiptMetadata(
  value: unknown,
  sessionId: string,
  jobId: string,
): { updatedAtMs: number; summary: string } | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  const receipt = value as Record<string, unknown>;
  if (
    receipt.schemaVersion !== 'rag-ime.agent-background-job-cancel-receipt.v1'
    || receipt.ok !== true
  ) return undefined;
  const receiptJob = receipt.job;
  if (!receiptJob || typeof receiptJob !== 'object' || Array.isArray(receiptJob)) return undefined;
  const candidate = receiptJob as Record<string, unknown>;
  if (
    candidate.schemaVersion !== 'rag-ime.agent-background-job.v1'
    || candidate.sessionId !== sessionId
    || candidate.jobId !== jobId
    || typeof candidate.updatedAtMs !== 'number'
  ) return undefined;
  const summary = typeof receipt.summary === 'string' ? receipt.summary.slice(0, 240) : '';
  return { updatedAtMs: candidate.updatedAtMs, summary };
}

function upsertListedJob(
  current: BackgroundJobListResponse,
  job: AgentBackgroundJobV1,
): BackgroundJobListResponse {
  if (current.sessionId !== job.sessionId) return current;
  const items = current.items.some((item) => item.jobId === job.jobId)
    ? current.items.map((item) => item.jobId === job.jobId ? job : item)
    : [job, ...current.items].slice(0, BACKGROUND_JOB_LIST_LIMIT);
  return {
    ...current,
    items,
    activeCount: items.filter((item) => ACTIVE_BACKGROUND_JOB_STATUSES[item.status]).length,
  };
}

function logWindowNotice(
  job: AgentBackgroundJobV1,
  log: BackgroundJobLogResponse | undefined,
  requestedCursor: number,
): string {
  if (!log) return '';
  const range = `${formatBytes(log.cursor)}–${formatBytes(log.nextCursor)}`;
  const suffix = log.hasMore ? ' 仍有更新输出可读取。' : '';
  if (job.logTruncated || job.logStartCursor > 0 || log.truncatedBeforeCursor) {
    return `较早日志已被 Runtime 截断；当前显示字节 ${range}（单次最多 ${formatBytes(BACKGROUND_JOB_LOG_LIMIT_BYTES)}）。${suffix}`;
  }
  if (requestedCursor > 0) {
    return `为限制读取量，当前仅显示最近 ${formatBytes(BACKGROUND_JOB_LOG_LIMIT_BYTES)}（字节 ${range}）。${suffix}`;
  }
  return suffix.trim();
}


function formatBytes(value: number): string {
  if (value < 1_024) return `${value} B`;
  if (value < 1_048_576) return `${(value / 1_024).toFixed(value >= 102_400 ? 0 : 1)} KB`;
  return `${(value / 1_048_576).toFixed(1)} MB`;
}

function publicError(error: unknown): string {
  return error instanceof Error ? error.message.slice(0, 240) : '停止任务失败';
}
