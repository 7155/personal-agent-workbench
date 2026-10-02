from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .agent_model_defaults import DEFAULT_AGENT_MODEL_ID
from .activity_timeline_curation import (
    ACTIVITY_ORGANIZATION_CONTRACT_REPAIR_PROMPT_VERSION,
    ActivityOrganizationContractError,
    ActivityOrganizationPacket,
    ActivityOrganizationResult,
    activity_organization_output_schema,
    build_activity_organization_contract_repair_prompt,
    validate_activity_organization_output,
)
from .text_utils import compact_whitespace


REQUIRED_EVALUATION_MODEL = DEFAULT_AGENT_MODEL_ID
REQUIRED_EVALUATION_THINKING = "max"
_READ_COMPATIBLE_EVALUATION_MODELS = frozenset({
    REQUIRED_EVALUATION_MODEL, "gpt-6.1-sol", "gpt-5.6-luna", "gpt-5.6-sol",
})


@dataclass(frozen=True)
class FrozenActivityTimeline:
    timeline_id: str
    project: str
    timeline_date: str
    timezone: str
    status: str
    source_event_hash: str
    event_ids: tuple[int, ...]
    event_rows: tuple[dict[str, object], ...]
    baseline_segments: tuple[dict[str, object], ...]
    metadata: dict[str, object]


@dataclass(frozen=True)
class LunaStructuredRun:
    phase: str
    model: str
    thinking: str
    command: tuple[str, ...]
    elapsed_seconds: float
    exit_code: int
    prompt_sha256: str
    schema_sha256: str
    output_sha256: str
    stdout_sha256: str
    stderr_sha256: str
    output: dict[str, object]

    def redacted_receipt(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "model": self.model,
            "thinking": self.thinking,
            "elapsedSeconds": round(self.elapsed_seconds, 3),
            "exitCode": self.exit_code,
            "promptSha256": self.prompt_sha256,
            "schemaSha256": self.schema_sha256,
            "outputSha256": self.output_sha256,
            "stdoutSha256": self.stdout_sha256,
            "stderrSha256": self.stderr_sha256,
        }


@dataclass(frozen=True)
class ActivityOrganizationCandidateRun:
    run: LunaStructuredRun
    result: ActivityOrganizationResult
    contract_repairs: tuple[dict[str, object], ...]


def load_frozen_activity_timeline(
    db_path: str | Path,
    *,
    timeline_id: str,
    require_approved: bool = True,
) -> FrozenActivityTimeline:
    """Load one exact Timeline and its source rows through an immutable connection."""

    path = Path(db_path).expanduser().resolve(strict=True)
    identifier = compact_whitespace(timeline_id)
    if not identifier:
        raise ValueError("timeline_id is required")
    uri = f"file:{quote(str(path), safe='/')}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        timeline = conn.execute(
            """
            SELECT timeline_id, project, timeline_date, timezone, status,
                   source_event_ids_json, source_event_hash, segments_json,
                   metadata_json, event_count, segment_count
            FROM daily_activity_timelines
            WHERE timeline_id = ?
            """,
            (identifier,),
        ).fetchone()
        if timeline is None:
            raise ValueError("daily Activity Timeline does not exist")
        status = str(timeline["status"] or "")
        if require_approved and status != "approved":
            raise ValueError("real-data evaluation requires an approved Timeline")

        event_ids = _positive_unique_ids(timeline["source_event_ids_json"])
        expected_count = int(timeline["event_count"] or 0)
        if expected_count != len(event_ids):
            raise ValueError("Timeline event_count does not match source event membership")
        events_by_id: dict[int, dict[str, object]] = {}
        for chunk in _chunks(event_ids, 400):
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT id, created_at_ms, source, committed_text, recent_context,
                       preedit, app, project, context_group_id, context_group_level
                FROM input_events
                WHERE id IN ({placeholders})
                """,
                tuple(chunk),
            ).fetchall()
            for row in rows:
                event_id = int(row["id"])
                events_by_id[event_id] = {
                    "id": event_id,
                    "created_at_ms": int(row["created_at_ms"] or 0),
                    "source": str(row["source"] or ""),
                    "committed_text": str(row["committed_text"] or ""),
                    "recent_context": str(row["recent_context"] or ""),
                    "preedit": str(row["preedit"] or ""),
                    "app": str(row["app"] or ""),
                    "project": str(row["project"] or ""),
                    "context_group_id": str(row["context_group_id"] or ""),
                    "context_group_level": str(row["context_group_level"] or ""),
                }
        missing = [event_id for event_id in event_ids if event_id not in events_by_id]
        if missing:
            raise ValueError(f"Timeline source events are missing: {missing[:8]}")

        segments = _json_list_of_objects(timeline["segments_json"], field="segments_json")
        expected_segments = int(timeline["segment_count"] or 0)
        if expected_segments != len(segments):
            raise ValueError("Timeline segment_count does not match stored segments")
        metadata = _json_object(timeline["metadata_json"], field="metadata_json")
        return FrozenActivityTimeline(
            timeline_id=str(timeline["timeline_id"]),
            project=str(timeline["project"] or ""),
            timeline_date=str(timeline["timeline_date"] or ""),
            timezone=str(timeline["timezone"] or "UTC"),
            status=status,
            source_event_hash=str(timeline["source_event_hash"] or ""),
            event_ids=event_ids,
            event_rows=tuple(events_by_id[event_id] for event_id in event_ids),
            baseline_segments=tuple(segments),
            metadata=metadata,
        )
    finally:
        conn.close()


def baseline_activity_summary(snapshot: FrozenActivityTimeline) -> dict[str, object]:
    """Describe the baseline without copying its titles, summaries, or source text."""

    memberships: list[int] = []
    event_counts: list[int] = []
    spans_minutes: list[float] = []
    app_counts: list[int] = []
    title_hashes: list[str] = []
    for segment in snapshot.baseline_segments:
        raw_ids = segment.get("sourceEventIds")
        ids = [int(value) for value in raw_ids] if isinstance(raw_ids, list) else []
        memberships.extend(ids)
        event_counts.append(len(ids))
        start_ms = _safe_int(segment.get("startMs"))
        end_ms = max(start_ms, _safe_int(segment.get("endMs")))
        spans_minutes.append(round((end_ms - start_ms) / 60_000, 2))
        apps = segment.get("apps")
        app_counts.append(len(set(str(value) for value in apps)) if isinstance(apps, list) else 0)
        title = compact_whitespace(str(segment.get("title") or ""))
        title_hashes.append(_sha256(title))

    duplicates = len(memberships) != len(set(memberships))
    exact = not duplicates and sorted(memberships) == sorted(snapshot.event_ids)
    return {
        "segmentationMode": str(snapshot.metadata.get("segmentationMode") or "unknown"),
        "segmentCount": len(snapshot.baseline_segments),
        "eventCount": len(snapshot.event_ids),
        "eventCounts": event_counts,
        "spanMinutes": spans_minutes,
        "appCounts": app_counts,
        "titleSha256": title_hashes,
        "exactEventCoverage": exact,
        "duplicateEventMembership": duplicates,
    }


def evaluation_timezone(value: object, *, fallback: str) -> str:
    """Use stored IANA zones, but make legacy/local abbreviations explicit."""

    candidate = compact_whitespace(str(value or ""))
    fallback_name = compact_whitespace(fallback)
    try:
        ZoneInfo(fallback_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("evaluation timezone fallback must be an IANA timezone") from exc
    if candidate and candidate != "local":
        try:
            ZoneInfo(candidate)
            return candidate
        except ZoneInfoNotFoundError:
            pass
    return fallback_name


def run_luna_structured(
    *,
    prompt: str,
    schema: Mapping[str, object],
    artifact_dir: str | Path,
    phase: str,
    timeout_seconds: float = 1_200.0,
    codex_bin: str = "codex",
    model: str = REQUIRED_EVALUATION_MODEL,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> LunaStructuredRun:
    """Run one ephemeral structured Luna call without putting source text in argv."""

    if model not in _READ_COMPATIBLE_EVALUATION_MODELS:
        raise ValueError("structured evaluation model is unsupported")
    normalized_phase = compact_whitespace(phase).casefold().replace("_", "-")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", normalized_phase):
        raise ValueError("phase must be a short filesystem-safe identifier")
    directory = Path(artifact_dir).expanduser()
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    directory.chmod(0o700)
    os.umask(0o077)

    schema_text = json.dumps(
        dict(schema),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    prompt_path = directory / f"{normalized_phase}-prompt.txt"
    schema_path = directory / f"{normalized_phase}-schema.json"
    output_path = directory / f"{normalized_phase}-output.json"
    stdout_path = directory / f"{normalized_phase}-stdout.log"
    stderr_path = directory / f"{normalized_phase}-stderr.log"
    receipt_path = directory / f"{normalized_phase}-receipt.json"
    _write_private_exclusive(prompt_path, prompt)
    _write_private_exclusive(schema_path, schema_text)

    command = (
        codex_bin,
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--model",
        model,
        "--config",
        f'model_reasoning_effort="{REQUIRED_EVALUATION_THINKING}"',
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--color",
        "never",
        "--cd",
        str(directory),
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(output_path),
        "-",
    )
    started = time.monotonic()
    completed = command_runner(
        list(command),
        input=prompt,
        text=True,
        capture_output=True,
        timeout=max(1.0, float(timeout_seconds)),
        check=False,
    )
    elapsed = time.monotonic() - started
    stdout = str(completed.stdout or "")
    stderr = str(completed.stderr or "")
    _write_private_exclusive(stdout_path, stdout)
    _write_private_exclusive(stderr_path, stderr)
    if int(completed.returncode) != 0:
        raise RuntimeError(
            f"Luna {normalized_phase} exited {completed.returncode}; private logs retained"
        )
    if not output_path.exists():
        raise RuntimeError(f"Luna {normalized_phase} produced no structured output file")
    output_path.chmod(0o600)
    output_text = output_path.read_text(encoding="utf-8")
    try:
        decoded = json.loads(output_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Luna {normalized_phase} output is not JSON") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError(f"Luna {normalized_phase} output must be an object")

    run = LunaStructuredRun(
        phase=normalized_phase,
        model=model,
        thinking=REQUIRED_EVALUATION_THINKING,
        command=command,
        elapsed_seconds=elapsed,
        exit_code=int(completed.returncode),
        prompt_sha256=_sha256(prompt),
        schema_sha256=_sha256(schema_text),
        output_sha256=_sha256(output_text),
        stdout_sha256=_sha256(stdout),
        stderr_sha256=_sha256(stderr),
        output=dict(decoded),
    )
    _write_private_exclusive(
        receipt_path,
        json.dumps(run.redacted_receipt(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return run


def load_luna_structured_run(
    artifact_dir: str | Path,
    *,
    phase: str,
    expected_model: str | None = None,
) -> LunaStructuredRun:
    """Resume from a completed private run only when its receipt still matches."""

    normalized_phase = compact_whitespace(phase).casefold().replace("_", "-")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", normalized_phase):
        raise ValueError("phase must be a short filesystem-safe identifier")
    directory = Path(artifact_dir).expanduser().resolve(strict=True)
    if stat.S_IMODE(directory.stat().st_mode) & 0o077:
        raise ValueError("private Luna artifact directory has unsafe permissions")
    output_path = directory / f"{normalized_phase}-output.json"
    receipt_path = directory / f"{normalized_phase}-receipt.json"
    for path in (output_path, receipt_path):
        if not path.is_file():
            raise ValueError(f"private Luna artifact is missing: {path.name}")
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError(f"private Luna artifact has unsafe permissions: {path.name}")

    output_text = output_path.read_text(encoding="utf-8")
    receipt_text = receipt_path.read_text(encoding="utf-8")
    try:
        output = json.loads(output_text)
        receipt = json.loads(receipt_text)
    except json.JSONDecodeError as exc:
        raise ValueError("private Luna artifact is invalid JSON") from exc
    if not isinstance(output, dict) or not isinstance(receipt, dict):
        raise ValueError("private Luna output and receipt must be objects")
    if str(receipt.get("phase") or "") != normalized_phase:
        raise ValueError("private Luna receipt phase does not match requested phase")
    model = str(receipt.get("model") or "")
    if model not in _READ_COMPATIBLE_EVALUATION_MODELS or (expected_model is not None and model != expected_model):
        raise ValueError("private Luna receipt model does not match evaluation model")
    if str(receipt.get("thinking") or "") != REQUIRED_EVALUATION_THINKING:
        raise ValueError("private Luna receipt thinking level does not match evaluation")
    if receipt.get("exitCode") != 0:
        raise ValueError("private Luna receipt is not a successful run")
    output_sha256 = _sha256(output_text)
    if str(receipt.get("outputSha256") or "") != output_sha256:
        raise ValueError("private Luna output hash does not match its receipt")

    def receipt_hash(field: str) -> str:
        value = str(receipt.get(field) or "")
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError(f"private Luna receipt has invalid {field}")
        return value

    return LunaStructuredRun(
        phase=normalized_phase,
        model=model,
        thinking=REQUIRED_EVALUATION_THINKING,
        command=(),
        elapsed_seconds=float(receipt.get("elapsedSeconds") or 0.0),
        exit_code=0,
        prompt_sha256=receipt_hash("promptSha256"),
        schema_sha256=receipt_hash("schemaSha256"),
        output_sha256=output_sha256,
        stdout_sha256=receipt_hash("stdoutSha256"),
        stderr_sha256=receipt_hash("stderrSha256"),
        output=dict(output),
    )


def validate_activity_candidate_with_one_contract_repair(
    *,
    packet: ActivityOrganizationPacket,
    initial_run: LunaStructuredRun,
    artifact_dir: str | Path,
    timeout_seconds: float,
    codex_bin: str,
    structured_runner: Callable[..., LunaStructuredRun] = run_luna_structured,
) -> ActivityOrganizationCandidateRun:
    """Validate a candidate and allow one explicit repair of output-contract drift."""

    try:
        result = validate_activity_organization_output(initial_run.output, packet=packet)
    except ActivityOrganizationContractError as exc:
        contract_error = str(exc)
        repaired_run = structured_runner(
            prompt=build_activity_organization_contract_repair_prompt(
                packet,
                organizer_output=initial_run.output,
                contract_error=contract_error,
            ),
            schema=activity_organization_output_schema(),
            artifact_dir=Path(artifact_dir) / "contract-repair",
            phase="contract-repair",
            timeout_seconds=float(timeout_seconds),
            codex_bin=str(codex_bin),
        )
        repaired_result = validate_activity_organization_output(
            repaired_run.output,
            packet=packet,
        )
        return ActivityOrganizationCandidateRun(
            run=repaired_run,
            result=repaired_result,
            contract_repairs=(
                {
                    "promptVersion": ACTIVITY_ORGANIZATION_CONTRACT_REPAIR_PROMPT_VERSION,
                    "contractErrorSha256": _sha256(contract_error),
                    "rejectedRun": initial_run.redacted_receipt(),
                    "repairedRun": repaired_run.redacted_receipt(),
                },
            ),
        )
    return ActivityOrganizationCandidateRun(
        run=initial_run,
        result=result,
        contract_repairs=(),
    )


def redacted_organization_summary(
    result: Mapping[str, object],
    *,
    run: LunaStructuredRun,
) -> dict[str, object]:
    """Create report-safe metrics without copying model or source language."""

    raw_activities = result.get("activities")
    raw_unclassified = result.get("unclassified")
    activities = raw_activities if isinstance(raw_activities, list) else []
    unclassified = raw_unclassified if isinstance(raw_unclassified, list) else []
    event_counts: list[int] = []
    confidences: list[float] = []
    membership_hashes: list[str] = []
    activity_ids: list[str] = []
    for item in activities:
        if not isinstance(item, Mapping):
            continue
        refs = item.get("eventRefs")
        normalized_refs = [str(ref) for ref in refs] if isinstance(refs, list) else []
        event_counts.append(len(normalized_refs))
        membership_hashes.append(_sha256(json.dumps(normalized_refs, separators=(",", ":"))))
        confidence = item.get("confidence")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
            confidences.append(float(confidence))
        activity_id = compact_whitespace(str(item.get("activityId") or ""))
        if activity_id:
            activity_ids.append(activity_id)
    return {
        "activityCount": len(activities),
        "activityEventCounts": event_counts,
        "unclassifiedCount": len(unclassified),
        "activityIds": activity_ids,
        "activityMembershipSha256": membership_hashes,
        "confidenceMinimum": min(confidences, default=0.0),
        "confidenceMean": (
            round(sum(confidences) / len(confidences), 4) if confidences else 0.0
        ),
        "confidenceMaximum": max(confidences, default=0.0),
        "run": run.redacted_receipt(),
    }


def _positive_unique_ids(value: object) -> tuple[int, ...]:
    try:
        decoded = json.loads(str(value or "[]"))
    except json.JSONDecodeError as exc:
        raise ValueError("source_event_ids_json is not valid JSON") from exc
    if not isinstance(decoded, list):
        raise ValueError("source_event_ids_json must be an array")
    try:
        ids = tuple(int(item) for item in decoded)
    except (TypeError, ValueError) as exc:
        raise ValueError("source_event_ids_json contains a non-integer") from exc
    if any(value <= 0 for value in ids) or len(ids) != len(set(ids)) or not ids:
        raise ValueError("source_event_ids_json must contain unique positive IDs")
    return ids


def _json_list_of_objects(value: object, *, field: str) -> list[dict[str, object]]:
    try:
        decoded = json.loads(str(value or "[]"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field} is not valid JSON") from exc
    if not isinstance(decoded, list) or not all(isinstance(item, dict) for item in decoded):
        raise ValueError(f"{field} must be an array of objects")
    return [dict(item) for item in decoded]


def _json_object(value: object, *, field: str) -> dict[str, object]:
    try:
        decoded = json.loads(str(value or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field} is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"{field} must be an object")
    return dict(decoded)


def _chunks(values: Sequence[int], size: int) -> list[tuple[int, ...]]:
    return [tuple(values[index : index + size]) for index in range(0, len(values), size)]


def _write_private_exclusive(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(text)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    path.chmod(0o600)


def _safe_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
