from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from tests.frozen_rag_replay import run_frozen_standard_verifier


ROOT = Path(__file__).resolve().parents[1]
QRELS_V2 = ROOT / ".rag-ime-data/eval/host/validation-answer-evidence-qrels-v2.json"
STANDARD = (
    ROOT
    / "eval/interview-metrics/enterprise-rag-answer-evidence-standard.validation-candidate-citation-revision-r3.json"
)
QRELS = (
    ROOT
    / ".rag-ime-data/eval/host/validation-answer-evidence-qrels-candidate-citation-revision-r3.json"
)
RECEIPT = (
    ROOT
    / "eval/interview-metrics/runs/enterprise-rag-answer-evidence-standard-candidate-citation-revision-r3-calibration-20260905.v1.json"
)

EXPECTED_QRELS_V2_FILE_SHA256 = (
    "b0b081c4a0ecbced1c589b69126de555a0f1b8815c508e1f4eb269adce608428"
)
EXPECTED_ADDITIONS = {
    ("qst_0474", "F2"): {
        "documentId": "dsid_e22f3cff77dd4fd3bee54fa8c4a99aac",
        "chunkOrdinal": 3,
        "chunkSha256": "4e170501b9197220876472e219ce78d55df7701e866d5bea18c07b8b4a900c97",
        "quote": "- Managed cloud endpoints (major cloud providers): emphasize model-level optimizations (KV cache, continuous batching) and transparent unit economics vs opaque managed prices.",
    },
    ("qst_0474", "F3"): {
        "documentId": "dsid_e22f3cff77dd4fd3bee54fa8c4a99aac",
        "chunkOrdinal": 3,
        "chunkSha256": "4e170501b9197220876472e219ce78d55df7701e866d5bea18c07b8b4a900c97",
        "quote": "- Managed cloud endpoints (major cloud providers): emphasize model-level optimizations (KV cache, continuous batching) and transparent unit economics vs opaque managed prices.",
    },
    ("qst_0474", "F5"): {
        "documentId": "dsid_94b0d0306935482d820e38d0bd986139",
        "chunkOrdinal": 2,
        "chunkSha256": "65d11ed6ef3300b58a94c4b9c4c14e5324d73b3798de54dfc4ce602da3d9813b",
        "quote": "- Feature signals: (1) model family/architecture (Llama/Mistral/Qwen/Gemma/variants), (2) seq bucket / regime hints, (3) GPU SKU.",
    },
}


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fact_by_id(qrels: dict[str, object], query_id: str, fact_id: str) -> dict[str, object]:
    cases = qrels.get("cases")
    if not isinstance(cases, list):
        raise AssertionError("qrels cases are malformed")
    for case in cases:
        if not isinstance(case, dict) or case.get("queryId") != query_id:
            continue
        for fact in case.get("facts") or []:
            if isinstance(fact, dict) and fact.get("factId") == fact_id:
                return fact
    raise AssertionError(f"missing fact {query_id}/{fact_id}")


class EnterpriseRagCandidateCitationRevisionTests(unittest.TestCase):
    @unittest.skipUnless(all(path.is_file() for path in (QRELS_V2, QRELS)), "Private RAG corpus is not bundled in public source")
    def test_revision_is_append_only_and_only_adds_verified_candidate_chunks(self) -> None:
        self.assertEqual(EXPECTED_QRELS_V2_FILE_SHA256, _sha256(QRELS_V2))

        previous = _read(QRELS_V2)
        standard = _read(STANDARD)
        revised = _read(QRELS)

        self.assertEqual("rag-ime.rag-answer-evidence-standard.v2", standard["schemaVersion"])
        self.assertEqual("post-validation-calibrated", standard["calibrationLabel"])
        self.assertFalse(standard["candidateBlind"])
        self.assertFalse(standard["heldOutOpened"])
        self.assertFalse(standard["unbiasedPromotionClaimAllowed"])
        self.assertIn("candidate-citation", standard["calibrationSource"]["auditId"])
        self.assertNotEqual(previous["standardManifestSha256"], standard["manifestSha256"])
        self.assertEqual(standard, revised["answerEvidenceStandard"])
        self.assertEqual(standard["manifestSha256"], revised["standardManifestSha256"])
        self.assertEqual(EXPECTED_QRELS_V2_FILE_SHA256, revised["candidateOf"]["fileSha256"])
        self.assertEqual(previous["manifestSha256"], revised["candidateOf"]["manifestSha256"])

        changed: set[tuple[str, str]] = set()
        for query_id in ("qst_0474", "qst_0477"):
            fact_count = 5 if query_id == "qst_0474" else 4
            for index in range(1, fact_count + 1):
                fact_id = f"F{index}"
                old_fact = _fact_by_id(previous, query_id, fact_id)
                new_fact = _fact_by_id(revised, query_id, fact_id)
                old_groups = old_fact["supportGroups"]
                new_groups = new_fact["supportGroups"]
                if new_groups != old_groups:
                    changed.add((query_id, fact_id))
                    self.assertEqual(old_groups, new_groups[:-1])
                    self.assertEqual(1, len(new_groups[-1]["evidence"]))
                    binding = new_groups[-1]["evidence"][0]
                    expected = EXPECTED_ADDITIONS[(query_id, fact_id)]
                    for key, value in expected.items():
                        self.assertEqual(value, binding[key])
                    self.assertEqual(
                        hashlib.sha256(expected["quote"].encode("utf-8")).hexdigest(),
                        binding["quoteSha256"],
                    )

        self.assertEqual(set(EXPECTED_ADDITIONS), changed)
        self.assertEqual(_fact_by_id(previous, "qst_0474", "F1"), _fact_by_id(revised, "qst_0474", "F1"))
        self.assertEqual(
            next(case for case in previous["cases"] if case["queryId"] == "qst_0477"),
            next(case for case in revised["cases"] if case["queryId"] == "qst_0477"),
        )
        self.assertEqual(17, revised["counts"]["supportGroupCount"])
        self.assertEqual(17, revised["counts"]["evidenceBindingCount"])

    def test_public_receipt_is_hash_only_and_preserves_the_claim_boundary(self) -> None:
        receipt = _read(RECEIPT)

        self.assertEqual(0, receipt["providerCalls"])
        self.assertFalse(receipt["heldOutOpened"])
        self.assertFalse(receipt["unbiasedPromotionClaimAllowed"])
        self.assertFalse(receipt["candidateBlind"])
        self.assertTrue(receipt["auditBoundary"]["candidateCitationsAccessed"])
        self.assertEqual(3, receipt["calibrationDelta"]["addedExactBindings"])
        self.assertEqual(["qst_0474/F2", "qst_0474/F3", "qst_0474/F5"], receipt["calibrationDelta"]["changedFacts"])
        self.assertEqual(6, receipt["frozenCandidateRescore"]["coveredFactCount"])
        self.assertEqual(9, receipt["frozenCandidateRescore"]["factCount"])
        self.assertEqual("reject", receipt["frozenCandidateRescore"]["decision"])
        self.assertFalse(receipt["publicSafety"]["containsQuestions"])
        self.assertFalse(receipt["publicSafety"]["containsFactBodies"])
        self.assertFalse(receipt["publicSafety"]["containsEvidenceQuotes"])
        self.assertFalse(receipt["publicSafety"]["containsQrelsBody"])
        self.assertFalse(receipt["publicSafety"]["containsCandidateAnswers"])
        serialized = json.dumps(receipt, ensure_ascii=False)
        for addition in EXPECTED_ADDITIONS.values():
            self.assertNotIn(addition["quote"], serialized)

    @unittest.skipUnless(QRELS.is_file(), "Private RAG corpus is not bundled in public source")
    def test_dedicated_verifier_reproduces_the_fixed_point_offline(self) -> None:
        completed = run_frozen_standard_verifier(
            "verify_enterprise_rag_answer_evidence_candidate_citation_revision.py"
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual("ENTERPRISE_RAG_CANDIDATE_CITATION_REVISION_R3_OK", payload["event"])
        self.assertTrue(payload["runnerAcceptedCandidateAwareStandard"])
        self.assertEqual(0, payload["providerCalls"])
        self.assertEqual(17, payload["evidenceBindingCount"])
        self.assertEqual(6, payload["frozenCandidateCoveredFactCount"])


if __name__ == "__main__":
    unittest.main()
