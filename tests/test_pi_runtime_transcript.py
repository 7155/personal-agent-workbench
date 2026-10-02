from __future__ import annotations

import json
import subprocess
import sys
import unittest
from copy import deepcopy
from pathlib import Path

from rag_ime.pi.transcript import (
    DURABLE_TURN_ID_KEY,
    durable_branch_messages,
    durable_tool_history_events,
    latest_terminal_descendant_leaf,
    recent_messages_from_proven_tail,
    recent_public_message_window,
    recent_tool_history_events,
    terminal_branch_anchor,
    unambiguous_descendant_leaf,
)


class PiRuntimeTranscriptTests(unittest.TestCase):
    def test_bound_evidence_keeps_full_arguments_without_expanding_public_history(self):
        script = "const steps = " + json.dumps(["公开操作" * 100] * 80) + "; console.log(steps.length)"
        messages = []
        for turn in ("old", "accepted", "next"):
            messages.extend([
                {"role": "user", DURABLE_TURN_ID_KEY: turn, "content": "private prompt"},
                {"role": "assistant", "content": [{"type": "toolCall", "id": turn + ":browser",
                    "name": "browser", "arguments": {"op": "run", "script": script,
                        "timeoutMs": 120000, "apiKey": "private-credential"}}]},
                {"role": "toolResult", "toolCallId": turn + ":browser", "toolName": "browser",
                    "details": {"stdout": "done"}},
            ])
        original = deepcopy(messages)
        public = durable_tool_history_events(messages, session_id="worker", maximum_tools=None,
                                            maximum_public_chars=None)
        evidence = durable_tool_history_events(messages, session_id="worker", maximum_tools=None,
            maximum_public_chars=None, evidence_turn_id="accepted")
        self.assertEqual({e["turnId"] for e in evidence}, {"accepted"})
        started = next(e for e in evidence if e["eventType"] == "tool_started")
        self.assertEqual(started["payload"]["args"]["script"], script)
        self.assertEqual(started["payload"]["args"]["timeoutMs"], 120000)
        self.assertEqual(started["payload"]["argumentSource"], "native_transcript_arguments")
        self.assertNotIn("private-credential", json.dumps(evidence))
        self.assertNotIn("private prompt", json.dumps(evidence))
        public_started = next(e for e in public if e["eventId"] == started["eventId"])
        self.assertEqual(len(public_started["payload"]["args"]["script"]), 500)
        self.assertEqual(started.get("timelineSequence"), public_started.get("timelineSequence"))
        self.assertEqual(messages, original)

    def test_bound_codemode_arguments_are_full_but_missing_nested_results_stay_missing(self):
        script = "console.log(" + json.dumps("合法操作" * 1000) + ")"
        messages = [{"role": "user", DURABLE_TURN_ID_KEY: "ptc-turn", "content": "task"},
            {"role": "toolResult", "toolCallId": "outer", "toolName": "codemode", "nestedCalls": {
                "calls": [{"id": "inner", "name": "browser", "status": "ok",
                    "arguments": {"op": "run", "script": script, "authorization": "private-secret"}}]}}]
        events = durable_tool_history_events(messages, session_id="worker", evidence_turn_id="ptc-turn")
        child = [e for e in events if e["payload"].get("toolCallId") == "inner"]
        self.assertEqual(len(child), 2)
        self.assertEqual(child[0]["payload"]["args"]["script"], script)
        self.assertEqual(child[0]["payload"]["argumentSource"], "native_nested_call_arguments")
        self.assertNotIn("result", child[1]["payload"])
        self.assertNotIn("private-secret", json.dumps(events))

    def test_projection_can_import_and_run_without_starting_host_or_opening_storage(
        self,
    ) -> None:
        process = subprocess.run(
            [
                sys.executable,
                "-c",
                """
import sys
from unittest.mock import patch
for name in (
    'rag_ime.pi_runtime', 'rag_ime.pi.runtime',
    'rag_ime.pi.protocols', 'rag_ime.agent_service',
):
    sys.modules[name] = None
with patch('sqlite3.connect', side_effect=AssertionError('database opened')), \
     patch('subprocess.Popen', side_effect=AssertionError('Host started')):
    from rag_ime.pi.transcript import durable_branch_messages, durable_tool_history_events
    assert durable_branch_messages([]) == ([], [])
    assert durable_tool_history_events([], session_id='isolated') == []
""",
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)

    def test_bound_history_preserves_identity_order_and_inputs_across_projections(
        self,
    ) -> None:
        entries = [
            {"type": "session", "id": "root"},
            {
                "type": "custom",
                "id": "binding",
                "parentId": "root",
                "customType": "rag-ime.pi-turn-binding",
                "data": {
                    "schemaVersion": "rag-ime.pi-turn-binding.v1",
                    "turnId": "accepted-turn",
                    "clientMessageId": "client-message",
                },
            },
            {
                "type": "message",
                "id": "user",
                "parentId": "binding",
                "timestamp": 100,
                "message": {"role": "user", "timestamp": 100, "content": "修改文件"},
            },
            {
                "type": "message",
                "id": "tool-call",
                "parentId": "user",
                "timestamp": 200,
                "message": {
                    "role": "assistant",
                    "timestamp": 120,
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "write-1",
                            "name": "write",
                            "arguments": {"path": "file.txt"},
                        }
                    ],
                },
            },
            {
                "type": "message",
                "id": "tool-result",
                "parentId": "tool-call",
                "timestamp": 300,
                "message": {
                    "role": "toolResult",
                    "timestamp": 130,
                    "toolCallId": "write-1",
                    "toolName": "write",
                    "content": [{"type": "text", "text": "done"}],
                },
            },
            {
                "type": "message",
                "id": "answer",
                "parentId": "tool-result",
                "timestamp": 400,
                "message": {
                    "role": "assistant",
                    "timestamp": 140,
                    "stopReason": "stop",
                    "content": [{"type": "text", "text": "完成"}],
                },
            },
        ]
        original = deepcopy(entries)
        messages, selected = durable_branch_messages(entries, leaf_id="answer")
        original_messages = deepcopy(messages)
        events = durable_tool_history_events(
            messages, session_id="session-a", raw_entries=selected
        )
        replay = durable_tool_history_events(
            messages, session_id="session-a", raw_entries=selected
        )
        other = durable_tool_history_events(
            messages, session_id="session-b", raw_entries=selected
        )
        window = recent_public_message_window(
            messages,
            session_id="session-a",
            raw_entries=selected,
            media_resolver=None,
        )
        recent_events = recent_tool_history_events(
            messages,
            session_id="session-a",
            raw_entries=selected,
            projected_messages=window,
        )

        self.assertEqual(messages[0][DURABLE_TURN_ID_KEY], "accepted-turn")
        self.assertEqual(messages[0]["clientMessageId"], "client-message")
        self.assertEqual(
            [message["id"] for message in messages],
            ["user", "tool-call", "tool-result", "answer"],
        )
        self.assertEqual([event["createdAtMs"] for event in events], [200, 300])
        self.assertEqual([event["timelineSequence"] for event in events], [2.2, 3.8])
        self.assertEqual({event["turnId"] for event in events}, {"accepted-turn"})
        self.assertEqual({message["turnId"] for message in window}, {"accepted-turn"})
        self.assertEqual(replay, events)
        self.assertEqual(recent_events, events)
        self.assertTrue(
            {event["eventId"] for event in events}.isdisjoint(
                event["eventId"] for event in other
            )
        )
        self.assertEqual(entries, original)
        self.assertEqual(messages, original_messages)

    def test_tail_projection_distinguishes_complete_partial_and_unproven_branches(
        self,
    ) -> None:
        entries = [
            {
                "type": "message",
                "id": "user",
                "parentId": "root",
                "message": {"role": "user", "content": "继续"},
            },
        ]
        complete = recent_messages_from_proven_tail(
            entries, leaf_id="user", header_id="root"
        )
        self.assertIsNotNone(complete)
        self.assertTrue(complete[2])
        partial = recent_messages_from_proven_tail(
            entries, leaf_id="user", header_id="older-root"
        )
        self.assertIsNotNone(partial)
        self.assertFalse(partial[2])
        self.assertIsNone(
            recent_messages_from_proven_tail(
                entries, leaf_id="missing", header_id="root"
            )
        )
        entries[0]["parentId"] = "user"
        self.assertIsNone(
            recent_messages_from_proven_tail(entries, leaf_id="user", header_id="root")
        )

    def test_branch_cursor_does_not_follow_a_forked_physical_last_row(self) -> None:
        entries = [
            {"type": "session", "id": "root"},
            {
                "type": "message",
                "id": "user",
                "parentId": "root",
                "message": {"role": "user", "content": "旧问题"},
            },
            {
                "type": "message",
                "id": "old-answer",
                "parentId": "user",
                "message": {"role": "assistant", "content": "旧回答"},
            },
            {
                "type": "message",
                "id": "current-user",
                "parentId": "old-answer",
                "message": {"role": "user", "content": "当前问题"},
            },
            {
                "type": "custom",
                "id": "current-settlement",
                "parentId": "current-user",
                "customType": "rag-ime.pi-turn-settlement",
                "data": {
                    "schemaVersion": "rag-ime.pi-turn-settlement.v1",
                    "turnId": "current-turn",
                },
            },
            {
                "type": "message",
                "id": "fork-user",
                "parentId": "old-answer",
                "message": {"role": "user", "content": "分叉问题"},
            },
            {
                "type": "message",
                "id": "fork-answer",
                "parentId": "fork-user",
                "message": {"role": "assistant", "content": "分叉回答"},
            },
        ]

        self.assertEqual(
            unambiguous_descendant_leaf(entries, ancestor_id="old-answer"),
            "old-answer",
        )
        self.assertEqual(
            terminal_branch_anchor(entries, turn_id="current-turn"),
            "current-settlement",
        )
        self.assertEqual(
            latest_terminal_descendant_leaf(entries, ancestor_id="old-answer"),
            "current-settlement",
        )
        forked_terminal = [
            *entries,
            {
                "type": "custom",
                "id": "fork-settlement",
                "parentId": "fork-answer",
                "customType": "rag-ime.pi-turn-settlement",
                "data": {
                    "schemaVersion": "rag-ime.pi-turn-settlement.v1",
                    "turnId": "fork-turn",
                },
            },
        ]
        self.assertEqual(
            latest_terminal_descendant_leaf(
                forked_terminal,
                ancestor_id="old-answer",
            ),
            "",
        )
        messages, selected = durable_branch_messages(
            entries,
            leaf_id="old-answer",
        )
        self.assertEqual([entry["id"] for entry in selected], ["user", "old-answer"])
        self.assertEqual([message["id"] for message in messages], ["user", "old-answer"])

    def test_transcript_tool_failure_keeps_pi_error_content_in_public_receipt(
        self,
    ) -> None:
        raw_messages = [
            {
                "id": "user-validation",
                "role": "user",
                "timestamp": 100,
                "content": [{"type": "text", "text": "修改文件"}],
            },
            {
                "id": "assistant-validation",
                "role": "assistant",
                "timestamp": 101,
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tool-validation",
                        "name": "write",
                        "arguments": {"path": "file.txt"},
                    }
                ],
            },
            {
                "role": "toolResult",
                "timestamp": 102,
                "toolCallId": "tool-validation",
                "toolName": "write",
                "isError": True,
                "details": {},
                "content": [
                    {
                        "type": "text",
                        "text": "Validation failed: resourceRevision: must have required properties resourceRevision",
                    }
                ],
            },
        ]

        events = durable_tool_history_events(
            raw_messages,
            session_id="session-validation",
        )
        finished = next(
            event for event in events if event["eventType"] == "tool_finished"
        )

        self.assertTrue(finished["payload"]["isError"])
        self.assertIn(
            "resourceRevision",
            json.dumps(finished["payload"]["result"], ensure_ascii=False),
        )

    def test_failed_provider_message_does_not_project_unexecuted_tool_draft(
        self,
    ) -> None:
        raw_messages = [
            {
                "id": "user-provider-retry",
                "role": "user",
                "timestamp": 100,
                "content": [{"type": "text", "text": "委派一次覆盖审查"}],
            },
            {
                "id": "assistant-provider-failed",
                "role": "assistant",
                "timestamp": 101,
                "stopReason": "error",
                "errorMessage": "fetch failed",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "call-never-executed",
                        "name": "agents",
                        "arguments": {
                            "op": "delegate",
                            "agent": "reviewer",
                            "version": "1",
                            "task": "partial provider draft",
                        },
                    }
                ],
            },
            {
                "id": "assistant-provider-recovered",
                "role": "assistant",
                "timestamp": 102,
                "stopReason": "toolUse",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "call-executed",
                        "name": "agents",
                        "arguments": {
                            "op": "delegate",
                            "tasks": [{"agent": "reviewer", "version": "1"}],
                        },
                    }
                ],
            },
            {
                "role": "toolResult",
                "timestamp": 103,
                "toolCallId": "call-executed",
                "toolName": "agents",
                "isError": False,
                "details": {"schemaVersion": "rag-ime.agent-delegation.v1"},
            },
        ]

        events = durable_tool_history_events(
            raw_messages,
            session_id="session-provider-retry-tool-draft",
        )

        tool_ids = [
            str(event["payload"].get("toolCallId") or "")
            for event in events
            if event["eventType"] in {"tool_started", "tool_finished"}
        ]
        self.assertEqual(["call-executed", "call-executed"], tool_ids)

    def test_snapshot_history_uses_only_the_selected_durable_branch(self) -> None:
        entries = [
            {"type": "session", "id": "root"},
            {
                "type": "message",
                "id": "entry-user",
                "parentId": "root",
                "timestamp": "1970-01-01T00:00:00.100Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "保留的提问"}],
                },
            },
            {
                "type": "message",
                "id": "entry-answer",
                "parentId": "entry-user",
                "timestamp": "1970-01-01T00:00:00.200Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "保留的回答"}],
                },
            },
            {
                "type": "message",
                "id": "entry-other-branch",
                "parentId": "entry-user",
                "timestamp": "1970-01-01T00:00:00.300Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "另一分支"}],
                },
            },
        ]

        messages, selected_entries = durable_branch_messages(
            entries,
            leaf_id="entry-answer",
        )

        self.assertEqual(
            [message["content"][0]["text"] for message in messages],
            ["保留的提问", "保留的回答"],
        )
        self.assertEqual(
            [entry["id"] for entry in selected_entries], ["entry-user", "entry-answer"]
        )
        self.assertEqual(messages[0]["id"], "entry-user")
        self.assertEqual(messages[0]["timestamp"], 100)

    def test_transcript_tool_messages_rebuild_an_inspectable_durable_timeline(
        self,
    ) -> None:
        raw_messages = [
            {
                "id": "user-1",
                "role": "user",
                "timestamp": 100,
                "content": [{"type": "text", "text": "检查项目"}],
            },
            {
                "id": "assistant-tool-1",
                "role": "assistant",
                "api": "openai-codex-responses",
                "timestamp": 101,
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "**Planning project inspection**",
                    },
                    {
                        "type": "toolCall",
                        "id": "tool-1",
                        "name": "workspace_read",
                        "arguments": {
                            "path": "/Users/private/project/README.md",
                            "apiKey": "top-secret",
                        },
                    },
                ],
            },
            {
                "role": "toolResult",
                "timestamp": 102,
                "toolCallId": "tool-1",
                "toolName": "workspace_read",
                "isError": False,
                "details": {
                    "summary": "读取 /Users/private/project/README.md",
                    "token": "secret",
                },
            },
        ]
        events = durable_tool_history_events(
            raw_messages,
            session_id="session-1",
            raw_entries=[
                {
                    "type": "message",
                    "timestamp": "1970-01-01T00:00:00.100Z",
                    "message": raw_messages[0],
                },
                {
                    "type": "message",
                    "timestamp": "1970-01-01T00:00:00.501Z",
                    "message": raw_messages[1],
                },
                {
                    "type": "message",
                    "timestamp": "1970-01-01T00:00:00.902Z",
                    "message": raw_messages[2],
                },
            ],
        )

        self.assertEqual(
            [event["eventType"] for event in events],
            ["reasoning_summary", "tool_started", "tool_finished"],
        )
        self.assertEqual(
            [event["turnId"] for event in events],
            ["history:user-1", "history:user-1", "history:user-1"],
        )
        self.assertEqual([event["createdAtMs"] for event in events], [501, 502, 902])
        self.assertEqual(events[0]["payload"]["items"], ["Planning project inspection"])
        self.assertEqual(events[1]["payload"]["publicResult"]["fileName"], "README.md")
        serialized = json.dumps(events, ensure_ascii=False)
        self.assertNotIn("top-secret", serialized)
        self.assertIn("/Users/private/project/README.md", serialized)
        self.assertIn("[REDACTED_SECRET]", serialized)

    def test_transcript_bash_failure_uses_the_structured_exit_receipt(self) -> None:
        raw_messages = [
            {
                "id": "user-bash",
                "role": "user",
                "timestamp": 100,
                "content": [{"type": "text", "text": "运行测试"}],
            },
            {
                "id": "assistant-bash",
                "role": "assistant",
                "timestamp": 101,
                "content": [
                    {
                        "type": "toolCall",
                        "id": "tool-bash-failed",
                        "name": "bash",
                        "arguments": {"command": "python3 -m unittest"},
                    }
                ],
            },
            {
                "role": "toolResult",
                "timestamp": 102,
                "toolCallId": "tool-bash-failed",
                "toolName": "bash",
                "isError": False,
                "content": [{"type": "text", "text": "FAILED"}],
                "details": {
                    "receipt": {
                        "exitCode": 1,
                        "timedOut": False,
                    }
                },
            },
        ]

        events = durable_tool_history_events(
            raw_messages,
            session_id="session-bash-history",
        )

        finished = next(
            event for event in events if event["eventType"] == "tool_finished"
        )
        self.assertTrue(finished["payload"]["isError"])

    def test_tool_history_applies_one_bounded_public_budget_per_session_snapshot(
        self,
    ) -> None:
        raw_messages: list[dict[str, object]] = [
            {
                "id": "user-tool-budget",
                "role": "user",
                "timestamp": 100,
                "content": [{"type": "text", "text": "执行多项检查"}],
            }
        ]
        for index in range(20):
            tool_call_id = f"tool-budget-{index + 1}"
            raw_messages.extend(
                [
                    {
                        "id": f"assistant-{index + 1}",
                        "role": "assistant",
                        "timestamp": 101 + index * 2,
                        "content": [
                            {
                                "type": "toolCall",
                                "id": tool_call_id,
                                "name": "bash",
                                "arguments": {"command": f"probe-{index + 1}"},
                            }
                        ],
                    },
                    {
                        "role": "toolResult",
                        "timestamp": 102 + index * 2,
                        "toolCallId": tool_call_id,
                        "toolName": "bash",
                        "isError": False,
                        "details": {"output": "X" * 5_000},
                    },
                ]
            )

        events = durable_tool_history_events(
            raw_messages,
            session_id="session-tool-budget",
        )
        serialized = json.dumps(
            events,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        tool_ids = {str(event["payload"].get("toolCallId") or "") for event in events}

        self.assertLessEqual(len(serialized), 50_000)
        self.assertIn("tool-budget-20", tool_ids)
        self.assertNotIn("tool-budget-1", tool_ids)
        for tool_call_id in tool_ids:
            self.assertEqual(
                [
                    event["eventType"]
                    for event in events
                    if event["payload"].get("toolCallId") == tool_call_id
                ],
                ["tool_started", "tool_finished"],
            )

    def test_nested_calls_restore_parent_link_and_do_not_invent_unfinished_terminal(self) -> None:
        raw_messages = [
            {
                "id": "user-codemode",
                "role": "user",
                "timestamp": 100,
                "content": [{"type": "text", "text": "批量检查"}],
            },
            {
                "id": "assistant-codemode",
                "role": "assistant",
                "timestamp": 101,
                "content": [
                    {
                        "type": "toolCall",
                        "id": "codemode:1",
                        "name": "codemode",
                        "arguments": {"code": "await tools.workspace_read({path: 'README.md'})"},
                    }
                ],
            },
            {
                "role": "toolResult",
                "timestamp": 102,
                "toolCallId": "codemode:1",
                "toolName": "codemode",
                "isError": True,
                "content": [{"type": "text", "text": "Script failed"}],
                "nestedCalls": {
                    "complete": False,
                    "calls": [
                        {
                            "id": "codemode:1/1",
                            "name": "workspace_read",
                            "arguments": {"path": "README.md"},
                            "status": "ok",
                            "durationMs": 12,
                        },
                        {
                            "id": "codemode:1/2",
                            "name": "workspace_write",
                            "arguments": {"path": "out.txt"},
                            "status": "unfinished",
                        },
                        {
                            "id": "codemode:1/3",
                            "name": "workspace_delete",
                            "args": "{\"path\":\"out.txt\"}",
                            "status": "cancelled",
                            "error": "user stopped the script",
                        },
                    ],
                },
            },
        ]

        events = durable_tool_history_events(
            raw_messages,
            session_id="session-codemode-history",
        )
        nested = [
            event
            for event in events
            if event["payload"].get("parentToolCallId") == "codemode:1"
        ]

        self.assertEqual(
            [(event["eventType"], event["payload"]["toolCallId"]) for event in nested],
            [
                ("tool_started", "codemode:1/1"),
                ("tool_finished", "codemode:1/1"),
                ("tool_started", "codemode:1/2"),
                ("tool_started", "codemode:1/3"),
                ("tool_finished", "codemode:1/3"),
            ],
        )
        finished = next(event for event in nested if event["eventType"] == "tool_finished")
        self.assertEqual(finished["payload"]["args"], {"path": "README.md"})
        self.assertEqual(finished["payload"]["durationMs"], 12)
        cancelled = next(
            event
            for event in nested
            if event["payload"]["toolCallId"] == "codemode:1/3"
            and event["eventType"] == "tool_finished"
        )
        self.assertTrue(cancelled["payload"]["isError"])
        self.assertEqual(cancelled["payload"]["error"], "user stopped the script")
        outer = next(
            event
            for event in events
            if event["payload"].get("toolCallId") == "codemode:1"
            and "nestedCallsComplete" in event["payload"]
        )
        self.assertFalse(outer["payload"]["nestedCallsComplete"])
        self.assertTrue(events[-1]["payload"]["isError"])


if __name__ == "__main__":
    unittest.main()
