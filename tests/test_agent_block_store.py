from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rag_ime.agent_block_store import AgentBlockConflict, AgentBlockStore
from rag_ime.agent_blocks import normalize_trusted_agent_blocks


class AgentBlockStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "blocks.sqlite"
        self.store = AgentBlockStore(self.db_path)
        self.store.initialize()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def message(self, *, generation: int = 2) -> dict[str, object]:
        blocks = normalize_trusted_agent_blocks(
            [{"id": "table:1", "type": "table", "data": {"title": "结果", "columns": ["项"], "rows": [["raw"]]}}],
            source_kind="pi_runtime_event",
            source_ref="message:1",
            generation=generation,
        )
        return {
            "schemaVersion": "rag-ime.agent-message.v1",
            "id": "message:1",
            "sessionId": "session:1",
            "turnId": "turn:1",
            "role": "assistant",
            "status": "completed",
            "blocks": list(blocks),
            "attachments": [],
            "citations": [],
            "createdAtMs": 1,
        }

    def test_recent_hydration_uses_only_exact_ids_and_preserves_four_receipts(self) -> None:
        message = self.message()
        message["blocks"] = list(normalize_trusted_agent_blocks([
            {"id": f"file:{i}", "type": "file", "data": {"filename": "result.txt", "mediaId": f"media_public_{i:024d}", "sha256": str(i + 1) * 64, "bytes": 34 + i}}
            for i in range(4)
        ], source_kind="pi_runtime_event", source_ref="message:1", generation=2))
        self.store.persist_message(message, generation=2)
        runtime = {**message, "blocks": [{"type": "text", "data": {"text": "same answer"}}]}
        adjacent = {**message, "id": "adjacent", "blocks": []}
        foreign = {**runtime, "sessionId": "foreign"}
        with patch.object(self.store, "hydrate_messages", side_effect=AssertionError("full forbidden")), patch.object(self.store, "blocks_for_message", side_effect=AssertionError("per-message query forbidden")):
            actual = self.store.hydrate_recent_messages("session:1", [runtime, adjacent, foreign])
        self.assertEqual([item["id"] for item in actual], ["message:1", "adjacent", "message:1"])
        self.assertCountEqual([b["data"]["mediaId"] for b in actual[0]["blocks"] if b["type"] == "file"], [f"media_public_{i:024d}" for i in range(4)])
        self.assertEqual(actual[1:], [adjacent, foreign])

    def test_recent_does_not_guess_an_alias_from_equal_text_or_timestamp(self) -> None:
        message = self.message()
        message["id"] = "turn:1:assistant"
        self.store.persist_message(message, generation=2)
        runtime = {**message, "id": "native:unknown", "blocks": []}
        self.assertEqual(self.store.hydrate_recent_messages("session:1", [runtime]), [runtime])
        self.assertEqual(self.store.hydrate_recent_messages("session:1", []), [])

    def test_exact_alias_is_generation_bound_and_replay_cannot_rebind_it(self) -> None:
        message = self.message()
        self.store.persist_message(message, generation=2, native_message_id="native:1")
        self.store.persist_message(message, generation=2, native_message_id="native:1")
        newer = self.message(generation=3)
        self.store.persist_message(newer, generation=3)
        runtime = {**message, "id": "native:1", "blocks": []}
        actual = self.store.hydrate_recent_messages("session:1", [runtime])
        self.assertEqual(actual[0]["id"], "native:1")
        self.assertEqual(actual[0]["blocks"][0]["generation"], 2)
        full = self.store.hydrate_messages("session:1", [runtime])
        bound = next(message for message in full if message["id"] == "native:1")
        self.assertEqual(bound["blocks"][0]["generation"], 2)
        with self.assertRaisesRegex(AgentBlockConflict, "alias"):
            self.store.persist_message(newer, generation=3, native_message_id="native:1")
        alien = {**message, "id": "other:source"}
        with self.assertRaisesRegex(AgentBlockConflict, "alias"):
            self.store.persist_message(alien, generation=2, native_message_id="native:1")
        with self.store._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_block_message_envelopes WHERE message_id='other:source'").fetchone()[0], 0)
        self.assertEqual(self.store.hydrate_recent_messages("session:1", [runtime]), actual)

    def test_one_source_cannot_claim_two_native_entries(self) -> None:
        message = self.message()
        self.store.persist_message(message, generation=2, native_message_id="native:one")
        with self.assertRaisesRegex(AgentBlockConflict, "alias"):
            self.store.persist_message(message, generation=2, native_message_id="native:two")
        self.assertEqual(self.store.hydrate_recent_messages("session:1", [{**message, "id": "native:two", "blocks": []}])[0]["blocks"], [])

    def test_recent_alias_revoke_and_foreign_session_do_not_borrow_blocks(self) -> None:
        message = self.message()
        self.store.persist_message(message, generation=2, native_message_id="native:1")
        runtime = {**message, "id": "native:1", "blocks": []}
        self.assertEqual(self.store.hydrate_recent_messages("foreign", [runtime]), [runtime])
        rich_before_revoke = self.store.hydrate_recent_messages("session:1", [runtime])
        ref = str(self.store.blocks_for_message("session:1", "message:1")[0]["ref"])
        self.store.revoke(ref, root_id="session:session:1", session_id="session:1")
        self.assertEqual(self.store.hydrate_recent_messages("session:1", [runtime]), [runtime])
        self.assertEqual(self.store.hydrate_recent_messages("session:1", rich_before_revoke), [runtime])

    def test_recent_native_alias_is_scoped_to_each_original_session(self) -> None:
        first = self.message()
        self.store.persist_message(first, generation=2, native_message_id="same-native-id")
        second = {**self.message(), "sessionId": "session:other", "id": "other:source"}
        second["blocks"] = list(normalize_trusted_agent_blocks([{"id": "other", "type": "status", "data": {"title": "other original"}}], source_kind="pi_runtime_event", source_ref="other:source", generation=2))
        self.store.persist_message(second, generation=2, native_message_id="same-native-id")
        for message in (first, second):
            actual = self.store.hydrate_recent_messages(str(message["sessionId"]), [{**message, "id": "same-native-id", "blocks": []}])
            self.assertEqual([b["id"] for b in actual[0]["blocks"]], [message["blocks"][0]["id"]])

    def test_recent_batch_is_bounded_and_queries_only_returned_ids(self) -> None:
        original_connect = sqlite3.connect
        message = self.message()
        self.store.persist_message(message, generation=2)
        statements: list[str] = []
        steps: list[int] = []
        def traced(*args, **kwargs):
            conn = original_connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            conn.set_progress_handler(lambda: (steps.append(1), 0)[1], 1)
            return conn
        runtime = [{**message, "id": f"missing:{i}", "blocks": []} for i in range(95)] + [{**message, "blocks": []}]
        with patch("rag_ime.agent_block_store.sqlite3.connect", side_effect=traced):
            self.store.hydrate_recent_messages("session:1", runtime)
        baseline_steps = len(steps)
        with self.store._connect() as conn:
            # Adjacent history is deliberately large; it is not a recovery source.
            conn.executemany("INSERT INTO agent_block_message_envelopes SELECT session_id,?,generation,root_id,message_hash,message_json,created_at_ms FROM agent_block_message_envelopes WHERE message_id='message:1'", [(f"old:{i}",) for i in range(1000)])
        statements.clear()
        steps.clear()
        with patch("rag_ime.agent_block_store.sqlite3.connect", side_effect=traced):
            actual = self.store.hydrate_recent_messages("session:1", runtime)
        self.assertEqual(len(actual), 96)
        self.assertEqual(len(actual[-1]["blocks"]), 1)
        self.assertEqual(sum(statement.lstrip().upper().startswith(("SELECT", "WITH")) for statement in statements), 2)
        self.assertEqual(len(steps), baseline_steps)
        with patch.object(self.store, "_connect", side_effect=AssertionError("must not query excessive input")):
            self.assertEqual(self.store.hydrate_recent_messages("session:1", [*runtime, runtime[0]]), [*runtime, runtime[0]])

    def test_persists_rerender_data_and_projection_receipt(self) -> None:
        receipt = self.store.persist_message(
            self.message(), root_id="root:1", task_id="task:1",
            invocation_id="dispatch:1", generation=2, created_at_ms=10,
        )
        self.assertEqual(receipt["blockCount"], 1)
        self.assertGreater(receipt["beforeBytes"], receipt["afterBytes"])
        blocks = self.store.blocks_for_message("session:1", "message:1", generation=2)
        self.assertEqual(blocks[0]["data"]["rows"], [["raw"]])
        self.assertEqual(blocks[0]["visibility"], "private_session")

    def test_dedupe_conflict_revoke_and_cancel_generation(self) -> None:
        message = self.message()
        first = self.store.persist_message(message, root_id="root:1", generation=2, created_at_ms=10)
        replay = self.store.persist_message(message, root_id="root:1", generation=2, created_at_ms=11)
        self.assertEqual(first["projectionHash"], replay["projectionHash"])
        block_ref = str(self.store.blocks_for_message("session:1", "message:1", generation=2)[0]["ref"])
        self.assertFalse(self.store.revoke(block_ref, root_id="root:other", session_id="session:1", now_ms=12))
        self.assertTrue(self.store.revoke(block_ref, root_id="root:1", session_id="session:1", now_ms=12))
        self.assertEqual(self.store.blocks_for_message("session:1", "message:1"), [])

        second = self.message(generation=3)
        second["id"] = "message:2"
        second["blocks"][0]["id"] = "table:2"
        # New server identity must also carry a distinct ref/digest, so create it afresh.
        second["blocks"] = list(normalize_trusted_agent_blocks(
            [{"id": "table:2", "type": "table", "data": {"title": "结果", "columns": ["项"], "rows": [["raw"]]}}],
            source_kind="pi_runtime_event", source_ref="message:2", generation=3,
        ))
        self.store.persist_message(second, root_id="root:1", generation=3, created_at_ms=13)
        # Completed history remains rerenderable; Root cancellation fences stale
        # future writes rather than erasing already completed Session output.
        self.assertEqual(self.store.cancel_generation("root:1", 3, now_ms=14), 0)
        self.assertEqual(len(self.store.blocks_for_message("session:1", "message:2")), 1)

    def test_message_envelope_replay_is_atomic_and_cannot_partially_append(self) -> None:
        message = self.message()
        self.store.persist_message(message, root_id="root:1", generation=2, created_at_ms=10)
        expanded = json.loads(json.dumps(message))
        expanded["blocks"] = [
            *expanded["blocks"],
            *normalize_trusted_agent_blocks(
                [{"id": "status:2", "type": "status", "data": {"title": "late"}}],
                source_kind="pi_runtime_event", source_ref="message:1", generation=2,
            ),
        ]
        with self.assertRaisesRegex(AgentBlockConflict, "envelope"):
            self.store.persist_message(expanded, root_id="root:1", generation=2, created_at_ms=11)
        blocks = self.store.blocks_for_message("session:1", "message:1", generation=2)
        self.assertEqual([block["id"] for block in blocks], ["table:1"])

    def test_hydrates_runtime_messages_and_recovers_missing_compacted_envelope(self) -> None:
        message = self.message()
        text_block = {
            "id": "text:1", "type": "text", "status": "completed",
            "presentationKind": "markdown", "data": {"text": "可读结论"},
        }
        message["blocks"] = [text_block, *message["blocks"]]
        self.store.persist_message(message, root_id="root:1", generation=2, created_at_ms=10)

        hydrated = self.store.hydrate_messages("session:1", [{**message, "blocks": [text_block]}])
        self.assertEqual([block["type"] for block in hydrated[0]["blocks"]], ["text", "table"])
        recovered = self.store.hydrate_messages("session:1", [])
        self.assertEqual(recovered, hydrated)
        self.assertEqual(recovered[0]["blocks"][1]["data"]["rows"], [["raw"]])

        newer = {
            "schemaVersion": "rag-ime.agent-message.v1",
            "id": "message:2",
            "sessionId": "session:1",
            "turnId": "turn:2",
            "role": "assistant",
            "status": "completed",
            "blocks": [text_block],
            "attachments": [],
            "citations": [],
            "createdAtMs": 20,
        }
        merged = self.store.hydrate_messages("session:1", [newer])
        self.assertEqual([item["id"] for item in merged], ["message:1", "message:2"])

    def test_hydration_retains_unbound_legacy_receipt_without_guessing_native_alias(self) -> None:
        live = self.message()
        live["id"] = "turn:1:assistant"
        live["blocks"] = [
            {
                "id": "turn:1:assistant:text",
                "type": "text",
                "status": "completed",
                "presentationKind": "markdown",
                "data": {"text": "修复完成"},
            },
            *live["blocks"],
        ]
        # Equal text and nearby clocks are not a producer binding.
        live["createdAtMs"] = 95
        self.store.persist_message(
            live,
            root_id="root:1",
            generation=2,
            created_at_ms=95,
        )
        durable = {
            **live,
            "id": "pi:message:assistant:101",
            "turnId": "history:user:100",
            "blocks": [
                {
                    "id": "history:user:100:text:0",
                    "type": "text",
                    "status": "completed",
                    "presentationKind": "markdown",
                    "data": {"text": "修复完成"},
                }
            ],
            "createdAtMs": 101,
        }

        hydrated = self.store.hydrate_messages("session:1", [durable])

        self.assertEqual(len(hydrated), 2)
        self.assertEqual([item["id"] for item in hydrated], ["turn:1:assistant", "pi:message:assistant:101"])
        self.assertEqual([block["type"] for block in hydrated[0]["blocks"]], ["text", "table"])
        self.assertEqual(hydrated[1], durable)
        with self.store._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_block_native_aliases").fetchone()[0], 0)

    def test_hydration_preserves_runtime_order_when_timestamps_are_reversed_or_equal(self) -> None:
        first = {
            **self.message(),
            "id": "runtime:first",
            "blocks": [],
            "createdAtMs": 200,
        }
        second = {
            **self.message(),
            "id": "runtime:second",
            "blocks": [],
            "createdAtMs": 100,
        }
        third = {
            **self.message(),
            "id": "runtime:third",
            "blocks": [],
            "createdAtMs": 100,
        }

        hydrated = self.store.hydrate_messages(
            "session:1", [first, second, third]
        )

        self.assertEqual(
            [message["id"] for message in hydrated],
            ["runtime:first", "runtime:second", "runtime:third"],
        )

    def test_exact_replay_does_not_double_count_root_budget(self) -> None:
        message = self.message()
        first = self.store.persist_message(
            message, root_id="root:1", generation=2, created_at_ms=10
        )
        with patch(
            "rag_ime.agent_block_store.MAX_ROOT_BLOCK_BYTES", first["beforeBytes"]
        ):
            replay = self.store.persist_message(
                message, root_id="root:1", generation=2, created_at_ms=11
            )
        self.assertEqual(replay, first)

    def test_same_ref_cannot_be_rebound(self) -> None:
        message = self.message()
        self.store.persist_message(message, root_id="root:1", generation=2, created_at_ms=10)
        mutated = json.loads(json.dumps(message))
        mutated["blocks"] = list(normalize_trusted_agent_blocks(
            [{"id": "table:1", "type": "table", "data": {"title": "结果", "columns": ["项"], "rows": [["changed"]]}}],
            source_kind="pi_runtime_event", source_ref="session:1:message:1", generation=2,
        ))
        with self.assertRaises(AgentBlockConflict):
            self.store.persist_message(mutated, root_id="root:1", generation=2, created_at_ms=11)

    def test_same_local_id_is_isolated_across_sessions_for_equal_or_different_data(self) -> None:
        first = self.message()
        self.store.persist_message(first, root_id="root:1", generation=2, created_at_ms=10)
        for session_id, rows in (("session:2", [["raw"]]), ("session:3", [["different"]])):
            message_id = f"message:{session_id[-1]}"
            message = {
                **self.message(),
                "id": message_id,
                "sessionId": session_id,
                "blocks": list(normalize_trusted_agent_blocks(
                    [{"id": "table:1", "type": "table", "data": {"title": "结果", "columns": ["项"], "rows": rows}}],
                    source_kind="pi_runtime_event", source_ref=f"{session_id}:{message_id}", generation=2,
                )),
            }
            self.store.persist_message(message, root_id=f"root:{session_id[-1]}", generation=2, created_at_ms=11)
            self.assertEqual(self.store.blocks_for_message(session_id, message_id)[0]["data"]["rows"], rows)

        refs = {
            self.store.blocks_for_message(session, message)[0]["ref"]
            for session, message in (("session:1", "message:1"), ("session:2", "message:2"), ("session:3", "message:3"))
        }
        self.assertEqual(len(refs), 3)


if __name__ == "__main__":
    unittest.main()
