"""Replay real SQLite/Gateway previews through the shipped legacy Pi consumer."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import unittest

from tests import test_agent_governed_memory_tools as governed


class PiMemoryPreviewConsumerTests(unittest.TestCase):
    def setUp(self):
        self.owner = governed.GovernedMemoryToolTests()
        self.owner.setUp()
        self.addCleanup(self.owner.tearDown)
        self.script = Path(self.owner.tmp.name) / "consumer.mjs"
        self.script.write_text('''import {readFileSync} from 'node:fs';
const input = JSON.parse(readFileSync(0, 'utf8'));
const tools = [], requests = [], confirmations = [];
globalThis.fetch = async (url, options) => {
  requests.push({url:String(url),body:JSON.parse(options.body)});
  if (!input.responses.length) throw new Error('unexpected gateway request');
  return new Response(JSON.stringify(input.responses.shift()), {status:200});
};
const extension = await import(process.env.TEST_EXTENSION_URL);
extension.default({registerTool:tool=>tools.push(tool)});
const tool = tools.find(tool=>tool.name===input.tool);
const ctx = input.port ? {ui:{confirm:async (...args)=>{confirmations.push(args);return input.confirmed;}}} : {};
try {
  const result=await tool.execute('consumer-call',input.args,undefined,undefined,ctx);
  console.log(JSON.stringify({result,requests,confirmations}));
} catch(error) {console.log(JSON.stringify({error:error.message,requests,confirmations}));}
''')

    def preview(self, operation="remember_preview"):
        args = {"text": "演示项目的发布标签是蓝色。", "memoryKind": "project_state",
                "reason": "用户确认演示项目事实",
                "evidenceIds": [self.owner.evidence_ids["safe"]]}
        if operation != "remember_preview":
            args["targetId"] = "atom:model-choice"
            args["evidenceIds"] = [self.owner.evidence_ids["correct"], self.owner.evidence_ids["model-eval"]]
        if operation == "forget_preview":
            args.pop("text")
            args.pop("memoryKind")
            args["reason"] = "用户撤回过期事实"
        return self.owner._execute("memory", operation, **args)["result"]

    def consume(self, result, *, op="remember_preview", tool="memory", port=True,
                confirmed=False, responses=None):
        env = {**os.environ, "RAG_IME_AGENT_TOOL_URL": "http://gateway.invalid/tool/execute",
               "RAG_IME_AGENT_TOOL_TOKEN": "synthetic-test-token", "RAG_IME_AGENT_SESSION_ID": self.owner.session["id"],
               "RAG_IME_AGENT_SESSION_MODE": "assistant", "RAG_IME_AGENT_TOOL_PROFILE_VERSION": "control-center-v1",
               "RAG_IME_AGENT_EXECUTION_MODE": "per_action", "RAG_IME_AGENT_ROOM_BOUND": "0",
               "TEST_EXTENSION_URL": (Path(__file__).parents[1] / "integrations/pi/rag-ime-control.ts").as_uri()}
        process = subprocess.run(["node", "--experimental-strip-types", str(self.script)], env=env,
                                 input=json.dumps({"tool": tool, "args": {"op": op}, "port": port,
                                                   "confirmed": confirmed,
                                                   "responses": responses or [{"ok": True, "result": result}]}),
                                 capture_output=True, text=True, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)
        return json.loads(process.stdout)

    def test_real_three_preview_producers_return_original_proposal_without_review_or_apply(self):
        before = self.owner._memory_table_counts()
        for operation in ("remember_preview", "correct_preview", "forget_preview"):
            with self.subTest(operation=operation):
                preview = self.preview(operation)
                returned = self.consume(preview, op=operation)
                self.assertNotIn("error", returned)
                self.assertEqual(json.loads(returned["result"]["content"][0]["text"]), preview)
                self.assertEqual(returned["confirmations"], [])
                self.assertEqual(len(returned["requests"]), 1)
        self.assertEqual(self.owner._memory_table_counts(), before)

    def test_read_only_preview_needs_no_native_decision_port(self):
        preview = self.preview()
        returned = self.consume(preview, port=False)
        self.assertNotIn("error", returned)
        self.assertEqual(returned["result"]["details"]["proposalId"], preview["proposalId"])
        self.assertFalse(returned["result"]["details"]["mutationApplied"])

    def test_mismatched_or_malformed_preview_cannot_escape_existing_review_guard(self):
        preview = self.preview()
        changes = ({"schemaVersion": "other-preview.v1"}, {"operation": "correct_preview"},
                   {"sessionId": "foreign-session"}, {"previewId": "foreign-proposal"},
                   {"applyOperation": "forget_apply"}, {"proposalId": ""}, {"mutationApplied": True},
                   {"writes": {**preview["writes"], "memoryAtoms": True}},
                   {"audit": {**preview["audit"], "payloadSha256": "invalid"}},
                   {"audit": {**preview["audit"], "sessionId": "foreign-session"}})
        for change in changes:
            with self.subTest(change=change):
                returned = self.consume({**copy.deepcopy(preview), **change})
                self.assertEqual(returned["error"], "native review bridge is unavailable")
                self.assertEqual(returned["confirmations"], [])

    def test_non_memory_apply_or_wrapper_cannot_impersonate_preview(self):
        preview = self.preview()
        for kwargs, value in (({"tool": "agent_role_book", "op": "review"}, preview),
                              ({"op": "remember_apply"}, preview),
                              ({}, {"reviewRequired": True, "result": preview})):
            with self.subTest(kwargs=kwargs):
                returned = self.consume(value, **kwargs)
                self.assertEqual(returned["error"], "native review bridge is unavailable")

    def test_whole_memory_review_keeps_run_confirm_and_missing_run_guard(self):
        returned = self.consume({"reviewRequired": True, "run": {"runId": "whole-run"}}, op="curation_prepare")
        self.assertEqual(returned["confirmations"][0][0], "RAG-IME-REVIEW:whole-run")
        self.assertEqual(returned["result"]["details"]["reviewState"], "deferred")
        for value, port in (({"reviewRequired": True}, True),
                            ({"reviewRequired": True, "runId": "whole-run"}, False)):
            self.assertEqual(self.consume(value, op="curation_prepare", port=port)["error"],
                             "native review bridge is unavailable")

    def test_real_prepared_apply_still_requires_original_hash_bound_native_approval(self):
        preview = self.preview()
        prepared = self.owner._execute("memory", "remember_apply", proposalId=preview["proposalId"])["result"]
        before = self.owner._memory_table_counts()
        self.assertTrue(prepared["approvalRequired"])
        self.assertEqual(prepared["approval"]["preview"]["actionPayload"]["payloadSha256"],
                         preview["audit"]["payloadSha256"])
        returned = self.consume(prepared, op="remember_apply", port=False)
        self.assertEqual(returned["error"], "native approval bridge is unavailable")
        rejected = {**prepared["approval"], "state": "rejected"}
        returned = self.consume(prepared, op="remember_apply",
                                responses=[{"ok": True, "result": prepared}, {"ok": True, "approval": rejected}])
        self.assertEqual(returned["confirmations"][0][0], "RAG-IME-APPROVAL:" + prepared["approvalId"])
        self.assertEqual(returned["result"]["details"]["approvalState"], "rejected")
        self.assertEqual(self.owner._memory_table_counts(), before)


if __name__ == "__main__":
    unittest.main()
