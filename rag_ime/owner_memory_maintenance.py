from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import ProxyHandler, Request, build_opener

from .owner_memory_curation import (
    DEFAULT_MAX_SOURCES,
    MAX_PERSONAL_V2_SOURCES,
)


_DEFAULT_GATEWAY_URL = "http://127.0.0.1:8768"
_TERMINAL_JOB_STATES = frozenset({"completed", "failed", "expired"})


class GatewayMemoryMaintenanceJobs:
    """One trigger lane with a durable job receipt and Trace identity.

    The worker thread is process-owned, while admission/progress/result and the
    original request are persisted. A Gateway restart re-queues the same job;
    the durable Memory executor then resumes any accepted model request by its
    request id instead of creating an uncorrelated retry.
    """

    def __init__(
        self,
        execute: Callable[[Mapping[str, object]], Mapping[str, object]],
        *,
        db_path: str | Path | None = None,
        event_publisher: Callable[[Mapping[str, object]], None] | None = None,
        execution_owner: bool = True,
    ) -> None:
        self._execute = execute
        self._execution_owner = execution_owner
        self._db_path = (
            Path(db_path).expanduser()
            if db_path not in (None, "", ":memory:")
            else None
        )
        self._event_publisher = event_publisher
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, object]] = {}
        self._active_job_id = ""
        self._recent_jobs_loaded = False
        self._closed = False
        if self._db_path is not None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize_store()

    def trigger(self, payload: Mapping[str, object]) -> dict[str, object]:
        with self._lock:
            if self._closed:
                raise RuntimeError("Gateway memory maintenance is closed")
            if not self._execution_owner:
                raise RuntimeError("Memory maintenance belongs to the Agent Gateway execution owner")
            self._recover_and_start_locked()
            if self._active_job_id:
                active = self._jobs.get(self._active_job_id)
                if active is not None and str(active.get("state")) in {
                    "queued",
                    "running",
                }:
                    return self._payload(active, reused=True)
            job_id = f"memory-maintenance:{uuid.uuid4()}"
            timestamp = int(time.time() * 1_000)
            job: dict[str, object] = {
                "jobId": job_id,
                "state": "queued",
                "request": dict(payload),
                "result": {},
                "progress": {},
                "error": "",
                "createdAtMs": timestamp,
                "updatedAtMs": timestamp,
                "completedAtMs": 0,
            }
            self._jobs[job_id] = job
            self._persist_job(job)
            self._start_job_locked(job)
            return self._payload(job, reused=False)

    def status(self, job_id: object) -> dict[str, object]:
        normalized = str(job_id or "").strip()
        with self._lock:
            job = self._jobs.get(normalized) if self._execution_owner else None
            if job is None:
                job = self._load_job(normalized)
                if job is not None:
                    self._jobs[normalized] = job
                    self._recover_and_start_locked()
                    return self._payload(job, reused=False)
                # No durable receipt exists for this id. This is different
                # from a persisted failed/completed job and remains a truthful
                # retryable expiry projection.
                return self._expired_payload(normalized)
            return self._payload(job, reused=False)

    def latest_status(self, *, project: str = "") -> dict[str, object]:
        """Return the newest real maintenance job without requiring its id.

        The shell needs one lightweight background-activity projection, not the
        much heavier maintenance report. Load durable receipts at most once per
        process; every job admitted by this process is already in ``_jobs``.
        """

        normalized_project = str(project or "").strip()
        with self._lock:
            if not self._execution_owner:
                # The observer has no worker updating its cache. Read the
                # owner's durable progress on each status projection.
                self._jobs.clear()
                self._recent_jobs_loaded = False
            if not self._recent_jobs_loaded:
                for durable in self._load_recent_jobs():
                    job_id = str(durable.get("jobId") or "")
                    if job_id and job_id not in self._jobs:
                        self._jobs[job_id] = durable
                self._recent_jobs_loaded = True
            self._recover_and_start_locked()
            active = self._jobs.get(self._active_job_id)
            if active is not None and self._matches_project(active, normalized_project):
                return self._payload(active, reused=False, compact=True)
            candidates = sorted(
                (
                    job
                    for job in self._jobs.values()
                    if self._matches_project(job, normalized_project)
                ),
                key=lambda item: (
                    str(item.get("state")) in {"queued", "running"},
                    int(item.get("updatedAtMs") or 0),
                ),
                reverse=True,
            )
            return (
                self._payload(candidates[0], reused=False, compact=True)
                if candidates
                else {}
            )

    def activity_timeline_status(self, *, project: str = "") -> dict[str, object]:
        """Return one safe timeline-job projection for refresh recovery."""

        normalized_project = str(project or "").strip()
        with self._lock:
            if not self._execution_owner:
                # Observers never receive worker callbacks. Refresh durable
                # receipts before consulting the cache populated by other views.
                self._jobs = {
                    str(job["jobId"]): job for job in self._load_recent_jobs()
                }
            candidates = sorted(
                self._jobs.values(),
                key=lambda item: (
                    str(item.get("state")) in {"queued", "running"},
                    int(item.get("updatedAtMs") or 0),
                ),
                reverse=True,
            )
            active = self._jobs.get(self._active_job_id)
            if active is not None and self._matches_project(
                active,
                normalized_project,
            ):
                return self._timeline_payload(active)
            for job in candidates:
                if not self._matches_project(job, normalized_project):
                    continue
                request = (
                    job.get("request")
                    if isinstance(job.get("request"), Mapping)
                    else {}
                )
                if str(job.get("state")) in {"queued", "running"} and request.get("catalogOnly") is not True:
                    return self._timeline_payload(job)
                if request.get("timelineDate") or request.get("timelineThroughDate"):
                    return self._timeline_payload(job)
                if self._automatic_timeline_result(job):
                    return self._timeline_payload(job)
            for job in self._load_recent_jobs():
                if not self._matches_project(job, normalized_project):
                    continue
                request = (
                    job.get("request")
                    if isinstance(job.get("request"), Mapping)
                    else {}
                )
                if request.get("timelineDate") or request.get("timelineThroughDate"):
                    return self._timeline_payload(job)
                if self._automatic_timeline_result(job):
                    return self._timeline_payload(job)
            return {}

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def resume_persisted_jobs(self) -> None:
        """Resume a maintenance job left queued/running by a Gateway restart."""

        with self._lock:
            if not self._closed:
                self._recover_and_start_locked()

    def _recover_and_start_locked(self) -> None:
        if self._closed or not self._execution_owner:
            return
        if self._db_path is not None and not self._recent_jobs_loaded:
            for durable in self._load_recent_jobs():
                job_id = str(durable.get("jobId") or "")
                if job_id and job_id not in self._jobs:
                    self._jobs[job_id] = durable
            self._recent_jobs_loaded = True
        if self._active_job_id:
            active = self._jobs.get(self._active_job_id)
            if active is not None and str(active.get("state") or "") in {
                "queued",
                "running",
            }:
                return
            self._active_job_id = ""
        candidates = sorted(
            (
                job
                for job in self._jobs.values()
                if str(job.get("jobId") or "").startswith("memory-maintenance:")
                and str(job.get("state") or "") in {"queued", "running"}
            ),
            key=lambda item: int(item.get("updatedAtMs") or 0),
            reverse=True,
        )
        if not candidates:
            return
        job = candidates[0]
        if str(job.get("state") or "") == "running":
            # The previous process cannot still own this worker. Requeue the
            # exact persisted request; the Memory executor will resume any
            # accepted model turn by its durable request id.
            job["state"] = "queued"
            job["error"] = ""
            job["completedAtMs"] = 0
            job["updatedAtMs"] = int(time.time() * 1_000)
            self._persist_job(job)
        self._start_job_locked(job)

    def _start_job_locked(self, job: dict[str, object]) -> None:
        job_id = str(job.get("jobId") or "")
        if not job_id or self._active_job_id:
            return
        thread = threading.Thread(
            target=self._run,
            args=(job_id,),
            name="rag-ime-gateway-memory-maintenance",
            daemon=True,
        )
        job["thread"] = thread
        self._active_job_id = job_id
        thread.start()

    def _run(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job["state"] = "running"
            job["updatedAtMs"] = int(time.time() * 1_000)
            request = dict(job["request"])
            # The Gateway job owns the public maintenance trace.  Keep this
            # context internal to the worker request; it is not user input
            # and is deliberately excluded from the persisted request JSON.
            request["_memoryTraceContext"] = {
                "traceId": f"trace:memory:{job_id}",
                "maintenanceJobId": job_id,
                "parentSpanId": f"span:memory:{job_id}:started",
            }
            timeline_date = str(request.get("timelineDate") or "").strip()
            timeline_through_date = str(
                request.get("timelineThroughDate") or ""
            ).strip()
            if isinstance(job.get("progress"), Mapping) and job["progress"]:
                # Resuming the same request must keep its last observed source
                # cursor/counts until the executor emits a fresh checkpoint.
                pass
            elif timeline_through_date:
                job["progress"] = {
                    "phase": "activity_timeline_catch_up",
                    "throughDate": timeline_through_date,
                    "currentDate": "",
                    "totalDayCount": 0,
                    "completedDayCount": 0,
                }
            elif timeline_date:
                job["progress"] = {
                    "phase": "activity_timeline_single",
                    "currentDate": timeline_date,
                    "totalDayCount": 1,
                    "completedDayCount": 0,
                }
            elif request.get("catalogOnly") is True:
                job["progress"] = {"phase": "memory_catalog_consolidation"}
            else:
                job["progress"] = {
                    "phase": "memory_maintenance",
                    "currentDate": "",
                    "totalDayCount": 0,
                    "completedDayCount": 0,
                }
            self._persist_job(job)
            self._publish_event(
                job,
                phase="started",
                status="running",
                summary="Memory maintenance job started",
            )
            request["_progressCallback"] = lambda value: self._set_progress(
                job_id,
                value,
            )
        state = "failed"
        result: dict[str, object] = {}
        error = ""
        try:
            result = dict(self._execute(request))
            state = "completed" if result.get("ok") is True else "failed"
            if state == "failed":
                error = " ".join(str(result.get("error") or "maintenance_failed").split())[:800]
        except Exception as exc:
            error = " ".join(str(exc).split())[:800] or exc.__class__.__name__
        timestamp = int(time.time() * 1_000)
        with self._lock:
            job = self._jobs[job_id]
            job["state"] = state
            job["result"] = result
            job["error"] = error
            job["updatedAtMs"] = timestamp
            job["completedAtMs"] = timestamp
            job.pop("thread", None)
            self._persist_job(job)
            if self._active_job_id == job_id:
                self._active_job_id = ""
            self._prune_locked()
            self._publish_event(
                job,
                phase=state,
                status="completed" if state == "completed" else "failed",
                summary=(
                    "Memory maintenance job completed"
                    if state == "completed"
                    else error or "Memory maintenance job failed"
                ),
            )
            self._recover_and_start_locked()

    def _set_progress(
        self,
        job_id: str,
        value: Mapping[str, object],
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or str(job.get("state")) not in {"queued", "running"}:
                return
            job["progress"] = dict(value)
            job["updatedAtMs"] = int(time.time() * 1_000)
            self._persist_job(job)

    def _initialize_store(self) -> None:
        if self._db_path is None:
            return
        with closing(sqlite3.connect(self._db_path, timeout=10)) as conn:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memory_maintenance_jobs (
                        job_id TEXT PRIMARY KEY,
                        state TEXT NOT NULL,
                        request_json TEXT NOT NULL,
                        result_json TEXT NOT NULL,
                        progress_json TEXT NOT NULL,
                        error TEXT NOT NULL DEFAULT '',
                        created_at_ms INTEGER NOT NULL,
                        updated_at_ms INTEGER NOT NULL,
                        completed_at_ms INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_memory_maintenance_jobs_updated
                    ON memory_maintenance_jobs(updated_at_ms DESC)
                    """
                )

    def _persist_job(self, job: Mapping[str, object]) -> None:
        if self._db_path is None:
            return
        request = job.get("request") if isinstance(job.get("request"), Mapping) else {}
        request_json = json.dumps(
            {
                str(key): value
                for key, value in request.items()
                if not str(key).startswith("_")
            },
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        result = job.get("result") if isinstance(job.get("result"), Mapping) else {}
        progress = job.get("progress") if isinstance(job.get("progress"), Mapping) else {}
        with closing(sqlite3.connect(self._db_path, timeout=10)) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO memory_maintenance_jobs(
                        job_id, state, request_json, result_json, progress_json,
                        error, created_at_ms, updated_at_ms, completed_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id) DO UPDATE SET
                        state=excluded.state,
                        request_json=excluded.request_json,
                        result_json=excluded.result_json,
                        progress_json=excluded.progress_json,
                        error=excluded.error,
                        updated_at_ms=excluded.updated_at_ms,
                        completed_at_ms=excluded.completed_at_ms
                    """,
                    (
                        str(job.get("jobId") or ""),
                        str(job.get("state") or "queued"),
                        request_json,
                        json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str),
                        json.dumps(progress, ensure_ascii=False, separators=(",", ":"), default=str),
                        str(job.get("error") or ""),
                        int(job.get("createdAtMs") or 0),
                        int(job.get("updatedAtMs") or 0),
                        int(job.get("completedAtMs") or 0),
                    ),
                )

    def _load_job(self, job_id: str) -> dict[str, object] | None:
        if self._db_path is None or not job_id:
            return None
        with closing(sqlite3.connect(self._db_path, timeout=10)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM memory_maintenance_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            job = self._job_from_row(row)
        return self._recover_persisted_job(job)

    def _load_recent_jobs(self) -> list[dict[str, object]]:
        if self._db_path is None:
            return []
        with closing(sqlite3.connect(self._db_path, timeout=10)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM memory_maintenance_jobs
                WHERE job_id LIKE 'memory-maintenance:%'
                  AND (state IN ('queued', 'running') OR job_id IN (
                    SELECT job_id FROM memory_maintenance_jobs
                    WHERE job_id LIKE 'memory-maintenance:%'
                      AND state NOT IN ('queued', 'running')
                    ORDER BY updated_at_ms DESC, job_id DESC LIMIT 64
                  ))
                ORDER BY updated_at_ms DESC
                """
            ).fetchall()
            jobs = [self._job_from_row(row) for row in rows]
        return [self._recover_persisted_job(job) for job in jobs]

    def _recover_persisted_job(self, job: dict[str, object]) -> dict[str, object]:
        """Normalize a prior worker receipt for process-local resumption."""

        # Projection refreshes have durable lease tokens and their own recovery
        # owner. A status read must not expire another process's live worker.
        if not self._execution_owner or str(job.get("jobId") or "").startswith("memory-refresh:"):
            return job
        if str(job.get("state") or "") not in {"queued", "running"}:
            return job
        job["state"] = "queued"
        job["error"] = ""
        job["completedAtMs"] = 0
        return job

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> dict[str, object]:
        def decoded(name: str) -> dict[str, object]:
            try:
                value = json.loads(row[name] or "{}")
            except (TypeError, json.JSONDecodeError):
                return {}
            return dict(value) if isinstance(value, Mapping) else {}

        return {
            "jobId": str(row["job_id"] or ""),
            "state": str(row["state"] or ""),
            "request": decoded("request_json"),
            "result": decoded("result_json"),
            "progress": decoded("progress_json"),
            "error": str(row["error"] or ""),
            "createdAtMs": int(row["created_at_ms"] or 0),
            "updatedAtMs": int(row["updated_at_ms"] or 0),
            "completedAtMs": int(row["completed_at_ms"] or 0),
        }

    def _publish_event(
        self,
        job: Mapping[str, object],
        *,
        phase: str,
        status: str,
        summary: str,
    ) -> None:
        publisher = self._event_publisher
        if not callable(publisher):
            return
        job_id = str(job.get("jobId") or "")
        result = job.get("result") if isinstance(job.get("result"), Mapping) else {}
        try:
            publisher(
                {
                    "eventType": "memory_maintenance_job",
                    "phase": phase,
                    "status": status,
                    "summary": summary,
                    "jobId": job_id,
                    "runId": job_id,
                    "traceId": f"trace:memory:{job_id}",
                    "sourceCursor": _nested_mapping(result, "sourceCursor"),
                    "result": dict(result),
                    "error": str(job.get("error") or ""),
                }
            )
        except Exception:
            # Trace is an observability side channel; failure to emit it must
            # never lose the durable maintenance result or change its state.
            return

    def _prune_locked(self) -> None:
        terminal = sorted(
            (
                job
                for job in self._jobs.values()
                if str(job.get("state")) in _TERMINAL_JOB_STATES
            ),
            key=lambda item: int(item.get("completedAtMs") or 0),
            reverse=True,
        )
        for stale in terminal[64:]:
            self._jobs.pop(str(stale.get("jobId") or ""), None)

    @staticmethod
    def _matches_project(job: Mapping[str, object], project: str) -> bool:
        request = (
            job.get("request")
            if isinstance(job.get("request"), Mapping)
            else {}
        )
        requested_project = str(request.get("project") or "").strip()
        # A blank request project means "use this Gateway's configured
        # project".  That is how the installed scheduled trigger runs, so it
        # must remain visible when the calendar queries the resolved project.
        return not project or not requested_project or requested_project == project

    @classmethod
    def _timeline_payload(cls, job: Mapping[str, object]) -> dict[str, object]:
        request = (
            job.get("request")
            if isinstance(job.get("request"), Mapping)
            else {}
        )
        mode = (
            "manual_catch_up"
            if request.get("timelineThroughDate")
            else "single_day"
            if request.get("timelineDate")
            else "automatic_catch_up"
        )
        timeline_result = cls._automatic_timeline_result(job)
        result_summary = {
            key: timeline_result[key]
            for key in (
                "ok",
                "throughDate",
                "pendingDayCount",
                "batchDayCount",
                "completedDayCount",
                "remainingDayCount",
                "failedDate",
                "error",
                "status",
                "autoPublished",
                "semanticOrganization",
                "activityTimelineSummary",
                "warningCount",
                "activityTimelines",
            )
            if key in timeline_result
        }
        result = (
            job.get("result")
            if isinstance(job.get("result"), Mapping)
            else {}
        )
        job_id = str(job.get("jobId") or "")
        state = str(job.get("state") or "")
        trace_id = f"trace:memory:{job_id}"
        return {
            "schemaVersion": "rag-ime.activity-timeline-job-status.v1",
            "ok": str(job.get("state") or "") != "failed",
            "jobId": str(job.get("jobId") or ""),
            "state": str(job.get("state") or ""),
            "mode": mode,
            "progress": (
                dict(job.get("progress") or {})
                if isinstance(job.get("progress"), Mapping)
                else {}
            ),
            "result": result_summary,
            "error": str(result_summary.get("error") or job.get("error") or ""),
            "createdAtMs": int(job.get("createdAtMs") or 0),
            "updatedAtMs": int(job.get("updatedAtMs") or 0),
            "completedAtMs": int(job.get("completedAtMs") or 0),
            "traceId": trace_id,
            "runId": job_id,
            "failureRef": job_id if state == "failed" else "",
            "sourceCursor": _nested_mapping(result, "sourceCursor"),
            "sourceInputRefs": _nested_list(result, "sourceInputRefs"),
            "traceEvidence": {
                "traceId": trace_id,
                "runId": job_id,
                "source": "observation_journal",
                "terminal": state in _TERMINAL_JOB_STATES,
            },
        }

    @staticmethod
    def _automatic_timeline_result(job: Mapping[str, object]) -> dict[str, object]:
        request = (
            job.get("request")
            if isinstance(job.get("request"), Mapping)
            else {}
        )
        result = (
            job.get("result")
            if isinstance(job.get("result"), Mapping)
            else {}
        )
        if request.get("timelineDate") or request.get("timelineThroughDate"):
            return dict(result)
        dreaming = (
            result.get("dreaming")
            if isinstance(result.get("dreaming"), Mapping)
            else result
        )
        catch_up = (
            dreaming.get("activityTimelineCatchUp")
            if isinstance(dreaming, Mapping)
            and isinstance(dreaming.get("activityTimelineCatchUp"), Mapping)
            else {}
        )
        return dict(catch_up)

    @staticmethod
    def _payload(
        job: Mapping[str, object],
        *,
        reused: bool,
        compact: bool = False,
    ) -> dict[str, object]:
        job_id = str(job.get("jobId") or "")
        result = (
            {}
            if compact
            else dict(job.get("result") or {})
            if isinstance(job.get("result"), Mapping)
            else {}
        )
        state = str(job.get("state") or "")
        source_cursor = _nested_mapping(result, "sourceCursor")
        payload = {
            "schemaVersion": "rag-ime.gateway-memory-maintenance-job.v1",
            "ok": state not in {"failed", "expired"},
            "jobId": job_id,
            "state": state,
            "reused": bool(reused),
            "catalogOnly": (
                isinstance(job.get("request"), Mapping)
                and job["request"].get("catalogOnly") is True
            ),
            "result": result,
            "progress": (
                dict(job.get("progress") or {})
                if isinstance(job.get("progress"), Mapping)
                else {}
            ),
            "error": str(job.get("error") or ""),
            "createdAtMs": int(job.get("createdAtMs") or 0),
            "updatedAtMs": int(job.get("updatedAtMs") or 0),
            "completedAtMs": int(job.get("completedAtMs") or 0),
            "traceId": f"trace:memory:{job_id}",
            "runId": job_id,
            "failureRef": job_id if state == "failed" else "",
            "sourceCursor": source_cursor,
            "sourceInputRefs": [] if compact else _nested_list(result, "sourceInputRefs"),
            "traceEvidence": {
                "traceId": f"trace:memory:{job_id}",
                "runId": job_id,
                "source": "observation_journal",
                "terminal": state in _TERMINAL_JOB_STATES,
            },
        }
        if state == "expired":
            payload.update(
                {
                    "errorCode": "memory_maintenance_job_expired",
                    "recovery": {
                        "recoverable": False,
                        "retryable": True,
                        "action": "trigger_new_job",
                        "reason": "process_local_job_registry_lost",
                    },
                }
            )
        return payload

    @staticmethod
    def _expired_payload(job_id: str) -> dict[str, object]:
        return {
            "schemaVersion": "rag-ime.gateway-memory-maintenance-job.v1",
            "ok": False,
            "jobId": job_id,
            "state": "expired",
            "reused": False,
            "result": {},
            "progress": {},
            "errorCode": "memory_maintenance_job_expired",
            "error": (
                "Gateway restarted before this process-local Memory maintenance "
                "job could be read; the old job cannot be recovered."
            ),
            "recovery": {
                "recoverable": False,
                "retryable": True,
                "action": "trigger_new_job",
                "reason": "process_local_job_registry_lost",
            },
            "createdAtMs": 0,
            "updatedAtMs": 0,
            "completedAtMs": 0,
        }


def _nested_mapping(value: object, key: str) -> dict[str, object]:
    """Find a bounded structured receipt in a nested maintenance report."""

    if isinstance(value, Mapping):
        candidate = value.get(key)
        if isinstance(candidate, Mapping):
            return dict(candidate)
        for child in value.values():
            found = _nested_mapping(child, key)
            if found:
                return found
    elif isinstance(value, list):
        for child in value[:128]:
            found = _nested_mapping(child, key)
            if found:
                return found
    return {}


def _nested_list(value: object, key: str) -> list[dict[str, object]]:
    if isinstance(value, Mapping):
        candidate = value.get(key)
        if isinstance(candidate, list):
            return [dict(item) for item in candidate[:128] if isinstance(item, Mapping)]
        for child in value.values():
            found = _nested_list(child, key)
            if found:
                return found
    elif isinstance(value, list):
        for child in value[:128]:
            found = _nested_list(child, key)
            if found:
                return found
    return []


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ask the resident Agent Gateway to run Memory maintenance.",
    )
    parser.add_argument(
        "--gateway-url",
        default=os.environ.get("RAG_IME_AGENT_GATEWAY_URL", _DEFAULT_GATEWAY_URL),
    )
    parser.add_argument("--project", default="")
    parser.add_argument("--owner-kind", default="")
    parser.add_argument("--owner-id", default="")
    parser.add_argument("--instruction", default="")
    parser.add_argument("--manual", action="store_true")
    parser.add_argument("--max-sources", type=int, default=DEFAULT_MAX_SOURCES)
    parser.add_argument("--timeout-seconds", type=float, default=3_600.0)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    # Accepted only so old manual invocations fail over to the new Gateway
    # owner without launching a second Runtime Host or reading SQLite here.
    parser.add_argument("--db-path", default="", help=argparse.SUPPRESS)
    parser.add_argument("--model-env-path", default="", help=argparse.SUPPRESS)
    parser.add_argument("--model", default="", help=argparse.SUPPRESS)
    auto_apply = parser.add_mutually_exclusive_group()
    auto_apply.add_argument(
        "--auto-apply",
        dest="auto_apply",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    auto_apply.add_argument(
        "--no-auto-apply",
        dest="auto_apply",
        action="store_false",
        help=argparse.SUPPRESS,
    )
    parser.set_defaults(auto_apply=None)
    return parser


def run_gateway_memory_maintenance(
    gateway_url: object,
    payload: Mapping[str, object],
    *,
    timeout_seconds: float = 3_600.0,
    poll_interval: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    endpoint = _gateway_endpoint(gateway_url)
    timeout = max(1.0, min(7_200.0, float(timeout_seconds)))
    interval = max(0.05, min(5.0, float(poll_interval)))
    opener = build_opener(ProxyHandler({}))
    triggered = _request_json(
        opener,
        Request(
            endpoint,
            data=json.dumps(
                dict(payload),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "RagIme-Memory-Maintenance/1",
            },
            method="POST",
        ),
        timeout=min(30.0, timeout),
    )
    job_id = str(triggered.get("jobId") or "").strip()
    if not job_id:
        raise RuntimeError("Agent Gateway did not return a Memory maintenance job id")
    deadline = time.monotonic() + timeout
    current = triggered
    while str(current.get("state") or "") not in _TERMINAL_JOB_STATES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"Gateway Memory maintenance job timed out: {job_id}"
            )
        time.sleep(min(interval, remaining))
        current = _request_json(
            opener,
            Request(
                f"{endpoint}?{urlencode({'jobId': job_id})}",
                headers={
                    "Accept": "application/json",
                    "User-Agent": "RagIme-Memory-Maintenance/1",
                },
                method="GET",
            ),
            timeout=min(30.0, max(1.0, remaining)),
        )
    return current


def _gateway_endpoint(value: object) -> str:
    base = str(value or "").strip().rstrip("/")
    parsed = urlparse(base)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("Memory maintenance Gateway URL must be a loopback HTTP origin")
    return f"{base}/api/agent/memory-maintenance"


def _request_json(opener: object, request: Request, *, timeout: float) -> dict[str, object]:
    try:
        with opener.open(request, timeout=timeout) as response:  # type: ignore[attr-defined]
            raw = response.read()
    except HTTPError as exc:
        raw = exc.read()
        detail = _response_error(raw) or str(exc.reason or "HTTP error")
        raise RuntimeError(f"Agent Gateway returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Agent Gateway is unavailable: {exc.reason}") from exc
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Agent Gateway returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("Agent Gateway returned a non-object JSON response")
    return decoded


def _response_error(raw: bytes) -> str:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return ""
    return " ".join(str(payload.get("error") or "").split())[:800] if isinstance(payload, dict) else ""


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = {
        "project": str(args.project or "").strip(),
        "ownerKind": str(args.owner_kind or "").strip(),
        "ownerId": str(args.owner_id or "").strip(),
        "instruction": str(args.instruction or "").strip(),
        "manual": bool(args.manual),
        "maxSources": max(1, min(MAX_PERSONAL_V2_SOURCES, int(args.max_sources))),
    }
    try:
        report = run_gateway_memory_maintenance(
            args.gateway_url,
            payload,
            timeout_seconds=args.timeout_seconds,
            poll_interval=args.poll_interval,
        )
    except Exception as exc:
        report = {
            "schemaVersion": "rag-ime.gateway-memory-maintenance-client.v1",
            "ok": False,
            "state": "failed",
            "error": " ".join(str(exc).split())[:800] or exc.__class__.__name__,
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    result = report.get("result")
    result_ok = isinstance(result, Mapping) and result.get("ok") is True
    return 0 if report.get("state") == "completed" and result_ok else 1


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    raise SystemExit(main())
