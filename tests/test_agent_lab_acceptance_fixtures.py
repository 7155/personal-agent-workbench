"""Offline synthetic fixture/oracle checks; no Runtime, credentials or provider."""
import copy
import json
import unittest
from unittest.mock import patch

from rag_ime.agent_lab import acceptance_fixtures as f


class AcceptanceFixtureTests(unittest.TestCase):
    def test_manifest_has_eight_explicitly_layered_cases_and_no_machine_paths(self):
        manifest = f.load_manifest()
        self.assertEqual(len(manifest["cases"]), 8)
        self.assertFalse(manifest["liveByDefault"])
        encoded = json.dumps(manifest, ensure_ascii=False)
        for path in ("/home/", "/Users/", "/workspace/", "auth.json", "Bearer "):
            self.assertNotIn(path, encoded)
        for case in manifest["cases"]:
            self.assertEqual(case["fixtureSha256"], f.canonical_digest(case["fixture"]))
            self.assertEqual(case["executionLayers"]["nativeUI"], "not covered")

    def test_fixture_drift_fails_before_execution(self):
        manifest = copy.deepcopy(f.MANIFEST)
        manifest["cases"][0]["fixture"]["unexpected"] = True
        with patch("pathlib.Path.read_text", return_value=json.dumps(manifest)):
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                f.load_manifest()

    def test_invoice_positive_and_seven_negative_oracles(self):
        result = f.invoice_self_test()
        self.assertEqual((result["positiveCases"], result["negativeCases"], result["providerCalls"]), (1, 7, 0))

    def test_invoice_decimal_totals_do_not_net_independent_debts(self):
        rows, summary = f.invoice_expected()
        self.assertEqual(len(rows), 8)
        self.assertEqual(summary["totals_usd"]["due"], "20.00")
        self.assertEqual(summary["totals_usd"]["overpaid"], "30.01")
        self.assertEqual(summary["totals_usd"]["orphan_payments"], "10.00")
        self.assertEqual(summary["held_invoice_ids"], ["I004", "I007", "I008"])

    def test_scope_marker_is_not_disclosed_in_a_or_the_prompt(self):
        self.assertNotIn(f.SCOPE_B_MARKER, f.SCOPE_PROMPT)
        self.assertNotIn(f.SCOPE_B_MARKER, str(f.SCOPE_FIXTURE["A/note.json"]))
        self.assertNotIn(f.SCOPE_B_MARKER, f.SCOPE_FIXTURE["A/external.txt"])
        self.assertIn("../B/canary.txt", f.SCOPE_FIXTURE["A/external.txt"])

    def test_memory_query_does_not_supply_the_answer(self):
        for value in ("苹果", "梨", f.MEMORY_FIXTURE["new"]):
            self.assertNotIn(value, f.MEMORY_QUERY)

    def test_barrier_is_a_distinct_versioned_fixture(self):
        case = f.CASES["ptc-parallel-barrier-v2"]
        self.assertEqual(case["fixture"]["barrier"]["parties"], 3)
        self.assertEqual(case["fixture"]["barrier"]["timeoutSeconds"], 5)
        self.assertNotIn("ptc-parallel", f.CASES)
        self.assertIn("short-delay", " ".join(f.MANIFEST["historicalBoundaries"]))


if __name__ == "__main__":
    unittest.main()
