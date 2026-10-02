#!/usr/bin/env python3
"""Run and score leakage-resistant Personal Agent Workbench SWE-bench cases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag_ime.agent_model_defaults import DEFAULT_AGENT_MODEL_ID
from rag_ime.agent_service import AgentService  # noqa: E402
from rag_ime.agent_tools import ControlToolGateway  # noqa: E402
from rag_ime.managed_pi_runtime import snapshot_managed_pi_runtime  # noqa: E402
from rag_ime.pi.config import PiRuntimeConfig
from rag_ime.rag_benchmark_agent import RagBenchmarkAgentSpoolGateway  # noqa: E402
from rag_ime.swe_bench_agent_eval import (  # noqa: E402
    DEFAULT_MODEL_REFERENCE,
    RUN_SCHEMA_VERSION,
    SweBenchAgentEvalError,
    SweBenchWorkspaceHarness,
    build_agent_eval_report,
    capture_model_patch,
    load_official_instance_results,
    load_prepared_swe_bench,
    official_prediction,
    official_verifier_command,
    stage_agent_workspace,
    write_official_predictions,
)


DEFAULT_PREPARED = (
    ROOT
    / ".rag-ime-data"
    / "prepared"
    / "rag-interview"
    / "swe-bench-verified.prepared.json"
)
DEFAULT_PRIVATE_ROOT = ROOT / ".rag-ime-data" / "runs" / "swe-bench-agent"
DEFAULT_OUTPUT_ROOT = ROOT / ".rag-ime-data" / "results" / "swe-bench-agent"
DEFAULT_AGENT_CONFIG = (
    Path.home()
    / "Library"
    / "Application Support"
    / "RagIme"
    / "Agent"
    / "config"
)
ALLOWED_TOOLS = (
    "agents",
    "todo",
    "workspace_list",
    "workspace_read",
    "workspace_search",
    "workspace_patch",
    "workspace_edit",
    "workspace_write",
    "workspace_shell",
)


class _DeferredToolGateway:
    def __init__(self) -> None:
        self.target: ControlToolGateway | None = None

    def execute(self, payload: Mapping[str, object]) -> dict[str, object]:
        if self.target is None:
            raise RuntimeError("SWE-bench Tool gateway is not bound")
        return self.target.execute(payload)


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser("preflight")
    _dataset_arguments(preflight)
    preflight.add_argument("--source-repository", type=Path)
    preflight.add_argument("--source-agent-config", type=Path, default=DEFAULT_AGENT_CONFIG)
    preflight.add_argument("--output", type=Path)

    run = subparsers.add_parser("run")
    _dataset_arguments(run)
    run.add_argument("--instance-id", required=True)
    run.add_argument("--source-repository", type=Path, required=True)
    run.add_argument("--source-agent-config", type=Path, default=DEFAULT_AGENT_CONFIG)
    run.add_argument("--private-root", type=Path, default=DEFAULT_PRIVATE_ROOT)
    run.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    run.add_argument("--timeout-seconds", type=float, default=1_800.0)
    run.add_argument("--subagent-budget", type=int, choices=(0, 1, 2), default=1)

    command = subparsers.add_parser("verifier-command")
    command.add_argument("--predictions", type=Path, required=True)
    command.add_argument("--instance-id", action="append", required=True)
    command.add_argument("--run-id", required=True)
    command.add_argument("--max-workers", type=int, default=1)
    command.add_argument(
        "--cache-level", choices=("none", "base", "env", "instance"), default="env"
    )

    aggregate = subparsers.add_parser("aggregate")
    _dataset_arguments(aggregate)
    aggregate.add_argument("--run-record", type=Path, action="append", required=True)
    aggregate.add_argument("--official-results", type=Path)
    aggregate.add_argument("--output", type=Path, required=True)

    args = parser.parse_args(argv)
    if args.command == "preflight":
        report = _preflight(args)
        if args.output is not None:
            _write_json(args.output, report)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report["ready"] is True else 1
    if args.command == "run":
        record, prediction, trajectory = _run_case(args)
        output_root = args.output_root.expanduser().resolve(strict=False)
        output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        prefix = str(record["instanceId"])
        _write_json(output_root / f"{prefix}.run.json", record)
        _write_json(output_root / f"{prefix}.trajectory.json", trajectory)
        write_official_predictions(
            output_root / f"{prefix}.predictions.jsonl",
            [prediction],
        )
        print(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if record.get("inferencePassed") is True else 1
    if args.command == "verifier-command":
        value = official_verifier_command(
            predictions_path=args.predictions,
            instance_ids=args.instance_id,
            run_id=args.run_id,
            max_workers=args.max_workers,
            cache_level=args.cache_level,
        )
        print(json.dumps({"argv": value, "shell": shlex.join(value)}, indent=2))
        return 0
    report = _aggregate(args)
    _write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("passed") is True else 1


def _dataset_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--prepared", type=Path, default=DEFAULT_PREPARED)
    parser.add_argument("--full-split", action="store_true")


def _preflight(args: argparse.Namespace) -> dict[str, object]:
    checks: dict[str, object] = {}
    failure_details: dict[str, str] = {}
    try:
        dataset = load_prepared_swe_bench(args.prepared)
    except Exception as exc:
        checks["dataset"] = False
        failure_details["dataset"] = f"{type(exc).__name__}: {exc}"
        dataset = None
    else:
        checks["dataset"] = True
    config = args.source_agent_config.expanduser().resolve(strict=False)
    checks["privateAgentAuth"] = (config / "auth.json").is_file()
    if checks["privateAgentAuth"] is False:
        failure_details["privateAgentAuth"] = "isolated Luna runtime needs an existing auth.json"
    try:
        support = Path(
            os.environ.get("RAG_IME_APP_SUPPORT_DIR")
            or Path.home() / "Library" / "Application Support" / "RagIme"
        ).expanduser()
        installation = snapshot_managed_pi_runtime(
            support,
            expected_pi_version=os.environ.get("RAG_IME_PI_VERSION", "").strip(),
        )
    except Exception as exc:
        checks["managedPi"] = False
        failure_details["managedPi"] = f"{type(exc).__name__}: {exc}"
        managed_pi: dict[str, object] = {}
    else:
        checks["managedPi"] = True
        managed_pi = {
            "piVersion": installation.pi_version,
            "protocolVersion": installation.protocol_version,
            "tools": list(installation.tools),
        }
    docker = shutil.which("docker")
    checks["dockerCli"] = bool(docker)
    docker_detail = ""
    if docker:
        completed = subprocess.run(
            [docker, "info", "--format", "{{.ServerVersion}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        checks["dockerDaemon"] = completed.returncode == 0
        docker_detail = (completed.stdout or completed.stderr).strip()
        if not docker_detail and completed.returncode != 0:
            docker_detail = "docker daemon is not reachable"
    else:
        checks["dockerDaemon"] = False
        docker_detail = "docker CLI is unavailable"
    if checks["dockerDaemon"] is False:
        failure_details["dockerDaemon"] = docker_detail
    if args.source_repository is None:
        checks["sourceRepository"] = None
    else:
        source = args.source_repository.expanduser().resolve(strict=False)
        checks["sourceRepository"] = source.is_dir()
        if checks["sourceRepository"] is False:
            failure_details["sourceRepository"] = "source repository path is unavailable"
    usage = shutil.disk_usage(ROOT)
    checks["officialHarnessDisk120GiB"] = usage.free >= 120 * 1024**3
    if checks["officialHarnessDisk120GiB"] is False:
        failure_details["officialHarnessDisk120GiB"] = (
            f"only {round(usage.free / 1024**3, 2)} GiB is free"
        )
    required = [
        checks["dataset"],
        checks["privateAgentAuth"],
        checks["managedPi"],
        checks["dockerCli"],
        checks["dockerDaemon"],
        checks["officialHarnessDisk120GiB"],
    ]
    if checks["sourceRepository"] is not None:
        required.append(checks["sourceRepository"])
    return {
        "schemaVersion": "rag-ime.swe-bench-agent-preflight.v1",
        "ready": all(value is True for value in required),
        "localOnly": True,
        "uploaded": False,
        "dataset": (
            {
                "fullCount": len(dataset.full_instance_ids),
                "interviewCount": len(dataset.interview_instance_ids),
                "sourceSha256": dataset.source.get("sha256"),
                "interviewSplitSha256": dataset.interview_split_sha256,
            }
            if dataset is not None
            else {}
        ),
        "modelReference": DEFAULT_MODEL_REFERENCE,
        "thinkingLevel": "max",
        "checks": checks,
        "failureDetails": failure_details,
        "managedPi": managed_pi,
        "environment": {
            "platform": platform.platform(),
            "architecture": platform.machine(),
            "freeDiskBytes": usage.free,
            "docker": docker_detail,
        },
    }


def _run_case(
    args: argparse.Namespace,
) -> tuple[dict[str, object], dict[str, str], dict[str, object]]:
    dataset = load_prepared_swe_bench(args.prepared)
    case = dataset.select(
        [args.instance_id],
        interview_only=not args.full_split,
    )[0]
    verifier_case = dataset.verifier_cases[case.instance_id]
    source_repository = args.source_repository.expanduser().resolve(strict=True)
    source_agent_config = args.source_agent_config.expanduser().resolve(strict=True)
    private_root = args.private_root.expanduser().resolve(strict=False)
    private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_root.chmod(0o700)
    started_at_ms = int(time.time() * 1_000)
    event_payloads: list[dict[str, object]] = []
    terminal_event = ""
    ensure: dict[str, object] = {}
    prompt_receipt: dict[str, object] = {}
    failure = ""
    stage_receipt: dict[str, object] = {}
    patch_receipt: dict[str, object] = {
        "modelPatch": "",
        "modelPatchSha256": hashlib.sha256(b"").hexdigest(),
        "modelPatchBytes": 0,
        "emptyPatch": True,
        "changedPaths": [],
        "changedPathCount": 0,
        "testMutationPaths": [],
        "hiddenVerifierTestOverlap": [],
        "binaryPatch": False,
        "integrityPassed": True,
    }
    with tempfile.TemporaryDirectory(prefix=f"{case.instance_id}-", dir=private_root) as raw:
        run_root = Path(raw).resolve(strict=True)
        run_root.chmod(0o700)
        workspace = run_root / "workspace"
        service: AgentService | None = None
        spool: RagBenchmarkAgentSpoolGateway | None = None
        try:
            stage_receipt = stage_agent_workspace(
                case,
                source_repository=source_repository,
                workspace=workspace,
            )
            agent_config = run_root / "agent" / "config"
            _copy_private_agent_config(source_agent_config, agent_config)
            runtime_config = _isolated_runtime_config(run_root, agent_config=agent_config)
            workspace_harness = SweBenchWorkspaceHarness()
            deferred_gateway = _DeferredToolGateway()
            spool = RagBenchmarkAgentSpoolGateway(
                deferred_gateway,  # type: ignore[arg-type]
                spool_dir=run_root / "agent" / "tool-spool",
            ).start()
            service = AgentService(
                db_path=run_root / "agent.sqlite",
                runtime_config=runtime_config,
                project="swe-bench-agent-eval",
                tool_gateway_url=spool.tool_gateway_url,
                tool_gateway_token=spool.token,
                wake_scheduler_enabled=False,
                background_job_execution_owner=False,
            )
            gateway = ControlToolGateway(
                sessions=service.sessions,
                management=object(),
                core=object(),
                project=service.project,
                workspace_harness=workspace_harness,
                background_jobs=service.background_jobs,
                delegation=service.delegation,
                configuration_store=service.configuration_store,
                governed_skills=service.room_skill_policy,
                work_documents=service.work_documents,
            )
            service.bind_tool_manifest_provider(gateway.runtime_manifests)
            service.bind_approval_executor(gateway.apply_approval)
            gateway.bind_auto_approval_executor(service.auto_approve_pending)
            deferred_gateway.target = gateway
            session = service.create_session(
                {
                    "title": f"SWE-bench Verified: {case.instance_id}",
                    "mode": "coordinator",
                    "roleId": "companion-firstlight-v1",
                    "roleVersion": "1",
                    "toolProfileVersion": "control-center-v1",
                    "executionMode": "workspace_managed",
                    "workspaceRoots": [str(workspace)],
                    "workspaceScopeConfirmation": "APPROVE_WORKSPACE_SCOPE",
                }
            )["session"]
            session_id = str(session["id"])
            service.update_session(
                session_id,
                {
                    "mode": "coordinator",
                    "executionMode": "workspace_managed",
                    "workspaceRoots": [str(workspace)],
                    "workspaceScopeConfirmation": "APPROVE_WORKSPACE_SCOPE",
                    "toolAllowlistMode": "explicit",
                    "allowedTools": list(ALLOWED_TOOLS),
                    "projectContextEnabled": False,
                    "piSkillsEnabled": True,
                    "codexSkillsEnabled": False,
                },
            )
            ensure = service.ensure_runtime({"sessionId": session_id})
            _assert_luna_max(ensure)
            prompt_receipt = service.prompt(
                session_id,
                {
                    "message": _case_prompt(case.to_agent_payload(), args.subagent_budget),
                    "clientMessageId": f"swe-bench:{case.instance_id}:{started_at_ms}",
                },
            )
            event_payloads, terminal_event = _wait_for_terminal(
                service,
                session_id=session_id,
                turn_id=str(prompt_receipt.get("turnId") or ""),
                timeout_seconds=max(60.0, float(args.timeout_seconds)),
            )
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                if workspace.is_dir():
                    patch_receipt = capture_model_patch(
                        case,
                        source_repository=source_repository,
                        workspace=workspace,
                        verifier_case=verifier_case,
                    )
            except Exception as exc:
                if not failure:
                    failure = f"{type(exc).__name__}: {exc}"
            if service is not None:
                service.close()
            if spool is not None:
                spool.close()
    completed_at_ms = int(time.time() * 1_000)
    model_state = _public_model_state(ensure)
    tool_events = [
        item
        for item in event_payloads
        if item.get("eventType") in {"tool_started", "tool_finished"}
    ]
    finished_tool_events = [
        item for item in tool_events if item.get("eventType") == "tool_finished"
    ]
    tool_names = [
        str(payload.get("toolName") or "")
        for item in finished_tool_events
        if isinstance(payload := item.get("payload"), Mapping)
    ]
    inference_passed = (
        not failure
        and terminal_event == "turn_completed"
        and patch_receipt.get("emptyPatch") is False
        and patch_receipt.get("integrityPassed") is True
        and model_state.get("reference") == DEFAULT_MODEL_REFERENCE
        and model_state.get("thinkingLevel") == "max"
    )
    record: dict[str, object] = {
        "schemaVersion": RUN_SCHEMA_VERSION,
        "passed": False,
        "inferencePassed": inference_passed,
        "officiallyResolved": None,
        "localOnly": True,
        "uploaded": False,
        "instanceId": case.instance_id,
        "repo": case.repo,
        "baseCommit": case.base_commit,
        "difficulty": case.difficulty,
        "startedAtMs": started_at_ms,
        "completedAtMs": completed_at_ms,
        "elapsedMs": completed_at_ms - started_at_ms,
        "terminalEvent": terminal_event,
        "failure": failure,
        "modelReference": str(model_state.get("reference") or ""),
        "thinkingLevel": str(model_state.get("thinkingLevel") or ""),
        "protocolVersion": str(model_state.get("protocolVersion") or ""),
        "historyExposed": False,
        "networkUsed": False,
        "workspaceStage": stage_receipt,
        "promptTurnId": str(prompt_receipt.get("turnId") or ""),
        "subagentBudget": int(args.subagent_budget),
        "toolCallCount": len(finished_tool_events),
        "toolNames": tool_names,
        **{key: value for key, value in patch_receipt.items() if key != "modelPatch"},
    }
    record["runSha256"] = _sha256_json(record)
    prediction = official_prediction(
        instance_id=case.instance_id,
        model_patch=str(patch_receipt.get("modelPatch") or ""),
        model_name_or_path=DEFAULT_MODEL_REFERENCE,
    )
    trajectory = {
        "schemaVersion": "rag-ime.swe-bench-agent-trajectory.v1",
        "localOnly": True,
        "uploaded": False,
        "instanceId": case.instance_id,
        "problemStatement": case.problem_statement,
        "hintsText": case.hints_text,
        "events": event_payloads,
        "eventCount": len(event_payloads),
        "trajectorySha256": _sha256_json(event_payloads),
    }
    return record, prediction, trajectory


def _aggregate(args: argparse.Namespace) -> dict[str, object]:
    dataset = load_prepared_swe_bench(args.prepared)
    records = [
        json.loads(path.expanduser().resolve(strict=True).read_text(encoding="utf-8"))
        for path in args.run_record
    ]
    instance_ids = [str(item.get("instanceId") or "") for item in records]
    official = (
        load_official_instance_results(args.official_results)
        if args.official_results is not None
        else None
    )
    return build_agent_eval_report(
        dataset=dataset,
        selected_instance_ids=instance_ids,
        run_records=records,
        official_results=official,
        interview_only=not args.full_split,
    )


def _isolated_runtime_config(run_root: Path, *, agent_config: Path) -> PiRuntimeConfig:
    source_app_support = Path(
        os.environ.get("RAG_IME_APP_SUPPORT_DIR")
        or Path.home() / "Library" / "Application Support" / "RagIme"
    ).expanduser()
    installation = snapshot_managed_pi_runtime(
        source_app_support,
        expected_pi_version=os.environ.get("RAG_IME_PI_VERSION", "").strip(),
    )
    overrides = {
        "RAG_IME_APP_SUPPORT_DIR": str(run_root / "runtime-support"),
        "RAG_IME_PI_EXECUTABLE": str(installation.executable),
        "RAG_IME_PI_EXTENSION": str(installation.extension_path),
        "RAG_IME_PI_NODE": installation.node_executable,
        "RAG_IME_PI_VERSION": installation.pi_version,
        "RAG_IME_PI_PROTOCOL_VERSION": installation.protocol_version,
        "RAG_IME_PI_TOOLS": ",".join(installation.tools),
    }
    missing = object()
    previous: dict[str, str | object] = {
        name: os.environ.get(name, missing) for name in overrides
    }
    try:
        os.environ.update(overrides)
        discovered = PiRuntimeConfig.from_environment(enabled_default=True)
    finally:
        for name, value in previous.items():
            if value is missing:
                os.environ.pop(name, None)
            else:
                os.environ[name] = str(value)
    if discovered.executable is None or discovered.extension_path is None:
        raise RuntimeError(
            "managed Pi v2 runtime is unavailable: "
            + str(discovered.installation_error or "missing executable/extension")
        )
    provider_environment = dict(discovered.provider_environment)
    provider_environment.update(
        {
            "RAG_IME_BENCHMARK_GATEWAY_SPOOL": str(run_root / "agent" / "tool-spool"),
            "RAG_IME_BENCHMARK_RUNTIME_ENTRYPOINT": str(installation.executable),
            "RAG_IME_BENCHMARK_GATEWAY_TIMEOUT_MS": "180000",
        }
    )
    return replace(
        discovered,
        enabled=True,
        executable=ROOT / "scripts" / "rag_agent_spool_runtime_wrapper.mjs",
        agent_dir=agent_config,
        session_dir=run_root / "agent" / "sessions",
        logs_dir=run_root / "agent" / "logs",
        debug_context_dir=run_root / "agent" / "debug-context",
        provider="openai-codex",
        model=DEFAULT_AGENT_MODEL_ID,
        model_configured=True,
        model_configuration_error="",
        provider_environment=provider_environment,
        protocol_version="2",
        idle_timeout_seconds=0,
        command_timeout_seconds=180.0,
        max_sessions=3,
    )


def _copy_private_agent_config(source: Path, target: Path) -> None:
    target.mkdir(parents=True, mode=0o700)
    target.chmod(0o700)
    copied = []
    for name in ("auth.json", "models.json", "models-store.json", "settings.json"):
        source_path = source / name
        if not source_path.is_file():
            continue
        target_path = target / name
        shutil.copy2(source_path, target_path)
        target_path.chmod(0o600)
        copied.append(name)
    if "auth.json" not in copied:
        raise RuntimeError("isolated Agent config requires an existing auth.json")


def _case_prompt(case: Mapping[str, str], subagent_budget: int) -> str:
    delegation = (
        f"You may use at most {subagent_budget} read-only subagent(s) for independent "
        "localization or review. The parent alone may edit files and owns the final patch."
        if subagent_budget
        else "Do not delegate this case."
    )
    hints = str(case.get("hintsText") or "").strip()
    return (
        "Solve this SWE-bench Verified issue in the authorized history-free workspace. "
        "Continue through inspection, implementation, and bounded local verification; do not stop at analysis.\n\n"
        f"Instance: {case['instanceId']}\nRepository: {case['repo']}\n"
        f"Base commit: {case['baseCommit']}\nDifficulty label: {case['difficulty']}\n\n"
        f"Issue:\n{case['problemStatement']}\n\n"
        + (f"Public hints:\n{hints}\n\n" if hints else "")
        + "Rules:\n"
        "- Use only the disclosed workspace and delegation Tools. Network access is disabled.\n"
        "- The workspace intentionally has no .git metadata. Do not request commit history or future fixes.\n"
        "- Never create, edit, rename, or delete tests. Hidden verifier tests belong only to the evaluator.\n"
        "- Read the relevant source before every edit. Keep the patch minimal and production-quality.\n"
        "- Shell is only for allowlisted non-mutating test/check commands; use workspace Tools for code changes.\n"
        f"- {delegation}\n"
        "- If a local test cannot run because the benchmark environment is absent, record the exact failure and still finish the best source-grounded patch.\n"
        "End with a concise summary of the fix and local checks. Do not output a patch fence; the harness captures the workspace diff independently."
    )


def _wait_for_terminal(
    service: AgentService,
    *,
    session_id: str,
    turn_id: str,
    timeout_seconds: float,
) -> tuple[list[dict[str, object]], str]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        events, gap = service.events.replay(session_id)
        if gap:
            raise RuntimeError("Agent event replay developed a gap")
        payloads = [event.to_payload() for event in events]
        terminal = next(
            (
                event.event_type
                for event in reversed(events)
                if event.turn_id == turn_id
                and event.event_type in {"turn_completed", "turn_failed"}
            ),
            "",
        )
        if terminal:
            return payloads, terminal
        time.sleep(0.1)
    service.abort(session_id)
    raise TimeoutError(f"Agent turn did not settle within {timeout_seconds:.1f}s")


def _assert_luna_max(ensure: Mapping[str, object]) -> None:
    state = _public_model_state(ensure)
    if state.get("reference") != DEFAULT_MODEL_REFERENCE or state.get("thinkingLevel") != "max":
        raise RuntimeError("managed Agent runtime did not open Luna at max thinking")


def _public_model_state(ensure: Mapping[str, object]) -> dict[str, str]:
    state = ensure.get("state")
    state = state if isinstance(state, Mapping) else {}
    model = state.get("model")
    model = model if isinstance(model, Mapping) else {}
    provider = str(model.get("provider") or "")
    model_id = str(model.get("id") or "")
    return {
        "reference": f"{provider}/{model_id}" if provider and model_id else "",
        "thinkingLevel": str(state.get("thinkingLevel") or ""),
        "protocolVersion": str(state.get("protocolVersion") or "2"),
        "runtimeVersion": str(state.get("runtimeVersion") or ""),
    }


def _write_json(path: str | Path, value: object) -> None:
    target = Path(path).expanduser().resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    serialized = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(serialized, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(target)


def _sha256_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
