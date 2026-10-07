"""Replay immutable RAG standards with their original, hash-checked runner."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FROZEN_COMMIT = "b2b33fb74ba8e5c8904b4a0af5b8e64d30b823d1"
RUNNER_PATH = "scripts/run_rag_agent_ablation.py"
RUNNER_SHA256 = "e90af777b337374d750c7a994f78d7cf28770faf33d15ea7f7672ab16ef781e4"


def frozen_source(relative: str) -> bytes:
    return subprocess.check_output(["git", "show", f"{FROZEN_COMMIT}:{relative}"], cwd=ROOT)


def frozen_source_sha256(relative: str) -> str:
    return hashlib.sha256(frozen_source(relative)).hexdigest()


def run_frozen_standard_verifier(script_name: str) -> subprocess.CompletedProcess[str]:
    if not (ROOT / ".rag-ime-data/eval/host").is_dir():
        raise unittest.SkipTest("Private RAG corpus is not bundled in public source")
    runner = frozen_source(RUNNER_PATH)
    if hashlib.sha256(runner).hexdigest() != RUNNER_SHA256:
        raise AssertionError("historical RAG runner hash mismatch")
    with tempfile.TemporaryDirectory(prefix="rag-frozen-standard-") as temporary:
        base = Path(temporary)
        replay = base / "source"
        replay.mkdir()
        (base / "paw-vertical-research").symlink_to(ROOT.parent / "paw-vertical-research", target_is_directory=True)
        # The runner hashes its entire chunking pipeline. Replaying it with the
        # current product package changes that identity even for identical text.
        shutil.copytree(
            ROOT / "rag_ime", replay / "rag_ime",
            copy_function=lambda source, target: Path(target).symlink_to(source),
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        for relative in (
            "rag_ime/knowledge_library/parsers.py",
            "rag_ime/knowledge_library/service.py",
        ):
            target = replay / relative
            target.unlink()  # Replace the temporary link, never its source.
            target.write_bytes(frozen_source(relative))
        for name in ("eval", ".rag-ime-data"):
            (replay / name).symlink_to(ROOT / name, target_is_directory=True)
        scripts = replay / "scripts"
        scripts.mkdir()
        for path in (ROOT / "scripts").iterdir():
            target = scripts / path.name
            if path.name == "run_rag_agent_ablation.py":
                target.write_bytes(runner)
            elif path.suffix == ".py":
                # Preserve __file__-based roots and the verifier's self hash.
                target.write_bytes(path.read_bytes())
            else:
                target.symlink_to(path, target_is_directory=path.is_dir())
        return subprocess.run(
            [sys.executable, f"scripts/{script_name}"], cwd=replay,
            text=True, capture_output=True, check=False,
        )
