from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.contracts.json_schema import ContractValidationError, validate_contract
from rag_ime.agent_tools import (
    ControlToolGateway,
    _RUNTIME_TOOL_PROJECTIONS,
    _TOOL_SPEC_BY_ID,
    _normalize_runtime_tool_call,
    _runtime_tool_parameter_schema,
)


class AgentToolRuntimeContractTest(unittest.TestCase):
    @staticmethod
    def _branch(manifest, operation: str):
        schema = manifest["parameters"]
        branch = next(
            branch
            for branch in schema["oneOf"]
            if branch["properties"]["op"]["const"] == operation
        )
        return {
            **branch,
            "additionalProperties": branch.get(
                "additionalProperties",
                schema.get("additionalProperties"),
            ),
            "properties": {
                **schema.get("properties", {}),
                **branch.get("properties", {}),
            },
        }

    def _runtime_contracts(self, *, mode: str = "assistant", profile: str = "control-center-v1"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        store = AgentSessionStore(root / "agent.sqlite")
        store.initialize()
        session = store.create(
            title="tool contract",
            mode=mode,
            workspace_roots=[str(root)] if mode == "coordinator" else [],
            tool_profile_version=profile,
            created_at_ms=1,
        )
        store.set_disclosure_preferences(str(session["id"]), {"tool:memory": "enabled"})
        session = store.get(str(session["id"]))
        gateway = ControlToolGateway(
            sessions=store,
            management=object(),
            core=object(),
            project="contract-test",
        )
        public_catalog = gateway.manifests(session_id=str(session["id"]))["items"]
        # Runtime manifests intentionally include modelVisible=false execution
        # targets. Compare them against the internal projection, while keeping
        # Pi-host-native entries such as `ask` from the public handshake.
        catalog = gateway._manifest_items(session)
        catalog.extend(
            item for item in public_catalog if item.get("runtimeOwner") == "pi_host"
        )
        return catalog, gateway.runtime_manifests(session)

    def test_runtime_schema_projection_does_not_share_mutable_global_branches(self) -> None:
        operations = ["init", "start", "done", "drop", "block", "unblock", "append", "view", "rm"]
        todo = {"parameters": _runtime_tool_parameter_schema("todo", operations)}
        append = next(
            branch
            for branch in todo["parameters"]["oneOf"]
            if branch["properties"]["op"]["const"] == "append"
        )
        append.pop("additionalProperties")

        fresh_todo = {"parameters": _runtime_tool_parameter_schema("todo", operations)}
        fresh_append = next(
            branch
            for branch in fresh_todo["parameters"]["oneOf"]
            if branch["properties"]["op"]["const"] == "append"
        )

        self.assertFalse(fresh_append["additionalProperties"])

    def test_native_workspace_names_normalize_to_single_gateway_targets(self) -> None:
        cases = {
            "ls": ({"path": "src"}, "workspace_list", {"op": "list", "path": "src"}),
            "read": (
                {"path": "README.md", "offset": 3, "limit": 10, "asArtifact": True},
                "workspace_read",
                {
                    "op": "read",
                    "path": "README.md",
                    "lineOffset": 3,
                    "lineLimit": 10,
                    "asArtifact": True,
                },
            ),
            "grep": (
                {"pattern": "needle", "ignoreCase": True},
                "workspace_search",
                {
                    "op": "search",
                    "query": "needle",
                    "mode": "content",
                    "patternKind": "regex",
                    "caseSensitive": False,
                },
            ),
            "find": (
                {"pattern": "*.py"},
                "workspace_search",
                {
                    "op": "search",
                    "query": "*.py",
                    "mode": "name",
                    "patternKind": "glob",
                },
            ),
            "edit": (
                {"path": "a.py", "resourceRevision": "sha256:" + "a" * 64, "edits": []},
                "workspace_edit",
                {
                    "op": "apply",
                    "path": "a.py",
                    "resourceRevision": "sha256:" + "a" * 64,
                    "edits": [],
                },
            ),
            "write": (
                {"path": "a.py", "resourceRevision": "missing", "content": "x"},
                "workspace_write",
                {"op": "apply", "path": "a.py", "resourceRevision": "missing", "content": "x"},
            ),
            "bash": (
                {"command": "pwd", "timeout": 12},
                "workspace_shell",
                {"op": "run", "command": "pwd", "timeoutSeconds": 12},
            ),
        }
        for name, (args, expected_tool, expected_args) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(
                    _normalize_runtime_tool_call(name, args),
                    (expected_tool, expected_args),
                )

    def test_native_html_read_automatically_requests_managed_preview(self) -> None:
        self.assertEqual(
            _normalize_runtime_tool_call("read", {"path": "reports/result.html"}),
            (
                "workspace_read",
                {"op": "read", "path": "reports/result.html", "asArtifact": True},
            ),
        )
        self.assertEqual(
            _normalize_runtime_tool_call(
                "read",
                {"path": "reports/result.html", "asArtifact": False},
            ),
            (
                "workspace_read",
                {"op": "read", "path": "reports/result.html", "asArtifact": False},
            ),
        )

    def test_planning_manifest_requires_dashboard_identifiers_for_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = AgentSessionStore(Path(temporary) / "agent.sqlite")
            store.initialize()
            session = store.create(title="tool contract", created_at_ms=1)
            gateway = ControlToolGateway(
                sessions=store,
                management=object(),
                core=object(),
                project="contract-test",
            )

            manifests = gateway.runtime_manifests(session)

        planning = next(item for item in manifests if item["name"] == "planning")
        self.assertEqual(planning["description"], "规划与任务")
        schema = planning["parameters"]
        self.assertEqual(schema["type"], "object")
        task_action = next(
            branch
            for branch in schema["oneOf"]
            if branch["properties"]["op"]["const"] == "task_action"
        )
        self.assertEqual(task_action["required"], ["op", "taskId", "date", "action"])
        self.assertIn("dashboard", task_action["properties"]["taskId"]["description"])

    def test_every_enabled_runtime_tool_exposes_exact_operation_branches(self) -> None:
        catalog, manifests = self._runtime_contracts(mode="coordinator")
        effective: dict[str, set[str]] = {}
        for item in catalog:
            if item["enabled"] is not True or item.get("runtimeOwner") == "pi_host":
                continue
            tool_id = str(item["id"])
            if tool_id in {"todo", "agent_goal"}:
                continue
            operations = {str(value) for value in item["effectiveOperations"]}
            effective[tool_id] = operations
        native_ask = next(item for item in catalog if item["id"] == "ask")
        self.assertEqual(native_ask["runtimeOwner"], "pi_host")
        self.assertTrue(native_ask["alwaysAvailable"])

        self.assertEqual(
            {str(manifest["name"]) for manifest in manifests},
            set(effective),
        )
        for manifest in manifests:
            with self.subTest(tool=manifest["name"]):
                self.assertEqual(
                    set(manifest) & {"name", "when", "notFor", "input", "output", "does"},
                    {"name", "when", "notFor", "input", "output", "does"},
                )
                self.assertTrue(manifest["when"])
                self.assertTrue(manifest["notFor"])
                self.assertTrue(manifest["input"])
                self.assertTrue(manifest["output"])
                self.assertTrue(manifest["does"])
                schema = manifest["parameters"]
                self.assertEqual(schema["type"], "object")
                branches = schema.get("oneOf", [])
                operation_branches = [
                    branch
                    for branch in branches
                    if isinstance(branch.get("properties", {}).get("op"), dict)
                    and "const" in branch["properties"]["op"]
                ]
                self.assertEqual(
                    {
                        branch["properties"]["op"]["const"]
                        for branch in operation_branches
                    },
                    effective[manifest["name"]],
                )
                for branch in operation_branches:
                    self.assertIs(
                        branch.get(
                            "additionalProperties",
                            schema.get("additionalProperties"),
                        ),
                        False,
                    )
                    self.assertIn("op", branch["required"])
                compatibility_branches = [
                    branch for branch in branches if branch not in operation_branches
                ]
                if manifest["name"] == "memory":
                    self.assertEqual(len(compatibility_branches), 1)
                    self.assertEqual(compatibility_branches[0]["required"], ["query"])
                    self.assertEqual(
                        compatibility_branches[0]["not"],
                        {"required": ["op"]},
                    )
                else:
                    self.assertEqual(compatibility_branches, [])

    def test_runtime_manifest_keeps_internal_catalog_and_public_cards_bounded(self) -> None:
        _catalog, manifests = self._runtime_contracts(mode="coordinator")
        encoded = json.dumps(
            manifests,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        cards = [
            {
                key: manifest[key]
                for key in ("name", "when", "notFor", "input", "output", "does")
            }
            for manifest in manifests
        ]
        public_encoded = json.dumps(
            cards,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        # The full registry is an internal Product -> Pi handshake and includes
        # every deferred JSON Schema plus the hidden native-tool execution
        # targets. Only the compact cards enter the prompt, so keep their
        # tighter Provider-facing budget independent from this transport
        # envelope.
        # Goal lifecycle, fixed Todo policy, explicit Knowledge rerank,
        # Session navigation, and delegated-runtime capability fences add full
        # internal schemas. Their compact public cards remain covered by the
        # independent Provider-facing limit.
        self.assertLess(len(encoded), 56_000)
        self.assertLess(len(public_encoded), 12_000)
        self.assertTrue(all("profile" not in manifest for manifest in manifests))
        self.assertTrue(
            all(
                manifest["description"]
                == _TOOL_SPEC_BY_ID[str(manifest["name"])]["displayName"]
                for manifest in manifests
            )
        )
        self.assertIn("work_documents", {manifest["name"] for manifest in manifests})
        by_name = {manifest["name"]: manifest for manifest in manifests}
        self.assertTrue(
            any("Chrome/Edge" in item for item in by_name["browser"]["notFor"])
        )
        self.assertTrue(
            any("独立 Chrome/Edge" in item for item in by_name["desktop_semantic"]["notFor"])
        )
        self.assertNotIn("todo", {manifest["name"] for manifest in manifests})
        self.assertNotIn("agent_goal", {manifest["name"] for manifest in manifests})

    def test_coordinator_parameters_follow_the_disclosed_operation_without_losing_delegation(self) -> None:
        _catalog, manifests = self._runtime_contracts(mode="coordinator")
        ordinary = next(item["parameters"] for item in manifests if item["name"] == "agents")
        coordinator_fields = {"action", "targetId", "clientRequestId", "input"}
        self.assertNotIn("coordinator", ordinary["properties"]["op"]["enum"])
        self.assertTrue(coordinator_fields.isdisjoint(ordinary["properties"]))

        enabled = _runtime_tool_parameter_schema(
            "agents", [*ordinary["properties"]["op"]["enum"], "coordinator"],
        )
        self.assertTrue(coordinator_fields.issubset(enabled["properties"]))
        branch = self._branch({"parameters": enabled}, "coordinator")
        self.assertEqual(branch["required"], ["op", "action"])
        self.assertEqual(
            branch["properties"]["action"]["enum"],
            ["read", "create_session", "create_room", "prompt", "stop", "resume"],
        )
        # Existing delegation access, tools, skills and workspace arguments are
        # identical; only the unavailable coordinator's fields are omitted.
        self.assertEqual(
            {key: value for key, value in enabled["properties"].items()
             if key not in coordinator_fields | {"op"}},
            {key: value for key, value in ordinary["properties"].items() if key != "op"},
        )
        self.assertEqual(enabled["oneOf"][:-1], ordinary["oneOf"])
        validate_contract({"op": "catalog"}, ordinary)
        validate_contract({"op": "call", "targetRunId": "run-1", "message": "Continue"}, ordinary)
        creation = {
            "op": "coordinator", "action": "create_session",
            "clientRequestId": "create-1", "input": {"task": "Inspect the supplied notes"},
        }
        validate_contract(creation, enabled)
        for payload in (creation, {"op": "catalog", "input": {}}):
            with self.assertRaises(ContractValidationError):
                validate_contract(payload, ordinary)
        for payload in ({"op": "coordinator"}, {"op": "coordinator", "action": "delete_all"}):
            with self.assertRaises(ContractValidationError):
                validate_contract(payload, enabled)
        enabled["properties"].pop("input")
        enabled["oneOf"][-1]["required"].append("targetId")
        fresh = _runtime_tool_parameter_schema("agents", ["coordinator"])
        self.assertIn("input", fresh["properties"])
        self.assertEqual(fresh["oneOf"][0]["required"], ["op", "action"])

    def test_runtime_contracts_require_tool_specific_identifiers_and_payloads(self) -> None:
        _catalog, manifests = self._runtime_contracts(mode="coordinator")
        tools = {manifest["name"]: manifest for manifest in manifests}
        legacy_todo = {
            "parameters": _runtime_tool_parameter_schema(
                "todo",
                ["init", "start", "done", "drop", "block", "unblock", "append", "view", "rm"],
            )
        }
        legacy_goal = {
            "parameters": _runtime_tool_parameter_schema(
                "agent_goal",
                ["list", "confirm_setup", "update", "pause", "resume", "complete", "cancel"],
            )
        }
        internal_edit = {
            "parameters": _runtime_tool_parameter_schema("workspace_edit", ["apply"])
        }
        internal_write = {
            "parameters": _runtime_tool_parameter_schema("workspace_write", ["apply"])
        }
        internal_shell = {
            "parameters": _runtime_tool_parameter_schema("workspace_shell", ["run"])
        }

        self.assertEqual(
            self._branch(tools["workspace_patch"], "apply")["required"],
            ["op", "path", "oldText", "newText"],
        )
        self.assertEqual(
            self._branch(internal_edit, "apply")["required"],
            ["op", "path", "resourceRevision", "edits"],
        )
        self.assertEqual(
            self._branch(internal_write, "apply")["required"],
            ["op", "path", "resourceRevision", "content"],
        )
        self.assertEqual(
            self._branch(tools["workspace_lsp"], "rename")["required"],
            ["op", "path", "newName"],
        )
        self.assertEqual(
            self._branch(tools["workspace_lsp"], "code_action_apply")["required"],
            ["op", "path", "title"],
        )
        self.assertEqual(
            self._branch(internal_shell, "run")["required"],
            ["op", "command"],
        )
        self.assertEqual(
            self._branch(tools["plugins"], "create_package")["required"],
            ["op", "draftId", "packageJson", "files"],
        )
        self.assertEqual(
            self._branch(tools["plugins"], "validate")["required"],
            ["op"],
        )
        self.assertEqual(
            self._branch(tools["memory"], "read")["required"],
            ["op", "bookId"],
        )
        self.assertEqual(
            self._branch(tools["knowledge"], "search")["required"],
            ["op", "kbId", "query"],
        )
        self.assertEqual(
            self._branch(tools["agent_schedule"], "schedule")["required"],
            ["op", "instruction", "targetType", "wakeAtMs"],
        )
        self.assertEqual(
            self._branch(tools["browser"], "navigate")["required"],
            ["op", "url"],
        )
        self.assertEqual(
            self._branch(tools["browser"], "back")["required"],
            ["op"],
        )
        self.assertEqual(
            self._branch(tools["browser"], "forward")["required"],
            ["op"],
        )
        self.assertEqual(
            self._branch(tools["browser"], "type")["required"],
            ["op", "refId", "text"],
        )
        self.assertEqual(
            self._branch(tools["desktop_semantic"], "act")["required"],
            ["op", "action"],
        )
        self.assertEqual(
            self._branch(tools["desktop_semantic"], "find")["required"],
            ["op", "match"],
        )
        self.assertEqual(
            self._branch(tools["desktop_semantic"], "inspect")["properties"]["maxNodes"]["maximum"],
            500,
        )

        delegate = self._branch(tools["agents"], "delegate")
        self.assertEqual(
            delegate["oneOf"],
            [
                {
                    "required": [
                        "agent",
                        "task",
                        "expectedOutput",
                        "acceptanceCriteria",
                    ],
                    "not": {"required": ["tasks"]},
                },
                {
                    "required": ["tasks"],
                    "not": {
                        "anyOf": [
                            {"required": [field]}
                            for field in (
                                "agent",
                                "version",
                                "task",
                                "expectedOutput",
                                "acceptanceCriteria",
                                "outputSchema",
                                "modelProfile",
                                "thinkingLevel",
                                "budget",
                                "access",
                                "allowedTools",
                                "piSkillsEnabled",
                                "codexSkillsEnabled",
                                "workspaceRoots",
                            )
                        ]
                    },
                },
            ],
        )
        self.assertEqual(delegate["properties"]["todoTask"]["maxLength"], 240)
        self.assertIn("可选 Todo 导航链接", delegate["properties"]["todoTask"]["description"])
        todo_branches = {
            branch["properties"]["op"]["const"]: branch
            for branch in legacy_todo["parameters"]["oneOf"]
        }
        self.assertEqual(
            set(todo_branches),
            {
                "init",
                "start",
                "done",
                "drop",
                "block",
                "unblock",
                "append",
                "view",
                "rm",
            },
        )
        self.assertEqual(
            todo_branches["init"]["oneOf"],
            [{"required": ["list"]}, {"required": ["items"]}],
        )
        self.assertEqual(todo_branches["start"]["required"], ["op", "task"])
        self.assertEqual(
            todo_branches["append"]["required"],
            ["op", "phase", "items"],
        )
        self.assertEqual(todo_branches["view"]["required"], ["op"])
        for operation in ("done", "drop", "block", "unblock"):
            with self.subTest(todo_operation=operation):
                self.assertEqual(todo_branches[operation]["required"], ["op"])
                self.assertEqual(
                    todo_branches[operation]["oneOf"],
                    [{"required": ["task"]}, {"required": ["phase"]}],
                )
        self.assertEqual(todo_branches["rm"]["required"], ["op"])
        self.assertEqual(
            todo_branches["rm"]["not"],
            {"required": ["task", "phase"]},
        )
        abort = self._branch(tools["agents"], "abort")
        self.assertEqual(
            abort["anyOf"],
            [{"required": ["runId"]}, {"required": ["batchId"]}],
        )
        self.assertEqual(self._branch(tools["agents"], "status")["required"], ["op"])
        goal_setup = self._branch(legacy_goal, "confirm_setup")
        self.assertEqual(
            goal_setup["required"],
            ["op", "confirmed", "objective"],
        )
        goal_complete = self._branch(legacy_goal, "complete")
        self.assertEqual(
            goal_complete["properties"]["evidence"]["items"]["required"],
            ["kind", "summary", "reference"],
        )
        model_apply = self._branch(tools["models"], "profile_apply")
        self.assertEqual(model_apply["required"], ["op", "slot"])
        self.assertEqual(
            model_apply["anyOf"],
            [
                {"required": ["provider"]},
                {"required": ["endpoint"]},
                {"required": ["model"]},
            ],
        )

    def test_progressive_optional_arguments_are_not_hidden_from_pi(self) -> None:
        _catalog, manifests = self._runtime_contracts(mode="coordinator")
        tools = {manifest["name"]: manifest for manifest in manifests}

        # Pi owns the model-facing ls/read/grep/find/edit/write/bash names. PAW
        # sends hidden workspace_* execution targets plus explicit native
        # projections, never a second backend registration of Pi's names.
        projections = {
            "workspace_list": [{"name": "ls", "operation": "list"}],
            "workspace_read": [{"name": "read", "operation": "read"}],
            "workspace_search": [
                {"name": "grep", "operation": "search"},
                {"name": "find", "operation": "search"},
            ],
            "workspace_edit": [{"name": "edit", "operation": "apply"}],
            "workspace_write": [{"name": "write", "operation": "apply"}],
            "workspace_shell": [{"name": "bash", "operation": "run"}],
        }
        for internal_name, expected_projections in projections.items():
            self.assertIn(internal_name, tools)
            self.assertIs(tools[internal_name]["modelVisible"], False)
            self.assertEqual(
                tools[internal_name]["runtimeProjections"],
                expected_projections,
            )
        for reserved_name in (
            "ls",
            "read",
            "grep",
            "find",
            "edit",
            "write",
            "bash",
        ):
            self.assertNotIn(reserved_name, tools)
        for internal_name in (
            "workspace_list",
            "workspace_read",
            "workspace_search",
            "workspace_edit",
            "workspace_write",
            "workspace_shell",
        ):
            self.assertIn("op", self._branch(tools[internal_name], tools[internal_name]["runtimeProjections"][0]["operation"])["required"])
        lsp_properties = self._branch(tools["workspace_lsp"], "references")["properties"]
        self.assertIn("includeDeclaration", lsp_properties)
        self.assertEqual(lsp_properties["timeoutMs"]["maximum"], 20_000)
        self.assertEqual(lsp_properties["query"]["maxLength"], 240)
        audit_properties = self._branch(tools["configuration"], "audit")["properties"]
        self.assertIn("action", audit_properties)

    def test_readonly_profile_filters_parameter_branches_with_operations(self) -> None:
        catalog, manifests = self._runtime_contracts(
            mode="coordinator",
            profile="subagent-readonly-v1",
        )
        effective = {
            item["id"]: set(item["effectiveOperations"])
            for item in catalog
            if item["enabled"] is True
        }
        self.assertEqual(effective["workspace_list"], {"list"})
        self.assertEqual(effective["workspace_read"], {"read"})
        self.assertEqual(effective["workspace_search"], {"search"})
        self.assertEqual(
            effective["workspace_lsp"],
            {
                "status",
                "symbols",
                "hover",
                "definition",
                "references",
                "diagnostics",
            },
        )
        self.assertNotIn("rename", effective["workspace_lsp"])
        self.assertNotIn("code_action_apply", effective["workspace_lsp"])
        self.assertEqual(
            effective["agents"],
            {"catalog", "status", "artifact", "delegate", "call", "abort"},
        )
        self.assertIn("delegate", effective["agents"])
        self.assertIn("call", effective["agents"])
        self.assertIn("abort", effective["agents"])
        self.assertNotIn("workspace_patch", effective)
        self.assertEqual(effective["workspace_shell"], {"run"})

        for manifest in manifests:
            with self.subTest(tool=manifest["name"]):
                operations = {
                    branch["properties"]["op"]["const"]
                    for branch in manifest["parameters"]["oneOf"]
                    if "op" in branch.get("properties", {})
                }
                self.assertEqual(operations, effective[manifest["name"]])
                self.assertTrue(operations)

    def test_readonly_profile_keeps_validation_shell_authority_behind_native_bash(self) -> None:
        catalog, manifests = self._runtime_contracts(
            mode="coordinator",
            profile="subagent-readonly-v1",
        )
        shell = next(item for item in catalog if item["id"] == "workspace_shell")

        self.assertTrue(shell["enabled"])
        self.assertEqual(shell["effectiveOperations"], ["run"])
        by_name = {item["name"]: item for item in manifests}
        self.assertIn("workspace_shell", by_name)
        self.assertIs(by_name["workspace_shell"]["modelVisible"], False)
        self.assertEqual(
            by_name["workspace_shell"]["runtimeProjections"],
            [{"name": "bash", "operation": "run"}],
        )
        self.assertNotIn("bash", by_name)


if __name__ == "__main__":
    unittest.main()
