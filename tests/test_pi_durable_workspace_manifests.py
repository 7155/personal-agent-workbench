from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jsonschema

from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.agent_tools import ControlToolGateway
from tests.sqlite_fixtures import copy_current_database


_CLASSIC_PROJECTIONS = {
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
_DIRECT_INPUTS = {
    "workspace_list": {"op": "list"},
    "workspace_read": {"op": "read", "path": "README.md"},
    "workspace_search": {"op": "search", "query": "marker", "mode": "content"},
    "workspace_lsp": {"op": "status"},
    "workspace_edit": {
        "op": "apply", "path": "notes.txt", "resourceRevision": "sha256:" + "a" * 64,
        "edits": [{"oldText": "before", "newText": "after"}],
    },
    "workspace_patch": {
        "op": "apply", "path": "notes.txt", "oldText": "before", "newText": "after",
    },
    "workspace_write": {
        "op": "apply", "path": "new.txt", "resourceRevision": "missing", "content": "text",
    },
    "workspace_shell": {
        "op": "run", "command": "printf bounded", "timeoutSeconds": 5, "allowNetwork": False,
    },
}


class PiDurableWorkspaceManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-durable-workspace-manifest-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        support_patch = patch.dict(os.environ, {"RAG_IME_APP_SUPPORT_DIR": str(self.root / "support")})
        support_patch.start()
        self.addCleanup(support_patch.stop)
        database = self.root / "paw.sqlite"
        copy_current_database(database)
        self.sessions = AgentSessionStore(database)
        self.sessions.initialize()
        self.addCleanup(self.sessions.close)
        self.gateway = ControlToolGateway(
            sessions=self.sessions,
            management=SimpleNamespace(),
            core=SimpleNamespace(),
            project="workspace-manifest-test",
            desktop_client=SimpleNamespace(),
            memory_enabled_provider=lambda: True,
        )

    def _session(self, engine: str, *, mode: str = "coordinator", roots: bool = True) -> dict:
        return self.sessions.create(
            title=f"{engine} governed workspace",
            mode=mode,
            runtime_engine=engine,
            workspace_roots=[str(self.root)] if roots else [],
            created_at_ms=1,
        )

    def _manifests(self, session: dict) -> dict:
        return {str(item["name"]): item for item in self.gateway.runtime_manifests(session)}

    def test_durable_direct_workspace_schemas_retain_required_operation_and_arguments(self) -> None:
        manifests = self._manifests(self._session("durable"))
        for name, args in _DIRECT_INPUTS.items():
            with self.subTest(name=name):
                manifest = manifests[name]
                self.assertIsNot(manifest.get("modelVisible"), False)
                self.assertNotIn("runtimeProjections", manifest)
                schema = manifest["parameters"]
                jsonschema.validate(args, schema)
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate({key: value for key, value in args.items() if key != "op"}, schema)
        for name, required_argument in (
            ("workspace_shell", "command"), ("workspace_read", "path"),
            ("workspace_edit", "resourceRevision"), ("workspace_write", "resourceRevision"),
        ):
            with self.subTest(name=name, required_argument=required_argument):
                args = dict(_DIRECT_INPUTS[name])
                del args[required_argument]
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(args, manifests[name]["parameters"])

    def test_classic_aliases_and_hidden_targets_remain_unchanged(self) -> None:
        manifests = self._manifests(self._session("classic"))
        for name, projections in _CLASSIC_PROJECTIONS.items():
            with self.subTest(name=name):
                self.assertIs(manifests[name].get("modelVisible"), False)
                self.assertEqual(manifests[name]["runtimeProjections"], projections)
        self.assertIs(manifests["workspace_patch"].get("modelVisible"), False)
        self.assertNotIn("runtimeProjections", manifests["workspace_patch"])
        self.assertNotIn("modelVisible", manifests["workspace_lsp"])

    def test_engine_selection_reloads_immutable_persisted_session(self) -> None:
        durable = self._session("durable")
        durable["runtimeEngine"] = "classic"
        shell = self._manifests(durable)["workspace_shell"]
        self.assertIsNot(shell.get("modelVisible"), False)
        self.assertNotIn("runtimeProjections", shell)
        classic = self._session("classic")
        classic["runtimeEngine"] = "durable"
        shell = self._manifests(classic)["workspace_shell"]
        self.assertIs(shell.get("modelVisible"), False)
        self.assertEqual(shell["runtimeProjections"], _CLASSIC_PROJECTIONS["workspace_shell"])

    def test_durable_does_not_disclose_workspace_without_mode_and_roots(self) -> None:
        for mode, roots in (("assistant", False), ("coordinator", False)):
            with self.subTest(mode=mode, roots=roots):
                manifests = self._manifests(self._session("durable", mode=mode, roots=roots))
                self.assertFalse(any(name.startswith("workspace_") for name in manifests))
        with self.assertRaisesRegex(ValueError, "assistant conversation sessions cannot carry workspace roots"):
            self._session("durable", mode="assistant", roots=True)

    def test_durable_respects_current_tool_allowlist_and_disclosure(self) -> None:
        session = self._session("durable")
        original = dict(session)
        self.sessions.set_runtime_policy(
            str(session["id"]), mode="coordinator", tool_profile_version="control-center-v1",
            allowed_tools=["workspace_read"], updated_at_ms=2,
        )
        manifests = self._manifests(original)
        self.assertEqual({name for name in manifests if name.startswith("workspace_")}, {"workspace_read"})
        self.assertIsNot(manifests["workspace_read"].get("modelVisible"), False)
        self.sessions.set_disclosure_preferences(
            str(session["id"]), {"tool:workspace_read": "disabled"}, updated_at_ms=3,
        )
        self.assertNotIn("workspace_read", self._manifests(original))

    def test_durable_readonly_profile_retains_existing_operation_limits(self) -> None:
        session = self._session("durable")
        self.sessions.set_runtime_policy(
            str(session["id"]), mode="coordinator", tool_profile_version="subagent-readonly-v1",
            allowed_tools=None, updated_at_ms=2,
        )
        manifests = self._manifests(session)
        self.assertEqual(
            {name for name in manifests if name.startswith("workspace_")},
            {"workspace_list", "workspace_read", "workspace_search", "workspace_lsp", "workspace_shell"},
        )
        for name in ("workspace_list", "workspace_read", "workspace_search", "workspace_lsp"):
            self.assertIsNot(manifests[name].get("modelVisible"), False)
            self.assertNotIn("runtimeProjections", manifests[name])
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate({"op": "rename"}, manifests["workspace_lsp"]["parameters"])
        # Read-only policy already permits governed Shell source validation;
        # compare allowed operations with Classic rather than removing it.
        classic = self._session("classic")
        self.sessions.set_runtime_policy(
            str(classic["id"]), mode="coordinator", tool_profile_version="subagent-readonly-v1",
            allowed_tools=None, updated_at_ms=2,
        )
        classic_manifests = self._manifests(classic)
        self.assertEqual(
            {name: item["parameters"] for name, item in manifests.items() if name.startswith("workspace_")},
            {name: item["parameters"] for name, item in classic_manifests.items() if name.startswith("workspace_")},
        )

    def test_durable_does_not_introduce_native_aliases_or_unavailable_capabilities(self) -> None:
        session = self._session("durable")
        self.sessions.set_disclosure_preferences(
            str(session["id"]), {"tool:memory": "enabled"}, updated_at_ms=2,
        )
        manifests = self._manifests(session)
        self.assertFalse({"ls", "read", "grep", "find", "edit", "write", "bash", "codemode", "nativeMcp", "plugins", "workspace_job"} & manifests.keys())
        self.assertEqual(manifests["memory"]["runtimeProjections"], [{"name": "memory_capture", "operation": "capture"}])

    @unittest.skipUnless(os.environ.get("PAW_PI_WORKSPACE_TOOL_TEST_HOST_DIST"), "requires paired Pi Host dist for native catalog/load contract")
    def test_actual_native_catalog_and_loader_accept_canonical_durable_input(self) -> None:
        node = os.environ.get("PAW_PI_WORKSPACE_TOOL_TEST_NODE", "node")
        dist = os.environ["PAW_PI_WORKSPACE_TOOL_TEST_HOST_DIST"]
        durable = list(self._manifests(self._session("durable")).values())
        classic = list(self._manifests(self._session("classic")).values())
        completed = subprocess.run(
            [node, str(Path(__file__).parent / "fixtures" / "pi_durable_workspace_tool_load.mjs")],
            input=json.dumps({"durable": durable, "classic": classic, "names": list(_DIRECT_INPUTS)}),
            text=True, capture_output=True, timeout=20, check=False,
            env={"PATH": os.environ.get("PATH", ""), "HOME": str(self.root), "PAW_PI_WORKSPACE_TOOL_TEST_HOST_DIST": dist},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertIsNone(result["durable"]["error"], result["durable"]["error"])
        for name, args in _DIRECT_INPUTS.items():
            with self.subTest(name=name):
                self.assertIn(name, result["durable"]["catalog"])
                schema = result["durable"]["loaded"][name]["parameters"]
                jsonschema.validate(args, schema)
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate({key: value for key, value in args.items() if key != "op"}, schema)
        self.assertNotIn("workspace_shell", result["classic"]["catalog"])
        self.assertIn("Unknown or unavailable product tool: workspace_shell", result["classic"]["error"])


if __name__ == "__main__":
    unittest.main()
