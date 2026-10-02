"""Cold native child receipts reach Jev without trusting the outer script's text."""
from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace

from rag_ime.pi.runtime import PiRuntimeHostManager
from rag_ime.pi.transcript import durable_branch_messages, durable_tool_history_events
from tests import test_jev_verification_evidence as fixtures


class NativeNestedToolEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WorkerEvidenceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.path = self.fixture.root / "native-session.jsonl"

    def entries(self):
        entries = []
        for turn, dispatch in (("old-turn", "old-dispatch"), ("turn:a", "dispatch:a"),
                               ("next-turn", "next-dispatch")):
            calls = [
                {"id": f"{turn}/1", "name": "read", "status": "ok", "arguments": {"path": "/authorized/a.json"},
                 "result": {"content": [{"type": "text", "text": "[1,2,3,4,5]"}],
                            "details": {"path": "/authorized/a.json", "apiKey": "private-credential"}}},
                {"id": f"{turn}/2", "name": "bash", "status": "ok", "arguments": {"command": "sum A"},
                 "result": {"content": [{"type": "text", "text": "15\n"}],
                            "details": {"receipt": {"exitCode": 0, "output": "15\n"},
                                        "memoryCheckpoint": {"private": "never-share-memory"}}}},
                {"id": f"{turn}/3", "name": "write", "status": "ok", "arguments": {"path": "sum.txt"},
                 "result": {"content": [{"type": "text", "text": "written"}], "details": {"path": "sum.txt"}}},
            ]
            values = [
                {"type": "custom", "customType": "rag-ime.pi-turn-binding", "data": {
                    "schemaVersion": "rag-ime.pi-turn-binding.v1", "turnId": turn, "clientMessageId": dispatch}},
                {"type": "message", "message": {"role": "user", "content": "private task"}},
                {"type": "message", "message": {"role": "assistant", "content": [
                    {"type": "toolCall", "id": turn, "name": "codemode", "arguments": {"code": "private script"}}]}},
                {"type": "message", "message": {"role": "toolResult", "toolCallId": turn, "toolName": "codemode",
                    "content": [{"type": "text", "text": "fabricated-outer-success"}],
                    "nestedCalls": {"complete": True, "calls": calls}}},
            ]
            for value in values:
                entries.append({"id": f"entry:{len(entries)}", **value})
        return entries

    def open_reader(self, entries):
        self.path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
        # Each read reopens the durable JSONL, with no live event cache or Host.
        reader = SimpleNamespace(_durable_history_snapshot=lambda _session_id: {
            "entries": [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]})
        self.fixture.runtime.session_tool_evidence = lambda session_id, **kwargs: (
            PiRuntimeHostManager.session_tool_evidence(reader, session_id, **kwargs))

    def test_cold_results_are_bound_to_dispatch_and_remain_readable_after_reopening(self):
        entries = self.entries()
        self.open_reader(entries)
        evidence = self.fixture.project()
        self.assertEqual(evidence["status"], "available")
        self.assertEqual([tool["toolName"] for tool in evidence["tools"]], ["read", "bash", "write"])
        self.assertEqual(evidence["tools"][1]["result"]["receipt"], {"exitCode": 0, "output": "15\n"})
        self.assertTrue(all(tool["resultSource"] == "native_nested_call_result" for tool in evidence["tools"]))
        self.assertEqual(evidence["binding"]["dispatchId"], "dispatch:a")
        self.assertEqual(evidence["binding"]["turnId"], "turn:a")
        _, raw = self.fixture.read_media(evidence["readRef"].removeprefix("media://"),
                                        session_id=self.fixture.verifier["id"])
        self.assertEqual(json.loads(raw)["tools"], evidence["tools"])
        for secret in ("private task", "private script", "fabricated-outer-success", "private-credential",
                       "never-share-memory", "old-turn", "next-turn"):
            self.assertNotIn(secret, raw.decode())
        self.open_reader(entries)
        self.assertEqual(self.fixture.project()["readRef"], evidence["readRef"])

    def test_wrong_durable_dispatch_cannot_supply_worker_evidence(self):
        entries = self.entries()
        entries[4]["data"]["clientMessageId"] = "other-dispatch"
        self.open_reader(entries)
        self.assertEqual(self.fixture.project()["status"], "unavailable")

    def test_durable_child_projection_preserves_structured_output_without_leaking_credentials(self):
        entries = self.entries()
        child = entries[7]["message"]["nestedCalls"]["calls"][0]
        child["result"]["structuredContent"] = {
            "content": [{"type": "text", "text": "display preview"}],
            "structuredContent": {"rows": [{"id": 42}], "apiKey": "private-structured-key"},
        }
        messages, selected = durable_branch_messages(entries)
        events = durable_tool_history_events(messages, session_id=self.fixture.worker["id"],
                                            raw_entries=selected, maximum_public_chars=None)
        finished = next(event for event in events if event["turnId"] == "turn:a"
                        and event["eventType"] == "tool_finished"
                        and event["payload"].get("toolCallId") == "turn:a/1")
        self.assertEqual(finished["payload"]["result"]["structuredContent"]["structuredContent"],
                         {"rows": [{"id": 42}], "apiKey": "[REDACTED_SECRET]"})
        self.assertNotIn("private-structured-key", json.dumps(events))
        parent = next(event for event in events if event["turnId"] == "turn:a"
                      and event["eventType"] == "tool_finished"
                      and event["payload"].get("toolCallId") == "turn:a")
        self.assertNotIn("structuredContent", json.dumps(parent))

    def test_public_metadata_and_jev_archive_do_not_duplicate_private_child_data(self):
        entries = self.entries()
        child = entries[7]["message"]["nestedCalls"]["calls"][0]
        child["result"]["content"].append({"type": "image", "data": "private-image-bytes", "mimeType": "image/png"})
        messages, selected = durable_branch_messages(entries)
        events = durable_tool_history_events(messages, session_id=self.fixture.worker["id"],
                                            raw_entries=selected, maximum_public_chars=None)
        parent = next(event for event in events if event["turnId"] == "turn:a"
                      and event["eventType"] == "tool_finished" and event["payload"].get("toolCallId") == "turn:a")
        self.assertNotIn("result", parent["payload"]["nestedCalls"][0])
        self.assertNotIn("private-image-bytes", json.dumps(parent))
        self.assertNotIn("private-credential", json.dumps(events))
        started = next(event for event in events if event["turnId"] == "turn:a"
                       and event["eventType"] == "tool_started" and event["payload"].get("toolCallId") == "turn:a/1")
        self.assertNotIn("result", started["payload"])
        self.open_reader(entries)
        evidence = self.fixture.project()
        _, archive = self.fixture.read_media(evidence["readRef"].removeprefix("media://"),
                                            session_id=self.fixture.verifier["id"])
        self.assertNotIn("private-image-bytes", archive.decode())
        self.assertNotIn("private-credential", archive.decode())

    def test_failed_child_receipt_is_not_overridden_by_outer_success(self):
        entries = self.entries()
        child = entries[7]["message"]["nestedCalls"]["calls"][1]
        # Even an adapter reporting transport success must preserve failed
        # command semantics from the actual structured receipt.
        child["result"]["details"]["receipt"].update(exitCode=3, output="actual failure")
        self.open_reader(entries)
        evidence = self.fixture.project()
        self.assertTrue(evidence["tools"][1]["isError"])
        self.assertEqual(evidence["tools"][1]["result"]["receipt"]["exitCode"], 3)

    def test_omitted_native_arguments_make_retained_receipt_partial(self):
        entries = self.entries()
        child = entries[7]["message"]["nestedCalls"]["calls"][1]
        child.pop("arguments")
        child["argumentsBytes"] = 10000
        self.open_reader(entries)
        evidence = self.fixture.project()
        self.assertEqual(evidence["status"], "partial")
        self.assertEqual(evidence["tools"][1]["result"]["receipt"]["output"], "15\n")

    def test_dropped_native_calls_keep_the_archive_partial(self):
        entries = self.entries()
        entries[7]["message"]["nestedCalls"]["complete"] = False
        self.open_reader(entries)
        evidence = self.fixture.project()
        self.assertEqual(evidence["status"], "partial")
        self.assertEqual(len(evidence["tools"]), 3)

    def test_missing_or_unbound_results_remain_partial(self):
        for defect in ("legacy", "oversized", "unrelated_id", "tool_authored_summary", "missing_native_status"):
            with self.subTest(defect=defect):
                entries = self.entries()
                outer = entries[7]["message"]
                child = outer["nestedCalls"]["calls"][1]
                if defect in {"legacy", "oversized"}:
                    child.pop("result")
                    if defect == "oversized":
                        child["resultUnavailable"] = "size_limit"
                elif defect == "unrelated_id":
                    child["id"] = "other-parent/2"
                elif defect == "tool_authored_summary":
                    outer["details"] = {"calls": [copy.deepcopy(child)]}
                    child.pop("result")
                else:
                    outer["details"] = {"calls": [copy.deepcopy(child)]}
                    child.pop("status")
                self.open_reader(entries)
                evidence = self.fixture.project()
                self.assertEqual(evidence["status"], "partial")
                self.assertEqual(evidence["unfinishedToolCalls"], 0)
                self.assertEqual([tool["toolName"] for tool in evidence["tools"]], ["read", "write"])


if __name__ == "__main__":
    unittest.main()
