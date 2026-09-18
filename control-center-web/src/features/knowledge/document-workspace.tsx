import {
  ArrowLeft,
  ChevronDown,
  CircleStop,
  Download,
  Eye,
  FileImage,
  FileSearch,
  FileText,
  GalleryHorizontalEnd,
  Grid3X3,
  PanelRightOpen,
  RotateCcw,
  Rows3,
  Search,
  Table2,
  Trash2,
  Upload,
  X,
} from 'lucide-react';
import { useEffect, useMemo, useRef, useState, type DragEvent as ReactDragEvent } from 'react';
import ReactMarkdown from 'react-markdown';
import { Virtuoso } from 'react-virtuoso';
import remarkGfm from 'remark-gfm';
import { Button, Disclosure, EmptyState, IconButton, Input, Select, Skeleton, Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/primitives';
import { EvidenceEchoUsage } from '@/features/evidence-echo/EvidenceEchoUsage';
import { InlineNotice, StatusBadge, publicErrorText } from '@/features/overview/management-ui';
import { TraceAgentHandoffButton } from '@/features/trace-agent/handoff';
import type { ControlTransport } from '@/platform/transport';
import type {
  KnowledgeAsset,
  KnowledgeDocument,
  KnowledgeDocumentDetail,
  KnowledgeIndexJob,
  KnowledgeParserMode,
  KnowledgeSearchHit,
  KnowledgeTableArtifact,
} from './api';
import { publicKnowledgeText } from './public-copy';
import { extractMarkdownOutline, type MarkdownOutlineItem } from './reading-outline';

export interface KnowledgeUploadItem {
  id: string;
  fileName: string;
  byteSize: number;
  parser: KnowledgeParserMode;
  status: 'queued' | 'uploading' | 'accepted' | 'failed';
  documentId: string;
  error: string;
  file?: File;
}

export function KnowledgeMaterialsPanel({
  detail,
  detailError,
  detailLoading,
  documents,
  dropSupported,
  error,
  loading,
  filter: initialFilter,
  onFilterChange,
  importError,
  importing,
  onDelete,
  onImport,
  onImportFiles,
  onOpen,
  onReparse,
  onRetryList,
  onRetryDetail,
  onRetryUpload,
  onClearUploads,
  onSelect,
  pendingDocumentId,
  selectedDocumentId,
  uploadItems,
}: {
  detail: KnowledgeDocumentDetail | null;
  detailError: Error | null;
  detailLoading: boolean;
  documents: readonly KnowledgeDocument[];
  dropSupported: boolean;
  error: Error | null;
  loading: boolean;
  filter: string;
  onFilterChange: (filter: string) => void;
  importError: Error | null;
  importing: boolean;
  onDelete: (document: KnowledgeDocument, trigger: HTMLElement) => void;
  onImport: () => void;
  onImportFiles: (files: File[]) => void;
  onOpen: (documentId: string) => void;
  onReparse: (document: KnowledgeDocument, trigger: HTMLElement) => void;
  onRetryList: () => void;
  onRetryDetail: () => void;
  onRetryUpload: (item: KnowledgeUploadItem) => void;
  onClearUploads: () => void;
  onSelect: (documentId: string) => void;
  pendingDocumentId: string;
  selectedDocumentId: string;
  uploadItems: readonly KnowledgeUploadItem[];
}) {
  const summary = useMemo(() => summarizeDocuments(documents), [documents]);
  const [filter, setLocalFilter] = useState(initialFilter);
  const setFilter = (value: string) => { setLocalFilter(value); onFilterChange(value); };
  const normalizedFilter = filter.trim().toLowerCase();
  const visibleDocuments = useMemo(() => (
    normalizedFilter
      ? documents.filter((document) => document.name.toLowerCase().includes(normalizedFilter))
      : documents
  ), [documents, normalizedFilter]);
  const selected = documents.find((item) => item.id === selectedDocumentId) ?? documents[0] ?? null;
  const listUnavailable = !documents.length && (loading || Boolean(error));
  return (
    <div className="knowledge-panel knowledge-materials">
      <div className="knowledge-panel__toolbar">
        <div><strong>资料</strong>{!listUnavailable ? <span>{documents.length} 个文件 · {formatBytes(summary.bytes)} · {summary.ready} 个可检索</span> : null}</div>
      </div>
      {!listUnavailable ? <dl className="knowledge-material-stats">
        <div><dt>可检索</dt><dd>{summary.ready}</dd></div>
        <div><dt>处理中</dt><dd>{summary.processing}</dd></div>
        <div><dt>需处理</dt><dd>{summary.attention}</dd></div>
        <div><dt>已索引段落</dt><dd>{summary.chunks}</dd></div>
      </dl> : null}
      <MaterialsDropzone dropSupported={dropSupported} importing={importing} onImport={onImport} onImportFiles={onImportFiles} />
      <UploadQueue items={uploadItems} onClear={onClearUploads} onRetry={onRetryUpload} />
      {error ? <InlineNotice title="文件列表暂不可用" tone="warning"><p>{publicErrorText(error, '可以重新读取文件列表。')}{documents.length ? ' 以下保留已读取的资料。' : ''}</p><Button onClick={onRetryList} size="small" variant="quiet">重新读取文件列表</Button></InlineNotice> : null}
      {importError ? <InlineNotice title="导入未完成" tone="warning">{publicErrorText(importError, '请查看上传队列后重试。')}</InlineNotice> : null}
      {documents.length ? (
        <div className="knowledge-material-workspace">
          <div className="knowledge-material-list" aria-label="知识库文件">
            <div className="knowledge-material-filter">
              <Search aria-hidden="true" size={13} />
              <Input aria-label="筛选文件" onChange={(event) => setFilter(event.target.value)} placeholder="按文件名筛选" value={filter} />
              <span>{normalizedFilter ? `${visibleDocuments.length} / ${documents.length}` : `${documents.length} 个文件`}</span>
              {normalizedFilter ? <IconButton icon={<X size={13} />} label="清除筛选" onClick={() => setFilter('')} size="small" tooltip /> : null}
            </div>
            <div className="knowledge-material-list__head"><span>文件</span><span>状态</span><span>段落</span><span>操作</span></div>
            {visibleDocuments.length ? (
              <Virtuoso
                className="knowledge-material-list__body"
                data={visibleDocuments}
                itemContent={(_index, document) => (
                  <div className="knowledge-material-row" data-selected={selected?.id === document.id || undefined}>
                    <button aria-current={selected?.id === document.id || undefined} className="knowledge-material-row__select" onClick={() => onSelect(document.id)} type="button">
                      <FileText aria-hidden="true" size={15} />
                      <span><strong>{document.name}</strong><small>{parserLabel(document.parser)} · {formatBytes(document.byteSize)}</small></span>
                    </button>
                    <StatusBadge label={documentStatusLabel(document.status)} tone={documentTone(document.status)} />
                    <span className="knowledge-material-row__chunks">{document.chunkCount || '—'}</span>
                    <span className="knowledge-material-row__actions">
                      <IconButton disabled={pendingDocumentId === document.id || ['queued', 'parsing', 'indexing'].includes(document.status)} icon={<RotateCcw size={13} />} label={`重新解析 ${document.name}`} onClick={(event) => onReparse(document, event.currentTarget)} size="small" tooltip tooltipSide="top" />
                      <IconButton icon={<PanelRightOpen size={13} />} label={`查看 ${document.name}`} onClick={() => onOpen(document.id)} size="small" tooltip tooltipSide="top" />
                      <IconButton icon={<Trash2 size={13} />} label={`删除 ${document.name}`} onClick={(event) => onDelete(document, event.currentTarget)} size="small" tooltip tooltipSide="top" />
                    </span>
                    {['queued', 'parsing', 'indexing'].includes(document.status) ? <i className="knowledge-material-row__progress" style={{ '--document-progress': document.progress } as React.CSSProperties} /> : null}
                  </div>
                )}
              />
            ) : (
              <EmptyState
                action={<Button onClick={() => setFilter('')} size="small">显示全部文件</Button>}
                description={`没有名称包含“${filter.trim()}”的文件；清除筛选可查看全部 ${documents.length} 个文件。`}
                icon={FileSearch}
                title="没有匹配的文件"
              />
            )}
          </div>
          <DocumentSummary detail={detail} document={selected} error={detailError} loading={detailLoading} onReparse={onReparse} onRetry={onRetryDetail} reparsePending={Boolean(selected && pendingDocumentId === selected.id)} />
        </div>
      ) : loading ? <KnowledgeReadingLoading label="正在读取文件列表" /> : !error ? (
        <EmptyState description="通过上方导入区选择或拖入文件；文件会在这里排队解析并进入可检索目录。" icon={FileText} title="还没有资料" />
      ) : null}
    </div>
  );
}

function MaterialsDropzone({
  dropSupported,
  importing,
  onImport,
  onImportFiles,
}: {
  dropSupported: boolean;
  importing: boolean;
  onImport: () => void;
  onImportFiles: (files: File[]) => void;
}) {
  const [dragActive, setDragActive] = useState(false);
  const [dropNotice, setDropNotice] = useState('');
  const dragDepth = useRef(0);
  const dropUnsupportedNotice = '当前运行环境不支持拖放导入；请点击导入区改用系统文件选择。';
  const resetDragState = () => {
    dragDepth.current = 0;
    setDragActive(false);
  };
  const handleDrop = (event: ReactDragEvent<HTMLButtonElement>) => {
    event.preventDefault();
    resetDragState();
    if (importing) {
      setDropNotice('正在导入上一批文件，完成后再拖入新文件。');
      return;
    }
    if (!dropSupported) {
      setDropNotice(dropUnsupportedNotice);
      return;
    }
    const files = [...(event.dataTransfer?.files ?? [])];
    if (!files.length) {
      setDropNotice('拖入的内容里没有文件；请直接拖动本机文件，或点击导入区选择。');
      return;
    }
    if (files.length > 20) {
      setDropNotice(`一次最多导入 20 个文件：已开始前 20 个，其余 ${files.length - 20} 个请分批拖入。`);
      onImportFiles(files.slice(0, 20));
      return;
    }
    setDropNotice('');
    onImportFiles(files);
  };
  return (
    <div className="knowledge-dropzone-region" aria-label="导入资料">
      <button
        aria-busy={importing || undefined}
        aria-label="导入文件"
        className="knowledge-dropzone"
        data-active={(dragActive && dropSupported && !importing) || undefined}
        onClick={() => { if (!importing) { setDropNotice(''); onImport(); } }}
        onDragEnter={(event) => {
          event.preventDefault();
          dragDepth.current += 1;
          setDragActive(true);
          if (!dropSupported) setDropNotice(dropUnsupportedNotice);
        }}
        onDragLeave={() => { dragDepth.current = Math.max(0, dragDepth.current - 1); if (!dragDepth.current) setDragActive(false); }}
        onDragOver={(event) => event.preventDefault()}
        onDrop={handleDrop}
        type="button"
      >
        <Upload aria-hidden="true" size={17} />
        <span>
          <strong>{importing ? '正在导入文件…' : dropSupported ? '拖放文件到这里，或点击选择' : '点击选择本机文件导入'}</strong>
          <small>支持 PDF、Word、PPT、Excel、Markdown、文本与图片 · 单次最多 20 个</small>
        </span>
      </button>
      {dropNotice ? <InlineNotice title="导入提示" tone="info">{dropNotice}</InlineNotice> : null}
    </div>
  );
}

function UploadQueue({ items, onClear, onRetry }: { items: readonly KnowledgeUploadItem[]; onClear: () => void; onRetry: (item: KnowledgeUploadItem) => void }) {
  if (!items.length) return null;
  const active = items.filter((item) => item.status === 'queued' || item.status === 'uploading').length;
  const failed = items.filter((item) => item.status === 'failed').length;
  return (
    <section className="knowledge-upload-queue" aria-label="上传队列">
      <header><div><strong>上传队列</strong><span>{active} 个处理中 · {failed} 个失败 · {items.length} 个文件</span></div><Button disabled={active > 0} onClick={onClear} size="small" variant="quiet">清空记录</Button></header>
      <div>
        {items.map((item) => (
          <article key={item.id}>
            <Upload size={14} />
            <span><strong>{item.fileName}</strong><small>{formatBytes(item.byteSize)} · {parserLabel(item.parser)}</small>{item.error ? <em>{item.error}</em> : null}</span>
            <StatusBadge label={uploadStatusLabel(item.status)} tone={uploadTone(item.status)} />
            {item.status === 'failed' ? <IconButton icon={<RotateCcw size={13} />} label={`重试上传 ${item.fileName}`} onClick={() => onRetry(item)} size="small" tooltip /> : null}
          </article>
        ))}
      </div>
    </section>
  );
}

function DocumentSummary({ detail, document, error, loading, onReparse, onRetry, reparsePending }: { detail: KnowledgeDocumentDetail | null; document: KnowledgeDocument | null; error: Error | null; loading: boolean; onReparse: (document: KnowledgeDocument, trigger: HTMLElement) => void; onRetry: () => void; reparsePending: boolean }) {
  if (!document) return null;
  return (
    <aside className="knowledge-document-summary" aria-label={`${document.name} 处理与详情`}>
      <header><FileText size={17} /><div><strong>{document.name}</strong><span>{fileFormatLabel(document.mimeType)}</span></div></header>
      <DocumentPipeline document={document} onReparse={onReparse} reparsePending={reparsePending} />
      {loading ? <p className="knowledge-detail-loading">正在读取材料详情…</p> : null}
      {error ? <InlineNotice title="详情暂不可用" tone="warning"><p>{publicErrorText(error, '可以重新读取材料详情。')}</p><Button onClick={onRetry} size="small" variant="quiet">重新读取详情</Button></InlineNotice> : null}
      <dl>
        <div><dt>解析方式</dt><dd>{parserLabel(document.parser)}</dd></div>
        <div><dt>页数</dt><dd>{document.pageCount || detail?.pages.length || '未提供'}</dd></div>
        <div><dt>段落</dt><dd>{detail?.chunkTotal || document.chunkCount || 0}</dd></div>
        <div><dt>文件大小</dt><dd>{formatBytes(document.byteSize)}</dd></div>
        <div><dt>更新时间</dt><dd>{formatTime(document.updatedAtMs)}</dd></div>
      </dl>
      <Disclosure className="knowledge-document-summary__advanced" summary="高级：材料详情"><dl><div><dt>文件格式</dt><dd>{document.mimeType || '未提供'}</dd></div><div><dt>解析版本</dt><dd>{document.parserVersion || '未提供'}</dd></div><div><dt>内容容量</dt><dd>{document.tokenCount || '未提供'}</dd></div><div><dt>内容指纹</dt><dd>{shortHash(document.sha256)}</dd></div></dl></Disclosure>
      <div className="knowledge-document-summary__counts">
        <span><FileImage size={13} />{detail ? `${detail.assets.length} 个图片/附件` : '图片/附件待读取'}</span>
        <span><Table2 size={13} />{detail ? `${detail.tables.length} 个表格` : '表格待读取'}</span>
      </div>
    </aside>
  );
}

type PipelineStageState = 'done' | 'active' | 'waiting' | 'failed' | 'stale';

interface PipelineStage {
  id: 'received' | 'parse' | 'chunk' | 'index' | 'ready';
  label: string;
  state: PipelineStageState;
}

function DocumentPipeline({
  document,
  onReparse,
  reparsePending,
}: {
  document: KnowledgeDocument;
  onReparse: (document: KnowledgeDocument, trigger: HTMLElement) => void;
  reparsePending: boolean;
}) {
  const stages = pipelineStages(document);
  const activeStage = stages.find((stage) => stage.state === 'active');
  const progressPercent = activeStage && document.progress > 0 && document.progress < 1
    ? Math.round(document.progress * 100)
    : null;
  const recoverable = document.status === 'failed' || document.status === 'stale';
  return (
    <div className="knowledge-pipeline" data-status={document.status}>
      <ol aria-label={`${document.name} 处理流水线`} className="knowledge-pipeline__stages">
        {stages.map((stage, index) => (
          <li aria-current={stage.state === 'active' ? 'step' : undefined} data-state={stage.state} key={stage.id}>
            {index > 0 ? <i aria-hidden="true" className="knowledge-pipeline__link" /> : null}
            <span aria-hidden="true" className="knowledge-pipeline__dot" />
            <span className="knowledge-pipeline__label">
              {stage.label}
              {stage === activeStage && progressPercent !== null ? <b>{progressPercent}%</b> : null}
            </span>
          </li>
        ))}
      </ol>
      <p className="knowledge-pipeline__note">{pipelineNote(document)}</p>
      {recoverable ? (
        <div className="knowledge-pipeline__recovery">
          {document.status === 'failed' && document.error ? (
            <span className="knowledge-document-summary__error" role="alert">{document.error}</span>
          ) : null}
          <Button
            disabled={reparsePending}
            leadingIcon={<RotateCcw size={13} />}
            loading={reparsePending}
            onClick={(event) => onReparse(document, event.currentTarget)}
            size="small"
            variant="quiet"
          >
            {document.status === 'stale' ? '重新解析以重建' : '重新解析'}
          </Button>
          <TraceAgentHandoffButton handoff={{
            kind: 'knowledge',
            entityId: document.id,
            title: `知识文档解析${document.status === 'failed' ? '失败' : '过期'}`,
            summary: document.error || pipelineNote(document),
            error: document.error || undefined,
            sourceRoute: `/knowledge?base=${encodeURIComponent(document.baseId)}&document=${encodeURIComponent(document.id)}`,
            refs: { baseId: document.baseId, documentId: document.id, status: document.status },
          }} />
        </div>
      ) : null}
    </div>
  );
}

function pipelineStages(document: KnowledgeDocument): PipelineStage[] {
  const base: PipelineStage[] = [
    { id: 'received', label: '接收', state: 'done' },
    { id: 'parse', label: '解析', state: 'waiting' },
    { id: 'chunk', label: '切分', state: 'waiting' },
    { id: 'index', label: '索引', state: 'waiting' },
    { id: 'ready', label: '可检索', state: 'waiting' },
  ];
  const set = (states: Partial<Record<PipelineStage['id'], PipelineStageState>>) => (
    base.map((stage) => ({ ...stage, state: states[stage.id] ?? stage.state }))
  );
  const stageText = (document.stage || document.status).toLowerCase();
  const reachedIndex = stageText.includes('index') || stageText.includes('embed') || document.chunkCount > 0;
  switch (document.status) {
    case 'ready':
      return set({ parse: 'done', chunk: 'done', index: 'done', ready: 'done' });
    case 'stale':
      return set({ parse: 'done', chunk: 'done', index: 'stale', ready: 'stale' });
    case 'failed':
      return reachedIndex
        ? set({ parse: 'done', chunk: 'done', index: 'failed' })
        : set({ parse: 'failed' });
    case 'indexing':
      return set({ parse: 'done', chunk: 'done', index: 'active' });
    case 'parsing':
      return set({ parse: 'active' });
    default:
      return set({});
  }
}

function pipelineNote(document: KnowledgeDocument): string {
  const percent = document.progress > 0 && document.progress < 1 ? `，已完成 ${Math.round(document.progress * 100)}%` : '';
  switch (document.status) {
    case 'ready':
      return '解析与索引已完成，这份材料可以检索。';
    case 'stale':
      return '内容已解析；切分或检索配置已更新，重建完成前检索仍使用现有索引。';
    case 'failed':
      return (document.stage || '').toLowerCase().includes('index') || document.chunkCount > 0
        ? '索引没有完成；重新解析会重新生成段落与索引。'
        : '解析没有完成；重新解析会从现有文件重新开始。';
    case 'indexing':
      return `正在写入检索索引${percent}。`;
    case 'parsing':
      return `正在解析内容${percent}。`;
    default:
      return '已接收，排队等待解析。';
  }
}

export function KnowledgeDocumentViewer({
  detail,
  error,
  loading,
  onRetry,
  onBack,
  backLabel,
  onImportMaterials,
  onSelectDocument,
  selectedDocumentId,
  documents,
  transport,
  focusHit,
  hasMoreChunks,
  hasMoreContent,
  loadMoreChunksFailed,
  loadingMoreChunks,
  loadingMoreContent,
  onLoadMoreChunks,
  onLoadMoreContent,
}: {
  detail: KnowledgeDocumentDetail | null;
  error: Error | null;
  loading: boolean;
  onRetry: () => void;
  onBack: () => void;
  backLabel: string;
  onImportMaterials: () => void;
  onSelectDocument: (documentId: string) => void;
  selectedDocumentId: string;
  documents: readonly KnowledgeDocument[];
  transport: ControlTransport;
  focusHit: KnowledgeSearchHit | null;
  hasMoreChunks: boolean;
  hasMoreContent: boolean;
  loadMoreChunksFailed: boolean;
  loadingMoreChunks: boolean;
  loadingMoreContent: boolean;
  onLoadMoreChunks: () => void;
  onLoadMoreContent: () => void;
}) {
  const [view, setView] = useState<'source' | 'markdown' | 'chunks' | 'artifacts'>('markdown');
  const pageCount = detail ? detail.pages.length || detail.document.pageCount : 0;
  const readableDocuments = detail?.document.id === selectedDocumentId && !documents.some((document) => document.id === selectedDocumentId)
    ? [detail.document, ...documents]
    : documents;
  const selectedDocument = readableDocuments.find((item) => item.id === (selectedDocumentId || readableDocuments[0]?.id)) ?? null;
  useEffect(() => setView('markdown'), [selectedDocumentId]);
  useEffect(() => { if (focusHit?.documentId === selectedDocumentId) setView('chunks'); }, [focusHit, selectedDocumentId]);
  if (!documents.length && !selectedDocumentId) {
    if (loading) return <KnowledgeReadingLoading label="正在读取文件列表" />;
    if (error) return <InlineNotice title="文件列表暂不可用" tone="warning"><p>{publicErrorText(error, '可以重新读取文件列表。')}</p><Button onClick={onRetry} size="small" variant="quiet">重新读取文件列表</Button></InlineNotice>;
    return (
      <EmptyState
        action={<Button onClick={onImportMaterials} size="small">去导入资料</Button>}
        description="先在“资料”页导入文件，再回来查看解析结果。"
        icon={FileText}
        title="先导入资料"
      />
    );
  }
  return (
    <div className="knowledge-panel knowledge-viewer">
      <div className="knowledge-viewer__bar">
        <Button className="knowledge-viewer__back" leadingIcon={<ArrowLeft size={14} />} onClick={onBack} size="small" variant="quiet">{backLabel}</Button>
        <label className="knowledge-viewer__document"><span>材料</span><Select aria-label="材料" disabled={!readableDocuments.length} onValueChange={onSelectDocument} options={readableDocuments.map((item) => ({ value: item.id, label: item.name }))} value={selectedDocumentId || readableDocuments[0]?.id} /></label>
        <div aria-label="材料状态与内容统计" className="knowledge-viewer__meta" role="group">
          {selectedDocument ? <StatusBadge label={documentStatusLabel(selectedDocument.status)} tone={documentTone(selectedDocument.status)} /> : null}
          {detail ? <><span>{detail.chunkTotal} 个段落</span><span>{pageCount ? `${pageCount} 页` : '页码未提供'}</span><span>{detail.assets.length} 个产物</span></> : null}
        </div>
      </div>
      {loading && !detail ? <KnowledgeReadingLoading label="正在读取解析结果" /> : null}
      {error ? <InlineNotice title="暂时无法查看材料" tone="warning"><p>{publicErrorText(error, '可以重新读取材料。')}</p><Button onClick={onRetry} size="small" variant="quiet">重新读取材料</Button></InlineNotice> : null}
      {detail ? (
        <Tabs className="knowledge-document-tabs" onValueChange={(value) => setView(value === 'source' || value === 'chunks' || value === 'artifacts' ? value : 'markdown')} value={view}>
          <TabsList aria-label="材料查看方式">
            <TabsTrigger value="source"><FileText size={13} />源文件</TabsTrigger>
            <TabsTrigger value="markdown"><Rows3 size={13} />正文</TabsTrigger>
            <TabsTrigger value="chunks"><Grid3X3 size={13} />段落</TabsTrigger>
            <TabsTrigger value="artifacts"><GalleryHorizontalEnd size={13} />解析产物</TabsTrigger>
          </TabsList>
          <TabsContent value="source"><DocumentSource detail={detail} transport={transport} /></TabsContent>
          <TabsContent value="markdown"><DocumentContent detail={detail} hasMore={hasMoreContent} loadingMore={loadingMoreContent} onLoadMore={onLoadMoreContent} /></TabsContent>
          <TabsContent value="chunks"><ChunkGallery detail={detail} focusHit={focusHit?.documentId === selectedDocumentId ? focusHit : null} hasMore={hasMoreChunks} loadFailed={loadMoreChunksFailed} loadingMore={loadingMoreChunks} onLoadMore={onLoadMoreChunks} /></TabsContent>
          <TabsContent value="artifacts"><ArtifactGallery assets={detail.assets} document={detail.document} tables={detail.tables} transport={transport} /></TabsContent>
        </Tabs>
      ) : null}
      {selectedDocument ? (
        <Disclosure className="knowledge-viewer__usage" contentClassName="knowledge-viewer__usage-content" key={selectedDocument.id} summary={<><ChevronDown aria-hidden="true" size={13} />装配记录</>}>
          <EvidenceEchoUsage appId="knowledge" entityId={selectedDocument.id} entityLabel={selectedDocument.name} />
        </Disclosure>
      ) : null}
    </div>
  );
}

function KnowledgeReadingLoading({ label }: { label: string }) {
  return <div aria-label={label} className="knowledge-reading-loading" role="status"><p>{label}…</p><Skeleton /><Skeleton /><Skeleton /></div>;
}

function DocumentContent({ detail, hasMore, loadingMore, onLoadMore }: { detail: KnowledgeDocumentDetail; hasMore: boolean; loadingMore: boolean; onLoadMore: () => void }) {
  const bodyRef = useRef<HTMLDivElement>(null);
  const [activeHeadingId, setActiveHeadingId] = useState('');
  const publicLines = useMemo(() => (
    detail.contentWindow.map((line) => ({ lineNumber: line.lineNumber, content: publicKnowledgeText(line.content) }))
  ), [detail.contentWindow]);
  const outline = useMemo(() => extractMarkdownOutline(publicLines), [publicLines]);
  useEffect(() => setActiveHeadingId(''), [detail.document.id]);
  if (publicLines.length) {
    const markdown = publicLines.map((line) => line.content).join('\n');
    return (
      <div className="knowledge-reading-desk" data-has-outline={outline.length > 0 || undefined}>
        {outline.length ? (
          <nav aria-label="文档目录" className="knowledge-reading-outline">
            <header><strong>目录</strong><span>{outline.length} 个标题</span></header>
            <ol>
              {outline.map((item) => (
                <li data-level={Math.min(item.level, 4)} key={item.id}>
                  <button
                    aria-current={activeHeadingId === item.id ? 'location' : undefined}
                    onClick={() => { jumpToHeading(bodyRef.current, outline, item); setActiveHeadingId(item.id); }}
                    type="button"
                  >
                    {item.text}
                  </button>
                </li>
              ))}
            </ol>
            {hasMore ? <p>目录来自已加载的 {publicLines.length} / {detail.contentLineTotal} 行，继续加载正文后会补全。</p> : null}
          </nav>
        ) : null}
        <div className="knowledge-markdown-preview">
          <header><span>解析正文</span><b>{detail.contentWindow.length} / {detail.contentLineTotal || detail.contentWindow.length} 行 · {formatBytes(detail.artifact.byteSize)}</b></header>
          <div className="knowledge-markdown-body" ref={bodyRef}>
            <ReactMarkdown
              components={{
                img: ({ alt }) => <span className="knowledge-markdown-blocked-image">图片引用已隔离：{alt || '未命名图片'}</span>,
                a: ({ children, href }) => { const safe = safeMarkdownLink(href); return safe ? <a href={safe} rel="noreferrer" target="_blank">{children}</a> : <span>{children}</span>; },
              }}
              remarkPlugins={[remarkGfm]}
            >{markdown}</ReactMarkdown>
          </div>
          {hasMore ? <footer><span>已加载 {detail.contentWindow.length} / {detail.contentLineTotal} 行</span><Button loading={loadingMore} onClick={onLoadMore} size="small">继续加载正文</Button></footer> : null}
        </div>
      </div>
    );
  }
  const pages = groupChunksByPage(detail);
  return pages.length ? (
    <div className="knowledge-page-preview">
      {pages.map((page) => (
        <section key={page.key}>
          <header><span>{page.page ? `第 ${page.page} 页` : '无页码内容'}</span><b>{page.chunks.length} 个段落</b></header>
          {page.chunks.map((chunk) => <article key={chunk.id}>{chunk.heading ? <h4>{publicKnowledgeText(chunk.heading)}</h4> : null}<p>{publicKnowledgeText(chunk.content)}</p></article>)}
        </section>
      ))}
    </div>
  ) : <EmptyState description="当前文件还没有返回可显示的正文；可回到“资料”页重新解析，或切换到源文件查看。" icon={FileText} title="暂无解析正文" />;
}

// The 目录 addresses rendered headings by plain text + occurrence: the outline
// and the DOM both derive from the same Markdown, so the n-th outline entry
// with a given text is the n-th rendered heading with that text. Unmatched
// entries (exotic inline markup) degrade to no scroll instead of a wrong jump.
function jumpToHeading(body: HTMLElement | null, outline: readonly MarkdownOutlineItem[], item: MarkdownOutlineItem): void {
  if (!body) return;
  const occurrence = outline.filter((entry) => entry.index < item.index && entry.text === item.text).length;
  const matches = [...body.querySelectorAll<HTMLElement>('h1, h2, h3, h4, h5, h6')]
    .filter((node) => (node.textContent ?? '').replace(/\s+/gu, ' ').trim() === item.text);
  const target = matches[occurrence];
  if (target && typeof target.scrollIntoView === 'function') {
    target.scrollIntoView({ behavior: prefersReducedMotion() ? 'auto' : 'smooth', block: 'start' });
  }
}

function prefersReducedMotion(): boolean {
  if (document.documentElement.dataset.reduceMotion === 'true') return true;
  return typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function DocumentSource({ detail, transport }: { detail: KnowledgeDocumentDetail; transport: ControlTransport }) {
  const source = useKnowledgeDocumentSource(detail, transport);
  if (!detail.document.sourceReadPath || !transport.readKnowledgeDocumentSource) {
    return <EmptyState description="当前运行环境未提供可安全读取的源文件；请回到“资料”页重新解析。" icon={FileText} title="源文件预览不可用" />;
  }
  if (source.loading || !source.error && !source.url) return <p className="knowledge-detail-loading">正在安全读取源文件…</p>;
  if (source.error || !source.url) {
    return <InlineNotice title="源文件暂不可用" tone="warning"><p>{publicErrorText(source.error, '可以重新读取源文件。')}</p><Button leadingIcon={<RotateCcw size={13} />} onClick={source.retry} size="small" variant="quiet">重新读取源文件</Button></InlineNotice>;
  }
  if (source.mimeType === 'application/pdf') {
    return <iframe className="knowledge-source-frame" src={source.url} title={`${detail.document.name} 源文件`} />;
  }
  if (source.mimeType.startsWith('image/')) {
    return <div className="knowledge-source-image"><img alt={detail.document.name} src={source.url} /></div>;
  }
  return <div className="knowledge-source-fallback"><FileText size={24} /><strong>{detail.document.name}</strong><a href={source.url} rel="noreferrer" target="_blank">打开源文件</a></div>;
}

function ChunkGallery({ detail, focusHit, hasMore, loadFailed, loadingMore, onLoadMore }: { detail: KnowledgeDocumentDetail; focusHit: KnowledgeSearchHit | null; hasMore: boolean; loadFailed: boolean; loadingMore: boolean; onLoadMore: () => void }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const focusedLoaded = Boolean(focusHit && detail.chunks.some((chunk) => chunk.id === focusHit.id));
  useEffect(() => {
    if (focusHit && !focusedLoaded && hasMore && !loadFailed && !loadingMore) onLoadMore();
  }, [focusHit, focusedLoaded, hasMore, loadFailed, loadingMore, onLoadMore]);
  useEffect(() => {
    if (!focusHit || !focusedLoaded) return;
    const node = containerRef.current?.querySelector<HTMLElement>('[data-focused="true"]');
    if (node && typeof node.scrollIntoView === 'function') node.scrollIntoView({ block: 'center' });
  }, [focusHit, focusedLoaded, detail.chunks.length]);
  return detail.chunks.length ? (
    <div className="knowledge-chunk-grid" ref={containerRef}>
      {focusHit ? <div className="knowledge-focus-banner"><FileSearch size={14} /><span>{focusedLoaded ? `已定位检索命中：${publicKnowledgeText(focusHit.title)}` : loadFailed ? `命中段落未能加载：${publicKnowledgeText(focusHit.title)}；已读段落已保留，可重试继续定位。` : hasMore ? `正在加载命中段落：${publicKnowledgeText(focusHit.title)}` : `命中来自较早索引：${publicKnowledgeText(focusHit.title)}；重新处理材料后可更新。`}</span></div> : null}
      {detail.chunks.map((chunk) => (
        <article data-focused={focusHit?.id === chunk.id || undefined} key={chunk.id}>
          <header><b>#{chunk.ordinal + 1}{focusHit?.id === chunk.id ? ' · 检索命中' : ''}</b><span>{chunk.page ? `第 ${chunk.page} 页` : chunk.lineStart ? `第 ${chunk.lineStart} 行` : '无页码'}</span></header>
          {chunk.heading ? <h4>{publicKnowledgeText(chunk.heading)}</h4> : null}
          <p>{focusHit?.id === chunk.id ? <HighlightedChunkText content={publicKnowledgeText(chunk.content)} excerpt={publicKnowledgeText(focusHit.excerpt)} /> : publicKnowledgeText(chunk.content)}</p>
          <footer><span>文档段落</span><Disclosure className="knowledge-chunk-detail" contentClassName="knowledge-chunk-detail__content" summary="高级：段落详情"><span>{chunk.tokenCount ? `${chunk.tokenCount} Token` : 'Token 未统计'}</span><span>{chunk.id}</span></Disclosure></footer>
        </article>
      ))}
      {hasMore ? <div className="knowledge-more-note"><span>已显示 {detail.chunks.length} / {detail.chunkTotal} 个段落</span><Button loading={loadingMore} onClick={onLoadMore} size="small">{loadFailed ? focusHit && !focusedLoaded ? '重试加载命中段落' : '重试加载更多' : '加载更多'}</Button></div> : <p className="knowledge-more-note">已加载全部 {detail.chunkTotal} 个段落。</p>}
    </div>
  ) : <EmptyState description="当前文件还没有可展示的段落；完成解析后可在此查看检索命中。" icon={Grid3X3} title="暂无段落" />;
}

function HighlightedChunkText({ content, excerpt }: { content: string; excerpt: string }) {
  const needle = excerpt.trim();
  const index = needle ? content.indexOf(needle) : -1;
  return index >= 0 ? <>{content.slice(0, index)}<mark>{needle}</mark>{content.slice(index + needle.length)}</> : <mark>{content}</mark>;
}

function ArtifactGallery({ assets, document, tables, transport }: { assets: readonly KnowledgeAsset[]; document: KnowledgeDocument; tables: readonly KnowledgeTableArtifact[]; transport: ControlTransport }) {
  const images = assets.filter((item) => item.mimeType.startsWith('image/') && item.readPath);
  const attachments = assets.filter((item) => !images.includes(item));
  if (!assets.length && !tables.length) return <EmptyState description="当前解析没有返回图片或表格产物；重新解析后可再次检查。" icon={GalleryHorizontalEnd} title="暂无解析产物" />;
  return (
    <div className="knowledge-artifacts">
      {images.length ? <section><header><FileImage size={14} /><strong>图片</strong><span>{images.length}</span></header><div className="knowledge-image-grid">{images.map((asset) => <KnowledgeAssetImage asset={asset} document={document} key={asset.id} transport={transport} />)}</div></section> : null}
      {tables.length ? <section><header><Table2 size={14} /><strong>表格</strong><span>{tables.length}</span></header><div className="knowledge-table-gallery">{tables.map((table) => <ParsedTable key={table.id} table={table} />)}</div></section> : null}
      {attachments.length ? <section><header><FileText size={14} /><strong>附件产物</strong><span>{attachments.length}</span></header><div className="knowledge-asset-list">{attachments.map((asset) => <KnowledgeAssetAttachment asset={asset} document={document} key={asset.id} transport={transport} />)}</div></section> : null}
    </div>
  );
}

function KnowledgeAssetImage({ asset, document, transport }: { asset: KnowledgeAsset; document: KnowledgeDocument; transport: ControlTransport }) {
  const binary = useKnowledgeAsset(document, asset, transport);
  return (
    <figure>
      {binary.loading ? <div className="knowledge-image-placeholder">正在读取…</div> : null}
      {binary.url ? <img alt={asset.caption || asset.name} loading="lazy" src={binary.url} /> : null}
      {binary.error ? <div className="knowledge-image-placeholder"><span>读取失败</span><Button aria-label={`重新读取 ${asset.name}`} leadingIcon={<RotateCcw size={13} />} onClick={binary.retry} size="small" variant="quiet">重新读取</Button></div> : null}
      <figcaption><span><strong>{asset.name}</strong><small>{asset.page ? `第 ${asset.page} 页 · ` : ''}{formatBytes(asset.byteSize)}</small></span>{binary.url ? <span className="knowledge-asset-actions"><a aria-label={`查看 ${asset.name}`} href={binary.url} rel="noreferrer" target="_blank" title={`查看 ${asset.name}`}><Eye size={13} /></a><a aria-label={`下载 ${asset.name}`} download={asset.name} href={binary.url} title={`下载 ${asset.name}`}><Download size={13} /></a></span> : null}</figcaption>
    </figure>
  );
}

function KnowledgeAssetAttachment({ asset, document, transport }: { asset: KnowledgeAsset; document: KnowledgeDocument; transport: ControlTransport }) {
  const binary = useKnowledgeAsset(document, asset, transport);
  return <div><FileText size={14} /><span><strong>{asset.name}</strong><small>{fileFormatLabel(asset.mimeType)} · {formatBytes(asset.byteSize)}{binary.loading ? ' · 正在读取…' : binary.error ? ' · 读取失败' : ''}</small></span>{binary.error ? <Button aria-label={`重新读取 ${asset.name}`} leadingIcon={<RotateCcw size={13} />} onClick={binary.retry} size="small" variant="quiet">重新读取</Button> : null}{binary.url ? <span className="knowledge-asset-actions"><a aria-label={`查看 ${asset.name}`} href={binary.url} rel="noreferrer" target="_blank"><Eye size={13} /></a><a aria-label={`下载 ${asset.name}`} download={asset.name} href={binary.url}><Download size={13} /></a></span> : null}</div>;
}

interface BinaryViewState {
  error: Error | null;
  loading: boolean;
  mimeType: string;
  url: string;
}

interface BinaryViewResult extends BinaryViewState {
  retry: () => void;
}

function useKnowledgeDocumentSource(detail: KnowledgeDocumentDetail, transport: ControlTransport): BinaryViewResult {
  const [state, setState] = useState<BinaryViewState>({ error: null, loading: false, mimeType: '', url: '' });
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!detail.document.sourceReadPath || !transport.readKnowledgeDocumentSource) {
      setState({ error: null, loading: false, mimeType: '', url: '' });
      return;
    }
    const controller = new AbortController();
    let objectUrl = '';
    let active = true;
    setState({ error: null, loading: true, mimeType: '', url: '' });
    void transport.readKnowledgeDocumentSource({
      kbId: detail.document.baseId,
      fileId: detail.document.id,
      signal: controller.signal,
    }).then((payload) => {
      if (!active) return;
      objectUrl = URL.createObjectURL(payload.blob);
      setState({ error: null, loading: false, mimeType: payload.mimeType, url: objectUrl });
    }).catch((error: unknown) => {
      if (!active || controller.signal.aborted) return;
      setState({ error: asError(error), loading: false, mimeType: '', url: '' });
    });
    return () => {
      active = false;
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [attempt, detail.document.baseId, detail.document.id, detail.document.sourceReadPath, transport]);
  return { ...state, retry: () => setAttempt((current) => current + 1) };
}

function useKnowledgeAsset(document: KnowledgeDocument, asset: KnowledgeAsset, transport: ControlTransport): BinaryViewResult {
  const [state, setState] = useState<BinaryViewState>({ error: null, loading: false, mimeType: '', url: '' });
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!asset.readPath || !transport.readKnowledgeAsset) {
      setState({ error: null, loading: false, mimeType: '', url: '' });
      return;
    }
    const controller = new AbortController();
    let objectUrl = '';
    let active = true;
    setState({ error: null, loading: true, mimeType: '', url: '' });
    void transport.readKnowledgeAsset({
      kbId: document.baseId,
      fileId: document.id,
      assetId: asset.id,
      signal: controller.signal,
    }).then((payload) => {
      if (!active) return;
      objectUrl = URL.createObjectURL(payload.blob);
      setState({ error: null, loading: false, mimeType: payload.mimeType, url: objectUrl });
    }).catch((error: unknown) => {
      if (!active || controller.signal.aborted) return;
      setState({ error: asError(error), loading: false, mimeType: '', url: '' });
    });
    return () => {
      active = false;
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [attempt, asset.id, asset.readPath, document.baseId, document.id, transport]);
  return { ...state, retry: () => setAttempt((current) => current + 1) };
}

function ParsedTable({ table }: { table: KnowledgeTableArtifact }) {
  const [visibleRows, setVisibleRows] = useState(20);
  useEffect(() => setVisibleRows(20), [table.id]);
  return (
    <article>
      <header><strong>{table.title}</strong><span>{table.page ? `第 ${table.page} 页` : ''}</span></header>
      {table.columns.length && table.rows.length ? <><div className="knowledge-table-scroll"><table><thead><tr>{table.columns.map((column, index) => <th key={`${index}:${column}`}>{column}</th>)}</tr></thead><tbody>{table.rows.slice(0, visibleRows).map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}</tr>)}</tbody></table></div>{visibleRows < table.rows.length ? <div className="knowledge-table-more"><span>{visibleRows} / {table.rows.length} 行</span><Button onClick={() => setVisibleRows((value) => Math.min(table.rows.length, value + 20))} size="small">加载更多</Button></div> : null}</> : <pre>{table.markdown || '表格内容未结构化'}</pre>}
    </article>
  );
}

export function KnowledgeJobsPanel({ cancellingJobId, cancelError, error, jobs, loading, onCancel, onRefresh }: { cancellingJobId: string; cancelError: unknown; error: Error | null; jobs: readonly KnowledgeIndexJob[]; loading: boolean; onCancel: (jobId: string) => void; onRefresh: () => void }) {
  const [expandedId, setExpandedId] = useState('');
  const active = jobs.filter((job) => ['queued', 'running', 'parsing', 'embedding', 'indexing'].includes(job.status.toLowerCase()));
  const summary = error
    ? jobs.length ? `保留上次读取的 ${jobs.length} 条记录` : '记录数量暂不可用'
    : loading && !jobs.length ? '记录数量待读取' : `${active.length} 个进行中 · ${jobs.length} 条记录`;
  return (
    <div className="knowledge-panel knowledge-jobs">
      <div className="knowledge-panel__toolbar"><div><strong>处理记录</strong><span>{summary}</span></div><IconButton disabled={loading} icon={<RotateCcw className={loading ? 'ui-spin' : undefined} size={14} />} label="刷新处理记录" onClick={onRefresh} size="small" tooltip /></div>
      {error ? <InlineNotice title="任务记录暂不可用" tone="warning">{publicErrorText(error, '刷新后重试。')}</InlineNotice> : null}
      {cancelError ? <InlineNotice title="任务未取消" tone="warning">{publicErrorText(cancelError, '请刷新任务状态后重试。')}</InlineNotice> : null}
      {jobs.length ? <div className="knowledge-job-list">{jobs.map((job) => {
        const expanded = expandedId === job.id;
        return <article data-expanded={expanded || undefined} key={job.id}><span className="knowledge-job-list__icon"><RotateCcw size={14} /></span><button aria-expanded={expanded} className="knowledge-job-list__summary" onClick={() => setExpandedId(expanded ? '' : job.id)} type="button"><span><strong>{job.documentName || job.kind}</strong><small>{jobStageLabel(job.stage)} · {formatTime(job.updatedAtMs || job.createdAtMs)}</small>{job.error ? <em>处理未完成，请展开查看详情。</em> : null}<i style={{ '--job-progress': terminalJobStatus(job.status) ? 1 : job.progress } as React.CSSProperties} /></span><ChevronDown aria-hidden="true" size={14} /></button><StatusBadge label={jobStatusLabel(job.status)} tone={jobTone(job.status)} />{job.cancellable ? <IconButton disabled={cancellingJobId === job.id} icon={<CircleStop size={14} />} label="取消任务" onClick={() => onCancel(job.id)} size="small" tooltip /> : null}{expanded ? <JobDetails job={job} /> : null}</article>;
      })}</div> : loading ? <p className="knowledge-detail-loading">正在读取处理记录…</p> : !error ? <EmptyState description="导入或重新处理材料后，进度和结果会显示在这里。" icon={RotateCcw} title="还没有处理记录" /> : null}
    </div>
  );
}

function JobDetails({ job }: { job: KnowledgeIndexJob }) {
  const finished = job.finishedAtMs || (terminalJobStatus(job.status) ? job.updatedAtMs : 0);
  const started = job.startedAtMs || job.createdAtMs;
  return <div className="knowledge-job-detail"><dl><div><dt>当前进度</dt><dd>{jobStageLabel(job.stage)}</dd></div><div><dt>耗时</dt><dd>{finished && started ? formatDuration(finished - started) : '进行中'}</dd></div></dl><ol aria-label="任务阶段记录"><li><span>创建</span><time>{formatTime(job.createdAtMs)}</time></li>{job.startedAtMs ? <li><span>开始 · {jobStageLabel(job.stage)}</span><time>{formatTime(job.startedAtMs)}</time></li> : null}{finished ? <li><span>{jobStatusLabel(job.status)}</span><time>{formatTime(finished)}</time></li> : null}</ol>{job.error ? <p>{publicErrorText(job.error, '这项处理没有完成，请稍后重试。')}</p> : null}<Disclosure className="knowledge-job-detail__technical" summary="高级：处理详情"><dl><div><dt>任务 ID</dt><dd>{job.id}</dd></div><div><dt>类型</dt><dd>{job.kind}</dd></div><div><dt>解析器</dt><dd>{parserLabel(job.parserMode)}</dd></div><div><dt>文档 ID</dt><dd>{job.documentId || '整库任务'}</dd></div><div><dt>索引版本</dt><dd>{job.revision || '未提供'}</dd></div><div><dt>错误代码</dt><dd>{job.errorCode || '无'}</dd></div></dl></Disclosure></div>;
}

function summarizeDocuments(documents: readonly KnowledgeDocument[]) {
  return documents.reduce((summary, item) => ({
    bytes: summary.bytes + item.byteSize,
    chunks: summary.chunks + item.chunkCount,
    ready: summary.ready + (item.status === 'ready' ? 1 : 0),
    processing: summary.processing + (['queued', 'parsing', 'indexing'].includes(item.status) ? 1 : 0),
    attention: summary.attention + (['failed', 'stale'].includes(item.status) ? 1 : 0),
  }), { bytes: 0, chunks: 0, ready: 0, processing: 0, attention: 0 });
}

function groupChunksByPage(detail: KnowledgeDocumentDetail) {
  const groups = new Map<string, { key: string; page: number | null; chunks: KnowledgeDocumentDetail['chunks'] }>();
  for (const chunk of detail.chunks) {
    const key = chunk.page === null ? 'none' : String(chunk.page);
    const group = groups.get(key) ?? { key, page: chunk.page, chunks: [] };
    group.chunks.push(chunk);
    groups.set(key, group);
  }
  return [...groups.values()].sort((left, right) => (left.page ?? Number.MAX_SAFE_INTEGER) - (right.page ?? Number.MAX_SAFE_INTEGER));
}

function asError(value: unknown): Error { return value instanceof Error ? value : new Error('读取文件失败。'); }

function documentStatusLabel(value: KnowledgeDocument['status']): string { return ({ queued: '等待处理', parsing: '解析中', indexing: '索引中', ready: '可检索', failed: '处理失败', stale: '需要重建' } as const)[value]; }
function documentTone(value: KnowledgeDocument['status']): 'success' | 'warning' | 'danger' | 'info' { return value === 'ready' ? 'success' : value === 'failed' ? 'danger' : value === 'stale' ? 'warning' : 'info'; }
function formatBytes(value: number): string { if (!value) return '0 B'; if (value < 1_024) return `${value} B`; if (value < 1_048_576) return `${(value / 1_024).toFixed(1)} KB`; return `${(value / 1_048_576).toFixed(1)} MB`; }
function formatTime(value: number): string { return value ? new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(value) : '暂无'; }
function fileFormatLabel(value: string): string {
  const format = value.trim().toLowerCase();
  if (!format) return '格式未报告';
  if (format === 'text/markdown') return 'Markdown 文档';
  if (format === 'application/pdf') return 'PDF 文档';
  if (format === 'text/plain') return '文本文档';
  if (format === 'application/json') return 'JSON 文件';
  if (format.startsWith('image/')) return '图片';
  if (format.includes('wordprocessingml')) return 'Word 文档';
  if (format.includes('spreadsheetml') || format.includes('excel')) return '表格文件';
  if (format.includes('presentationml') || format.includes('powerpoint')) return '演示文稿';
  return '其他文件';
}
function shortHash(value: string): string { return value ? `${value.slice(0, 8)}…${value.slice(-6)}` : '未记录'; }
function jobStageLabel(value: string): string { return (({ queued: '等待开始', parsing: '解析材料', embedding: '生成向量', indexing: '写入索引', ready: '完成' } as Record<string, string>)[value] ?? value) || '处理中'; }
function jobStatusLabel(value: string): string { return ({ queued: '等待中', running: '进行中', parsing: '进行中', embedding: '进行中', indexing: '进行中', ready: '已完成', succeeded: '已完成', success: '已完成', completed: '已完成', failed: '失败', cancelled: '已取消', canceled: '已取消' } as Record<string, string>)[value.toLowerCase()] ?? value; }
function jobTone(value: string): 'success' | 'warning' | 'danger' | 'info' { const status = value.toLowerCase(); return ['ready', 'succeeded', 'success', 'completed'].includes(status) ? 'success' : status === 'failed' ? 'danger' : ['cancelled', 'canceled'].includes(status) ? 'warning' : 'info'; }
function terminalJobStatus(value: string): boolean { return ['ready', 'succeeded', 'success', 'completed', 'failed', 'cancelled', 'canceled'].includes(value.toLowerCase()); }
function formatDuration(value: number): string { if (value < 1_000) return `${Math.max(0, value)} ms`; const seconds = Math.round(value / 1_000); return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`; }
function parserLabel(value: string): string { return value === 'mineru' ? 'MinerU OCR' : value === 'builtin' ? '内置解析' : '自动选择'; }
function uploadStatusLabel(value: KnowledgeUploadItem['status']): string { return ({ queued: '等待上传', uploading: '上传中', accepted: '已进入解析', failed: '上传失败' } as const)[value]; }
function uploadTone(value: KnowledgeUploadItem['status']): 'success' | 'danger' | 'info' { return value === 'accepted' ? 'success' : value === 'failed' ? 'danger' : 'info'; }
function safeMarkdownLink(value: string | undefined): string | null { return value && /^(?:https?:|#)/iu.test(value) ? value : null; }
