from __future__ import annotations

"""Read-only evidence projection for the local Agent Lab run archive.

The evaluation runner keeps its Pi SQLite database and JSONL transcripts in a
separate source-local directory.  Those files are useful evidence, but they
must not become ordinary project Sessions or be handed to a Provider again.
This module exposes a small, path-free projection for the Agent Lab surface:
run inventory on the list request and one bounded public transcript on demand.

The projection deliberately omits private thinking, system/developer prompts,
hidden gold, raw SQL, credentials, and filesystem paths.  It is a read model;
it never opens a database for writing and never starts Pi.
"""

import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


_SCHEMA = "rag-ime.eval-lab-evidence.v1"
_REPORT_SCHEMA = "paw.enterpriseops-csm-eval.v1"
_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,159}$")
_MAX_RUNS = 500
_MAX_TASKS = 500
_MAX_TURNS = 800
_MAX_TEXT = 1600
_MAX_TOOL_TEXT = 720
_MAX_TRANSCRIPT_BYTES = 128 * 1024 * 1024
_MAX_TRANSCRIPT_LINE_BYTES = 8 * 1024 * 1024

_PATH_PATTERN = re.compile(
    r"(?<![\w])"
    r"(?:(?:[A-Za-z]:[\\/])|"
    r"/(?:Users|Volumes|private|tmp|var|home|opt|workspace|"
    r"mnt|data|root|usr|etc|srv|dev|System|Library|Applications)"
    r"(?:[\\/]|(?=\s|$)))"
    r"[^\n`'\"<>),;]*?"
    r"(?=(?:[.!?](?:\s|$)|[,;)}\]]|$))",
)
_SQL_PATTERN = re.compile(
    r"\b(?:select\b(?:[^;]{0,3000}\bfrom\b|[^;]{1,3000};)|"
    r"insert\s+into\b|update\s+\S+\s+set\b|delete\s+from\b|"
    r"create\s+(?:table|index|view|trigger|database)\b|alter\s+table\b|"
    r"drop\s+(?:table|index|view|database)\b|truncate\s+table\b|"
    r"with\s+\w+\s+as\s*\(|(?:sql|query)\s*[:=])[^;]{0,3000};?",
    re.IGNORECASE,
)
_HIDDEN_PATTERN = re.compile(
    r"\b(?:(?:hidden|raw)[\s_.-]+(?:gold|answer|expected|value|sql|result)|"
    r"ground[\s_.-]+truth|oracle|"
    r"expected[\s_.-]+(?:value|answer|sql|result))\b"
    r"(?:\s*[:=]\s*[^.;]{0,320})?",
    re.IGNORECASE,
)
_EMAIL_PATTERN = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_LONG_SECRET_PATTERN = re.compile(r"(?<!\w)(?:sk|key|token|secret)[_-]?[A-Za-z0-9_-]{12,}(?!\w)", re.IGNORECASE)
_CREDENTIAL_KEY_PATTERN = re.compile(
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passphrase|"
    r"credential|secret|private[_-]?key|client[_-]?secret|authorization|"
    r"bearer|cookie)",
    re.IGNORECASE,
)
_CREDENTIAL_ASSIGNMENT_PATTERN = re.compile(
    r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passphrase|"
    r"credential|secret|private[_-]?key|client[_-]?secret|authorization|"
    r"bearer)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;)}\]]+)",
    re.IGNORECASE,
)
_TASK_MARKERS = ("customer request:", "user request:", "task request:")
_TASK_END_MARKERS = ("\nwork only through", "\nsuite v2 frozen", "\nstop after the task")


@dataclass(frozen=True)
class _TranscriptStats:
    exists: bool
    sha256: str = ""
    bytes: int = 0
    lines: int = 0
    user_messages: int = 0
    assistant_messages: int = 0
    tool_results: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    tool_names: tuple[str, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    first_user_text: str = ""
    last_assistant_text: str = ""
    provider: str = ""
    model: str = ""
    thinking: str = ""


@dataclass(frozen=True)
class _RunLocation:
    """A read-only pointer to one evaluation archive run.

    The first archive used a ``runs/<id>/paw.sqlite`` layout.  CloudOps, RAG,
    Trace and checked-in receipts use slightly different layouts, so the
    catalog keeps the location metadata separate from the public projection.
    No absolute path from this object is ever returned to the client.
    """

    run_id: str
    directory: Path
    db_path: Path | None = None
    report_path: Path | None = None
    marker_path: Path | None = None
    source_id: str = "enterpriseops"
    source_label: str = "EnterpriseOps 评测研究盘（只读）"
    family: str = "EnterpriseOps CSM"
    title: str = ""
    split: str = ""
    knowledge_path: Path | None = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _public_text(value: object, *, fallback: str = "暂无公开文本。", limit: int = _MAX_TEXT) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return fallback
    text = _PATH_PATTERN.sub("[路径已隐藏]", text)
    text = _SQL_PATTERN.sub("[评测查询已隐藏]", text)
    text = _HIDDEN_PATTERN.sub("[评测内部字段已隐藏]", text)
    text = _CREDENTIAL_ASSIGNMENT_PATTERN.sub("[凭据已隐藏]", text)
    text = _EMAIL_PATTERN.sub("[邮箱已隐藏]", text)
    text = _LONG_SECRET_PATTERN.sub("[凭据已隐藏]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] or fallback


def _message_text(message: Mapping[str, object]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return "\n".join(parts)


def _user_task_text(value: str) -> str:
    """Keep the business request, not the repeated evaluator policy preamble."""

    candidate = value
    lowered = value.casefold()
    for marker in _TASK_MARKERS:
        start = lowered.find(marker)
        if start < 0:
            continue
        candidate = value[start + len(marker):]
        lowered_candidate = candidate.casefold()
        cuts = [
            lowered_candidate.find(end.casefold())
            for end in _TASK_END_MARKERS
            if lowered_candidate.find(end.casefold()) >= 0
        ]
        if cuts:
            candidate = candidate[: min(cuts)]
        break
    return candidate.strip()


def _text_blocks(message: Mapping[str, object]) -> list[str]:
    content = message.get("content")
    if isinstance(content, str):
        return [content] if content.strip() else []
    if not isinstance(content, list):
        return []
    values: list[str] = []
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            values.append(text)
    return values


def _tool_call_blocks(message: Mapping[str, object]) -> list[Mapping[str, object]]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [
        block
        for block in content
        if isinstance(block, Mapping) and block.get("type") in {"toolCall", "tool_call"}
    ]


def _tool_result_text(message: Mapping[str, object]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") not in {"text", "tool_result"}:
            continue
        text = block.get("text")
        if text is None:
            text = block.get("content")
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return "\n".join(parts)


def _public_tool_name(value: object) -> str:
    return _public_text(value, fallback="tool", limit=120)


def _public_argument_keys(value: object) -> list[str]:
    """Expose harmless argument names without surfacing secret-bearing keys."""

    if not isinstance(value, Mapping):
        return []
    result: list[str] = []
    for key in value:
        key_text = str(key)
        if re.search(
            r"(?:prompt|system|developer|gold|answer|sql|path|secret|token|"
            r"credential|password|passphrase|authorization|cookie)",
            key_text,
            re.IGNORECASE,
        ):
            continue
        safe = _public_text(key_text, fallback="", limit=80)
        if safe:
            result.append(safe)
    return sorted(set(result))[:32]


def _timestamp_ms(value: object) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0, int(value))
    text = str(value or "").strip()
    if not text:
        return 0
    # Pi uses ISO timestamps in JSONL.  Avoid importing datetime for malformed
    # historical records; a zero timestamp is an honest unknown.
    try:
        from datetime import datetime

        return max(0, int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000))
    except (TypeError, ValueError, OverflowError):
        return 0


def _usage_values(message: Mapping[str, object]) -> tuple[int, int, int, int]:
    usage = message.get("usage")
    if not isinstance(usage, Mapping):
        return (0, 0, 0, 0)

    def number(*names: str) -> int:
        for name in names:
            value = usage.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return max(0, int(value))
        return 0

    return (
        number("input", "inputTokens", "prompt_tokens"),
        number("output", "outputTokens", "completion_tokens"),
        number("cacheRead", "cache_read_input_tokens", "cacheReadTokens"),
        number("cacheWrite", "cache_creation_input_tokens", "cacheWriteTokens"),
    )


def _safe_source_root(explicit: Path | str | None = None) -> Path | None:
    candidates: list[Path] = []
    if explicit is not None:
        # An explicit path is authoritative.  Falling through to the machine's
        # default archive would make a missing test/deployment binding appear
        # healthy and could expose a different user's evidence.
        candidates.append(Path(explicit).expanduser())
    else:
        configured = os.environ.get("PAW_EVAL_LAB_EVIDENCE_ROOT", "").strip()
        if configured:
            candidates.append(Path(configured).expanduser())
        # The research archive is deliberately discovered only as a sibling of
        # the checkout; no source path is returned to the browser and the env
        # override remains the deployment escape hatch.
        repo_root = _project_root()
        candidates.append(repo_root.parent / "paw-vertical-research" / "incident-changeops")
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_dir() and (resolved / "runs").is_dir():
            return resolved
    return None


def _project_root() -> Path:
    """Resolve the source checkout even when the sidecar runs from an app copy."""

    candidates = [
        os.environ.get("PAW_EVAL_LAB_REPO_ROOT", ""),
        os.environ.get("RAG_IME_SOURCE_ROOT", ""),
        os.environ.get("PAW_WORKSPACE_ROOT", ""),
        os.getcwd(),
        str(Path(__file__).resolve().parents[1]),
    ]
    for raw in candidates:
        if not raw:
            continue
        try:
            candidate = Path(raw).expanduser().resolve()
        except OSError:
            continue
        if (candidate / "eval").is_dir() or (candidate / ".rag-ime-data").is_dir():
            return candidate
    return Path(__file__).resolve().parents[1]


def _read_only_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


class EvalLabEvidenceProjection:
    """Project source-local evaluation evidence without mutation.

    ``incident-changeops`` remains the authoritative EnterpriseOps archive.
    When the service is running from the product checkout (no explicit archive
    override), the projection also discovers the local CloudOps, RAG, Trace
    and public-ledger receipts.  This makes the Lab a complete evidence index
    while keeping every source read-only and preserving missing-evidence
    boundaries.
    """

    def __init__(self, source_root: Path | str | None = None) -> None:
        self._explicit_source_root = source_root is not None or bool(os.environ.get("PAW_EVAL_LAB_EVIDENCE_ROOT", "").strip())
        self.source_root = _safe_source_root(source_root)
        self._stats_cache: dict[str, tuple[int, _TranscriptStats]] = {}
        self._locations: dict[str, _RunLocation] = {}
        self._catalog_cache: dict[str, object] | None = None
        self._catalog_cache_at = 0.0

    def read(self, payload: Mapping[str, object] | None = None) -> dict[str, object]:
        request = payload if isinstance(payload, Mapping) else {}
        catalog = self._catalog()
        run_id = str(request.get("runId") or "").strip()
        task_index = self._task_index(request.get("taskIndex"))
        response: dict[str, object] = {
            "schemaVersion": _SCHEMA,
            "ok": True,
            "source": catalog["source"],
            "sources": catalog.get("sources", []),
            "runs": catalog["runs"],
            "total": len(catalog["runs"]),
        }
        if run_id:
            response["detail"] = self._detail_any(run_id, task_index)
        return response

    @staticmethod
    def _task_index(value: object) -> int | None:
        if value is None or value == "":
            return None
        try:
            parsed = int(str(value))
        except (TypeError, ValueError):
            return None
        # ``0`` is an explicit report-only request; an omitted query remains
        # ``None`` and defaults to the first transcript task.
        return parsed if parsed >= 0 else None

    def _catalog(self) -> dict[str, object]:
        # Catalog construction hashes read-only databases and (for RAG runs)
        # inspects large Knowledge files.  Keep a short-lived projection cache
        # so opening several task drawers does not rescan the archive for every
        # click.  A fresh page request after the TTL observes new files.
        now = time.monotonic()
        if self._catalog_cache is not None and now - self._catalog_cache_at < 10.0:
            return self._catalog_cache
        locations = self._discover_locations()
        self._locations = {location.run_id: location for location in locations}
        runs: list[dict[str, object]] = []
        for location in locations:
            try:
                run = self._run_summary_location(location)
            except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError):
                # One corrupt historical directory should not blank the whole
                # Lab. Keep a visible, evidence-missing row instead.
                run = self._unreadable_run_summary(location)
            runs.append(run)
            if len(runs) >= _MAX_RUNS:
                break
        runs.sort(key=lambda item: (-int(item.get("updatedAtMs") or 0), str(item.get("runId") or "")))
        session_count = sum(int(item.get("sessionCount") or 0) for item in runs)
        transcript_count = sum(int(item.get("transcriptCount") or 0) for item in runs)
        transcript_bytes = sum(int(item.get("transcriptBytes") or 0) for item in runs)
        missing_transcript_count = sum(max(0, int(item.get("sessionCount") or 0) - int(item.get("transcriptCount") or 0)) for item in runs)
        report_only_run_count = sum(int(not int(item.get("sessionCount") or 0) and bool(item.get("reportAvailable"))) for item in runs)
        source_rows: dict[str, dict[str, object]] = {}
        for run in runs:
            source_id = str(run.get("sourceId") or "unknown")
            row = source_rows.setdefault(source_id, {
                "sourceId": source_id,
                "label": str(run.get("sourceLabel") or source_id),
                "available": True,
                "runCount": 0,
                "sessionCount": 0,
                "transcriptCount": 0,
                "transcriptBytes": 0,
                "missingTranscriptCount": 0,
                "reportOnlyRunCount": 0,
            })
            row["runCount"] = int(row["runCount"]) + 1
            row["sessionCount"] = int(row["sessionCount"]) + int(run.get("sessionCount") or 0)
            row["transcriptCount"] = int(row["transcriptCount"]) + int(run.get("transcriptCount") or 0)
            row["transcriptBytes"] = int(row["transcriptBytes"]) + int(run.get("transcriptBytes") or 0)
            row["missingTranscriptCount"] = int(row["missingTranscriptCount"]) + max(0, int(run.get("sessionCount") or 0) - int(run.get("transcriptCount") or 0))
            row["reportOnlyRunCount"] = int(row["reportOnlyRunCount"]) + int(not int(run.get("sessionCount") or 0) and bool(run.get("reportAvailable")))
        sources = sorted(source_rows.values(), key=lambda item: str(item["sourceId"]))
        primary_available = self.source_root is not None
        if not sources and not primary_available:
            label = "本机没有可读的评测证据目录"
        elif not primary_available:
            label = "本机 PAW 评测证据（只读；研究盘未连接）"
        else:
            label = "本机评测证据（只读）"
        result = {
            "source": {
                "available": bool(runs),
                "label": label,
                "runCount": len(runs),
                "sessionCount": session_count,
                "transcriptCount": transcript_count,
                "transcriptBytes": transcript_bytes,
                "missingTranscriptCount": missing_transcript_count,
                "reportOnlyRunCount": report_only_run_count,
                "generatedAtMs": int(time.time() * 1000),
            },
            "sources": sources,
            "runs": runs,
        }
        self._catalog_cache = result
        self._catalog_cache_at = now
        return result

    def _discover_locations(self) -> list[_RunLocation]:
        """Find bounded, local archive pointers.

        An explicit ``source_root`` is intentionally authoritative (used by
        tests and deployments that bind one archive).  The default product
        view discovers the sibling EnterpriseOps archive, the checkout's
        privacy-safe `.rag-ime-data` experiments, and checked-in public
        receipts.  Discovery only looks at known directory shapes; it never
        recursively imports arbitrary files or follows symlinks outside a run.
        """

        locations: list[_RunLocation] = []
        if self.source_root is not None:
            root = self.source_root / "runs"
            try:
                directories = sorted(root.iterdir(), key=lambda item: item.name)
            except OSError:
                directories = []
            for directory in directories:
                if not directory.is_dir() or directory.name.startswith("."):
                    continue
                if not _RUN_ID.fullmatch(directory.name):
                    continue
                locations.append(_RunLocation(
                    run_id=directory.name,
                    directory=directory,
                    db_path=(directory / "paw.sqlite") if (directory / "paw.sqlite").is_file() else None,
                    report_path=(directory / "report.json") if (directory / "report.json").is_file() else None,
                    source_id="enterpriseops-archive",
                    source_label="EnterpriseOps 研究盘（只读）",
                    family="EnterpriseOps CSM",
                ))
        if self._explicit_source_root:
            return locations

        repo_root = _project_root()
        local_root = repo_root / ".rag-ime-data"

        def add(location: _RunLocation) -> None:
            if not _RUN_ID.fullmatch(location.run_id):
                return
            if any(existing.run_id == location.run_id for existing in locations):
                return
            locations.append(location)

        # CloudOps keeps a per-trial observability DB and managed-Pi sessions.
        cloud_root = local_root / "cloudops-agent-eval"
        if cloud_root.is_dir():
            try:
                cloud_dirs = sorted(cloud_root.iterdir(), key=lambda item: item.name)
            except OSError:
                cloud_dirs = []
            for directory in cloud_dirs:
                if not directory.is_dir() or directory.name.startswith("."):
                    continue
                db = directory / "observability.sqlite"
                report = directory / "report.json"
                session_files = self._session_files(directory)
                if not (db.is_file() or report.is_file() or session_files):
                    continue
                add(_RunLocation(
                    run_id=f"cloudops--{directory.name}",
                    directory=directory,
                    db_path=db if db.is_file() else None,
                    report_path=report if report.is_file() else None,
                    source_id="paw-local-cloudops",
                    source_label="PAW 本地 CloudOps 评测（只读）",
                    family="CloudOps",
                    title=directory.name,
                ))

        # EnterpriseOps Validation candidates use a source-local PAW database
        # and managed-Pi transcript directory.  They intentionally stay out of
        # the ordinary Session store, but Agent Lab still needs a read-only,
        # redacted path back to the actual conversation and Tool receipts.
        enterpriseops_root = local_root / "enterpriseops-agent-eval"
        if enterpriseops_root.is_dir():
            try:
                enterpriseops_dirs = sorted(enterpriseops_root.iterdir(), key=lambda item: item.name)
            except OSError:
                enterpriseops_dirs = []
            for directory in enterpriseops_dirs:
                if not directory.is_dir() or directory.name.startswith("."):
                    continue
                db = directory / "paw.sqlite"
                report = directory / "report.json"
                session_files = self._session_files(directory)
                if not (db.is_file() or report.is_file() or session_files):
                    continue
                add(_RunLocation(
                    run_id=f"enterpriseops-local--{directory.name}",
                    directory=directory,
                    db_path=db if db.is_file() else None,
                    report_path=report if report.is_file() else None,
                    source_id="paw-local-enterpriseops",
                    source_label="PAW 本地 EnterpriseOps 评测（只读）",
                    family="EnterpriseOps CSM",
                    title=self._run_title(directory.name),
                    split=self._split_from_name(directory.name),
                ))

        # Trace/RAG source captures use the same Pi DB shape but may have a
        # report-only verification directory.
        trace_root = local_root / "trace-agent"
        if trace_root.is_dir():
            try:
                trace_dirs = sorted(trace_root.iterdir(), key=lambda item: item.name)
            except OSError:
                trace_dirs = []
            for directory in trace_dirs:
                if not directory.is_dir() or directory.name.startswith("."):
                    continue
                db = directory / "agent.sqlite"
                report = directory / "report.json"
                manifest = directory / "public-manifest.json"
                session_files = self._session_files(directory)
                if not (db.is_file() or report.is_file() or manifest.is_file() or session_files):
                    continue
                add(_RunLocation(
                    run_id=f"trace--{directory.name}",
                    directory=directory,
                    db_path=db if db.is_file() else None,
                    report_path=report if report.is_file() else (manifest if manifest.is_file() else None),
                    source_id="paw-local-trace",
                    source_label="PAW 本地 Trace / RAG 证据（只读）",
                    family="Trace / RAG",
                    title=directory.name,
                ))

        # RAG ablation runs put the Agent DB one level below the experiment and
        # a corpus marker below knowledge-runs.  Do not enumerate every source
        # document; only expose corpus counts and the session transcript.
        rag_runs_root = local_root / "runs"
        if rag_runs_root.is_dir():
            try:
                experiment_dirs = sorted(rag_runs_root.glob("rag-agent-ablation*/run-*"), key=lambda item: item.as_posix())
            except OSError:
                experiment_dirs = []
            for directory in experiment_dirs:
                if not directory.is_dir():
                    continue
                db = directory / "agent.sqlite"
                marker_candidates = sorted(directory.glob("knowledge-runs/*/.rag-benchmark-run.json"))
                marker = marker_candidates[0] if marker_candidates else None
                session_files = self._session_files(directory)
                if not (db.is_file() or marker is not None or session_files):
                    continue
                knowledge_db = None
                try:
                    candidates = sorted(directory.glob("knowledge-runs/*/knowledge/knowledge.sqlite"))
                    knowledge_db = candidates[0] if candidates else None
                except OSError:
                    pass
                add(_RunLocation(
                    run_id=f"rag--{directory.parent.name}--{directory.name}",
                    directory=directory,
                    db_path=db if db.is_file() else None,
                    marker_path=marker,
                    source_id="paw-local-rag",
                    source_label="PAW 本地 Knowledge RAG 评测（只读）",
                    family="Enterprise RAG",
                    title=f"{directory.parent.name} · {directory.name}",
                    knowledge_path=knowledge_db,
                ))

        # The checked-in ledger contains public receipts for runs whose raw
        # transcript was intentionally kept elsewhere.  Showing these rows is
        # important: a human can see the result and the explicit “transcript
        # unavailable” boundary instead of mistaking a missing file for a pass.
        ledger_root = repo_root / "eval" / "interview-metrics" / "runs"
        if ledger_root.is_dir():
            try:
                ledger_files = sorted(ledger_root.glob("*.json"), key=lambda item: item.name)
            except OSError:
                ledger_files = []
            for report in ledger_files:
                add(_RunLocation(
                    run_id=f"ledger--{report.stem}",
                    directory=report.parent,
                    report_path=report,
                    source_id="checked-in-ledger",
                    source_label="仓库公开评测回执（只读）",
                    family="公开评测回执",
                    title=report.stem,
                ))
        return locations

    @staticmethod
    def _session_files(directory: Path) -> list[Path]:
        files: list[Path] = []
        for pattern in ("agent/sessions/*.jsonl", "sessions/*.jsonl"):
            try:
                files.extend(path for path in directory.glob(pattern) if path.is_file())
            except OSError:
                continue
        unique: dict[str, Path] = {path.resolve().as_posix(): path for path in files}
        return sorted(unique.values(), key=lambda item: item.name)

    def _unreadable_run_summary(self, location: _RunLocation) -> dict[str, object]:
        return {
            "runId": location.run_id,
            "title": self._safe_title(location.title or self._run_title(location.directory.name)),
            "family": location.family,
            "sourceId": location.source_id,
            "sourceLabel": location.source_label,
            "split": location.split or "unknown",
            "status": "evidence_unavailable",
            "evidenceKind": "unavailable",
            "reportAvailable": bool(location.report_path and location.report_path.is_file()),
            "databaseAvailable": bool(location.db_path and location.db_path.is_file()),
            "sessionCount": 0,
            "transcriptCount": 0,
            "transcriptBytes": 0,
            "missingTranscriptCount": 0,
            "reportOnlyRunCount": int(bool(location.report_path and location.report_path.is_file())),
            "metrics": {},
            "tasks": [],
            "environment": {},
            "updatedAtMs": 0,
        }

    def _run_summary(self, directory: Path) -> dict[str, object]:
        return self._run_summary_location(_RunLocation(
            run_id=directory.name,
            directory=directory,
            db_path=(directory / "paw.sqlite") if (directory / "paw.sqlite").is_file() else None,
            report_path=(directory / "report.json") if (directory / "report.json").is_file() else None,
        ))

    def _run_summary_location(self, location: _RunLocation) -> dict[str, object]:
        directory = location.directory
        db_path = location.db_path
        report_path = location.report_path
        report = self._load_json(report_path) if report_path and report_path.is_file() else {}
        marker = self._load_json(location.marker_path) if location.marker_path and location.marker_path.is_file() else {}
        # A RAG benchmark marker is a report-like public receipt, but it is
        # intentionally kept separate from the optional runner report.
        report_view = report or marker
        database_sha = _sha256(db_path) if db_path and db_path.is_file() else ""
        report_sha = _sha256(report_path) if report_path and report_path.is_file() else ""
        marker_sha = _sha256(location.marker_path) if location.marker_path and location.marker_path.is_file() else ""
        source_sessions = self._source_sessions(db_path, directory) if db_path and db_path.is_file() else self._file_sessions(directory)
        has_sessions = bool(source_sessions)
        report_tasks = self._report_tasks(report_view)
        # A public report may contain per-task verifier results even when the
        # original Pi transcript was not copied into the evidence directory.
        # Keep those rows visible as report-only tasks instead of collapsing the
        # entire run into one opaque card.  They deliberately have no session
        # identity and can never be mistaken for a transcript.
        task_rows = source_sessions or [
            {
                "title": self._report_task_title(item, index),
                "model_profile": self._report_model(report_view),
                "thinking_level": self._report_thinking(report_view),
                "execution_mode": self._report_execution_mode(report_view),
                "created_at_ms": 0,
                "updated_at_ms": 0,
                "message_count": 0,
                "transcript_ref": "",
                "external_session_id": "",
            }
            for index, item in enumerate(report_tasks, start=1)
        ]
        tasks: list[dict[str, object]] = []
        for index, row in enumerate(task_rows, start=1):
            title = str(row.get("title") or f"Task {index}")
            task_record = self._match_report_task(title, report_tasks, index)
            report_tool_failures = 0
            report_tool_receipt_complete = False
            if task_record:
                explicit_failures = task_record.get("failedToolCalls")
                tool_calls = task_record.get("toolCalls")
                successful_tool_calls = task_record.get("successfulToolCalls")
                if isinstance(explicit_failures, (int, float)) and not isinstance(explicit_failures, bool):
                    report_tool_failures = max(0, int(explicit_failures))
                    report_tool_receipt_complete = True
                elif (
                    isinstance(tool_calls, (int, float))
                    and not isinstance(tool_calls, bool)
                    and isinstance(successful_tool_calls, (int, float))
                    and not isinstance(successful_tool_calls, bool)
                ):
                    report_tool_failures = max(0, int(tool_calls) - int(successful_tool_calls))
                    report_tool_receipt_complete = True
            transcript_path = self._safe_transcript_path(directory, row.get("transcript_ref"))
            stats = self._transcript_stats(transcript_path) if transcript_path else _TranscriptStats(False)
            task = {
                "taskIndex": index,
                "taskLabel": self._task_label(title, index),
                "title": self._safe_title(title),
                "transcriptAvailable": stats.exists,
                "transcriptSha256": stats.sha256,
                "transcriptBytes": stats.bytes,
                "jsonlLines": stats.lines,
                "userMessages": stats.user_messages,
                "assistantMessages": stats.assistant_messages,
                "toolCalls": int((task_record.get("toolCalls") or 0) if task_record else stats.tool_calls),
                "toolFailures": report_tool_failures if task_record else stats.tool_failures,
                "toolReceiptComplete": stats.exists or report_tool_receipt_complete,
                "toolNames": list(stats.tool_names),
                "inputTokens": stats.input_tokens,
                "outputTokens": stats.output_tokens,
                "cacheReadTokens": stats.cache_read_tokens,
                "cacheWriteTokens": stats.cache_write_tokens,
                "externalSessionRef": hashlib.sha256(str(row.get("external_session_id") or "").encode()).hexdigest()[:12] if row.get("external_session_id") else "",
                "provider": _public_text(stats.provider, fallback="", limit=160),
                "model": _public_text(stats.model or str(row.get("model_profile") or ""), fallback="", limit=200),
                "thinking": _public_text(stats.thinking or str(row.get("thinking_level") or ""), fallback="", limit=80),
                "executionMode": _public_text(str(row.get("execution_mode") or "unknown"), fallback="unknown", limit=120),
                "createdAtMs": int(row.get("created_at_ms") or 0),
                "updatedAtMs": int(row.get("updated_at_ms") or 0),
                "evidenceStatus": "available" if stats.exists else "transcript_missing" if has_sessions else "report_only",
            }
            if task_record:
                task.update(self._task_metrics(task_record))
            tasks.append(task)
        lane = report_view.get("lane") if isinstance(report_view, Mapping) else {}
        lane = lane if isinstance(lane, Mapping) else {}
        contract = report_view.get("evaluationContract") if isinstance(report_view, Mapping) else {}
        contract = contract if isinstance(contract, Mapping) else {}
        runtime = report_view.get("runtimeIdentity") if isinstance(report_view, Mapping) else {}
        runtime = runtime if isinstance(runtime, Mapping) else {}
        environment = self._environment(source_sessions, report_view, contract, runtime, tasks, location)
        updated = max([int(item.get("updatedAtMs") or 0) for item in tasks] or [0])
        if report_path and report_path.is_file():
            try:
                updated = max(updated, int(report_path.stat().st_mtime_ns // 1_000_000))
            except OSError:
                pass
        if location.marker_path and location.marker_path.is_file():
            try:
                updated = max(updated, int(location.marker_path.stat().st_mtime_ns // 1_000_000))
            except OSError:
                pass
        report_available = bool(
            (report_path and report_path.is_file())
            or (location.marker_path and location.marker_path.is_file())
        )
        raw_status = report_view.get("status")
        status = _public_text(raw_status, fallback="", limit=120)
        if not status:
            # A transcript or a report is evidence, but neither proves that an
            # evaluation completed. Keep the evidence boundary visible when a
            # historical receipt omitted its own status field.
            if has_sessions and report_available:
                status = "evidence_available"
            elif has_sessions:
                status = "transcript_only"
            elif report_available:
                status = "report_only"
            else:
                status = "evidence_unavailable"
        return {
            "runId": location.run_id,
            "title": self._safe_title(location.title or self._run_title(directory.name)),
            "family": location.family or ("EnterpriseOps CSM" if "csm" in directory.name else "EnterpriseOps diagnostics"),
            "sourceId": location.source_id,
            "sourceLabel": location.source_label,
            "split": _public_text(
                report_view.get("split")
                or contract.get("split")
                or location.split
                or self._split_from_name(directory.name),
                fallback="unknown",
                limit=120,
            ),
            "status": status,
            "evidenceKind": "transcript_and_report" if has_sessions and report_available else "transcript_only" if has_sessions else "report_only" if report_available else "unavailable",
            "reportAvailable": report_available,
            "databaseAvailable": bool(db_path and db_path.is_file()),
            "reportSha256": report_sha,
            "databaseSha256": database_sha,
            "markerSha256": marker_sha,
            "sessionCount": len(source_sessions),
            "transcriptCount": sum(bool(item.get("transcriptAvailable")) for item in tasks),
            "transcriptBytes": sum(int(item.get("transcriptBytes") or 0) for item in tasks),
            "missingTranscriptCount": max(0, len(source_sessions) - sum(bool(item.get("transcriptAvailable")) for item in tasks)),
            "reportOnlyRunCount": int(not has_sessions and report_available),
            "metrics": self._lane_metrics(lane, report_view),
            "environment": environment,
            "tasks": tasks[:_MAX_TASKS],
            "artifacts": self._artifact_projection(location, report_view),
            "updatedAtMs": updated,
        }

    def _detail(self, run_id: str, task_index: int | None) -> dict[str, object]:
        if not _RUN_ID.fullmatch(run_id):
            return {"status": "invalid_run_id", "runId": run_id}
        if self.source_root is None:
            return {"status": "source_unavailable", "runId": run_id, "message": "评测研究盘当前不可用。"}
        location = self._locations.get(run_id)
        if location is None:
            directory = self.source_root / "runs" / run_id
            if not directory.is_dir():
                return {"status": "not_found", "runId": run_id}
            location = _RunLocation(
                run_id=run_id,
                directory=directory,
                db_path=(directory / "paw.sqlite") if (directory / "paw.sqlite").is_file() else None,
                report_path=(directory / "report.json") if (directory / "report.json").is_file() else None,
            )
        return self._detail_location(location, task_index)

    def _detail_any(self, run_id: str, task_index: int | None) -> dict[str, object]:
        """Resolve a catalog id and expose either one task or a report-only run."""

        if self._locations:
            location = self._locations.get(run_id)
            if location is not None:
                return self._detail_location(location, task_index)
        return self._detail(run_id, task_index)

    def _detail_location(self, location: _RunLocation, task_index: int | None) -> dict[str, object]:
        run_id = location.run_id
        directory = location.directory
        summary = self._run_summary_location(location)
        tasks = summary.get("tasks") if isinstance(summary.get("tasks"), list) else []
        source_rows = self._source_sessions(location.db_path, directory) if location.db_path and location.db_path.is_file() else self._file_sessions(directory)
        report_path = location.report_path or location.marker_path
        # taskIndex=0 always means the run-level report. It must not silently
        # fall through to Task 1 when the same run also retained Sessions.
        if task_index == 0:
            report_text = self._report_public_summary(report_path)
            return {
                "status": "report_available" if report_path and source_rows else "report_only" if report_path else "report_unavailable",
                "runId": run_id,
                "summary": summary,
                "environment": summary.get("environment") or {},
                "turns": [],
                "tools": [],
                "report": report_text,
                "protected": self._protected_projection(),
            }
        if not tasks:
            report_text = self._report_public_summary(report_path)
            return {
                "status": "report_only" if report_path else "no_sessions",
                "runId": run_id,
                "summary": summary,
                "environment": summary.get("environment") or {},
                "turns": [],
                "tools": [],
                "report": report_text,
                "protected": self._protected_projection(),
            }
        index = task_index or 1
        selected = next((item for item in tasks if isinstance(item, Mapping) and int(item.get("taskIndex") or 0) == index), None)
        if selected is None:
            return {"status": "task_not_found", "runId": run_id, "taskIndex": index, "summary": summary}
        row = source_rows[index - 1] if index <= len(source_rows) else {}
        transcript = self._safe_transcript_path(directory, row.get("transcript_ref"))
        if transcript is None:
            evidence_status = str(selected.get("evidenceStatus") or "")
            return {
                "status": "report_only" if evidence_status == "report_only" and report_path else "transcript_missing",
                "runId": run_id,
                "taskIndex": index,
                "task": selected,
                "summary": summary,
                "environment": summary.get("environment") or {},
                "turns": [],
                "tools": [],
                "report": self._report_public_summary(report_path),
                "protected": self._protected_projection(),
            }
        turns, tools, usage = self._public_transcript(transcript)
        detail = {
            "status": "available",
            "runId": run_id,
            "taskIndex": index,
            "task": selected,
            "session": {
                "title": self._safe_title(str(row.get("title") or selected.get("title") or f"Task {index}")),
                "model": _public_text(str(row.get("model_profile") or "") or str(usage.get("model", "")), fallback="", limit=200),
                "thinking": _public_text(str(row.get("thinking_level") or "") or str(usage.get("thinking", "")), fallback="", limit=80),
                "executionMode": _public_text(str(row.get("execution_mode") or "unknown"), fallback="unknown", limit=120),
                "sessionMode": _public_text(str(row.get("session_mode") or "assistant"), fallback="assistant", limit=120),
                "messageCount": int(row.get("message_count") or 0),
            },
            "environment": {
                **(summary.get("environment") if isinstance(summary.get("environment"), Mapping) else {}),
                "tokenUsage": usage,
            },
            "summary": summary,
            "report": self._report_public_summary(report_path),
            "turns": turns,
            "tools": tools,
            "protected": self._protected_projection(),
        }
        return detail

    def _source_sessions(self, db_path: Path | None, directory: Path | None = None) -> list[dict[str, object]]:
        if db_path is None:
            return self._file_sessions(directory) if directory is not None else []
        connection = _read_only_connection(db_path)
        try:
            cols = {str(row[1]) for row in connection.execute("pragma table_info(agent_sessions)").fetchall()}
            if not cols:
                return []
            wanted = [
                "id", "pi_session_id", "session_file",
                "title", "model_profile", "thinking_level", "session_mode", "execution_mode",
                "created_at_ms", "updated_at_ms", "message_count",
            ]
            select = [f's."{name}"' for name in wanted if name in cols]
            order_columns = [f's."{name}"' for name in ("created_at_ms", "id") if name in cols]
            order_sql = f" order by {', '.join(order_columns)}" if order_columns else ""
            binding_cols = {str(row[1]) for row in connection.execute("pragma table_info(agent_runtime_bindings)").fetchall()}
            if not binding_cols:
                rows = connection.execute("select " + ",".join(select) + " from agent_sessions s" + order_sql).fetchall()
                result = [dict(row) for row in rows]
                for item in result:
                    item.setdefault("transcript_ref", item.get("session_file"))
                    item.setdefault("external_session_id", item.get("pi_session_id"))
                return result
            select.extend(["b.external_session_id", "b.transcript_ref"])
            rows = connection.execute(
                "select " + ",".join(select) + " from agent_sessions s left join agent_runtime_bindings b on b.session_id=s.id" + order_sql
            ).fetchall()
            result = [dict(row) for row in rows]
            for item in result:
                if not item.get("transcript_ref"):
                    item["transcript_ref"] = item.get("session_file")
                if not item.get("external_session_id"):
                    item["external_session_id"] = item.get("pi_session_id")
            return result
        finally:
            connection.close()

    def _file_sessions(self, directory: Path) -> list[dict[str, object]]:
        """Build minimal session rows for report/source folders without a DB."""

        rows: list[dict[str, object]] = []
        for index, path in enumerate(self._session_files(directory), start=1):
            rows.append({
                "title": path.stem,
                "session_file": path.as_posix(),
                "transcript_ref": path.as_posix(),
                "external_session_id": path.stem,
                "created_at_ms": 0,
                "updated_at_ms": 0,
                "message_count": 0,
                "session_mode": "assistant",
                "execution_mode": "read_only",
            })
        return rows

    def _safe_transcript_path(self, directory: Path, value: object) -> Path | None:
        if not value:
            return None
        candidate = Path(str(value))
        if not candidate.is_absolute():
            candidate = directory / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return None
        try:
            if not resolved.is_file() or not resolved.is_relative_to(directory.resolve()):
                return None
            if resolved.stat().st_size > _MAX_TRANSCRIPT_BYTES:
                return None
        except OSError:
            return None
        return resolved

    def _transcript_stats(self, path: Path) -> _TranscriptStats:
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return _TranscriptStats(False)
        cached = self._stats_cache.get(path.as_posix())
        if cached and cached[0] == stamp:
            return cached[1]
        stats = self._parse_transcript(path, include_turns=False)[2]
        self._stats_cache[path.as_posix()] = (stamp, stats)
        return stats

    def _parse_transcript(self, path: Path, *, include_turns: bool) -> tuple[list[dict[str, object]], list[dict[str, object]], _TranscriptStats]:
        digest = hashlib.sha256()
        turns: list[dict[str, object]] = []
        tools: list[dict[str, object]] = []
        lines = user = assistant = tool_results = tool_calls = failures = 0
        names: list[str] = []
        input_tokens = output_tokens = cache_read = cache_write = 0
        first_user = last_assistant = provider = model = thinking = ""
        with path.open("rb") as stream:
            for raw in stream:
                if len(raw) > _MAX_TRANSCRIPT_LINE_BYTES:
                    continue
                digest.update(raw)
                lines += 1
                try:
                    payload = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, Mapping):
                    continue
                kind = str(payload.get("type") or "")
                if kind == "model_change":
                    provider = _public_text(payload.get("provider"), fallback=provider, limit=160)
                    model = _public_text(payload.get("modelId"), fallback=model, limit=200)
                elif kind == "thinking_level_change":
                    thinking = _public_text(payload.get("thinkingLevel"), fallback=thinking, limit=80)
                if kind != "message":
                    continue
                message = payload.get("message")
                if not isinstance(message, Mapping):
                    continue
                role = str(message.get("role") or "")
                timestamp = _timestamp_ms(message.get("timestamp") or payload.get("timestamp"))
                message_id = hashlib.sha256(str(payload.get("id") or f"line-{lines}").encode()).hexdigest()[:12]
                if role == "user":
                    user += 1
                    text_values = _text_blocks(message)
                    text = _public_text(_user_task_text("\n".join(text_values)), limit=_MAX_TEXT)
                    if not first_user and text_values:
                        first_user = text
                    if include_turns and text_values:
                        turns.append({"kind": "message", "role": "user", "text": text, "timestampMs": timestamp, "entryRef": message_id})
                elif role == "assistant":
                    assistant += 1
                    text_values = _text_blocks(message)
                    if text_values:
                        last_assistant = _public_text("\n".join(text_values), limit=_MAX_TEXT)
                        if include_turns:
                            turns.append({"kind": "message", "role": "assistant", "text": last_assistant, "timestampMs": timestamp, "entryRef": message_id})
                    for block in _tool_call_blocks(message):
                        name = _public_tool_name(block.get("name") or block.get("toolName"))
                        if name not in names:
                            names.append(name)
                        tool_calls += 1
                        if include_turns:
                            turns.append({
                                "kind": "tool_call",
                                "role": "assistant",
                                "toolName": name,
                                "text": f"调用工具：{name}",
                                "argumentKeys": _public_argument_keys(block.get("arguments")),
                                "timestampMs": timestamp,
                                "entryRef": message_id,
                            })
                elif role == "toolResult":
                    tool_results += 1
                    is_error = bool(message.get("isError"))
                    failures += int(is_error)
                    name = _public_tool_name(message.get("toolName"))
                    if name not in names:
                        names.append(name)
                    result_text = _public_text(_tool_result_text(message), fallback="工具返回了结果。", limit=_MAX_TOOL_TEXT)
                    if include_turns:
                        turns.append({
                            "kind": "tool_result",
                            "role": "tool",
                            "toolName": name,
                            "status": "failed" if is_error else "completed",
                            "text": result_text,
                            "timestampMs": timestamp,
                            "entryRef": message_id,
                        })
                        tools.append({
                            "toolName": name,
                            "status": "failed" if is_error else "completed",
                            "text": result_text,
                            "timestampMs": timestamp,
                        })
                usage = _usage_values(message)
                input_tokens += usage[0]
                output_tokens += usage[1]
                cache_read += usage[2]
                cache_write += usage[3]
        stats = _TranscriptStats(
            exists=True,
            sha256=digest.hexdigest(),
            bytes=path.stat().st_size,
            lines=lines,
            user_messages=user,
            assistant_messages=assistant,
            tool_results=tool_results,
            tool_calls=tool_calls,
            tool_failures=failures,
            tool_names=tuple(names[:128]),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            first_user_text=first_user,
            last_assistant_text=last_assistant,
            provider=provider,
            model=model,
            thinking=thinking,
        )
        if include_turns:
            turns = turns[-_MAX_TURNS:]
        return turns, tools, stats

    def _public_transcript(self, path: Path) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
        turns, tools, stats = self._parse_transcript(path, include_turns=True)
        return turns, tools, {
            "provider": stats.provider,
            "model": stats.model,
            "thinking": stats.thinking,
            "inputTokens": stats.input_tokens,
            "outputTokens": stats.output_tokens,
            "cacheReadTokens": stats.cache_read_tokens,
            "cacheWriteTokens": stats.cache_write_tokens,
            "totalTokens": stats.input_tokens + stats.output_tokens,
        }

    def _knowledge_snapshot(self, path: Path) -> dict[str, object]:
        """Return counts/configuration for a RAG database, never its documents."""

        result: dict[str, object] = {
            "available": False,
            "databaseSha256": "",
            "documentCount": 0,
            "chunkCount": 0,
            "denseVectorCount": 0,
            "graphNodeCount": 0,
            "graphEdgeCount": 0,
            "parserMode": "",
            "configRevision": 0,
        }
        try:
            result["databaseSha256"] = _sha256(path)
            connection = _read_only_connection(path)
            try:
                def count(table: str) -> int:
                    try:
                        return max(0, int(connection.execute(f'select count(*) from "{table}"').fetchone()[0]))
                    except sqlite3.Error:
                        return 0

                result["documentCount"] = count("knowledge_documents")
                result["chunkCount"] = count("knowledge_chunks")
                result["denseVectorCount"] = count("knowledge_dense_chunks")
                result["graphNodeCount"] = count("knowledge_graph_nodes")
                result["graphEdgeCount"] = count("knowledge_graph_edges")
                try:
                    row = connection.execute("select parser_mode, config_revision from knowledge_bases order by updated_at_ms desc limit 1").fetchone()
                    if row:
                        result["parserMode"] = _public_text(row[0], fallback="", limit=120)
                        result["configRevision"] = max(0, int(row[1] or 0))
                except sqlite3.Error:
                    pass
                result["available"] = True
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            pass
        return result

    def _artifact_projection(self, location: _RunLocation, report: Mapping[str, object]) -> list[dict[str, object]]:
        artifacts: list[dict[str, object]] = []

        def add(kind: str, label: str, path: Path | None) -> None:
            if path is None or not path.is_file():
                return
            try:
                stat = path.stat()
                artifacts.append({
                    "kind": kind,
                    "label": label,
                    "available": True,
                    "bytes": int(stat.st_size),
                    "sha256": _sha256(path),
                })
            except OSError:
                artifacts.append({"kind": kind, "label": label, "available": False, "bytes": 0, "sha256": ""})

        add("report", "公开评测报告", location.report_path)
        add("marker", "Knowledge 运行 marker", location.marker_path)
        add("database", "只读运行数据库", location.db_path)
        add("knowledge", "只读 Knowledge 数据库", location.knowledge_path)
        # A transcript artifact is represented by per-task hashes in the run
        # itself; this aggregate count helps a reviewer see what is missing.
        transcript_count = len([path for path in self._session_files(location.directory) if path.is_file()])
        if transcript_count:
            artifacts.append({"kind": "transcripts", "label": f"JSONL transcript（{transcript_count} 份）", "available": True, "bytes": 0, "sha256": ""})
        return artifacts

    def _report_public_summary(self, path: Path | None) -> dict[str, object]:
        if path is None or not path.is_file():
            return {"available": False}
        report = self._load_json(path)
        if not report:
            return {"available": False}
        summary: dict[str, object] = {
            "available": True,
            "schemaVersion": _public_text(report.get("schemaVersion"), fallback="", limit=160),
            "status": _public_text(report.get("status"), fallback="", limit=120),
            "decision": _public_text(report.get("decision"), fallback="", limit=160),
        }
        # These are the public, reviewer-useful sections found across the
        # historical receipts.  Keep the allow-list explicit: reports may also
        # contain prompts, hidden gold, SQL, or private paths.
        public_sections = (
            "metrics", "comparison", "failedHardGates", "provenanceGaps", "boundaries",
            "executiveVerdict", "decision", "claim", "runtime", "runtimeIdentity",
            "environment", "configuration", "dataset", "sample", "result", "usage",
            "estimate", "billing", "pricingIdentity", "authority", "runtimeCostReceipt",
            "observabilityLimit", "verification",
            "evidence", "frozenIdentity", "baseline", "winner", "oneShot",
            "evaluationScope", "evaluationMode", "heldOutEvaluated", "formalAcceptanceEligible",
            "formalAcceptancePassed", "heldOutGateProduced", "cleanupPassed", "localOnly",
            "uploaded", "elapsedMs", "startedAtMs", "completedAtMs", "measuredAt",
        )
        for key in public_sections:
            value = report.get(key)
            if value is None:
                continue
            if isinstance(value, str):
                # The section name is an allow-list, not a trust boundary:
                # historical receipts can still contain an accidental path,
                # query, prompt fragment or credential in a scalar field.
                summary[key] = _public_text(value, fallback="", limit=320)
            elif isinstance(value, (int, float, bool)):
                summary[key] = value
            elif isinstance(value, list):
                # Keep labels/counts but never copy hidden answer data. Nested
                # lists are walked too; leaving one opaque JSON value untouched
                # would bypass the same redaction used for mappings.
                summary[key] = self._safe_report_list(value, depth=0, limit=24)
            elif isinstance(value, Mapping):
                summary[key] = self._safe_report_mapping(value, depth=0)
        return summary

    def _safe_report_list(
        self,
        value: list[object],
        *,
        depth: int,
        limit: int = 16,
    ) -> list[object]:
        if depth > 2:
            return ["[评测内部内容已省略]"] if value else []
        result: list[object] = []
        for item in value[:limit]:
            if isinstance(item, str):
                result.append(_public_text(item, fallback="", limit=240))
            elif isinstance(item, Mapping):
                result.append(self._safe_report_mapping(item, depth=depth + 1))
            elif isinstance(item, list):
                result.append(self._safe_report_list(item, depth=depth + 1))
            elif isinstance(item, (int, float, bool)) or item is None:
                result.append(item)
        return result

    def _safe_report_mapping(self, value: Mapping[str, object], *, depth: int) -> dict[str, object]:
        if depth > 2:
            return {"present": True}
        result: dict[str, object] = {}
        for key, item in value.items():
            key_text = str(key)
            key_folded = key_text.casefold()
            public_metric_key = bool(re.search(
                r"(?:rate|count|calls|ms|seconds|bytes|coverage|recall|mrr|ndcg|correctness|score|passed|total|delta|percent|available|cleaned|deleted)$",
                key_folded,
            ))
            public_hash_key = key_folded.endswith("sha256") or key_folded.endswith("hash")
            # Token *counts* are useful cost evidence; credential/token values
            # are not.  Keep the explicit usage vocabulary and drop ambiguous
            # auth fields such as apiToken/accessToken.
            safe_usage_key = key_folded in {
                "inputtokens", "outputtokens", "prompttokens", "completiontokens",
                "totaltokens", "cachedinputtokens", "uncachedinputtokens",
                "cachereadtokens", "cachewritetokens", "cache_read_input_tokens",
                "cache_creation_input_tokens", "usageavailable",
            }
            sensitive_key = bool(
                re.search(
                    r"gold|sql|credential|secret|path|internal|private|reasoning|"
                    r"analysis|thought|prompt|instruction|systemmessage|developermessage",
                    key_folded,
                )
                or _CREDENTIAL_KEY_PATTERN.search(key_folded)
            )
            sensitive_body_key = key_folded in {
                "answer", "answertext", "answerbody", "outputtext", "prompt", "prompttext",
                "promptbody", "systemprompt", "developerprompt", "rawoutput", "rawanswer",
                "reasoning", "analysis", "chainofthought", "privatereasoning",
                "internalreasoning", "thoughts", "thinkingtext",
            }
            if sensitive_key and not public_metric_key and not public_hash_key and not safe_usage_key:
                continue
            if sensitive_body_key and not public_metric_key and not public_hash_key and not safe_usage_key:
                continue
            if "prompt" in key_folded and not (public_metric_key or public_hash_key or safe_usage_key):
                continue
            if re.search(r"token", key_text, re.IGNORECASE) and not (safe_usage_key or public_metric_key):
                continue
            if isinstance(item, str):
                result[key_text] = _public_text(item, fallback="", limit=320)
            elif isinstance(item, Mapping):
                result[key_text] = self._safe_report_mapping(item, depth=depth + 1)
            elif isinstance(item, list):
                result[key_text] = self._safe_report_list(item, depth=depth + 1)
            elif isinstance(item, (int, float, bool)) or item is None:
                result[key_text] = item
        return result

    def _load_json(self, path: Path) -> dict[str, object]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return dict(value) if isinstance(value, Mapping) else {}

    def _report_tasks(self, report: Mapping[str, object]) -> list[Mapping[str, object]]:
        lane = report.get("lane")
        if isinstance(lane, Mapping):
            tasks = lane.get("tasks")
            if isinstance(tasks, list):
                return [item for item in tasks if isinstance(item, Mapping)]
        # CloudOps reports expose one public receipt per assigned batch.
        batches = report.get("batches")
        if isinstance(batches, list):
            return [item for item in batches if isinstance(item, Mapping)]
        tasks = report.get("tasks")
        if isinstance(tasks, list):
            return [item for item in tasks if isinstance(item, Mapping)]
        singular = report.get("task")
        if isinstance(singular, Mapping):
            return [singular]
        lanes = report.get("lanes")
        if isinstance(lanes, Mapping):
            return [
                {"taskId": str(name), **dict(value)}
                for name, value in lanes.items()
                if isinstance(value, Mapping)
            ]
        if isinstance(lanes, list):
            # RAG ablation reports keep four diagnostic lanes in one receipt.
            # The final accountable result is the agentic lane; expose only
            # its per-case gate projection so the UI can compare two complete
            # runs without copying answers, citations, qrels, or hidden Gold.
            agentic = next(
                (
                    item for item in lanes
                    if isinstance(item, Mapping)
                    and str(item.get("lane") or "") == "agentic"
                ),
                None,
            )
            if isinstance(agentic, Mapping):
                score = agentic.get("score")
                score = score if isinstance(score, Mapping) else {}
                answer_cases = score.get("answerCases")
                if isinstance(answer_cases, list):
                    terminal = str(agentic.get("terminalEvent") or "unknown")
                    projected: list[Mapping[str, object]] = []
                    for raw_case in answer_cases:
                        if not isinstance(raw_case, Mapping):
                            continue
                        case_id = str(
                            raw_case.get("evaluationCaseId")
                            or raw_case.get("queryId")
                            or ""
                        ).strip()
                        if not case_id:
                            continue
                        gate_names = (
                            "answerSuccess",
                            "citationSupport",
                            "abstentionCorrect",
                            "toolSuccess",
                        )
                        gates = [
                            (name, raw_case.get(name))
                            for name in gate_names
                            if isinstance(raw_case.get(name), bool)
                        ]
                        failed_names = [name for name, passed in gates if not passed]
                        verifier_results = [
                            {"verifierIndex": index, "passed": bool(passed)}
                            for index, (_, passed) in enumerate(gates, start=1)
                        ]
                        task: dict[str, object] = {
                            "taskId": case_id,
                            "taskSucceeded": raw_case.get("agentSuccess") is True,
                            "terminalEvent": terminal,
                            "failedToolCalls": int(raw_case.get("toolSuccess") is False),
                            "verifier": {
                                "passed": sum(bool(passed) for _, passed in gates),
                                "total": len(gates),
                                "failedVerifierNames": failed_names,
                                "verifierResults": verifier_results,
                            },
                        }
                        for key in (
                            "answerFactCoverage",
                            "citationFactCoverage",
                        ):
                            value = raw_case.get(key)
                            if isinstance(value, (int, float)) and not isinstance(value, bool):
                                task[key] = value
                        projected.append(task)
                    if projected:
                        return projected
        # A few historical receipts nest their public task/batch projection
        # below result/validation/heldOut.  Inspect only these known wrappers;
        # never recurse into arbitrary report data.
        for wrapper_name in ("result", "validation", "heldOut", "oneShot", "evaluation"):
            wrapper = report.get(wrapper_name)
            if not isinstance(wrapper, Mapping):
                continue
            nested = self._report_tasks(wrapper)
            if nested:
                return nested
        return []

    @staticmethod
    def _report_task_title(task: Mapping[str, object], index: int) -> str:
        for key in ("taskId", "taskAlias", "caseId", "batchId", "name", "id"):
            value = task.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return f"报告任务 {index}"

    @staticmethod
    def _report_model(report: Mapping[str, object]) -> str:
        for container_name in ("evaluationContract", "runtime", "runtimeIdentity", "configuration"):
            container = report.get(container_name)
            if isinstance(container, Mapping):
                for key in ("model", "modelProfile", "modelId"):
                    value = container.get(key)
                    if isinstance(value, str) and value.strip():
                        return _public_text(value, fallback="", limit=200)
        for key in ("model", "modelProfile", "modelId"):
            value = report.get(key)
            if isinstance(value, str) and value.strip():
                return _public_text(value, fallback="", limit=200)
        return ""

    @staticmethod
    def _report_thinking(report: Mapping[str, object]) -> str:
        for container_name in ("evaluationContract", "runtime", "runtimeIdentity"):
            container = report.get(container_name)
            if isinstance(container, Mapping):
                value = container.get("thinking") or container.get("thinkingLevel")
                if isinstance(value, str) and value.strip():
                    return _public_text(value, fallback="", limit=80)
        value = report.get("thinking") or report.get("thinkingLevel")
        return _public_text(value, fallback="", limit=80) if isinstance(value, str) else ""

    @staticmethod
    def _report_execution_mode(report: Mapping[str, object]) -> str:
        value = report.get("executionMode")
        if isinstance(value, str) and value.strip():
            return _public_text(value, fallback="", limit=120)
        contract = report.get("evaluationContract")
        if isinstance(contract, Mapping) and isinstance(contract.get("executionMode"), str):
            return _public_text(contract.get("executionMode"), fallback="", limit=120)
        return "read_only"

    def _match_report_task(self, title: str, tasks: list[Mapping[str, object]], index: int) -> Mapping[str, object] | None:
        for task in tasks:
            task_id = str(task.get("taskId") or "")
            if task_id and task_id in title:
                return task
        return tasks[index - 1] if index <= len(tasks) else None

    @staticmethod
    def _task_metrics(task: Mapping[str, object]) -> dict[str, object]:
        verifier = task.get("verifier") if isinstance(task.get("verifier"), Mapping) else {}
        terminal = str(task.get("terminalEvent") or ("turn_completed" if int(task.get("answerCount") or 0) > 0 else "unknown"))
        succeeded_value = task.get("taskSucceeded")
        succeeded = bool(succeeded_value) if isinstance(succeeded_value, bool) else (terminal == "turn_completed" and int(task.get("answerCount") or 0) > 0)
        result: dict[str, object] = {
            "taskSucceeded": succeeded,
            "terminalEvent": terminal,
            "verifierPassed": int(verifier.get("passed") or task.get("verifierPassed") or task.get("verifiersPassed") or 0),
            "verifierTotal": int(verifier.get("total") or task.get("verifierTotal") or task.get("verifiersTotal") or 0),
            "latencyMs": float(task.get("latencyMs") or task.get("elapsedMs") or 0.0),
        }
        failed_indexes = verifier.get("failedVerifierIndexes") or task.get("failedVerifierIndexes")
        if isinstance(failed_indexes, list):
            result["failedVerifierIndexes"] = [int(item) for item in failed_indexes if isinstance(item, (int, float)) and not isinstance(item, bool)]
        failed_names = verifier.get("failedVerifierNames") or task.get("failedVerifierNames")
        if isinstance(failed_names, list):
            result["failedVerifierNames"] = [_public_text(item, fallback="", limit=120) for item in failed_names if isinstance(item, str) and item.strip()][:64]
        verifier_results = verifier.get("verifierResults")
        if isinstance(verifier_results, list):
            result["verifierResults"] = [
                {"index": int(item.get("verifierIndex") or 0), "passed": bool(item.get("passed"))}
                for item in verifier_results
                if isinstance(item, Mapping) and int(item.get("verifierIndex") or 0) > 0
            ]
        # CloudOps and retrieval receipts use a public answer/terminal result
        # rather than Host verifier counters.  Preserve those facts on the
        # task row so a reviewer can inspect the exact denominator and failure
        # without opening the private report.
        for source_key in (
            "answerCount", "toolCalls", "failedToolCalls", "searchCalls", "listCalls",
            "readCalls", "elapsedMs", "latencyMs", "answerCoverage", "ca", "fa",
            "jra", "top3Jra", "formalScoreProduced", "usageAvailable",
            "answerFactCoverage", "citationFactCoverage",
        ):
            value = task.get(source_key)
            if isinstance(value, (int, float, bool)) and not isinstance(value, bool):
                result.setdefault(source_key, value)
        for source_key, target_key in (("databaseCleanupStatus", "cleanupStatus"), ("runtimeErrorType", "runtimeErrorType"), ("successfulToolCalls", "successfulToolCalls")):
            value = task.get(source_key)
            if isinstance(value, (str, int, float, bool)):
                result[target_key] = _public_text(value, fallback="", limit=160) if isinstance(value, str) else value
        owner = task.get("provisionalFirstOwner") or task.get("failureOwner")
        if isinstance(owner, str) and owner.strip():
            result["failureOwner"] = _public_text(owner, fallback="", limit=120)
        usage = task.get("usage")
        if isinstance(usage, Mapping):
            for target, aliases in (
                ("inputTokens", ("input", "inputTokens")),
                ("outputTokens", ("output", "outputTokens")),
                ("cacheReadTokens", ("cacheRead", "cacheReadTokens")),
                ("cacheWriteTokens", ("cacheWrite", "cacheWriteTokens")),
            ):
                value = next(
                    (
                        usage.get(alias)
                        for alias in aliases
                        if isinstance(usage.get(alias), (int, float))
                        and not isinstance(usage.get(alias), bool)
                    ),
                    None,
                )
                if value is not None:
                    result[target] = max(0, int(value))
        return result

    @staticmethod
    def _lane_metrics(lane: Mapping[str, object], report: Mapping[str, object] | None = None) -> dict[str, object]:
        """Project stable scalar metrics without copying arbitrary report data."""

        names = (
            "taskCount", "taskSuccessCount", "taskSuccessRate", "verifierCount",
            "verifierPassCount", "verifierPassRate", "toolCalls", "failedToolCalls",
            "latencyMs", "allDatabasesCleaned", "answerCoverage", "AnswerCoverage",
            "ca", "CA", "fa", "FA", "jra", "JRA", "top3Jra", "Top3JRA",
            "elapsedMs", "signals", "usage", "tokens", "totalTokens",
            "mrr", "ndcgAt10", "recallAt10", "citationFactCoverage",
            "answerableCitationSupportRate", "infoNotFoundAbstentionRecall",
        )
        result: dict[str, object] = {}
        for name in names:
            value = lane.get(name)
            if isinstance(value, (int, float, bool)):
                result[name] = value
        if report:
            for section_name in ("metrics", "signals", "usage"):
                section = report.get(section_name)
                if isinstance(section, Mapping):
                    for key, value in section.items():
                        safe_key = _public_text(key, fallback="", limit=120)
                        if not safe_key:
                            continue
                        if isinstance(value, (int, float, bool)):
                            result.setdefault(safe_key, value)
                        elif isinstance(value, Mapping):
                            for nested_key, nested_value in value.items():
                                safe_nested_key = _public_text(nested_key, fallback="", limit=80)
                                if not safe_nested_key:
                                    continue
                                if isinstance(nested_value, (int, float, bool)):
                                    result.setdefault(f"{safe_key}.{safe_nested_key}", nested_value)
        # Normalise CloudOps' capitalised public metric names for the frontend
        # while retaining the original keys for source comparison.
        aliases = {"AnswerCoverage": "answerCoverage", "CA": "ca", "FA": "fa", "JRA": "jra", "Top3JRA": "top3Jra"}
        for source_name, target_name in aliases.items():
            if target_name not in result and source_name in result:
                result[target_name] = result[source_name]
        return result

    def _environment(
        self,
        sessions: list[Mapping[str, object]],
        report: Mapping[str, object],
        contract: Mapping[str, object],
        runtime: Mapping[str, object],
        tasks: list[Mapping[str, object]],
        location: _RunLocation | None = None,
    ) -> dict[str, object]:
        host = report.get("hostInvocation") if isinstance(report.get("hostInvocation"), Mapping) else {}
        host = host if isinstance(host, Mapping) else {}
        report_runtime = report.get("runtime") if isinstance(report.get("runtime"), Mapping) else {}
        report_runtime = report_runtime if isinstance(report_runtime, Mapping) else {}
        identity = report.get("runtimeIdentity") if isinstance(report.get("runtimeIdentity"), Mapping) else {}
        identity = identity if isinstance(identity, Mapping) else {}
        config = report.get("configuration") if isinstance(report.get("configuration"), Mapping) else {}
        config = config if isinstance(config, Mapping) else {}
        models = sorted({str(item.get("model") or item.get("model_profile") or "") for item in tasks if str(item.get("model") or item.get("model_profile") or "")})
        providers = sorted({str(item.get("provider") or "") for item in tasks if str(item.get("provider") or "")})
        execution = sorted({str(item.get("executionMode") or "") for item in tasks if str(item.get("executionMode") or "")})
        raw_model = str(
            contract.get("model")
            or host.get("model")
            or runtime.get("model")
            or report_runtime.get("model")
            or identity.get("model")
            or report.get("model")
            or report.get("modelProfile")
            or (models[0] if len(models) == 1 else "")
        )
        provider = str(
            contract.get("provider")
            or host.get("provider")
            or runtime.get("provider")
            or report_runtime.get("provider")
            or identity.get("provider")
            or report.get("provider")
            or (providers[0] if len(providers) == 1 else "")
            or ""
        )
        provider, normalized_model = self._normalize_model_provider(raw_model, provider)
        provider = _public_text(provider, fallback="", limit=160)
        normalized_model = _public_text(normalized_model, fallback="", limit=200)
        if not normalized_model and len(models) > 1:
            normalized_models = list(dict.fromkeys(
                _public_text(self._normalize_model_provider(model, "")[1] or model, fallback="", limit=200)
                for model in models
            ))
            normalized_model = "多个：" + "、".join(item for item in normalized_models[:4] if item)
        split = _public_text(
            contract.get("split")
            or report.get("split")
            or report.get("evaluationScope")
            or location.split if location else "",
            fallback="",
            limit=120,
        )
        if not split:
            split = self._split_from_name(location.run_id if location else "")
        if split in {"validation-only", "validation_only"}:
            split = "validation"
        environment: dict[str, object] = {
            "provider": provider,
            "model": normalized_model,
            "thinking": _public_text(contract.get("thinking") or host.get("thinking") or report_runtime.get("thinking") or report.get("thinkingLevel") or report.get("thinking") or "", fallback="", limit=80),
            "workflowProfile": _public_text(contract.get("workflowProfile") or report.get("workflowProfile") or "", fallback="", limit=160),
            "suiteRevision": _public_text(contract.get("suiteRevision") or report.get("suiteRevision") or report.get("suiteSha256") or "", fallback="", limit=200),
            "split": split,
            "transport": _public_text(contract.get("transport") or host.get("transport") or "", fallback="", limit=120),
            "timeoutSeconds": float(contract.get("effectiveTimeoutSeconds") or contract.get("timeoutSeconds") or host.get("timeoutSeconds") or 0.0),
            "piVersion": _public_text(runtime.get("piVersion") or report_runtime.get("piVersion") or identity.get("piVersion") or "", fallback="", limit=120),
            "runtimeVersion": _public_text(runtime.get("runtimeVersion") or report_runtime.get("runtimeVersion") or identity.get("runtimeVersion") or "", fallback="", limit=200),
            "protocolVersion": _public_text(runtime.get("protocolVersion") or report_runtime.get("protocolVersion") or identity.get("protocolVersion") or "", fallback="", limit=120),
            "executionModes": [_public_text(item, fallback="", limit=120) for item in execution],
            "workspace": "source-local 沙盒（路径隐藏）",
            "network": "按运行合同限制（具体路径与凭据不展示）",
            "pricingUsage": "已从 transcript 汇总" if any(int(item.get("inputTokens") or 0) + int(item.get("outputTokens") or 0) > 0 for item in tasks) else "usage 未投影",
        }
        identity_hashes = {
            key: _public_text(contract.get(key) or runtime.get(key) or report_runtime.get(key) or identity.get(key) or "", fallback="", limit=80)
            for key in ("contractSha256", "runnerSha256", "toolCatalogSha256", "runtimeIdentitySha256", "runtimeProvenanceSha256", "suiteSha256", "manifestSha256", "identitySha256")
            if str(contract.get(key) or runtime.get(key) or report_runtime.get(key) or identity.get(key) or "")
        }
        if identity_hashes:
            environment["identityHashes"] = identity_hashes
        # Keep the knobs a reviewer needs to reproduce a run, but only scalar
        # public configuration (never auth, paths, prompts or hidden labels).
        public_config = self._public_runtime_config(report, host, config)
        if public_config:
            environment["publicConfig"] = public_config
        machine = report.get("environment")
        if isinstance(machine, Mapping):
            environment["machine"] = self._safe_report_mapping(machine, depth=0)
        usage = report.get("usage")
        if isinstance(usage, Mapping):
            environment["usageReceipt"] = self._safe_report_mapping(usage, depth=0)
            environment["pricingUsage"] = "报告含 usage 回执"
        transcript_usage = {
            "input": sum(int(item.get("inputTokens") or 0) for item in tasks),
            "output": sum(int(item.get("outputTokens") or 0) for item in tasks),
            "cacheRead": sum(int(item.get("cacheReadTokens") or 0) for item in tasks),
            "cacheWrite": sum(int(item.get("cacheWriteTokens") or 0) for item in tasks),
        }
        if any(transcript_usage.values()):
            usage_source = (
                "transcript"
                if any(
                    item.get("transcriptAvailable") is True
                    and any(int(item.get(key) or 0) > 0 for key in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens"))
                    for item in tasks
                )
                else "report_task_usage"
            )
            report_task_usage_complete = False
            if usage_source == "report_task_usage":
                report_tasks = self._report_tasks(report)
                aliases = (
                    ("input", "inputTokens"),
                    ("output", "outputTokens"),
                    ("cacheRead", "cacheReadTokens"),
                    ("cacheWrite", "cacheWriteTokens"),
                )
                report_task_usage_complete = bool(report_tasks) and all(
                    isinstance(item.get("usage"), Mapping)
                    and all(
                        any(
                            isinstance(item["usage"].get(key), (int, float))
                            and not isinstance(item["usage"].get(key), bool)
                            for key in keys
                        )
                        for keys in aliases
                    )
                    for item in report_tasks
                )
            environment["usageReceipt"] = {
                "source": usage_source,
                **transcript_usage,
                "totalTokens": sum(transcript_usage.values()),
                **(
                    {"categoryReceiptComplete": report_task_usage_complete}
                    if usage_source == "report_task_usage"
                    else {}
                ),
            }
            environment["pricingUsage"] = (
                "已从 transcript 汇总 Provider usage"
                if usage_source == "transcript"
                else "已从逐 Task 报告汇总 Provider usage"
            )
        estimate = report.get("estimate")
        if isinstance(estimate, Mapping):
            environment["costEstimate"] = self._safe_report_mapping(estimate, depth=0)
        billing = report.get("billing")
        if isinstance(billing, Mapping):
            environment["billing"] = self._safe_report_mapping(billing, depth=0)
        pricing = report.get("pricingIdentity")
        if isinstance(pricing, Mapping):
            environment["pricingIdentity"] = self._safe_report_mapping(pricing, depth=0)
        cost_authority = report.get("authority")
        if isinstance(cost_authority, str):
            environment["costAuthority"] = _public_text(
                cost_authority,
                fallback="",
                limit=80,
            )
        runtime_cost_receipt = report.get("runtimeCostReceipt")
        if isinstance(runtime_cost_receipt, Mapping):
            environment["runtimeCostReceipt"] = self._safe_report_mapping(
                runtime_cost_receipt,
                depth=0,
            )
        trace_ids: list[str] = []
        raw_trace_ids = report.get("traceIds")
        if isinstance(raw_trace_ids, list):
            trace_ids.extend(
                _public_text(item, fallback="", limit=200)
                for item in raw_trace_ids
                if isinstance(item, str)
            )
        receipts = report.get("receipts")
        if isinstance(receipts, Mapping):
            nested_trace_ids = receipts.get("traceIds")
            if isinstance(nested_trace_ids, list):
                trace_ids.extend(
                    _public_text(item, fallback="", limit=200)
                    for item in nested_trace_ids
                    if isinstance(item, str)
                )
            trace_ids.extend(
                _public_text(receipts.get(key), fallback="", limit=200)
                for key in ("aggregateTraceId", "traceId")
                if isinstance(receipts.get(key), str)
            )
        trace_ids = list(dict.fromkeys(
            trace_id for trace_id in trace_ids if trace_id.startswith("trace:")
        ))
        if trace_ids:
            environment["traceIds"] = trace_ids
            environment["traceCount"] = len(trace_ids)
        if location and location.knowledge_path and location.knowledge_path.is_file():
            environment["knowledge"] = self._knowledge_snapshot(location.knowledge_path)
        return environment

    @staticmethod
    def _normalize_model_provider(model: str, provider: str) -> tuple[str, str]:
        """Split public ``provider/model`` identities for a readable UI."""

        model = model.strip()
        provider = provider.strip()
        if "/" in model:
            prefix, suffix = model.split("/", 1)
            if not provider:
                provider = prefix
            model = suffix
        return provider, model

    def _public_runtime_config(
        self,
        report: Mapping[str, object],
        host: Mapping[str, object],
        config: Mapping[str, object],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for source in (host, report, config):
            for key in (
                "maxReadsPerCase", "candidateDepth", "finalDepth", "scoredPairs",
                "fallbackCount", "errorCount", "timeoutSeconds", "transport",
                "batchPlan", "batchCount", "caseCount", "workflowProfile",
                "chunking", "embedding", "reranker", "candidateCount",
            ):
                value = source.get(key)
                if isinstance(value, str) and key not in result:
                    result[key] = _public_text(value, fallback="", limit=240)
                elif isinstance(value, (int, float, bool)) and key not in result:
                    result[key] = value
                elif isinstance(value, Mapping) and key not in result:
                    safe = self._safe_report_mapping(value, depth=0)
                    if safe:
                        result[key] = safe
        cohort = report.get("replayCohort")
        if isinstance(cohort, Mapping):
            for key in ("suiteId", "suiteRevision", "modelProfileFingerprint", "toolProfileFingerprint", "skillProfileFingerprint", "environmentFingerprint", "inputFingerprint", "configFingerprint"):
                value = cohort.get(key)
                if isinstance(value, str) and value:
                    result.setdefault(key, _public_text(value, fallback="", limit=240))
        signals = report.get("signals")
        if isinstance(signals, Mapping):
            for key in ("searchCalls", "listCalls", "readCalls", "successfulToolCalls", "failedToolCalls", "elapsedMs", "firstToolLatencyMs"):
                value = signals.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    result.setdefault(key, value)
        return result

    @staticmethod
    def _protected_projection() -> dict[str, object]:
        return {
            "sourceReadOnly": True,
            "thinkingShown": False,
            "systemPromptShown": False,
            "hiddenGoldShown": False,
            "rawSqlShown": False,
            "pathsAndCredentialsShown": False,
            "redactions": ["内部推理", "系统提示词", "隐藏标准答案", "SQL/凭据", "本机路径"],
        }

    @staticmethod
    def _safe_title(value: str) -> str:
        return _public_text(value, fallback="EnterpriseOps Session", limit=180)

    @staticmethod
    def _task_label(title: str, index: int) -> str:
        match = re.search(r"task[_ -]([0-9]{8}_[0-9]{6}_[0-9]+_[a-z0-9_]+)", title, re.IGNORECASE)
        return f"Task {index} · {match.group(1)[:48]}" if match else f"Task {index}"

    @staticmethod
    def _run_title(name: str) -> str:
        labels = {
            "baseline-validation": "Baseline 验证",
            "final-baseline-validation": "最终 Baseline 验证",
            "final-state-validation": "最终状态合同验证",
            "state-validation": "状态合同验证",
            "heldout-one-shot": "Held-out 单次门禁",
            "dependency-plan-validation": "依赖计划验证",
            "plumbing-loopback": "传输回环调试",
            "plumbing-one-task": "单任务传输调试",
            "thinking-debug": "Thinking 绑定调试",
        }
        for key, label in labels.items():
            if key in name:
                return f"EnterpriseOps · {label} · {name.rsplit('-', 1)[-1] if name.endswith(tuple(str(i) for i in range(10))) else ''}".strip(" ·")
        return name.replace("enterpriseops-csm-", "EnterpriseOps · ").replace("-", " ")

    @staticmethod
    def _split_from_name(name: str) -> str:
        if "heldout" in name:
            return "held-out"
        if "debug" in name or "plumbing" in name:
            return "diagnostic"
        return "validation"


__all__ = ["EvalLabEvidenceProjection"]
