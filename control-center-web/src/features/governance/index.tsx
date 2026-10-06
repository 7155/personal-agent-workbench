import {
  AlertTriangle,
  Archive,
  ArrowRight,
  BadgeCheck,
  BookOpenCheck,
  ClipboardCheck,
  DatabaseZap,
  FileClock,
  FlaskConical,
  History,
  Inbox,
  Layers,
  Lightbulb,
  Scale,
  ScanSearch,
  Shield,
  ShieldAlert,
  ShieldCheck,
  Stamp,
  Undo2,
  type LucideIcon,
} from 'lucide-react';
import { Children, useEffect, useMemo, useState, type ReactNode } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { Button, Disclosure, Field, Input, SegmentedControl, Skeleton } from '@/components/primitives';
import {
  InlineNotice,
  ManagementSection,
  ManagementPage,
  StatusBadge,
} from '@/features/overview/management-ui';
import {
  activeGuards,
  emptyGovernanceProjection,
  emptyKnowledgeGovernanceProjection,
  guardIntegrity,
  matchesScope,
  safeDisplay,
  visibleClaimText,
  type GovernanceProjection,
  type KnowledgeGovernanceProjection,
} from './model';
import './governance.css';
import { usePawOsAppActive } from '@/features/paw-os/surface-context';

type ScopeFilters = { root: string; owner: string; room: string; session: string };
type GovernanceLoadState = 'readonly' | 'loading' | 'ready' | 'error';
type GovernanceView = 'overview' | 'rules' | 'knowledge';
const emptyFilters: ScopeFilters = { root: '', owner: '', room: '', session: '' };

export function GovernanceFeature() {
  const transport = useControlTransport();
  const pageActive = usePawOsAppActive() ?? true;
  const [governance, setGovernance] = useState(emptyGovernanceProjection);
  const [knowledge, setKnowledge] = useState(emptyKnowledgeGovernanceProjection);
  const [live, setLive] = useState(false);
  const [loadState, setLoadState] = useState<GovernanceLoadState>('loading');
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    if (!pageActive) return;
    let active = true;
    setLoadState(current => current === 'ready' ? current : 'loading');
    void Promise.all([
      transport.request({ pathId: 'agent.governance.read' }),
      transport.request({ pathId: 'agent.knowledgeGovernance.read' }),
    ]).then(([governanceResponse, knowledgeResponse]) => {
      if (!active) return;
      const governancePayload = governanceResponse as { governance?: GovernanceProjection };
      const knowledgePayload = knowledgeResponse as { knowledge?: KnowledgeGovernanceProjection };
      setGovernance(governancePayload.governance ?? emptyGovernanceProjection);
      setKnowledge(knowledgePayload.knowledge ?? emptyKnowledgeGovernanceProjection);
      setLive(true);
      setLoadState('ready');
    }).catch(() => {
      if (!active) return;
      setLive(false);
      setLoadState('error');
    });
    return () => { active = false; };
  }, [pageActive, reloadToken, transport]);

  return (
    <GovernanceCenter
      governance={governance}
      knowledge={knowledge}
      loadState={loadState}
      liveRoutesAvailable={live}
      onRetry={loadState === 'error' ? () => setReloadToken((current) => current + 1) : undefined}
    />
  );
}

export function GovernanceCenter({
  governance,
  knowledge,
  loadState,
  liveRoutesAvailable = false,
  onRetry,
}: {
  governance: GovernanceProjection;
  knowledge: KnowledgeGovernanceProjection;
  loadState?: GovernanceLoadState;
  liveRoutesAvailable?: boolean;
  onRetry?: () => void;
}) {
  const [view, setView] = useState<GovernanceView>('overview');
  const [filters, setFilters] = useState<ScopeFilters>(emptyFilters);
  const filteredGuards = useMemo(() => governance.guardCandidates.filter((item) => matchesScope(item, filters)), [filters, governance.guardCandidates]);
  const filteredClaims = useMemo(() => knowledge.claims.filter((item) => matchesScope(item, filters)), [filters, knowledge.claims]);
  const filteredPromotionCandidates = useMemo(() => knowledge.promotionCandidates.filter((item) => matchesScope(item, filters)), [filters, knowledge.promotionCandidates]);
  const active = activeGuards(governance);
  const activeCount = active.filter((item) => item.candidate).length;
  const deadOutbox = knowledge.outbox.filter((item) => item.state === 'dead_letter');
  const attentionCount = governance.incidents.length + governance.deadLetters.length + deadOutbox.length;
  const resolvedLoadState = loadState
    ?? (liveRoutesAvailable ? 'ready' : onRetry ? 'error' : 'readonly');

  return (
    <ManagementPage
      description="先看保护是否生效、有没有需要处理的问题；规则审计和知识记录按视图分开查看。"
      eyebrow="本机保护记录"
      routeId="governance"
      title="安全与治理"
    >
      {resolvedLoadState === 'loading' ? <GovernanceLoadingState /> : null}
      {resolvedLoadState === 'error' ? (
        <div className="governance-load-error">
          <InlineNotice title="暂时无法读取安全记录" tone="danger">
            本机服务没有返回安全记录。重新检查只会再次读取状态，不会改变任何规则。
          </InlineNotice>
          {onRetry ? <Button onClick={onRetry}>重新检查</Button> : null}
        </div>
      ) : null}
      {resolvedLoadState === 'readonly' ? (
        <InlineNotice title={onRetry ? '暂时无法读取安全记录' : '安全记录暂时只读'} tone="warning">
          {onRetry ? '本机服务没有返回安全记录；重新检查不会改变任何规则。' : '当前为查看模式；规则变更会在具备完整审计与授权边界后开放。'}
        </InlineNotice>
      ) : null}
      {resolvedLoadState === 'ready' || resolvedLoadState === 'readonly' ? (
        <>
          <section aria-label="当前保护结果" className="governance-board" data-tone={verdictTone(attentionCount, activeCount)}>
            <div className="governance-verdict">
              <span aria-hidden="true" className="governance-verdict__icon">
                <VerdictIcon activeCount={activeCount} attentionCount={attentionCount} />
              </span>
              <div className="governance-verdict__copy">
                <strong>{verdictTitle(attentionCount, activeCount)}</strong>
                <p>{verdictHint(attentionCount, activeCount, governance.guardCandidates.length)}</p>
              </div>
            </div>
            <dl className="governance-stats">
              <div data-tone={governance.incidents.length ? 'warning' : 'neutral'}>
                <dt>异常事件</dt>
                <dd>{governance.incidents.length}</dd>
              </div>
              <div data-tone={governance.guardCandidates.length ? 'info' : 'neutral'}>
                <dt>待确认规则</dt>
                <dd>{governance.guardCandidates.length}</dd>
              </div>
              <div data-tone={activeCount ? 'success' : 'neutral'}>
                <dt>正在生效</dt>
                <dd>{activeCount}</dd>
              </div>
              <div data-tone={deadOutbox.length ? 'danger' : 'neutral'}>
                <dt>索引失败</dt>
                <dd>{deadOutbox.length}</dd>
              </div>
            </dl>
          </section>

          <div className="governance-viewbar">
            <SegmentedControl
              aria-label="治理视图"
              items={[
                { value: 'overview', label: '概况' },
                { value: 'rules', label: '规则与审计' },
                { value: 'knowledge', label: '记忆与知识' },
              ]}
              onValueChange={(value) => setView(value as GovernanceView)}
              value={view}
            />
          </div>

          {view === 'overview' ? (
            <GovernanceOverview
              active={active}
              deadOutboxCount={deadOutbox.length}
              governance={governance}
              onNavigate={setView}
            />
          ) : null}
          {view === 'rules' ? (
            <GovernanceRules
              filteredGuards={filteredGuards}
              filters={filters}
              governance={governance}
              onFiltersChange={setFilters}
            />
          ) : null}
          {view === 'knowledge' ? (
            <>
              <ScopeFilter filters={filters} onFiltersChange={setFilters} />
              <KnowledgeGovernance
                filteredClaims={filteredClaims}
                filteredPromotionCandidates={filteredPromotionCandidates}
                projection={knowledge}
              />
            </>
          ) : null}
        </>
      ) : null}
    </ManagementPage>
  );
}

function VerdictIcon({ activeCount, attentionCount }: { activeCount: number; attentionCount: number }) {
  if (attentionCount) return <ShieldAlert size={24} />;
  if (activeCount) return <ShieldCheck size={24} />;
  return <Shield size={24} />;
}

function verdictTone(attentionCount: number, activeCount: number): 'warning' | 'success' | 'neutral' {
  if (attentionCount) return 'warning';
  return activeCount ? 'success' : 'neutral';
}

function verdictTitle(attentionCount: number, activeCount: number): string {
  if (attentionCount) return `${attentionCount} 项需要处理`;
  if (activeCount) return `${activeCount} 条保护规则正在生效`;
  return '目前没有需要处理的问题';
}

function verdictHint(attentionCount: number, activeCount: number, candidateCount: number): string {
  if (attentionCount) return '先处理「概况」里列出的问题，再查看完整审计记录。';
  if (activeCount) return '新任务会继续受这些规则保护；启用与撤销都会留下审计记录。';
  if (candidateCount) return '有保护建议等待评估；只有明确启用的规则才会生效。';
  return '还没有生效中的保护规则；系统会在发现问题时先给出建议。';
}

function GovernanceLoadingState() {
  return (
    <div aria-live="polite" className="governance-loading" role="status">
      <ManagementSection
        description="正在读取生效中的规则、异常事件和索引状态。完成前不会显示推测结果。"
        title="正在核对本机保护"
      >
        <div aria-hidden="true" className="governance-loading__skeletons">
          <Skeleton />
          <Skeleton />
          <Skeleton />
          <Skeleton />
        </div>
      </ManagementSection>
    </div>
  );
}

function GovernanceOverview({
  active,
  deadOutboxCount,
  governance,
  onNavigate,
}: {
  active: ReturnType<typeof activeGuards>;
  deadOutboxCount: number;
  governance: GovernanceProjection;
  onNavigate: (view: GovernanceView) => void;
}) {
  const attention: Array<{ key: string; view: GovernanceView; tone: 'warning' | 'danger'; title: string; detail: string }> = [
    ...governance.incidents.map((item) => ({
      key: `incident:${item.incidentId}`,
      view: 'rules' as const,
      tone: 'warning' as const,
      title: incidentLabel(item.taxonomy),
      detail: `发生 ${item.occurrenceCount} 次 · 系统已记录这类异常`,
    })),
    ...governance.deadLetters.map((item) => ({
      key: `dead:${item.deadLetterId}`,
      view: 'rules' as const,
      tone: 'danger' as const,
      title: deadLetterLabel(item.reasonCode),
      detail: `${item.attemptCount} 次尝试后仍未完成 · 需要人工处理`,
    })),
    ...(deadOutboxCount ? [{
      key: 'outbox:dead',
      view: 'knowledge' as const,
      tone: 'danger' as const,
      title: '知识索引更新失败',
      detail: `${deadOutboxCount} 项更新多次重试后停止 · 需要人工处理`,
    }] : []),
  ];

  return (
    <>
      <ManagementSection
        description="发现异常时先处理这里列出的问题；每一项都能跳到对应的完整记录。"
        title="需要处理"
      >
        {attention.length ? (
          <ul className="governance-attention">
            {attention.map((item) => (
              <li data-tone={item.tone} key={item.key}>
                <button onClick={() => onNavigate(item.view)} type="button">
                  <span aria-hidden="true" className="governance-attention__dot" />
                  <span className="governance-attention__copy">
                    <strong>{item.title}</strong>
                    <small>{item.detail}</small>
                  </span>
                  <span aria-hidden="true" className="governance-attention__go">
                    <ArrowRight size={15} />
                  </span>
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <p className="governance-empty-line">目前没有需要处理的问题；有新问题时会先出现在这里。</p>
        )}
      </ManagementSection>

      <ManagementSection
        description="只有这里列出的规则会保护新任务；启用与撤销都会留下审计记录。"
        title="生效中的保护"
      >
        {active.length ? (
          <ul className="governance-active-grid">
            {active.map(({ pointer, candidate }) => (
              <li data-state={candidate ? 'on' : 'off'} key={pointer.scopeKey}>
                <span aria-hidden="true" className="governance-active-grid__icon">
                  {candidate ? <ShieldCheck size={17} /> : <Shield size={17} />}
                </span>
                <strong>{scopeLabel(pointer.scopeKey)}</strong>
                <span>{candidate ? '由一条已确认规则保护' : '这个范围暂时没有生效规则'}</span>
                <small>规则版本 {pointer.guardEpoch}</small>
              </li>
            ))}
          </ul>
        ) : (
          <p className="governance-empty-line">当前没有生效中的保护规则。</p>
        )}
      </ManagementSection>
    </>
  );
}

function ScopeFilter({
  filters,
  onFiltersChange,
}: {
  filters: ScopeFilters;
  onFiltersChange: (update: (current: ScopeFilters) => ScopeFilters) => void;
}) {
  return (
    <Disclosure className="governance-filter-disclosure" summary="精确查找特定记录">
      <p>手头已有完整记录编号时使用；日常查看无需填写。</p>
      <section aria-label="安全记录范围筛选" className="governance-filters">
        {(['root', 'owner', 'room', 'session'] as const).map((key) => (
          <Field htmlFor={`governance-filter-${key}`} key={key} label={key === 'root' ? '整项任务' : key === 'owner' ? '责任方' : key === 'room' ? '协作空间' : '对话'}>
            <Input
              id={`governance-filter-${key}`}
              onChange={(event) => onFiltersChange((current) => ({ ...current, [key]: event.target.value }))}
              placeholder="输入完整记录编号"
              value={filters[key]}
            />
          </Field>
        ))}
      </section>
    </Disclosure>
  );
}

type AuditEvent = {
  key: string;
  atMs: number;
  kind: 'eval' | 'approval' | 'activation' | 'rollback';
  title: string;
  badge: { label: ReactNode; tone: 'success' | 'warning' | 'danger' | 'info' | 'neutral' };
  summary: string;
  rows: Array<[string, unknown]>;
  payloads?: Array<[string, unknown]>;
};

function auditTrail(governance: GovernanceProjection): AuditEvent[] {
  const events: AuditEvent[] = [
    ...governance.evalRuns.map((item): AuditEvent => ({
      key: `eval:${item.evalRunId}`,
      atMs: item.createdAtMs,
      kind: 'eval',
      title: '规则评估',
      badge: { label: statusLabel(item.status), tone: item.status === 'passed' ? 'success' : 'danger' },
      summary: '这次评估检查规则是否达到启用要求。',
      rows: [
        ['评估运行编号', item.evalRunId],
        ['规则建议编号', item.guardCandidateId],
        ['评估方式', item.mode],
        ['原始状态', item.status],
      ],
      payloads: [['评估结果', item.metrics]],
    })),
    ...governance.approvals.map((item): AuditEvent => ({
      key: `approval:${item.approvalReceiptId}`,
      atMs: item.createdAtMs,
      kind: 'approval',
      title: '规则批准记录',
      badge: { label: decisionLabel(item.decision), tone: 'neutral' },
      summary: '这条记录说明规则是否已获得正式批准。',
      rows: [
        ['批准记录编号', item.approvalReceiptId],
        ['规则建议编号', item.guardCandidateId],
        ['批准方编号', item.authorityRef],
        ['原始决定', item.decision],
      ],
    })),
    ...governance.activations.map((item): AuditEvent => ({
      key: `activation:${item.activationReceiptId}`,
      atMs: item.createdAtMs,
      kind: 'activation',
      title: '规则已启用',
      badge: { label: `版本 ${item.guardEpoch}`, tone: 'success' },
      summary: `${scopeLabel(item.scopeKey)}从这条记录起受到保护。`,
      rows: [
        ['启用记录编号', item.activationReceiptId],
        ['规则建议编号', item.guardCandidateId],
        ['保护范围编号', item.scopeKey],
        ['评估运行编号', item.evalRunIds.join('\n')],
      ],
    })),
    ...governance.rollbacks.map((item): AuditEvent => ({
      key: `rollback:${item.rollbackReceiptId}`,
      atMs: item.createdAtMs,
      kind: 'rollback',
      title: '规则已撤销',
      badge: { label: `版本 ${item.guardEpoch}`, tone: 'warning' },
      summary: item.restoredGuardCandidateId ? '已恢复上一条可用规则。' : '这个范围现在没有生效中的保护规则。',
      rows: [
        ['撤销记录编号', item.rollbackReceiptId],
        ['被撤销规则编号', item.fromGuardCandidateId],
        ['恢复规则编号', item.restoredGuardCandidateId],
      ],
    })),
  ];
  return events.sort((left, right) => right.atMs - left.atMs);
}

function GovernanceRules({
  filteredGuards,
  filters,
  governance,
  onFiltersChange,
}: {
  filteredGuards: GovernanceProjection['guardCandidates'];
  filters: ScopeFilters;
  governance: GovernanceProjection;
  onFiltersChange: (update: (current: ScopeFilters) => ScopeFilters) => void;
}) {
  const trail = auditTrail(governance);
  return (
    <>
      <ScopeFilter filters={filters} onFiltersChange={onFiltersChange} />

      <ManagementSection description="系统会从失败中整理经验和保护建议；只有经过评估并明确启用的规则才会真正生效。" title="异常与保护建议">
        <div className="governance-columns">
          <GovernanceList empty="目前没有异常事件" title="异常事件">
            {governance.incidents.map((item) => (
              <GovRecord
                badges={<StatusBadge label={`${item.occurrenceCount} 次`} tone="warning" />}
                icon={AlertTriangle}
                key={item.incidentId}
                rows={[
                  ['异常记录编号', item.incidentId],
                  ['异常分类', item.taxonomy],
                  ['失败特征', item.failureSignature],
                  ['证据引用', item.evidenceRefs.join('\n')],
                ]}
                title={incidentLabel(item.taxonomy)}
                tone="warning"
              >
                <p>系统已经记录这类异常，后续建议会先经过评估再启用。</p>
              </GovRecord>
            ))}
          </GovernanceList>
          <GovernanceList empty="目前没有待确认的经验建议" title="经验建议">
            {governance.lessons.map((item) => (
              <GovRecord
                badges={<StatusBadge label="尚未采用" />}
                icon={Lightbulb}
                key={item.lessonCandidateId}
                payloads={[
                  ['依据与边界', { facts: item.facts, causes: item.causes, applicabilityBoundary: item.applicabilityBoundary, counterexamples: item.counterexamples }],
                ]}
                rows={[
                  ['建议编号', item.lessonCandidateId],
                  ['来源异常编号', item.incidentId],
                  ['建议状态', item.state],
                ]}
                title="可复核的处理经验"
              >
                <p>系统已从异常中整理出建议；确认适用范围后才能转为保护规则。</p>
              </GovRecord>
            ))}
          </GovernanceList>
          <GovernanceList empty="目前没有匹配的保护规则建议" title="保护规则建议">
            {filteredGuards.map((item) => {
              const pointer = governance.activePointers.find((entry) => entry.activeGuardCandidateId === item.guardCandidateId);
              const integrity = guardIntegrity(governance, item, pointer);
              return (
                <GovRecord
                  badges={<StatusBadge label={pointer ? '正在生效' : '尚未启用'} tone={pointer ? 'success' : 'neutral'} />}
                  icon={ShieldCheck}
                  key={item.guardCandidateId}
                  payloads={[
                    ['规则条件与动作', { scope: item.scope, condition: item.condition, action: item.action, thresholds: item.thresholds }],
                  ]}
                  rows={[
                    ['规则建议编号', item.guardCandidateId],
                    ['来源建议编号', item.lessonCandidateId],
                    ['责任方编号', item.owner],
                    ['原始风险等级', item.risk],
                    ['原始状态', item.state],
                  ]}
                  title={guardSummary(item.action)}
                  tone={pointer ? 'success' : 'neutral'}
                >
                  <div className="governance-badges">
                    <StatusBadge label={riskLabel(item.risk)} tone="warning" />
                    {integrity.tampered ? <StatusBadge label="完整性异常" tone="danger" /> : null}
                    {integrity.staleEpoch ? <StatusBadge label="版本已过期" tone="danger" /> : null}
                  </div>
                  <p>适用于当前责任范围；启用前需要完成评估与批准。</p>
                </GovRecord>
              );
            })}
          </GovernanceList>
        </div>
      </ManagementSection>

      <ManagementSection description="规则从评估、批准、启用到撤销的完整过程按时间排列；只有「正在生效」的规则会保护新任务。" title="规则启用记录">
        {trail.length ? (
          <ol className="governance-trail">
            {trail.map((event) => (
              <li data-kind={event.kind} key={event.key}>
                <span aria-hidden="true" className="governance-trail__node">
                  <TrailIcon kind={event.kind} />
                </span>
                <article>
                  <header>
                    <strong>{event.title}</strong>
                    <StatusBadge label={event.badge.label} tone={event.badge.tone} />
                  </header>
                  <p>{event.summary}</p>
                  <RecordDetails payloads={event.payloads} rows={event.rows} />
                </article>
              </li>
            ))}
          </ol>
        ) : (
          <p className="governance-empty-line">目前没有规则启用记录</p>
        )}
      </ManagementSection>

      <ManagementSection title="自动整理与规则应用">
        <div className="governance-columns governance-columns--two">
          <GovernanceList empty="目前没有需要人工处理的整理任务" title="等待人工处理">
            {governance.deadLetters.map((item) => (
              <GovRecord
                badges={<StatusBadge label={`${item.attemptCount} 次尝试`} tone="danger" />}
                icon={Inbox}
                key={item.deadLetterId}
                rows={[
                  ['待处理记录编号', item.deadLetterId],
                  ['来源异常编号', item.incidentId],
                  ['责任方编号', item.ownerRef],
                  ['原始原因', item.reasonCode],
                  ['证据引用', item.lastEvidenceRefs.map(String).join('\n')],
                ]}
                title={deadLetterLabel(item.reasonCode)}
                tone="danger"
              >
                <p>{nextActionLabel(item.nextAction)}</p>
              </GovRecord>
            ))}
          </GovernanceList>
          <GovernanceList empty="目前没有规则应用记录" title="规则应用记录">
            {governance.materializations.map((item) => (
              <GovRecord
                badges={<StatusBadge label={statusLabel(item.status)} tone={item.status === 'applied' ? 'success' : item.errorCode ? 'danger' : 'warning'} />}
                icon={Layers}
                key={item.materializationReceiptId}
                meta={`规则版本 ${item.guardEpoch}`}
                rows={[
                  ['应用记录编号', item.materializationReceiptId],
                  ['规则建议编号', item.guardCandidateId],
                  ['投影引用', item.projectionRef],
                  ['原始类型', item.artifactKind],
                  ['原始状态', item.status],
                  ['错误代码', item.errorCode],
                ]}
                title={artifactLabel(item.artifactKind)}
                tone={item.errorCode ? 'danger' : 'neutral'}
              >
                <p>{item.errorCode ? '最近一次应用没有完成，需要检查记录详情。' : '规则已经同步到对应保护位置。'}</p>
              </GovRecord>
            ))}
          </GovernanceList>
        </div>
      </ManagementSection>
    </>
  );
}

function TrailIcon({ kind }: { kind: AuditEvent['kind'] }) {
  const Icon: LucideIcon = { eval: FlaskConical, approval: Stamp, activation: BadgeCheck, rollback: Undo2 }[kind];
  return <Icon size={14} />;
}

function KnowledgeGovernance({ projection, filteredClaims, filteredPromotionCandidates }: { projection: KnowledgeGovernanceProjection; filteredClaims: KnowledgeGovernanceProjection['claims']; filteredPromotionCandidates: KnowledgeGovernanceProjection['promotionCandidates'] }) {
  return <ManagementSection title="记忆与知识保护" description="查看哪些信息等待确认、哪些记录彼此冲突，以及旧内容如何失效和退出检索。">
    <div className="governance-columns governance-columns--two">
      <GovernanceList empty="目前没有等待确认的信息" title="等待确认的信息">
        {filteredPromotionCandidates.map((item) => (
          <GovRecord
            badges={<StatusBadge label={riskLabel(item.risk)} tone={item.risk === 'low' ? 'neutral' : 'warning'} />}
            icon={BookOpenCheck}
            key={item.promotionCandidateId}
            meta={`${evidenceLabel(item.evidenceKind)} · ${scopeKindLabel(item.scopeKind)}`}
            rows={[
              ['候选记录编号', item.promotionCandidateId],
              ['信息键', item.claimKey],
              ['证据类型', item.evidenceKind],
              ['证据引用', item.evidenceRef],
              ['归属类型', item.ownerKind],
              ['归属编号', item.ownerId],
              ['范围类型', item.scopeKind],
              ['范围编号', item.scopeId],
              ['原始可见性', item.visibility],
              ['原始风险等级', item.risk],
              ['冲突记录引用', item.conflictClaimRefs.join('\n')],
            ]}
            title="待确认信息"
          >
            <p>{visibleClaimText(item)}</p>
          </GovRecord>
        ))}
        {projection.promotionReceipts.map((item) => (
          <GovRecord
            badges={<StatusBadge label={`版本 ${item.knowledgeEpoch}`} tone="success" />}
            icon={BadgeCheck}
            key={item.promotionReceiptId}
            rows={[
              ['确认记录编号', item.promotionReceiptId],
              ['候选记录编号', item.promotionCandidateId],
              ['信息版本编号', item.claimVersionId],
              ['保护范围编号', item.scopeKey],
            ]}
            title="信息已确认"
            tone="success"
          >
            <p>这项信息已经进入受保护知识。</p>
          </GovRecord>
        ))}
      </GovernanceList>
      <GovernanceList empty="目前没有匹配的信息" title="已确认信息与冲突">
        {filteredClaims.map((item) => (
          <GovRecord
            badges={<StatusBadge label={visibilityLabel(item.visibility)} />}
            icon={BookOpenCheck}
            key={item.claimVersionId}
            meta={`${ownerKindLabel(item.ownerKind)} · ${scopeKindLabel(item.scopeKind)}`}
            payloads={[['来源信息', item.provenance]]}
            rows={[
              ['信息版本编号', item.claimVersionId],
              ['信息身份编号', item.claimIdentity],
              ['信息键', item.claimKey],
              ['归属类型', item.ownerKind],
              ['归属编号', item.ownerId],
              ['范围类型', item.scopeKind],
              ['范围编号', item.scopeId],
              ['原始可见性', item.visibility],
              ['矛盾记录引用', item.contradictionRefs.join('\n')],
            ]}
            title="已确认信息"
          >
            <p>{visibleClaimText(item)}</p>
          </GovRecord>
        ))}
        {projection.conflicts.map((item) => (
          <GovRecord
            badges={<StatusBadge label={conflictStateLabel(item.state)} tone={item.state === 'clear' ? 'success' : 'warning'} />}
            icon={Scale}
            key={item.promotionCandidateId}
            rows={[
              ['候选记录编号', item.promotionCandidateId],
              ['原始状态', item.state],
              ['冲突信息引用', item.claimRefs.join('\n')],
            ]}
            title="发现信息冲突"
            tone="warning"
          >
            <p>{item.claimRefs.length} 条信息需要核对后再决定保留哪一条。</p>
          </GovRecord>
        ))}
      </GovernanceList>
      <GovernanceList empty="目前没有更新或失效记录" title="更新与失效记录">
        {projection.lifecycleReceipts.map((item) => (
          <GovRecord
            badges={<StatusBadge label={`版本 ${item.knowledgeEpoch}`} tone="warning" />}
            icon={FileClock}
            key={item.lifecycleReceiptId}
            rows={[
              ['生命周期记录编号', item.lifecycleReceiptId],
              ['信息身份编号', item.claimIdentity],
              ['保护范围编号', item.scopeKey],
              ['原始操作', item.operation],
            ]}
            title={operationLabel(item.operation)}
          >
            <p>这项信息的生命周期已经更新。</p>
          </GovRecord>
        ))}
        {projection.epochs.map((item) => (
          <GovRecord
            badges={<StatusBadge label={`版本 ${item.knowledgeEpoch}`} />}
            icon={History}
            key={item.scopeKey}
            rows={[['保护范围编号', item.scopeKey]]}
            title={scopeLabel(item.scopeKey)}
          >
            <p>这个范围的知识保护版本已经更新。</p>
          </GovRecord>
        ))}
        {projection.tombstones.map((item) => (
          <GovRecord
            badges={<StatusBadge label={reasonLabel(item.reason)} tone="warning" />}
            icon={Archive}
            key={item.tombstoneId}
            meta={`知识版本 ${item.knowledgeEpoch}`}
            rows={[
              ['失效记录编号', item.tombstoneId],
              ['保护范围编号', item.scopeKey],
              ['对话编号', item.sessionId],
              ['原始原因', item.reason],
            ]}
            title="已退出检索"
          >
            <p>这段对话的旧内容不再用于后续检索。</p>
          </GovRecord>
        ))}
      </GovernanceList>
      <GovernanceList empty="目前没有隔离或索引异常" title="导入隔离与索引队列">
        {projection.quarantines.map((item) => (
          <GovRecord
            badges={<StatusBadge label={statusLabel(item.status)} tone={item.status === 'quarantined' ? 'danger' : 'success'} />}
            icon={ShieldAlert}
            key={item.importId}
            rows={[
              ['导入记录编号', item.importId],
              ['内容校验值', item.contentHash],
              ['原始状态', item.status],
            ]}
            title={item.sourceName || '待检查的导入材料'}
            tone={item.status === 'quarantined' ? 'danger' : 'neutral'}
          >
            <p>{item.findingCount} 个扫描发现，原始内容不展示。</p>
          </GovRecord>
        ))}
        {projection.outbox.map((item) => (
          <GovRecord
            badges={<StatusBadge label={statusLabel(item.state)} tone={item.state === 'applied' ? 'success' : item.state === 'dead_letter' ? 'danger' : 'warning'} />}
            icon={DatabaseZap}
            key={item.outboxId}
            rows={[
              ['队列记录编号', item.outboxId],
              ['信息版本编号', item.claimVersionId],
              ['原始操作', item.operation],
              ['原始状态', item.state],
              ['最后错误', item.lastError],
            ]}
            title={operationLabel(item.operation)}
            tone={item.state === 'dead_letter' ? 'danger' : 'neutral'}
          >
            <p>{item.lastError ? '最近一次更新没有完成，请检查记录详情。' : '这项更新正在按顺序处理。'}</p>
          </GovRecord>
        ))}
      </GovernanceList>
    </div>
    <GovernanceList empty="目前没有检索使用评估" title="检索与引用质量检查">
      {projection.evalDatasets.map((item) => (
        <GovRecord
          badges={<StatusBadge label={`第 ${item.datasetVersion} 版`} />}
          icon={ClipboardCheck}
          key={item.datasetId}
          payloads={[['检查阈值', item.thresholds]]}
          rows={[
            ['标准数据编号', item.datasetId],
            ['内容校验值', item.contentHash],
          ]}
          title="引用质量标准"
        >
          <p>用于检查引用是否充分，以及是否越过授权边界。</p>
        </GovRecord>
      ))}
      {projection.searchUseEvalRuns.map((item) => (
        <GovRecord
          badges={(
            <span className="governance-badges">
              <StatusBadge label={statusLabel(item.status)} tone={item.status === 'passed' ? 'success' : 'danger'} />
              <StatusBadge label="仅报告" tone="info" />
            </span>
          )}
          icon={ScanSearch}
          key={item.evalRunId}
          payloads={[['检查结果', { metrics: item.metrics, strataMetrics: item.strataMetrics, failureReasons: item.failureReasons }]]}
          rows={[
            ['检查运行编号', item.evalRunId],
            ['标准数据编号', item.datasetId],
            ['协作绑定编号', item.roomBindingId],
            ['评估方编号', item.evaluatorId],
            ['原始状态', item.status],
          ]}
          title="引用质量检查"
        >
          <p>已检查 {item.traceCount} 条使用记录；结果只用于报告，不会自动改变知识。</p>
        </GovRecord>
      ))}
    </GovernanceList>
  </ManagementSection>;
}

function GovernanceList({ children, empty, title }: { children: ReactNode; empty: string; title: string }) {
  const hasChildren = Children.count(children) > 0;
  return <section className="governance-list"><h3>{title}</h3><div>{hasChildren ? children : <p className="governance-empty">{empty}</p>}</div></section>;
}

function GovRecord({
  badges,
  children,
  icon: Icon,
  meta,
  payloads,
  rows,
  title,
  tone = 'neutral',
}: {
  badges?: ReactNode;
  children?: ReactNode;
  icon: LucideIcon;
  meta?: string;
  payloads?: Array<[string, unknown]>;
  rows: Array<[string, unknown]>;
  title: string;
  tone?: 'neutral' | 'success' | 'warning' | 'danger';
}) {
  return (
    <article className="governance-record" data-tone={tone}>
      <span aria-hidden="true" className="governance-record__icon"><Icon size={15} /></span>
      <div className="governance-record__body">
        <header>
          <strong>{title}</strong>
          {badges}
        </header>
        {children}
        {meta ? <small className="governance-record__meta">{meta}</small> : null}
        <RecordDetails payloads={payloads} rows={rows} />
      </div>
    </article>
  );
}

function RecordDetails({ rows, payloads = [] }: { rows: Array<[string, unknown]>; payloads?: Array<[string, unknown]> }) {
  const visibleRows = rows.filter(([, value]) => value !== undefined && value !== null && String(value).trim());
  if (!visibleRows.length && !payloads.length) return null;
  return (
    <Disclosure className="governance-record-details" summary="原始记录">
      {visibleRows.length ? <dl>{visibleRows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd><code>{String(value)}</code></dd></div>)}</dl> : null}
      {payloads.map(([label, value]) => <section key={label}><h4>{label}</h4><pre>{safeDisplay(value)}</pre></section>)}
    </Disclosure>
  );
}

function incidentLabel(value: string): string {
  return ({ routing_loop: '重复路由', permission_denied: '权限受限', timeout: '处理超时', stale_state: '状态已过期' } as Record<string, string>)[value] ?? '运行异常';
}

function guardSummary(action: Record<string, unknown>): string {
  return action.stop === true ? '阻止异常执行继续扩散' : '限制异常执行范围';
}

function riskLabel(value: string): string {
  return ({ low: '低风险', medium: '中风险', high: '高风险', critical: '极高风险' } as Record<string, string>)[value] ?? '风险待确认';
}

function statusLabel(value: string): string {
  return ({ applied: '已应用', candidate_only: '等待评估', clear: '已解决', completed: '已完成', dead_letter: '需要人工处理', failed: '未通过', open: '待处理', passed: '已通过', pending: '等待处理', quarantined: '已隔离', rejected: '未通过', running: '处理中' } as Record<string, string>)[value] ?? '状态待确认';
}

function decisionLabel(value: string): string {
  return ({ approved: '已批准', rejected: '未批准', pending: '等待决定' } as Record<string, string>)[value] ?? '决定待确认';
}

function artifactLabel(value: string): string {
  return ({ prompt_guard: '对话保护规则', runtime_guard: '运行保护规则', retrieval_guard: '检索保护规则' } as Record<string, string>)[value] ?? '保护规则';
}

function deadLetterLabel(value: string): string {
  return ({ retry_exhausted: '多次尝试后仍未完成', invalid_state: '记录状态不完整', unavailable: '相关服务暂不可用' } as Record<string, string>)[value] ?? '需要人工检查';
}

function nextActionLabel(value: string): string {
  if (/人工|复核|检查/u.test(value)) return '需要人工复核后再继续。';
  return '请检查记录详情并决定下一步。';
}

function evidenceLabel(value: string): string {
  return ({ external_import: '外部导入', local_observation: '本机观察', user_confirmed: '用户确认' } as Record<string, string>)[value] ?? '已有来源';
}

function visibilityLabel(value: string): string {
  return ({ private: '仅自己可见', room: '协作范围可见', secret: '敏感内容', shared: '项目共享', user: '仅自己可见' } as Record<string, string>)[value] ?? '可见范围待确认';
}

function ownerKindLabel(value: string): string {
  return ({ agent: '伙伴记忆', owner: '指定责任方', room: '协作记忆', shared: '项目共享', user: '个人记忆' } as Record<string, string>)[value] ?? '其他归属';
}

function scopeKindLabel(value: string): string {
  return ({ owner: '责任方范围', project: '项目范围', root: '整项任务', room: '协作空间范围', session: '对话范围', shared: '共享范围', user: '个人范围' } as Record<string, string>)[value] ?? '其他范围';
}

function conflictStateLabel(value: string): string {
  return ({ clear: '已解决', open: '等待核对', resolved: '已解决' } as Record<string, string>)[value] ?? '等待核对';
}

function operationLabel(value: string): string {
  return ({ create: '信息已建立', index: '索引更新', promote: '信息已确认', revoke: '信息已撤回', update: '信息已更新' } as Record<string, string>)[value] ?? '信息已更新';
}

function reasonLabel(value: string): string {
  return ({ expired: '已过期', revoke: '已撤回', superseded: '已被新内容替代' } as Record<string, string>)[value] ?? '不再使用';
}

function scopeLabel(value: string): string {
  return scopeKindLabel(value.split(':', 1)[0] ?? '');
}
