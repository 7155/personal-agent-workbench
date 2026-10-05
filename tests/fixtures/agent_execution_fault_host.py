"""Private loopback host used by the Agent execution fault tests.

This process is deliberately a test transport. It owns a real
``AgentLabTrialApplication`` and ``AgentLabTrialStore`` while keeping its
effect journal in a second SQLite database. It has no Provider, service, or
credential access and emits only one bounded readiness line on stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import signal
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_STARTED = time.monotonic()


def _startup_phase(phase: str) -> None:
    print(f"startup +{time.monotonic() - _STARTED:.3f}s: {phase}", file=sys.stderr, flush=True)


_startup_phase("loading runtime modules")

from rag_ime.agent_lab.trial_execution import (
    AgentLabTrialExecutionInterrupted,
    AgentLabTrialApplication,
    TrialObserver,
)
from rag_ime.agent_lab.trials import (
    AgentLabTrialConflict,
    AgentLabTrialStore,
)


_startup_phase("runtime modules ready")
_MAX_BODY_BYTES = 64 * 1024


def _json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )


class EffectAdapter:
    """A deterministic adapter with one durable side effect per execution."""

    def __init__(self, effects_path: Path, *, hold_terminal: bool) -> None:
        self.effects_path = effects_path
        self.hold_terminal = hold_terminal
        self._release = threading.Event()
        self._write_lock = threading.Lock()
        self._initialize()

    def _initialize(self) -> None:
        self.effects_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.effects_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS effects (
                    effect_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_key TEXT NOT NULL,
                    input_sha256 TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL
                )
                """
            )

    def prepare(self, spec: dict[str, Any], job_id: str) -> dict[str, object]:
        request_key = spec.get("requestKey")
        input_value = spec.get("input")
        if (
            not isinstance(request_key, str)
            or not request_key
            or len(request_key) > 256
            or not isinstance(input_value, str)
            or not input_value
            or len(input_value) > 256
        ):
            raise ValueError("effect fixture requires bounded requestKey and input")
        input_sha256 = hashlib.sha256(input_value.encode("utf-8")).hexdigest()
        return {
            "publicSpec": {"requestKey": request_key, "input": input_value},
            "privateInput": {
                "requestKey": request_key,
                "inputSha256": input_sha256,
                "jobId": job_id,
            },
        }

    def execute(
        self,
        private_input: dict[str, object],
        observer: TrialObserver,
        cancelled,
    ) -> dict[str, object]:
        del observer
        if cancelled():
            raise AgentLabTrialExecutionInterrupted("fixture execution cancelled")
        with self._write_lock, sqlite3.connect(self.effects_path) as connection:
            connection.execute(
                "INSERT INTO effects(request_key,input_sha256,job_id,created_at_ms) VALUES(?,?,?,?)",
                (
                    str(private_input["requestKey"]),
                    str(private_input["inputSha256"]),
                    str(private_input["jobId"]),
                    time.time_ns() // 1_000_000,
                ),
            )
        if self.hold_terminal:
            while not self._release.wait(0.02):
                if cancelled():
                    raise AgentLabTrialExecutionInterrupted(
                        "fixture terminal settlement cancelled"
                    )
        return {
            "status": "completed",
            "requestKey": str(private_input["requestKey"]),
            "effect": "inserted",
        }


class FaultHostState:
    def __init__(self, app: AgentLabTrialApplication, effects_path: Path) -> None:
        self.app = app
        self.effects_path = effects_path

    def effects(self, request_key: str) -> dict[str, object]:
        with sqlite3.connect(self.effects_path) as connection:
            rows = connection.execute(
                "SELECT job_id FROM effects WHERE request_key=? ORDER BY effect_id",
                (request_key,),
            ).fetchall()
        return {
            "requestKey": request_key,
            "count": len(rows),
            "jobIds": [str(row[0]) for row in rows],
        }


class FaultHostHandler(BaseHTTPRequestHandler):
    state: FaultHostState

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _write_json(self, status: int, body: object) -> None:
        payload = _json(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _drop_response(self) -> None:
        self.close_connection = True
        try:
            self.connection.shutdown(2)
        except OSError:
            pass
        try:
            self.connection.close()
        except OSError:
            pass

    def _body(self) -> dict[str, object]:
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("fixture request length is invalid") from exc
        if not 0 <= length <= _MAX_BODY_BYTES:
            raise ValueError("fixture request body is too large")
        raw = self.rfile.read(length)
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("fixture request must be a JSON object")
        return value

    def _wait_terminal(self, job_id: str) -> dict[str, object]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.state.app.read(job_id)
            job = result.get("job")
            if isinstance(job, dict) and job.get("state") in {
                "completed",
                "failed",
                "cancelled",
                "interrupted",
            }:
                return result
            time.sleep(0.01)
        raise TimeoutError("fixture trial did not reach a terminal state")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/health":
                self._write_json(200, {"ready": True})
                return
            if parsed.path == "/trials":
                job_id = str((query.get("jobId") or [""])[0])
                self._write_json(200, self.state.app.read(job_id))
                return
            if parsed.path == "/effects":
                request_key = str((query.get("requestKey") or [""])[0])
                if not request_key:
                    raise ValueError("requestKey is required")
                self._write_json(200, self.state.effects(request_key))
                return
            self._write_json(404, {"error": "not found"})
        except Exception:
            self._write_json(500, {"error": "fixture request failed"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/trials/start":
            self._write_json(404, {"error": "not found"})
            return
        try:
            body = self._body()
            if set(body) != {"clientRequestId", "sceneId", "spec"}:
                raise ValueError("fixture trial fields are invalid")
            client_request_id = body["clientRequestId"]
            scene_id = body["sceneId"]
            spec = body["spec"]
            if (
                not isinstance(client_request_id, str)
                or not isinstance(scene_id, str)
                or not isinstance(spec, dict)
            ):
                raise ValueError("fixture trial body is invalid")
            admission = self.state.app.start(client_request_id, scene_id, spec)
            job = admission.get("job")
            if not isinstance(job, dict):
                raise ValueError("fixture trial admission is malformed")
            if (parse_qs(parsed.query).get("drop") or [""])[0] == "1":
                self._wait_terminal(str(job["jobId"]))
                self._drop_response()
                return
            self._write_json(202, admission)
        except AgentLabTrialConflict as exc:
            self._write_json(409, exc.response_payload())
        except (TypeError, ValueError):
            self._write_json(422, {"error": "fixture trial request is invalid"})
        except Exception:
            self._write_json(500, {"error": "fixture trial request failed"})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--effects-db", required=True)
    parser.add_argument("--hold-terminal", action="store_true")
    args = parser.parse_args()

    _startup_phase("initializing trial database")
    store = AgentLabTrialStore(Path(args.db))
    _startup_phase("trial database ready")
    adapter = EffectAdapter(Path(args.effects_db), hold_terminal=args.hold_terminal)
    _startup_phase("effect journal ready")
    app = AgentLabTrialApplication(store, {"effect": adapter}, max_workers=4)
    _startup_phase("application ready")
    state = FaultHostState(app, adapter.effects_path)
    FaultHostHandler.state = state
    _startup_phase("binding HTTP server")
    server = ThreadingHTTPServer(("127.0.0.1", 0), FaultHostHandler)
    _startup_phase("HTTP server ready")

    def stop(_signum: int, _frame: object) -> None:
        # ``shutdown`` must run away from the thread currently in
        # ``serve_forever``; signal handlers execute on that same main thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(json.dumps({"ready": True, "port": server.server_port}), flush=True)
    try:
        server.serve_forever(poll_interval=0.05)
    finally:
        server.server_close()
        app.close()


if __name__ == "__main__":
    main()
