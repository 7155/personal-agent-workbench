from __future__ import annotations

import copy
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from scripts import unit_test_shards as shards


ROOT = Path(__file__).resolve().parents[1]


def cases_in(suite: unittest.TestSuite):
    """Traverse independently of the planner's extraction and audit helpers."""
    for child in suite:
        if isinstance(child, unittest.TestSuite):
            yield from cases_in(child)
        else:
            yield child


class UnitTestShardContractTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="paw-shard-contract-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "tests").mkdir()
        self.prefix = "test_shard_contract_" + uuid.uuid4().hex + "_"
        self.original_path = sys.path[:]
        self.addCleanup(self.clear_modules)
        self.enterContext(patch.object(shards, "repository_tree", return_value="fixture-tree"))

    def clear_modules(self) -> None:
        for name in list(sys.modules):
            if name.startswith(self.prefix):
                del sys.modules[name]
        sys.path[:] = self.original_path
        importlib.invalidate_caches()

    def module(self, suffix: str, body: str) -> tuple[str, Path]:
        name = self.prefix + suffix
        path = self.root / "tests" / (name + ".py")
        path.write_text(body, encoding="utf-8")
        importlib.invalidate_caches()
        return name, path

    def simple_plan(self, count: int = 2) -> dict:
        for index in range(count):
            self.module(
                f"{index:02d}",
                "import unittest\nclass Case(unittest.TestCase):\n"
                " def test_first(self): pass\n def test_second(self): pass\n",
            )
        return shards.make_plan(self.root, count)

    def run_shard(self, plan: dict, index: int) -> tuple[dict, unittest.TestResult]:
        with patch.object(unittest.TestLoader, "discover", side_effect=AssertionError("worker rediscovered")):
            suite = shards.load_shard(self.root, plan, index)
        self.assertIs(type(suite), unittest.TestSuite)
        loaded = [case.id() for case in cases_in(suite)]
        expected = [case_id for unit in plan["shards"][index] for case_id in plan["units"][unit]["caseIds"]]
        self.assertEqual(loaded, expected)
        result = unittest.TestResult()
        suite.run(result)
        return shards.make_receipt(plan, index, loaded, result), result

    def passing_receipts(self, plan: dict) -> list[dict]:
        return [self.run_shard(plan, index)[0] for index in range(len(plan["shards"]))]

    def test_all_twelve_shards_cover_each_independently_discovered_occurrence_once(self) -> None:
        plan = self.simple_plan(12)
        baseline = unittest.TestLoader().discover(str(self.root / "tests"))
        baseline_ids = [case.id() for case in cases_in(baseline)]
        self.assertEqual(plan["schemaVersion"], 1)
        self.assertEqual(plan["totalTests"], 24)
        self.assertEqual(plan["baselineCaseIds"], baseline_ids)
        self.assertEqual(plan["baselineOrigins"], [unit["module"] for unit in plan["units"]])
        self.assertEqual(len(plan["shards"]), 12)
        self.assertTrue(all(plan["shards"]))
        self.assertEqual(Counter(index for shard in plan["shards"] for index in shard), Counter(range(12)))
        self.assertEqual(json.loads(json.dumps(plan)), plan)
        receipts = self.passing_receipts(plan)
        self.assertEqual(Counter(case for receipt in receipts for case in receipt["loadedCaseIds"]), Counter(baseline_ids))
        self.assertEqual(sum(receipt["testsRun"] for receipt in receipts), 24)
        self.assertIsInstance(shards.verify_receipts(plan, list(reversed(receipts)), "success"), dict)

    def test_failures_errors_and_skips_have_the_same_outcomes_as_unsharded_discovery(self) -> None:
        self.module(
            "a", "import unittest\nclass Case(unittest.TestCase):\n"
            " def test_ok(self): pass\n def test_bad(self): self.fail('failure sentinel')\n",
        )
        self.module(
            "b", "import unittest\nclass Case(unittest.TestCase):\n"
            " def test_error(self): raise RuntimeError('error sentinel')\n"
            " @unittest.skip('intentional skip')\n def test_skip(self): pass\n",
        )
        plan = shards.make_plan(self.root, 2)
        baseline_result = unittest.TestResult()
        unittest.TestLoader().discover(str(self.root / "tests")).run(baseline_result)
        executions = [self.run_shard(plan, index) for index in range(2)]
        self.assertEqual(sum(result.testsRun for _, result in executions), baseline_result.testsRun)
        for field in ("failures", "errors", "skipped"):
            self.assertEqual(sum(len(getattr(result, field)) for _, result in executions), len(getattr(baseline_result, field)), field)
        self.assertEqual([baseline_result.testsRun, len(baseline_result.failures), len(baseline_result.errors), len(baseline_result.skipped)], [4, 1, 1, 1])
        with self.assertRaises(shards.PlanError):
            shards.verify_receipts(plan, [receipt for receipt, _ in executions], "success")

    def test_a_b_a_module_and_class_fixture_lifetimes_preserve_intervening_unit(self) -> None:
        trace = self.root / "trace.txt"
        common = (
            "import unittest\nfrom pathlib import Path\n"
            f"def event(value):\n with Path({str(trace)!r}).open('a', encoding='utf-8') as stream: stream.write(value+'\\n')\n"
        )
        a, _ = self.module(
            "a", common + "def setUpModule(): event('A module up')\n"
            "def tearDownModule(): event('A module down')\nclass Case(unittest.TestCase):\n"
            " @classmethod\n def setUpClass(cls): event('A class up')\n"
            " @classmethod\n def tearDownClass(cls): event('A class down')\n"
            " def test_a(self): event('A test')\n",
        )
        self.module("b", common + "class Case(unittest.TestCase):\n def test_b(self): event('B test')\n")
        self.module("c", f"from {a} import Case\n")
        self.module("d", "import unittest\nclass Case(unittest.TestCase):\n def test_d(self): pass\n")
        plan = shards.make_plan(self.root, 2)
        self.assertIn([0, 1, 2], plan["shards"])
        self.assertEqual(plan["units"][0]["caseIds"], plan["units"][2]["caseIds"])
        unittest.TestLoader().discover(str(self.root / "tests")).run(unittest.TestResult())
        expected = trace.read_text(encoding="utf-8").splitlines()
        self.assertEqual(expected, ["A module up", "A class up", "A test", "A class down", "A module down", "B test",
                                    "A module up", "A class up", "A test", "A class down", "A module down"])
        trace.write_text("", encoding="utf-8")
        self.passing_receipts(plan)
        self.assertEqual(trace.read_text(encoding="utf-8").splitlines(), expected)
        # A and A still share a worker here, but removing B changes fixture lifetime.
        changed = copy.deepcopy(plan)
        changed["shards"] = [[0, 2], [1, 3]]
        with self.assertRaises(shards.PlanError):
            shards.audit(changed)

    def test_inherited_tests_and_duplicate_case_ids_retain_every_instance(self) -> None:
        a, _ = self.module(
            "a", "import unittest\nclass Base(unittest.TestCase):\n def test_inherited(self): pass\nAlias = Base\n",
        )
        self.module("b", f"from {a} import Base\nclass Child(Base):\n def test_local(self): pass\ndel Base\n")
        self.module("c", f"from {a} import Base as Case\n")
        self.module("d", "import unittest\nclass Case(unittest.TestCase):\n def test_last(self): pass\n")
        plan = shards.make_plan(self.root, 2)
        independent = [case.id() for case in cases_in(unittest.TestLoader().discover(str(self.root / "tests")))]
        self.assertEqual(plan["baselineCaseIds"], independent)
        self.assertEqual(plan["totalTests"], 6)
        self.assertEqual(Counter(independent)[f"{a}.Base.test_inherited"], 3)
        receipts = self.passing_receipts(plan)
        self.assertEqual(sum(receipt["testsRun"] for receipt in receipts), 6)
        shards.verify_receipts(plan, receipts, "success")
        changed = copy.deepcopy(receipts)
        duplicate_owner = next(receipt for receipt in changed if len(set(receipt["loadedCaseIds"])) < len(receipt["loadedCaseIds"]))
        duplicate_owner["loadedCaseIds"] = list(dict.fromkeys(duplicate_owner["loadedCaseIds"]))
        with self.assertRaises(shards.PlanError):
            shards.verify_receipts(plan, changed, "success")

    def test_independent_discovery_detects_equal_id_suite_substitution(self) -> None:
        a, _ = self.module("a", "import unittest\nclass Case(unittest.TestCase):\n def test_same(self): pass\n")
        self.module("b", f"from {a} import Case\n")
        original = shards.discovery_units

        def substitute(suite, loader):
            if id(suite) in loader.origins:
                yield from original(suite, loader)
                return
            units = list(original(suite, loader))
            self.assertEqual(len(units), 2)
            self.assertEqual([case.id() for case in cases_in(units[0][1])], [case.id() for case in cases_in(units[1][1])])
            yield units[0]
            # Origin labels, counts, and IDs remain identical. The actual second
            # TestCase instance has been lost, so a set/count-only audit is unsafe.
            yield units[1][0], units[0][1]

        with patch.object(shards, "discovery_units", side_effect=substitute):
            with self.assertRaises(shards.PlanError):
                shards.make_plan(self.root, 1)

    def test_empty_origins_original_names_and_filename_pattern_are_retained(self) -> None:
        empty, _ = self.module("keep_a", "# An empty discovered module is still an origin.\n")
        active, _ = self.module("keep_b", "import unittest\nclass Case(unittest.TestCase):\n def test_b(self): pass\n")
        ignored, _ = self.module("ignore", "raise AssertionError('pattern excluded module imported')\n")
        pattern = self.prefix + "keep*.py"
        plan = shards.make_plan(self.root, 2, pattern=pattern)
        self.assertEqual(plan["pattern"], pattern)
        self.assertEqual(plan["baselineOrigins"], [empty, active])
        self.assertEqual(plan["units"][0]["caseIds"], [])
        self.assertEqual(plan["baselineCaseIds"], [active + ".Case.test_b"])
        self.assertNotIn(ignored, sys.modules)
        receipts = self.passing_receipts(plan)
        self.assertEqual(sum(receipt["testsRun"] for receipt in receipts), 1)
        shards.verify_receipts(plan, receipts, "success")
        changed = copy.deepcopy(plan)
        changed["units"].pop(0)
        changed["shards"] = [[0]]
        with self.assertRaises(shards.PlanError):
            shards.audit(changed)

    def test_audit_rejects_missing_duplicate_invalid_or_reordered_assignments(self) -> None:
        plan = self.simple_plan()
        for assignments in ([[0]], [[0], [0, 1]], [[0], [2]], [[-1], [1]], [[1, 0]]):
            with self.subTest(assignments=assignments):
                changed = copy.deepcopy(plan)
                changed["shards"] = assignments
                with self.assertRaises(shards.PlanError):
                    shards.audit(changed)
        for field, value in (("totalTests", 3), ("baselineCaseIds", list(reversed(plan["baselineCaseIds"]))),
                             ("baselineOrigins", list(reversed(plan["baselineOrigins"])))):
            with self.subTest(field=field):
                changed = copy.deepcopy(plan)
                changed[field] = value
                with self.assertRaises(shards.PlanError):
                    shards.audit(changed)
        changed = copy.deepcopy(plan)
        changed["units"][1]["caseIds"] = changed["units"][0]["caseIds"][:]
        with self.assertRaises(shards.PlanError):
            shards.audit(changed)

    def test_worker_rejects_tree_platform_python_and_source_drift_before_execution(self) -> None:
        plan = self.simple_plan()
        for field, value in (("tree", "other-tree"), ("platform", "other-platform"), ("python", [0, 0])):
            with self.subTest(field=field):
                changed = copy.deepcopy(plan)
                changed[field] = value
                with self.assertRaises(shards.PlanError):
                    shards.load_shard(self.root, changed, 0)
        unit = plan["units"][plan["shards"][0][0]]
        source = self.root / unit["source"]
        source.write_text("raise AssertionError('changed source must not be imported')\n", encoding="utf-8")
        sys.modules.pop(unit["module"], None)
        with self.assertRaises(shards.PlanError):
            shards.load_shard(self.root, plan, 0)

    def test_worker_rejects_reloaded_case_and_skip_topology_drift(self) -> None:
        plan = self.simple_plan()
        unit = plan["units"][plan["shards"][0][0]]
        case_type = sys.modules[unit["module"]].Case
        with patch.object(case_type, "test_extra", lambda self: None, create=True):
            with self.assertRaises(shards.PlanError):
                shards.load_shard(self.root, plan, 0)
        with patch.object(case_type.test_first, "__unittest_skip__", True, create=True):
            with self.assertRaises(shards.PlanError):
                shards.load_shard(self.root, plan, 0)

    def test_worker_rejects_module_resolving_to_another_checkout(self) -> None:
        plan = self.simple_plan()
        unit = plan["units"][plan["shards"][0][0]]
        with patch.object(sys.modules[unit["module"]], "__file__", str(self.root / "wrong.py")):
            with self.assertRaises(shards.PlanError):
                shards.load_shard(self.root, plan, 0)

    def test_worker_rejects_invalid_shard_index(self) -> None:
        plan = self.simple_plan()
        for index in (-1, 2):
            with self.subTest(index=index), self.assertRaises(shards.PlanError):
                shards.load_shard(self.root, plan, index)

    def test_fresh_workers_import_only_selected_modules_without_full_discovery(self) -> None:
        plan = self.simple_plan()
        manifest = self.root / "plan.json"
        manifest.write_text(json.dumps(plan), encoding="utf-8")
        code = "\n".join([
            "import json, sys, unittest",
            "from pathlib import Path",
            f"sys.path.insert(0, {str(ROOT)!r})",
            "from scripts import unit_test_shards as shards",
            "shards.repository_tree = lambda root: 'fixture-tree'",
            "def forbidden(*args, **kwargs): raise AssertionError('worker rediscovered')",
            "unittest.TestLoader.discover = forbidden",
            "def ids(node):",
            " if isinstance(node, unittest.TestSuite):",
            "  return [value for child in node for value in ids(child)]",
            " return [node.id()]",
            "root, manifest, index, prefix = sys.argv[1:]",
            "plan = json.loads(Path(manifest).read_text(encoding='utf-8'))",
            "suite = shards.load_shard(Path(root), plan, int(index))",
            "loaded = ids(suite)",
            "result = unittest.TestResult()",
            "suite.run(result)",
            "receipt = shards.make_receipt(plan, int(index), loaded, result)",
            "print(json.dumps({'receipt': receipt, 'modules': sorted(name for name in sys.modules if name.startswith(prefix))}))",
        ])
        receipts = []
        for index, indices in enumerate(plan["shards"]):
            completed = subprocess.run(
                [sys.executable, "-B", "-c", code, str(self.root), str(manifest), str(index), self.prefix],
                cwd=self.root, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["modules"], sorted(plan["units"][unit]["module"] for unit in indices))
            receipts.append(payload["receipt"])
        shards.verify_receipts(plan, receipts, "success")

    def test_discovery_import_and_syntax_errors_do_not_create_a_green_empty_plan(self) -> None:
        name, path = self.module("a", "raise RuntimeError('import failure sentinel')\n")
        with self.assertRaisesRegex(shards.PlanError, "import failure sentinel"):
            shards.make_plan(self.root, 1)
        sys.modules.pop(name, None)
        path.write_text("invalid syntax :\n", encoding="utf-8")
        importlib.invalidate_caches()
        with self.assertRaisesRegex(shards.PlanError, "SyntaxError"):
            shards.make_plan(self.root, 1)

    def test_import_time_skip_collector_is_explicitly_unsupported(self) -> None:
        self.module("a", "import unittest\nraise unittest.SkipTest('import-time skip')\n")
        with self.assertRaises(shards.PlanError):
            shards.make_plan(self.root, 1)

    def test_load_tests_hook_is_rejected_without_calling_it(self) -> None:
        self.module("a", "def load_tests(loader, suite, pattern):\n raise AssertionError('hook must not run')\n")
        with self.assertRaisesRegex(shards.PlanError, "unsupported load_tests"):
            shards.make_plan(self.root, 1)

    def test_custom_suite_and_nested_discovery_are_explicitly_unsupported(self) -> None:
        self.module("a", "import unittest\nclass Case(unittest.TestCase):\n def test_a(self): pass\n")

        class CustomSuite(unittest.TestSuite):
            def run(self, result, debug=False):
                raise AssertionError("unsupported custom run must not execute")

        with patch.object(unittest.TestLoader, "suiteClass", CustomSuite):
            with self.assertRaises(shards.PlanError):
                shards.make_plan(self.root, 1)
        nested = self.root / "tests" / (self.prefix + "nested")
        nested.mkdir()
        (nested / "__init__.py").write_text("", encoding="utf-8")
        (nested / "test_inner.py").write_text("import unittest\nclass Case(unittest.TestCase):\n def test_inner(self): pass\n", encoding="utf-8")
        with self.assertRaises(shards.PlanError):
            shards.make_plan(self.root, 1)

    def test_plan_digest_is_canonical_and_binds_manifest_contents(self) -> None:
        plan = self.simple_plan()
        digest = shards.plan_digest(plan)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        reordered = json.loads(json.dumps(plan, sort_keys=True, indent=4))
        self.assertEqual(shards.plan_digest(reordered), digest)
        changed = copy.deepcopy(plan)
        changed["tree"] = "another-tree"
        self.assertNotEqual(shards.plan_digest(changed), digest)

    def test_receipt_contains_exact_manifest_provenance_and_terminal_outcome(self) -> None:
        plan = self.simple_plan()
        receipt, result = self.run_shard(plan, 0)
        self.assertEqual(receipt["schemaVersion"], 1)
        self.assertEqual(receipt["manifestSha256"], shards.plan_digest(plan))
        self.assertEqual(receipt["shardIndex"], 0)
        self.assertEqual(receipt["unitIndices"], plan["shards"][0])
        for field in ("tree", "platform", "python", "pattern"):
            self.assertEqual(receipt[field], plan[field], field)
        self.assertEqual(receipt["status"], "completed")
        self.assertIs(receipt["successful"], True)
        self.assertEqual(receipt["testsRun"], result.testsRun)
        self.assertEqual([receipt[field] for field in ("failures", "errors", "skipped")], [0, 0, 0])

    def test_gate_rejects_missing_duplicate_extra_and_mixed_plan_receipts(self) -> None:
        plan = self.simple_plan()
        receipts = self.passing_receipts(plan)
        other_plan = copy.deepcopy(plan)
        other_plan["tree"] = "another-tree"
        mixed = copy.deepcopy(receipts)
        mixed[1]["manifestSha256"] = shards.plan_digest(other_plan)
        for invalid in ([], receipts[:1], [receipts[0], receipts[0]], receipts + receipts[:1], mixed):
            with self.subTest(receipts=invalid), self.assertRaises(shards.PlanError):
                shards.verify_receipts(plan, invalid, "success")

    def test_gate_requires_exact_successful_workflow_result(self) -> None:
        plan = self.simple_plan()
        receipts = self.passing_receipts(plan)
        for outcome in ("failure", "cancelled", "skipped", "timed_out", "neutral", "", "Success", None):
            with self.subTest(outcome=outcome), self.assertRaises(shards.PlanError):
                shards.verify_receipts(plan, receipts, outcome)

    def test_gate_rejects_changed_provenance_indices_or_ordered_loaded_cases(self) -> None:
        plan = self.simple_plan()
        receipts = self.passing_receipts(plan)
        changes = (
            ("schemaVersion", 2), ("manifestSha256", "0" * 64), ("tree", "other-tree"),
            ("platform", "other-platform"), ("python", [0, 0]), ("pattern", "other*.py"),
            ("shardIndex", 1), ("shardIndex", -1), ("shardIndex", 2),
            ("unitIndices", []), ("unitIndices", plan["shards"][1]),
            ("loadedCaseIds", []), ("loadedCaseIds", list(reversed(receipts[0]["loadedCaseIds"]))),
            ("loadedCaseIds", receipts[0]["loadedCaseIds"] + receipts[0]["loadedCaseIds"][:1]),
        )
        for field, value in changes:
            with self.subTest(field=field, value=value):
                changed = copy.deepcopy(receipts)
                changed[0][field] = value
                with self.assertRaises(shards.PlanError):
                    shards.verify_receipts(plan, changed, "success")

    def test_gate_rejects_incomplete_failed_or_malformed_terminal_receipts(self) -> None:
        plan = self.simple_plan()
        receipts = self.passing_receipts(plan)
        changes = [("status", "interrupted"), ("status", "running"), ("successful", False),
                   ("successful", 1), ("failures", 1), ("errors", 1)]
        changes.extend((field, value) for field in ("testsRun", "failures", "errors", "skipped") for value in (-1, "0", True))
        for field, value in changes:
            with self.subTest(field=field, value=value):
                changed = copy.deepcopy(receipts)
                changed[0][field] = value
                with self.assertRaises(shards.PlanError):
                    shards.verify_receipts(plan, changed, "success")
        for field in ("schemaVersion", "manifestSha256", "shardIndex", "unitIndices", "loadedCaseIds", "tree",
                      "platform", "python", "pattern", "status", "successful", "testsRun", "failures", "errors", "skipped"):
            with self.subTest(missing=field):
                changed = copy.deepcopy(receipts)
                del changed[0][field]
                with self.assertRaises(shards.PlanError):
                    shards.verify_receipts(plan, changed, "success")

    def test_interrupted_result_cannot_produce_an_accepted_receipt(self) -> None:
        plan = self.simple_plan()
        receipts = self.passing_receipts(plan)
        result = unittest.TestResult()
        result.stop()
        receipts[0] = shards.make_receipt(plan, 0, receipts[0]["loadedCaseIds"], result)
        self.assertEqual(receipts[0]["status"], "interrupted")
        with self.assertRaises(shards.PlanError):
            shards.verify_receipts(plan, receipts, "success")

    def test_gate_rejects_unexplained_unexecuted_cases(self) -> None:
        plan = self.simple_plan()
        receipts = self.passing_receipts(plan)
        receipts[0]["testsRun"] -= 1
        with self.assertRaisesRegex(shards.PlanError, "unexplained unexecuted"):
            shards.verify_receipts(plan, receipts, "success")

    def test_normal_test_and_fixture_skips_are_successful_receipts(self) -> None:
        self.module("a", "import unittest\nclass Case(unittest.TestCase):\n"
                    " @unittest.skip('normal test skip')\n def test_skip(self): pass\n def test_ok(self): pass\n")
        self.module("b", "import unittest\ndef setUpModule(): raise unittest.SkipTest('module fixture skip')\n"
                    "class Case(unittest.TestCase):\n def test_first(self): pass\n def test_second(self): pass\n")
        self.module("c", "import unittest\nclass Case(unittest.TestCase):\n"
                    " @classmethod\n def setUpClass(cls): raise unittest.SkipTest('class fixture skip')\n"
                    " def test_first(self): pass\n def test_second(self): pass\n")
        plan = shards.make_plan(self.root, 3)
        receipts = self.passing_receipts(plan)
        self.assertEqual(plan["totalTests"], 6)
        self.assertEqual(sum(receipt["testsRun"] for receipt in receipts), 2)
        self.assertEqual(sum(receipt["skipped"] for receipt in receipts), 3)
        self.assertTrue(all(receipt["successful"] for receipt in receipts))
        shards.verify_receipts(plan, receipts, "success")

    def test_unexpected_success_still_fails_gate_with_no_failure_or_error_entries(self) -> None:
        self.module("a", "import unittest\nclass Case(unittest.TestCase):\n"
                    " @unittest.expectedFailure\n def test_unexpected_success(self): pass\n")
        plan = shards.make_plan(self.root, 1)
        receipt, result = self.run_shard(plan, 0)
        self.assertEqual(len(result.unexpectedSuccesses), 1)
        self.assertEqual([receipt["failures"], receipt["errors"]], [0, 0])
        self.assertIs(receipt["successful"], False)
        with self.assertRaises(shards.PlanError):
            shards.verify_receipts(plan, [receipt], "success")


if __name__ == "__main__":
    unittest.main()
