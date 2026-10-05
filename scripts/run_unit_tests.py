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
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.unit_test_shards import (
    PlanError,
    audit,
    leaves,
    load_shard,
    make_plan,
    make_receipt,
    plan_digest,
    repository_tree,
    verify_receipts,
)


@contextmanager
def stack_watchdog(seconds: float, *, stream: TextIO | None = None) -> Iterator[None]:
    """Sample stacks from a Python diagnostic thread; never stop a test."""
    if seconds <= 0:
        raise ValueError("watchdog interval must be positive")
    output = stream if stream is not None else sys.stderr
    stop = threading.Event()

    def sample() -> None:
        while not stop.wait(seconds):
            try:
                print(f"Diagnostic stack watchdog ({seconds:g}s; tests continue)", file=output, flush=True)
                faulthandler.dump_traceback(file=output, all_threads=True)
            except (OSError, ValueError):
                return  # A closed diagnostic stream cannot invalidate test results.

    thread = threading.Thread(target=sample, name="paw-unit-stack-watchdog", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1.0)


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

    def _print_latest_problem(self, flavour: str, problems: list) -> None:
        # CI can reach its job deadline before unittest's final error summary.
        # Keep that summary and result semantics, but flush each traceback now.
        self.printErrorList(flavour, problems[-1:])
        self.stream.flush()

    def addError(self, test: unittest.TestCase, err: tuple) -> None:
        super().addError(test, err)
        self._print_latest_problem("ERROR", self.errors)

    def addFailure(self, test: unittest.TestCase, err: tuple) -> None:
        super().addFailure(test, err)
        self._print_latest_problem("FAIL", self.failures)

    def addSubTest(self, test: unittest.TestCase, subtest: unittest.TestCase, err: tuple | None) -> None:
        errors, failures = len(self.errors), len(self.failures)
        super().addSubTest(test, subtest, err)
        if len(self.errors) > errors:
            self._print_latest_problem("ERROR", self.errors)
        if len(self.failures) > failures:
            self._print_latest_problem("FAIL", self.failures)

    def _event(self, event: str, **fields: object) -> None:
        if self.timing is not None:
            self.timing.write(json.dumps({"event": event, **fields}) + "\n")
            self.timing.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tests", nargs="*", help="unittest module/class/test names; default: full discovery")
    parser.add_argument("--pattern", help="discovery filename pattern; default: test*.py")
    parser.add_argument("--timing-jsonl", type=Path, help="write per-test start/stop and final outcome")
    parser.add_argument("--watchdog-seconds", type=float, default=60, help="repeated stack dump interval; never kills or skips a test")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--write-shard-plan", type=Path, help="discover once and write an exhaustive module plan")
    modes.add_argument("--shard-plan", type=Path, help="load only this plan's assigned modules")
    modes.add_argument("--verify-shards", type=Path, help="require successful terminal receipts for the whole plan")
    parser.add_argument("--shard-count", type=int, help="number of complete module partitions; default: 12")
    parser.add_argument("--shard-index", type=int, help="zero-based assigned partition")
    parser.add_argument("--shard-receipt", type=Path, help="write this worker's terminal result")
    parser.add_argument("--shard-receipts", type=Path, help="directory containing downloaded shard-*.json receipts")
    parser.add_argument("--workflow-result", choices=("success", "failure", "cancelled", "skipped"))
    args = parser.parse_args()
    if args.watchdog_seconds <= 0:
        parser.error("--watchdog-seconds must be positive")
    if args.tests and (args.write_shard_plan or args.shard_plan or args.verify_shards):
        parser.error("named test selection cannot be combined with full-discovery sharding")
    if args.shard_count is not None and not args.write_shard_plan:
        parser.error("--shard-count requires --write-shard-plan")
    if args.shard_plan and (args.shard_index is None or args.shard_receipt is None):
        parser.error("--shard-plan requires --shard-index and --shard-receipt")
    if not args.shard_plan and (args.shard_index is not None or args.shard_receipt is not None):
        parser.error("worker options require --shard-plan")
    if args.verify_shards and (args.shard_receipts is None or args.workflow_result is None):
        parser.error("--verify-shards requires --shard-receipts and --workflow-result")
    if not args.verify_shards and (args.shard_receipts is not None or args.workflow_result is not None):
        parser.error("gate options require --verify-shards")
    timing = args.timing_jsonl.open("w", encoding="utf-8") if args.timing_jsonl else None
    started = time.monotonic()
    faulthandler.enable()
    with stack_watchdog(args.watchdog_seconds):
        try:
            if args.write_shard_plan:
                plan = make_plan(ROOT, args.shard_count if args.shard_count is not None else 12,
                                 pattern=args.pattern or "test*.py")
                _write_json(args.write_shard_plan, plan)
                print(json.dumps({**audit(plan), "shards": len(plan["shards"]),
                                  "manifestSha256": plan_digest(plan)}))
                return 0
            if args.verify_shards:
                plan = json.loads(args.verify_shards.read_text(encoding="utf-8"))
                audit(plan)
                if (plan["tree"] != repository_tree(ROOT) or plan["platform"] != sys.platform
                        or plan["python"] != list(sys.version_info[:2])):
                    raise PlanError("aggregate gate identity differs from discovery plan")
                if args.pattern is not None and args.pattern != plan["pattern"]:
                    raise PlanError("gate pattern differs from discovery plan")
                if not args.shard_receipts.is_dir():
                    raise PlanError("shard receipt directory is missing")
                receipts = [json.loads(path.read_text(encoding="utf-8"))
                            for path in sorted(args.shard_receipts.rglob("shard-*.json"))]
                print(json.dumps(verify_receipts(plan, receipts, args.workflow_result)))
                return 0
            plan = None
            loaded_case_ids = []
            if args.shard_plan:
                plan = json.loads(args.shard_plan.read_text(encoding="utf-8"))
                if args.pattern is not None and args.pattern != plan.get("pattern"):
                    raise PlanError("worker pattern differs from discovery plan")
                suite = load_shard(ROOT, plan, args.shard_index)
                loaded_case_ids = [case.id() for case in leaves(suite)]
            else:
                loader = unittest.TestLoader()
                suite = (loader.loadTestsFromNames(args.tests) if args.tests
                         else loader.discover(str(ROOT / "tests"), pattern=args.pattern or "test*.py"))
            print(f"Loaded {suite.countTestCases()} tests in {time.monotonic() - started:.3f}s", file=sys.stderr, flush=True)
            TimedResult.timing = timing
            unittest.installHandler()
            result = unittest.TextTestRunner(verbosity=2, resultclass=TimedResult).run(suite)
            if plan is not None:
                _write_json(args.shard_receipt, make_receipt(plan, args.shard_index, loaded_case_ids, result))
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
        except (PlanError, OSError, json.JSONDecodeError) as error:
            print(f"Unit runner could not complete: {error}", file=sys.stderr, flush=True)
            return 1
        finally:
            TimedResult.timing = None
            if timing is not None:
                timing.close()


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
