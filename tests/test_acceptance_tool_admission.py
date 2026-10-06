"""Synthetic concurrency only: no provider, credentials, or live budget ledger."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time
import unittest
from rag_ime.agent_lab.acceptance_budget import AcceptanceBudgetError, AcceptanceRunBudget

class AcceptanceToolAdmissionTests(unittest.TestCase):
    def test_parallel_admission_cannot_exceed_the_declared_tool_limit(self):
        class PreemptedCounter(int):
            def __add__(self, other):
                time.sleep(.002)  # Exercise the read/modify/write preemption window.
                return PreemptedCounter(super().__add__(other))
        budget = AcceptanceRunBudget(root=Path('/tmp/paw-synthetic-no-ledger'),
            reservation={'maxProviderRequests': 1, 'reservedUsd': 1}, max_tools=8)
        budget.tool_calls = PreemptedCounter(0)
        ready = threading.Barrier(24)
        def admit(_index):
            ready.wait(timeout=5)
            try:
                budget.check_tool_admission()
                return True
            except AcceptanceBudgetError:
                return False
        with ThreadPoolExecutor(max_workers=24) as pool:
            admitted = list(pool.map(admit, range(24)))
        self.assertEqual(sum(admitted), 8)
        self.assertEqual(budget.tool_calls, 24)
