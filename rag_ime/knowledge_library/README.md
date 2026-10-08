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

## Retrieval testing and configuration

In Knowledge → Search, expand **搜索范围与方式** to test a retrieval mode,
top K, score threshold, or reranking with a bounded candidate depth. These
overrides apply only to that search request. **恢复知识库默认参数** removes the
overrides; saving a default for future Agent searches remains in Settings.
The search result retains its recorded configuration while the next test is
being edited, including when returning from source reading.

With reranking enabled, the first stage supplies the bounded candidate window
before threshold filtering. The threshold applies to the final reranker score;
without reranking it applies to the first-stage retrieval score. A threshold
chosen for one mode or model is not evidence of quality in another. Inspect
the source passage and both ranks, and include queries with no supporting
document when selecting defaults. An unavailable reranker is an explicit
error, never a successful unreranked result.

Only ready documents indexed against the base's current configuration are
eligible retrieval evidence. SQLite filters the canonical document and chunk
rows before selecting dense candidates. When an ANN projection contains stale
or orphaned candidates, it uses the same exact filtered scan; the runtime
reports `readyFilterFallbackCount`. This prioritizes usable results during a
rebuild and may increase query latency until the projection is refreshed.

## Document structure and paper parsing

### Local native image retrieval

The optional `embedding-local` runtime supports the public
[`google/embeddinggemma-2`](https://huggingface.co/google/embeddinggemma-2)
model (Apache 2.0). Download a fixed Hugging Face revision outside the
repository, then probe its local snapshot with the existing
`sentence-transformers` embedding profile. The model uses
`task: search result | query: ` for queries and `title: none | text: ` for
document text; PAW supplies those defaults for this model. It runs offline on
CPU with float32 and loads the text/image encoders. No Provider request is
needed for local embedding.

With this profile, imported images and local image assets from structured
documents receive native image vectors in the text model's shared space.
PDF pages also receive bounded local rendered-page vectors, including scanned
pages without OCR text. Each visual evidence chunk keeps its asset hash,
original document, supplied page, and local asset read path. A source label
in a visual hit describes its location; it is not a generated caption or an
OCR transcription. Text/caption chunks remain separately searchable.

There are at most 64 visual units per document; PDF page rendering uses a
1024-pixel maximum side. Document metadata reports truncation. These limits
bound local indexing cost and do not establish recognition accuracy on real
papers. Other embedding profiles retain the existing text/OCR/MinerU path.

### Local audio and video intake

With an EmbeddingGemma 2 profile and local `ffmpeg` / `ffprobe` on PATH, the
same import/job/asset/chunk/dense owners accept WAV, MP3, FLAC, OGG, M4A,
MP4, MOV, WebM and MKV. Inputs are forced through container demuxers with local
file/pipe protocols; playlists, remote sources and command strings are not
media inputs. Each subprocess has a 30-second deadline, each produced asset
has a 4 MiB limit, and video frames have the same 20-million-pixel input limit
and 1024-pixel output-side limit as images. No new package or model is fetched.

Audio is decoded into mono 16 kHz PCM16 segments of at most 30 seconds and
encoded by the actual audio tower in the shared vector space. The first audio
request upgrades the existing lazy model to include audio; text/image-only
use keeps the smaller footprint. Loading all towers increases memory cost.
Video is sampled every 10 seconds into image vectors, with its audio track
segmented separately when the profile supports audio. These are frame and
sound evidence, not a native temporal-video embedding or an ASR/OCR transcript.
Frame timestamps are requested seek offsets, not certified presentation
timestamps; short events between samples can be missed.

There are at most 64 media units per document, split into 32 frames / 32 audio
segments when both streams are indexed. Otherwise the single lane may use all
64. Metadata reports omitted audio and each truncated lane; unprocessed tails
are never represented as fully indexed. Original source bytes and hashes
remain authoritative. Each hit and source block keeps the time offset/range,
asset hash and existing read path; audio assets use the existing binary
readback endpoint. A missing audio encoder fails audio import explicitly;
model/encoding failures produce failed media jobs rather than searchable
source-label substitutes. Cancellation is checked between bounded subprocesses.

The audio capability changes the EmbeddingGemma 2 provider fingerprint. Use the
existing preview/apply/rebuild path to switch; older vectors are not reused
under the new fingerprint. This source capability and local sandbox checks do
not enable a user profile or prove installed/browser playback acceptance.

Changing this profile still requires the normal embedding preview/apply and
explicit base rebuild. Merely downloading the model does not switch an active
profile or replace existing vectors. The installed worker must include the
`embedding-local` optional dependencies before this profile can load.

File format, parsing engine and chunking strategy are separate choices. The
**论文（保留章节与参考文献）** strategy uses the configured MinerU engine for
PDFs in `auto` mode, including when an existing automatically parsed document
is rebuilt. Explicit per-document parser choices survive retry and rebuild.
Without MinerU, paper chunking preserves recognizable text headings and
references; it does not reconstruct columns, equations or missing OCR text.

| Input | Preserved structure | Current limits |
| --- | --- | --- |
| MinerU PDF/image output | Ordered content-list blocks, headings, tables, formulas, image captions, reference blocks, one-based pages and declared bounding boxes; original JSON attachment | Depends on the configured engine's actual output; missing/unsupported structure has explicit fallback metadata |
| DOCX | Continuous text across formatting runs, paragraphs, heading ancestry, table cells, merge metadata and embedded media | No rendered pagination; image text comes from supplied alternative text, not OCR |
| XLSX | Sheet order/names, cell coordinates, empty columns, cached values, formulas, merge ranges and embedded media | Very sparse sheets use coordinate/value rows; no date/number display formatting or merged-cell HTML |
| PPTX | Presentation slide order, tables, speaker notes, actual slide numbers and embedded media | Notes are searchable; image text comes from supplied alternative text, not OCR |
| EPUB | Spine reading order, chapter headings, tables and packaged image assets | No invented print-page numbers; encrypted chapters require another adapter; remote resources are not fetched |
| DOC/XLS/PPT | Optional local LibreOffice conversion into the corresponding OOXML parser | Requires an available `soffice`/`libreoffice`; conversion fidelity depends on that installed version |
| Text/Markdown and builtin PDF text | Existing text parsing; paper strategy retains explicit sections and bibliography | No inferred authors, layout coordinates or Word/Excel page numbers |

General, Markdown, Book and Paper strategies preserve supplied structure and
keep tables/formulas separate from prose. Oversized
tables use bounded row groups with repeated headers where possible; a single
oversized row or formula must still split to respect the chunk size. Prose
overlap applies inside long blocks. QA, Laws, Fixed and Separator strategies
retain their existing text-template rules rather than block geometry. Printed
page-number blocks remain in the parse tree but are not standalone search chunks.

Office pictures retain their original bytes/hash and source part. Chart blocks
retain available cached series/category data; missing cached data stays explicitly
unavailable. Neither operation performs image OCR or recalculates chart formulas.
Legacy conversion uses a separate temporary profile, disables macros and active
content, bounds execution/output, and leaves the source unchanged. Its receipt
records original/converted formats, converter version and converted-file hash.

Chunks carry bounded source-block provenance through search citations, source
opening, re-chunk previews and portable search snapshots. Search details show
block type and supplied page/coordinate evidence. A full parsed block tree is
kept internally for re-chunking, rather than returned with every document list.
Old chunks and snapshots remain readable with empty provenance. Existing bases
need an explicit rebuild to acquire new parser output; preview alone never
reruns OCR. These guarantees are covered by synthetic offline fixtures and do
not establish recognition accuracy on real papers.

## Reading and source verification

Document text, titles, headings and retrieval excerpts retain their original
wording. UI terminology localization never rewrites the source. The reader
connects tables and images to chunks using persisted block identities and
asset hashes. Known local Markdown images open on demand; remote image URLs
are not fetched. Identical filenames without a unique source association do
not establish a match.

Structured tables display rows and columns, retaining blank cells. A split
retrieval hit distinguishes its exact excerpt from the larger source table.
Large previews explicitly report truncation; use the original source download
for the complete table. The reader projection is bounded to 32 tables, 800
data rows and 16,000 cells overall, with at most 200 rows per table. Original
files and parser artifacts remain unchanged. Legacy documents without typed
blocks use the existing text table fallback.

For PDFs, **源文件** opens a valid search-hit page and provides bounded page
navigation. When total pages are unknown, only evidenced pages are offered;
the number of indexed pages is never presented as the document page count.
Downloads retain the unmodified source. PDF page navigation depends on the
browser PDF viewer; bounding-box highlighting and reconstructed PDF layouts
are not implemented.

## RAGFlow design references

The comparison baseline is
[`302ada2cdbdd72a5db4bd8e046478e52011fe4f0`](https://github.com/infiniflow/ragflow/tree/302ada2cdbdd72a5db4bd8e046478e52011fe4f0).
PAW implements these patterns in its own owners; no RAGFlow source is bundled.

| RAGFlow reference | PAW implementation |
| --- | --- |
| [Retrieval testing](https://github.com/infiniflow/ragflow/blob/302ada2cdbdd72a5db4bd8e046478e52011fe4f0/docs/guides/dataset/retrieval_testing.md): per-test settings and source inspection | Request-only overrides, result configuration snapshots, source-reader return context |
| [Search pipeline](https://github.com/infiniflow/ragflow/blob/302ada2cdbdd72a5db4bd8e046478e52011fe4f0/rag/nlp/search.py): eligibility constraints and final score filtering | Canonical ready/current-index filtering before candidate limits; threshold after optional reranking |
| [Parsing task execution](https://github.com/infiniflow/ragflow/blob/302ada2cdbdd72a5db4bd8e046478e52011fe4f0/rag/svr/task_executor.py): empty-output checks and cancellation | Document revision fences, cancellation-aware terminal transitions and explicit parser validation in the existing Knowledge worker |

Offline regression checks use temporary Knowledge roots and deterministic
encoders/rerankers:

```bash
python3 -m unittest discover -s tests -p 'test_knowledge*.py'
pnpm --dir control-center-web test src/features/knowledge src/app/preview-control-transport.test.ts
pnpm --dir control-center-web typecheck
pnpm --dir control-center-web build
```

These checks establish engineering behavior, not retrieval-quality gains for a
real corpus. Provider/model quality, MinerU output quality and installed native
behavior require separate runtime evidence. Existing source and index revisions
are retained; this work does not automatically reparse or rebuild a user's base.
