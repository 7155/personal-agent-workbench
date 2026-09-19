# Jev in PAW

Jev supplies structured decisions, not generated summaries. PAW uses the native
[TypeSafe API](https://docs.typesafe.ai/introduction/quickstart), with the
`jev-latest` model and typed Choice/Score questions.

## Configuration

In a build containing this integration, open System Settings → Model accounts →
TypeSafe / Jev. Save the API key in macOS Keychain. The existing command-line
alternative is `bash scripts/configure_jev_key.sh`. Neither the catalog nor
receipts contain the key. `TYPESAFE_API_KEY` takes precedence; remove it and
restart the service before managing that credential through the UI.

The scenario list separates configured credentials, available adapters and
unimplemented candidates. It does not claim that a saved credential was tested
or that every scenario has been activated.

## Implemented paths

- **Tool approval:** configured Jev is preferred on the next new approval.
  Confidence below 0.70 yields a denial without asking Luna to override it.
  Transport/provider/response failures use the existing Luna Max fallback.
  A completed approval reuses its stored receipt.
- **Knowledge reranking:** select `RAG_IME_KNOWLEDGE_RERANK_PROVIDER=typesafe-jev`
  in the Knowledge worker environment, restart that worker, and enable reranking
  in the base retrieval settings or retrieval test. This is an explicit remote
  path: queries and selected candidate passages are sent to TypeSafe. Merely
  saving a Jev key does not select this reranker. The existing local Qwen3 path
  and default provider remain unchanged.

The reranker scores up to 100 candidates in one request (48,000 UTF-8 bytes
maximum). Scores on a four-level rubric are normalized to 0–1. Source IDs,
content and citations are preserved; equal scores preserve initial order.
Failures are reported through Knowledge's existing reranker error boundary,
never represented as successful reranking. The existing retrieval diagnostics
show provider, original rank, final rank and score. This adapter has synthetic
smoke coverage, not a corpus-quality benchmark; compare representative queries
before replacing a base's existing retrieval profile.

## Candidate scenarios

| Scenario | Potential Jev responsibility | Existing owner retained |
| --- | --- | --- |
| Context compression | Decide which spans must survive | Pi generates summaries and owns compaction |
| Memory deduplication/conflicts | Compare candidate facts | Memory retains evidence and writeback authority |
| Retrieval routing | Decide whether/how to retrieve | Knowledge executes bounded retrieval |
| Evidence sufficiency | Identify unsupported answer requirements | Agent decides follow-up and generates the answer |

These four are not enabled by this integration. No summarization, memory
writeback, background routing or automatic extra calls are introduced.
