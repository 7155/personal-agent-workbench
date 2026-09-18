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
    search: { draft: '', query: '', config: null, hits: [], retrieval: null, selectedId: '', status: 'idle', error: '', request: 0 },
  };
}

/** In-memory reading context survives a native route remount. It is scoped to
 * the App window and library, and never issues or persists a backend request. */
export function useKnowledgeReadingContext(baseId: string, windowId: string) {
  const client = useQueryClient();
  const key = ['knowledge-reading-context', windowId, baseId] as const;
  const { data } = useQuery({
    queryKey: key,
    queryFn: skipToken,
    initialData: emptyContext,
    gcTime: 30 * 60 * 1_000,
    staleTime: Infinity,
  });
  const update = (change: (current: KnowledgeReadingContext) => KnowledgeReadingContext) => {
    const next = change(client.getQueryData<KnowledgeReadingContext>(key) ?? emptyContext());
    client.setQueryData(key, next);
    return next;
  };
  return { context: data ?? emptyContext(), update };
}

export type KnowledgeReadingController = ReturnType<typeof useKnowledgeReadingContext>;
