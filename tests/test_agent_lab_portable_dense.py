from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from rag_ime.agent_lab import app_knowledge_runtime
from rag_ime.agent_lab.knowledge import AgentLabKnowledgeResource, SCENE_ID
from rag_ime.agent_lab.trials import AgentLabTrialStore
from rag_ime.agent_lab.trial_execution import AgentLabTrialApplication


class _Encoded:
    def __init__(self, value): self.value = value
    def tolist(self): return self.value


class _LocalModel:
    document_calls = 0
    query_calls = 0
    loads = []

    def __init__(self, model, **options): self.loads.append((model, options))
    def encode(self, texts, **_options):
        def vector(text): return [1.0, 0.0, 0.25] if 'ocean' in text.lower() or '海洋' in text else [0.0, 1.0, 0.25]
        if isinstance(texts, list):
            type(self).document_calls += len(texts)
            return _Encoded([vector(t) for t in texts])
        type(self).query_calls += 1
        return _Encoded(vector(texts))


class PortableDenseTests(unittest.TestCase):
    def test_local_cache_identity_is_portable_and_cancelled_import_writes_no_vectors(self):
        from rag_ime.embeddings import SentenceTransformerEmbeddingProvider
        from rag_ime.knowledge_library.dense import SqliteDenseIndex
        from rag_ime.knowledge_library.store import KnowledgeStore
        local = self.root / 'private-cache/models--example--frozen-model/snapshots' / ('b' * 40)
        local.mkdir(parents=True); (local / 'config.json').write_text('{"hidden_size":3}')
        self.profile.update(model=str(local)); self.profile.pop('modelRevision'); self.profile.pop('modelReference')
        _, _, exported, _ = self.prepare_package()
        manifest = exported['knowledge']
        self.assertEqual(manifest['embedding']['model'], 'example/frozen-model')
        self.assertEqual(manifest['embedding']['modelRevision'], 'b' * 40)
        self.assertNotIn(str(self.root), json.dumps(manifest))
        self.assertNotIn(str(self.root), exported['files']['knowledge/dense-snapshot.json'])
        search = json.loads(exported['files']['knowledge/search-snapshot.json'])
        dense = json.loads(exported['files']['knowledge/dense-snapshot.json'])
        database = self.root / 'cancelled/knowledge.sqlite'
        store = KnowledgeStore(database); store.import_search_snapshot(search)
        provider = SentenceTransformerEmbeddingProvider(model='example/frozen-model',
            model_reference='example/frozen-model', model_revision='b' * 40)
        index = SqliteDenseIndex(database, provider)
        before = _LocalModel.document_calls
        with self.assertRaises(InterruptedError):
            index.import_snapshot(dense, expected_base_id=search['base']['id'],
                expected_document_hashes={d['id']: d['sha256'] for d in search['documents']}, cancelled=lambda: True)
        self.assertEqual(index.status()['vectorCount'], 0)
        self.assertEqual(_LocalModel.document_calls, before)

    def test_missing_encoder_and_corrupt_projection_never_become_lexical_success_or_ready_index(self):
        _, index, exported, package = self.prepare_package()
        with patch.object(_LocalModel, 'encode', side_effect=RuntimeError('local encoder unavailable')):
            with self.assertRaises(RuntimeError):
                app_knowledge_runtime.retrieve(package, exported['knowledge'], {'question': 'ocean'})
            binding = {'projectId': 'project-one', 'indexId': index['jobId'], 'corpusHash': index['corpusHash'],
                       'configHash': index['configHash'], 'profile': exported['knowledge']['profile']}
            with self.assertRaisesRegex(ValueError, '不能回退'):
                self.resource.answer_sources(binding, 'ocean')
        with patch.object(_LocalModel, 'encode', return_value=_Encoded([1.0, 0.0])):
            with self.assertRaisesRegex(RuntimeError, 'dimensions'):
                app_knowledge_runtime.retrieve(package, exported['knowledge'], {'question': 'changed encoder dimensions'})
        # An internally inconsistent projection must fail even when its file hash
        # has been updated. The already completed source index remains usable.
        dense_path = package / 'knowledge/dense-snapshot.json'
        dense = json.loads(dense_path.read_text()); dense['chunks'][0]['vectorSha256'] = '0' * 64
        dense_path.write_text(json.dumps(dense))
        app = json.loads((package / 'app.json').read_text())
        app['knowledge']['dense']['snapshotSha256'] = hashlib.sha256(dense_path.read_bytes()).hexdigest()
        (package / 'app.json').write_text(json.dumps(app))
        corpus = self.run_operation('import_corpus', project='project-two', path=str(self.corpus_path))
        admitted = self.application.start('corrupt-restore', SCENE_ID, {'operation': 'restore_index',
            'projectId': 'project-two', 'corpusId': corpus['jobId'], 'packagePath': str(package)})['job']
        failed = self.application.run_job(admitted['jobId'])['job']
        self.assertEqual(failed['state'], 'failed')
        self.assertEqual(self.resource.read('project-two')['indexes'], [])
        self.assertEqual(len(self.resource.read('project-one')['indexes']), 1)

    def test_zip_restore_preserves_snapshot_and_export_rejects_unpinned_or_reranked_profile(self):
        _, index, exported, package = self.prepare_package()
        archive = self.root / 'exported.zip'
        with zipfile.ZipFile(archive, 'w') as zipped:
            for path in package.rglob('*'):
                if path.is_file(): zipped.write(path, path.relative_to(package))
        corpus = self.run_operation('import_corpus', project='zip-project', path=str(self.corpus_path))
        before = _LocalModel.document_calls
        restored = self.run_operation('restore_index', project='zip-project', corpusId=corpus['jobId'], packagePath=str(archive))
        self.assertEqual(_LocalModel.document_calls, before)
        self.assertEqual(restored['indexReuse']['packageHashes']['search'], exported['knowledge']['snapshotSha256'])
        with self.assertRaisesRegex(ValueError, '重排'):
            self.resource.app_resources('project-one', {'indexId': index['jobId'], 'profile': {'mode': 'hybrid', 'rerank': True}})
        self.profile.pop('modelRevision'); self.profile.pop('modelReference')
        unpinned = self.run_operation('index', corpusId=self.resource.read('project-one')['corpora'][0]['jobId'], embedding='configured')
        with self.assertRaisesRegex(ValueError, 'fixed local revision'):
            self.resource.app_resources('project-one', {'indexId': unpinned['jobId'], 'profile': {'mode': 'hybrid'}})

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        fake = types.ModuleType('sentence_transformers'); fake.SentenceTransformer = _LocalModel
        mocked = patch.dict(sys.modules, {'sentence_transformers': fake}); mocked.start(); self.addCleanup(mocked.stop)
        _LocalModel.document_calls = _LocalModel.query_calls = 0; _LocalModel.loads = []
        self.profile = {'provider': 'sentence-transformers', 'model': 'example/frozen-model',
            'modelReference': 'example/frozen-model', 'modelRevision': 'a' * 40, 'dimensions': 3, 'denseBackend': 'sqlite-exact'}
        self.store = AgentLabTrialStore(self.root / 'lab.sqlite')
        self.resource = AgentLabKnowledgeResource(self.root / 'resources', read_trials=self.store.read,
            settings=lambda: {'knowledgeLibrary': {'embedding': self.profile}})
        self.application = AgentLabTrialApplication(self.store, {SCENE_ID: self.resource}, start_workers=False)
        self.addCleanup(self.application.close)
        self.serial = 0
        self.corpus_path = self.root / 'corpus.jsonl'
        self.corpus_path.write_text('\n'.join(json.dumps(d) for d in [
            {'id': 'ocean-paper', 'title': 'Ocean', 'text': 'Ocean heat reaches the shelf. Observations measure the transport.'},
            {'id': 'ice-paper', 'title': 'Ice', 'text': 'Ice thickness changes the circulation. Simulation has limited forcing.'}]))

    def run_operation(self, operation, *, project='project-one', **values):
        self.serial += 1
        job = self.application.start(f'portable-{self.serial}', SCENE_ID, {'operation': operation, 'projectId': project, **values})['job']
        finished = self.application.run_job(job['jobId'])['job']
        self.assertEqual(finished['state'], 'completed', finished)
        return finished['result']

    def prepare_package(self):
        corpus = self.run_operation('import_corpus', path=str(self.corpus_path))
        index = self.run_operation('index', corpusId=corpus['jobId'], embedding='configured')
        exported = self.resource.app_resources('project-one', {'indexId': index['jobId'],
            'profile': {'mode': 'hybrid', 'topK': 2, 'contextChars': 4000}})
        package = self.root / 'package'; package.mkdir()
        for name, content in exported['files'].items():
            path = package / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(content)
        (package / 'app.json').write_text(json.dumps({'knowledge': exported['knowledge']}))
        return corpus, index, exported, package

    def test_dense_and_hybrid_export_match_lab_ranking_without_reembedding_documents(self):
        _, index, exported, package = self.prepare_package()
        before = _LocalModel.document_calls
        self.assertEqual(exported['knowledge']['dense']['vectorCount'], 2)
        for mode in ('dense', 'hybrid'):
            profile = {**exported['knowledge']['profile'], 'mode': mode}
            live = self.run_operation('search', indexId=index['jobId'], profile=profile, query='海洋 ocean')
            portable = app_knowledge_runtime.retrieve(package, {**exported['knowledge'], 'profile': profile}, {'question': '海洋 ocean'})
            self.assertEqual([h['chunkId'] for h in live['hits']], [h['chunkId'] for h in portable['retrievalHits']])
            self.assertEqual([h['content'] for h in live['hits']], [h['text'] for h in portable['sources']])
            self.assertEqual([h['score'] for h in live['hits']], [h['score'] for h in portable['retrievalHits']])
        self.assertEqual(_LocalModel.document_calls, before)
        self.assertTrue(all(options['local_files_only'] for _, options in _LocalModel.loads))
        self.assertTrue(all(options.get('revision') == 'a' * 40 for _, options in _LocalModel.loads))
        self.assertNotIn(str(self.root), json.dumps(exported['knowledge']))

    def test_restore_complete_projection_to_new_project_reuses_vectors_and_rejects_changed_corpus(self):
        _, _, exported, package = self.prepare_package()
        new_corpus = self.run_operation('import_corpus', project='project-two', path=str(self.corpus_path))
        before = (_LocalModel.document_calls, _LocalModel.query_calls)
        restored = self.run_operation('restore_index', project='project-two', corpusId=new_corpus['jobId'], packagePath=str(package))
        self.assertEqual((_LocalModel.document_calls, _LocalModel.query_calls), before)
        self.assertEqual(restored['documentCount'], 2)
        self.assertEqual(restored['chunkCount'], 2)
        self.assertEqual(restored['dense']['vectorCount'], 2)
        self.assertEqual(restored['indexReuse']['embeddingModelCalls'], 0)
        result = self.run_operation('search', project='project-two', indexId=restored['jobId'],
            profile=exported['knowledge']['profile'], query='海洋 ocean')
        self.assertEqual(result['hits'][0]['sourceId'], 'ocean-paper')
        self.corpus_path.write_text(self.corpus_path.read_text().replace('heat reaches', 'heat never reaches'))
        changed = self.run_operation('import_corpus', project='project-three', path=str(self.corpus_path))
        with self.assertRaises(ValueError):
            self.application.start('reject-changed', SCENE_ID, {'operation': 'restore_index', 'projectId': 'project-three',
                'corpusId': changed['jobId'], 'packagePath': str(package)})
