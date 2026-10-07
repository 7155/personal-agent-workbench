"""Validate Native and registered legacy model schemas against the real Gateway."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from rag_ime.contracts.json_schema import ContractValidationError, validate_contract
from tests import test_agent_governed_memory_tools as governed


class PiCorrectPreviewSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.TemporaryDirectory(prefix="paw-memory-model-schema-") as directory:
            script = Path(directory) / "schema.mjs"
            script.write_text("""const tools = [];
globalThis.fetch = () => { throw new Error('schema registration must not request a gateway'); };
const extension = await import(process.env.TEST_EXTENSION_URL);
extension.default({registerTool: tool => tools.push(tool)});
console.log(JSON.stringify(tools.find(tool => tool.name === 'memory').parameters));
""", encoding="utf-8")
            env = {**os.environ,
                   "RAG_IME_AGENT_SESSION_MODE": "assistant",
                   "RAG_IME_AGENT_TOOL_PROFILE_VERSION": "control-center-v1",
                   "RAG_IME_AGENT_EXECUTION_MODE": "per_action",
                   "RAG_IME_AGENT_ROOM_BOUND": "0",
                   "TEST_EXTENSION_URL": (Path(__file__).parents[1] / "integrations/pi/rag-ime-control.ts").as_uri()}
            process = subprocess.run(
                ["node", "--experimental-strip-types", str(script)], env=env,
                capture_output=True, text=True, timeout=20, check=True,
            )
            cls.model_schema = json.loads(process.stdout)

    def setUp(self) -> None:
        self.owner = governed.GovernedMemoryToolTests()
        self.owner.setUp()
        self.addCleanup(self.owner.tearDown)
        native = next(tool for tool in self.owner.gateway.runtime_manifests(self.owner.session) if tool["name"] == "memory")
        self.schemas = {"legacy": self.model_schema, "native": native["parameters"]}
        self.arguments = {
            "op": "correct_preview", "targetId": "atom:model-choice",
            "text": "当前使用的是 100M 自训练模型", "memoryKind": "fact",
            "evidenceIds": [self.owner.evidence_ids["correct"], self.owner.evidence_ids["model-eval"]],
        }

    def test_missing_and_empty_reason_are_rejected_before_call_and_by_gateway(self) -> None:
        before = self.owner._memory_table_counts()
        for reason in (None, ""):
            with self.subTest(reason=reason):
                arguments = {**self.arguments, **({} if reason is None else {"reason": reason})}
                with self.assertRaisesRegex(ValueError, "reason"):
                    self.owner._execute("memory", arguments["op"], **{key: value for key, value in arguments.items() if key != "op"})
                for transport, schema in self.schemas.items():
                    with self.subTest(transport=transport), self.assertRaises(ContractValidationError):
                        validate_contract(arguments, schema)
        self.assertEqual(self.owner._memory_table_counts(), before)

    def test_legal_reason_including_400_char_boundary_produces_only_preview(self) -> None:
        before = self.owner._memory_table_counts()
        for reason in ("用户明确更正原模型事实", "用户确认" * 100):
            with self.subTest(length=len(reason)):
                arguments = {**self.arguments, "reason": reason}
                for schema in self.schemas.values():
                    validate_contract(arguments, schema)
                preview = self.owner._execute("memory", arguments["op"], **{key: value for key, value in arguments.items() if key != "op"})["result"]
                validate_contract(preview, "memory-governance-preview.v1.json")
                self.assertEqual(preview["operation"], "correct_preview")
                self.assertFalse(preview["mutationApplied"])
                self.assertTrue(preview["proposalId"])
        self.assertEqual(self.owner._memory_table_counts(), before)

    def test_oversize_reason_is_rejected_by_both_owners(self) -> None:
        arguments = {**self.arguments, "reason": "用户确认" * 100 + "多"}
        for transport, schema in self.schemas.items():
            with self.subTest(transport=transport), self.assertRaises(ContractValidationError):
                validate_contract(arguments, schema)
        with self.assertRaisesRegex(ValueError, "reason must not exceed 400 characters"):
            self.owner._execute("memory", arguments["op"], **{key: value for key, value in arguments.items() if key != "op"})

    def test_remember_reason_remains_optional_and_forget_apply_keep_original_requirements(self) -> None:
        remember = {"op": "remember_preview", "text": "用户明确陈述这是一条正常事实",
                    "evidenceIds": [self.owner.evidence_ids["safe"]]}
        for schema in self.schemas.values():
            validate_contract(remember, schema)
        preview = self.owner._execute("memory", "remember_preview", **{key: value for key, value in remember.items() if key != "op"})["result"]
        self.assertFalse(preview["mutationApplied"])
        for schema in self.schemas.values():
            with self.assertRaises(ContractValidationError):
                validate_contract({"op": "forget_preview", "targetId": "atom:old-model-choice"}, schema)
            with self.assertRaises(ContractValidationError):
                validate_contract({"op": "correct_apply"}, schema)
            validate_contract({"op": "correct_apply", "proposalId": preview["proposalId"]}, schema)


if __name__ == "__main__":
    unittest.main()
