"""Verification receives exact Runtime results, never another turn's prose."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

from rag_ime.agent_media import AgentMediaStore
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.agent_tools import ControlToolGateway
from rag_ime.browser_control import BrowserControlService
from rag_ime.jev_tasks.materials import JevMaterialService
from rag_ime.jev_tasks.types import Task, canonical
from rag_ime.pi.transcript import durable_branch_messages, durable_tool_history_events
from rag_ime.rooms.store import AgentRoomStore
from tests import test_jev_host_application as host


class WorkerEvidenceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="paw-jev-evidence-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        db = self.root / "test.sqlite"
        self.sessions = AgentSessionStore(db)
        self.sessions.initialize()
        worker, verifier, outsider = [self.sessions.create(title=name) for name in ("worker", "verifier", "outsider")]
        self.rooms = AgentRoomStore(db, room_dir=self.root / "rooms")
        self.rooms.initialize()
        room = self.rooms.create(title="Evidence", routing_policy="moderator", participants=[
            {"sessionId": s["id"], "roleId": "implementer", "roleVersion": "1",
             "displayName": s["title"], "collaborationRole": "implementer"}
            for s in (worker, verifier)])
        self.worker, self.verifier, self.outsider = worker, verifier, outsider
        self.actor = room["participants"][0]
        self.task = Task("task:a", "root:a", room["id"], "review", 2, self.actor["id"],
                         self.actor["id"], "assignment:a", "dispatch:a", "Read and sum A", "sum", ("real tools",))
        self.snapshot = SimpleNamespace(graph_id="graph:a", root_id="root:a", room_id=room["id"],
                                        task=lambda task_id: self.task)
        request = {"graphId": "graph:a", "rootId": "root:a", "roomId": room["id"],
                   "taskId": self.task.id, "taskRevision": 2, "ownerId": self.actor["id"],
                   "assignmentKey": "assignment:a", "acceptedTurnId": "dispatch:a",
                   "dispatchId": "dispatch:a", "sessionId": worker["id"], "purpose": "execute"}
        self.effect = {"state": "accepted", "operation": "dispatch", "effectId": "dispatch:a",
                       "request": request, "receipt": {"state": "accepted", "dispatchId": "dispatch:a",
                       "taskId": self.task.id, "sessionId": worker["id"], "turnId": "turn:a"}}
        self.media = AgentMediaStore(db)
        self.media.initialize()
        self.runtime = SimpleNamespace(session_snapshot=Mock(return_value={"toolHistoryEvents": self.events()}))
        self.service = SimpleNamespace(db_path=db, rooms=self.rooms, sessions=self.sessions, runtime=self.runtime,
                                       media=self.media, read_media_resource=self.read_media)
        self.materials = JevMaterialService(self.service)

    def read_media(self, media_id, *, session_id):
        participant = self.rooms.participant_for_session(session_id)
        if not participant or participant["status"] != "active":
            raise KeyError("not a Room participant")
        return self.media.read(media_id, room_id=participant["roomId"])

    def events(self):
        entries = []
        def add(value):
            entries.append({"id": f"e{len(entries)}", **value})
        for turn in ("old-turn", "turn:a", "next-turn"):
            add({"type": "custom", "customType": "rag-ime.pi-turn-binding", "data": {
                "schemaVersion": "rag-ime.pi-turn-binding.v1", "turnId": turn, "clientMessageId": turn}})
            add({"type": "message", "message": {"role": "user", "content": "private user prompt"}})
            for tool, result in (("read", {"path": "/authorized/a.json", "resourceRevision": "sha256:abc",
                                          "content": [{"type": "text", "text": "[1,2,3,4,5]"}], "truncated": False}),
                                 ("bash", {"receipt": {"exitCode": 0, "output": "15\n", "timedOut": False},
                                           "memoryCheckpoint": {"private": "never-share-memory"},
                                           "content": [{"type": "text", "text": "15\n[exit code: 0]"}]}),
                                 ("room_partner", {"content": [{"type": "text", "text": "other worker private context"}]})):
                call_id = f"{turn}:{tool}"
                add({"type": "message", "message": {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "never-share-hidden-reasoning"},
                    {"type": "toolCall", "id": call_id, "name": tool, "arguments": {"command": "sum A"}}]}})
                add({"type": "message", "message": {"role": "toolResult", "toolCallId": call_id,
                    "toolName": tool, "isError": False, "details": result, "content": result.get("content")}})
        messages, selected = durable_branch_messages(entries)
        return durable_tool_history_events(messages, session_id=self.worker["id"], raw_entries=selected,
                                          maximum_tools=None, maximum_public_chars=None)

    def project(self, **kwargs):
        return self.materials.worker_tool_evidence(self.snapshot, self.task, self.effect, **kwargs)

    def test_exact_turn_results_have_real_readable_ref_without_private_context(self):
        evidence = self.project()
        self.assertEqual(evidence["status"], "available")
        self.assertEqual(evidence["binding"]["taskRevision"], 2)
        self.assertEqual(evidence["binding"]["turnId"], "turn:a")
        self.assertEqual([item["toolName"] for item in evidence["tools"]], ["read", "bash"])
        self.assertEqual(evidence["tools"][1]["result"]["receipt"]["exitCode"], 0)
        self.assertEqual(evidence["tools"][1]["result"]["receipt"]["output"], "15\n")
        self.assertLess(evidence["tools"][0]["timelineSequence"], evidence["tools"][1]["timelineSequence"])
        self.assertTrue(evidence["readRef"].startswith("media://"))
        gateway = ControlToolGateway(sessions=self.sessions, management=object(), core=object(), project=object(),
                                     collaboration=self.service)
        read = gateway._read_internal_resource(self.verifier["id"], {"resourceRef": evidence["readRef"]})
        archived = json.loads(read["content"])
        self.assertEqual(archived["tools"], evidence["tools"])
        for hidden in ("old-turn", "next-turn", "never-share", "other worker", "private user prompt"):
            self.assertNotIn(hidden, read["content"])
        with self.assertRaises(KeyError):
            gateway._read_internal_resource(self.outsider["id"], {"resourceRef": evidence["readRef"]})
        # Preparation/restart reuses the content-addressed projection via the existing owner.
        self.assertEqual(self.project()["readRef"], evidence["readRef"])
        self.assertEqual(len(self.media.list_for_room(self.task.room_id)), 1)

    def browser_events(self, *, operation="run", receipt=None):
        entries = copy.deepcopy(self.events())
        for event in entries:
            payload = event["payload"]
            if payload.get("toolName") != "bash":
                continue
            payload["toolName"] = "browser"
            if event["eventType"] == "tool_started":
                payload["args"] = {"op": operation, "script": "console.log('WebGL2 checked')"}
            else:
                payload["result"] = receipt or {
                    "schemaVersion": "rag-ime.browser-control.v1", "ok": True,
                    "commandId": "bcmd_" + event["turnId"], "action": operation,
                    "status": "completed", "durationMs": 150, "failureReason": "",
                    "result": {"stdout": "WebGL2 checked", "stderr": "", "exitCode": 0,
                               "timedOut": False, "snapshotId": "snap_current", "width": 1280,
                               "screenshotDataUrl": "never-share-image-bytes", "cookies": "never-share-cookies"},
                    "content": [{"type": "text", "text": "never-share-global-browser-catalog"}],
                    "tabs": [{"title": "never-share-other-task"}],
                }
        return entries

    def test_browser_command_receipt_belongs_to_exact_worker_turn_without_global_browser_data(self):
        self.runtime.session_snapshot.return_value = {"toolHistoryEvents": self.browser_events()}
        evidence = self.project()
        self.assertEqual(evidence["status"], "available")
        browser = next(item for item in evidence["tools"] if item["toolName"] == "browser")
        self.assertEqual(browser["arguments"]["op"], "run")
        self.assertEqual(browser["result"]["commandId"], "bcmd_turn:a")
        self.assertEqual(browser["result"]["status"], "completed")
        self.assertEqual(browser["result"]["result"]["stdout"], "WebGL2 checked")
        self.assertEqual(browser["result"]["result"]["exitCode"], 0)
        self.assertEqual(browser["result"]["result"]["snapshotId"], "snap_current")
        archived = self.media.read(evidence["readRef"].removeprefix("media://"), room_id=self.task.room_id)[1]
        for hidden in (b"never-share", b"old-turn", b"next-turn"):
            self.assertNotIn(hidden, archived)

    def test_scoped_native_evidence_preserves_large_browser_scripts_and_all_completed_calls(self):
        script = "const legal = " + json.dumps(["种植，购买，暂停" * 100] * 70) + "; console.log(legal.length)"
        source = [event for event in self.browser_events() if event["turnId"] == "turn:a"
                  and event["payload"].get("toolName") == "browser"]
        events = []
        for index in range(70):
            for item in source:
                item = copy.deepcopy(item)
                item["eventId"] = f"browser-{index}:{item['eventType']}"
                item["payload"]["toolCallId"] = f"browser-{index}"
                if item["eventType"] == "tool_started":
                    item["payload"].update(argumentSource="native_transcript_arguments",
                        args={"op": "run", "script": script if index in (0, 69) else "console.log('ok')", "timeoutMs": 120000})
                events.append(item)
        self.runtime.session_tool_evidence = Mock(return_value={"toolHistoryEvents": events})
        evidence = self.project(inline_byte_budget=6000)
        self.runtime.session_tool_evidence.assert_called_once_with(self.worker["id"], turn_id="turn:a")
        self.runtime.session_snapshot.assert_not_called()
        self.assertLessEqual(len(canonical(evidence).encode()), 6000)
        _receipt, body = self.read_media(evidence["readRef"].removeprefix("media://"), session_id=self.verifier["id"])
        archive = json.loads(body)
        self.assertEqual(archive["status"], "available")
        self.assertEqual(archive["omittedToolResults"], 0)
        self.assertEqual(len(archive["tools"]), 70)
        self.assertEqual(archive["tools"][0]["arguments"]["script"], script)
        self.assertEqual(archive["tools"][-1]["arguments"]["script"], script)
        self.assertEqual(archive["tools"][0]["argumentSource"], "native_transcript_arguments")


    def test_browser_discovery_is_excluded_and_failed_command_is_preserved(self):
        for operation in ("status", "tabs", "trace", "snapshot", "stop"):
            with self.subTest(operation=operation):
                self.runtime.session_snapshot.return_value = {"toolHistoryEvents": self.browser_events(operation=operation)}
                evidence = self.project()
                self.assertEqual(evidence["status"], "available")
                self.assertEqual([item["toolName"] for item in evidence["tools"]], ["read"])
        events = self.browser_events()
        failed = next(e for e in events if e["turnId"] == "turn:a" and e["eventType"] == "tool_finished"
                      and e["payload"]["toolName"] == "browser")
        failed["payload"]["isError"] = True
        failed["payload"]["result"].update(ok=False, status="failed", failureReason="ego_browser_timeout")
        failed["payload"]["result"]["result"].update(exitCode=1, timedOut=True)
        self.runtime.session_snapshot.return_value = {"toolHistoryEvents": events}
        browser = next(item for item in self.project()["tools"] if item["toolName"] == "browser")
        self.assertTrue(browser["isError"])
        self.assertFalse(browser["result"]["ok"])
        self.assertEqual(browser["result"]["failureReason"], "ego_browser_timeout")
        self.assertTrue(browser["result"]["result"]["timedOut"])

    def test_browser_unbound_result_cannot_become_execution_evidence(self):
        self.runtime.session_snapshot.return_value = {"toolHistoryEvents": self.browser_events(
            receipt={"schemaVersion": "rag-ime.browser-control.v1", "status": "completed",
                     "action": "run", "result": {"stdout": "unbound-success"}})}
        evidence = self.project()
        self.assertEqual(evidence["status"], "partial")
        self.assertNotIn("unbound-success", canonical(evidence))
        self.assertTrue(next(item for item in evidence["tools"] if item["toolName"] == "browser")
                        ["result"]["receiptUnavailable"])

    def test_stale_revision_dispatch_owner_or_receipt_never_reads_runtime(self):
        for section, key, value in (("request", "taskRevision", 1), ("request", "ownerId", "other"),
                                    ("request", "dispatchId", "old"), ("request", "purpose", "verify"),
                                    ("receipt", "sessionId", self.verifier["id"]),
                                    ("receipt", "state", "unknown"), ("receipt", "turnId", "")):
            with self.subTest(key=key):
                effect = copy.deepcopy(self.effect)
                effect[section][key] = value
                result = self.materials.worker_tool_evidence(self.snapshot, self.task, effect)
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["tools"], [])
        self.runtime.session_snapshot.assert_not_called()
        self.assertEqual(self.media.list_for_room(self.task.room_id), [])

    def test_other_session_or_turn_cannot_supply_current_attempt_evidence(self):
        for mutate in (lambda e: e.update(turnId="old-turn"),
                       lambda e: e.update(sessionId=self.verifier["id"])):
            events = copy.deepcopy(self.events())
            for event in events:
                mutate(event)
            self.runtime.session_snapshot.return_value = {"toolHistoryEvents": events}
            result = self.project()
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["tools"], [])

    def test_errors_and_partial_results_are_preserved_without_success_invention(self):
        events = self.events()
        failed = next(e for e in events if e["turnId"] == "turn:a" and e["eventType"] == "tool_finished"
                      and e["payload"]["toolName"] == "bash")
        failed["payload"]["isError"] = True
        failed["payload"]["result"]["receipt"] = {"exitCode": 3, "output": "failed", "outputLimited": True}
        self.runtime.session_snapshot.return_value = {"toolHistoryEvents": events}
        result = self.project()
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["tools"][1]["isError"])
        self.assertEqual(result["tools"][1]["result"]["receipt"]["exitCode"], 3)
        self.assertTrue(result["tools"][1]["result"]["receipt"]["outputLimited"])

    def test_raw_result_store_truncation_remains_partial_after_allowlist_projection(self):
        for shape in ("direct", "native_text_envelope"):
            with self.subTest(shape=shape):
                events = self.events()
                read = next(e for e in events if e["turnId"] == "turn:a" and e["eventType"] == "tool_finished"
                            and e["payload"]["toolName"] == "read")
                raw = read["payload"]["result"]
                raw["privateControlMetadata"] = "do-not-share"
                if shape == "direct":
                    raw["modelResultTruncated"] = True
                else:
                    # Actual native tool-bridge shape: details retain the raw
                    # object; content carries modelVisibleResult's JSON envelope.
                    raw["content"] = [{"type": "text", "text": canonical({
                        "evidenceHandle": "tool-result://sha256/" + "a" * 64,
                        "evidenceSummary": "bounded excerpt", "truncated": True,
                        "modelResultTruncated": True})}]
                self.runtime.session_snapshot.return_value = {"toolHistoryEvents": events}
                result = self.project()
                self.assertEqual(result["status"], "partial")
                _receipt, body = self.read_media(result["readRef"].removeprefix("media://"), session_id=self.verifier["id"])
                self.assertEqual(json.loads(body)["status"], "partial")
                self.assertNotIn("modelResultTruncated", result["tools"][0]["result"])
                for excluded in ("privateControlMetadata", "do-not-share"):
                    self.assertNotIn(excluded, canonical(result))
                    self.assertNotIn(excluded, body.decode())

    def test_large_result_is_bounded_but_readable_archive_retains_original(self):
        events = self.events()
        read = next(e for e in events if e["turnId"] == "turn:a" and e["eventType"] == "tool_finished"
                    and e["payload"]["toolName"] == "read")
        read["payload"]["result"]["content"] = [{"type": "text", "text": "证据原文" * 3000}]
        self.runtime.session_snapshot.return_value = {"toolHistoryEvents": events}
        result = self.project(inline_byte_budget=6000)
        self.assertLessEqual(len(canonical(result).encode()), 6000)
        self.assertTrue(result["inlineTruncated"])
        _receipt, body = self.read_media(result["readRef"].removeprefix("media://"), session_id=self.verifier["id"])
        self.assertIn("证据原文" * 3000, body.decode())
        gateway = ControlToolGateway(sessions=self.sessions, management=object(), core=object(), project=object(),
                                     collaboration=self.service)
        chunks, revisions, offset = [], set(), 0
        while True:
            page = gateway._read_internal_resource(self.verifier["id"],
                {"resourceRef": result["readRef"], "offset": offset, "limit": 4097})
            chunks.append(page["content"])
            revisions.add(page["resourceRevision"])
            self.assertEqual(page["offset"], offset)
            self.assertLessEqual(page["contentBytes"], 4097)
            if page["nextOffset"] is None:
                self.assertFalse(page["truncated"])
                break
            self.assertTrue(page["truncated"])
            self.assertEqual(page["nextOffset"], offset + len(page["content"].encode("utf-8")))
            offset = page["nextOffset"]
        self.assertEqual("".join(chunks).encode("utf-8"), body)
        self.assertEqual(revisions, {result["archiveSha256"]})
        middle_of_scalar = body.index("证".encode("utf-8")) + 1
        with self.assertRaisesRegex(ValueError, "UTF-8 character boundary"):
            gateway._read_internal_resource(self.verifier["id"],
                {"resourceRef": result["readRef"], "offset": middle_of_scalar})
        with self.assertRaises(KeyError):
            gateway._read_internal_resource(self.outsider["id"],
                {"resourceRef": result["readRef"], "offset": offset})

    def test_missing_runtime_history_stays_unavailable_and_does_not_share_worker_prose(self):
        self.task = replace(self.task, result="I definitely ran every test", evidence=("trust me",))
        self.runtime.session_snapshot.side_effect = RuntimeError("private host failure")
        result = self.project()
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("private host failure", canonical(result))
        self.assertNotIn("definitely", canonical(result))

    def test_native_reader_failure_does_not_fall_back_to_shortened_arguments(self):
        self.runtime.session_tool_evidence = Mock(side_effect=RuntimeError("source unavailable"))
        evidence = self.project()
        self.assertEqual(evidence["status"], "unavailable")
        self.runtime.session_snapshot.assert_not_called()

    def test_completed_nested_call_without_result_is_partial_not_unfinished(self):
        events = self.events()
        finished = next(e for e in events if e["turnId"] == "turn:a" and e["eventType"] == "tool_finished"
                        and e["payload"]["toolName"] == "bash")
        finished["payload"].pop("result")
        self.runtime.session_snapshot.return_value = {"toolHistoryEvents": events}
        evidence = self.project()
        self.assertEqual(evidence["status"], "partial")
        self.assertEqual(evidence["unfinishedToolCalls"], 0)

    def nested_browser_events(self):
        args = {"op": "run", "script": "console.log('original execution')", "timeoutMs": 3000}
        def event(kind, call, name, at, **payload):
            return {"eventId": call + ':' + kind, "sessionId": self.worker['id'],
                    "turnId": "turn:a", "eventType": kind, "createdAtMs": at,
                    "payload": {"toolCallId": call, "toolName": name, **payload}}
        return [event('tool_started', 'outer', 'codemode', 1000,
                      argumentSource='native_transcript_arguments', args={'code': 'private orchestration'}),
                event('tool_started', 'outer/1', 'browser', 2100,
                      parentToolCallId='outer', argumentSource='native_nested_call_arguments', args=args),
                event('tool_finished', 'outer/1', 'browser', 2100,
                      parentToolCallId='outer', status='ok', isError=False),
                event('tool_finished', 'outer', 'codemode', 2100, result={'private': 'not-shared'})]

    def seed_nested_browser_receipt(self):
        browser = BrowserControlService(self.sessions.db_path, app_support_root=self.root / 'browser')
        with browser._connection() as conn:
            conn.execute("""INSERT INTO browser_control_commands(
                command_id,device_id,session_id,action,payload_json,status,created_at_ms,completed_at_ms,result_json)
                VALUES ('bcmd_native','paw-browser',?,'run',?,'completed',1200,2000,?)""",
                (self.worker['id'], json.dumps({'script': "console.log('original execution')", 'timeoutMs': 3000}),
                 json.dumps({'ok': True, 'stdout': '{"observed":42}', 'exitCode': 0})))
        return browser

    def test_cold_nested_browser_result_uses_one_exact_existing_owner_receipt(self):
        self.seed_nested_browser_receipt()
        self.runtime.session_tool_evidence = Mock(return_value={'toolHistoryEvents': self.nested_browser_events()})
        evidence = self.project()
        self.assertEqual(evidence['status'], 'available')
        tool = evidence['tools'][0]
        self.assertEqual(tool['toolCallId'], 'outer/1')
        self.assertEqual(tool['result']['commandId'], 'bcmd_native')
        self.assertEqual(tool['result']['result']['stdout'], '{"observed":42}')
        self.assertEqual(tool['resultSource'], 'bound_browser_control_receipt')
        self.assertNotIn('private orchestration', canonical(evidence))

    def test_cold_nested_browser_receipt_rejects_mismatch_and_ambiguity(self):
        browser = self.seed_nested_browser_receipt()
        self.runtime.session_tool_evidence = Mock(return_value={'toolHistoryEvents': self.nested_browser_events()})
        for field, value in [('session_id', self.outsider['id']), ('created_at_ms', 999),
                             ('completed_at_ms', 2200), ('status', 'claimed'),
                             ('payload_json', '{"script":"different","timeoutMs":3000}')]:
            with self.subTest(field=field), browser._connection() as conn:
                original = conn.execute('SELECT '+field+' FROM browser_control_commands').fetchone()[0]
                conn.execute('UPDATE browser_control_commands SET '+field+'=?', (value,))
                conn.commit()
                try:
                    self.assertIn(self.project()['status'], {'partial', 'unavailable'})
                finally:
                    conn.execute('UPDATE browser_control_commands SET '+field+'=?', (original,))
        with browser._connection() as conn:
            conn.execute("""INSERT INTO browser_control_commands(
                command_id,device_id,session_id,action,payload_json,status,created_at_ms,
                claimed_at_ms,claimed_by,result_json,failure_reason,completed_at_ms)
                SELECT 'bcmd_ambiguous', device_id, session_id, action, payload_json, status,
                       created_at_ms, claimed_at_ms, claimed_by, result_json, failure_reason, completed_at_ms
                FROM browser_control_commands WHERE command_id='bcmd_native'""")
        self.assertIn(self.project()['status'], {'partial', 'unavailable'})

    def test_cold_nested_browser_failure_is_retained_and_unproven_parent_is_rejected(self):
        browser = self.seed_nested_browser_receipt()
        with browser._connection() as conn:
            conn.execute("UPDATE browser_control_commands SET status='failed',result_json=?,failure_reason='script_failed'",
                         (json.dumps({'ok': False, 'stderr': 'original failure', 'exitCode': 1}),))
        events = self.nested_browser_events()
        events[2]['payload'].update(status='error', isError=True)
        self.runtime.session_tool_evidence = Mock(return_value={'toolHistoryEvents': events})
        tool = self.project()['tools'][0]
        self.assertTrue(tool['isError'])
        self.assertEqual(tool['result']['status'], 'failed')
        self.assertEqual(tool['result']['result']['stderr'], 'original failure')
        for event_index in (0, 1):
            unbound = copy.deepcopy(events)
            unbound[event_index]['payload'].pop('argumentSource')
            self.runtime.session_tool_evidence.return_value = {'toolHistoryEvents': unbound}
            self.assertIn(self.project()['status'], {'partial', 'unavailable'})

    def test_exact_owner_command_is_used_only_with_matching_causal_receipt(self):
        events = self.events()
        bash = next(e for e in events if e["turnId"] == "turn:a" and e["eventType"] == "tool_finished"
                    and e["payload"]["toolName"] == "bash")
        approval = {"sessionId": self.worker["id"], "toolCallId": "turn:a:bash",
                    "causalMetadata": {"roomId": self.task.room_id, "rootId": "root:a",
                                       "dispatchId": "dispatch:a", "turnId": "turn:a"},
                    "preview": {"actionPayload": {"command": "python3 sum.py /authorized/a.json"}}}
        bash["payload"]["result"]["approval"] = approval
        self.runtime.session_snapshot.return_value = {"toolHistoryEvents": events}
        tool = self.project()["tools"][1]
        self.assertEqual(tool["argumentSource"], "causally_bound_owner_request")
        self.assertEqual(tool["arguments"]["command"], "python3 sum.py /authorized/a.json")
        approval["causalMetadata"]["turnId"] = "different-turn"
        tool = self.project()["tools"][1]
        self.assertEqual(tool["argumentSource"], "runtime_redacted_arguments")
        self.assertNotIn("sum.py", canonical(tool))

    def test_command_receipt_semantics_preserve_raw_flags_and_do_not_assess_side_effects(self):
        receipt = {"schemaVersion": "rag-ime.workspace-command-receipt.v1", "exitCode": 0,
                   "timedOut": False, "outputLimited": False, "output": "15\n",
                   "mutationApplied": True, "sourceReadOnly": False, "networkAllowed": True,
                   "temporaryWritesDiscarded": False}
        semantics = []
        for command in ("python3 -c 'print(sum([1,2,3,4,5]))'", "printf changed > output.txt"):
            with self.subTest(command=command):
                events = self.events()
                for event in events:
                    if event["turnId"] != "turn:a" or event["payload"].get("toolName") != "bash":
                        continue
                    if event["eventType"] == "tool_started":
                        event["payload"]["args"] = {"command": command, "allowNetwork": True}
                    elif event["eventType"] == "tool_finished":
                        event["payload"]["result"]["receipt"] = copy.deepcopy(receipt)
                self.runtime.session_snapshot.return_value = {"toolHistoryEvents": events}
                projected = self.project()
                tool = projected["tools"][1]
                self.assertEqual(tool["result"]["receipt"], receipt)
                self.assertEqual(tool["arguments"]["command"], command)
                self.assertTrue(tool["arguments"]["allowNetwork"])
                explanation = tool["receiptSemantics"]
                self.assertEqual(explanation["schemaVersion"], receipt["schemaVersion"])
                self.assertEqual(explanation["receiptPath"], "result.receipt")
                self.assertFalse(explanation["sideEffectsAssessed"])
                self.assertIn("sourceReadOnly", explanation["fields"]["mutationApplied"])
                self.assertIn("文件差异", explanation["fields"]["mutationApplied"])
                self.assertIn("权限", explanation["fields"]["networkAllowed"])
                semantics.append(explanation)
                _receipt, body = self.read_media(projected["readRef"].removeprefix("media://"), session_id=self.verifier["id"])
                self.assertEqual(json.loads(body)["tools"][1]["receiptSemantics"], explanation)
        # The same field glossary is supplied for an explicit file write. It
        # never classifies a command as read-only or returns an acceptance verdict.
        self.assertEqual(semantics[0], semantics[1])

    def test_command_semantics_are_not_applied_to_other_or_missing_receipt_schema(self):
        for schema in (None, "rag-ime.workspace-command-receipt.v2", "other.command-receipt.v1"):
            with self.subTest(schema=schema):
                events = self.events()
                bash = next(e for e in events if e["turnId"] == "turn:a" and e["eventType"] == "tool_finished"
                            and e["payload"]["toolName"] == "bash")
                if schema:
                    bash["payload"]["result"]["receipt"]["schemaVersion"] = schema
                self.runtime.session_snapshot.return_value = {"toolHistoryEvents": events}
                self.assertNotIn("receiptSemantics", self.project()["tools"][1])

    def test_incomplete_archive_never_claims_unbounded_raw_evidence(self):
        events = self.events()
        read = next(e for e in events if e["turnId"] == "turn:a" and e["eventType"] == "tool_finished"
                    and e["payload"]["toolName"] == "read")
        read["payload"]["result"]["content"] = [{"type": "text", "text": "证据" * 360000}]
        self.runtime.session_snapshot.return_value = {"toolHistoryEvents": events}
        result = self.project()
        self.assertEqual(result["status"], "partial")
        self.assertLessEqual(result["archiveBytes"], 2 * 1024 * 1024)
        _receipt, body = self.read_media(result["readRef"].removeprefix("media://"), session_id=self.verifier["id"])
        archived = json.loads(body)
        self.assertTrue(archived["tools"][0]["result"]["truncated"])

    def test_omitted_completed_results_are_not_unfinished_calls(self):
        source = [event for event in self.events() if event["turnId"] == "turn:a"
                  and event["payload"].get("toolName") == "read"]
        completed = []
        for index in range(65):
            for event in source:
                copied = copy.deepcopy(event)
                copied["eventId"] = f"read-{index}:{event['eventType']}"
                copied["payload"]["toolCallId"] = f"read-{index}"
                completed.append(copied)
        for unfinished in (0, 1):
            with self.subTest(unfinished=unfinished):
                events = copy.deepcopy(completed)
                if unfinished:
                    pending = copy.deepcopy(next(event for event in source
                                                  if event["eventType"] == "tool_started"))
                    pending["eventId"] = "pending-read:tool_started"
                    pending["payload"]["toolCallId"] = "pending-read"
                    events.append(pending)
                self.runtime.session_snapshot.return_value = {"toolHistoryEvents": events}
                result = self.project()
                self.assertEqual(result["status"], "partial" if unfinished else "available")
                self.assertEqual(result["omittedToolResults"], 0)
                self.assertEqual(result["unfinishedToolCalls"], unfinished)
                _receipt, body = self.read_media(result["readRef"].removeprefix("media://"),
                                                session_id=self.verifier["id"])
                archived = json.loads(body)
                self.assertEqual(len(archived["tools"]), 65)
                self.assertEqual(archived["status"], "partial" if unfinished else "available")
                self.assertEqual(archived["omittedToolResults"], 0)
                self.assertEqual(archived["unfinishedToolCalls"], unfinished)

    def test_archive_budget_retains_latest_results_in_execution_order(self):
        source = [event for event in self.events() if event["turnId"] == "turn:a"
                  and event["payload"].get("toolName") == "read"]
        for count, content, budget in ((65, "small", 24000), (7, "读取" * 8000, 64000)):
            with self.subTest(count=count):
                events = []
                for index in range(count):
                    for event in source:
                        item = copy.deepcopy(event)
                        item["eventId"] = f"read-{index}:{event['eventType']}"
                        item["payload"]["toolCallId"] = f"read-{index}"
                        if item["eventType"] == "tool_finished":
                            item["payload"]["result"]["content"] = [{"type": "text", "text": content + str(index)}]
                        events.append(item)
                self.runtime.session_snapshot.return_value = {"toolHistoryEvents": events}
                with patch("rag_ime.jev_tasks.materials._EVIDENCE_ARCHIVE_BYTES", budget):
                    result = self.project()
                _, body = self.read_media(result["readRef"].removeprefix("media://"),
                                          session_id=self.verifier["id"])
                archive = json.loads(body)
                ids = [item["toolCallId"] for item in archive["tools"]]
                self.assertEqual(ids[-1], f"read-{count - 1}")
                omitted = archive["omittedToolResults"]
                self.assertGreater(omitted, 0)
                self.assertEqual(ids, [f"read-{index}" for index in range(omitted, count)])
                self.assertEqual(archive["status"], "partial")
                self.assertEqual(archive["unfinishedToolCalls"], 0)
                self.assertLessEqual(len(body), budget)
                self.assertEqual(archive["tools"][-1]["result"]["content"][0]["text"], content + str(count - 1))


class VerificationPreparationTests(host.JevHostFixture):
    def test_prepared_verifier_persists_exact_results_and_readable_media(self):
        created = self.app.create(self.room["id"], {
            "clientMessageId": "inspect-evidence", "message": "独立检查计算结果",
            "strategy": "direct", "modelRouting": "participant", "verificationMode": "independent",
        })
        self.app.tick()
        effect = next(e for e in self.app.projection(self.room["id"], created["graphId"])["effects"]
                      if e["operation"] == "dispatch")
        request, receipt = effect["request"], effect["receipt"]
        self.app.tool_operation(request["sessionId"], {"op": "result_submit", "proposal": {
            "resultSummary": "sum 15", "evidenceRefs": ["worker-bash"], "artifactRefs": []}}, tool_call_id="worker-submit")
        self.service.runtime.release_prompt_admission(request["sessionId"], client_message_id=request["dispatchId"])
        self.service.room_turns.finish(request["sessionId"], receipt["turnId"], request["rootId"])
        events = durable_tool_history_events([
            {"role": "user", "id": "worker-input", "_ragImeTurnId": receipt["turnId"], "content": "task"},
            {"role": "assistant", "content": [{"type": "toolCall", "name": "bash", "id": "worker-bash",
                                                 "arguments": {"command": "sum A"}}]},
            {"role": "toolResult", "toolName": "bash", "toolCallId": "worker-bash", "isError": False,
             "content": [{"type": "text", "text": "15\n[exit code: 0]"}],
             "details": {"receipt": {"output": "15\n", "exitCode": 0}}},
        ], session_id=request["sessionId"])
        terminal = {"eventId": "terminal:a", "eventType": "turn_completed", "status": "completed"}
        with patch.object(self.app, "execution_terminal", side_effect=lambda e, **kw: terminal if e["effectId"] == effect["effectId"] else None), \
             patch.object(self.service.runtime, "session_tool_evidence", return_value={"toolHistoryEvents": events}):
            self.app.tick()
        verifier = next(e for e in self.app.projection(self.room["id"], created["graphId"])["effects"]
                        if e["operation"] == "dispatch" and e["request"].get("purpose") == "verify")
        self.assertEqual(verifier["state"], "accepted")
        pack = json.loads(verifier["request"]["taskBrief"]["objective"].split("\nExecutionPack:\n")[1])
        instructions = verifier["request"]["taskBrief"]["objective"].split("\nExecutionPack:\n")[0]
        self.assertIn("现有read工具", instructions)
        self.assertIn("offset=1、limit=2000", instructions)
        self.assertIn("按返回的nextLineOffset继续读取", instructions)
        self.assertIn("长JSON行会无损分段", instructions)
        self.assertIn("offset/limit不是字节偏移", instructions)
        self.assertNotIn("用workspace_read", instructions)
        evidence = pack["workerToolEvidence"]
        self.assertEqual(evidence["binding"]["dispatchId"], effect["effectId"])
        self.assertEqual(evidence["binding"]["turnId"], receipt["turnId"])
        self.assertEqual(evidence["tools"][0]["result"]["receipt"]["output"], "15\n")
        _media, raw = self.service.read_media_resource(evidence["readRef"].removeprefix("media://"),
                                                     session_id=verifier["request"]["sessionId"])
        self.assertEqual(json.loads(raw)["tools"], evidence["tools"])
