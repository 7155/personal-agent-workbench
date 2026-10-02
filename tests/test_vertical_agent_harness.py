from __future__ import annotations

import copy
import unittest

from rag_ime.vertical_agent_harness import (
    VerticalHarnessError,
    list_builtin_eval_suites,
    load_builtin_manifests,
    validate_vertical_manifest,
    verify_vertical_trace,
)
from rag_ime.trace_runtime import EvidenceRef, build_trace_envelope, make_span


class VerticalAgentHarnessTests(unittest.TestCase):
    def test_builtin_eval_catalog_is_bounded_and_contains_no_fixture_details(self) -> None:
        catalog = list_builtin_eval_suites()
        by_id = {item["suiteId"]: item for item in catalog}
        self.assertEqual(set(by_id), {"earth-research", "sgg", "zhanggui-wenshu"})
        self.assertEqual(by_id["earth-research"], {
            "suiteId": "earth-research", "suiteRevision": "source-grounding-v1",
            "displayName": "Earth Agent 来源使用验证（非选址计算验收）", "fixtureCount": 1,
            "capabilities": ["eval.ground_truth", "memory.recall", "rag.retrieval", "sandbox.self_test", "trace.emit"],
        })

        self.assertEqual(
            [by_id[suite_id] for suite_id in ("sgg", "zhanggui-wenshu")],
            [
                {
                    "suiteId": "sgg",
                    "suiteRevision": "fixture-v2",
                    "displayName": "SGG 示例垂直 Agent",
                    "fixtureCount": 1,
                    "capabilities": [
                        "eval.ground_truth",
                        "memory.recall",
                        "rag.retrieval",
                        "sandbox.self_test",
                        "trace.emit",
                    ],
                },
                {
                    "suiteId": "zhanggui-wenshu",
                    "suiteRevision": "fixture-v2",
                    "displayName": "掌柜问数",
                    "fixtureCount": 1,
                    "capabilities": [
                        "eval.ground_truth",
                        "memory.recall",
                        "rag.retrieval",
                        "sandbox.self_test",
                        "trace.emit",
                    ],
                },
            ],
        )
        serialized = repr(catalog)
        for forbidden in ("fixturePath", "requiredEvidenceIds", "sales-ledger.md"):
            self.assertNotIn(forbidden, serialized)
        self.assertNotIn("truth", {key for item in catalog for key in item})

    @staticmethod
    def _trace(manifest: dict[str, object], *, evidence: tuple[EvidenceRef, ...] = ()) -> dict[str, object]:
        names = manifest["traceRequirements"]["requiredSpanNames"]
        spans = tuple(
            make_span(
                span_id=f"span:{index}",
                name=name,
                started_at_ms=100 + index,
                ended_at_ms=101 + index,
            )
            for index, name in enumerate(names)
        )
        return build_trace_envelope(
            trace_id="trace:vertical:fixture-1",
            source_kind="vertical_agent",
            input_text="public fixture query",
            spans=spans,
            evidence=evidence,
            now_ms=100,
        ).to_dict()

    def test_builtin_sgg_and_zhanggui_manifests_validate(self) -> None:
        manifests = load_builtin_manifests()
        self.assertEqual(set(manifests), {"earth-research", "sgg", "zhanggui-wenshu"})
        for manifest in manifests.values():
            validate_vertical_manifest(manifest)
            self.assertEqual(manifest["sandbox"]["network"], "blocked")
            self.assertTrue(manifest["sandbox"]["productionWriteBlocked"])
            self.assertIn("trace.emit", manifest["capabilities"])
            self.assertTrue(manifest["ragChecks"]["required"])
            self.assertTrue(manifest["memoryChecks"]["required"])

    def test_fixture_manifests_declare_rag_evidence_provenance(self) -> None:
        manifests = load_builtin_manifests()

        self.assertEqual(
            manifests["sgg"]["fixtures"][0]["ragEvidence"],
            {
                "evidenceId": "knowledge:sales-ledger",
                "sourceKind": "knowledge",
                "sourceLane": "sandbox_knowledge",
                "sourceRef": "K-08fe67dcbc",
            },
        )
        self.assertEqual(
            manifests["zhanggui-wenshu"]["fixtures"][0]["ragEvidence"],
            {
                "evidenceId": "knowledge:orders-ledger",
                "sourceKind": "knowledge",
                "sourceLane": "sandbox_knowledge",
                "sourceRef": "K-0ff2373dd2",
            },
        )

    def test_manifest_self_test_and_fixture_truth_are_strictly_bound(self) -> None:
        base = load_builtin_manifests()["sgg"]
        mutations = {
            "missing selfTest field": ("selfTest", None),
            "fixture path escapes": ("fixturePath", "../outside.md"),
            "missing memory source": ("memory.sourceRef", None),
            "memory source is not fixture": ("memory.sourceRef", "memory://not-a-fixture"),
            "truth omits memory": ("truth", ["knowledge:sales-ledger"]),
        }
        for label, (field, value) in mutations.items():
            with self.subTest(label=label):
                candidate = copy.deepcopy(base)
                if field == "selfTest":
                    candidate.pop("selfTest")
                elif field == "fixturePath":
                    candidate["selfTest"]["fixturePath"] = value
                elif field == "memory.sourceRef":
                    if value is None:
                        candidate["selfTest"]["memory"].pop("sourceRef")
                    else:
                        candidate["selfTest"]["memory"]["sourceRef"] = value
                else:
                    candidate["fixtures"][0]["truth"]["requiredEvidenceIds"] = value
                with self.assertRaises(VerticalHarnessError):
                    validate_vertical_manifest(candidate)

    def test_duplicate_evidence_is_rejected_before_a_vertical_pass(self) -> None:
        manifest = load_builtin_manifests()["sgg"]
        valid = self._trace(
            manifest,
            evidence=(
                EvidenceRef(
                    "knowledge:sales-ledger",
                    "knowledge",
                    "K-08fe67dcbc",
                    source_lane="sandbox_knowledge",
                    evidence_stage="retrieval_output",
                ),
                EvidenceRef(
                    "memory:fixture:sgg:pricing-policy",
                    "memory",
                    "fixture://memory/sgg/pricing-policy",
                    source_lane="fixture_memory",
                    evidence_stage="memory_recall",
                ),
            ),
        )
        duplicate = copy.deepcopy(valid)
        duplicate["evidence"].append(dict(duplicate["evidence"][0]))
        with self.assertRaisesRegex(VerticalHarnessError, "trace contract invalid"):
            verify_vertical_trace(manifest, duplicate)

    def test_harness_verifies_deterministic_truth_and_rag_memory_trace_evidence(self) -> None:
        manifest = load_builtin_manifests()["sgg"]
        trace = self._trace(
            manifest,
            evidence=(
                EvidenceRef(
                    "knowledge:sales-ledger",
                    "knowledge",
                    "K-08fe67dcbc",
                    source_lane="sandbox_knowledge",
                    evidence_stage="retrieval_output",
                ),
                EvidenceRef(
                    "memory:fixture:sgg:pricing-policy",
                    "memory",
                    "fixture://memory/sgg/pricing-policy",
                    source_lane="fixture_memory",
                    evidence_stage="memory_recall",
                ),
            ),
        )

        report = verify_vertical_trace(manifest, trace)

        self.assertTrue(report["verified"])
        self.assertEqual(
            report["truth"]["matchedEvidenceIds"],
            ["knowledge:sales-ledger", "memory:fixture:sgg:pricing-policy"],
        )
        self.assertEqual(report["rag"]["evidenceCount"], 1)
        self.assertEqual(report["memory"]["evidenceCount"], 1)

    def test_vertical_trace_requires_vertical_agent_source_kind(self) -> None:
        manifest = load_builtin_manifests()["sgg"]
        candidate = self._trace(
            manifest,
            evidence=(
                EvidenceRef(
                    "knowledge:sales-ledger",
                    "knowledge",
                    "K-08fe67dcbc",
                    source_lane="sandbox_knowledge",
                    evidence_stage="retrieval_output",
                ),
                EvidenceRef(
                    "memory:fixture:sgg:pricing-policy",
                    "memory",
                    "fixture://memory/sgg/pricing-policy",
                    source_lane="fixture_memory",
                    evidence_stage="memory_recall",
                ),
            ),
        )
        candidate["sourceKind"] = "session"

        with self.assertRaisesRegex(VerticalHarnessError, "sourceKind.*vertical_agent"):
            verify_vertical_trace(manifest, candidate)

    def test_memory_evidence_requires_declared_kind_and_exact_source_ref(self) -> None:
        manifest = load_builtin_manifests()["sgg"]
        valid = self._trace(
            manifest,
            evidence=(
                EvidenceRef(
                    "knowledge:sales-ledger",
                    "knowledge",
                    "K-08fe67dcbc",
                    source_lane="sandbox_knowledge",
                    evidence_stage="retrieval_output",
                ),
                EvidenceRef(
                    "memory:fixture:sgg:pricing-policy",
                    "memory",
                    "fixture://memory/sgg/pricing-policy",
                    source_lane="fixture_memory",
                    evidence_stage="memory_recall",
                ),
            ),
        )
        for field, value in (
            ("sourceKind", "knowledge"),
            ("sourceRef", "fixture://memory/sgg/other-receipt"),
        ):
            with self.subTest(field=field):
                candidate = copy.deepcopy(valid)
                memory = next(
                    item
                    for item in candidate["evidence"]
                    if item["evidenceId"] == "memory:fixture:sgg:pricing-policy"
                )
                memory[field] = value
                with self.assertRaisesRegex(VerticalHarnessError, "Memory fixture evidence"):
                    verify_vertical_trace(manifest, candidate)

    def test_required_spans_must_be_completed_for_a_vertical_pass(self) -> None:
        manifest = load_builtin_manifests()["sgg"]
        valid = self._trace(
            manifest,
            evidence=(
                EvidenceRef(
                    "knowledge:sales-ledger",
                    "knowledge",
                    "K-08fe67dcbc",
                    source_lane="sandbox_knowledge",
                    evidence_stage="retrieval_output",
                ),
                EvidenceRef(
                    "memory:fixture:sgg:pricing-policy",
                    "memory",
                    "fixture://memory/sgg/pricing-policy",
                    source_lane="fixture_memory",
                    evidence_stage="memory_recall",
                ),
            ),
        )

        for status in ("running", "failed", "cancelled"):
            with self.subTest(status=status):
                candidate = copy.deepcopy(valid)
                next(
                    item
                    for item in candidate["spans"]
                    if item["name"] == "rag.retrieve"
                )["status"] = status
                with self.assertRaisesRegex(
                    VerticalHarnessError,
                    "required span must be completed",
                ):
                    verify_vertical_trace(manifest, candidate)

    def test_rag_evidence_must_match_declared_fixture_provenance(self) -> None:
        manifest = load_builtin_manifests()["sgg"]
        valid = self._trace(
            manifest,
            evidence=(
                EvidenceRef(
                    "knowledge:sales-ledger",
                    "knowledge",
                    "K-08fe67dcbc",
                    source_lane="sandbox_knowledge",
                    evidence_stage="retrieval_output",
                ),
                EvidenceRef(
                    "memory:fixture:sgg:pricing-policy",
                    "memory",
                    "fixture://memory/sgg/pricing-policy",
                    source_lane="fixture_memory",
                    evidence_stage="memory_recall",
                ),
            ),
        )

        for field, value in (
            ("sourceRef", "K-fake-source"),
            ("sourceLane", "participant_private"),
        ):
            with self.subTest(field=field):
                candidate = copy.deepcopy(valid)
                evidence = next(
                    item
                    for item in candidate["evidence"]
                    if item["evidenceId"] == "knowledge:sales-ledger"
                )
                evidence[field] = value
                with self.assertRaisesRegex(
                    VerticalHarnessError,
                    "RAG evidence provenance",
                ):
                    verify_vertical_trace(manifest, candidate)

    def test_missing_trace_rag_or_memory_evidence_fails_closed(self) -> None:
        manifest = load_builtin_manifests()["zhanggui-wenshu"]
        incomplete = build_trace_envelope(
            trace_id="trace:zhanggui:bad",
            source_kind="vertical_agent",
            input_text="fixture",
            spans=(make_span(span_id="span:input", name="agent.input", started_at_ms=1, ended_at_ms=2),),
            now_ms=1,
        ).to_dict()

        with self.assertRaisesRegex(VerticalHarnessError, "required span"):
            verify_vertical_trace(manifest, incomplete)

        complete_spans = self._trace(manifest)
        with self.assertRaisesRegex(VerticalHarnessError, "deterministic truth"):
            verify_vertical_trace(manifest, complete_spans)

        rag_only = self._trace(
            manifest,
            evidence=(
                EvidenceRef(
                    "knowledge:orders-ledger",
                    "knowledge",
                    "K-0ff2373dd2",
                    source_lane="sandbox_knowledge",
                    evidence_stage="retrieval_output",
                ),
                EvidenceRef(
                    "memory:fixture:zhanggui-wenshu:reporting-policy",
                    "memory",
                    "fixture://memory/zhanggui-wenshu/reporting-policy",
                    source_lane="fixture_memory",
                ),
            ),
        )
        with self.assertRaisesRegex(VerticalHarnessError, "Memory evidence"):
            verify_vertical_trace(manifest, rag_only)

    def test_generic_observation_refs_do_not_impersonate_retrieval_or_memory_receipts(self) -> None:
        manifest = load_builtin_manifests()["sgg"]
        generic = self._trace(
            manifest,
            evidence=(
                EvidenceRef(
                    "knowledge:sales-ledger",
                    "knowledge",
                    "K-08fe67dcbc",
                    source_lane="sandbox_knowledge",
                ),
                EvidenceRef(
                    "memory:fixture:sgg:pricing-policy",
                    "memory",
                    "fixture://memory/sgg/pricing-policy",
                    source_lane="fixture_memory",
                ),
            ),
        )

        with self.assertRaisesRegex(VerticalHarnessError, "RAG evidence stage"):
            verify_vertical_trace(manifest, generic)


if __name__ == "__main__":
    unittest.main()
