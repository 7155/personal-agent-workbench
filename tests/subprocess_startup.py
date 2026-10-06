"""Test-only child stack samples while waiting for the real readiness signal.

The temporary sitecustomize is inherited across launch.py's exec into app.py.
It neither patches product code nor extends a readiness deadline.
"""

from __future__ import annotations

import os
import subprocess
from contextlib import ExitStack
from pathlib import Path


_REAL_POPEN = subprocess.Popen


class StartupDiagnostics:
    def __init__(self, directory: Path, *, sample_seconds: float = 2.0, trace_path: Path | None = None) -> None:
        if sample_seconds <= 0:
            raise ValueError("startup sample interval must be positive")
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.marker = directory / "ready"
        self.marker.unlink(missing_ok=True)
        output_setup = "_startup_stream = sys.stderr\n"
        if trace_path is not None:
            output_setup = (
                "import atexit\n"
                f"_startup_stream = open({str(trace_path)!r}, 'a', encoding='utf-8', buffering=1)\n"
                "atexit.register(_startup_stream.close)\n"
            )
        source = (
            "import faulthandler, sys, threading, time\n"
            "from pathlib import Path\n"
            f"_ready = Path({str(self.marker)!r})\n"
            f"{output_setup}"
            "def _sample_startup():\n"
            "    while not _ready.exists():\n"
            f"        time.sleep({sample_seconds!r})\n"
            "        if _ready.exists():\n"
            "            return\n"
            "        try:\n"
            "            print('startup: still waiting for readiness', file=_startup_stream, flush=True)\n"
            "            faulthandler.dump_traceback(file=_startup_stream, all_threads=True)\n"
            "        except (OSError, ValueError):\n"
            "            return\n"
            "print('startup: interpreter diagnostics ready', file=_startup_stream, flush=True)\n"
            "threading.Thread(target=_sample_startup, daemon=True).start()\n"
        )
        self.source = source
        (directory / "sitecustomize.py").write_text(source, encoding="utf-8")

    def environment(self, inherited: dict[str, str] | None = None) -> dict[str, str]:
        environment = dict(os.environ if inherited is None else inherited)
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = str(self.directory) + (os.pathsep + existing if existing else "")
        return environment

    def ready(self) -> None:
        self.marker.touch()

    def isolated_command(self, command: list[str]) -> list[str]:
        """Embed the same stdlib sampler without weakening -I/-S isolation.

        The exported file/archive is untouched. runpy executes the original
        target in this same interpreter and PID, preserving its program argv.
        """
        flags = []
        index = 1
        while index < len(command) and command[index] in {"-I", "-S", "-u", "-B", "-E"}:
            flags.append(command[index])
            index += 1
        if "-I" not in flags or index >= len(command):
            raise ValueError("isolated diagnostics require an explicit -I Python target")
        if command[index] == "-m":
            index += 1
            if index >= len(command):
                raise ValueError("isolated module name is required")
            run = "runpy.run_module(_target, run_name='__main__', alter_sys=True)"
        elif command[index].endswith(".py"):
            run = "runpy.run_path(_target, run_name='__main__')"
        else:
            raise ValueError("isolated diagnostics support only a Python file or module")
        wrapper = (self.source + "\nimport runpy\n_target = sys.argv[1]\n"
                   "sys.argv = sys.argv[1:]\n" + run + "\n")
        return [command[0], *flags, "-c", wrapper, *command[index:]]


class StartupCapture:
    """Failure-only, bounded diagnostic tails for owned synthetic test children.

    Callers keep their existing readiness deadlines and process ownership.
    Register close() as fallback cleanup outside this context, so exception
    notes describe the failure before cleanup. Temporary files preserve the
    original DEVNULL semantics; their displayed tails, not disk size, are capped.
    """

    def __init__(self, directory, *, label, isolated=False, match=None,
                 sample_seconds=2.0, tail_bytes=8192, popen=None):
        if not 1 <= tail_bytes <= 16384:
            raise ValueError("diagnostic tail must be between 1 and 16384 bytes")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.label = label
        self.isolated = isolated
        self.match = match
        self.sample_seconds = sample_seconds
        self.tail_bytes = tail_bytes
        self.real_popen = _REAL_POPEN if popen is None else popen
        self.children = []

    def __enter__(self):
        return self

    def __exit__(self, _kind, error, _traceback):
        if error is not None:
            error.add_note(self.failure_details())

    def _tail(self, value):
        if value is None:
            return b""
        if isinstance(value, str):
            value = value.encode("utf-8", errors="replace")
        return value[-self.tail_bytes:]

    def popen(self, command, **kwargs):
        return self._spawn(command, kwargs, inject_environment=not self.isolated)

    def popen_inherited(self, command, **kwargs):
        """Capture stdio only; leave an existing inherited environment intact."""
        return self._spawn(command, kwargs, inject_environment=False)

    def _spawn(self, command, kwargs, *, inject_environment):
        if self.match is not None and not self.match(command):
            return self.real_popen(command, **kwargs)
        # The product supplied this environment. Do not reintroduce ambient
        # environment entries removed by its sanitization or log their values.
        if (inject_environment or self.isolated) and not isinstance(kwargs.get("env"), dict):
            raise ValueError("diagnostic child requires its supplied environment")
        directory = self.directory / f"child-{len(self.children)}"
        # A verifier may leave its stderr PIPE unread until cleanup. Keep
        # diagnostic stacks out of that pipe so instrumentation cannot fill it.
        trace_path = directory / "startup-stacks.log" if self.isolated else None
        startup = StartupDiagnostics(directory, sample_seconds=self.sample_seconds, trace_path=trace_path)
        if self.isolated:
            command = startup.isolated_command(command)
        elif inject_environment:
            kwargs["env"] = startup.environment(kwargs["env"])
        record = {"startup": startup, "process": None, "paths": {}, "tails": {},
                  "termination_requested": False, "before_terminate": None}
        record["sampler"] = ("embedded stdlib" if self.isolated else "sitecustomize"
                             if inject_environment else "not injected; inherited environment unchanged")
        if trace_path is not None:
            record["paths"]["startup"] = trace_path
        self.children.append(record)
        with ExitStack() as streams:
            for name in ("stdout", "stderr"):
                if kwargs.get(name) == subprocess.DEVNULL:
                    path = directory / name
                    record["paths"][name] = path
                    kwargs[name] = streams.enter_context(path.open("wb"))
                elif kwargs.get(name) != subprocess.PIPE:
                    raise ValueError("diagnostic streams must be PIPE or DEVNULL")
            process = self.real_popen(command, **kwargs)
        record["process"] = process
        if self.isolated:
            # Export verifiers retain their original PIPE/select/communicate
            # behavior, including the high-numbered stdout descriptor check.
            communicate = process.communicate
            def retain_output(*args, **options):
                try:
                    stdout, stderr = communicate(*args, **options)
                except subprocess.TimeoutExpired as error:
                    record["tails"].update(stdout=self._tail(error.output), stderr=self._tail(error.stderr))
                    raise
                record["tails"].update(stdout=self._tail(stdout), stderr=self._tail(stderr))
                return stdout, stderr
            process.communicate = retain_output
            terminate = process.terminate
            def retain_precleanup_state():
                if not record["termination_requested"]:
                    record["before_terminate"] = process.poll()
                    record["termination_requested"] = True
                return terminate()
            process.terminate = retain_precleanup_state
        return process

    def ready(self, process):
        for record in self.children:
            if record["process"] is process:
                record["startup"].ready()
                return
        raise ValueError("readiness must refer to this capture's child")

    def failure_details(self):
        lines = [f"{self.label}: child startup diagnostics (no command/environment dump)"]
        for index, record in enumerate(self.children):
            process = record["process"]
            state = "not-created" if process is None else f"pid={process.pid} returncode={process.poll()}"
            lines.append(f"child {index}: {state}")
            lines.append(f"sampler: {record['sampler']}")
            if record["termination_requested"]:
                lines.append(f"returncode before caller terminate={record['before_terminate']}")
            for name in ("stdout", "stderr", *(["startup"] if "startup" in record["paths"] else [])):
                value = record["tails"].get(name, b"")
                if name in record["paths"]:
                    try:
                        with record["paths"][name].open("rb") as stream:
                            stream.seek(0, os.SEEK_END)
                            stream.seek(max(0, stream.tell() - self.tail_bytes))
                            value = stream.read(self.tail_bytes)
                    except OSError as error:
                        value = f"capture unavailable: {type(error).__name__}".encode()
                lines.append(f"{name} tail (at most {self.tail_bytes} bytes):\n"
                             + value.decode("utf-8", errors="replace"))
        return "\n".join(lines)

    def close(self):
        for record in self.children:
            record["startup"].ready()
            process = record["process"]
            if process is None:
                continue
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
