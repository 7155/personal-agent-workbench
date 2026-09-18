import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useControlTransport } from '@/app/control-transport';
import { transientControlErrorRefetchInterval } from '@/app/query-client';
import type {
  ControlTransport,
  JsonValue,
  KnowledgeDocumentImportInput,
  KnowledgeDocumentImportReceipt,
} from '@/platform/transport';

export type KnowledgeParserMode = 'auto' | 'builtin' | 'mineru';
export type KnowledgeDocumentStatus = 'queued' | 'parsing' | 'indexing' | 'ready' | 'failed' | 'stale';

export interface DocumentKnowledgeBase {
  id: string;
  name: string;
  description: string;
  documentCount: number;
  chunkCount: number;
  status: string;
  agentEnabled: boolean;
  parser: KnowledgeParserMode;
  updatedAtMs: number;
  revision: JsonValue;
  chunkingConfig: KnowledgeChunkingConfig;
  retrievalConfig: KnowledgeRetrievalConfig;
}

export interface KnowledgeChunkingConfig {
  strategy: 'general' | 'markdown' | 'book' | 'qa' | 'laws' | 'separator' | 'fixed';
  size: number;
  overlap: number;
  separator: string;
  respectHeadings: boolean;
  respectPageBoundaries: boolean;
}

export interface KnowledgeRetrievalConfig {
  mode: 'hybrid' | 'dense' | 'lexical';
  topK: number;
  threshold: number;
  lexicalWeight: number;
  denseWeight: number;
  graphEnabled: boolean;
  graphWeight: number;
  rrfK: number;
  candidateMultiplier: number;
  rerankEnabled: boolean;
  rerankCandidateDepth: number;
}

export interface KnowledgeDocument {
  id: string;
  baseId: string;
  name: string;
  mimeType: string;
  byteSize: number;
  status: KnowledgeDocumentStatus;
  stage: string;
  progress: number;
  error: string;
  chunkCount: number;
  parser: string;
  updatedAtMs: number;
  revision: JsonValue;
  sha256: string;
  pageCount: number;
  tokenCount: number;
  parserVersion: string;
  sourceReadPath: string;
  indexedConfigRevision: number;
}

export interface KnowledgeChunk {
  id: string;
  ordinal: number;
  content: string;
  page: number | null;
  heading: string;
  lineStart: number | null;
  lineEnd: number | null;
  tokenCount: number;
}

export interface KnowledgePageSummary {
  page: number;
  chunkCount: number;
}

export interface KnowledgeAsset {
  id: string;
  name: string;
  mimeType: string;
  byteSize: number;
  sha256: string;
  readPath: string;
  page: number | null;
  caption: string;
}

export interface KnowledgeTableArtifact {
  id: string;
  title: string;
  page: number | null;
  columns: string[];
  rows: string[][];
  markdown: string;
}

export interface KnowledgeDocumentDetail {
  document: KnowledgeDocument;
  chunks: KnowledgeChunk[];
  chunkTotal: number;
  chunkHasMore: boolean;
  pages: KnowledgePageSummary[];
  assets: KnowledgeAsset[];
  tables: KnowledgeTableArtifact[];
  artifact: {
    available: boolean;
    format: string;
    mimeType: string;
    byteSize: number;
    lineCount: number;
    sha256: string;
  };
  contentWindow: { lineNumber: number; content: string }[];
  contentLineTotal: number;
  contentHasMore: boolean;
}

export interface KnowledgeIndexJob {
  id: string;
  baseId: string;
  documentId: string;
  documentName: string;
  kind: string;
  status: string;
  stage: string;
  progress: number;
  error: string;
  createdAtMs: number;
  startedAtMs: number;
  finishedAtMs: number;
  updatedAtMs: number;
  revision: number;
  errorCode: string;
  parserMode: KnowledgeParserMode;
  cancellable: boolean;
}

export interface KnowledgeChunkPreview {
  documentId: string;
  total: number;
  truncated: boolean;
  chunks: KnowledgeChunk[];
}

export interface KnowledgeIndexRuntimeStatus {
  available: boolean;
  degraded: boolean;
  kind: string;
  fingerprint: string;
  provider: string;
  model: string;
  dimensions: number | null;
  vectorCount: number | null;
  reason: string;
}

export type KnowledgeEmbeddingProvider =
  | 'environment'
  | 'none'
  | 'local-hash'
  | 'sentence-transformers'
  | 'mlx-bert'
  | 'openai-compatible';

export type KnowledgeDenseBackend = 'sqlite-exact' | 'usearch';

export interface KnowledgeEmbeddingProfile {
  source: 'environment' | 'settings';
  provider: KnowledgeEmbeddingProvider;
  model: string;
  baseUrl: string;
  dimensions: number;
  secretReference: string;
  queryPrefix: string;
  documentPrefix: string;
  denseBackend: KnowledgeDenseBackend;
  secretAvailable: boolean;
  profileSha256: string;
  secretsVisible: false;
}

export type KnowledgeEmbeddingCandidate = Omit<
  KnowledgeEmbeddingProfile,
  'source' | 'secretAvailable' | 'profileSha256' | 'secretsVisible'
>;

export interface KnowledgeEmbeddingProfileState {
  profile: KnowledgeEmbeddingProfile;
  phase: 'active' | 'applied_pending_restart' | 'applied_pending_rebuild';
  runtime: {
    provider: Record<string, unknown>;
    fingerprint: string;
    dimensions: number | null;
    vectorCount: number;
    chunkCount: number;
    coverage: number;
    available: boolean;
    degraded: boolean;
    reason: string;
  };
}

export interface KnowledgeEmbeddingProbe {
  ready: true;
  profileSha256: string;
  provider: string;
  model: string;
  fingerprint: string;
  dimensions: number;
  semantic: boolean;
  latencyMs: number;
  secretsVisible: false;
}

export interface KnowledgeEmbeddingImpact {
  candidate: KnowledgeEmbeddingCandidate;
  probe: KnowledgeEmbeddingProbe;
  currentProfileSha256: string;
  configurationChanges: Record<string, JsonValue>;
  requiresWorkerRestart: boolean;
  requiresRebuild: boolean;
  affectedBases: Array<{ kbId: string; name: string; documentCount: number; chunkCount: number }>;
  affectedBaseCount: number;
  affectedDocumentCount: number;
  affectedChunkCount: number;
  approvalRequiredForApply: true;
  secretsVisible: false;
}

export interface KnowledgeReindexPreview {
  previewToken: string;
  payloadSha256: string;
  expectedRevision: JsonValue;
  documentCount: number;
  staleDocumentCount: number;
  estimatedChunkCount: number;
}

export interface KnowledgeSearchHit {
  id: string;
  documentId: string;
  documentName: string;
  title: string;
  excerpt: string;
  score: number | null;
  page: number | null;
  heading: string;
  lineStart: number | null;
  lineEnd: number | null;
  diagnostics: {
    effectiveMode: 'hybrid' | 'lexical' | 'dense' | 'unknown';
    lexicalRank: number | null;
    denseRank: number | null;
    graphRank: number | null;
    lexicalScore: number | null;
    denseScore: number | null;
    graphScore: number | null;
    retrievalScore: number | null;
    retrievalRank: number | null;
    rerankScore: number | null;
    rerankRank: number | null;
    rerankProvider: string;
    rerankFingerprint: string;
    independentRerankStage: boolean;
    subagentSubstitute: boolean;
    graphMatches: string[];
    graphPaths: string[];
  };
}

export interface KnowledgeSearchRetrievalLibrary {
  baseId: string;
  baseName: string;
  config: KnowledgeRetrievalConfig | null;
  effectiveMode: string;
  candidateLimit: number;
  lexicalCandidates: number;
  denseCandidates: number;
  graphCandidates: number;
  graphStatus: string;
  graphMatchedNodes: number;
  rerankApplied: boolean;
  rerankCandidates: number;
  returned: number;
}

export interface KnowledgeSearchRetrieval {
  mode: string;
  effectiveMode: string;
  config: KnowledgeRetrievalConfig | null;
  libraries: KnowledgeSearchRetrievalLibrary[];
  lexicalAvailable: boolean;
  dense: { available: boolean; degraded: boolean; provider: string; model: string };
  reranker: { provider: string; configured: boolean; fingerprint: string; independentStage: boolean; subagentSubstitute: boolean; fallbackCount: number; error: string };
}

export interface KnowledgeSearchResult {
  hits: KnowledgeSearchHit[];
  retrieval: KnowledgeSearchRetrieval | null;
}

export type KnowledgeGraphNodeKind = 'document' | 'chunk' | 'topic' | 'entity' | 'term' | 'unknown';

export interface KnowledgeGraphNode {
  id: string;
  label: string;
  kind: KnowledgeGraphNodeKind;
  documentId: string;
  documentName: string;
  chunkId: string;
  heading: string;
  excerpt: string;
  page: number | null;
  weight: number | null;
}

export interface KnowledgeGraphEdge {
  id: string;
  source: string;
  target: string;
  kind: string;
  label: string;
  weight: number | null;
}

export interface KnowledgeGraph {
  schemaVersion: string;
  baseId: string;
  revision: JsonValue;
  status: 'ready' | 'building' | 'stale' | 'failed';
  updatedAtMs: number;
  nodes: KnowledgeGraphNode[];
  edges: KnowledgeGraphEdge[];
  stats: { nodeCount: number; edgeCount: number; documentCount: number; chunkCount: number; indexedDocumentCount: number; pendingDocumentCount: number };
  sourceRevision: JsonValue;
  truncated: boolean;
  extractor: KnowledgeGraphExtractorStatus | null;
}

export type KnowledgeGraphExtractorMode = 'model' | 'deterministic';

export interface KnowledgeGraphExtractorStatus {
  mode: KnowledgeGraphExtractorMode;
  model: string;
  configured: boolean;
  degraded: boolean;
  processedChunkCount: number;
  cachedChunkCount: number;
  modelChunkCount: number;
  fallbackChunkCount: number;
  errorCount: number;
  batchSize: number;
  batchCount: number;
  extractionConcurrency: number;
  effectiveExtractionConcurrency: number;
  entityCount: number;
  topicCount: number;
  relationCount: number;
  lastError: string;
}

export interface KnowledgeGraphRebuildOptions {
  extractorMode?: KnowledgeGraphExtractorMode;
  modelId?: string;
  batchSize?: number;
  extractionConcurrency?: number;
  maxEntitiesPerChunk?: number;
  maxRelationsPerChunk?: number;
  maxTopicsPerChunk?: number;
}

export interface KnowledgeGraphFilters {
  documentId?: string;
  query?: string;
  kinds?: readonly KnowledgeGraphNodeKind[];
  limit?: number;
  depth?: number;
  excludeChunks?: boolean;
}

export const knowledgeLibraryKeys = {
  root: ['knowledge-library'] as const,
  bases: () => [...knowledgeLibraryKeys.root, 'bases'] as const,
  base: (baseId: string) => [...knowledgeLibraryKeys.root, 'base', baseId] as const,
  documents: (baseId: string) => [...knowledgeLibraryKeys.root, 'documents', baseId] as const,
  jobs: (baseId: string) => [...knowledgeLibraryKeys.root, 'jobs', baseId] as const,
  documentDetail: (baseId: string, documentId: string) => [...knowledgeLibraryKeys.root, 'document-detail', baseId, documentId] as const,
  documentContent: (baseId: string, documentId: string) => [...knowledgeLibraryKeys.root, 'document-content', baseId, documentId] as const,
  worker: () => [...knowledgeLibraryKeys.root, 'worker'] as const,
  parsers: () => [...knowledgeLibraryKeys.root, 'parsers'] as const,
  embeddingProfile: () => [...knowledgeLibraryKeys.root, 'embedding-profile'] as const,
  settings: () => [...knowledgeLibraryKeys.root, 'settings'] as const,
  graph: (baseId: string, filters: KnowledgeGraphFilters = {}) => [
    ...knowledgeLibraryKeys.root,
    'graph',
    baseId,
    filters.documentId ?? '',
    filters.query ?? '',
    filters.limit ?? 100,
    filters.depth ?? 2,
    filters.excludeChunks ?? true,
    ...(filters.kinds ?? []),
  ] as const,
};

export function useKnowledgeLibraryQueries(baseId: string, enabled = true) {
  const transport = useControlTransport();
  const bases = useQuery({
    queryKey: knowledgeLibraryKeys.bases(),
    enabled,
    queryFn: async ({ signal }) => normalizeBases(await transport.request({
      pathId: 'knowledgeBases.list',
      signal,
    })),
    refetchInterval: transientControlErrorRefetchInterval(enabled),
    staleTime: 10_000,
  });
  const base = useQuery({
    queryKey: knowledgeLibraryKeys.base(baseId),
    enabled: enabled && Boolean(baseId),
    queryFn: async ({ signal }) => normalizeBaseEnvelope(await transport.request({
      pathId: 'knowledgeBases.get',
      params: { kbId: baseId },
      signal,
    })),
    refetchInterval: transientControlErrorRefetchInterval(enabled && Boolean(baseId)),
    staleTime: 10_000,
  });
  const documents = useQuery({
    queryKey: knowledgeLibraryKeys.documents(baseId),
    enabled: enabled && Boolean(baseId),
    queryFn: async ({ signal }) => normalizeDocuments(await transport.request({
      pathId: 'knowledgeBases.documents.list',
      params: { kbId: baseId },
      signal,
    }), baseId),
    refetchInterval: (query) => {
      if (query.state.status === 'error') return enabled ? 4_000 : false;
      const rows = (query.state.data ?? []) as KnowledgeDocument[];
      return rows.some((row) => ['queued', 'parsing', 'indexing'].includes(row.status)) ? 1_500 : false;
    },
  });
  const jobs = useQuery({
    queryKey: knowledgeLibraryKeys.jobs(baseId),
    enabled: enabled && Boolean(baseId),
    queryFn: async ({ signal }) => normalizeJobs(await transport.request({
      pathId: 'knowledgeBases.jobs.list',
      params: { kbId: baseId },
      signal,
    }), baseId),
    staleTime: 2_000,
    refetchInterval: (query) => {
      const rows = (query.state.data ?? []) as KnowledgeIndexJob[];
      return rows.some((row) => isActiveJobStatus(row.status)) ? 1_500 : 15_000;
    },
  });
  const worker = useQuery({
    queryKey: knowledgeLibraryKeys.worker(),
    enabled,
    queryFn: ({ signal }) => transport.request({ pathId: 'knowledgeWorker.health', signal }),
    staleTime: 15_000,
    refetchInterval: 30_000,
  });
  const parsers = useQuery({
    queryKey: knowledgeLibraryKeys.parsers(),
    enabled,
    queryFn: ({ signal }) => transport.request({ pathId: 'knowledgeParsers.list', signal }),
    staleTime: 30_000,
  });
  const embeddingProfile = useQuery({
    queryKey: knowledgeLibraryKeys.embeddingProfile(),
    enabled,
    queryFn: async ({ signal }) => normalizeEmbeddingProfileState(await transport.request({
      pathId: 'knowledgeEmbedding.profile',
      signal,
    })),
    staleTime: 10_000,
  });
  const settings = useQuery({
    queryKey: knowledgeLibraryKeys.settings(),
    enabled,
    queryFn: ({ signal }) => transport.request({ pathId: 'configuration.settings', signal }),
    staleTime: 10_000,
  });
  return { base, bases, documents, embeddingProfile, jobs, parsers, settings, transport, worker };
}

export async function probeKnowledgeEmbedding(
  transport: ControlTransport,
  profile: KnowledgeEmbeddingCandidate,
): Promise<KnowledgeEmbeddingProbe> {
  return normalizeEmbeddingProbe(await transport.request({
    pathId: 'knowledgeEmbedding.probe',
    body: { profile: jsonRecord({ ...profile }) },
  }));
}

export async function previewKnowledgeEmbeddingImpact(
  transport: ControlTransport,
  profile: KnowledgeEmbeddingCandidate,
): Promise<KnowledgeEmbeddingImpact> {
  return normalizeEmbeddingImpact(await transport.request({
    pathId: 'knowledgeEmbedding.impact',
    body: { profile: jsonRecord({ ...profile }) },
  }));
}

export function useKnowledgeGraphQuery(baseId: string, filters: KnowledgeGraphFilters, enabled = true) {
  const transport = useControlTransport();
  return useQuery({
    queryKey: knowledgeLibraryKeys.graph(baseId, filters),
    enabled: enabled && Boolean(baseId),
    queryFn: async ({ signal }) => normalizeKnowledgeGraph(await transport.request({
      pathId: 'knowledgeBases.graph.get',
      params: { kbId: baseId },
      query: {
        limit: filters.limit ?? 100,
        depth: filters.depth ?? 2,
        excludeChunks: filters.excludeChunks ?? true,
        ...(filters.documentId ? { documentId: filters.documentId } : {}),
        ...(filters.query ? { query: filters.query } : {}),
        ...(filters.kinds?.length ? { kinds: filters.kinds.join(',') } : {}),
      },
      signal,
    }), baseId),
    staleTime: 10_000,
    refetchInterval: (query) => query.state.data?.status === 'building' ? 1_500 : false,
  });
}

export async function rebuildKnowledgeGraph(
  transport: ControlTransport,
  baseId: string,
  expectedRevision: JsonValue,
  options: KnowledgeGraphRebuildOptions = {},
): Promise<{ jobId: string; status: string }> {
  const extractorMode = options.extractorMode ?? 'model';
  const payload = record(await transport.request({
    pathId: 'knowledgeBases.graph.rebuild',
    params: { kbId: baseId },
    body: {
      expectedRevision,
      extractorMode,
      ...(options.modelId ? { modelId: options.modelId } : {}),
      batchSize: options.batchSize ?? 4,
      extractionConcurrency: options.extractionConcurrency ?? 2,
      maxEntitiesPerChunk: options.maxEntitiesPerChunk ?? 5,
      maxRelationsPerChunk: options.maxRelationsPerChunk ?? 4,
      maxTopicsPerChunk: options.maxTopicsPerChunk ?? 2,
    },
  }));
  return { jobId: text(payload.jobId), status: text(payload.status, 'queued') };
}

export function useKnowledgeDocumentDetail(baseId: string, documentId: string, enabled = true) {
  const transport = useControlTransport();
  const chunksQuery = useInfiniteQuery({
    queryKey: knowledgeLibraryKeys.documentDetail(baseId, documentId),
    enabled: enabled && Boolean(baseId && documentId),
    initialPageParam: 0,
    queryFn: async ({ pageParam, signal }) => normalizeDocumentDetail(await transport.request({
      pathId: 'knowledgeBases.document.get',
      params: { kbId: baseId, fileId: documentId },
      query: { offset: pageParam, limit: 200, lineOffset: 0, lineLimit: 1 },
      signal,
    }), baseId, documentId),
    getNextPageParam: (lastPage, pages) => lastPage.chunkHasMore
      ? pages.reduce((count, page) => count + page.chunks.length, 0)
      : undefined,
    staleTime: 10_000,
  });
  const contentQuery = useInfiniteQuery({
    queryKey: knowledgeLibraryKeys.documentContent(baseId, documentId),
    enabled: enabled && Boolean(baseId && documentId),
    initialPageParam: 0,
    queryFn: async ({ pageParam, signal }) => normalizeDocumentDetail(await transport.request({
      pathId: 'knowledgeBases.document.get',
      params: { kbId: baseId, fileId: documentId },
      query: { offset: 0, limit: 1, lineOffset: pageParam, lineLimit: 200 },
      signal,
    }), baseId, documentId),
    getNextPageParam: (lastPage, pages) => lastPage.contentHasMore
      ? pages.reduce((count, page) => count + page.contentWindow.length, 0)
      : undefined,
    staleTime: 10_000,
  });
  const chunkDetail = chunksQuery.data ? mergeDocumentDetailPages(chunksQuery.data.pages) : undefined;
  return {
    ...chunksQuery,
    data: chunkDetail && contentQuery.data ? mergeDocumentContentPages(chunkDetail, contentQuery.data.pages) : chunkDetail,
    error: chunksQuery.error ?? contentQuery.error,
    isPending: chunksQuery.isPending || contentQuery.isPending,
    isFetching: chunksQuery.isFetching || contentQuery.isFetching,
    // The reader owns two independent windows. Retry both so a正文 failure
    // cannot remain stuck behind a successful paragraphs-only refresh.
    refetch: () => Promise.all([chunksQuery.refetch(), contentQuery.refetch()]),
    fetchNextContentPage: contentQuery.fetchNextPage,
    hasNextContentPage: contentQuery.hasNextPage,
    isFetchingNextContentPage: contentQuery.isFetchingNextPage,
  };
}

export async function createKnowledgeBase(
  transport: ControlTransport,
  input: { name: string; description: string },
): Promise<DocumentKnowledgeBase> {
  return normalizeBaseEnvelope(await transport.request({
    pathId: 'knowledgeBases.create',
    body: { ...input, agentEnabled: false, parserProvider: 'auto' },
  }));
}

export async function updateKnowledgeBase(
  transport: ControlTransport,
  base: DocumentKnowledgeBase,
  patch: {
    name?: string;
    description?: string;
    agentEnabled?: boolean;
    parser?: KnowledgeParserMode;
    chunkingConfig?: KnowledgeChunkingConfig;
    retrievalConfig?: KnowledgeRetrievalConfig;
  },
): Promise<DocumentKnowledgeBase> {
  return normalizeBaseEnvelope(await transport.request({
    pathId: 'knowledgeBases.update',
    params: { kbId: base.id },
    body: jsonRecord({
      ...patch,
      ...(patch.parser ? { parserProvider: parserProvider(patch.parser), parser: undefined } : {}),
      expectedRevision: base.revision,
    }),
  }));
}

export async function deleteKnowledgeBase(transport: ControlTransport, base: DocumentKnowledgeBase): Promise<void> {
  const preview = record(await transport.request({
    pathId: 'knowledgeBases.delete.preview',
    params: { kbId: base.id },
    body: { expectedRevision: base.revision },
  }));
  await transport.request({
    pathId: 'knowledgeBases.delete.apply',
    params: { kbId: base.id },
    body: jsonRecord({
      confirmText: 'delete',
      previewToken: text(preview.previewToken),
      payloadSha256: text(preview.payloadSha256),
      expectedRevision: preview.expectedRevision ?? base.revision,
    }),
  });
}

export async function importKnowledgeDocuments(
  transport: ControlTransport,
  options: KnowledgeDocumentImportInput,
): Promise<KnowledgeDocumentImportReceipt[]> {
  const importer = transport.importKnowledgeDocuments;
  if (!importer) throw new Error('当前控制通道不支持文档导入。');
  return importer.call(transport, options);
}

export async function retryKnowledgeDocument(
  transport: ControlTransport,
  base: DocumentKnowledgeBase,
  document: KnowledgeDocument,
  options: { parser?: KnowledgeParserMode; stage?: 'parse' | 'index' } = {},
): Promise<void> {
  await transport.request({
    pathId: 'knowledgeBases.document.retry',
    params: { kbId: base.id, fileId: document.id },
    body: {
      stage: options.stage ?? 'parse',
      ...(options.parser ? { parserProvider: parserProvider(options.parser) } : {}),
      expectedRevision: document.revision,
    },
  });
}

export async function previewKnowledgeReindex(
  transport: ControlTransport,
  base: DocumentKnowledgeBase,
): Promise<KnowledgeReindexPreview> {
  const payload = record(await transport.request({
    pathId: 'knowledgeBases.reindexPreview',
    params: { kbId: base.id },
  }));
  const summary = record(payload.summary);
  return {
    previewToken: text(payload.previewToken),
    payloadSha256: text(payload.payloadSha256),
    expectedRevision: jsonValue(payload.expectedRevision ?? base.revision),
    documentCount: number(summary.documentCount ?? payload.documentCount),
    staleDocumentCount: number(summary.staleDocumentCount ?? payload.staleDocumentCount),
    estimatedChunkCount: number(summary.estimatedChunkCount ?? payload.estimatedChunkCount),
  };
}

export async function rebuildKnowledgeBase(
  transport: ControlTransport,
  base: DocumentKnowledgeBase,
  preview: KnowledgeReindexPreview,
): Promise<void> {
  await transport.request({
    pathId: 'knowledgeBases.rebuild',
    params: { kbId: base.id },
    body: {
      previewToken: preview.previewToken,
      payloadSha256: preview.payloadSha256,
      expectedRevision: preview.expectedRevision,
      confirmText: 'REBUILD',
    },
  });
}

export async function cancelKnowledgeJob(
  transport: ControlTransport,
  baseId: string,
  jobId: string,
): Promise<void> {
  await transport.request({
    pathId: 'knowledgeBases.job.cancel',
    params: { kbId: baseId, jobId },
    body: {},
  });
}

export async function previewKnowledgeChunking(
  transport: ControlTransport,
  baseId: string,
  documentId: string,
  chunkingConfig: KnowledgeChunkingConfig,
): Promise<KnowledgeChunkPreview> {
  const payload = record(await transport.request({
    pathId: 'knowledgeBases.chunkPreview',
    params: { kbId: baseId, fileId: documentId },
    body: { chunkingConfig: jsonRecord({ ...chunkingConfig }), limit: 12 },
  }));
  const chunks = list(payload.items).map((item, index) => {
    const row = record(item);
    return {
      id: text(row.chunkId, text(row.id, `preview-${index + 1}`)),
      ordinal: number(row.ordinal ?? index),
      content: text(row.content),
      page: nullableNumber(row.page),
      heading: text(row.heading),
      lineStart: null,
      lineEnd: null,
      tokenCount: number(row.tokenCount),
    } satisfies KnowledgeChunk;
  });
  return {
    documentId,
    total: number(payload.total),
    truncated: bool(payload.truncated),
    chunks,
  };
}

export async function deleteKnowledgeDocument(
  transport: ControlTransport,
  baseId: string,
  documentId: string,
): Promise<void> {
  await transport.request({
    pathId: 'knowledgeBases.document.delete',
    params: { kbId: baseId, fileId: documentId },
  });
}

export async function searchKnowledgeBase(
  transport: ControlTransport,
  baseId: string,
  query: string,
  config: KnowledgeRetrievalConfig,
  signal?: AbortSignal,
): Promise<KnowledgeSearchResult> {
  const payload = await transport.request({
    pathId: 'knowledgeBases.search',
    params: { kbId: baseId },
    body: {
      query,
      topK: config.topK,
      mode: config.mode,
      threshold: config.threshold,
      rerank: config.rerankEnabled,
      rerankCandidateDepth: config.rerankCandidateDepth,
    },
    ...(signal ? { signal } : {}),
  });
  const root = record(payload);
  return { hits: normalizeSearchHits(payload), retrieval: normalizeSearchRetrieval(root.retrieval) };
}

export async function openKnowledgeHit(
  transport: ControlTransport,
  baseId: string,
  hit: KnowledgeSearchHit,
): Promise<void> {
  await transport.request({
    pathId: 'knowledgeBases.open',
    params: { kbId: baseId, fileId: hit.documentId },
    query: {
      chunkId: hit.id,
      ...(hit.page !== null ? { page: hit.page } : {}),
      ...(hit.lineStart !== null ? { startLine: hit.lineStart } : {}),
      lines: 80,
    },
  });
}

export function chooseKnowledgeFiles(maxFiles = 20): Promise<File[]> {
  return new Promise((resolve) => {
    const input = document.createElement('input');
    input.type = 'file';
    input.multiple = true;
    input.accept = '.pdf,.docx,.pptx,.xlsx,.txt,.md,.html,.htm,.png,.jpg,.jpeg,.webp';
    input.addEventListener('change', () => resolve([...(input.files ?? [])].slice(0, maxFiles)), { once: true });
    input.addEventListener('cancel', () => resolve([]), { once: true });
    input.click();
  });
}

function normalizeBases(value: unknown): DocumentKnowledgeBase[] {
  const payload = record(value);
  const rows = Array.isArray(value) ? value : list(payload.items ?? payload.bases ?? payload.knowledgeBases);
  return rows.map(normalizeBase).filter((row) => Boolean(row.id));
}

export function normalizeKnowledgeGraph(value: unknown, baseId = ''): KnowledgeGraph {
  const root = record(value);
  const payload = record(root.graph ?? value);
  const nodes = list(payload.nodes).map((item, index) => {
    const row = record(item);
    const rawKind = text(row.kind, text(row.type, 'unknown')).toLowerCase();
    const kind: KnowledgeGraphNodeKind = rawKind === 'file' || rawKind === 'document'
      ? 'document'
      : rawKind === 'concept' || rawKind === 'topic'
        ? 'topic'
        : ['chunk', 'entity', 'term'].includes(rawKind) ? rawKind as KnowledgeGraphNodeKind : 'unknown';
    return {
      id: text(row.id, `node-${index + 1}`),
      label: text(row.label, text(row.name, text(row.heading, '未命名节点'))),
      kind,
      documentId: text(row.documentId, text(row.document_id, text(row.fileId, text(row.file_id)))),
      documentName: text(row.documentName, text(row.document_name, text(row.fileName))),
      chunkId: text(row.chunkId, text(row.chunk_id)),
      heading: text(row.heading),
      excerpt: text(row.excerpt, text(row.summary, text(row.content, text(row.text)))),
      page: nullableNumber(row.page),
      weight: optionalNumber(row.weight ?? row.score),
    } satisfies KnowledgeGraphNode;
  }).filter((node) => Boolean(node.id));
  const nodeIds = new Set(nodes.map((node) => node.id));
  const edges = list(payload.edges).map((item, index) => {
    const row = record(item);
    const source = text(row.source, text(row.sourceId, text(row.source_id)));
    const target = text(row.target, text(row.targetId, text(row.target_id)));
    return {
      id: text(row.id, `${source}:${target}:${index}`), source, target,
      kind: text(row.kind, text(row.relation, text(row.type, 'related'))),
      label: text(row.label), weight: optionalNumber(row.weight ?? row.score),
    } satisfies KnowledgeGraphEdge;
  }).filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target));
  const rawStats = record(payload.stats);
  const rawStatus = record(payload.status);
  const rawExtractor = record(payload.extractor);
  const statusValue = typeof payload.status === 'string' ? payload.status : text(rawStatus.state, 'ready');
  return {
    schemaVersion: text(payload.schemaVersion, text(payload.schema_version, 'rag-ime.knowledge-graph.v1')),
    baseId: text(payload.kbId, text(payload.baseId, baseId)),
    revision: jsonValue(payload.revision ?? 0),
    status: graphStatus(statusValue),
    updatedAtMs: number(payload.updatedAtMs ?? payload.updated_at_ms ?? rawStatus.updatedAtMs ?? rawStatus.updated_at_ms),
    nodes,
    edges,
    stats: {
      nodeCount: number(rawStats.nodeCount ?? rawStats.node_count ?? rawStatus.nodeCount ?? rawStatus.node_count) || nodes.length,
      edgeCount: number(rawStats.edgeCount ?? rawStats.edge_count ?? rawStatus.edgeCount ?? rawStatus.edge_count) || edges.length,
      documentCount: number(rawStats.documentCount ?? rawStats.document_count) || nodes.filter((node) => node.kind === 'document').length,
      chunkCount: number(rawStats.chunkCount ?? rawStats.chunk_count) || nodes.filter((node) => node.kind === 'chunk').length,
      indexedDocumentCount: number(rawStats.indexedDocumentCount ?? rawStats.indexed_document_count ?? rawStatus.indexedDocumentCount ?? rawStatus.indexed_document_count),
      pendingDocumentCount: number(rawStats.pendingDocumentCount ?? rawStats.pending_document_count ?? rawStatus.pendingDocumentCount ?? rawStatus.pending_document_count),
    },
    sourceRevision: jsonValue(rawStatus.sourceRevision ?? rawStatus.source_revision ?? payload.sourceRevision ?? payload.source_revision ?? 0),
    truncated: bool(payload.truncated),
    extractor: Object.keys(rawExtractor).length ? {
      mode: text(rawExtractor.mode) === 'model' ? 'model' : 'deterministic',
      model: text(rawExtractor.model),
      configured: bool(rawExtractor.configured),
      degraded: bool(rawExtractor.degraded),
      processedChunkCount: number(rawExtractor.processedChunkCount),
      cachedChunkCount: number(rawExtractor.cachedChunkCount),
      modelChunkCount: number(rawExtractor.modelChunkCount),
      fallbackChunkCount: number(rawExtractor.fallbackChunkCount),
      errorCount: number(rawExtractor.errorCount),
      batchSize: number(rawExtractor.batchSize),
      batchCount: number(rawExtractor.batchCount),
      extractionConcurrency: number(rawExtractor.extractionConcurrency),
      effectiveExtractionConcurrency: number(rawExtractor.effectiveExtractionConcurrency),
      entityCount: number(rawExtractor.entityCount),
      topicCount: number(rawExtractor.topicCount),
      relationCount: number(rawExtractor.relationCount),
      lastError: text(rawExtractor.lastError),
    } : null,
  };
}

function normalizeBaseEnvelope(value: unknown): DocumentKnowledgeBase {
  const payload = record(value);
  return normalizeBase(record(payload.base ?? payload.knowledgeBase ?? value));
}

function normalizeBase(value: unknown): DocumentKnowledgeBase {
  const row = record(value);
  const parser = text(row.parser, text(row.parserMode, text(record(row.settings).parser, 'auto')));
  return {
    id: text(row.id, text(row.baseId)),
    name: text(row.name, '未命名知识库'),
    description: text(row.description),
    documentCount: number(row.documentCount ?? row.documentsCount),
    chunkCount: number(row.chunkCount),
    status: text(row.status, 'ready'),
    agentEnabled: bool(row.agentEnabled ?? row.agent_enabled),
    parser: parserMode(parser),
    updatedAtMs: number(row.updatedAtMs ?? row.updated_at_ms),
    revision: jsonValue(row.revision ?? row.updatedAtMs ?? row.updated_at_ms ?? 0),
    chunkingConfig: normalizeChunkingConfig(row.chunkingConfig ?? row.chunking_config),
    retrievalConfig: normalizeRetrievalConfig(row.retrievalConfig ?? row.retrieval_config),
  };
}

function normalizeDocuments(value: unknown, baseId: string): KnowledgeDocument[] {
  const payload = record(value);
  return list(payload.items ?? payload.documents ?? value).map((item) => {
    const row = record(item);
    const rawStatus = text(row.status, 'queued');
    return {
      id: text(row.id, text(row.documentId)),
      baseId: text(row.baseId, baseId),
      name: text(row.name, text(row.fileName, '未命名文档')),
      mimeType: text(row.mimeType),
      byteSize: number(row.byteSize ?? row.size),
      status: documentStatus(rawStatus),
      stage: text(row.stage, rawStatus),
      progress: Math.max(0, Math.min(1, number(row.progress))),
      error: text(row.error, text(row.errorMessage)),
      chunkCount: number(row.chunkCount),
      parser: text(row.parser, text(row.parserProvider)),
      updatedAtMs: number(row.updatedAtMs ?? row.updated_at_ms),
      revision: jsonValue(row.revision ?? row.updatedAtMs ?? row.updated_at_ms ?? 0),
      sha256: text(row.sha256),
      pageCount: number(row.pageCount ?? row.pages),
      tokenCount: number(row.tokenCount ?? row.tokens),
      parserVersion: text(row.parserVersion ?? row.parser_version),
      sourceReadPath: safeSourcePath(text(row.sourceReadPath)),
      indexedConfigRevision: number(row.indexedConfigRevision ?? row.indexed_config_revision),
    };
  }).filter((row) => Boolean(row.id));
}

function normalizeDocumentDetail(value: unknown, baseId: string, documentId: string): KnowledgeDocumentDetail {
  const payload = record(value);
  const envelope = record(payload.detail ?? value);
  const documentPayload = record(envelope.document);
  const fallbackDocument = normalizeDocuments({ items: [{
    ...documentPayload,
    id: documentPayload.id ?? documentPayload.documentId ?? documentId,
    baseId: documentPayload.baseId ?? baseId,
    name: documentPayload.name ?? documentPayload.fileName ?? envelope.documentName,
    status: documentPayload.status ?? 'ready',
  }] }, baseId)[0] ?? emptyDocument(baseId, documentId);
  const chunksPayload = record(envelope.chunks);
  const chunkRows = Array.isArray(envelope.chunks) ? envelope.chunks : list(chunksPayload.items);
  const chunks = chunkRows.map((item, index) => {
    const row = record(item);
    return {
      id: text(row.chunkId, text(row.id, `chunk-${index + 1}`)),
      ordinal: number(row.ordinal ?? row.chunkOrder ?? row.chunk_order_index ?? index),
      content: text(row.content, text(row.text)),
      page: nullableNumber(row.page),
      heading: text(row.heading),
      lineStart: nullableNumber(row.lineStart ?? row.startLine),
      lineEnd: nullableNumber(row.lineEnd ?? row.endLine),
      tokenCount: number(row.tokenCount ?? row.tokens),
    } satisfies KnowledgeChunk;
  });
  return {
    document: fallbackDocument,
    chunks,
    chunkTotal: number(chunksPayload.total) || chunks.length,
    chunkHasMore: bool(chunksPayload.hasMore),
    pages: list(envelope.pages).map((item) => {
      const row = record(item);
      return { page: number(row.page), chunkCount: number(row.chunkCount) };
    }).filter((item) => item.page > 0),
    assets: list(envelope.assets).map((item, index) => {
      const row = record(item);
      return {
        id: text(row.assetId, text(row.id, `asset-${index + 1}`)),
        name: text(row.name, '解析产物'),
        mimeType: text(row.mimeType),
        byteSize: number(row.byteSize),
        sha256: text(row.sha256),
        readPath: safeAssetPath(text(row.readPath)),
        page: nullableNumber(row.page),
        caption: text(row.caption),
      } satisfies KnowledgeAsset;
    }),
    tables: list(envelope.tables).map((item, index) => {
      const row = record(item);
      return {
        id: text(row.tableId, text(row.id, `table-${index + 1}`)),
        title: text(row.title, `表格 ${index + 1}`),
        page: nullableNumber(row.page),
        columns: stringList(row.columns, 64),
        rows: list(row.rows).map((cells) => stringList(cells, 64)).slice(0, 500),
        markdown: text(row.markdown),
      } satisfies KnowledgeTableArtifact;
    }),
    artifact: normalizeArtifact(envelope.artifact),
    contentWindow: list(record(envelope.contentWindow).items ?? envelope.contentWindow).map((item, index) => {
      const row = record(item);
      return { lineNumber: number(row.lineNumber) || index + 1, content: text(row.content) };
    }),
    contentLineTotal: number(record(envelope.contentWindow).total) || number(record(envelope.artifact).lineCount),
    contentHasMore: bool(record(envelope.contentWindow).hasMore),
  };
}

function normalizeJobs(value: unknown, baseId: string): KnowledgeIndexJob[] {
  const payload = record(value);
  return list(payload.items ?? payload.jobs ?? value).map((item, index) => {
    const row = record(item);
    const nestedError = record(row.error);
    return {
      id: text(row.id, text(row.jobId, `job-${index + 1}`)),
      baseId: text(row.baseId, baseId),
      documentId: text(row.documentId, text(row.fileId)),
      documentName: text(row.documentName, text(row.fileName)),
      kind: text(row.kind, text(row.type, 'index')),
      status: text(row.status, 'queued'),
      stage: text(row.stage, text(row.status, 'queued')),
      progress: Math.max(0, Math.min(1, number(row.progress))),
      error: text(row.errorMessage, text(nestedError.message)),
      createdAtMs: number(row.createdAtMs ?? row.created_at_ms),
      startedAtMs: number(row.startedAtMs ?? row.started_at_ms),
      finishedAtMs: number(row.finishedAtMs ?? row.finished_at_ms),
      updatedAtMs: number(row.updatedAtMs ?? row.updated_at_ms),
      revision: number(row.revision),
      errorCode: text(row.errorCode, text(nestedError.code)),
      parserMode: parserMode(text(row.parserMode, 'auto')),
      cancellable: bool(row.cancellable) || isActiveJobStatus(text(row.status)),
    };
  });
}

function normalizeChunkingConfig(value: unknown): KnowledgeChunkingConfig {
  const row = record(value);
  const rawStrategy = text(row.strategy, 'markdown');
  const strategy = rawStrategy === 'paragraph' ? 'general' : rawStrategy;
  return {
    strategy: ['general', 'markdown', 'book', 'qa', 'laws', 'separator', 'fixed'].includes(strategy)
      ? strategy as KnowledgeChunkingConfig['strategy']
      : 'markdown',
    size: boundedNumber(row.size, 200, 8_000, 1_200),
    overlap: boundedNumber(row.overlap, 0, 2_000, 160),
    separator: text(row.separator, '\n\n'),
    respectHeadings: row.respectHeadings !== false,
    respectPageBoundaries: row.respectPageBoundaries !== false,
  };
}

function normalizeRetrievalConfig(value: unknown): KnowledgeRetrievalConfig {
  const row = record(value);
  const mode = text(row.mode, 'hybrid');
  return {
    mode: mode === 'dense' || mode === 'lexical' ? mode : 'hybrid',
    topK: boundedNumber(row.topK ?? row.top_k, 1, 100, 10),
    threshold: boundedNumber(row.threshold, 0, 1, 0),
    lexicalWeight: boundedNumber(row.lexicalWeight ?? row.lexical_weight, 0, 10, 1),
    denseWeight: boundedNumber(row.denseWeight ?? row.dense_weight, 0, 10, 1),
    graphEnabled: (row.graphEnabled ?? row.graph_enabled) !== false,
    graphWeight: boundedNumber(row.graphWeight ?? row.graph_weight, 0, 10, .7),
    rrfK: boundedNumber(row.rrfK ?? row.rrf_k, 1, 1_000, 60),
    candidateMultiplier: boundedNumber(row.candidateMultiplier ?? row.candidate_multiplier, 1, 20, 4),
    rerankEnabled: row.rerankEnabled === true || row.rerank_enabled === true,
    rerankCandidateDepth: boundedNumber(row.rerankCandidateDepth ?? row.rerank_candidate_depth, 1, 100, 40),
  };
}

function emptyDocument(baseId: string, documentId: string): KnowledgeDocument {
  return {
    id: documentId, baseId, name: '未命名文档', mimeType: '', byteSize: 0, status: 'ready', stage: 'ready',
    progress: 1, error: '', chunkCount: 0, parser: '', updatedAtMs: 0, revision: 0, sha256: '', pageCount: 0,
    tokenCount: 0, parserVersion: '', sourceReadPath: '', indexedConfigRevision: 0,
  };
}

function mergeDocumentDetailPages(pages: KnowledgeDocumentDetail[]): KnowledgeDocumentDetail {
  const first = pages[0];
  if (!first) throw new Error('文档详情为空。');
  const chunks = pages.flatMap((page) => page.chunks);
  const last = pages.at(-1) ?? first;
  return { ...first, chunks, chunkHasMore: last.chunkHasMore };
}

function mergeDocumentContentPages(detail: KnowledgeDocumentDetail, pages: KnowledgeDocumentDetail[]): KnowledgeDocumentDetail {
  const contentWindow = pages.flatMap((page) => page.contentWindow);
  const last = pages.at(-1);
  return {
    ...detail,
    contentWindow,
    contentLineTotal: pages[0]?.contentLineTotal || detail.contentLineTotal,
    contentHasMore: last?.contentHasMore ?? false,
  };
}

function isActiveJobStatus(value: string): boolean {
  return ['queued', 'running', 'parsing', 'embedding', 'indexing'].includes(value.toLowerCase());
}

function normalizeEmbeddingProfileState(value: unknown): KnowledgeEmbeddingProfileState {
  const root = record(value);
  const profile = record(root.profile);
  const runtime = record(root.runtime);
  const rawPhase = text(root.phase);
  return {
    profile: {
      source: text(profile.source) === 'environment' ? 'environment' : 'settings',
      provider: embeddingProvider(profile.provider),
      model: text(profile.model),
      baseUrl: text(profile.baseUrl),
      dimensions: number(profile.dimensions),
      secretReference: text(profile.secretReference),
      queryPrefix: text(profile.queryPrefix),
      documentPrefix: text(profile.documentPrefix),
      denseBackend: text(profile.denseBackend) === 'usearch' ? 'usearch' : 'sqlite-exact',
      secretAvailable: bool(profile.secretAvailable),
      profileSha256: text(profile.profileSha256),
      secretsVisible: false,
    },
    phase: rawPhase === 'active' || rawPhase === 'applied_pending_rebuild'
      ? rawPhase
      : 'applied_pending_restart',
    runtime: {
      provider: record(runtime.provider),
      fingerprint: text(runtime.fingerprint),
      dimensions: nullableNumber(runtime.dimensions),
      vectorCount: number(runtime.vectorCount),
      chunkCount: number(runtime.chunkCount),
      coverage: Math.max(0, Math.min(1, number(runtime.coverage))),
      available: bool(runtime.available),
      degraded: bool(runtime.degraded),
      reason: text(runtime.reason),
    },
  };
}

function normalizeEmbeddingProbe(value: unknown): KnowledgeEmbeddingProbe {
  const payload = record(value);
  if (payload.ready !== true || payload.secretsVisible !== false) {
    throw new Error('向量模型连接测试没有返回可验证的无密钥结果。');
  }
  return {
    ready: true,
    profileSha256: text(payload.profileSha256),
    provider: text(payload.provider),
    model: text(payload.model),
    fingerprint: text(payload.fingerprint),
    dimensions: number(payload.dimensions),
    semantic: bool(payload.semantic),
    latencyMs: number(payload.latencyMs),
    secretsVisible: false,
  };
}

function normalizeEmbeddingImpact(value: unknown): KnowledgeEmbeddingImpact {
  const payload = record(value);
  if (payload.approvalRequiredForApply !== true || payload.secretsVisible !== false) {
    throw new Error('向量模型影响预览没有通过安全检查。');
  }
  const candidate = embeddingCandidate(record(payload.candidate));
  return {
    candidate,
    probe: normalizeEmbeddingProbe(payload.probe),
    currentProfileSha256: text(payload.currentProfileSha256),
    configurationChanges: Object.fromEntries(
      Object.entries(record(payload.configurationChanges)).map(([key, item]) => [key, jsonValue(item)]),
    ),
    requiresWorkerRestart: bool(payload.requiresWorkerRestart),
    requiresRebuild: bool(payload.requiresRebuild),
    affectedBases: list(payload.affectedBases).map((item) => {
      const row = record(item);
      return {
        kbId: text(row.kbId),
        name: text(row.name),
        documentCount: number(row.documentCount),
        chunkCount: number(row.chunkCount),
      };
    }).filter((item) => Boolean(item.kbId)),
    affectedBaseCount: number(payload.affectedBaseCount),
    affectedDocumentCount: number(payload.affectedDocumentCount),
    affectedChunkCount: number(payload.affectedChunkCount),
    approvalRequiredForApply: true,
    secretsVisible: false,
  };
}

export function embeddingCandidate(value: Record<string, unknown>): KnowledgeEmbeddingCandidate {
  return {
    provider: embeddingProvider(value.provider),
    model: text(value.model),
    baseUrl: text(value.baseUrl),
    dimensions: number(value.dimensions),
    secretReference: text(value.secretReference),
    queryPrefix: text(value.queryPrefix),
    documentPrefix: text(value.documentPrefix),
    denseBackend: text(value.denseBackend) === 'usearch' ? 'usearch' : 'sqlite-exact',
  };
}

function embeddingProvider(value: unknown): KnowledgeEmbeddingProvider {
  const provider = text(value, 'none');
  return ['environment', 'none', 'local-hash', 'sentence-transformers', 'mlx-bert', 'openai-compatible'].includes(provider)
    ? provider as KnowledgeEmbeddingProvider
    : 'none';
}

export function knowledgeIndexRuntimeStatus(value: unknown): KnowledgeIndexRuntimeStatus {
  const root = record(value);
  const components = record(root.components);
  const providerRoot = record(root.provider);
  const denseContainer = firstRecord(root.dense, components.dense, providerRoot.dense, root.embedding, components.embedding);
  const dense = { ...denseContainer, ...record(denseContainer.provider) };
  const fingerprint = text(dense.fingerprint);
  const parts = fingerprint.split(':');
  const dimensions = number(dense.dimensions ?? dense.dimension) || parts.map((part) => Number(part)).find((part) => Number.isInteger(part) && part > 0) || null;
  return {
    available: dense.available === true,
    degraded: dense.degraded === true,
    kind: text(dense.kind, '未报告'),
    fingerprint,
    provider: text(dense.providerName, text(dense.kind, parts[0] || '未报告')),
    model: text(dense.model, parts.length > 2 ? parts.slice(1).filter((part) => !/^\d+$/u.test(part)).join(':') || '内置' : parts[1] || '未报告'),
    dimensions,
    vectorCount: typeof dense.vectorCount === 'number' && Number.isFinite(dense.vectorCount) ? dense.vectorCount : null,
    reason: text(dense.reason),
  };
}

function firstRecord(...values: unknown[]): Record<string, unknown> {
  return values.map(record).find((value) => Object.keys(value).length > 0) ?? {};
}

function normalizeSearchHits(value: unknown): KnowledgeSearchHit[] {
  const payload = record(value);
  return list(payload.items ?? payload.hits ?? payload.results ?? value).map((item, index) => {
    const row = record(item);
    const citation = record(row.citation ?? row.provenance);
    const diagnostics = record(row.diagnostics);
    const score = row.score;
    const effectiveMode = text(diagnostics.effectiveMode);
    return {
      id: text(row.id, text(row.chunkId, `hit-${index + 1}`)),
      documentId: text(row.documentId, text(citation.documentId)),
      documentName: text(row.documentName, text(citation.documentName, '未命名文档')),
      title: text(row.title, text(row.heading, '文档片段')),
      excerpt: text(row.excerpt, text(row.text, text(row.content))),
      score: typeof score === 'number' && Number.isFinite(score) ? score : null,
      page: nullableNumber(row.page ?? citation.page),
      heading: text(row.heading, text(citation.heading)),
      lineStart: nullableNumber(row.lineStart ?? citation.lineStart),
      lineEnd: nullableNumber(row.lineEnd ?? citation.lineEnd),
      diagnostics: {
        effectiveMode: effectiveMode === 'hybrid' || effectiveMode === 'lexical' || effectiveMode === 'dense' ? effectiveMode : 'unknown',
        lexicalRank: nullableNumber(diagnostics.lexicalRank),
        denseRank: nullableNumber(diagnostics.denseRank),
        graphRank: nullableNumber(diagnostics.graphRank),
        lexicalScore: nullableNumber(diagnostics.lexicalScore),
        denseScore: nullableNumber(diagnostics.denseScore),
        graphScore: nullableNumber(diagnostics.graphScore),
        retrievalScore: nullableNumber(diagnostics.retrievalScore),
        retrievalRank: nullableNumber(diagnostics.retrievalRank),
        rerankScore: nullableNumber(diagnostics.rerankScore),
        rerankRank: nullableNumber(diagnostics.rerankRank),
        rerankProvider: text(diagnostics.rerankProvider),
        rerankFingerprint: text(diagnostics.rerankFingerprint),
        independentRerankStage: diagnostics.independentRerankStage === true,
        subagentSubstitute: diagnostics.subagentSubstitute === true,
        graphMatches: list(diagnostics.graphMatches).map((item) => text(item)).filter(Boolean),
        graphPaths: list(diagnostics.graphPaths).map((item) => text(item)).filter(Boolean),
      },
    };
  });
}

function normalizeSearchRetrieval(value: unknown): KnowledgeSearchRetrieval | null {
  const root = record(value);
  if (!Object.keys(root).length) return null;
  const dense = record(root.dense);
  const reranker = record(root.reranker);
  return {
    mode: text(root.mode, 'unknown'),
    effectiveMode: text(root.effectiveMode, 'unknown'),
    config: Object.keys(record(root.config)).length ? normalizeRetrievalConfig(root.config) : null,
    libraries: list(root.libraries).map((item) => {
      const row = record(item);
      return {
        baseId: text(row.kbId),
        baseName: text(row.kbName),
        config: Object.keys(record(row.config)).length ? normalizeRetrievalConfig(row.config) : null,
        effectiveMode: text(row.effectiveMode, 'unknown'),
        candidateLimit: number(row.candidateLimit),
        lexicalCandidates: number(row.lexicalCandidates),
        denseCandidates: number(row.denseCandidates),
        graphCandidates: number(row.graphCandidates),
        graphStatus: text(row.graphStatus, 'unknown'),
        graphMatchedNodes: number(row.graphMatchedNodes),
        rerankApplied: row.rerankApplied === true,
        rerankCandidates: number(row.rerankCandidates),
        returned: number(row.returned),
      };
    }),
    lexicalAvailable: root.lexicalAvailable !== false,
    dense: {
      available: dense.available === true,
      degraded: dense.degraded === true,
      provider: text(dense.provider, text(dense.providerName)),
      model: text(dense.model),
    },
    reranker: {
      provider: text(reranker.provider, 'none'),
      configured: reranker.configured === true,
      fingerprint: text(reranker.fingerprint),
      independentStage: reranker.independentStage !== false,
      subagentSubstitute: reranker.subagentSubstitute === true,
      fallbackCount: number(reranker.fallbackCount),
      error: text(reranker.error),
    },
  };
}

function documentStatus(value: string): KnowledgeDocumentStatus {
  return ['queued', 'parsing', 'indexing', 'ready', 'failed', 'stale'].includes(value)
    ? value as KnowledgeDocumentStatus
    : value === 'error' ? 'failed' : 'queued';
}

function parserMode(value: string): KnowledgeParserMode {
  return value === 'builtin' ? value : value === 'mineru' || value === 'mineru_local_http' ? 'mineru' : 'auto';
}

function parserProvider(value: KnowledgeParserMode): 'auto' | 'builtin' | 'mineru_local_http' {
  return value === 'mineru' ? 'mineru_local_http' : value;
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function list(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' && value.trim() ? value.trim() : fallback;
}

function number(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

function nullableNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function optionalNumber(value: unknown): number | null {
  return value === null || value === undefined || value === '' ? null : nullableNumber(value);
}

function graphStatus(value: string): KnowledgeGraph['status'] {
  return value === 'building' || value === 'stale' || value === 'failed' ? value : 'ready';
}

function boundedNumber(value: unknown, min: number, max: number, fallback: number): number {
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(parsed) ? Math.max(min, Math.min(max, parsed)) : fallback;
}

function stringList(value: unknown, limit: number): string[] {
  return Array.isArray(value)
    ? value.flatMap((item) => typeof item === 'string' ? [item.slice(0, 2_000)] : []).slice(0, limit)
    : [];
}

function safeAssetPath(value: string): string {
  if (/^\/api\/knowledge-bases\/[A-Za-z0-9._:-]+\/documents\/[A-Za-z0-9._:-]+\/assets\/[a-f0-9]{64}$/u.test(value)) return value;
  if (/^\/v1\/knowledge\/assets\/[A-Za-z0-9._-]+$/u.test(value)) return value;
  if (/^(?:blob:|data:image\/(?:png|jpeg|webp|gif);base64,)/iu.test(value)) return value;
  return '';
}

function safeSourcePath(value: string): string {
  return /^\/api\/knowledge-bases\/[A-Za-z0-9._:-]+\/documents\/[A-Za-z0-9._:-]+\/source$/u.test(value) ? value : '';
}

function normalizeArtifact(value: unknown): KnowledgeDocumentDetail['artifact'] {
  const row = record(value);
  return {
    available: bool(row.available),
    format: text(row.format),
    mimeType: text(row.mimeType),
    byteSize: number(row.byteSize),
    lineCount: number(row.lineCount),
    sha256: text(row.sha256),
  };
}

function bool(value: unknown): boolean {
  return value === true || value === 1;
}

function jsonRecord(value: Record<string, unknown>): Record<string, JsonValue> {
  return Object.fromEntries(Object.entries(value).filter((entry): entry is [string, JsonValue] => isJson(entry[1])));
}

function isJson(value: unknown): value is JsonValue {
  if (value === null || ['string', 'number', 'boolean'].includes(typeof value)) return true;
  if (Array.isArray(value)) return value.every(isJson);
  if (typeof value === 'object') return Object.values(value as Record<string, unknown>).every(isJson);
  return false;
}

function jsonValue(value: unknown): JsonValue {
  return isJson(value) ? value : 0;
}
