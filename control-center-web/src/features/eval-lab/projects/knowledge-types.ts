import { object } from './types';

export type RetrievalProfile = {
  mode: 'lexical' | 'dense' | 'hybrid'; topK: number; threshold: number; rerank: boolean; candidateDepth: number; contextChars: number;
};
export type KnowledgeCorpus = {
  jobId: string; title: string; corpusHash: string; documentCount: number; byteSize: number;
  intake: { skippedCount?: number; scannedCount?: number };
  preview: { sourceId: string; title: string; uri: string; byteSize: number; excerpt: string }[];
};
export type KnowledgeDataset = {
  datasetId: string; corpusId: string; corpusHash: string; title: string; sha256: string; caseCount: number;
  splits: { development: number; holdout: number }; referenceAnswerCount: number; retrievalEvaluableCount: number;
  officialSplit: false; providedSplit?: boolean; preview: { caseId: string; question: string }[];
};
export type KnowledgeIndex = {
  jobId: string; corpusId: string; corpusHash: string; title: string; documentCount: number; chunkCount: number; configHash: string;
  sourceCount?: number; duplicateSourceCount?: number;
  chunking: { strategy: string; size: number; overlap: number };
  dense: { available: boolean; provider: { semantic: boolean; model?: string; provider: string }; vectorCount: number };
  reranker: { configured?: boolean; provider?: string };
};
export type KnowledgeEvaluation = {
  jobId: string; indexId: string; corpusHash: string; datasetId: string; datasetHash: string; split: 'development' | 'holdout';
  plannedCount: number; evaluatedCount: number; unlabeledCount: number; holdoutUseNumber: number; profile: RetrievalProfile;
  indexConfig?: { chunking: { strategy: string; size: number; overlap: number } };
  report: { metrics: { metrics: { mrr: number; recallAtK: Record<string, number>; ndcgAtK: Record<string, number> } };
    costs: { meanRetrievalLatencyMs: number; retrievalCalls: number }; receiptSha256: string };
};
export type KnowledgeHit = { sourceId: string; title: string; uri: string; content: string; chunkId: string; score?: number };
export type KnowledgeJob = { jobId: string; state: string; progress: string; error: string; createdAtMs: number; updatedAtMs: number;
  publicSpec: { operation: string; projectId: string }; result: ({ kind?: string; message?: string; hits?: KnowledgeHit[]; query?: string } & Record<string, unknown>) | null };
export type KnowledgeState = {
  schemaVersion: 'paw.lab-knowledge-resource.v1'; corpora: KnowledgeCorpus[]; indexes: KnowledgeIndex[]; datasets: KnowledgeDataset[];
  evaluations: KnowledgeEvaluation[]; jobs: KnowledgeJob[]; embedding: { provider: string; model: string };
};
export const activeKnowledgeJob = (job: KnowledgeJob) => ['queued', 'running', 'cancelling'].includes(job.state);
export const defaultRetrieval: RetrievalProfile = { mode: 'lexical', topK: 10, threshold: 0, rerank: false, candidateDepth: 40, contextChars: 16000 };
export function parseKnowledgeState(raw: unknown): KnowledgeState {
  const value = object(raw);
  const finite = (value: unknown) => typeof value === 'number' && Number.isFinite(value);
  const fields = (row: Record<string, unknown>, keys: string[]) => keys.every((key) => typeof row[key] === 'string');
  if (value.schemaVersion !== 'paw.lab-knowledge-resource.v1'
      || !['corpora', 'indexes', 'datasets', 'evaluations', 'jobs'].every((key) => Array.isArray(value[key]))
      || !fields(object(value.embedding), ['provider', 'model'])
      || !(value.corpora as unknown[]).every((raw) => { const row = object(raw); return fields(row, ['jobId', 'title', 'corpusHash']) && finite(row.documentCount) && finite(row.byteSize) && !!row.intake && Array.isArray(row.preview) && row.preview.every((raw) => fields(object(raw), ['sourceId', 'title', 'excerpt'])); })
      || !(value.indexes as unknown[]).every((raw) => { const row = object(raw); return fields(row, ['jobId', 'corpusId', 'corpusHash']) && finite(row.chunkCount) && finite(row.documentCount) && typeof object(row.dense).available === 'boolean' && !!object(row.dense).provider && !!row.chunking && !!row.reranker; })
      || !(value.datasets as unknown[]).every((raw) => { const row = object(raw); return fields(row, ['datasetId', 'corpusHash', 'title', 'sha256']) && finite(row.caseCount) && finite(row.retrievalEvaluableCount) && finite(object(row.splits).development) && finite(object(row.splits).holdout) && (row.providedSplit === undefined || typeof row.providedSplit === 'boolean'); })
      || !(value.evaluations as unknown[]).every((raw) => { const row = object(raw); const report = object(row.report); const metrics = object(object(report.metrics).metrics); return fields(row, ['jobId', 'indexId', 'datasetHash', 'split']) && finite(row.plannedCount) && finite(row.evaluatedCount) && finite(object(row.profile).topK) && finite(metrics.mrr) && !!metrics.recallAtK && !!metrics.ndcgAtK && finite(object(report.costs).meanRetrievalLatencyMs); })
      || !(value.jobs as unknown[]).every((raw) => { const row = object(raw); return fields(row, ['jobId', 'state', 'progress', 'error']) && typeof object(row.publicSpec).operation === 'string'; })) {
    throw new Error('知识库实验数据未完整返回，请重新读取。');
  }
  return value as KnowledgeState;
}
