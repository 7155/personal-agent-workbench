from __future__ import annotations

import tempfile
import unittest
import base64
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from rag_ime.agent_service import AgentService
from rag_ime.agent_tools import ControlToolGateway
from rag_ime.agent_skill_routing import default_skill_routing, skill_allowlist_for_session
from rag_ime.control_api import ControlAccessContext, ControlRequest, default_route_policy
from rag_ime.debug_server import DebugRequestHandler
from rag_ime.pi.config import PiRuntimeConfig
from tests.test_agent_lab_apps import write_app
from rag_ime.agent_lab.apps import AgentLabAppStore


def handler(agent):
    value = DebugRequestHandler.__new__(DebugRequestHandler)
    value.service = SimpleNamespace(agent=agent)
    value._authorize_gateway_request = lambda *_args: True
    value._serve_isolated_html_preview = lambda _path: False
    value._serve_gateway_static = lambda _path: False
    value._management_post_security_error = lambda *_args, **_kwargs: None
    written = []
    value._write_json = lambda status, body: written.append((int(status), body))
    return value, written


class LabProjectRouteTests(unittest.TestCase):
    def test_control_policy_accepts_versioned_commands_and_read_identity(self):
        policy = default_route_policy()
        body = {"action": "create", "expectedRevision": 0, "clientRequestId": "new-1", "input": {"description": "订单助手"}}
        route = policy.resolve("agent.eval-lab.projects.command")
        self.assertEqual(route.local_8766_path, "/api/agent/eval-lab/projects/command")
        policy.authorize(ControlRequest(request_id="http-1", path_id="agent.eval-lab.projects.command", body=body), ControlAccessContext.native())

    def test_get_and_post_forward_identity_and_receipt(self):
        read = Mock(return_value={"ok": True, "items": [], "project": None})
        receipt = {"ok": True, "project": {"projectId": "project-1"}, "replayed": False, "clientRequestId": "create-1"}
        command = Mock(return_value=receipt)
        value, written = handler(SimpleNamespace(eval_lab_projects=read, eval_lab_project_command=command))
        value.path = "/api/agent/eval-lab/projects?projectId=project-1&materialSetId=materials-1"
        value.do_GET()
        read.assert_called_once_with({"projectId": "project-1", "materialSetId": "materials-1", "artifactId": "", "artifactRevision": ""})
        self.assertEqual(written[0][0], 200)
        value.path = "/api/agent/eval-lab/projects/command"
        payload = {"action": "create", "expectedRevision": 0, "clientRequestId": "create-1", "input": {"description": "订单助手"}}
        value._read_json = lambda: payload
        value.do_POST()
        command.assert_called_once_with(payload)
        self.assertEqual(written[-1], (200, receipt))

    def test_read_failure_remains_distinct_from_empty_state(self):
        value, written = handler(SimpleNamespace(eval_lab_projects=Mock(side_effect=OSError("/private/local.sqlite"))))
        value.path = "/api/agent/eval-lab/projects"
        value.do_GET()
        self.assertEqual(written[0][0], 503)
        self.assertEqual(written[0][1]["code"], "AGENT_LAB_PROJECT_UNAVAILABLE")
        self.assertNotIn("/private", str(written))

    def test_invalid_app_session_filter_returns_a_known_rejection(self):
        value, written = handler(SimpleNamespace(list_sessions=Mock(side_effect=ValueError('Agent sessions cannot carry App surface ownership'))))
        value.path = '/api/agent/sessions?ownerAppId=extension%3Aagent-lab'
        value.do_GET()
        self.assertEqual(written[0][0],400)
        self.assertFalse(written[0][1]['ok'])

    def test_real_service_connects_project_materials_standard_and_one_app_owned_pi_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = AgentService(
                db_path=root / "paw.sqlite",
                runtime_config=PiRuntimeConfig(enabled=False, executable=None, agent_dir=root / "config", session_dir=root / "sessions", logs_dir=root / "logs"),
                background_job_execution_owner=False, startup_recovery_enabled=False,
            )
            try:
                self.assertEqual(service.eval_lab_projects()["items"], [])
                self.assertEqual(service.eval_lab_projects({"projectId": "", "materialSetId": "", "artifactId": "", "artifactRevision": ""})["items"], [])
                project = service.eval_lab_project_command({"action": "create", "expectedRevision": 0, "clientRequestId": "create", "input": {
                    "description": "根据新的订单规则提供答复", "materials": [{"title": "订单规则", "text": "缺货时通知顾客等待，不能提前声称已发货。"}],
                }})["project"]
                project = service.eval_lab_project_command({"action": "bind_execution", "projectId": project["projectId"], "expectedRevision": project["revision"], "clientRequestId": "standard", "input": {"adapterId": "golden.context_qa", "input": {"targetCount": 4}}})["project"]
                suite = service.eval_lab_golden({"suiteId": project["bindings"][0]["ownerRef"]["id"]})["suite"]
                self.assertEqual(suite["scenario"], project["description"])
                self.assertEqual(suite["sources"][0]["text"], project["materialSet"]["materials"][0]["text"])
                self.assertEqual(suite["jobs"], [])
                projected = service.eval_lab_projects({"projectId": project["projectId"]})["project"]
                self.assertEqual(projected["bindings"][0]["execution"]["status"], "not_started")
                self.assertTrue(projected["bindings"][0]["execution"]["canContinue"])
                self.assertIn("真实模型运行", projected["bindings"][0]["execution"]["reason"])
                primary = service.configuration_store.snapshot()["configuration"]["modelRouting"]["primary"]
                self.assertEqual(f'{suite["judgeConfig"]["provider"]}/{suite["judgeConfig"]["model"]}', primary["modelProfile"])
                guide = {"action": "ensure_guide", "projectId": project["projectId"], "expectedRevision": project["revision"], "clientRequestId": "guide", "input": {}}
                response = service.eval_lab_project_command(guide)
                session_id = response["project"]["guideSessionId"]
                session = service.sessions.get(session_id)
                self.assertEqual(session["surfaceKind"], "extension_app")
                self.assertEqual(session["ownerAppId"], "extension:agent-lab")
                self.assertEqual(session["surfaceKey"], f'project.{project["projectId"]}.guide')
                self.assertEqual(session["mode"], "coordinator")
                self.assertEqual(session["executionMode"], "workspace_managed")
                self.assertEqual(session["workspaceRoots"], [response["project"]["executionWorkspace"]["path"]])
                self.assertTrue(Path(session["workspaceRoots"][0]).is_dir())
                self.assertEqual(service.eval_lab_project_command(guide)["project"]["guideSessionId"], session_id)
                self.assertTrue(service.eval_lab_project_command(guide)["replayed"])
                second = {**guide, "expectedRevision": response["project"]["revision"], "clientRequestId": "guide-again"}
                self.assertEqual(service.eval_lab_project_command(second)["project"]["guideSessionId"], session_id)
                selected = skill_allowlist_for_session({"skillRouting": default_skill_routing()}, session, room_participant=False)
                self.assertIn("agent-lab-project", selected)
                self.assertNotIn("agent-eval-room-optimizer", selected)
                gateway = ControlToolGateway(sessions=service.sessions, management=Mock(), core=SimpleNamespace(), project="lab-test", lab_projects=service)
                current = service.eval_lab_project_tool(session_id, "read", {"op": "read"})["project"]
                call = {"schemaVersion": "rag-ime.agent-tool-call.v1", "sessionId": session_id, "tool": "lab_project", "toolCallId": "publish-artifact",
                        "args": {"op": "command", "action": "publish_artifact", "expectedRevision": current["revision"], "clientRequestId": "tool-publish",
                                 "input": {"title": "订单规则观察", "kind": "business_observation", "view": "markdown", "content": "缺货不能声称已发货。"}}}
                published = gateway.execute(call)["result"]
                self.assertEqual(published["artifact"]["content"], "缺货不能声称已发货。")
                self.assertEqual(service.eval_lab_projects({"projectId": project["projectId"], "artifactId": published["artifact"]["artifactId"]})["artifact"], published["artifact"])
                other_suite = service.eval_lab_golden_command({"action": "create", "expectedRevision": 0, "clientRequestId": "unrelated-suite", "input": {
                    "title": "不属于当前项目", "scenario": "另一个项目", "sources": suite["sources"],
                }})["suite"]
                execution = service.eval_lab_project_tool(session_id, "execution_read", {"bindingId": current["bindings"][0]["bindingId"]})
                self.assertEqual([item["suiteId"] for item in execution["execution"]["items"]], [suite["suiteId"]])
                self.assertNotIn(other_suite["suiteId"], str(execution))
                # Gateway retries preserve the original response byte-for-byte;
                # a fresh Tool invocation reaches the Lab command replay owner.
                self.assertEqual(gateway.execute(call)["result"], published)
                self.assertTrue(gateway.execute({**call, "toolCallId": "publish-artifact-again"})["result"]["replayed"])
                write_app(Path(current['executionWorkspace']['path']))
                prepared = service.eval_lab_project_tool(session_id,'command',{'action':'prepare_app',
                    'expectedRevision':published['project']['revision'],'clientRequestId':'prepare-app','input':{'directory':'app'}})
                app = prepared['application']
                self.assertEqual(service.eval_lab_apps({'projectId':project['projectId']})['items'][0]['appId'],app['appId'])
                app_store = AgentLabAppStore(root/'paw.sqlite')
                app_call = app_store.command({'action':'invoke','appId':app['appId'],'expectedRevision':app['revision'],
                    'clientRequestId':'offline-app-receipt','input':{'version':1,'actionId':'answer','values':{'question':'第7天可以吗？'}}})['call']
                app_store.update_call(app_call['callId'],state='completed',result_json='{"text":"测试中的已保存回执","usage":{"totalTokens":9}}')
                app_read = {**call,'toolCallId':'read-app-receipts','args':{'op':'read','appId':app['appId']}}
                observed = gateway.execute(app_read)['result']['application']
                self.assertEqual([row['appId'] for row in observed['items']],[app['appId']])
                self.assertNotIn('html',observed['version']); self.assertNotIn('result',observed['calls'][0])
                detail = service.eval_lab_project_tool(session_id,'read',{'appId':app['appId'],'appVersion':1,'appCallId':app_call['callId']})['application']
                self.assertEqual(detail['call']['result']['text'],'测试中的已保存回执')
                other_project = service.eval_lab_project_command({'action':'create','expectedRevision':0,'clientRequestId':'second-project',
                    'input':{'description':'另一个项目'}})['project']
                scoped_read = service.eval_lab_project_tool(session_id, 'read', {})
                self.assertEqual([item['projectId'] for item in scoped_read['items']], [project['projectId']])
                self.assertNotIn(other_project['projectId'], json.dumps(scoped_read))
                self.assertEqual(len(service.eval_lab_projects()['items']), 2)
                with self.assertRaises(ValueError):
                    service.eval_lab_project_tool(other_project['guideSessionId'],'read',{'appId':app['appId']})
                activated = service.eval_lab_app_command({'action':'activate','appId':app['appId'],'expectedRevision':app['revision'],
                    'clientRequestId':'activate-app','input':{'version':1}})['app']
                self.assertEqual(activated['activeVersion'],1)
                self.assertEqual(service.eval_lab_apps()['items'][0]['installation']['hosting']['projectId'],project['projectId'])
                package = service.eval_lab_app_download({'appId':app['appId'],'version':'1','target':'standalone'})
                with zipfile.ZipFile(io.BytesIO(base64.b64decode(package['base64']))) as archive:
                    self.assertIn('app.py',archive.namelist()); self.assertEqual(archive.read('rules.md').decode(),'在第 7 天仍可申请，第 8 天不能申请。')
                outsider = service.sessions.create(title="Other Session")
                with self.assertRaises(ValueError):
                    service.eval_lab_project_tool(outsider["id"], "read", {"op": "read"})
                with self.assertRaises(ValueError):
                    service.eval_lab_project_tool(session_id, "command", {"op": "command", "action": "import_materials", "expectedRevision": published["project"]["revision"],
                        "clientRequestId": "outside-path", "input": {"path": str(root / "outside.md")}})
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
