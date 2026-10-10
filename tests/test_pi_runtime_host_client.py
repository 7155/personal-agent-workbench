from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from rag_ime.pi.config import PiRuntimeConfig
from rag_ime.pi.host_client import PiRuntimeHostClient
from rag_ime.pi.values import PiRuntimeCommandAcceptanceUnknown, PiRuntimeCommandRejected
from rag_ime.room_runtime_host_kill_gate import RuntimeHostKillGate


WIRE_HOST = r'''#!/usr/bin/env python3
import json,sys
for line in sys.stdin:
    request=json.loads(line)
    method=request['method']
    if method in {'fatal', 'fatal_partial'}:
        if method=='fatal_partial':
            sys.stdout.write('{"event":"notice","payload":{"text":"' + 'x'*128000)
            sys.stdout.flush()
        print('FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory sk-secret123456 /Users/example/private ' + 'x'*700, file=sys.stderr, flush=True)
        for i in range(50):
            print(str(i)+': native stack frame [/usr/lib/dyld]', file=sys.stderr, flush=True)
        if method=='fatal_partial':
            sys.exit(17)
        break
    if method=='partial_noise':
        sys.stdout.write('{"event":"notice","payload":{"text":"public partial')
        sys.stdout.flush()
        print('ordinary shutdown note sk-secret123456 /Users/example/private', file=sys.stderr, flush=True)
        break
    if method=='drop':
        break
    if method=='timeout':
        continue
    if method=='event':
        print(json.dumps({'event':'notice','payload':{'step':1}}),flush=True)
    if method=='invalid':
        print('not-json',flush=True)
        break
    if method=='reject':
        response={'ok':False,'error':{'code':'DENIED','message':'rejected'}}
    else:
        response={'ok':True,'result':{'method':method,'params':request['params']}}
    response['id']=request['id']
    print(json.dumps(response),flush=True)
'''


class PiRuntimeHostClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="paw-host-wire-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        executable = root / "host"
        executable.write_text(WIRE_HOST)
        executable.chmod(0o755)
        self.gate = RuntimeHostKillGate(root / "gate.sqlite")
        self.gate.initialize()
        self.received = []
        self.exits = []
        self.client = PiRuntimeHostClient(
            PiRuntimeConfig(enabled=True, executable=executable, agent_dir=root / "agent", session_dir=root / "sessions", logs_dir=root / "logs", command_timeout_seconds=5),
            on_event=self.received.append, on_exit=lambda code, error: self.exits.append((code,error)),
            kill_gate=self.gate, owner_instance_id="test:wire",
        )
        self.addCleanup(self.client.stop)
        self.assertEqual(self.client.start()["method"], "hello")

    def test_response_lane_progresses_while_observer_waits_then_drains_in_order(self):
        entered, release, delivered = threading.Event(), threading.Event(), threading.Event()
        def observer(event):
            entered.set()
            release.wait(3)
            self.received.append(event)
            delivered.set()
        self.client.on_event = observer
        try:
            response = self.client.send("event", {"text":"中文"})
            self.assertTrue(entered.wait(1))
            self.assertEqual(response["params"], {"text":"中文"})
            self.assertEqual(self.client.send("ping")["method"], "ping")
        finally:
            release.set()
        self.assertTrue(delivered.wait(1))
        self.client.stop()
        self.assertEqual(len(self.received), 1)
        self.assertFalse(self.client._event_thread.is_alive())

    def test_distinguishes_rejection_from_uncertain_timeout_and_eof(self):
        with self.assertRaises(PiRuntimeCommandRejected) as rejected:
            self.client.send("reject")
        self.assertEqual(rejected.exception.host_error_code, "DENIED")
        with self.assertRaises(PiRuntimeCommandAcceptanceUnknown):
            self.client.send("timeout", timeout=0.05)
        self.assertFalse(self.client._pending)
        with self.assertRaises(PiRuntimeCommandAcceptanceUnknown):
            self.client.send("drop")
        self.assertTrue(self.exits)
        self.assertFalse(self.client._pending)

    def test_before_write_failure_does_not_dispatch_and_clears_waiter(self):
        def cancel():
            raise ValueError("cancelled before write")
        with self.assertRaisesRegex(ValueError,"cancelled before write"):
            self.client.send("ping", before_write=cancel)
        self.assertFalse(self.client._pending)
        self.assertEqual(self.client.send("ping")["method"], "ping")

    def test_bad_json_exit_preserves_diagnostic_and_drains_waiters(self):
        with self.assertRaisesRegex(PiRuntimeCommandAcceptanceUnknown,"invalid.*JSONL"):
            self.client.send("invalid")
        self.assertIn("invalid Pi Runtime Host JSONL", self.exits[-1][1])
        self.assertFalse(self.client._pending)

    def test_fatal_summary_survives_long_native_stack_and_is_redacted_and_bounded(self):
        with self.assertRaises(PiRuntimeCommandAcceptanceUnknown):
            self.client.send("fatal")
        error = self.exits[-1][1]
        self.assertIn("FATAL ERROR: Reached heap limit", error)
        self.assertIn("JavaScript heap out of memory", error)
        self.assertNotIn("sk-secret123456", error)
        self.assertNotIn("/Users/example/private", error)
        self.assertLessEqual(len(error), 500)
        self.assertEqual(len(self.client._stderr), 32)
        self.client.stop()
        self.client.host_identity = "pi-host:test-restarted-generation"
        self.client.start()
        self.assertEqual(self.client.diagnostic_error(), "")

    def test_partial_json_eof_keeps_fatal_cause_redacted_bounded_and_acceptance_unknown(self):
        process = self.client._process
        with self.assertRaises(PiRuntimeCommandAcceptanceUnknown) as uncertain:
            self.client.send("fatal_partial")
        exit_code, error = self.exits[-1]
        self.assertEqual(exit_code, 17)
        self.assertIn("invalid Pi Runtime Host JSONL", error)
        self.assertIn("Unterminated string", error)
        self.assertIn("FATAL ERROR: Reached heap limit", error)
        self.assertIn("JavaScript heap out of memory", error)
        self.assertNotIn("sk-secret123456", error)
        self.assertNotIn("/Users/example/private", error)
        self.assertLessEqual(len(error), 500)
        self.assertEqual(str(uncertain.exception), error)
        self.assertFalse(self.client._pending)
        self.assertEqual(len(self.exits), 1)
        self.client.stop()
        self.assertEqual(process.poll(), 17)
        self.assertTrue(all(stream.closed for stream in (process.stdin, process.stdout, process.stderr)))
        self.assertFalse(self.client._stdout_thread.is_alive())
        self.assertFalse(self.client._stderr_thread.is_alive())
        self.assertFalse(self.client._event_thread.is_alive())

    def test_partial_json_does_not_promote_ordinary_stderr_to_fatal(self):
        with self.assertRaises(PiRuntimeCommandAcceptanceUnknown):
            self.client.send("partial_noise")
        self.assertIn("invalid Pi Runtime Host JSONL", self.exits[-1][1])
        self.assertNotIn("ordinary shutdown note", self.exits[-1][1])
        self.assertNotIn("sk-secret123456", self.exits[-1][1])
        self.assertEqual(self.client._fatal_error, "")
        self.assertFalse(self.client._pending)

    def test_registry_failure_closes_new_process_and_streams(self):
        self.client.stop()
        with patch.object(self.gate,"register_process",side_effect=RuntimeError("registry failed")):
            with self.assertRaisesRegex(RuntimeError,"registry failed"):
                self.client.start()
        self.assertFalse(self.client.running)
        self.assertIsNone(self.client._process)
