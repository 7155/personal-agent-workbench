import { ChevronDown, RefreshCw } from 'lucide-react';
import { useState } from 'react';
import { Button } from '@/components/primitives';
import { asRecord, numberValue, stringValue } from '@/features/overview/management-ui';

type MemoryLayer = 'evidence' | 'atoms' | 'books';
export type MemorySummaryState = 'ready' | 'pending' | 'error';

export function memoryLibraryCounts(summary: Record<string, unknown>) {
  const curation = asRecord(summary.ownerCuration);
  return {
    memories: numberValue(summary.currentAtomCount, numberValue(summary.memoryAtomCount)),
    topics: numberValue(summary.memoryBookCount),
    sources: numberValue(summary.memoryEvidenceCount,
      numberValue(summary.evidenceSourceCount) + numberValue(summary.agentEvidenceCount)),
    pendingSources: numberValue(summary.ownerCurationPendingSourceCount,
      numberValue(summary.pendingGovernedEvidenceCount,
        numberValue(curation.pendingSourceCount, numberValue(summary.pendingCompileEvents)))),
  };
}

export function MemoryLibraryNavigation({
  activeLayer,
  onOpenLayer,
  onOpenOrganize,
  onRetry,
  summary,
  summaryState,
}: {
  activeLayer: MemoryLayer;
  onOpenLayer: (layer: MemoryLayer) => void;
  onOpenOrganize: () => void;
  onRetry: () => void;
  summary: Record<string, unknown>;
  summaryState: MemorySummaryState;
}) {
  const [expanded, setExpanded] = useState(false);
  const counts = memoryLibraryCounts(summary);
  const historicalCount = numberValue(summary.historicalAtomCount, numberValue(summary.memoryAtomArchivedCount));
  const totalCount = numberValue(summary.memoryAtomTotalCount,
    counts.memories + historicalCount + numberValue(summary.memoryAtomSourceArchiveCount));
  const pendingReview = numberValue(summary.needsReviewSourceCount)
    + numberValue(asRecord(summary.governanceProposalCounts).preview)
    + numberValue(asRecord(summary.activityTimelineCounts).draft);
  const ready = summaryState === 'ready';

  return (
    <section aria-label="记忆库分类与状态" className="memory-library" data-state={summaryState}>
      <div className="memory-library__navigation">
        <ul aria-label="记忆内容分类" className="memory-library__layers">
          {([
            { layer: 'books', label: '主题', description: '长期主题', count: counts.topics },
            { layer: 'atoms', label: '记忆', description: '已整理记忆', count: counts.memories },
            { layer: 'evidence', label: '来源', description: '来源记录', count: counts.sources },
          ] as const).map(({ layer, label, description, count }) => (
            <li key={layer}>
              <button
                aria-current={activeLayer === layer ? 'page' : undefined}
                aria-label={`${label} · ${description} · ${ready ? `${count} 项` : '计数暂不可用'}`}
                onClick={() => onOpenLayer(layer)}
                type="button"
              >
                <span>{label}</span><small>{ready ? count : '—'}</small>
              </button>
            </li>
          ))}
        </ul>
        <div className="memory-library__actions">
        <Button leadingIcon={<RefreshCw size={14} />} onClick={onOpenOrganize} size="small" variant="quiet">整理记忆</Button>
        <button
          aria-controls="memory-library-status"
          aria-expanded={expanded}
          aria-label="整理状态摘要"
          className="memory-library__toggle"
          onClick={() => setExpanded((value) => !value)}
          type="button"
        >
          整理状态 <ChevronDown aria-hidden="true" size={14} />
        </button>
        </div>
      </div>
      <p className="memory-library__layer-note">{activeLayer === 'evidence'
        ? '来源是原始记录，不代表已经保存为记忆。'
        : activeLayer === 'atoms'
          ? '这里是整理后的记忆；打开详情可核对来源、修正或查看历史。'
          : '主题把相关记忆放在一起，方便继续查找和使用。'}</p>
      {summaryState === 'error' && !expanded ? (
        <div className="memory-library__pending" role="status">
          <span>整理状态读取失败，计数暂不可用。</span>
          <button onClick={onRetry} type="button">重新读取状态</button>
        </div>
      ) : null}
      {ready && counts.pendingSources > 0 ? (
        <div className="memory-library__pending">
          <span>{counts.pendingSources} 条来源待整理</span>
          <button onClick={onOpenOrganize} type="button">查看待整理来源</button>
        </div>
      ) : null}
      {expanded ? (
        <div className="memory-library__status" id="memory-library-status">
          {!ready ? (
            <div role="status">
              <span>{summaryState === 'error' ? '记忆状态读取失败，计数暂不可用。' : '正在读取整理状态…'}</span>
              {summaryState === 'error' ? <Button leadingIcon={<RefreshCw size={13} />} onClick={onRetry} size="small" variant="quiet">重试</Button> : null}
            </div>
          ) : (
            <>
              <dl>
                <div><dt>来源记录</dt><dd>输入法 {numberValue(summary.inputMethodEvidenceCount)} · 语音 {numberValue(summary.voiceEvidenceCount)} · 伙伴主动记录 {numberValue(summary.agentCapturedEvidenceCount, numberValue(summary.agentEvidenceCount))}</dd></div>
                <div><dt>已整理记忆</dt><dd>全部 {totalCount} · 历史 {historicalCount}</dd></div>
                <div><dt>长期主题</dt><dd>按主题持续查找</dd></div>
                <div><dt>检索索引</dt><dd>{projectionStatus(asRecord(summary.projection))}</dd></div>
                <div><dt>待确认</dt><dd>{pendingReview ? `${pendingReview} 项等待处理` : '没有待处理草案'}</dd></div>
                <div><dt>最近整理</dt><dd>{latestTimelineStatus(asRecord(summary.latestActivityTimeline))}</dd></div>
              </dl>
              <Button onClick={onOpenOrganize} size="small" variant="quiet">查看整理与审核</Button>
              <p>主题和记忆都保留来源；打开详情可核对原文和最近的使用记录。</p>
            </>
          )}
        </div>
      ) : null}
    </section>
  );
}

function latestTimelineStatus(value: Record<string, unknown>): string {
  const date = stringValue(value.date);
  if (!date) return '尚无日记';
  const status = ({ draft: '待审核', approved: '已发布', rejected: '已驳回', superseded: '已更新' } as Record<string, string>)[stringValue(value.status)] ?? '已整理';
  return `${date} ${status}`;
}

function projectionStatus(value: Record<string, unknown>): string {
  if (!Object.keys(value).length) return '状态未知';
  if (value.fresh === true) return `${numberValue(value.retrievalDocuments)} 份检索文档`;
  if (value.projectionInitialized === false) return '尚未初始化';
  if (numberValue(value.dead)) return `${numberValue(value.dead)} 项需要人工处理`;
  if (numberValue(value.backlog)) return `${numberValue(value.backlog)} 项正在同步`;
  return '等待重新校验';
}
