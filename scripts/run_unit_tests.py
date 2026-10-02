#!/usr/bin/env python3
"""Run ordinary unittest checks with timing and a non-terminating stack watchdog."""

from __future__ import annotations

import argparse
import faulthandler
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class TimedResult(unittest.TextTestResult):
    timing: TextIO | None = None

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.module_seconds: dict[str, float] = {}

    def startTest(self, test: unittest.TestCase) -> None:
        module = test.__class__.__module__
        if hasattr(self, "last_module") and module != self.last_module:
            print(f"Module completed {self.module_seconds[self.last_module]:.3f}s {self.last_module}", file=sys.stderr, flush=True)
        self.last_module = module
        self.started = time.monotonic()
        self._event("start", test=test.id())
        super().startTest(test)

    def stopTest(self, test: unittest.TestCase) -> None:
        elapsed = time.monotonic() - self.started
        module = test.__class__.__module__
        self.module_seconds[module] = self.module_seconds.get(module, 0) + elapsed
        self._event("stop", test=test.id(), seconds=elapsed, threads=threading.active_count())
        super().stopTest(test)

    def _event(self, event: str, **fields: object) -> None:
        if self.timing is not None:
            self.timing.write(json.dumps({"event": event, **fields}) + "\n")
            self.timing.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tests", nargs="*", help="unittest module/class/test names; default: full discovery")
    parser.add_argument("--pattern", default="test*.py", help="discovery filename pattern")
    parser.add_argument("--timing-jsonl", type=Path, help="write per-test start/stop and final outcome")
    parser.add_argument("--watchdog-seconds", type=float, default=60, help="repeated stack dump interval; never kills or skips a test")
    args = parser.parse_args()
    if args.watchdog_seconds <= 0:
        parser.error("--watchdog-seconds must be positive")
    timing = args.timing_jsonl.open("w", encoding="utf-8") if args.timing_jsonl else None
    started = time.monotonic()
    faulthandler.enable()
    faulthandler.dump_traceback_later(args.watchdog_seconds, repeat=True)
    try:
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromNames(args.tests) if args.tests else loader.discover(str(ROOT / "tests"), pattern=args.pattern)
        print(f"Loaded {suite.countTestCases()} tests in {time.monotonic() - started:.3f}s", file=sys.stderr, flush=True)
        TimedResult.timing = timing
        unittest.installHandler()
        result = unittest.TextTestRunner(verbosity=2, resultclass=TimedResult).run(suite)
        for module, elapsed in sorted(result.module_seconds.items(), key=lambda item: item[1], reverse=True):
            print(f"Module time {elapsed:.3f}s {module}", file=sys.stderr)
        if timing is not None:
            timing.write(json.dumps({"event": "result", "seconds": time.monotonic() - started,
                                    "tests": result.testsRun, "failures": len(result.failures),
                                    "errors": len(result.errors), "skipped": len(result.skipped),
                                    "interrupted": result.shouldStop}) + "\n")
        if result.shouldStop:
            print("Interrupted: suite is incomplete", file=sys.stderr)
            return 130
        return int(not result.wasSuccessful())
    finally:
        faulthandler.cancel_dump_traceback_later()
        TimedResult.timing = None
        if timing is not None:
            timing.close()


if __name__ == "__main__":
    raise SystemExit(main())
