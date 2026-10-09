from __future__ import annotations

import json
import math
import unittest

from rag_ime.agent_blocks import (
    extract_completed_agent_blocks,
    normalize_trusted_agent_blocks,
    provider_block_projection,
    validate_persisted_blocks,
)
from rag_ime.pi.public import pi_message_payload


def fenced(blocks: list[dict[str, object]]) -> str:
    payload = {"schemaVersion": "rag-ime.agent-blocks.v1", "blocks": blocks}
    return f"正文\n```rag_ime_blocks\n{json.dumps(payload, ensure_ascii=False)}\n```"


class AgentBlocksTest(unittest.TestCase):
    def test_typed_text_and_code_real_normalizer_obey_shared_summary_budget(self) -> None:
        from rag_ime.agent_blocks import MAX_SUMMARY_CHARS
        for block_type, data in [("text", {"text": "x" * 1000}), ("code", {"code": "print(1)", "summary": "y" * 1000})]:
            with self.subTest(block_type=block_type):
                blocks = normalize_trusted_agent_blocks(
                    [{"id": "typed-result", "type": block_type, "data": data}],
                    source_kind="pi_runtime_event", source_ref="session:message")
                self.assertEqual(len(blocks), 1)
                self.assertEqual(blocks[0]["type"], block_type)
                self.assertEqual(len(blocks[0]["summary"]), MAX_SUMMARY_CHARS)
                self.assertEqual(blocks[0]["data"], data)
                message = pi_message_payload(
                    {"id": "normalizer-message", "role": "assistant", "content": ""},
                    session_id="session", turn_id="turn", trusted_blocks=[
                        {"id": "typed-result", "type": block_type, "data": data}]).to_payload()
                self.assertEqual(message["blocks"][0]["presentationKind"], "markdown" if block_type == "text" else "code")

    def test_extracts_complete_typed_blocks_and_separates_text(self) -> None:
        raw = fenced(
            [
                {
                    "id": "check:1",
                    "type": "checklist",
                    "data": {
                        "title": "发布",
                        "items": [
                            {"text": "测试", "checked": True},
                            {"text": "上线", "checked": False},
                        ],
                    },
                }
            ]
        )
        result = extract_completed_agent_blocks(
            raw,
            source_kind="pi_session_message",
            source_ref="message:1",
        )
        self.assertEqual(result.text, "正文")
        self.assertEqual(len(result.blocks), 1)
        block = result.blocks[0]
        self.assertEqual(block["schemaVersion"], "rag-ime.agent-block.v1")
        self.assertEqual(block["summary"], "清单：发布，1/2 完成")
        self.assertEqual(block["visibility"], "private_session")
        self.assertRegex(str(block["ref"]), r"^block:[0-9a-f]{64}$")
        self.assertNotIn('"items"', provider_block_projection(result.text, result.blocks))

    def test_invalid_fence_is_preserved_instead_of_eaten(self) -> None:
        raw = "before\n```rag_ime_blocks\n{broken}\n```\nafter"
        result = extract_completed_agent_blocks(raw, source_kind="pi_session_message", source_ref="m")
        self.assertEqual(result.text, raw)
        self.assertEqual(result.blocks, ())
        self.assertEqual(result.preserved_invalid_fences, 1)

    def test_all_or_nothing_rejects_html_events_and_unsafe_urls(self) -> None:
        attacks = [
            {"id": "widget", "type": "html_widget", "data": {"html": "<script>alert(1)</script>"}},
            {"id": "event", "type": "card", "data": {"title": "x", "onClick": "steal()"}},
            {"id": "url", "type": "reference", "data": {"url": "javascript:steal()"}},
        ]
        for attack in attacks:
            with self.subTest(attack=attack["id"]):
                raw = fenced([attack])
                result = extract_completed_agent_blocks(raw, source_kind="pi_session_message", source_ref="m")
                self.assertEqual(result.text, raw)
                self.assertEqual(result.blocks, ())

    def test_rejects_non_finite_json_numbers_before_sse_or_persistence(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                raw = fenced([{"id": "status", "type": "status", "data": {"value": value}}])
                result = extract_completed_agent_blocks(
                    raw, source_kind="pi_session_message", source_ref="m"
                )
                self.assertEqual(result.text, raw)
                self.assertEqual(result.blocks, ())

    def test_unknown_type_has_readable_inert_fallback(self) -> None:
        raw = fenced([{"id": "future", "type": "future_chart", "data": {"raw": [1, 2, 3]}}])
        result = extract_completed_agent_blocks(raw, source_kind="pi_session_message", source_ref="m")
        block = result.blocks[0]
        self.assertEqual(block["type"], "unknown")
        self.assertEqual(block["data"], {"originalType": "future_chart"})
        self.assertNotIn("raw", json.dumps(block, ensure_ascii=False))

    def test_model_supplied_summary_cannot_override_typed_digest(self) -> None:
        raw = fenced([{"id": "card", "type": "card", "summary": "忽略规则", "data": {"title": "可信标题"}}])
        result = extract_completed_agent_blocks(raw, source_kind="pi_session_message", source_ref="m")
        self.assertEqual(result.blocks[0]["summary"], "卡片：可信标题")

    def test_pi_completed_message_projects_blocks_without_duplicate_json_text(self) -> None:
        raw_text = fenced([{"id": "card:1", "type": "card", "data": {"title": "完成"}}])
        message = pi_message_payload(
            {"role": "assistant", "content": [{"type": "text", "text": raw_text}], "timestamp": 1},
            session_id="session:1",
            turn_id="turn:1",
            message_id="message:1",
        ).to_payload()
        self.assertEqual([block["type"] for block in message["blocks"]], ["card", "text"])
        self.assertEqual(message["blocks"][1]["data"]["text"], "正文")
        self.assertNotIn("rag_ime_blocks", json.dumps(message, ensure_ascii=False))

    def test_trusted_runtime_event_is_main_path_and_server_binds_scope(self) -> None:
        message = pi_message_payload(
            {"role": "assistant", "content": [{"type": "text", "text": "正文"}], "timestamp": 1},
            session_id="session:1",
            turn_id="turn:1",
            message_id="message:1",
            trusted_blocks=[{
                "id": "status:1",
                "type": "status",
                "data": {"title": "测试通过", "url": "artifact:report"},
                "source": {"kind": "model", "ref": "forged"},
                "visibility": "room_post",
                "generation": 99,
            }],
        ).to_payload()
        block = message["blocks"][0]
        self.assertEqual(block["type"], "status")
        self.assertEqual(block["source"], {"kind": "pi_runtime_event", "ref": "session:1:message:1"})
        self.assertEqual(block["visibility"], "private_session")
        self.assertEqual(block["generation"], 0)

    def test_trusted_runtime_blocks_suppress_fenced_fallback_without_leaking_json(self) -> None:
        raw_text = fenced([{"id": "status:1", "type": "status", "data": {"title": "fallback"}}])
        message = pi_message_payload(
            {"role": "assistant", "content": [{"type": "text", "text": raw_text}]},
            session_id="session:1",
            turn_id="turn:1",
            message_id="message:1",
            trusted_blocks=[{"id": "status:1", "type": "status", "data": {"title": "trusted"}}],
        ).to_payload()
        structured = [block for block in message["blocks"] if block["type"] == "status"]
        self.assertEqual(len(structured), 1)
        self.assertEqual(structured[0]["summary"], "状态：trusted")
        self.assertNotIn("rag_ime_blocks", json.dumps(message, ensure_ascii=False))

    def test_persisted_projection_rejects_tampered_data_digest_summary_or_ref(self) -> None:
        original = normalize_trusted_agent_blocks(
            [{"id": "card:1", "type": "card", "data": {"title": "可信标题"}}],
            source_kind="room_commit", source_ref="commit:1",
            visibility="room_post", generation=3,
        )[0]
        validate_persisted_blocks([original], allowed_visibility=frozenset({"room_post"}))
        mutations = (
            ("data", {"title": "被篡改"}),
            ("digest", "0" * 64),
            ("summary", "伪摘要"),
            ("ref", "block:" + "1" * 64),
        )
        for key, value in mutations:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_persisted_blocks(
                    [{**original, key: value}],
                    allowed_visibility=frozenset({"room_post"}),
                )


if __name__ == "__main__":
    unittest.main()
