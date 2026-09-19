# Knowledge library

This package owns PAW's source-backed document Knowledge worker. The worker is
the single owner of parsing, chunk revisions, lexical/dense/hybrid retrieval,
citations, graph state, assets, and recoverable indexing jobs. Memory remains a
separate governed system.

RAGFlow is kept as an optional sibling reference checkout at `../ragflow`
relative to the PAW repository root (the local baseline is `302ada2c`). Its
template chunking, hybrid search, RRF fusion, reranking, metadata filtering,
GraphRAG, and retrieval-test concepts are used to audit this package. PAW does
not embed a second RAGFlow server or a second Agent loop. On Apple Silicon the
RAGFlow Docker images are not a drop-in runtime, so the native PAW path uses
MinerU, SQLite FTS5, a local MLX BGE encoder, and optional MLX Qwen3 reranking.

## Operational contract

Use the management API on port 8766. Never edit the Knowledge SQLite database or
worker files directly:

1. Probe a candidate embedding profile with
   `POST /api/knowledge-bases/embedding-probe`.
2. Save settings with the preview/apply work contract. A changed profile is
   `applied_pending_rebuild` until every ready chunk has a vector.
3. Rebuild a base only from its `reindex-preview` receipt; apply the preview
   token, configuration revision, payload hash, and `REBUILD` confirmation.
4. Use the jobs endpoint to observe parsing/indexing failures and retry a
   document through the formal route. Worker restarts recover queued jobs.
5. Rebuild the graph after the document/index revision is stable. A stale graph
   remains visible as stale and is not counted as a successful graph retrieval.

The search response is intentionally diagnostic: it reports the requested and
effective retrieval mode, lexical/dense/graph candidate counts, RRF settings,
reranker state, fallbacks, and source citations. That evidence is required for
an acceptance claim; a UI label saying “hybrid” is not enough.
