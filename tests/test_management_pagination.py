from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from rag_ime.debug_server import DebugImeService, DebugServerConfig
from rag_ime.local_sqlite_core import LocalSqliteCoreClient
from rag_ime.management_service import page_request
from rag_ime.models import InputEvent


class ManagementPaginationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-control-pagination-")
        self.db_path = Path(self.tmp.name) / "rag-ime.sqlite"
        self.core = LocalSqliteCoreClient(self.db_path)
        self.core.initialize()
        self.service = DebugImeService(DebugServerConfig(db_path=self.db_path, seed_if_empty=False))
        for index in range(125):
            self.core.record_event(
                InputEvent(
                    event_id=None,
                    created_at_ms=1_900_000_000_000 + index,
                    source="manual",
                    committed_text=f"不能默认返回的真实历史正文 {index}",
                    privacy_disposition="allowed",
                    recent_context=f"private context {index}",
                    app="TextEdit",
                    project="wisdom-weasel-rag-ime",
                    context_group_id="document:test",
                    context_group_level="document",
                )
            )

    def tearDown(self) -> None:
        self.service.close()
        self.core.close()
        self.tmp.cleanup()

    def test_history_clamps_limit_and_uses_cursor(self) -> None:
        first = self.service.management.history_page(page_request({"limit": 500}))
        second = self.service.management.history_page(page_request({"limit": 20, "cursor": first["nextCursor"]}))

        self.assertEqual(first["limit"], 100)
        self.assertEqual(len(first["items"]), 100)
        self.assertEqual(len(second["items"]), 20)
        self.assertLess(second["items"][0]["id"], first["items"][-1]["id"])
        self.assertNotIn("text", first["items"][0])
        self.assertNotIn("recentContext", first["items"][0])
        self.assertTrue(first["items"][0]["textHash"].startswith("sha256:"))

    def test_history_source_filter_uses_display_categories_not_exact_storage_values(self) -> None:
        cases = (
            ("squirrel_input_segment", "squirrel:accessibility", "rime_commit"),
            ("voice_streaming_asr", "voice_streaming_asr", "voice"),
            ("squirrel_rime_sidecar", "rime-sidecar:model", "assistant_candidate"),
            ("codex_history_import", "local", "import"),
        )
        for offset, (source, provider, _category) in enumerate(cases):
            self.core.record_event(
                InputEvent(
                    event_id=None,
                    created_at_ms=1_900_000_010_000 + offset,
                    source=source,
                    committed_text=f"分类来源 {source}",
                    privacy_disposition="allowed",
                    app="com.openai.codex",
                    project="wisdom-weasel-rag-ime",
                    provider_name=provider,
                )
            )

        for source, _provider, category in cases:
            page = self.service.management.history_page(
                page_request({"limit": 20, "status": category})
            )
            self.assertEqual([item["source"] for item in page["items"]], [source])
            self.assertEqual(page["items"][0]["sourceCategory"], category)

    def test_history_detail_exposes_saved_auxiliary_context_without_guessing_model_request(self) -> None:
        self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=1_900_000_020_000,
                source="squirrel_input_segment",
                committed_text="继续修复历史详情",
                privacy_disposition="allowed",
                recent_context="前面正在检查来源筛选。继续修复历史详情",
                app="com.openai.codex",
                project="wisdom-weasel-rag-ime",
                provider_name="squirrel:accessibility",
                capture_metadata={
                    "captureSource": "accessibility",
                    "fallbackReason": "",
                    "fieldContextChars": 21,
                    "imeBufferChars": 8,
                    "selectedTextSha256": "must-not-be-returned",
                },
            )
        )
        with sqlite3.connect(self.db_path) as conn:
            event_id = int(
                conn.execute(
                    "SELECT MAX(id) FROM input_events WHERE source = 'squirrel_input_segment'"
                ).fetchone()[0]
            )

        page = self.service.management.history_page(page_request({"limit": 1}))
        detail = self.service.management.history_detail(event_id)
        auxiliary = detail["item"]["auxiliaryContext"]

        self.assertNotIn("auxiliaryContext", page["items"][0])
        self.assertTrue(detail["rawTextVisible"])
        self.assertEqual(auxiliary["text"], "前面正在检查来源筛选。继续修复历史详情")
        self.assertEqual(auxiliary["captureSource"], "accessibility")
        self.assertEqual(auxiliary["fieldContextChars"], 21)
        self.assertEqual(auxiliary["imeBufferChars"], 8)
        self.assertTrue(auxiliary["hasAdditionalText"])
        self.assertFalse(auxiliary["modelRequestLinked"])
        self.assertNotIn("selectedTextSha256", auxiliary)

    def test_history_detail_explains_voice_capture_zero_ime_buffer_and_model_non_applicability(self) -> None:
        text = "语音定稿不经过输入法缓冲区"
        capture_metadata = {
            "schemaVersion": "rag-ime.input-capture.v2",
            "captureId": "capture:voice:detail:1",
            "transactionId": "transaction:voice:detail",
            "sequence": 1,
            "channel": "voice",
            "boundaryKind": "voice_final",
            "boundaryConfidence": "strong",
            "nativeCompositionBefore": False,
            "rimeHandled": False,
            "hostForwarded": True,
            "modifiedReturn": False,
            "finalCommitted": True,
            "controllerEpoch": 3,
            "focusEpoch": 8,
            "appBundleId": "com.openai.codex",
            "fieldIdentitySha256": hashlib.sha256(b"field:voice").hexdigest(),
            "privacyRevision": "foreground-privacy.v1",
            "occurredStartMs": 1_900_000_030_000,
            "occurredEndMs": 1_900_000_030_100,
            "contentSha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "captureSource": "voice_insertion",
            "fallbackReason": "",
            "fieldContextChars": 14,
            "imeBufferChars": 0,
            "selectionRule": "voice_final_inserted_text",
        }
        event_ref = self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=1_900_000_030_100,
                source="voice_final",
                committed_text=text,
                privacy_disposition="allowed",
                app="com.openai.codex",
                project="wisdom-weasel-rag-ime",
                provider_name="voice_streaming_asr",
                capture_metadata=capture_metadata,
            )
        )

        detail = self.service.management.history_detail(int(event_ref.split(":", 1)[1]))
        auxiliary = detail["item"]["auxiliaryContext"]

        self.assertEqual(auxiliary["captureSource"], "voice_insertion")
        self.assertTrue(auxiliary["fieldContextRecorded"])
        self.assertEqual(auxiliary["fieldContextChars"], 14)
        self.assertTrue(auxiliary["imeBufferRecorded"])
        self.assertEqual(auxiliary["imeBufferChars"], 0)
        self.assertEqual(auxiliary["modelRequestAssociation"], "not_applicable")
        self.assertEqual(
            auxiliary["modelRequestReason"],
            "voice_capture_does_not_request_assistant_candidates",
        )
        self.assertEqual(
            auxiliary["captureReceipt"],
            {
                "available": True,
                "channel": "voice",
                "boundaryKind": "voice_final",
                "boundaryConfidence": "strong",
                "outcome": "stored",
                "reason": "strong_final_boundary",
                "evidenceState": "candidate",
                "evidenceReason": "awaiting_luna_adjudication",
            },
        )
        summary = self.service.management.memory_summary()
        self.assertEqual(summary["pendingGovernedEvidenceCount"], 1)
        self.assertEqual(summary["governedNeedsReviewEvidenceCount"], 0)

    def test_memory_apps_only_count_complete_inputs_and_keep_app_provenance(self) -> None:
        self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=1_900_000_001_000,
                source="squirrel_input_segment",
                committed_text="把当前输入缓冲区按回车封口后再写入长期记忆。",
                privacy_disposition="allowed",
                app="com.openai.codex",
                project="wisdom-weasel-rag-ime",
                tags=("input-segment", "finalized", "complete-input"),
                context_group_id="app:com.openai.codex:segment:1",
                context_group_level="app",
            )
        )
        self.core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=1_900_000_001_001,
                source="squirrel_rime_commit_burst",
                committed_text="ai",
                privacy_disposition="allowed",
                app="com.openai.codex",
                project="wisdom-weasel-rag-ime",
                tags=("rime-commit",),
            )
        )
        with sqlite3.connect(self.db_path) as conn:
            event_id = int(
                conn.execute(
                    "SELECT id FROM input_events WHERE source = 'squirrel_input_segment' ORDER BY id DESC LIMIT 1"
                ).fetchone()[0]
            )
            conn.execute(
                "UPDATE memory_state SET deleted = 1 WHERE event_id = (SELECT MAX(id) FROM input_events)"
            )
            conn.execute(
                """
                INSERT INTO memory_atoms(
                    id, kind, text, source_event_ids_json, source_memory_ids_json,
                    scope_app, privacy_level, status, created_at_ms, updated_at_ms
                ) VALUES ('atom:codex', 'preference', '输入封口边界', ?, '[]',
                          'com.openai.codex', 'local', 'active', 1, 1)
                """,
                (json.dumps([event_id]),),
            )
            conn.execute(
                """
                INSERT INTO memory_books(
                    book_id, book_type, book_key, title, app, source_event_ids_json,
                    memory_atom_ids_json, status, created_at_ms, updated_at_ms
                ) VALUES ('book:codex', 'topic', 'codex', 'Codex 输入',
                          'com.openai.codex', ?, '["atom:codex"]', 'active', 1, 1)
                """,
                (json.dumps([event_id]),),
            )

        result = self.service.management.memory_page(
            "apps",
            page_request({"limit": 10, "query": "Codex"}),
        )

        self.assertTrue(result["ok"])
        self.assertFalse(result["rawTextVisible"])
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual(item["id"], "com.openai.codex")
        self.assertEqual(item["eventCount"], 1)
        self.assertEqual(item["finalizedSegmentCount"], 1)
        self.assertEqual(item["contextGroupCount"], 1)
        self.assertEqual(item["atomCount"], 1)
        self.assertEqual(item["bookCount"], 1)

    def test_memory_summary_separates_active_history_and_raw_evidence_atoms(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT INTO memory_atoms(
                    id, kind, text, source_event_ids_json, source_memory_ids_json,
                    privacy_level, status, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, '[]', '[]', 'local', ?, 1, 1)
                """,
                (
                    ("atom:active", "project_requirement", "仍在使用", "active"),
                    ("atom:superseded", "project_requirement", "已被新版替代", "superseded"),
                    ("atom:hidden", "project_fact", "历史保留", "hidden"),
                    ("atom:source", "source_event_archive", "原始碎片证据", "hidden"),
                ),
            )

        result = self.service.management.memory_summary()

        self.assertEqual(result["memoryAtomCount"], 1)
        self.assertEqual(result["memoryAtomTotalCount"], 4)
        self.assertEqual(result["memoryAtomArchivedCount"], 2)
        self.assertEqual(result["memoryAtomSourceArchiveCount"], 1)

        preserved = self.service.management.memory_page(
            "atoms",
            page_request({"limit": 10, "status": "hidden"}),
        )
        source_archive = self.service.management.memory_page(
            "atoms",
            page_request({"limit": 10, "status": "source_archive"}),
        )

        self.assertEqual(
            {str(item["id"]) for item in preserved["items"]},
            {"atom:hidden"},
        )
        self.assertEqual(
            [str(item["id"]) for item in source_archive["items"]],
            ["atom:source"],
        )
        self.assertEqual(source_archive["items"][0]["status"], "source_archive")

    def test_memory_atom_current_status_includes_active_and_approved(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT INTO memory_atoms(
                    id, kind, text, source_event_ids_json, source_memory_ids_json,
                    privacy_level, status, created_at_ms, updated_at_ms
                ) VALUES (?, 'preference', ?, '[]', '[]', 'local', ?, 1, 1)
                """,
                (
                    ("atom:active-current", "自动启用记忆", "active"),
                    ("atom:approved-current", "用户确认记忆", "approved"),
                    ("atom:archived-history", "历史归档记忆", "archived"),
                ),
            )

        result = self.service.management.memory_page(
            "atoms",
            page_request({"limit": 10, "status": "current"}),
        )

        self.assertEqual(
            {str(item["id"]) for item in result["items"]},
            {"atom:active-current", "atom:approved-current"},
        )

    def test_memory_groups_are_paginated_human_readable_and_do_not_return_raw_events(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO memory_semantic_groups(
                    group_id, title, description, project, source_event_ids_json,
                    confidence, quality_score, created_at_ms, updated_at_ms
                ) VALUES (
                    'group:input-method', '输入法', '输入法与个人知识召回',
                    'wisdom-weasel-rag-ime', '[1]', 0.9, 0.9, 1, 1
                )
                """
            )
        result = self.service.management.memory_page("groups", page_request({"limit": 10}))

        self.assertTrue(result["ok"])
        self.assertLessEqual(len(result["items"]), 10)
        self.assertTrue(result["rawTextVisible"])
        self.assertNotIn("committed_text", result["items"][0])

        edited = self.service.management.memory_edit(
            {
                "kind": "group",
                "id": result["items"][0]["id"],
                "title": "输入法前台测试",
                "note": "只汇总这个文档中的输入",
                "color": "teal",
            }
        )
        refreshed = self.service.management.memory_page("groups", page_request({"limit": 10}))

        self.assertTrue(edited["ok"])
        self.assertEqual(refreshed["items"][0]["title"], "输入法前台测试")
        self.assertEqual(refreshed["items"][0]["note"], "只汇总这个文档中的输入")
        self.assertEqual(refreshed["items"][0]["color_token"], "teal")

    def test_agent_project_scope_filters_memory_content_in_sql(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT INTO memory_atoms(
                    id, kind, text, canonical_text, source_event_ids_json,
                    source_memory_ids_json, scope_project, privacy_level,
                    status, created_at_ms, updated_at_ms
                ) VALUES (?, 'fact', ?, ?, '[]', '[]', ?, 'private', 'active', 1, 1)
                """,
                (
                    ("atom:project-a", "Project A public context", "Project A public context", "project-A"),
                    ("atom:project-b", "Project B private roadmap", "Project B private roadmap", "project-B"),
                ),
            )
            conn.executemany(
                """
                INSERT INTO memory_books(
                    book_id, book_type, book_key, title, summary, project,
                    memory_atom_ids_json, status, created_at_ms, updated_at_ms
                ) VALUES (?, 'topic', ?, ?, ?, ?, ?, 'active', 1, 1)
                """,
                (
                    (
                        "book:project-a",
                        "project-a",
                        "Project A",
                        "Project A context",
                        "project-A",
                        json.dumps(["atom:project-a"]),
                    ),
                    (
                        "book:project-b",
                        "project-b",
                        "Project B",
                        "Project B private roadmap",
                        "project-B",
                        json.dumps(["atom:project-b"]),
                    ),
                ),
            )
            conn.executemany(
                """
                INSERT INTO memory_semantic_groups(
                    group_id, title, description, project, source_event_ids_json,
                    confidence, quality_score, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, '[]', 0.9, 0.9, 1, 1)
                """,
                (
                    ("group:project-a", "Project A", "Project A group", "project-A"),
                    ("group:project-b", "Project B", "Project B private group", "project-B"),
                ),
            )
            for project, suffix in (("project-A", "A"), ("project-B", "B")):
                conn.execute(
                    """
                    INSERT INTO memory_items(
                        memory_id, kind, text, normalized_text, project,
                        privacy_class, created_at_ms, updated_at_ms
                    ) VALUES (?, 'phrase', ?, ?, ?, 'private', 1, 1)
                    """,
                    (
                        f"phrase:project-{suffix.lower()}",
                        f"Project {suffix} private phrase",
                        f"project {suffix.lower()} private phrase",
                        project,
                    ),
                )

        scoped = page_request({"project": "project-A", "limit": 20})
        atoms = self.service.management.memory_page("atoms", scoped)["items"]
        books = self.service.management.memory_page("books", scoped)["items"]
        groups = self.service.management.memory_page("groups", scoped)["items"]
        phrases = self.service.management.memory_page("phrases", scoped)["items"]
        negative = self.service.management.memory_page("negative", scoped)["items"]

        self.assertEqual({item["id"] for item in atoms}, {"atom:project-a"})
        self.assertEqual({item["id"] for item in books}, {"book:project-a"})
        self.assertEqual({item["id"] for item in groups}, {"group:project-a"})
        self.assertEqual(
            {item["memoryId"] for item in phrases},
            {"phrase:project-a"},
        )
        self.assertEqual(negative, [])

    def test_semantic_groups_can_be_merged_without_losing_members(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO memory_semantic_groups(
                    group_id, title, description, project, source_event_ids_json,
                    confidence, quality_score, created_at_ms, updated_at_ms
                ) VALUES
                    ('group:input-method', '输入法', '输入法产品与模型', 'wisdom-weasel-rag-ime', '[1]', 0.9, 0.9, 1, 1),
                    ('group:ime-ui', '候选框', '输入法界面细节', 'wisdom-weasel-rag-ime', '[2]', 0.7, 0.7, 1, 1)
                """
            )
            conn.execute(
                """
                INSERT INTO memory_semantic_group_members(
                    group_id, member_type, member_id, weight, source, updated_at_ms
                ) VALUES ('group:ime-ui', 'atom', 'atom:ui', 0.8, 'dsv4', 1)
                """
            )

        merged = self.service.management.memory_edit(
            {
                "kind": "groups",
                "id": "group:ime-ui",
                "mergeIntoId": "group:input-method",
            }
        )

        self.assertTrue(merged["ok"])
        self.assertTrue(merged["changes"]["merged"])
        with sqlite3.connect(self.db_path) as conn:
            source_status = conn.execute(
                "SELECT status FROM memory_semantic_groups WHERE group_id = 'group:ime-ui'"
            ).fetchone()[0]
            target_member = conn.execute(
                """
                SELECT source FROM memory_semantic_group_members
                WHERE group_id = 'group:input-method' AND member_type = 'atom' AND member_id = 'atom:ui'
                """
            ).fetchone()
        self.assertEqual(source_status, "merged")
        self.assertEqual(target_member[0], "user_merge")

    def test_tags_expose_profiles_and_merge_without_losing_relations(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            first = conn.execute(
                "INSERT INTO memory_tags(tag, normalized_tag, tag_type, quality_score, created_at_ms, updated_at_ms, source, status) "
                "VALUES ('输入法', '输入法', 'topic', 0.9, 1, 1, 'user', 'active')"
            ).lastrowid
            second = conn.execute(
                "INSERT INTO memory_tags(tag, normalized_tag, tag_type, quality_score, created_at_ms, updated_at_ms, source, status) "
                "VALUES ('IME', 'ime', 'alias', 0.8, 1, 1, 'user', 'active')"
            ).lastrowid
            conn.execute(
                "INSERT INTO memory_atoms(id, kind, text, source_event_ids_json, source_memory_ids_json, privacy_level, status, created_at_ms, updated_at_ms) "
                "VALUES ('atom:one', 'fact', '输入法项目', '[]', '[]', 'local', 'active', 1, 1)"
            )
            conn.execute(
                "INSERT INTO memory_atom_tags(memory_atom_id, tag_id, weight, source) VALUES ('atom:one', ?, 0.8, 'test')",
                (str(second),),
            )

        edited = self.service.management.memory_edit(
            {
                "kind": "tags",
                "id": str(second),
                "title": "IME",
                "type": "alias",
                "aliases": ["智能输入"],
                "color": "purple",
                "mergeIntoId": str(first),
            }
        )
        page = self.service.management.memory_page("tags", page_request({"limit": 20}))
        merged = next(item for item in page["items"] if item["id"] == str(first))

        self.assertTrue(edited["ok"])
        self.assertTrue(edited["changes"]["merged"])
        self.assertEqual(merged["color_token"], "purple")
        self.assertIn("IME", merged["aliases"])
        self.assertIn("智能输入", merged["aliases"])
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_tags WHERE id = ?", (second,)).fetchone()[0], 0)
            self.assertEqual(
                conn.execute(
                    "SELECT CAST(tag_id AS TEXT) FROM memory_atom_tags WHERE memory_atom_id = 'atom:one'"
                ).fetchone()[0],
                str(first),
            )

    def test_tags_filter_nested_memories_and_edges_by_visible_owner(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            role_a_tag = conn.execute(
                """
                INSERT INTO memory_tags(
                    tag, normalized_tag, tag_type, quality_score,
                    created_at_ms, updated_at_ms, source, status
                ) VALUES ('甲角色标签', '甲角色标签', 'topic', 0.9, 1, 1, 'user', 'active')
                """
            ).lastrowid
            role_b_tag = conn.execute(
                """
                INSERT INTO memory_tags(
                    tag, normalized_tag, tag_type, quality_score,
                    created_at_ms, updated_at_ms, source, status
                ) VALUES ('乙角色标签', '乙角色标签', 'topic', 0.8, 1, 1, 'user', 'active')
                """
            ).lastrowid
            conn.execute(
                """
                INSERT INTO memory_atoms(
                    id, kind, text, source_event_ids_json, source_memory_ids_json,
                    privacy_level, status, owner_kind, owner_id,
                    created_at_ms, updated_at_ms
                ) VALUES
                    ('atom:role-a-tag', 'fact', '甲角色私有事实', '[]', '[]',
                     'local', 'active', 'agent', 'role-a', 1, 1),
                    ('atom:role-b-tag', 'fact', '乙角色私有事实', '[]', '[]',
                     'local', 'active', 'agent', 'role-b', 1, 1)
                """
            )
            conn.execute(
                """
                INSERT INTO memory_atom_tags(memory_atom_id, tag_id, weight, source)
                VALUES
                    ('atom:role-a-tag', ?, 0.9, 'test'),
                    ('atom:role-b-tag', ?, 0.9, 'test')
                """,
                (str(role_a_tag), str(role_b_tag)),
            )
            conn.execute(
                """
                INSERT INTO memory_tag_edges(
                    src_tag_id, dst_tag_id, edge_type, weight,
                    evidence_count, updated_at_ms
                ) VALUES (?, ?, 'related', 0.8, 1, 1)
                """,
                (role_a_tag, role_b_tag),
            )

        page = self.service.management.memory_page(
            "tags",
            page_request(
                {
                    "limit": 20,
                    "ownerKind": "agent",
                    "ownerId": "role-a",
                }
            ),
        )

        self.assertEqual([item["tag"] for item in page["items"]], ["甲角色标签"])
        self.assertEqual(
            [item["text"] for item in page["items"][0]["memories"]],
            ["甲角色私有事实"],
        )
        self.assertEqual(page["items"][0]["connections"], [])
        self.assertEqual(page["items"][0]["edge_count"], 0)

    def test_user_can_edit_semantic_tag_description(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            tag_id = conn.execute(
                """
                INSERT INTO memory_tags(
                    tag, normalized_tag, tag_type, quality_score,
                    created_at_ms, updated_at_ms, source, status
                ) VALUES ('语音纠错', '语音纠错', 'concept', 0.8, 1, 1, 'dsv4', 'active')
                """
            ).lastrowid

        edited = self.service.management.memory_edit(
            {
                "kind": "tags",
                "id": str(tag_id),
                "title": "语音纠错",
                "description": "语音转写后的错别字修正与最终稿替换",
                "type": "concept",
                "aliases": ["ASR 纠错"],
            }
        )
        page = self.service.management.memory_page("tags", page_request({"limit": 20}))
        item = next(row for row in page["items"] if row["id"] == str(tag_id))

        self.assertTrue(edited["ok"])
        self.assertEqual(item["description"], "语音转写后的错别字修正与最终稿替换")
        self.assertEqual(item["source"], "user")

    def test_atom_merge_tombstones_source_and_updates_book_membership(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            for atom_id, text, event_id in (("atom:source", "旧表述", 1), ("atom:target", "保留表述", 2)):
                conn.execute(
                    """
                    INSERT INTO memory_atoms(
                        id, kind, text, canonical_text, source_event_ids_json,
                        source_memory_ids_json, privacy_level, status, confidence,
                        quality_score, created_at_ms, updated_at_ms
                    ) VALUES (?, 'fact', ?, ?, ?, '[]', 'local', 'active', 0.8, 0.8, 1, 1)
                    """,
                    (atom_id, text, text, json.dumps([event_id])),
                )
            conn.execute(
                """
                INSERT INTO memory_books(
                    book_id, book_type, book_key, title, memory_atom_ids_json,
                    created_at_ms, updated_at_ms
                ) VALUES ('book:one', 'topic', 'one', '输入法', ?, 1, 1)
                """,
                (json.dumps(["atom:source"]),),
            )

        result = self.service.management.memory_edit(
            {"kind": "atoms", "id": "atom:source", "mergeIntoId": "atom:target"}
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["changes"]["mergedIntoId"], "atom:target")
        with sqlite3.connect(self.db_path) as conn:
            source_status = conn.execute("SELECT status FROM memory_atoms WHERE id = 'atom:source'").fetchone()[0]
            target_events = json.loads(
                conn.execute("SELECT source_event_ids_json FROM memory_atoms WHERE id = 'atom:target'").fetchone()[0]
            )
            book_atoms = json.loads(
                conn.execute("SELECT memory_atom_ids_json FROM memory_books WHERE book_id = 'book:one'").fetchone()[0]
            )
        self.assertEqual(source_status, "tombstoned")
        self.assertEqual(target_events, [2, 1])
        self.assertEqual(book_atoms, ["atom:target"])


if __name__ == "__main__":
    unittest.main()
