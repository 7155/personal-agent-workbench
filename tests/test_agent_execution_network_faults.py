from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.client import RemoteDisconnected
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from tests.sqlite_fixtures import copy_current_database
from tests.subprocess_startup import StartupDiagnostics


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "agent_execution_fault_host.py"


class _FaultHost:
    """Real loopback transport around the fixture's Application process."""

    def __init__(self, root: Path, *, hold_terminal: bool = False) -> None:
        self.root = root
        self.db_path = root / "trials.sqlite"
        self.effects_path = root / "effects.sqlite"
        self.stdout_path = root / "host.stdout"
        self.stderr_path = root / "host.stderr"
        self.startup = StartupDiagnostics(root / "startup-diagnostics")
        self.process: subprocess.Popen[bytes] | None = None
        try:
            self.port = self._start(hold_terminal=hold_terminal)
        except BaseException:
            # Readiness failure must not leave a fixture process behind while
            # the caller unwinds its temporary directory.
            self.stop()
            raise

    def _start(self, *, hold_terminal: bool) -> int:
        command = [
            sys.executable,
            str(FIXTURE),
            "--db",
            str(self.db_path),
            "--effects-db",
            str(self.effects_path),
        ]
        if hold_terminal:
            command.append("--hold-terminal")
        stdout = self.stdout_path.open("wb")
        stderr = self.stderr_path.open("wb")
        try:
            self.process = subprocess.Popen(
                command,
                cwd=ROOT,
                env=self.startup.environment(),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
        finally:
            stdout.close()
            stderr.close()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            line = ""
            try:
                lines = self.stdout_path.read_text(encoding="utf-8").splitlines()
                line = lines[0] if lines else ""
            except FileNotFoundError:
                pass
            if line:
                ready = json.loads(line)
                if ready.get("ready") is True:
                    port = ready.get("port")
                    if type(port) is int and 1 <= port <= 65_535:
                        self.startup.ready()
                        return port
            if self.process.poll() is not None:
                raise AssertionError(self._host_logs())
            time.sleep(0.01)
        raise AssertionError("fixture host did not become ready\n" + self._host_logs())

    def _host_logs(self) -> str:
        returncode = self.process.poll() if self.process is not None else "not started"
        output = [f"fixture process returncode: {returncode}"]
        for path in (self.stdout_path, self.stderr_path):
            try:
                output.append(f"{path.name}: {path.read_text(encoding='utf-8')[-2000:]}")
            except FileNotFoundError:
                output.append(f"{path.name}: <missing>")
        return "\n".join(output)

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
        *,
        timeout: float = 2,
    ) -> tuple[int, dict[str, object]]:
        payload = None
        headers: dict[str, str] = {}
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=payload,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                return int(response.status), json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                payload = exc.read()
            finally:
                exc.close()
            return int(exc.code), json.loads(payload.decode("utf-8"))

    def start_trial(self, request_key: str, input_value: str, *, drop: bool = False):
        query = "?drop=1" if drop else ""
        return self.request(
            "POST",
            "/trials/start" + query,
            {
                "clientRequestId": request_key,
                "sceneId": "effect",
                "spec": {"requestKey": request_key, "input": input_value},
            },
        )

    def read_job(self, job_id: str) -> dict[str, object]:
        query = urlencode({"jobId": job_id})
        _status, body = self.request("GET", "/trials?" + query)
        return body

    def effects(self, request_key: str) -> dict[str, object]:
        query = urlencode({"requestKey": request_key})
        _status, body = self.request("GET", "/effects?" + query)
        return body

    def stop(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
        except (OSError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                try:
                    process.kill()
                except (OSError, ProcessLookupError):
                    pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except (OSError, ProcessLookupError):
                    pass
                process.wait(timeout=3)
        self.process = process


@contextmanager
def _managed_host(root: Path, *, hold_terminal: bool = False):
    host = _FaultHost(root, hold_terminal=hold_terminal)
    try:
        yield host
    finally:
        host.stop()


@contextmanager
def _temporary_host(*, prefix: str, hold_terminal: bool = False):
    # Nest the host scope inside the directory scope so host.stop() and its
    # wait/reap complete before TemporaryDirectory removes the SQLite files.
    with tempfile.TemporaryDirectory(prefix=prefix) as directory:
        root = Path(directory)
        # Schema construction is fixture setup; the child still initializes its Store.
        # F2 restarts via _managed_host and reopens this same database unchanged.
        copy_current_database(root / "trials.sqlite")
        with _managed_host(root, hold_terminal=hold_terminal) as host:
            yield root, host


def _wait_for_effects(host: _FaultHost, request_key: str, *, timeout: float = 5) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        latest = host.effects(request_key)
        if latest.get("count") == 1:
            return latest
        time.sleep(0.01)
    raise AssertionError(f"effect was not committed: {latest}")


def _wait_for_state(
    host: _FaultHost,
    job_id: str,
    states: set[str],
    *,
    timeout: float = 5,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        latest = host.read_job(job_id)
        job = latest.get("job")
        if isinstance(job, dict) and job.get("state") in states:
            return latest
        time.sleep(0.01)
    raise AssertionError(f"job did not reach {states}: {latest}")


@unittest.skipUnless(os.name == "posix", "requires a POSIX process host")
class AgentExecutionNetworkFaultTests(unittest.TestCase):
    """F1-F3 use a real TCP fixture, not the installed Gateway acceptance path."""

    def test_f1_dropped_terminal_response_replays_same_job_without_duplicate_effect(self) -> None:
        with _temporary_host(prefix="agent-execution-f1-") as (_root, host):
            request_key = "fault-f1"

            with self.assertRaises((RemoteDisconnected, URLError)):
                host.start_trial(request_key, "alpha", drop=True)

            effects = _wait_for_effects(host, request_key)
            job_id = str(effects["jobIds"][0])
            terminal = _wait_for_state(host, job_id, {"completed"})
            self.assertEqual(terminal["job"]["state"], "completed")

            status, replay = host.start_trial(request_key, "alpha")
            self.assertEqual(status, 202)
            self.assertTrue(replay["replayed"])
            self.assertEqual(replay["job"]["jobId"], job_id)
            self.assertEqual(host.effects(request_key)["count"], 1)

    def test_f2_sigkill_before_terminal_recovery_replays_interrupted_without_duplicate_effect(self) -> None:
        with _temporary_host(prefix="agent-execution-f2-", hold_terminal=True) as (root, first_host):
            request_key = "fault-f2"
            status, admission = first_host.start_trial(request_key, "beta")
            self.assertEqual(status, 202)
            job_id = str(admission["job"]["jobId"])
            _wait_for_effects(first_host, request_key)
            self.assertEqual(first_host.read_job(job_id)["job"]["state"], "running")

            process = first_host.process
            self.assertIsNotNone(process)
            os.kill(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
            self.assertIsNotNone(process.poll(), "SIGKILL did not stop the fixture host")

            with _managed_host(root, hold_terminal=True) as restarted:
                recovered = restarted.read_job(job_id)
                self.assertEqual(recovered["job"]["state"], "interrupted")
                status, replay = restarted.start_trial(request_key, "beta")
                self.assertEqual(status, 202)
                self.assertTrue(replay["replayed"])
                self.assertEqual(replay["job"]["jobId"], job_id)
                self.assertEqual(replay["job"]["state"], "interrupted")
                self.assertEqual(restarted.effects(request_key)["count"], 1)

    def test_f3_four_concurrent_same_key_requests_share_one_job_and_effect(self) -> None:
        with _temporary_host(prefix="agent-execution-f3-") as (_root, host):
            request_key = "fault-f3"
            with ThreadPoolExecutor(max_workers=4) as pool:
                responses = list(
                    pool.map(
                        lambda _index: host.start_trial(request_key, "gamma"),
                        range(4),
                    )
                )

            self.assertEqual({status for status, _body in responses}, {202})
            job_ids = {str(body["job"]["jobId"]) for _status, body in responses}
            self.assertEqual(len(job_ids), 1)
            job_id = next(iter(job_ids))
            _wait_for_state(host, job_id, {"completed"})
            effects = _wait_for_effects(host, request_key)
            self.assertEqual(effects["count"], 1)
            self.assertEqual(effects["jobIds"], [job_id])

            conflict_status, conflict = host.start_trial(request_key, "different-input")
            self.assertEqual(conflict_status, 409)
            self.assertEqual(conflict["reasonCode"], "request_id_conflict")
            self.assertEqual(host.effects(request_key)["count"], 1)


if __name__ == "__main__":
    unittest.main()
