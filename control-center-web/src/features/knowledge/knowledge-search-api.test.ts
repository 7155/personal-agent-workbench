import { expect, it } from 'vitest';
import { MockControlTransport } from '@/test/mock-transport';
import { searchKnowledgeBase, type KnowledgeRetrievalConfig } from './api';

const config: KnowledgeRetrievalConfig = { mode: 'hybrid', topK: 10, threshold: 0, lexicalWeight: 1, denseWeight: 1, graphEnabled: false, graphWeight: 0.7, rrfK: 60, candidateMultiplier: 4, rerankEnabled: true, rerankCandidateDepth: 40 };
it('preserves unknown diagnostics and distinguishes a configured model from actual reranking', async () => {
  const transport = new MockControlTransport({ routes: { 'knowledgeBases.search': () => ({ hits: [], retrieval: {
    libraries: [{ kbId: 'kb', rerankApplied: false, returned: 0 }],
    dense: { provider: { provider: 'configured-embedding', model: 'model-one' } },
    reranker: { configured: true, provider: 'configured-reranker' },
  } }) } });
  const result = await searchKnowledgeBase(transport, 'kb', 'question', config);
  expect(result.retrieval?.libraries[0]).toMatchObject({ candidateLimit: null, lexicalCandidates: null, rerankApplied: false, rerankCandidates: null, returned: 0 });
  expect(result.retrieval?.dense).toMatchObject({ provider: 'configured-embedding', model: 'model-one', available: null });
  expect(result.retrieval?.reranker).toMatchObject({ configured: true, fallbackCount: null });
  expect(transport.requests[0]?.request.body).toMatchObject({ rerank: true, rerankCandidateDepth: 40 });
});
it('does not invent disabled or zero-count stages in an older receipt', async () => {
  const transport = new MockControlTransport({ routes: { 'knowledgeBases.search': () => ({ hits: [], retrieval: { libraries: [{ kbId: 'kb' }] } }) } });
  const result = await searchKnowledgeBase(transport, 'kb', 'question', config);
  expect(result.retrieval?.libraries[0]?.rerankApplied).toBeNull();
  expect(result.retrieval?.reranker.configured).toBeNull();
  expect(result.retrieval?.reranker.provider).toBe('');
  expect(result.retrieval?.lexicalAvailable).toBeNull();
});
