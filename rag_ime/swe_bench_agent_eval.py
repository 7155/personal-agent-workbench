from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import tarfile
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent_model_defaults import DEFAULT_AGENT_MODEL_PROFILE
from .agent_workspace import (
    PreparedWorkspaceCommand,
    WorkspaceHarness,
    WorkspaceHarnessError,
)


DATASET_SCHEMA_VERSION = "rag-ime.swe-bench-verified-dataset.v1"
AGENT_CASE_SCHEMA_VERSION = "rag-ime.swe-bench-agent-case.v1"
RUN_SCHEMA_VERSION = "rag-ime.swe-bench-agent-run.v1"
REPORT_SCHEMA_VERSION = "rag-ime.swe-bench-agent-eval.v1"
OFFICIAL_DATASET_NAME = "princeton-nlp/SWE-bench_Verified"
DEFAULT_MODEL_REFERENCE = DEFAULT_AGENT_MODEL_PROFILE

_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INSTANCE_ID = re.compile(r"^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]+$")
_DIFF_HEADER = re.compile(r"^diff --git (.+) (.+)$")
_FORBIDDEN_AGENT_CASE_KEYS = frozenset(
    {
        "patch",
        "test_patch",
        "testPatch",
        "goldPatch",
        "referencePatch",
        "modelPatch",
        "failToPass",
        "passToPass",
    }
)
_TEST_DIRECTORY_NAMES = frozenset({"test", "tests", "testing"})
_TEST_FILE_NAMES = frozenset({"conftest.py", "pytest.ini", "tox.ini"})
_SHELL_MUTATION = re.compile(
    r"(?:^|[;&|\n])\s*(?:rm|mv|cp|install|patch|git|sed|perl|ruby|tee|touch|truncate)\b"
    r"|(?:^|\s)(?:>|>>|2>|2>>|&>)"
    r"|\bpython(?:[0-9.]*)?\s+(?:-c(?:\s|$)|-(?:\s|$))",
    re.IGNORECASE,
)
_ALLOWED_TEST_COMMAND = re.compile(
    r"^(?:"
    r"(?:python(?:[0-9.]*)?\s+-m\s+(?:pytest|unittest|compileall))"
    r"|(?:pytest|py\.test|tox|nox)(?:\s|$)"
    r"|(?:python(?:[0-9.]*)?\s+(?:tests?/)?runtests\.py)(?:\s|$)"
    r"|(?:\./)?tests?/runtests\.py(?:\s|$)"
    r"|make\s+(?:test|tests|check)(?:\s|$)"
    r"|ruff\s+check(?:\s|$)"
    r")",
    re.IGNORECASE,
)


class SweBenchAgentEvalError(ValueError):
    """The local SWE-bench evaluation contract is malformed or unsafe."""


@dataclass(frozen=True)
class SweBenchAgentCase:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    hints_text: str
    created_at: str
    version: str
    difficulty: str

    def to_agent_payload(self) -> dict[str, str]:
        return {
            "schemaVersion": AGENT_CASE_SCHEMA_VERSION,
            "instanceId": self.instance_id,
            "repo": self.repo,
            "baseCommit": self.base_commit,
            "problemStatement": self.problem_statement,
            "hintsText": self.hints_text,
            "createdAt": self.created_at,
            "version": self.version,
            "difficulty": self.difficulty,
        }


@dataclass(frozen=True)
class SweBenchVerifierCase:
    instance_id: str
    environment_setup_commit: str
    fail_to_pass: tuple[str, ...]
    pass_to_pass: tuple[str, ...]
    gold_patch_sha256: str
    test_patch_sha256: str


@dataclass(frozen=True)
class SweBenchPreparedDataset:
    source: Mapping[str, object]
    adapter: Mapping[str, object]
    full_instance_ids: tuple[str, ...]
    interview_instance_ids: tuple[str, ...]
    full_split_sha256: str
    interview_split_sha256: str
    agent_cases: Mapping[str, SweBenchAgentCase]
    verifier_cases: Mapping[str, SweBenchVerifierCase]

    def select(
        self,
        instance_ids: Sequence[str] | None = None,
        *,
        interview_only: bool = True,
    ) -> list[SweBenchAgentCase]:
        allowed = (
            set(self.interview_instance_ids)
            if interview_only
            else set(self.full_instance_ids)
        )
        selected = list(instance_ids or self.interview_instance_ids)
        if not selected:
            raise SweBenchAgentEvalError("at least one SWE-bench instance is required")
        if len(selected) != len(set(selected)):
            raise SweBenchAgentEvalError("SWE-bench instance selection contains duplicates")
        unknown = sorted(set(selected) - allowed)
        if unknown:
            scope = "interview split" if interview_only else "full split"
            raise SweBenchAgentEvalError(
                f"instances are outside the frozen {scope}: {', '.join(unknown[:5])}"
            )
        return [self.agent_cases[instance_id] for instance_id in selected]


class SweBenchWorkspaceHarness(WorkspaceHarness):
    """Workspace Tool boundary for leakage-resistant SWE-bench inference.

    The Agent receives a git-archive workspace with no repository history. All
    writes go through the existing governed workspace operations. Test files
    are immutable and Shell is restricted to non-mutating test/check commands;
    the official verifier remains the only owner of the hidden test patch.
    """

    def prepare_patch(
        self,
        session: Mapping[str, object],
        args: Mapping[str, object],
    ) -> Any:
        _reject_test_mutation_path(args.get("path"))
        return super().prepare_patch(session, args)

    def prepare_edit(
        self,
        session: Mapping[str, object],
        args: Mapping[str, object],
    ) -> Any:
        _reject_test_mutation_path(args.get("path"))
        return super().prepare_edit(session, args)

    def prepare_write(
        self,
        session: Mapping[str, object],
        args: Mapping[str, object],
    ) -> Any:
        _reject_test_mutation_path(args.get("path"))
        return super().prepare_write(session, args)

    def prepare_command(
        self,
        session: Mapping[str, object],
        args: Mapping[str, object],
    ) -> PreparedWorkspaceCommand:
        if args.get("allowNetwork") is not False:
            raise WorkspaceHarnessError(
                "SWE-bench inference commands must explicitly disable network access"
            )
        command = " ".join(str(args.get("command") or "").split())
        if (
            not command
            or _SHELL_MUTATION.search(command)
            or re.search(r"(?:&&|\|\||[;`]|\$\()", command)
        ):
            raise WorkspaceHarnessError(
                "SWE-bench Shell is restricted to non-mutating test/check commands"
            )
        if _ALLOWED_TEST_COMMAND.match(command) is None:
            raise WorkspaceHarnessError(
                "SWE-bench Shell command is outside the test/check allowlist"
            )
        return super().prepare_command(session, {**dict(args), "command": command})


def load_prepared_swe_bench(path: str | Path) -> SweBenchPreparedDataset:
    source_path = Path(path).expanduser().resolve(strict=True)
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SweBenchAgentEvalError("cannot read prepared SWE-bench dataset") from exc
    if not isinstance(payload, dict) or payload.get("schemaVersion") != DATASET_SCHEMA_VERSION:
        raise SweBenchAgentEvalError("unexpected prepared SWE-bench schema")
    source = _mapping(payload.get("source"), "source")
    adapter = _mapping(payload.get("adapter"), "adapter")
    if adapter.get("name") != "swe-bench-verified":
        raise SweBenchAgentEvalError("prepared dataset is not SWE-bench Verified")
    if adapter.get("goldPatchExposed") is not False:
        raise SweBenchAgentEvalError("prepared dataset exposes the gold patch")
    if adapter.get("testPatchExposed") is not False:
        raise SweBenchAgentEvalError("prepared dataset exposes the test patch")

    full_ids = _identifier_array(payload.get("fullInstanceIds"), "fullInstanceIds")
    interview_ids = _identifier_array(
        payload.get("interviewInstanceIds"), "interviewInstanceIds"
    )
    if len(full_ids) != len(set(full_ids)) or len(interview_ids) != len(set(interview_ids)):
        raise SweBenchAgentEvalError("prepared SWE-bench split contains duplicate IDs")
    if not set(interview_ids).issubset(full_ids):
        raise SweBenchAgentEvalError("interview split is not a subset of the full split")
    if int(adapter.get("fullCount") or -1) != len(full_ids):
        raise SweBenchAgentEvalError("full SWE-bench count does not match the adapter")
    if int(adapter.get("interviewCount") or -1) != len(interview_ids):
        raise SweBenchAgentEvalError("interview count does not match the adapter")
    full_sha = _sha256(payload.get("fullSplitSha256"), "fullSplitSha256")
    interview_sha = _sha256(
        payload.get("interviewSplitSha256"), "interviewSplitSha256"
    )
    if full_sha != _bundle_sha256({"instanceIds": full_ids}):
        raise SweBenchAgentEvalError("full SWE-bench split hash does not match")
    if interview_sha != _bundle_sha256({"instanceIds": interview_ids}):
        raise SweBenchAgentEvalError("interview SWE-bench split hash does not match")

    raw_agent_cases = payload.get("agentCases")
    raw_verifier_cases = payload.get("verifierCases")
    if not isinstance(raw_agent_cases, list) or not isinstance(raw_verifier_cases, list):
        raise SweBenchAgentEvalError("prepared SWE-bench cases must be arrays")
    agent_cases: dict[str, SweBenchAgentCase] = {}
    for raw in raw_agent_cases:
        value = _mapping(raw, "agentCases item")
        exposed = _FORBIDDEN_AGENT_CASE_KEYS.intersection(value)
        if exposed:
            raise SweBenchAgentEvalError(
                "Agent case exposes verifier-only fields: " + ", ".join(sorted(exposed))
            )
        if value.get("schemaVersion") != AGENT_CASE_SCHEMA_VERSION:
            raise SweBenchAgentEvalError("unexpected SWE-bench Agent case schema")
        instance_id = _instance_id(value.get("instanceId"), "agent instanceId")
        if instance_id in agent_cases:
            raise SweBenchAgentEvalError(f"duplicate Agent case: {instance_id}")
        agent_cases[instance_id] = SweBenchAgentCase(
            instance_id=instance_id,
            repo=_repo_slug(value.get("repo")),
            base_commit=_sha1(value.get("baseCommit"), f"{instance_id}.baseCommit"),
            problem_statement=_text(value.get("problemStatement"), "problemStatement", 500_000),
            hints_text=str(value.get("hintsText") or ""),
            created_at=_text(value.get("createdAt"), "createdAt", 100),
            version=str(value.get("version") or ""),
            difficulty=str(value.get("difficulty") or "unspecified"),
        )
    verifier_cases: dict[str, SweBenchVerifierCase] = {}
    for raw in raw_verifier_cases:
        value = _mapping(raw, "verifierCases item")
        instance_id = _instance_id(value.get("instanceId"), "verifier instanceId")
        if instance_id in verifier_cases:
            raise SweBenchAgentEvalError(f"duplicate verifier case: {instance_id}")
        forbidden_content = {
            key
            for key in ("patch", "test_patch", "goldPatch", "testPatch")
            if key in value
        }
        if forbidden_content:
            raise SweBenchAgentEvalError("verifier case contains patch content")
        verifier_cases[instance_id] = SweBenchVerifierCase(
            instance_id=instance_id,
            environment_setup_commit=_sha1(
                value.get("environmentSetupCommit"),
                f"{instance_id}.environmentSetupCommit",
            ),
            fail_to_pass=tuple(_string_array(value.get("failToPass"), "failToPass")),
            pass_to_pass=tuple(_string_array(value.get("passToPass"), "passToPass")),
            gold_patch_sha256=_sha256(
                value.get("goldPatchSha256"), f"{instance_id}.goldPatchSha256"
            ),
            test_patch_sha256=_sha256(
                value.get("testPatchSha256"), f"{instance_id}.testPatchSha256"
            ),
        )
    if set(agent_cases) != set(full_ids) or set(verifier_cases) != set(full_ids):
        raise SweBenchAgentEvalError("prepared SWE-bench case IDs do not match the full split")
    return SweBenchPreparedDataset(
        source=source,
        adapter=adapter,
        full_instance_ids=tuple(full_ids),
        interview_instance_ids=tuple(interview_ids),
        full_split_sha256=full_sha,
        interview_split_sha256=interview_sha,
        agent_cases=agent_cases,
        verifier_cases=verifier_cases,
    )


def stage_agent_workspace(
    case: SweBenchAgentCase,
    *,
    source_repository: str | Path,
    workspace: str | Path,
) -> dict[str, object]:
    source = Path(source_repository).expanduser().resolve(strict=True)
    target = Path(workspace).expanduser().resolve(strict=False)
    if target.exists():
        if not target.is_dir() or any(target.iterdir()):
            raise SweBenchAgentEvalError("SWE-bench workspace must be absent or empty")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.chmod(0o700)
    git = _git_prefix(source)
    remote = _run(git + ["config", "--get", "remote.origin.url"], allow_empty=True)
    if _repo_slug_from_remote(remote) != case.repo:
        raise SweBenchAgentEvalError(
            f"repository remote does not match {case.repo}: {remote or '<missing>'}"
        )
    commit = _run(git + ["rev-parse", f"{case.base_commit}^{{commit}}"])
    if commit != case.base_commit:
        raise SweBenchAgentEvalError("repository does not contain the exact base commit")
    with tempfile.NamedTemporaryFile(
        prefix="paw-swe-bench-", suffix=".tar", delete=False
    ) as handle:
        archive_path = Path(handle.name)
    try:
        _run(
            git + ["archive", "--format=tar", f"--output={archive_path}", case.base_commit],
            allow_empty=True,
        )
        with tarfile.open(archive_path, mode="r:") as archive:
            _validate_archive_members(archive.getmembers())
            archive.extractall(target, filter="data")
    finally:
        archive_path.unlink(missing_ok=True)
    tree = workspace_tree_receipt(target)
    if tree["gitMetadataPresent"] is True:
        raise SweBenchAgentEvalError("staged Agent workspace unexpectedly contains .git metadata")
    return {
        "schemaVersion": "rag-ime.swe-bench-workspace-stage.v1",
        "instanceId": case.instance_id,
        "repo": case.repo,
        "baseCommit": case.base_commit,
        "sourceRemoteSha256": hashlib.sha256(remote.encode("utf-8")).hexdigest(),
        "historyExposed": False,
        "networkUsed": False,
        "workspace": tree,
    }


def workspace_tree_receipt(workspace: str | Path) -> dict[str, object]:
    root = Path(workspace).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise SweBenchAgentEvalError("SWE-bench workspace must be a directory")
    digest = hashlib.sha256()
    file_count = 0
    byte_count = 0
    symlink_count = 0
    for path in sorted(root.rglob("*"), key=lambda value: value.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if path.is_symlink():
            target = os.readlink(path)
            digest.update(b"L\0")
            digest.update(target.encode("utf-8"))
            symlink_count += 1
        elif path.is_file():
            digest.update(b"F\0")
            file_count += 1
            size = path.stat().st_size
            byte_count += size
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        elif path.is_dir():
            digest.update(b"D\0")
        digest.update(b"\0")
    return {
        "treeSha256": digest.hexdigest(),
        "fileCount": file_count,
        "byteCount": byte_count,
        "symlinkCount": symlink_count,
        "gitMetadataPresent": (root / ".git").exists(),
    }


def capture_model_patch(
    case: SweBenchAgentCase,
    *,
    source_repository: str | Path,
    workspace: str | Path,
    verifier_case: SweBenchVerifierCase | None = None,
    max_patch_bytes: int = 2 * 1024 * 1024,
) -> dict[str, object]:
    source = Path(source_repository).expanduser().resolve(strict=True)
    root = Path(workspace).expanduser().resolve(strict=True)
    if (root / ".git").exists():
        raise SweBenchAgentEvalError("Agent workspace must not contain git history")
    git = _git_prefix(source)
    with tempfile.TemporaryDirectory(prefix="paw-swe-bench-index-") as temporary:
        index_path = Path(temporary) / "index"
        env = {
            **os.environ,
            "GIT_INDEX_FILE": str(index_path),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        _run(git + ["read-tree", case.base_commit], env=env, allow_empty=True)
        _run(git + [f"--work-tree={root}", "add", "-A"], env=env, allow_empty=True)
        completed = subprocess.run(
            git
            + [
                f"--work-tree={root}",
                "diff",
                "--cached",
                "--binary",
                "--no-ext-diff",
                "--src-prefix=a/",
                "--dst-prefix=b/",
                case.base_commit,
            ],
            check=False,
            capture_output=True,
            env=env,
        )
    if completed.returncode != 0:
        raise SweBenchAgentEvalError(
            completed.stderr.decode("utf-8", errors="replace").strip()
            or "cannot capture SWE-bench model patch"
        )
    patch_bytes = completed.stdout
    if len(patch_bytes) > max(1, int(max_patch_bytes)):
        raise SweBenchAgentEvalError("SWE-bench model patch exceeds the configured limit")
    try:
        patch = patch_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SweBenchAgentEvalError("SWE-bench model patch must be UTF-8 text") from exc
    analysis = analyze_model_patch(patch, verifier_case=verifier_case)
    return {
        "schemaVersion": "rag-ime.swe-bench-model-patch.v1",
        "instanceId": case.instance_id,
        "modelPatch": patch,
        "modelPatchSha256": hashlib.sha256(patch_bytes).hexdigest(),
        "modelPatchBytes": len(patch_bytes),
        **analysis,
    }


def analyze_model_patch(
    patch: str,
    *,
    verifier_case: SweBenchVerifierCase | None = None,
) -> dict[str, object]:
    if not isinstance(patch, str):
        raise SweBenchAgentEvalError("model patch must be text")
    changed_paths: list[str] = []
    for line in patch.splitlines():
        matched = _DIFF_HEADER.match(line)
        if matched is None:
            continue
        try:
            left, right = shlex.split(matched.group(1))[0], shlex.split(matched.group(2))[0]
        except (ValueError, IndexError) as exc:
            raise SweBenchAgentEvalError("model patch has an invalid diff header") from exc
        candidate = right if right != "/dev/null" else left
        if candidate.startswith(("a/", "b/")):
            candidate = candidate[2:]
        normalized = Path(candidate).as_posix().lstrip("/")
        if not normalized or ".." in Path(normalized).parts:
            raise SweBenchAgentEvalError("model patch path escapes the repository")
        changed_paths.append(normalized)
    if patch and not changed_paths:
        raise SweBenchAgentEvalError("non-empty model patch has no diff headers")
    if len(changed_paths) != len(set(changed_paths)):
        changed_paths = list(dict.fromkeys(changed_paths))
    test_paths = sorted(path for path in changed_paths if is_test_path(path))
    hidden_test_paths: set[str] = set()
    if verifier_case is not None:
        for test_id in (*verifier_case.fail_to_pass, *verifier_case.pass_to_pass):
            candidate = str(test_id).split("::", 1)[0].strip()
            if candidate:
                hidden_test_paths.add(Path(candidate).as_posix())
    hidden_overlap = sorted(set(changed_paths).intersection(hidden_test_paths))
    binary = "GIT binary patch" in patch or "Binary files " in patch
    return {
        "emptyPatch": not bool(patch.strip()),
        "changedPaths": changed_paths,
        "changedPathCount": len(changed_paths),
        "testMutationPaths": test_paths,
        "hiddenVerifierTestOverlap": hidden_overlap,
        "binaryPatch": binary,
        "integrityPassed": not binary and not test_paths and not hidden_overlap,
    }


def official_prediction(
    *,
    instance_id: str,
    model_patch: str,
    model_name_or_path: str = DEFAULT_MODEL_REFERENCE,
) -> dict[str, str]:
    return {
        "instance_id": _instance_id(instance_id, "prediction instance_id"),
        "model_name_or_path": _text(model_name_or_path, "model_name_or_path", 500),
        "model_patch": str(model_patch),
    }


def write_official_predictions(
    path: str | Path,
    predictions: Iterable[Mapping[str, object]],
) -> dict[str, object]:
    target = Path(path).expanduser().resolve(strict=False)
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in predictions:
        row = official_prediction(
            instance_id=str(raw.get("instance_id") or raw.get("instanceId") or ""),
            model_name_or_path=str(
                raw.get("model_name_or_path")
                or raw.get("modelNameOrPath")
                or DEFAULT_MODEL_REFERENCE
            ),
            model_patch=str(raw.get("model_patch") or raw.get("modelPatch") or ""),
        )
        if row["instance_id"] in seen:
            raise SweBenchAgentEvalError("prediction file contains duplicate instances")
        seen.add(row["instance_id"])
        rows.append(row)
    if not rows:
        raise SweBenchAgentEvalError("prediction file requires at least one row")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    serialized = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in rows
    )
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(serialized, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(target)
    return {
        "path": str(target),
        "rowCount": len(rows),
        "instanceIds": [row["instance_id"] for row in rows],
        "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        "uploaded": False,
    }


def official_verifier_command(
    *,
    predictions_path: str | Path,
    instance_ids: Sequence[str],
    run_id: str,
    max_workers: int = 1,
    cache_level: str = "env",
) -> list[str]:
    selected = [_instance_id(value, "verifier instance_id") for value in instance_ids]
    if not selected:
        raise SweBenchAgentEvalError("official verifier requires instance IDs")
    workers = int(max_workers)
    if workers < 1 or workers > 24:
        raise SweBenchAgentEvalError("max_workers must be between 1 and 24")
    if cache_level not in {"none", "base", "env", "instance"}:
        raise SweBenchAgentEvalError("unsupported SWE-bench cache level")
    return [
        "python",
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        OFFICIAL_DATASET_NAME,
        "--split",
        "test",
        "--predictions_path",
        str(Path(predictions_path).expanduser().resolve(strict=False)),
        "--max_workers",
        str(workers),
        "--run_id",
        _text(run_id, "run_id", 200),
        "--cache_level",
        cache_level,
        "--clean",
        "True",
        "--instance_ids",
        *selected,
    ]


def load_official_instance_results(path: str | Path) -> dict[str, dict[str, object]]:
    source = Path(path).expanduser().resolve(strict=True)
    try:
        if source.suffix.casefold() == ".jsonl":
            values = [
                json.loads(line)
                for line in source.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        else:
            values = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SweBenchAgentEvalError("cannot read official SWE-bench results") from exc
    rows: dict[str, dict[str, object]] = {}
    if isinstance(values, dict):
        if isinstance(values.get("instances"), list):
            iterable: Iterable[object] = values["instances"]
        elif all(isinstance(value, Mapping) for value in values.values()):
            iterable = [
                {"instance_id": key, **dict(value)}
                for key, value in values.items()
                if isinstance(value, Mapping)
            ]
        else:
            raise SweBenchAgentEvalError(
                "official results need per-instance resolution records"
            )
    elif isinstance(values, list):
        iterable = values
    else:
        raise SweBenchAgentEvalError("official results must be JSON or JSONL records")
    for raw in iterable:
        value = _mapping(raw, "official result")
        instance_id = _instance_id(
            value.get("instance_id") or value.get("instanceId"),
            "official result instance_id",
        )
        if instance_id in rows:
            raise SweBenchAgentEvalError("official results contain duplicate instances")
        resolved = value.get("resolved")
        if not isinstance(resolved, bool):
            raise SweBenchAgentEvalError("official result resolved must be boolean")
        rows[instance_id] = {**dict(value), "instanceId": instance_id, "resolved": resolved}
    if not rows:
        raise SweBenchAgentEvalError("official results contain no instances")
    return rows


def build_agent_eval_report(
    *,
    dataset: SweBenchPreparedDataset,
    selected_instance_ids: Sequence[str],
    run_records: Sequence[Mapping[str, object]],
    official_results: Mapping[str, Mapping[str, object]] | None = None,
    model_reference: str = DEFAULT_MODEL_REFERENCE,
    interview_only: bool = True,
) -> dict[str, object]:
    selected = [
        case.instance_id
        for case in dataset.select(
            selected_instance_ids,
            interview_only=interview_only,
        )
    ]
    selected_set = set(selected)
    records: dict[str, Mapping[str, object]] = {}
    for raw in run_records:
        instance_id = _instance_id(raw.get("instanceId"), "run instanceId")
        if instance_id not in selected_set:
            raise SweBenchAgentEvalError("run record is outside the selected split")
        if instance_id in records:
            raise SweBenchAgentEvalError("run records contain duplicate instances")
        records[instance_id] = raw
    official = dict(official_results or {})
    unexpected_official = sorted(set(official) - selected_set)
    if unexpected_official:
        raise SweBenchAgentEvalError("official results include an unselected instance")
    attempted = sum(
        1 for value in records.values() if not bool(value.get("emptyPatch", True))
    )
    terminal = sum(
        1 for value in records.values() if value.get("terminalEvent") == "turn_completed"
    )
    integrity = sum(
        1 for value in records.values() if value.get("integrityPassed") is True
    )
    official_count = len(official)
    resolved = sum(1 for value in official.values() if value.get("resolved") is True)
    denominator = len(selected)
    verified = official_count > 0
    metrics = {
        "denominator": denominator,
        "runCount": len(records),
        "agentTerminalSuccessRate": _ratio(terminal, denominator),
        "patchGenerationRate": _ratio(attempted, denominator),
        "patchIntegrityRate": _ratio(integrity, denominator),
        "officialSubmittedCount": official_count,
        "officialResolvedCount": resolved,
        "resolvedRateTotal": _ratio(resolved, denominator) if verified else None,
        "resolvedRateSubmitted": _ratio(resolved, official_count) if verified else None,
    }
    hard_gates = {
        "splitIntegrity": set(records).issubset(selected_set),
        "goldPatchHidden": dataset.adapter.get("goldPatchExposed") is False,
        "testPatchHidden": dataset.adapter.get("testPatchExposed") is False,
        "historyHidden": all(value.get("historyExposed") is False for value in records.values()),
        "networkDisabled": all(value.get("networkUsed") is False for value in records.values()),
        "testMutationFree": all(
            not list(value.get("testMutationPaths") or [])
            and not list(value.get("hiddenVerifierTestOverlap") or [])
            for value in records.values()
        ),
        "modelIdentity": all(
            str(value.get("modelReference") or "") == model_reference
            and str(value.get("thinkingLevel") or "") == "max"
            for value in records.values()
        ),
        "officialVerifier": verified,
        "localOnly": all(value.get("uploaded") is False for value in records.values()),
    }
    report: dict[str, object] = {
        "schemaVersion": REPORT_SCHEMA_VERSION,
        "passed": bool(records)
        and len(records) == denominator
        and all(hard_gates.values()),
        "localOnly": True,
        "uploaded": False,
        "modelReference": model_reference,
        "thinkingLevel": "max",
        "dataset": {
            "name": "swe-bench-verified",
            "sourceVersion": dataset.source.get("version"),
            "sourceSha256": dataset.source.get("sha256"),
            "fullCount": len(dataset.full_instance_ids),
            "interviewCount": len(dataset.interview_instance_ids),
            "selectedCount": denominator,
            "selectedInstanceIds": selected,
            "selectedSha256": _bundle_sha256({"instanceIds": selected}),
            "goldPatchExposed": False,
            "testPatchExposed": False,
        },
        "metrics": metrics,
        "hardGates": hard_gates,
        "officialResultsPresent": verified,
        "runs": [dict(records[instance_id]) for instance_id in selected if instance_id in records],
    }
    report["reportSha256"] = _bundle_sha256(report)
    return report


def is_test_path(value: str | Path) -> bool:
    path = Path(str(value).replace("\\", "/"))
    lowered_parts = tuple(part.casefold() for part in path.parts)
    name = path.name.casefold()
    return (
        any(part in _TEST_DIRECTORY_NAMES for part in lowered_parts)
        or name in _TEST_FILE_NAMES
        or name.startswith("test_")
        or name.endswith(("_test.py", "_tests.py", ".spec.js", ".spec.ts", ".test.js", ".test.ts"))
    )


def _reject_test_mutation_path(value: object) -> None:
    path = str(value or "").strip()
    if not path:
        return
    if is_test_path(path):
        raise WorkspaceHarnessError(
            "SWE-bench Agent cannot mutate tests; hidden verifier ownership is isolated"
        )


def _validate_archive_members(members: Sequence[tarfile.TarInfo]) -> None:
    for member in members:
        path = Path(member.name)
        if path.is_absolute() or ".." in path.parts or member.name.startswith("/"):
            raise SweBenchAgentEvalError("repository archive contains an unsafe path")
        if member.issym() or member.islnk():
            target = Path(member.linkname)
            if target.is_absolute() or ".." in target.parts:
                raise SweBenchAgentEvalError("repository archive contains an unsafe link")


def _git_prefix(source: Path) -> list[str]:
    if (source / ".git").exists():
        prefix = ["git", "-C", str(source)]
    else:
        prefix = ["git", f"--git-dir={source}"]
    try:
        _run(prefix + ["rev-parse", "--git-dir"])
    except SweBenchAgentEvalError as exc:
        raise SweBenchAgentEvalError("source repository is not a Git repository") from exc
    return prefix


def _repo_slug_from_remote(value: str) -> str:
    text = value.strip()
    matched = re.search(r"(?:github\.com[/:])([^/\s:]+/[^/\s]+?)(?:\.git)?$", text)
    if matched is None:
        return ""
    return matched.group(1).removesuffix(".git")


def _run(
    command: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    allow_empty: bool = False,
) -> str:
    completed = subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
        env=dict(env) if env is not None else None,
    )
    if completed.returncode != 0:
        raise SweBenchAgentEvalError(
            completed.stderr.strip() or f"command failed with exit {completed.returncode}"
        )
    output = completed.stdout.strip()
    if not output and not allow_empty:
        raise SweBenchAgentEvalError("command returned no output")
    return output


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise SweBenchAgentEvalError(f"{field} must be an object")
    return value


def _identifier_array(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise SweBenchAgentEvalError(f"{field} must be a non-empty array")
    return [_instance_id(item, field) for item in value]


def _string_array(value: object, field: str) -> list[str]:
    if not isinstance(value, list):
        raise SweBenchAgentEvalError(f"{field} must be an array")
    result = [str(item).strip() for item in value]
    if any(not item for item in result):
        raise SweBenchAgentEvalError(f"{field} must contain non-empty strings")
    return result


def _instance_id(value: object, field: str) -> str:
    text = str(value or "").strip()
    if _INSTANCE_ID.fullmatch(text) is None:
        raise SweBenchAgentEvalError(f"{field} is not a SWE-bench instance ID")
    return text


def _repo_slug(value: object) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", text) is None:
        raise SweBenchAgentEvalError("SWE-bench repo must be owner/name")
    return text


def _sha1(value: object, field: str) -> str:
    text = str(value or "").strip().lower()
    if _SHA1.fullmatch(text) is None:
        raise SweBenchAgentEvalError(f"{field} must be a SHA-1 commit")
    return text


def _sha256(value: object, field: str) -> str:
    text = str(value or "").strip().lower()
    if _SHA256.fullmatch(text) is None:
        raise SweBenchAgentEvalError(f"{field} must be SHA-256")
    return text


def _text(value: object, field: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum:
        raise SweBenchAgentEvalError(f"{field} must contain 1-{maximum} characters")
    return text


def _bundle_sha256(value: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0
