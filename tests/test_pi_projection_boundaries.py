from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rag_ime.pi.event_projection import (
    failed_settlement_receipt,
    text_delta_payload,
    tool_event_payload,
)
from rag_ime.pi.transcript_io import (
    read_recent_transcript_tail,
    transcript_boundary_sha256,
)
from rag_ime.pi.ui_requests import public_ui_request, resolve_ui_response
from rag_ime.pi.values import PiRuntimeError


class PiProjectionBoundaryTests(unittest.TestCase):
    def test_recent_file_reader_keeps_header_and_only_complete_tail_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.jsonl"
            header = {"type": "session", "id": "pi:test"}
            last = {"type": "message", "id": "last"}
            path.write_text(
                json.dumps(header)
                + "\n"
                + json.dumps({"text": "x" * 500})
                + "\n"
                + json.dumps(last)
                + "\n",
                encoding="utf-8",
            )
            with patch("rag_ime.pi.transcript_io._RECENT_SESSION_TAIL_SCAN_BYTES", 100):
                self.assertEqual(
                    read_recent_transcript_tail(path, path.stat().st_size),
                    (header, [last]),
                )

    def test_append_boundary_ignores_new_rows_but_rejects_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.jsonl"
            path.write_bytes(b"original\n")
            digest = transcript_boundary_sha256(path, 9)
            self.assertEqual(digest, hashlib.sha256(b"original\n").hexdigest())
            path.write_bytes(b"original\nappended\n")
            self.assertEqual(transcript_boundary_sha256(path, 9), digest)
            path.write_bytes(b"short")
            with self.assertRaises(OSError):
                transcript_boundary_sha256(path, 9)

    def test_invalid_header_is_unavailable_without_creating_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.jsonl"
            path.write_bytes(b"not-json\n")
            self.assertIsNone(read_recent_transcript_tail(path, path.stat().st_size))
            self.assertEqual(path.read_bytes(), b"not-json\n")

    def test_transcript_readers_reject_file_and_directory_symlink_swaps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            session_root = root / "sessions"
            outside = root / "outside"
            session_root.mkdir()
            outside.mkdir()
            target = session_root / "history.jsonl"
            target.write_bytes(b'{"type":"session","id":"pi:test"}\n')
            size = target.stat().st_size

            # The caller may have measured the old regular file before a
            # concurrent writer replaces it with a symlink.  The descriptor
            # open must reject the new final component rather than follow it.
            swapped_file = session_root / "swapped.jsonl"
            swapped_file.write_bytes(target.read_bytes())
            old_size = swapped_file.stat().st_size
            swapped_file.unlink()
            swapped_file.symlink_to(outside / "secret.jsonl")
            (outside / "secret.jsonl").write_bytes(target.read_bytes())
            with self.assertRaises(OSError):
                read_recent_transcript_tail(swapped_file, old_size)
            with self.assertRaises(OSError):
                transcript_boundary_sha256(swapped_file, old_size)

            # A parent directory can be exchanged for a symlink after the
            # path was persisted.  O_NOFOLLOW is required on every parent,
            # not only on the final transcript filename.
            swapped_parent = session_root / "parts"
            swapped_parent.mkdir()
            swapped_path = swapped_parent / "history.jsonl"
            swapped_path.write_bytes(target.read_bytes())
            swapped_parent.rename(session_root / "parts-old")
            swapped_parent.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(OSError):
                read_recent_transcript_tail(swapped_path, size)
            with self.assertRaises(OSError):
                transcript_boundary_sha256(swapped_path, size)

    def test_text_delta_keeps_identity_and_filters_internal_preamble(self) -> None:
        message = {"content": "<thinking>private</thinking>Visible"}
        event = text_delta_payload(
            message,
            {"delta": "Visible", "contentIndex": 0},
            turn_id="turn:1",
            replace_block=True,
            source_loop_id="loop:2",
        )
        self.assertEqual(event["delta"], "Visible")
        self.assertEqual(event["messageId"], "turn:1:assistant")
        self.assertTrue(event["replaceContent"])
        self.assertTrue(event["replaceBlock"])
        self.assertEqual(event["sourceLoopId"], "loop:2")
        self.assertEqual(message["content"], "<thinking>private</thinking>Visible")

    def test_tool_events_preserve_measured_duration_without_inventing_timing(
        self,
    ) -> None:
        raw = {
            "toolName": "example",
            "toolCallId": "call:1",
            "args": {"path": "sample"},
        }
        before = copy.deepcopy(raw)
        event_type, start = tool_event_payload(
            raw, event_type="tool_execution_start", source_loop_id="loop:1"
        )
        self.assertEqual(event_type, "tool_started")
        self.assertNotIn("durationMs", start)
        event_type, end = tool_event_payload(
            {**raw, "durationMs": 37, "result": {"ok": True}},
            event_type="tool_execution_end",
            source_loop_id="loop:1",
        )
        self.assertEqual(event_type, "tool_finished")
        self.assertEqual(end["durationMs"], 37)
        self.assertFalse(end["isError"])
        self.assertEqual(end["sourceLoopId"], "loop:1")
        self.assertEqual(raw, before)

    def test_failed_settlement_requires_numeric_zero_pending_work(self) -> None:
        for pending, terminal in [(0, True), (False, False), ("0", False), (1, False)]:
            with self.subTest(pending=pending):
                result = failed_settlement_receipt(
                    {
                        "receipt": {
                            "schemaVersion": "pi.agent-settled.v2",
                            "disposition": "failed",
                            "pendingOperations": pending,
                        }
                    },
                    allow_aborted=False,
                )
                self.assertIsNotNone(result)
                self.assertEqual(result[0], terminal)

    def test_ui_projection_bounds_wire_fields_without_mutating_input(self) -> None:
        raw = {
            "message": "m" * 700,
            "options": ["o" * 300] * 120,
            "prefill": "p" * 5000,
            "timeout": "-7",
        }
        before = copy.deepcopy(raw)
        safe = public_ui_request(
            raw, request_id="q:1", method="select", title="t" * 200
        )
        self.assertEqual(len(safe["title"]), 160)
        self.assertEqual(len(safe["message"]), 500)
        self.assertEqual(len(safe["options"]), 100)
        self.assertEqual(len(safe["options"][0]), 240)
        self.assertEqual(len(safe["prefill"]), 4000)
        self.assertEqual(safe["timeout"], 0)
        self.assertEqual(raw, before)

    def test_ui_rejection_does_not_change_request_or_prevent_valid_retry(self) -> None:
        request = {"method": "select", "options": ["A", "B"], "_resolving": True}
        before = copy.deepcopy(request)
        with self.assertRaises(PiRuntimeError):
            resolve_ui_response(request, {"value": "C"})
        self.assertEqual(request, before)
        self.assertEqual(
            resolve_ui_response(request, {"value": "B"}),
            ({"value": "B"}, "direct_user"),
        )

    def test_automatic_ui_resolution_cannot_confirm_on_behalf_of_user(self) -> None:
        request = {"method": "confirm"}
        for source in ("timeout", "runtime_cancelled"):
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    resolve_ui_response(
                        request, {"confirmed": True, "resolutionSource": source}
                    )
                self.assertEqual(
                    resolve_ui_response(
                        request, {"cancelled": True, "resolutionSource": source}
                    ),
                    ({"cancelled": True}, source),
                )
        self.assertEqual(
            resolve_ui_response(request, {"confirmed": False}),
            ({"confirmed": False}, "direct_user"),
        )
