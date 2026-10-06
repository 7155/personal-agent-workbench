import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertTriangle,
  Check,
  ExternalLink,
  Inbox,
  RefreshCw,
  Search,
  ShieldCheck,
  ShieldQuestion,
  X,
} from 'lucide-react';
import { useMemo, useState } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { Button, Disclosure, EmptyState, Field, Input, SegmentedControl, Select } from '@/components/primitives';
import type { AgentApprovalV1 } from '@/contracts/generated/agent-approval.v1';
import {
  ManagementPage,
  QueryState,
  StatusBadge,
  publicErrorText,
} from '@/features/overview/management-ui';
import './approvals.css';
import { usePawOsAppActive } from '@/features/paw-os/surface-context';

type ApprovalFilter = 'pending' | 'all' | 'resolved';
type RiskFilter = 'all' | AgentApprovalV1['riskLevel'];

/**
 * 审批中心 — a decision desk, not a card wall.
 *
 * The queue on the left orders what waits; the panel on the right holds
 * exactly one hash-bound request with its full evidence and two stable
 * actions. Deciding never moves the buttons under the pointer.
 */
export function ApprovalsFeature() {
  const transport = useControlTransport();
  const active = usePawOsAppActive() ?? true;
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<ApprovalFilter>('pending');
  const [risk, setRisk] = useState<RiskFilter>('all');
  const [query, setQuery] = useState('');
  const [selectedId, setSelectedId] = useState('');
  const [pendingId, setPendingId] = useState('');
  const [confirmingId, setConfirmingId] = useState('');
  const [actionError, setActionError] = useState<Record<string, string>>({});
  const approvalsQuery = useQuery({
    enabled: active,
    queryKey: ['approvals', 'all'],
    queryFn: ({ signal }) => transport.request({
      pathId: 'agent.approvals.list',
      query: { limit: 500 },
      signal,
    }),
    refetchInterval: (queryState) => active && approvalItems(queryState.state.data).some((item) => item.state === 'pending') ? 5_000 : false,
    retry: false,
  });
  const sessionsQuery = useQuery({
    enabled: active,
    queryKey: ['approvals', 'sessions'],
    queryFn: ({ signal }) => transport.request({
      pathId: 'agent.sessions.list',
      query: { limit: 200, includeArchived: true },
      signal,
    }),
    staleTime: 30_000,
    retry: false,
  });
  const approvals = useMemo(() => approvalItems(approvalsQuery.data), [approvalsQuery.data]);
  const sessions = useMemo(() => sessionTitles(sessionsQuery.data), [sessionsQuery.data]);
  const pending = approvals.filter((item) => item.state === 'pending');
  const highRisk = pending.filter((item) => item.riskLevel === 'R3');
  const expiring = pending.filter((item) => item.expiresAtMs > Date.now() && item.expiresAtMs - Date.now() <= 5 * 60_000);
  const completed = approvals.filter((item) => ['applied', 'rejected'].includes(item.state));
  const visible = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase('zh-CN');
    return approvals.filter((item) => {
      const matchesState = filter === 'all'
        || (filter === 'pending' ? item.state === 'pending' : item.state !== 'pending');
      const matchesRisk = risk === 'all' || item.riskLevel === risk;
      const haystack = [
        item.toolId,
        item.operation,
        previewSummary(item.preview),
        sessions.get(item.sessionId) ?? '',
      ].join(' ').toLocaleLowerCase('zh-CN');
      return matchesState && matchesRisk && (!needle || haystack.includes(needle));
    }).sort((left, right) => {
      const pendingDelta = Number(right.state === 'pending') - Number(left.state === 'pending');
      if (pendingDelta) return pendingDelta;
      const riskDelta = riskRank(left.riskLevel) - riskRank(right.riskLevel);
      return riskDelta || left.requestedAtMs - right.requestedAtMs;
    });
  }, [approvals, filter, query, risk, sessions]);
  const selected = visible.find((item) => item.approvalId === selectedId) ?? visible[0];

  async function decide(item: AgentApprovalV1, decision: 'approve' | 'reject'): Promise<void> {
    if (decision === 'approve' && item.riskLevel === 'R3' && confirmingId !== item.approvalId) {
      setConfirmingId(item.approvalId);
      return;
    }
    if (pendingId) return;
    setPendingId(item.approvalId);
    setActionError((current) => ({ ...current, [item.approvalId]: '' }));
    try {
      await transport.request({
        pathId: 'agent.approval.decide',
        params: { approvalId: item.approvalId },
        body: { decision, payloadSha256: item.payloadSha256 },
      });
      setConfirmingId('');
      await queryClient.invalidateQueries({ queryKey: ['approvals', 'all'] });
    } catch (error) {
      setActionError((current) => ({
        ...current,
        [item.approvalId]: publicErrorText(error, '这项审批没有更新；已保留原状态。'),
      }));
      await approvalsQuery.refetch();
    } finally {
      setPendingId('');
    }
  }

  return (
    <ManagementPage
      actions={<Button leadingIcon={<RefreshCw size={15} />} loading={approvalsQuery.isFetching} onClick={() => void approvalsQuery.refetch()} size="small">刷新</Button>}
      description="伙伴执行高风险操作前会先停在这里等你决定。每个决定只针对当前这一项请求，不会顺带放行之后的同类操作。"
      eyebrow="需要你决定"
      routeId="approvals"
      title="审批中心"
    >
      <QueryState error={approvalsQuery.error ? new Error(publicErrorText(approvalsQuery.error, '无法读取审批队列。')) : null} isPending={approvalsQuery.isPending} onRetry={() => void approvalsQuery.refetch()}>
        <section
          aria-label="审批现状"
          className="approvals-pulse"
          data-tone={pending.length ? (highRisk.length ? 'danger' : 'attention') : 'calm'}
        >
          <span aria-hidden="true" className="approvals-pulse__icon">
            {pending.length ? <ShieldQuestion size={19} /> : <ShieldCheck size={19} />}
          </span>
          <div className="approvals-pulse__copy">
            <strong>{pending.length ? `${pending.length} 项操作在等你决定` : '队列已清空'}</strong>
            <p>{pulseDetail(pending.length, highRisk.length, expiring.length)}</p>
          </div>
          {completed.length ? <span className="approvals-pulse__done">已处理 {completed.length} 项</span> : null}
        </section>

        <section aria-label="审批工作台" className="approvals-desk">
          <section aria-label="审批队列" className="approvals-queue">
            <div className="approvals-queue__filters">
              <SegmentedControl
                aria-label="审批状态筛选"
                items={[
                  { label: '待审批', value: 'pending' },
                  { label: '全部', value: 'all' },
                  { label: '已处理', value: 'resolved' },
                ]}
                onValueChange={(value) => setFilter(value as ApprovalFilter)}
                value={filter}
              />
              <span aria-label={`${visible.length} 项审批`} className="approvals-queue__count">{visible.length} 项</span>
            </div>
            <div className="approvals-queue__tools">
              <Field htmlFor="approval-risk" label="风险">
                <Select
                  id="approval-risk"
                  onValueChange={(value) => setRisk(value as RiskFilter)}
                  options={[
                    { value: 'all', label: '全部风险' },
                    { value: 'R1', label: 'R1 · 低风险' },
                    { value: 'R2', label: 'R2 · 受控操作' },
                    { value: 'R3', label: 'R3 · 高风险' },
                  ]}
                  value={risk}
                />
              </Field>
              <Field className="approvals-queue__search" htmlFor="approval-search" label="搜索">
                <Input id="approval-search" onChange={(event) => setQuery(event.target.value)} placeholder="工具、操作、对话或摘要" type="search" value={query} />
              </Field>
            </div>

            {visible.length ? (
              <ol aria-label="审批项目" className="approvals-queue__list">
                {visible.map((item) => {
                  const active = item.approvalId === selected?.approvalId;
                  return (
                    <li data-active={active || undefined} data-risk={item.riskLevel} data-state={item.state} key={item.approvalId}>
                      <button aria-current={active ? 'true' : undefined} onClick={() => setSelectedId(item.approvalId)} type="button">
                        <span aria-hidden="true" className="approvals-risk">{item.riskLevel}</span>
                        <span className="approvals-queue__copy">
                          <strong>{previewSummary(item.preview) || `${item.toolId} · ${item.operation}`}</strong>
                          <small>{toolLabel(item.toolId)} · {operationLabel(item.operation)} · {sessions.get(item.sessionId) ?? '所属对话'}</small>
                        </span>
                        <span className="approvals-queue__meta">
                          <StatusBadge label={stateLabel(item.state)} tone={stateTone(item.state)} />
                          <time dateTime={new Date(item.requestedAtMs).toISOString()}>{formatTime(item.requestedAtMs)}</time>
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ol>
            ) : (
              <EmptyState
                action={(query || risk !== 'all' || filter !== 'pending') ? <Button onClick={() => { setQuery(''); setRisk('all'); setFilter('pending'); }} size="small">查看待审批</Button> : undefined}
                description={filter === 'pending' && !query && risk === 'all' ? '新的高风险请求会在这里逐项出现。' : '调整筛选条件后再查看。'}
                headingLevel={3}
                icon={Search}
                title={filter === 'pending' && !query && risk === 'all' ? '当前没有待审批请求' : '没有匹配的审批'}
              />
            )}
          </section>

          <section aria-label="审批详情" className="approvals-decision">
            {selected ? (
              <ApprovalDecision
                confirming={confirmingId === selected.approvalId}
                error={actionError[selected.approvalId] ?? ''}
                item={selected}
                key={selected.approvalId}
                onCancelConfirm={() => setConfirmingId('')}
                onDecide={(decision) => void decide(selected, decision)}
                pending={pendingId === selected.approvalId}
                sessionTitle={sessions.get(selected.sessionId) ?? '所属对话'}
              />
            ) : (
              <EmptyState
                description="从左侧队列选择一项后，这里会显示与哈希绑定的完整请求内容。"
                headingLevel={3}
                icon={Inbox}
                title="没有可显示的审批"
              />
            )}
          </section>
        </section>
      </QueryState>
    </ManagementPage>
  );
}

function ApprovalDecision({
  confirming,
  error,
  item,
  onCancelConfirm,
  onDecide,
  pending,
  sessionTitle,
}: {
  confirming: boolean;
  error: string;
  item: AgentApprovalV1;
  onCancelConfirm: () => void;
  onDecide: (decision: 'approve' | 'reject') => void;
  pending: boolean;
  sessionTitle: string;
}) {
  const summary = previewSummary(item.preview) || `${item.toolId} · ${item.operation}`;
  const facts = previewFacts(item.preview);
  const fieldCount = Object.keys(item.preview).length;
  return (
    <article className="approvals-decision__card" data-risk={item.riskLevel} data-state={item.state}>
      <header className="approvals-decision__head">
        <span aria-hidden="true" className="approvals-risk">{item.riskLevel}</span>
        <div className="approvals-decision__title">
          <h3>{summary}</h3>
          <p>{toolLabel(item.toolId)} · {operationLabel(item.operation)} · {riskMeaning(item.riskLevel)}</p>
        </div>
        <StatusBadge label={stateLabel(item.state)} tone={stateTone(item.state)} />
      </header>

      <ul className="approvals-decision__context">
        <li><a href={`#/agent?session=${encodeURIComponent(item.sessionId)}`}>{sessionTitle}<ExternalLink size={12} /></a></li>
        <li>请求于 {formatTime(item.requestedAtMs)}</li>
        <li>{item.state === 'pending' ? expiryLabel(item.expiresAtMs) : `决定于 ${formatTime(item.decidedAtMs ?? item.requestedAtMs)}`}</li>
      </ul>

      {facts.length ? (
        <dl className="approvals-decision__facts">
          {facts.map((fact) => <div key={fact.label}><dt>{fact.label}</dt><dd>{fact.value}</dd></div>)}
        </dl>
      ) : null}

      {fieldCount ? (
        <Disclosure
          className="approvals-preview"
          revealClassName="approvals-preview__reveal"
          summary={<>
            <span>完整预览</span>
            <small>{fieldCount} 个字段 · SHA-256 绑定</small>
          </>}
        >
          <div className="approvals-preview__body">
            <p><ShieldCheck size={15} /><span>以下完整预览与本次审批哈希绑定</span></p>
            <div className="approvals-preview__binding"><span>SHA-256</span><code>{item.payloadSha256}</code></div>
            <pre>{JSON.stringify(redactApprovalPreview(item.preview), null, 2)}</pre>
          </div>
        </Disclosure>
      ) : null}

      {confirming ? <p className="approvals-decision__confirm"><AlertTriangle size={15} /><span><strong>确认批准 R3 高风险操作？</strong><small>只批准当前哈希绑定请求；不会自动批准同类操作。</small></span></p> : null}
      {error ? <p className="approvals-decision__error" role="alert">{error}</p> : null}

      {item.state === 'pending' ? (
        <footer className="approvals-decision__actions">
          {confirming ? <Button disabled={pending} onClick={onCancelConfirm} size="small" variant="quiet">取消</Button> : null}
          <Button disabled={pending} leadingIcon={<X size={14} />} onClick={() => onDecide('reject')} size="small" variant="quiet">拒绝</Button>
          <Button leadingIcon={<Check size={14} />} loading={pending} onClick={() => onDecide('approve')} size="small" variant="primary">{confirming ? '确认批准' : '批准'}</Button>
        </footer>
      ) : null}
    </article>
  );
}

function pulseDetail(pendingCount: number, highRiskCount: number, expiringCount: number): string {
  if (!pendingCount) return '新的高风险请求会先停在这里，问过你再执行。';
  const parts: string[] = [];
  if (highRiskCount) parts.push(`${highRiskCount} 项高风险需要二次确认`);
  if (expiringCount) parts.push(`${expiringCount} 项将在 5 分钟内过期`);
  return parts.length ? parts.join('；') + '。' : '逐项核对后决定；到期未处理的请求会自动作废。';
}

function approvalItems(value: unknown): AgentApprovalV1[] {
  const items = record(value).items;
  return Array.isArray(items) ? items.filter(isApproval) : [];
}

function isApproval(value: unknown): value is AgentApprovalV1 {
  const item = record(value);
  return item.schemaVersion === 'rag-ime.agent-approval.v1'
    && typeof item.approvalId === 'string'
    && typeof item.sessionId === 'string'
    && typeof item.payloadSha256 === 'string'
    && ['R1', 'R2', 'R3'].includes(String(item.riskLevel))
    && ['pending', 'approved', 'external_pending', 'rejected', 'expired', 'stale', 'applied', 'failed'].includes(String(item.state));
}

function sessionTitles(value: unknown): Map<string, string> {
  const envelope = record(value);
  const items = Array.isArray(envelope.items) ? envelope.items : Array.isArray(envelope.sessions) ? envelope.sessions : [];
  return new Map(items.flatMap((value) => {
    const item = record(value);
    const id = text(item.id);
    return id ? [[id, text(item.title) || '所属对话'] as const] : [];
  }));
}

function previewSummary(preview: Record<string, unknown>): string {
  return [preview.summary, preview.title, preview.message, preview.description].map(text).find(Boolean) ?? '';
}

const secretPreviewKey = /token|secret|password|api.?key|authorization|cookie/i;

function previewFacts(preview: Record<string, unknown>): Array<{ label: string; value: string }> {
  const ignored = new Set(['summary', 'title', 'message', 'description']);
  return Object.entries(preview).flatMap(([key, value]) => {
    if (ignored.has(key)) return [];
    if (secretPreviewKey.test(key)) return [{ label: publicKey(key), value: value ? '已隐藏' : '未配置' }];
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
      return [{ label: publicKey(key), value: compactPreviewValue(String(value)) }];
    }
    if (Array.isArray(value)) return [{ label: publicKey(key), value: `${value.length} 项` }];
    return [];
  });
}

function compactPreviewValue(value: string): string {
  return value.length > 160 ? `${value.slice(0, 157)}…` : value;
}

function redactApprovalPreview(value: unknown, key = ''): unknown {
  if (secretPreviewKey.test(key)) return value ? '已隐藏' : '未配置';
  if (Array.isArray(value)) return value.map((item) => redactApprovalPreview(item));
  if (!value || typeof value !== 'object') return value;
  return Object.fromEntries(Object.entries(value as Record<string, unknown>).map(([entryKey, item]) => (
    [entryKey, redactApprovalPreview(item, entryKey)]
  )));
}

function stateLabel(state: AgentApprovalV1['state']): string {
  return ({ pending: '等待决定', approved: '已批准待执行', external_pending: '等待外部完成', rejected: '已拒绝', expired: '已过期', stale: '已失效', applied: '已执行', failed: '执行失败' })[state];
}

function stateTone(state: AgentApprovalV1['state']): 'success' | 'warning' | 'danger' | 'info' | 'neutral' {
  if (state === 'applied') return 'success';
  if (state === 'pending' || state === 'approved' || state === 'external_pending') return 'warning';
  if (state === 'failed') return 'danger';
  return 'neutral';
}

function toolLabel(value: string): string {
  return ({ workspace_shell: '运行命令', workspace_patch: '修改文件', workspace_write: '写入文件', runtime: '运行时', configuration: '设置', memory: '记忆', input: '输入法' } as Record<string, string>)[value] ?? value;
}

function operationLabel(value: string): string {
  return ({ run: '执行', apply: '应用', restart: '重启', restore_apply: '恢复', apply_settings: '应用设置' } as Record<string, string>)[value] ?? value;
}

function riskRank(value: AgentApprovalV1['riskLevel']): number {
  return ({ R3: 0, R2: 1, R1: 2 })[value];
}

function riskMeaning(value: AgentApprovalV1['riskLevel']): string {
  return ({ R1: '低风险，影响可逆', R2: '受控操作，影响已在预览中列出', R3: '高风险，批准前需要二次确认' })[value];
}

function publicKey(value: string): string {
  return ({ command: '命令', path: '路径', changes: '变更', target: '目标', scope: '范围', files: '文件', rollback: '恢复方式' } as Record<string, string>)[value] ?? value;
}

function expiryLabel(value: number): string {
  const remaining = value - Date.now();
  if (remaining <= 0) return '正在核对是否过期';
  const minutes = Math.max(1, Math.ceil(remaining / 60_000));
  return `${minutes} 分钟后过期`;
}

function formatTime(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '时间未记录';
  return new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(value));
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}
