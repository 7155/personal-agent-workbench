from __future__ import annotations

import json
import io
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest.mock import patch

from rag_ime.agent_lab.app_runtime import AppInputError, AppProviderUnconfirmed, AppRequestStore, create_server, _stream_completion
from tests.test_agent_lab_apps import write_app


class StandaloneRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'app-state.sqlite3'
        self.store = AppRequestStore(self.path, 'frozen-source-a')
        self.request = {'requestId':str(uuid.uuid4()),'actionId':'consult','input':{'question':'第七天能退吗？'}}

    def tearDown(self): self.temp.cleanup()

    def test_parallel_admission_keeps_one_logical_call(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            receipts = list(pool.map(lambda _: self.store.claim(self.request), range(6)))
        self.assertEqual(sum(created for _, created in receipts), 1)
        self.assertEqual({value['requestId'] for value, _ in receipts}, {self.request['requestId']})

    def test_completed_result_survives_restart_without_new_call(self):
        self.store.claim(self.request)
        self.store.finish(self.request['requestId'], 'completed', {'text':'第7天仍在范围内。','usage':{'total_tokens':33}})
        restarted = AppRequestStore(self.path, 'frozen-source-a')
        record, created = restarted.claim(self.request)
        self.assertFalse(created)
        self.assertEqual(record['state'], 'completed')
        self.assertEqual(record['result']['text'], '第7天仍在范围内。')
        self.assertEqual(restarted.history()[0]['input'], self.request['input'])

    def test_unsettled_old_owner_is_unconfirmed_and_never_replayed(self):
        self.store.claim(self.request)
        restarted = AppRequestStore(self.path, 'frozen-source-a')
        record, created = restarted.claim(self.request)
        self.assertFalse(created)
        self.assertEqual(record['state'], 'unconfirmed')
        # The original process may still settle; a new reader accepts that fact.
        self.store.finish(self.request['requestId'], 'completed', {'text':'原调用已完成'})
        self.assertEqual(restarted.read(self.request['requestId'])['state'], 'completed')

    def test_reused_id_cannot_change_input_or_frozen_sources(self):
        self.store.claim(self.request)
        with self.assertRaises(AppInputError):
            self.store.claim({**self.request,'input':{'question':'另一个问题'}})
        with self.assertRaises(AppInputError):
            AppRequestStore(self.path, 'different-source').claim(self.request)

    def test_only_the_admitting_runtime_can_settle_and_terminal_receipts_stay_fixed(self):
        self.store.claim(self.request)
        other = AppRequestStore(self.path, 'frozen-source-a')
        self.assertFalse(other.finish(self.request['requestId'], 'completed', {'text':'wrong owner'}))
        self.assertTrue(self.store.finish(self.request['requestId'], 'completed', {'text':'first settlement'}))
        self.assertFalse(self.store.finish(self.request['requestId'], 'failed', {'message':'late overwrite'}))
        self.assertEqual(other.read(self.request['requestId'])['result']['text'], 'first settlement')

    def test_live_progress_does_not_become_a_result_or_allow_late_or_foreign_writes(self):
        rid = self.request['requestId']; self.store.claim(self.request)
        self.store.progress(rid,{'stage':'sources_ready','sources':[{'title':'Actual source'}],'thinking':'private'})
        self.store.progress(rid,{'stage':'answering','text':'Partial answer'})
        pending = self.store.read(rid)
        self.assertIsNone(pending['result']); self.assertEqual(pending['state'],'running')
        self.assertEqual(pending['progress']['sources'][0]['title'],'Actual source')
        self.assertNotIn('private',json.dumps(pending))
        other = AppRequestStore(self.path,'frozen-source-a')
        other.progress(rid,{'stage':'answering','text':'wrong owner'})
        self.assertEqual(other.read(rid)['progress']['stage'],'unconfirmed')
        self.assertEqual(other.read(rid)['progress']['text'],'Partial answer')
        self.store.finish(rid,'completed',{'text':'Final answer'})
        self.store.progress(rid,{'stage':'thinking','text':'late overwrite'})
        final = self.store.read(rid)
        self.assertEqual(final['progress']['stage'],'completed')
        self.assertEqual(final['result']['text'],'Final answer')

    def test_stream_shows_public_output_and_requires_completion_before_success(self):
        frames=[{'choices':[{'delta':{'reasoning_content':'private chain'}}]},
                {'choices':[{'delta':{'content':'First '}}]},
                {'choices':[{'delta':{'content':'answer'},'finish_reason':'stop'}]},
                {'choices':[],'usage':{'total_tokens':20}}]
        body=b''.join(('data: '+json.dumps(frame)+'\n\n').encode() for frame in frames)
        updates=[]
        result=_stream_completion(io.BytesIO(body+b'data: [DONE]\n'),updates.append)
        self.assertEqual(result['choices'][0]['message']['content'],'First answer')
        self.assertEqual(result['usage'],{'total_tokens':20})
        self.assertIn({'stage':'thinking'},updates)
        self.assertEqual(updates[-1],{'stage':'answering','text':'First answer'})
        self.assertNotIn('private chain',json.dumps(updates))
        with self.assertRaises(AppProviderUnconfirmed):
            _stream_completion(io.BytesIO(b'data: {"choices":[{"delta":{"content":"Partial"}}]}\n'),updates.append)
        self.assertEqual(updates[-1]['text'],'Partial')

    def test_call_limit_is_checked_before_creating_a_receipt(self):
        for _ in range(4): self.store.claim({**self.request,'requestId':str(uuid.uuid4())})
        with self.assertRaises(AppInputError): self.store.claim(self.request)
        self.assertIsNone(self.store.read(self.request['requestId']))
        self.assertEqual(len(self.store.history()), 4)

    def test_state_file_is_private_and_does_not_store_environment_credentials(self):
        self.store.claim(self.request)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('APP_API_KEY', json.dumps(self.store.history()))

    def test_local_server_rejects_foreign_host_even_when_origin_matches_it(self):
        root = write_app(Path(self.temp.name))
        with patch('rag_ime.agent_lab.app_runtime.provider_complete') as complete:
            server = create_server(root,'127.0.0.1',0)
            worker = threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                for path in ['/','/api/history']:
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(Request(base+path,headers={'Host':f'foreign.test:{server.server_port}'}),timeout=3)
                    self.assertEqual(caught.exception.code,403)
                    caught.exception.close()
                headers = {'Host':f'foreign.test:{server.server_port}',
                           'Origin':f'http://foreign.test:{server.server_port}','Content-Type':'application/json'}
                request = {**self.request,'actionId':'answer'}
                with self.assertRaises(HTTPError) as caught:
                    urlopen(Request(base+'/api/invoke',data=json.dumps(request).encode(),headers=headers,method='POST'),timeout=3)
                self.assertEqual(caught.exception.code,403);caught.exception.close()
                with urlopen(base+'/api/history',timeout=3) as response: self.assertEqual(json.load(response)['records'],[])
                with urlopen(base,timeout=3) as response: self.assertEqual(response.headers['X-Frame-Options'],'DENY')
                complete.assert_not_called()
            finally:
                server.shutdown();server.server_close();worker.join(timeout=3)

    def test_research_without_pi_never_falls_back_to_direct_completion(self):
        root = write_app(Path(self.temp.name))
        spec = json.loads((root / 'app.json').read_text())
        spec['workflow'] = {'kind': 'adaptive_research'}
        (root / 'app.json').write_text(json.dumps(spec))
        with patch.dict('os.environ', {'APP_API_KEY': 'fixture', 'APP_API_BASE_URL': 'https://invalid.example/v1'}, clear=True), \
             patch('rag_ime.agent_lab.app_runtime.provider_complete') as complete:
            server = create_server(root, '127.0.0.1', 0)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                with urlopen(base + '/health', timeout=3) as response:
                    health = json.load(response)
                self.assertFalse(health['configured'])
                self.assertEqual(health['transport'], 'paw_pi_required')
                request = {**self.request, 'actionId': 'answer'}
                with urlopen(Request(base + '/api/invoke', data=json.dumps(request).encode(),
                                     headers={'Content-Type': 'application/json'}), timeout=3) as response:
                    json.load(response)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    with urlopen(base + '/api/requests/' + request['requestId'], timeout=3) as response:
                        record = json.load(response)['record']
                    if record['state'] != 'running':
                        break
                    time.sleep(.01)
                self.assertEqual(record['state'], 'failed')
                self.assertIn('APP_PAW_GATEWAY_URL', record['message'])
                complete.assert_not_called()
            finally:
                server.shutdown(); server.server_close(); worker.join(timeout=3)

    def test_exported_workspace_shell_keeps_service_separate_and_conversation_same_origin(self):
        root = write_app(Path(self.temp.name)); spec_path = root/'app.json'
        spec = json.loads(spec_path.read_text()); spec['externalWorkspace'] = {'title':'空间工作台','url':'http://127.0.0.1:5173/'}
        spec_path.write_text(json.dumps(spec))
        with patch('rag_ime.agent_lab.app_runtime.provider_complete') as complete:
            server = create_server(root,'127.0.0.1',0)
            worker = threading.Thread(target=server.serve_forever,daemon=True); worker.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                with urlopen(base,timeout=3) as response:
                    page = response.read().decode()
                    self.assertEqual(response.headers['X-Frame-Options'],'DENY')
                    self.assertIn('<title>'+spec['title']+'</title>',page)
                    self.assertIn('src="/conversation"',page)
                    self.assertIn('data-src="http://127.0.0.1:5173/"',page)
                    self.assertNotRegex(page,r'window\.pawApp\s*=')
                with urlopen(base+'/conversation',timeout=3) as response:
                    self.assertEqual(response.headers['X-Frame-Options'],'SAMEORIGIN')
                    self.assertRegex(response.read().decode(),r'window\.pawApp\s*=')
                complete.assert_not_called()
            finally:
                server.shutdown(); server.server_close(); worker.join(timeout=3)

    def test_workspace_deployment_override_does_not_modify_frozen_source(self):
        root = write_app(Path(self.temp.name)); path = root/'app.json'
        spec = json.loads(path.read_text()); spec['externalWorkspace'] = {'title':'地图','url':'http://127.0.0.1:5173/', 'presentation':'split'}
        path.write_text(json.dumps(spec)); frozen = path.read_bytes()
        with patch.dict('os.environ', {'APP_WORKSPACE_URL':'http://127.0.0.1:18875/'}):
            server = create_server(root,'127.0.0.1',0)
            worker = threading.Thread(target=server.serve_forever,daemon=True); worker.start()
            try:
                with urlopen(f'http://127.0.0.1:{server.server_port}',timeout=3) as response:
                    page = response.read().decode()
                    self.assertIn('data-src="http://127.0.0.1:18875/"',page)
                    self.assertIn('data-presentation="split"',page)
                self.assertEqual(path.read_bytes(),frozen)
            finally:
                server.shutdown(); server.server_close(); worker.join(timeout=3)
        with patch.dict('os.environ', {'APP_WORKSPACE_URL':'https://user:password@example.com/'}):
            with self.assertRaises(ValueError): create_server(root,'127.0.0.1',0)

    def test_http_browser_reconnect_reads_original_receipt_without_extra_provider_call(self):
        root = write_app(Path(self.temp.name))
        entered, release = threading.Event(), threading.Event()
        calls = []
        def complete(prompt, _spec, *, on_progress, on_response=None):
            on_progress({'stage':'thinking'})
            calls.append(prompt); entered.set(); release.wait(3)
            return {'text':'真实回执占位测试','usage':{'total_tokens':9}}
        with patch('rag_ime.agent_lab.app_runtime.provider_complete', side_effect=complete):
            server = create_server(root, '127.0.0.1', 0)
            worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
            base = f'http://127.0.0.1:{server.server_port}'
            request = {**self.request, 'actionId':'answer'}
            def read(path):
                with urlopen(base+path, timeout=3) as response: return json.load(response)
            def submit():
                with urlopen(Request(base+'/api/invoke',data=json.dumps(request).encode(),headers={'Content-Type':'application/json'},method='POST'), timeout=3) as response:
                    return response.status, json.load(response)
            try:
                self.assertEqual(submit()[0], 202); self.assertTrue(entered.wait(2))
                self.assertEqual(submit()[1]['record']['requestId'], request['requestId'])
                self.assertEqual(read('/api/history')['records'][0]['state'], 'running')
                self.assertEqual(read('/api/history')['records'][0]['progress']['stage'], 'thinking')
                self.assertEqual(len(calls), 1)
                release.set()
                deadline = time.monotonic()+3
                while time.monotonic() < deadline:
                    record = read('/api/requests/'+request['requestId'])['record']
                    if record['state']=='completed': break
                    time.sleep(.01)
                self.assertEqual(record['state'], 'completed')
                self.assertEqual(submit()[1]['record']['result']['text'], '真实回执占位测试')
                self.assertEqual(len(calls), 1)
                self.assertEqual(read('/api/history')['records'][0]['input'], request['input'])
            finally:
                release.set(); server.shutdown(); server.server_close(); worker.join(3)

    def test_http_cancel_preserves_real_context_and_partial_output_without_replaying_or_accepting_late_success(self):
        root = write_app(Path(self.temp.name))
        entered, release, ended = threading.Event(), threading.Event(), threading.Event()
        def complete(_prompt, _spec, *, on_progress, on_response):
            class Response:
                def close(self): release.set()
            on_response(Response())
            on_progress({'stage':'answering','text':'已经收到的部分回答'})
            entered.set(); release.wait(3); ended.set()
            return {'text':'late final answer'}
        with patch('rag_ime.agent_lab.app_runtime.provider_complete', side_effect=complete) as provider:
            server = create_server(root, '127.0.0.1', 0)
            worker = threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            base = f'http://127.0.0.1:{server.server_port}'
            request = {**self.request, 'actionId':'answer'}
            def post(path, value):
                with urlopen(Request(base+path,data=json.dumps(value).encode(),
                        headers={'Content-Type':'application/json'},method='POST'),timeout=3) as response:
                    return json.load(response)['record']
            try:
                post('/api/invoke',request);self.assertTrue(entered.wait(2))
                stopped = post('/api/cancel',{'requestId':request['requestId']})
                self.assertEqual(stopped['state'],'cancelled')
                self.assertEqual(stopped['providerOutcome'],'unconfirmed')
                self.assertEqual(stopped['progress']['text'],'已经收到的部分回答')
                self.assertEqual(stopped['progress']['sources'][0]['text'],(root/'rules.md').read_text())
                self.assertEqual(stopped['progress']['knowledge']['sourceKind'],'provided_context')
                self.assertEqual(len(stopped['progress']['sources']),1)
                self.assertNotIn('只使用材料，不猜测',json.dumps(stopped,ensure_ascii=False))
                self.assertTrue(ended.wait(2))
                self.assertEqual(post('/api/invoke',request)['state'],'cancelled')
                self.assertEqual(post('/api/cancel',{'requestId':request['requestId']})['state'],'cancelled')
                self.assertEqual(provider.call_count,1)
                with urlopen(base+'/api/history',timeout=3) as response:
                    records=json.load(response)['records']
                self.assertEqual(len(records),1);self.assertIsNone(records[0]['result'])
            finally:
                release.set();server.shutdown();server.server_close();worker.join(3)


if __name__ == '__main__': unittest.main()
