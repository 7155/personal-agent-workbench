from __future__ import annotations

import json
import base64
import hashlib
import tempfile
import unittest
from pathlib import Path

from rag_ime.agent_lab.golden import AgentLabGoldenStore
from rag_ime.agent_lab.knowledge import AgentLabKnowledgeResource, SCENE_ID
from rag_ime.agent_lab.knowledge_data import KnowledgeIntakeError, normalize_cases, normalize_documents
from rag_ime.agent_lab.trial_execution import AgentLabTrialApplication
from rag_ime.agent_lab.trials import AgentLabTrialConflict, AgentLabTrialStore


class KnowledgeDataTests(unittest.TestCase):
    def test_import_keeps_explicit_development_and_holdout_assignments(self):
        docs = normalize_documents([{'id': 'd1', 'text': 'First original paper'}, {'id': 'd2', 'text': 'Second original paper'}])
        cases, summary = normalize_cases([
            {'id': 'a', 'question': 'First?', 'article_ids': ['d1'], 'partition': 'development'},
            {'id': 'b', 'question': 'Second?', 'article_ids': ['d2'], 'partition': 'holdout'},
        ], docs, {'split': 'partition'})
        self.assertEqual([row['split'] for row in cases], ['development', 'holdout'])
        self.assertTrue(summary['providedSplit'])
        self.assertFalse(summary['officialSplit'])

    def test_import_rejects_leaked_supplied_splits_instead_of_silently_reassigning(self):
        docs = normalize_documents([{'id': 'original', 'text': 'Shared paper'}, {'id': 'alias', 'text': 'Shared paper'}])
        with self.assertRaisesRegex(KnowledgeIntakeError, '不会静默重分'):
            normalize_cases([
                {'question': 'First?', 'article_ids': ['original'], 'split': 'development'},
                {'question': 'Second?', 'article_ids': ['alias'], 'split': 'holdout'},
            ], docs)

    def test_original_question_answer_and_source_identity_survive_mapping(self):
        docs = normalize_documents([{"key": "原文/退款.md", "body": "Refunds take seven days.", "name": "Refunds"}],
                                   {"id": "key", "text": "body", "title": "name"})
        self.assertEqual(docs[0]["sourceId"], "原文/退款.md")
        self.assertTrue(docs[0]["externalId"].startswith("doc:"))
        cases, summary = normalize_cases([{"query": "  How long?\n", "reference": "Seven days.\n", "refs": ["原文/退款.md"]}], docs,
                                         {"question": "query", "answer": "reference", "sources": "refs"})
        self.assertEqual(cases[0]["question"], "  How long?\n")
        self.assertEqual(cases[0]["answer"], "Seven days.\n")
        self.assertFalse(summary["officialSplit"])

    def test_qa_cannot_be_misimported_as_corpus_and_missing_reference_is_rejected(self):
        with self.assertRaises(KnowledgeIntakeError):
            normalize_documents([{"id": "1", "question": "Q", "answer": "SECRET ANSWER"}])
        docs = normalize_documents([{"id": "d1", "contents": "Policy"}])
        with self.assertRaisesRegex(KnowledgeIntakeError, "参考来源不存在"):
            normalize_cases([{"question": "Q", "answer": "A", "article_ids": ["missing"]}], docs)

    def test_shared_source_or_duplicate_question_components_never_cross_splits(self):
        docs = normalize_documents([{"id": f"d{n}", "text": f"Document {n}"} for n in range(5)])
        rows = [{"id": "a", "question": "First question", "answer": "A", "article_ids": ["d0"]},
                {"id": "b", "question": "Second question", "answer": "B", "article_ids": ["d0", "d1"]},
                {"id": "c", "question": " SECOND   QUESTION ", "answer": "C", "article_ids": ["d2"]},
                {"id": "d", "question": "Last question", "answer": "D", "article_ids": ["d2"]}]
        cases, summary = normalize_cases(rows, docs)
        self.assertEqual(len({row["familyId"] for row in cases}), 1)
        self.assertEqual(len({row["split"] for row in cases}), 1)
        self.assertEqual(summary["familyCount"], 1)

    def test_missing_qrels_remains_unlabeled_instead_of_perfect_zero_cost_quality(self):
        docs = normalize_documents([{"id": "d", "text": "Document"}])
        cases, summary = normalize_cases([{"question": "Q", "answer": "A"}], docs)
        self.assertFalse(cases[0]["retrievalEvaluable"])
        self.assertEqual(summary["retrievalEvaluableCount"], 0)

    def test_identical_bodies_with_different_source_ids_stay_in_one_split_family(self):
        docs = normalize_documents([{"id": "original", "text": "Identical policy."},
                                    {"id": "alias", "text": "Identical policy."}])
        cases, summary = normalize_cases([
            {"question": "First question", "article_ids": ["original"]},
            {"question": "Different question", "article_ids": ["alias"]},
        ], docs)
        self.assertEqual(summary["familyCount"], 1)
        self.assertEqual(cases[1]["sourceIds"], ["alias"])


class KnowledgeResourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="paw-lab-knowledge-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = AgentLabTrialStore(self.root / "lab.sqlite")
        self.resource = AgentLabKnowledgeResource(self.root / "resources", read_trials=self.store.read)
        self.application = AgentLabTrialApplication(self.store, {SCENE_ID: self.resource}, start_workers=False)
        self.addCleanup(self.application.close)
        self.serial = 0
        self.project = {"projectId": "project-one", "title": "Support knowledge", "description": "Answer product support questions."}
        self.corpus_file = self.root / "corpus.jsonl"
        self.corpus_file.write_text("\n".join(json.dumps({"id": f"doc-{number}", "title": f"Policy {number}",
            "contents": f"The refundcode{number} policy permits returns within {number + 3} days."}) for number in range(20)))
        self.cases_file = self.root / "qa.jsonl"
        self.cases_file.write_text("\n".join(json.dumps({"id": f"question-{number}", "question": f"refundcode{number}",
            "answer": f"Returns are permitted within {number + 3} days.", "article_ids": [f"doc-{number}"]}) for number in range(20)))

    def run_operation(self, operation: str, **spec):
        self.serial += 1
        admitted = self.application.start(f"request-{self.serial}", SCENE_ID, {"projectId": self.project["projectId"], "operation": operation, **spec})
        result = self.application.run_job(admitted["job"]["jobId"])["job"]
        self.assertEqual(result["state"], "completed", result)
        return result["result"]

    def prepare_resources(self):
        corpus = self.run_operation("import_corpus", path=str(self.corpus_file))
        dataset = self.run_operation("import_dataset", corpusId=corpus["jobId"], path=str(self.cases_file))["dataset"]
        index = self.run_operation("index", corpusId=corpus["jobId"], embedding="none", chunking={"size": 400, "overlap": 40})
        return corpus, dataset, index

    def test_url_and_unicode_source_identities_reach_metrics_without_losing_qrels(self):
        urls = [f"https://developers.example.test/资料/api-{n}" for n in range(5)]
        self.corpus_file.write_text("\n".join(json.dumps({"id": url, "contents": f"uniqueapicode{n} reference."}) for n, url in enumerate(urls)))
        self.cases_file.write_text("\n".join(json.dumps({"id": f"企业问题/{n}", "question": f"uniqueapicode{n}", "answer": "Reference.", "sourceIds": [url]}) for n, url in enumerate(urls)))
        corpus, dataset, index = self.prepare_resources()
        found = self.run_operation("search", indexId=index["jobId"], query="uniqueapicode0", profile={"topK": 3})
        self.assertEqual(found["hits"][0]["sourceId"], urls[0])
        result = self.run_operation("evaluate", indexId=index["jobId"], datasetId=dataset["datasetId"], profile={"topK": 3}, split="development")
        self.assertEqual(result["report"]["metrics"]["metrics"]["recallAtK"]["3"], 1)
        self.assertEqual(result["evaluatedCount"], dataset["splits"]["development"])
        self.assertEqual(result["answerModelCalls"], 0)

    def test_real_knowledge_index_retrieval_metrics_and_reopen(self):
        corpus, dataset, index = self.prepare_resources()
        self.assertEqual(index["documentCount"], 20)
        self.assertEqual(index["chunkCount"], 20)
        self.assertFalse(index["dense"]["available"])
        self.assertEqual(dataset["caseCount"], 20)
        self.assertTrue(all(dataset["splits"].values()))
        found = self.run_operation("search", indexId=index["jobId"], query="refundcode7", profile={"topK": 3})
        self.assertEqual(found["hits"][0]["sourceId"], "doc-7")
        scored = self.run_operation("evaluate", indexId=index["jobId"], datasetId=dataset["datasetId"], profile={"topK": 3}, split="development")
        self.assertEqual(scored["plannedCount"], dataset["splits"]["development"])
        self.assertEqual(scored["evaluatedCount"], scored["plannedCount"])
        self.assertEqual(scored["answerModelCalls"], 0)
        self.assertEqual(scored["embeddingQueryCalls"], 0)
        self.assertIsNone(scored["providerCost"])
        self.assertEqual(scored["report"]["metrics"]["metrics"]["recallAtK"]["3"], 1)
        self.assertNotIn("privateCases", scored["report"])
        reopened = AgentLabKnowledgeResource(self.root / "resources", read_trials=self.store.read)
        observed = reopened.read(self.project["projectId"])
        self.assertEqual(observed["corpora"][0]["corpusHash"], corpus["corpusHash"])
        self.assertEqual(observed["indexes"][0]["jobId"], index["jobId"])
        self.assertEqual(len(observed["evaluations"]), 1)
        self.assertEqual(reopened.read("another-project")["indexes"], [])

    def test_admission_replay_and_reads_never_reexecute(self):
        spec = {"projectId": self.project["projectId"], "operation": "import_corpus", "path": str(self.corpus_file)}
        first = self.application.start("once", SCENE_ID, spec)
        self.application.run_job(first["job"]["jobId"])
        previous = self.store.read(first["job"]["jobId"])["job"]
        replay = self.application.start("once", SCENE_ID, spec)
        self.resource.read(self.project["projectId"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.store.read(first["job"]["jobId"])["job"], previous)
        with self.assertRaises(AgentLabTrialConflict):
            self.application.start("once", SCENE_ID, {**spec, "path": str(self.cases_file)})

    def test_article_title_with_path_separators_indexes_without_losing_original_title(self):
        self.corpus_file.write_text(json.dumps({"id": "delivery", "title": "Delivery/Pickup \\ Help",
                                              "contents": "Customers can choose a delivery date."}))
        corpus = self.run_operation("import_corpus", path=str(self.corpus_file))
        indexed = self.run_operation("index", corpusId=corpus["jobId"], embedding="none")
        found = self.run_operation("search", indexId=indexed["jobId"], query="delivery", profile={"topK": 3})
        self.assertEqual(indexed["documentCount"], 1)
        self.assertEqual(found["hits"][0]["title"], "Delivery/Pickup \\ Help")

    def test_duplicate_bodies_index_once_preserve_sources_and_score_canonical_identity(self):
        rows = [json.loads(line) for line in self.corpus_file.read_text().splitlines()]
        rows.append({**rows[0], "id": "alternate-source", "title": "Alternate policy URL", "url": "https://example.org/alternate"})
        self.corpus_file.write_text("\n".join(json.dumps(row) for row in rows))
        questions = [json.loads(line) for line in self.cases_file.read_text().splitlines()]
        questions[0]["article_ids"] = ["doc-0", "alternate-source"]
        self.cases_file.write_text("\n".join(json.dumps(row) for row in questions))
        corpus, dataset, index = self.prepare_resources()
        self.assertEqual(corpus["documentCount"], 21)
        self.assertEqual(index["sourceCount"], 21)
        self.assertEqual(index["documentCount"], 20)
        self.assertEqual(index["chunkCount"], 20)
        self.assertEqual(index["duplicateSourceCount"], 1)
        original = self.resource._cases(dataset)
        self.assertEqual(original[0]["sourceIds"], ["doc-0", "alternate-source"])
        found = self.run_operation("search", indexId=index["jobId"], query="refundcode0", profile={"topK": 3})
        self.assertEqual({row["sourceId"] for row in found["hits"][0]["sourceAliases"]}, {"doc-0", "alternate-source"})
        scored = self.run_operation("evaluate", indexId=index["jobId"], datasetId=dataset["datasetId"],
                                    profile={"topK": 3}, split=original[0]["split"])
        self.assertEqual(scored["report"]["metrics"]["metrics"]["recallAtK"]["3"], 1)
        golden = self.resource.golden_inputs(self.project, {"indexId": index["jobId"], "datasetId": dataset["datasetId"], "targetCount": 20})
        case = next(row for row in golden["importedCases"] if row["caseId"] == "question-0")
        self.assertEqual([row["sourceId"] for row in case["evidence"]], ["doc-0"])
        self.assertEqual(len(golden["sources"]), 20)

    def test_foreign_index_and_fake_semantic_selection_are_rejected_before_execution(self):
        corpus, _, index = self.prepare_resources()
        with self.assertRaises(KnowledgeIntakeError):
            self.application.start("foreign", SCENE_ID, {"projectId": "different", "operation": "search", "indexId": index["jobId"], "query": "Q"})
        with self.assertRaisesRegex(KnowledgeIntakeError, "没有语义向量"):
            self.application.start("dense", SCENE_ID, {"projectId": self.project["projectId"], "operation": "search", "indexId": index["jobId"], "query": "Q", "profile": {"mode": "dense"}})
        with self.assertRaisesRegex(KnowledgeIntakeError, "尚未配置语义"):
            self.application.start("embedding", SCENE_ID, {"projectId": self.project["projectId"], "operation": "index", "corpusId": corpus["jobId"], "embedding": "configured"})

    def test_imported_standards_remain_pending_and_solver_search_excludes_references(self):
        _, dataset, index = self.prepare_resources()
        value = self.resource.golden_inputs(self.project, {"indexId": index["jobId"], "datasetId": dataset["datasetId"], "targetCount": 4})
        store = AgentLabGoldenStore(self.root / "lab.sqlite", default_model={"provider": "test", "model": "test", "thinkingLevel": "low"})
        suite = store.command({"action": "create", "expectedRevision": 0, "clientRequestId": "golden", "input": value})["suite"]
        self.assertEqual(len(suite["cases"]), 4)
        self.assertEqual(suite["datasetProvenance"]["totalCases"], 20)
        self.assertEqual({row["split"] for row in suite["cases"]}, {"development", "holdout"})
        self.assertTrue(all(row["review"]["status"] == "pending" for row in suite["cases"]))
        self.assertTrue(all(sample["humanVerdict"] is None for row in suite["cases"] for sample in row["samples"]))
        sources = self.resource.answer_sources(suite["knowledge"], "refundcode7")
        self.assertEqual(sources[0]["sourceId"], "doc-7")
        self.assertNotIn("Returns are permitted", json.dumps(sources))
        self.assertLess(len(sources), index["documentCount"])

    def test_portable_search_uses_frozen_knowledge_owner_with_equal_ranking_and_no_labels(self):
        from rag_ime.agent_lab.app_knowledge_runtime import materialize, retrieve
        _, dataset, index = self.prepare_resources()
        package = self.resource.app_resources(self.project["projectId"], {"indexId": index["jobId"], "profile": {"topK": 3}})
        root = self.root / 'portable'
        materialize(root, package['files'])
        encoded = package['files']['knowledge/search-snapshot.json']
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn('Returns are permitted', encoded)
        self.assertNotIn('references.jsonl', encoded)
        self.assertNotIn('stored_path', encoded)
        for query in ('refundcode7', 'refundcode3 policy', 'policy'):
            original = self.resource.answer_sources({'projectId':self.project['projectId'], 'indexId':index['jobId'],
                'corpusHash':index['corpusHash'], 'configHash':index['configHash'], 'profile':{'topK':3}}, query)
            portable = retrieve(root,package['knowledge'],{'question':query})
            self.assertEqual([(row['sourceId'],row['chunkId'],row['text']) for row in original],
                             [(row['sourceId'],row['chunkId'],row['text']) for row in portable['sources']])
            self.assertEqual([row['citationNumber'] for row in original], [row['citationNumber'] for row in portable['sources']])
            self.assertEqual(portable['modelCalls'],0)
        self.assertEqual(len(self.resource._cases(dataset)),20)
        snapshot_path = root / package['knowledge']['snapshotFile']
        snapshot_path.write_text(snapshot_path.read_text() + ' ')
        with self.assertRaisesRegex(ValueError,'不一致'):
            retrieve(root,package['knowledge'],{'question':'refundcode7'})

    def test_portable_export_never_silently_downgrades_semantic_or_reranked_profiles(self):
        _, _, index = self.prepare_resources()
        for profile in ({'mode':'dense'}, {'rerank':True}):
            with self.assertRaisesRegex(KnowledgeIntakeError,'不能直接替换'):
                self.resource.app_resources(self.project['projectId'],{'indexId':index['jobId'],'profile':profile})

    def test_knowledge_app_freezes_small_rows_runs_real_retrieval_and_exports_independent_runtime(self):
        import io
        import sqlite3
        import sys
        import threading
        import time
        import uuid
        import zipfile
        from urllib.request import Request, urlopen
        from unittest.mock import patch
        from rag_ime.agent_lab.apps import AgentLabAppApplication, AgentLabAppStore
        from rag_ime.agent_lab.projects import AgentLabProjectStore
        from rag_ime.agent_lab.app_assets import asset_root
        from rag_ime.agent_lab.app_runtime import create_server
        from tests.test_agent_lab_apps import write_app, MODEL
        workspace = self.root / 'workspace'; workspace.mkdir()
        apps = AgentLabAppStore(self.root / 'lab.sqlite', freeze_knowledge=self.resource.app_resources)
        projects = AgentLabProjectStore(self.root / 'lab.sqlite',
            create_guide=lambda _conn,_project:{'sessionId':'guide','workspace':{'kind':'managed','path':str(workspace),'createdAtMs':1}},
            prepare_app=lambda conn,project,value:apps.prepare(conn,project,value,MODEL))
        self.project = projects.command({'action':'create','expectedRevision':0,'clientRequestId':'create-app-project',
                                         'input':{'description':'Knowledge App'}})['project']
        _, _, index = self.prepare_resources()
        source = write_app(workspace)
        spec = json.loads((source / 'app.json').read_text())
        spec['knowledge'] = {'indexId':index['jobId'], 'profile':{'topK':3,'contextChars':1000}}
        spec['actions'].append({**spec['actions'][0], 'id':'sources', 'kind':'retrieval'})
        (source / 'app.json').write_text(json.dumps(spec))
        app = projects.command({'action':'prepare_app','projectId':self.project['projectId'],
            'expectedRevision':self.project['revision'],'clientRequestId':'prepare-knowledge-app','input':{'directory':'app'}})['application']
        with sqlite3.connect(apps.db_path) as conn:
            saved = json.loads(conn.execute('SELECT payload_json FROM agent_lab_app_versions').fetchone()[0])
        self.assertNotIn('resourceFiles', saved)
        self.assertNotIn('base64', json.dumps(saved))
        # Frozen runtime and launchers are executable source, independent of
        # corpus size. Bound metadata without mistaking a longer launcher for
        # embedded corpus growth; arbitrary extra bootstrap files stay forbidden.
        self.assertEqual(set(saved['bootstrapFiles']), {'launch.py', 'Start.command', 'requirements-app.txt'})
        self.assertLess(len(json.dumps({key: value for key, value in saved.items()
                                       if key not in {'runtimeSource', 'bootstrapFiles'}})), 12000)
        public = apps.read({'appId':app['appId']})
        self.assertEqual(public['version']['spec']['knowledge']['documentCount'],20)
        self.assertNotIn('refundcode7', json.dumps(public))
        observed = []
        def complete(**value):
            observed.append(value)
            running = apps.call_input(value['request_id'])[0]
            self.assertEqual(running['state'],'running')
            self.assertEqual(running['result'],{})
            self.assertEqual(running['progress']['sources'][0]['sourceId'],'doc-7')
            value['on_progress']({'stage':'thinking'})
            value['on_progress']({'stage':'answering','text':'Partial output'})
            self.assertEqual(apps.call_input(value['request_id'])[0]['result'],{})
            return {'text':'Answer grounded in the retrieved evidence.', 'usage':{}}
        runtime = AgentLabAppApplication(apps, complete=complete, abort=lambda _id:None, start_workers=False)
        self.addCleanup(runtime.close)
        def invoke(action, serial):
            command = {'appId':app['appId'],'expectedRevision':app['revision'],'action':'invoke',
                       'clientRequestId':serial,'input':{'version':1,'actionId':action,'values':{'question':'refundcode7'}}}
            call = runtime.command(command)['call']
            self.assertEqual(runtime.command(command)['call']['callId'], call['callId'])
            runtime.run_call(call['callId']); runtime.run_call(call['callId'])
            return apps.call_input(call['callId'])[0]
        local = invoke('sources','retrieve-once')
        self.assertEqual(local['state'],'completed',local)
        self.assertEqual(local['result']['sources'][0]['sourceId'],'doc-7')
        self.assertEqual(local['result']['usage']['calls'],0); self.assertEqual(observed,[])
        answered = invoke('answer','answer-once')
        self.assertEqual(answered['state'],'completed',answered); self.assertEqual(len(observed),1)
        apps.update_progress(answered['callId'],{'stage':'answering','text':'late'})
        self.assertEqual(apps.call_input(answered['callId'])[0]['progress']['stage'],'completed')
        self.assertIn('refundcode7', observed[0]['prompt'])
        self.assertNotIn('refundcode19', observed[0]['prompt'])
        self.assertNotIn('Returns are permitted', observed[0]['prompt'])
        self.assertLessEqual(answered['result']['knowledge']['contextChars'],1000)
        download = apps.download({'appId':app['appId'],'version':1,'target':'standalone'})
        portable = self.root / 'standalone'; portable.mkdir()
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(download['base64']))) as archive:
            archive.extractall(portable)
            self.assertIn('knowledge/search-snapshot.json', archive.namelist())
            self.assertNotIn(str(workspace), archive.read('knowledge/search-snapshot.json').decode())
        with patch.object(sys,'path',[str(portable),*sys.path]):
            server = create_server(portable, '127.0.0.1', 0)
            worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
            try:
                base = f'http://127.0.0.1:{server.server_port}'
                rid = str(uuid.uuid4())
                request = Request(base+'/api/invoke', data=json.dumps({'requestId':rid,'actionId':'sources','input':{'question':'refundcode7'}}).encode(),headers={'Content-Type':'application/json'})
                with urlopen(request) as response: self.assertTrue(json.load(response)['ok'])
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    with urlopen(base+'/api/requests/'+rid) as response: record=json.load(response)['record']
                    if record['state'] != 'running': break
                    time.sleep(.02)
                self.assertEqual(record['state'],'completed',record)
                self.assertEqual(record['result']['sources'],local['result']['sources'])
                self.assertEqual(record['result']['usage']['calls'],0)
            finally:
                server.shutdown(); server.server_close(); worker.join()
                sys.modules.pop('knowledge_runtime',None)
        root = asset_root(apps.db_path, saved['assetKey'])
        (root / saved['spec']['knowledge']['snapshotFile']).write_text('{}')
        failed = invoke('answer','damaged-snapshot')
        self.assertEqual(failed['state'],'failed'); self.assertEqual(len(observed),1)
        self.assertIn('尚未调用回答模型',failed['error'])

    def test_interrupted_index_is_not_a_ready_generation(self):
        corpus = self.run_operation("import_corpus", path=str(self.corpus_file))
        admitted = self.application.start("unfinished-index", SCENE_ID, {"projectId": self.project["projectId"], "operation": "index", "corpusId": corpus["jobId"]})
        self.store.claim(admitted["job"]["jobId"])
        replacement = AgentLabTrialApplication(self.store, {SCENE_ID: self.resource}, start_workers=False)
        self.addCleanup(replacement.close)
        observed = self.resource.read(self.project["projectId"])
        self.assertEqual(observed["indexes"], [])
        self.assertEqual(observed["jobs"][0]["state"], "interrupted")

    def test_changed_corpus_fails_without_overwriting_the_previous_generation(self):
        corpus, _, index = self.prepare_resources()
        path = self.resource._path(corpus["jobId"]) / "corpus.jsonl"
        path.write_text(path.read_text() + "\n")
        with self.assertRaisesRegex(KnowledgeIntakeError, "快照发生变化"):
            self.resource.answer_sources({"projectId": self.project["projectId"], "indexId": index["jobId"],
                "corpusHash": index["corpusHash"], "configHash": index["configHash"], "profile": {}}, "refundcode7")

    def test_browser_upload_replays_chunks_checks_scope_and_seals_original_bytes(self):
        data = self.corpus_file.read_bytes() + b" " * (520 * 1024)
        value = {"operation": "upload_begin", "name": "original-corpus.jsonl", "bytes": len(data),
                 "sha256": hashlib.sha256(data).hexdigest()}
        first = self.resource.upload("project-one", "browser-upload", value)
        self.assertTrue(self.resource.upload("project-one", "browser-upload", value)["replayed"])
        upload_id = first["upload"]["uploadId"]
        for number, offset in enumerate(range(0, len(data), 512 * 1024)):
            command = {"operation": "upload_chunk", "uploadId": upload_id, "index": number,
                       "data": base64.b64encode(data[offset:offset + 512 * 1024]).decode()}
            self.resource.upload("project-one", f"chunk-{number}", command)
            self.assertTrue(self.resource.upload("project-one", f"chunk-{number}", command)["replayed"])
        with self.assertRaisesRegex(KnowledgeIntakeError, "不属于"):
            self.resource.upload("other-project", "seal", {"operation": "upload_seal", "uploadId": upload_id})
        sealed = self.resource.upload("project-one", "seal", {"operation": "upload_seal", "uploadId": upload_id})
        self.assertTrue(sealed["upload"]["ready"])
        self.assertEqual(self.resource._uploaded_path("project-one", upload_id).read_bytes(), data)
        imported = self.run_operation("import_corpus", uploadId=upload_id)
        self.assertEqual(imported["title"], "original-corpus.jsonl")
        self.assertEqual(imported["documentCount"], 20)


if __name__ == "__main__":
    unittest.main()
