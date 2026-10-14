# Governed Memory Patterns

Reference implementations inspected locally:

- `VCPToolBox/Plugin/RAGDiaryPlugin`: memory remains outside model weights and is injected only after retrieval.
- `VCPToolBox/Plugin/LightMemo`: memory operations are explicit tools with bounded inputs and observable results.
- `VCPToolBox/Plugin/SemanticGroupEditor`: semantic grouping is a governed editing operation rather than an automatic rewrite of raw history.

Personal Agent Workbench adapts those ideas as follows:

| Reference pattern | Personal Agent Workbench rule |
| --- | --- |
| Raw diary text is not the final prompt | Keep Evidence separate from Current Atom and Topic Book retrieval documents. |
| Tool calls make memory changes observable | Pi uses governed preview/apply/rollback operations on `memory`; the Skill never writes SQLite. |
| Semantic grouping needs control | Cross-App Task Timelines retain every App as provenance and remain continuity context, not fact evidence. |
| Retrieval should be useful but bounded | Ordinary Agent context admits only finalized, quality-gated segments. |
| Runtime metadata is not knowledge | App is provenance; transport source/tag names never become semantic tags. |
| Agent identity is not user memory | Role Book changes stay in pinned revisions and can only be proposed/reviewed by the Agent. |

The important difference is that evidence can arrive from Agent conversations,
approved input capture, tools, files, or task receipts. The source channel does
not decide durability: every candidate still passes the same provenance,
privacy, conflict, and governance boundary.
