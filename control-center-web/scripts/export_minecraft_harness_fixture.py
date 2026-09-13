#!/usr/bin/env python3
"""Export one privacy-safe, production-shaped PAW Session/Room fixture bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any


ROOM_ALIAS = "room:00000000-0000-4000-8000-000000000001"
SESSION_ROLE_NAMES = ("coordinator", "reviewer", "core-specialist", "ui-specialist")
PROJECT_ALIAS = "/workspace/minecraft-harness-20260825"
ID_PATTERNS = (
    ("room", re.compile(r"room:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")),
    ("participant", re.compile(r"participant:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")),
    ("agent", re.compile(r"agent:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")),
    ("room-work", re.compile(r"room-work:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")),
    ("room-turn", re.compile(r"room-turn:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")),
    ("room-dispatch", re.compile(r"room-dispatch:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")),
    ("room-child", re.compile(r"room-child:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")),
    ("topic", re.compile(r"topic:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")),
    ("goal", re.compile(r"goal:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")),
)


class Sanitizer:
    def __init__(self, project_root: Path, room_id: str, session_ids: list[str]) -> None:
        self.project_root = str(project_root)
        self.aliases: dict[str, str] = {room_id: ROOM_ALIAS}
        self.counters: dict[str, int] = {}
        for index, session_id in enumerate(session_ids, start=1):
            self.aliases[session_id] = f"agent:00000000-0000-4000-8000-{index:012d}"

    def alias(self, kind: str, source: str) -> str:
        if source in self.aliases:
            return self.aliases[source]
        self.counters[kind] = self.counters.get(kind, 0) + 1
        value = self.counters[kind]
        prefix = kind + ":"
        self.aliases[source] = f"{prefix}10000000-0000-4000-8000-{value:012d}"
        return self.aliases[source]

    def text(self, value: str) -> str:
        value = value.replace(self.project_root, PROJECT_ALIAS)
        value = re.sub(r"/Volumes/[^/\n]+/git/", "/workspace/", value)
        value = re.sub(r"/Users/[^/\n]+/", "/Users/example/", value)
        for source, alias in tuple(self.aliases.items()):
            value = value.replace(
                urllib.parse.quote(source, safe=""),
                urllib.parse.quote(alias, safe=""),
            )
        for kind, pattern in ID_PATTERNS:
            value = pattern.sub(lambda match, kind=kind: self.alias(kind, match.group(0)), value)
        value = re.sub(
            r"workdoc_[0-9a-f]{32}",
            lambda match: "workdoc_" + hashlib.sha256(match.group(0).encode()).hexdigest()[:32],
            value,
        )
        value = re.sub(
            r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])",
            lambda match: hashlib.sha256(("fixture:" + match.group(0)).encode()).hexdigest(),
            value,
        )
        return value

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            return {key: self.value(item) for key, item in value.items()}
        return value


def fetch_json(gateway: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(gateway.rstrip("/") + path, timeout=30) as response:
        return json.load(response)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def project_archive(
    project_root: Path,
    destination: Path,
    sanitizer: Sanitizer,
) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(project_root.rglob("*")):
            relative = path.relative_to(project_root)
            if not path.is_file() or any(part in {".git", "node_modules", ".DS_Store"} for part in relative.parts):
                continue
            data = path.read_bytes()
            try:
                data = sanitizer.text(data.decode("utf-8")).encode("utf-8")
            except UnicodeDecodeError:
                pass
            info = zipfile.ZipInfo(str(relative), (2026, 8, 25, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
            files.append({
                "path": str(relative),
                "sizeBytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
    return files


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gateway", default="http://127.0.0.1:8768")
    parser.add_argument("--room-id", required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--room-jsonl", type=Path, required=True)
    parser.add_argument("--acceptance-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    output = args.output.resolve()
    preserved_readme = (output / "README.md").read_text() if (output / "README.md").exists() else ""
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    if preserved_readme:
        (output / "README.md").write_text(preserved_readme)
    quoted_room = urllib.parse.quote(args.room_id, safe="")
    raw_room_get = fetch_json(args.gateway, f"/api/agent/rooms/{quoted_room}")
    participants = sorted(
        raw_room_get["room"]["participants"],
        key=lambda participant: int(participant.get("ordinal", 0)),
    )
    if len(participants) != len(SESSION_ROLE_NAMES):
        raise RuntimeError("this fixture requires exactly four visible Room participants")
    session_roles = {
        str(participant["sessionId"]): role
        for participant, role in zip(participants, SESSION_ROLE_NAMES, strict=True)
    }
    sanitizer = Sanitizer(
        args.project_root.resolve(),
        args.room_id,
        list(session_roles),
    )

    room_get = sanitizer.value(raw_room_get)
    room_snapshot = sanitizer.value(fetch_json(args.gateway, f"/api/agent/rooms/{quoted_room}/snapshot"))
    write_json(output / "room" / "get.json", room_get)
    write_json(output / "room" / "snapshot.json", room_snapshot)

    events: list[dict[str, Any]] = []
    with args.room_jsonl.open() as source, (output / "room" / "history.jsonl").open("w") as target:
        for line in source:
            if not line.strip():
                continue
            event = sanitizer.value(json.loads(line))
            events.append(event)
            target.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")

    session_files: list[dict[str, Any]] = []
    for session_id, role in session_roles.items():
        quoted = urllib.parse.quote(session_id, safe="")
        snapshot = sanitizer.value(fetch_json(args.gateway, f"/api/agent/sessions/{quoted}/messages"))
        relative = Path("sessions") / f"{role}.snapshot.json"
        write_json(output / relative, snapshot)
        session_files.append({
            "role": role,
            "sessionId": sanitizer.aliases[session_id],
            "path": str(relative),
            "messageCount": len(snapshot.get("items", [])),
            "liveEventCount": len(snapshot.get("liveEvents", [])),
            "lastSequence": snapshot.get("lastSequence", 0),
        })

    project_files = project_archive(
        args.project_root.resolve(),
        output / "project" / "minecraft-harness-project.zip",
        sanitizer,
    )
    write_json(output / "project" / "files.json", {"root": PROJECT_ALIAS, "items": project_files})
    acceptance = sanitizer.text(args.acceptance_report.read_text())
    (output / "ACCEPTANCE.md").write_text(acceptance)

    scene_index = {
        "schemaVersion": "pawos.minecraft-harness-scenes.v1",
        "roomId": ROOM_ALIAS,
        "scenes": [
            {"id": "initial-parallel-build", "fromSequence": 1, "toSequence": 2033, "purpose": "并行实现、伙伴回执、失败审查与第一轮收敛"},
            {"id": "browser-blocker-and-recovery", "fromSequence": 2034, "toSequence": 2665, "purpose": "重复继续、Browser 阻塞、失败结果与恢复"},
            {"id": "short-resume-turns", "fromSequence": 2668, "toSequence": 2888, "purpose": "短输入继续、恢复态与无重复旧操作"},
            {"id": "durable-final-recovery", "fromSequence": 2889, "toSequence": 3261, "purpose": "重启后继续、重新验收、Ego 成功与唯一 Root 终态"},
        ],
        "expectedProblemStates": [
            "participant tool failure",
            "partner submitted but reviewer verdict is unverified",
            "Facilitator contradictory accept",
            "workspace job orphan and restart",
            "Browser attach/screenshot timeout and recovery",
            "Tool Agent timeout/abort",
            "single final Root result",
        ],
    }
    write_json(output / "scene-index.json", scene_index)

    archive_path = output / "project" / "minecraft-harness-project.zip"
    manifest = {
        "schemaVersion": "pawos.frontend-test-bundle.v1",
        "fixtureId": "minecraft-harness-20260825",
        "sourceKind": "installed-paw-blackbox-run",
        "privacy": "stable aliases; system prompts, credentials and unrelated machine history excluded",
        "room": {
            "id": ROOM_ALIAS,
            "eventCount": len(events),
            "firstSequence": events[0]["sequence"],
            "lastSequence": events[-1]["sequence"],
            "snapshotEventCount": len(room_snapshot.get("events", [])),
            "files": ["room/get.json", "room/snapshot.json", "room/history.jsonl"],
        },
        "sessions": session_files,
        "project": {
            "fileCount": len(project_files),
            "archive": "project/minecraft-harness-project.zip",
            "archiveBytes": archive_path.stat().st_size,
            "archiveSha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
            "fileIndex": "project/files.json",
        },
        "sceneIndex": "scene-index.json",
        "acceptance": "ACCEPTANCE.md",
    }
    write_json(output / "manifest.json", manifest)


if __name__ == "__main__":
    main()
