from __future__ import annotations

import json
import unittest

from rag_ime.memory_long_context_evaluation import (
    adjusted_filler_chars,
    build_transport_canary_payload,
    calibrated_filler_chars,
    canary_messages,
    input_tokens_from_usage,
    near_budget_checks,
    parse_and_verify_canary_output,
    redacted_marker_summary,
)


class MemoryLongContextEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.markers = {
            "begin": "B-private-begin",
            "middle": "M-private-middle",
            "end": "E-private-end",
        }

    def test_payload_places_three_markers_across_the_full_packet(self) -> None:
        payload, offsets = build_transport_canary_payload(
            self.markers,
            filler_chars=180_000,
        )

        self.assertGreater(offsets["middle"], 80_000)
        self.assertGreater(offsets["end"], 170_000)
        self.assertLess(offsets["begin"], offsets["middle"])
        self.assertLess(offsets["middle"], offsets["end"])
        messages = canary_messages(payload)
        self.assertEqual([item["role"] for item in messages], ["system", "user"])
        self.assertEqual(messages[1]["content"], payload)

    def test_output_must_be_one_exact_json_object(self) -> None:
        output = json.dumps(self.markers, separators=(",", ":"))
        passed = parse_and_verify_canary_output(output, self.markers)
        fenced = parse_and_verify_canary_output(f"```json\n{output}\n```", self.markers)
        missing = parse_and_verify_canary_output(
            json.dumps({"begin": self.markers["begin"]}),
            self.markers,
        )

        self.assertTrue(passed["passed"])
        self.assertFalse(fenced["passed"])
        self.assertFalse(missing["passed"])

    def test_public_marker_summary_never_contains_marker_values(self) -> None:
        summary = redacted_marker_summary(self.markers)
        encoded = json.dumps(summary, ensure_ascii=False)

        self.assertEqual(summary["count"], 3)
        for marker in self.markers.values():
            self.assertNotIn(marker, encoded)

    def test_calibration_and_adjustment_target_the_actual_usage(self) -> None:
        initial = calibrated_filler_chars(
            calibration_prompt_chars=25_000,
            calibration_input_tokens=15_000,
            target_input_tokens=165_000,
        )
        adjusted = adjusted_filler_chars(
            initial,
            110_000,
            target_input_tokens=165_000,
        )

        self.assertGreater(initial, 250_000)
        self.assertGreater(adjusted, initial)
        self.assertEqual(input_tokens_from_usage({"input": 165_001}), 165_001)
        self.assertEqual(input_tokens_from_usage({"inputTokens": 165_002}), 165_002)

    def test_new_checks_require_current_default_and_historical_checks_require_explicit_identity(self) -> None:
        options = dict(input_tokens=165_000, context_window=372_000, thinking_level="max", output_validation={"passed":True})
        self.assertTrue(all(near_budget_checks(model_reference="openai-codex/gpt-6.1-sol", **options).values()))
        self.assertFalse(near_budget_checks(model_reference="openai-codex/gpt-5.6-luna", **options)["actualProviderModel"])
        self.assertTrue(all(near_budget_checks(model_reference="openai-codex/gpt-5.6-luna", expected_model_reference="openai-codex/gpt-5.6-luna", **options).values()))

    def test_final_checks_require_real_luna_max_and_near_budget_usage(self) -> None:
        checks = near_budget_checks(
            input_tokens=165_000,
            context_window=372_000,
            model_reference="openai-codex/gpt-5.6-luna",
            expected_model_reference="openai-codex/gpt-5.6-luna",
            thinking_level="max",
            output_validation={"passed": True},
        )
        too_short = near_budget_checks(
            input_tokens=63_999,
            context_window=372_000,
            model_reference="openai-codex/gpt-5.6-luna",
            expected_model_reference="openai-codex/gpt-5.6-luna",
            thinking_level="max",
            output_validation={"passed": True},
        )

        self.assertTrue(all(checks.values()))
        self.assertFalse(too_short["inputBeyond64K"])
        self.assertFalse(too_short["inputNearBudget"])


if __name__ == "__main__":
    unittest.main()
