from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from rag_ime.memory_near_budget_evaluation import (
    NEAR_BUDGET_VERDICT_SCHEMA_VERSION,
    SqliteConnectionAudit,
    build_near_budget_case,
    memory_near_budget_messages,
    parse_near_budget_verdict,
    redacted_near_budget_summary,
)


class MemoryNearBudgetEvaluationTests(unittest.TestCase):
    def test_payload_is_deterministic_exact_length_and_orders_unique_markers(self) -> None:
        first = build_near_budget_case(180_000, seed="transport-control")
        second = build_near_budget_case(180_000, seed="transport-control")

        self.assertEqual(first.payload, second.payload)
        self.assertEqual(len(first.payload), 180_000)
        self.assertEqual(first.payload_sha256, second.payload_sha256)
        positions = [first.payload.index(marker) for marker in first.markers.values()]
        self.assertEqual(positions, sorted(positions))
        for marker in first.markers.values():
            self.assertEqual(first.payload.count(marker), 1)

    def test_prompt_never_discloses_expected_nonces_outside_payload(self) -> None:
        case = build_near_budget_case(180_000, seed="prompt-control")

        messages = memory_near_budget_messages(case)

        self.assertEqual([item["role"] for item in messages], ["system", "user"])
        self.assertEqual(messages[1]["content"], case.payload)
        self.assertIn(NEAR_BUDGET_VERDICT_SCHEMA_VERSION, messages[0]["content"])
        for nonce in case.nonces.values():
            self.assertNotIn(nonce, messages[0]["content"])

    def test_verdict_requires_all_three_exact_nonces_in_order(self) -> None:
        case = build_near_budget_case(180_000, seed="verdict-control")
        response = json.dumps(
            {
                "schemaVersion": NEAR_BUDGET_VERDICT_SCHEMA_VERSION,
                "beginNonce": case.nonces["begin"],
                "middleNonce": case.nonces["middle"],
                "endNonce": case.nonces["end"],
                "order": ["begin", "middle", "end"],
            }
        )

        verdict = parse_near_budget_verdict(response, case)

        self.assertTrue(verdict["passed"])
        with self.assertRaisesRegex(ValueError, "end marker nonce"):
            parse_near_budget_verdict(
                response.replace(case.nonces["end"], "0" * 24),
                case,
            )

    def test_public_summary_contains_receipts_but_not_payload_or_raw_nonces(self) -> None:
        case = build_near_budget_case(180_000, seed="summary-control")
        verdict = {
            "passed": True,
            "beginMatched": True,
            "middleMatched": True,
            "endMatched": True,
            "orderMatched": True,
        }
        response = {
            "receipt": {
                "provider": "openai-codex",
                "modelId": "gpt-5.6-luna",
                "thinkingLevel": "max",
                "contextWindow": 372_000,
                "inputChars": 180_451,
                "elapsedMs": 9_876,
                "transport": "gateway_internal_session",
                "usage": {"input": 90_000, "output": 40},
            }
        }

        summary = redacted_near_budget_summary(
            case,
            response=response,
            verdict=verdict,
            runtime_manifest_sha256="a" * 64,
            source_hashes={"memory_model_executor.py": "b" * 64},
            production_access={
                "guardPassed": True,
                "productionDatabaseOpened": False,
                "allFileConnectionsPrivate": True,
                "observedConnectionCount": 3,
            },
            production_file_identity_changed=True,
            expected_model_id="gpt-5.6-luna",
        )
        encoded = json.dumps(summary, ensure_ascii=False, sort_keys=True)

        self.assertTrue(summary["passed"])
        self.assertEqual(summary["payloadChars"], 180_000)
        self.assertNotIn(case.payload, encoded)
        for nonce in case.nonces.values():
            self.assertNotIn(nonce, encoded)

    def test_sqlite_connection_audit_allows_private_and_blocks_production(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memory-near-budget-") as temporary:
            private_root = Path(temporary) / "private"
            private_root.mkdir()
            private_db = private_root / "shadow.sqlite"
            production_db = Path(temporary) / "production.sqlite"
            production_db.touch()
            audit = SqliteConnectionAudit(
                private_root=private_root,
                production_db=production_db,
            )

            with audit:
                with sqlite3.connect(private_db) as conn:
                    conn.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY)")

            summary = audit.summary()
            self.assertTrue(summary["guardPassed"])
            self.assertTrue(summary["allFileConnectionsPrivate"])
            self.assertFalse(summary["productionDatabaseOpened"])

            blocked = SqliteConnectionAudit(
                private_root=private_root,
                production_db=production_db,
            )
            with blocked:
                with self.assertRaisesRegex(RuntimeError, "production SQLite"):
                    sqlite3.connect(production_db)
            self.assertEqual(blocked.summary()["blockedProductionAttemptCount"], 1)


if __name__ == "__main__":
    unittest.main()
