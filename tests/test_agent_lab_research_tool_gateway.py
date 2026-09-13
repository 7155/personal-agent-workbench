from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rag_ime.agent_lab.apps import AgentLabAppStore
from rag_ime.agent_lab.app_runtime import AppInputError
from rag_ime.agent_lab.projects import AgentLabProjectStore
from rag_ime.agent_service import AgentService
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.agent_tools import ControlToolGateway, _runtime_tool_parameter_schema
from tests.test_agent_lab_apps import MODEL, write_app


class LabResearchGatewayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sessions = AgentSessionStore(self.root / 'lab.sqlite')
        self.sessions.initialize()
        self.addCleanup(self.sessions.close)
        self.session = self.sessions.create(title='research', surface_kind='extension_app',
            owner_app_id='extension:lab-test', surface_key='application.call-one',
            tool_profile_version='subagent-readonly-v1', workspace_roots=[])
        self.sessions.set_runtime_policy(self.session['id'], mode='assistant',
            tool_profile_version='subagent-readonly-v1', allowed_tools=['lab_research'],
            project_context_enabled=False, pi_skills_enabled=False, codex_skills_enabled=False, workspace_roots=[])
        self.session = self.sessions.get(self.session['id'])
        self.owner = Mock()
        self.owner.research_tool.return_value = {'schemaVersion': 'paw.app-research-result.v1',
            'status': 'completed', 'documents': [{'documentId': 'paper-one'}],
            'budget': {'usedToolCalls': 1}, 'untrustedData': True}
        self.service = AgentService.__new__(AgentService)
        self.service.sessions = self.sessions
        self.service._eval_lab_app_lock = threading.RLock()
        self.service._eval_lab_app_application = self.owner
        self.service._eval_lab_app_research_application = None
        self.gateway = ControlToolGateway(sessions=self.sessions, management=Mock(), core=object(),
            project='research-test', lab_projects=self.service)

    def call(self, op='discover', **args):
        return self.gateway.execute({'schemaVersion': 'rag-ime.agent-tool-call.v1',
            'sessionId': self.session['id'], 'tool': 'lab_research', 'toolCallId': 'tool-one',
            'args': {'op': op, **args}})

    def test_all_four_operations_reach_app_owner_with_only_host_identities(self):
        for op in ('discover', 'find', 'open', 'search'):
            self.call(op, query='Orion', _sessionId='spoofed', _toolCallId='spoofed')
            session, operation, args = self.owner.research_tool.call_args.args
            self.assertEqual(session['id'], self.session['id'])
            self.assertEqual(operation, op)
            self.assertEqual(args, {'op': op, 'query': 'Orion',
                '_sessionId': self.session['id'], '_toolCallId': 'tool-one'})
        self.assertEqual(self.owner.research_tool.call_count, 4)

    def test_explicit_empty_golden_policy_cannot_use_research_or_global_knowledge(self):
        self.sessions.set_runtime_policy(self.session['id'], mode='assistant',
            tool_profile_version='subagent-readonly-v1', allowed_tools=[],
            project_context_enabled=False, pi_skills_enabled=False, codex_skills_enabled=False, workspace_roots=[])
        with self.assertRaisesRegex(ValueError, 'tool profile'):
            self.call()
        self.owner.research_tool.assert_not_called()
        manifests = self.gateway.runtime_manifests(self.sessions.get(self.session['id']))
        self.assertNotIn('lab_research', [item['name'] for item in manifests])
        self.assertNotIn('knowledge', [item['name'] for item in manifests])

    def test_research_manifest_is_scoped_and_preserves_guide_readonly_contract(self):
        manifests = self.gateway.runtime_manifests(self.session)
        research = next(item for item in manifests if item['name'] == 'lab_research')
        self.assertEqual(research['parameters'], _runtime_tool_parameter_schema('lab_research', ['discover', 'find', 'open', 'search']))
        self.assertNotIn('_sessionId', json.dumps(research['parameters']))
        self.assertNotIn('_toolCallId', json.dumps(research['parameters']))
        outsider = {**self.session, 'ownerAppId': 'extension:agent-lab', 'surfaceKey': 'golden.one'}
        item = next(item for item in self.gateway._manifest_items(outsider) if item['id'] == 'lab_research')
        self.assertFalse(item['enabled'])
        guide = {**self.session, 'ownerAppId': 'extension:agent-lab', 'surfaceKey': 'project.one.guide',
                 'allowedTools': ['lab_project']}
        item = next(item for item in self.gateway._manifest_items(guide) if item['id'] == 'lab_project')
        self.assertIn('knowledge_read', item['effectiveOperations'])
        self.assertNotIn('knowledge_command', item['effectiveOperations'])
        self.assertNotIn('execution_command', item['effectiveOperations'])
        self.assertNotIn('app_command', item['effectiveOperations'])

    def test_project_read_and_app_command_routes_have_valid_result_envelopes(self):
        guide = self.sessions.create(title='guide', surface_kind='extension_app',
            owner_app_id='extension:agent-lab', surface_key='project.one.guide',
            tool_profile_version='control-center-v1', workspace_roots=[])
        project = Mock()
        project.tool.return_value = {'call': {'callId': 'app-call-original', 'state': 'queued'}}
        self.service._lab_project_application = Mock(return_value=project)
        for operation in ('knowledge_read', 'knowledge_command', 'app_command'):
            request = {'schemaVersion': 'rag-ime.agent-tool-call.v1', 'sessionId': guide['id'],
                'tool': 'lab_project', 'toolCallId': 'project-tool',
                'args': {'op': operation, 'appId': 'extension:lab-test', 'action': 'invoke',
                         'expectedRevision': 1, 'clientRequestId': 'same-request', 'input': {}}}
            response = self.gateway.execute(request)
            self.assertEqual(response['result']['call']['callId'], 'app-call-original')
            self.assertEqual(project.tool.call_args.args[1], operation)
        self.sessions.set_runtime_policy(guide['id'], mode='assistant',
            tool_profile_version='subagent-readonly-v1', allowed_tools=['lab_project'],
            project_context_enabled=False, pi_skills_enabled=False, codex_skills_enabled=False, workspace_roots=[])
        with self.assertRaisesRegex(ValueError, 'tool profile'):
            self.gateway.execute(request)

    def test_service_read_replica_is_cached_and_never_runs_recovery(self):
        self.service._eval_lab_app_application = None
        self.service._lab_project_application = Mock(return_value=SimpleNamespace(apps=object()))
        with patch('rag_ime.agent_lab.apps.AgentLabAppApplication') as factory:
            factory.return_value.research_tool.return_value = {'status': 'completed'}
            self.service.eval_lab_app_research_tool(self.session['id'], 'discover', {})
            self.service.eval_lab_app_research_tool(self.session['id'], 'open', {})
            self.assertEqual(factory.call_count, 1)
            self.assertFalse(factory.call_args.kwargs['start_workers'])
            self.assertFalse(factory.call_args.kwargs['recover'])
            factory.return_value.close.assert_not_called()
            self.assertIsNone(self.service._eval_lab_app_application)

    def test_service_tool_read_does_not_interrupt_an_actual_running_call(self):
        workspace = self.root / 'workspace'
        workspace.mkdir()
        write_app(workspace)
        apps = AgentLabAppStore(self.sessions.db_path)
        projects = AgentLabProjectStore(self.sessions.db_path,
            create_guide=lambda _conn, _project: {'sessionId': 'guide-one',
                'workspace': {'kind': 'managed', 'path': str(workspace), 'createdAtMs': 1}},
            prepare_app=lambda conn, project, value: apps.prepare(conn, project, value, MODEL))
        project = projects.command({'action': 'create', 'expectedRevision': 0,
            'clientRequestId': 'create', 'input': {'description': 'test'}})['project']
        prepared = projects.command({'action': 'prepare_app', 'projectId': project['projectId'],
            'expectedRevision': project['revision'], 'clientRequestId': 'prepare', 'input': {'directory': 'app'}})['application']
        receipt = apps.command({'action': 'invoke', 'appId': prepared['appId'], 'expectedRevision': prepared['revision'],
            'clientRequestId': 'invoke', 'input': {'version': 1, 'actionId': 'answer', 'values': {'question': 'day7'}}})
        call_id = receipt['call']['callId']
        apps.update_call(call_id, state='running')
        self.service._eval_lab_app_application = None
        self.service._lab_project_application = Mock(return_value=SimpleNamespace(apps=apps))
        # This ordinary App has no research binding; even a rejected read must
        # leave its active execution alone. No complete/Provider is invoked.
        with self.assertRaises(AppInputError):
            self.service.eval_lab_app_research_tool(self.session['id'], 'discover',
                {'_sessionId': self.session['id'], '_toolCallId': 'tool-one'})
        self.assertEqual(apps.call_input(call_id)[0]['state'], 'running')
        self.service._eval_lab_app_research_application.close()
        self.assertEqual(apps.call_input(call_id)[0]['state'], 'running')

    @unittest.skipUnless(shutil.which('node'), 'Node is required to execute the real Pi extension')
    def test_real_pi_registration_and_http_gateway_route_match_python_schema(self):
        gateway = self.gateway
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(request)
                try:
                    result = gateway.execute(request)
                    status = 200
                except Exception as error:
                    result, status = {'ok': False, 'error': str(error)}, 400
                body = json.dumps(result).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        extension = Path(__file__).parents[1] / 'integrations/pi/rag-ime-control.ts'
        script = self.root / 'probe.mjs'
        script.write_text('''const tools = [];
const extension = await import(process.env.TEST_EXTENSION_URL);
extension.default({registerTool: tool => tools.push(tool)});
const research = tools.find(tool => tool.name === 'lab_research');
if (!research) throw new Error('research tool was not registered for readonly Pi');
const result = await research.execute('pi-tool-one', {op:'discover',query:'Orion'});
console.log(JSON.stringify({parameters:research.parameters,result,projectParameters:tools.find(tool=>tool.name==='lab_project').parameters}));
''')
        try:
            env = {**os.environ, 'RAG_IME_AGENT_TOOL_URL': f'http://127.0.0.1:{server.server_port}/tool/execute',
                'RAG_IME_AGENT_TOOL_TOKEN': 'isolated-test-token', 'RAG_IME_AGENT_SESSION_ID': self.session['id'],
                'RAG_IME_AGENT_TOOL_PROFILE_VERSION': 'subagent-readonly-v1', 'RAG_IME_AGENT_SESSION_MODE': 'assistant',
                'RAG_IME_AGENT_EXECUTION_MODE': 'read_only', 'RAG_IME_AGENT_ROOM_BOUND': '0',
                'TEST_EXTENSION_URL': extension.as_uri()}
            process = subprocess.run(['node', '--experimental-strip-types', str(script)],
                env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads(process.stdout)
            self.assertEqual(result['parameters'], _runtime_tool_parameter_schema('lab_research', ['discover', 'find', 'open', 'search']))
            operations = [branch['properties']['op']['const'] for branch in result['projectParameters']['oneOf']]
            self.assertIn('knowledge_read', operations)
            self.assertNotIn('knowledge_command', operations)
            self.assertNotIn('app_command', operations)
            self.assertNotIn('execution_command', operations)
            self.assertEqual(result['result']['details']['documents'], [{'documentId': 'paper-one'}])
            self.assertEqual(requests[0]['tool'], 'lab_research')
            self.assertEqual(requests[0]['args'], {'op': 'discover', 'query': 'Orion'})
            self.assertEqual(self.owner.research_tool.call_args.args[2], {'op': 'discover', 'query': 'Orion',
                '_sessionId': self.session['id'], '_toolCallId': 'pi-tool-one'})
            guide = self.sessions.create(title='guide', surface_kind='extension_app',
                owner_app_id='extension:agent-lab', surface_key='project.http.guide',
                tool_profile_version='control-center-v1', workspace_roots=[])
            project = Mock()
            project.tool.return_value = {'call': {'callId': 'original-app-call', 'state': 'queued'}}
            self.service._lab_project_application = Mock(return_value=project)
            script.write_text('''const tools = [];
const extension = await import(process.env.TEST_EXTENSION_URL);
extension.default({registerTool: tool => tools.push(tool)});
const project = tools.find(tool=>tool.name==='lab_project');
const args = {op:'app_command',appId:'extension:lab-test',action:'invoke',expectedRevision:1,clientRequestId:'original-request',input:{}};
console.log(JSON.stringify({parameters:project.parameters,result:await project.execute('invoke-tool',args)}));
''')
            process = subprocess.run(['node', '--experimental-strip-types', str(script)],
                env={**env, 'RAG_IME_AGENT_SESSION_ID': guide['id'],
                     'RAG_IME_AGENT_TOOL_PROFILE_VERSION': 'control-center-v1', 'RAG_IME_AGENT_EXECUTION_MODE': 'per_action'},
                capture_output=True, text=True, timeout=20)
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads(process.stdout)
            branch = next(branch for branch in result['parameters']['oneOf'] if branch['properties']['op']['const'] == 'app_command')
            self.assertEqual(branch, _runtime_tool_parameter_schema('lab_project', ['app_command'])['oneOf'][0])
            self.assertEqual(result['result']['details']['call']['callId'], 'original-app-call')
            self.assertEqual(project.tool.call_args.args[1], 'app_command')
            self.assertEqual(project.tool.call_args.args[2]['clientRequestId'], 'original-request')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
