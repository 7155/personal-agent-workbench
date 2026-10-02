from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from unittest.mock import PropertyMock, patch
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from rag_ime.adapter import InputMethodAdapter, SuggestionRequest
from rag_ime.core_client import FixtureCoreClient
import rag_ime.debug_server as debug_server_module
from rag_ime.debug_server import DebugImeService, DebugRequestHandler, DebugServerConfig
from rag_ime.local_sqlite_core import LocalSqliteCoreClient
from rag_ime.memory_generator import GeneratedMemoryItem, GeneratedMemoryReport
from rag_ime.memory_evidence_admission import transition_evidence_admission
from rag_ime.memory_models import ImeQueryContext
from rag_ime.models import InputEvent, InputSuggestion, MemoryAction, ModelPrediction
from rag_ime.predictor import OllamaPredictionConfig, OllamaPredictionProvider
from rag_ime.settings_store import ManagementSettingsStore
from rag_ime.text_utils import compact_whitespace


def _capture_v2(
    text: str,
    *,
    capture_id: str = "capture:debug:1",
    boundary_kind: str = "host_return",
    boundary_confidence: str = "strong",
    native_composition_before: bool = False,
    rime_handled: bool = False,
    host_forwarded: bool = True,
    modified_return: bool = False,
    final_committed: bool = True,
) -> dict[str, object]:
    return {
        "schemaVersion": "rag-ime.input-capture.v2",
        "captureId": capture_id,
        "transactionId": "transaction:debug",
        "sequence": 1,
        "channel": "input_method",
        "boundaryKind": boundary_kind,
        "boundaryConfidence": boundary_confidence,
        "nativeCompositionBefore": native_composition_before,
        "rimeHandled": rime_handled,
        "hostForwarded": host_forwarded,
        "modifiedReturn": modified_return,
        "finalCommitted": final_committed,
        "controllerEpoch": 7,
        "focusEpoch": 9,
        "appBundleId": "com.apple.TextEdit",
        "fieldIdentitySha256": hashlib.sha256(b"debug-field").hexdigest(),
        "privacyRevision": "foreground-privacy.v1",
        "occurredStartMs": 100,
        "occurredEndMs": 150,
        "contentSha256": hashlib.sha256(
            compact_whitespace(text).encode("utf-8")
        ).hexdigest(),
        "captureSource": "text_input_client",
        "fallbackReason": "",
        "fieldContextChars": len(text),
        "imeBufferChars": len(text),
        "selectionRule": "final_committed_segment",
    }


class FakePredictionProvider:
    def __init__(self) -> None:
        self.last_current_input = ""
        self.last_recent_context = ""
        self.calls = 0

    def predict(self, *, current_input: str, recent_context: str = "", max_candidates: int = 5):
        self.calls += 1
        self.last_current_input = current_input
        self.last_recent_context = recent_context
        return [
            ModelPrediction(
                text=f"{current_input}候选",
                rank=1,
                provider_name="fake-model",
                latency_ms=7,
                confidence=0.9,
            )
        ][:max_candidates]


class PrefixPredictionProvider(FakePredictionProvider):
    def predict(self, *, current_input: str, recent_context: str = "", max_candidates: int = 5):
        self.calls += 1
        self.last_current_input = current_input
        self.last_recent_context = recent_context
        return [
            ModelPrediction(
                text="设计输入法状态机",
                rank=1,
                provider_name="qwen-mlx",
                latency_ms=9,
                confidence=0.84,
                metadata={"initials": "sjsrfztj"},
            ),
            ModelPrediction(
                text="把这个项目整理成面试亮点",
                rank=2,
                provider_name="qwen-mlx",
                latency_ms=9,
                confidence=0.9,
                metadata={"initials": "bzgxmzlmsld"},
            ),
        ][:max_candidates]


class MarsEmbeddingProvider:
    fingerprint = "test-mars:v1"

    def embed(self, text: str) -> list[float]:
        if "火星任务" in text or "赤色星球" in text:
            return [1.0, 0.0]
        return [0.0, 1.0]


class EmptyThenDraftMemoryOrganizer:
    provider_name = "fixture"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def curate_owner_memory(self, *, bundle, **_kwargs):
        inputs = [dict(item) for item in bundle.get("inputs") or []]
        self.calls.append([str(item.get("text") or "") for item in inputs])
        first_batch = len(self.calls) == 1
        decisions = [
            {
                "sourceRef": item["sourceRef"],
                "disposition": "not_for_memory" if first_batch else "remember",
                "reasonCode": "fact_free_question" if first_batch else "durable_requirement",
                "confidence": 0.99,
            }
            for item in inputs
        ]
        atoms = []
        if not first_batch:
            atoms = [
                {
                    "canonicalText": "项目必须把显式整理请求中的全部新增证据扫描完，再报告没有变化。",
                    "summary": "显式整理请求必须扫描完新增证据",
                    "kind": "durable_preference",
                    "claimKey": "user:memory-curation:explicit-request-drain",
                    "sourceEventIds": list(inputs[0]["sourceEventIds"]),
                    "confidence": 0.99,
                    "qualityScore": 0.99,
                    "directCandidateAllowed": False,
                }
            ]
        return {
            "schemaVersion": "rag-ime.owner-memory-curation.v1",
            "provider": self.provider_name,
            "model": "fixture-memory",
            "sourceDecisions": decisions,
            "memoryAtoms": atoms,
            "topicBooks": [],
            "supersedes": [],
        }


class BlockingPredictionProvider:
    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()
        self.calls = 0

    def predict(self, *, current_input: str, recent_context: str = "", max_candidates: int = 5):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("blocking test predictor was not released")
        return [
            ModelPrediction(
                text=f"{current_input}阻塞候选",
                rank=1,
                provider_name="blocking-model",
                latency_ms=25,
                confidence=0.9,
            )
        ][:max_candidates]


class FakeDeepSeekV4MemoryGenerator:
    calls: list[dict[str, object]] = []

    @classmethod
    def from_env_path(cls, env_path=None):
        return cls()

    def generate(self, *, text: str, recent_context: str = "", project: str = "wisdom-weasel-rag-ime", max_items: int = 3):
        self.__class__.calls.append(
            {
                "text": text,
                "recentContext": recent_context,
                "project": project,
                "maxItems": max_items,
            }
        )
        return GeneratedMemoryReport(
            provider="deepseek-v4",
            model="deepseek-v4-flash",
            elapsed_ms=12,
            items=(
                GeneratedMemoryItem(
                    text="用户希望输入历史先经 API 蒸馏后再进入长期记忆",
                    tags=("project_requirement", "memory"),
                    importance=0.9,
                    reason="稳定项目要求",
                ),
            ),
            metadata={"wireApi": "responses"},
        )


class MockOllamaStreamingHandler(BaseHTTPRequestHandler):
    captured_payloads: list[dict[str, object]] = []

    def do_POST(self) -> None:  # noqa: N802 - stdlib API
        length = int(self.headers.get("Content-Length") or "0")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        MockOllamaStreamingHandler.captured_payloads.append(payload)
        chunks = [
            {"message": {"role": "assistant", "content": '["本地记忆"'}},
            {"message": {"role": "assistant", "content": ',"输入法候选"]'}, "done": True},
        ]
        body = b"".join(json.dumps(item, ensure_ascii=False).encode("utf-8") + b"\n" for item in chunks)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def log_message(self, fmt: str, *args: object) -> None:
        return


class VectorAwareFixtureCore(FixtureCoreClient):
    def __init__(self) -> None:
        super().__init__()
        self.vector_revision = 1

    def vector_index_stats(self) -> dict[str, object]:
        return {
            "enabled": True,
            "providerFingerprint": "test-vector:v1",
            "totalVectors": self.vector_revision,
            "activeProviderVectors": self.vector_revision,
            "candidateLimit": 8,
            "weight": 1.0,
        }


class PrefixFixtureCore(FixtureCoreClient):
    def suggest_for_input(self, *, current_input: str, recent_context: str = "", project: str = "", app: str = "", top_k: int = 5):
        return [
            InputSuggestion(
                suggestion_id="prefix:1",
                surface_text="设计一个候选展示方式",
                suggestion_type="rag",
                source_event_id=1,
                evidence_preview="debug prefix evidence",
                confidence=0.95,
                metadata={
                    "insert_text": "设计一个候选展示方式",
                    "source_type": "rag",
                    "initials": "sjygxhzsfs",
                },
            ),
            InputSuggestion(
                suggestion_id="prefix:2",
                surface_text="把本地记忆注入 Agent 首次运行上下文",
                suggestion_type="rag",
                source_event_id=2,
                evidence_preview="does not match sj",
                confidence=0.99,
                metadata={"source_type": "rag", "initials": "bbdjy zr agent scyx sxw"},
            ),
        ][:top_k]


class DebugImeServiceTests(unittest.TestCase):
    def test_tool_gateway_accept_queue_exceeds_the_runtime_host_concurrency_limit(self) -> None:
        self.assertEqual(
            debug_server_module.QuietThreadingHTTPServer.request_queue_size,
            64,
        )

    def test_expected_client_disconnect_does_not_dump_server_traceback(self) -> None:
        server = object.__new__(debug_server_module.QuietThreadingHTTPServer)
        with patch.object(ThreadingHTTPServer, "handle_error") as parent_handler:
            try:
                raise ConnectionResetError("client cancelled stale request")
            except ConnectionResetError:
                server.handle_error(object(), ("127.0.0.1", 12345))
        parent_handler.assert_not_called()

    def setUp(self) -> None:
        self._optimizer_env = {
            "RAG_IME_MEMORY_OPTIMIZER": os.environ.get("RAG_IME_MEMORY_OPTIMIZER"),
            "RAG_IME_MEMORY_OPTIMIZER_TRACE": os.environ.get("RAG_IME_MEMORY_OPTIMIZER_TRACE"),
            "RAG_IME_AI_AFTER_COMMIT_ONLY": os.environ.get("RAG_IME_AI_AFTER_COMMIT_ONLY"),
            "RAG_IME_ENABLE_COMPOSING_MODEL": os.environ.get("RAG_IME_ENABLE_COMPOSING_MODEL"),
            "RAG_IME_ENABLE_PINYIN_CONSTRAINED_MODEL": os.environ.get("RAG_IME_ENABLE_PINYIN_CONSTRAINED_MODEL"),
            "RAG_IME_PINYIN_FUZZY_ENABLED": os.environ.get("RAG_IME_PINYIN_FUZZY_ENABLED"),
            "RAG_IME_PINYIN_FUZZY_PROFILE": os.environ.get("RAG_IME_PINYIN_FUZZY_PROFILE"),
            "RAG_IME_PINYIN_FUZZY_S_SH": os.environ.get("RAG_IME_PINYIN_FUZZY_S_SH"),
            "RAG_IME_PINYIN_FUZZY_N_L": os.environ.get("RAG_IME_PINYIN_FUZZY_N_L"),
        }
        self.addCleanup(self._restore_optimizer_env)
        os.environ["RAG_IME_AI_AFTER_COMMIT_ONLY"] = "0"
        os.environ["RAG_IME_ENABLE_COMPOSING_MODEL"] = "1"
        os.environ["RAG_IME_ENABLE_PINYIN_CONSTRAINED_MODEL"] = "1"
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-debug-test-")
        self.addCleanup(self.tmp.cleanup)
        self.service = self._new_service(
            DebugServerConfig(
                db_path=Path(self.tmp.name) / "rag-ime.sqlite",
                static_dir=Path("debug"),
                seed_if_empty=True,
            )
        )

    def _new_service(self, config: DebugServerConfig) -> DebugImeService:
        service = DebugImeService(config)
        # LIFO cleanup closes every owned service before its database directory.
        self.addCleanup(service.close)
        return service

    def _restore_optimizer_env(self) -> None:
        for key, value in self._optimizer_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_health_and_seed_use_local_sqlite(self) -> None:
        health = self.service.health()
        self.assertTrue(health["ok"])
        self.assertGreaterEqual(health["eventCount"], 1)
        self.assertEqual(health["rimeSuggestCache"]["ttlMs"], 400)
        self.assertEqual(health["rimeSuggestCache"]["hits"], 0)
        self.assertTrue(health["suggestionCache"]["enabled"])
        self.assertEqual(health["suggestionCache"]["hits"], 0)
        self.assertFalse(health["vectorStats"]["enabled"])
        self.assertFalse(health["predictor"]["configured"])
        self.assertEqual(health["predictor"]["providerName"], "NullPredictionProvider")
        seeded = self.service.seed()
        self.assertTrue(seeded["ok"])
        self.assertGreaterEqual(seeded["seeded"], 1)

    def test_memory_job_terminal_event_links_every_owner_curation_run(self) -> None:
        emitted: list[dict[str, object]] = []

        def capture(**values: object) -> dict[str, object]:
            emitted.append(dict(values))
            return dict(values)

        with patch.object(
            self.service.agent.observations,
            "emit_memory_event",
            side_effect=capture,
        ):
            self.service._publish_memory_maintenance_event(
                {
                    "jobId": "memory-maintenance:job-links",
                    "runId": "memory-maintenance:job-links",
                    "phase": "completed",
                    "status": "completed",
                    "summary": "Memory maintenance completed",
                    "result": {
                        "ok": True,
                        "results": [
                            {"runId": "owner-run:a"},
                            {"runId": "owner-run:b"},
                            {"runId": "owner-run:a"},
                        ],
                    },
                }
            )

        self.assertEqual(len(emitted), 1)
        owner_refs = [
            ref
            for ref in emitted[0]["refs"]
            if ref["kind"] == "owner_memory_run"
        ]
        self.assertEqual(
            owner_refs,
            [
                {"kind": "owner_memory_run", "id": "owner-run:a", "label": "Owner memory run"},
                {"kind": "owner_memory_run", "id": "owner-run:b", "label": "Owner memory run"},
            ],
        )
    def test_only_agent_gateway_owns_pi_runtime_execution(self) -> None:
        sidecar_db = Path(self.tmp.name) / "passive-sidecar.sqlite"
        gateway_db = Path(self.tmp.name) / "active-gateway.sqlite"
        for db_path in (sidecar_db, gateway_db):
            ManagementSettingsStore(db_path).update_settings(
                {"agent": {"pi": {"enabled": True}}},
                updated_by="test",
            )
        with patch.dict(
            os.environ,
            {
                "RAG_IME_AGENT_GATEWAY_ENABLED": "1",
            },
            clear=False,
        ):
            sidecar = self._new_service(
                DebugServerConfig(
                    db_path=sidecar_db,
                    seed_if_empty=False,
                    server_name="sidecar server",
                )
            )
            gateway = self._new_service(
                DebugServerConfig(
                    db_path=gateway_db,
                    seed_if_empty=False,
                    server_name="agent gateway",
                )
            )
        try:
            self.assertFalse(hasattr(sidecar.agent, "room_kernel_worker_loop"))
            self.assertFalse(hasattr(sidecar.agent, "room_kernel_commands"))
            self.assertFalse(sidecar.agent.runtime.runtime_status()["enabled"])
            self.assertFalse(sidecar.agent.runtime_factory.execution_owner)
            self.assertFalse(hasattr(gateway.agent, "room_kernel_worker_loop"))
            self.assertFalse(hasattr(gateway.agent, "room_kernel_commands"))
            self.assertTrue(gateway.agent.runtime.runtime_status()["enabled"])
            self.assertTrue(gateway.agent.runtime_factory.execution_owner)
        finally:
            sidecar.close()
            gateway.close()

    def test_passive_sidecar_rejects_room_runtime_writes_before_agent_mutation(
        self,
    ) -> None:
        db_path = Path(self.tmp.name) / "passive-room-writes.sqlite"
        ManagementSettingsStore(db_path).update_settings(
            {"agent": {"pi": {"enabled": True}}},
            updated_by="test",
        )
        with patch.dict(
            os.environ,
            {"RAG_IME_AGENT_GATEWAY_ENABLED": "1"},
            clear=False,
        ):
            sidecar = self._new_service(
                DebugServerConfig(
                    db_path=db_path,
                    seed_if_empty=False,
                    server_name="sidecar server",
                )
            )

        class Handler(DebugRequestHandler):
            pass

        Handler.service = sidecar
        Handler.static_dir = Path("debug")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def post(path: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
            request = Request(
                f"http://127.0.0.1:{server.server_port}{path}",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                try:
                    return exc.code, json.loads(exc.read().decode("utf-8"))
                finally:
                    exc.close()

        try:
            with (
                patch.object(sidecar.agent, "post_room_message") as room_message,
                patch.object(sidecar.agent, "steer_room_participant") as participant_steer,
            ):
                responses = (
                    post(
                        "/api/agent/rooms/room:test/messages",
                        {"message": "must not reach AgentService"},
                    ),
                    post(
                        "/api/agent/rooms/room:test/steer",
                        {
                            "rootId": "room-turn:test",
                            "clientActionId": "steer:test",
                        },
                    ),
                )
                room_message.assert_not_called()
                participant_steer.assert_not_called()
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()
            sidecar.close()

        for status, payload in responses:
            self.assertEqual(status, HTTPStatus.CONFLICT)
            self.assertEqual(payload["code"], "AGENT_GATEWAY_REQUIRED")
            self.assertFalse(payload["ok"])

    def test_passive_sidecar_proxies_jev_reads_and_commands_to_gateway_owner(self) -> None:
        gateway_requests: list[tuple[str, str, bytes]] = []
        gateway_auth_headers: list[tuple[str, str]] = []

        class GatewayHandler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib API
                gateway_requests.append(("GET", self.path, b""))
                gateway_auth_headers.append(
                    (
                        self.headers.get("X-RAG-IME-Admin-Token", ""),
                        self.headers.get("Origin", ""),
                    )
                )
                body = json.dumps(
                    {
                        "schemaVersion": "owner-jev.v1",
                        "graphId": "graph:owner",
                        "executionStatus": "running",
                    }
                ).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802 - stdlib API
                length = int(self.headers.get("Content-Length") or "0")
                body = self.rfile.read(length)
                gateway_requests.append(("POST", self.path, body))
                gateway_auth_headers.append(
                    (
                        self.headers.get("X-RAG-IME-Admin-Token", ""),
                        self.headers.get("Origin", ""),
                    )
                )
                response = {"schemaVersion": "owner-jev-command.v1", "ok": True}
                encoded = json.dumps(response).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *args):
                pass

        gateway_server = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
        gateway_thread = Thread(target=gateway_server.serve_forever, daemon=True)
        gateway_thread.start()
        self.addCleanup(gateway_server.server_close)
        self.addCleanup(gateway_thread.join, 2)
        self.addCleanup(gateway_server.shutdown)

        db_path = Path(self.tmp.name) / "passive-jev-proxy.sqlite"
        with patch.dict(
            os.environ,
            {
                "RAG_IME_AGENT_GATEWAY_ENABLED": "1",
                "RAG_IME_AGENT_GATEWAY_URL": (
                    f"http://127.0.0.1:{gateway_server.server_port}"
                ),
            },
            clear=False,
        ):
            sidecar = self._new_service(
                DebugServerConfig(
                    db_path=db_path,
                    seed_if_empty=False,
                    server_name="sidecar server",
                )
            )
        self.addCleanup(sidecar.close)
        sidecar.settings_update(
            {
                "managementSecurity.requireToken": True,
                "managementSecurity.token": "proxy-token",
            }
        )

        class Handler(DebugRequestHandler):
            pass

        Handler.service = sidecar
        Handler.static_dir = Path("debug")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)

        room = quote("room:test", safe="")
        graph = quote("graph:owner", safe="")
        base = f"http://127.0.0.1:{server.server_port}/api/agent/rooms/{room}/jev"
        with patch.object(sidecar.agent, "jev_workspace") as local_read, patch.object(
            sidecar.agent, "jev_command"
        ) as local_command:
            with urlopen(f"{base}?graphId={graph}", timeout=5) as response:
                read_payload = json.loads(response.read().decode("utf-8"))
            command_body = {"action": "retry_route", "graphId": "graph:owner"}
            command_request = Request(
                base,
                data=json.dumps(command_body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Origin": f"http://127.0.0.1:{server.server_port}",
                    "X-RAG-IME-Admin-Token": "proxy-token",
                },
                method="POST",
            )
            with urlopen(command_request, timeout=5) as response:
                command_payload = json.loads(response.read().decode("utf-8"))

        local_read.assert_not_called()
        local_command.assert_not_called()
        self.assertEqual(read_payload["executionStatus"], "running")
        self.assertTrue(command_payload["ok"])
        self.assertEqual(
            gateway_requests[0][0:2],
            ("GET", f"/api/agent/rooms/{room}/jev?graphId={graph}"),
        )
        self.assertEqual(
            gateway_requests[1][0:2],
            ("POST", f"/api/agent/rooms/{room}/jev"),
        )
        self.assertEqual(json.loads(gateway_requests[1][2]), command_body)
        self.assertEqual(gateway_auth_headers[0], ("", ""))
        self.assertEqual(gateway_auth_headers[1], ("proxy-token", ""))

    def test_missing_role_book_returns_json_not_found(self) -> None:
        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = (
                f"http://127.0.0.1:{server.server_port}/api/agent/role-book"
                "?roleId=missing-role&roleVersion=1"
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(url, timeout=5)
            error = raised.exception
            try:
                self.assertEqual(error.code, HTTPStatus.NOT_FOUND)
                payload = json.loads(error.read().decode("utf-8"))
            finally:
                error.close()
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(payload, {"ok": False, "error": "role book not found"})

    def test_explicit_memory_prepare_drains_empty_batches_until_one_draft(self) -> None:
        db_path = Path(self.tmp.name) / "manual-curation-drain.sqlite"
        service = self._new_service(
            DebugServerConfig(db_path=db_path, seed_if_empty=False)
        )
        organizer = EmptyThenDraftMemoryOrganizer()
        try:
            session = service.agent.sessions.create(title="memory-drain", created_at_ms=1)
            sources = service.agent.memory_sources
            first_source = sources.checkpoint_user_message(
                session_id=str(session["id"]),
                pi_entry_id="entry:first-empty",
                turn_id="turn:first-empty",
                text=(
                    "第一段只是会话背景。第二段只是会话背景。第三段只是会话背景。"
                    "第四段只是会话背景。第五段只是会话背景。第六段只是会话背景。"
                ),
                created_at_ms=100,
            )
            second_source = sources.checkpoint_user_message(
                session_id=str(session["id"]),
                pi_entry_id="entry:durable",
                turn_id="turn:durable",
                text="项目必须把显式整理请求中的全部新增证据扫描完，再报告没有变化。",
                created_at_ms=200,
            )
            sources.capture_hint(
                session_id=str(session["id"]),
                source_id=str(first_source["source"]["sourceId"]),
                kind="decision",
                claim="第一段只是这次整理的临时会话背景。",
                scope="user",
                basis="explicit_user_statement",
                future_use="用于验证整理器能够拒绝不应形成长期记忆的临时背景。",
                created_at_ms=101,
            )
            sources.capture_hint(
                session_id=str(session["id"]),
                source_id=str(second_source["source"]["sourceId"]),
                kind="preference",
                claim="用户要求显式整理扫描完全部新增证据后再报告没有变化。",
                scope="user",
                basis="explicit_user_statement",
                future_use="这会改变未来显式记忆整理的默认扫描行为。",
                created_at_ms=201,
            )

            with patch.object(
                debug_server_module,
                "build_governed_memory_model_executor",
                return_value=object(),
            ), patch.object(
                debug_server_module,
                "ManagedPiMemoryOrganizer",
                return_value=organizer,
            ), patch.object(
                debug_server_module.MemoryMaintenanceSettings,
                "automatic_organization_auto_apply",
                new_callable=PropertyMock,
                return_value=False,
            ):
                result = service.agent_memory_maintenance_prepare(
                    {
                        "project": "wisdom-weasel-rag-ime",
                        "ownerKind": "user",
                        "ownerId": "default",
                        "instruction": "只生成一份可审阅草案。",
                        "maxSources": 1,
                    }
                )

            self.assertTrue(result["ok"], result)
            self.assertTrue(result["storedDraft"], result)
            self.assertEqual(result["source"]["batchCount"], 2)
            self.assertEqual(result["source"]["pendingSourceCount"], 0)
            self.assertEqual(len(organizer.calls), 2)
            self.assertEqual(len(organizer.calls[0]), 1)
            self.assertEqual(len(organizer.calls[1]), 1)
            self.assertGreater(len(result["storedRun"]["diffs"]), 0)
        finally:
            service.close()

    def test_default_local_core_uses_configured_embedding_provider(self) -> None:
        db_path = Path(self.tmp.name) / "configured-embedding.sqlite"
        with patch.dict(
            os.environ,
            {
                "RAG_IME_EMBEDDING_PROVIDER": "openai-compatible",
                "RAG_IME_EMBEDDING_BASE_URL": "http://embedding.test:8000",
                "RAG_IME_EMBEDDING_MODEL": "Qwen/Qwen3-Embedding-0.6B",
                "RAG_IME_EMBEDDING_DIMENSIONS": "1024",
            },
            clear=False,
        ):
            service = self._new_service(DebugServerConfig(db_path=db_path, seed_if_empty=False))
        try:
            self.assertIsInstance(service.core, LocalSqliteCoreClient)
            assert isinstance(service.core, LocalSqliteCoreClient)
            self.assertIn("Qwen/Qwen3-Embedding-0.6B", service.core.embedding_provider.fingerprint)
        finally:
            service.management.close()

    def test_startup_can_backfill_vector_index_when_provider_enabled(self) -> None:
        db_path = Path(self.tmp.name) / "startup-vector.sqlite"
        plain_core = LocalSqliteCoreClient(db_path)
        event_ref, receipt = plain_core.record_event_with_capture_receipt(
            InputEvent(
                event_id=None,
                created_at_ms=int(time.time() * 1_000),
                source="squirrel_input_segment",
                committed_text="赤色星球探索计划",
                recent_context="航天项目背景",
                privacy_disposition="allowed",
                app="com.apple.TextEdit",
                tags=("curated",),
                capture_metadata=_capture_v2(
                    "赤色星球探索计划",
                    capture_id="capture:debug:retrieval-vector",
                ),
            )
        )
        timestamp = int(time.time() * 1_000)
        with plain_core._connect() as conn:
            event_id = int(event_ref.split(":", 1)[1])
            transition_evidence_admission(
                conn,
                str(receipt["evidenceId"]),
                new_state="admitted",
                reason_code="luna_personal_memory_confirmed",
                actor_kind="luna",
                created_at_ms=timestamp + 1,
            )
            conn.execute(
                """
                INSERT INTO memory_atoms(
                    id, kind, text, canonical_text, source_event_ids_json,
                    source_memory_ids_json, scope_project, confidence,
                    quality_score, privacy_level, status, created_at_ms,
                    updated_at_ms
                ) VALUES ('atom:mars-plan', 'fact', ?, ?, ?, '[]', '', 0.9,
                          0.9, 'local', 'active', ?, ?)
                """,
                (
                    "赤色星球探索计划",
                    "赤色星球探索计划",
                    json.dumps([event_id]),
                    timestamp,
                    timestamp,
                ),
            )

        vector_core = LocalSqliteCoreClient(db_path, embedding_provider=MarsEmbeddingProvider(), vector_weight=2.0)
        service = self._new_service(
            DebugServerConfig(
                db_path=db_path,
                core=vector_core,
                seed_if_empty=False,
                vector_auto_rebuild_limit=10,
            )
        )

        health = service.health()
        self.assertTrue(health["vectorStats"]["enabled"])
        self.assertEqual(health["vectorStats"]["activeProviderVectors"], 1)
        self.assertEqual(health["vectorAutoRebuild"]["lastRun"]["indexed"], 1)
        payload = service.suggest({"currentInput": "火星任务", "topK": 1})
        self.assertEqual(payload["suggestions"][0]["surfaceText"], "赤色星球探索计划")

    def test_startup_backfills_missing_retrieval_vectors_even_with_event_vectors(self) -> None:
        db_path = Path(self.tmp.name) / "startup-retrieval-vector.sqlite"
        plain_core = LocalSqliteCoreClient(db_path)
        event_ref, receipt = plain_core.record_event_with_capture_receipt(
            InputEvent(
                event_id=None,
                created_at_ms=int(time.time() * 1_000),
                source="squirrel_input_segment",
                committed_text="赤色星球探索计划",
                recent_context="航天项目背景",
                privacy_disposition="allowed",
                app="com.apple.TextEdit",
                tags=("curated",),
                capture_metadata=_capture_v2(
                    "赤色星球探索计划",
                    capture_id="capture:debug:retrieval-doc-vector",
                ),
            )
        )
        timestamp = int(time.time() * 1_000)
        with plain_core._connect() as conn:
            event_id = int(event_ref.split(":", 1)[1])
            transition_evidence_admission(
                conn,
                str(receipt["evidenceId"]),
                new_state="admitted",
                reason_code="luna_personal_memory_confirmed",
                actor_kind="luna",
                created_at_ms=timestamp + 1,
            )
            conn.execute(
                """
                INSERT INTO memory_atoms(
                    id, kind, text, canonical_text, source_event_ids_json,
                    source_memory_ids_json, scope_project, confidence,
                    quality_score, privacy_level, status, created_at_ms,
                    updated_at_ms
                ) VALUES ('atom:mars-plan', 'fact', ?, ?, ?, '[]', '', 0.9,
                          0.9, 'local', 'active', ?, ?)
                """,
                (
                    "赤色星球探索计划",
                    "赤色星球探索计划",
                    json.dumps([event_id]),
                    timestamp,
                    timestamp,
                ),
            )
        vector_core = LocalSqliteCoreClient(
            db_path,
            embedding_provider=MarsEmbeddingProvider(),
            vector_weight=2.0,
        )
        vector_core.rebuild_vector_index(limit=10)
        with vector_core._connect() as conn:
            conn.execute("DELETE FROM memory_retrieval_doc_vectors")
        before = vector_core.vector_index_stats()
        self.assertEqual(before["activeProviderVectors"], 1)
        self.assertEqual(before["activeProviderRetrievalDocVectors"], 0)

        service = self._new_service(
            DebugServerConfig(
                db_path=db_path,
                core=vector_core,
                seed_if_empty=False,
                vector_auto_rebuild_limit=10,
            )
        )
        try:
            health = service.health()
            self.assertEqual(health["vectorAutoRebuild"]["lastRun"]["indexed"], 1)
            self.assertGreater(
                health["vectorStats"]["activeProviderRetrievalDocVectors"],
                0,
            )
        finally:
            service.management.close()

    def test_rebuild_vector_index_endpoint_backfills_existing_events(self) -> None:
        db_path = Path(self.tmp.name) / "manual-vector.sqlite"
        plain_core = LocalSqliteCoreClient(db_path)
        InputMethodAdapter(plain_core).commit_text(
            "赤色星球探索计划",
            recent_context="航天项目背景",
            tags=("curated",),
            privacy_disposition="allowed",
        )

        vector_core = LocalSqliteCoreClient(db_path, embedding_provider=MarsEmbeddingProvider(), vector_weight=2.0)
        service = self._new_service(
            DebugServerConfig(
                db_path=db_path,
                core=vector_core,
                seed_if_empty=False,
            )
        )
        self.assertEqual(service.health()["vectorStats"]["activeProviderVectors"], 0)

        report = service.rebuild_vector_index({"limit": 10})

        self.assertTrue(report["ok"])
        self.assertEqual(report["indexed"], 1)
        self.assertEqual(report["vectorStats"]["activeProviderVectors"], 1)

    def test_memory_history_lists_recent_rows_and_generated_stats(self) -> None:
        event_id = self.service.adapter.commit_text(
            "API 整理后的输入历史记忆",
            recent_context="历史治理",
            source="api_memory_generator",
            provider_name="deepseek-v4:fake",
            tags=("generated-memory", "deepseek-v4"),
            privacy_disposition="allowed",
        )

        payload = self.service.memory_history({"query": "输入历史", "generatedOnly": True, "limit": 10})

        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(payload["totals"]["generated"], 1)
        self.assertEqual(payload["items"][0]["eventId"], int(event_id.removeprefix("event:")))
        self.assertIn("generated-memory", payload["items"][0]["tags"])

    def test_memory_optimizer_trace_and_candidate_explain_endpoints(self) -> None:
        os.environ["RAG_IME_MEMORY_OPTIMIZER"] = "1"
        os.environ["RAG_IME_MEMORY_OPTIMIZER_TRACE"] = "1"
        self.service.adapter.commit_text(
            "连续预测",
            recent_context="RAG 输入法需要更好的候选",
            project=self.service.config.project,
            tags=("phrase-memory",),
            privacy_disposition="allowed",
        )
        response = self.service.rime_suggest(
            {
                "sessionId": "debug-trace",
                "requestSeq": 91,
                "privacyDisposition": "allowed",
                "committedContext": "我想继续写连续",
                "maxVisibleCandidates": 4,
                "maxSideCandidates": 2,
                "forceSideCandidates": True,
                "rimeContext": {"candidates": [{"label": "1", "text": "连续", "comment": "rime"}]},
            }
        )

        trace_id = response["ragLane"]["memoryOptimizer"]["traceId"]
        trace = self.service.memory_optimizer_trace({"traceId": trace_id})
        explanation = self.service.memory_candidate_explain(
            {
                "candidateId": "phrase:连续预测",
                "contextHash": response["committedContextHash"],
            }
        )

        self.assertTrue(trace["ok"])
        self.assertEqual(trace["traceId"], trace_id)
        self.assertTrue(any(item["id"] == "phrase:连续预测" for item in trace["rawResults"]))
        self.assertTrue(any(item["reason"] == "recent_committed_echo" for item in trace["blocked"]))
        self.assertTrue(explanation["ok"])
        self.assertEqual(explanation["candidateId"], "phrase:连续预测")
        self.assertEqual(explanation["recentTrace"]["traceId"], trace_id)
        self.assertTrue(
            any(item["reason"] == "recent_committed_echo" for item in explanation["recentTrace"]["blocked"])
        )

    def test_memory_governance_and_cleanup_runs_endpoints(self) -> None:
        self.service.core.record_memory_feedback(
            {
                "event": "skipped",
                "candidateId": "phrase:连续预测",
                "candidateText": "连续预测",
                "sourceType": "memory",
                "contextHash": "ctx:debug-admin",
                "timestampMs": 1,
            }
        )
        self.service.core.record_memory_feedback(
            {
                "event": "skipped",
                "candidateId": "phrase:连续预测",
                "candidateText": "连续预测",
                "sourceType": "memory",
                "contextHash": "ctx:debug-admin",
                "timestampMs": 2,
            }
        )
        cleanup = self.service.core.build_memory_cleanup_plan(project=self.service.config.project)

        governance = self.service.memory_governance({"limit": 10})
        cleanup_runs = self.service.memory_cleanup_runs({"limit": 10})

        self.assertTrue(governance["ok"])
        self.assertIn("phrase:连续预测", [item["matchValue"] for item in governance["suppressions"]])
        self.assertTrue(cleanup_runs["ok"])
        self.assertTrue(any(item["runId"] == cleanup["runId"] for item in cleanup_runs["items"]))

    def test_memory_cleanup_review_and_tombstone_endpoints(self) -> None:
        self.service.adapter.commit_text(
            "连续预测",
            recent_context="RAG 输入法需要更好的候选",
            project=self.service.config.project,
            tags=("phrase-memory",),
            privacy_disposition="allowed",
        )
        self.service.adapter.commit_text(
            "连续预测",
            recent_context="RAG 输入法需要更好的候选",
            project=self.service.config.project,
            tags=("phrase-memory",),
            privacy_disposition="allowed",
        )
        cleanup = self.service.core.build_memory_cleanup_plan(project=self.service.config.project)

        review = self.service.memory_cleanup_runs(
            {
                "runId": cleanup["runId"],
                "reviewStatus": "approved",
            }
        )
        before = self.service.core.retrieve_candidates_v2(
            context=ImeQueryContext(current_input="连续", project=self.service.config.project, top_k=5)
        )
        tombstone = self.service.memory_tombstone(
            {
                "targetType": "memory_id",
                "targetValue": "phrase:连续预测",
                "reason": "debug review",
                "metadata": {"source": "debug-test"},
            }
        )
        after = self.service.core.retrieve_candidates_v2(
            context=ImeQueryContext(current_input="连续", project=self.service.config.project, top_k=5)
        )

        self.assertTrue(review["ok"])
        self.assertEqual(review["status"], "reviewed")
        self.assertTrue(all(item["status"] == "approved" for item in review["diffs"]))
        self.assertIn("连续预测", [item["text"] for item in before["candidates"]])
        self.assertTrue(tombstone["ok"])
        self.assertEqual(tombstone["targetValue"], "phrase:连续预测")
        self.assertEqual(tombstone["metadata"]["source"], "debug-test")
        self.assertNotIn("连续预测", [item["text"] for item in after["candidates"]])

    def test_generate_memory_endpoint_uses_deepseek_v4_generator_and_records_rows(self) -> None:
        original = debug_server_module.VcpRebuildMemoryGenerator
        FakeDeepSeekV4MemoryGenerator.calls = []
        debug_server_module.VcpRebuildMemoryGenerator = FakeDeepSeekV4MemoryGenerator
        try:
            payload = self.service.generate_memory(
                {
                    "text": "输入历史和记忆需要 API 整理",
                    "recentContext": "用户希望可视化治理历史数据",
                    "maxItems": 2,
                }
            )
        finally:
            debug_server_module.VcpRebuildMemoryGenerator = original

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["recorded"], 1)
        self.assertEqual(FakeDeepSeekV4MemoryGenerator.calls[0]["maxItems"], 2)
        history = self.service.memory_history({"generatedOnly": True, "query": "API 蒸馏"})
        self.assertEqual(history["items"][0]["text"], "用户希望输入历史先经 API 蒸馏后再进入长期记忆")

    def test_organize_rag_database_endpoint_reports_dry_run(self) -> None:
        report = self.service.organize_rag_database({"dryRun": True, "sampleSize": 2})

        self.assertTrue(report["ok"])
        self.assertTrue(report["dryRun"])
        self.assertIn("matchedNoise", report)

    def test_suggest_returns_native_frontend_payload(self) -> None:
        predictor = FakePredictionProvider()
        self.service.predictor = predictor
        payload = self.service.suggest(
            {
                "currentInput": "SQLite 和 FTS5 第一版",
                "recentContext": "MVP 先 local-first",
                "topK": 3,
            }
        )
        self.assertEqual(payload["schemaVersion"], "rag-ime.suggestions.v1")
        self.assertEqual(payload["currentInput"], "SQLite 和 FTS5 第一版")
        self.assertEqual(payload["modelPredictions"][0]["providerName"], "fake-model")
        self.assertIn("MVP 先 local-first", predictor.last_recent_context)
        self.assertGreaterEqual(len(payload["suggestions"]), 1)
        first = payload["suggestions"][0]
        self.assertIn("surfaceText", first)
        self.assertIn("memoryId", first)
        self.assertIn("evidencePreview", first)

    def test_model_prediction_context_includes_committed_history(self) -> None:
        predictor = FakePredictionProvider()
        self.service.predictor = predictor
        self.service.commit(
            {
                "text": "历史输入会进入模型预测",
                "privacyDisposition": "allowed",
                "recentContext": "用户刚刚写过 RAG 输入法",
                "preedit": "lishi",
                "tags": ["history"],
            }
        )
        payload = self.service.suggest({"currentInput": "继续", "recentContext": "当前正在续写", "topK": 2})
        self.assertIn("历史输入会进入模型预测", predictor.last_recent_context)
        self.assertIn("当前正在续写", predictor.last_recent_context)
        self.assertIn("historyContext", payload)
        self.assertIn("历史输入会进入模型预测", payload["historyContext"])

    def test_predictor_ttfc_probe_uses_streaming_provider(self) -> None:
        MockOllamaStreamingHandler.captured_payloads = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), MockOllamaStreamingHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.service.predictor = OllamaPredictionProvider(
                OllamaPredictionConfig(
                    base_url=f"http://127.0.0.1:{server.server_port}",
                    model="qwen3.5:0.8b-mlx",
                    profile="instant",
                    timeout_s=1.0,
                )
            )
            report = self.service.predictor_ttfc(
                {
                    "cases": [
                        {
                            "id": "debug-case",
                            "currentInput": "RAG 输入法",
                            "recentContext": "用户正在调试模型首候选",
                        }
                    ],
                    "repeat": 1,
                    "latencyBudgetMs": 200,
                }
            )
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(report["schemaVersion"], "rag-ime.debug-predictor-ttfc.v1")
        self.assertEqual(report["repeat"]["effectiveCaseCount"], 1)
        benchmark = report["benchmark"]
        self.assertTrue(benchmark["supported"])
        self.assertTrue(benchmark["summary"]["hasFirstCandidate"])
        self.assertEqual(benchmark["cases"][0]["caseId"], "debug-case")
        self.assertEqual(benchmark["cases"][0]["candidates"][0], "本地记忆")
        self.assertTrue(MockOllamaStreamingHandler.captured_payloads[0]["stream"])
        self.assertFalse(MockOllamaStreamingHandler.captured_payloads[0]["think"])

    def test_rime_suggest_endpoint_uses_rime_candidates_for_side_query(self) -> None:
        predictor = FakePredictionProvider()
        self.service.predictor = predictor
        payload = self.service.rime_suggest(
            {
                "sessionId": "debug-rime",
                "requestSeq": 11,
                "privacyDisposition": "allowed",
                "rawInput": "jiubiruwopinshishur",
                "preedit": "jiubiruwopinshishur",
                "committedContext": "用户正在写输入法设计",
                "maxVisibleCandidates": 4,
                "maxSideCandidates": 2,
                "debugAllowCompositionLanes": True,
                "rimeContext": {
                    "candidates": [
                        {"label": "1", "text": "就比如", "comment": "rime"},
                        {"label": "2", "text": "我平时输入", "comment": "rime"},
                    ]
                },
            }
        )
        self.assertEqual(payload["schemaVersion"], "rag-ime.rime-sidecar.v1")
        self.assertEqual(payload["queryBasis"], "rimeCandidates")
        self.assertIn("就比如", predictor.last_current_input)
        self.assertNotIn("jiubiruwopinshishur", predictor.last_current_input)
        self.assertEqual(payload["uiMode"], "composition_rime")
        self.assertEqual([item["selectionAction"] for item in payload["displayCandidates"]], ["select_rime_candidate", "select_rime_candidate"])
        self.assertEqual([item["displayLayout"] for item in payload["displayCandidates"]], ["fallback", "fallback"])
        self.assertFalse(payload["cache"]["hit"])
        self.assertEqual(payload["rankingDiagnostics"]["schemaVersion"], "rag-ime.ranking-diagnostics.v1")
        self.assertEqual(payload["rankingDiagnostics"]["candidateCount"], len(payload["displayCandidates"]))
        self.assertEqual(payload["rankingDiagnostics"]["sourceCounts"], {"rime": 2})

    def test_rime_suggest_ranking_diagnostics_explain_rag_score_breakdown(self) -> None:
        db_path = Path(self.tmp.name) / "ranking-diagnostics.sqlite"
        core = LocalSqliteCoreClient(db_path)
        core.initialize()
        adapter = InputMethodAdapter(core)
        memory_id = adapter.commit_text(
            "设计一个候选展示方式",
            recent_context="Prediction-first RAG IME 需要解释候选排序",
            project="wisdom-weasel-rag-ime",
            tags=("rag", "memory", "phrase-memory"),
            privacy_disposition="allowed",
        )
        core.apply_action(
            MemoryAction(
                action_id=None,
                created_at_ms=0,
                memory_id=memory_id,
                action_type="accepted",
                query="候选展示方式",
            )
        )
        service = self._new_service(
            DebugServerConfig(
                db_path=db_path,
                static_dir=Path("debug"),
                seed_if_empty=False,
                core=core,
                predictor=FakePredictionProvider(),
            )
        )

        payload = {
            "sessionId": "debug-rag-score",
            "requestSeq": 41,
            "privacyDisposition": "allowed",
            "rawInput": "zs",
            "preedit": "zs",
            "committedContext": "我想设计一个",
            "maxVisibleCandidates": 5,
            "maxSideCandidates": 3,
            "forceSideCandidates": True,
            "rimeContext": {"candidates": [{"label": "1", "text": "展示", "comment": "rime"}]},
        }
        response = service.rime_suggest(payload)

        diagnostics = response["rankingDiagnostics"]
        self.assertTrue(diagnostics["hasRagScoreBreakdown"])
        self.assertEqual(diagnostics["evidenceSourceCounts"]["rag"], 1)
        rag_diag = next(item for item in diagnostics["evidenceItems"] if item["sourceType"] == "rag")
        self.assertEqual(rag_diag["scoreBreakdown"]["schemaVersion"], "rag-ime.score-breakdown.v1")
        self.assertEqual(rag_diag["scoreBreakdown"]["components"]["accepted"], 0.6)
        self.assertEqual(rag_diag["rawSignalsSummary"]["acceptedCount"], 1)
        self.assertTrue(rag_diag["topScoreComponents"])

        cached_payload = dict(payload)
        cached_payload["sessionId"] = "debug-rag-score-cache"
        cached_payload["requestSeq"] = 42
        cached = service.rime_suggest(cached_payload)
        self.assertTrue(cached["cache"]["hit"])
        self.assertTrue(cached["rankingDiagnostics"]["hasRagScoreBreakdown"])
        self.assertEqual(cached["rankingDiagnostics"]["evidenceSourceCounts"]["rag"], 1)

    def test_rime_suggest_prediction_first_merge_is_debuggable_and_cache_separated(self) -> None:
        service = self._new_service(
            DebugServerConfig(
                db_path=Path(self.tmp.name) / "prediction-first-cache.sqlite",
                static_dir=Path("debug"),
                seed_if_empty=False,
                core=PrefixFixtureCore(),
                predictor=PrefixPredictionProvider(),
            )
        )
        base_payload = {
            "sessionId": "debug-prediction-first",
            "requestSeq": 31,
            "privacyDisposition": "allowed",
            "rawInput": "sj",
            "preedit": "sj",
            "committedContext": "我想",
            "forceSideCandidates": True,
            "maxVisibleCandidates": 5,
            "maxSideCandidates": 3,
            "rimeContext": {
                "candidates": [
                    {"label": "1", "text": "手机", "comment": "wanxiang"},
                    {"label": "2", "text": "世界", "comment": "wanxiang"},
                ]
            },
        }

        legacy_payload = dict(base_payload)
        legacy_payload["predictionFirstMerge"] = False
        legacy = service.rime_suggest(legacy_payload)
        prediction_first_payload = dict(base_payload)
        prediction_first_payload["requestSeq"] = 32
        prediction_first_payload["predictionFirstMerge"] = True
        prediction_first = service.rime_suggest(prediction_first_payload)

        self.assertFalse(legacy["predictionFirst"]["enabled"])
        self.assertFalse(prediction_first["cache"]["hit"])
        self.assertTrue(prediction_first["predictionFirst"]["enabled"])
        self.assertEqual(prediction_first["predictionFirst"]["mode"], "prefix_constrained_composing")
        self.assertEqual(prediction_first["predictionFirst"]["pinyinPrefix"], "sj")
        self.assertEqual(
            [item["text"] for item in prediction_first["displayCandidates"]],
            ["设计输入法状态机", "手机", "世界"],
        )
        self.assertEqual(
            [item["displayLane"] for item in prediction_first["displayCandidates"]],
            ["model", "wanxiang", "wanxiang"],
        )
        self.assertEqual(prediction_first["predictionFirst"]["policy"]["sideInserted"], 1)
        self.assertTrue(prediction_first["predictionFirst"]["policy"]["panelVisible"])
        self.assertTrue(prediction_first["predictionFirst"]["policy"]["sessionBound"])
        self.assertTrue(prediction_first["predictionFirst"]["policy"]["rimeCompositionOwnedByRime"])

    def test_prediction_live_trace_redacts_text_and_reports_lanes(self) -> None:
        service = self._new_service(
            DebugServerConfig(
                db_path=Path(self.tmp.name) / "prediction-live-trace.sqlite",
                static_dir=Path("debug"),
                seed_if_empty=False,
                core=PrefixFixtureCore(),
                predictor=PrefixPredictionProvider(),
            )
        )
        service.rime_suggest(
            {
                "sessionId": "debug-live-trace",
                "requestSeq": 1,
                "privacyDisposition": "allowed",
                "rawInput": "sj",
                "preedit": "sj",
                "committedContext": "我想输入真实内容",
                "maxVisibleCandidates": 5,
                "maxSideCandidates": 3,
                "forceSideCandidates": True,
                "predictionFirstMerge": True,
                "rimeContext": {"candidates": [{"label": "1", "text": "手机", "comment": "wanxiang"}]},
            }
        )

        trace = service.prediction_live_trace({"limit": 10})
        self.assertEqual(trace["schemaVersion"], "rag-ime.prediction-live-trace.v1")
        self.assertEqual(trace["count"], 1)
        self.assertFalse(trace["rawTextVisible"])
        frame = trace["frames"][0]
        self.assertEqual(frame["sessionId"], "debug-live-trace")
        self.assertEqual(frame["predictionSession"]["phase"], "prefix_constrained")
        self.assertIn("queryAnchor", frame["predictionSession"])
        self.assertNotIn("text", frame["committedContext"])
        self.assertEqual(frame["committedContext"]["length"], len("我想输入真实内容"))
        self.assertGreaterEqual(frame["display"]["sourceCounts"]["model"], 1)
        self.assertTrue(frame["traceEvents"])

    def test_management_context_ignores_all_doctor_probe_sessions(self) -> None:
        service = self._new_service(
            DebugServerConfig(
                db_path=Path(self.tmp.name) / "management-context-doctor-filter.sqlite",
                static_dir=Path("debug"),
                seed_if_empty=False,
                core=PrefixFixtureCore(),
                predictor=PrefixPredictionProvider(),
            )
        )
        service._prediction_live_trace = [
            {
                "sessionId": "real-foreground-session",
                "requestId": "real-request",
                "foregroundContext": {
                    "source": "text_input_client",
                    "applied": True,
                    "commitTextMatched": True,
                    "capturedAtMs": 123,
                    "surroundingBeforeChars": 18,
                },
            },
            {
                "sessionId": "doctor-raw-context",
                "requestId": "synthetic-request",
                "foregroundContext": {
                    "source": "missing",
                    "applied": False,
                    "captureFailureReason": "foregroundText payload missing",
                },
            },
        ]

        latest = service._last_management_prediction()

        self.assertEqual(latest["requestId"], "real-request")
        self.assertEqual(latest["contextSource"], "text_input_client")
        self.assertTrue(latest["foregroundContext"]["applied"])

    def test_prediction_live_trace_http_endpoint(self) -> None:
        service = self._new_service(
            DebugServerConfig(
                db_path=Path(self.tmp.name) / "prediction-live-trace-http.sqlite",
                static_dir=Path("debug"),
                seed_if_empty=False,
                core=PrefixFixtureCore(),
                predictor=PrefixPredictionProvider(),
            )
        )
        service.rime_suggest(
            {
                "sessionId": "debug-live-trace-http",
                "requestSeq": 1,
                "privacyDisposition": "allowed",
                "rawInput": "sj",
                "preedit": "sj",
                "committedContext": "我想",
                "maxVisibleCandidates": 5,
                "maxSideCandidates": 3,
                "forceSideCandidates": True,
                "predictionFirstMerge": True,
                "rimeContext": {"candidates": [{"label": "1", "text": "手机", "comment": "wanxiang"}]},
            }
        )

        class Handler(DebugRequestHandler):
            pass

        Handler.service = service
        Handler.static_dir = Path("debug")
        debug_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        debug_thread = Thread(target=debug_server.serve_forever, daemon=True)
        debug_thread.start()
        try:
            with urlopen(
                f"http://127.0.0.1:{debug_server.server_port}/api/prediction/live-trace?limit=5",
                timeout=5,
            ) as response:
                trace_payload = json.loads(response.read().decode("utf-8"))
            with urlopen(
                f"http://127.0.0.1:{debug_server.server_port}/api/prediction/drop-stats?limit=5",
                timeout=5,
            ) as response:
                drop_payload = json.loads(response.read().decode("utf-8"))
        finally:
            debug_server.shutdown()
            debug_thread.join(timeout=2)
            debug_server.server_close()

        self.assertEqual(trace_payload["schemaVersion"], "rag-ime.prediction-live-trace.v1")
        self.assertEqual(trace_payload["count"], 1)
        self.assertEqual(trace_payload["frames"][0]["sessionId"], "debug-live-trace-http")
        self.assertEqual(drop_payload["schemaVersion"], "rag-ime.prediction-drop-stats.v1")
        self.assertEqual(drop_payload["frameCount"], 1)

    def test_rime_suggest_cache_hit_rebinds_frontend_transaction_fields(self) -> None:
        service = self._new_service(
            DebugServerConfig(
                db_path=Path(self.tmp.name) / "prediction-first-transaction-cache.sqlite",
                static_dir=Path("debug"),
                seed_if_empty=False,
                core=PrefixFixtureCore(),
                predictor=PrefixPredictionProvider(),
            )
        )
        base_payload = {
            "sessionId": "debug-cache-transaction-a",
            "requestSeq": 71,
            "privacyDisposition": "allowed",
            "frontendRevision": 10,
            "selectionEpoch": 20,
            "frontAppBundleId": "com.apple.TextEdit",
            "inputSourceId": "im.rime.inputmethod.Squirrel.Hans",
            "compositionHash": "sha256:composition-a",
            "committedContextHash": "sha256:context-a",
            "panelSessionId": "panel-a",
            "rawInput": "sj",
            "preedit": "sj",
            "committedContext": "我想",
            "maxVisibleCandidates": 5,
            "maxSideCandidates": 3,
            "predictionFirstMerge": True,
            "rimeContext": {
                "candidates": [
                    {"label": "1", "text": "手机", "comment": "wanxiang"},
                    {"label": "2", "text": "世界", "comment": "wanxiang"},
                ]
            },
        }
        first = service.rime_suggest(base_payload)
        second_payload = dict(base_payload)
        second_payload.update(
            {
                "sessionId": "debug-cache-transaction-b",
                "requestSeq": 72,
                "frontendRevision": 11,
                "selectionEpoch": 21,
                "frontAppBundleId": "com.apple.Notes",
                "inputSourceId": "im.rime.inputmethod.Squirrel.Hans.Patched",
                "compositionHash": "sha256:composition-b",
                "committedContextHash": "sha256:context-b",
                "panelSessionId": "panel-b",
            }
        )
        second = service.rime_suggest(second_payload)

        self.assertFalse(first["cache"]["hit"])
        self.assertTrue(second["cache"]["hit"])
        self.assertEqual(second["sessionId"], "debug-cache-transaction-b")
        self.assertEqual(second["requestSeq"], 72)
        self.assertEqual(second["frontendRevision"], 11)
        self.assertEqual(second["selectionEpoch"], 21)
        self.assertEqual(second["frontendTransaction"]["panelSessionId"], "panel-b")
        self.assertTrue(second["predictionSession"]["cacheRebound"])
        self.assertEqual(second["predictionSession"]["requestSeq"], 72)
        self.assertEqual(second["predictionSession"]["frontendRevision"], 11)
        self.assertEqual(second["predictionSession"]["selectionEpoch"], 21)
        self.assertEqual(second["predictionSession"]["frontAppBundleId"], "com.apple.Notes")
        self.assertEqual(second["predictionSession"]["inputSourceId"], "im.rime.inputmethod.Squirrel.Hans.Patched")
        self.assertEqual(second["predictionSession"]["compositionHash"], "sha256:composition-b")
        self.assertEqual(second["predictionSession"]["committedContextHash"], "sha256:context-b")
        self.assertEqual(second["predictionSession"]["panelSessionId"], "panel-b")
        self.assertNotEqual(first["predictionSession"]["hardContextAnchor"], second["predictionSession"]["hardContextAnchor"])
        for item in second["displayCandidates"]:
            metadata = item["metadata"]
            self.assertTrue(metadata["cacheRebound"])
            self.assertEqual(metadata["requestSeq"], 72)
            self.assertEqual(metadata["sessionId"], "debug-cache-transaction-b")
            self.assertEqual(metadata["frontendRevision"], 11)
            self.assertEqual(metadata["selectionEpoch"], 21)
            self.assertEqual(metadata["panelSessionId"], "panel-b")
            self.assertEqual(item["hardContextAnchor"], second["predictionSession"]["hardContextAnchor"])

    def test_rime_suggest_cache_hits_repeated_equivalent_payloads(self) -> None:
        predictor = FakePredictionProvider()
        self.service.predictor = predictor
        payload = {
            "sessionId": "cache-a",
            "requestSeq": 21,
            "privacyDisposition": "allowed",
            "rawInput": "ragshurufa",
            "preedit": "ragshurufa",
            "committedContext": "用户正在写 RAG 输入法",
            "maxVisibleCandidates": 5,
            "maxSideCandidates": 2,
            "forceSideCandidates": True,
            "rimeContext": {"candidates": [{"label": "1", "text": "RAG 输入法", "comment": "rime"}]},
        }
        first = self.service.rime_suggest(payload)
        second_payload = dict(payload)
        second_payload["requestSeq"] = 22
        second_payload["sessionId"] = "cache-b"
        second = self.service.rime_suggest(second_payload)
        self.assertEqual(predictor.calls, 1)
        self.assertFalse(first["cache"]["hit"])
        self.assertTrue(second["cache"]["hit"])
        self.assertEqual(second["requestSeq"], 22)
        self.assertEqual(second["sessionId"], "cache-b")

        self.service.commit({"text": "cache invalidation commit", "privacyDisposition": "allowed"})
        third_payload = dict(payload)
        third_payload["requestSeq"] = 23
        third = self.service.rime_suggest(third_payload)
        self.assertFalse(third["cache"]["hit"])
        self.assertEqual(predictor.calls, 2)

    def test_rime_suggest_cache_key_and_env_follow_pinyin_settings(self) -> None:
        payload = {
            "sessionId": "cache-pinyin-a",
            "requestSeq": 1,
            "privacyDisposition": "allowed",
            "rawInput": "shijie",
            "preedit": "shijie",
            "rimeContext": {"candidates": [{"text": "世界", "label": "1"}]},
        }

        first = self.service.rime_suggest(payload)
        self.service.settings_update(
            {
                "pinyin.fuzzyProfile": "none",
                "pinyin.rerankUsesFuzzy": False,
                "pinyin.pairs.sSh": False,
                "pinyin.pairs.nL": True,
            }
        )
        second_payload = dict(payload)
        second_payload.update({"sessionId": "cache-pinyin-b", "requestSeq": 2})
        second = self.service.rime_suggest(second_payload)

        self.assertFalse(first["cache"]["hit"])
        self.assertFalse(second["cache"]["hit"])
        self.assertEqual(os.environ["RAG_IME_PINYIN_FUZZY_ENABLED"], "0")
        self.assertEqual(os.environ["RAG_IME_PINYIN_FUZZY_PROFILE"], "none")
        self.assertEqual(os.environ["RAG_IME_PINYIN_FUZZY_S_SH"], "0")
        self.assertEqual(os.environ["RAG_IME_PINYIN_FUZZY_N_L"], "1")

    def test_rime_suggest_cache_bypasses_progressive_followup(self) -> None:
        predictor = FakePredictionProvider()
        self.service.predictor = predictor
        payload = {
            "sessionId": "cache-progressive-a",
            "requestSeq": 31,
            "privacyDisposition": "allowed",
            "rawInput": "ragshurufa",
            "preedit": "ragshurufa",
            "committedContext": "用户正在写 RAG 输入法",
            "maxVisibleCandidates": 5,
            "maxSideCandidates": 2,
            "forceSideCandidates": True,
            "rimeContext": {"candidates": [{"label": "1", "text": "RAG 输入法", "comment": "rime"}]},
        }
        first = self.service.rime_suggest(payload)
        second_payload = dict(payload)
        second_payload.update({"sessionId": "cache-progressive-b", "requestSeq": 32, "progressiveFollowUp": True})
        second = self.service.rime_suggest(second_payload)

        self.assertFalse(first["cache"]["hit"])
        self.assertFalse(second["cache"]["hit"])
        self.assertEqual(predictor.calls, 2)

    def test_rime_suggest_dedupes_in_flight_equivalent_payloads(self) -> None:
        predictor = BlockingPredictionProvider()
        self.service.predictor = predictor
        payload = {
            "sessionId": "inflight-a",
            "requestSeq": 61,
            "privacyDisposition": "allowed",
            "rawInput": "ragshurufa",
            "preedit": "ragshurufa",
            "committedContext": "用户正在写 RAG 输入法",
            "maxVisibleCandidates": 5,
            "maxSideCandidates": 2,
            "forceSideCandidates": True,
            "rimeContext": {"candidates": [{"label": "1", "text": "RAG 输入法", "comment": "rime"}]},
        }
        second_payload = dict(payload)
        second_payload["sessionId"] = "inflight-b"
        second_payload["requestSeq"] = 62

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(self.service.rime_suggest, payload)
            self.assertTrue(predictor.entered.wait(timeout=2))
            second_future = pool.submit(self.service.rime_suggest, second_payload)
            self._wait_for_inflight_hits(expected=1)
            predictor.release.set()
            first = first_future.result(timeout=2)
            second = second_future.result(timeout=2)

        self.assertEqual(predictor.calls, 1)
        self.assertFalse(first["cache"]["hit"])
        self.assertFalse(first["cache"]["inFlightHit"])
        self.assertFalse(second["cache"]["hit"])
        self.assertTrue(second["cache"]["inFlightHit"])
        self.assertEqual(second["sessionId"], "inflight-b")
        self.assertEqual(second["requestSeq"], 62)
        self.assertEqual(self.service.health()["rimeSuggestCache"]["inFlightHits"], 1)

    def test_rime_suggest_cache_key_includes_vector_index_state(self) -> None:
        predictor = FakePredictionProvider()
        core = VectorAwareFixtureCore()
        service = self._new_service(
            DebugServerConfig(
                db_path=Path(self.tmp.name) / "cache-vector-state.sqlite",
                core=core,
                predictor=predictor,
                seed_if_empty=False,
                project="wisdom-weasel-rag-ime",
            )
        )
        payload = {
            "sessionId": "cache-vector-a",
            "requestSeq": 51,
            "privacyDisposition": "allowed",
            "rawInput": "ragshurufa",
            "preedit": "ragshurufa",
            "committedContext": "用户正在写 RAG 输入法",
            "maxVisibleCandidates": 5,
            "maxSideCandidates": 2,
            "forceSideCandidates": True,
            "rimeContext": {"candidates": [{"label": "1", "text": "RAG 输入法", "comment": "rime"}]},
        }

        first = service.rime_suggest(payload)
        second_payload = dict(payload)
        second_payload["requestSeq"] = 52
        second = service.rime_suggest(second_payload)
        core.vector_revision += 1
        third_payload = dict(payload)
        third_payload["requestSeq"] = 53
        third = service.rime_suggest(third_payload)

        self.assertEqual(predictor.calls, 2)
        self.assertFalse(first["cache"]["hit"])
        self.assertTrue(second["cache"]["hit"])
        self.assertFalse(third["cache"]["hit"])
        self.assertEqual(service.health()["vectorStats"]["activeProviderVectors"], 2)

    def test_rime_suggest_cache_ignores_raw_pinyin_when_semantic_query_is_stable(self) -> None:
        predictor = FakePredictionProvider()
        self.service.predictor = predictor
        payload = {
            "sessionId": "cache-raw-a",
            "requestSeq": 41,
            "privacyDisposition": "allowed",
            "rawInput": "ragshuru",
            "preedit": "ragshuru",
            "committedContext": "用户正在写 RAG 输入法",
            "maxVisibleCandidates": 5,
            "maxSideCandidates": 2,
            "forceSideCandidates": True,
            "rimeContext": {"candidates": [{"label": "1", "text": "RAG 输入法", "comment": "rime"}]},
        }
        first = self.service.rime_suggest(payload)
        second_payload = dict(payload)
        second_payload["sessionId"] = "cache-raw-b"
        second_payload["requestSeq"] = 42
        second_payload["rawInput"] = "ragshurufa"
        second_payload["preedit"] = "ragshurufa"
        second = self.service.rime_suggest(second_payload)

        self.assertEqual(predictor.calls, 1)
        self.assertFalse(first["cache"]["hit"])
        self.assertTrue(second["cache"]["hit"])
        self.assertEqual(second["rawInput"], "ragshurufa")
        self.assertEqual(second["preedit"], "ragshurufa")
        self.assertIn("RAG 输入法", second["semanticQuery"])
        self.assertNotIn("ragshuru", second["semanticQuery"])
        self.assertEqual(second["queryBasis"], "rimeCandidates")

    def test_cache_probe_reports_warm_suggestion_and_rime_hits(self) -> None:
        report = self.service.cache_probe(
            {
                "currentInput": "RAG 输入法",
                "recentContext": "用户正在调试缓存命中",
                "repeat": 3,
                "topK": 3,
            }
        )

        self.assertEqual(report["schemaVersion"], "rag-ime.debug-cache-probe.v1")
        self.assertEqual(report["repeat"], 3)
        self.assertEqual(report["expectedWarmHits"], 2)
        self.assertGreaterEqual(report["suggestionCache"]["hitsDelta"], 2)
        self.assertGreaterEqual(report["rimeSuggestCache"]["hitsDelta"], 2)
        self.assertTrue(report["summary"]["suggestionCachePassed"])
        self.assertTrue(report["summary"]["rimeCachePassed"])
        self.assertEqual(len(report["samples"]["rimeSuggest"]), 3)
        self.assertFalse(report["samples"]["rimeSuggest"][0]["hit"])
        self.assertTrue(report["samples"]["rimeSuggest"][1]["hit"])
        self.assertIn("sourceCounts", report["samples"]["rimeSuggest"][0])
        self.assertIn("topCandidate", report["samples"]["rimeSuggest"][0])
        self.assertTrue(report["samples"]["suggest"][0]["topSuggestions"])

    def test_commit_and_action_are_wired_for_debug_page(self) -> None:
        suggestion = self.service.suggest({"currentInput": "FTS5", "topK": 1})["suggestions"][0]
        action = self.service.action(
            {
                "actionType": "pin",
                "privacyDisposition": "allowed",
                "memoryId": suggestion["memoryId"],
                "suggestionId": suggestion["suggestionId"],
                "sourceEventId": suggestion["sourceEventId"],
                "query": "FTS5",
                "surfaceText": suggestion["surfaceText"],
            }
        )
        self.assertEqual(action["schemaVersion"], "rag-ime.action.v1")
        self.assertEqual(action["actionType"], "pin")
        committed = self.service.commit(
            {
                "text": "输入法调试页接受了一条候选",
                "privacyDisposition": "allowed",
                "recentContext": "debug page",
                "preedit": "debug",
                "candidateRank": 1,
                "tags": ["debug"],
            }
        )
        self.assertTrue(committed["ok"])
        self.assertTrue(str(committed["eventId"]).startswith("event:"))

    def test_assistant_candidate_remember_creates_and_pins_model_memory(self) -> None:
        result = self.service.assistant_candidate_action(
            {
                "action": "remember",
                "privacyDisposition": "allowed",
                "candidate": {
                    "text": "把前台验收流程跑通",
                    "insertText": "把前台验收流程跑通",
                    "sourceType": "model",
                    "memoryId": "active-rag:synthetic-model-candidate",
                    "candidateStableId": "model:remember-test",
                },
                "query": "前台验收",
                "project": self.service.config.project,
                "app": "com.apple.TextEdit",
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "remember")
        self.assertTrue(str(result["memoryId"]).startswith("event:"))
        self.assertEqual(result["pinned"]["actionType"], "pin")

    def test_assistant_candidate_suppress_creates_text_tombstone(self) -> None:
        result = self.service.assistant_candidate_action(
            {
                "action": "suppress",
                "privacyDisposition": "allowed",
                "candidate": {
                    "text": "不要再显示这个候选",
                    "insertText": "不要再显示这个候选",
                    "sourceType": "model",
                    "candidateStableId": "model:suppress-test",
                },
                "query": "候选反馈",
                "project": self.service.config.project,
                "app": "com.apple.TextEdit",
            }
        )
        governance = self.service.core.optimizer_governance_snapshot(
            memory_ids=[],
            texts=["不要再显示这个候选"],
            project=self.service.config.project,
            app="com.apple.TextEdit",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "suppress")
        self.assertGreater(int(result["tombstoneId"]), 0)
        self.assertIn("不要再显示这个候选", governance["tombstonedTexts"])

    def test_assistant_candidate_action_fails_closed_before_memory_or_tombstone_write(self) -> None:
        before_events = self.service.core.event_count()
        before_actions = self.service.core.action_count()
        missing = self.service.assistant_candidate_action(
            {
                "action": "remember",
                "candidate": {
                    "insertText": "缺少隐私判断的候选",
                    "sourceType": "model",
                },
            }
        )
        sensitive = self.service.assistant_candidate_action(
            {
                "action": "suppress",
                "privacyDisposition": "allowed",
                "secureInput": True,
                "candidate": {
                    "insertText": "敏感字段候选",
                    "sourceType": "model",
                },
            }
        )
        governance = self.service.core.optimizer_governance_snapshot(
            memory_ids=[],
            texts=["缺少隐私判断的候选", "敏感字段候选"],
            project=self.service.config.project,
            app="",
        )

        for response, disposition in ((missing, "unknown"), (sensitive, "sensitive")):
            self.assertTrue(response["noStore"])
            self.assertFalse(response["stored"])
            self.assertEqual(response["privacyAssessment"]["disposition"], disposition)
            self.assertEqual(response["storageReceipt"]["outcome"], "no_store")
        self.assertEqual(self.service.core.event_count(), before_events)
        self.assertEqual(self.service.core.action_count(), before_actions)
        self.assertNotIn("缺少隐私判断的候选", governance["tombstonedTexts"])
        self.assertNotIn("敏感字段候选", governance["tombstonedTexts"])

    def test_rime_select_records_commit_and_rag_action(self) -> None:
        before_actions = self.service.core.action_count()
        with patch.dict(os.environ, {"RAG_IME_RAG_DIRECT_DISPLAY": "1"}):
            response = self.service.rime_suggest(
                {
                    "sessionId": "select-rag",
                    "requestSeq": 31,
                    "privacyDisposition": "allowed",
                    "rawInput": "ragshurufa",
                    "preedit": "ragshurufa",
                    "committedContext": "用户正在写 RAG 输入法",
                    "forceSideCandidates": True,
                    "maxVisibleCandidates": 6,
                    "maxSideCandidates": 4,
                    "rimeContext": {"candidates": [{"label": "1", "text": "RAG 输入法", "comment": "rime"}]},
                }
            )
        rag_candidate = next(item for item in response["displayCandidates"] if item["sourceType"] in {"rag", "memory"})
        selection = self.service.rime_select(
            {
                "privacyDisposition": "allowed",
                "candidate": rag_candidate,
                "query": response["semanticQuery"],
                "recentContext": response["committedContext"],
                "preedit": response["preedit"],
                "project": response["project"],
            }
        )
        self.assertEqual(selection["schemaVersion"], "rag-ime.rime-selection.v1")
        self.assertTrue(selection["ok"])
        self.assertTrue(str(selection["eventId"]).startswith("event:"))
        self.assertTrue(selection["recordedAction"])
        self.assertFalse(selection["recordedCommitAction"])
        self.assertEqual(selection["action"]["schemaVersion"], "rag-ime.action.v1")
        self.assertEqual(selection["action"]["actionType"], "accepted")
        self.assertEqual(self.service.core.action_count(), before_actions + 1)

    def test_rime_select_records_model_candidate_with_committed_event_feedback(self) -> None:
        before_actions = self.service.core.action_count()
        selection = self.service.rime_select(
            {
                "privacyDisposition": "allowed",
                "candidate": {
                    "label": "2",
                    "text": "模型短候选",
                    "insertText": "模型短候选",
                    "sourceType": "model",
                    "selectionAction": "commit_side_candidate",
                    "sourceIndex": 0,
                },
                "query": "模型",
                "recentContext": "用户正在测试 side candidate",
                "preedit": "moxing",
            }
        )
        self.assertTrue(str(selection["eventId"]).startswith("event:"))
        self.assertFalse(selection["recordedAction"])
        self.assertIsNone(selection["action"])
        self.assertTrue(selection["recordedCommitAction"])
        self.assertEqual(selection["commitAction"]["memoryId"], selection["eventId"])
        self.assertEqual(selection["commitAction"]["actionType"], "accepted")
        self.assertEqual(self.service.core.action_count(), before_actions + 1)

    def test_candidate_edit_feedback_downranks_all_sources_but_only_rime_updates_lexicon(self) -> None:
        model_feedback = self.service.candidate_edit_feedback(
            {
                "privacyDisposition": "allowed",
                "sourceType": "model",
                "candidateId": "model:accepted",
                "candidateText": "错误模型候选",
                "contextHash": "ctx:model",
                "app": "app.test",
            }
        )
        rime_feedback = self.service.candidate_edit_feedback(
            {
                "privacyDisposition": "allowed",
                "sourceType": "rime",
                "candidateId": "rime:accepted",
                "candidateText": "错误词语",
                "preedit": "cuowu",
                "contextHash": "ctx:rime",
                "app": "app.test",
            }
        )

        with self.service.core._connect() as conn:
            memory_rows = conn.execute(
                "SELECT candidate_source, action FROM memory_feedback_events "
                "WHERE candidate_id IN ('model:accepted', 'rime:accepted') ORDER BY candidate_id"
            ).fetchall()
            rank_rows = conn.execute(
                "SELECT accepted_text, action FROM rime_rank_feedback ORDER BY id"
            ).fetchall()

        self.assertTrue(model_feedback["recorded"])
        self.assertFalse(model_feedback["rimeRecorded"])
        self.assertTrue(rime_feedback["recorded"])
        self.assertTrue(rime_feedback["rimeRecorded"])
        self.assertEqual(
            [(str(row[0]), str(row[1])) for row in memory_rows],
            [("model", "backspace_after_accept"), ("rime", "backspace_after_accept")],
        )
        self.assertEqual([(str(row[0]), str(row[1])) for row in rank_rows], [("错误词语", "backspace_downrank")])

    def test_rime_select_records_memory_candidate_action(self) -> None:
        committed = self.service.commit(
            {
                "text": "用户选择候选会反向校准记忆源",
                "privacyDisposition": "allowed",
                "recentContext": "memory candidate feedback",
                "tags": ["memory"],
            }
        )
        memory_id = str(committed["eventId"])
        source_event_id = int(memory_id.split(":", 1)[1])
        before_actions = self.service.core.action_count()
        selection = self.service.rime_select(
            {
                "privacyDisposition": "allowed",
                "candidate": {
                    "label": "4",
                    "text": "用户选择候选会反向校准记忆源",
                    "insertText": "用户选择候选会反向校准记忆源",
                    "sourceType": "memory",
                    "selectionAction": "commit_side_candidate",
                    "sourceIndex": 0,
                    "suggestionId": "sug-mem-feedback",
                    "memoryId": memory_id,
                    "sourceEventId": source_event_id,
                },
                "query": "记忆候选反馈",
                "recentContext": "用户正在选择 memory candidate",
                "preedit": "jiyi",
            }
        )

        self.assertTrue(str(selection["eventId"]).startswith("event:"))
        self.assertTrue(selection["recordedAction"])
        self.assertFalse(selection["recordedCommitAction"])
        self.assertEqual(selection["action"]["actionType"], "accepted")
        self.assertEqual(self.service.core.action_count(), before_actions + 1)

    def test_rime_select_recent_context_without_source_event_records_committed_feedback_only(self) -> None:
        before_actions = self.service.core.action_count()
        selection = self.service.rime_select(
            {
                "privacyDisposition": "allowed",
                "candidate": {
                    "label": "5",
                    "text": "最近上下文切片",
                    "insertText": "最近上下文切片",
                    "sourceType": "memory",
                    "selectionAction": "commit_side_candidate",
                    "sourceIndex": 0,
                    "suggestionId": "recent-context-memory:0",
                    "memoryId": "recent-context:0",
                    "sourceEventId": 0,
                },
                "query": "recent",
                "recentContext": "debug-only recent context fallback",
                "preedit": "recent",
            }
        )

        self.assertTrue(str(selection["eventId"]).startswith("event:"))
        self.assertFalse(selection["recordedAction"])
        self.assertIsNone(selection["action"])
        self.assertTrue(selection["recordedCommitAction"])
        self.assertEqual(selection["commitAction"]["memoryId"], selection["eventId"])
        self.assertEqual(self.service.core.action_count(), before_actions + 1)

    def test_rime_select_dry_run_does_not_record_model_candidate(self) -> None:
        before_events = self.service.core.event_count()
        before_actions = self.service.core.action_count()
        selection = self.service.rime_select(
            {
                "dryRun": True,
                "privacyDisposition": "allowed",
                "candidate": {
                    "label": "2",
                    "text": "doctor side candidate",
                    "insertText": "doctor side candidate",
                    "sourceType": "model",
                    "selectionAction": "commit_side_candidate",
                    "sourceIndex": 0,
                },
                "query": "doctor",
                "recentContext": "Squirrel tryout readiness probe",
                "preedit": "doctor",
            }
        )
        self.assertEqual(selection["schemaVersion"], "rag-ime.rime-selection.v1")
        self.assertTrue(selection["dryRun"])
        self.assertEqual(selection["eventId"], "")
        self.assertFalse(selection["recordedAction"])
        self.assertFalse(selection["recordedCommitAction"])
        self.assertEqual(self.service.core.event_count(), before_events)
        self.assertEqual(self.service.core.action_count(), before_actions)

    def test_commit_endpoint_accepts_squirrel_source(self) -> None:
        committed = self.service.commit(
            {
                "text": "Squirrel HTTP sidecar commit",
                "privacyDisposition": "allowed",
                "source": "squirrel_rime_sidecar",
                "tags": ["squirrel"],
            }
        )
        self.assertTrue(committed["ok"])
        with closing(sqlite3.connect(self.service.config.db_path)) as conn, conn:
            source = conn.execute(
                "SELECT source FROM input_events WHERE committed_text = ?",
                ("Squirrel HTTP sidecar commit",),
            ).fetchone()[0]
        self.assertEqual(source, "squirrel_rime_sidecar")

    def test_commit_sanitizes_final_input_capture_metadata(self) -> None:
        digest = "b" * 64
        committed = self.service.commit(
            {
                "text": "输入框里的最终几百字正文",
                "privacyDisposition": "allowed",
                "source": "squirrel_input_segment",
                "captureMetadata": {
                    "captureSource": "accessibility",
                    "fallbackReason": "",
                    "fieldContextChars": 312,
                    "imeBufferChars": 2,
                    "selectedTextSha256": f"sha256:{digest}",
                    "selectionRule": "field_context_if_not_shorter_else_ime_buffer",
                    "rawText": "不允许复制到元数据",
                },
            }
        )

        self.assertTrue(committed["ok"])
        with closing(sqlite3.connect(self.service.config.db_path)) as conn, conn:
            raw = conn.execute(
                "SELECT capture_metadata_json FROM input_events WHERE committed_text = ?",
                ("输入框里的最终几百字正文",),
            ).fetchone()[0]
        metadata = json.loads(raw)
        self.assertEqual(metadata["captureSource"], "accessibility")
        self.assertEqual(metadata["selectedTextSha256"], digest)
        self.assertNotIn("rawText", metadata)
        self.assertNotIn("不允许", raw)

    def test_v2_strong_capture_is_idempotent_and_returns_durable_receipt(self) -> None:
        text = "Return 已由宿主提交的最终文本"
        before = self.service.core.event_count()
        payload = {
            "text": text,
            "privacyDisposition": "allowed",
            "source": "squirrel_input_segment",
            "app": "com.apple.TextEdit",
            "captureMetadata": _capture_v2(text),
        }

        first = self.service.commit(payload)
        replay = self.service.commit(payload)

        self.assertTrue(first["stored"])
        self.assertEqual(first["captureReceipt"]["outcome"], "stored")
        self.assertFalse(first["captureReceipt"]["duplicate"])
        self.assertEqual(replay["eventId"], first["eventId"])
        self.assertTrue(replay["captureReceipt"]["duplicate"])
        self.assertEqual(self.service.core.event_count(), before + 1)

    def test_v2_weak_lifecycle_boundary_is_audited_without_input_event(self) -> None:
        text = "焦点切换时尚未确认提交的缓冲文本"
        before = self.service.core.event_count()
        metadata = _capture_v2(
            text,
            capture_id="capture:debug:weak",
            boundary_kind="focus_change",
            boundary_confidence="weak",
            host_forwarded=False,
            final_committed=False,
        )

        response = self.service.commit(
            {
                "text": text,
                "privacyDisposition": "allowed",
                "source": "squirrel_input_segment",
                "app": "com.apple.TextEdit",
                "captureMetadata": metadata,
            }
        )

        self.assertFalse(response["stored"])
        self.assertTrue(response["noStore"])
        self.assertEqual(response["captureReceipt"]["outcome"], "quarantined")
        self.assertEqual(response["captureReceipt"]["eventId"], "")
        self.assertEqual(self.service.core.event_count(), before)

    def test_v2_privacy_rejection_precedes_weak_boundary_classification(self) -> None:
        text = "敏感字段中的未提交缓冲文本"
        metadata = _capture_v2(
            text,
            capture_id="capture:debug:weak-sensitive",
            boundary_kind="focus_change",
            boundary_confidence="weak",
            host_forwarded=False,
            final_committed=False,
        )

        response = self.service.commit(
            {
                "text": text,
                "privacyDisposition": "sensitive",
                "sensitiveField": True,
                "source": "squirrel_input_segment",
                "app": "com.apple.TextEdit",
                "captureMetadata": metadata,
            }
        )

        self.assertEqual(response["captureReceipt"]["outcome"], "no_store")
        self.assertNotEqual(
            response["captureReceipt"]["reason"],
            "weak_boundary_not_final",
        )

    def test_v2_privacy_no_store_receipt_wins_over_later_retry(self) -> None:
        text = "隐私状态未确认的输入"
        before = self.service.core.event_count()
        metadata = _capture_v2(text, capture_id="capture:debug:no-store")
        base = {
            "text": text,
            "source": "squirrel_input_segment",
            "app": "com.apple.TextEdit",
            "captureMetadata": metadata,
        }

        first = self.service.commit({**base, "privacyDisposition": "unknown"})
        retry = self.service.commit({**base, "privacyDisposition": "allowed"})

        self.assertEqual(first["captureReceipt"]["outcome"], "no_store")
        self.assertEqual(retry["captureReceipt"]["outcome"], "no_store")
        self.assertTrue(retry["captureReceipt"]["duplicate"])
        self.assertEqual(self.service.core.event_count(), before)

    def test_v2_candidate_selection_return_cannot_be_recorded(self) -> None:
        text = "候选选择不是宿主最终提交"
        metadata = _capture_v2(
            text,
            capture_id="capture:debug:candidate-return",
            native_composition_before=True,
            rime_handled=True,
            host_forwarded=False,
        )

        with self.assertRaisesRegex(ValueError, "prior composition"):
            self.service.commit(
                {
                    "text": text,
                    "privacyDisposition": "allowed",
                    "source": "squirrel_input_segment",
                    "app": "com.apple.TextEdit",
                    "captureMetadata": metadata,
                }
            )

    def test_commit_rejects_terminal_accessibility_scrollback_as_input(self) -> None:
        before = self.service.core.event_count()
        committed = self.service.commit(
            {
                "text": "终端 AXValue 返回的整屏滚动缓冲区",
                "privacyDisposition": "allowed",
                "source": "squirrel_input_segment",
                "app": "com.mitchellh.ghostty",
                "captureMetadata": {
                    "captureSource": "accessibility",
                    "fieldContextChars": 3123,
                    "imeBufferChars": 24,
                },
            }
        )

        self.assertFalse(committed["stored"])
        self.assertEqual(committed["eventId"], "skipped:accessibility_context_not_input")
        self.assertEqual(self.service.core.event_count(), before)

    def test_foreground_write_apis_return_no_store_receipts_without_explicit_allowed(self) -> None:
        before_events = self.service.core.event_count()
        before_actions = self.service.core.action_count()

        missing = self.service.commit({"text": "旧客户端缺少隐私判断"})
        explicit_unknown = self.service.commit(
            {"text": "前台状态无法判断", "privacyDisposition": "unknown"}
        )
        sensitive = self.service.commit(
            {
                "text": "密码字段内容",
                "privacyDisposition": "allowed",
                "sensitiveField": True,
            }
        )
        selection = self.service.rime_select(
            {
                "candidate": {
                    "insertText": "不应记录的候选",
                    "sourceType": "model",
                }
            }
        )
        action = self.service.action(
            {
                "actionType": "pin",
                "memoryId": "event:1",
            }
        )

        for response, disposition in (
            (missing, "unknown"),
            (explicit_unknown, "unknown"),
            (sensitive, "sensitive"),
            (selection, "unknown"),
            (action, "unknown"),
        ):
            self.assertTrue(response["ok"])
            self.assertTrue(response["noStore"])
            self.assertFalse(response["stored"])
            self.assertEqual(response["storageReceipt"]["outcome"], "no_store")
            self.assertEqual(response["privacyAssessment"]["disposition"], disposition)

        self.assertEqual(self.service.core.event_count(), before_events)
        self.assertEqual(self.service.core.action_count(), before_actions)

    def test_http_sidecar_accepts_root_paths(self) -> None:
        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/rime-suggest"
            request = Request(
                url,
                data=json.dumps(
                    {
                        "sessionId": "http-root",
                        "requestSeq": 1,
                        "privacyDisposition": "allowed",
                        "maxVisibleCandidates": 3,
                        "maxSideCandidates": 1,
                        "rimeContext": {"candidates": [{"label": "1", "text": "本地记忆"}]},
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            select_url = f"http://127.0.0.1:{server.server_port}/rime-select"
            select_request = Request(
                select_url,
                data=json.dumps(
                    {
                        "privacyDisposition": "allowed",
                        "candidate": {
                            "label": "2",
                            "text": "HTTP side candidate",
                            "insertText": "HTTP side candidate",
                            "sourceType": "model",
                            "selectionAction": "commit_side_candidate",
                            "sourceIndex": 0,
                        },
                        "query": "HTTP",
                        "recentContext": "root path",
                        "preedit": "http",
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(select_request, timeout=5) as response:
                selection = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(payload["schemaVersion"], "rag-ime.rime-sidecar.v1")
        self.assertEqual(payload["sessionId"], "http-root")
        self.assertEqual(selection["schemaVersion"], "rag-ime.rime-selection.v1")
        self.assertTrue(str(selection["eventId"]).startswith("event:"))

    def test_http_foreground_write_routes_fail_closed_for_legacy_privacy_payloads(self) -> None:
        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        before_events = self.service.core.event_count()
        before_actions = self.service.core.action_count()

        def post(path: str, payload: dict[str, object]) -> dict[str, object]:
            request = Request(
                f"http://127.0.0.1:{server.server_port}{path}",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))

        try:
            responses = (
                post("/commit", {"text": "legacy commit"}),
                post(
                    "/rime-select",
                    {"candidate": {"insertText": "legacy select", "sourceType": "model"}},
                ),
                post(
                    "/rime-rank-feedback",
                    {
                        "schemaVersion": "rag-ime.rime-rank-selection.v1",
                        "selectionId": "legacy-rime-rank",
                        "sourceType": "rime",
                        "preedit": "legacy",
                        "acceptedText": "旧客户端",
                        "candidateRank": 1,
                    },
                ),
                post(
                    "/assistant-candidate-action",
                    {
                        "action": "suppress",
                        "candidate": {"insertText": "legacy action", "sourceType": "model"},
                    },
                ),
                post(
                    "/action",
                    {"actionType": "pin", "memoryId": "event:1"},
                ),
            )
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        for response in responses:
            self.assertTrue(response["ok"])
            self.assertTrue(response["noStore"])
            self.assertFalse(response["stored"])
            self.assertEqual(response["privacyAssessment"]["disposition"], "unknown")
            self.assertEqual(response["storageReceipt"]["outcome"], "no_store")
        self.assertEqual(self.service.core.event_count(), before_events)
        self.assertEqual(self.service.core.action_count(), before_actions)

    def test_http_debug_server_exposes_predictor_ttfc_probe(self) -> None:
        MockOllamaStreamingHandler.captured_payloads = []
        model_server = ThreadingHTTPServer(("127.0.0.1", 0), MockOllamaStreamingHandler)
        model_thread = Thread(target=model_server.serve_forever, daemon=True)
        model_thread.start()

        class Handler(DebugRequestHandler):
            pass

        self.service.predictor = OllamaPredictionProvider(
            OllamaPredictionConfig(
                base_url=f"http://127.0.0.1:{model_server.server_port}",
                model="qwen3.5:0.8b-mlx",
                profile="instant",
                timeout_s=1.0,
            )
        )
        Handler.service = self.service
        Handler.static_dir = Path("debug")
        debug_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        debug_thread = Thread(target=debug_server.serve_forever, daemon=True)
        debug_thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{debug_server.server_port}/api/predictor-ttfc",
                data=json.dumps(
                    {
                        "currentInput": "Squirrel 候选",
                        "recentContext": "HTTP debug probe",
                        "repeat": 1,
                        "latencyBudgetMs": 200,
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        finally:
            debug_server.shutdown()
            debug_thread.join(timeout=2)
            debug_server.server_close()
            model_server.shutdown()
            model_thread.join(timeout=2)
            model_server.server_close()

        self.assertEqual(payload["schemaVersion"], "rag-ime.debug-predictor-ttfc.v1")
        self.assertEqual(payload["benchmark"]["providerName"], "local-ollama")
        self.assertTrue(payload["benchmark"]["summary"]["hasFirstCandidate"])
        self.assertEqual(MockOllamaStreamingHandler.captured_payloads[0]["model"], "qwen3.5:0.8b-mlx")

    def test_http_debug_server_exposes_cache_probe(self) -> None:
        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        debug_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        debug_thread = Thread(target=debug_server.serve_forever, daemon=True)
        debug_thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{debug_server.server_port}/api/cache-probe",
                data=json.dumps(
                    {
                        "currentInput": "缓存命中",
                        "recentContext": "HTTP debug cache probe",
                        "repeat": 3,
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        finally:
            debug_server.shutdown()
            debug_thread.join(timeout=2)
            debug_server.server_close()

        self.assertEqual(payload["schemaVersion"], "rag-ime.debug-cache-probe.v1")
        self.assertGreaterEqual(payload["summary"]["rimeCacheHitDelta"], 2)
        self.assertIn("suggestionCache", payload)

    def test_http_debug_server_exposes_memory_path_endpoints(self) -> None:
        os.environ["RAG_IME_MEMORY_OPTIMIZER"] = "1"
        os.environ["RAG_IME_MEMORY_OPTIMIZER_TRACE"] = "1"
        event_id = self.service.adapter.commit_text(
            "连续预测",
            recent_context="RAG 输入法需要更好的候选",
            project=self.service.config.project,
            tags=("phrase-memory",),
            privacy_disposition="allowed",
        )
        self.service.core.apply_action(
            MemoryAction(
                action_id=None,
                created_at_ms=2,
                memory_id=event_id,
                action_type="accepted",
                query="连续",
                metadata={"project": self.service.config.project},
            )
        )
        response = self.service.rime_suggest(
            {
                "sessionId": "debug-http-memory",
                "requestSeq": 101,
                "privacyDisposition": "allowed",
                "forceSideCandidates": True,
                "committedContext": "我想继续写连续",
                "maxVisibleCandidates": 4,
                "maxSideCandidates": 2,
                "rimeContext": {"candidates": [{"label": "1", "text": "连续", "comment": "rime"}]},
            }
        )
        trace_id = response["ragLane"]["memoryOptimizer"]["traceId"]
        cleanup = self.service.core.build_memory_cleanup_plan(project=self.service.config.project)
        cleanup_runs = self.service.core.list_memory_cleanup_runs(run_id=cleanup["runId"], limit=5)
        stable_diff = next(item for item in cleanup_runs["items"][0]["diffs"] if item["op"] == "add_stable_memory")

        class Handler(DebugRequestHandler):
            pass

        Handler.service = self.service
        Handler.static_dir = Path("debug")
        debug_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        debug_thread = Thread(target=debug_server.serve_forever, daemon=True)
        debug_thread.start()
        try:
            with urlopen(
                f"http://127.0.0.1:{debug_server.server_port}/api/memory/optimizer/trace/{quote(trace_id)}",
                timeout=5,
            ) as trace_response:
                trace_payload = json.loads(trace_response.read().decode("utf-8"))
            with urlopen(
                f"http://127.0.0.1:{debug_server.server_port}/api/memory/candidate/{quote('phrase:连续预测')}/explain?contextHash={quote(response['committedContextHash'])}",
                timeout=5,
            ) as explain_response:
                explain_payload = json.loads(explain_response.read().decode("utf-8"))
            with urlopen(
                f"http://127.0.0.1:{debug_server.server_port}/api/memory/suppressions?limit=10",
                timeout=5,
            ) as suppressions_response:
                suppressions_payload = json.loads(suppressions_response.read().decode("utf-8"))

            apply_request = Request(
                f"http://127.0.0.1:{debug_server.server_port}/api/memory/cleanup-diff/{stable_diff['diffId']}/apply",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(apply_request, timeout=5) as apply_response:
                apply_payload = json.loads(apply_response.read().decode("utf-8"))

            rollback_request = Request(
                f"http://127.0.0.1:{debug_server.server_port}/api/memory/cleanup-diff/{stable_diff['diffId']}/rollback",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(rollback_request, timeout=5) as rollback_response:
                rollback_payload = json.loads(rollback_response.read().decode("utf-8"))
        finally:
            debug_server.shutdown()
            debug_thread.join(timeout=2)
            debug_server.server_close()

        self.assertTrue(trace_payload["ok"])
        self.assertEqual(trace_payload["traceId"], trace_id)
        self.assertTrue(any(item["reason"] == "recent_committed_echo" for item in trace_payload["blocked"]))
        self.assertTrue(explain_payload["ok"])
        self.assertEqual(explain_payload["candidateId"], "phrase:连续预测")
        self.assertEqual(explain_payload["recentTrace"]["traceId"], trace_id)
        self.assertTrue(
            any(item["reason"] == "recent_committed_echo" for item in explain_payload["recentTrace"]["blocked"])
        )
        self.assertTrue(suppressions_payload["ok"])
        self.assertIn("suppressions", suppressions_payload)
        self.assertTrue(apply_payload["ok"])
        self.assertEqual(apply_payload["diff"]["status"], "applied")
        self.assertTrue(rollback_payload["ok"])
        self.assertEqual(rollback_payload["diff"]["status"], "rolled_back")

    def test_cli_cache_probe_runs_without_debug_server(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="rag-ime-cli-cache-probe-") as tmp:
            db_path = Path(tmp) / "rag-ime.sqlite"
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "rag_ime.cli",
                    "--db-path",
                    str(db_path),
                    "seed-demo",
                    "--reset",
                ],
                cwd=root,
                check=True,
                text=True,
                capture_output=True,
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "rag_ime.cli",
                    "--db-path",
                    str(db_path),
                    "cache-probe",
                    "RAG 输入法",
                    "--recent-context",
                    "CLI cache probe",
                    "--repeat",
                    "3",
                    "--rime-candidate",
                    "RAG 输入法",
                ],
                cwd=root,
                check=True,
                text=True,
                capture_output=True,
            )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schemaVersion"], "rag-ime.debug-cache-probe.v1")
        self.assertEqual(payload["repeat"], 3)
        self.assertGreaterEqual(payload["suggestionCache"]["hitsDelta"], 2)
        self.assertGreaterEqual(payload["rimeSuggestCache"]["hitsDelta"], 2)
        self.assertTrue(payload["summary"]["suggestionCachePassed"])
        self.assertTrue(payload["summary"]["rimeCachePassed"])

    def test_input_source_status_parses_check_script_output(self) -> None:
        script = Path(self.tmp.name) / "check-input-source.sh"
        script.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "echo 'id=im.rime.inputmethod.Squirrel.Hans name=Squirrel - Simplified enabled=true selectable=true selected=false current=com.apple.keylayout.ABC hitoolboxEnabled=true'",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        service = self._new_service(
            DebugServerConfig(
                db_path=Path(self.tmp.name) / "input-source.sqlite",
                static_dir=Path("debug"),
                seed_if_empty=False,
                input_source_check_script=script,
            )
        )

        status = service.input_source_status()

        self.assertEqual(status["schemaVersion"], "rag-ime.debug-input-source.v1")
        self.assertTrue(status["ok"])
        self.assertFalse(status["typingReady"])
        self.assertEqual(status["id"], "im.rime.inputmethod.Squirrel.Hans")
        self.assertEqual(status["name"], "Squirrel - Simplified")
        self.assertTrue(status["enabled"])
        self.assertTrue(status["selectable"])
        self.assertTrue(status["hitoolboxEnabled"])
        self.assertEqual(status["current"], "com.apple.keylayout.ABC")
        self.assertEqual(status["readinessState"], "switch")
        self.assertEqual(status["nextAction"], "select Squirrel from the macOS input menu")
        self.assertEqual(status["manualAction"], "macOS input menu -> Squirrel - Simplified")
        self.assertEqual(status["verificationCommand"], "scripts/wait_squirrel_typing_ready.sh")
        self.assertEqual(
            [item["name"] for item in status["readinessChecks"]],
            ["tis-visible", "third-party-list", "selected"],
        )

    def test_http_debug_server_exposes_input_source_status(self) -> None:
        script = Path(self.tmp.name) / "check-input-source.sh"
        script.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "echo 'id=im.rime.inputmethod.Squirrel.Hans name=Squirrel - Simplified enabled=true selectable=true selected=true current=im.rime.inputmethod.Squirrel.Hans hitoolboxEnabled=true'",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        service = self._new_service(
            DebugServerConfig(
                db_path=Path(self.tmp.name) / "input-source-http.sqlite",
                static_dir=Path("debug"),
                seed_if_empty=False,
                input_source_check_script=script,
            )
        )

        class Handler(DebugRequestHandler):
            pass

        Handler.service = service
        Handler.static_dir = Path("debug")
        debug_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        debug_thread = Thread(target=debug_server.serve_forever, daemon=True)
        debug_thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{debug_server.server_port}/api/input-source", timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        finally:
            debug_server.shutdown()
            debug_thread.join(timeout=2)
            debug_server.server_close()

        self.assertEqual(payload["schemaVersion"], "rag-ime.debug-input-source.v1")
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["typingReady"])
        self.assertTrue(payload["selected"])
        self.assertEqual(payload["readinessState"], "ready")
        self.assertEqual(payload["nextAction"], "start typing with Squirrel")
        self.assertEqual(payload["manualAction"], "type in a foreground macOS text field")
        self.assertEqual(payload["verificationCommand"], "scripts/wait_squirrel_typing_ready.sh")

    def test_input_source_status_marks_missing_source_as_install_needed(self) -> None:
        script = Path(self.tmp.name) / "check-input-source-missing.sh"
        script.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "echo 'id=im.rime.inputmethod.Squirrel.Hans name=Squirrel - Simplified enabled=false selectable=false selected=false current=com.apple.keylayout.ABC hitoolboxEnabled=false'",
                    "exit 1",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        service = self._new_service(
            DebugServerConfig(
                db_path=Path(self.tmp.name) / "input-source-missing.sqlite",
                static_dir=Path("debug"),
                seed_if_empty=False,
                input_source_check_script=script,
            )
        )

        status = service.input_source_status()

        self.assertFalse(status["ok"])
        self.assertFalse(status["typingReady"])
        self.assertEqual(status["readinessState"], "install")
        self.assertEqual(status["nextAction"], "add Squirrel in System Settings, then wait for the add gate")
        self.assertEqual(
            status["manualAction"],
            "System Settings -> Keyboard -> Input Sources -> Add -> Chinese, Simplified -> Squirrel - Simplified",
        )
        self.assertEqual(status["helperCommand"], "scripts/open_squirrel_input_source_settings.sh --wait")
        self.assertEqual(status["verificationCommand"], "scripts/wait_squirrel_input_source_added.sh")

    def test_input_source_status_marks_missing_third_party_registration_as_install_needed(self) -> None:
        script = Path(self.tmp.name) / "check-input-source-third-party-missing.sh"
        script.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "echo 'id=im.rime.inputmethod.Squirrel.Hans name=Squirrel - Simplified enabled=true selectable=true selected=false current=com.apple.keylayout.ABC hitoolboxEnabled=false thirdPartyEnabled=false'",
                    "exit 1",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        service = self._new_service(
            DebugServerConfig(
                db_path=Path(self.tmp.name) / "input-source-third-party-missing.sqlite",
                static_dir=Path("debug"),
                seed_if_empty=False,
                input_source_check_script=script,
            )
        )

        status = service.input_source_status()

        self.assertFalse(status["ok"])
        self.assertFalse(status["typingReady"])
        self.assertTrue(status["enabled"])
        self.assertTrue(status["selectable"])
        self.assertFalse(status["hitoolboxEnabled"])
        self.assertFalse(status["thirdPartyEnabled"])
        self.assertEqual(status["readinessState"], "install")
        self.assertEqual(status["nextAction"], "add Squirrel in System Settings, then wait for the add gate")
        self.assertEqual(status["helperCommand"], "scripts/open_squirrel_input_source_settings.sh --wait")
        self.assertEqual(status["verificationCommand"], "scripts/wait_squirrel_input_source_added.sh")
        third_party_check = next(item for item in status["readinessChecks"] if item["name"] == "third-party-list")
        self.assertFalse(third_party_check["passed"])
        self.assertFalse(third_party_check["thirdPartyEnabled"])

    def test_input_source_status_uses_branded_rag_ime_manual_action(self) -> None:
        script = Path(self.tmp.name) / "check-input-source-rag-ime-third-party-missing.sh"
        script.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "echo 'id=im.rag-ime.inputmethod.RagIme.Hans name=RAG-IME - Simplified enabled=true selectable=true selected=false current=im.rime.inputmethod.Squirrel.Hans hitoolboxEnabled=false thirdPartyEnabled=false'",
                    "exit 1",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        service = self._new_service(
            DebugServerConfig(
                db_path=Path(self.tmp.name) / "input-source-rag-ime-missing.sqlite",
                static_dir=Path("debug"),
                seed_if_empty=False,
                input_source_id="im.rag-ime.inputmethod.RagIme.Hans",
                input_source_check_script=script,
            )
        )

        status = service.input_source_status()

        self.assertFalse(status["ok"])
        self.assertEqual(status["readinessState"], "install")
        self.assertEqual(status["nextAction"], "add RAG-IME in System Settings, then wait for the add gate")
        self.assertEqual(
            status["manualAction"],
            "System Settings -> Keyboard -> Input Sources -> Add -> Chinese, Simplified -> RAG-IME - Simplified",
        )
        self.assertEqual(status["expectedInputSourceId"], "im.rag-ime.inputmethod.RagIme.Hans")

    def test_rejects_empty_commit_and_bad_action(self) -> None:
        with self.assertRaises(ValueError):
            self.service.commit({"text": " ", "privacyDisposition": "allowed"})
        with self.assertRaises(ValueError):
            self.service.action({"actionType": "unknown", "memoryId": "event:1"})

    def test_debug_service_can_use_injected_shared_core_adapter(self) -> None:
        service = self._new_service(
            DebugServerConfig(
                db_path=Path(self.tmp.name) / "fixture-core.sqlite",
                core=FixtureCoreClient(),
                seed_if_empty=False,
                project="wisdom-weasel-rag-ime",
            )
        )
        health = service.health()
        self.assertEqual(health["coreMode"], "json")
        self.assertIsNone(health["eventCount"])
        payload = service.suggest({"currentInput": "local-first 记忆", "topK": 2})
        self.assertEqual(payload["schemaVersion"], "rag-ime.suggestions.v1")
        self.assertGreaterEqual(len(payload["suggestions"]), 1)

    def _wait_for_inflight_hits(self, *, expected: int) -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if self.service.health()["rimeSuggestCache"]["inFlightHits"] >= expected:
                return
            time.sleep(0.01)
        self.fail(f"timed out waiting for {expected} in-flight cache hit(s)")


if __name__ == "__main__":
    unittest.main()
