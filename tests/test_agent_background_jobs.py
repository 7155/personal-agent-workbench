from __future__ import annotations

import json
import tempfile
import os
import subprocess
import sys
import time
import unittest
import threading
from unittest.mock import patch
from contextlib import nullcontext
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import rag_ime.agent_background_jobs as background_job_module
from rag_ime.agent_background_jobs import _LiveJob
from rag_ime.agent_background_jobs import AgentBackgroundJobError, AgentBackgroundJobService
from rag_ime.agent_execution_policy import (
    DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
    FULL_TRUST_EXECUTION_MODE,
)
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.db import sqlite_connection
from rag_ime.agent_workspace import WorkspaceHarness
from rag_ime.debug_server import DebugImeService, DebugRequestHandler, DebugServerConfig


class _BlockingReplacePath:
    def __init__(
        self,
        path: Path,
        replace_started: threading.Event,
        allow_replace: threading.Event,
    ) -> None:
        self.path = path
        self.replace_started = replace_started
        self.allow_replace = allow_replace

    def exists(self) -> bool:
        return self.path.exists()

    def stat(self):
        return self.path.stat()

    def read_bytes(self) -> bytes:
        return self.path.read_bytes()

    def write_bytes(self, data: bytes) -> int:
        self.replace_started.set()
        if not self.allow_replace.wait(timeout=3):
            raise TimeoutError("test did not release log replacement")
        return self.path.write_bytes(data)

    def open(self, *args, **kwargs):
        return self.path.open(*args, **kwargs)


class AgentBackgroundJobServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="rag-ime-background-job-")
        self.root = Path(self.temporary.name) / "workspace"
        self.root.mkdir()
        self.db_path = Path(self.temporary.name) / "rag-ime.sqlite"
        self.sessions = AgentSessionStore(self.db_path)
        self.sessions.initialize()
        self.session = self.sessions.create(
            title="background job test",
            mode="coordinator",
            workspace_roots=[str(self.root)],
            created_at_ms=1,
        )
        self.events: list[tuple[str, dict[str, object]]] = []
        self.harness = WorkspaceHarness()
        self.service = AgentBackgroundJobService(
            self.db_path,
            events=self._record_event,
            workspace_harness=self.harness,
        )
        self.service.initialize()

    def tearDown(self) -> None:
        self.service.close()
        self.temporary.cleanup()

    def _use_full_trust_process_session(self) -> None:
        """Exercise process ownership with an explicitly unrestricted Session."""
        self.session = self.sessions.create(
            title="full-trust process lifecycle test",
            mode="coordinator",
            tool_profile_version=DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
            execution_mode=FULL_TRUST_EXECUTION_MODE,
            workspace_roots=[str(self.root)],
        )

    def test_managed_launch_without_sandbox_fails_before_process_spawn(self) -> None:
        harness = WorkspaceHarness(
            sandbox_executable=self.root / "missing-sandbox-exec",
        )
        self.service.workspace_harness = harness
        prepared = harness.prepare_background_command(
            self.session,
            {
                "command": "printf should-not-run",
                "cwd": str(self.root),
                "timeoutSeconds": 10,
            },
        )
        self.assertFalse(prepared.unrestricted)

        with patch(
            "rag_ime.agent_workspace.subprocess.Popen",
            side_effect=AssertionError("managed command must not spawn without a sandbox"),
        ) as popen:
            with self.assertRaisesRegex(
                AgentBackgroundJobError,
                "macOS command harness is unavailable; refusing unsandboxed execution",
            ):
                self.service.start(str(self.session["id"]), prepared)
        popen.assert_not_called()

        jobs = self.service.list(str(self.session["id"]))["items"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "failed")
        self.assertIsNone(jobs[0]["exitCode"])
        self.assertGreater(jobs[0]["endedAtMs"], 0)
        self.assertEqual(self.service._live, {})
        self.assertEqual(
            [event_type for event_type, _ in self.events],
            ["background_job_failed"],
        )

    @unittest.skipUnless(sys.platform == "darwin", "requires the macOS sandbox harness")
    def test_command_completes_with_durable_redacted_logs_and_events(self) -> None:
        raw_secret = "abcdefghijklmnop"
        prepared = self.harness.prepare_background_command(
            self.session,
            {
                "command": (
                    "python3 -c \"print('hello'); "
                    "print('api' + '_key=' + 'abcdefghijklmnop')\""
                ),
                "cwd": str(self.root),
                "timeoutSeconds": 10,
            },
        )

        self.assertFalse(prepared.unrestricted)
        receipt = self.service.start(str(self.session["id"]), prepared, label="日志任务")
        job_id = str(receipt["job"]["jobId"])
        job = self._wait_for_terminal(job_id)
        logs = self.service.logs(str(self.session["id"]), job_id, cursor=0)

        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["exitCode"], 0)
        self.assertIn("hello", logs["text"])
        self.assertIn("api_key=[REDACTED]", logs["text"])
        self.assertNotIn(raw_secret, logs["text"])
        self.assertEqual(
            [event_type for event_type, _ in self.events if event_type != "background_job_progress"],
            ["background_job_started", "background_job_completed"],
        )

        observer = AgentBackgroundJobService(
            self.db_path,
            events=lambda *_args, **_kwargs: None,
            workspace_harness=self.harness,
            execution_owner=False,
        )
        observer.initialize()
        try:
            durable = observer.status(str(self.session["id"]), job_id)["job"]
            self.assertEqual(durable["status"], "completed")
            self.assertIn("hello", observer.logs(str(self.session["id"]), job_id)["text"])
            with self.assertRaisesRegex(AgentBackgroundJobError, "Agent Gateway"):
                observer.start(str(self.session["id"]), prepared)
        finally:
            observer.close()

    @unittest.skipUnless(sys.platform == "darwin", "requires the macOS sandbox harness")
    def test_explicit_shell_exit_preserves_authoritative_exit_receipt(self) -> None:
        inherited_home = Path(self.temporary.name) / "inherited-home"
        inherited_home.mkdir()
        prepared = self.harness.prepare_background_command(
            self.session,
            {
                "command": (
                    "printf '0\\n' > \"$HOME/exit.status\"; "
                    "printf 'before-explicit-exit\\n'; exit 7"
                ),
                "cwd": str(self.root),
                "timeoutSeconds": 10,
            },
        )

        self.assertFalse(prepared.unrestricted)
        # Keep even a regressed environment boundary away from the real HOME.
        with patch.dict(os.environ, {"HOME": str(inherited_home)}):
            receipt = self.service.start(str(self.session["id"]), prepared)
        job_id = str(receipt["job"]["jobId"])
        job = self._wait_for_terminal(job_id)
        logs = self.service.logs(str(self.session["id"]), job_id)

        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["exitCode"], 7)
        self.assertIn("before-explicit-exit", logs["text"])
        self.assertFalse((inherited_home / "exit.status").exists())

    def test_raw_output_preserves_redaction_across_chunks(self) -> None:
        chunk_size = 1_048_576
        secret = b"abcdefghijklmnop"
        raw_path = Path(self.temporary.name) / "chunked.raw"
        log_path = Path(self.temporary.name) / "chunked.log"
        log_path.touch()
        raw_path.write_bytes(
            b"x" * (chunk_size - len(b" api_"))
            + b" api_"
            + b"key="
            + secret
            + b"\n"
        )
        live = _LiveJob(
            launched=object(),
            log_path=log_path,
            max_run_seconds=60,
            raw_output_path=raw_path,
        )

        with (
            patch.object(self.service, "_persist_live_progress"),
            patch.object(self.service._ownership, "guard", side_effect=lambda _: nullcontext()),
        ):
            self.service._drain_raw_output("bg_chunked", live, final=True)
        durable = log_path.read_text(encoding="utf-8")

        self.assertNotIn(secret.decode("ascii"), durable)
        self.assertIn("api_key=[REDACTED]", durable)

    def test_raw_output_preserves_utf8_across_chunks(self) -> None:
        chunk_size = 1_048_576
        raw_path = Path(self.temporary.name) / "utf8.raw"
        log_path = Path(self.temporary.name) / "utf8.log"
        log_path.touch()
        raw_path.write_bytes(
            b"A" * (chunk_size - 1) + "😀".encode("utf-8") + b"Z"
        )
        live = _LiveJob(
            launched=object(),
            log_path=log_path,
            max_run_seconds=60,
            raw_output_path=raw_path,
        )

        with (
            patch.object(self.service, "_persist_live_progress"),
            patch.object(self.service._ownership, "guard", side_effect=lambda _: nullcontext()),
        ):
            self.service._drain_raw_output("bg_utf8", live, final=True)
        durable = log_path.read_text(encoding="utf-8")

        self.assertIn("😀Z", durable)
        self.assertNotIn("�", durable)

    def test_newline_free_threshold_flush_preserves_secret_redaction_context(self) -> None:
        log_path = Path(self.temporary.name) / "threshold.log"
        log_path.touch()
        live = _LiveJob(
            launched=object(),
            log_path=log_path,
            max_run_seconds=60,
        )
        secret = "abcdefghijklmnop"
        total_length = 65_537
        flush_at = total_length - 1_024
        label = " api_key="
        secret_start = flush_at - 8
        text = (
            "x" * (secret_start - len(label))
            + label
            + secret
            + " "
        )
        text += "y" * (total_length - len(text))

        pending = self.service._flush_complete_lines(live, text)
        self.service._append_redacted_text(live, pending)
        durable_text = log_path.read_text(encoding="utf-8")

        self.assertEqual(durable_text, self.harness.redact_output(text))
        self.assertNotIn(secret[8:], durable_text)

    def test_unrestricted_profiles_preserve_raw_background_output(self) -> None:
        secret = "abcdefghijklmnop"
        for profile, execution_mode in (
            ("control-center-full-access-v1", "per_action"),
            ("control-center-auto-approve-v1", "full_trust"),
        ):
            with self.subTest(profile=profile):
                session = self.sessions.create(
                    title=f"raw output {profile}",
                    mode="coordinator",
                    tool_profile_version=profile,
                    execution_mode=execution_mode,
                    workspace_roots=["/"],
                )
                log_path = Path(self.temporary.name) / f"{execution_mode}.log"
                log_path.touch()
                live = _LiveJob(
                    launched=object(),
                    log_path=log_path,
                    max_run_seconds=60,
                    session_id=str(session["id"]),
                )

                self.service._append_redacted_text(
                    live,
                    f"api_key={secret}\n",
                )

                self.assertEqual(
                    log_path.read_text(encoding="utf-8"),
                    f"api_key={secret}\n",
                )

    def test_log_pages_extend_limit_to_complete_utf8_characters(self) -> None:
        job_id = "bg_" + "b" * 32
        text = "你A🙂"
        encoded = text.encode("utf-8")
        log_path = self.service._log_path(job_id)
        log_path.write_bytes(encoded)
        with sqlite_connection(self.db_path, foreign_keys=True) as conn:
            conn.execute(
                """
                INSERT INTO agent_background_jobs(
                    job_id, session_id, label, status, command, command_sha256,
                    cwd, network_allowed, max_run_seconds, log_path,
                    output_bytes, created_at_ms, updated_at_ms, ended_at_ms
                ) VALUES (?, ?, 'utf8', 'completed', 'echo utf8', ?, ?, 0, 60, ?,
                          ?, 1, 1, 1)
                """,
                (
                    job_id,
                    str(self.session["id"]),
                    "b" * 64,
                    str(self.root),
                    str(log_path),
                    len(encoded),
                ),
            )

        cursor = 0
        pages: list[str] = []
        page_bytes: list[bytes] = []
        while cursor < len(encoded):
            page = self.service.logs(
                str(self.session["id"]),
                job_id,
                cursor=cursor,
                limit_bytes=1,
            )
            payload = str(page["text"]).encode("utf-8")
            self.assertGreater(len(payload), 0)
            self.assertEqual(page["cursor"], cursor)
            self.assertEqual(page["nextCursor"], cursor + len(payload))
            pages.append(str(page["text"]))
            page_bytes.append(payload)
            cursor = int(page["nextCursor"])

        self.assertEqual("".join(pages), text)
        self.assertEqual(b"".join(page_bytes), encoded)

    def test_terminal_event_publish_exception_does_not_reverse_completion(self) -> None:
        self._use_full_trust_process_session()
        attempted_events: list[str] = []

        def fail_completed_event(
            _session_id: str,
            event_type: str,
            _payload: dict[str, object],
            **_kwargs: object,
        ) -> None:
            attempted_events.append(event_type)
            if event_type == "background_job_completed":
                raise RuntimeError("terminal event publisher unavailable")

        self.service.events = fail_completed_event
        prepared = self.harness.prepare_background_command(
            self.session,
            {
                "command": "python3 -c \"print('completed')\"",
                "cwd": str(self.root),
                "timeoutSeconds": 10,
            },
        )
        self.assertTrue(prepared.unrestricted)

        receipt = self.service.start(str(self.session["id"]), prepared)
        job_id = str(receipt["job"]["jobId"])
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            with self.service._lock:
                is_live = job_id in self.service._live
            if not is_live:
                break
            time.sleep(0.02)

        persisted = self.service.status(str(self.session["id"]), job_id)["job"]
        self.assertFalse(is_live)
        self.assertEqual(persisted["status"], "completed")
        self.assertEqual(persisted["error"], "")
        self.assertIn("background_job_completed", attempted_events)
        self.assertNotIn("background_job_failed", attempted_events)

    def test_cancel_stops_process_group_and_persists_receipt(self) -> None:
        self._use_full_trust_process_session()
        prepared = self.harness.prepare_background_command(
            self.session,
            {
                "command": "python3 -c \"import time; print('ready', flush=True); time.sleep(30)\"",
                "cwd": str(self.root),
                "timeoutSeconds": 60,
            },
        )
        self.assertTrue(prepared.unrestricted)
        started = self.service.start(str(self.session["id"]), prepared, label="可取消任务")
        job_id = str(started["job"]["jobId"])
        self._wait_for_log(job_id, "ready")

        receipt = self.service.cancel(
            str(self.session["id"]),
            job_id,
            reason="test_requested",
        )
        job = self._wait_for_terminal(job_id)

        self.assertTrue(receipt["ok"])
        self.assertEqual(job["status"], "cancelled")
        self.assertGreater(job["endedAtMs"], 0)
        self.assertEqual(job["error"], "test_requested")
        self.assertIn("background_job_cancelled", [event_type for event_type, _ in self.events])

    def test_repeated_cancel_preserves_first_request(self) -> None:
        job_id = "bg_" + "c" * 32
        log_path = self.service._log_path(job_id)
        log_path.touch()
        with sqlite_connection(self.db_path, foreign_keys=True) as conn:
            conn.execute(
                """
                INSERT INTO agent_background_jobs(
                    job_id, session_id, label, status, command, command_sha256,
                    cwd, network_allowed, max_run_seconds, log_path,
                    lifecycle_cancel_request_id, created_at_ms, updated_at_ms
                ) VALUES (?, ?, 'idempotent cancel', 'running', 'sleep', ?, ?,
                          0, 60, ?, 'receipt:first', 1, 1)
                """,
                (
                    job_id,
                    str(self.session["id"]),
                    "c" * 64,
                    str(self.root),
                    str(log_path),
                ),
            )

        class NoopTerminationHarness(WorkspaceHarness):
            def terminate_background(self, _launched) -> None:
                return None

        live = _LiveJob(
            launched=object(),
            log_path=log_path,
            max_run_seconds=60,
        )
        self.service.workspace_harness = NoopTerminationHarness()
        with self.service._lock:
            self.service._live[job_id] = live
        try:
            with patch.object(
                background_job_module,
                "_now_ms",
                side_effect=[1_000, 2_000],
            ):
                first = self.service.cancel(
                    str(self.session["id"]),
                    job_id,
                    reason="first_reason",
                )
                repeated = self.service.cancel(
                    str(self.session["id"]),
                    job_id,
                    reason="second_reason",
                )

            self.assertEqual(first["cancelReceipt"]["requestedAtMs"], 1_000)
            self.assertEqual(repeated["cancelReceipt"]["requestedAtMs"], 1_000)
            self.assertEqual(repeated["job"]["cancelRequestedAtMs"], 1_000)
            self.assertEqual(repeated["job"]["error"], "first_reason")
            with sqlite_connection(self.db_path) as conn:
                persisted = conn.execute(
                    """
                    SELECT lifecycle_cancel_request_id
                    FROM agent_background_jobs
                    WHERE job_id = ?
                    """,
                    (job_id,),
                ).fetchone()
            self.assertEqual(persisted[0], "receipt:first")
        finally:
            with self.service._lock:
                self.service._live.pop(job_id, None)
            with sqlite_connection(self.db_path) as conn:
                conn.execute(
                    """
                    UPDATE agent_background_jobs
                    SET status = 'failed', ended_at_ms = 1
                    WHERE job_id = ?
                    """,
                    (job_id,),
                )

    def test_room_bound_cancel_requires_matching_room_owner(self) -> None:
        self._use_full_trust_process_session()
        prepared = self.harness.prepare_background_command(
            self.session,
            {
                "command": (
                    "python3 -c \"import time; "
                    "print('room-ready', flush=True); time.sleep(30)\""
                ),
                "cwd": str(self.root),
                "timeoutSeconds": 60,
            },
        )
        self.assertTrue(prepared.unrestricted)
        started = self.service.start(
            str(self.session["id"]),
            prepared,
            causal_metadata={
                "turnId": "room-root:test",
                "roomBound": True,
            },
        )
        job_id = str(started["job"]["jobId"])
        self._wait_for_log(job_id, "room-ready")

        with self.assertRaisesRegex(AgentBackgroundJobError, "Room/Root owner"):
            self.service.cancel(str(self.session["id"]), job_id)
        with self.assertRaisesRegex(AgentBackgroundJobError, "Room/Root owner"):
            self.service.cancel_session(
                str(self.session["id"]),
                reason="ordinary_session_abort",
            )
        self.assertEqual(
            self.service.status(str(self.session["id"]), job_id)["job"]["status"],
            "running",
        )

        receipt = self.service.cancel_room_owned(
            str(self.session["id"]),
            job_id,
            room_turn_id="room-root:test",
            reason="room_owner_requested",
        )
        job = self._wait_for_terminal(job_id)
        self.assertTrue(receipt["ok"])
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["error"], "room_owner_requested")

    def test_execution_owner_reattaches_running_job_after_restart(self) -> None:
        self._use_full_trust_process_session()
        prepared = self.harness.prepare_background_command(
            self.session,
            {
                "command": (
                    "python3 -c \"import time; "
                    "print('before-restart', flush=True); time.sleep(1.5); "
                    "print('after-restart', flush=True)\""
                ),
                "cwd": str(self.root),
                "timeoutSeconds": 10,
            },
        )
        self.assertTrue(prepared.unrestricted)
        started = self.service.start(str(self.session["id"]), prepared, label="重启语义")
        job_id = str(started["job"]["jobId"])
        original_pid = started["job"]["pid"]
        self._wait_for_log(job_id, "before-restart")
        self.service.close()

        observer = AgentBackgroundJobService(
            self.db_path,
            events=lambda *_args, **_kwargs: None,
            workspace_harness=self.harness,
            execution_owner=False,
        )
        observer.initialize()
        try:
            self.assertEqual(
                observer.status(str(self.session["id"]), job_id)["job"]["status"],
                "running",
            )
        finally:
            observer.close()

        restarted_owner = AgentBackgroundJobService(
            self.db_path,
            events=lambda *_args, **_kwargs: None,
            workspace_harness=self.harness,
        )
        restarted_owner.initialize()
        try:
            deadline = time.monotonic() + 8
            recovered = restarted_owner.status(str(self.session["id"]), job_id)["job"]
            while recovered["status"] in {"queued", "running", "cancelling"} and time.monotonic() < deadline:
                time.sleep(0.05)
                recovered = restarted_owner.status(str(self.session["id"]), job_id)["job"]
            self.assertEqual(recovered["status"], "completed")
            self.assertEqual(recovered["pid"], original_pid)
            self.assertEqual(recovered["exitCode"], 0)
            logs = restarted_owner.logs(str(self.session["id"]), job_id)["text"]
            self.assertIn("before-restart", logs)
            self.assertIn("after-restart", logs)
        finally:
            restarted_owner.close()

    def test_owner_preserves_verified_legacy_process_group_on_recovery(self) -> None:
        job_id = "bg_" + "d" * 32
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        waiter = threading.Thread(target=process.wait)
        waiter.start()
        try:
            identity = background_job_module._process_identity(process.pid)
            self.assertIsNotNone(identity)
            process_group_id, birth_token = identity
            log_path = self.service._log_path(job_id)
            with sqlite_connection(self.db_path, foreign_keys=True) as conn:
                conn.execute(
                    """
                    INSERT INTO agent_background_jobs(
                        job_id, session_id, label, status, command,
                        command_sha256, cwd, network_allowed, max_run_seconds,
                        pid, process_group_id, process_birth_token, log_path,
                        created_at_ms, started_at_ms, updated_at_ms
                    ) VALUES (?, ?, 'recover live group', 'running', 'sleep', ?,
                              ?, 0, 60, ?, ?, ?, ?, 1, 1, 1)
                    """,
                    (
                        job_id,
                        str(self.session["id"]),
                        "d" * 64,
                        str(self.root),
                        process.pid,
                        process_group_id,
                        birth_token,
                        str(log_path),
                    ),
                )

            observer = AgentBackgroundJobService(
                self.db_path,
                events=lambda *_args, **_kwargs: None,
                execution_owner=False,
            )
            try:
                with patch.object(
                    background_job_module,
                    "_signal_process_group",
                ) as signal_group:
                    observer.initialize()
                signal_group.assert_not_called()
                self.assertIsNone(process.poll())
                self.assertEqual(
                    observer.status(str(self.session["id"]), job_id)["job"]["status"],
                    "running",
                )
            finally:
                observer.close()

            recovered_owner = AgentBackgroundJobService(
                self.db_path,
                events=lambda *_args, **_kwargs: None,
            )
            try:
                recovered_owner.initialize()
                recovered = recovered_owner.status(
                    str(self.session["id"]),
                    job_id,
                )["job"]
                self.assertEqual(recovered["status"], "running")
                self.assertIsNone(process.poll())
                recovered_owner.cancel(
                    str(self.session["id"]),
                    job_id,
                    reason="test_cleanup",
                )
                waiter.join(timeout=3)
                self.assertFalse(waiter.is_alive())
                self.assertIsNotNone(process.poll())
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    recovered = recovered_owner.status(
                        str(self.session["id"]), job_id
                    )["job"]
                    if recovered["status"] == "cancelled":
                        break
                    time.sleep(0.02)
                self.assertEqual(recovered["status"], "cancelled")
            finally:
                recovered_owner.close()
        finally:
            if process.poll() is None:
                WorkspaceHarness._terminate_group(process)
            process.wait(timeout=3)
            waiter.join(timeout=3)

    def test_recovery_identity_mismatch_orphans_without_signalling(self) -> None:
        job_id = "bg_" + "e" * 32
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            identity = background_job_module._process_identity(process.pid)
            self.assertIsNotNone(identity)
            process_group_id, birth_token = identity
            log_path = self.service._log_path(job_id)
            with sqlite_connection(self.db_path, foreign_keys=True) as conn:
                conn.execute(
                    """
                    INSERT INTO agent_background_jobs(
                        job_id, session_id, label, status, command,
                        command_sha256, cwd, network_allowed, max_run_seconds,
                        pid, process_group_id, process_birth_token, log_path,
                        created_at_ms, started_at_ms, updated_at_ms
                    ) VALUES (?, ?, 'mismatched live group', 'running', 'sleep', ?,
                              ?, 0, 60, ?, ?, ?, ?, 1, 1, 1)
                    """,
                    (
                        job_id,
                        str(self.session["id"]),
                        "e" * 64,
                        str(self.root),
                        process.pid,
                        process_group_id,
                        f"{birth_token}:recycled",
                        str(log_path),
                    ),
                )

            recovered_owner = AgentBackgroundJobService(
                self.db_path,
                events=lambda *_args, **_kwargs: None,
            )
            try:
                with patch.object(
                    background_job_module,
                    "_signal_process_group",
                ) as signal_group:
                    recovered_owner.initialize()
                signal_group.assert_not_called()
                self.assertIsNone(process.poll())
                recovered = recovered_owner.status(
                    str(self.session["id"]),
                    job_id,
                )["job"]
                self.assertEqual(recovered["status"], "orphaned")
                self.assertGreater(recovered["endedAtMs"], 0)
                self.assertIn("未向可能复用的进程发送信号", recovered["error"])
                self.assertEqual(
                    recovered_owner.list(str(self.session["id"]))["activeCount"],
                    0,
                )
            finally:
                recovered_owner.close()
        finally:
            WorkspaceHarness._terminate_group(process)
            process.wait(timeout=3)

    def test_post_spawn_setup_failure_reaps_process_and_cleans_live_resources(self) -> None:
        self._use_full_trust_process_session()
        class CapturingHarness(WorkspaceHarness):
            launched = None

            def spawn_background(self, prepared, **kwargs):
                self.launched = super().spawn_background(prepared, **kwargs)
                return self.launched

        harness = CapturingHarness()
        self.service.workspace_harness = harness

        def fail_started_event(*_args, **_kwargs):
            raise RuntimeError("event publisher unavailable")

        self.service.events = fail_started_event
        prepared = harness.prepare_background_command(
            self.session,
            {
                "command": "python3 -c \"import time; time.sleep(30)\"",
                "cwd": str(self.root),
                "timeoutSeconds": 60,
            },
        )
        self.assertTrue(prepared.unrestricted)

        with self.assertRaisesRegex(AgentBackgroundJobError, "event publisher unavailable"):
            self.service.start(str(self.session["id"]), prepared)

        self.assertIsNotNone(harness.launched)
        self.assertIsNotNone(harness.launched.process.poll())
        self.assertIsNotNone(harness.launched.temporary_path)
        self.assertFalse(harness.launched.temporary_path.exists())
        self.assertEqual(self.service._live, {})
        failed = self.service.list(str(self.session["id"]))["items"][0]
        self.assertEqual(failed["status"], "failed")
        self.assertIn("event publisher unavailable", failed["error"])

    def test_monitor_failure_reaps_process_before_terminalizing(self) -> None:
        self._use_full_trust_process_session()
        class CapturingHarness(WorkspaceHarness):
            launched = None

            def spawn_background(self, prepared, **kwargs):
                self.launched = super().spawn_background(prepared, **kwargs)
                return self.launched

        class FailingMonitorService(AgentBackgroundJobService):
            def _persist_live_progress(self, _job_id, _live, *, force=False):
                del force
                raise RuntimeError("progress persistence unavailable")

        harness = CapturingHarness()
        service = FailingMonitorService(
            self.db_path,
            events=lambda *_args, **_kwargs: None,
            workspace_harness=harness,
        )
        prepared = harness.prepare_background_command(
            self.session,
            {
                "command": (
                    "python3 -c \"import time; "
                    "print('monitor-failure', flush=True); time.sleep(30)\""
                ),
                "cwd": str(self.root),
                "timeoutSeconds": 60,
            },
        )
        self.assertTrue(prepared.unrestricted)
        try:
            receipt = service.start(str(self.session["id"]), prepared)
            job_id = str(receipt["job"]["jobId"])
            deadline = time.monotonic() + 5
            job = receipt["job"]
            while time.monotonic() < deadline:
                job = service.status(str(self.session["id"]), job_id)["job"]
                with service._lock:
                    live = job_id in service._live
                if job["status"] == "failed" and not live:
                    break
                time.sleep(0.02)

            self.assertEqual(job["status"], "failed")
            self.assertIn("progress persistence unavailable", job["error"])
            self.assertIsNotNone(harness.launched.process.poll())
            self.assertIsNotNone(harness.launched.temporary_path)
            self.assertFalse(harness.launched.temporary_path.exists())
        finally:
            service.close()

    def test_log_trim_metadata_and_file_read_are_one_cursor_snapshot(self) -> None:
        job_id = "bg_" + "a" * 32
        log_path = self.service._log_path(job_id)
        log_path.write_bytes(b"A" * 40)
        with sqlite_connection(self.db_path, foreign_keys=True) as conn:
            conn.execute(
                """
                INSERT INTO agent_background_jobs(
                    job_id, session_id, label, status, command, command_sha256,
                    cwd, network_allowed, max_run_seconds, log_path,
                    output_bytes, created_at_ms, updated_at_ms
                ) VALUES (?, ?, 'trim', 'running', 'echo trim', ?, ?, 0, 60, ?,
                          40, 1, 1)
                """,
                (
                    job_id,
                    str(self.session["id"]),
                    "a" * 64,
                    str(self.root),
                    str(log_path),
                ),
            )
        replace_started = threading.Event()
        allow_replace = threading.Event()
        live = _LiveJob(
            launched=object(),
            log_path=_BlockingReplacePath(log_path, replace_started, allow_replace),
            max_run_seconds=60,
            output_bytes=40,
        )
        with self.service._lock:
            self.service._live[job_id] = live
        reader_result: list[dict[str, object]] = []
        reader_started = threading.Event()

        def append() -> None:
            self.service._append_redacted_text(live, "BBBB")

        def read_snapshot() -> None:
            reader_started.set()
            reader_result.append(
                self.service.logs(
                    str(self.session["id"]),
                    job_id,
                    cursor=0,
                    limit_bytes=16,
                )
            )

        try:
            with (
                patch.object(background_job_module, "_MAX_LOG_BYTES", 16),
                patch.object(background_job_module, "_LOG_TRIM_TRIGGER_BYTES", 40),
            ):
                writer = threading.Thread(target=append)
                writer.start()
                self.assertTrue(replace_started.wait(timeout=2))
                reader = threading.Thread(target=read_snapshot)
                reader.start()
                self.assertTrue(reader_started.wait(timeout=2))
                time.sleep(0.02)
                allow_replace.set()
                writer.join(timeout=2)
                reader.join(timeout=2)

            self.assertFalse(writer.is_alive())
            self.assertFalse(reader.is_alive())
            snapshot = reader_result[0]
            self.assertEqual(snapshot["logStartCursor"], 28)
            self.assertEqual(snapshot["cursor"], 28)
            self.assertEqual(snapshot["nextCursor"], 44)
            self.assertEqual(snapshot["text"], "A" * 12 + "BBBB")
        finally:
            allow_replace.set()
            with self.service._lock:
                self.service._live.pop(job_id, None)

    def test_concurrent_admission_reserves_at_most_eight_jobs(self) -> None:
        class BlockingSpawnHarness(WorkspaceHarness):
            def __init__(self) -> None:
                super().__init__()
                self.lock = threading.Lock()
                self.release = threading.Event()
                self.eight_reserved = threading.Event()
                self.spawned = 0

            def spawn_background(self, _prepared, **_kwargs):
                with self.lock:
                    self.spawned += 1
                    if self.spawned == 8:
                        self.eight_reserved.set()
                if not self.release.wait(timeout=5):
                    raise TimeoutError("capacity test release timed out")
                raise RuntimeError("capacity test launch stopped")

        harness = BlockingSpawnHarness()
        service = AgentBackgroundJobService(
            self.db_path,
            events=lambda *_args, **_kwargs: None,
            workspace_harness=harness,
        )
        prepared = self.harness.prepare_background_command(
            self.session,
            {
                "command": "echo capacity",
                "cwd": str(self.root),
                "timeoutSeconds": 60,
            },
        )
        errors: list[BaseException] = []
        errors_lock = threading.Lock()
        eight_rejected = threading.Event()

        def start() -> None:
            try:
                service.start(str(self.session["id"]), prepared)
            except BaseException as exc:
                with errors_lock:
                    errors.append(exc)
                    if len(errors) == 8:
                        eight_rejected.set()

        threads = [threading.Thread(target=start) for _ in range(16)]
        try:
            for thread in threads:
                thread.start()
            self.assertTrue(harness.eight_reserved.wait(timeout=5))
            self.assertTrue(eight_rejected.wait(timeout=5))
            active = service.list(str(self.session["id"]), limit=100)
            self.assertEqual(active["activeCount"], 8)
            self.assertEqual(harness.spawned, 8)
            self.assertTrue(
                all(
                    "at most 8 active" in str(exc)
                    for exc in errors
                )
            )
        finally:
            harness.release.set()
            for thread in threads:
                thread.join(timeout=5)
            service.close()

    def _wait_for_terminal(self, job_id: str) -> dict[str, object]:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            job = self.service.status(str(self.session["id"]), job_id)["job"]
            if job["status"] not in {"queued", "running", "cancelling"}:
                return job
            time.sleep(0.05)
        self.fail(f"background job {job_id} did not finish")

    def _wait_for_log(self, job_id: str, expected: str) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            text = self.service.logs(str(self.session["id"]), job_id)["text"]
            if expected in text:
                return
            time.sleep(0.05)
        self.fail(f"background job {job_id} did not emit {expected!r}")

    def _record_event(
        self,
        _session_id: str,
        event_type: str,
        payload: dict[str, object],
        **_kwargs: object,
    ) -> None:
        self.events.append((event_type, payload))


class AgentBackgroundJobHttpTests(unittest.TestCase):
    def test_management_routes_list_logs_and_cancel_the_owned_process(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-background-http-") as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            service = DebugImeService(
                DebugServerConfig(
                    db_path=Path(directory) / "rag-ime.sqlite",
                    seed_if_empty=False,
                    knowledge_client=object(),
                    rime_user_dir=Path(directory) / "Rime",
                    rime_lexicon_backup_root=Path(directory) / "LexiconBackups",
                )
            )
            session = service.agent.create_session(
                {
                    "title": "后台任务 HTTP",
                    "mode": "coordinator",
                    "toolProfileVersion": DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
                    "executionMode": FULL_TRUST_EXECUTION_MODE,
                    "workspaceRoots": [str(root)],
                }
            )["session"]
            prepared = service.agent.background_jobs.workspace_harness.prepare_background_command(
                session,
                {
                    "command": "python3 -c \"import time; print('http-ready', flush=True); time.sleep(30)\"",
                    "cwd": str(root),
                    "timeoutSeconds": 60,
                },
            )
            self.assertTrue(prepared.unrestricted)
            started = service.agent.background_jobs.start(
                str(session["id"]),
                prepared,
                label="HTTP 后台任务",
            )
            job_id = str(started["job"]["jobId"])

            class Handler(DebugRequestHandler):
                pass

            Handler.service = service
            Handler.static_dir = Path("debug")
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            session_path = quote(str(session["id"]), safe="")
            job_path = quote(job_id, safe="")
            base = (
                f"http://127.0.0.1:{server.server_port}/api/agent/sessions/"
                f"{session_path}/background-jobs"
            )
            try:
                deadline = time.monotonic() + 5
                log_payload: dict[str, object] = {}
                while time.monotonic() < deadline:
                    with urlopen(f"{base}/{job_path}/logs?cursor=0", timeout=5) as response:
                        log_payload = json.load(response)
                    if "http-ready" in str(log_payload.get("text") or ""):
                        break
                    time.sleep(0.05)
                with urlopen(base, timeout=5) as response:
                    listed = json.load(response)
                with urlopen(f"{base}/{job_path}", timeout=5) as response:
                    fetched = json.load(response)
                cancel_request = Request(
                    f"{base}/{job_path}/cancel",
                    data=json.dumps({"reason": "http_test"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(cancel_request, timeout=5) as response:
                    cancelled = json.load(response)
                room_started = service.agent.background_jobs.start(
                    str(session["id"]),
                    prepared,
                    label="Room HTTP 后台任务",
                    causal_metadata={
                        "turnId": "room-root:http",
                        "roomBound": True,
                    },
                )
                room_job_id = str(room_started["job"]["jobId"])
                room_job_path = quote(room_job_id, safe="")
                room_cancel_request = Request(
                    f"{base}/{room_job_path}/cancel",
                    data=json.dumps({"reason": "ordinary_http"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(HTTPError) as raised:
                    urlopen(room_cancel_request, timeout=5)
                with raised.exception as response:
                    rejected = json.load(response)
                wrong_room_cancel_request = Request(
                    f"{base}/{room_job_path}/cancel",
                    data=json.dumps(
                        {"reason": "room_http_owner", "roomTurnId": "room-root:wrong"}
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(HTTPError) as wrong_room_raised:
                    urlopen(wrong_room_cancel_request, timeout=5)
                with wrong_room_raised.exception as response:
                    wrong_room_rejected = json.load(response)
                owned_room_cancel_request = Request(
                    f"{base}/{room_job_path}/cancel",
                    data=json.dumps(
                        {"reason": "room_http_owner", "roomTurnId": "room-root:http"}
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(owned_room_cancel_request, timeout=5) as response:
                    room_cancelled = json.load(response)

                self.assertEqual(listed["items"][0]["jobId"], job_id)
                self.assertEqual(fetched["job"]["jobId"], job_id)
                self.assertIn("http-ready", log_payload["text"])
                self.assertTrue(cancelled["ok"])
                self.assertEqual(cancelled["job"]["status"], "cancelled")
                self.assertIn("Room/Root owner", json.dumps(rejected))
                self.assertIn("another Room root", json.dumps(wrong_room_rejected))
                self.assertTrue(room_cancelled["ok"])
                self.assertEqual(room_cancelled["job"]["status"], "cancelled")
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()
                service.close()


if __name__ == "__main__":
    unittest.main()
