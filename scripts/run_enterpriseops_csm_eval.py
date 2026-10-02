#!/usr/bin/env python3
"""Run frozen EnterpriseOps-Gym CSM tasks through source-local PAW Sessions.

The Agent receives only the user request and task-selected Tool schemas. Seed
SQL, database identity and every verifier remain in the Host. Each lane gets a
fresh disposable database and every raw verifier is scored by position so the
upstream duplicate-name overwrite cannot erase conditions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from threading import RLock
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag_ime.agent_model_defaults import DEFAULT_AGENT_MODEL_ID
from rag_ime.agent_service import AgentService
from rag_ime.rag_benchmark_agent import RagBenchmarkAgentGatewayServer
from scripts.run_cloudops_agent_eval import (
    EvaluationCancelled,
    _check_cancelled,
    _candidate_runtime_config,
    _token_usage,
    _wait_for_terminal,
    _write_json_atomic,
)
from scripts.agent_eval_candidate_prompt import (
    append_candidate_prompt, candidate_prompt_identity, load_candidate_prompt,
)


VALIDATION_TASK_COUNT = 3
HELD_OUT_TASK_COUNT = 8
MISSING_SEED_TASK_ID = "task_20251209_132736_542_99ba2325_6705caf4"
FROZEN_VALIDATION_TASK_IDS = (
    "task_20251205_153330_906_a8eea1c0_8c7a6205",
    "task_20260101_122222_300_ad5a67e3_3757206c",
    "task_20260102_161510_799_ad5a67e3_7221f3c8",
)
FROZEN_HELD_OUT_TASK_IDS = (
    "task_20251205_154853_044_d4a463c3_fce76046",
    "task_20251210_002938_761_897c3e0b_f075229a",
    "task_20251210_113549_827_d4a463c3_47b48a98",
    "task_20251231_162628_797_a8eea1c0_0a53db6c",
    "task_20260101_043715_660_a8eea1c0_7304e48e",
    "task_20260102_155148_014_accab84d_910b176a",
    "task_20260102_172945_725_ad5a67e3_5480e26f",
    "task_20260104_202644_765_00364ece_a86667c0",
)
LUNA_ROLE_TENURE_PROFILE = "luna-role-tenure-preloaded-v4"
LUNA_SELECTED_CATALOG_PROFILE = "luna-selected-catalog-preloaded-v5"
LUNA_EXPLICIT_ENUM_PROFILE = "luna-explicit-enum-preloaded-v6"
WORKFLOW_PROFILES = (
    "baseline-v1",
    "dependency-plan-v1",
    "state-contract-v1",
    "state-contract-compact-v2",
    "state-contract-preloaded-v3",
    LUNA_ROLE_TENURE_PROFILE,
    LUNA_SELECTED_CATALOG_PROFILE,
    LUNA_EXPLICIT_ENUM_PROFILE,
)
_PRELOADED_WORKFLOW_PROFILES = frozenset(
    {
        "state-contract-preloaded-v3",
        LUNA_ROLE_TENURE_PROFILE,
        LUNA_SELECTED_CATALOG_PROFILE,
        LUNA_EXPLICIT_ENUM_PROFILE,
    }
)
ENTERPRISEOPS_SUITE_V2_REVISION = "enterpriseops-csm-suite-v2"
ENTERPRISEOPS_SUITE_V2_BUSINESS_AS_OF_DATE = "2025-11-04"
ENTERPRISEOPS_SUITE_V2_OVERLAY_PATH = ROOT / "eval/enterpriseops-csm-suite-v2.overlay.v1.json"
_TASK_ID = re.compile(r"task_[A-Za-z0-9_]+\Z")
_SQL_TIMESTAMP = re.compile(r"\b(20\d{2}-\d{2}-\d{2})[ T]\d{2}:\d{2}:\d{2}\b")
_CALL_FIELDS = frozenset(
    {"schemaVersion", "sessionId", "tool", "toolCallId", "sourceLoopId", "args", "loadReceiptId"}
)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: object) -> str:
    raw = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def workflow_preloads_selected_schemas(workflow_profile: str) -> bool:
    if workflow_profile not in WORKFLOW_PROFILES:
        raise ValueError("EnterpriseOps workflow profile is unsupported")
    return workflow_profile in _PRELOADED_WORKFLOW_PROFILES


def _git_head(path: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def load_suite_overlay(path: str | Path = ENTERPRISEOPS_SUITE_V2_OVERLAY_PATH) -> dict[str, object]:
    overlay_path = Path(path).expanduser().resolve(strict=True)
    raw = json.loads(overlay_path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("EnterpriseOps suite overlay must be an object")
    if raw.get("schemaVersion") != "paw.enterpriseops-csm-suite-v2-overlay.v1":
        raise ValueError("EnterpriseOps suite overlay schema drifted")
    if raw.get("suiteRevision") != ENTERPRISEOPS_SUITE_V2_REVISION:
        raise ValueError("EnterpriseOps suite overlay revision drifted")
    if raw.get("businessAsOfDate") != ENTERPRISEOPS_SUITE_V2_BUSINESS_AS_OF_DATE:
        raise ValueError("EnterpriseOps suite overlay date drifted")
    tasks = raw.get("tasks")
    if not isinstance(tasks, Mapping) or set(tasks) != set(FROZEN_VALIDATION_TASK_IDS):
        raise ValueError("EnterpriseOps suite overlay must cover exactly Validation tasks")
    expected_hash = str(raw.get("overlaySha256") or "")
    payload = {key: value for key, value in raw.items() if key != "overlaySha256"}
    calculated_hash = _sha256(payload)
    if expected_hash != calculated_hash:
        raise ValueError("EnterpriseOps suite overlay hash drifted")
    return {**dict(raw), "overlaySha256": calculated_hash}


def reserve_held_out_consumption(
    promotion_receipt: str | Path,
    *,
    workflow_profile: str,
    suite_revision: str,
) -> Path:
    """Validate only the authorization envelope, then consume it exclusively.

    This function intentionally performs no source, dataset, Runtime, or
    catalog reads. The marker is durable even if a later Held-out preflight or
    execution step fails.
    """
    receipt_path = Path(promotion_receipt).expanduser().resolve(strict=True)
    raw = json.loads(receipt_path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("EnterpriseOps promotion receipt must be an object")
    if raw.get("schemaVersion") != "paw.enterpriseops-csm-validation-promotion.v1":
        raise ValueError("EnterpriseOps promotion receipt schema is unsupported")
    if raw.get("status") != "authorized" or raw.get("heldOutAuthorized") is not True:
        raise ValueError("EnterpriseOps promotion receipt is not authorized")
    receipt_hash = str(raw.get("receiptSha256") or "")
    payload = {key: value for key, value in raw.items() if key != "receiptSha256"}
    if not receipt_hash or receipt_hash != _sha256(payload):
        raise ValueError("EnterpriseOps promotion receipt hash is invalid")
    winner = raw.get("winner")
    if not isinstance(winner, Mapping):
        raise ValueError("EnterpriseOps promotion winner is missing")
    if winner.get("workflowProfile") != workflow_profile:
        raise ValueError("EnterpriseOps promotion winner profile does not match")
    if winner.get("suiteRevision") != suite_revision:
        raise ValueError("EnterpriseOps promotion winner suite does not match")
    marker = receipt_path.with_name(receipt_path.name + ".held-out-consumed")
    marker_payload = {
        "schemaVersion": "paw.enterpriseops-csm-held-out-consumption.v1",
        "promotionReceiptSha256": receipt_hash,
        "workflowProfile": workflow_profile,
        "suiteRevision": suite_revision,
    }
    try:
        with marker.open("x", encoding="utf-8") as handle:
            handle.write(_canonical(marker_payload) + "\n")
    except FileExistsError as exc:
        raise FileExistsError("EnterpriseOps Held-out authorization is already consumed") from exc
    return marker


def _suite_v2_contract_query(contract: str, *, country: str = "uk") -> str:
    if contract == "larson-case-assigned-active-worker-v1":
        return (
            "SELECT COUNT(*) FROM customer_case cc "
            "JOIN account a ON cc.account_id = a.account_id "
            "JOIN case_sla cs ON cc.case_id = cs.case_id "
            "JOIN sla_definition s ON s.sla_def_id = cs.sla_def_id "
            "JOIN user assigned ON cc.assigned_to = assigned.user_id "
            "WHERE a.name = 'Larson PLC' AND cc.assigned_to IS NOT NULL "
            "AND assigned.active = 1 AND assigned.role IN ('agent','manager') "
            "AND cc.channel = 'phone' AND cc.priority = 'moderate' AND cc.state = 'in_progress' "
            "AND s.name = 'SLA Response - Moderate' AND cs.stage = 'in_progress';"
        )
    if contract == "larson-knowledge-owner-country-tenure-v1":
        safe_country = str(country).strip().lower()
        if not re.fullmatch(r"[a-z_]+", safe_country):
            raise ValueError("EnterpriseOps owner country is invalid")
        return (
            "SELECT COUNT(*) FROM knowledge k JOIN user u ON k.owner_id = u.user_id "
            "JOIN location l ON u.location_id = l.location_id "
            "WHERE k.title = 'SLES Setup Guide' AND u.active = 1 "
            "AND u.role IN ('agent','manager') AND l.country = '" + safe_country + "' "
            "AND u.user_id = (SELECT u2.user_id FROM user u2 "
            "JOIN location l2 ON u2.location_id = l2.location_id "
            "WHERE u2.active = 1 AND u2.role IN ('agent','manager') "
            "AND l2.country = '" + safe_country + "' "
            "ORDER BY u2.sys_created_on ASC, u2.user_id ASC LIMIT 1);"
        )
    if contract == "larson-knowledge-exists-v1":
        return (
            "SELECT COUNT(*) FROM knowledge k JOIN product p ON k.product_id = p.product_id "
            "WHERE k.title = 'SLES Setup Guide' AND k.state = 'published' "
            "AND k.visibility = 'internal' AND p.name = 'SUSE Linux Enterprise Server 15';"
        )
    if contract == "larson-knowledge-resolution-link-v1":
        return (
            "SELECT COUNT(*) FROM case_knowledge ck "
            "JOIN knowledge k ON ck.knowledge_id = k.knowledge_id "
            "JOIN customer_case cc ON ck.case_id = cc.case_id "
            "JOIN account a ON cc.account_id = a.account_id "
            "WHERE a.name = 'Larson PLC' AND k.title = 'SLES Setup Guide' "
            "AND ck.used_as = 'resolution';"
        )
    raise ValueError(f"EnterpriseOps suite v2 verifier contract is unsupported: {contract}")


def apply_suite_overlay(
    task: Mapping[str, object],
    overlay: Mapping[str, object],
    *,
    suite_revision: str,
) -> dict[str, object]:
    patched = dict(task)
    if suite_revision == "v1":
        return patched
    if suite_revision != ENTERPRISEOPS_SUITE_V2_REVISION:
        raise ValueError("EnterpriseOps suite revision is unsupported")
    task_id = _task_id(task.get("taskId"))
    entries = overlay.get("tasks")
    if not isinstance(entries, Mapping) or not isinstance(entries.get(task_id), Mapping):
        raise ValueError("EnterpriseOps suite v2 overlay is missing a task")
    entry = dict(entries[task_id])
    expected_source_hash = str(entry.get("sourceTaskSha256") or "")
    actual_source_hash = str(task.get("taskConfigSha256") or "")
    if expected_source_hash and actual_source_hash != expected_source_hash:
        raise ValueError(f"EnterpriseOps source task hash drifted: {task_id}")
    date = str(entry.get("businessAsOfDate") or overlay.get("businessAsOfDate") or "")
    if date != ENTERPRISEOPS_SUITE_V2_BUSINESS_AS_OF_DATE:
        raise ValueError("EnterpriseOps suite v2 task date drifted")
    patched["businessAsOfDate"] = date
    raw_verifiers = task.get("verifiers")
    if not isinstance(raw_verifiers, list):
        raise ValueError("EnterpriseOps suite v2 verifier list is invalid")
    verifiers = [dict(verifier) for verifier in raw_verifiers if isinstance(verifier, Mapping)]
    if len(verifiers) != len(raw_verifiers):
        raise ValueError("EnterpriseOps suite v2 verifier entry is invalid")
    overrides = entry.get("verifierOverrides")
    if not isinstance(overrides, Mapping):
        raise ValueError("EnterpriseOps suite v2 verifier overrides are invalid")
    for index_text, override_value in overrides.items():
        if not str(index_text).isdigit() or not isinstance(override_value, Mapping):
            raise ValueError("EnterpriseOps suite v2 verifier override is invalid")
        index = int(index_text)
        if index < 1 or index > len(verifiers):
            raise ValueError("EnterpriseOps suite v2 verifier index is out of range")
        override = dict(override_value)
        contract = str(override.get("contract") or "")
        verifier = verifiers[index - 1]
        config = verifier.get("validation_config")
        if not isinstance(config, Mapping):
            raise ValueError("EnterpriseOps suite v2 verifier config is invalid")
        verifiers[index - 1] = {
            **verifier,
            "validation_config": {
                **dict(config),
                "query": _suite_v2_contract_query(
                    contract,
                    country=str(override.get("country") or "uk"),
                ),
                "expected_value": 1,
                "comparison_type": "equals",
            },
        }
    selected_tools = task.get("selected_tools")
    if not isinstance(selected_tools, list) or not selected_tools:
        raise ValueError("EnterpriseOps suite v2 selected Tool set is invalid")
    additions = entry.get("additionalSelectedTools", [])
    if not isinstance(additions, list) or any(
        not isinstance(item, str) or not item.strip() for item in additions
    ):
        raise ValueError("EnterpriseOps suite v2 additional Tool set is invalid")
    patched["selected_tools"] = list(
        dict.fromkeys([*(str(item) for item in selected_tools), *additions])
    )
    patched["verifiers"] = verifiers
    return patched


def _runtime_candidate_provenance(
    candidate_root: Path,
    *,
    source_agent_config: Path,
) -> dict[str, object]:
    candidate = candidate_root.expanduser().resolve(strict=True)
    manifest_path = candidate / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, Mapping) or not isinstance(manifest.get("source"), Mapping):
        raise ValueError("EnterpriseOps Runtime candidate provenance is unavailable")
    source = dict(manifest["source"])
    public_source = {
        "commit": str(source.get("commit") or ""),
        "handlersCommit": str(source.get("handlersCommit") or ""),
        "productCommit": str(source.get("productCommit") or ""),
        "sourceContractSha256": str(source.get("sourceContractSha256") or ""),
        "repository": str(source.get("repository") or ""),
    }
    config_root = source_agent_config.expanduser().resolve(strict=True)
    config_bindings = {
        name: _file_sha256(config_root / name)
        for name in ("models.json", "models-store.json", "settings.json")
        if (config_root / name).is_file() and not (config_root / name).is_symlink()
    }
    payload = {
        "schemaVersion": "paw.enterpriseops-runtime-provenance.v1",
        "candidateManifestSha256": _file_sha256(manifest_path),
        "candidateSource": public_source,
        "runnerProductCommit": _git_head(ROOT),
        "candidateMatchesRunnerProductCommit": (
            str(public_source["productCommit"]) == _git_head(ROOT)
        ),
        "nonSecretConfigBindings": config_bindings,
        "authCredentialMaterialPublished": False,
        "installActionPerformed": False,
    }
    payload["provenanceSha256"] = _sha256(payload)
    return payload


def infer_business_as_of_date(seed_file: str | Path) -> str:
    seed = Path(seed_file).expanduser().resolve(strict=True)
    dates = _SQL_TIMESTAMP.findall(seed.read_text(encoding="utf-8"))
    if not dates:
        raise ValueError("EnterpriseOps seed has no timestamped business snapshot")
    return max(dates)


def _task_id(value: object) -> str:
    result = str(value or "").strip()
    if _TASK_ID.fullmatch(result) is None:
        raise ValueError("EnterpriseOps task id is invalid")
    return result


def select_csm_task_split(
    rows: Sequence[Mapping[str, object]], split: str
) -> list[dict[str, object]]:
    normalized = str(split).strip().lower().replace("_", "-")
    if normalized not in {"validation", "held-out"}:
        raise ValueError("EnterpriseOps split must be validation or held-out")
    by_id = {_task_id(row.get("taskId")): dict(row) for row in rows}
    frozen = (
        FROZEN_VALIDATION_TASK_IDS
        if normalized == "validation"
        else FROZEN_HELD_OUT_TASK_IDS
    )
    # Unit fixtures may use generic IDs; production must match the explicit
    # frozen manifest and is checked again by load_tasks().
    if all(task_id in by_id for task_id in frozen):
        return [by_id[task_id] for task_id in frozen]
    ordered = [dict(row) for row in sorted(rows, key=lambda item: _task_id(item.get("taskId")))]
    count = VALIDATION_TASK_COUNT if normalized == "validation" else HELD_OUT_TASK_COUNT
    return ordered[:count] if normalized == "validation" else ordered[VALIDATION_TASK_COUNT : VALIDATION_TASK_COUNT + count]


def load_tasks(
    tasks_root: str | Path,
    *,
    benchmark_root: str | Path,
    split: str,
    suite_revision: str = "v1",
    overlay: Mapping[str, object] | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    root = Path(tasks_root).expanduser().resolve(strict=True)
    benchmark = Path(benchmark_root).expanduser().resolve(strict=True)
    normalized_split = str(split).strip().lower().replace("_", "-")
    if normalized_split not in {"validation", "held-out"}:
        raise ValueError("EnterpriseOps split must be validation or held-out")
    if suite_revision != "v1" and overlay is None:
        raise ValueError("EnterpriseOps suite overlay is required for non-v1 runs")
    expected_universe = (
        set(FROZEN_VALIDATION_TASK_IDS)
        | set(FROZEN_HELD_OUT_TASK_IDS)
        | {MISSING_SEED_TASK_ID}
    )
    discovered = {path.stem for path in root.glob("task_*.json")}
    if discovered != expected_universe:
        raise ValueError("EnterpriseOps task-file universe drifted from frozen contract")
    selected_ids = (
        FROZEN_VALIDATION_TASK_IDS
        if normalized_split == "validation"
        else FROZEN_HELD_OUT_TASK_IDS
    )
    eligible: list[dict[str, object]] = []
    for task_id in selected_ids:
        path = root / f"{task_id}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise ValueError("EnterpriseOps task must be an object")
        task = dict(raw)
        task_id = _task_id(task_id)
        task["taskId"] = task_id
        task["taskConfigSha256"] = _file_sha256(path)
        servers = task.get("gym_servers_config")
        if not isinstance(servers, list) or len(servers) != 1 or not isinstance(servers[0], Mapping):
            raise ValueError("EnterpriseOps CSM task must bind one Gym server")
        server = dict(servers[0])
        relative_seed = str(server.get("seed_database_file") or "")
        seed = (benchmark / relative_seed).resolve(strict=False)
        try:
            seed.relative_to(benchmark)
        except ValueError as exc:
            raise ValueError("EnterpriseOps seed path escapes benchmark root") from exc
        if not seed.is_file() or seed.is_symlink():
            raise ValueError("EnterpriseOps selected task seed is unavailable")
        tools = task.get("selected_tools")
        verifiers = task.get("verifiers")
        if not isinstance(tools, list) or not tools or len(tools) != len(set(map(str, tools))):
            raise ValueError("EnterpriseOps selected Tool set is invalid")
        if not isinstance(verifiers, list) or not verifiers:
            raise ValueError("EnterpriseOps verifier set is missing")
        for verifier in verifiers:
            if not isinstance(verifier, Mapping) or verifier.get("verifier_type") != "database_state":
                raise ValueError("EnterpriseOps CSM lane requires deterministic database_state verifiers")
            config = verifier.get("validation_config")
            if not isinstance(config, Mapping) or config.get("comparison_type", "equals") != "equals":
                raise ValueError("EnterpriseOps CSM lane requires deterministic equals verifiers")
        task["seedFile"] = str(seed)
        task["seedSha256"] = _file_sha256(seed)
        task["businessAsOfDate"] = infer_business_as_of_date(seed)
        if overlay is not None:
            if normalized_split == "validation":
                task = apply_suite_overlay(task, overlay, suite_revision=suite_revision)
            elif suite_revision == ENTERPRISEOPS_SUITE_V2_REVISION:
                # The v2 overlay owns the Validation contract edits. Held-out
                # uses the same frozen date but never inherits Validation-only
                # verifier overrides or opens its task bodies early.
                task["businessAsOfDate"] = str(overlay.get("businessAsOfDate") or "")
            else:
                raise ValueError("EnterpriseOps suite overlay is unsupported for this split")
        eligible.append(task)
    excluded = [
        {
            "taskId": MISSING_SEED_TASK_ID,
            "reason": "missing_seed",
            "seedPathSha256": _sha256(
                "Domain Wise DBs and Task-DB Mappings/csm/dbs/db_1765219280033_d7pqrz32b.sql"
            ),
        }
    ]
    return eligible, excluded


def build_task_manifest(
    rows: Sequence[Mapping[str, object]],
    *,
    split: str,
    suite_revision: str = "v1",
    overlay_sha256: str = "",
) -> dict[str, object]:
    tasks = []
    for row in sorted(rows, key=lambda item: _task_id(item.get("taskId"))):
        tools = [str(item) for item in row.get("selected_tools", [])]
        tasks.append(
            {
                "taskId": _task_id(row.get("taskId")),
                "taskConfigSha256": str(row.get("taskConfigSha256") or _sha256(row)),
                "userPromptSha256": _sha256(str(row.get("user_prompt") or "")),
                "selectedToolsSha256": _sha256(tools),
                "selectedToolCount": len(tools),
                "rawVerifierCount": len(row.get("verifiers", [])),
                "seedSha256": str(row.get("seedSha256") or ""),
                "businessAsOfDate": str(row.get("businessAsOfDate") or ""),
            }
        )
    payload = {
        "schemaVersion": "paw.enterpriseops-csm-task-manifest.v1",
        "domain": "csm",
        "split": str(split),
        "tasks": tasks,
    }
    if suite_revision != "v1":
        payload["suiteRevision"] = suite_revision
        payload["overlaySha256"] = overlay_sha256
    return {
        **payload,
        "taskIds": [item["taskId"] for item in tasks],
        "taskCount": len(tasks),
        "rawVerifierCount": sum(int(item["rawVerifierCount"]) for item in tasks),
        "manifestSha256": _sha256(payload),
    }


def build_agent_prompt(
    task: Mapping[str, object],
    *,
    workflow_profile: str,
    suite_revision: str = "v1",
) -> str:
    if workflow_profile not in WORKFLOW_PROFILES:
        raise ValueError("EnterpriseOps workflow profile is unsupported")
    system = str(task.get("system_prompt") or "").strip()
    request = str(task.get("user_prompt") or "").strip()
    base = (
        f"{system}\n\nCustomer request:\n{request}\n\n"
        "Work only through the available CSM tools. Perform the requested state changes; "
        "do not merely describe them. The host filesystem is not writable, but the available "
        "CSM tools are authorized to mutate only this disposable evaluation database. "
        "Do not invent identifiers. Stop after the task is complete."
    )
    if suite_revision == ENTERPRISEOPS_SUITE_V2_REVISION:
        business_as_of = str(task.get("businessAsOfDate") or "").strip()
        if business_as_of != ENTERPRISEOPS_SUITE_V2_BUSINESS_AS_OF_DATE:
            raise ValueError("EnterpriseOps suite v2 requires the frozen business as-of date")
        base += (
            f"\n\nSuite v2 frozen business as-of date: {business_as_of}. "
            "Resolve relative dates from this date, not from the model's current clock."
        )
        if re.search(r"\bnext year\b", request, flags=re.IGNORECASE):
            next_year = int(business_as_of[:4]) + 1
            base += (
                f" In this suite, 'next year' is the full calendar interval "
                f"{next_year:04d}-01-01 through {next_year:04d}-12-31."
            )
    elif suite_revision != "v1":
        raise ValueError("EnterpriseOps suite revision is unsupported")
    if workflow_profile == "baseline-v1":
        return base
    if workflow_profile in {
        "state-contract-v1",
        "state-contract-compact-v2",
        "state-contract-preloaded-v3",
        LUNA_ROLE_TENURE_PROFILE,
        LUNA_SELECTED_CATALOG_PROFILE,
        LUNA_EXPLICIT_ENUM_PROFILE,
    }:
        business_as_of = str(task.get("businessAsOfDate") or "").strip()
        if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", business_as_of):
            raise ValueError("EnterpriseOps state contract requires a business as-of date")
        temporal_contract = ""
        if (
            suite_revision != ENTERPRISEOPS_SUITE_V2_REVISION
            and re.search(r"\bnext year\b", request, flags=re.IGNORECASE)
        ):
            next_year = int(business_as_of[:4]) + 1
            temporal_contract = (
                f" The phrase 'next year' is deterministically resolved to "
                f"{next_year:04d}-01-01 through {next_year:04d}-12-31."
            )
        date_contract = "" if suite_revision == ENTERPRISEOPS_SUITE_V2_REVISION else (
            f" The frozen business as-of date is {business_as_of}; resolve relative dates from that date, not from "
            "the model's current clock."
        )
        if workflow_profile in {
            "state-contract-compact-v2",
            "state-contract-preloaded-v3",
            LUNA_ROLE_TENURE_PROFILE,
            LUNA_SELECTED_CATALOG_PROFILE,
            LUNA_EXPLICIT_ENUM_PROFILE,
        }:
            compact_contract = (
                base
                + f"\n\nKeep a compact internal state contract.{date_contract}{temporal_contract} "
                "Do not call the built-in session_workflow Tool or any planning Tool. Preserve exact strings, dates, "
                "amounts, enums and limits. Resolve each distinct role independently. Execute each required mutation "
                "once in dependency order. Do not reread unchanged records: trust successful mutation results, make "
                "one final verification pass with CSM reads, and repair at most once. End with one short summary."
            )
            if workflow_profile in {
                LUNA_ROLE_TENURE_PROFILE,
                LUNA_SELECTED_CATALOG_PROFILE,
                LUNA_EXPLICIT_ENUM_PROFILE,
            }:
                role_tenure_contract = compact_contract + (
                    " When selecting a person under role, geography and tenure constraints, form one candidate set "
                    "across every allowed role. Confirm active status and geographic eligibility from the authoritative "
                    "linked location record; never infer geography from names, phone numbers or other proxies. Rank only "
                    "eligible candidates by sys_created_on ascending, and use the lowest stable record identifier only "
                    "to break an exact timestamp tie."
                )
                if workflow_profile in {
                    LUNA_SELECTED_CATALOG_PROFILE,
                    LUNA_EXPLICIT_ENUM_PROFILE,
                }:
                    selected_catalog_contract = role_tenure_contract + (
                        " The selected Tool catalog is authoritative for this disposable run. Do not search for an "
                        "unavailable Tool. If a policy mentions an operation absent from that catalog, complete "
                        "independently executable requested mutations with the available Tools, then report that "
                        "unsupported substep without inventing its result."
                    )
                    if workflow_profile == LUNA_EXPLICIT_ENUM_PROFILE:
                        return selected_catalog_contract + (
                            " When the request states an exact allowed enum value, use that exact value. Do not replace "
                            "it by interpreting nearby adjectives or superlatives unless the user explicitly corrects it."
                        )
                    return selected_catalog_contract
                return role_tenure_contract
            return compact_contract
        return (
            base
            + f"\n\nCompile a state contract before the first mutation.{date_contract}{temporal_contract} Preserve user-provided exact strings, quoted titles, "
            "descriptions, dates, amounts, enum values and limits without embellishment. Track "
            "each distinct role separately (for example contact, case assignee, group member, "
            "knowledge owner); do not reuse an identifier merely because it was already found. "
            "Execute every state transition and relationship in dependency order, reread each "
            "created or updated record, and finish with one clause-by-clause coverage audit."
        )
    return (
        base
        + "\n\nUse this dependency plan before the first mutation: extract every required entity, "
        "target state and relationship; batch the read/look-up calls needed to resolve IDs; then "
        "write parent records before dependent records and linkages. After each mutation, use an "
        "available read/search tool to verify the resulting state. Finish with a bounded coverage "
        "audit against every clause in the customer request and perform at most one repair pass."
    )


class LiveCsmClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 120.0) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self._request_id = 0

    def _http(
        self,
        path: str,
        payload: Mapping[str, object],
        *,
        method: str = "POST",
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        data = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", **dict(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                value = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"EnterpriseOps HTTP {exc.code}: {detail}") from exc
        if not isinstance(value, dict):
            raise RuntimeError("EnterpriseOps endpoint returned a non-object")
        return value

    def _rpc(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        database_id: str = "",
        context: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        self._request_id += 1
        headers = {"Accept": "application/json, text/event-stream"}
        if database_id:
            headers["x-database-id"] = database_id
        for key, value in dict(context or {}).items():
            name = str(key)
            if not name.lower().startswith("x-"):
                name = "x-" + name.lower().replace("_", "-")
            headers[name] = str(value)
        result = self._http(
            "/mcp",
            {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": dict(params)},
            headers=headers,
        )
        if result.get("error"):
            raise RuntimeError("EnterpriseOps MCP call failed")
        value = result.get("result")
        return dict(value) if isinstance(value, Mapping) else {"value": value}

    def list_tools(self) -> list[dict[str, object]]:
        value = self._rpc("tools/list", {})
        tools = value.get("tools")
        if not isinstance(tools, list):
            raise RuntimeError("EnterpriseOps tools/list returned no catalog")
        return [dict(tool) for tool in tools if isinstance(tool, Mapping)]

    def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        database_id: str = "",
        context: dict[str, object] | None = None,
    ) -> dict[str, object]:
        value = self._rpc(
            "tools/call",
            {"name": str(name), "arguments": dict(arguments)},
            database_id=database_id,
            context=context,
        )
        return {"success": True, "result": value}

    def seed_database(self, seed_file: str | Path) -> str:
        path = Path(seed_file).resolve(strict=True)
        database_id = "paw_csm_" + secrets.token_hex(12)
        sql = path.read_text(encoding="utf-8")
        self._http(
            "/api/seed-database",
            {
                "database_id": database_id,
                "name": "PAW EnterpriseOps evaluation",
                "description": "ephemeral source-local sandbox",
                "sql_content": sql,
            },
        )
        return database_id

    def delete_database(self, database_id: str) -> bool:
        try:
            self._http(
                "/api/delete-database",
                {"database_id": str(database_id)},
                method="DELETE",
            )
            return True
        except Exception:
            return False

    def sql_query(
        self,
        query: str,
        *,
        database_id: str,
        context: Mapping[str, object],
    ) -> dict[str, object]:
        headers = {"x-database-id": database_id}
        for key, value in context.items():
            name = str(key)
            if not name.lower().startswith("x-"):
                name = "x-" + name.lower().replace("_", "-")
            headers[name] = str(value)
        return self._http(
            "/api/sql-runner",
            {"query": str(query), "database_id": database_id},
            headers=headers,
        )


class EnterpriseOpsToolGateway:
    def __init__(self, client: Any, catalog: Sequence[Mapping[str, object]]) -> None:
        self.client = client
        self.catalog = {str(item.get("name") or ""): dict(item) for item in catalog}
        self._bindings: dict[str, dict[str, object]] = {}
        self._ledger: list[dict[str, object]] = []
        self._call_receipts: dict[tuple[str, str], dict[str, object]] = {}
        self._lock = RLock()

    def bind_session(
        self,
        session_id: str,
        *,
        database_id: str,
        context: Mapping[str, object],
        allowed_tools: Sequence[str],
        preload_tools: bool = False,
    ) -> None:
        tools = [str(name) for name in allowed_tools]
        missing = [name for name in tools if name not in self.catalog]
        if missing:
            raise ValueError("EnterpriseOps selected Tool is missing from live catalog")
        with self._lock:
            if session_id in self._bindings:
                raise ValueError("EnterpriseOps Session is already bound")
            self._bindings[session_id] = {
                "databaseId": str(database_id),
                "context": dict(context),
                "tools": tools,
                "preloadTools": bool(preload_tools),
            }

    def unbind_session(self, session_id: str) -> bool:
        with self._lock:
            return self._bindings.pop(str(session_id), None) is not None

    def runtime_manifests(self, session: Mapping[str, object]) -> list[dict[str, object]]:
        with self._lock:
            binding = self._bindings.get(str(session.get("id") or ""))
        if binding is None:
            return []
        manifests = []
        for name in binding["tools"]:
            source = self.catalog[str(name)]
            manifest = {
                "name": str(name),
                "description": str(source.get("description") or ""),
                "parameters": dict(source.get("inputSchema") or {"type": "object"}),
                "profile": "enterpriseops-csm-ephemeral-v1",
                "risk": "R0",
            }
            if binding["preloadTools"] is True:
                manifest["alwaysAvailable"] = True
            manifests.append(manifest)
        return manifests

    def execute(self, payload: Mapping[str, object]) -> dict[str, object]:
        unknown = set(payload) - _CALL_FIELDS
        if unknown or payload.get("schemaVersion") != "rag-ime.agent-tool-call.v1":
            raise ValueError("EnterpriseOps Tool envelope is invalid")
        session_id = str(payload.get("sessionId") or "")
        tool = str(payload.get("tool") or "")
        tool_call_id = str(payload.get("toolCallId") or "").strip()
        args = payload.get("args")
        if not session_id or not tool_call_id or not isinstance(args, Mapping):
            if not tool_call_id:
                raise ValueError("EnterpriseOps Tool envelope requires toolCallId")
            raise ValueError("EnterpriseOps Tool envelope is incomplete")
        with self._lock:
            binding = self._bindings.get(session_id)
        if binding is None:
            raise ValueError("EnterpriseOps Tool is not bound to this Session")
        if tool not in binding["tools"]:
            raise ValueError("EnterpriseOps Tool is outside the task allowlist")
        call_key = (session_id, tool_call_id)
        call_sha256 = _sha256({"tool": tool, "args": dict(args)})
        with self._lock:
            previous = self._call_receipts.get(call_key)
            if previous is not None:
                if previous["callSha256"] != call_sha256:
                    raise ValueError("EnterpriseOps toolCallId was reused with a different payload")
                if previous["status"] == "completed":
                    return dict(previous["response"])
                raise ValueError("EnterpriseOps Tool call already failed or remains unresolved")
            self._call_receipts[call_key] = {
                "callSha256": call_sha256,
                "status": "pending",
            }
        started = time.perf_counter_ns()
        try:
            result = self.client.call_tool(
                tool,
                dict(args),
                database_id=str(binding["databaseId"]),
                context=dict(binding["context"]),
            )
            value = result.get("result") if isinstance(result, Mapping) else None
            if (
                not isinstance(result, Mapping)
                or result.get("success") is not True
                or (isinstance(value, Mapping) and value.get("isError") is True)
            ):
                raise ValueError("EnterpriseOps MCP Tool failed")
            ok = True
        except Exception as exc:
            self._record(payload, session_id, tool, args, started, False, None, exc)
            with self._lock:
                self._call_receipts[call_key] = {
                    "callSha256": call_sha256,
                    "status": "failed",
                    "errorType": type(exc).__name__,
                }
            raise
        response = {
            "schemaVersion": "paw.enterpriseops-tool-result.v1",
            "ok": True,
            "tool": tool,
            "result": value,
        }
        self._record(payload, session_id, tool, args, started, ok, value, None)
        with self._lock:
            self._call_receipts[call_key] = {
                "callSha256": call_sha256,
                "status": "completed",
                "response": response,
            }
        return response

    def _record(
        self,
        payload: Mapping[str, object],
        session_id: str,
        tool: str,
        args: Mapping[str, object],
        started: int,
        ok: bool,
        result: object,
        error: Exception | None,
    ) -> None:
        item = {
            "sessionIdSha256": _sha256(session_id),
            "toolCallIdSha256": _sha256(str(payload.get("toolCallId") or "")),
            "sourceLoopIdSha256": _sha256(str(payload.get("sourceLoopId") or "")),
            "loadReceiptIdSha256": _sha256(str(payload.get("loadReceiptId") or "")),
            "tool": tool,
            "argsSha256": _sha256(args),
            "resultSha256": _sha256(result) if error is None else "",
            "ok": ok,
            "errorType": "" if error is None else type(error).__name__,
            "durationMs": round((time.perf_counter_ns() - started) / 1_000_000, 3),
        }
        with self._lock:
            self._ledger.append(item)

    def ledger(self, session_id: str) -> list[dict[str, object]]:
        session_hash = _sha256(session_id)
        with self._lock:
            return [dict(item) for item in self._ledger if item["sessionIdSha256"] == session_hash]


def _extract_sql_value(value: Mapping[str, object]) -> object:
    data = value.get("data")
    if isinstance(data, list):
        if len(data) == 1 and isinstance(data[0], Mapping):
            row = dict(data[0])
            return next(iter(row.values())) if len(row) == 1 else row
        return data
    rows = value.get("rows")
    if isinstance(rows, list):
        if len(rows) == 1 and isinstance(rows[0], Mapping):
            row = dict(rows[0])
            return next(iter(row.values())) if len(row) == 1 else row
        return rows
    return value.get("result", value)


def execute_verifiers(
    client: LiveCsmClient,
    task: Mapping[str, object],
    *,
    database_id: str,
    context: Mapping[str, object],
) -> list[dict[str, object]]:
    results = []
    for index, raw in enumerate(task.get("verifiers", []), start=1):
        assert isinstance(raw, Mapping)
        config = raw.get("validation_config")
        assert isinstance(config, Mapping)
        query = str(config.get("query") or "")
        expected = config.get("expected_value")
        try:
            actual = _extract_sql_value(
                client.sql_query(
                    query,
                    database_id=database_id,
                    context=context,
                )
            )
            passed = actual == expected
            results.append(
                {
                    "verifierIndex": index,
                    "passed": passed,
                }
            )
        except Exception as exc:
            results.append(
                {
                    "verifierIndex": index,
                    "passed": False,
                    "errorType": type(exc).__name__,
                }
            )
    return results


def score_verifiers(results: Sequence[Mapping[str, object]]) -> dict[str, object]:
    total = len(results)
    passed = sum(item.get("passed") is True for item in results)
    return {
        "overallSuccess": total > 0 and passed == total,
        "total": total,
        "passed": passed,
        "passRate": passed / total if total else 0.0,
        "failedVerifierIndexes": [
            int(item.get("verifierIndex") or index)
            for index, item in enumerate(results, start=1)
            if item.get("passed") is not True
        ],
    }


def task_execution_succeeded(
    terminal_event: str,
    runtime_error_type: str,
    verifier_score: Mapping[str, object],
) -> bool:
    return (
        str(terminal_event) == "turn_completed"
        and not str(runtime_error_type)
        and verifier_score.get("overallSuccess") is True
    )


def _run_profile(
    *,
    tasks: Sequence[Mapping[str, object]],
    workflow_profile: str,
    service: AgentService,
    gateway: EnterpriseOpsToolGateway,
    client: LiveCsmClient,
    timeout_seconds: float,
    thinking_level: str,
    expected_provider: str,
    expected_model: str,
    suite_revision: str = "v1",
    candidate_prompt_file: str | Path | None = None,
    evaluation_split: str = "validation",
    runtime_identity: Mapping[str, object] | None = None,
    cancelled: Callable[[], bool] | None = None,
    on_session: Callable[[str], None] | None = None,
) -> dict[str, object]:
    candidate_prompt = load_candidate_prompt(candidate_prompt_file, evaluation_split=evaluation_split)
    prompt_identity = candidate_prompt_identity(candidate_prompt)
    frozen_controls = {
        "taskContractSha256": _sha256(tasks), "suiteRevision": suite_revision,
        "split": evaluation_split, "workflowProfile": workflow_profile,
        "provider": expected_provider, "model": expected_model, "thinking": thinking_level,
        "timeoutSeconds": timeout_seconds, "skills": [],
        "runtimeIdentity": dict(runtime_identity or {}),
        "toolCatalogSha256": _sha256(gateway.catalog),
        "runnerSha256": _file_sha256(Path(__file__).resolve()),
        "basePromptSha256": [_sha256(build_agent_prompt(task, workflow_profile=workflow_profile, suite_revision=suite_revision)) for task in tasks],
    }
    frozen_controls["contractSha256"] = _sha256(frozen_controls)
    task_results: list[dict[str, object]] = []
    was_cancelled = False
    for task in tasks:
        if cancelled is not None and cancelled():
            was_cancelled = True
            break
        task_id = str(task["taskId"])
        server = task["gym_servers_config"][0]
        assert isinstance(server, Mapping)
        context = dict(server.get("context") or {})
        database_id = ""
        session_id = ""
        cleanup_status = "not_created"
        terminal = ""
        turn_id = ""
        events: list[dict[str, object]] = []
        ensured: Mapping[str, object] | None = None
        started = time.monotonic()
        error_type = ""
        error_fingerprint = ""
        runtime_stage = "seed_database"
        verifier_error_type = ""
        verifier_results = [
            {"verifierIndex": index, "passed": False, "errorType": "not_executed"}
            for index, _ in enumerate(task.get("verifiers", []), start=1)
        ]
        ledger: list[dict[str, object]] = []
        prompt = append_candidate_prompt(build_agent_prompt(task, workflow_profile=workflow_profile, suite_revision=suite_revision), candidate_prompt)
        try:
            database_id = client.seed_database(str(task["seedFile"]))
            runtime_stage = "session_create"
            session_payload: dict[str, object] = {
                "title": f"EnterpriseOps CSM {task_id}",
                "mode": "assistant",
                "roleId": "companion-firstlight-v1",
                "roleVersion": "1",
                "toolProfileVersion": "subagent-readonly-v1",
                "toolAllowlistMode": "explicit",
                "allowedTools": [],
                "_modelRoute": "primary",
            }
            if expected_provider and expected_model:
                session_payload["modelProfile"] = f"{expected_provider}/{expected_model}"
            session = service.create_session(session_payload)["session"]
            session_id = str(session["id"])
            if on_session is not None:
                on_session(session_id)
            _check_cancelled(cancelled, service, session_id)
            service.update_session(
                session_id,
                {
                    "mode": "assistant",
                    "executionMode": "per_action",
                    "toolProfileVersion": "subagent-readonly-v1",
                    "toolAllowlistMode": "explicit",
                    "allowedTools": [],
                    "projectContextEnabled": False,
                    "piSkillsEnabled": False,
                    "codexSkillsEnabled": False,
                    "workspaceRoots": [],
                },
            )
            runtime_stage = "tool_bind"
            gateway.bind_session(
                session_id,
                database_id=database_id,
                context=context,
                allowed_tools=[str(name) for name in task["selected_tools"]],
                preload_tools=workflow_preloads_selected_schemas(workflow_profile),
            )
            runtime_stage = "runtime_ensure"
            ensured = service.ensure_runtime({"sessionId": session_id})
            selected_model = ensured.get("state", {}).get("model")
            if not isinstance(selected_model, Mapping) or (
                str(selected_model.get("provider") or ""),
                str(selected_model.get("id") or ""),
            ) != (expected_provider, expected_model):
                raise RuntimeError(
                    "EnterpriseOps evaluation model route drifted from the frozen Runtime identity"
                )
            runtime_stage = "thinking_select"
            thinking_receipt = service.select_thinking_level(
                session_id, {"level": thinking_level}
            )
            if str(thinking_receipt.get("thinkingLevel") or "") != thinking_level:
                raise RuntimeError(
                    "EnterpriseOps evaluation thinking level drifted from the frozen Runtime identity"
                )
            ensured = {"runtime": ensured, "thinkingSelection": thinking_receipt}
            runtime_stage = "prompt"
            _check_cancelled(cancelled, service, session_id)
            receipt = service.prompt(
                session_id,
                {
                    "message": prompt,
                    "clientMessageId": f"enterpriseops:{workflow_profile}:{task_id}",
                },
            )
            turn_id = str(receipt.get("turnId") or "")
            runtime_stage = "wait_terminal"
            events, terminal = _wait_for_terminal(
                service,
                session_id=session_id,
                turn_id=turn_id,
                timeout_seconds=timeout_seconds,
                cancelled=cancelled,
            )
        except Exception as exc:
            if isinstance(exc, EvaluationCancelled):
                was_cancelled = True
                terminal = "turn_cancelled"
            error_type = type(exc).__name__
            error_fingerprint = "sha256:" + _sha256(
                f"{runtime_stage}:{type(exc).__name__}:{exc}"
            )
        finally:
            if database_id:
                try:
                    verifier_results = execute_verifiers(
                        client,
                        task,
                        database_id=database_id,
                        context=context,
                    )
                except Exception as exc:
                    verifier_error_type = type(exc).__name__
                    verifier_results = [
                        {
                            "verifierIndex": index,
                            "passed": False,
                            "errorType": "verifier_execution_failed",
                        }
                        for index, _ in enumerate(task.get("verifiers", []), start=1)
                    ]
            ledger = gateway.ledger(session_id) if session_id else []
            if session_id:
                gateway.unbind_session(session_id)
            if database_id:
                cleanup_status = (
                    "deleted" if client.delete_database(database_id) else "delete_failed"
                )
            score = score_verifiers(verifier_results)
        task_succeeded = task_execution_succeeded(terminal, error_type, score)
        task_results.append(
            {
                "taskId": task_id,
                "taskSucceeded": task_succeeded,
                "terminalEvent": terminal,
                "runtimeErrorType": error_type,
                "runtimeErrorFingerprint": error_fingerprint,
                "runtimeStage": runtime_stage,
                "verifierExecutionErrorType": verifier_error_type,
                "verifier": score,
                "verifierResults": verifier_results,
                "toolCalls": len(ledger),
                "successfulToolCalls": sum(item.get("ok") is True for item in ledger),
                "failedToolCalls": sum(item.get("ok") is not True for item in ledger),
                "usage": _token_usage(events),
                "latencyMs": round((time.monotonic() - started) * 1000, 3),
                "databaseCleanupStatus": cleanup_status,
                "sessionIdSha256": _sha256(session_id),
                "turnIdSha256": _sha256(turn_id),
                "modelIdentitySha256": _sha256(ensured) if ensured is not None else "",
                "promptSha256": _sha256(prompt),
            }
        )
        if was_cancelled:
            break
    total_verifiers = sum(int(item["verifier"]["total"]) for item in task_results)
    passed_verifiers = sum(int(item["verifier"]["passed"]) for item in task_results)
    successful_tasks = sum(item["taskSucceeded"] is True for item in task_results)
    return {
        "status": "cancelled" if was_cancelled else "completed",
        "candidatePrompt": prompt_identity,
        "frozenControls": frozen_controls,
        "workflowProfile": workflow_profile,
        "taskCount": len(task_results),
        "taskSuccessCount": successful_tasks,
        "taskSuccessRate": successful_tasks / len(task_results) if task_results else 0.0,
        "verifierCount": total_verifiers,
        "verifierPassCount": passed_verifiers,
        "verifierPassRate": passed_verifiers / total_verifiers if total_verifiers else 0.0,
        "toolCalls": sum(int(item["toolCalls"]) for item in task_results),
        "failedToolCalls": sum(int(item["failedToolCalls"]) for item in task_results),
        "latencyMs": sum(float(item["latencyMs"]) for item in task_results),
        "allDatabasesCleaned": all(
            item["databaseCleanupStatus"] in {"deleted", "not_created"}
            for item in task_results
        ),
        "tasks": task_results,
    }


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark-root", type=Path, required=True)
    parser.add_argument("--tasks-root", type=Path, required=True)
    parser.add_argument("--gym-url", default="http://127.0.0.1:8001")
    parser.add_argument("--runtime-candidate", type=Path, required=True)
    parser.add_argument("--source-agent-config", type=Path, required=True)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--split", choices=("validation", "held-out"), default="validation")
    parser.add_argument("--workflow-profile", choices=WORKFLOW_PROFILES, required=True)
    parser.add_argument("--suite-revision", choices=("v1", ENTERPRISEOPS_SUITE_V2_REVISION), default="v1")
    parser.add_argument("--suite-overlay", type=Path)
    parser.add_argument("--trial-id", required=True)
    parser.add_argument("--candidate-prompt-file", type=Path, help="UTF-8 generic Validation instructions; never Host Gold.")
    parser.add_argument("--provider", default="openai-codex")
    parser.add_argument("--model", default=DEFAULT_AGENT_MODEL_ID)
    parser.add_argument("--thinking", default="high")
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument("--promotion-receipt", type=Path)
    args = parser.parse_args(argv)
    if args.candidate_prompt_file is not None and args.split != "validation":
        raise ValueError("candidate Prompt files are allowed only in validation; Held-out is unchanged")

    promotion: dict[str, object] | None = None
    promotion_winner: Mapping[str, object] | None = None
    if args.split == "held-out":
        if args.suite_revision != ENTERPRISEOPS_SUITE_V2_REVISION:
            raise ValueError("EnterpriseOps Held-out requires Suite v2")
        if args.promotion_receipt is None:
            raise ValueError("EnterpriseOps Held-out requires --promotion-receipt")
        # This is deliberately the first external read after argument
        # validation. The O_EXCL marker is created before overlay, task,
        # dataset, Runtime, or Tool catalog access; any later failure consumes
        # the one-shot authorization as well.
        reserve_held_out_consumption(
            args.promotion_receipt,
            workflow_profile=args.workflow_profile,
            suite_revision=args.suite_revision,
        )
        promotion = json.loads(args.promotion_receipt.read_text(encoding="utf-8"))
        promotion_winner = promotion.get("winner") if isinstance(promotion, Mapping) else None
        if not isinstance(promotion_winner, Mapping):
            raise ValueError("EnterpriseOps promotion winner is missing")
    overlay = load_suite_overlay(args.suite_overlay or ENTERPRISEOPS_SUITE_V2_OVERLAY_PATH) if args.suite_revision != "v1" else None
    if promotion_winner is not None and promotion_winner.get("overlaySha256") != (overlay or {}).get("overlaySha256"):
        raise ValueError("EnterpriseOps promotion overlay does not match")
    if promotion_winner is not None and promotion_winner.get("provider") != args.provider:
        raise ValueError("EnterpriseOps promotion provider does not match")
    if promotion_winner is not None and promotion_winner.get("model") != args.model:
        raise ValueError("EnterpriseOps promotion model does not match")
    if promotion_winner is not None and promotion_winner.get("thinking") != args.thinking:
        raise ValueError("EnterpriseOps promotion thinking level does not match")
    tasks, excluded = load_tasks(
        args.tasks_root,
        benchmark_root=args.benchmark_root,
        split=args.split,
        suite_revision=args.suite_revision,
        overlay=overlay,
    )
    manifest = build_task_manifest(
        tasks,
        split=args.split,
        suite_revision=args.suite_revision,
        overlay_sha256=str(overlay.get("overlaySha256") or "") if overlay else "",
    )
    if args.split == "validation" and manifest["taskIds"] != sorted(FROZEN_VALIDATION_TASK_IDS):
        raise ValueError("EnterpriseOps Validation manifest drifted")
    if promotion_winner is not None:
        validation_tasks, _ = load_tasks(
            args.tasks_root,
            benchmark_root=args.benchmark_root,
            split="validation",
            suite_revision=args.suite_revision,
            overlay=overlay,
        )
        validation_manifest = build_task_manifest(
            validation_tasks,
            split="validation",
            suite_revision=args.suite_revision,
            overlay_sha256=str(overlay.get("overlaySha256") or "") if overlay else "",
        )
        if promotion_winner.get("validationTaskManifestSha256") != validation_manifest.get("manifestSha256"):
            raise ValueError("EnterpriseOps promotion Validation manifest does not match")
    client = LiveCsmClient(args.gym_url)
    catalog = client.list_tools()
    if len(catalog) != 89:
        raise ValueError("EnterpriseOps live CSM Tool catalog drifted from 89 tools")
    if promotion_winner is not None and promotion_winner.get("toolCatalogSha256") != _sha256(catalog):
        raise ValueError("EnterpriseOps promotion Tool catalog does not match")
    gateway = EnterpriseOpsToolGateway(client, catalog)
    private_root = args.private_root.expanduser().resolve(strict=False)
    private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_root = private_root / str(args.trial_id)
    if run_root.exists():
        raise FileExistsError("EnterpriseOps trial root already exists")
    run_root.mkdir(mode=0o700)
    transport = RagBenchmarkAgentGatewayServer(gateway)
    transport.start()
    service: AgentService | None = None
    try:
        config, runtime_identity = _candidate_runtime_config(
            args.runtime_candidate,
            run_root=run_root,
            source_agent_config=args.source_agent_config,
            provider=args.provider,
            model=args.model,
            tool_gateway_url=transport.tool_gateway_url,
            tool_gateway_token=transport.token,
        )
        runtime_provenance = _runtime_candidate_provenance(
            args.runtime_candidate,
            source_agent_config=args.source_agent_config,
        )
        if promotion_winner is not None and promotion_winner.get("runtimeIdentitySha256") != runtime_identity.get("identitySha256"):
            raise ValueError("EnterpriseOps promotion Runtime identity does not match")
        database = run_root / "paw.sqlite"
        service = AgentService(
            db_path=database,
            runtime_config=config,
            project="enterpriseops-csm-eval",
            tool_gateway_url=transport.tool_gateway_url,
            tool_gateway_token=transport.token,
            wake_scheduler_enabled=False,
            background_job_execution_owner=False,
        )
        service.bind_tool_manifest_provider(gateway.runtime_manifests)
        lane = _run_profile(
            tasks=tasks,
            workflow_profile=args.workflow_profile,
            service=service,
            gateway=gateway,
            client=client,
            timeout_seconds=max(30.0, args.timeout_seconds),
            thinking_level=args.thinking,
            expected_provider=str(runtime_identity.get("provider") or ""),
            expected_model=str(runtime_identity.get("model") or ""),
            suite_revision=args.suite_revision,
            candidate_prompt_file=args.candidate_prompt_file,
            evaluation_split=args.split,
            runtime_identity=runtime_identity,
        )
        prompt_contract = [
            {
                "taskId": str(task["taskId"]),
                "promptSha256": str(task["promptSha256"]),
            }
            for task in lane["tasks"]
        ]
        evaluation_contract = {
            "schemaVersion": "paw.enterpriseops-csm-evaluation-contract.v1",
            "split": args.split,
            "workflowProfile": args.workflow_profile,
            "toolDisclosureMode": (
                "preloaded_selected_schemas"
                if workflow_preloads_selected_schemas(args.workflow_profile)
                else "progressive"
            ),
            "suiteRevision": args.suite_revision,
            "overlaySha256": str(overlay.get("overlaySha256") or "") if overlay else "",
            "taskManifestSha256": manifest["manifestSha256"],
            "toolCatalogSha256": _sha256(catalog),
            "runtimeIdentitySha256": runtime_identity["identitySha256"],
            "runnerSha256": _file_sha256(Path(__file__).resolve()),
            "provider": args.provider,
            "model": args.model,
            "thinking": args.thinking,
            "timeoutSeconds": float(args.timeout_seconds),
            "effectiveTimeoutSeconds": max(30.0, float(args.timeout_seconds)),
            "transport": transport.transport,
            "mcpEndpoint": "/mcp",
            "runtimeProvenanceSha256": runtime_provenance["provenanceSha256"],
            "promptContract": prompt_contract,
            "promptContractSha256": _sha256(prompt_contract),
            "candidatePrompt": lane["candidatePrompt"],
            "frozenControls": lane["frozenControls"],
        }
        if promotion is not None:
            evaluation_contract["promotionReceiptSha256"] = str(promotion.get("receiptSha256") or "")
        evaluation_contract["contractSha256"] = _sha256(evaluation_contract)
        report = {
            "schemaVersion": "paw.enterpriseops-csm-eval.v1",
            "status": (
                lane["status"] if lane["allDatabasesCleaned"] else "invalid_cleanup"
            ),
            "split": args.split,
            "manifest": manifest,
            "excluded": excluded,
            "liveToolCount": len(catalog),
            "runtimeIdentity": runtime_identity,
            "runtimeProvenance": runtime_provenance,
            "evaluationContract": evaluation_contract,
            "candidatePrompt": lane["candidatePrompt"],
            "frozenControls": lane["frozenControls"],
            "toolSelectionMode": "task_selected_oracle",
            "selectedToolNamesVisibleToAgent": True,
            "verifierGoldVisibleToAgent": False,
            "seedVisibleToAgent": False,
            "databaseIdentityVisibleToAgent": False,
            "verifierDuplicateNamesCollapsed": False,
            "lane": lane,
        }
        report["reportSha256"] = _sha256(report)
        _write_json_atomic(args.output, report)
        print(json.dumps({"status": report["status"], "profile": args.workflow_profile, "taskSuccessRate": lane["taskSuccessRate"], "verifierPassRate": lane["verifierPassRate"], "reportSha256": report["reportSha256"]}))
        return 0 if report["status"] == "completed" else 2
    finally:
        if service is not None:
            service.close()
        transport.close()


if __name__ == "__main__":
    raise SystemExit(main())
