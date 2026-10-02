#!/usr/bin/env python3
"""Run a redacted long-context Memory transport canary on an isolated Gateway."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import stat
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Mapping
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag_ime.agent_model_defaults import DEFAULT_AGENT_MODEL_ID, DEFAULT_AGENT_MODEL_PROFILE
from rag_ime.agent_configuration import default_agent_configuration
from rag_ime.agent_service import AgentService
from rag_ime.embeddings import HashingEmbeddingProvider
from rag_ime.local_sqlite_core import LocalSqliteCoreClient
from rag_ime.memory_long_context_evaluation import (
    MAXIMUM_NEAR_BUDGET_INPUT_TOKENS,
    MEMORY_LONG_CONTEXT_EVALUATION_SCHEMA_VERSION,
    MINIMUM_NEAR_BUDGET_INPUT_TOKENS,
    TARGET_NEAR_BUDGET_INPUT_TOKENS,
    adjusted_filler_chars,
    build_transport_canary_payload,
    calibrated_filler_chars,
    canary_messages,
    input_tokens_from_usage,
    near_budget_checks,
    parse_and_verify_canary_output,
    redacted_marker_summary,
)
from rag_ime.memory_model_executor import build_governed_memory_model_executor
from rag_ime.pi.config import PiRuntimeConfig

from pi_canary_support import (
    OPENAI_CODEX_PROXY_ENV,
    installed_agent_config_dir,
    launch_environment as load_launch_environment,
    stage_openai_codex_oauth,
    temporary_environment,
)


MODEL_REFERENCE = DEFAULT_AGENT_MODEL_PROFILE
THINKING_LEVEL = "max"
DEFAULT_LAUNCH_AGENT = (
    Path.home() / "Library" / "LaunchAgents" / "com.rag-ime.agent-gateway.plist"
)
DEFAULT_PRODUCTION_DB = (
    Path.home() / "Library" / "Application Support" / "RagIme" / "rag-ime.sqlite"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a >64K, near-budget Memory packet through the real "
            "Gateway-owned Luna/max path without touching production state."
        )
    )
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--pi-payload", type=Path, required=True)
    parser.add_argument("--public-report", type=Path)
    parser.add_argument("--launch-agent-plist", type=Path, default=DEFAULT_LAUNCH_AGENT)
    parser.add_argument("--production-db", type=Path, default=DEFAULT_PRODUCTION_DB)
    parser.add_argument("--timeout-seconds", type=float, default=1_200.0)
    parser.add_argument("--calibration-filler-chars", type=int, default=24_000)
    parser.add_argument(
        "--target-input-tokens",
        type=int,
        default=TARGET_NEAR_BUDGET_INPUT_TOKENS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = build_parser().parse_args(argv)
    private_root = _private_root(args.private_dir)
    payload_root = args.pi_payload.expanduser().resolve(strict=True)
    launch_agent = args.launch_agent_plist.expanduser().resolve(strict=True)
    production_db = args.production_db.expanduser().resolve(strict=False)
    node = payload_root / "bin" / "node"
    entrypoint = payload_root / "runtime-host" / "cli.mjs"
    manifest = payload_root / "manifest.json"
    for required in (node, entrypoint, manifest):
        if not required.is_file():
            raise SystemExit(f"managed Pi payload is incomplete: {required}")

    installed_before = _installed_observation(
        launch_agent=launch_agent,
        production_db=production_db,
    )
    started_at_ms = int(time.time() * 1_000)
    db_path = private_root / "long-context.sqlite"
    app_support = private_root / "app-support"
    agent_dir = app_support / "Agent" / "config"
    agent_dir.mkdir(parents=True, mode=0o700)
    (agent_dir / "settings.json").write_text(
        json.dumps(
            {"compaction": {"enabled": False}},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(agent_dir / "settings.json", 0o600)
    installed_auth: Path | None = None
    installed_auth_sha256 = ""
    report: dict[str, object] = {
        "schemaVersion": MEMORY_LONG_CONTEXT_EVALUATION_SCHEMA_VERSION,
        "startedAtMs": started_at_ms,
        "productionEnabled": False,
        "productionDatabaseOpened": False,
        "installedMutationRequested": False,
        "privateDirectory": str(private_root),
        "modelReference": MODEL_REFERENCE,
        "thinkingLevel": THINKING_LEVEL,
        "targetInputTokens": int(args.target_input_tokens),
        "calibration": {},
        "attempts": [],
        "checks": {},
        "passed": False,
    }
    agent: AgentService | None = None
    executor = None
    run_id = f"memory-long-context:{started_at_ms}:{secrets.token_hex(6)}"
    error: BaseException | None = None
    try:
        installed_auth = installed_agent_config_dir(launch_agent) / "auth.json"
        installed_auth_sha256 = _file_sha256(installed_auth)
        stage_openai_codex_oauth(installed_auth.parent, agent_dir)
        launch_environment = load_launch_environment(launch_agent)
        environment = {
            **launch_environment,
            "RAG_IME_APP_SUPPORT_DIR": str(app_support),
            "RAG_IME_DB_PATH": str(db_path),
            "RAG_IME_PI_ENABLED": "1",
            "RAG_IME_PI_EXECUTABLE": str(entrypoint),
            "RAG_IME_PI_NODE": str(node),
            "RAG_IME_PI_PROTOCOL_VERSION": "2",
        }
        with temporary_environment(environment):
            base_runtime = PiRuntimeConfig.from_environment(enabled_default=True)
            runtime = replace(
                base_runtime,
                enabled=True,
                executable=entrypoint,
                node_executable=str(node),
                extension_path=None,
                agent_dir=agent_dir,
                session_dir=app_support / "Agent" / "sessions",
                logs_dir=app_support / "Agent" / "logs",
                debug_context_dir=None,
                protocol_version="2",
                command_timeout_seconds=max(30.0, float(args.timeout_seconds)),
                idle_timeout_seconds=0,
                provider_environment={
                    key: value
                    for key, value in base_runtime.provider_environment.items()
                    if key in OPENAI_CODEX_PROXY_ENV
                },
                provider="openai-codex",
                model=DEFAULT_AGENT_MODEL_ID,
                model_base_url="",
                model_configured=True,
                model_configuration_error="",
            )
            embedding = HashingEmbeddingProvider(dimensions=192)
            core = LocalSqliteCoreClient(db_path, embedding_provider=embedding)
            core.initialize()
            agent = AgentService(
                db_path=db_path,
                runtime_config=runtime,
                configuration_defaults=default_agent_configuration(
                    enabled=True,
                    idle_timeout_seconds=0,
                    role_id="companion-future-v1",
                    role_version="1",
                    model_profile=MODEL_REFERENCE,
                    tool_profile_version="control-center-v1",
                    resume_last_session=False,
                    coordinator_enabled=False,
                ),
                project="memory-long-context-evaluation",
                tool_gateway_url="http://127.0.0.1:9/api/agent/tool/execute",
                memory_embedding_provider=embedding,
                wake_scheduler_enabled=False,
                background_job_execution_owner=False,
            )
            executor = build_governed_memory_model_executor(
                agent.runtime,
                MODEL_REFERENCE,
                THINKING_LEVEL,
                timeout_seconds=float(args.timeout_seconds),
                db_path=db_path,
                sessions=agent.sessions,
                events=agent.events,
            )
            executor.begin_run(run_id)

            calibration = _run_canary(
                executor,
                phase="transport-calibration",
                filler_chars=max(2_000, int(args.calibration_filler_chars)),
            )
            report["calibration"] = calibration
            if not bool(calibration["outputValidation"]["passed"]):
                raise RuntimeError("calibration_marker_mismatch")
            calibration_tokens = int(calibration["inputTokens"])
            if calibration_tokens <= 0:
                raise RuntimeError("calibration_usage_missing")
            filler_chars = calibrated_filler_chars(
                calibration_prompt_chars=int(calibration["inputChars"]),
                calibration_input_tokens=calibration_tokens,
                target_input_tokens=int(args.target_input_tokens),
            )

            attempts: list[dict[str, object]] = []
            for ordinal in range(1, 3):
                attempt = _run_canary(
                    executor,
                    phase=f"near-budget-{ordinal}",
                    filler_chars=filler_chars,
                )
                attempts.append(attempt)
                tokens = int(attempt["inputTokens"])
                if (
                    MINIMUM_NEAR_BUDGET_INPUT_TOKENS
                    <= tokens
                    <= MAXIMUM_NEAR_BUDGET_INPUT_TOKENS
                ):
                    break
                filler_chars = adjusted_filler_chars(
                    filler_chars,
                    tokens,
                    target_input_tokens=int(args.target_input_tokens),
                )
            report["attempts"] = attempts
            final = attempts[-1]
            receipt = dict(final["receipt"])
            checks = near_budget_checks(
                input_tokens=int(final["inputTokens"]),
                context_window=int(receipt.get("contextWindow") or 0),
                model_reference=(
                    f"{str(receipt.get('provider') or '')}/"
                    f"{str(receipt.get('modelId') or '')}"
                ),
                thinking_level=str(receipt.get("thinkingLevel") or ""),
                output_validation=dict(final["outputValidation"]),
            )
            checks["gatewayInternalSession"] = (
                str(receipt.get("transport") or "") == "gateway_internal_session"
            )
            checks["compactionDisabled"] = True
            report["checks"] = checks
            report["passed"] = all(checks.values())
            executor.finish_run(
                state="completed" if bool(report["passed"]) else "failed"
            )
    except BaseException as exc:
        error = exc
        report["errorType"] = type(exc).__name__
        report["errorCode"] = _public_error_code(exc)
        if executor is not None:
            try:
                executor.fail_run(exc)
            except Exception:
                pass
    finally:
        cleanup_errors: list[str] = []
        if executor is not None:
            try:
                executor.close()
            except Exception as exc:
                cleanup_errors.append(f"executor:{type(exc).__name__}")
        if agent is not None:
            try:
                agent.close()
            except Exception as exc:
                cleanup_errors.append(f"agent:{type(exc).__name__}")
        shutil.rmtree(agent_dir, ignore_errors=True)
        try:
            installed_after = _installed_observation(
                launch_agent=launch_agent,
                production_db=production_db,
            )
        except Exception as exc:
            cleanup_errors.append(f"installed-observation:{type(exc).__name__}")
            installed_after = {
                "gateway": {},
                "launchAgentSha256": "",
                "productionDbIdentity": {},
            }
        try:
            credential_unchanged = bool(
                installed_auth is not None
                and installed_auth_sha256
                and installed_auth_sha256 == _file_sha256(installed_auth)
            )
        except OSError:
            credential_unchanged = False
        report["installedObservation"] = {
            "gatewayBefore": installed_before["gateway"],
            "gatewayAfter": installed_after["gateway"],
            "sameGatewayPid": (
                installed_before["gateway"].get("pid", 0)
                == installed_after["gateway"].get("pid", 0)
                and int(installed_before["gateway"].get("pid", 0)) > 0
            ),
            "launchAgentUnchanged": (
                installed_before["launchAgentSha256"]
                == installed_after["launchAgentSha256"]
            ),
            "productionDbIdentityBefore": installed_before["productionDbIdentity"],
            "productionDbIdentityAfter": installed_after["productionDbIdentity"],
            "installedCredentialUnchanged": credential_unchanged,
            "privateCredentialRemoved": not agent_dir.exists(),
        }
        checks = dict(report.get("checks") or {})
        checks["installedGatewayHealthyAndSamePid"] = bool(
            report["installedObservation"]["sameGatewayPid"]
            and installed_after["gateway"].get("healthStatus") == 200
        )
        checks["installedLaunchAgentUnchanged"] = bool(
            report["installedObservation"]["launchAgentUnchanged"]
        )
        checks["installedCredentialUnchanged"] = credential_unchanged
        checks["privateCredentialRemoved"] = not agent_dir.exists()
        checks["productionDatabaseNotOpened"] = True
        report["checks"] = checks
        if cleanup_errors:
            report["cleanupErrors"] = cleanup_errors
        report["passed"] = bool(
            error is None
            and not cleanup_errors
            and checks
            and all(checks.values())
        )
        report["completedAtMs"] = int(time.time() * 1_000)
        report["sourceSnapshot"] = _source_snapshot()
        private_report = private_root / "long-context-report.private.json"
        try:
            _write_json(private_report, report, mode=0o600)
            if args.public_report is not None:
                _write_json(
                    args.public_report.expanduser().resolve(strict=False),
                    report,
                    mode=0o644,
                )
        finally:
            shutil.rmtree(agent_dir, ignore_errors=True)

    print(
        json.dumps(
            {
                "schemaVersion": report["schemaVersion"],
                "passed": report["passed"],
                "privateReport": str(private_root / "long-context-report.private.json"),
                "checks": report.get("checks"),
                "errorType": report.get("errorType", ""),
                "errorCode": report.get("errorCode", ""),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if bool(report["passed"]) and error is None else 1


def _run_canary(executor: object, *, phase: str, filler_chars: int) -> dict[str, object]:
    markers = {
        "begin": f"B-{secrets.token_hex(18)}",
        "middle": f"M-{secrets.token_hex(18)}",
        "end": f"E-{secrets.token_hex(18)}",
    }
    payload, offsets = build_transport_canary_payload(
        markers,
        filler_chars=filler_chars,
    )
    response = executor.complete(  # type: ignore[attr-defined]
        messages=canary_messages(payload),
        max_tokens=128,
        phase=phase,
        isolated=True,
    )
    output = str(response["choices"][0]["message"]["content"])
    receipt = dict(response.get("receipt") or {})
    usage = dict(receipt.get("usage") or {})
    return {
        "phase": phase,
        "fillerChars": int(filler_chars),
        "payloadChars": len(payload),
        "payloadSha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "markerOffsets": offsets,
        "markers": redacted_marker_summary(markers),
        "inputChars": int(receipt.get("inputChars") or 0),
        "inputTokens": input_tokens_from_usage(usage),
        "outputValidation": parse_and_verify_canary_output(output, markers),
        "receipt": {
            key: receipt.get(key)
            for key in (
                "schemaVersion",
                "profile",
                "transport",
                "provider",
                "modelId",
                "thinkingLevel",
                "contextWindow",
                "maxTokens",
                "inputSha256",
                "inputChars",
                "elapsedMs",
                "usage",
            )
        },
    }


def _private_root(value: Path) -> Path:
    path = value.expanduser().resolve(strict=False)
    if path.is_relative_to(ROOT):
        raise SystemExit("--private-dir must be outside the Git worktree")
    if path.exists():
        if not path.is_dir() or stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise SystemExit("existing --private-dir must be a mode-0700 directory")
        if any(path.iterdir()):
            raise SystemExit("existing --private-dir must be empty")
    else:
        path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def _installed_observation(*, launch_agent: Path, production_db: Path) -> dict[str, object]:
    return {
        "gateway": _gateway_observation(),
        "launchAgentSha256": _file_sha256(launch_agent),
        "productionDbIdentity": _file_identity(production_db),
    }


def _gateway_observation() -> dict[str, object]:
    pid = 0
    try:
        import subprocess

        completed = subprocess.run(
            ["lsof", "-tiTCP:8768", "-sTCP:LISTEN"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        values = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        pid = int(values[0]) if values else 0
    except Exception:
        pid = 0
    status = 0
    try:
        with urlopen("http://127.0.0.1:8768/health", timeout=3) as response:
            status = int(response.status)
    except Exception:
        status = 0
    return {"pid": pid, "healthStatus": status}


def _file_identity(path: Path) -> dict[str, object]:
    try:
        metadata = path.stat()
    except OSError:
        return {"exists": False}
    return {
        "exists": True,
        "device": int(metadata.st_dev),
        "inode": int(metadata.st_ino),
        "size": int(metadata.st_size),
        "mtimeNs": int(metadata.st_mtime_ns),
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _source_snapshot() -> dict[str, str]:
    paths = (
        ROOT / "rag_ime" / "memory_model_executor.py",
        ROOT / "rag_ime" / "memory_long_context_evaluation.py",
        ROOT / "scripts" / "eval_memory_long_context_luna.py",
    )
    return {
        str(path.relative_to(ROOT)): _file_sha256(path)
        for path in paths
    }


def _write_json(path: Path, payload: Mapping[str, object], *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, mode)


def _public_error_code(error: BaseException) -> str:
    text = str(error or "").casefold()
    for value in (
        "calibration_marker_mismatch",
        "calibration_usage_missing",
        "memory_curation_timeout",
        "memory_model_request_failed",
    ):
        if value in text:
            return value
    return type(error).__name__.casefold()


if __name__ == "__main__":
    raise SystemExit(main())
