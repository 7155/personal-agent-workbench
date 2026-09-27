from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .activity_timeline import DailyActivityTimelineStore
from .agent_role_book import AgentRoleBookStore
from .db import apply_database_migrations
from .memory_maintenance_settings import (
    DEFAULT_MAINTENANCE_MODEL,
    DEFAULT_MAINTENANCE_THINKING_LEVEL,
)
from .deepseek_memory_organizer import (
    ActivitySemanticVerificationError,
    ManagedPiMemoryOrganizer,
)
from .memory_model_executor import (
    MemoryModelUnavailable,
    build_managed_pi_memory_model_executor,
)
from .personal_context import (
    DEFAULT_CONSOLIDATION_INTERVAL_MS,
    PersonalContextConsolidator,
)
from .text_utils import compact_whitespace


PERSONAL_CONTEXT_MAINTENANCE_RUN_SCHEMA_VERSION = (
    "rag-ime.personal-context-maintenance-run.v1"
)
PERSONAL_CONTEXT_MAINTENANCE_STATUS_SCHEMA_VERSION = (
    "rag-ime.personal-context-maintenance-status.v1"
)

ConsolidatorFactory = Callable[..., PersonalContextConsolidator]


@dataclass(frozen=True)
class PersonalContextMaintenanceConfig:
    enabled: bool = True
    consolidate_roles: bool = True
    build_timelines: bool = True
    project: str = ""
    role_id: str = ""
    role_version: str = ""
    min_interval_ms: int = DEFAULT_CONSOLIDATION_INTERVAL_MS
    apply_safe_recent_work: bool = False
    auto_publish_timelines: bool = False
    timeline_catch_up_limit: int = 0
    batch_limit: int = 500
    model: str = DEFAULT_MAINTENANCE_MODEL
    thinking_level: str = DEFAULT_MAINTENANCE_THINKING_LEVEL

    def normalized(self) -> "PersonalContextMaintenanceConfig":
        project = compact_whitespace(self.project)
        role_id = compact_whitespace(self.role_id)
        role_version = compact_whitespace(self.role_version)
        if role_version and not role_id:
            raise ValueError("role_version requires an explicit role_id")
        return PersonalContextMaintenanceConfig(
            enabled=bool(self.enabled),
            consolidate_roles=bool(self.consolidate_roles),
            build_timelines=bool(self.build_timelines),
            project=project,
            role_id=role_id,
            role_version=role_version,
            min_interval_ms=max(0, int(self.min_interval_ms)),
            apply_safe_recent_work=bool(self.apply_safe_recent_work),
            auto_publish_timelines=bool(self.auto_publish_timelines),
            timeline_catch_up_limit=max(
                0,
                min(int(self.timeline_catch_up_limit), 31),
            ),
            batch_limit=max(1, min(int(self.batch_limit), 1_000)),
            model=(
                compact_whitespace(self.model)
                or DEFAULT_MAINTENANCE_MODEL
            ),
            thinking_level=(
                compact_whitespace(self.thinking_level).lower()
                or DEFAULT_MAINTENANCE_THINKING_LEVEL
            ),
        )

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        project: str = "",
        role_id: str = "",
        role_version: str = "",
    ) -> "PersonalContextMaintenanceConfig":
        values = os.environ if environ is None else environ
        return cls(
            enabled=_environment_bool(
                values,
                "RAG_IME_PERSONAL_CONTEXT_MAINTENANCE_ENABLED",
                default=True,
            ),
            consolidate_roles=_environment_bool(
                values,
                "RAG_IME_MEMORY_DREAMING_ENABLED",
                default=True,
            ),
            build_timelines=_environment_bool(
                values,
                "RAG_IME_MEMORY_AUTOMATIC_ORGANIZATION_ENABLED",
                default=True,
            ),
            project=project or values.get("RAG_IME_PROJECT", ""),
            role_id=role_id,
            role_version=role_version,
            min_interval_ms=(
                max(
                    0,
                    _environment_int(
                        values,
                        "RAG_IME_PERSONAL_CONTEXT_INTERVAL_SECONDS",
                        default=24 * 60 * 60,
                    ),
                )
                * 1_000
            ),
            apply_safe_recent_work=_environment_bool(
                values,
                "RAG_IME_PERSONAL_CONTEXT_APPLY_SAFE_RECENT_WORK",
                default=False,
            ),
            auto_publish_timelines=_environment_bool(
                values,
                "RAG_IME_PERSONAL_CONTEXT_AUTO_PUBLISH_TIMELINES",
                default=False,
            ),
            batch_limit=_environment_int(
                values,
                "RAG_IME_PERSONAL_CONTEXT_BATCH_LIMIT",
                default=500,
            ),
            model=values.get(
                "RAG_IME_MEMORY_DREAMING_MODEL",
                DEFAULT_MAINTENANCE_MODEL,
            ),
            thinking_level=values.get(
                "RAG_IME_MEMORY_DREAMING_THINKING_LEVEL",
                DEFAULT_MAINTENANCE_THINKING_LEVEL,
            ),
        ).normalized()



class PersonalContextMaintenanceRunner:
    """Run daily per-project, per-role consolidation outside the Agent loop."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        config: PersonalContextMaintenanceConfig | None = None,
        role_book_applier: object | None = None,
        role_book_organizer: object | None = None,
        activity_organizer: object | None = None,
        consolidator_factory: ConsolidatorFactory = PersonalContextConsolidator,
    ) -> None:
        self.db_path = Path(db_path)
        self.config = (config or PersonalContextMaintenanceConfig()).normalized()
        self._provided_role_book_applier = role_book_applier
        self._provided_role_book_organizer = role_book_organizer
        self._provided_activity_organizer = activity_organizer
        self._default_role_book_applier: AgentRoleBookStore | None = None
        self._default_role_book_organizer: object | None = None
        self._default_role_book_organizer_resolved = False
        self._default_activity_organizer: object | None = None
        self._default_activity_organizer_resolved = False
        self._consolidator_factory = consolidator_factory

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            apply_database_migrations(conn)

    def status(self, *, now_ms: int | None = None) -> dict[str, object]:
        timestamp = _timestamp(now_ms)
        self.initialize()
        targets = [
            self._safe_target_status(project, role_id, now_ms=timestamp)
            for project, role_id in self._target_keys()
        ]
        return self._status_report(targets, generated_at_ms=timestamp)

    def run_once(
        self,
        *,
        now_ms: int | None = None,
        force: bool = False,
        progress: object | None = None,
    ) -> dict[str, object]:
        timestamp = _timestamp(now_ms)
        self.initialize()
        target_keys = self._target_keys()
        timeline_date = _local_date(timestamp)
        timeline_results = self._draft_activity_timelines(
            timeline_date,
            timestamp=timestamp,
            target_keys=target_keys,
        )
        timeline_ids = {
            str(item.get("project") or ""): str(
                (item.get("timeline") or {}).get("timelineId") or ""
            )
            for item in timeline_results
            if isinstance(item.get("timeline"), Mapping)
        }

        targets: list[dict[str, object]] = []
        for project, role_id in target_keys:
            before = self._safe_target_status(project, role_id, now_ms=timestamp)
            entry = dict(before)
            if not self.config.enabled or not self.config.consolidate_roles:
                entry["runStatus"] = "disabled"
                entry["disabledReason"] = (
                    "maintenance_disabled"
                    if not self.config.enabled
                    else "dreaming_disabled"
                )
                targets.append(entry)
                continue
            if before.get("probeError"):
                entry["runStatus"] = "failed"
                entry["error"] = str(before["probeError"])
                targets.append(entry)
                continue
            if not force and not bool(before.get("due")):
                entry["runStatus"] = "not_due"
                targets.append(entry)
                continue
            role_version = str(before.get("roleVersion") or "")
            if not role_version:
                entry["runStatus"] = "failed"
                entry["error"] = "role_version_unresolved"
                targets.append(entry)
                continue
            run_error = ""
            try:
                target_timeline_id = (
                    self._timeline_id_for_target(
                        project,
                        role_id,
                        generated_at_ms=timestamp,
                        fallback_timeline_id=timeline_ids.get(project, ""),
                    )
                    if self.config.build_timelines
                    else ""
                )
                result = self._consolidator(project).run(
                    role_id,
                    role_version,
                    now_ms=timestamp,
                    min_interval_ms=self.config.min_interval_ms,
                    force=bool(force),
                    apply_safe_recent_work=self.config.apply_safe_recent_work,
                    batch_limit=self.config.batch_limit,
                    activity_timeline_id=target_timeline_id,
                )
                entry["runStatus"] = str(result.get("status") or "unknown")
                entry["runId"] = str(result.get("runId") or "")
                entry["resultReason"] = str(result.get("reason") or "")
                entry["artifacts"] = _artifact_refs(result)
                if result.get("error"):
                    run_error = str(result["error"])
            except Exception as exc:
                entry["runStatus"] = "failed"
                run_error = _public_error(exc)
            outcome = {
                key: entry[key]
                for key in ("runStatus", "runId", "resultReason", "artifacts")
                if key in entry
            }
            refreshed = self._safe_target_status(project, role_id, now_ms=timestamp)
            entry.update(refreshed)
            entry.update(outcome)
            if run_error:
                entry["error"] = run_error
            if refreshed.get("probeError"):
                entry["statusRefreshError"] = str(refreshed["probeError"])
            targets.append(entry)

        timeline_catch_up: dict[str, object] = {
            "schemaVersion": "rag-ime.activity-timeline-catch-up.v1",
            "ok": True,
            "skipped": True,
            "reason": "automatic_catch_up_disabled",
            "throughDate": "",
            "pendingDayCount": 0,
            "batchDayCount": 0,
            "completedDayCount": 0,
            "remainingDayCount": 0,
            "failedDate": "",
            "error": "",
            "warningCount": 0,
            "activityTimelines": [],
        }
        if (
            self.config.enabled
            and self.config.build_timelines
            and self.config.auto_publish_timelines
            and self.config.timeline_catch_up_limit > 0
        ):
            previous_date = (
                datetime.fromtimestamp(timestamp / 1_000).astimezone().date()
                - timedelta(days=1)
            ).isoformat()
            timeline_catch_up = self.build_activity_timelines_through(
                previous_date,
                now_ms=timestamp,
                progress=progress,
                max_days=self.config.timeline_catch_up_limit,
                progress_phase="activity_timeline_auto_catch_up",
            )

        failed_count = sum(
            1 for target in targets if str(target.get("runStatus") or "") == "failed"
        )
        timeline_failed_count = sum(
            1 for item in timeline_results if not bool(item.get("ok"))
        )
        timeline_warning_count = sum(
            1
            for item in timeline_results
            if isinstance(item.get("semanticOrganization"), Mapping)
            and str(item["semanticOrganization"].get("status") or "") == "warning"
        )
        return {
            "schemaVersion": PERSONAL_CONTEXT_MAINTENANCE_RUN_SCHEMA_VERSION,
            "ok": (
                failed_count == 0
                and timeline_failed_count == 0
                and timeline_catch_up.get("ok") is True
            ),
            "generatedAtMs": timestamp,
            "enabled": self.config.enabled,
            "dreamingEnabled": self.config.consolidate_roles,
            "automaticOrganizationEnabled": self.config.build_timelines,
            "draftOnly": not self.config.apply_safe_recent_work,
            "applySafeRecentWork": self.config.apply_safe_recent_work,
            "autoPublishTimelines": self.config.auto_publish_timelines,
            "force": bool(force),
            "intervalMs": self.config.min_interval_ms,
            "batchLimit": self.config.batch_limit,
            "model": self.config.model,
            "activityTimelineDate": timeline_date,
            "activityTimelineSummary": {
                "projectCount": len(timeline_results),
                "draftCount": sum(
                    1
                    for item in timeline_results
                    if str(item.get("status") or "") == "draft"
                ),
                "approvedCount": sum(
                    1
                    for item in timeline_results
                    if str(item.get("status") or "") == "approved"
                ),
                "noEventCount": sum(
                    1
                    for item in timeline_results
                    if str(item.get("status") or "") == "no_events"
                ),
                "failedCount": timeline_failed_count,
                "warningCount": timeline_warning_count,
            },
            "activityTimelines": timeline_results,
            "activityTimelineCatchUp": timeline_catch_up,
            "summary": _summary(targets),
            "targets": targets,
        }

    def build_activity_timeline(
        self,
        timeline_date: str,
        *,
        now_ms: int | None = None,
    ) -> dict[str, object]:
        """Run the same governed Activity path used by scheduled maintenance."""

        timestamp = _timestamp(now_ms)
        self.initialize()
        return self._build_activity_timeline(
            self.config.project,
            timeline_date,
            timestamp=timestamp,
        )

    def build_activity_timelines_through(
        self,
        through_date: str,
        *,
        start_date: str = "",
        now_ms: int | None = None,
        progress: object | None = None,
        max_days: int | None = None,
        progress_phase: str = "activity_timeline_catch_up",
    ) -> dict[str, object]:
        """Serially organize a bounded prefix of pending source days."""

        timestamp = _timestamp(now_ms)
        self.initialize()
        store = DailyActivityTimelineStore(
            self.db_path,
            project=self.config.project,
        )
        pending_dates = store.dates_requiring_model_organization(
            through_date,
            **({"start_date": start_date} if start_date else {}),
        )
        bounded_limit = (
            len(pending_dates)
            if max_days is None
            else max(0, min(int(max_days), 31))
        )
        dates = pending_dates[:bounded_limit]
        phase = (
            "activity_timeline_auto_catch_up"
            if progress_phase == "activity_timeline_auto_catch_up"
            else "activity_timeline_catch_up"
        )
        results: list[dict[str, object]] = []
        callback = progress if callable(progress) else None
        if callback is not None:
            callback(
                {
                    "phase": phase,
                    "throughDate": through_date,
                    "totalDayCount": len(dates),
                    "backlogDayCount": len(pending_dates),
                    "completedDayCount": 0,
                    "remainingDayCount": len(pending_dates),
                    "currentDate": "",
                }
            )
        for index, timeline_date in enumerate(dates):
            if callback is not None:
                completed_so_far = sum(item.get("ok") is True for item in results)
                callback(
                    {
                        "phase": phase,
                        "throughDate": through_date,
                        "totalDayCount": len(dates),
                        "backlogDayCount": len(pending_dates),
                        "completedDayCount": completed_so_far,
                        "remainingDayCount": max(
                            0,
                            len(pending_dates) - completed_so_far,
                        ),
                        "currentDate": timeline_date,
                    }
                )
            result = self._build_activity_timeline(
                self.config.project,
                timeline_date,
                timestamp=timestamp + index,
            )
            timeline = (
                result.get("timeline")
                if isinstance(result.get("timeline"), Mapping)
                else {}
            )
            semantic = (
                result.get("semanticOrganization")
                if isinstance(result.get("semanticOrganization"), Mapping)
                else {}
            )
            # A review warning keeps the draft unorganized. It must remain in
            # the backlog rather than being counted as a completed catch-up day.
            day_completed = (
                result.get("ok") is True
                and str(semantic.get("status") or "") != "warning"
            )
            results.append(
                {
                    "ok": day_completed,
                    "date": timeline_date,
                    "status": str(result.get("status") or ""),
                    "timelineId": str(timeline.get("timelineId") or ""),
                    "eventCount": int(timeline.get("eventCount") or 0),
                    "activityCount": int(timeline.get("segmentCount") or 0),
                    "semanticStatus": str(semantic.get("status") or ""),
                    "receipt": dict(semantic.get("receipt") or {}),
                    "reviewRequired": semantic.get("reviewRequired") is True,
                    "verification": dict(semantic.get("verification") or {}),
                    "error": str(result.get("error") or semantic.get("error") or ""),
                }
            )
            # A review warning is still pending, but another date can be
            # organized independently. Stop only for an execution failure.
            if result.get("ok") is not True:
                break
        completed = sum(item.get("ok") is True for item in results)
        failed = next(
            (item for item in results if item.get("ok") is not True),
            None,
        )
        if callback is not None:
            callback(
                {
                    "phase": phase,
                    "throughDate": through_date,
                    "totalDayCount": len(dates),
                    "backlogDayCount": len(pending_dates),
                    "completedDayCount": completed,
                    "remainingDayCount": max(
                        0,
                        len(pending_dates) - completed,
                    ),
                    "currentDate": str((failed or {}).get("date") or ""),
                    "failedDate": str((failed or {}).get("date") or ""),
                }
            )
        return {
            "schemaVersion": "rag-ime.activity-timeline-catch-up.v1",
            "ok": failed is None,
            "throughDate": through_date,
            "pendingDayCount": len(pending_dates),
            "batchDayCount": len(dates),
            "completedDayCount": completed,
            "remainingDayCount": max(0, len(pending_dates) - completed),
            "failedDate": str((failed or {}).get("date") or ""),
            "error": str((failed or {}).get("error") or ""),
            "warningCount": sum(
                1
                for item in results
                if str(item.get("semanticStatus") or "") == "warning"
            ),
            "activityTimelines": results,
        }

    def _draft_activity_timelines(
        self,
        timeline_date: str,
        *,
        timestamp: int,
        target_keys: tuple[tuple[str, str], ...],
    ) -> list[dict[str, object]]:
        if not self.config.enabled or not self.config.build_timelines:
            return []
        projects = {project for project, _ in target_keys}
        try:
            discovered = DailyActivityTimelineStore(
                self.db_path,
            ).projects_for_date(timeline_date)
            projects.update(discovered)
        except Exception as exc:
            return [
                {
                    "schemaVersion": "rag-ime.daily-activity-timeline-build.v1",
                    "ok": False,
                    "created": False,
                    "status": "failed",
                    "project": self.config.project,
                    "date": timeline_date,
                    "timeline": {},
                    "error": _public_error(exc),
                }
            ]
        if self.config.project:
            projects = {
                project for project in projects if project == self.config.project
            }
            projects.add(self.config.project)
        results: list[dict[str, object]] = []
        for project in sorted(projects):
            try:
                result = self._build_activity_timeline(
                    project,
                    timeline_date,
                    timestamp=timestamp,
                )
                results.append(result)
            except Exception as exc:
                results.append(
                    {
                        "schemaVersion": "rag-ime.daily-activity-timeline-build.v1",
                        "ok": False,
                        "created": False,
                        "status": "failed",
                        "project": project,
                        "date": timeline_date,
                        "timeline": {},
                        "error": _public_error(exc),
                    }
                )
        return results

    def _build_activity_timeline(
        self,
        project: str,
        timeline_date: str,
        *,
        timestamp: int,
    ) -> dict[str, object]:
        store = DailyActivityTimelineStore(self.db_path, project=project)
        result = store.build_draft(
            timeline_date,
            generated_at_ms=timestamp,
        )
        timeline = result.get("timeline")
        organization: dict[str, object] = {}
        organization_error = ""
        organization_warning: dict[str, object] = {}
        needs_organization = bool(
            self.config.auto_publish_timelines
            and isinstance(timeline, Mapping)
            and str(result.get("status") or "") in {"draft", "approved"}
            and store.requires_model_organization(
                str(timeline.get("timelineId") or "")
            )
        )
        if (
            needs_organization
            and isinstance(timeline, Mapping)
        ):
            organizer = self._activity_organizer()
            organize = getattr(organizer, "organize_activity_timeline", None)
            if not callable(organize):
                organization_error = "activity_organizer_unavailable"
            else:
                packet = store.organization_packet(
                    str(timeline.get("timelineId") or "")
                )
                model_run_started = False
                try:
                    begin_model_run = getattr(organizer, "begin_run", None)
                    if callable(begin_model_run):
                        begin_model_run(
                            f"activity-organization:{packet.membership_sha256}",
                            frozen_input_sha256=hashlib.sha256(
                                packet.json_text().encode("utf-8")
                            ).hexdigest(),
                        )
                        model_run_started = True
                    organization = dict(organize(packet=packet))
                    organized_payload = organization.get("organization")
                    receipt = organization.get("receipt")
                    if not isinstance(organized_payload, Mapping) or not isinstance(
                        receipt, Mapping
                    ):
                        raise ValueError(
                            "Activity organizer returned an incomplete governed result"
                        )
                    timeline = store.apply_model_organization(
                        str(timeline.get("timelineId") or ""),
                        organization=organized_payload,
                        receipt=receipt,
                        organized_at_ms=timestamp,
                    )
                    result = {**result, "timeline": timeline}
                    if model_run_started:
                        finish_model_run = getattr(organizer, "finish_run", None)
                        if callable(finish_model_run):
                            finish_model_run()
                        model_run_started = False
                except ActivitySemanticVerificationError as exc:
                    if model_run_started:
                        finish_model_run = getattr(organizer, "finish_run", None)
                        try:
                            if callable(finish_model_run):
                                finish_model_run()
                        except Exception as finish_exc:
                            organization_error = _public_error(finish_exc)
                            fail_model_run = getattr(organizer, "fail_run", None)
                            if callable(fail_model_run):
                                try:
                                    fail_model_run(finish_exc)
                                except Exception:
                                    pass
                        else:
                            model_run_started = False
                    if not organization_error:
                        organization_warning = {
                            "status": "warning",
                            "error": _public_error(exc),
                            "reviewRequired": True,
                            "verification": dict(exc.verification),
                            "receipt": dict(exc.receipt),
                        }
                    organization = {}
                except Exception as exc:
                    if model_run_started:
                        fail_model_run = getattr(organizer, "fail_run", None)
                        if callable(fail_model_run):
                            try:
                                fail_model_run(exc)
                            except Exception:
                                pass
                    organization_error = _public_error(exc)
                    organization = {}
        if organization_error:
            return {
                **result,
                "ok": False,
                "status": "failed",
                "autoPublished": False,
                "semanticOrganization": {
                    "status": "failed",
                    "error": organization_error,
                },
            }
        if organization_warning:
            return {
                **result,
                "ok": True,
                "timeline": timeline,
                "autoPublished": False,
                "semanticOrganization": organization_warning,
            }
        if (
            self.config.auto_publish_timelines
            and str(result.get("status") or "") == "draft"
            and isinstance(timeline, Mapping)
        ):
            approved = store.approve(
                str(timeline.get("timelineId") or ""),
                expected_source_event_hash=str(
                    timeline.get("sourceEventHash") or ""
                ),
                approved_by="system:memory-maintenance",
                confirm_text="approve",
                approved_at_ms=timestamp,
            )
            return {
                **result,
                "status": "approved",
                "timeline": approved,
                "autoPublished": True,
                "semanticOrganization": {
                    "status": "completed",
                    "receipt": dict(organization.get("receipt") or {}),
                },
            }
        if needs_organization:
            return {
                **result,
                "timeline": timeline,
                "autoPublished": False,
                "semanticOrganization": {
                    "status": "completed",
                    "receipt": dict(organization.get("receipt") or {}),
                },
            }
        return {
            **result,
            "autoPublished": False,
            "semanticOrganization": {
                "status": "not_required",
                "receipt": {},
            },
        }

    def _timeline_id_for_target(
        self,
        project: str,
        role_id: str,
        *,
        generated_at_ms: int,
        fallback_timeline_id: str,
    ) -> str:
        """Build/load the timeline for the next evidence day, not wall-clock day."""

        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT last_evidence_at_ms, last_evidence_id
                FROM personal_context_consolidation_cursors
                WHERE project = ? AND role_id = ?
                """,
                (project, role_id),
            ).fetchone()
            cursor_at_ms = int(cursor["last_evidence_at_ms"] or 0) if cursor else 0
            cursor_id = str(cursor["last_evidence_id"] or "") if cursor else ""
            evidence = conn.execute(
                """
                SELECT occurred_at_ms
                FROM agent_memory_evidence
                WHERE project = ? AND role_id = ? AND status = 'active'
                  AND (
                    occurred_at_ms > ?
                    OR (occurred_at_ms = ? AND evidence_id > ?)
                  )
                ORDER BY occurred_at_ms ASC, evidence_id ASC
                LIMIT 1
                """,
                (project, role_id, cursor_at_ms, cursor_at_ms, cursor_id),
            ).fetchone()
        if evidence is None:
            return fallback_timeline_id
        timeline_date = _local_date(int(evidence["occurred_at_ms"] or 0))
        result = self._build_activity_timeline(
            project,
            timeline_date,
            timestamp=generated_at_ms,
        )
        timeline = result.get("timeline")
        if isinstance(timeline, Mapping):
            return str(timeline.get("timelineId") or "")
        return ""

    def _status_report(
        self,
        targets: list[dict[str, object]],
        *,
        generated_at_ms: int,
    ) -> dict[str, object]:
        failed_count = sum(
            1
            for target in targets
            if target.get("probeError")
            or str(target.get("status") or "")
            in {"error", "failed"}
            or (
                self.config.enabled
                and self.config.consolidate_roles
                and str(target.get("status") or "") == "unresolved"
            )
        )
        return {
            "schemaVersion": PERSONAL_CONTEXT_MAINTENANCE_STATUS_SCHEMA_VERSION,
            "ok": failed_count == 0,
            "generatedAtMs": generated_at_ms,
            "enabled": self.config.enabled,
            "dreamingEnabled": self.config.consolidate_roles,
            "automaticOrganizationEnabled": self.config.build_timelines,
            "draftOnly": not self.config.apply_safe_recent_work,
            "applySafeRecentWork": self.config.apply_safe_recent_work,
            "autoPublishTimelines": self.config.auto_publish_timelines,
            "intervalMs": self.config.min_interval_ms,
            "batchLimit": self.config.batch_limit,
            "model": self.config.model,
            "summary": _summary(targets),
            "targets": targets,
        }

    def _safe_target_status(
        self,
        project: str,
        role_id: str,
        *,
        now_ms: int,
    ) -> dict[str, object]:
        try:
            return self._target_status(project, role_id, now_ms=now_ms)
        except Exception as exc:
            error = _public_error(exc)
            return {
                "project": project,
                "roleId": role_id,
                "roleVersion": "",
                "roleVersionSource": "unavailable",
                "due": False,
                "dueReason": "probe_failed",
                "nextDueAtMs": 0,
                "status": "error",
                "error": error,
                "probeError": error,
                "lastRunId": "",
                "lastRunStatus": "",
                "lastRunAtMs": 0,
                "lastSucceededAtMs": 0,
                "lastRunError": "",
                "attemptCount": 0,
            }

    def _target_status(
        self,
        project: str,
        role_id: str,
        *,
        now_ms: int,
    ) -> dict[str, object]:
        role_version, version_source = self._resolve_role_version(project, role_id)
        entry: dict[str, object] = {
            "project": project,
            "roleId": role_id,
            "roleVersion": role_version,
            "roleVersionSource": version_source,
            "due": False,
            "dueReason": "",
            "nextDueAtMs": 0,
            "status": "idle",
            "error": "",
        }
        try:
            due = self._consolidator(project).due(
                role_id,
                now_ms=now_ms,
                min_interval_ms=self.config.min_interval_ms,
            )
            entry["due"] = bool(due.get("due"))
            entry["dueReason"] = str(due.get("reason") or "")
            entry["nextDueAtMs"] = int(due.get("nextDueAtMs") or 0)
            entry["cursor"] = dict(due.get("cursor") or {})
            entry["status"] = "due" if entry["due"] else "idle"
        except Exception as exc:
            entry["status"] = "error"
            entry["probeError"] = _public_error(exc)
            entry["error"] = entry["probeError"]
        entry.update(self._latest_run_fields(project, role_id))
        if bool(entry.get("due")) and not role_version:
            entry["status"] = "unresolved"
            entry["error"] = "role_version_unresolved"
        elif (
            str(entry.get("lastRunStatus") or "") == "failed"
            and str(entry.get("dueReason") or "").startswith("retry_")
        ):
            entry["status"] = "failed"
            entry["error"] = str(entry.get("lastRunError") or "")
        return entry

    def _target_keys(self) -> tuple[tuple[str, str], ...]:
        where = ["role_id <> ''"]
        params: list[object] = []
        if self.config.project:
            where.append("project = ?")
            params.append(self.config.project)
        if self.config.role_id:
            where.append("role_id = ?")
            params.append(self.config.role_id)
        predicate = " AND ".join(where)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT project, role_id
                FROM agent_memory_evidence
                WHERE status = 'active' AND {predicate}
                UNION
                SELECT project, role_id
                FROM personal_context_consolidation_runs
                WHERE {predicate}
                UNION
                SELECT project, role_id
                FROM personal_context_consolidation_cursors
                WHERE {predicate}
                ORDER BY project, role_id
                """,
                tuple(params * 3),
            ).fetchall()
        keys = {(str(row["project"] or ""), str(row["role_id"] or "")) for row in rows}
        if self.config.project and self.config.role_id:
            keys.add((self.config.project, self.config.role_id))
        return tuple(sorted(keys))

    def _resolve_role_version(
        self,
        project: str,
        role_id: str,
    ) -> tuple[str, str]:
        if self.config.role_id == role_id and self.config.role_version:
            return self.config.role_version, "explicit"
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT s.role_version
                FROM agent_memory_evidence e
                JOIN agent_sessions s ON s.id = e.session_id
                WHERE e.project = ? AND e.role_id = ? AND e.status = 'active'
                  AND s.role_id = e.role_id AND s.role_version <> ''
                ORDER BY e.occurred_at_ms DESC, e.evidence_id DESC
                LIMIT 1
                """,
                (project, role_id),
            ).fetchone()
            if row is not None:
                return str(row["role_version"]), "latest_evidence_session"
            row = conn.execute(
                """
                SELECT role_version
                FROM personal_context_consolidation_runs
                WHERE project = ? AND role_id = ? AND role_version <> ''
                ORDER BY created_at_ms DESC, run_id DESC
                LIMIT 1
                """,
                (project, role_id),
            ).fetchone()
            if row is not None:
                return str(row["role_version"]), "last_consolidation"
            row = conn.execute(
                """
                SELECT role_version
                FROM agent_role_books
                WHERE role_id = ? AND role_version <> ''
                ORDER BY updated_at_ms DESC, role_version DESC
                LIMIT 1
                """,
                (role_id,),
            ).fetchone()
        if row is not None:
            return str(row["role_version"]), "latest_role_book"
        return "", "unresolved"

    def _latest_run_fields(
        self,
        project: str,
        role_id: str,
        *,
        preserve_run_status: str = "",
    ) -> dict[str, object]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT run_id, role_version, status, output_json, attempt_count,
                       error_text, created_at_ms, started_at_ms,
                       completed_at_ms, updated_at_ms
                FROM personal_context_consolidation_runs
                WHERE project = ? AND role_id = ?
                ORDER BY created_at_ms DESC, run_id DESC
                LIMIT 1
                """,
                (project, role_id),
            ).fetchone()
            cursor = conn.execute(
                """
                SELECT last_succeeded_at_ms
                FROM personal_context_consolidation_cursors
                WHERE project = ? AND role_id = ?
                """,
                (project, role_id),
            ).fetchone()
        last_succeeded_at_ms = int(cursor["last_succeeded_at_ms"] or 0) if cursor else 0
        if row is None:
            return {
                "lastRunId": "",
                "lastRunStatus": "",
                "lastRunAtMs": 0,
                "lastSucceededAtMs": last_succeeded_at_ms,
                "lastRunError": "",
                "attemptCount": 0,
            }
        output = _json_object(row["output_json"])
        fields: dict[str, object] = {
            "lastRunId": str(row["run_id"]),
            "lastRunStatus": str(row["status"]),
            "lastRunAtMs": int(
                row["completed_at_ms"]
                or row["updated_at_ms"]
                or row["started_at_ms"]
                or row["created_at_ms"]
                or 0
            ),
            "lastSucceededAtMs": last_succeeded_at_ms,
            "lastRunError": str(row["error_text"] or ""),
            "attemptCount": int(row["attempt_count"] or 0),
            "lastArtifacts": _artifact_refs(output),
        }
        if preserve_run_status:
            fields["runStatus"] = preserve_run_status
        return fields

    def _consolidator(self, project: str) -> PersonalContextConsolidator:
        kwargs: dict[str, object] = {
            "project": project,
            "role_book_applier": self._role_book_applier(),
        }
        organizer = self._role_book_organizer()
        if organizer is not None:
            kwargs["role_book_organizer"] = organizer
        return self._consolidator_factory(self.db_path, **kwargs)

    def _role_book_applier(self) -> object | None:
        if not self.config.apply_safe_recent_work:
            return None
        if self._provided_role_book_applier is not None:
            return self._provided_role_book_applier
        if self._default_role_book_applier is None:
            store = AgentRoleBookStore(self.db_path)
            store.initialize()
            self._default_role_book_applier = store
        return self._default_role_book_applier

    def _role_book_organizer(self) -> object | None:
        if self._provided_role_book_organizer is not None:
            return self._provided_role_book_organizer
        if self._default_role_book_organizer_resolved:
            return self._default_role_book_organizer
        self._default_role_book_organizer_resolved = True
        try:
            executor = build_managed_pi_memory_model_executor(
                self.db_path,
                self.config.model,
                self.config.thinking_level,
            )
        except MemoryModelUnavailable:
            self._default_role_book_organizer = None
        else:
            self._default_role_book_organizer = ManagedPiMemoryOrganizer(executor)
        return self._default_role_book_organizer

    def _activity_organizer(self) -> object | None:
        if self._provided_activity_organizer is not None:
            return self._provided_activity_organizer
        if self._provided_role_book_organizer is not None and callable(
            getattr(
                self._provided_role_book_organizer,
                "organize_activity_timeline",
                None,
            )
        ):
            return self._provided_role_book_organizer
        if self._default_activity_organizer_resolved:
            return self._default_activity_organizer
        self._default_activity_organizer_resolved = True
        try:
            executor = build_managed_pi_memory_model_executor(
                self.db_path,
                self.config.model,
                self.config.thinking_level,
            )
        except MemoryModelUnavailable:
            self._default_activity_organizer = None
        else:
            self._default_activity_organizer = ManagedPiMemoryOrganizer(executor)
        return self._default_activity_organizer

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


def write_personal_context_maintenance_report(
    path: str | Path,
    payload: Mapping[str, object],
) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


def _summary(targets: list[dict[str, object]]) -> dict[str, int]:
    return {
        "targetCount": len(targets),
        "dueCount": sum(1 for target in targets if bool(target.get("due"))),
        "succeededCount": sum(
            1 for target in targets if str(target.get("runStatus") or "") == "succeeded"
        ),
        "failedCount": sum(
            1 for target in targets if str(target.get("runStatus") or "") == "failed"
        ),
        "errorCount": sum(
            1
            for target in targets
            if target.get("probeError")
            or target.get("statusRefreshError")
            or str(target.get("status") or "")
            in {"error", "failed", "unresolved"}
        ),
        "notDueCount": sum(
            1 for target in targets if str(target.get("runStatus") or "") == "not_due"
        ),
        "disabledCount": sum(
            1 for target in targets if str(target.get("runStatus") or "") == "disabled"
        ),
    }


def _artifact_refs(payload: Mapping[str, object]) -> dict[str, str]:
    digest = payload.get("digest")
    digest = digest if isinstance(digest, Mapping) else {}
    user_draft = payload.get("userMemoryDraft")
    user_draft = user_draft if isinstance(user_draft, Mapping) else {}
    role_draft = payload.get("roleBookDraft")
    role_draft = role_draft if isinstance(role_draft, Mapping) else {}
    return {
        "digestId": str(digest.get("digestId") or ""),
        "activityTimelineId": str(digest.get("activityTimelineId") or ""),
        "userMemoryDraftId": str(user_draft.get("draftId") or ""),
        "roleBookDraftId": str(role_draft.get("draftId") or ""),
        "appliedRoleBookRevisionId": str(
            payload.get("appliedRoleBookRevisionId") or ""
        ),
        "proposedRoleBookRevisionId": str(
            payload.get("proposedRoleBookRevisionId") or ""
        ),
    }


def _environment_bool(
    environ: Mapping[str, str],
    key: str,
    *,
    default: bool,
) -> bool:
    raw = compact_whitespace(environ.get(key, ""))
    if not raw:
        return default
    normalized = raw.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{key} must be a boolean flag")


def _environment_int(
    environ: Mapping[str, str],
    key: str,
    *,
    default: int,
) -> int:
    raw = compact_whitespace(environ.get(key, ""))
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc


def _json_object(value: object) -> dict[str, Any]:
    try:
        payload = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _timestamp(value: int | None) -> int:
    return int(time.time() * 1_000) if value is None else max(0, int(value))


def _local_date(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1_000).astimezone().date().isoformat()


def _public_error(exc: BaseException) -> str:
    return compact_whitespace(str(exc))[:800] or exc.__class__.__name__
