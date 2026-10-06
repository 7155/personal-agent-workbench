import {
  Check,
  Copy,
  Eye,
  RefreshCw,
  Search,
} from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  Disclosure,
  EmptyState,
  Field,
  Input,
  Select,
} from '@/components/primitives';
import { writeClipboardText } from '@/platform/clipboard';
import {
  historyMutationPathIds,
  useHistoryDetail,
  useHistoryMutationBoundary,
  useHistoryPages,
} from './api';
import {
  InlineNotice,
  ManagementPage,
  ManagementSection,
  StatusBadge,
  PaginationBar,
  QueryState,
  arrayRecords,
  asRecord,
  formatTime,
  numberValue,
  stringValue,
} from '@/features/overview/management-ui';
import {
  ManagementMutationWorkflow,
  parseManagementWorkPreview,
  parseManagementWorkReceipt,
} from '@/features/overview/management-mutation';
import { useProductIdentity } from '@/features/identity/product-identity';
import { usePawOsAppActive } from '@/features/paw-os/surface-context';
import './history.css';

export function HistoryFeature() {
  const identity = useProductIdentity();
  const active = usePawOsAppActive() ?? true;
  const [draftQuery, setDraftQuery] = useState('');
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState('');
  const [selectedId, setSelectedId] = useState('');
  const [detailEventId, setDetailEventId] = useState<number | null>(null);
  useEffect(() => { if (!active) setDetailEventId(null); }, [active]);
  const { pages } = useHistoryPages(query, filter);
  const mutationBoundary = useHistoryMutationBoundary();
  const rows = useMemo(() => (pages.data?.pages ?? [])
    .flatMap((page) => arrayRecords(asRecord(page).items))
    .map((item) => ({
      ...item,
      created: formatTime(item.createdAtMs),
      sourceLabel: sourceLabel(stringValue(item.source), stringValue(item.sourceCategory)),
      text: stringValue(item.textPreview, `已脱敏 · ${numberValue(item.textChars)} 字`),
    } as Record<string, unknown>)), [pages.data]);
  const rawRuntimeRevision = asRecord(pages.data?.pages[0]).runtimeRevision;
  const runtimeRevision = typeof rawRuntimeRevision === 'number'
    && Number.isInteger(rawRuntimeRevision)
    && rawRuntimeRevision >= 0
    ? rawRuntimeRevision
    : null;
  const selectedEventId = rows.some((row) => String(row.id) === selectedId) ? Number(selectedId) : 0;
  const tombstoneDraft = {
    eventId: selectedEventId,
    reason: 'control-center-history',
  };
  const openDetail = (row: Record<string, unknown>) => {
    const eventId = numberValue(row.id);
    if (!Number.isInteger(eventId) || eventId <= 0) return;
    setSelectedId(String(eventId));
    setDetailEventId(eventId);
  };
  const search = () => {
    const next = draftQuery.trim();
    if (next !== query) setSelectedId('');
    setQuery(next);
  };

  return (
    <ManagementPage
      actions={<Button leadingIcon={<RefreshCw size={15} />} loading={pages.isRefetching} onClick={() => void pages.refetch()} size="small">刷新</Button>}
      description="查看来自输入法、语音和导入内容的脱敏记录；完整原文只在你主动打开时读取。"
      eyebrow="记忆来源"
      routeId="history"
      title="输入记录"
    >
        <ManagementSection title="查找记录" trailing={pages.data ? <StatusBadge label={`已显示 ${rows.length} 条`} /> : undefined}>
          <div aria-label="筛选输入记录" className="history-filter-toolbar" role="search">
            <Field className="history-filter-toolbar__search" htmlFor="history-search" label="搜索">
              <Input id="history-search" onChange={(event) => setDraftQuery(event.target.value)} onKeyDown={(event) => {
                if (event.key !== 'Enter' || event.nativeEvent.isComposing || event.keyCode === 229) return;
                event.preventDefault();
                search();
              }} placeholder="文本、应用或项目" value={draftQuery} />
            </Field>
            <Button className="history-filter-toolbar__submit" leadingIcon={<Search size={14} />} onClick={search} size="small">查找</Button>
            <Field className="history-filter-toolbar__source" htmlFor="history-source-filter" label="来源">
              <Select id="history-source-filter" onValueChange={(next) => { setSelectedId(''); setFilter(next); }} options={[
                { value: '', label: '全部来源' },
                { value: 'rime_commit', label: '输入法' },
                { value: 'assistant_candidate', label: '联想候选' },
                { value: 'voice', label: '语音' },
                { value: 'import', label: '导入' },
              ]} value={filter} />
            </Field>
          </div>
          <p className="mgmt-muted">列表只显示脱敏摘要；完整输入仅在你主动打开详情时读取。</p>
          {pages.error && pages.data ? <div className="history-refresh-issue">
            <InlineNotice title={pages.isFetchNextPageError ? '后续记录未能加载' : '记录刷新失败'} tone="warning">已加载的记录仍可查看，筛选条件保持不变。</InlineNotice>
            <Button loading={pages.isFetching} onClick={() => void (pages.isFetchNextPageError ? pages.fetchNextPage() : pages.refetch())} size="small">
              {pages.isFetchNextPageError ? '重试加载更多' : '重试刷新'}
            </Button>
          </div> : null}
          <QueryState error={pages.data ? null : pages.error as Error | null} isPending={pages.isPending} onRetry={() => void pages.refetch()}>
          {rows.length ? (
            <>
              <HistoryTable onOpen={openDetail} productName={identity.productName} rows={rows} />
              {!pages.isFetchNextPageError ? <PaginationBar count={rows.length} hasMore={pages.hasNextPage} isFetching={pages.isFetchingNextPage} onLoadMore={() => void pages.fetchNextPage()} /> : null}
            </>
          ) : <EmptyState
            description={query || filter
              ? '试试更换关键词或来源。'
              : '当你通过输入法、语音或导入提供内容后，记录会出现在这里。'}
            icon={Search}
            title={query || filter ? '没有找到符合条件的记录' : '还没有输入记录'}
          />}
          </QueryState>
        </ManagementSection>

        {rows.length ? (
          <ManagementSection title="不再用于记忆" description="选择一条记录后，可以让它以后不再用于联想。原始记录仍会保留，操作也可以撤销。">
            <Field htmlFor="history-record" label="选择记录" style={{ maxWidth: 420 }}>
              <Select id="history-record" onValueChange={setSelectedId} options={[
                { value: '', label: '请选择一条记录' },
                ...rows.map((row) => ({ value: stringValue(row.id), label: `${stringValue(row.created)} · ${stringValue(row.text)}` })),
              ]} value={selectedEventId > 0 ? selectedId : ''} />
            </Field>
            <div style={{ marginTop: 12, maxWidth: 540 }}>
            <ManagementMutationWorkflow
              availability={mutationBoundary.availability(
                !Number.isInteger(selectedEventId) || selectedEventId <= 0
                  ? '先从已加载记录中选择一项。'
                  : runtimeRevision === null
                    ? '当前历史状态尚未同步，请刷新后重试。'
                    : '',
              )}
              description="让所选记录以后不再用于联想，原始记录仍然保留。"
              draftKey={JSON.stringify(tombstoneDraft)}
              mutationKey={['history', 'mutation', 'tombstone']}
              onApply={async (preview) => parseManagementWorkReceipt(
                await mutationBoundary.request({
                  pathId: historyMutationPathIds.apply,
                  body: {
                    ...preview.context,
                    expectedRuntimeRevision: preview.expectedRuntimeRevision,
                    previewToken: preview.previewToken,
                    payloadSha256: preview.payloadSha256,
                    confirmText: preview.requiredConfirm,
                  },
                }),
                historyMutationPathIds.apply,
                preview.payloadSha256,
              )}
              onApplied={() => {
                setSelectedId('');
                void pages.refetch();
              }}
              onPreview={async () => {
                if (runtimeRevision === null) throw new Error('当前历史状态尚未同步，请刷新后重试。');
                const preview = parseManagementWorkPreview(
                  await mutationBoundary.request({
                    pathId: historyMutationPathIds.preview,
                    body: {
                      ...tombstoneDraft,
                      expectedRuntimeRevision: runtimeRevision,
                    },
                  }),
                  historyMutationPathIds.apply,
                  tombstoneDraft,
                );
                return {
                  ...preview,
                  summary: {
                    ...preview.summary,
                    items: preview.summary.items.map(historyWorkflowText),
                  },
                };
              }}
              onRollback={async (receipt, preview) => parseManagementWorkReceipt(
                await mutationBoundary.request({
                  pathId: historyMutationPathIds.rollback,
                  body: {
                    receiptId: receipt.receiptId,
                    rollbackToken: receipt.rollbackToken,
                    payloadSha256: receipt.payloadSha256,
                    confirmText: 'rollback',
                  },
                }),
                historyMutationPathIds.rollback,
                preview.payloadSha256,
              )}
              onRolledBack={() => void pages.refetch()}
              risk="R2"
              title="不再用于记忆"
            />
            </div>
          </ManagementSection>
        ) : null}
      <HistoryDetailDialog
        eventId={detailEventId}
        onOpenChange={(open) => { if (!open) setDetailEventId(null); }}
        productName={identity.productName}
        returnFocusId={selectedId}
      />
    </ManagementPage>
  );
}

function HistoryTable({
  rows,
  onOpen,
  productName,
}: {
  rows: readonly Record<string, unknown>[];
  onOpen: (row: Record<string, unknown>) => void;
  productName: string;
}) {
  return (
    <div className="history-table-wrap" tabIndex={0}>
      <table className="history-table">
        <caption>输入记录分页；选择任意记录查看完整详情</caption>
        <thead><tr><th scope="col">时间</th><th scope="col">来源</th><th scope="col">文本预览</th><th scope="col">应用</th><th scope="col">项目</th><th scope="col"><span className="history-sr-only">操作</span></th></tr></thead>
        <tbody>
          {rows.map((row) => {
            const id = stringValue(row.id);
            const label = `查看 ${stringValue(row.created, '这条记录')} 的输入详情`;
            return (
              <tr key={id}>
                <td>{stringValue(row.created, '未记录')}</td>
                <td>{stringValue(row.sourceLabel, '未知来源')}</td>
                <td className="history-table__preview">{stringValue(row.text, '没有可显示的摘要')}</td>
                <td>{stringValue(row.app, '未记录')}</td>
                <td>{projectLabel(stringValue(row.project), productName)}</td>
                <td><button aria-label={label} className="history-table__open" data-history-event-id={id} onClick={() => onOpen(row)} type="button"><Eye aria-hidden="true" size={15} /><span>查看详情</span></button></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function HistoryDetailDialog({
  eventId,
  onOpenChange,
  productName,
  returnFocusId,
}: {
  eventId: number | null;
  onOpenChange: (open: boolean) => void;
  productName: string;
  returnFocusId: string;
}) {
  const detail = useHistoryDetail(eventId);
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const [contextCopyState, setContextCopyState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const response = asRecord(detail.data);
  const item = asRecord(response.item);
  const feedback = asRecord(item.feedback);
  const auxiliaryContext = asRecord(item.auxiliaryContext);
  const captureReceipt = asRecord(auxiliaryContext.captureReceipt);
  const text = stringValue(item.text);
  const auxiliaryText = stringValue(auxiliaryContext.text);
  const candidateRank = item.candidateRank === null ? 0 : numberValue(item.candidateRank);
  const hasFeedback = feedback.available === true;

  const copyText = async () => {
    if (!text) return;
    try {
      await writeClipboardText(text);
      setCopyState('copied');
    } catch {
      setCopyState('failed');
    }
  };

  const copyAuxiliaryContext = async () => {
    if (!auxiliaryText) return;
    try {
      await writeClipboardText(auxiliaryText);
      setContextCopyState('copied');
    } catch {
      setContextCopyState('failed');
    }
  };

  return (
    <Dialog onOpenChange={(open) => {
      setCopyState('idle');
      setContextCopyState('idle');
      onOpenChange(open);
    }} open={eventId !== null}>
      <DialogContent
        className="history-detail-dialog"
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          window.setTimeout(() => {
            document.querySelector<HTMLButtonElement>(
              `.history-table__open[data-history-event-id="${CSS.escape(returnFocusId)}"]`,
            )?.focus({ preventScroll: true });
          });
        }}
      >
        <DialogHeader>
          <DialogTitle>输入详情</DialogTitle>
          <DialogDescription>{response.ok === true ? `${formatTime(item.createdAtMs)} 保存的内容` : '读取已保存的完整输入内容'}</DialogDescription>
        </DialogHeader>
        {detail.isPending ? <p className="history-detail__state" role="status">正在读取详情...</p> : null}
        {detail.error ? (
          <div className="history-detail__state history-detail__state--error" role="alert">
            <p>详情读取失败。</p>
            <Button leadingIcon={<RefreshCw size={14} />} loading={detail.isFetching} onClick={() => void detail.refetch()} size="small" variant="quiet">重试读取</Button>
          </div>
        ) : null}
        {!detail.isPending && !detail.error && response.ok !== true ? <p className="history-detail__state history-detail__state--error" role="alert">这条记录已不存在或当前不可读取。</p> : null}
        {response.ok === true ? (
          <div className="history-detail__body">
            <dl className="history-detail__facts">
              <DetailFact label="时间" value={formatTime(item.createdAtMs)} />
              <DetailFact label="来源" value={sourceLabel(stringValue(item.source), stringValue(item.sourceCategory))} />
              <DetailFact label="应用" value={applicationLabel(stringValue(item.app))} />
              <DetailFact label="项目" value={projectLabel(stringValue(item.project), productName)} />
              <DetailFact label="记录状态" value={stringValue(item.status) === 'hidden' ? '已隐藏' : '可用于后续联想'} />
              <DetailFact label="上下文范围" value={groupLevelLabel(stringValue(item.groupLevel))} />
            </dl>
            <section aria-labelledby="history-detail-text" className="history-detail__text">
              <div className="history-detail__section-heading">
                <div><h3 id="history-detail-text">完整文本</h3><small>{numberValue(item.textChars)} 字</small></div>
                <Button leadingIcon={copyState === 'copied' ? <Check size={14} /> : <Copy size={14} />} onClick={() => void copyText()} size="small" variant="quiet">{copyState === 'copied' ? '已复制' : '复制全文'}</Button>
              </div>
              <pre tabIndex={0}>{text}</pre>
              {copyState === 'failed' ? <p className="history-detail__copy-error" role="status">复制失败，可直接选择上方全文复制。</p> : null}
            </section>
            <section aria-labelledby="history-detail-context" className="history-detail__context">
              <div className="history-detail__section-heading">
                <div>
                  <h3 id="history-detail-context">上下文获取</h3>
                  <small>{auxiliaryContext.available === true
                    ? `${numberValue(auxiliaryContext.textChars)} 字 · ${auxiliaryContext.hasAdditionalText === true ? '包含附近文本' : '仅当前输入'}`
                    : '这条记录没有保存附近文本'}</small>
                </div>
                {auxiliaryContext.available === true ? (
                  <Button leadingIcon={contextCopyState === 'copied' ? <Check size={14} /> : <Copy size={14} />} onClick={() => void copyAuxiliaryContext()} size="small" variant="quiet">
                    {contextCopyState === 'copied' ? '已复制' : '复制上下文'}
                  </Button>
                ) : null}
              </div>
              <dl className="history-detail__context-grid">
                <DetailFact label="附近文本" value={auxiliaryContext.hasAdditionalText === true ? '已保存' : '未保存'} />
                <DetailFact label="是否参与记忆" value={captureEvidenceLabel(captureReceipt)} />
              </dl>
              {auxiliaryText ? <pre tabIndex={0}>{auxiliaryText}</pre> : <p className="history-detail__empty">这条记录没有可查看的上下文。</p>}
              {auxiliaryContext.truncated === true ? <p className="history-detail__empty">内容较长，当前显示前 8000 字。</p> : null}
              <Disclosure
                className="history-detail__advanced-context"
                contentClassName="history-detail__advanced-context-body"
                summary="高级：采集详情"
              >
                <dl className="history-detail__context-grid">
                  <DetailFact label="来源方式" value={captureSourceLabel(stringValue(auxiliaryContext.captureSource), stringValue(auxiliaryContext.captureMode))} />
                  <DetailFact label="识别或候选服务" value={providerLabel(stringValue(item.provider))} />
                  <DetailFact label="候选位置" value={candidateRank > 0 ? `第 ${candidateRank} 位` : '未记录'} />
                  <DetailFact label="相关服务" value={modelRequestAssociationLabel(auxiliaryContext)} />
                  <DetailFact label="输入控件文本" value={capturedCharacterCountLabel(auxiliaryContext, 'fieldContext')} />
                  <DetailFact label="输入缓冲" value={capturedCharacterCountLabel(auxiliaryContext, 'imeBuffer')} />
                  <DetailFact label="保存状态" value={captureReceiptLabel(captureReceipt)} />
                </dl>
                {stringValue(auxiliaryContext.fallbackReason) ? <p className="history-detail__empty">读取方式变化：{captureFallbackLabel(stringValue(auxiliaryContext.fallbackReason))}</p> : null}
              </Disclosure>
              {contextCopyState === 'failed' ? <p className="history-detail__copy-error" role="status">复制失败，可直接选择上方内容复制。</p> : null}
            </section>
            <section aria-labelledby="history-detail-feedback" className="history-detail__feedback">
              <div className="history-detail__section-heading"><div><h3 id="history-detail-feedback">使用反馈</h3><small>这条记录的使用情况</small></div></div>
              {hasFeedback ? (
                <dl className="history-detail__feedback-grid">
                  <DetailFact label="采用" value={`${numberValue(feedback.acceptedCount)} 次`} />
                  <DetailFact label="跳过" value={`${numberValue(feedback.skippedCount)} 次`} />
                  <DetailFact label="置顶" value={feedback.pinned === true ? '是' : '否'} />
                  <DetailFact label="降低排序" value={feedback.downranked === true ? '是' : '否'} />
                  <DetailFact label="最近动作" value={latestActionLabel(stringValue(feedback.latestAction))} />
                  <DetailFact label="状态更新时间" value={formatTime(feedback.updatedAtMs)} />
                </dl>
              ) : <p className="history-detail__empty">这条记录没有关联到可验证的反馈状态。</p>}
            </section>
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

function DetailFact({ label, value }: { label: string; value: string }) {
  return <div><dt>{label}</dt><dd>{value || '未记录'}</dd></div>;
}

function historyWorkflowText(value: string): string {
  if (/停止参与后续召回/.test(value)) return '以后不再用于联想。';
  return value;
}

function sourceLabel(source: string, category = ''): string {
  if (category === 'rime_commit') return '输入法';
  if (category === 'voice') return '语音';
  if (category === 'assistant_candidate') return '联想候选';
  if (category === 'import') return '导入';
  const normalized = source.toLocaleLowerCase('en-US');
  if (normalized.includes('rime')) return '输入法';
  if (normalized.includes('voice')) return '语音';
  if (normalized.includes('rag')) return '知识召回';
  if (normalized.includes('memory')) return '个人记忆';
  if (normalized.includes('assistant') || normalized.includes('model')) return '联想候选';
  return source ? '其他来源' : '未知来源';
}

function providerLabel(provider: string): string {
  const normalized = provider.toLocaleLowerCase('en-US');
  if (!provider || normalized === 'local') return '本机';
  if (normalized.includes('volc') || normalized.includes('doubao')) return '豆包语音';
  if (normalized.includes('voice') || normalized.includes('asr')) return '流式语音识别';
  if (normalized.includes('rime')) return '输入法';
  return '其他识别服务';
}

function applicationLabel(app: string): string {
  const normalized = app.toLocaleLowerCase('en-US');
  if (!app) return '未记录';
  if (normalized.includes('openai.codex')) return 'Codex';
  if (normalized.includes('ghostty')) return 'Ghostty';
  if (normalized.includes('squirrel') || normalized.includes('rime')) return '输入法';
  if (/^[\p{L}\p{N} _+-]{1,48}$/u.test(app)) return app;
  return '其他应用';
}

function projectLabel(project: string, productName: string): string {
  if (!project) return '未记录';
  if (project === 'wisdom-weasel-rag-ime') return productName;
  if (/^[\p{L}\p{N} _+\u00b7-]{1,48}$/u.test(project) && /[\u3400-\u9fff]/u.test(project)) return project;
  return '本机项目';
}

function groupLevelLabel(level: string): string {
  if (level === 'document') return '当前文档';
  if (level === 'project') return '当前项目';
  if (level === 'app') return '当前应用';
  if (level === 'global') return '全部应用';
  return level ? '已分组' : '未记录';
}

function captureSourceLabel(source: string, mode: string): string {
  if (mode === 'terminal_visible_range') return '终端可见范围';
  if (mode === 'accessibility_semantics' || source === 'accessibility') return '辅助功能读取';
  if (source === 'text_input_client') return '当前输入控件';
  if (source === 'ime_active_buffer') return '输入法缓冲区';
  if (source === 'voice_insertion') return '语音定稿插入';
  if (source === 'stored_event_context') return '事件上下文';
  return source ? `已记录 · ${source}` : '旧记录未保存采集方式';
}

function modelRequestAssociationLabel(context: Record<string, unknown>): string {
  const association = stringValue(context.modelRequestAssociation);
  if (association === 'not_applicable') return '不适用 · 语音定稿不请求智能候选';
  if (association === 'intrinsic_candidate') return '已记录 · 该记录本身是智能候选';
  if (association === 'linked' || context.modelRequestLinked === true) return '已关联';
  if (association === 'not_recorded') return '采集契约未记录请求标识';
  return '旧记录无法核对模型请求';
}

function capturedCharacterCountLabel(
  context: Record<string, unknown>,
  field: 'fieldContext' | 'imeBuffer',
): string {
  const countKey = field === 'fieldContext' ? 'fieldContextChars' : 'imeBufferChars';
  const recordedKey = field === 'fieldContext' ? 'fieldContextRecorded' : 'imeBufferRecorded';
  const count = numberValue(context[countKey]);
  const recorded = context[recordedKey] === true || (!(recordedKey in context) && count > 0);
  if (!recorded) return '旧记录未采集';
  if (count > 0) return `${count} 字 · 已记录`;
  if (field === 'imeBuffer' && stringValue(context.captureSource) === 'voice_insertion') {
    return '0 字 · 语音输入不经过输入法缓冲区';
  }
  return '0 字 · 本次没有可验证文本';
}

function captureReceiptLabel(receipt: Record<string, unknown>): string {
  if (receipt.available !== true) return '无法核对 · 旧记录或非原生采集';
  const outcome = stringValue(receipt.outcome);
  const confidence = stringValue(receipt.boundaryConfidence);
  const outcomeLabel = outcome === 'stored'
    ? '已存入'
    : outcome === 'quarantined'
      ? '已隔离'
      : outcome === 'no_store'
        ? '未存入'
        : '状态未知';
  const confidenceLabel = confidence === 'strong' ? '强边界' : confidence === 'weak' ? '弱边界' : '边界未知';
  return `${outcomeLabel} · ${confidenceLabel}`;
}

function captureEvidenceLabel(receipt: Record<string, unknown>): string {
  if (receipt.available !== true) return '无法核对 · 记录来源不完整';
  const state = stringValue(receipt.evidenceState);
  if (state === 'admitted') return '已整理到长期记忆';
  if (state === 'candidate') return '等待整理';
  if (state === 'needs_review') return '隔离待人工检查';
  if (state === 'rejected') return '已排除，不进入记忆';
  if (state === 'forgotten') return '已忘记，可按恢复规则重新启用';
  return '尚未评估';
}

function captureFallbackLabel(reason: string): string {
  if (reason === 'document_length_unavailable') return '应用未提供文档长度，改用输入法缓冲区';
  if (reason === 'focused_element_unavailable') return '应用未提供可读取的焦点文本控件';
  if (reason === 'ax_string_for_range_failed') return '应用不支持按字符范围读取';
  return '应用未提供完整的辅助文本';
}

function latestActionLabel(action: string): string {
  if (!action) return '无';
  if (action === 'accept') return '采用';
  if (action === 'skip') return '跳过';
  if (action === 'pin') return '置顶';
  if (action === 'unpin') return '取消置顶';
  if (action === 'downrank') return '降低排序';
  if (action === 'restore') return '恢复';
  if (action === 'hide' || action === 'delete') return '隐藏';
  return '已记录反馈';
}
