#!/usr/bin/env python3
"""Verify a large Memory packet through a private Gateway-owned Luna Session."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag_ime.agent_model_defaults import DEFAULT_AGENT_MODEL_PROFILE
from rag_ime.managed_pi_runtime import discover_managed_pi_runtime
from rag_ime.memory_near_budget_evaluation import (
    DEFAULT_NEAR_BUDGET_PAYLOAD_CHARS,
    SqliteConnectionAudit,
    build_near_budget_case,
    memory_near_budget_messages,
    parse_near_budget_verdict,
    redacted_near_budget_summary,
    render_near_budget_public_report,
)


DEFAULT_PRODUCTION_DB = (
    Path.home() / "Library" / "Application Support" / "RagIme" / "rag-ime.sqlite"
)
DEFAULT_RUNTIME_APP_SUPPORT = (
    Path.home() / "Library" / "Application Support" / "RagIme"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Send a deterministic large synthetic packet through the real "
            "Gateway Memory executor and Luna/max without opening production SQLite."
        )
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--private-app-support", type=Path, required=True)
    parser.add_argument("--private-report", type=Path, required=True)
    parser.add_argument("--public-report", type=Path)
    parser.add_argument("--payload-chars", type=int, default=DEFAULT_NEAR_BUDGET_PAYLOAD_CHARS)
    parser.add_argument("--timeout-seconds", type=float, default=1_200.0)
    parser.add_argument("--project", default="memory-near-budget-control")
    parser.add_argument("--seed", default="gateway-memory-transport-v1")
    parser.add_argument("--production-db", type=Path, default=DEFAULT_PRODUCTION_DB)
    parser.add_argument(
        "--runtime-app-support",
        type=Path,
        default=DEFAULT_RUNTIME_APP_SUPPORT,
        help="Read-only source of the already installed managed Pi runtime.",
    )
    parser.add_argument(
        "--provider-config",
        type=Path,
        default=DEFAULT_RUNTIME_APP_SUPPORT / "pi-providers.json",
        help="Read-only Provider catalog; native openai-codex auth stays in private app support.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = build_parser().parse_args(argv)
    db_path = args.db.expanduser().resolve(strict=True)
    private_app_support = args.private_app_support.expanduser().resolve(strict=True)
    private_report = args.private_report.expanduser().resolve(strict=False)
    production_db = args.production_db.expanduser().resolve(strict=False)
    runtime_app_support = args.runtime_app_support.expanduser().resolve(strict=True)
    provider_config = args.provider_config.expanduser().resolve(strict=True)

    _verify_private_file(db_path, label="shadow database")
    _verify_private_directory(private_app_support, label="private app support")
    _verify_private_file(
        private_app_support / "Agent" / "config" / "auth.json",
        label="private native Provider auth",
    )
    if production_db.exists() and os.path.samefile(db_path, production_db):
        raise SystemExit("--db must not resolve to production SQLite")
    if _path_within(db_path, ROOT) or _path_within(private_app_support, ROOT):
        raise SystemExit("private evaluation state must remain outside the Git worktree")
    production_before = _file_identity(production_db)

    installation = discover_managed_pi_runtime(
        runtime_app_support,
        expected_pi_version=os.environ.get("RAG_IME_PI_VERSION", "").strip(),
    )
    _configure_private_gateway_environment(
        db_path=db_path,
        private_app_support=private_app_support,
        provider_config=provider_config,
        installation=installation,
    )

    # Imports happen after the private runtime boundary is frozen so no source
    # module can accidentally capture the installed App Support path.
    from rag_ime.debug_server import DebugImeService, DebugServerConfig
    from rag_ime.memory_model_executor import build_governed_memory_model_executor

    case = build_near_budget_case(int(args.payload_chars), seed=str(args.seed))
    source_hashes = {
        "rag_ime/memory_model_executor.py": _sha256_file(
            ROOT / "rag_ime" / "memory_model_executor.py"
        ),
        "rag_ime/memory_near_budget_evaluation.py": _sha256_file(
            ROOT / "rag_ime" / "memory_near_budget_evaluation.py"
        ),
        "scripts/eval_memory_near_budget_gateway_luna.py": _sha256_file(
            Path(__file__).resolve(strict=True)
        ),
    }
    run_key = _sha256_text(
        case.payload_sha256
        + "\0"
        + source_hashes["rag_ime/memory_near_budget_evaluation.py"]
    )
    run_id = f"memory_near_budget_{run_key[:32]}"
    service = None
    executor = None
    response: Mapping[str, object] = {}
    verdict: Mapping[str, object] = {}
    connection_audit = SqliteConnectionAudit(
        private_root=db_path.parent,
        production_db=production_db,
    )
    with connection_audit:
        try:
            service = DebugImeService(
                DebugServerConfig(
                    db_path=db_path,
                    project=str(args.project),
                    seed_if_empty=False,
                    server_name="agent gateway",
                    memory_projection_worker_enabled=False,
                )
            )
            executor = build_governed_memory_model_executor(
                service.agent.runtime,
                DEFAULT_AGENT_MODEL_PROFILE,
                "max",
                timeout_seconds=float(args.timeout_seconds),
                db_path=db_path,
            )
            executor.begin_run(run_id, frozen_input_sha256=case.payload_sha256)
            response = executor.complete(
                phase="near-budget-transport-verification",
                messages=memory_near_budget_messages(case),
                max_tokens=256,
            )
            choices = response.get("choices")
            if not isinstance(choices, list) or not choices:
                raise ValueError("near-budget model response has no assistant choice")
            first = choices[0]
            message = first.get("message") if isinstance(first, Mapping) else None
            output_text = message.get("content") if isinstance(message, Mapping) else ""
            verdict = parse_near_budget_verdict(output_text, case)
            executor.finish_run(state="completed")
        except BaseException as exc:
            if executor is not None:
                executor.fail_run(exc)
            raise
        finally:
            if executor is not None:
                executor.close()
            if service is not None:
                service.close()

    production_identity_changed = _file_identity(production_db) != production_before
    summary = redacted_near_budget_summary(
        case,
        response=response,
        verdict=verdict,
        runtime_manifest_sha256=installation.manifest_sha256,
        source_hashes=source_hashes,
        production_access=connection_audit.summary(),
        production_file_identity_changed=production_identity_changed,
    )
    summary["runIdSha256"] = _sha256_text(run_id)
    summary["privateArtifactDirectorySha256"] = _sha256_text(str(db_path.parent))
    _write_secure_json(private_report, summary)
    if args.public_report is not None:
        _write_secure_text(
            args.public_report.expanduser().resolve(strict=False),
            render_near_budget_public_report(summary),
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if bool(summary.get("passed")) else 1


def _configure_private_gateway_environment(
    *,
    db_path: Path,
    private_app_support: Path,
    provider_config: Path,
    installation: object,
) -> None:
    executable = Path(getattr(installation, "executable")).resolve(strict=True)
    extension = Path(getattr(installation, "extension_path")).resolve(strict=True)
    node = str(getattr(installation, "node_executable") or "")
    if not node or not Path(node).resolve(strict=True).is_file():
        raise SystemExit("managed Pi Node executable is unavailable")
    os.environ.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "RAG_IME_ROOT": str(ROOT),
            "RAG_IME_SOURCE_ROOT": str(ROOT),
            "RAG_IME_APP_SUPPORT_DIR": str(private_app_support),
            "RAG_IME_DB_PATH": str(db_path),
            "RAG_IME_PI_ENABLED": "1",
            "RAG_IME_PI_VERSION": str(getattr(installation, "pi_version")),
            "RAG_IME_PI_PROTOCOL_VERSION": str(
                getattr(installation, "protocol_version")
            ),
            "RAG_IME_PI_EXECUTABLE": str(executable),
            "RAG_IME_PI_NODE": str(Path(node).resolve(strict=True)),
            "RAG_IME_PI_EXTENSION": str(extension),
            "RAG_IME_PI_PROVIDER_CONFIG": str(provider_config),
            "RAG_IME_PI_DEBUG_CONTEXT_DIR": str(
                private_app_support / "Agent" / "debug-context"
            ),
            "RAG_IME_AGENT_TOOL_URL": "http://127.0.0.1:1/api/agent/tool/execute",
            "RAG_IME_KNOWLEDGE_SHARED_WORKER": "0",
        }
    )
    # openai-codex is a native authenticated Pi Provider selected per Session,
    # not an imported provider-config key. Inherited explicit overrides would
    # fail before set_model and therefore must not cross this evaluation fence.
    os.environ.pop("RAG_IME_PI_PROVIDER", None)
    os.environ.pop("RAG_IME_PI_MODEL", None)


def _verify_private_directory(path: Path, *, label: str) -> None:
    if not path.is_dir() or path.is_symlink():
        raise SystemExit(f"{label} must be a real directory")
    if path.stat().st_mode & 0o077:
        raise SystemExit(f"{label} must not grant group or other permissions")


def _verify_private_file(path: Path, *, label: str) -> None:
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f"{label} must be a real file")
    if path.stat().st_mode & 0o077:
        raise SystemExit(f"{label} must not grant group or other permissions")


def _file_identity(path: Path) -> dict[str, int] | None:
    if not path.is_file():
        return None
    value = path.stat()
    return {
        "device": int(value.st_dev),
        "inode": int(value.st_ino),
        "size": int(value.st_size),
        "mtimeNs": int(value.st_mtime_ns),
    }


def _path_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent.resolve(strict=True))
    except ValueError:
        return False
    return True


def _write_secure_json(path: Path, payload: Mapping[str, object]) -> None:
    _write_secure_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _write_secure_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(text, encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
