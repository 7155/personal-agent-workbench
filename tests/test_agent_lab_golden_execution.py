from __future__ import annotations

import copy
import hashlib
import json
import threading
import tempfile
import time
import unittest
from pathlib import Path

from rag_ime.agent_lab.golden_execution import (
    AgentLabGoldenApplication,
    AgentLabGoldenExecutionInterrupted,
)
from rag_ime.agent_lab.golden import GOLDEN_JUDGE_PROTOCOL_VERSION


MODEL = {"provider": "test", "model": "baseline", "thinkingLevel": "low", "prompt": "baseline prompt"}
JUDGE = {**MODEL, "model": "frozen-judge", "prompt": "frozen judge instructions"}
SOURCES = [{"sourceId": "source-1", "title": "真实文档", "kind": "document", "uri": "docs/source.md", "text": "Alpha is supported by the document."}]


def golden_case(case_id: str = "dev-1", split: str = "development") -> dict:
    return {
        "caseId": case_id, "question": f"{case_id} question", "taskType": "context_qa",
        "answerable": True, "requiredFacts": ["REFERENCE-FACT-SECRET"],
        "evidence": [{"sourceId": "source-1", "quote": "Alpha is supported"}],
        "rubric": ["RUBRIC-SECRET"], "split": split,
        "review": {"status": "approved", "note": "REVIEW-SECRET", "reviewedAtMs": 1},
        "samples": [
            {"sampleId": "positive", "answer": "supported answer", "category": "correct", "humanVerdict": "pass", "humanNote": "HUMAN-LABEL-SECRET"},
            {"sampleId": "negative", "answer": "bad answer", "category": "incorrect", "humanVerdict": "fail", "humanNote": "HUMAN-LABEL-SECRET"},
            {"sampleId": "boundary", "answer": "boundary answer", "category": "boundary", "humanVerdict": "uncertain", "humanNote": "HUMAN-LABEL-SECRET"},
        ],
    }


def suite() -> dict:
    return {"suiteId": "suite-1", "revision": 4, "targetCount": 2, "title": "Evidence QA", "scenario": "context_qa", "sources": copy.deepcopy(SOURCES), "cases": [golden_case(), golden_case("HOLDOUT-SECRET", "holdout")], "judgeConfig": copy.deepcopy(JUDGE)}


def snapshot() -> dict:
    return {**suite(), "snapshotId": "snapshot-1", "sourceRevision": 4, "version": 1, "judgeProtocolVersion": GOLDEN_JUDGE_PROTOCOL_VERSION, "calibration": {"ready": True, "judgeProtocolVersion": GOLDEN_JUDGE_PROTOCOL_VERSION}}


class FakeStore:
    def __init__(self, kind: str = "draft", input: dict | None = None) -> None:
        self.lock = threading.RLock()
        self.job = {"jobId": "job-1", "kind": kind, "state": "queued", "progress": "", "sessionId": "", "error": "", "result": None, "createdAtMs": 1, "updatedAtMs": 1}
        self.frozen = suite()
        self.snapshot = snapshot() if kind == "experiment" else None
        self.input = input or {"model": copy.deepcopy(MODEL)}
        self.cancel_requested = False
        self.finishes = 0

    def read(self, suite_id: str = "") -> dict:
        with self.lock:
            item = {**copy.deepcopy(self.frozen), "jobs": [copy.deepcopy(self.job)]}
            return {"ok": True, "items": [item], "suite": item}

    def command(self, payload: dict) -> dict:
        with self.lock:
            action = payload["action"]
            if action == "cancel":
                self.cancel_requested = True
                if self.job["state"] == "queued":
                    self.job["state"] = "cancelled"
            if action == "resume":
                if self.job["state"] != "interrupted":
                    raise ValueError("Only interrupted jobs resume")
                self.job["state"] = "queued"
                self.cancel_requested = False
            return {"ok": True, "suite": self.read()["suite"], "job": copy.deepcopy(self.job), "clientRequestId": payload.get("clientRequestId", "request-1"), "replayed": False}

    def job_input(self, job_id: str) -> dict:
        with self.lock:
            return {"job": copy.deepcopy(self.job), "suite": copy.deepcopy(self.frozen), "snapshot": copy.deepcopy(self.snapshot), "input": copy.deepcopy(self.input), "cancelRequested": self.cancel_requested}

    def begin_validation(self, job_id: str) -> dict:
        with self.lock:
            return self.job.setdefault('validationUse', {'snapshotId':self.snapshot['snapshotId'],'ordinal':1,'priorStartedRuns':0,'priorCompletedRuns':0,'reused':False,'scope':'local_lab_validation_entry','startedAtMs':1})

    def update_job(self, job_id: str, patch: dict) -> dict:
        with self.lock:
            if self.job["state"] not in {"completed", "failed", "cancelled", "interrupted"}:
                self.job.update(copy.deepcopy(patch))
            return copy.deepcopy(self.job)

    def finish_job(self, job_id: str, result: dict) -> dict:
        with self.lock:
            if self.job["state"] == "running" and not self.cancel_requested:
                self.finishes += 1
                self.job.update(state="completed", result=copy.deepcopy(result))
            return copy.deepcopy(self.job)

    def recover_interrupted_jobs(self) -> list[dict]:
        with self.lock:
            if self.job["state"] in {"queued", "running"}:
                self.job["state"] = "interrupted"
                return [copy.deepcopy(self.job)]
            return []


def response(text: str, *, cost: bool = True, status: str = "completed") -> dict:
    return {"text": text, "sessionId": "pi-session-1", "turnId": "pi-turn-1", "usage": {"inputTokens": 10, "outputTokens": 5, **({"costUsd": 0.002} if cost else {})}, "receipt": {"status": status, "source": "test-pi-receipt"}}


def task_data(prompt: str) -> dict:
    return json.loads(prompt.split("<TASK_DATA>\n", 1)[1].split("\n</TASK_DATA>", 1)[0])


class PiDouble:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.cache: dict[str, dict] = {}
        self.judge_unknown = False

    def __call__(self, **request: object) -> dict:
        request = dict(request)
        self.calls.append(request)
        request["on_session"]("pi-session-1")
        key = request["request_id"]
        if key in self.cache:
            return copy.deepcopy(self.cache[key])
        data = task_data(request["prompt"])
        if ":draft" in key:
            value = {"cases": [golden_case(), golden_case("case-2", "holdout")]}
        elif ":optimize:" in key:
            value = {"prompt": "Use only the supplied corpus and cite supporting evidence.", "model": "must-not-replace-model", "judgeConfig": {"model": "must-not-change-judge"}}
        elif ":judge:" in key or ":calibration:" in key:
            value = {"verdict": "uncertain" if self.judge_unknown else "fail" if "bad" in data["answer"] else "pass", "reason": "Grounded evaluation", "evidence": [{"sourceId": "source-1", "quote": "Alpha is supported"}]}
        else:
            value = None
        result = response(json.dumps(value) if value is not None else ("bad answer" if request["model"]["model"] == "baseline" else "supported answer"), cost=len(self.cache) != 1)
        self.cache[key] = copy.deepcopy(result)
        return result


class GoldenExecutionTests(unittest.TestCase):
    def test_changed_method_body_cannot_recover_under_old_hash_or_report_title_as_skill_gain(self):
        from rag_ime.agent_lab.golden import optimization_scope
        body = '# Frozen method\nKeep unsupported claims unknown.\n'
        method = {'kind': 'application_skill', 'title': 'Before', 'body': body,
                  'sha256': hashlib.sha256(body.encode()).hexdigest(), 'source': {'kind': 'inline'}}
        self.assertEqual(optimization_scope({**MODEL, 'applicationMethod': method},
            {**MODEL, 'applicationMethod': {**method, 'title': 'After'}}, False), 'repeat')
        store = FakeStore('experiment', {'snapshotId': 'snapshot-1', 'baseline': MODEL,
            'candidate': {**MODEL, 'applicationMethod': {**method, 'body': 'MUTATED'}},
            'optimizePrompt': False, 'maxCandidates': 1})
        pi = PiDouble()
        job = self.run_job(self.application(store, pi), store)
        self.assertEqual(job['state'], 'failed')
        self.assertEqual(pi.calls, [])

    def test_application_skill_bodies_reach_answers_with_bound_receipts_and_blind_judge(self):
        bodies = ['# Baseline method\nSummarize the supplied evidence.\n',
                  '# Candidate method\nCompare authors separately; preserve evidence gaps.\n']
        methods = [{'kind': 'application_skill', 'title': 'Method', 'body': body,
                    'sha256': hashlib.sha256(body.encode()).hexdigest(), 'source': {'kind': 'inline'}} for body in bodies]
        store = FakeStore('experiment', {'snapshotId': 'snapshot-1',
            'baseline': {**MODEL, 'applicationMethod': methods[0]},
            'candidate': {**MODEL, 'applicationMethod': methods[1]}, 'optimizePrompt': False, 'maxCandidates': 1})
        pi = PiDouble()
        job = self.run_job(self.application(store, pi), store)
        self.assertEqual(job['state'], 'completed')
        result = job['result']
        self.assertEqual(result['optimizationScope'], 'skill')
        comparison = result['applicationMethodComparison']
        self.assertTrue(comparison['changed'])
        self.assertIn('-Summarize the supplied evidence.', comparison['diff'])
        self.assertEqual(comparison['candidate']['sha256'], methods[1]['sha256'])
        for call in pi.calls:
            if ':answer:' in call['request_id']:
                index = int(':candidate:' in call['request_id'])
                self.assertIn('<application_method>\n' + bodies[index] + '\n</application_method>', call['prompt'])
                self.assertNotIn(bodies[1-index], call['prompt'])
                for secret in ('REFERENCE-FACT-SECRET', 'RUBRIC-SECRET', 'HUMAN-LABEL-SECRET'):
                    self.assertNotIn(secret, call['prompt'])
            elif ':judge:' in call['request_id']:
                self.assertNotIn('applicationMethod', call['prompt'])
                self.assertNotIn('<application_method>', call['prompt'])
        answers = [r for r in result['receipts'] if r['stage'] == 'answer']
        self.assertEqual(len(answers), 4)
        for receipt in answers:
            index = int(receipt['variant'] == 'candidate')
            self.assertEqual(receipt['applicationMethod']['sha256'], methods[index]['sha256'])
            self.assertNotIn('body', receipt['applicationMethod'])
            self.assertEqual(len(receipt['promptSha256']), 64)
        self.assertEqual(result['development']['cases'][0]['candidate']['applicationMethod']['sha256'], methods[1]['sha256'])

    def test_knowledge_answers_use_real_retriever_callback_and_never_receive_reference_corpus(self):
        store = FakeStore("experiment", {"snapshotId": "snapshot-1", "baseline": MODEL,
            "candidate": {**MODEL, "model": "candidate"}, "optimizePrompt": False, "maxCandidates": 1})
        binding = {"indexId": "index-1", "corpusHash": "frozen-corpus"}
        store.snapshot["knowledge"] = binding
        store.snapshot["calibration"].update(referenceAuthority="agent_assisted", labelAuthors={"human": 0, "agent": 3, "unrecorded": 0})
        pi = PiDouble()
        retrieval_calls = []
        def retrieve(bound, question):
            retrieval_calls.append((bound, question))
            return [{"sourceId": "retrieved-source", "title": "Retrieved", "text": "RETRIEVED-EVIDENCE-ONLY", "chunkId": "chunk-1", "uri": "kb://1"}]
        app = AgentLabGoldenApplication(store=store, complete=pi, abort=lambda _: None,
                                        start_workers=False, retrieve_knowledge=retrieve)
        self.addCleanup(app.close)
        job = self.run_job(app, store)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["result"]["executionMode"], "knowledge_qa")
        self.assertEqual(job["result"]["optimizationScope"], "model")
        self.assertEqual(job["result"]["knowledge"], binding)
        self.assertEqual(job["result"]["referenceAuthority"], "agent_assisted")
        self.assertEqual(job["result"]["labelAuthors"]["agent"], 3)
        answers = [request for request in pi.calls if ":answer:" in request["request_id"]]
        self.assertEqual(len(answers), 4)
        self.assertEqual(len(retrieval_calls), 2)
        for request in answers:
            data = task_data(request["prompt"])
            self.assertEqual(data["sources"][0]["text"], "RETRIEVED-EVIDENCE-ONLY")
            for secret in ("REFERENCE-FACT-SECRET", "RUBRIC-SECRET", "HUMAN-LABEL-SECRET", SOURCES[0]["text"]):
                self.assertNotIn(secret, request["prompt"])
        self.assertEqual(job["result"]["development"]["cases"][0]["baseline"]["retrieval"]["sourceCount"], 1)
        for request in pi.calls:
            if ":judge:" in request["request_id"]:
                sources = task_data(request["prompt"])["sources"]
                self.assertIn("RETRIEVED-EVIDENCE-ONLY", [source["text"] for source in sources])

    def test_knowledge_judge_accepts_quotes_from_the_actual_packet(self):
        store = self.experiment_store()
        store.snapshot["knowledge"] = {"indexId": "index-1", "corpusHash": "frozen"}
        pi = PiDouble()
        original = pi.__call__
        def complete(**request):
            result = original(**request)
            if ":judge:" in request["request_id"]:
                data = task_data(request["prompt"])
                self.assertIn("retrieved-source", [source["sourceId"] for source in data["sources"]])
                result["text"] = json.dumps({"verdict": "pass", "reason": "Supported by the actual retrieval packet", "evidence": [{"sourceId": "retrieved-source", "quote": "Packet evidence"}]})
            return result
        app = AgentLabGoldenApplication(store=store, complete=complete, abort=lambda _: None, start_workers=False,
            retrieve_knowledge=lambda *_: [{"sourceId": "retrieved-source", "title": "Actual packet", "text": "Packet evidence", "chunkId": "chunk-1", "uri": "kb://1"}])
        self.addCleanup(app.close)
        result = self.run_job(app, store)["result"]
        self.assertEqual(result["holdout"]["baselineMetrics"]["passRate"], 1.0)

    def test_missing_knowledge_retrieval_never_falls_back_to_reference_corpus(self):
        store = FakeStore("experiment", {"snapshotId": "snapshot-1", "baseline": MODEL,
            "candidate": MODEL, "optimizePrompt": False, "maxCandidates": 1})
        store.snapshot["knowledge"] = {"indexId": "missing", "corpusHash": "frozen"}
        pi = PiDouble()
        app = self.application(store, complete=pi)
        job = self.run_job(app, store)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(pi.calls, [])

    def test_knowledge_recovery_reuses_the_original_evidence_before_recovering_pi(self):
        store = FakeStore("experiment", {"snapshotId": "snapshot-1", "baseline": MODEL,
            "candidate": MODEL, "optimizePrompt": False, "maxCandidates": 1})
        store.snapshot["knowledge"] = {"indexId": "index-1", "corpusHash": "frozen"}
        pi, seen, interrupted = PiDouble(), set(), False
        def retrieve(_binding, question):
            self.assertNotIn(question, seen, "Recovery must not retrieve again for an accepted answer")
            seen.add(question)
            return [{"sourceId": "source-1", "text": "Original retrieved packet", "chunkId": "chunk-1"}]
        def complete(**request):
            nonlocal interrupted
            result = pi(**request)
            if not interrupted and ":answer:" in request["request_id"]:
                interrupted = True
                raise RuntimeError("Lost observation after Pi completed")
            return result
        app = AgentLabGoldenApplication(store=store, complete=complete, abort=lambda _: None,
                                        start_workers=False, retrieve_knowledge=retrieve)
        self.addCleanup(app.close)
        first = self.run_job(app, store)
        self.assertEqual(first["state"], "interrupted")
        self.assertEqual(len(first["result"]["knowledgePackets"]), 1)
        resumed = self.run_job(app, store)
        self.assertEqual(resumed["state"], "completed")
        self.assertEqual(len(seen), 2)

    def application(self, store: FakeStore, complete=None, abort=None, *, workers: bool = False) -> AgentLabGoldenApplication:
        app = AgentLabGoldenApplication(store=store, complete=complete or PiDouble(), abort=abort or (lambda session_id: None), start_workers=workers)
        self.addCleanup(app.close)
        return app

    def run_job(self, app: AgentLabGoldenApplication, store: FakeStore) -> dict:
        app.command({"action": "resume", "input": {"jobId": "job-1"}, "clientRequestId": "resume-1"})
        app.run_job("job-1")
        return store.job

    def test_draft_calls_pi_and_never_authors_human_review_or_labels(self) -> None:
        store, pi = FakeStore(), PiDouble()
        job = self.run_job(self.application(store, pi), store)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(len(pi.calls), 1)
        self.assertIn(SOURCES[0]["text"], pi.calls[0]["prompt"])
        for case in job["result"]["cases"]:
            self.assertEqual(case["review"]["status"], "pending")
            self.assertEqual({item["category"] for item in case["samples"]}, {"correct", "incorrect", "boundary"})
            self.assertTrue(all(item["humanVerdict"] is None and item["humanNote"] == "" for item in case["samples"]))

    def test_observed_draft_structure_normalizes_locally_from_one_settled_receipt(self) -> None:
        store, calls = FakeStore(), []
        cases = [golden_case(), golden_case("case-2", "holdout")]
        aliases = {"correct": "supported_answer", "incorrect": "contradictory_answer", "boundary": "scope_edge_answer"}
        for case in cases:
            case["rubric"] = "  保留原始评分标准全文。  "
            for sample in case["samples"]:
                sample["category"] = aliases[sample["category"]]
        original = copy.deepcopy(cases)
        settled = response(json.dumps({"cases": cases}, ensure_ascii=False))

        def same_receipt(**request):
            calls.append(request)
            return settled

        job = self.run_job(self.application(store, same_receipt), store)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(len(calls), 1)
        self.assertEqual(cases, original)
        self.assertEqual(len(job["result"]["normalizations"]), 8)
        for expected, actual in zip(original, job["result"]["cases"]):
            self.assertEqual(actual["rubric"], [expected["rubric"]])
            self.assertEqual(actual["requiredFacts"], expected["requiredFacts"])
            self.assertEqual(actual["evidence"], expected["evidence"])
            self.assertEqual([sample["answer"] for sample in actual["samples"]], [sample["answer"] for sample in expected["samples"]])
            self.assertEqual({sample["category"] for sample in actual["samples"]}, {"correct", "incorrect", "boundary"})
            self.assertTrue(all(sample["humanVerdict"] is None for sample in actual["samples"]))

    def test_draft_prompt_has_a_typed_example_with_exact_canonical_categories(self) -> None:
        store, pi = FakeStore(), PiDouble()
        self.run_job(self.application(store, pi), store)
        example = json.loads(pi.calls[0]["prompt"].split("<DRAFT_SCHEMA_EXAMPLE>\n", 1)[1].split("\n</DRAFT_SCHEMA_EXAMPLE>", 1)[0])
        case = example["cases"][0]
        self.assertIsInstance(case["rubric"], list)
        self.assertIsInstance(case["requiredFacts"], list)
        self.assertEqual({sample["category"] for sample in case["samples"]}, {"correct", "incorrect", "boundary"})

    def test_unknown_draft_category_is_not_guessed_or_retried(self) -> None:
        store, calls = FakeStore(), []
        cases = [golden_case(), golden_case("case-2", "holdout")]
        cases[0]["samples"][0]["category"] = "plausible_but_unrecognized"

        def invalid(**request):
            calls.append(request)
            return response(json.dumps({"cases": cases}))

        job = self.run_job(self.application(store, invalid), store)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(store.finishes, 0)
        self.assertEqual(len(calls), 1)

    def test_reprocess_only_draft_reads_the_original_receipt_without_calling_complete(self) -> None:
        store, complete_calls, read_calls = FakeStore(), [], []
        cases = [golden_case(), golden_case("case-2", "holdout")]
        for case in cases:
            case["rubric"] = "原评分标准"
            for sample, category in zip(case["samples"], ("supported_answer", "contradictory_answer", "scope_edge_answer")):
                sample["category"] = category
        saved = response(json.dumps({"cases": cases}))
        store.job["reprocessOnly"] = True
        store.job["result"] = {"receipts": [{"requestId": "job-1:draft", "stage": "draft", "sessionId": saved["sessionId"], "turnId": saved["turnId"], "usage": saved["usage"], "receipt": saved["receipt"]}]}

        def never_complete(complete_calls=complete_calls, saved=saved, **request):
            complete_calls.append(request)
            raise AssertionError("Reprocessing cannot issue a model call")

        def completed_result(**request):
            read_calls.append(request)
            return saved

        app = AgentLabGoldenApplication(store=store, complete=never_complete, abort=lambda session_id: None, completed_result=completed_result, start_workers=False)
        self.addCleanup(app.close)
        job = self.run_job(app, store)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(complete_calls, [])
        self.assertEqual(read_calls, [{"request_id": "job-1:draft", "model": MODEL}])
        self.assertEqual(job["result"]["usage"]["calls"], 1)
        self.assertEqual(job["result"]["cases"][0]["rubric"], ["原评分标准"])

    def test_reprocess_missing_receipt_stays_interrupted_and_never_falls_back_to_complete(self) -> None:
        for reader in (None, lambda **request: None, lambda **request: {}, lambda **request: response("not settled", status="failed")):
            with self.subTest(reader=reader):
                store, complete_calls = FakeStore(), []
                saved = response("previously settled text")
                store.job["reprocessOnly"] = True
                store.job["result"] = {"receipts": [{"requestId": "job-1:draft", "stage": "draft", "sessionId": saved["sessionId"], "turnId": saved["turnId"], "usage": saved["usage"], "receipt": saved["receipt"]}]}

                def never_complete(complete_calls=complete_calls, saved=saved, **request):
                    complete_calls.append(request)
                    return saved

                app = AgentLabGoldenApplication(store=store, complete=never_complete, abort=lambda session_id: None, completed_result=reader, start_workers=False)
                self.addCleanup(app.close)
                job = self.run_job(app, store)
                self.assertEqual(job["state"], "interrupted")
                self.assertEqual(complete_calls, [])
                self.assertEqual(job["result"]["receipts"][0]["requestId"], "job-1:draft")

    def test_experiment_reprocess_missing_cache_never_admits_a_replacement_call(self):
        store = self.experiment_store(optimize=False)
        store.job['reprocessOnly'] = True
        paid = []
        def complete(**request):
            paid.append(request)
            raise AssertionError('Reprocessing must remain read-only')
        app = AgentLabGoldenApplication(store=store, complete=complete, completed_result=lambda **_: None,
                                        abort=lambda _: None, start_workers=False)
        self.addCleanup(app.close)
        result = self.run_job(app, store)
        self.assertEqual(result['state'], 'interrupted')
        self.assertEqual(paid, [])

    def test_calibration_publishes_completed_sample_progress_while_calls_are_running(self) -> None:
        store, pi, observed = FakeStore("calibrate"), PiDouble(), []
        def complete(**request):
            observed.append(store.read()["suite"]["jobs"][0]["progress"])
            return pi(**request)
        job = self.run_job(self.application(store, complete), store)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(len(observed), 3)
        for count, progress in enumerate(observed):
            self.assertIn(f"{count} / 3", progress)

    def test_explicit_retry_reads_prior_success_and_only_admits_failed_and_remaining_calls(self) -> None:
        store, pi, paid = FakeStore('calibrate'), PiDouble(), []
        success = 'job-1:calibration:dev-1:positive'
        failed = 'job-1:calibration:dev-1:negative'
        cached = response(json.dumps({'verdict':'pass','reason':'Supported','evidence':[{'sourceId':'source-1','quote':'Alpha is supported'}]}))
        pi.cache[success] = cached
        store.job['requestRetries'] = {failed:{'requestId':failed+':retry:1','attempt':1}}
        store.job['result'] = {'receipts':[
            {'requestId':success,'stage':'calibration',**{key:cached[key] for key in ['sessionId','turnId','receipt','usage']}},
            {'requestId':failed,'stage':'calibration','sessionId':'failed-session','turnId':'failed-turn','receipt':{'status':'failed'},'usage':{'inputTokens':3}},
        ]}
        def complete(**request):
            if request['request_id'] not in pi.cache: paid.append(request['request_id'])
            return pi(**request)
        job = self.run_job(self.application(store, complete), store)
        self.assertEqual(job['state'],'completed')
        self.assertEqual(paid,[failed+':retry:1','job-1:calibration:dev-1:boundary'])
        self.assertEqual({row['requestId'] for row in job['result']['receipts']}, {success,failed,*paid})
        self.assertEqual(len(job['result']['judgments']),3)

    def test_calibration_only_judges_labeled_approved_development_without_label_leakage(self) -> None:
        store, pi = FakeStore("calibrate"), PiDouble()
        pending = golden_case("pending")
        pending["review"]["status"] = "pending"
        store.frozen["cases"].append(pending)
        job = self.run_job(self.application(store, pi), store)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(len(pi.calls), 3)
        self.assertEqual({item["caseId"] for item in job["result"]["judgments"]}, {"dev-1"})
        for call in pi.calls:
            self.assertEqual(call["model"], JUDGE)
            self.assertIn("The quotation requirement applies to YOUR evidence, not automatically to the answer", call['prompt'])
            self.assertIn('Accept a factually correct paraphrase', call['prompt'])
            for secret in ("HUMAN-LABEL-SECRET", "HOLDOUT-SECRET", '"humanVerdict"', '"category"', "REVIEW-SECRET"):
                self.assertNotIn(secret, call["prompt"])

    def experiment_store(self, *, optimize: bool = True) -> FakeStore:
        return FakeStore("experiment", {"snapshotId": "snapshot-1", "baseline": copy.deepcopy(MODEL), "candidate": {**MODEL, "model": "candidate"}, "optimizePrompt": optimize, "maxCandidates": 2})

    def test_experiment_freezes_judge_excludes_holdout_from_optimizer_and_compares_same_snapshot(self) -> None:
        store, pi = self.experiment_store(), PiDouble()
        # Live Golden edits cannot alter the admitted frozen snapshot.
        store.frozen["sources"][0]["text"] = "MUTATED-LIVE-GOLDEN"
        store.frozen["judgeConfig"]["model"] = "MUTATED-LIVE-JUDGE"
        job = self.run_job(self.application(store, pi), store)
        self.assertEqual(job["state"], "completed")
        result = job["result"]
        self.assertEqual(result["executionMode"], "context_qa")
        self.assertEqual(result["comparison"]["decision"], "improved")
        self.assertTrue(result["comparison"]["sameSnapshot"])
        self.assertFalse(result["comparison"]["goldenChanged"])
        self.assertEqual(result["judgeConfig"], JUDGE)
        self.assertEqual(result["candidate"]["model"], "candidate")
        optimize_calls = [call for call in pi.calls if ":optimize:" in call["request_id"]]
        self.assertEqual(len(optimize_calls), 2)
        for call in optimize_calls:
            self.assertNotIn("HOLDOUT-SECRET", call["prompt"])
            self.assertNotIn("HUMAN-LABEL-SECRET", call["prompt"])
        answer_calls = [call for call in pi.calls if ":answer:" in call["request_id"]]
        for call in answer_calls:
            self.assertEqual(task_data(call["prompt"])["sources"], SOURCES)
            for forbidden in ("REFERENCE-FACT-SECRET", "RUBRIC-SECRET", "HUMAN-LABEL-SECRET", "MUTATED-LIVE-GOLDEN"):
                self.assertNotIn(forbidden, call["prompt"])
        for call in pi.calls:
            if ":judge:" in call["request_id"]:
                self.assertEqual(call["model"], JUDGE)
                self.assertNotIn('"baseline"', call["prompt"])
                self.assertNotIn('"candidate"', call["prompt"])
        first_holdout = next(index for index, call in enumerate(pi.calls) if ":holdout:" in call["request_id"])
        self.assertTrue(all(":optimize:" not in call["request_id"] for call in pi.calls[first_holdout:]))
        self.assertEqual(len(result["holdout"]["cases"]), 1)
        self.assertIsNone(result["usage"]["costUsd"])
        self.assertGreater(result["usage"]["knownCostUsd"], 0)
        self.assertEqual(result["usage"]["totalTokens"], 15 * len(pi.cache))
        self.assertEqual(result["holdout"]["usageScope"], "answer_calls_only")
        self.assertEqual(result["holdout"]["baselineUsage"]["calls"], 1)
        self.assertEqual(result["holdout"]["candidateUsage"]["calls"], 1)
        self.assertEqual(result["usageByScope"]["optimization"]["calls"], 2)
        self.assertGreater(result["usageByScope"]["judging"]["calls"], 0)
        self.assertLess(result["holdout"]["candidateUsage"]["totalTokens"], result["usage"]["totalTokens"])

    def test_unknown_judge_never_becomes_a_passing_or_improved_experiment(self) -> None:
        store, pi = self.experiment_store(optimize=False), PiDouble()
        pi.judge_unknown = True
        job = self.run_job(self.application(store, pi), store)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["result"]["comparison"]["decision"], "inconclusive")
        self.assertFalse(job["result"]["comparison"]["comparable"])
        self.assertIsNone(job["result"]["holdout"]["candidateMetrics"]["passRate"])

    def test_equal_quality_lower_answer_cost_is_improvement_with_actual_or_estimate_basis(self) -> None:
        for estimated in (False, True):
            with self.subTest(estimated=estimated):
                store, pi = self.experiment_store(optimize=False), PiDouble()

                def priced(pi=pi, estimated=estimated, **request):
                    result = pi(**request)
                    if ":answer:" in request["request_id"]:
                        result["text"] = "supported answer"
                    price = 2.0 if request["model"]["model"] == "baseline" else 1.0
                    result["usage"] = {"inputTokens": 10, "outputTokens": 5, **({"estimatedCostUsd": price, "costBasis": "model_catalog_estimate"} if estimated else {"costUsd": price})}
                    return result

                result = self.run_job(self.application(store, priced), store)["result"]
                self.assertEqual(result["comparison"]["decision"], "improved")
                self.assertEqual(result["comparison"]["improvementBasis"], "answer_cost_estimate" if estimated else "answer_cost")
                costs = result["holdout"]["businessCost"]
                self.assertEqual(costs["basis"], "model_catalog_estimate" if estimated else "actual")
                self.assertEqual(costs["baselineUsd"], 2.0)
                self.assertEqual(costs["candidateUsd"], 1.0)
                self.assertEqual(costs["candidateCostPerSuccessUsd"], 1.0)
                if estimated:
                    self.assertIsNone(result["usage"]["costUsd"])
                    self.assertIsNotNone(result["usage"]["estimatedCostUsd"])

    def test_equal_aggregate_quality_and_lower_cost_cannot_hide_a_task_type_regression(self) -> None:
        store, pi = self.experiment_store(optimize=False), PiDouble()
        left, right = golden_case("holdout-A", "holdout"), golden_case("holdout-B", "holdout")
        left["taskType"], right["taskType"] = "grounding", "coverage"
        store.snapshot["cases"] = [golden_case(), left, right]

        def mixed(**request):
            result = pi(**request)
            baseline = request["model"]["model"] == "baseline"
            if ":answer:" in request["request_id"]:
                question = task_data(request["prompt"])["question"]
                fails = (question.startswith("holdout-A") and not baseline) or (question.startswith("holdout-B") and baseline)
                result["text"] = "bad answer" if fails else "supported answer"
            result["usage"] = {"inputTokens": 10, "outputTokens": 5, "costUsd": 2 if baseline else 1}
            return result

        result = self.run_job(self.application(store, mixed), store)["result"]
        self.assertEqual(result["comparison"]["holdoutDelta"], 0)
        self.assertLess(result["holdout"]["businessCost"]["deltaUsd"], 0)
        self.assertEqual(result["comparison"]["decision"], "no_improvement")
        self.assertEqual(result["comparison"]["groupRegressions"][0]["taskType"], "grounding")

    def test_judge_pass_without_valid_frozen_evidence_stays_unknown(self) -> None:
        store = FakeStore("calibrate")
        result = response(json.dumps({"verdict": "pass", "reason": "unsupported claim", "evidence": [{"sourceId": ["invalid-id"], "quote": "invented"}]}))
        job = self.run_job(self.application(store, lambda **request: result), store)
        self.assertEqual(job["state"], "completed")
        self.assertTrue(all(item["verdict"] == "uncertain" for item in job["result"]["judgments"]))

    def test_overlapping_development_and_holdout_are_rejected_before_any_model_call(self) -> None:
        store, pi = self.experiment_store(), PiDouble()
        store.snapshot["cases"][1]["question"] = store.snapshot["cases"][0]["question"]
        job = self.run_job(self.application(store, pi), store)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(pi.calls, [])

    def test_a_changed_judge_protocol_cannot_reuse_prior_calibration(self) -> None:
        store, pi = self.experiment_store(), PiDouble()
        store.snapshot["judgeProtocolVersion"] = "an-older-evaluator"
        job = self.run_job(self.application(store, pi), store)
        self.assertEqual(job["state"], "failed")
        self.assertIn("评审协议", job["error"])
        self.assertEqual(pi.calls, [])

    def test_runtime_failure_records_the_case_without_ever_starting_holdout(self) -> None:
        store, calls = self.experiment_store(), []

        def failed(**request):
            calls.append(request)
            return response("A model claim cannot prove success", status="failed")

        job = self.run_job(self.application(store, failed), store)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["result"]["caseRuns"][0]["status"], "runtime_error")
        self.assertTrue(job["result"]["partial"])
        self.assertFalse(any(":holdout:" in call["request_id"] for call in calls))

    def test_failed_answer_receipt_cost_is_retained_in_its_business_scope(self) -> None:
        class PaidFailure(RuntimeError):
            interrupted = False
            completion = response("", status="failed")

        store = self.experiment_store()

        def fail(**request):
            raise PaidFailure()

        job = self.run_job(self.application(store, fail), store)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["result"]["usageByScope"]["baselineAnswers"]["costUsd"], 0.002)
        self.assertEqual(job["result"]["caseRuns"][0]["judgment"]["verdict"], "uncertain")

    def test_adapter_interruption_attribute_and_generic_unknown_errors_remain_resumable(self) -> None:
        class AdapterError(RuntimeError):
            def __init__(self, interrupted):
                super().__init__("private provider details")
                self.interrupted = interrupted

        for error, expected in ((AdapterError(True), "interrupted"), (RuntimeError("unknown accepted outcome"), "interrupted"), (AdapterError(False), "failed")):
            with self.subTest(error=error):
                store = FakeStore()

                def fail(error=error, **request):
                    raise error

                job = self.run_job(self.application(store, fail), store)
                self.assertEqual(job["state"], expected)
                self.assertEqual(store.finishes, 0)
                self.assertNotIn("private provider details", job["error"])

    def test_explicit_failed_pi_receipt_and_empty_drafts_do_not_complete(self) -> None:
        for result in (response('{"cases": []}'), response('{"cases": []}', status="failed")):
            with self.subTest(result=result):
                store = FakeStore()
                job = self.run_job(self.application(store, lambda result=result, **kwargs: result), store)
                self.assertEqual(job["state"], "failed")
                self.assertEqual(store.finishes, 0)

    def test_missing_or_running_settlement_is_interrupted_not_accepted_as_success(self) -> None:
        for result in (response("looks successful", status="running"), {"text": "looks successful"}):
            with self.subTest(result=result):
                store = FakeStore()
                job = self.run_job(self.application(store, lambda result=result, **kwargs: result), store)
                self.assertEqual(job["state"], "interrupted")
                self.assertEqual(store.finishes, 0)

    def test_resume_reuses_exact_call_identity_and_input_after_unknown_admission(self) -> None:
        store, pi = FakeStore(), PiDouble()
        accepted = False

        def uncertain_once(**request):
            nonlocal accepted
            result = pi(**request)
            if not accepted:
                accepted = True
                raise AgentLabGoldenExecutionInterrupted("原 Pi 回合待确认")
            return result

        app = self.application(store, uncertain_once)
        self.run_job(app, store)
        self.assertEqual(store.job["state"], "interrupted")
        self.run_job(app, store)
        self.assertEqual(store.job["state"], "completed")
        self.assertEqual(len(pi.cache), 1)
        self.assertEqual(pi.calls[0]["request_id"], pi.calls[1]["request_id"])
        self.assertEqual(pi.calls[0]["model"], pi.calls[1]["model"])
        self.assertEqual(pi.calls[0]["prompt"], pi.calls[1]["prompt"])

    def test_async_cancel_aborts_actual_bound_session_and_rejects_late_success(self) -> None:
        store, pi = FakeStore(), PiDouble()
        bound, release, aborted = threading.Event(), threading.Event(), []

        def pending(**request):
            request["on_session"]("live-pi-session")
            bound.set()
            release.wait(2)
            return pi(**request)

        app = self.application(store, pending, aborted.append, workers=True)
        app.command({"action": "resume", "input": {"jobId": "job-1"}})
        self.assertTrue(bound.wait(2))
        app.command({"action": "draft", "input": {}})  # Duplicate admission cannot spawn a second worker.
        app.command({"action": "cancel", "input": {"jobId": "job-1"}})
        release.set()
        self.wait_state(store, {"cancelled"})
        self.assertIn("live-pi-session", aborted)
        self.assertEqual(store.finishes, 0)
        self.assertLessEqual(len(pi.cache), 1)

    def test_close_and_reload_preserve_interrupted_job_without_automatic_reexecution(self) -> None:
        store, pi = FakeStore(), PiDouble()
        bound, release = threading.Event(), threading.Event()

        def pending(**request):
            request["on_session"]("live-pi-session")
            bound.set()
            release.wait(2)
            return pi(**request)

        app = self.application(store, pending, workers=True)
        app.command({"action": "resume", "input": {"jobId": "job-1"}})
        self.assertTrue(bound.wait(2))
        app.close()
        release.set()
        self.wait_state(store, {"interrupted"})
        calls = len(pi.calls)
        reopened = self.application(store, pi)
        self.assertEqual(reopened.read()["suite"]["jobs"][0]["state"], "interrupted")
        self.assertEqual(len(pi.calls), calls)
        self.assertEqual(store.finishes, 0)

    def test_real_sqlite_store_draft_review_calibrate_freeze_and_experiment_seam(self) -> None:
        from rag_ime.agent_lab.golden import AgentLabGoldenStore

        with tempfile.TemporaryDirectory(prefix="paw-golden-execution-") as directory:
            store = AgentLabGoldenStore(Path(directory) / "test.sqlite", default_model=JUDGE)
            app = self.application(store, PiDouble())
            sequence = 0
            current = None

            def command(action: str, input: dict) -> dict:
                nonlocal sequence, current
                sequence += 1
                result = app.command({"action": action, "suiteId": current["suiteId"] if current else "", "expectedRevision": current["revision"] if current else 0, "clientRequestId": f"fixture-{sequence}", "input": input})
                current = result["suite"]
                if result["job"]:
                    app.run_job(result["job"]["jobId"])
                    current = store.read(current["suiteId"])["suite"]
                    self.assertEqual(next(job for job in current["jobs"] if job["jobId"] == result["job"]["jobId"])["state"], "completed")
                return result

            command("create", {"title": "Fixture-only Golden acceptance", "scenario": "context_qa", "targetCount": 2, "sources": SOURCES})
            command("draft", {"model": MODEL})
            self.assertTrue(all(case["review"]["status"] == "pending" for case in current["cases"]))
            for case in copy.deepcopy(current["cases"]):
                command("review_case", {**case, "verdict": "approved", "note": "Fixture human action, not production approval"})
            for sample in copy.deepcopy(current["cases"][0]["samples"]):
                verdict = {"correct": "pass", "incorrect": "fail", "boundary": "uncertain"}[sample["category"]]
                command("label_sample", {"caseId": current["cases"][0]["caseId"], "sampleId": sample["sampleId"], "answer": sample["answer"], "humanVerdict": verdict, "humanNote": "fixture human label"})
            command("calibrate", {})
            self.assertTrue(current["calibration"]["ready"])
            command("freeze", {})
            snapshot_id = current["snapshot"]["snapshotId"]
            result = command("experiment", {"snapshotId": snapshot_id, "baseline": MODEL, "candidate": {**MODEL, "model": "candidate"}, "optimizePrompt": True, "maxCandidates": 1})
            completed = next(job for job in current["jobs"] if job["jobId"] == result["job"]["jobId"])
            self.assertEqual(completed["result"]["snapshotId"], snapshot_id)
            self.assertEqual(completed["result"]["comparison"]["decision"], "improved")
            app.close()

    def wait_state(self, store: FakeStore, states: set[str]) -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and store.job["state"] not in states:
            time.sleep(0.005)
        self.assertIn(store.job["state"], states)


if __name__ == "__main__":
    unittest.main()
