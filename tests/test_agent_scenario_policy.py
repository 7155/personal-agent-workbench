from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from rag_ime.agent_scenario_policy import (
    default_scenario_policy_configuration,
    normalize_scenario_policies,
    scenario_policy_catalog,
    scenario_policy_for_session,
    scenario_prompt_for_session,
)
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.agent_tools import ControlToolGateway
from rag_ime.agent_skill_routing import scenario_for_session
from rag_ime.agent_tool_ids import DANGEROUS_AUTO_APPROVE_TOOL_PROFILE


class AgentScenarioPolicyTests(unittest.TestCase):
    def test_policy_catalog_has_app_identity_and_editable_layers(self) -> None:
        configuration = {"scenarioPolicies": default_scenario_policy_configuration()}
        catalog = scenario_policy_catalog(configuration)
        scenarios = {str(item["id"]): item for item in catalog["scenarios"]}
        self.assertEqual(scenarios["trace"]["appIds"], ["extension:trace-agent"])
        self.assertIn("trace_diagnostics", scenarios["trace"]["builtInToolIds"])
        self.assertIn("lab_research", scenarios["agentLab"]["builtInToolIds"])
        self.assertEqual(
            normalize_scenario_policies(configuration["scenarioPolicies"]),
            configuration["scenarioPolicies"],
        )

    def test_each_product_scenario_has_its_own_prompt_layer(self) -> None:
        sessions = {
            "ordinary": {"surfaceKind": "agent", "surfaceKey": ""},
            "room": {
                "surfaceKind": "agent",
                "surfaceKey": "",
                "roomParticipant": {"collaborationRole": "coordinator"},
            },
            "trace": {
                "surfaceKind": "extension_app",
                "ownerAppId": "extension:trace-agent",
                "surfaceKey": "diagnostic",
            },
            "trace-repair": {
                "surfaceKind": "extension_app",
                "ownerAppId": "extension:trace-agent",
                "surfaceKey": "repair",
            },
            "lab": {
                "surfaceKind": "extension_app",
                "ownerAppId": "extension:agent-lab",
                "surfaceKey": "project.project-1.guide",
            },
            "lab-app": {
                "surfaceKind": "extension_app",
                "ownerAppId": "extension:lab-demo",
                "surfaceKey": "application.call-1",
            },
        }

        prompts = {
            name: scenario_prompt_for_session(session)
            for name, session in sessions.items()
        }

        self.assertEqual(
            len({prompt for prompt in prompts.values()}),
            len(prompts),
        )
        self.assertIn('kind="ordinary"', prompts["ordinary"])
        self.assertIn('kind="room"', prompts["room"])
        self.assertIn('kind="trace"', prompts["trace"])
        self.assertIn('mode="diagnostic"', prompts["trace"])
        self.assertIn('mode="repair"', prompts["trace-repair"])
        self.assertIn('kind="agentLab"', prompts["lab"])
        self.assertIn('mode="project-guide"', prompts["lab"])
        self.assertIn('mode="application"', prompts["lab-app"])

    def test_trace_evaluation_and_lab_application_are_classified(self) -> None:
        self.assertEqual(
            scenario_for_session(
                {
                    "surfaceKind": "extension_app",
                    "ownerAppId": "extension:trace-agent",
                    "surfaceKey": "optimization.eval.request-1",
                },
                room_participant=False,
            ),
            "trace",
        )
        self.assertEqual(
            scenario_for_session(
                {
                    "surfaceKind": "extension_app",
                    "ownerAppId": "extension:agent-lab",
                    "surfaceKey": "application.call-1",
                },
                room_participant=False,
            ),
            "agentLab",
        )

    def test_configured_lab_policy_is_an_intersection_with_the_builtin_fence(self) -> None:
        session = {
            "surfaceKind": "extension_app",
            "ownerAppId": "extension:agent-lab",
            "surfaceKey": "project.project-1.guide",
        }
        policy = scenario_policy_for_session(
            session,
            configured_policy={
                "promptInstructions": "输出实验编号和证据引用。",
                "toolAllowlist": ["lab_project", "workspace_read", "trace_diagnostics"],
            },
        )
        self.assertEqual(policy.tool_ids, {"lab_project", "workspace_read"})
        self.assertIn("输出实验编号和证据引用。", policy.prompt)

    def test_tool_disclosure_follows_the_scenario(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="paw-scenario-policy-")
        self.addCleanup(temporary.cleanup)
        store = AgentSessionStore(Path(temporary.name) / "agent.sqlite")
        store.initialize()
        self.addCleanup(store.close)

        room_participant = {
            "id": "participant:1",
            "collaborationRole": "coordinator",
        }
        gateway = ControlToolGateway(
            sessions=store,
            management=SimpleNamespace(),
            core=SimpleNamespace(),
            project="scenario-policy",
            collaboration=SimpleNamespace(
                rooms=SimpleNamespace(
                    participant_for_session=lambda _session_id, active_only=False: (
                        room_participant if active_only is False else room_participant
                    ),
                ),
            ),
            lab_projects=SimpleNamespace(),
        )

        ordinary = {
            "id": "agent:ordinary",
            "mode": "coordinator",
            "surfaceKind": "agent",
            "surfaceKey": "",
            "toolProfileVersion": "control-center-v1",
            "executionMode": "per_action",
            "workspaceRoots": [],
        }
        trace = {
            "id": "agent:trace",
            "mode": "coordinator",
            "surfaceKind": "extension_app",
            "ownerAppId": "extension:trace-agent",
            "surfaceKey": "diagnostic",
            "toolProfileVersion": DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
            "executionMode": "full_trust",
            "workspaceRoots": ["/tmp"],
        }
        lab = {
            "id": "agent:lab",
            "mode": "coordinator",
            "surfaceKind": "extension_app",
            "ownerAppId": "extension:agent-lab",
            "surfaceKey": "project.project-1.guide",
            "toolProfileVersion": "control-center-v1",
            "executionMode": "workspace_managed",
            "workspaceRoots": ["/tmp"],
        }

        ordinary_items = {
            str(item["id"]): item
            for item in gateway._manifest_items(ordinary)
        }
        trace_items = {
            str(item["id"]): item
            for item in gateway._manifest_items(trace)
        }
        lab_items = {
            str(item["id"]): item
            for item in gateway._manifest_items(lab)
        }

        self.assertEqual(ordinary_items["trace_diagnostics"]["effectiveOperations"], [])
        self.assertEqual(ordinary_items["lab_project"]["effectiveOperations"], [])
        self.assertTrue(trace_items["trace_diagnostics"]["effectiveOperations"])
        self.assertEqual(trace_items["lab_project"]["effectiveOperations"], [])
        self.assertTrue(lab_items["lab_project"]["effectiveOperations"])
        self.assertEqual(lab_items["trace_diagnostics"]["effectiveOperations"], [])


if __name__ == "__main__":
    unittest.main()
