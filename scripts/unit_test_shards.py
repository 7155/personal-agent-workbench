"""Fail-closed module plans for complete unittest coverage across independent CI jobs.

The ordinary runner remains available for unsupported custom discovery behavior.
Plans contain repository-relative sources and retain original discovered origins.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys
import unittest


class PlanError(ValueError):
    pass


class RecordingLoader(unittest.TestLoader):
    def __init__(self):
        super().__init__()
        self.origins = {}

    def loadTestsFromModule(self, module, *, pattern=None):
        if hasattr(module, "load_tests"):
            raise PlanError(f"unsupported load_tests hook: {module.__name__}; use the unsharded runner")
        suite = super().loadTestsFromModule(module, pattern=pattern)
        self.origins[id(suite)] = module.__name__
        return suite


def leaves(node):
    if isinstance(node, unittest.BaseTestSuite):
        for child in node:
            yield from leaves(child)
    elif isinstance(node, unittest.TestCase):
        yield node
    else:
        raise PlanError(f"unsupported discovery node: {type(node)!r}")


def topology(node):
    kind = f"{type(node).__module__}.{type(node).__qualname__}"
    if isinstance(node, unittest.BaseTestSuite):
        if type(node) is not unittest.TestSuite:
            raise PlanError(f"unsupported custom suite: {kind}; use the unsharded runner")
        return [kind, [topology(child) for child in node]]
    if not isinstance(node, unittest.TestCase):
        raise PlanError(f"unsupported case: {type(node)!r}")
    method = getattr(node, node._testMethodName, None)
    skipped = bool(getattr(type(node), "__unittest_skip__", False)
                   or getattr(method, "__unittest_skip__", False))
    return [kind, node.id(), skipped]


def discovery_units(node, loader):
    origin = loader.origins.get(id(node))
    if origin is not None:
        yield origin, node
    elif type(node) is unittest.TestSuite:
        for child in node:
            yield from discovery_units(child, loader)
    else:
        raise PlanError("discovery returned an unowned custom suite; cannot safely partition it")


def components(units):
    """Preserve A/B/A fixture boundaries by keeping their entire interval together."""
    last = {}
    for index, unit in enumerate(units):
        for module in unit["fixtureModules"]:
            last[module] = index
    result = []
    start = 0
    while start < len(units):
        end = start
        index = start
        while index <= end:
            for module in units[index]["fixtureModules"]:
                end = max(end, last[module])
            index += 1
        result.append(list(range(start, end + 1)))
        start = end + 1
    return result


def assign(units, shard_count):
    groups = components(units)
    if type(shard_count) is not int or not 1 <= shard_count <= len(groups):
        raise PlanError("shard count must fit the nonempty discovery components")
    bins = [[] for _ in range(shard_count)]
    weights = [0] * shard_count
    weighted = [(sum(max(1, len(units[i]["caseIds"])) for i in group), group) for group in groups]
    for weight, group in sorted(weighted, key=lambda item: (-item[0], item[1][0])):
        target = min(range(shard_count), key=lambda i: (weights[i], i))
        bins[target].extend(group)
        weights[target] += weight
    return [sorted(group) for group in bins], weights, groups


def _plan_shape(plan):
    if not isinstance(plan, dict) or type(plan.get("schemaVersion")) is not int or plan["schemaVersion"] != 1:
        raise PlanError("unsupported shard plan schema")
    for key in ("tree", "platform", "pattern"):
        if not isinstance(plan.get(key), str) or not plan[key]:
            raise PlanError(f"missing plan identity: {key}")
    if (not isinstance(plan.get("python"), list) or len(plan["python"]) != 2
            or any(type(part) is not int for part in plan["python"])):
        raise PlanError("invalid Python identity")
    if type(plan.get("totalTests")) is not int or plan["totalTests"] <= 0:
        raise PlanError("full discovery must contain tests")
    for key in ("units", "shards", "baselineCaseIds", "baselineOrigins"):
        if not isinstance(plan.get(key), list):
            raise PlanError(f"invalid plan collection: {key}")
    if not plan["units"] or not plan["shards"]:
        raise PlanError("empty shard plan")
    for unit in plan["units"]:
        if not isinstance(unit, dict):
            raise PlanError("invalid discovery unit")
        for key in ("module", "source", "sourceSha256"):
            if not isinstance(unit.get(key), str) or not unit[key]:
                raise PlanError(f"invalid origin field: {key}")
        source = Path(unit["source"])
        if (source.is_absolute() or ".." in source.parts or source.parent != Path("tests")
                or source.suffix != ".py" or source.stem != unit["module"]):
            raise PlanError("unsupported nested or external discovery source")
        for key in ("caseIds", "fixtureModules"):
            if not isinstance(unit.get(key), list) or any(not isinstance(value, str) for value in unit[key]):
                raise PlanError(f"invalid case collection: {key}")
        if "topology" not in unit:
            raise PlanError("missing discovery topology")
    for key in ("baselineCaseIds", "baselineOrigins"):
        if any(not isinstance(value, str) for value in plan[key]):
            raise PlanError("invalid independent discovery baseline")
    for shard in plan["shards"]:
        if (not isinstance(shard, list) or not shard
                or any(type(index) is not int or not 0 <= index < len(plan["units"]) for index in shard)):
            raise PlanError("invalid shard assignment")


def audit(plan):
    _plan_shape(plan)
    units = plan["units"]
    flattened = [case_id for unit in units for case_id in unit["caseIds"]]
    if flattened != plan["baselineCaseIds"]:
        raise PlanError("origin extraction differs from independently traversed discovery cases")
    if [unit["module"] for unit in units] != plan["baselineOrigins"]:
        raise PlanError("origin extraction lost or repeated a discovery root, including an empty origin")
    indices = [index for shard in plan["shards"] for index in shard]
    if Counter(indices) != Counter(range(len(units))):
        raise PlanError("a discovery unit is missing or assigned more than once")
    expected = Counter((i, j, case_id) for i, unit in enumerate(units)
                       for j, case_id in enumerate(unit["caseIds"]))
    assigned = Counter((i, j, case_id) for shard in plan["shards"] for i in shard
                       for j, case_id in enumerate(units[i]["caseIds"]))
    if expected != assigned or sum(expected.values()) != plan["totalTests"]:
        raise PlanError("case occurrence coverage differs from full discovery")
    owners = {}
    positions = {}
    for shard_index, shard in enumerate(plan["shards"]):
        if shard != sorted(shard):
            raise PlanError("within-shard discovery order changed")
        for index in shard:
            for module in units[index]["fixtureModules"]:
                if module in owners and owners[module] != shard_index:
                    raise PlanError("one module's fixtures were split across shards")
                owners[module] = shard_index
            positions[index] = shard_index
    for group in components(units):
        if len({positions[index] for index in group}) != 1:
            raise PlanError("an intervening fixture boundary was removed")
    return {"tests": sum(expected.values()), "units": len(units),
            "duplicatesOrMissing": 0, "components": len(components(units))}


def source_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def repository_tree(root):
    status = subprocess.check_output(["git", "status", "--porcelain=v1", "--untracked-files=normal"],
                                     cwd=root, text=True)
    if status.strip():
        raise PlanError("discovery and worker loading require a clean frozen checkout")
    return subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=root, text=True).strip()


def make_plan(root, shard_count, *, pattern="test*.py"):
    root = root.resolve()
    tree = repository_tree(root)
    test_root = root / "tests"
    sys.path.insert(0, str(root))
    loader = RecordingLoader()
    suite = loader.discover(str(test_root), pattern=pattern)
    expected_count = suite.countTestCases()
    if loader.errors:
        # Discovery errors are a red plan, never an empty or apparently passing shard.
        raise PlanError("\n".join(loader.errors))
    baseline_cases = list(leaves(suite))
    baseline_roots = list(suite)
    if any(id(unit) not in loader.origins for unit in baseline_roots):
        raise PlanError("unowned custom suite or nested discovery root; use the unsharded runner")
    baseline_origins = [loader.origins[id(unit)] for unit in baseline_roots]
    units = []
    extracted_objects = []
    for origin, unit in discovery_units(suite, loader):
        cases = list(leaves(unit))
        extracted_objects.extend(cases)
        if len(cases) != unit.countTestCases():
            raise PlanError(f"non-repeatable custom suite: {origin}")
        module_path = Path(sys.modules[origin].__file__).resolve()
        if module_path.parent != test_root.resolve() or "." in origin:
            raise PlanError("nested discovery is unsupported; use the unsharded runner")
        path = str(module_path.relative_to(root))
        units.append({"module": origin, "source": path, "sourceSha256": source_hash(module_path),
                      "caseIds": [case.id() for case in cases],
                      "fixtureModules": sorted({type(case).__module__ for case in cases}),
                      "topology": topology(unit)})
    if ([id(case) for case in extracted_objects] != [id(case) for case in baseline_cases]
            or len(baseline_cases) != expected_count):
        raise PlanError("discovery topology lost test cases")
    if repository_tree(root) != tree:
        raise PlanError("source tree changed during discovery")
    shards, _weights, _groups = assign(units, shard_count)
    plan = {"schemaVersion": 1, "tree": tree, "platform": sys.platform,
            "python": list(sys.version_info[:2]), "pattern": pattern, "totalTests": expected_count,
            "baselineCaseIds": [case.id() for case in baseline_cases], "baselineOrigins": baseline_origins,
            "units": units, "shards": shards}
    audit(plan)
    return plan


def load_shard(root, plan, shard_index):
    audit(plan)
    if plan["tree"] != repository_tree(root):
        raise PlanError("repository tree differs from discovery plan")
    if plan["platform"] != sys.platform or plan["python"] != list(sys.version_info[:2]):
        raise PlanError("discovery plan was produced for another platform or Python minor")
    if type(shard_index) is not int or not 0 <= shard_index < len(plan["shards"]):
        raise PlanError("invalid shard index")
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "tests"))
    loader = unittest.TestLoader()
    # load_tests may call discover itself; retain the original discovery root.
    loader._top_level_dir = str(root / "tests")
    selected = []
    for index in plan["shards"][shard_index]:
        expected = plan["units"][index]
        if source_hash(root / expected["source"]) != expected["sourceSha256"]:
            raise PlanError(f"test module changed: {expected['module']}")
        module = importlib.import_module(expected["module"])
        if Path(module.__file__).resolve() != (root / expected["source"]).resolve():
            raise PlanError(f"origin resolved to another checkout: {expected['module']}")
        if hasattr(module, "load_tests"):
            raise PlanError(f"unsupported load_tests hook: {expected['module']}; use the unsharded runner")
        unit = loader.loadTestsFromModule(module, pattern=plan["pattern"])
        if loader.errors:
            raise PlanError("\n".join(loader.errors))
        if topology(unit) != expected["topology"]:
            raise PlanError(f"load_tests topology changed: {expected['module']}")
        if [case.id() for case in leaves(unit)] != expected["caseIds"]:
            raise PlanError(f"case manifest changed: {expected['module']}")
        selected.append(unit)  # Preserve ordinary suite nesting and fixture order.
    return unittest.TestSuite(selected)

def plan_digest(plan):
    """Bind receipts to all identities, origins, occurrences, and assignments."""
    audit(plan)
    body = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _selected_cases(plan, index):
    return [case_id for unit in plan["shards"][index] for case_id in plan["units"][unit]["caseIds"]]


def make_receipt(plan, index, loaded_case_ids, result):
    audit(plan)
    if type(index) is not int or not 0 <= index < len(plan["shards"]):
        raise PlanError("invalid receipt shard index")
    if loaded_case_ids != _selected_cases(plan, index):
        raise PlanError("loaded cases differ from the shard assignment")
    return {
        "schemaVersion": 1, "manifestSha256": plan_digest(plan), "shardIndex": index,
        "unitIndices": plan["shards"][index], "loadedCaseIds": loaded_case_ids,
        "tree": plan["tree"], "platform": plan["platform"], "python": plan["python"],
        "pattern": plan["pattern"], "status": "interrupted" if result.shouldStop else "completed",
        "successful": bool(result.wasSuccessful() and not result.shouldStop),
        "testsRun": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
        "skipped": len(result.skipped),
    }


def verify_receipts(plan, receipts, workflow_result):
    """A skipped/cancelled/missing job cannot be confused with a unittest skip."""
    audit(plan)
    if workflow_result != "success":
        raise PlanError(f"unit workflow did not succeed: {workflow_result}")
    if not isinstance(receipts, list) or len(receipts) != len(plan["shards"]):
        raise PlanError("missing or duplicate shard receipts")
    digest = plan_digest(plan)
    seen = set()
    for receipt in receipts:
        if not isinstance(receipt, dict):
            raise PlanError("invalid shard receipt")
        index = receipt.get("shardIndex")
        if type(index) is not int or not 0 <= index < len(plan["shards"]) or index in seen:
            raise PlanError("missing, duplicate, or invalid shard index")
        seen.add(index)
        if type(receipt.get("schemaVersion")) is not int or receipt["schemaVersion"] != 1:
            raise PlanError("unsupported shard receipt schema")
        for key, expected in (("manifestSha256", digest), ("tree", plan["tree"]),
                              ("platform", plan["platform"]), ("python", plan["python"]),
                              ("pattern", plan["pattern"]), ("unitIndices", plan["shards"][index]),
                              ("loadedCaseIds", _selected_cases(plan, index))):
            if receipt.get(key) != expected:
                raise PlanError(f"shard {index} has mismatched {key}")
        if receipt.get("status") != "completed" or receipt.get("successful") is not True:
            raise PlanError(f"shard {index} did not reach a successful terminal state")
        for key in ("testsRun", "failures", "errors", "skipped"):
            if type(receipt.get(key)) is not int or receipt[key] < 0:
                raise PlanError(f"shard {index} has an invalid {key} counter")
        if receipt["failures"] or receipt["errors"]:
            raise PlanError(f"shard {index} reported failures or errors")
        if receipt["testsRun"] > len(receipt["loadedCaseIds"]):
            raise PlanError(f"shard {index} ran unexpected extra cases")
        if receipt["testsRun"] < len(receipt["loadedCaseIds"]) and not receipt["skipped"]:
            raise PlanError(f"shard {index} left unexplained unexecuted cases")
    if seen != set(range(len(plan["shards"]))):
        raise PlanError("not all shard indices completed")
    # setUpModule/setUpClass may raise SkipTest before startTest, so testsRun can
    # legitimately be below the loaded count. Intact standard Suite completion
    # plus the terminal result preserves those ordinary unittest semantics.
    return {"manifestSha256": digest, "shards": len(receipts), "tests": plan["totalTests"],
            "testsRun": sum(receipt["testsRun"] for receipt in receipts),
            "skipped": sum(receipt["skipped"] for receipt in receipts), "successful": True}
