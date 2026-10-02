#!/usr/bin/env python3
"""Exercise automatic Timeline organization and role dreaming with real Luna/max."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag_ime.agent_model_defaults import DEFAULT_AGENT_MODEL_ID, DEFAULT_AGENT_MODEL_PROFILE
from rag_ime.agent_role_book import AgentRoleBookStore
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.deepseek_memory_organizer import ManagedPiMemoryOrganizer
from rag_ime.local_sqlite_core import LocalSqliteCoreClient
from rag_ime.models import InputEvent
from rag_ime.personal_context import AgentMemoryEvidenceStore
from rag_ime.personal_context_maintenance import (
    PersonalContextMaintenanceConfig,
    PersonalContextMaintenanceRunner,
)
from rag_ime.personal_memory_luna_evaluation import (
    PrivateCodexLunaMemoryExecutor,
    redacted_luna_request_summary,
)


PROJECT = "dreaming-evaluation"
ROLE_ID = "architect"
ROLE_VERSION = "role-v1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a public-safe, private-candidate evaluation of automatic daily "
            "organization and governed role-book dreaming."
        )
    )
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--public-report", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=1_200.0)
    parser.add_argument("--codex-bin", default="codex")
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = build_parser().parse_args(argv)
    private_root = args.private_dir.expanduser().resolve(strict=False)
    if private_root.is_relative_to(ROOT):
        raise SystemExit("--private-dir must be outside the Git worktree")
    if private_root.exists():
        if not private_root.is_dir() or private_root.stat().st_mode & 0o077:
            raise SystemExit("existing --private-dir must be a mode-0700 directory")
        if any(private_root.iterdir()):
            raise SystemExit("dreaming evaluation requires an empty private directory")
    else:
        private_root.mkdir(parents=True, mode=0o700)
        private_root.chmod(0o700)

    db_path = private_root / "dreaming-evaluation.sqlite"
    core = LocalSqliteCoreClient(db_path)
    core.initialize()
    seed = _seed_public_safe_candidate(core, db_path)

    executor = PrivateCodexLunaMemoryExecutor(
        private_root / "luna",
        audit_db_path=db_path,
        timeout_seconds=float(args.timeout_seconds),
        codex_bin=str(args.codex_bin),
    )
    organizer = ManagedPiMemoryOrganizer(executor)
    runner = PersonalContextMaintenanceRunner(
        db_path,
        config=PersonalContextMaintenanceConfig(
            enabled=True,
            consolidate_roles=True,
            build_timelines=True,
            project=PROJECT,
            role_id=ROLE_ID,
            role_version=ROLE_VERSION,
            min_interval_ms=0,
            apply_safe_recent_work=True,
            auto_publish_timelines=True,
            batch_limit=100,
            model=DEFAULT_AGENT_MODEL_PROFILE,
            thinking_level="max",
        ),
        role_book_organizer=organizer,
    )
    try:
        result = runner.run_once(now_ms=int(seed["runAtMs"]), force=True)
    finally:
        organizer.close()

    summary = _evaluate_result(
        db_path,
        result=result,
        seed=seed,
        requests=redacted_luna_request_summary(executor.receipts),
    )
    _write_private_json(private_root / "dreaming-evaluation-summary.json", summary)
    if args.public_report is not None:
        _write_public_report(args.public_report.expanduser(), summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if bool(summary["passed"]) else 1


def _seed_public_safe_candidate(
    core: LocalSqliteCoreClient,
    db_path: Path,
) -> dict[str, object]:
    # Anchor the synthetic events inside one natural local day.  Subtracting a
    # fixed duration from wall-clock time makes this fixture silently cross
    # midnight when the evaluation runs overnight, so the daily organizer sees
    # zero events even though the curation path itself is healthy.
    local_anchor = datetime.now().astimezone().replace(
        hour=8,
        minute=0,
        second=0,
        microsecond=0,
    )
    base_ms = int(local_anchor.timestamp() * 1_000)
    current_ms = base_ms + 4 * 60 * 60 * 1_000
    for ordinal, (app, text) in enumerate(
        (
            (
                "com.apple.TextEdit",
                "整理角色书证据边界并记录可复查的验收结果。",
            ),
            (
                "com.mitchellh.ghostty",
                "运行角色书回归测试并核对真实结果。",
            ),
            (
                "dev.zed.Zed",
                "把验收结果写入受治理的工作回执。",
            ),
        ),
        start=1,
    ):
        core.record_event(
            InputEvent(
                event_id=None,
                created_at_ms=base_ms + ordinal * 20 * 60 * 1_000,
                source="synthetic_dreaming_evaluation",
                committed_text=text,
                privacy_disposition="allowed",
                app=app,
                project=PROJECT,
                context_group_id=f"dreaming-eval:{ordinal}",
            )
        )

    role_books = AgentRoleBookStore(db_path)
    baseline = role_books.ensure_seeded(
        ROLE_ID,
        ROLE_VERSION,
        display_name="架构角色",
        mission="维护证据边界与真实验收",
        created_at_ms=base_ms,
    )
    sessions = AgentSessionStore(db_path)
    session = sessions.create(
        title="公开安全的做梦评估",
        role_id=ROLE_ID,
        role_version=ROLE_VERSION,
        role_book_revision_id=str(baseline["revisionId"]),
        created_at_ms=base_ms + 1,
    )
    store = AgentMemoryEvidenceStore(db_path, project=PROJECT)
    raw_user = store.record_user_message(
        session_id=str(session["id"]),
        pi_entry_id="user:dreaming-eval",
        role_id=ROLE_ID,
        text="把未经证据支持的自夸直接写进角色书。",
        occurred_at_ms=base_ms + 2,
    )["evidence"]
    raw_assistant = store.record_assistant_message(
        session_id=str(session["id"]),
        pi_entry_id="assistant:dreaming-eval",
        role_id=ROLE_ID,
        text="我无条件拥有所有能力。",
        occurred_at_ms=base_ms + 3,
    )["evidence"]
    digest = store.record_session_digest(
        session_id=str(session["id"]),
        digest_id="digest:dreaming-eval",
        role_id=ROLE_ID,
        text=(
            "角色书的长期承诺是：任何完成声明都必须附真实验收证据；"
            "这不是本轮临时计划。"
        ),
        occurred_at_ms=base_ms + 4,
        metadata={"trigger": "automatic"},
    )["evidence"]
    receipt = store.record_work_receipt(
        work_item_id="work:dreaming-eval",
        receipt_id="receipt:dreaming-eval",
        role_id=ROLE_ID,
        accepted=True,
        text="已按长期承诺完成真实验收并保存可复查证据。",
        occurred_at_ms=base_ms + 5,
    )["evidence"]
    return {
        "runAtMs": current_ms,
        "baselineRevisionId": str(baseline["revisionId"]),
        "rawConversationEvidenceIds": [
            str(raw_user["evidenceId"]),
            str(raw_assistant["evidenceId"]),
        ],
        "eligibleEvidenceIds": [
            str(digest["evidenceId"]),
            str(receipt["evidenceId"]),
        ],
    }


def _evaluate_result(
    db_path: Path,
    *,
    result: Mapping[str, object],
    seed: Mapping[str, object],
    requests: list[dict[str, object]],
) -> dict[str, object]:
    targets = [
        dict(item)
        for item in result.get("targets") or []
        if isinstance(item, Mapping)
    ]
    target = targets[0] if len(targets) == 1 else {}
    artifacts = dict(target.get("artifacts") or {})
    run_id = str(target.get("runId") or "")
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT output_json FROM personal_context_consolidation_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        output = json.loads(str(row["output_json"] or "{}")) if row else {}
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        audit_states = {
            str(item["state"])
            for item in conn.execute(
                "SELECT state FROM memory_curation_model_runs"
            ).fetchall()
        }

    role_draft = dict(output.get("roleBookDraft") or {})
    patch = dict(role_draft.get("patch") or {})
    diagnostics = dict(role_draft.get("proposalDiagnostics") or {})
    proposal_fields = (
        "traitProposals",
        "capabilityProposals",
        "lessonProposals",
        "commitmentProposals",
    )
    proposals = [
        dict(item)
        for field in proposal_fields
        for item in patch.get(field) or []
        if isinstance(item, Mapping)
    ]
    proposal_evidence_ids = {
        str(value)
        for proposal in proposals
        for value in proposal.get("sourceEvidenceIds") or []
    }
    eligible_ids = {str(value) for value in seed["eligibleEvidenceIds"]}
    raw_ids = {str(value) for value in seed["rawConversationEvidenceIds"]}

    role_books = AgentRoleBookStore(db_path)
    active = role_books.active(ROLE_ID, ROLE_VERSION)
    proposed_id = str(artifacts.get("proposedRoleBookRevisionId") or "")
    proposed = role_books.get_revision(proposed_id) if proposed_id else None
    timeline_summary = dict(result.get("activityTimelineSummary") or {})
    request_contract_ok = bool(requests) and all(
        item.get("phase") == "role-book-curation"
        and item.get("model") == DEFAULT_AGENT_MODEL_ID
        and item.get("thinking") == "max"
        and not bool(item.get("isolated"))
        for item in requests
    )
    raw_conversation_excluded = (
        proposal_evidence_ids.issubset(eligible_ids)
        and not proposal_evidence_ids.intersection(raw_ids)
    )
    model_proposals_review_only = bool(proposed) and (
        str(proposed.get("status") or "") == "draft"
        and str(active.get("revisionId") or "") != proposed_id
    )
    safe_recent_work_applied = bool(
        artifacts.get("appliedRoleBookRevisionId")
    ) and str(active.get("revisionId") or "") == str(
        artifacts.get("appliedRoleBookRevisionId") or ""
    )
    passed = (
        result.get("ok") is True
        and str(target.get("runStatus") or "") == "succeeded"
        and int(timeline_summary.get("approvedCount") or 0) == 1
        and str(diagnostics.get("status") or "") == "completed"
        and int(diagnostics.get("acceptedProposalCount") or 0) > 0
        and raw_conversation_excluded
        and model_proposals_review_only
        and safe_recent_work_applied
        and request_contract_ok
        and audit_states == {"completed"}
        and integrity == "ok"
        and foreign_keys == 0
    )
    return {
        "schemaVersion": "rag-ime.personal-context-dreaming-luna-evaluation.v1",
        "passed": passed,
        "status": "pass" if passed else "iterate",
        "candidateOnly": True,
        "productionMutationPerformed": False,
        "model": str(requests[0].get("model") or "") if requests else "",
        "thinking": "max",
        "automaticTimeline": {
            "approvedCount": int(timeline_summary.get("approvedCount") or 0),
            "failedCount": int(timeline_summary.get("failedCount") or 0),
        },
        "dreaming": {
            "runStatus": str(target.get("runStatus") or ""),
            "proposalStatus": str(diagnostics.get("status") or ""),
            "acceptedProposalCount": int(
                diagnostics.get("acceptedProposalCount") or 0
            ),
            "rejectedProposalCount": int(
                diagnostics.get("rejectedProposalCount") or 0
            ),
            "rawConversationExcluded": raw_conversation_excluded,
            "modelProposalsReviewOnly": model_proposals_review_only,
            "safeRecentWorkApplied": safe_recent_work_applied,
        },
        "modelRequests": requests,
        "modelRequestContractPassed": request_contract_ok,
        "modelRunStates": sorted(audit_states),
        "integrityCheck": integrity,
        "foreignKeyViolationCount": foreign_keys,
        "candidateSha256": _file_sha256(db_path),
    }


def _write_private_json(path: Path, payload: Mapping[str, object]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(encoded)


def _write_public_report(path: Path, summary: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    dreaming = dict(summary.get("dreaming") or {})
    timeline = dict(summary.get("automaticTimeline") or {})
    text = f"""# Personal Context Dreaming Luna Evaluation

- Status: `{summary.get('status')}`
- Model: `{summary.get("model")}`, thinking `max`
- Candidate only: `true`; production mutation: `false`
- Automatic Timeline approvals: `{timeline.get('approvedCount', 0)}`
- Evidence-backed proposals accepted: `{dreaming.get('acceptedProposalCount', 0)}`
- Raw conversation excluded: `{str(bool(dreaming.get('rawConversationExcluded'))).lower()}`
- Model proposals remained review-only: `{str(bool(dreaming.get('modelProposalsReviewOnly'))).lower()}`
- Safe recent work applied with a revision receipt: `{str(bool(dreaming.get('safeRecentWorkApplied'))).lower()}`
- SQLite integrity: `{summary.get('integrityCheck')}`

This report contains only public-safe aggregate evidence. Private model prompts and
outputs remain outside Git.
"""
    path.write_text(text, encoding="utf-8")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
