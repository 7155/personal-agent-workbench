from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch
from urllib.request import urlopen

from rag_ime.debug_server import (
    DebugImeService,
    DebugRequestHandler,
    DebugServerConfig,
    _memory_projection_worker_enabled,
    run_debug_server,
)
from rag_ime.embeddings import HashingEmbeddingProvider
from rag_ime.local_sqlite_core import LocalSqliteCoreClient
from rag_ime.memory_ingest import normalize_text, upsert_memory_item
from rag_ime.memory_projection import (
    RETRIEVAL_DOCS_PROJECTION,
    enqueue_memory_projection,
)


class MemoryProjectionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-ime-projection-lifecycle-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.db_path = Path(self.temporary.name) / "rag-ime.sqlite"

    def test_sidecar_service_starts_real_worker_exposes_freshness_and_stops_it(self) -> None:
        provider = HashingEmbeddingProvider(dimensions=16)
        core = LocalSqliteCoreClient(
            self.db_path,
            embedding_provider=provider,
        )
        core.initialize()
        with core._connect() as conn:  # type: ignore[attr-defined]
            _insert_phrase(conn, "phrase:lifecycle", "生产投影生命周期")
            enqueue_memory_projection(
                conn,
                projection_kind=RETRIEVAL_DOCS_PROJECTION,
                aggregate_type="memory_item",
                aggregate_id="phrase:lifecycle",
                operation="upsert",
                project="rag-ime",
            )
        service = DebugImeService(
            DebugServerConfig(
                db_path=self.db_path,
                project="rag-ime",
                core=core,
                seed_if_empty=False,
                server_name="sidecar server",
                memory_projection_poll_interval_s=0.05,
            )
        )
        self.addCleanup(service.close)

        before = service.memory_projection_status()
        self.assertTrue(before["configured"])
        self.assertFalse(before["running"])

        service.start_background_services()
        self.assertTrue(
            _wait_until(
                # Document creation can precede the vector/checkpoint work.
                # Wait for the complete public readiness state asserted below.
                lambda: _projection_applied(core, "phrase:lifecycle")
                and service.memory_projection_status()["freshness"]["fresh"],
                timeout_s=3.0,
            )
        )
        health = service.health()
        maintenance = service.agent_memory_maintenance_status(
            {"project": "rag-ime", "limit": 5}
        )

        self.assertTrue(health["ok"])
        projection = health["memoryProjection"]
        self.assertTrue(projection["configured"])
        self.assertTrue(projection["running"])
        self.assertEqual(projection["owner"], "sidecar server")
        self.assertEqual(
            projection["freshness"]["providerFingerprint"],
            provider.fingerprint,
        )
        self.assertTrue(projection["freshness"]["fresh"])
        self.assertTrue(maintenance["projection"]["running"])
        self.assertTrue(maintenance["projection"]["freshness"]["fresh"])

        worker = service.memory_projection_worker
        self.assertIsNotNone(worker)
        service.close()
        assert worker is not None
        self.assertFalse(worker.status()["running"])
        # Idempotent shutdown must not close a component twice.
        service.close()

    def test_worker_start_failure_degrades_status_without_breaking_foreground_health(self) -> None:
        core = LocalSqliteCoreClient(self.db_path)
        failing_worker = _FailingProjectionWorker()
        with patch(
            "rag_ime.debug_server.MemoryProjectionWorker",
            return_value=failing_worker,
        ):
            service = DebugImeService(
                DebugServerConfig(
                    db_path=self.db_path,
                    core=core,
                    seed_if_empty=False,
                    server_name="sidecar server",
                )
            )
        self.addCleanup(service.close)

        service.start_background_services()
        self.assertTrue(
            _wait_until(
                lambda: "thread creation failed"
                in str(service.memory_projection_status()["lastError"]),
                timeout_s=2,
            )
        )
        health = service.health()

        self.assertTrue(health["ok"])
        self.assertFalse(health["memoryProjection"]["ok"])
        self.assertFalse(health["memoryProjection"]["running"])
        self.assertIn(
            "thread creation failed",
            health["memoryProjection"]["lastError"],
        )
        service.close()
        self.assertEqual(failing_worker.stop_calls, 1)

    def test_agent_gateway_does_not_compete_for_projection_ownership(self) -> None:
        service = DebugImeService(
            DebugServerConfig(
                db_path=self.db_path,
                core=LocalSqliteCoreClient(self.db_path),
                seed_if_empty=False,
                server_name="agent gateway",
            )
        )
        self.addCleanup(service.close)

        service.start_background_services()
        status = service.memory_projection_status()

        self.assertFalse(status["configured"])
        self.assertFalse(status["running"])
        self.assertEqual(status["disabledReason"], "not_projection_owner")

    def test_only_sidecar_is_default_projection_owner(self) -> None:
        sidecar = DebugServerConfig(
            db_path=self.db_path,
            server_name="sidecar server",
        )
        debug = DebugServerConfig(
            db_path=self.db_path,
            server_name="debug server",
        )
        gateway = DebugServerConfig(
            db_path=self.db_path,
            server_name="agent gateway",
        )

        with patch.dict("rag_ime.debug_server.os.environ", {}, clear=True):
            self.assertTrue(_memory_projection_worker_enabled(sidecar))
            self.assertFalse(_memory_projection_worker_enabled(debug))
            self.assertFalse(_memory_projection_worker_enabled(gateway))

        with patch.dict(
            "rag_ime.debug_server.os.environ",
            {"RAG_IME_MEMORY_PROJECTION_WORKER": "1"},
            clear=True,
        ):
            self.assertTrue(_memory_projection_worker_enabled(sidecar))
            self.assertFalse(_memory_projection_worker_enabled(debug))
            self.assertFalse(_memory_projection_worker_enabled(gateway))

        with patch.dict(
            "rag_ime.debug_server.os.environ",
            {"RAG_IME_DEBUG_MEMORY_PROJECTION_WORKER": "1"},
            clear=True,
        ):
            self.assertTrue(_memory_projection_worker_enabled(debug))
            self.assertFalse(_memory_projection_worker_enabled(gateway))

    def test_http_server_lifecycle_starts_and_closes_service(self) -> None:
        service = Mock()
        server = Mock()
        server.serve_forever.side_effect = KeyboardInterrupt
        config = DebugServerConfig(
            db_path=self.db_path,
            seed_if_empty=False,
            server_name="sidecar server",
        )
        with (
            patch("rag_ime.debug_server.DebugImeService", return_value=service),
            patch(
                "rag_ime.debug_server.QuietThreadingHTTPServer",
                return_value=server,
            ),
        ):
            run_debug_server(config)

        service.start_background_services.assert_called_once_with()
        server.serve_forever.assert_called_once_with()
        server.server_close.assert_called_once_with()
        service.close.assert_called_once_with()

    def test_blocking_embedding_warmup_does_not_block_gateway_health_or_sessions(self) -> None:
        provider = _BlockingWarmupEmbeddingProvider()
        constructed = Event()
        result: dict[str, DebugImeService] = {}

        def construct() -> None:
            with patch(
                "rag_ime.debug_server.embedding_provider_from_env",
                return_value=provider,
            ):
                result["service"] = DebugImeService(
                    DebugServerConfig(
                        db_path=self.db_path,
                        seed_if_empty=False,
                        server_name="agent gateway",
                    )
                )
            constructed.set()

        constructor = threading.Thread(target=construct, daemon=True)
        constructor.start()
        try:
            self.assertTrue(
                constructed.wait(timeout=5),
                "embedding warmup blocked DebugImeService construction before bind",
            )
        finally:
            provider.release.set()
            constructor.join(timeout=5)
        service = result["service"]
        self.addCleanup(service.close)
        self.assertEqual(service.health()["embeddingWarmup"]["status"], "pending")

        class Handler(DebugRequestHandler):
            pass

        Handler.service = service
        Handler.static_dir = Path(self.temporary.name)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        try:
            provider.release.clear()
            service.start_background_services()
            thread.start()
            self.assertTrue(provider.entered.wait(timeout=1))
            base_url = f"http://127.0.0.1:{server.server_port}"
            with urlopen(f"{base_url}/api/health", timeout=2) as response:
                health = json.loads(response.read().decode("utf-8"))
            with urlopen(
                f"{base_url}/api/agent/sessions?limit=1",
                timeout=2,
            ) as response:
                sessions = json.loads(response.read().decode("utf-8"))

            self.assertEqual(health["embeddingWarmup"]["status"], "running")
            self.assertTrue(sessions["ok"])
            self.assertEqual(sessions["schemaVersion"], "rag-ime.agent-session-list.v1")
        finally:
            provider.release.set()
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertTrue(_wait_until(
            lambda: service.health()["embeddingWarmup"]["status"] == "complete",
            timeout_s=2,
        ))
        self.assertTrue(service.health()["embeddingWarmup"]["ok"])

    def test_blocking_startup_reconciliation_does_not_block_health_or_sessions(
        self,
    ) -> None:
        reconciliation_entered = Event()
        reconciliation_release = Event()
        constructed = Event()
        result: dict[str, DebugImeService] = {}

        def blocking_reconcile(_service: object, **_kwargs: object) -> None:
            reconciliation_entered.set()
            if not reconciliation_release.wait(timeout=15):
                raise TimeoutError("startup reconciliation was not released")

        def construct() -> None:
            result["service"] = DebugImeService(
                DebugServerConfig(
                    db_path=self.db_path,
                    seed_if_empty=False,
                    server_name="agent gateway",
                )
            )
            constructed.set()

        with patch(
            "rag_ime.work_documents.WorkDocumentService.reconcile",
            new=blocking_reconcile,
        ):
            constructor = threading.Thread(target=construct, daemon=True)
            constructor.start()
            try:
                self.assertTrue(
                    constructed.wait(timeout=5),
                    "startup reconciliation blocked service construction before bind",
                )
                service = result["service"]
                self.addCleanup(service.close)

                class Handler(DebugRequestHandler):
                    pass

                Handler.service = service
                Handler.static_dir = Path(self.temporary.name)
                server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                service.start_background_services()
                self.assertTrue(reconciliation_entered.wait(timeout=2))
                base_url = f"http://127.0.0.1:{server.server_port}"
                with urlopen(f"{base_url}/api/health", timeout=2) as response:
                    health = json.loads(response.read().decode("utf-8"))
                with urlopen(
                    f"{base_url}/api/agent/sessions?limit=1",
                    timeout=2,
                ) as response:
                    sessions = json.loads(response.read().decode("utf-8"))

                self.assertTrue(health["ok"])
                self.assertEqual(health["startupRecovery"]["status"], "running")
                self.assertTrue(sessions["ok"])
                self.assertEqual(
                    sessions["schemaVersion"],
                    "rag-ime.agent-session-list.v1",
                )
            finally:
                reconciliation_release.set()
                constructor.join(timeout=5)
                if "server" in locals():
                    server.shutdown()
                    thread.join(timeout=2)
                    server.server_close()

        self.assertTrue(
            _wait_until(
                lambda: service.health()["startupRecovery"]["status"]
                == "complete",
                timeout_s=2,
            )
        )

    def test_heavy_startup_jobs_use_one_serial_background_lane(self) -> None:
        provider = _BlockingWarmupEmbeddingProvider()
        maintenance_entered = Event()
        maintenance_release = Event()
        with patch(
            "rag_ime.debug_server.embedding_provider_from_env",
            return_value=provider,
        ):
            service = DebugImeService(
                DebugServerConfig(
                    db_path=self.db_path,
                    seed_if_empty=False,
                    server_name="agent gateway",
                )
            )
        self.addCleanup(service.close)
        run_maintenance = service.core.run_startup_maintenance

        def blocking_maintenance() -> None:
            maintenance_entered.set()
            if not maintenance_release.wait(timeout=15):
                raise TimeoutError("startup maintenance was not released")
            run_maintenance()

        service.core.run_startup_maintenance = blocking_maintenance  # type: ignore[method-assign]
        service.start_background_services()
        try:
            self.assertTrue(maintenance_entered.wait(timeout=2))
            self.assertEqual(
                service.embedding_warmup_status()["status"],
                "pending",
            )
            self.assertFalse(provider.entered.wait(timeout=0.1))
        finally:
            maintenance_release.set()

        self.assertTrue(provider.entered.wait(timeout=2))
        self.assertEqual(service.embedding_warmup_status()["status"], "running")
        provider.release.set()
        self.assertTrue(
            _wait_until(
                lambda: service.embedding_warmup_status()["status"]
                == "complete",
                timeout_s=2,
            )
        )

    def test_repeated_health_session_and_room_reads_reuse_open_schema(self) -> None:
        service = DebugImeService(
            DebugServerConfig(
                db_path=self.db_path,
                seed_if_empty=False,
                server_name="agent gateway",
            )
        )
        self.addCleanup(service.close)
        session_store = service.agent.sessions
        room_store = service.agent.rooms
        core = service.core
        foreground_thread = threading.get_ident()
        service_database = self.db_path.resolve()
        connect = sqlite3.connect
        observed_connections: list[dict[str, object]] = []

        def observe_connection(*args, **kwargs):
            database = args[0] if args else kwargs["database"]
            # Other tests can leave background work alive. Keep every foreground
            # open and every open of this service's database, on any thread.
            if (
                threading.get_ident() == foreground_thread
                or Path(str(database)).resolve() == service_database
            ):
                observed_connections.append({
                    "database": str(database),
                    "thread": threading.current_thread().name,
                })
            return connect(*args, **kwargs)

        def read_unrelated_database() -> None:
            with closing(sqlite3.connect(self.db_path.with_name("unrelated.sqlite"))) as conn:
                conn.execute("SELECT 1").fetchone()

        with (
            patch.object(
                session_store,
                "_connect",
                wraps=session_store._connect,
            ) as session_connections,
            patch.object(
                room_store,
                "_connect",
                wraps=room_store._connect,
            ) as room_connections,
            patch.object(
                core,
                "_connect",
                wraps=core._connect,
            ) as core_connections,
            patch(
                "sqlite3.connect",
                side_effect=observe_connection,
            ),
            ThreadPoolExecutor(max_workers=1) as unrelated_work,
        ):
            unrelated_read = unrelated_work.submit(read_unrelated_database)
            for _ in range(2):
                health = service.health()
                sessions = service.agent.list_sessions(
                    {"limit": 20, "projectionOnly": True}
                )
                rooms = service.agent.list_rooms(
                    {"limit": 20, "projectionOnly": True}
                )
                self.assertTrue(health["ok"])
                self.assertTrue(sessions["ok"])
                self.assertTrue(rooms["ok"])
            unrelated_read.result(timeout=2)

        self.assertEqual(session_connections.call_count, 0)
        self.assertEqual(room_connections.call_count, 0)
        self.assertEqual(core_connections.call_count, 0)
        self.assertEqual(observed_connections, [])

    def test_startup_recovery_failure_is_observable_without_breaking_health(
        self,
    ) -> None:
        with patch(
            "rag_ime.work_documents.WorkDocumentService.reconcile",
            side_effect=RuntimeError("recovery failed"),
        ):
            service = DebugImeService(
                DebugServerConfig(
                    db_path=self.db_path,
                    seed_if_empty=False,
                    server_name="agent gateway",
                )
            )
            self.addCleanup(service.close)
            self.assertEqual(service.startup_recovery_status()["status"], "pending")
            service.start_background_services()
            self.assertTrue(
                _wait_until(
                    lambda: service.startup_recovery_status()["status"]
                    == "failed",
                    timeout_s=2,
                )
            )

        health = service.health()
        self.assertTrue(health["ok"])
        self.assertEqual(health["startupRecovery"]["status"], "failed")
        self.assertEqual(health["startupRecovery"]["error"], "RuntimeError")

    def test_gateway_owned_agent_recovery_is_skipped_in_sidecar(self) -> None:
        with (
            patch.dict(
                "rag_ime.debug_server.os.environ",
                {"RAG_IME_AGENT_GATEWAY_ENABLED": "1"},
            ),
            patch(
                "rag_ime.work_documents.WorkDocumentService.reconcile",
                side_effect=AssertionError(
                    "passive Sidecar must not run Agent recovery"
                ),
            ),
        ):
            service = DebugImeService(
                DebugServerConfig(
                    db_path=self.db_path,
                    seed_if_empty=False,
                    server_name="sidecar server",
                )
            )
            self.addCleanup(service.close)
            core_maintenance = Mock(
                side_effect=AssertionError(
                    "passive Sidecar must not run Core maintenance"
                )
            )
            service.core.run_startup_maintenance = core_maintenance  # type: ignore[method-assign]
            service.start_background_services()
            self.assertTrue(
                _wait_until(
                    lambda: service.startup_recovery_status()["status"]
                    == "complete",
                    timeout_s=2,
                )
            )

        agent_recovery = service.startup_recovery_status()["components"][
            "agentRecovery"
        ]
        self.assertFalse(agent_recovery["enabled"])
        self.assertEqual(agent_recovery["status"], "complete")
        self.assertEqual(agent_recovery["skippedReason"], "not_execution_owner")
        core_recovery = service.startup_recovery_status()["components"][
            "coreMaintenance"
        ]
        self.assertFalse(core_recovery["enabled"])
        self.assertEqual(core_recovery["skippedReason"], "not_execution_owner")
        core_maintenance.assert_not_called()
        service.close()

    def test_embedding_warmup_failure_is_visible_without_breaking_health(self) -> None:
        provider = _FailingWarmupEmbeddingProvider()
        with patch(
            "rag_ime.debug_server.embedding_provider_from_env",
            return_value=provider,
        ):
            service = DebugImeService(
                DebugServerConfig(
                    db_path=self.db_path,
                    seed_if_empty=False,
                    server_name="agent gateway",
                )
            )
        self.addCleanup(service.close)

        self.assertEqual(service.health()["embeddingWarmup"]["status"], "pending")
        service.start_background_services()
        self.assertTrue(_wait_until(
            lambda: service.health()["embeddingWarmup"]["status"] == "failed",
            timeout_s=2,
        ))
        health = service.health()
        self.assertTrue(health["ok"])
        self.assertFalse(health["embeddingWarmup"]["ok"])
        self.assertEqual(health["embeddingWarmup"]["error"], "RuntimeError")

    def test_embedding_warmup_delay_stays_pending_before_running(self) -> None:
        provider = _BlockingWarmupEmbeddingProvider()
        with (
            patch(
                "rag_ime.debug_server.embedding_provider_from_env",
                return_value=provider,
            ),
            patch.dict(
                "rag_ime.debug_server.os.environ",
                {"RAG_IME_EMBEDDING_WARMUP_DELAY_SECONDS": "0.2"},
            ),
        ):
            service = DebugImeService(
                DebugServerConfig(
                    db_path=self.db_path,
                    seed_if_empty=False,
                    server_name="agent gateway",
                )
            )
        self.addCleanup(service.close)

        started = time.perf_counter()
        service.start_background_services()
        elapsed = time.perf_counter() - started
        delayed = service.health()["embeddingWarmup"]

        self.assertLess(elapsed, 0.1)
        self.assertEqual(delayed["status"], "pending")
        self.assertEqual(delayed["delayMs"], 200)
        self.assertFalse(provider.entered.wait(timeout=0.05))
        self.assertTrue(provider.entered.wait(timeout=1))
        self.assertEqual(
            service.health()["embeddingWarmup"]["status"],
            "running",
        )
        provider.release.set()
        self.assertTrue(
            _wait_until(
                lambda: service.health()["embeddingWarmup"]["status"]
                == "complete",
                timeout_s=2,
            )
        )

    def test_close_cancels_pending_embedding_warmup_delay(self) -> None:
        provider = _BlockingWarmupEmbeddingProvider()
        with (
            patch(
                "rag_ime.debug_server.embedding_provider_from_env",
                return_value=provider,
            ),
            patch.dict(
                "rag_ime.debug_server.os.environ",
                {"RAG_IME_EMBEDDING_WARMUP_DELAY_SECONDS": "0.15"},
            ),
        ):
            service = DebugImeService(
                DebugServerConfig(
                    db_path=self.db_path,
                    seed_if_empty=False,
                    server_name="agent gateway",
                )
            )

        service.start_background_services()
        pending = service.health()["embeddingWarmup"]
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["delayMs"], 150)
        service.close()

        self.assertFalse(provider.entered.wait(timeout=0.25))

    def test_delayed_embedding_warmup_failure_is_visible(self) -> None:
        provider = _FailingWarmupEmbeddingProvider()
        with (
            patch(
                "rag_ime.debug_server.embedding_provider_from_env",
                return_value=provider,
            ),
            patch.dict(
                "rag_ime.debug_server.os.environ",
                {"RAG_IME_EMBEDDING_WARMUP_DELAY_SECONDS": "1"},
            ),
        ):
            service = DebugImeService(
                DebugServerConfig(
                    db_path=self.db_path,
                    seed_if_empty=False,
                    server_name="agent gateway",
                )
            )
        self.addCleanup(service.close)

        service.start_background_services()
        pending = service.embedding_warmup_status()
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["delayMs"], 1_000)
        self.assertFalse(provider.entered.wait(timeout=0.1))
        self.assertTrue(provider.entered.wait(timeout=2))
        self.assertTrue(
            _wait_until(
                lambda: service.health()["embeddingWarmup"]["status"]
                == "failed",
                timeout_s=2,
            )
        )
        self.assertEqual(
            service.health()["embeddingWarmup"]["error"],
            "RuntimeError",
        )

    def test_embedding_warmup_delay_is_bounded_nonnegative(self) -> None:
        reports: list[dict[str, object]] = []
        for configured in ("-5", "999999"):
            provider = _BlockingWarmupEmbeddingProvider()
            with (
                patch(
                    "rag_ime.debug_server.embedding_provider_from_env",
                    return_value=provider,
                ),
                patch.dict(
                    "rag_ime.debug_server.os.environ",
                    {"RAG_IME_EMBEDDING_WARMUP_DELAY_SECONDS": configured},
                ),
            ):
                service = DebugImeService(
                    DebugServerConfig(
                        db_path=Path(self.temporary.name) / f"delay-{configured}.sqlite",
                        seed_if_empty=False,
                        server_name="agent gateway",
                    )
                )
            reports.append(service.health()["embeddingWarmup"])
            service.close()

        self.assertEqual(reports[0]["delayMs"], 0)
        self.assertEqual(reports[1]["delayMs"], 300_000)


class _FailingProjectionWorker:
    def __init__(self) -> None:
        self.stop_calls = 0

    def start(self) -> None:
        raise RuntimeError("thread creation failed")

    def stop(self) -> None:
        self.stop_calls += 1

    def status(self) -> dict[str, object]:
        return {
            "running": False,
            "projectionKinds": [],
            "lastRunAtMs": 0,
            "lastError": "",
            "lastReport": {},
        }


class _BlockingWarmupEmbeddingProvider:
    fingerprint = "mlx-bert:test-blocking:cfg-test"

    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()

    def embed_query(self, _text: str) -> list[float]:
        self.entered.set()
        if not self.release.wait(timeout=15):
            raise TimeoutError("blocking embedding warmup was not released")
        return [0.25, 0.75]


class _FailingWarmupEmbeddingProvider:
    fingerprint = "mlx-bert:test-failing:cfg-test"

    def __init__(self) -> None:
        self.entered = Event()

    def embed_query(self, _text: str) -> list[float]:
        self.entered.set()
        raise RuntimeError("embedding warmup failed")


def _insert_phrase(conn: sqlite3.Connection, memory_id: str, text: str) -> None:
    timestamp = int(time.time() * 1_000)
    upsert_memory_item(
        conn,
        memory_id=memory_id,
        kind="phrase",
        text=text,
        normalized_text=normalize_text(text),
        summary="",
        source_event_id=None,
        project="rag-ime",
        app="",
        confidence=0.9,
        quality_score=0.9,
        status="active",
        privacy_class="local",
        created_at_ms=timestamp,
        updated_at_ms=timestamp,
        metadata={"source": "lifecycle-test"},
        tags=("projection",),
        embedding_provider=None,
    )


def _projection_applied(core: LocalSqliteCoreClient, source_id: str) -> bool:
    with core._connect() as conn:  # type: ignore[attr-defined]
        row = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM memory_retrieval_docs WHERE source_id = ?),
                (SELECT COUNT(*) FROM memory_projection_outbox WHERE state = 'applied')
            """,
            (source_id,),
        ).fetchone()
    return int(row[0] or 0) == 1 and int(row[1] or 0) >= 1


def _wait_until(predicate: object, *, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        time.sleep(0.02)
    return False


if __name__ == "__main__":
    unittest.main()
