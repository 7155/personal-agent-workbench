# Third-Party Notices

This file records the current dependency and reference boundaries. It is not a
substitute for legal review. Before distributing a release, pin every shipped
version, include the corresponding upstream license text, and verify that the
release bundle satisfies each license.

The release gate also requires `rag-ime.release-manifest.v2` evidence for the
exact `THIRD_PARTY_NOTICES.md` digest and the patched-Squirrel corresponding
source archive. A notice filename or product-status boolean is not treated as
proof: the manifest-listed files must exist and their SHA-256 values must match.

## Modified Or Linked Components

| Project | Use in this repository | Version / source | License boundary |
| --- | --- | --- | --- |
| [Squirrel](https://github.com/rime/squirrel) | The production macOS route applies `squirrel-patches/0001-add-rag-ime-sidecar.patch` and can distribute a modified Squirrel binary. | Commit `2158538` | GPL-3.0. Distribution of the patched source or binary must satisfy the GPL, including source-availability and notice obligations. |
| [librime](https://github.com/rime/librime) | Rime engine linked by the pinned Squirrel checkout. | Commit `33e78140250125871856cdc5b42ddc6a5fcd3cd4` in the pinned checkout | BSD 3-Clause in the upstream `LICENSE`; retain its notices when distributing binaries. |
| [MiniMind](https://github.com/jingyaogong/minimind) | Architecture and training baseline for the project-specific completion checkpoint. No upstream or local weights are committed here. | Obtain separately | Apache-2.0 covers upstream code. A custom checkpoint and training corpus need their own provenance and artifact license. |
| [MLX](https://github.com/ml-explore/mlx) / [MLX-LM](https://github.com/ml-explore/mlx-lm) | Optional local Apple Silicon inference runtime installed separately. | Not vendored | Both are MIT upstream. Preserve their notices if a packaged release redistributes either runtime. |
| [MLX Examples BERT](https://github.com/ml-explore/mlx-examples/tree/main/bert) | Reference architecture and Hugging Face key conversion used by the local MLX BERT embedding provider. | Adapted source | MIT. |
| [Ollama](https://github.com/ollama/ollama) / [llama.cpp](https://github.com/ggml-org/llama.cpp) | Optional externally managed loopback model servers. | Not vendored | Both are MIT upstream. No binaries are redistributed by the current source tree. |

## Bundled Source

Earth Agent uses [Leaflet-Geoman Free](https://github.com/geoman-io/leaflet-geoman)
2.20.1 for map drawing, vertex editing, snapping, and polygon cutting. It is an
MIT-licensed npm dependency, copyright (c) 2017 Sumit Kumar. Its complete license
is retained at `licenses/Leaflet-Geoman-LICENSE`.

| Project | Use in this repository | Version / source | License boundary |
| --- | --- | --- | --- |
| [GISclaw](https://github.com/geumjin99/GISclaw) | The Earth Agent deterministic GIS operation registry is vendored from `src/agent/geo_ops.py` to provide CRS, geometry, overlay, analysis, and raster operators. No GISclaw Agent loop or UI is embedded. | Commit `d96b5d2afc9fa90dba8974492a08837b52222569` | AGPL-3.0-or-later. The vendored source retains its upstream header; the corresponding license is `licenses/GISclaw-LICENSE`. PAW must preserve this notice and license when distributing the Earth Agent package. |

## Design And Interaction References

These projects informed design study or interaction expectations. They are not
runtime dependencies and their names do not imply endorsement.

- [Wisdom-Weasel](https://github.com/Felix3322/Wisdom-Weasel): GPL-3.0
  prediction and candidate-lifecycle reference. No source file is intentionally
  copied into this repository.
- [VCPToolBox](https://github.com/lioensky/VCPToolBox): memory/RAG and context
  injection study. No VCP service is required. The upstream project states
  CC BY-NC-SA 4.0; do not copy its material into a differently licensed release
  without reviewing those terms.
- [OpenLess](https://github.com/Open-Less/openless): MIT voice-pipeline and
  push-to-talk architecture reference. No OpenLess source is intentionally
  copied.
- [Matt Pocock Skills](https://github.com/mattpocock/skills): MIT debugging and
  codebase-architecture method reference. The Room-bounded
  `systematic-debugging` and `improve-codebase-architecture` Skills adapt the
  upstream `diagnosing-bugs` and `improve-codebase-architecture` methods from
  commit `2ab958093e83e0ec752e6c1c5932da465bf23e0c`; Room policy and runtime
  authority remain project-owned.
- [LazyTyper releases](https://github.com/oldcai/LazyTyper-releases): interaction
  reference only. The linked repository is a binary release channel and does
  not establish an open-source license for reusable code.

## Remote Service

[Volcengine Doubao streaming ASR 2.0](https://docs.volcengine.com/docs/6561/1354869?lang=zh)
is an optional remote speech-recognition service. Audio leaves the Mac only
during an explicit voice session. Users are responsible for the provider's
terms, privacy policy, credentials, and charges.

The optional Notion Worker / Custom Agent integration is also a remote-service
workflow. The template in this repository is project-authored; users remain
responsible for Notion's current platform and service terms.
