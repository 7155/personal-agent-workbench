from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from .agent_memory_sources import AgentMemorySourceStore
from .contracts.json_schema import validate_contract
from .db import apply_database_migrations
from .embeddings import EmbeddingProvider
from .hybrid_rag_models import HybridRagQuery
from .hybrid_rag_retriever import retrieve_hybrid_rag_memory_hit_objects
from .input_quality import FINALIZED_INPUT_SOURCE, assess_input_text
from .memory_book_compiler import (
    apply_stored_memory_book_run,
    collapse_rime_fragment_run,
    inspect_memory_book_plan,
    memory_book_plan_from_compile_output,
    rime_fragments_belong_together,
    store_memory_book_plan,
)
from .memory_evidence_ledger import backfill_input_event_evidence
from .memory_card_mutations import card_revision, card_source_refs
from .memory_evidence_admission import (
    PERSONAL_EVIDENCE_ORIGINS,
    admitted_personal_evidence_sql,
    curatable_personal_evidence_sql,
    rollback_evidence_admissions_for_run,
    transition_evidence_admission,
)
from .memory_evidence_policy import memory_evidence_exclusion_reason
from .memory_ingest import normalize_text
from .memory_decision_context import resolve_decision_context
from .memory_curation import MEMORY_CURATION_ARCHITECTURE
from .memory_purpose import personal_current_state_profile, purpose_audit_fields
from .personal_context import (
    load_activity_timeline_context,
    local_date_for_timestamp,
    local_day_bounds_ms,
)
from .sensitive_content import contains_sensitive_content
from .text_utils import compact_whitespace, split_sentences, stable_text_hash, token_terms


OWNER_CURATION_STATUS_SCHEMA_VERSION = "rag-ime.owner-memory-curation-status.v1"
OWNER_CURATION_RUN_SCHEMA_VERSION = "rag-ime.owner-memory-curation-run.v1"
DEFAULT_DAILY_INTERVAL_MS = 24 * 60 * 60 * 1000
DEFAULT_INITIAL_SETTLE_MS = 20 * 60 * 1000
DEFAULT_RUNNING_LEASE_MS = 60 * 60 * 1000
DEFAULT_MAX_SOURCES = 1_000
MAX_PERSONAL_V2_SOURCES = 1_500
MAX_PERSONAL_V2_INPUT_TOKENS = 200_000
MAX_PERSONAL_V2_WINDOW_MS = 12 * 60 * 60 * 1_000
MAX_AUTOMATIC_CATCH_UP_BATCHES = 3
MAX_EXTERNAL_MODEL_INPUTS_PER_RUN = 8
MAX_OWNER_MODEL_INPUTS_PER_RUN = 6
MAX_OWNER_MEMORY_ATOMS_PER_RUN = 6
MAX_ATOM_FIRST_TOPIC_BOOKS_PER_RUN = 8
MAX_EXISTING_MEMORY_RECALL_PROBES = 20
MAX_RECALLED_EXISTING_ATOMS = 20
MAX_RECALLED_EXISTING_BOOKS = 8
MAX_ARCHIVED_TOPIC_BOOK_GUARDS = 64
MIN_CLAIM_REUSE_SCORE = 5.0
MIN_CLAIM_REUSE_MARGIN = 1.5
MIN_EXISTING_CLAIM_COMPATIBILITY_SCORE = 2.5
MIN_SAME_SOURCE_ANCHOR_SCORE = 3.5

_OWNER_KINDS = frozenset({"user", "shared", "agent", "session", "room"})
_ELIGIBLE_DISPOSITIONS = ("pending", "needs_review", "remember")
_CLAIM_UPDATE_SIGNAL_RE = re.compile(
    r"(?:已经|已改|改为|切换|调整|更新|升级|收紧|放宽|不再|退出|替代|"
    r"仍然?|继续|当前|现在|只(?:保留|允许|采用|注入|接收)|仅(?:保留|允许|采用|注入|接收)|"
    r"默认|优先|从.+(?:改|切换|调整)为)",
    re.IGNORECASE,
)
_FILLER_RE = re.compile(
    r"^(?:(?:嗯+|呃+|额+|啊+|哦+|唉+|那个|这个|然后|就是|对对对|好好好|行行行)[，。！？、,.!?\s]*)+$",
    re.IGNORECASE,
)
_REFERENTIAL_FRAGMENT_RE = re.compile(
    r"^(?:这|那|这个|那个|它|他|她|它们|他们|她们|这些|那些|这里|那里|"
    r"这边|那边|这样|那样)(?:呢|吧|啊|呀)?[，。！？、,.!?\s]*$",
    re.IGNORECASE,
)
_RANDOM_INPUT_RE = re.compile(r"^[\W_]*$|^[a-z0-9]{1,3}$", re.IGNORECASE)
_RUNTIME_PROBE_RE = re.compile(
    r"(?:测试一下|test(?:ing)?\s*(?:123)?|hello\s*world|ceshiwendang|"
    r"wait for (?:llm|model)|只回复数字|不要调用工具)",
    re.IGNORECASE,
)
_TRANSIENT_TOOL_RECEIPT_RE = re.compile(
    r"(?:已暂停|已恢复|已停止|已启动|已重启|暂停成功|恢复成功|停止成功|启动成功|重启成功)",
    re.IGNORECASE,
)
_FAILED_TOOL_RECEIPT_RE = re.compile(
    r"(?:失败|出错|错误|异常|被拒绝|校验拒绝|未成功|未生成|未执行|未应用|"
    r"无法完成|调用超时|执行超时|\bfailed\b|\berror\b|\btimeout\b|\bblocked\b)",
    re.IGNORECASE,
)
_TRANSIENT_USER_COMMAND_RE = re.compile(
    r"^(?:请)?(?:继续|重试|再试(?:一次)?|刷新|打开|关闭|点击|滚动|切换|"
    r"修改|改|修复|合并|提交|编译|安装|运行|检查|看一下|读一下|删除)(?:一下|这个|该|当前)?"
    r"[^。！？!?]{0,36}[。！？!?]?$",
    re.IGNORECASE,
)
_TRANSIENT_CONTEXT_SIGNAL_RE = re.compile(
    r"(?:临时|暂时|演示|排查|测试|试用|当前操作|随手|这轮|本轮)",
    re.IGNORECASE,
)
_NON_DURABLE_CONCLUSION_RE = re.compile(
    r"(?:不形成|没有形成|未形成|不代表|没有决定|未决定|尚未形成|"
    r"不保留|没有变化|未变化|不是(?:产品)?约束|没有产生|未产生|"
    r"下一轮(?:仍|恢复|继续))",
    re.IGNORECASE,
)
_EXPLICIT_MEMORY_FORGET_RE = re.compile(
    r"^(?:(?:请|帮我|把)\s*)?(?:忘(?:掉|记)|不要再记|别再记)|"
    r"(?:忘(?:掉|记)|删除|移除|清除|撤回).{0,20}(?:记忆|事实|偏好|这条)|"
    r"(?:不要再记|不再记住|别再记)",
    re.IGNORECASE,
)
_QUESTION_SIGNAL_RE = re.compile(
    r"(?:为什么|怎么|如何|是什么|什么是|是否|能否|有没有|哪里|哪个|谁|"
    r"什么时候|多少|几种|哪一|咋)",
    re.IGNORECASE,
)
_DURABLE_ASSERTION_RE = re.compile(
    r"(?:我(?:决定|希望|要求|偏好)|以后(?:都|要|不要)|长期|始终|每(?:次|天|周)|"
    r"默认(?:使用|采用|开启|关闭|保留)|项目(?:采用|需要|必须|禁止)|"
    r"必须|禁止|不要|优先|需要支持)",
    re.IGNORECASE,
)
_DERIVED_PROTOCOL_NOISE_RE = re.compile(
    r"(?:user_message|assistant_message|\[敏感内容已隐藏\]|\[REDACTED:|"
    r"请(?:调用|使用)\s*memory|\bcuration_prepare\b|\brunId\b|"
    r"可审阅草案|等待(?:原生)?审阅)",
    re.IGNORECASE,
)
_DURABLE_ATOM_KINDS = frozenset(
    {
        "personal_fact",
        "personal_habit",
        "durable_preference",
        "personal_principle",
        # Existing reviewed catalogs retain these legacy kinds so corrections
        # and rollback remain possible. The active model boundary accepts only
        # the four personal-memory kinds above.
        "project_fact",
        "project_requirement",
        "project_decision",
        "security_constraint",
        "project_constraint",
    }
)
_PERSONAL_ATOM_KINDS = frozenset(
    {
        "personal_fact",
        "personal_habit",
        "durable_preference",
        "personal_principle",
    }
)
_GENERIC_OWNER_BOOK_TITLE_RE = re.compile(
    r"^(?:个人长期记忆|共享长期记忆|.+的长期记忆)$"
)


class OwnerMemoryOrganizer(Protocol):
    @property
    def provider_name(self) -> str: ...

    def curate_owner_memory(
        self,
        *,
        bundle: dict[str, object],
        project: str,
        owner_kind: str,
        owner_id: str,
        instruction: str = "",
    ) -> dict[str, object]: ...


class OwnerMemoryCurator:
    """Compile only user-grounded durable-memory candidates and explicit imports."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        organizer: OwnerMemoryOrganizer,
        project: str = "",
        clock_ms: Callable[[], int] | None = None,
        daily_interval_ms: int = DEFAULT_DAILY_INTERVAL_MS,
        initial_settle_ms: int = DEFAULT_INITIAL_SETTLE_MS,
        running_lease_ms: int = DEFAULT_RUNNING_LEASE_MS,
        max_sources: int = DEFAULT_MAX_SOURCES,
        auto_apply: bool = False,
        include_agent_dialogue: bool = True,
        embedding_provider: EmbeddingProvider | None = None,
        personal_window_ms: int = MAX_PERSONAL_V2_WINDOW_MS,
        observations: object | None = None,
        trace_id: str = "",
        maintenance_job_id: str = "",
        parent_span_id: str = "",
    ) -> None:
        self.db_path = Path(db_path)
        self.organizer = organizer
        self.project = compact_whitespace(project)
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.daily_interval_ms = max(60_000, int(daily_interval_ms))
        self.initial_settle_ms = max(0, int(initial_settle_ms))
        self.running_lease_ms = max(60_000, int(running_lease_ms))
        self.auto_apply = bool(auto_apply)
        self.include_agent_dialogue = bool(include_agent_dialogue)
        self.embedding_provider = embedding_provider
        self.personal_window_ms = max(
            60_000,
            min(24 * 60 * 60 * 1_000, int(personal_window_ms)),
        )
        # Optional to keep the curator usable in isolated/library tests.  The
        # Gateway supplies ObservationHub so a real maintenance run can keep
        # one durable run id across started, review, apply, and failure phases.
        self.observations = observations
        self.trace_id = compact_whitespace(trace_id)
        self.maintenance_job_id = compact_whitespace(maintenance_job_id)
        self.parent_span_id = compact_whitespace(parent_span_id)
        self.curation_protocol_version = compact_whitespace(
            str(getattr(organizer, "curation_protocol_version", ""))
        )
        self.atom_first = (
            self.curation_protocol_version == MEMORY_CURATION_ARCHITECTURE
        )
        # ``personal_v2`` remains the compatibility flag for the canonical
        # Evidence ledger, large frozen batches, resumable model receipts and
        # rollback. Atom-first replaces the old personal-only classifier but
        # must retain those governance guarantees.
        self.personal_v2 = self.curation_protocol_version in {
            "personal-v2",
            MEMORY_CURATION_ARCHITECTURE,
        }
        # Legacy organizers retain their 64-input contract. The governed Luna
        # path uses continuity-sized batches and a separate token hard gate.
        self.max_sources = max(
            1,
            min(
                int(max_sources),
                MAX_PERSONAL_V2_SOURCES if self.personal_v2 else 64,
            ),
        )
        self.sources = AgentMemorySourceStore(self.db_path, project=self.project)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            apply_database_migrations(conn)
            backfill_input_event_evidence(conn, project=self.project)
        self.sources.initialize()

    def status(
        self,
        *,
        owner_kind: str = "",
        owner_id: str = "",
        current_ms: int | None = None,
    ) -> dict[str, object]:
        timestamp = self.clock_ms() if current_ms is None else max(0, int(current_ms))
        if self.personal_v2:
            requested_kind = compact_whitespace(owner_kind)
            requested_id = compact_whitespace(owner_id)
            if requested_kind not in {"", "user"} or requested_id not in {
                "",
                "default",
            }:
                raise ValueError(
                    "canonical Evidence curation only accepts the global user/default owner"
                )
            owner_kind = "user"
            owner_id = "default"
        with self._connect() as conn:
            return owner_memory_curation_status(
                conn,
                project=self.project,
                current_ms=timestamp,
                initial_settle_ms=self.initial_settle_ms,
                daily_interval_ms=self.daily_interval_ms,
                running_lease_ms=self.running_lease_ms,
                owner_kind=owner_kind,
                owner_id=owner_id,
                auto_apply=self.auto_apply,
                include_agent_dialogue=self.include_agent_dialogue,
                canonical_personal=self.personal_v2,
            )

    def run_due(
        self,
        *,
        manual: bool = False,
        owner_kind: str = "",
        owner_id: str = "",
        instruction: str = "",
        current_ms: int | None = None,
    ) -> dict[str, object]:
        timestamp = self.clock_ms() if current_ms is None else max(0, int(current_ms))
        with self._connect() as conn:
            backfill_input_event_evidence(conn, project=self.project)
        before = self.status(
            owner_kind=owner_kind,
            owner_id=owner_id,
            current_ms=timestamp,
        )
        results: list[dict[str, object]] = []
        for scope in before["scopes"]:
            if not isinstance(scope, dict):
                continue
            if not manual and not bool(scope.get("due")):
                continue
            batch_limit = MAX_AUTOMATIC_CATCH_UP_BATCHES if self.personal_v2 and not manual and self.auto_apply else 1
            for _ in range(batch_limit):
                result = self._run_scope(
                    owner_kind=str(scope["ownerKind"]),
                    owner_id=str(scope["ownerId"]),
                    manual=manual,
                    instruction=instruction,
                    current_ms=timestamp,
                )
                results.append(result)
                if not result.get("ok") or not result.get("catchUpPending"):
                    break
        after = self.status(
            owner_kind=owner_kind,
            owner_id=owner_id,
            current_ms=timestamp,
        )
        return {
            "schemaVersion": OWNER_CURATION_RUN_SCHEMA_VERSION,
            "ok": all(bool(item.get("ok")) for item in results),
            "manual": bool(manual),
            "ranScopeCount": len({(item.get("ownerKind"), item.get("ownerId")) for item in results}),
            "ranBatchCount": len(results),
            "results": results,
            "status": after,
        }

    def _run_scope(
        self,
        *,
        owner_kind: str,
        owner_id: str,
        manual: bool,
        instruction: str,
        current_ms: int,
    ) -> dict[str, object]:
        owner = _owner(owner_kind, owner_id)
        claim = self._claim_scope(
            owner_kind=owner[0],
            owner_id=owner[1],
            manual=manual,
            current_ms=current_ms,
        )
        if not claim["claimed"]:
            return {
                "ok": True,
                "ownerKind": owner[0],
                "ownerId": owner[1],
                "skipped": True,
                "reason": claim["reason"],
            }

        run_id = str(claim.get("runId") or _owner_run_id(owner[0], owner[1], current_ms))
        model_run_started = False
        attempt_id = run_id
        personal_evidence_applied = False
        stored_plan = False
        try:
            with self._connect() as conn:
                bundle = _build_owner_source_bundle(
                    conn,
                    owner_kind=owner[0],
                    owner_id=owner[1],
                    project=self.project,
                    limit=self.max_sources,
                    include_agent_dialogue=self.include_agent_dialogue,
                    embedding_provider=self.embedding_provider,
                    include_existing_memory=False,
                    canonical_personal=self.personal_v2,
                    personal_window_ms=self.personal_window_ms,
                )
            inputs = [
                dict(item)
                for item in bundle.get("inputs") or []
                if isinstance(item, dict)
            ]
            if not inputs:
                self._emit_memory_event(
                    phase="draft_finished",
                    status="completed",
                    summary="所有者记忆维护未发现待处理来源",
                    run_id=run_id,
                    attempt_id=attempt_id,
                )
                self._finish_scope(
                    owner_kind=owner[0],
                    owner_id=owner[1],
                    run_id="",
                    boundary=None,
                    status="idle",
                    next_due_at_ms=current_ms + self.daily_interval_ms,
                    current_ms=current_ms,
                )
                return {
                    "ok": True,
                    "ownerKind": owner[0],
                    "ownerId": owner[1],
                    "skipped": True,
                    "reason": "no_sources",
                }

            cursor_boundary = (
                int(
                    dict(bundle.get("cursor") or {}).get(
                        "fromSourceCreatedAtMs"
                    )
                    or 0
                ),
                str(
                    dict(bundle.get("cursor") or {}).get("fromSourceId")
                    or ""
                ),
            )
            input_boundary = (
                int(inputs[-1]["createdAtMs"]),
                str(inputs[-1]["sourceId"]),
            )
            # Capture-v2 outbox replay can insert a finalized input after the
            # daily cursor has passed its event-time position. Personal Memory
            # explicitly rescans unresolved Evidence below, so keep the main
            # watermark monotonic instead of replaying already-adjudicated rows.
            boundary = (
                max(cursor_boundary, input_boundary)
                if self.personal_v2
                else input_boundary
            )
            deterministic: list[dict[str, object]] = []
            model_inputs: list[dict[str, object]] = []
            last_user_input_by_text = {
                normalize_text(str(item.get("text") or "")): index
                for index, item in enumerate(inputs)
                if str(item.get("sourceKind") or "") == "user_final"
                and normalize_text(str(item.get("text") or ""))
            }
            for index, item in enumerate(inputs):
                normalized_text = normalize_text(str(item.get("text") or ""))
                duplicate = (
                    str(item.get("sourceKind") or "") == "user_final"
                    and bool(normalized_text)
                    and last_user_input_by_text.get(normalized_text) != index
                )
                rule = (
                    "duplicate_repeated_input"
                    if duplicate
                    else (
                        _deterministic_personal_v2_disposition(item)
                        if self.personal_v2
                        else _deterministic_disposition(item)
                    )
                )
                evidence_ids = _input_evidence_ids(item)
                evidence_state = compact_whitespace(
                    str(item.get("evidenceAdmissionState") or "")
                ).lower()
                if self.personal_v2 and rule is not None and evidence_state == "admitted":
                    # A deterministic noise/duplicate rule may keep fresh
                    # candidates away from Luna, but it cannot silently demote
                    # Evidence that a previous governed run or the user already
                    # admitted. Preserve the canonical state and move on.
                    source_ids = _input_source_ids(item)
                    deterministic.append(
                        {
                            "sourceRef": item["sourceRef"],
                            "sourceId": item["sourceId"],
                            "sourceIds": source_ids,
                            "evidenceId": evidence_ids[0] if len(evidence_ids) == 1 else "",
                            "evidenceIds": evidence_ids,
                            "evidenceAdmissionState": "admitted",
                            "disposition": "remember",
                            "reasonCode": "already_admitted_evidence",
                            "changed": False,
                        }
                    )
                    continue
                if rule is None:
                    model_inputs.append(item)
                    continue
                source_ids = _input_source_ids(item)
                if self.personal_v2 and evidence_ids:
                    with self._connect() as conn:
                        transitions = [
                            transition_evidence_admission(
                                conn,
                                evidence_id,
                                new_state="rejected",
                                reason_code=rule,
                                actor_kind="rule",
                                run_id=run_id,
                                created_at_ms=current_ms,
                                metadata={
                                    "protocol": "personal-v2",
                                    "sourceRef": str(item["sourceRef"]),
                                },
                            )
                            for evidence_id in evidence_ids
                        ]
                else:
                    # Malformed or ambiguous canonical identity fails closed at
                    # the compatibility source ledger; there is no Evidence row
                    # that can be mutated safely.
                    transitions = [
                        self.sources.set_disposition(
                            source_id,
                            disposition="not_for_memory",
                            reason_code=rule,
                            actor_kind="rule",
                            run_id=run_id,
                            created_at_ms=current_ms,
                        )
                        for source_id in source_ids
                    ]
                deterministic.append(
                    {
                        "sourceRef": item["sourceRef"],
                        "sourceId": item["sourceId"],
                        "sourceIds": source_ids,
                        "evidenceId": evidence_ids[0] if len(evidence_ids) == 1 else "",
                        "evidenceIds": evidence_ids,
                        "evidenceAdmissionState": (
                            "rejected" if evidence_ids else ""
                        ),
                        "disposition": "not_for_memory",
                        "reasonCode": rule,
                        "changed": any(
                            bool(transition["changed"])
                            for transition in transitions
                        ),
                    }
                )

            bounded_model_inputs = (
                _bounded_personal_v2_model_inputs(
                    model_inputs,
                    window_ms=self.personal_window_ms,
                )
                if self.personal_v2
                else _bounded_owner_model_inputs(model_inputs)
            )
            deferred_model_input_count = max(
                0,
                len(model_inputs) - len(bounded_model_inputs),
            )
            if deferred_model_input_count:
                model_inputs = bounded_model_inputs
                # The organizer may emit at most six durable Atoms. Stop the
                # cursor at the last source actually presented to it. The
                # bound accounts for clauses as well as source count because a
                # single user input can legitimately contain several facts.
                model_boundary = (
                    int(model_inputs[-1]["createdAtMs"]),
                    str(model_inputs[-1]["sourceId"]),
                )
                boundary = (
                    max(cursor_boundary, model_boundary)
                    if self.personal_v2
                    else model_boundary
                )

            compile_output: dict[str, object] = {}
            model_decisions: list[dict[str, object]] = []
            claim_key_reconciliations: list[dict[str, object]] = []
            memory_atom_repairs: list[dict[str, object]] = []
            plan: dict[str, object] | None = None
            if model_inputs:
                with self._connect() as conn:
                    existing_memory_context = (
                        _build_atom_first_memory_context(
                            conn,
                            inputs=model_inputs,
                            owner_kind=owner[0],
                            owner_id=owner[1],
                            project=self.project,
                            embedding_provider=self.embedding_provider,
                        )
                        if self.atom_first
                        else _build_current_personal_atom_catalog(conn)
                        if self.personal_v2
                        else _build_existing_memory_context(
                            conn,
                            inputs=model_inputs,
                            owner_kind=owner[0],
                            owner_id=owner[1],
                            project=self.project,
                            embedding_provider=self.embedding_provider,
                        )
                    )
                if self.personal_v2:
                    fitted_model_inputs = _fit_personal_v2_inputs_to_catalog(
                        model_inputs,
                        existing_memory_context=existing_memory_context,
                        context_only=bundle.get("contextOnly"),
                    )
                    if len(fitted_model_inputs) < len(model_inputs):
                        deferred_model_input_count += (
                            len(model_inputs) - len(fitted_model_inputs)
                        )
                        model_inputs = fitted_model_inputs
                        model_boundary = (
                            int(model_inputs[-1]["createdAtMs"]),
                            str(model_inputs[-1]["sourceId"]),
                        )
                        boundary = (
                            max(cursor_boundary, model_boundary)
                            if self.personal_v2
                            else model_boundary
                        )
                model_event_ids = [
                    event_id
                    for item in model_inputs
                    for event_id in item.get("sourceEventIds") or []
                    if isinstance(event_id, int)
                ]
                model_bundle_payload = {
                        **bundle,
                        **existing_memory_context,
                        "inputs": model_inputs,
                        "recentEvents": [
                            {
                                "eventId": int(item["sourceEventIds"][0]),
                                "sourceEventIds": list(item["sourceEventIds"]),
                                "sourceRef": item["sourceRef"],
                                "sourceIds": _input_source_ids(item),
                                "createdAtMs": item["createdAtMs"],
                                "sourceOccurredAtMs": item["sourceOccurredAtMs"],
                                "text": item["text"],
                                "decisionContext": item.get("decisionContext"),
                                "sourceKind": item["sourceKind"],
                                "recentContext": item.get("recentContext", ""),
                                "source": item["source"],
                                "project": item["project"],
                                "app": item["app"],
                                "contextGroupId": item["contextGroupId"],
                            }
                            for item in model_inputs
                        ],
                        "legalSourceEventIds": model_event_ids,
                        "legalEvidenceIds": [
                            evidence_id
                            for item in model_inputs
                            for evidence_id in _input_evidence_ids(item)
                        ],
                        "cursor": {
                            **dict(bundle.get("cursor") or {}),
                            "toSourceCreatedAtMs": boundary[0],
                            "toSourceId": boundary[1],
                            "pendingEventCount": len(model_inputs),
                            "batchSourceCount": len(model_inputs),
                        },
                    }
                model_run_id = _owner_model_run_id(
                    owner[0],
                    owner[1],
                    self.project,
                    model_bundle_payload,
                )
                model_bundle = _with_owner_bundle_hash(
                    {
                        **model_bundle_payload,
                        # This identity belongs to the frozen model request,
                        # not to a single application attempt. A retry after
                        # backoff must reopen the database-owned request rather
                        # than create a fresh Pi Session for the same packet.
                        "curationRunId": model_run_id,
                    }
                )
                begin_model_run = getattr(self.organizer, "begin_run", None)
                if callable(begin_model_run):
                    begin_result = begin_model_run(
                        model_run_id,
                        frozen_input_sha256=hashlib.sha256(
                            json.dumps(
                                model_bundle,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest(),
                    )
                    attempt_id = (
                        str(begin_result.get("runId") or run_id)
                        if isinstance(begin_result, Mapping)
                        else run_id
                    )
                    model_run_started = True
                self._emit_memory_event(
                    phase="started",
                    status="completed",
                    summary="所有者记忆维护已开始",
                    run_id=run_id,
                    attempt_id=attempt_id,
                )
                compile_output = self.organizer.curate_owner_memory(
                    bundle=model_bundle,
                    project=self.project,
                    owner_kind=owner[0],
                    owner_id=owner[1],
                    instruction=instruction,
                )
                if self.personal_v2:
                    compile_output = _with_personal_v2_run_identity(
                        compile_output,
                        run_id=run_id,
                        model_run_id=model_run_id,
                    )
                if not self.personal_v2:
                    compile_output = _reconcile_owner_compile_claim_keys(
                        compile_output,
                        model_inputs=model_inputs,
                        existing_memory_atoms=[
                            dict(item)
                            for item in model_bundle.get("existingMemoryAtoms") or []
                            if isinstance(item, dict)
                        ],
                    )
                claim_key_reconciliations = [
                    dict(item)
                    for item in compile_output.get("claimKeyReconciliations") or []
                    if isinstance(item, dict)
                ]
                memory_atom_repairs = [
                    dict(item)
                    for item in compile_output.get("memoryAtomRepairs") or []
                    if isinstance(item, dict)
                ]
                model_decisions = (
                    self._apply_personal_v2_evidence_decisions(
                        compile_output,
                        model_inputs=model_inputs,
                        run_id=run_id,
                        current_ms=current_ms,
                        apply=False,
                    )
                    if self.personal_v2
                    else self._apply_model_decisions(
                        compile_output,
                        model_inputs=model_inputs,
                        existing_memory_atoms=[
                            dict(item)
                            for item in model_bundle.get("existingMemoryAtoms") or []
                            if isinstance(item, dict)
                        ],
                        run_id=run_id,
                        current_ms=current_ms,
                    )
                )
                needs_review_source_ids = [
                    source_id
                    for decision in model_decisions
                    if decision.get("disposition") == "needs_review"
                    for source_id in decision.get("sourceIds") or []
                    if compact_whitespace(str(source_id or ""))
                ]
                if (
                    needs_review_source_ids
                    and not self.personal_v2
                    and not self.auto_apply
                ):
                    with self._connect() as conn:
                        boundary = _boundary_before_sources(
                            conn,
                            owner_kind=owner[0],
                            owner_id=owner[1],
                            project=self.project,
                            source_ids=needs_review_source_ids,
                            fallback=(
                                int(
                                    dict(bundle.get("cursor") or {}).get(
                                        "fromSourceCreatedAtMs"
                                    )
                                    or 0
                                ),
                                str(
                                    dict(bundle.get("cursor") or {}).get(
                                        "fromSourceId"
                                    )
                                    or ""
                                ),
                            ),
                        )
                governed = _govern_owner_compile_output(
                    {
                        **compile_output,
                        # Durable writes follow decisions validated against the
                        # frozen canonical Evidence refs, not unchecked model
                        # output.
                        "sourceDecisions": model_decisions,
                    },
                    bundle=model_bundle,
                    owner_kind=owner[0],
                    owner_id=owner[1],
                )
                if _has_durable_memory(governed):
                    plan = memory_book_plan_from_compile_output(
                        governed,
                        project=self.project,
                        provider=str(
                            compile_output.get("provider")
                            or getattr(self.organizer, "provider_name", "")
                        ),
                        model=str(compile_output.get("model") or ""),
                        source_bundle=model_bundle,
                        owner_kind=owner[0],
                        owner_id=owner[1],
                        run_kind="manual_curation" if manual else "daily_curation",
                    )
                    plan["runId"] = run_id
                    metadata = dict(plan.get("metadata") or {})
                    metadata.update(
                        {
                            "ownerKind": owner[0],
                            "ownerId": owner[1],
                            "runKind": (
                                "manual_curation" if manual else "daily_curation"
                            ),
                            "sourceIds": _all_input_source_ids(model_inputs),
                            "sourceDecisionCount": len(model_decisions),
                            "sourceDecisions": model_decisions,
                            "deterministicDispositionCount": len(deterministic),
                            **purpose_audit_fields(
                                dict(model_bundle.get("purposeProfile") or {})
                            ),
                        }
                    )
                    plan["metadata"] = metadata
                    validation = inspect_memory_book_plan(plan)
                    if not validation.get("ok"):
                        raise ValueError(
                            "owner memory curation produced an invalid review plan: "
                            + json.dumps(
                                validation.get("errors") or [],
                                ensure_ascii=False,
                                sort_keys=True,
                            )[:1200]
                        )
                if self.personal_v2 and model_run_started:
                    finish_model_run = getattr(self.organizer, "finish_run", None)
                    if callable(finish_model_run):
                        finish_model_run()
                    model_run_started = False
                if self.personal_v2:
                    model_decisions = self._apply_personal_v2_evidence_decisions(
                        compile_output,
                        model_inputs=model_inputs,
                        run_id=run_id,
                        current_ms=current_ms,
                        apply=True,
                    )
                    personal_evidence_applied = any(
                        bool(decision.get("changed"))
                        for decision in model_decisions
                    )
                    if plan is not None:
                        metadata = dict(plan.get("metadata") or {})
                        metadata["sourceDecisions"] = model_decisions
                        plan["metadata"] = metadata

                if plan is not None:
                    with self._connect() as conn:
                        stored = store_memory_book_plan(
                            conn,
                            plan,
                            supersede_project_drafts=True,
                        )
                    stored_plan = True
                    stored_run_id = str(stored.get("runId") or run_id)
                    if self.auto_apply:
                        with self._connect() as conn:
                            applied = apply_stored_memory_book_run(
                                conn,
                                run_id=stored_run_id,
                            )
                        run_status = str(applied.get("status") or "applied")
                    else:
                        run_status = "waiting_review"
                else:
                    with self._connect() as conn:
                        _store_empty_owner_run(
                            conn,
                            run_id=run_id,
                            owner_kind=owner[0],
                            owner_id=owner[1],
                            project=self.project,
                            provider=str(
                                compile_output.get("provider")
                                or getattr(self.organizer, "provider_name", "")
                            ),
                            model=str(compile_output.get("model") or ""),
                            source_ids=_all_input_source_ids(model_inputs),
                            source_decisions=model_decisions,
                            created_at_ms=current_ms,
                            run_kind="manual_curation" if manual else "daily_curation",
                            curation_metadata={
                                "personalCurationV2": dict(
                                    compile_output.get("personalCurationV2") or {}
                                ),
                                "modelDiagnostics": dict(
                                    compile_output.get("modelDiagnostics") or {}
                                ),
                                "modelBundleStats": dict(
                                    compile_output.get("modelBundleStats") or {}
                                ),
                            },
                        )
                    run_status = "idle"
                    stored_run_id = run_id
            else:
                self._emit_memory_event(
                    phase="started",
                    status="completed",
                    summary="所有者记忆维护已开始",
                    run_id=run_id,
                    attempt_id=attempt_id,
                )
                with self._connect() as conn:
                    _store_empty_owner_run(
                        conn,
                        run_id=run_id,
                        owner_kind=owner[0],
                        owner_id=owner[1],
                        project=self.project,
                        provider="rules",
                        model="",
                        source_ids=_all_input_source_ids(inputs),
                        source_decisions=deterministic,
                        created_at_ms=current_ms,
                        run_kind="manual_curation" if manual else "daily_curation",
                    )
                run_status = "idle"
                stored_run_id = run_id

            if model_run_started:
                finish_model_run = getattr(self.organizer, "finish_run", None)
                if callable(finish_model_run):
                    finish_model_run()
                model_run_started = False
            # A processed time slice is not a completed backlog. Keep bounded
            # automatic batches runnable until the queue drains; waiting for the
            # normal daily cadence here makes historical work starve new input.
            with self._connect() as conn:
                remaining_sources = _pending_source_count(
                    conn, owner_kind=owner[0], owner_id=owner[1], project=self.project,
                    cursor_ms=boundary[0], cursor_id=boundary[1], canonical_personal=self.personal_v2,
                )
            catch_up_pending = (
                self.personal_v2 and self.auto_apply and not manual
                and run_status in {"applied", "empty", "idle"}
                and remaining_sources > 0
                and (
                    boundary > cursor_boundary
                    or any(
                        decision.get("changed")
                        and decision.get("disposition") in {"remember", "not_for_memory"}
                        for decision in [*deterministic, *model_decisions]
                    )
                )
            )
            self._finish_scope(
                owner_kind=owner[0],
                owner_id=owner[1],
                run_id=stored_run_id,
                boundary=boundary,
                status="idle" if run_status in {"applied", "empty"} else run_status,
                next_due_at_ms=current_ms if catch_up_pending else current_ms + self.daily_interval_ms,
                current_ms=current_ms,
            )
            self._emit_memory_event(
                phase=(
                    "applied"
                    if run_status == "applied"
                    else "draft_ready"
                    if run_status == "waiting_review"
                    else "draft_finished"
                ),
                status=(
                    "completed"
                    if run_status in {"applied", "idle", "empty"}
                    else "waiting"
                ),
                summary=(
                    "所有者记忆维护已应用"
                    if run_status == "applied"
                    else "所有者记忆草案已生成，等待审阅"
                    if run_status == "waiting_review"
                    else "所有者记忆维护已完成"
                ),
                run_id=stored_run_id,
                attempt_id=attempt_id,
            )
            return {
                "ok": True,
                "ownerKind": owner[0],
                "ownerId": owner[1],
                "skipped": False,
                "runId": stored_run_id,
                "runStatus": run_status,
                "sourceCount": len(_all_input_source_ids(inputs)),
                "logicalInputCount": len(inputs),
                "modelSourceCount": len(_all_input_source_ids(model_inputs)),
                "modelFactProbeCount": len(
                    _existing_memory_recall_probes(model_inputs)
                ),
                "deferredModelInputCount": deferred_model_input_count,
                "claimKeyReconciliationCount": len(claim_key_reconciliations),
                "claimKeyReconciliations": claim_key_reconciliations,
                "memoryAtomRepairCount": len(memory_atom_repairs),
                "memoryAtomRepairs": memory_atom_repairs,
                "deterministicDecisions": deterministic,
                "modelDecisions": model_decisions,
                "reviewRequired": run_status == "waiting_review",
                "autoApplied": self.auto_apply and run_status == "applied",
                "catchUpPending": catch_up_pending,
                "remainingSourceCount": remaining_sources,
                "diffCount": len(plan.get("diffs") or []) if plan is not None else 0,
            }
        except Exception as exc:
            self._emit_memory_event(
                phase="failed",
                status="failed",
                summary="所有者记忆维护失败",
                run_id=run_id,
                attempt_id=attempt_id,
                metrics={"errorType": exc.__class__.__name__},
            )
            if model_run_started:
                fail_model_run = getattr(self.organizer, "fail_run", None)
                if callable(fail_model_run):
                    try:
                        fail_model_run(exc)
                    except Exception:
                        # The curation cursor still fails closed below. A
                        # resumable model-run receipt must not be overwritten
                        # by cleanup failure in this adapter boundary.
                        pass
            self._fail_scope(
                owner_kind=owner[0],
                owner_id=owner[1],
                run_id=run_id,
                error=exc,
                current_ms=current_ms,
            )
            if personal_evidence_applied and not stored_plan:
                try:
                    with self._connect() as conn:
                        rollback_evidence_admissions_for_run(
                            conn,
                            run_id,
                            created_at_ms=current_ms,
                        )
                except Exception:
                    # Preserve the primary failure. The admission run remains
                    # fully auditable and a later recovery can retry the same
                    # guarded rollback by run id.
                    pass
            return {
                "ok": False,
                "ownerKind": owner[0],
                "ownerId": owner[1],
                "skipped": False,
                "runId": run_id,
                "error": _public_error(exc),
            }

    def _emit_memory_event(
        self,
        *,
        phase: str,
        status: str,
        summary: str,
        run_id: str,
        attempt_id: str = "",
        metrics: Mapping[str, object] | None = None,
    ) -> None:
        emitter = getattr(self.observations, "emit_memory_event", None)
        if callable(emitter):
            values: dict[str, object] = {
                "phase": phase,
                "status": status,
                "summary": summary,
                "run_id": run_id,
                "attempt_id": attempt_id,
                "metrics": metrics,
            }
            if self.trace_id:
                values.update(
                    {
                        "trace_id": self.trace_id,
                        "maintenance_job_id": self.maintenance_job_id,
                        # The Gateway's started span is the parent only for
                        # the first owner phase. Later phases naturally hang
                        # from the owner attempt's started span.
                        "parent_span_id": (
                            self.parent_span_id if phase == "started" else ""
                        ),
                    }
                )
            emitter(**values)

    def _apply_model_decisions(
        self,
        compile_output: Mapping[str, object],
        *,
        model_inputs: list[dict[str, object]],
        existing_memory_atoms: list[dict[str, object]],
        run_id: str,
        current_ms: int,
    ) -> list[dict[str, object]]:
        sources_by_ref = {
            str(item["sourceRef"]): _input_source_ids(item)
            for item in model_inputs
        }
        decisions_by_ref: dict[str, dict[str, object]] = {}
        decisions = compile_output.get("sourceDecisions")
        for decision in decisions if isinstance(decisions, list) else []:
            if not isinstance(decision, dict):
                continue
            source_ref = compact_whitespace(str(decision.get("sourceRef") or ""))
            if source_ref not in sources_by_ref or source_ref in decisions_by_ref:
                continue
            decisions_by_ref[source_ref] = decision

        (
            durable_atom_event_ids,
            over_capacity_atom_event_ids,
        ) = _owner_atom_event_ids_by_capacity(
            compile_output,
            model_inputs=model_inputs,
            existing_claim_keys={
                compact_whitespace(str(item.get("claimKey") or ""))
                for item in existing_memory_atoms
                if compact_whitespace(str(item.get("claimKey") or ""))
            },
        )
        legal_model_event_ids = {
            event_id
            for model_input in model_inputs
            for event_id in _positive_event_ids(
                model_input.get("sourceEventIds")
            )
        }

        results: list[dict[str, object]] = []
        for item in model_inputs:
            source_ref = compact_whitespace(str(item.get("sourceRef") or ""))
            source_ids = sources_by_ref.get(source_ref, [])
            if not source_ids:
                continue
            decision = decisions_by_ref.get(source_ref)
            agent_curated_external = _is_agent_curated_external_source(item)
            source_event_ids = _positive_event_ids(item.get("sourceEventIds"))
            supports_durable_atom = bool(
                durable_atom_event_ids.intersection(source_event_ids)
            )
            actor_kind = "model"
            if over_capacity_atom_event_ids.intersection(source_event_ids):
                # Never consolidate a dense logical source after storing only
                # a prefix of its proposed Atoms. Keep the whole source in the
                # review lane so an operator can split or re-curate it.
                disposition = "needs_review"
                confidence = 1.0
                effective = "needs_review"
                reason = "atom_batch_capacity_exceeded"
                actor_kind = "system"
            elif decision is None:
                if agent_curated_external:
                    disposition = "remember"
                    confidence = 0.9
                    effective = "remember"
                    reason = "agent_curated_external_memory"
                    actor_kind = "system"
                elif supports_durable_atom:
                    # The organizer may omit the parallel sourceDecisions row
                    # while still emitting a valid Atom backed by this source.
                    # Atom-first evidence is stronger than the missing routing
                    # field, so do not strand the source in needs_review.
                    disposition = "remember"
                    confidence = 0.9
                    effective = "remember"
                    reason = "durable_atom_evidence"
                    actor_kind = "system"
                elif self.auto_apply:
                    # Automatic curation must close every reviewed source. An
                    # omitted source that produced no durable Atom is retained
                    # as immutable evidence but excluded from long-term recall.
                    disposition = "not_for_memory"
                    confidence = 0.9
                    effective = "not_for_memory"
                    reason = "model_omitted_no_durable_atom"
                    actor_kind = "system"
                else:
                    disposition = "needs_review"
                    confidence = 0.0
                    effective = "needs_review"
                    reason = "model_decision_missing"
            else:
                disposition = compact_whitespace(
                    str(decision.get("disposition") or "")
                )
                decision_reason = compact_whitespace(
                    str(decision.get("reasonCode") or "")
                )[:120]
                confidence = _bounded_float(
                    decision.get("confidence"),
                    default=0.0,
                )
                if (
                    agent_curated_external
                    and disposition == "needs_review"
                    and decision_reason == "model_omitted_source"
                ):
                    disposition = "remember"
                    confidence = 0.9
                    effective = "remember"
                    reason = "agent_curated_external_memory"
                    actor_kind = "system"
                elif agent_curated_external and disposition == "remember":
                    confidence = max(confidence, 0.9)
                    effective = "remember"
                    reason = decision_reason or "agent_curated_external_memory"
                elif disposition == "not_for_memory" and confidence < 0.9:
                    effective = "needs_review"
                    reason = "low_confidence_not_for_memory"
                elif disposition == "remember" and confidence < 0.55:
                    effective = "needs_review"
                    reason = "low_confidence_remember"
                elif (
                    disposition == "remember"
                    and not agent_curated_external
                    and not durable_atom_event_ids.intersection(
                        source_event_ids
                    )
                ):
                    # Atom-first is a storage invariant, not merely a prompt
                    # preference. A source cannot become remembered evidence
                    # when the organizer emitted only a transcript, question,
                    # Book summary, or other non-Atom artifact for it.
                    effective = "needs_review"
                    reason = "remember_without_durable_atom"
                elif disposition in {
                    "remember",
                    "not_for_memory",
                    "needs_review",
                }:
                    effective = disposition
                    reason = decision_reason
                else:
                    effective = "needs_review"
                    reason = "invalid_model_disposition"
            transitions = [
                self.sources.set_disposition(
                    source_id,
                    disposition=effective,
                    reason_code=reason or "model_curation",
                    actor_kind=actor_kind,
                    run_id=run_id,
                    metadata={
                        "sourceRef": source_ref,
                        "modelDisposition": disposition,
                        "confidence": confidence,
                    },
                    created_at_ms=current_ms,
                )
                for source_id in source_ids
            ]
            results.append(
                {
                    "sourceRef": source_ref,
                    "sourceId": source_ids[-1],
                    "sourceIds": source_ids,
                    "disposition": effective,
                    "reasonCode": reason or "model_curation",
                    "confidence": confidence,
                    "changed": any(
                        bool(transition["changed"])
                        for transition in transitions
                    ),
                }
            )
        return results

    def _apply_personal_v2_evidence_decisions(
        self,
        compile_output: Mapping[str, object],
        *,
        model_inputs: list[dict[str, object]],
        run_id: str,
        current_ms: int,
        apply: bool = True,
    ) -> list[dict[str, object]]:
        inputs_by_ref = {
            compact_whitespace(str(item.get("sourceRef") or "")): item
            for item in model_inputs
            if compact_whitespace(str(item.get("sourceRef") or ""))
        }
        raw_decisions = compile_output.get("sourceDecisions")
        decisions = [
            dict(item)
            for item in raw_decisions if isinstance(item, Mapping)
        ] if isinstance(raw_decisions, list) else []
        decisions_by_ref: dict[str, dict[str, object]] = {}
        for decision in decisions:
            source_ref = compact_whitespace(str(decision.get("sourceRef") or ""))
            if source_ref not in inputs_by_ref or source_ref in decisions_by_ref:
                raise ValueError(
                    "personal-v2 Evidence decisions contain an unknown or duplicate source ref"
                )
            decisions_by_ref[source_ref] = decision
        if set(decisions_by_ref) != set(inputs_by_ref):
            raise ValueError(
                "personal-v2 Evidence decisions do not cover the frozen batch"
            )

        state_to_disposition = {
            "admitted": "remember",
            "rejected": "not_for_memory",
            "needs_review": "needs_review",
        }
        prepared: list[dict[str, object]] = []
        for source_ref, item in inputs_by_ref.items():
            evidence_ids = _input_evidence_ids(item)
            if not evidence_ids:
                raise ValueError(
                    "personal-v2 source does not resolve to canonical Evidence"
                )
            decision = decisions_by_ref[source_ref]
            decision_evidence_ids = _input_evidence_ids(decision)
            if decision_evidence_ids != evidence_ids:
                raise ValueError(
                    "personal-v2 Evidence decision changed canonical Evidence ids"
                )
            state = compact_whitespace(
                str(decision.get("evidenceAdmissionState") or "")
            ).lower()
            disposition = compact_whitespace(
                str(decision.get("disposition") or "")
            ).lower()
            if state not in state_to_disposition or state_to_disposition[state] != disposition:
                raise ValueError(
                    "personal-v2 Evidence decision has an inconsistent state"
                )
            reason = compact_whitespace(
                str(decision.get("reasonCode") or "luna_personal_memory_review")
            )[:160]
            source_ids = _input_source_ids(item)
            prepared.append(
                {
                    "sourceRef": source_ref,
                    "sourceId": source_ids[-1] if source_ids else "",
                    "sourceIds": source_ids,
                    "evidenceId": evidence_ids[0] if len(evidence_ids) == 1 else "",
                    "evidenceIds": evidence_ids,
                    "disposition": disposition,
                    "reasonCode": reason,
                    "confidence": _bounded_float(
                        decision.get("confidence"),
                        default=0.0,
                    ),
                    "evidenceAdmissionState": state,
                    "changed": False,
                }
            )
        if not apply:
            return prepared
        results: list[dict[str, object]] = []
        with self._connect() as conn:
            for item in prepared:
                transitions = [
                    transition_evidence_admission(
                        conn,
                        str(evidence_id),
                        new_state=str(item["evidenceAdmissionState"]),
                        reason_code=str(item["reasonCode"]),
                        actor_kind="luna",
                        created_at_ms=current_ms,
                        run_id=run_id,
                        metadata={
                            "protocol": "personal-v2",
                            "sourceRef": str(item["sourceRef"]),
                            "confidence": float(item["confidence"]),
                        },
                    )
                    for evidence_id in item["evidenceIds"]
                ]
                results.append(
                    {
                        **item,
                        "changed": any(
                            bool(transition.get("changed"))
                            for transition in transitions
                        ),
                    }
                )
        return results

    def _claim_scope(
        self,
        *,
        owner_kind: str,
        owner_id: str,
        manual: bool,
        current_ms: int,
    ) -> dict[str, object]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT *
                FROM memory_curation_cursors
                WHERE owner_kind = ? AND owner_id = ? AND project = ? AND lane = 'daily'
                """,
                (owner_kind, owner_id, self.project),
            ).fetchone()
            run_status = _run_status(
                conn,
                str(row["last_run_id"] or "") if row is not None else "",
            )
            if (
                not self.auto_apply
                and row is not None
                and str(row["status"]) == "waiting_review"
                and run_status == "draft"
            ):
                conn.commit()
                return {"claimed": False, "reason": "draft_pending_review"}
            if (
                row is not None
                and str(row["status"]) == "running"
                and current_ms - int(row["updated_at_ms"] or 0) < self.running_lease_ms
                # A manual Gateway retry is the recovery boundary after a
                # failed/expired maintenance job.  The Gateway admits only
                # one active job at a time, so a cursor that already carries
                # failures cannot belong to a live competing run here.
                and not (manual and int(row["consecutive_failures"] or 0) > 0)
            ):
                conn.commit()
                return {"claimed": False, "reason": "already_running"}
            if (
                not manual
                and row is not None
                and int(row["next_due_at_ms"] or 0) > current_ms
            ):
                conn.commit()
                return {"claimed": False, "reason": "not_due"}
            conn.execute(
                """
                INSERT INTO memory_curation_cursors(
                    owner_kind, owner_id, project, lane, status, updated_at_ms
                ) VALUES (?, ?, ?, 'daily', 'running', ?)
                ON CONFLICT(owner_kind, owner_id, project, lane) DO UPDATE SET
                    status = 'running',
                    last_error = '',
                    updated_at_ms = excluded.updated_at_ms
                """,
                (owner_kind, owner_id, self.project, current_ms),
            )
            conn.commit()
        resume_run_id = ""
        if row is not None and str(row["status"] or "") == "backoff":
            resume_run_id = str(row["last_run_id"] or "")
        return {
            "claimed": True,
            "reason": "manual" if manual else "due",
            "runId": resume_run_id,
        }

    def _finish_scope(
        self,
        *,
        owner_kind: str,
        owner_id: str,
        run_id: str,
        boundary: tuple[int, str] | None,
        status: str,
        next_due_at_ms: int,
        current_ms: int,
    ) -> None:
        boundary_ms, boundary_id = boundary or (0, "")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_curation_cursors(
                    owner_kind, owner_id, project, lane,
                    last_source_created_at_ms, last_source_id, last_run_ms,
                    last_run_id, next_due_at_ms, status, consecutive_failures,
                    last_error, updated_at_ms
                ) VALUES (?, ?, ?, 'daily', ?, ?, ?, ?, ?, ?, 0, '', ?)
                ON CONFLICT(owner_kind, owner_id, project, lane) DO UPDATE SET
                    last_source_created_at_ms = CASE
                        WHEN excluded.last_source_created_at_ms > 0
                        THEN excluded.last_source_created_at_ms
                        ELSE memory_curation_cursors.last_source_created_at_ms
                    END,
                    last_source_id = CASE
                        WHEN excluded.last_source_created_at_ms > 0
                        THEN excluded.last_source_id
                        ELSE memory_curation_cursors.last_source_id
                    END,
                    last_run_ms = excluded.last_run_ms,
                    last_run_id = excluded.last_run_id,
                    next_due_at_ms = excluded.next_due_at_ms,
                    status = excluded.status,
                    consecutive_failures = 0,
                    last_error = '',
                    updated_at_ms = excluded.updated_at_ms
                """,
                (
                    owner_kind,
                    owner_id,
                    self.project,
                    boundary_ms,
                    boundary_id,
                    current_ms,
                    run_id,
                    next_due_at_ms,
                    status,
                    current_ms,
                ),
            )

    def _fail_scope(
        self,
        *,
        owner_kind: str,
        owner_id: str,
        run_id: str,
        error: BaseException,
        current_ms: int,
    ) -> None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT consecutive_failures
                FROM memory_curation_cursors
                WHERE owner_kind = ? AND owner_id = ? AND project = ? AND lane = 'daily'
                """,
                (owner_kind, owner_id, self.project),
            ).fetchone()
            failures = int(row["consecutive_failures"] or 0) + 1 if row is not None else 1
            backoff_ms = min(
                self.daily_interval_ms,
                15 * 60 * 1000 * (2 ** min(failures - 1, 6)),
            )
            conn.execute(
                """
                INSERT INTO memory_curation_cursors(
                    owner_kind, owner_id, project, lane, last_run_id, next_due_at_ms,
                    status, consecutive_failures, last_error, updated_at_ms
                ) VALUES (?, ?, ?, 'daily', ?, ?, 'backoff', ?, ?, ?)
                ON CONFLICT(owner_kind, owner_id, project, lane) DO UPDATE SET
                    last_run_id = excluded.last_run_id,
                    next_due_at_ms = excluded.next_due_at_ms,
                    status = 'backoff',
                    consecutive_failures = excluded.consecutive_failures,
                    last_error = excluded.last_error,
                    updated_at_ms = excluded.updated_at_ms
                """,
                (
                    owner_kind,
                    owner_id,
                    self.project,
                    run_id,
                    current_ms + backoff_ms,
                    failures,
                    _public_error(error),
                    current_ms,
                ),
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # A completed model turn can coincide with a bounded diagnostic read or
        # projection query.  Let that short reader finish instead of discarding
        # the governed curation run at the audit/write boundary.
        conn = sqlite3.connect(self.db_path, timeout=30.0)
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


def owner_memory_curation_status(
    conn: sqlite3.Connection,
    *,
    project: str,
    current_ms: int | None = None,
    initial_settle_ms: int = DEFAULT_INITIAL_SETTLE_MS,
    daily_interval_ms: int = DEFAULT_DAILY_INTERVAL_MS,
    running_lease_ms: int = DEFAULT_RUNNING_LEASE_MS,
    owner_kind: str = "",
    owner_id: str = "",
    auto_apply: bool = False,
    include_agent_dialogue: bool = True,
    canonical_personal: bool = False,
) -> dict[str, object]:
    timestamp = int(time.time() * 1000) if current_ms is None else max(0, int(current_ms))
    scopes = _owner_scope_statuses(
        conn,
        project=compact_whitespace(project),
        current_ms=timestamp,
        initial_settle_ms=max(0, int(initial_settle_ms)),
        running_lease_ms=max(60_000, int(running_lease_ms)),
        owner_kind=owner_kind,
        owner_id=owner_id,
        auto_apply=auto_apply,
        canonical_personal=canonical_personal,
    )
    interval_ms = max(60_000, int(daily_interval_ms))
    backlog = _owner_curation_backlog_projection(
        conn,
        project=compact_whitespace(project),
        scopes=scopes,
        current_ms=timestamp,
        canonical_personal=canonical_personal,
    )
    return {
        "schemaVersion": OWNER_CURATION_STATUS_SCHEMA_VERSION,
        "ok": True,
        "project": compact_whitespace(project),
        "policy": {
            "cadence": (
                "twice_daily"
                if interval_ms == 12 * 60 * 60 * 1000
                else "daily"
                if interval_ms == 24 * 60 * 60 * 1000
                else "interval"
            ),
            "dailyIntervalMs": interval_ms,
            "sourceKinds": (
                ["user_final", "explicit_memory", "tool_receipt"]
                if canonical_personal
                else ["user_final", "explicit_memory", "session_digest"]
            ),
            "evidenceOrigins": (
                sorted(PERSONAL_EVIDENCE_ORIGINS) if canonical_personal else []
            ),
            "historicalLegacyRequiresPromotionReceipt": bool(canonical_personal),
            "canonicalPersonalEvidenceOnly": bool(canonical_personal),
            "assistantTurnsRead": bool(include_agent_dialogue),
            "assistantTurnsAreContextOnly": bool(include_agent_dialogue),
            "toolReceiptsEligible": bool(canonical_personal),
            "sessionArtifactsEligible": False,
            "rawScreenshotsRead": False,
            "semanticWritesRequireReview": not auto_apply,
            "autoApplyGovernedWrites": bool(auto_apply),
            "sourceForgettingReversible": True,
        },
        "due": any(bool(item["due"]) for item in scopes),
        "pendingSourceCount": sum(int(item["pendingSourceCount"]) for item in scopes),
        "needsReviewSourceCount": sum(
            int(item["needsReviewSourceCount"]) for item in scopes
        ),
        "backlog": backlog,
        "scopes": scopes,
    }


def _owner_curation_backlog_projection(
    conn: sqlite3.Connection,
    *,
    project: str,
    scopes: list[dict[str, object]],
    current_ms: int,
    canonical_personal: bool,
) -> dict[str, object]:
    """Return bounded, text-free backlog facets for the Control Center."""

    rows: list[sqlite3.Row] = []
    for scope in scopes:
        owner_kind = str(scope.get("ownerKind") or "")
        owner_id = str(scope.get("ownerId") or "")
        cursor = dict(scope.get("lastSourceCursor") or {})
        cursor_ms = int(cursor.get("createdAtMs") or 0)
        cursor_id = str(cursor.get("sourceId") or "")
        cursor_clause = (
            ""
            if canonical_personal
            else """
              AND (
                  s.created_at_ms > ?
                  OR (s.created_at_ms = ? AND s.source_id > ?)
              )
            """
        )
        params: list[object] = [
            owner_kind,
            owner_id,
            project,
            project,
            *_ELIGIBLE_DISPOSITIONS,
        ]
        if not canonical_personal:
            params.extend((cursor_ms, cursor_ms, cursor_id))
        evidence_join = (
            f"""
              JOIN memory_evidence_input_event_links AS source_link
                ON source_link.input_event_id = s.input_event_id
               AND source_link.relation = 'source'
              JOIN agent_memory_evidence AS evidence
                ON evidence.evidence_id = source_link.evidence_id
               AND {curatable_personal_evidence_sql('evidence')}
            """
            if canonical_personal
            else ""
        )
        evidence_state = (
            "MIN(evidence.admission_state)"
            if canonical_personal
            else "CASE WHEN s.disposition = 'needs_review' THEN 'needs_review' ELSE 'candidate' END"
        )
        having = (
            "HAVING COUNT(DISTINCT evidence.evidence_id) = 1"
            if canonical_personal
            else ""
        )
        rows.extend(
            conn.execute(
                f"""
                SELECT s.source_id, e.created_at_ms, e.app,
                       e.source AS event_source,
                       {evidence_state} AS admission_state
                FROM agent_memory_sources AS s
                JOIN input_events AS e ON e.id = s.input_event_id
                {evidence_join}
                WHERE s.owner_kind = ? AND s.owner_id = ?
                  AND s.status = 'active'
                  AND (? = '' OR e.project = ? OR e.project = '')
                  AND s.disposition IN ({','.join('?' for _ in _ELIGIBLE_DISPOSITIONS)})
                  AND e.source != 'personal_profile_editor'
                  {cursor_clause}
                GROUP BY s.source_id, e.created_at_ms, e.app, e.source, s.disposition
                {having}
                ORDER BY e.created_at_ms ASC, s.source_id ASC
                LIMIT 50000
                """,  # noqa: S608 - predicates and placeholders are fixed above.
                tuple(params),
            ).fetchall()
        )

    day_facets: dict[str, dict[str, object]] = {}
    application_facets: dict[str, dict[str, object]] = {}
    channel_facets: dict[str, dict[str, object]] = {}
    for row in rows:
        occurred_at_ms = int(row["created_at_ms"] or 0)
        timeline_date = local_date_for_timestamp(occurred_at_ms)
        application = compact_whitespace(str(row["app"] or "")) or "未知应用"
        channel = compact_whitespace(str(row["event_source"] or "")) or "未知来源"
        needs_review = str(row["admission_state"] or "") == "needs_review"
        day = day_facets.setdefault(
            timeline_date,
            {
                "date": timeline_date,
                "pendingSourceCount": 0,
                "needsReviewSourceCount": 0,
                "applications": {},
                "channels": {},
            },
        )
        day["pendingSourceCount"] = int(day["pendingSourceCount"]) + 1
        day["needsReviewSourceCount"] = int(day["needsReviewSourceCount"]) + int(needs_review)
        day_apps = day["applications"]
        day_channels = day["channels"]
        assert isinstance(day_apps, dict) and isinstance(day_channels, dict)
        day_apps[application] = int(day_apps.get(application, 0)) + 1
        day_channels[channel] = int(day_channels.get(channel, 0)) + 1
        for facets, name in ((application_facets, application), (channel_facets, channel)):
            facet = facets.setdefault(name, {"name": name, "count": 0, "lastSourceAtMs": 0})
            facet["count"] = int(facet["count"]) + 1
            facet["lastSourceAtMs"] = max(int(facet["lastSourceAtMs"]), occurred_at_ms)

    def ranked(values: dict[str, dict[str, object]], limit: int) -> list[dict[str, object]]:
        return sorted(
            values.values(),
            key=lambda item: (-int(item["count"]), str(item["name"])),
        )[:limit]

    days: list[dict[str, object]] = []
    for timeline_date in sorted(day_facets):
        item = day_facets[timeline_date]
        day_apps = item.pop("applications")
        day_channels = item.pop("channels")
        assert isinstance(day_apps, dict) and isinstance(day_channels, dict)
        days.append(
            {
                **item,
                "applications": [
                    {"name": name, "count": count}
                    for name, count in sorted(
                        day_apps.items(), key=lambda pair: (-int(pair[1]), str(pair[0]))
                    )[:6]
                ],
                "channels": [
                    {"name": name, "count": count}
                    for name, count in sorted(
                        day_channels.items(), key=lambda pair: (-int(pair[1]), str(pair[0]))
                    )[:6]
                ],
            }
        )

    first_ms = int(rows[0]["created_at_ms"] or 0) if rows else 0
    last_ms = int(rows[-1]["created_at_ms"] or 0) if rows else 0
    cursors = [
        int(dict(scope.get("lastSourceCursor") or {}).get("createdAtMs") or 0)
        for scope in scopes
    ]
    covered_through_ms = max(cursors, default=0)
    return {
        "schemaVersion": "rag-ime.owner-memory-curation-backlog.v1",
        "pendingSourceCount": len(rows),
        "pendingDayCount": len(days),
        "oldestPendingAtMs": first_ms,
        "newestPendingAtMs": last_ms,
        "coveredThroughAtMs": covered_through_ms,
        "coveredThroughDate": (
            local_date_for_timestamp(covered_through_ms) if covered_through_ms else ""
        ),
        "targetDate": local_date_for_timestamp(current_ms),
        "caughtUpThroughToday": not rows,
        "applications": ranked(application_facets, 12),
        "channels": ranked(channel_facets, 12),
        "days": days[-62:],
        "truncated": len(rows) >= 50_000 or len(days) > 62,
    }


def _owner_scope_statuses(
    conn: sqlite3.Connection,
    *,
    project: str,
    current_ms: int,
    initial_settle_ms: int,
    running_lease_ms: int,
    owner_kind: str = "",
    owner_id: str = "",
    auto_apply: bool = False,
    canonical_personal: bool = False,
) -> list[dict[str, object]]:
    clauses = [
        "s.status = 'active'",
        "(? = '' OR e.project = ? OR e.project = '')",
    ]
    values: list[object] = [project, project]
    normalized_kind = compact_whitespace(owner_kind)
    normalized_id = compact_whitespace(owner_id)
    if normalized_kind:
        if normalized_kind not in _OWNER_KINDS:
            raise ValueError("unsupported memory owner kind")
        clauses.append("s.owner_kind = ?")
        values.append(normalized_kind)
    if normalized_id:
        clauses.append("s.owner_id = ?")
        values.append(normalized_id)
    if canonical_personal:
        rows = conn.execute(
            f"""
            WITH canonical_sources AS (
                SELECT s.source_id, s.owner_kind, s.owner_id, s.created_at_ms,
                       MIN(evidence.admission_state) AS evidence_admission_state
                FROM agent_memory_sources AS s
                JOIN input_events AS e ON e.id = s.input_event_id
                CROSS JOIN memory_evidence_input_event_links AS source_link
                           INDEXED BY idx_memory_evidence_input_event
                CROSS JOIN agent_memory_evidence AS evidence
                WHERE {' AND '.join(clauses)}
                  AND source_link.input_event_id = s.input_event_id
                  AND source_link.relation = 'source'
                  AND evidence.evidence_id = source_link.evidence_id
                  AND {curatable_personal_evidence_sql('evidence')}
                GROUP BY s.source_id, s.owner_kind, s.owner_id, s.created_at_ms
                HAVING COUNT(DISTINCT evidence.evidence_id) = 1
            )
            SELECT owner_kind, owner_id,
                   MIN(created_at_ms) AS first_source_ms,
                   MAX(created_at_ms) AS last_source_ms,
                   COUNT(*) AS total_source_count,
                   SUM(CASE WHEN evidence_admission_state = 'needs_review'
                            THEN 1 ELSE 0 END) AS needs_review_source_count
            FROM canonical_sources
            GROUP BY owner_kind, owner_id
            ORDER BY owner_kind, owner_id
            """,  # noqa: S608 - clauses and Evidence predicate are fixed above.
            tuple(values),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT s.owner_kind, s.owner_id,
                   MIN(s.created_at_ms) AS first_source_ms,
                   MAX(s.created_at_ms) AS last_source_ms,
                   COUNT(*) AS total_source_count,
                   SUM(CASE WHEN s.disposition = 'needs_review' THEN 1 ELSE 0 END)
                       AS needs_review_source_count
            FROM agent_memory_sources AS s
            JOIN input_events AS e ON e.id = s.input_event_id
            WHERE {' AND '.join(clauses)}
            GROUP BY s.owner_kind, s.owner_id
            ORDER BY s.owner_kind, s.owner_id
            """,  # noqa: S608 - clauses are fixed above.
            tuple(values),
        ).fetchall()
    result: list[dict[str, object]] = []
    for row in rows:
        kind = str(row["owner_kind"])
        identity = str(row["owner_id"])
        cursor = conn.execute(
            """
            SELECT *
            FROM memory_curation_cursors
            WHERE owner_kind = ? AND owner_id = ? AND project = ? AND lane = 'daily'
            """,
            (kind, identity, project),
        ).fetchone()
        cursor_ms = int(cursor["last_source_created_at_ms"] or 0) if cursor is not None else 0
        cursor_id = str(cursor["last_source_id"] or "") if cursor is not None else ""
        pending = _pending_source_count(
            conn,
            owner_kind=kind,
            owner_id=identity,
            project=project,
            cursor_ms=cursor_ms,
            cursor_id=cursor_id,
            canonical_personal=canonical_personal,
        )
        last_run_id = str(cursor["last_run_id"] or "") if cursor is not None else ""
        last_run_status = _run_status(conn, last_run_id)
        stored_status = str(cursor["status"] or "idle") if cursor is not None else "idle"
        waiting_review = (
            not auto_apply
            and stored_status == "waiting_review"
            and last_run_status == "draft"
        )
        running = (
            stored_status == "running"
            and current_ms - int(cursor["updated_at_ms"] or 0) < running_lease_ms
            if cursor is not None
            else False
        )
        next_due = int(cursor["next_due_at_ms"] or 0) if cursor is not None else 0
        if next_due <= 0:
            next_due = int(row["last_source_ms"] or 0) + initial_settle_ms
        due = pending > 0 and not waiting_review and not running and current_ms >= next_due
        if waiting_review:
            reason = "draft_pending_review"
        elif running:
            reason = "running"
        elif pending <= 0:
            reason = "no_sources"
        elif current_ms >= next_due:
            reason = "daily"
        else:
            reason = "not_due"
        result.append(
            {
                "ownerKind": kind,
                "ownerId": identity,
                "pendingSourceCount": pending,
                "needsReviewSourceCount": int(
                    row["needs_review_source_count"] or 0
                ),
                "totalSourceCount": int(row["total_source_count"] or 0),
                "firstSourceAtMs": int(row["first_source_ms"] or 0),
                "lastSourceAtMs": int(row["last_source_ms"] or 0),
                "lastSourceCursor": {
                    "createdAtMs": cursor_ms,
                    "sourceId": cursor_id,
                },
                "lastRunAtMs": int(cursor["last_run_ms"] or 0) if cursor is not None else 0,
                "lastRunId": last_run_id,
                "lastRunStatus": last_run_status,
                "nextDueAtMs": next_due,
                "status": stored_status,
                "due": due,
                "dueReason": reason,
                "consecutiveFailures": (
                    int(cursor["consecutive_failures"] or 0) if cursor is not None else 0
                ),
                "lastError": str(cursor["last_error"] or "") if cursor is not None else "",
            }
        )
    return result


def _agent_conversation_context(
    conn: sqlite3.Connection,
    *,
    project: str,
    owner_kind: str,
    owner_id: str,
    session_ids: list[str],
    timeline_date: str,
    max_messages: int = 10,
    max_chars: int = 2_000,
) -> dict[str, object]:
    """Return the latest digest plus a small uncompacted dialogue tail.

    This projection is context-only: it carries no legal Evidence IDs and may
    never independently support an Atom or Book.
    """

    bounded_messages = max(1, min(int(max_messages), 10))
    char_budget = max(600, min(int(max_chars), 2_000))
    start_ms, end_ms = local_day_bounds_ms(timeline_date)
    clauses = [
        "project = ?",
        "status = 'active'",
        "occurred_at_ms >= ?",
        "occurred_at_ms < ?",
        "source_kind IN ("
        "'user_message', 'assistant_message', 'room_event', 'session_digest'"
        ")",
    ]
    params: list[object] = [project, start_ms, end_ms]
    if owner_kind == "agent":
        clauses.append("role_id = ?")
        params.append(owner_id)
    elif session_ids:
        scoped_sessions = session_ids[:64]
        clauses.append(
            f"session_id IN ({','.join('?' for _ in scoped_sessions)})"
        )
        params.extend(scoped_sessions)
    else:
        payload = _empty_agent_conversation_context(timeline_date)
        validate_contract(payload, "agent-conversation-context.v1.json")
        return payload
    params.append(80)
    rows = conn.execute(
        f"""
        SELECT session_id, source_kind, content_text, occurred_at_ms
        FROM agent_memory_evidence
        WHERE {' AND '.join(clauses)}
        ORDER BY occurred_at_ms DESC, evidence_id DESC
        LIMIT ?
        """,  # noqa: S608 - placeholders are generated, never values
        tuple(params),
    ).fetchall()
    rows = list(reversed(rows))

    latest_digest_ms: dict[str, int] = {}
    for row in rows:
        if str(row["source_kind"]) != "session_digest":
            continue
        session_id = str(row["session_id"] or "")
        latest_digest_ms[session_id] = max(
            latest_digest_ms.get(session_id, 0),
            int(row["occurred_at_ms"] or 0),
        )

    messages: list[dict[str, object]] = []
    deduplicated = 0
    redacted = 0
    remaining_chars = char_budget
    last_seen: dict[tuple[str, str], int] = {}
    for row in rows:
        if len(messages) >= bounded_messages or remaining_chars <= 0:
            break
        text = compact_whitespace(str(row["content_text"] or ""))
        if not text:
            continue
        if memory_evidence_exclusion_reason(text):
            deduplicated += 1
            continue
        if contains_sensitive_content(text):
            redacted += 1
            continue
        source_kind = str(row["source_kind"])
        session_id = str(row["session_id"] or "")
        digest_ms = latest_digest_ms.get(session_id, 0)
        if source_kind == "session_digest":
            if occurred_at_ms := int(row["occurred_at_ms"] or 0):
                if occurred_at_ms != digest_ms:
                    deduplicated += 1
                    continue
        elif digest_ms and int(row["occurred_at_ms"] or 0) <= digest_ms:
            deduplicated += 1
            continue
        role = "assistant" if source_kind == "assistant_message" else "user"
        if source_kind == "session_digest":
            role = "assistant"
        occurred_at_ms = int(row["occurred_at_ms"] or 0)
        key = (role, normalize_text(text))
        previous = last_seen.get(key)
        if previous is not None and occurred_at_ms - previous <= 5 * 60 * 1_000:
            deduplicated += 1
            continue
        last_seen[key] = occurred_at_ms
        per_message_chars = 600 if source_kind == "session_digest" else 320
        bounded_text = text[: min(per_message_chars, remaining_chars)]
        if not bounded_text:
            continue
        messages.append(
            {
                "role": role,
                "sourceKind": source_kind,
                "text": bounded_text,
                "occurredAtMs": occurred_at_ms,
            }
        )
        remaining_chars -= len(bounded_text)

    payload = {
        "schemaVersion": "rag-ime.agent-conversation-context.v1",
        "available": bool(messages),
        "date": timeline_date,
        "messages": messages,
        "messageCount": len(messages),
        "deduplicatedMessageCount": deduplicated,
        "redactedMessageCount": redacted,
        "corroborationOnly": True,
        "maySupportFacts": False,
    }
    validate_contract(payload, "agent-conversation-context.v1.json")
    return payload


def _empty_agent_conversation_context(timeline_date: str) -> dict[str, object]:
    return {
        "schemaVersion": "rag-ime.agent-conversation-context.v1",
        "available": False,
        "date": timeline_date,
        "messages": [],
        "messageCount": 0,
        "deduplicatedMessageCount": 0,
        "redactedMessageCount": 0,
        "corroborationOnly": True,
        "maySupportFacts": False,
    }


def _empty_existing_memory_context() -> dict[str, object]:
    return {
        "existingMemoryBooks": [],
        "existingMemoryBookIndex": [],
        "existingMemoryAtoms": [],
        "existingMemoryRecall": {
            "strategy": "deferred_until_quality_gate",
            "probeCount": 0,
            "hybridQueryCount": 0,
            "hybridErrorCount": 0,
            "vectorEnabled": False,
            "recalledAtomCount": 0,
            "recalledBookCount": 0,
        },
        "archivedMemoryBookGuards": [],
    }


def _build_current_personal_atom_catalog(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    """Load the complete supported current personal Atom catalog, never Books."""

    rows = conn.execute(
        f"""
        SELECT atom.*
        FROM memory_atoms AS atom
        WHERE atom.owner_kind = 'user' AND atom.owner_id = 'default'
          AND atom.knowledge_domain = 'personal_memory'
          AND atom.scope_kind = 'user' AND atom.scope_id = 'default'
          AND atom.scope_mode = 'authoritative'
          AND COALESCE(atom.scope_project, '') = ''
          AND COALESCE(atom.scope_app, '') = ''
          AND atom.kind IN (
              'personal_fact', 'personal_habit',
              'durable_preference', 'personal_principle'
          )
          AND atom.status IN ('active', 'approved')
          AND atom.claim_state = 'current'
        ORDER BY atom.claim_key, atom.id
        """
    ).fetchall()
    atoms: list[dict[str, object]] = []
    for row in rows:
        atom_id = str(row["id"])
        evidence_ids = [ref["id"] for ref in card_source_refs(conn, atom_id, limit=None)]
        if not evidence_ids:
            continue
        tags = [
            str(item["tag"])
            for item in conn.execute(
                """
                SELECT tag.tag
                FROM memory_atom_tags AS atom_tag
                JOIN memory_tags AS tag
                  ON CAST(tag.id AS TEXT) = atom_tag.tag_id
                WHERE atom_tag.memory_atom_id = ?
                ORDER BY atom_tag.weight DESC, tag.tag
                """,
                (atom_id,),
            ).fetchall()
        ]
        atoms.append(
            {
                "atomId": atom_id,
                "revision": card_revision(dict(row)),
                "kind": str(row["kind"]),
                "claimKey": str(row["claim_key"]),
                "canonicalText": str(row["canonical_text"] or row["text"] or ""),
                "evidenceIds": evidence_ids,
                "sourceEventIds": _json_ints(row["source_event_ids_json"]),
                "tags": tags,
                "confidence": float(row["confidence"] or 0.0),
                "validFromMs": int(row["valid_from_ms"] or 0),
                "lineageId": str(row["lineage_id"] or ""),
                "claimState": str(row["claim_state"] or "current"),
                "status": str(row["status"] or "active"),
                "project": "",
                "app": "",
                "ownerKind": str(row["owner_kind"] or ""),
                "ownerId": str(row["owner_id"] or ""),
                "privacyLevel": str(row["privacy_level"] or ""),
                "knowledgeDomain": str(row["knowledge_domain"] or ""),
                "scopeKind": str(row["scope_kind"] or ""),
                "scopeId": str(row["scope_id"] or ""),
                "visibility": str(row["visibility"] or ""),
                "authorizationRevision": str(row["authorization_revision"] or ""),
                "bindingId": str(row["binding_id"] or ""),
                "scopeMode": str(row["scope_mode"] or ""),
            }
        )
    return {
        "existingMemoryBooks": [],
        "existingMemoryBookIndex": [],
        "existingMemoryAtoms": atoms,
        "existingMemoryRecall": {
            "strategy": "complete_current_personal_atom_catalog",
            "probeCount": 0,
            "hybridQueryCount": 0,
            "hybridErrorCount": 0,
            "vectorEnabled": False,
            "recalledAtomCount": len(atoms),
            "recalledBookCount": 0,
        },
        "archivedMemoryBookGuards": [],
    }


def _owner_topic_book_index(
    conn: sqlite3.Connection,
    *,
    owner_kind: str,
    owner_id: str,
    project: str = "",
) -> list[dict[str, object]]:
    """Return a complete identity-only index for this owner's Topic Books."""

    rows = conn.execute(
        """
        SELECT *
        FROM memory_books
        WHERE owner_kind = ? AND owner_id = ?
          AND book_type = 'topic'
          AND (? = '' OR project = ? OR project = '')
        ORDER BY book_id
        """,
        (owner_kind, owner_id, compact_whitespace(project), compact_whitespace(project)),
    ).fetchall()
    result: list[dict[str, object]] = []
    for row in rows:
        metadata = _json_mapping(row["metadata_json"])
        aliases: list[str] = []
        for key in ("topicAliases", "aliases"):
            value = metadata.get(key)
            values = value if isinstance(value, list) else [value]
            aliases.extend(
                compact_whitespace(str(item))
                for item in values
                if item is not None
                and compact_whitespace(str(item))
                and not contains_sensitive_content(str(item))
            )
        for value in (row["title"], row["book_key"]):
            text = compact_whitespace(str(value or ""))
            if text and not contains_sensitive_content(text):
                aliases.append(text)
        result.append(
            {
                "bookId": str(row["book_id"] or ""),
                "bookType": str(row["book_type"] or "topic"),
                "createdAtMs": int(row["created_at_ms"] or 0),
                "bookKey": str(row["book_key"] or ""),
                "title": compact_whitespace(str(row["title"] or ""))[:160],
                "aliases": list(dict.fromkeys(aliases))[:64],
                "tags": _json_strings(row["tags_json"]),
                "queryExpansions": _json_strings(row["query_expansions_json"]),
                "semanticGroupIds": [
                    str(member[0])
                    for member in conn.execute(
                        "SELECT group_id FROM memory_semantic_group_members "
                        "WHERE member_type = 'book' AND member_id = ? ORDER BY group_id",
                        (str(row["book_id"] or ""),),
                    ).fetchall()
                ],
                "memoryAtomIds": _json_strings(row["memory_atom_ids_json"]),
                "sourceEventIds": _json_ints(row["source_event_ids_json"]),
                "ownerKind": str(row["owner_kind"] or ""),
                "ownerId": str(row["owner_id"] or ""),
                "project": str(row["project"] or ""),
                "app": str(row["app"] or ""),
                "knowledgeDomain": str(row["knowledge_domain"] or ""),
                "scopeKind": str(row["scope_kind"] or ""),
                "scopeId": str(row["scope_id"] or ""),
                "visibility": str(row["visibility"] or ""),
                "authorizationRevision": str(row["authorization_revision"] or ""),
                "bindingId": str(row["binding_id"] or ""),
                "scopeMode": str(row["scope_mode"] or ""),
                "status": str(row["status"] or "active"),
                "supersededByBookId": compact_whitespace(
                    str(metadata.get("supersededByBookId") or "")
                ),
            }
        )
    return result


def _build_existing_memory_context(
    conn: sqlite3.Connection,
    *,
    inputs: list[dict[str, object]],
    owner_kind: str,
    owner_id: str,
    project: str,
    embedding_provider: EmbeddingProvider | None,
) -> dict[str, object]:
    """Recall only the old facts relevant to quality-gated source inputs."""

    book_index = _owner_topic_book_index(
        conn,
        owner_kind=owner_kind,
        owner_id=owner_id,
        project=project,
    )
    book_rows = conn.execute(
        """
        SELECT *
        FROM memory_books
        WHERE owner_kind = ? AND owner_id = ?
          AND status IN ('active', 'approved', 'archived')
          AND book_type = 'topic'
          AND (? = '' OR project = ? OR project = '')
        ORDER BY CASE WHEN status = 'archived' THEN 1 ELSE 0 END,
                 updated_at_ms DESC
        """,
        (owner_kind, owner_id, project, project),
    ).fetchall()
    atom_rows = conn.execute(
        """
        SELECT *
        FROM memory_atoms
        WHERE owner_kind = ? AND owner_id = ? AND status = 'active'
          AND claim_state = 'current'
          AND (? = '' OR scope_project = ? OR scope_project = '')
        ORDER BY updated_at_ms DESC
        """,
        (owner_kind, owner_id, project, project),
    ).fetchall()
    recalled_atom_ids, recalled_book_ids, recall_summary = (
        _recall_existing_memory_for_inputs(
            conn,
            inputs=inputs,
            atom_rows=atom_rows,
            book_rows=book_rows,
            owner_kind=owner_kind,
            owner_id=owner_id,
            project=project,
            embedding_provider=embedding_provider,
        )
    )
    atom_rows_by_id = {str(row["id"]): row for row in atom_rows}
    book_rows_by_id = {str(row["book_id"]): row for row in book_rows}
    books = [
        {
            "bookId": str(row["book_id"]),
            "bookType": str(row["book_type"]),
            "createdAtMs": int(row["created_at_ms"] or 0),
            "bookKey": str(row["book_key"]),
            "title": str(row["title"]),
            "summary": compact_whitespace(str(row["summary"]))[:800],
            "aliases": [
                *_metadata_strings(
                    _json_mapping(row["metadata_json"]).get("topicAliases")
                ),
                *_metadata_strings(
                    _json_mapping(row["metadata_json"]).get("aliases")
                ),
            ],
            "tags": _json_strings(row["tags_json"]),
            "sourceEventIds": _json_ints(row["source_event_ids_json"]),
            "memoryAtomIds": _json_strings(row["memory_atom_ids_json"]),
            "status": str(row["status"]),
            "ownerKind": str(row["owner_kind"] or ""),
            "ownerId": str(row["owner_id"] or ""),
            "project": str(row["project"] or ""),
            "app": str(row["app"] or ""),
            "knowledgeDomain": str(row["knowledge_domain"] or ""),
            "scopeKind": str(row["scope_kind"] or ""),
            "scopeId": str(row["scope_id"] or ""),
            "visibility": str(row["visibility"] or ""),
            "authorizationRevision": str(row["authorization_revision"] or ""),
            "bindingId": str(row["binding_id"] or ""),
            "scopeMode": str(row["scope_mode"] or ""),
        }
        for book_id in recalled_book_ids
        if (row := book_rows_by_id.get(book_id)) is not None
    ]
    atoms = [
        {
            "atomId": str(row["id"]),
            "kind": str(row["kind"]),
            "canonicalText": str(row["canonical_text"] or row["text"]),
            "summary": "",
            "tags": [],
            "sourceEventIds": _json_ints(row["source_event_ids_json"]),
            "status": str(row["status"]),
            "claimKey": str(row["claim_key"] or ""),
            "lineageId": str(row["lineage_id"] or ""),
            "claimState": str(row["claim_state"] or "current"),
            "validFromMs": int(row["valid_from_ms"] or 0),
            "validToMs": (
                int(row["valid_to_ms"])
                if row["valid_to_ms"] is not None
                else None
            ),
            "supersedesId": str(row["supersedes_id"] or ""),
            "project": str(row["scope_project"] or ""),
            "app": str(row["scope_app"] or ""),
            "ownerKind": str(row["owner_kind"] or ""),
            "ownerId": str(row["owner_id"] or ""),
            "privacyLevel": str(row["privacy_level"] or ""),
            "knowledgeDomain": str(row["knowledge_domain"] or ""),
            "scopeKind": str(row["scope_kind"] or ""),
            "scopeId": str(row["scope_id"] or ""),
            "visibility": str(row["visibility"] or ""),
            "authorizationRevision": str(row["authorization_revision"] or ""),
            "bindingId": str(row["binding_id"] or ""),
            "scopeMode": str(row["scope_mode"] or ""),
        }
        for atom_id in recalled_atom_ids
        if (row := atom_rows_by_id.get(atom_id)) is not None
    ]
    archived_book_guards = [
        {
            "bookId": str(row["book_id"]),
            "bookKey": str(row["book_key"] or ""),
            "title": str(row["title"] or ""),
            "tags": _json_strings(row["tags_json"]),
            "status": "archived",
        }
        for row in book_rows
        if str(row["status"] or "") == "archived"
    ][:MAX_ARCHIVED_TOPIC_BOOK_GUARDS]
    return {
        "existingMemoryBooks": books,
        "existingMemoryBookIndex": book_index,
        "existingMemoryAtoms": atoms,
        "existingMemoryRecall": recall_summary,
        "archivedMemoryBookGuards": archived_book_guards,
    }


def _build_atom_first_memory_context(
    conn: sqlite3.Connection,
    *,
    inputs: list[dict[str, object]],
    owner_kind: str,
    owner_id: str,
    project: str,
    embedding_provider: EmbeddingProvider | None,
) -> dict[str, object]:
    """Combine the complete personal catalog with relevant topic memory.

    Personal claims need full-catalog duplicate/correction checks. Project and
    knowledge-topic Atoms can be much larger, so they retain bounded hybrid
    recall together with their Books. This is one model snapshot, not two
    classifiers or two write paths.
    """

    personal = _build_current_personal_atom_catalog(conn)
    relevant = _build_existing_memory_context(
        conn,
        inputs=inputs,
        owner_kind=owner_kind,
        owner_id=owner_id,
        project=project,
        embedding_provider=embedding_provider,
    )
    atoms_by_id: dict[str, dict[str, object]] = {}
    for source in (relevant, personal):
        for raw in source.get("existingMemoryAtoms") or []:
            if not isinstance(raw, dict):
                continue
            atom_id = compact_whitespace(str(raw.get("atomId") or ""))
            if atom_id:
                atoms_by_id[atom_id] = dict(raw)
    recall = dict(relevant.get("existingMemoryRecall") or {})
    recall.update(
        {
            "strategy": "complete_personal_plus_relevant_topic_memory",
            "personalCatalogAtomCount": len(
                personal.get("existingMemoryAtoms") or []
            ),
            "recalledAtomCount": len(atoms_by_id),
            "recalledBookCount": len(relevant.get("existingMemoryBooks") or []),
        }
    )
    return {
        "existingMemoryBooks": [
            dict(item)
            for item in relevant.get("existingMemoryBooks") or []
            if isinstance(item, dict)
        ],
        "existingMemoryBookIndex": [
            dict(item)
            for item in relevant.get("existingMemoryBookIndex") or []
            if isinstance(item, dict)
        ],
        "existingMemoryAtoms": list(atoms_by_id.values()),
        "existingMemoryRecall": recall,
        "archivedMemoryBookGuards": [
            dict(item)
            for item in relevant.get("archivedMemoryBookGuards") or []
            if isinstance(item, dict)
        ],
    }


def _with_owner_bundle_hash(payload: dict[str, object]) -> dict[str, object]:
    result = dict(payload)
    result.pop("bundleHash", None)
    result["bundleHash"] = stable_text_hash(
        json.dumps(result, ensure_ascii=False, sort_keys=True)
    )
    return result


def _capture_hints_for_sources(
    conn: sqlite3.Connection,
    source_ids: list[str],
) -> dict[str, list[dict[str, object]]]:
    result: dict[str, list[dict[str, object]]] = {}
    ids = tuple(dict.fromkeys(source_id for source_id in source_ids if source_id))
    for offset in range(0, len(ids), 400):
        chunk = ids[offset : offset + 400]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT source_id, hint_id, kind, normalized_claim, scope, reason,
                   basis, future_use, supersedes, evidence_ids_json, updated_at_ms
            FROM memory_capture_hints
            WHERE source_id IN ({placeholders}) AND status = 'active'
            ORDER BY updated_at_ms ASC, hint_id ASC
            """,
            chunk,
        ).fetchall()
        for row in rows:
            result.setdefault(str(row["source_id"]), []).append(
                {
                    "hintId": str(row["hint_id"]),
                    "kind": str(row["kind"]),
                    "claim": str(row["normalized_claim"]),
                    "scope": str(row["scope"]),
                    "reason": str(row["reason"]),
                    "basis": str(row["basis"]),
                    "futureUse": str(row["future_use"]),
                    "supersedes": str(row["supersedes"]),
                    "evidenceIds": _json_strings(row["evidence_ids_json"]),
                    "authoritative": False,
                }
            )
    return result


def _curatable_personal_evidence_by_event_id(
    conn: sqlite3.Connection,
    event_ids: list[int],
) -> dict[int, sqlite3.Row]:
    selected = sorted({int(value) for value in event_ids if int(value) > 0})
    candidates: dict[int, list[sqlite3.Row]] = {}
    for offset in range(0, len(selected), 400):
        chunk = selected[offset : offset + 400]
        if not chunk:
            continue
        rows = conn.execute(
            f"""
            SELECT source_link.input_event_id, evidence.*
            FROM memory_evidence_input_event_links AS source_link
                 INDEXED BY idx_memory_evidence_input_event
            CROSS JOIN agent_memory_evidence AS evidence
            WHERE source_link.input_event_id IN ({','.join('?' for _ in chunk)})
              AND source_link.relation = 'source'
              AND evidence.evidence_id = source_link.evidence_id
              AND {curatable_personal_evidence_sql('evidence')}
            ORDER BY source_link.input_event_id, evidence.recorded_at_ms,
                     evidence.evidence_id
            """,
            tuple(chunk),
        ).fetchall()
        for row in rows:
            candidates.setdefault(int(row["input_event_id"]), []).append(row)
    # Ambiguous identity fails closed: the model must never choose between two
    # physical Evidence owners for one immutable input event.
    return {
        event_id: rows[0]
        for event_id, rows in candidates.items()
        if len(rows) == 1
    }


def _preceding_personal_context_rows(
    conn: sqlite3.Connection,
    *,
    owner_kind: str,
    owner_id: str,
    project: str,
    before_created_at_ms: int,
    before_source_id: str,
) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT event.created_at_ms, event.committed_text, event.source,
               event.app
        FROM agent_memory_sources AS source
        JOIN input_events AS event ON event.id = source.input_event_id
        WHERE source.owner_kind = ? AND source.owner_id = ?
          AND source.status = 'active' AND source.source_role = 'user'
          AND (? = '' OR event.project = ? OR event.project = '')
          AND (
              source.created_at_ms < ?
              OR (source.created_at_ms = ? AND source.source_id < ?)
          )
        ORDER BY source.created_at_ms DESC, source.source_id DESC
        LIMIT 12
        """,
        (
            owner_kind,
            owner_id,
            project,
            project,
            max(0, int(before_created_at_ms)),
            max(0, int(before_created_at_ms)),
            compact_whitespace(before_source_id),
        ),
    ).fetchall()
    return [
        {
            "occurredAtMs": int(row["created_at_ms"] or 0),
            "channel": str(row["source"] or ""),
            "app": str(row["app"] or ""),
            "text": compact_whitespace(str(row["committed_text"] or ""))[:1200],
        }
        for row in reversed(rows)
        if compact_whitespace(str(row["committed_text"] or ""))
    ]


def _build_owner_source_bundle(
    conn: sqlite3.Connection,
    *,
    owner_kind: str,
    owner_id: str,
    project: str,
    limit: int,
    include_agent_dialogue: bool = True,
    embedding_provider: EmbeddingProvider | None = None,
    include_existing_memory: bool = True,
    canonical_personal: bool = False,
    personal_window_ms: int = MAX_PERSONAL_V2_WINDOW_MS,
) -> dict[str, object]:
    cursor = conn.execute(
        """
        SELECT last_source_created_at_ms, last_source_id
        FROM memory_curation_cursors
        WHERE owner_kind = ? AND owner_id = ? AND project = ? AND lane = 'daily'
        """,
        (owner_kind, owner_id, project),
    ).fetchone()
    cursor_ms = int(cursor["last_source_created_at_ms"] or 0) if cursor is not None else 0
    cursor_id = str(cursor["last_source_id"] or "") if cursor is not None else ""
    logical_limit = max(
        1,
        min(int(limit), MAX_PERSONAL_V2_SOURCES if canonical_personal else 64),
    )
    physical_limit = min(
        50_000,
        max(2_048, logical_limit * (2 if canonical_personal else 128)),
    )
    source_order_sql = (
        "COALESCE(CAST(json_extract(s.metadata_json, "
        "'$.sourceOccurredAtMs') AS INTEGER), s.created_at_ms) ASC, "
        "s.created_at_ms ASC, s.source_id ASC"
        if canonical_personal
        else "s.created_at_ms ASC, s.source_id ASC"
    )
    late_personal_clause = (
        f"""
              OR (
                  s.disposition IN ('pending', 'needs_review')
                  AND (
                      SELECT COUNT(DISTINCT late_evidence.evidence_id)
                      FROM memory_evidence_input_event_links AS late_link
                           INDEXED BY idx_memory_evidence_input_event
                      CROSS JOIN agent_memory_evidence AS late_evidence
                      WHERE late_link.input_event_id = s.input_event_id
                        AND late_link.relation = 'source'
                        AND late_evidence.evidence_id = late_link.evidence_id
                        AND {curatable_personal_evidence_sql('late_evidence')}
                  ) = 1
              )
        """
        if canonical_personal
        else ""
    )
    rows = conn.execute(
        f"""
        SELECT s.*, e.committed_text, e.source AS event_source,
               e.recent_context, e.app, e.project AS event_project,
               e.context_group_id, e.context_group_level, e.tags_json
        FROM agent_memory_sources AS s
        JOIN input_events AS e ON e.id = s.input_event_id
        WHERE s.owner_kind = ? AND s.owner_id = ? AND s.status = 'active'
          AND (? = '' OR e.project = ? OR e.project = '')
          AND s.disposition IN ({','.join('?' for _ in _ELIGIBLE_DISPOSITIONS)})
                  AND e.source != 'personal_profile_editor'
          AND (
              s.created_at_ms > ?
              OR (s.created_at_ms = ? AND s.source_id > ?)
              {late_personal_clause}
          )
        ORDER BY {source_order_sql}
        LIMIT ?
        """,  # noqa: S608 - the ordering expression is fixed above.
        (
            owner_kind,
            owner_id,
            project,
            project,
            *_ELIGIBLE_DISPOSITIONS,
            cursor_ms,
            cursor_ms,
            cursor_id,
            physical_limit,
        ),
    ).fetchall()
    capture_hints = _capture_hints_for_sources(
        conn,
        [str(row["source_id"]) for row in rows],
    )
    evidence_by_event_id = (
        _curatable_personal_evidence_by_event_id(
            conn,
            [int(row["input_event_id"]) for row in rows],
        )
        if canonical_personal
        else {}
    )
    raw_inputs: list[dict[str, object]] = []
    for row in rows:
        source_metadata = _json_mapping(row["metadata_json"])
        evidence = evidence_by_event_id.get(int(row["input_event_id"]))
        evidence_metadata = (
            _json_mapping(evidence["metadata_json"])
            if evidence is not None
            else {}
        )
        raw_inputs.append(
            {
                "sourceId": str(row["source_id"]),
                "sourceIds": [str(row["source_id"])],
                "sourceKind": str(row["source_kind"]),
                "decisionContext": resolve_decision_context(
                    conn, session_id=str(row["session_id"] or ""),
                    project=str(row["event_project"] or ""),
                    answer_entry_id=str(row["pi_entry_id"] or ""),
                    answer_source_id=str(row["source_id"] or ""),
                    expected_question_evidence_id=str(
                        source_metadata["decisionContext"].get("questionEvidenceId") or ""
                    ),
                    answer_text=str(row["committed_text"] or ""),
                    occurred_at_ms=int(row["created_at_ms"] or 0),
                ) if isinstance(source_metadata.get("decisionContext"), Mapping) else None,
                "trustClass": str(row["trust_class"]),
                "createdAtMs": int(row["created_at_ms"] or 0),
                "sourceEventIds": [int(row["input_event_id"])],
                "text": compact_whitespace(
                    str(
                        evidence["content_text"]
                        if evidence is not None
                        else row["committed_text"]
                        or ""
                    )
                ),
                "disposition": str(row["disposition"]),
                "source": str(row["event_source"] or ""),
                "recentContext": compact_whitespace(
                    str(row["recent_context"] or "")
                )[:4000],
                "app": str(row["app"] or ""),
                "project": str(row["event_project"] or ""),
                "contextGroupId": str(row["context_group_id"] or ""),
                "contextGroupLevel": str(row["context_group_level"] or "app"),
                "sourceMetadataTags": _json_strings(row["tags_json"]),
                "sessionId": str(row["session_id"] or ""),
                "captureHints": capture_hints.get(str(row["source_id"]), []),
                "sourceOccurredAtMs": int(
                    source_metadata.get("sourceOccurredAtMs")
                    or row["created_at_ms"]
                    or 0
                ),
                "externalProvider": compact_whitespace(
                    str(source_metadata.get("externalProvider") or "")
                )[:40],
                "externalTier": compact_whitespace(
                    str(source_metadata.get("externalTier") or "")
                )[:80],
                "evidenceId": (
                    str(evidence["evidence_id"]) if evidence is not None else ""
                ),
                "evidenceIds": (
                    [str(evidence["evidence_id"])] if evidence is not None else []
                ),
                "evidenceAdmissionState": (
                    str(evidence["admission_state"]) if evidence is not None else ""
                ),
                "evidenceOriginKind": (
                    str(evidence["origin_kind"]) if evidence is not None else ""
                ),
                "boundaryKind": (
                    str(evidence["boundary_kind"]) if evidence is not None else ""
                ),
                "sourceChannel": compact_whitespace(
                    str(
                        evidence_metadata.get("sourceChannel")
                        or (
                            "voice"
                            if evidence is not None
                            and str(evidence["origin_kind"]) == "capture_v2_voice"
                            else "input_method"
                        )
                    )
                )[:40],
            }
        )
    if canonical_personal:
        # Historical imports may be written months after the input occurred.
        # Luna needs one chronological local day, not one ingestion batch, both
        # to reconstruct Rime fragments correctly and to see the day's complete
        # cross-application context.
        raw_inputs.sort(
            key=lambda item: (
                int(
                    item.get("sourceOccurredAtMs")
                    or item.get("createdAtMs")
                    or 0
                ),
                int(item.get("createdAtMs") or 0),
                str(item.get("sourceId") or ""),
            )
        )
    curation_date = (
        local_date_for_timestamp(
            int(
                (
                    raw_inputs[0].get("sourceOccurredAtMs")
                    if canonical_personal
                    else raw_inputs[0].get("createdAtMs")
                )
                or 0
            )
        )
        if raw_inputs
        else local_date_for_timestamp(0)
    )
    raw_inputs = [
        item
        for item in raw_inputs
        if local_date_for_timestamp(
            int(
                (
                    item.get("sourceOccurredAtMs")
                    if canonical_personal
                    else item.get("createdAtMs")
                )
                or 0
            )
        )
        == curation_date
    ]
    if canonical_personal and raw_inputs:
        window_start_ms = int(
            raw_inputs[0].get("sourceOccurredAtMs")
            or raw_inputs[0].get("createdAtMs")
            or 0
        )
        raw_inputs = [
            item
            for item in raw_inputs
            if int(item.get("sourceOccurredAtMs") or item.get("createdAtMs") or 0)
            <= window_start_ms + max(60_000, int(personal_window_ms))
        ]
    projected_inputs = _coalesce_owner_inputs(raw_inputs)
    inputs = (
        projected_inputs[:logical_limit]
        if canonical_personal
        else _bounded_external_model_inputs(projected_inputs, limit=logical_limit)
    )
    session_ids = list(
        dict.fromkeys(
            str(item.get("sessionId") or "")
            for item in raw_inputs
            if str(item.get("sessionId") or "")
        )
    )
    activity_context = load_activity_timeline_context(
        conn,
        project=project,
        timeline_date=curation_date,
    )
    conversation_context = (
        _agent_conversation_context(
            conn,
            project=project,
            owner_kind=owner_kind,
            owner_id=owner_id,
            session_ids=session_ids,
            timeline_date=curation_date,
        )
        if include_agent_dialogue
        else _empty_agent_conversation_context(curation_date)
    )

    existing_memory_context = (
        _build_existing_memory_context(
            conn,
            inputs=inputs,
            owner_kind=owner_kind,
            owner_id=owner_id,
            project=project,
            embedding_provider=embedding_provider,
        )
        if include_existing_memory
        else _empty_existing_memory_context()
    )
    display_name = owner_id
    if owner_kind == "agent":
        display_row = conn.execute(
            """
            SELECT display_name
            FROM agent_personas
            WHERE role_id = ? AND status = 'active'
            ORDER BY updated_at_ms DESC
            LIMIT 1
            """,
            (owner_id,),
        ).fetchone()
        if display_row is not None:
            display_name = compact_whitespace(str(display_row["display_name"] or "")) or owner_id
    event_ids = [
        event_id
        for item in inputs
        for event_id in item["sourceEventIds"]
        if isinstance(event_id, int)
    ]
    purpose_profile = personal_current_state_profile(conn)
    context_only = (
        _preceding_personal_context_rows(
            conn,
            owner_kind=owner_kind,
            owner_id=owner_id,
            project=project,
            before_created_at_ms=int(inputs[0]["createdAtMs"]) if inputs else 0,
            before_source_id=str(inputs[0]["sourceId"]) if inputs else "",
        )
        if canonical_personal and inputs
        else []
    )
    payload: dict[str, object] = {
        "schemaVersion": "rag-ime.owner-memory-source-bundle.v1",
        "project": project,
        "owner": {
            "kind": owner_kind,
            "id": owner_id,
            "displayName": display_name,
        },
        "purposeProfile": purpose_profile,
        "inputs": inputs,
        "recentEvents": [
            {
                "eventId": int(item["sourceEventIds"][0]),
                "sourceEventIds": list(item["sourceEventIds"]),
                "sourceRef": item["sourceRef"],
                "sourceIds": _input_source_ids(item),
                "createdAtMs": item["createdAtMs"],
                "sourceOccurredAtMs": item["sourceOccurredAtMs"],
                "text": item["text"],
                "decisionContext": item.get("decisionContext"),
                "sourceKind": item["sourceKind"],
                "source": item["source"],
                "project": item["project"],
                "app": item["app"],
                "contextGroupId": item["contextGroupId"],
            }
            for item in inputs
        ],
        "existingMemoryBooks": existing_memory_context["existingMemoryBooks"],
        "existingMemoryBookIndex": existing_memory_context["existingMemoryBookIndex"],
        "existingMemoryAtoms": existing_memory_context["existingMemoryAtoms"],
        "existingMemoryRecall": existing_memory_context["existingMemoryRecall"],
        # Archived books are compact governance tombstones, not retrieval
        # context. The write gate uses them to prevent a later model response
        # from silently recreating a topic the user explicitly archived.
        "archivedMemoryBookGuards": existing_memory_context[
            "archivedMemoryBookGuards"
        ],
        "activityContext": activity_context,
        "agentConversationContext": conversation_context,
        "contextOnly": context_only,
        "legalContextGroupIds": [],
        # Only immutable owner inputs are legal fact evidence. Timeline and
        # conversation context intentionally expose no event/evidence ids.
        "legalSourceEventIds": event_ids,
        "cursor": {
            "fromSourceCreatedAtMs": cursor_ms,
            "fromSourceId": cursor_id,
            "toSourceCreatedAtMs": int(inputs[-1]["createdAtMs"]) if inputs else cursor_ms,
            "toSourceId": str(inputs[-1]["sourceId"]) if inputs else cursor_id,
            "fromEventId": min(event_ids, default=0),
            "toEventId": max(event_ids, default=0),
            "pendingEventCount": len(inputs),
        },
    }
    return _with_owner_bundle_hash(payload)


def _recall_existing_memory_for_inputs(
    conn: sqlite3.Connection,
    *,
    inputs: list[dict[str, object]],
    atom_rows: list[sqlite3.Row],
    book_rows: list[sqlite3.Row],
    owner_kind: str,
    owner_id: str,
    project: str,
    embedding_provider: EmbeddingProvider | None,
) -> tuple[list[str], list[str], dict[str, object]]:
    """Recall old claims per factual clause, then expand through Topic Books."""

    probes = _existing_memory_recall_probes(inputs)
    atom_rows_by_id = {str(row["id"]): row for row in atom_rows}
    book_rows_by_id = {str(row["book_id"]): row for row in book_rows}
    atom_scores: dict[str, dict[int, float]] = {}
    book_scores: dict[str, dict[int, float]] = {}
    hybrid_query_count = 0
    hybrid_error_count = 0

    for probe_index, probe in enumerate(probes):
        local_atoms = sorted(
            (
                (
                    str(row["id"]),
                    _existing_memory_text_score(
                        probe,
                        " ".join(
                            (
                                str(row["canonical_text"] or row["text"] or ""),
                                str(row["claim_key"] or ""),
                            )
                        ),
                    ),
                )
                for row in atom_rows
            ),
            key=lambda item: (-item[1], item[0]),
        )
        for atom_id, score in local_atoms[:6]:
            if score > 0.0:
                _add_recall_probe_score(atom_scores, atom_id, probe_index, score)

        local_books = sorted(
            (
                (
                    str(row["book_id"]),
                    _existing_memory_text_score(
                        probe,
                        " ".join(
                            (
                                str(row["title"] or ""),
                                str(row["summary"] or ""),
                                " ".join(_json_strings(row["tags_json"])),
                                " ".join(_json_strings(row["query_expansions_json"])),
                            )
                        ),
                    ),
                )
                for row in book_rows
            ),
            key=lambda item: (-item[1], item[0]),
        )
        for book_id, score in local_books[:4]:
            if score > 0.0:
                _add_recall_probe_score(book_scores, book_id, probe_index, score)

        try:
            hits = retrieve_hybrid_rag_memory_hit_objects(
                conn,
                HybridRagQuery(
                    query_text=probe,
                    raw_input=probe,
                    project=project,
                    top_k=10,
                    latency_budget_ms=250,
                    visible_owners=((owner_kind, owner_id),),
                    enabled_lanes=(("time", False), ("feedback", False)),
                ),
                embedding_provider,
            )
            hybrid_query_count += 1
        except (RuntimeError, sqlite3.Error, ValueError):
            # The lexical owner-local path keeps curation correct while a
            # projection or optional embedding provider is temporarily stale.
            hybrid_error_count += 1
            hits = []
        for rank, hit in enumerate(hits, start=1):
            metadata = dict(hit.metadata or {})
            if (
                str(metadata.get("ownerKind") or "") != owner_kind
                or str(metadata.get("ownerId") or "") != owner_id
            ):
                continue
            fused_score = 2.0 / (rank + 1.0) + min(max(hit.score, 0.0), 2.0) * 0.1
            if hit.doc_type == "atom" and hit.source_id in atom_rows_by_id:
                _add_recall_probe_score(
                    atom_scores,
                    hit.source_id,
                    probe_index,
                    fused_score,
                )
            elif hit.doc_type == "book" and hit.source_id in book_rows_by_id:
                _add_recall_probe_score(
                    book_scores,
                    hit.source_id,
                    probe_index,
                    fused_score,
                )

    direct_atom_scores = {
        atom_id: dict(per_probe) for atom_id, per_probe in atom_scores.items()
    }
    direct_book_scores = {
        book_id: dict(per_probe) for book_id, per_probe in book_scores.items()
    }
    atom_to_books: dict[str, list[str]] = {}
    for book_id, row in book_rows_by_id.items():
        for atom_id in _json_strings(row["memory_atom_ids_json"]):
            if atom_id not in atom_rows_by_id:
                continue
            atom_to_books.setdefault(atom_id, []).append(book_id)
            for probe_index, score in direct_book_scores.get(book_id, {}).items():
                _add_recall_probe_score(
                    atom_scores,
                    atom_id,
                    probe_index,
                    score * 0.55,
                )
    for atom_id, per_probe in direct_atom_scores.items():
        for book_id in atom_to_books.get(atom_id, []):
            for probe_index, score in per_probe.items():
                _add_recall_probe_score(
                    book_scores,
                    book_id,
                    probe_index,
                    score * 0.45,
                )

    recalled_atom_ids = _rank_recalled_ids_with_probe_coverage(
        atom_scores,
        coverage_scores=direct_atom_scores,
        rows=atom_rows_by_id,
        limit=MAX_RECALLED_EXISTING_ATOMS,
    )
    recalled_book_ids = _rank_recalled_ids(
        book_scores,
        rows=book_rows_by_id,
        limit=MAX_RECALLED_EXISTING_BOOKS,
    )
    return recalled_atom_ids, recalled_book_ids, {
        "strategy": "per_fact_hybrid_union_with_book_graph",
        "probeCount": len(probes),
        "hybridQueryCount": hybrid_query_count,
        "hybridErrorCount": hybrid_error_count,
        "vectorEnabled": bool(
            embedding_provider
            and getattr(embedding_provider, "fingerprint", "none") != "none"
        ),
        "recalledAtomCount": len(recalled_atom_ids),
        "recalledBookCount": len(recalled_book_ids),
    }


def _existing_memory_recall_probes(
    inputs: list[dict[str, object]],
) -> list[str]:
    probes: list[str] = []
    seen: set[str] = set()
    for item in inputs:
        hinted_claims = [
            compact_whitespace(str(hint.get("claim") or ""))
            for hint in item.get("captureHints") or []
            if isinstance(hint, Mapping)
            and compact_whitespace(str(hint.get("claim") or ""))
        ]
        texts = hinted_claims or [compact_whitespace(str(item.get("text") or ""))]
        clauses = [
            clause
            for text in texts
            if text
            for clause in (split_sentences(text) or [text])
        ]
        for clause in clauses:
            probe = compact_whitespace(clause)[:600]
            normalized = normalize_text(probe)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            probes.append(probe)
            if len(probes) >= MAX_EXISTING_MEMORY_RECALL_PROBES:
                return probes
    return probes


def _existing_memory_text_score(query: str, text: str) -> float:
    normalized_query = normalize_text(query)
    normalized_text = normalize_text(text)
    if not normalized_query or not normalized_text:
        return 0.0
    score = 0.0
    if normalized_query in normalized_text or normalized_text in normalized_query:
        score += 4.0
    hits: list[str] = []
    for term in token_terms(query, max_terms=64):
        if term.casefold() in text.casefold():
            hits.append(term)
    strong_hits = [
        term
        for term in hits
        if len(term) >= 3 or (term.isascii() and len(term) >= 2)
    ]
    if not strong_hits and len(hits) < 3:
        return score
    score += sum(1.0 + min(len(term), 8) / 8.0 for term in strong_hits)
    score += max(0, len(hits) - len(strong_hits)) * 0.2
    return score


def _reconcile_owner_compile_claim_keys(
    compile_output: Mapping[str, object],
    *,
    model_inputs: list[dict[str, object]],
    existing_memory_atoms: list[dict[str, object]],
) -> dict[str, object]:
    """Repair a high-confidence model claim-key split before durable gating.

    Recall and classification are intentionally separate. A model can see the
    correct old Atom yet rename its semantic slot when the new wording is more
    specific. Only an explicit update signal plus a strong, unambiguous text
    match may reuse an old claim; uncertain cases remain separate/reviewable.
    """

    result = dict(compile_output)
    existing_by_claim = {
        compact_whitespace(str(item.get("claimKey") or "")): dict(item)
        for item in existing_memory_atoms
        if compact_whitespace(str(item.get("claimKey") or ""))
    }
    raw_atoms = [
        dict(item)
        for item in compile_output.get("memoryAtoms") or []
        if isinstance(item, Mapping)
    ]
    raw_claim_key_counts: dict[str, int] = {}
    for item in raw_atoms:
        raw_claim_key = compact_whitespace(str(item.get("claimKey") or ""))
        if raw_claim_key:
            raw_claim_key_counts[raw_claim_key] = (
                raw_claim_key_counts.get(raw_claim_key, 0) + 1
            )
    assigned_existing: set[str] = set()
    reconciliations: list[dict[str, object]] = []
    atoms: list[dict[str, object]] = []
    for item in raw_atoms:
        claim_key = compact_whitespace(str(item.get("claimKey") or ""))
        canonical = compact_whitespace(
            str(item.get("canonicalText") or item.get("text") or "")
        )
        source_event_ids = _positive_event_ids(item.get("sourceEventIds"))
        source_text = " ".join(
            compact_whitespace(str(model_input.get("text") or ""))
            for model_input in model_inputs
            if source_event_ids.intersection(
                _positive_event_ids(model_input.get("sourceEventIds"))
            )
        )
        update_text = f"{canonical} {source_text}"
        if (
            canonical
            and _CLAIM_UPDATE_SIGNAL_RE.search(update_text)
            and (
                claim_key not in existing_by_claim
                or raw_claim_key_counts.get(claim_key, 0) > 1
            )
        ):
            ranked = sorted(
                (
                    (
                        _existing_memory_text_score(
                            update_text,
                            " ".join(
                                (
                                    compact_whitespace(
                                        str(
                                            candidate.get("canonicalText")
                                            or candidate.get("text")
                                            or ""
                                        )
                                    ),
                                    candidate_claim_key,
                                )
                            ),
                        ),
                        candidate_claim_key,
                        candidate,
                    )
                    for candidate_claim_key, candidate in existing_by_claim.items()
                    if candidate_claim_key not in assigned_existing
                    or candidate_claim_key == claim_key
                ),
                key=lambda candidate: (-candidate[0], candidate[1]),
            )
            if ranked:
                best_score, best_claim_key, best = ranked[0]
                runner_up_score = ranked[1][0] if len(ranked) > 1 else 0.0
                if (
                    best_claim_key != claim_key
                    and best_score >= MIN_CLAIM_REUSE_SCORE
                    and best_score - runner_up_score >= MIN_CLAIM_REUSE_MARGIN
                ):
                    original_claim_key = claim_key
                    claim_key = best_claim_key
                    item["claimKey"] = best_claim_key
                    item["kind"] = compact_whitespace(
                        str(best.get("kind") or item.get("kind") or "")
                    )
                    existing_lineage = compact_whitespace(
                        str(best.get("lineageId") or "")
                    )
                    if existing_lineage:
                        item["lineageId"] = existing_lineage
                    reconciliations.append(
                        {
                            "fromClaimKey": original_claim_key,
                            "toClaimKey": best_claim_key,
                            "targetAtomId": compact_whitespace(
                                str(best.get("atomId") or "")
                            ),
                            "score": round(best_score, 4),
                            "runnerUpScore": round(runner_up_score, 4),
                            "reason": (
                                "explicit_update_reassigned_mismatched_recalled_slot"
                                if original_claim_key in existing_by_claim
                                else "explicit_update_high_confidence_recalled_slot"
                            ),
                        }
                    )
        if claim_key in existing_by_claim:
            assigned_existing.add(claim_key)
        atoms.append(item)
    atoms, same_source_reconciliations = _merge_same_source_claim_fragments(
        atoms,
        model_inputs=model_inputs,
        existing_by_claim=existing_by_claim,
    )
    reconciliations.extend(same_source_reconciliations)
    atoms, memory_atom_repairs = _repair_single_atom_source_coverage(
        atoms,
        model_inputs=model_inputs,
        existing_by_claim=existing_by_claim,
    )
    result["memoryAtoms"] = atoms
    result["claimKeyReconciliations"] = reconciliations
    result["memoryAtomRepairs"] = memory_atom_repairs
    return result


def _merge_same_source_claim_fragments(
    atoms: list[dict[str, object]],
    *,
    model_inputs: list[dict[str, object]],
    existing_by_claim: Mapping[str, dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Keep a low-compatibility fragment from overwriting an unrelated slot."""

    source_text_by_events = {
        tuple(sorted(_positive_event_ids(item.get("sourceEventIds")))): compact_whitespace(
            str(item.get("text") or "")
        )
        for item in model_inputs
        if _positive_event_ids(item.get("sourceEventIds"))
    }
    indexes_by_events: dict[tuple[int, ...], list[int]] = {}
    for index, item in enumerate(atoms):
        event_key = tuple(sorted(_positive_event_ids(item.get("sourceEventIds"))))
        if event_key:
            indexes_by_events.setdefault(event_key, []).append(index)

    consumed: set[int] = set()
    reconciliations: list[dict[str, object]] = []
    for event_key, indexes in indexes_by_events.items():
        if len(indexes) < 2:
            continue
        source_text = source_text_by_events.get(event_key, "")
        scored: list[tuple[int, str, float, dict[str, object]]] = []
        for index in indexes:
            item = atoms[index]
            claim_key = compact_whitespace(str(item.get("claimKey") or ""))
            existing = existing_by_claim.get(claim_key)
            if existing is None:
                continue
            canonical = compact_whitespace(
                str(item.get("canonicalText") or item.get("text") or "")
            )
            score = _existing_memory_text_score(
                canonical,
                " ".join(
                    (
                        compact_whitespace(
                            str(
                                existing.get("canonicalText")
                                or existing.get("text")
                                or ""
                            )
                        ),
                        claim_key,
                    )
                ),
            )
            scored.append((index, claim_key, score, existing))
        anchors = [
            item for item in scored if item[2] >= MIN_SAME_SOURCE_ANCHOR_SCORE
        ]
        if not anchors:
            continue
        anchor_index, anchor_claim, _anchor_atom_score, anchor_existing = max(
            anchors,
            key=lambda item: (
                _existing_memory_text_score(
                    source_text,
                    " ".join(
                        (
                            compact_whitespace(
                                str(
                                    item[3].get("canonicalText")
                                    or item[3].get("text")
                                    or ""
                                )
                            ),
                            item[1],
                        )
                    ),
                ),
                item[2],
                item[1],
            ),
        )
        anchor_source_score = _existing_memory_text_score(
            source_text,
            " ".join(
                (
                    compact_whitespace(
                        str(
                            anchor_existing.get("canonicalText")
                            or anchor_existing.get("text")
                            or ""
                        )
                    ),
                    anchor_claim,
                )
            ),
        )
        anchor = atoms[anchor_index]
        for index, claim_key, score, existing in scored:
            if (
                index == anchor_index
                or claim_key == anchor_claim
                or score >= MIN_EXISTING_CLAIM_COMPATIBILITY_SCORE
                or anchor_source_score - score < MIN_CLAIM_REUSE_MARGIN
            ):
                continue
            fragment = atoms[index]
            anchor_text = compact_whitespace(
                str(anchor.get("canonicalText") or anchor.get("text") or "")
            )
            fragment_text = compact_whitespace(
                str(fragment.get("canonicalText") or fragment.get("text") or "")
            )
            if fragment_text and normalize_text(fragment_text) not in normalize_text(
                anchor_text
            ):
                anchor["canonicalText"] = "；".join(
                    value for value in (anchor_text.rstrip("；。"), fragment_text) if value
                )
            anchor["sourceEventIds"] = sorted(
                _positive_event_ids(anchor.get("sourceEventIds"))
                | _positive_event_ids(fragment.get("sourceEventIds"))
            )
            anchor["tags"] = list(
                dict.fromkeys(
                    [
                        *[str(value) for value in anchor.get("tags") or []],
                        *[str(value) for value in fragment.get("tags") or []],
                    ]
                )
            )
            consumed.add(index)
            reconciliations.append(
                {
                    "fromClaimKey": claim_key,
                    "toClaimKey": anchor_claim,
                    "targetAtomId": compact_whitespace(
                        str(anchor_existing.get("atomId") or "")
                    ),
                    "score": round(anchor_source_score, 4),
                    "runnerUpScore": round(score, 4),
                    "reason": "same_source_low_compatibility_merged_into_anchor",
                }
            )
    return [item for index, item in enumerate(atoms) if index not in consumed], reconciliations


def _repair_single_atom_source_coverage(
    atoms: list[dict[str, object]],
    *,
    model_inputs: list[dict[str, object]],
    existing_by_claim: Mapping[str, dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Use a concise authoritative update when model compression drops a clause."""

    atoms_by_events: dict[tuple[int, ...], list[dict[str, object]]] = {}
    for atom in atoms:
        event_key = tuple(sorted(_positive_event_ids(atom.get("sourceEventIds"))))
        if event_key:
            atoms_by_events.setdefault(event_key, []).append(atom)
    repairs: list[dict[str, object]] = []
    for model_input in model_inputs:
        event_key = tuple(
            sorted(_positive_event_ids(model_input.get("sourceEventIds")))
        )
        grouped_atoms = atoms_by_events.get(event_key, [])
        if len(grouped_atoms) != 1:
            continue
        atom = grouped_atoms[0]
        claim_key = compact_whitespace(str(atom.get("claimKey") or ""))
        source_text = compact_whitespace(str(model_input.get("text") or ""))
        canonical = compact_whitespace(
            str(atom.get("canonicalText") or atom.get("text") or "")
        )
        clauses = split_sentences(source_text)
        if (
            claim_key not in existing_by_claim
            or not source_text
            or not canonical
            or len(source_text) > 280
            or len(clauses) < 2
            or len(clauses) > 3
            or contains_sensitive_content(source_text)
            or _CLAIM_UPDATE_SIGNAL_RE.search(source_text) is None
        ):
            continue
        missing_clauses = [
            clause
            for clause in clauses
            if _existing_memory_text_score(clause, canonical)
            < MIN_EXISTING_CLAIM_COMPATIBILITY_SCORE
        ]
        if not missing_clauses:
            continue
        atom["canonicalText"] = source_text
        repairs.append(
            {
                "claimKey": claim_key,
                "sourceEventIds": list(event_key),
                "missingClauseCount": len(missing_clauses),
                "reason": "concise_existing_claim_source_clause_restored",
            }
        )
    return atoms, repairs


def _add_recall_probe_score(
    scores: dict[str, dict[int, float]],
    item_id: str,
    probe_index: int,
    score: float,
) -> None:
    if not item_id or score <= 0.0:
        return
    per_probe = scores.setdefault(item_id, {})
    per_probe[probe_index] = max(per_probe.get(probe_index, 0.0), float(score))


def _rank_recalled_ids(
    scores: dict[str, dict[int, float]],
    *,
    rows: dict[str, sqlite3.Row],
    limit: int,
) -> list[str]:
    return [
        item_id
        for item_id, _ in sorted(
            scores.items(),
            key=lambda item: (
                -(sum(item[1].values()) + max(0, len(item[1]) - 1) * 0.35),
                -int(rows[item[0]]["updated_at_ms"] or 0),
                item[0],
            ),
        )[: max(0, int(limit))]
        if item_id in rows
    ]


def _rank_recalled_ids_with_probe_coverage(
    scores: dict[str, dict[int, float]],
    *,
    coverage_scores: dict[str, dict[int, float]],
    rows: dict[str, sqlite3.Row],
    limit: int,
) -> list[str]:
    """Reserve one direct old-fact hit per probe before global reranking."""

    bounded_limit = max(0, int(limit))
    selected: list[str] = []
    selected_set: set[str] = set()
    probe_indexes = sorted(
        {
            probe_index
            for per_probe in coverage_scores.values()
            for probe_index in per_probe
        }
    )
    for probe_index in probe_indexes:
        candidates = [
            (item_id, per_probe[probe_index])
            for item_id, per_probe in coverage_scores.items()
            if probe_index in per_probe and item_id in rows
        ]
        if not candidates:
            continue
        uncovered_candidates = [
            item for item in candidates if item[0] not in selected_set
        ]
        item_id, _ = min(
            uncovered_candidates or candidates,
            key=lambda item: (
                -item[1],
                -int(rows[item[0]]["updated_at_ms"] or 0),
                item[0],
            ),
        )
        if item_id not in selected_set:
            selected.append(item_id)
            selected_set.add(item_id)
        if len(selected) >= bounded_limit:
            return selected

    for item_id in _rank_recalled_ids(scores, rows=rows, limit=bounded_limit):
        if item_id in selected_set:
            continue
        selected.append(item_id)
        selected_set.add(item_id)
        if len(selected) >= bounded_limit:
            break
    return selected


def _coalesce_owner_inputs(
    raw_inputs: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Project low-level Rime commits into one model-facing user utterance."""

    projected: list[dict[str, object]] = []
    fragment_run: list[dict[str, object]] = []

    def flush_fragments() -> None:
        if not fragment_run:
            return
        projected.append(collapse_rime_fragment_run(fragment_run))
        fragment_run.clear()

    for item in raw_inputs:
        if str(item.get("source") or "") == "squirrel_rime_commit_burst":
            if fragment_run:
                previous_state = compact_whitespace(
                    str(fragment_run[-1].get("evidenceAdmissionState") or "")
                )
                current_state = compact_whitespace(
                    str(item.get("evidenceAdmissionState") or "")
                )
                if (
                    previous_state
                    and current_state
                    and previous_state != current_state
                ) or not rime_fragments_belong_together(fragment_run[-1], item):
                    # A logical reconstruction is also the unit of Evidence
                    # adjudication. Never coalesce physical events that have
                    # already diverged into different admission states.
                    flush_fragments()
            fragment_run.append(item)
            continue
        flush_fragments()
        projected.append(dict(item))
    flush_fragments()

    result: list[dict[str, object]] = []
    for index, item in enumerate(projected, start=1):
        source_ids = _input_source_ids(item)
        if not source_ids:
            continue
        result.append(
            {
                **item,
                "sourceRef": f"S{index}",
                # The cursor uses the last physical source in a reconstructed
                # utterance, while review/apply keeps every source id.
                "sourceId": source_ids[-1],
                "sourceIds": source_ids,
            }
        )
    return result


def _bounded_external_model_inputs(
    inputs: list[dict[str, object]],
    *,
    limit: int,
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    external_count = 0
    for item in inputs:
        if _is_agent_curated_external_source(item):
            if external_count >= MAX_EXTERNAL_MODEL_INPUTS_PER_RUN:
                break
            external_count += 1
        result.append(item)
        if len(result) >= limit:
            break
    return result


def _bounded_owner_model_inputs(
    inputs: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Bound one organizer call by both logical sources and likely facts."""

    result: list[dict[str, object]] = []
    estimated_atom_count = 0
    for item in inputs:
        clause_count = len(_existing_memory_recall_probes([item]))
        atom_demand = min(
            MAX_OWNER_MEMORY_ATOMS_PER_RUN,
            max(1, clause_count),
        )
        if result and (
            len(result) >= MAX_OWNER_MODEL_INPUTS_PER_RUN
            or estimated_atom_count + atom_demand
            > MAX_OWNER_MEMORY_ATOMS_PER_RUN
        ):
            break
        result.append(item)
        estimated_atom_count += atom_demand
        if (
            len(result) >= MAX_OWNER_MODEL_INPUTS_PER_RUN
            or estimated_atom_count >= MAX_OWNER_MEMORY_ATOMS_PER_RUN
        ):
            break
    return result


def _bounded_personal_v2_model_inputs(
    inputs: list[dict[str, object]],
    *,
    window_ms: int = MAX_PERSONAL_V2_WINDOW_MS,
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    estimated_tokens = 0
    window_start_ms = 0
    for item in inputs[:MAX_PERSONAL_V2_SOURCES]:
        occurred_at_ms = int(
            item.get("sourceOccurredAtMs") or item.get("createdAtMs") or 0
        )
        if not window_start_ms:
            window_start_ms = occurred_at_ms
        if occurred_at_ms > window_start_ms + max(60_000, int(window_ms)):
            break
        item_tokens = _personal_v2_input_tokens(item)
        if result and estimated_tokens + item_tokens > MAX_PERSONAL_V2_INPUT_TOKENS:
            break
        if item_tokens > MAX_PERSONAL_V2_INPUT_TOKENS:
            raise ValueError("one personal Evidence expression exceeds the curation budget")
        result.append(item)
        estimated_tokens += item_tokens
    return result


def _fit_personal_v2_inputs_to_catalog(
    inputs: list[dict[str, object]],
    *,
    existing_memory_context: Mapping[str, object],
    context_only: object,
) -> list[dict[str, object]]:
    catalog_tokens = sum(
        _estimate_personal_v2_tokens(
            " ".join(
                (
                    compact_whitespace(str(item.get("claimKey") or "")),
                    compact_whitespace(
                        str(item.get("canonicalText") or item.get("text") or "")
                    ),
                )
            )
        )
        + 24
        for item in existing_memory_context.get("existingMemoryAtoms") or []
        if isinstance(item, Mapping)
    )
    context_tokens = sum(
        _estimate_personal_v2_tokens(str(item.get("text") or "")) + 12
        for item in context_only if isinstance(item, Mapping)
    ) if isinstance(context_only, list) else 0
    evidence_budget = MAX_PERSONAL_V2_INPUT_TOKENS - catalog_tokens - context_tokens - 2_000
    if evidence_budget < 1_000:
        raise ValueError(
            "complete current personal Atom catalog leaves no safe Evidence budget"
        )
    result: list[dict[str, object]] = []
    used = 0
    for item in inputs:
        cost = _personal_v2_input_tokens(item)
        if not result and cost > evidence_budget:
            raise ValueError("one complete Evidence expression exceeds the remaining curation budget")
        if result and used + cost > evidence_budget:
            break
        result.append(item)
        used += cost
    return result


def _personal_v2_input_tokens(item: Mapping[str, object]) -> int:
    text = compact_whitespace(str(item.get("text") or ""))
    decision = item.get("decisionContext")
    if isinstance(decision, Mapping):
        text += json.dumps(dict(decision), ensure_ascii=False, separators=(",", ":"))
    return _estimate_personal_v2_tokens(text) + 32


def _estimate_personal_v2_tokens(value: str) -> int:
    text = str(value or "")
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    return cjk + max(1, (len(text) - cjk + 3) // 4)


def _is_agent_curated_external_source(item: Mapping[str, object]) -> bool:
    provider = compact_whitespace(str(item.get("externalProvider") or ""))
    metadata_tags = item.get("sourceMetadataTags")
    tags = {
        compact_whitespace(str(value)).casefold()
        for value in metadata_tags
        if compact_whitespace(str(value))
    } if isinstance(metadata_tags, list) else set()
    return (
        str(item.get("sourceKind") or "") == "session_digest"
        and (
            bool(provider)
            or "external-memory" in tags
            or "agent-curated" in tags
        )
    )


def _input_source_ids(item: Mapping[str, object]) -> list[str]:
    candidates = item.get("sourceIds")
    values = candidates if isinstance(candidates, list) else [item.get("sourceId")]
    return list(
        dict.fromkeys(
            compact_whitespace(str(value or ""))
            for value in values
            if compact_whitespace(str(value or ""))
        )
    )


def _input_evidence_ids(item: Mapping[str, object]) -> list[str]:
    candidates = item.get("evidenceIds")
    values = candidates if isinstance(candidates, list) else [item.get("evidenceId")]
    return list(
        dict.fromkeys(
            compact_whitespace(str(value or ""))
            for value in values
            if compact_whitespace(str(value or ""))
        )
    )


def _all_input_source_ids(inputs: list[dict[str, object]]) -> list[str]:
    return list(
        dict.fromkeys(
            source_id
            for item in inputs
            for source_id in _input_source_ids(item)
        )
    )


def _positive_event_ids(value: object) -> set[int]:
    values = value if isinstance(value, list) else []
    return {
        int(item)
        for item in values
        if str(item).isdigit() and int(item) > 0
    }


def _looks_like_standalone_question(text: str) -> bool:
    normalized = compact_whitespace(text)
    if not normalized or _DURABLE_ASSERTION_RE.search(normalized):
        return False
    return bool(
        _QUESTION_SIGNAL_RE.search(normalized)
        and (
            normalized.endswith(("?", "？"))
            or len(normalized) <= 80
        )
    )


def _durable_atom_rejection_reason(
    canonical: str,
    *,
    kind: str,
    source_texts: list[str],
    allow_verbatim_existing_claim: bool = False,
) -> str:
    if kind not in _DURABLE_ATOM_KINDS:
        return "non_durable_atom_kind"
    if _DERIVED_PROTOCOL_NOISE_RE.search(canonical):
        return "workflow_protocol_noise"
    if canonical.endswith(("?", "？")) or _looks_like_standalone_question(canonical):
        return "standalone_question"
    normalized = normalize_text(canonical)
    if not allow_verbatim_existing_claim and len(canonical) >= 48 and any(
        normalized == normalize_text(source_text)
        for source_text in source_texts
    ):
        return "verbatim_long_source"
    return ""


def _derived_summary_rejection_reason(summary: str) -> str:
    if not summary:
        return "empty_summary"
    if contains_sensitive_content(summary):
        return "sensitive_content"
    if _DERIVED_PROTOCOL_NOISE_RE.search(summary):
        return "workflow_protocol_noise"
    return ""


def _expanded_logical_atom_sources(
    source_event_ids: object,
    *,
    model_inputs: list[dict[str, object]],
    legal_event_ids: set[int],
) -> tuple[list[int], list[str]]:
    selected = _positive_event_ids(source_event_ids).intersection(legal_event_ids)
    if not selected:
        return [], []
    expanded: set[int] = set()
    source_texts: list[str] = []
    for model_input in model_inputs:
        logical_ids = _positive_event_ids(model_input.get("sourceEventIds")).intersection(
            legal_event_ids
        )
        if not selected.intersection(logical_ids):
            continue
        expanded.update(logical_ids)
        text = compact_whitespace(str(model_input.get("text") or ""))
        if text:
            source_texts.append(text)
    return sorted(expanded), source_texts


def _is_durable_candidate_input(item: Mapping[str, object]) -> bool:
    source_kind = compact_whitespace(str(item.get("sourceKind") or ""))
    if _input_evidence_ids(item):
        return source_kind in {"user_final", "explicit_memory"}
    if source_kind == "explicit_memory" or _is_agent_curated_external_source(item):
        return True
    return source_kind == "user_final" and any(
        isinstance(hint, Mapping)
        and compact_whitespace(str(hint.get("claim") or ""))
        for hint in item.get("captureHints") or []
    )


def _origin_tags_for_event_ids(
    source_event_ids: object,
    *,
    model_inputs: list[dict[str, object]],
) -> list[str]:
    selected = _positive_event_ids(source_event_ids)
    if not selected:
        return []
    tags: list[str] = []
    for model_input in model_inputs:
        input_ids = _positive_event_ids(model_input.get("sourceEventIds"))
        if not selected.intersection(input_ids):
            continue
        metadata_tags = {
            compact_whitespace(str(value)).casefold()
            for value in model_input.get("sourceMetadataTags") or []
            if compact_whitespace(str(value))
        }
        provider = compact_whitespace(
            str(model_input.get("externalProvider") or "")
        ).casefold()
        if provider == "codex" or "codex" in metadata_tags:
            tags.extend(["Codex", "external-memory"])
    return list(dict.fromkeys(tags))


def _owner_atom_event_ids_by_capacity(
    compile_output: Mapping[str, object],
    *,
    model_inputs: list[dict[str, object]],
    existing_claim_keys: set[str],
) -> tuple[set[int], set[int]]:
    legal_event_ids = {
        event_id
        for model_input in model_inputs
        if _is_durable_candidate_input(model_input)
        for event_id in _positive_event_ids(model_input.get("sourceEventIds"))
    }
    eligible: set[int] = set()
    over_capacity: set[int] = set()
    personal_v2 = (
        compact_whitespace(
            str(dict(compile_output.get("personalCurationV2") or {}).get("protocol") or "")
        )
        == "personal-v2"
    )
    for atom_index, item in enumerate(
        list(compile_output.get("memoryAtoms") or [])
    ):
        if not isinstance(item, Mapping):
            continue
        canonical = compact_whitespace(
            str(item.get("canonicalText") or item.get("text") or "")
        )
        source_ids, source_texts = _expanded_logical_atom_sources(
            item.get("sourceEventIds"),
            model_inputs=model_inputs,
            legal_event_ids=legal_event_ids,
        )
        if not canonical or contains_sensitive_content(canonical) or not source_ids:
            continue
        if _durable_atom_rejection_reason(
            canonical,
            kind=compact_whitespace(str(item.get("kind") or "")),
            source_texts=source_texts,
            allow_verbatim_existing_claim=(
                compact_whitespace(str(item.get("claimKey") or ""))
                in existing_claim_keys
            ),
        ):
            continue
        target = (
            eligible
            if personal_v2 or atom_index < MAX_OWNER_MEMORY_ATOMS_PER_RUN
            else over_capacity
        )
        target.update(source_ids)
    return eligible, over_capacity


def _owner_topic_terms(*values: object) -> set[str]:
    text = " ".join(compact_whitespace(str(value or "")) for value in values)
    normalized = normalize_text(text)
    terms = {
        normalize_text(term)
        for term in token_terms(text, max_terms=80)
        if normalize_text(term)
    }
    cjk = "".join(re.findall(r"[\u3400-\u9fff]", normalized))
    if len(cjk) == 1:
        terms.add(cjk)
    elif len(cjk) >= 2:
        terms.update(cjk[index : index + 2] for index in range(len(cjk) - 1))
    return terms


def _owner_topic_similarity(
    proposed: Mapping[str, object],
    existing: Mapping[str, object],
) -> float:
    proposed_title = normalize_text(str(proposed.get("title") or ""))
    existing_title = normalize_text(str(existing.get("title") or ""))
    if proposed_title and proposed_title == existing_title:
        return 1.0
    if _GENERIC_OWNER_BOOK_TITLE_RE.fullmatch(
        compact_whitespace(str(existing.get("title") or ""))
    ):
        return 0.0
    proposed_terms = _owner_topic_terms(
        proposed.get("title"),
        *(proposed.get("tags") if isinstance(proposed.get("tags"), list) else []),
    )
    existing_terms = _owner_topic_terms(
        existing.get("title"),
        *(existing.get("tags") if isinstance(existing.get("tags"), list) else []),
    )
    if not proposed_terms or not existing_terms:
        return 0.0
    overlap = len(proposed_terms & existing_terms) / len(
        proposed_terms | existing_terms
    )
    if proposed_title and existing_title and (
        proposed_title in existing_title or existing_title in proposed_title
    ):
        overlap = max(overlap, 0.82)
    return overlap


def _match_owner_topic_book(
    proposed: Mapping[str, object],
    *,
    existing_books: list[dict[str, object]],
    used_book_ids: set[str],
) -> dict[str, object] | None:
    """Reuse one stable Book identity, including aliases and old redirects.

    An explicit physical ``bookId`` is an identity assertion, not a term for
    fuzzy matching.  Scope fields supplied by the caller are checked at every
    redirect hop so a stale alias cannot cross an owner or authorization
    boundary.
    """

    requested_id = compact_whitespace(str(proposed.get("bookId") or ""))
    requested_key = compact_whitespace(str(proposed.get("bookKey") or ""))
    requested_terms = {
        normalize_text(str(value or ""))
        for value in (
            proposed.get("title"),
            proposed.get("bookAlias"),
            proposed.get("topicAlias"),
            *(proposed.get("aliases") if isinstance(proposed.get("aliases"), list) else []),
        )
        if normalize_text(str(value or ""))
    }
    scope_fields = (
        "ownerKind",
        "ownerId",
        "project",
        "app",
        "knowledgeDomain",
        "scopeKind",
        "scopeId",
        "visibility",
        "authorizationRevision",
        "bindingId",
        "scopeMode",
    )
    by_id = {
        compact_whitespace(str(item.get("bookId") or "")): item
        for item in existing_books
        if compact_whitespace(str(item.get("bookId") or ""))
    }

    def compatible(candidate: Mapping[str, object]) -> bool:
        # Missing fields in older model proposals are filled by the governed
        # caller; do not make an old read-only proposal unusable.  Whenever a
        # scope field is present, however, an absent/different candidate is a
        # hard mismatch (including project="" versus a named project).
        for field in scope_fields:
            if field not in proposed or proposed.get(field) is None:
                continue
            requested = compact_whitespace(str(proposed.get(field) or ""))
            candidate_value = compact_whitespace(str(candidate.get(field) or ""))
            if requested != candidate_value:
                return False
        return True

    def resolved(candidate: Mapping[str, object]) -> dict[str, object] | None:
        current = dict(candidate)
        visited: set[str] = set()
        while True:
            if not compatible(current):
                return None
            current_id = compact_whitespace(str(current.get("bookId") or ""))
            if not current_id or current_id in visited:
                return None
            visited.add(current_id)
            if compact_whitespace(str(current.get("status") or "")) != "superseded":
                break
            redirect_id = compact_whitespace(
                str(current.get("supersededByBookId") or current.get("resolvedBookId") or "")
            )
            if not redirect_id or redirect_id in visited:
                return None
            current = dict(by_id.get(redirect_id) or {})
            if not current:
                return None
        resolved_id = compact_whitespace(str(current.get("bookId") or ""))
        if not resolved_id or resolved_id in used_book_ids:
            return None
        return current

    def unique_resolved(candidates: list[Mapping[str, object]]) -> dict[str, object] | None:
        matches: dict[str, dict[str, object]] = {}
        for candidate in candidates:
            match = resolved(candidate)
            if match is not None:
                matches.setdefault(str(match.get("bookId") or ""), match)
        if len(matches) == 1:
            return next(iter(matches.values()))
        return None

    # Identity precedence is deliberate: a conflicting title/alias must not
    # turn an explicit ID into a new Book or an ambiguous result.
    if requested_id:
        candidate = by_id.get(requested_id)
        return resolved(candidate) if candidate is not None else None
    if requested_key:
        match = unique_resolved(
            [
                candidate
                for candidate in existing_books
                if compact_whitespace(str(candidate.get("bookKey") or "")) == requested_key
            ]
        )
        if match is not None:
            return match
        if any(
            compact_whitespace(str(candidate.get("bookKey") or "")) == requested_key
            for candidate in existing_books
        ):
            return None

    exact_candidates = [
        candidate
        for candidate in existing_books
        if requested_terms.intersection(
            {
                normalize_text(str(candidate.get("title") or "")),
                normalize_text(str(candidate.get("bookKey") or "")),
                *{
                    normalize_text(str(value or ""))
                    for value in candidate.get("aliases") or []
                    if value is not None and normalize_text(str(value or ""))
                },
            }
        )
    ]
    if exact_candidates:
        # An alias that resolves to multiple live Books is ambiguous and must
        # not silently choose one by similarity.
        return unique_resolved(exact_candidates)

    candidates_by_resolved_id: dict[str, dict[str, object]] = {}
    for candidate in existing_books:
        match = resolved(candidate)
        if match is not None:
            candidates_by_resolved_id.setdefault(str(match.get("bookId") or ""), match)
    candidates = list(candidates_by_resolved_id.values())
    ranked = sorted(
        (
            (_owner_topic_similarity(proposed, candidate), candidate)
            for candidate in candidates
        ),
        key=lambda pair: (-pair[0], str(pair[1].get("bookId") or "")),
    )
    if not ranked or ranked[0][0] < 0.42:
        return None
    if (
        len(ranked) > 1
        and ranked[0][0] - ranked[1][0] < 0.08
        and ranked[0][0] < 0.82
    ):
        return None
    return ranked[0][1]


def _govern_owner_compile_output(
    compile_output: Mapping[str, object],
    *,
    bundle: Mapping[str, object],
    owner_kind: str,
    owner_id: str,
) -> dict[str, object]:
    result = dict(compile_output)
    legal_event_ids = {
        int(value)
        for value in bundle.get("legalSourceEventIds") or []
        if str(value).isdigit() and int(value) > 0
    }
    remembered_refs = {
        str(item.get("sourceRef") or "")
        for item in compile_output.get("sourceDecisions") or []
        if isinstance(item, dict)
        and str(item.get("disposition") or "") == "remember"
        and _bounded_float(item.get("confidence"), default=0.0) >= 0.55
    }
    remembered_event_ids = {
        int(event_id)
        for item in bundle.get("inputs") or []
        if isinstance(item, dict) and str(item.get("sourceRef") or "") in remembered_refs
        for event_id in item.get("sourceEventIds") or []
        if str(event_id).isdigit() and int(event_id) in legal_event_ids
    }
    personal_metadata = dict(compile_output.get("personalCurationV2") or {})
    personal_v2 = (
        compact_whitespace(str(personal_metadata.get("protocol") or ""))
        == "personal-v2"
    )
    atom_first = (
        compact_whitespace(str(compile_output.get("curationArchitecture") or ""))
        == MEMORY_CURATION_ARCHITECTURE
        or compact_whitespace(
            str(personal_metadata.get("curationArchitecture") or "")
        )
        == MEMORY_CURATION_ARCHITECTURE
    )
    personal_only = personal_v2 and not atom_first
    project = (
        ""
        if personal_only
        else compact_whitespace(str(bundle.get("project") or ""))
    )
    owner_hash = hashlib.sha256(
        f"{owner_kind}\0{owner_id}\0{project}".encode("utf-8")
    ).hexdigest()[:16]
    bundle_inputs = [
        dict(item)
        for item in bundle.get("inputs") or []
        if isinstance(item, dict)
    ]
    existing_atoms = [
        dict(item)
        for item in bundle.get("existingMemoryAtoms") or []
        if isinstance(item, dict)
    ]
    existing_atoms_by_id = {
        compact_whitespace(str(item.get("atomId") or "")): item
        for item in existing_atoms
        if compact_whitespace(str(item.get("atomId") or ""))
    }
    existing_atoms_by_claim_key = {
        compact_whitespace(str(item.get("claimKey") or "")): item
        for item in existing_atoms
        if compact_whitespace(str(item.get("claimKey") or ""))
    }
    atoms: list[dict[str, object]] = []
    atom_ids: list[str] = []
    governed_atom_id_by_proposed_id: dict[str, str] = {}
    legal_evidence_ids = {
        compact_whitespace(str(value or ""))
        for value in bundle.get("legalEvidenceIds") or []
        if compact_whitespace(str(value or ""))
    }
    evidence_ids_by_event_id: dict[int, set[str]] = {}
    for source_input in bundle_inputs:
        if compact_whitespace(str(source_input.get("sourceRef") or "")) not in remembered_refs:
            continue
        source_evidence_ids = {
            value
            for value in _input_evidence_ids(source_input)
            if value in legal_evidence_ids
        }
        for event_id in _positive_event_ids(source_input.get("sourceEventIds")):
            evidence_ids_by_event_id.setdefault(event_id, set()).update(
                source_evidence_ids
            )
    proposed_atoms = list(compile_output.get("memoryAtoms") or [])
    if not personal_v2:
        proposed_atoms = proposed_atoms[:MAX_OWNER_MEMORY_ATOMS_PER_RUN]
    for item in proposed_atoms:
        if not isinstance(item, dict):
            continue
        canonical = compact_whitespace(
            str(item.get("canonicalText") or item.get("text") or "")
        )
        summary = compact_whitespace(str(item.get("summary") or ""))[:500]
        source_ids, source_texts = _expanded_logical_atom_sources(
            item.get("sourceEventIds"),
            model_inputs=bundle_inputs,
            legal_event_ids=remembered_event_ids,
        )
        claim_key = compact_whitespace(str(item.get("claimKey") or ""))[:120]
        existing_claim = existing_atoms_by_claim_key.get(claim_key)
        kind = compact_whitespace(
            str(
                (existing_claim or {}).get("kind")
                or item.get("kind")
                or ""
            )
        )
        personal_atom = kind in _PERSONAL_ATOM_KINDS
        atom_project = "" if personal_atom else project
        atom_app = (
            ""
            if personal_atom
            else compact_whitespace(str(item.get("app") or ""))
        )
        atom_owner_hash = hashlib.sha256(
            f"{owner_kind}\0{owner_id}\0{atom_project}".encode("utf-8")
        ).hexdigest()[:16]
        if (
            not canonical
            or contains_sensitive_content(canonical)
            or not source_ids
            or _durable_atom_rejection_reason(
                canonical,
                kind=kind,
                source_texts=source_texts,
                allow_verbatim_existing_claim=existing_claim is not None,
            )
        ):
            continue
        operation = compact_whitespace(str(item.get("operation") or ""))
        atom_id = (
            compact_whitespace(str((existing_claim or {}).get("atomId") or ""))
            if operation in {"attach", "update", "merge"}
            and existing_claim is not None
            else (
                f"atom:{atom_owner_hash}:"
                f"{stable_text_hash(normalize_text(canonical)).removeprefix('sha256:')[:24]}"
            )
        )
        if not claim_key:
            claim_digest = stable_text_hash(normalize_text(canonical)).removeprefix(
                "sha256:"
            )[:8]
            claim_key = f"owner:{atom_owner_hash[:8]}:{kind}:{claim_digest}"
        origin_tags = _origin_tags_for_event_ids(
            source_ids,
            model_inputs=bundle_inputs,
        )
        supported_evidence_ids = {
            evidence_id
            for event_id in source_ids
            for evidence_id in evidence_ids_by_event_id.get(event_id, set())
        }
        requested_evidence_ids = {
            compact_whitespace(str(value or ""))
            for value in item.get("evidenceIds") or []
            if compact_whitespace(str(value or ""))
        }
        atom_evidence_ids = sorted(
            supported_evidence_ids.intersection(requested_evidence_ids)
            if requested_evidence_ids
            else supported_evidence_ids
        )
        if personal_v2 and not atom_evidence_ids:
            continue
        proposed_atom_id = compact_whitespace(str(item.get("atomId") or ""))
        if proposed_atom_id:
            governed_atom_id_by_proposed_id[proposed_atom_id] = atom_id
        atom_ids.append(atom_id)
        atoms.append(
            {
                **item,
                "atomId": atom_id,
                "canonicalText": canonical,
                "kind": kind,
                "claimKey": claim_key,
                "lineageId": compact_whitespace(
                    str(
                        (existing_claim or {}).get("lineageId")
                        or item.get("lineageId")
                        or ""
                    )
                ),
                "summary": (
                    ""
                    if contains_sensitive_content(summary)
                    else summary
                ),
                "sourceEventIds": source_ids,
                "tags": (
                    list(
                        dict.fromkeys(
                            [
                                *[
                                    compact_whitespace(str(value))
                                    for value in item.get("tags") or []
                                    if compact_whitespace(str(value))
                                ],
                                *origin_tags,
                            ]
                        )
                    )[:12]
                    if personal_v2
                    else origin_tags
                ),
                "semanticGroupIds": [],
                "directCandidateAllowed": False,
                "ownerKind": owner_kind,
                "ownerId": owner_id,
                "project": atom_project,
                "app": atom_app,
                "evidenceIds": atom_evidence_ids,
                "curationArchitecture": (
                    MEMORY_CURATION_ARCHITECTURE if atom_first else ""
                ),
                "knowledgeDomain": (
                    "personal_memory" if personal_atom else "legacy"
                ),
                "scopeKind": "user" if personal_atom else "legacy",
                "scopeId": "default" if personal_atom else "",
                "visibility": "private" if personal_atom else "legacy",
                "authorizationRevision": (
                    "memory-atom-v2" if personal_atom else ""
                ),
                "bindingId": (
                    "personal-memory:user:default" if personal_atom else ""
                ),
                "scopeMode": "authoritative" if personal_atom else "legacy",
            }
        )

    retractions: list[dict[str, object]] = []
    for item in compile_output.get("memoryRetractions") or []:
        if not isinstance(item, Mapping):
            continue
        target_id = compact_whitespace(str(item.get("targetAtomId") or ""))
        target = existing_atoms_by_id.get(target_id)
        if target is None:
            continue
        confidence = _bounded_float(item.get("confidence"), default=0.0)
        if confidence < 0.9:
            continue
        source_ids, _source_texts = _expanded_logical_atom_sources(
            item.get("sourceEventIds"),
            model_inputs=bundle_inputs,
            legal_event_ids=legal_event_ids,
        )
        explicit_event_ids = {
            event_id
            for model_input in bundle_inputs
            if compact_whitespace(str(model_input.get("sourceKind") or ""))
            == "user_final"
            and _explicit_forget_matches_atom(
                str(model_input.get("text") or ""),
                canonical_text=str(target.get("canonicalText") or ""),
            )
            for event_id in _positive_event_ids(
                model_input.get("sourceEventIds")
            )
            if event_id in source_ids
        }
        source_ids = sorted(explicit_event_ids)
        if not source_ids:
            continue
        retractions.append(
            {
                **item,
                "targetAtomId": target_id,
                "reason": compact_whitespace(
                    str(item.get("reason") or "explicit_user_forget")
                )[:240],
                "sourceEventIds": source_ids,
                "confidence": confidence,
                "ownerKind": owner_kind,
                "ownerId": owner_id,
                "project": project,
            }
        )
        if not personal_v2 and len(retractions) >= 4:
            break

    retracted_ids = {
        str(item["targetAtomId"])
        for item in retractions
    }
    replaced_claim_keys = {
        compact_whitespace(str(item.get("claimKey") or ""))
        for item in atoms
        if compact_whitespace(str(item.get("claimKey") or ""))
    }
    retained_existing_atoms = [
        item
        for item in existing_atoms
        if compact_whitespace(str(item.get("atomId") or ""))
        not in retracted_ids
        and (
            not compact_whitespace(str(item.get("claimKey") or ""))
            or compact_whitespace(str(item.get("claimKey") or ""))
            not in replaced_claim_keys
        )
    ]
    existing_books_by_id: dict[str, dict[str, object]] = {}
    for raw_book in [
        *(bundle.get("existingMemoryBookIndex") or []),
        *(bundle.get("existingMemoryBooks") or []),
        *(bundle.get("archivedMemoryBookGuards") or []),
    ]:
        if not isinstance(raw_book, dict):
            continue
        raw_book_id = compact_whitespace(str(raw_book.get("bookId") or ""))
        if raw_book_id:
            # The recalled body is richer than the identity index, but may not
            # repeat identity-only aliases/scope fields. Merge both views.
            previous_book = existing_books_by_id.get(raw_book_id, {})
            merged_book = {**previous_book, **dict(raw_book)}
            for list_field in ("aliases", "tags", "queryExpansions", "memoryAtomIds", "sourceEventIds", "semanticGroupIds"):
                if list_field in previous_book or list_field in raw_book:
                    previous_values = previous_book.get(list_field)
                    raw_values = raw_book.get(list_field)
                    previous_values = (
                        [] if previous_values is None
                        else previous_values if isinstance(previous_values, list)
                        else [previous_values]
                    )
                    raw_values = (
                        [] if raw_values is None
                        else raw_values if isinstance(raw_values, list)
                        else [raw_values]
                    )
                    merged_book[list_field] = list(
                        dict.fromkeys(
                            value
                            for value in [*previous_values, *raw_values]
                            if value is not None
                        )
                    )
            existing_books_by_id[raw_book_id] = merged_book
    existing_books = list(existing_books_by_id.values())
    proposed_book_limit = (
        0
        if personal_only
        else MAX_ATOM_FIRST_TOPIC_BOOKS_PER_RUN
        if atom_first
        else 3
    )
    proposed_books = [
        dict(item)
        for item in compile_output.get("topicBooks") or []
        if isinstance(item, dict)
    ][:proposed_book_limit]
    current_atom_by_id = {
        compact_whitespace(str(item.get("atomId") or "")): item
        for item in [*retained_existing_atoms, *atoms]
        if compact_whitespace(str(item.get("atomId") or ""))
    }
    current_atom_id_set = set(current_atom_by_id)
    books: list[dict[str, object]] = []
    used_existing_book_ids: set[str] = set()
    for proposed in proposed_books:
        proposed = dict(proposed)
        proposed_project = compact_whitespace(str(proposed.get("project") or ""))
        if proposed_project and proposed_project != project:
            # A model-provided project is an authorization assertion; never
            # rewrite it into the current run's project.
            continue
        proposed["project"] = project
        proposed["ownerKind"] = owner_kind
        proposed["ownerId"] = owner_id
        source_ids = _legal_ints(
            proposed.get("sourceEventIds"),
            remembered_event_ids,
        )
        proposed_member_candidates = []
        for value in proposed.get("memoryAtomIds") or []:
            proposed_member_id = compact_whitespace(str(value))
            member_id = governed_atom_id_by_proposed_id.get(
                proposed_member_id,
                proposed_member_id,
            )
            if member_id in current_atom_id_set:
                proposed_member_candidates.append(member_id)
        if not proposed_member_candidates:
            proposed_member_candidates = [
                atom_id
                for atom_id in atom_ids
                if set(
                    _positive_event_ids(
                        current_atom_by_id[atom_id].get("sourceEventIds")
                    )
                ).intersection(source_ids)
            ]
        scope_fields = (
            "knowledgeDomain",
            "scopeKind",
            "scopeId",
            "visibility",
            "authorizationRevision",
            "bindingId",
            "scopeMode",
        )
        candidate_scopes = {
            tuple(
                compact_whitespace(str(current_atom_by_id[atom_id].get(field) or ""))
                for field in scope_fields
            )
            for atom_id in proposed_member_candidates
        }
        candidate_scopes.discard(("", "", "", "", "", "", ""))
        if len(candidate_scopes) == 1:
            for field, value in zip(scope_fields, next(iter(candidate_scopes))):
                if not compact_whitespace(str(proposed.get(field) or "")):
                    proposed[field] = value
        title = compact_whitespace(str(proposed.get("title") or ""))[:120]
        proposed_summary = compact_whitespace(str(proposed.get("summary") or ""))
        if (
            not title
            or contains_sensitive_content(title)
            or _GENERIC_OWNER_BOOK_TITLE_RE.fullmatch(title)
        ):
            continue
        existing_book = _match_owner_topic_book(
            proposed,
            existing_books=existing_books,
            used_book_ids=used_existing_book_ids,
        )
        if (
            existing_book is not None
            and compact_whitespace(str(existing_book.get("status") or ""))
            == "archived"
        ):
            continue
        existing_book_id = compact_whitespace(
            str((existing_book or {}).get("bookId") or "")
        )
        if existing_book_id:
            used_existing_book_ids.add(existing_book_id)
        source_ids = _legal_ints(
            proposed.get("sourceEventIds"),
            remembered_event_ids,
        )
        explicit_member_ids = []
        for value in proposed.get("memoryAtomIds") or []:
            proposed_member_id = compact_whitespace(str(value))
            member_id = governed_atom_id_by_proposed_id.get(
                proposed_member_id,
                proposed_member_id,
            )
            if member_id in current_atom_id_set:
                explicit_member_ids.append(member_id)
        new_member_ids = (
            []
            if explicit_member_ids
            else [
                atom_id
                for atom_id in atom_ids
                if set(
                    _positive_event_ids(
                        current_atom_by_id[atom_id].get("sourceEventIds")
                    )
                ).intersection(source_ids)
            ]
        )
        retained_member_ids = [
            compact_whitespace(str(value))
            for value in (existing_book or {}).get("memoryAtomIds") or []
            if compact_whitespace(str(value)) in current_atom_id_set
        ]
        member_ids = list(
            dict.fromkeys(
                [*retained_member_ids, *explicit_member_ids, *new_member_ids]
            )
        )
        if not member_ids or not source_ids:
            continue
        if not proposed_summary or _derived_summary_rejection_reason(
            proposed_summary
        ):
            proposed_summary = "；".join(
                compact_whitespace(
                    str(current_atom_by_id[atom_id].get("canonicalText") or "")
                )
                for atom_id in member_ids
                if compact_whitespace(
                    str(current_atom_by_id[atom_id].get("canonicalText") or "")
                )
            )
        if not proposed_summary:
            continue
        if contains_sensitive_content(proposed_summary):
            continue
        topic_digest = stable_text_hash(normalize_text(title)).removeprefix(
            "sha256:"
        )[:16]
        book_id = existing_book_id or f"book:owner:{owner_hash}:topic:{topic_digest}"
        book_key = (
            compact_whitespace(str((existing_book or {}).get("bookKey") or ""))
            or f"owner-{owner_hash}-topic-{topic_digest}"
        )
        books.append(
            {
                **proposed,
                "bookId": book_id,
                "bookType": "topic",
                "bookKey": book_key,
                "title": title,
                "summary": proposed_summary[:2400],
                "sourceEventIds": source_ids,
                "memoryAtomIds": member_ids,
                "tags": list(
                    dict.fromkeys(
                        [
                            *[
                                compact_whitespace(str(value))
                                for value in proposed.get("tags") or []
                                if compact_whitespace(str(value))
                                and not contains_sensitive_content(value)
                            ],
                            *_origin_tags_for_event_ids(
                                source_ids,
                                model_inputs=bundle_inputs,
                            ),
                            *[
                                tag
                                for atom_id in member_ids
                                for tag in (
                                    current_atom_by_id[atom_id].get("tags")
                                    or []
                                )
                                if compact_whitespace(str(tag))
                            ],
                        ]
                    )
                )[:12],
                "queryExpansions": [
                    compact_whitespace(str(value))
                    for value in proposed.get("queryExpansions") or []
                    if compact_whitespace(str(value))
                    and not contains_sensitive_content(value)
                ][:16],
                "semanticGroupIds": [],
                "ownerKind": owner_kind,
                "ownerId": owner_id,
            }
        )

    result.update(
        {
            "dailyBooks": [],
            "topicBooks": books,
            "memoryAtoms": atoms,
            "semanticGroups": [],
            "semanticTags": [],
            "tagMerges": [],
            "tagEdges": [],
            "phraseCandidates": [],
            "negativePhrases": [],
            "supersedes": [],
            "memoryRetractions": retractions,
        }
    )
    return result


def _has_durable_memory(compile_output: Mapping[str, object]) -> bool:
    return bool(
        compile_output.get("topicBooks")
        or compile_output.get("memoryAtoms")
        or compile_output.get("memoryRetractions")
    )


def _explicit_forget_matches_atom(
    source_text: str,
    *,
    canonical_text: str,
) -> bool:
    request = compact_whitespace(source_text)
    canonical = compact_whitespace(canonical_text)
    if (
        not request
        or not canonical
        or _EXPLICIT_MEMORY_FORGET_RE.search(request) is None
    ):
        return False
    request_normalized = normalize_text(request)
    terms = [
        term
        for term in token_terms(canonical, max_terms=32)
        if len(term) >= 2
        and term not in {"用户", "项目", "记忆", "事实", "偏好", "当前"}
    ]
    return any(normalize_text(term) in request_normalized for term in terms)


def _has_active_capture_hint(item: Mapping[str, object]) -> bool:
    return any(
        isinstance(hint, Mapping)
        and compact_whitespace(str(hint.get("claim") or ""))
        for hint in item.get("captureHints") or []
    )


def _deterministic_disposition(item: Mapping[str, object]) -> str | None:
    source_kind = compact_whitespace(str(item.get("sourceKind") or ""))
    text = compact_whitespace(str(item.get("text") or ""))
    if source_kind == "tool_receipt" and _FAILED_TOOL_RECEIPT_RE.search(text):
        return "failed_tool_receipt"
    if source_kind == "tool_receipt" and _TRANSIENT_TOOL_RECEIPT_RE.search(text):
        return "transient_runtime_receipt"
    if source_kind == "tool_receipt":
        return "tool_receipt_not_durable"
    if source_kind == "session_compaction":
        return "session_artifact_audit_only"
    if source_kind == "session_digest":
        return None if _is_agent_curated_external_source(item) else "session_artifact_audit_only"
    if source_kind == "explicit_memory":
        return None
    if source_kind != "user_final":
        return "unsupported_memory_source"
    if not text:
        return "empty_input"
    if reason := memory_evidence_exclusion_reason(text):
        return reason
    if _FILLER_RE.fullmatch(text):
        return "input_noise_filler"
    if _REFERENTIAL_FRAGMENT_RE.fullmatch(text):
        return "referential_input_fragment"
    if len(text) <= 8 and _RANDOM_INPUT_RE.fullmatch(text):
        return "random_key_input"
    if len(text) <= 80 and _RUNTIME_PROBE_RE.search(text):
        return "runtime_probe"
    if _EXPLICIT_MEMORY_FORGET_RE.search(text):
        return None
    if (
        _TRANSIENT_CONTEXT_SIGNAL_RE.search(text)
        and _NON_DURABLE_CONCLUSION_RE.search(text)
    ):
        return "explicit_non_durable_context"
    if _looks_like_standalone_question(text):
        return "standalone_question_no_durable_claim"
    if (
        len(text) <= 60
        and _TRANSIENT_USER_COMMAND_RE.fullmatch(text)
        and not _DURABLE_ASSERTION_RE.search(text)
    ):
        return "transient_user_instruction"
    if not _has_active_capture_hint(item):
        return "memory_capture_not_requested"
    return None


def _deterministic_personal_v2_disposition(
    item: Mapping[str, object],
) -> str | None:
    """Reject structurally invalid/noisy inputs before Luna sees any text."""

    if not _input_evidence_ids(item):
        return "canonical_personal_evidence_missing"
    source_kind = compact_whitespace(str(item.get("sourceKind") or ""))
    text = compact_whitespace(str(item.get("text") or ""))
    if source_kind not in {"user_final", "explicit_memory"}:
        return "non_user_personal_evidence"
    if not text:
        return "empty_input"
    if reason := memory_evidence_exclusion_reason(text):
        return reason
    if contains_sensitive_content(text):
        return "sensitive_input"
    if isinstance(item.get("decisionContext"), Mapping):
        # Only the same-session resolver builds this context from stored rows.
        # Preserve the original short answer and let the semantic pass judge
        # whether the explicitly selected option has durable value.
        return None
    historical_reconstruction = (
        compact_whitespace(str(item.get("evidenceOriginKind") or ""))
        == "legacy_untyped_input"
        and compact_whitespace(str(item.get("source") or ""))
        == "reconstructed_user_input"
    )
    quality = assess_input_text(
        text,
        source=FINALIZED_INPUT_SOURCE,
        finalized=True,
        tags=("finalized", "complete-input"),
    )
    quality_reasons = set(quality.reasons)
    for reason in (
        "empty",
        "symbols_only",
        "repeated_noise",
        "known_low_signal_fragment",
        "isolated_ascii_token",
        "short_cjk_fragment",
        "single_word",
        "incomplete_expression",
        "insufficient_durable_signal",
    ):
        if reason in quality_reasons or (
            reason == "incomplete_expression" and not quality.injectable
        ):
            prefix = "historical_reconstruction_" if historical_reconstruction else ""
            return f"{prefix}{reason}"
    if _FILLER_RE.fullmatch(text):
        return "input_noise_filler"
    if _REFERENTIAL_FRAGMENT_RE.fullmatch(text):
        return "referential_input_fragment"
    if len(text) <= 8 and _RANDOM_INPUT_RE.fullmatch(text):
        return "random_key_input"
    if len(text) <= 80 and _RUNTIME_PROBE_RE.search(text):
        return "runtime_probe"
    if _DERIVED_PROTOCOL_NOISE_RE.search(text):
        return "workflow_protocol_noise"
    if _EXPLICIT_MEMORY_FORGET_RE.search(text):
        return None
    if (
        _TRANSIENT_CONTEXT_SIGNAL_RE.search(text)
        and _NON_DURABLE_CONCLUSION_RE.search(text)
    ):
        return "explicit_non_durable_context"
    if _looks_like_standalone_question(text):
        return "standalone_question_no_durable_claim"
    if (
        len(text) <= 60
        and _TRANSIENT_USER_COMMAND_RE.fullmatch(text)
        and not _DURABLE_ASSERTION_RE.search(text)
    ):
        return "transient_user_instruction"
    return None


def _with_personal_v2_run_identity(
    compile_output: Mapping[str, object],
    *,
    run_id: str,
    model_run_id: str = "",
) -> dict[str, object]:
    result = dict(compile_output)
    curation_run_id = compact_whitespace(model_run_id) or run_id
    for field in ("memoryAtoms", "memoryRetractions"):
        result[field] = [
            {**dict(item), "curationRunId": curation_run_id}
            for item in result.get(field) or []
            if isinstance(item, Mapping)
        ]
    metadata = dict(result.get("personalCurationV2") or {})
    metadata["runId"] = run_id
    result["personalCurationV2"] = metadata
    return result


def _pending_source_count(
    conn: sqlite3.Connection,
    *,
    owner_kind: str,
    owner_id: str,
    project: str,
    cursor_ms: int,
    cursor_id: str,
    canonical_personal: bool = False,
) -> int:
    canonical_clause = (
        f"""
              AND (
                  SELECT COUNT(DISTINCT pending_evidence.evidence_id)
                  FROM memory_evidence_input_event_links AS pending_link
                       INDEXED BY idx_memory_evidence_input_event
                  CROSS JOIN agent_memory_evidence AS pending_evidence
                  WHERE pending_link.input_event_id = s.input_event_id
                    AND pending_link.relation = 'source'
                    AND pending_evidence.evidence_id = pending_link.evidence_id
                    AND {curatable_personal_evidence_sql('pending_evidence')}
              ) = 1
        """
        if canonical_personal
        else ""
    )
    late_personal_clause = (
        "OR s.disposition IN ('pending', 'needs_review')"
        if canonical_personal
        else ""
    )
    return int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM agent_memory_sources AS s
            JOIN input_events AS e ON e.id = s.input_event_id
            WHERE s.owner_kind = ? AND s.owner_id = ? AND s.status = 'active'
              AND (? = '' OR e.project = ? OR e.project = '')
              AND s.disposition IN ({','.join('?' for _ in _ELIGIBLE_DISPOSITIONS)})
                  AND e.source != 'personal_profile_editor'
              {canonical_clause}
              AND (
                  s.created_at_ms > ?
                  OR (s.created_at_ms = ? AND s.source_id > ?)
                  {late_personal_clause}
              )
            """,
            (
                owner_kind,
                owner_id,
                project,
                project,
                *_ELIGIBLE_DISPOSITIONS,
                cursor_ms,
                cursor_ms,
                cursor_id,
            ),
        ).fetchone()[0]
    )


def _boundary_before_sources(
    conn: sqlite3.Connection,
    *,
    owner_kind: str,
    owner_id: str,
    project: str,
    source_ids: list[str],
    fallback: tuple[int, str],
) -> tuple[int, str]:
    normalized_ids = list(
        dict.fromkeys(
            compact_whitespace(str(source_id or ""))
            for source_id in source_ids
            if compact_whitespace(str(source_id or ""))
        )
    )
    if not normalized_ids:
        return fallback
    target = conn.execute(
        """
        WITH selected_source_ids(source_id) AS (
            SELECT DISTINCT CAST(value AS TEXT)
            FROM json_each(?)
        )
        SELECT s.created_at_ms, s.source_id
        FROM agent_memory_sources AS s
        JOIN input_events AS e ON e.id = s.input_event_id
        JOIN selected_source_ids AS selected
          ON selected.source_id = s.source_id
        WHERE s.owner_kind = ? AND s.owner_id = ?
          AND (? = '' OR e.project = ? OR e.project = '')
        ORDER BY s.created_at_ms ASC, s.source_id ASC
        LIMIT 1
        """,
        (
            json.dumps(
                normalized_ids,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            owner_kind,
            owner_id,
            project,
            project,
        ),
    ).fetchone()
    if target is None:
        return fallback
    target_ms = int(target["created_at_ms"] or 0)
    target_id = str(target["source_id"] or "")
    previous = conn.execute(
        f"""
        SELECT s.created_at_ms, s.source_id
        FROM agent_memory_sources AS s
        JOIN input_events AS e ON e.id = s.input_event_id
        WHERE s.owner_kind = ? AND s.owner_id = ? AND s.status = 'active'
          AND (? = '' OR e.project = ? OR e.project = '')
          AND (
              s.created_at_ms < ?
              OR (s.created_at_ms = ? AND s.source_id < ?)
          )
        ORDER BY s.created_at_ms DESC, s.source_id DESC
        LIMIT 1
        """,
        (
            owner_kind,
            owner_id,
            project,
            project,
            target_ms,
            target_ms,
            target_id,
        ),
    ).fetchone()
    candidate = (
        (int(previous["created_at_ms"] or 0), str(previous["source_id"] or ""))
        if previous is not None
        else (0, "")
    )
    return max(fallback, candidate)


def _store_empty_owner_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    owner_kind: str,
    owner_id: str,
    project: str,
    provider: str,
    model: str,
    source_ids: list[str],
    source_decisions: list[dict[str, object]],
    created_at_ms: int,
    run_kind: str,
    curation_metadata: Mapping[str, object] | None = None,
) -> None:
    metadata = {
        "ownerKind": owner_kind,
        "ownerId": owner_id,
        "project": project,
        "runKind": run_kind,
        "sourceIds": source_ids,
        "sourceDecisions": source_decisions,
        "needsReviewSourceCount": sum(
            len(decision.get("sourceIds") or [decision.get("sourceId")])
            for decision in source_decisions
            if decision.get("disposition") == "needs_review"
        ),
        "curationOutcome": "no_durable_memory_changes",
        **dict(curation_metadata or {}),
        **purpose_audit_fields(),
    }
    conn.execute(
        """
        INSERT OR REPLACE INTO memory_cleanup_runs(
            run_id, created_at_ms, provider, model, status, summary, metadata_json,
            owner_kind, owner_id, run_kind
        ) VALUES (?, ?, ?, ?, 'empty', ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            created_at_ms,
            provider,
            model,
            "整理完成：没有需要写入长期记忆的变更",
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            owner_kind,
            owner_id,
            run_kind,
        ),
    )


def _run_status(conn: sqlite3.Connection, run_id: str) -> str:
    if not run_id:
        return ""
    row = conn.execute(
        "SELECT status FROM memory_cleanup_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return str(row["status"] or "") if row is not None else ""


def _owner(owner_kind: object, owner_id: object) -> tuple[str, str]:
    kind = compact_whitespace(str(owner_kind or ""))
    identity = compact_whitespace(str(owner_id or ""))
    if kind not in _OWNER_KINDS:
        raise ValueError("unsupported memory owner kind")
    if not identity:
        raise ValueError("memory owner id must not be empty")
    return kind, identity


def _owner_run_id(owner_kind: str, owner_id: str, current_ms: int) -> str:
    owner_hash = hashlib.sha256(f"{owner_kind}\0{owner_id}".encode("utf-8")).hexdigest()[:10]
    return f"memory_book_owner_{current_ms}_{owner_hash}_{uuid.uuid4().hex[:8]}"


def _owner_model_run_id(
    owner_kind: str,
    owner_id: str,
    project: str,
    frozen_payload: Mapping[str, object],
) -> str:
    payload = dict(frozen_payload)
    payload.pop("bundleHash", None)
    payload.pop("curationRunId", None)
    input_hash = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    owner_hash = hashlib.sha256(
        f"{owner_kind}\0{owner_id}\0{project}".encode("utf-8")
    ).hexdigest()[:10]
    return f"memory_model_owner_{owner_hash}_{input_hash[:24]}"


def _legal_ints(value: object, legal: set[int]) -> list[int]:
    result: list[int] = []
    for item in value if isinstance(value, list) else []:
        try:
            parsed = int(item)
        except (TypeError, ValueError):
            continue
        if parsed in legal and parsed not in result:
            result.append(parsed)
    return result


def _json_strings(value: object) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [
        compact_whitespace(str(item))
        for item in parsed
        if compact_whitespace(str(item))
    ]


def _json_mapping(value: object) -> dict[str, object]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _metadata_strings(value: object) -> list[str]:
    values = value if isinstance(value, list) else ([] if value in (None, "") else [value])
    return [
        compact_whitespace(str(item))
        for item in values
        if item is not None and compact_whitespace(str(item))
    ]


def _json_ints(value: object) -> list[int]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    result: list[int] = []
    for item in parsed:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in result:
            result.append(number)
    return result


def _bounded_float(value: object, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.0, min(1.0, parsed))


def _public_error(error: BaseException) -> str:
    text = compact_whitespace(str(error))
    return (text or error.__class__.__name__)[:500]
