import { skipToken, useQuery, useQueryClient } from '@tanstack/react-query';
import type { KnowledgeRetrievalConfig, KnowledgeSearchHit, KnowledgeSearchRetrieval } from './api';

export type KnowledgeReaderOrigin = 'materials' | 'search' | 'graph';

export type KnowledgeReadingContext = {
  documentId: string;
  materialsFilter: string;
  readerOrigin: KnowledgeReaderOrigin;
  focusHit: KnowledgeSearchHit | null;
  search: {
    draft: string;
    query: string;
    config: KnowledgeRetrievalConfig | null;
    overrides: Partial<Pick<KnowledgeRetrievalConfig, 'mode' | 'topK' | 'threshold' | 'rerankEnabled' | 'rerankCandidateDepth'>> | null;
    hits: KnowledgeSearchHit[];
    retrieval: KnowledgeSearchRetrieval | null;
    selectedId: string;
    status: 'idle' | 'pending' | 'success' | 'error';
    error: string;
    request: number;
  };
};

function emptyContext(): KnowledgeReadingContext {
  return {
    documentId: '', materialsFilter: '', readerOrigin: 'materials', focusHit: null,
    search: { draft: '', query: '', config: null, overrides: null, hits: [], retrieval: null, selectedId: '', status: 'idle', error: '', request: 0 },
  };
}

const recoveryKey = (baseId: string, windowId: string) => `paw:knowledge-reading:v1:${JSON.stringify([windowId, baseId])}`;

function restoreContext(baseId: string, windowId: string): KnowledgeReadingContext {
  const context = emptyContext();
  if (!baseId || !windowId) return context;
  try {
    const raw = localStorage.getItem(recoveryKey(baseId, windowId));
    if (!raw) return context;
    const saved: unknown = JSON.parse(raw);
    if (!saved || typeof saved !== 'object' || Array.isArray(saved)) return context;
    const record = saved as Record<string, unknown>;
    if (record.version !== 1 || record.baseId !== baseId || record.windowId !== windowId
      || typeof record.draft !== 'string' || typeof record.documentId !== 'string' || typeof record.materialsFilter !== 'string'
      || (record.readerOrigin !== 'materials' && record.readerOrigin !== 'search' && record.readerOrigin !== 'graph')) return context;
    return { ...context, documentId: record.documentId, materialsFilter: record.materialsFilter,
      readerOrigin: record.readerOrigin, search: { ...context.search, draft: record.draft } };
  } catch { return context; }
}

function persistContext(baseId: string, windowId: string, current: KnowledgeReadingContext, next: KnowledgeReadingContext) {
  if (!baseId || !windowId || (current.search.draft === next.search.draft && current.documentId === next.documentId
    && current.materialsFilter === next.materialsFilter && current.readerOrigin === next.readerOrigin)) return;
  try {
    // Only local input and reading choices: never restore results, permissions,
    // request state or a submitted query. localStorage remains origin-scoped.
    localStorage.setItem(recoveryKey(baseId, windowId), JSON.stringify({
      version: 1, baseId, windowId, draft: next.search.draft, documentId: next.documentId,
      materialsFilter: next.materialsFilter, readerOrigin: next.readerOrigin,
    }));
  } catch { /* Storage can be unavailable or full; keep the current in-memory input. */ }
}

/** QueryClient keeps live reading/request state across route remounts. Only
 * local input and reading choices survive a reload, scoped to window/library. */
export function useKnowledgeReadingContext(baseId: string, windowId: string) {
  const client = useQueryClient();
  const key = ['knowledge-reading-context', windowId, baseId] as const;
  const { data } = useQuery({
    queryKey: key,
    queryFn: skipToken,
    initialData: () => restoreContext(baseId, windowId),
    gcTime: 30 * 60 * 1_000,
    staleTime: Infinity,
  });
  const update = (change: (current: KnowledgeReadingContext) => KnowledgeReadingContext) => {
    const current = client.getQueryData<KnowledgeReadingContext>(key) ?? restoreContext(baseId, windowId);
    const next = change(current);
    client.setQueryData(key, next);
    persistContext(baseId, windowId, current, next);
    return next;
  };
  return { context: data ?? emptyContext(), update };
}

export type KnowledgeReadingController = ReturnType<typeof useKnowledgeReadingContext>;
