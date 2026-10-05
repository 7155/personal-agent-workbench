"""Offline contract tests only. No provider, gateway server, auth, or file writes."""
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from rag_ime.agent_lab import acceptance_trace as module

class TraceTests(unittest.TestCase):
    def setUp(self):
        class Harness:
            def read(self, session, args):
                return {"content":"7"}
        class Gateway:
            workspace_harness = Harness()
            def execute(self, payload):
                result = self.workspace_harness.read({"id":payload["sessionId"]},payload["args"])
                return {"ok":True,"result":result}
        self.gateway = Gateway()
        self.trace = module.GatewayTrace()
        self.trace.install(SimpleNamespace(), self.gateway)
        self.root = Path("/synthetic-offline-not-created")
        self.trace.configs["s"] = {"workspace":self.root,"reads":{str(self.root/"a.json"):{"index":0,"value":7,"delayMs":50}}}
        self.patcher = patch.object(module,"hashes",return_value={"a.json":"synthetic-hash"})
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self.trace.close)

    def call(self, call="c", session="s", filename="a.json"):
        return self.gateway.execute({"sessionId":session,"toolCallId":call,"tool":"read","args":{"path":filename},
                                     "executionBinding":{"turnId":"t","clientMessageId":"m"}})

    def test_keeps_real_identity_and_original_result(self):
        self.assertEqual(self.call(), {"ok":True,"result":{"content":"7"}})
        row = self.trace.rows[0]
        self.assertEqual((row["sessionId"],row["toolCallId"],row["binding"]["turnId"]),("s","c","t"))
        self.assertGreater(row["fixtureEndedNs"],row["fixtureStartedNs"])

    def test_fixture_error_is_not_success(self):
        self.trace.configs["s"]["reads"][str(self.root/"a.json")]["errorCode"] = "FIXTURE_E_TRANSIENT"
        with self.assertRaisesRegex(ValueError,"FIXTURE_E_TRANSIENT"):
            self.call()
        self.assertFalse(self.trace.rows[0]["ok"])
        self.assertEqual(self.trace.rows[0]["fixtureErrorCode"],"FIXTURE_E_TRANSIENT")

    def test_threads_overlap_without_mixing_call_ids(self):
        threads = [threading.Thread(target=self.call,args=(str(i),)) for i in range(3)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual({r["toolCallId"] for r in self.trace.rows},{"0","1","2"})
        self.assertLess(max(r["fixtureStartedNs"] for r in self.trace.rows),min(r["fixtureEndedNs"] for r in self.trace.rows))

    def test_unregistered_session_is_not_injected(self):
        self.assertTrue(self.call(session="other")["ok"])
        self.assertEqual(self.trace.rows,[])

    def test_barrier_requires_three_actual_read_entries_and_preserves_overlap(self):
        config=self.trace.configs["s"]
        config.update(barrier=threading.Barrier(3,timeout=1),barrierIndices=set())
        for index,name in enumerate(("a.json","b.json","c.json")):
            config["reads"][str(self.root/name)]={"index":index,"value":index,"delayMs":50}
        threads=[threading.Thread(target=self.call,kwargs={"call":str(i),"filename":name})
                 for i,name in enumerate(("a.json","b.json","c.json"))]
        for t in threads:t.start()
        for t in threads:t.join()
        rows=self.trace.rows
        self.assertEqual({r["fixtureIndex"] for r in rows},{0,1,2})
        self.assertTrue(all(r.get("barrierReleased") for r in rows))
        self.assertLess(max(r["fixtureStartedNs"] for r in rows),min(r["fixtureEndedNs"] for r in rows))

    def test_barrier_timeout_cannot_be_reported_as_read_success(self):
        self.trace.configs["s"].update(barrier=threading.Barrier(3,timeout=0.01),barrierIndices=set())
        with self.assertRaises(threading.BrokenBarrierError):
            self.call()
        row=self.trace.rows[0]
        self.assertFalse(row["ok"])
        self.assertNotIn("fixtureStartedNs",row)
        self.assertNotIn("fixtureValue",row)

    def test_close_restores_original_methods(self):
        self.trace.close()
        self.call()
        self.assertEqual(self.trace.rows,[])

if __name__ == "__main__":
    unittest.main(verbosity=2)
