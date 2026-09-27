"""Optional, bounded headless conversion of legacy Office files to OOXML."""
from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .models import DocumentParseError, ParsedDocument


MAX_CONVERSION_BYTES = 200 * 1024 * 1024
MAX_LOG_BYTES = 2 * 1024 * 1024
CONVERSION_TIMEOUT = 120.0
_TARGETS = {".doc": ("docx", "Office Open XML Text"), ".xls": ("xlsx", "Calc MS Excel 2007 XML"), ".ppt": ("pptx", "Impress MS PowerPoint 2007 XML")}


def find_office_converter() -> str | None:
    executable = shutil.which("soffice") or shutil.which("libreoffice")
    if executable:
        return executable
    for root in (Path("/Applications"), Path.home() / "Applications"):
        candidate = root / "LibreOffice.app/Contents/MacOS/soffice"
        if candidate.is_file():
            return str(candidate)
    return None


def _run_converter(command: list[str], directory: Path, *, timeout: float, output: Path | None = None) -> str:
    log = directory / "converter.log"
    started = time.monotonic()
    with log.open("wb") as stream:
        try:
            process = subprocess.Popen(command, cwd=directory, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
                                       start_new_session=os.name == "posix")
        except OSError as exc:
            raise DocumentParseError("Office converter could not start", code="office_converter_unavailable") from exc
        try:
            while process.poll() is None:
                if time.monotonic() - started > timeout:
                    raise DocumentParseError("Office conversion timed out", code="office_conversion_timeout")
                if log.stat().st_size > MAX_LOG_BYTES or (output is not None and output.exists() and output.stat().st_size > MAX_CONVERSION_BYTES):
                    raise DocumentParseError("Office conversion output exceeds size limit", code="office_conversion_limit")
                try:
                    process.wait(timeout=min(0.2, timeout))
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if process.poll() is None:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                process.wait()
    if log.stat().st_size > MAX_LOG_BYTES or (output is not None and output.exists() and output.stat().st_size > MAX_CONVERSION_BYTES):
        raise DocumentParseError("Office conversion output exceeds size limit", code="office_conversion_limit")
    with log.open("rb") as stream:
        detail = stream.read(4096).decode("utf-8", errors="replace").strip()
    if process.returncode:
        raise DocumentParseError(f"Office conversion failed ({process.returncode}): {detail[:500]}", code="office_conversion_failed")
    return detail


def convert_legacy_office(path: Path, *, parse_converted: Callable[..., ParsedDocument]) -> ParsedDocument:
    converter = find_office_converter()
    if converter is None:
        raise DocumentParseError("Legacy DOC/XLS/PPT requires a local LibreOffice soffice converter", code="office_converter_unavailable")
    if path.stat().st_size > MAX_CONVERSION_BYTES:
        raise DocumentParseError("Legacy Office input exceeds conversion size limit", code="office_conversion_limit")
    suffix = path.suffix.lower()
    extension, output_filter = _TARGETS[suffix]
    with tempfile.TemporaryDirectory(prefix="paw-office-conversion-") as temporary:
        root = Path(temporary)
        profile = root / "profile"
        (profile / "user").mkdir(parents=True)
        # This new profile does not inherit trusted macro locations or saved
        # credentials. Disable scripts, active embedded objects and untrusted
        # document links using the converter's documented registry properties.
        (profile / "user/registrymodifications.xcu").write_text(
            '<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
            '<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>'
            '<prop oor:name="DisableActiveContent" oor:op="fuse"><value>true</value></prop>'
            '<prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop></item>'
            '</oor:items>', encoding="utf-8")
        source = root / f"input{suffix}"
        shutil.copyfile(path, source)
        output_dir = root / "converted"
        output_dir.mkdir()
        output = output_dir / f"input.{extension}"
        prefix = [converter, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--nologo", "--nodefault", "--norestore"]
        version = _run_converter([*prefix, "--version"], root, timeout=15.0).splitlines()
        converter_version = next((line[:200] for line in version if "LibreOffice" in line), "unknown")
        _run_converter([*prefix, "--convert-to", f"{extension}:{output_filter}", "--outdir", str(output_dir), str(source)],
                       root, timeout=CONVERSION_TIMEOUT, output=output)
        if not output.is_file() or not output.stat().st_size:
            raise DocumentParseError("Office converter produced no document", code="office_conversion_failed")
        if output.stat().st_size > MAX_CONVERSION_BYTES:
            raise DocumentParseError("Converted Office output exceeds size limit", code="office_conversion_limit")
        parsed = parse_converted(output, suffix=f".{extension}")
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        return replace(parsed, title=path.stem, provider_version=f"legacy-conversion-v1/{parsed.provider_version}", metadata={
            **parsed.metadata, "originalFormat": suffix, "convertedFormat": f".{extension}",
            "converter": "LibreOffice", "converterVersion": converter_version, "convertedSha256": digest,
        })
