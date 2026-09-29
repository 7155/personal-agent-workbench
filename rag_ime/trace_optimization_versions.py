"""Host-frozen source versions and execution plans for Trace optimization.

Agents submit paths and proposals, never version digests or execution receipts.
Registered sources are copied once; every subsequent read checks the snapshot.
This store does not install resources or execute an Agent loop.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Mapping, Sequence

from .db import apply_database_migrations, sqlite_connection

KINDS = frozenset({"tool", "skill", "prompt", "workflow", "model"})
_SKIP = {".git", "node_modules", "__pycache__", ".venv"}


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def text(value: object, name: str, maximum: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} is invalid")
    return value


def source_path(value: object, roots: Sequence[str]) -> Path:
    path = Path(text(value, "sourcePath", 4096)).expanduser().resolve(strict=True)
    allowed = [Path(root).expanduser().resolve(strict=True) for root in roots if root and root != "/"]
    if not allowed or not any(path == root or path.is_relative_to(root) for root in allowed):
        raise ValueError("sourcePath is outside the report's bound project")
    return path


def command_entry(command: Sequence[str]) -> str:
    """Identify the frozen executable, not merely an echoed version path."""
    argument = command[0] if command else ""
    if not argument.startswith("{version}/"):
        if Path(argument).name not in {"python", "python3", "node", "bash", "sh", "zsh", "ruby", "perl"} or len(command) < 2:
            return ""
        argument = command[1]
    if not argument.startswith("{version}/"):
        return ""
    relative = argument[len("{version}/"):]
    path = Path(relative)
    return relative if relative and not path.is_absolute() and ".." not in path.parts and "{version}" not in relative else ""


def normalize_local_plan(plan: Mapping) -> dict:
    if set(plan) - {"executionKind", "title", "cases", "timeoutSeconds"} or plan.get("executionKind", "local_command") != "local_command":
        raise ValueError("invalid local execution plan")
    cases = plan.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 32:
        raise ValueError("execution plan needs 1 to 32 frozen cases")
    ids = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != {"caseId", "command", "input", "expectedStdout"}:
            raise ValueError("each case needs caseId, command, input and expectedStdout")
        identifier = text(case["caseId"], "caseId", 120)
        if identifier in ids:
            raise ValueError("duplicate caseId")
        ids.add(identifier)
        command = case["command"]
        if not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg for arg in command):
            raise ValueError("case command must be a nonempty argv list")
        if not command_entry(command):
            raise ValueError("case command must execute a registered file directly or through a supported interpreter")
        if not isinstance(case["expectedStdout"], str):
            raise ValueError("expectedStdout must be a frozen string")
        canonical(case["input"])
    timeout = plan.get("timeoutSeconds", 30)
    if type(timeout) is not int or not 1 <= timeout <= 120:
        raise ValueError("timeoutSeconds must be between 1 and 120")
    return {**plan, "executionKind": "local_command", "timeoutSeconds": timeout}


class TraceOptimizationVersionStore:
    def __init__(self, db_path: str | Path, *, artifact_root: str | Path | None = None) -> None:
        self.db_path = Path(db_path)
        self.root = Path(artifact_root) if artifact_root else self.db_path.parent / "TraceOptimizationArtifacts"

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite_connection(self.db_path, foreign_keys=True) as conn:
            apply_database_migrations(conn)

    def register(self, report_id: str, value: Mapping[str, object], *, roots: Sequence[str]) -> dict:
        if set(value) - {"targetKind", "targetRef", "sourcePath", "entryPath", "destinationPath"}:
            raise ValueError("unsupported version registration fields")
        kind = text(value.get("targetKind"), "targetKind")
        if kind not in KINDS - {"model"}:
            raise ValueError("file version registration supports tool, skill, prompt and workflow")
        target = text(value.get("targetRef"), "targetRef")
        source = source_path(value.get("sourcePath"), roots)
        base = source if source.is_dir() else source.parent
        paths = [source] if source.is_file() else sorted(
            path for path in source.rglob("*")
            if path.is_file() and not any(part in _SKIP for part in path.relative_to(source).parts)
        )
        if not paths or len(paths) > 512:
            raise ValueError("register one bounded component (1 to 512 files)")
        files: dict[str, bytes] = {}
        modes: dict[str, int] = {}
        for path in paths:
            if path.is_symlink() or not path.resolve().is_relative_to(base.resolve()):
                raise ValueError("version sources must not contain external symlinks")
            files[path.relative_to(base).as_posix()] = path.read_bytes()
            modes[path.relative_to(base).as_posix()] = path.stat().st_mode & 0o777
            if sum(map(len, files.values())) > 8 * 1024 * 1024:
                raise ValueError("component snapshot exceeds 8 MiB")
        entry = str(value.get("entryPath") or (source.name if source.is_file() else "package.json"))
        if entry not in files:
            raise ValueError("entryPath must name a file in this component")
        destination = str(value.get("destinationPath") or source)
        destination_path = Path(destination).expanduser().resolve(strict=False)
        allowed = [Path(root).expanduser().resolve() for root in roots if root != "/"]
        if not any(destination_path == root or destination_path.is_relative_to(root) for root in allowed):
            raise ValueError("destinationPath is outside the bound project")
        manifest = {name: hashlib.sha256(body).hexdigest() for name, body in files.items()}
        content_hash = digest({"files": manifest, "modes": modes})
        identity = digest({"reportId": report_id, "targetKind": kind, "targetRef": target,
                           "files": manifest, "modes": modes, "entryPath": entry, "destinationPath": str(destination_path)})
        version_ref = f"trace-version:{identity}"
        self.initialize()
        self.root.mkdir(parents=True, exist_ok=True)
        snapshot = self.root / identity
        if not snapshot.exists():
            staging = Path(tempfile.mkdtemp(prefix="version-", dir=self.root))
            try:
                for name, body in files.items():
                    output = staging / name
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(body)
                    output.chmod(modes[name])
                try:
                    staging.rename(snapshot)
                except OSError:
                    if not snapshot.is_dir():
                        raise
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        payload = {"versionRef": version_ref, "reportId": report_id, "targetKind": kind,
                   "targetRef": target, "contentSha256": content_hash, "manifest": manifest, "fileModes": modes,
                   "entryPath": entry, "snapshotPath": str(snapshot), "sourceWasFile": source.is_file(),
                   "destinationPath": str(destination_path), "createdAtMs": now_ms()}
        with sqlite_connection(self.db_path) as conn:
            conn.execute("INSERT OR IGNORE INTO trace_optimization_versions VALUES(?,?,?,?,?,?,?)",
                         (version_ref, report_id, kind, target, content_hash, canonical(payload), payload["createdAtMs"]))
        return self.public(self.get(version_ref))

    def get(self, version_ref: str) -> dict:
        self.initialize()
        with sqlite_connection(self.db_path) as conn:
            row = conn.execute("SELECT payload_json FROM trace_optimization_versions WHERE version_ref=?", (version_ref,)).fetchone()
        if row is None:
            raise KeyError(version_ref)
        result = json.loads(row[0])
        root = Path(result["snapshotPath"])
        for name, expected in result["manifest"].items():
            path = root / name
            if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("registered candidate snapshot has changed")
            if name in result.get("fileModes", {}) and path.stat().st_mode & 0o777 != result["fileModes"][name]:
                raise ValueError("registered candidate snapshot permissions have changed")
        actual_names = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
        if actual_names != set(result["manifest"]):
            raise ValueError("registered candidate snapshot contains unregistered files")
        return result

    def resolve(self, kind: str, version_ref: str) -> dict | None:
        try:
            row = self.get(version_ref)
        except KeyError:
            return None
        if row["targetKind"] != kind:
            return None
        return self.public(row)

    @staticmethod
    def public(row: Mapping) -> dict:
        entry = Path(row["snapshotPath"]) / row["entryPath"]
        content = entry.read_bytes()
        try:
            decoded = content.decode("utf-8") if len(content) <= 16000 else ""
        except UnicodeDecodeError:
            decoded = ""
        package = "package.json" in row["manifest"]
        actions = ["run_candidate"]
        if package:
            actions.extend(["install", "replace"])
        elif row["sourceWasFile"]:
            actions.extend(["apply", "replace"])
        return {key: row[key] for key in ("versionRef", "reportId", "targetKind", "targetRef", "contentSha256")} | {
            "content": decoded, "files": list(row["manifest"]),
            "availableActions": actions,
        }

    def list(self) -> list[dict]:
        self.initialize()
        with sqlite_connection(self.db_path) as conn:
            refs = [row[0] for row in conn.execute("SELECT version_ref FROM trace_optimization_versions ORDER BY created_at_ms DESC LIMIT 200")]
        return [self.public(self.get(ref)) for ref in refs]

    def register_plan(self, report_id: str, value: Mapping, *, roots: Sequence[str]) -> dict:
        if set(value) != {"sourcePath"}:
            raise ValueError("execution plan must be registered from a sourcePath")
        path = source_path(value["sourcePath"], roots)
        raw = path.read_bytes()
        if len(raw) > 256000:
            raise ValueError("execution plan exceeds 256 KB")
        plan = json.loads(raw)
        if not isinstance(plan, dict):
            raise ValueError("invalid execution plan")
        if plan.get("executionKind") == "pi_session":
            from .trace_optimization_pi import normalize_pi_plan
            plan = normalize_pi_plan(plan)
        else:
            plan = normalize_local_plan(plan)
        identity = digest({"reportId": report_id, "plan": plan})
        plan_ref = f"trace-plan:{identity}"
        result = {"planRef": plan_ref, "reportId": report_id, "contentSha256": digest(plan),
                  "plan": plan, "createdAtMs": now_ms()}
        self.initialize()
        with sqlite_connection(self.db_path) as conn:
            conn.execute("INSERT OR IGNORE INTO trace_optimization_execution_plans VALUES(?,?,?,?)",
                         (plan_ref, report_id, canonical(result), result["createdAtMs"]))
        return {key: result[key] for key in ("planRef", "reportId", "contentSha256")} | {
            "executionKind": plan["executionKind"], "caseIds": [case["caseId"] for case in plan["cases"]]}

    def get_plan(self, plan_ref: str) -> dict:
        self.initialize()
        with sqlite_connection(self.db_path) as conn:
            row = conn.execute("SELECT payload_json FROM trace_optimization_execution_plans WHERE plan_ref=?", (plan_ref,)).fetchone()
        if row is None:
            raise KeyError(plan_ref)
        return json.loads(row[0])

    def application_receipt(self, receipt_ref: str) -> dict | None:
        self.initialize()
        with sqlite_connection(self.db_path) as conn:
            row = conn.execute("SELECT payload_json FROM trace_optimization_application_receipts WHERE receipt_ref=?", (receipt_ref,)).fetchone()
        return json.loads(row[0]) if row else None

    def begin_application(self, request_key: str, request: Mapping) -> tuple[dict, bool]:
        self.initialize()
        identity = digest(request)
        with sqlite_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT request_hash,payload_json FROM trace_optimization_application_receipts WHERE request_key=?", (request_key,)).fetchone()
            if existing:
                if existing[0] != identity:
                    raise ValueError("application request ID is already bound to different input")
                return json.loads(existing[1]), True
            in_flight = conn.execute("""SELECT receipt_ref FROM trace_optimization_application_receipts
                WHERE json_extract(payload_json,'$.candidateId')=?
                  AND json_extract(payload_json,'$.versionRef')=?
                  AND status IN ('applying','interrupted','applied') LIMIT 1""",
                (request.get("candidateId"), request.get("versionRef"))).fetchone()
            if in_flight:
                raise ValueError("candidate version already has an applied or uncertain operation; inspect its original receipt")
            receipt = {**request, "receiptRef": f"trace-application:{digest([request_key,identity])}",
                       "status": "applying", "createdAtMs": now_ms()}
            conn.execute("INSERT INTO trace_optimization_application_receipts VALUES(?,?,?,?,?,?)",
                         (receipt["receiptRef"], request_key, identity, "applying", canonical(receipt), receipt["createdAtMs"]))
        return receipt, False

    def finish_application(self, receipt: Mapping, *, status: str, details: Mapping | None = None) -> dict:
        if status not in {"applied", "failed", "rolled_back", "interrupted"}:
            raise ValueError("invalid application status")
        result = {**receipt, "status": status, "completedAtMs": now_ms(), "details": dict(details or {})}
        with sqlite_connection(self.db_path) as conn:
            conn.execute("UPDATE trace_optimization_application_receipts SET status=?,payload_json=? WHERE receipt_ref=? AND status='applying'",
                         (status, canonical(result), receipt["receiptRef"]))
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise ValueError("application already settled")
        return result

    def apply_file(self, before: Mapping, after: Mapping) -> dict:
        if not before["sourceWasFile"] or not after["sourceWasFile"]:
            raise ValueError("directory resources require the Pi Package installation owner")
        target = Path(before["destinationPath"])
        expected = before["manifest"][before["entryPath"]]
        if not target.is_file() or target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise ValueError("active file differs from the validated parent version")
        original = target.stat()
        body = (Path(after["snapshotPath"]) / after["entryPath"]).read_bytes()
        if hashlib.sha256(body).hexdigest() != after["manifest"][after["entryPath"]]:
            raise ValueError("candidate content changed before file application")
        descriptor, filename = tempfile.mkstemp(prefix=f".{target.name}.trace-", dir=target.parent)
        temporary = Path(filename)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, original.st_mode)
            latest = target.stat()
            if target.is_symlink() or (latest.st_dev, latest.st_ino, latest.st_mtime_ns, latest.st_mode) != (original.st_dev, original.st_ino, original.st_mtime_ns, original.st_mode) or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise ValueError("active file changed while preparing replacement")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return {"contentSha256": hashlib.sha256(target.read_bytes()).hexdigest(), "replacesVersionRef": before["versionRef"]}
