"""Test-only child stack samples while waiting for the real readiness signal.

The temporary sitecustomize is inherited across launch.py's exec into app.py.
It neither patches product code nor extends a readiness deadline.
"""

from __future__ import annotations

import os
from pathlib import Path


class StartupDiagnostics:
    def __init__(self, directory: Path, *, sample_seconds: float = 2.0) -> None:
        if sample_seconds <= 0:
            raise ValueError("startup sample interval must be positive")
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.marker = directory / "ready"
        self.marker.unlink(missing_ok=True)
        source = (
            "import faulthandler, sys, threading, time\n"
            "from pathlib import Path\n"
            f"_ready = Path({str(self.marker)!r})\n"
            "def _sample_startup():\n"
            "    while not _ready.exists():\n"
            f"        time.sleep({sample_seconds!r})\n"
            "        if _ready.exists():\n"
            "            return\n"
            "        try:\n"
            "            print('startup: still waiting for readiness', file=sys.stderr, flush=True)\n"
            "            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)\n"
            "        except (OSError, ValueError):\n"
            "            return\n"
            "print('startup: interpreter diagnostics ready', file=sys.stderr, flush=True)\n"
            "threading.Thread(target=_sample_startup, daemon=True).start()\n"
        )
        (directory / "sitecustomize.py").write_text(source, encoding="utf-8")

    def environment(self, inherited: dict[str, str] | None = None) -> dict[str, str]:
        environment = dict(os.environ if inherited is None else inherited)
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = str(self.directory) + (os.pathsep + existing if existing else "")
        return environment

    def ready(self) -> None:
        self.marker.touch()
