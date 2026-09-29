#!/usr/bin/env python3
"""Build/stage PAW's vendored Browser helpers without starting any browser."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (
    "package/ego-linux-host/package.json",
    "package/ego-linux-host/bin/ego-browser.mjs",
    "package/ego-linux-host/bin/ego-linux-hostd.mjs",
    "package/ego-linux-host/dist/cli.js",
    "package/ego-linux-host/dist/host-control.js",
    "package/ego-browser/package.json",
    "package/ego-browser/dist/src/run.js",
    "skills/ego-browser/SKILL.md",
    "LICENSE",
)
MARKER = "paw-ego-browser-runtime.json"


def validate_payload(root: Path, node: str) -> None:
    root = root.resolve()
    missing = [name for name in REQUIRED if not (root / name).is_file()]
    if missing:
        raise ValueError("Browser runtime is incomplete: " + ", ".join(missing))
    # --help returns before host discovery; imports prove runtime closure with
    # no node_modules, network, CDP connection, doctor, or browser launch.
    subprocess.run([node, str(root / REQUIRED[1]), "--help"], check=True,
                   capture_output=True, text=True, timeout=15)
    imports = [root / "package/ego-linux-host/dist/host-control.js",
               root / "package/ego-browser/dist/src/run.js"]
    subprocess.run([node, "--permission", f"--allow-fs-read={root}",
                    "--input-type=module", "-e",
                    ";".join(f"await import({json.dumps(path.as_uri())})" for path in imports)],
                   check=True, capture_output=True, text=True, timeout=15,
                   cwd=root)


def stage_payload(source: Path, output: Path, *, node: str, replace: bool = False) -> dict[str, object]:
    source, output = source.resolve(), output.absolute()
    if source == output or source in output.parents:
        raise ValueError("Browser runtime output must be outside its source tree")
    if output.exists() and not replace:
        raise ValueError(f"Browser runtime output exists (use --replace): {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".ego-stage-", dir=output.parent) as temporary:
        stage = (Path(temporary) / "upstream").resolve()
        stage.mkdir()
        for name in ("ego-browser", "ego-linux-host"):
            package = source / "package" / name
            target = stage / "package" / name
            target.mkdir(parents=True)
            shutil.copy2(package / "package.json", target / "package.json")
            shutil.copytree(package / "dist", target / "dist",
                            ignore=shutil.ignore_patterns("*.test.js", "*.test.mjs", "*.map"))
            if name == "ego-linux-host":
                shutil.copytree(package / "bin", target / "bin")
        shutil.copytree(source / "skills" / "ego-browser", stage / "skills" / "ego-browser")
        shutil.copy2(source / "LICENSE", stage / "LICENSE")
        validate_payload(stage, node)
        files = {path.relative_to(stage).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in sorted(stage.rglob("*")) if path.is_file()}
        digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        marker = {"schemaVersion": "paw.ego-browser-runtime.v1", "treeSha256": digest, "files": files}
        (stage / MARKER).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
        backup = Path(temporary) / "previous"
        if output.exists():
            os.replace(output, backup)
        try:
            os.replace(stage, output)
        except BaseException:
            if backup.exists():
                os.replace(backup, output)
            raise
    return {"ok": True, "output": str(output), "treeSha256": digest, "fileCount": len(files)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "integrations/ego-browser/upstream")
    parser.add_argument("--output", type=Path, default=ROOT / "build/ego-browser-runtime/upstream")
    parser.add_argument("--node", default=os.environ.get("RAG_IME_EGO_NODE") or shutil.which("node"))
    parser.add_argument("--from-built", action="store_true", help="Stage already built artifacts; never install dependencies")
    parser.add_argument("--replace", action="store_true", help="Replace output only after the staged runtime passes checks")
    args = parser.parse_args()
    if not args.node:
        parser.error("Node.js 22+ is required; set --node")
    node = str(Path(args.node).resolve())
    major = int(subprocess.check_output([node, "--version"], text=True).strip().lstrip("v").split(".")[0])
    if major < 22:
        parser.error("Node.js 22+ is required")
    if not args.from_built:
        npm = shutil.which("npm")
        if not npm:
            parser.error("npm is required to build vendored Browser runtime")
        for name in ("ego-browser", "ego-linux-host"):
            package = args.source / "package" / name
            if not (package / "node_modules").is_dir():
                subprocess.run([npm, "ci", "--ignore-scripts"], cwd=package, check=True)
            subprocess.run([npm, "run", "build"], cwd=package, check=True)
    print(json.dumps(stage_payload(args.source, args.output, node=node, replace=args.replace)))


if __name__ == "__main__":
    main()
