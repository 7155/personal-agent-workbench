from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rag_ime.agent_configuration import (
    AgentConfigurationStore,
    default_agent_configuration,
)
from rag_ime.agent_execution_policy import read_only_blocks_effect
from rag_ime.agent_session_policy import AgentSessionPolicyService
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.agent_tool_ids import (
    CONTROL_TOOL_IDS,
    DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
    FULL_ACCESS_TOOL_PROFILE,
)
from rag_ime.agent_tools import (
    ControlToolGateway,
    _TOOL_SPEC_BY_ID,
    _approval_payload_digest,
)
from rag_ime.agent_workspace import WorkspaceHarness
from rag_ime.contracts.json_schema import validate_contract


class _Runtime:
    def __init__(self) -> None:
        self.open_session_ids: list[str] = []
        self.active_session_id: str | None = None
        self.status = "ready"
        self.closed: list[str] = []
        self.running_jobs = ["job:running"]

    def runtime_status(self) -> dict[str, object]:
        return {
            "status": self.status,
            "activeSessionId": self.active_session_id,
            "openSessionIds": list(self.open_session_ids),
        }

    def close_session(self, session_id: str) -> bool:
        self.closed.append(session_id)
        self.open_session_ids = [
            value for value in self.open_session_ids if value != session_id
        ]
        if self.active_session_id == session_id:
            self.active_session_id = None
        return True


class _Rooms:
    def __init__(self) -> None:
        self.active_session_id = ""

    @property
    def rooms(self) -> "_Rooms":
        return self

    def participant_for_session(
        self, session_id: str, *, active_only: bool
    ) -> dict[str, object] | None:
        del active_only
        if session_id != self.active_session_id:
            return None
        return {"roomId": "room:1", "status": "active"}

    def get(self, room_id: str) -> dict[str, object]:
        self.assert_room_id = room_id
        return {"id": room_id, "status": "active"}


class _Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, dict[str, object]]] = []

    def publish(
        self, session_id: str, event: str, payload: dict[str, object]
    ) -> None:
        self.items.append((session_id, event, payload))


class _Skills:
    def governance_catalog(self) -> list[dict[str, object]]:
        return [
            {
                "skillId": "quality-gate",
                "name": "quality-gate",
                "when": ["before delivery"],
                "notFor": [],
                "input": "evidence",
                "output": "gate result",
                "does": "Checks delivery evidence.",
                "risk": "medium",
                "stages": ["delivery"],
                "policyId": "room-skills",
                "policyVersion": 1,
                "contentRevision": "abc123",
            }
        ]


class _Extensions:
    def catalog(self) -> dict[str, object]:
        return {
            "catalogVersion": "1",
            "items": [
                {
                    "id": "session-review",
                    "displayName": "Session Review",
                    "description": "Reviews a Session.",
                    "source": {"label": "Product bundle"},
                    "permissions": ["session.read"],
                    "installedVersion": "1.0.0",
                    "installed": True,
                    "enabled": True,
                    "installState": "installed",
                }
            ],
        }
class _RoomCapabilityGateway:
    def __init__(self, *, dispatch_active: bool = False) -> None:
        self.room_calls: list[tuple[str, dict[str, object]]] = []
        self.dispatch_active = dispatch_active

    def execute_room_partner_tool(
        self,
        session_id: str,
        args: dict[str, object],
        *,
        tool_call_id: str,
        source_loop_id: str = "",
    ) -> dict[str, object]:
        del session_id, tool_call_id, source_loop_id
        self.room_calls.append(("room_partner", dict(args)))
        return {"operation": str(args.get("op") or "list")}

    def _active_room_dispatch_authorizes_work(self, _session_id: str) -> bool:
        return self.dispatch_active




class AgentCapabilityPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.sessions = AgentSessionStore(self.root / "agent.sqlite3")
        self.sessions.initialize()
        self.configuration = AgentConfigurationStore(
            self.root / "agent.sqlite3"
        )
        self.configuration.initialize(default_agent_configuration())
        self.runtime = _Runtime()
        self.rooms = _Rooms()
        self.events = _Events()
        self.policy = AgentSessionPolicyService(
            sessions=self.sessions,
            runtime_provider=lambda: self.runtime,
            rooms=self.rooms,
            events=self.events,
            runtime_status=self.runtime.runtime_status,
            probe_memory_maintenance=lambda *_args, **_kwargs: {},
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _gateway(
        self,
        *,
        collaboration: object | None = None,
        workspace_harness: WorkspaceHarness | None = None,
    ) -> ControlToolGateway:
        return ControlToolGateway(
            sessions=self.sessions,
            management=object(),
            core=object(),
            project="test",
            configuration_store=self.configuration,
            governed_skills=_Skills(),
            extensions=_Extensions(),
            collaboration=collaboration,
            workspace_harness=workspace_harness,
        )

    def test_tool_inventory_ids_exactly_match_specs(self) -> None:
        self.assertEqual(frozenset(_TOOL_SPEC_BY_ID), frozenset(CONTROL_TOOL_IDS))
        self.assertEqual(len(_TOOL_SPEC_BY_ID), len(CONTROL_TOOL_IDS))
        self.assertIn("plugins", CONTROL_TOOL_IDS)

    def test_room_partner_is_disabled_outside_a_real_room_participant_session(self) -> None:
        session = self.sessions.create(title="ordinary session")
        self.policy.update_session(
            str(session["id"]),
            {
                "capabilityDisclosurePreferences": {
                    "tool:room_partner": "enabled",
                },
            },
        )
        catalog = self._gateway(collaboration=self.rooms).manifests(
            session_id=str(session["id"])
        )
        room_partner = next(
            item for item in catalog["items"] if item["canonicalId"] == "tool:room_partner"
        )

        self.assertEqual(room_partner["status"], "offline")
        self.assertEqual(room_partner["authorization"]["state"], "denied")
        self.assertEqual(room_partner["disclosure"]["effective"], "disabled")
        self.assertEqual(room_partner["disclosure"]["state"], "hidden")
        self.assertEqual(room_partner["disclosure"]["reason"], "room_context_required")

    def test_room_partner_is_enabled_for_an_active_room_participant_session(self) -> None:
        session = self.sessions.create(title="room participant", mode="coordinator")
        session_id = str(session["id"])
        self.rooms.active_session_id = session_id

        catalog = self._gateway(collaboration=self.rooms).manifests(
            session_id=session_id
        )
        room_partner = next(
            item for item in catalog["items"] if item["canonicalId"] == "tool:room_partner"
        )

        self.assertEqual(room_partner["status"], "online")
        self.assertEqual(room_partner["authorization"]["state"], "authorized")
        self.assertEqual(room_partner["disclosure"]["effective"], "enabled")
        self.assertEqual(room_partner["disclosure"]["state"], "disclosed")

    def test_disclosure_never_grants_tool_authorization(self) -> None:
        session = self.sessions.create(title="restricted")
        session = self.sessions.set_runtime_policy(
            str(session["id"]),
            mode="assistant",
            tool_profile_version="subagent-readonly-v1",
            allowed_tools=["overview"],
        )
        updated = self.policy.update_session(
            str(session["id"]),
            {
                "capabilityDisclosurePreferences": {
                    "tool:memory": "enabled"
                }
            },
        )
        self.assertEqual(updated["policyRevision"], 2)
        current = self.sessions.get(str(session["id"]))
        self.assertEqual(current["allowedTools"], ["overview"])
        memory = next(
            item
            for item in self._gateway().manifests(
                session_id=str(session["id"])
            )["items"]
            if item["canonicalId"] == "tool:memory"
        )
        self.assertEqual(memory["disclosure"]["state"], "disclosed")
        self.assertEqual(memory["authorization"]["state"], "denied")

    def test_unrestricted_profiles_honor_explicit_session_tool_choices(self) -> None:
        for profile, execution_mode in (
            (FULL_ACCESS_TOOL_PROFILE, "per_action"),
            (DANGEROUS_AUTO_APPROVE_TOOL_PROFILE, "full_trust"),
        ):
            with self.subTest(profile=profile):
                session = self.sessions.create(title=f"unrestricted {profile}")
                session_id = str(session["id"])
                self.policy.update_session(
                    session_id,
                    {
                        "capabilityDisclosurePreferences": {
                            "tool:memory": "disabled",
                            "skill:quality-gate": "disabled",
                            "extension:session-review": "disabled",
                        }
                    },
                )
                self.policy.update_session(
                    session_id,
                    {
                        "mode": "coordinator",
                        "executionMode": execution_mode,
                        "toolProfileVersion": profile,
                        "workspaceRoots": [str(self.root)],
                    },
                )

                items = self._gateway().manifests(session_id=session_id)["items"]
                by_id = {
                    str(item["canonicalId"]): item
                    for item in items
                }
                for canonical_id in (
                    "tool:memory",
                    "skill:quality-gate",
                    "extension:session-review",
                ):
                    disclosure = by_id[canonical_id]["disclosure"]
                    self.assertEqual(disclosure["state"], "hidden")
                    self.assertEqual(disclosure["reason"], "session_preference")
                self.policy.update_session(session_id, {"capabilityDisclosurePreferences": {}})
                restored = self._gateway().manifests(session_id=session_id)["items"]
                for item in restored:
                    if item["canonicalId"] in {"tool:memory", "skill:quality-gate", "extension:session-review"}:
                        memory = item["canonicalId"] == "tool:memory"
                        self.assertEqual(item["disclosure"]["effective"], "disabled" if memory else "enabled")
                        self.assertEqual(item["disclosure"]["reason"], "inherited_built_in_default" if memory else "unrestricted_session_profile")

    def test_memory_is_opt_in_and_explicit_enabling_reaches_runtime_tools(self) -> None:
        from rag_ime.agent_capability_catalog import capability_disclosure_enabled, session_resource_disclosure_policy

        session = self.sessions.create(title="memory opt in")
        session_id = str(session["id"])
        self.assertFalse(capability_disclosure_enabled(
            "tool:memory", session=session, configuration_store=self.configuration,
        ))
        self.assertNotIn("memory", [item["name"] for item in self._gateway().runtime_manifests(session)])
        self.policy.update_session(session_id, {"capabilityDisclosurePreferences": {"tool:memory": "enabled"}})
        updated = self.sessions.get(session_id)
        self.assertTrue(capability_disclosure_enabled(
            "tool:memory", session=updated, configuration_store=self.configuration,
        ))
        self.policy.update_session(session_id, {"capabilityDisclosurePreferences": {
            "skill:quality-gate": "disabled", "extension:session-review": "disabled",
        }})
        self.assertEqual(session_resource_disclosure_policy(self.sessions.get(session_id), configuration_store=self.configuration), {
            "disabledSkillNames": ["quality-gate"], "disabledPluginIds": ["session-review"],
        })

    def test_busy_mutation_is_rejected_without_retiring_runtime(self) -> None:
        session = self.sessions.create(title="busy")
        session_id = str(session["id"])
        self.sessions.set_status(session_id, "busy")
        self.runtime.active_session_id = session_id
        self.runtime.open_session_ids = [session_id]
        with self.assertRaisesRegex(ValueError, "Agent Loop"):
            self.policy.update_session(
                session_id,
                {
                    "capabilityDisclosurePreferences": {
                        "tool:memory": "disabled"
                    }
                },
            )
        self.assertEqual(self.runtime.closed, [])
        self.assertEqual(
            self.sessions.get(session_id)["capabilityDisclosurePreferences"],
            {},
        )

    def test_idle_mutation_retires_only_target_and_preserves_running_jobs(self) -> None:
        target = self.sessions.create(title="target")
        other = self.sessions.create(title="other")
        target_id = str(target["id"])
        other_id = str(other["id"])
        self.runtime.open_session_ids = [target_id, other_id]
        jobs_before = list(self.runtime.running_jobs)
        response = self.policy.update_session(
            target_id,
            {
                "capabilityDisclosurePreferences": {
                    "tool:memory": "disabled"
                }
            },
        )
        self.assertEqual(response["policyRevision"], 2)
        self.assertEqual(self.runtime.closed, [target_id])
        self.assertEqual(self.runtime.open_session_ids, [other_id])
        self.assertEqual(self.runtime.running_jobs, jobs_before)

    def test_idle_room_partner_accepts_capability_choices_without_changing_permissions(self) -> None:
        session = self.sessions.create(title="room participant")
        session_id = str(session["id"])
        self.rooms.active_session_id = session_id
        self.policy.update_session(session_id, {
            "capabilityDisclosurePreferences": {"skill:quality-gate": "disabled"},
        })
        self.assertEqual(
            self.sessions.get(session_id)["capabilityDisclosurePreferences"],
            {"skill:quality-gate": "disabled"},
        )
        self.assertEqual(self.sessions.get(session_id)["executionMode"], session["executionMode"])
        with self.assertRaisesRegex(ValueError, "managed by the Room"):
            self.policy.update_session(session_id, {"executionMode": "full_trust"})
        self.sessions.set_status(session_id, "busy")
        with self.assertRaisesRegex(ValueError, "Agent Loop"):
            self.policy.update_session(session_id, {"capabilityDisclosurePreferences": {"tool:memory": "enabled"}})

    def test_precedence_catalog_completeness_and_removed_item(self) -> None:
        snapshot = self.configuration.snapshot()
        self.configuration.update(
            {
                "sessionDefaults.capabilityDisclosurePreferences": {
                    "tool:memory": "disabled",
                    "extension:removed-plugin": "disabled",
                }
            },
            expected_revision=int(snapshot["revision"]),
            updated_by="test",
        )
        session = self.sessions.create(title="precedence")
        session_id = str(session["id"])
        self.policy.update_session(
            session_id,
            {
                "capabilityDisclosurePreferences": {
                    "tool:memory": "enabled",
                    "skill:quality-gate": "inherit",
                }
            },
        )
        catalog = self._gateway().manifests(session_id=session_id)
        by_id = {item["canonicalId"]: item for item in catalog["items"]}
        self.assertIn("tool:plugins", by_id)
        self.assertIn("skill:quality-gate", by_id)
        self.assertIn("extension:session-review", by_id)
        self.assertEqual(
            by_id["tool:memory"]["effectiveScope"], "session"
        )
        self.assertEqual(
            by_id["tool:memory"]["disclosure"]["effective"],
            "enabled",
        )
        removed = by_id["extension:removed-plugin"]
        self.assertEqual(removed["status"], "removed")
        self.assertEqual(removed["authorization"]["state"], "denied")
        self.assertFalse(catalog["projectScope"]["supported"])
        self.assertEqual(
            catalog["projectScope"]["reason"],
            "stable_project_identity_unavailable",
        )

    def test_project_default_precedes_global_and_session_precedes_project(self) -> None:
        session = self.sessions.create(
            title="project precedence",
            mode="coordinator",
            execution_mode="workspace_managed",
            workspace_roots=[str(self.root)],
        )
        session_id = str(session["id"])
        project_id = f"workspace-{session['workspaceScopeSha256']}"
        snapshot = self.configuration.snapshot()
        self.configuration.update(
            {
                "sessionDefaults.capabilityDisclosurePreferences": {
                    "tool:memory": "disabled",
                },
                "capabilityDisclosure.projectPreferences": {
                    project_id: {
                        "tool:memory": "enabled",
                        "extension:project-removed": "disabled",
                    }
                },
            },
            expected_revision=int(snapshot["revision"]),
            updated_by="test",
        )

        project_catalog = self._gateway().manifests(session_id=session_id)
        self.assertEqual(
            project_catalog["projectScope"],
            {
                "supported": True,
                "identityKind": "workspace_scope_sha256",
                "projectId": project_id,
                "reason": "session_workspace_scope",
            },
        )
        project_by_id = {
            item["canonicalId"]: item for item in project_catalog["items"]
        }
        self.assertEqual(
            project_by_id["tool:memory"]["effectiveScope"],
            "project_default",
        )
        self.assertEqual(
            project_by_id["tool:memory"]["disclosure"]["effective"],
            "enabled",
        )
        self.assertEqual(
            project_catalog["sessionPolicy"]["disclosurePreferences"][
                "projectDefault"
            ],
            {
                "extension:project-removed": "disabled",
                "tool:memory": "enabled",
            },
        )
        self.assertIn(
            "memory",
            {
                str(item["name"])
                for item in self._gateway().runtime_manifests(
                    self.sessions.get(session_id)
                )
            },
        )
        self.assertEqual(
            project_by_id["extension:project-removed"]["status"],
            "removed",
        )

        self.policy.update_session(
            session_id,
            {
                "capabilityDisclosurePreferences": {
                    "tool:memory": "disabled",
                }
            },
        )
        session_catalog = self._gateway().manifests(session_id=session_id)
        memory = next(
            item
            for item in session_catalog["items"]
            if item["canonicalId"] == "tool:memory"
        )
        self.assertEqual(memory["effectiveScope"], "session")
        self.assertEqual(memory["disclosure"]["effective"], "disabled")
        self.assertNotIn(
            "memory",
            {
                str(item["name"])
                for item in self._gateway().runtime_manifests(
                    self.sessions.get(session_id)
                )
            },
        )

    def test_workflow_package_tools_are_not_fixed_core_tools(self) -> None:
        session = self.sessions.create(title="fixed base tools")
        session_id = str(session["id"])

        updated = self.policy.update_session(
            session_id,
            {
                "toolAllowlistMode": "explicit",
                "allowedTools": ["memory"],
                "capabilityDisclosurePreferences": {
                    "tool:todo": "disabled",
                    "tool:ask": "disabled",
                },
            },
        )

        self.assertNotIn("todo", updated["session"]["allowedTools"])
        catalog = self._gateway().manifests(session_id=session_id)
        todo = next(
            item
            for item in catalog["items"]
            if item["canonicalId"] == "tool:todo"
        )
        self.assertEqual(todo["authorization"]["state"], "denied")
        self.assertEqual(todo["disclosure"]["effective"], "disabled")
        ask = next(
            item
            for item in catalog["items"]
            if item["canonicalId"] == "tool:ask"
        )
        self.assertEqual(ask["authorization"]["state"], "authorized")
        self.assertEqual(ask["disclosure"]["effective"], "enabled")
        self.assertEqual(ask["disclosure"]["reason"], "required_session_tool")
        self.assertTrue(ask["alwaysAvailable"])
        # `ask` is owned by Pi Host rather than the PAW gateway, but it is a
        # real model-facing tool. The public catalogue must therefore project
        # the same manifest contract as the gateway tools so the composer can
        # count and explain it instead of silently dropping it.
        validate_contract(ask, "control-tool-manifest.v1.json")
        self.assertEqual(ask["runtimeOwner"], "pi_host")
        runtime_names = {
            str(item["name"])
            for item in self._gateway().runtime_manifests(
                self.sessions.get(session_id)
            )
        }
        self.assertNotIn("todo", runtime_names)
        self.assertNotIn("agent_goal", runtime_names)

    def test_unrestricted_profiles_expose_ask_and_every_tool_operation(self) -> None:
        for profile, execution_mode in (
            (FULL_ACCESS_TOOL_PROFILE, "per_action"),
            (DANGEROUS_AUTO_APPROVE_TOOL_PROFILE, "full_trust"),
        ):
            session = self.sessions.create(
                title=profile,
                mode="coordinator",
                tool_profile_version=profile,
                execution_mode=execution_mode,
                workspace_roots=[str(self.root)],
            )
            self.policy.update_session(
                str(session["id"]),
                {
                    "capabilityDisclosurePreferences": {
                        "tool:overview": "disabled",
                    },
                },
            )
            catalog = self._gateway().manifests(session_id=str(session["id"]))
            ask = next(
                item
                for item in catalog["items"]
                if item["canonicalId"] == "tool:ask"
            )
            self.assertEqual(ask["authorization"]["state"], "authorized")
            overview = next(
                item
                for item in catalog["items"]
                if item["canonicalId"] == "tool:overview"
            )
            self.assertEqual(overview["disclosure"]["effective"], "disabled")
            self.assertEqual(
                overview["disclosure"]["reason"],
                "session_preference",
            )
            self.assertNotIn(
                "overview",
                {
                    str(item["name"])
                    for item in self._gateway().runtime_manifests(session)
                },
            )
            for item in catalog["items"]:
                if item.get("kind") != "tool" or item.get("id") == "ask":
                    continue
                self.assertEqual(
                    item["profileOperations"][profile],
                    item["operations"],
                    msg=f"{profile} did not expose all operations for {item['id']}",
                )

    def test_approval_preview_state_fences_follow_live_unrestricted_profiles(self) -> None:
        class _PlanningManagement:
            def __init__(self) -> None:
                self.task = {
                    "id": "task:1",
                    "title": "task",
                    "status": "done",
                    "updatedAtMs": 2,
                }
                self.calls: list[dict[str, object]] = []

            def planning_dashboard(self, *, plan_date: str, project: str) -> dict[str, object]:
                del plan_date, project
                return {"tasks": [dict(self.task)]}

            def planning_task_action(self, payload: dict[str, object]) -> dict[str, object]:
                self.calls.append(dict(payload))
                return {
                    "task": dict(self.task),
                    "eventId": "event:1",
                    "undoAvailable": True,
                    "auditId": 1,
                }

        action_payload = {
            "taskId": "task:1",
            "action": "complete",
            "date": "2026-01-01",
        }
        base_state = {"status": "todo", "updatedAtMs": 1}
        management = _PlanningManagement()
        gateway = self._gateway()
        gateway.management = management

        for profile, execution_mode in (
            (FULL_ACCESS_TOOL_PROFILE, "per_action"),
            (DANGEROUS_AUTO_APPROVE_TOOL_PROFILE, "full_trust"),
        ):
            session = self.sessions.create(
                title=profile,
                mode="coordinator",
                tool_profile_version=profile,
                execution_mode=execution_mode,
                workspace_roots=[str(self.root)],
            )
            session_id = str(session["id"])
            approval = {
                "approvalId": f"approval:{profile}",
                "sessionId": session_id,
                "state": "approved",
                "toolId": "planning",
                "operation": "task_action",
                "preview": {
                    "actionPayload": action_payload,
                    "baseState": base_state,
                    "summary": "complete task",
                },
                "payloadSha256": "stale-preview-digest",
            }
            self.assertFalse(gateway._approval_preview_state_fence_applies(session_id))
            self.assertTrue(
                gateway._approval_payload_matches(approval, "current-preview-digest")
            )
            self.assertEqual(gateway.apply_approval(approval)["mutationApplied"], True)

        legacy = self.sessions.create(
            title="legacy preview fences",
            mode="coordinator",
            tool_profile_version="control-center-v1",
            execution_mode="workspace_managed",
            workspace_roots=[str(self.root)],
        )
        legacy_id = str(legacy["id"])
        legacy_digest = _approval_payload_digest(
            session_id=legacy_id,
            tool="planning",
            operation="task_action",
            action_payload=action_payload,
            base_state=base_state,
        )
        legacy_approval = {
            "approvalId": "approval:legacy",
            "sessionId": legacy_id,
            "state": "approved",
            "toolId": "planning",
            "operation": "task_action",
            "preview": {
                "actionPayload": action_payload,
                "baseState": base_state,
                "summary": "complete task",
            },
            "payloadSha256": legacy_digest,
        }
        self.assertTrue(gateway._approval_preview_state_fence_applies(legacy_id))
        self.assertFalse(
            gateway._approval_payload_matches(legacy_approval, "changed-preview-digest")
        )
        with self.assertRaisesRegex(ValueError, "task changed after"):
            gateway.apply_approval(legacy_approval)

    def test_reviewer_and_read_only_collaborator_manifest_fences_workspace_mutations(
        self,
    ) -> None:
        sessions = (
            self.sessions.create(
                title="Reviewer",
                mode="coordinator",
                tool_profile_version="control-center-v1",
                execution_mode="read_only",
                workspace_roots=[str(self.root)],
            ),
            self.sessions.create(
                title="read-only collaborator",
                mode="coordinator",
                tool_profile_version="subagent-readonly-v1",
                workspace_roots=[str(self.root)],
            ),
        )
        expected_lsp_reads = {
            "status",
            "symbols",
            "hover",
            "definition",
            "references",
            "diagnostics",
        }
        forbidden_tools = {"edit", "write", "workspace_edit", "workspace_write"}
        reserved_provider_names = {"ls", "read", "grep", "find", "bash"}
        gateway = self._gateway()
        for session in sessions:
            manifests = gateway.runtime_manifests(dict(session))
            by_name = {str(item["name"]): item for item in manifests}
            self.assertTrue(
                {
                    "workspace_list",
                    "workspace_read",
                    "workspace_search",
                    "workspace_shell",
                    "workspace_lsp",
                }
                <= set(by_name)
            )
            self.assertTrue(forbidden_tools.isdisjoint(by_name))
            self.assertTrue(reserved_provider_names.isdisjoint(by_name))
            self.assertEqual(
                by_name["workspace_shell"]["runtimeProjections"],
                [{"name": "bash", "operation": "run"}],
            )
            shell_run = next(
                branch
                for branch in by_name["workspace_shell"]["parameters"]["oneOf"]
                if branch["properties"]["op"]["const"] == "run"
            )
            self.assertEqual(shell_run["required"], ["op", "command"])
            self.assertNotIn("workspace_job", by_name)
            lsp = by_name["workspace_lsp"]
            branches = lsp["parameters"].get("oneOf", [])
            lsp_operations = {
                str(branch["properties"]["op"]["const"])
                for branch in branches
                if isinstance(branch, dict)
                and isinstance(branch.get("properties"), dict)
                and isinstance(branch["properties"].get("op"), dict)
                and "const" in branch["properties"]["op"]
            }
            self.assertEqual(lsp_operations, expected_lsp_reads)
            knowledge = by_name["knowledge"]
            knowledge_operations = {
                str(branch["properties"]["op"]["const"])
                for branch in knowledge["parameters"].get("oneOf", [])
            }
            self.assertEqual(
                knowledge_operations,
                {
                    "list_bases",
                    "get_base",
                    "list_documents",
                    "search",
                    "find",
                    "open",
                    "status",
                    "rebuild_preview",
                },
            )
        for blocked_tool, operation in (
            ("workspace_job", "start"),
            ("workspace_job", "cancel"),
            ("workspace_patch", "apply"),
            ("workspace_edit", "apply"),
            ("workspace_write", "apply"),
            ("workspace_lsp", "rename"),
            ("workspace_lsp", "code_action_apply"),
            ("knowledge", "create_base"),
            ("knowledge", "configure_base"),
            ("knowledge", "import_text"),
            ("knowledge", "rebuild"),
            ("bash", "run"),
            ("edit", "apply"),
            ("write", "apply"),
            ("apply_patch", "apply"),
        ):
            self.assertTrue(read_only_blocks_effect(blocked_tool, operation))
        self.assertFalse(read_only_blocks_effect("workspace_shell", "run"))

    def test_read_only_workspace_shell_executes_without_a_write_approval(self) -> None:
        session = self.sessions.create(
            title="Reviewer validation",
            mode="coordinator",
            tool_profile_version="subagent-readonly-v1",
            execution_mode="read_only",
            workspace_roots=[str(self.root)],
        )
        captured = []

        def execute(prepared):
            captured.append(prepared)
            return {
                "schemaVersion": "rag-ime.workspace-command-receipt.v1",
                "mutationApplied": False,
                "exitCode": 0,
                "output": "OK\n",
                "sourceReadOnly": prepared.source_read_only,
            }

        gateway = self._gateway(
            workspace_harness=WorkspaceHarness(executor=execute),
        )

        response = gateway.execute(
            {
                "schemaVersion": "rag-ime.agent-tool-call.v1",
                "sessionId": str(session["id"]),
                "tool": "workspace_shell",
                "toolCallId": "tool:review-validation",
                "args": {
                    "op": "run",
                    "command": "python3 -m unittest tests.test_example",
                    "cwd": str(self.root),
                },
            }
        )

        self.assertTrue(response["ok"])
        self.assertNotIn("approvalRequired", response["result"])
        self.assertEqual(response["result"]["exitCode"], 0)
        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0].source_read_only)
        self.assertFalse(captured[0].allow_network)

    def test_read_only_room_public_and_workspace_read_surfaces_remain_usable(
        self,
    ) -> None:
        session = self.sessions.create(
            title="Reviewer public controls",
            mode="coordinator",
            tool_profile_version="control-center-v1",
            execution_mode="read_only",
            workspace_roots=[str(self.root)],
        )
        (self.root / "review.txt").write_text(
            "read-only review evidence\n",
            encoding="utf-8",
        )
        room_gateway = _RoomCapabilityGateway()
        gateway = self._gateway(collaboration=room_gateway)
        session_id = str(session["id"])

        result = gateway.execute(
            {
                "schemaVersion": "rag-ime.agent-tool-call.v1",
                "sessionId": session_id,
                "tool": "room_partner",
                "toolCallId": "tool:room-partner:list",
                "args": {"op": "list"},
            }
        )
        self.assertTrue(result["ok"])
        self.assertEqual(
            [tool for tool, _args in room_gateway.room_calls],
            ["room_partner"],
        )

        read_result = gateway.execute(
            {
                "schemaVersion": "rag-ime.agent-tool-call.v1",
                "sessionId": session_id,
                "tool": "workspace_read",
                "toolCallId": "tool:workspace-read",
                "args": {"op": "read", "path": "review.txt"},
            }
        )
        self.assertEqual(read_result["result"]["content"], "read-only review evidence\n")
        search_result = gateway.execute(
            {
                "schemaVersion": "rag-ime.agent-tool-call.v1",
                "sessionId": session_id,
                "tool": "workspace_search",
                "toolCallId": "tool:workspace-search",
                "args": {"op": "search", "query": "review evidence"},
            }
        )
        self.assertTrue(search_result["result"]["matches"])
        lsp_status = gateway.execute(
            {
                "schemaVersion": "rag-ime.agent-tool-call.v1",
                "sessionId": session_id,
                "tool": "workspace_lsp",
                "toolCallId": "tool:workspace-lsp-status",
                "args": {"op": "status"},
            }
        )
        self.assertIn(
            lsp_status["result"]["state"],
            {"ready", "available", "unavailable"},
        )

    def test_live_read_only_policy_rejects_stale_grant_before_room_auth_and_apply(
        self,
    ) -> None:
        managed = self.sessions.create(
            title="managed before review",
            mode="coordinator",
            tool_profile_version="control-center-v1",
            execution_mode="workspace_managed",
            workspace_roots=[str(self.root)],
        )
        managed_id = str(managed["id"])
        self.assertTrue(managed["workspaceScopeSha256"])
        gateway = self._gateway(
            collaboration=_RoomCapabilityGateway(),
        )
        initial_names = {
            str(item["name"])
            for item in gateway.runtime_manifests(dict(managed))
        }
        self.assertIn("workspace_patch", initial_names)
        approval = self.sessions.create_approval(
            session_id=managed_id,
            tool_name="workspace_patch",
            operation="apply",
            payload_sha256="a" * 64,
            preview={"actionPayload": {}, "baseState": {}},
            risk_level="R2",
        )
        approved = self.sessions.decide_approval(
            str(approval["approvalId"]),
            approved=True,
            payload_sha256=str(approval["payloadSha256"]),
        )
        live = self.sessions.set_runtime_policy(
            managed_id,
            mode="coordinator",
            tool_profile_version="control-center-v1",
            execution_mode="read_only",
            grant_workspace_scope=True,
            allowed_tools=None,
            workspace_roots=[str(self.root)],
        )
        self.assertEqual(live["workspaceScopeSha256"], "")
        current_names = {
            str(item["name"])
            for item in gateway.runtime_manifests(dict(managed))
        }
        self.assertNotIn("workspace_patch", current_names)
        with self.assertRaisesRegex(ValueError, "read-only policy"):
            gateway.execute(
                {
                    "schemaVersion": "rag-ime.agent-tool-call.v1",
                    "sessionId": managed_id,
                    "tool": "workspace_patch",
                    "toolCallId": "tool:stale-patch",
                    "args": {
                        "op": "apply",
                        "path": "review.txt",
                        "oldText": "before",
                        "newText": "after",
                        "expectedOccurrences": 1,
                    },
                }
            )
        collaboration = gateway.collaboration
        self.assertIsInstance(collaboration, _RoomCapabilityGateway)
        with self.assertRaisesRegex(ValueError, "read-only policy"):
            gateway._execute_product_tool(
                request={"sessionId": managed_id},
                session=managed,
                tool="workspace_patch",
                args={"op": "apply"},
                spec=_TOOL_SPEC_BY_ID["workspace_patch"],
                operation="apply",
            )
        with self.assertRaisesRegex(ValueError, "read-only policy"):
            gateway.apply_approval(approved)


if __name__ == "__main__":
    unittest.main()
