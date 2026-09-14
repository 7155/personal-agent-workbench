#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


BOOTSTRAP_DOCUMENT_BUDGETS = {"AGENTS.md": 1_250}

CORE_SKILLS = (
    "alignment-and-decision",
    "implementation-planning",
    "systematic-debugging",
    "test-driven-implementation",
    "orchestrate-session",
    "facilitate-room",
    "independent-review",
    "organize-work-documents",
)

RETIRED_FLOW_NAMES = (
    "implementation-execution",
    "quality-gate",
    "review-feedback-resolution",
    "structured-handoff",
    "work-document-archive",
    "room_define",
    "room_commit",
    "room_collaborate",
)

CURRENT_CONTRACT_DOCS = (
    "AGENTS.md",
    "README.md",
    "CONTRIBUTING.md",
)

LOCAL_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
RELEASE_SCOPE_SCHEMA = "personal-agent-workbench.release-candidate-scope.v1"
RELEASE_SCOPE_PATH = Path("release/release-candidate-scope.json")
RELEASE_DISPOSITIONS = {
    "keep_release",
    "delete_old_implementation",
    "other_work_not_in_release",
}


def _word_count(text: str) -> int:
    return len(text.split())


def _read_documents(root: Path, errors: list[str]) -> dict[str, str]:
    documents: dict[str, str] = {}
    for relative, budget in BOOTSTRAP_DOCUMENT_BUDGETS.items():
        path = root / relative
        if not path.is_file():
            errors.append(f"missing bootstrap authority document: {relative}")
            continue
        text = path.read_text(encoding="utf-8")
        documents[relative] = text
        count = _word_count(text)
        if count > budget:
            errors.append(
                f"{relative} exceeds its bounded context budget: {count} > {budget} words"
            )
    return documents


def _validate_local_links(root: Path, relative: str, text: str, errors: list[str]) -> None:
    source_dir = (root / relative).parent
    for target in LOCAL_LINK_RE.findall(text):
        if target.startswith("#") or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target):
            continue
        local_target = target.split("#", 1)[0]
        if not local_target:
            continue
        if not (source_dir / local_target).exists():
            errors.append(f"{relative} has a missing local link: {target}")


def _git_candidate_paths(root: Path, base_commit: str) -> tuple[dict[str, str], str]:
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "diff",
                "--name-status",
                "--no-renames",
                "-z",
                f"{base_commit}..HEAD",
            ],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        return {}, f"release scope Git diff failed: {type(exc).__name__}"

    status_names = {"A": "added", "M": "modified", "D": "deleted"}
    paths: dict[str, str] = {}
    # -z disables Git's C-style quoting and uses NUL separators for both
    # status and path. Decode bytes without universal-newline translation:
    # tabs, line breaks, CRs and non-ASCII characters are valid in Git paths.
    fields = completed.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    if fields.pop() != "" or len(fields) % 2:
        return {}, "release scope Git diff returned an unsupported row"
    for index in range(0, len(fields), 2):
        raw_status, relative = fields[index:index + 2]
        if not relative:
            return {}, "release scope Git diff returned an unsupported row"
        status = status_names.get(raw_status)
        if status is None:
            return {}, f"release scope Git diff returned unsupported status: {raw_status}"
        paths[relative] = status
    return paths, ""


def validate_release_candidate_scope(root: Path) -> list[str]:
    root = root.resolve()
    scope_path = root / RELEASE_SCOPE_PATH
    try:
        payload = json.loads(scope_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [f"missing release candidate scope: {RELEASE_SCOPE_PATH.as_posix()}"]
    except (OSError, json.JSONDecodeError) as exc:
        return [f"release candidate scope is unreadable: {type(exc).__name__}"]

    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["release candidate scope must be a JSON object"]
    if payload.get("schemaVersion") != RELEASE_SCOPE_SCHEMA:
        errors.append("release candidate scope schema is unsupported")

    source = payload.get("source")
    source_payload = source if isinstance(source, dict) else {}
    base_commit = str(source_payload.get("baseCommit") or "")
    if re.fullmatch(r"[0-9a-f]{40}", base_commit) is None:
        errors.append("release candidate scope source.baseCommit must be a full Git commit")
        return errors

    expected_paths, git_error = _git_candidate_paths(root, base_commit)
    if git_error:
        errors.append(git_error)
        return errors

    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        errors.append("release candidate scope items must be an array")
        return errors

    item_by_path: dict[str, dict[str, object]] = {}
    disposition_counts: dict[str, int] = {}
    group_counts: dict[str, int] = {}
    for index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, dict):
            errors.append(f"release candidate scope item {index} must be an object")
            continue
        relative = str(raw_item.get("path") or "")
        if not relative:
            errors.append(f"release candidate scope item {index} has no path")
            continue
        if relative in item_by_path:
            errors.append(f"release candidate scope has duplicate path: {relative}")
            continue
        item_by_path[relative] = raw_item
        disposition = str(raw_item.get("disposition") or "")
        if disposition not in RELEASE_DISPOSITIONS:
            errors.append(f"release candidate scope has unsupported disposition: {relative}")
        else:
            disposition_counts[disposition] = disposition_counts.get(disposition, 0) + 1
        group = str(raw_item.get("group") or "")
        if not group:
            errors.append(f"release candidate scope item has no group: {relative}")
        else:
            group_counts[group] = group_counts.get(group, 0) + 1
        if not str(raw_item.get("reason") or "").strip():
            errors.append(f"release candidate scope item has no reason: {relative}")

    missing = sorted(set(expected_paths) - set(item_by_path))
    extra = sorted(set(item_by_path) - set(expected_paths))
    if missing:
        errors.append(
            "release candidate scope misses base-to-HEAD paths: " + ", ".join(missing[:12])
        )
    if extra:
        errors.append(
            "release candidate scope contains paths outside base-to-HEAD: "
            + ", ".join(extra[:12])
        )
    for relative in sorted(set(expected_paths) & set(item_by_path)):
        actual_status = str(item_by_path[relative].get("status") or "")
        if actual_status != expected_paths[relative]:
            errors.append(
                f"release candidate scope status mismatch for {relative}: "
                f"{actual_status or 'missing'} != {expected_paths[relative]}"
            )

    summary = payload.get("summary")
    summary_payload = summary if isinstance(summary, dict) else {}
    if summary_payload.get("pathCount") != len(raw_items):
        errors.append("release candidate scope summary.pathCount is stale")
    if summary_payload.get("dispositionCounts") != disposition_counts:
        errors.append("release candidate scope summary.dispositionCounts is stale")
    summary_group_counts = summary_payload.get("groupCounts")
    if summary_group_counts != group_counts:
        errors.append("release candidate scope summary.groupCounts is stale")

    group_definitions = payload.get("groups")
    if not isinstance(group_definitions, dict):
        errors.append("release candidate scope groups must be an object")
        group_definitions = {}
    summary_group_counts_payload = (
        summary_group_counts if isinstance(summary_group_counts, dict) else {}
    )
    missing_count = object()
    for group in sorted(
        set(group_counts) | set(group_definitions) | set(summary_group_counts_payload)
    ):
        definition = group_definitions.get(group, missing_count)
        declared_count = (
            definition.get("pathCount", missing_count)
            if isinstance(definition, dict)
            else missing_count
        )
        summary_count = summary_group_counts_payload.get(group, missing_count)
        actual_count = group_counts.get(group, 0)
        if declared_count != actual_count or summary_count != actual_count:
            declared_label = (
                "missing" if declared_count is missing_count else repr(declared_count)
            )
            summary_label = (
                "missing" if summary_count is missing_count else repr(summary_count)
            )
            errors.append(
                f"release candidate scope group count mismatch for {group}: "
                f"groups.{group}.pathCount={declared_label}, "
                f"summary.groupCounts.{group}={summary_label}, "
                f"actual item membership={actual_count}"
            )
    unclassified = sum(
        1
        for item in raw_items
        if not isinstance(item, dict)
        or str(item.get("disposition") or "") not in RELEASE_DISPOSITIONS
        or not str(item.get("group") or "")
    )
    if summary_payload.get("unclassifiedPathCount") != unclassified:
        errors.append("release candidate scope summary.unclassifiedPathCount is stale")
    other_count = disposition_counts.get("other_work_not_in_release", 0)
    if summary_payload.get("otherWorkNotInReleasePathCount") != other_count:
        errors.append("release candidate scope other-work count is stale")
    return errors


def validate_project_harness(root: Path) -> list[str]:
    root = root.resolve()
    errors: list[str] = []
    documents = _read_documents(root, errors)

    agents = documents.get("AGENTS.md", "")
    for relative in ("README.md", "CONTRIBUTING.md"):
        if f"]({relative})" not in agents:
            errors.append(f"AGENTS.md must route to public guidance: {relative}")

    for skill in CORE_SKILLS:
        if f"`{skill}`" not in agents:
            errors.append(f"AGENTS.md does not route the core Skill: {skill}")
        if not (root / "integrations" / "pi" / "skills" / skill / "SKILL.md").is_file():
            errors.append(f"missing core Skill body: {skill}")

    for relative in CURRENT_CONTRACT_DOCS:
        path = root / relative
        if not path.is_file():
            if relative not in BOOTSTRAP_DOCUMENT_BUDGETS:
                errors.append(f"missing current contract document: {relative}")
            continue
        text = documents.get(relative)
        if text is None:
            text = path.read_text(encoding="utf-8")
        _validate_local_links(root, relative, text, errors)
        for retired in RETIRED_FLOW_NAMES:
            if retired in text:
                errors.append(f"{relative} still references retired flow name: {retired}")

    gitignore = root / ".gitignore"
    if not gitignore.is_file() or "/docs/" not in gitignore.read_text(encoding="utf-8"):
        errors.append("ignored local docs/ history boundary is missing from .gitignore")

    for retired in RETIRED_FLOW_NAMES[:5]:
        if (root / "integrations" / "pi" / "skills" / retired).exists():
            errors.append(f"retired fixed-pipeline Skill directory still exists: {retired}")

    errors.extend(validate_release_candidate_scope(root))

    return errors


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    errors = validate_project_harness(root)
    if errors:
        print("FAILED")
        for error in errors:
            print(f"- {error}")
        return 1
    print(
        "OK: bounded bootstrap context, progressive docs/Skill routing, "
        "retired-flow cleanup, and release scope are consistent"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
