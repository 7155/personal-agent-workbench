#!/usr/bin/env python3
"""Build, export, and acceptance-test one real PAWOS Extension App.

The source-isolated Extension App is the unit under test.  This command does
not create an App, install a Package, call a Provider, or publish anything. It
builds the existing Control Center frontend in a production HTTP configuration,
exports the App source plus its Pi Package and explicit dependency manifests,
then tests the resulting archive from a temporary clean environment.

The clean-environment check serves the exported frontend over a stdlib HTTP
server with an isolated HOME.  The optional browser check uses a
local Playwright browser and intercepts only the Runtime API with a deterministic
installed-App receipt; it never makes a Provider request.  The offline vertical
suite evaluation is run both before export and from the extracted archive so
the report can distinguish export parity from installed or foreground claims.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_APP = ROOT / "control-center-web" / "extension-apps" / "zhanggui-wenshu"
EXPORT_ROOT_NAME = "pawos-app-export.v1"
BUILD_METADATA_NAME = "rag-ime-control-web-build.json"
_ASSET_REF = re.compile(r"(?:src|href)=[\"']([^\"']+)[\"']")
_TEST_COUNTS = re.compile(r"Tests\s+(?P<count>\d+)\s+passed")
_FILE_COUNTS = re.compile(r"Test Files\s+(?P<count>\d+)\s+passed")


class ExportAcceptanceError(RuntimeError):
    """Raised when a source, build, archive, or clean-start contract fails."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _tree_sha256(root: Path) -> str:
    resolved_root = root.expanduser().resolve(strict=True)
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: str(item.relative_to(root))):
        if path.is_symlink():
            raise ExportAcceptanceError(f"Export source tree may not contain symlinks: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.name == ".DS_Store":
            continue
        resolved = path.resolve(strict=True)
        try:
            resolved.relative_to(resolved_root)
        except ValueError as error:
            raise ExportAcceptanceError(f"Export source file escapes its root: {path}") from error
        digest.update(str(relative).encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _require_directory(path: str | Path, *, label: str) -> Path:
    requested = Path(path).expanduser()
    if requested.is_symlink() or not requested.is_dir():
        raise ExportAcceptanceError(f"{label} must be a real directory: {requested}")
    return requested.resolve(strict=True)


def _require_file(path: str | Path, *, label: str) -> Path:
    requested = Path(path).expanduser()
    if requested.is_symlink() or not requested.is_file():
        raise ExportAcceptanceError(f"{label} must be a real file: {requested}")
    return requested.resolve(strict=True)


def _clean_environment(root: Path) -> dict[str, str]:
    home = root / "home"
    temporary = root / "tmp"
    config = root / "config"
    for directory in (home, temporary, config):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "TMP": str(temporary),
        "TEMP": str(temporary),
        "XDG_CONFIG_HOME": str(config),
        "XDG_CACHE_HOME": str(config / "cache"),
        "XDG_DATA_HOME": str(config / "data"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "CI": "1",
        "PYTHONNOUSERSITE": "1",
    }
    # Do not let a developer's local module path, package-manager config, or
    # node flags silently turn this into a test against the source checkout.
    for key in (
        "PYTHONPATH",
        "NODE_PATH",
        "NODE_OPTIONS",
        "NPM_CONFIG_USERCONFIG",
        "npm_config_userconfig",
        "PNPM_HOME",
    ):
        environment.pop(key, None)
    return environment


def _run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout: float,
) -> str:
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            env=dict(environment),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ExportAcceptanceError(
            f"Command failed to start or timed out: {' '.join(command)}"
        ) from error
    output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode != 0:
        detail = output.strip()[-6000:] or "no command output"
        raise ExportAcceptanceError(
            f"Command exited {completed.returncode}: {' '.join(command)}\n{detail}"
        )
    return output


def _source_git_metadata() -> dict[str, object]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=all"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return {"commit": "unknown", "dirty": None}
    return {"commit": commit, "dirty": dirty}


def _build_frontend(
    control_center_root: Path,
    output_directory: Path,
    *,
    clean_root: Path,
    source_git: Mapping[str, object],
) -> dict[str, object]:
    package_json = _require_file(
        control_center_root / "package.json", label="Control Center package manifest"
    )
    lockfile = _require_file(
        control_center_root / "pnpm-lock.yaml", label="Control Center pnpm lockfile"
    )
    package = json.loads(package_json.read_text(encoding="utf-8"))
    package_manager = package.get("packageManager")
    if not isinstance(package_manager, str) or not package_manager.startswith("pnpm@"):
        raise ExportAcceptanceError(
            "Control Center package.json must declare an exact pnpm packageManager"
        )
    output_directory.mkdir(parents=True, exist_ok=False)
    environment = _clean_environment(clean_root)
    environment.update(
        {
            "VITE_CONTROL_TRANSPORT": "http",
            "VITE_BUILD_CHANNEL": "production",
            "VITE_CONTROL_BASE_URL": "http://127.0.0.1:8766",
            "VITE_CONTROL_PROXY_TARGET": "http://127.0.0.1:8768",
            "VITE_PAW_PRODUCT_VERSION": str(package.get("version") or "0.0.0"),
            "VITE_PAW_BUILD_COMMIT": str(source_git.get("commit") or "unknown"),
            "VITE_PAW_SOURCE_DIRTY": "true" if source_git.get("dirty") else "false",
        }
    )
    command = [
        "pnpm",
        "exec",
        "vite",
        "build",
        "--outDir",
        str(output_directory),
    ]
    output = _run_command(
        command,
        cwd=control_center_root,
        environment=environment,
        timeout=180,
    )
    return {
        "command": ["pnpm", "exec", "vite", "build", "--outDir", "<temporary-dist>"],
        "packageManager": package_manager,
        "packageJsonSha256": _sha256_file(package_json),
        "pnpmLockSha256": _sha256_file(lockfile),
        "logFingerprint": f"sha256:{_sha256_bytes(output.encode('utf-8'))}",
    }


def _run_source_ui_tests(
    control_center_root: Path,
    app_root: Path,
    *,
    clean_root: Path,
) -> dict[str, object]:
    environment = _clean_environment(clean_root)
    environment.update(
        {
            "VITE_CONTROL_TRANSPORT": "mock",
            "VITE_BUILD_CHANNEL": "preview",
        }
    )
    relative = app_root.relative_to(control_center_root).as_posix()
    test_file = f"{relative}/App.test.tsx"
    output = _run_command(
        ["pnpm", "exec", "vitest", "run", test_file, "--maxWorkers=1"],
        cwd=control_center_root,
        environment=environment,
        timeout=180,
    )
    test_count = _TEST_COUNTS.search(output)
    file_count = _FILE_COUNTS.search(output)
    return {
        "status": "passed",
        "testFile": test_file,
        "testCount": int(test_count.group("count")) if test_count else None,
        "testFileCount": int(file_count.group("count")) if file_count else None,
        "outputFingerprint": f"sha256:{_sha256_bytes(output.encode('utf-8'))}",
    }


def _read_build_metadata(frontend_root: Path, manifest: Mapping[str, object]) -> dict[str, object]:
    metadata_path = _require_file(
        frontend_root / BUILD_METADATA_NAME,
        label="compiled Control Center build metadata",
    )
    value = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schemaVersion") != "rag-ime.control-web-build.v1":
        raise ExportAcceptanceError("compiled frontend build metadata schema is invalid")
    if value.get("buildChannel") != "production":
        raise ExportAcceptanceError("production frontend export requires a production frontend build")
    if value.get("transport") != "http":
        raise ExportAcceptanceError("production frontend export requires HTTP Runtime transport")
    if value.get("previewFixturesExcluded") is not True:
        raise ExportAcceptanceError("production frontend export must exclude preview fixtures")
    if value.get("forbiddenTransportModulesExcluded") is not True:
        raise ExportAcceptanceError("production frontend export must exclude preview/native transport modules")
    index = _require_file(frontend_root / "index.html", label="compiled frontend entry")
    index_text = index.read_text(encoding="utf-8")
    references: list[str] = []
    for raw_reference in _ASSET_REF.findall(index_text):
        parsed = urllib.parse.urlparse(raw_reference)
        if parsed.scheme or parsed.netloc or raw_reference.startswith("data:"):
            continue
        reference = urllib.parse.unquote(parsed.path.lstrip("./"))
        if not reference or ".." in Path(reference).parts:
            raise ExportAcceptanceError(f"compiled frontend entry escapes its root: {raw_reference}")
        _require_file(frontend_root / reference, label=f"compiled frontend asset {reference}")
        references.append(reference)
    slug = str(manifest["id"]).split(":", 1)[-1]
    label = str(manifest["label"])
    marker_files: list[str] = []
    slug_files: list[str] = []
    asset_files = sorted(
        path for path in frontend_root.rglob("*")
        if path.is_file() and path.suffix in {".js", ".css", ".html"}
    )
    for path in asset_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        relative = path.relative_to(frontend_root).as_posix()
        if label in text:
            marker_files.append(relative)
        if slug in text or str(manifest["id"]) in text:
            slug_files.append(relative)
    if not marker_files:
        raise ExportAcceptanceError(
            f"compiled frontend does not contain the real App label {label!r}"
        )
    if not slug_files:
        raise ExportAcceptanceError(
            f"compiled frontend does not contain the real App id/slug {slug!r}"
        )
    return {
        "metadata": value,
        "metadataSha256": _sha256_file(metadata_path),
        "indexSha256": _sha256_file(index),
        "entryAssetCount": len(references),
        "assetCount": len(asset_files),
        "appMarkerFiles": marker_files,
        "appIdMarkerFiles": slug_files,
        "frontendTreeSha256": _tree_sha256(frontend_root),
    }


def _copy_tree(source: Path, destination: Path) -> None:
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ExportAcceptanceError(f"Export does not include symlinked source: {path}")
    shutil.copytree(source, destination, symlinks=False)


def _zip_directory(source_root: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(source_root.rglob("*"), key=lambda item: str(item.relative_to(source_root))):
            if path.is_symlink():
                raise ExportAcceptanceError(f"Export archive may not contain symlinks: {path}")
            if not path.is_file():
                continue
            relative = f"{source_root.name}/{path.relative_to(source_root).as_posix()}"
            info = zipfile.ZipInfo(relative)
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o600 << 16
            archive.writestr(info, path.read_bytes())


def _safe_extract(archive_path: Path, destination: Path) -> Path:
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        prefix = f"{EXPORT_ROOT_NAME}/"
        if not names or any(not name.startswith(prefix) for name in names):
            raise ExportAcceptanceError("export archive has an invalid root or path")
        for info in archive.infolist():
            relative = Path(info.filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise ExportAcceptanceError(f"export archive contains an escaping path: {info.filename}")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise ExportAcceptanceError(f"export archive contains a symlink: {info.filename}")
        archive.extractall(destination)
    return destination / EXPORT_ROOT_NAME


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _fetch(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=8) as response:
            return int(response.status), response.read()
    except (OSError, urllib.error.URLError) as error:
        raise ExportAcceptanceError(f"clean export server could not fetch {url}") from error


def _start_static_server(web_root: Path, clean_root: Path) -> tuple[subprocess.Popen[str], str]:
    port = _free_port()
    environment = _clean_environment(clean_root)
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-u",
                "-m",
                "http.server",
                str(port),
                "--bind",
                "127.0.0.1",
                "--directory",
                str(web_root),
            ],
            cwd=web_root,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError as error:
        raise ExportAcceptanceError("could not start clean export HTTP server") from error
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ExportAcceptanceError("clean export HTTP server exited before startup")
        try:
            _fetch(f"{base_url}/index.html")
            return process, base_url
        except ExportAcceptanceError:
            time.sleep(0.05)
    process.terminate()
    process.wait(timeout=3)
    raise ExportAcceptanceError("clean export HTTP server did not become reachable")


def _clean_start_check(
    extracted_root: Path,
    *,
    app_manifest: Mapping[str, object],
    frontend_info: Mapping[str, object],
    clean_root: Path,
) -> dict[str, object]:
    web_root = _require_directory(extracted_root / "web", label="extracted exported web artifact")
    process, base_url = _start_static_server(web_root, clean_root)
    try:
        status, index_bytes = _fetch(f"{base_url}/index.html")
        if status != 200 or b"id=\"root\"" not in index_bytes:
            raise ExportAcceptanceError("clean export startup did not serve a root mount")
        metadata_status, metadata_bytes = _fetch(f"{base_url}/{BUILD_METADATA_NAME}")
        if metadata_status != 200:
            raise ExportAcceptanceError("clean export startup did not serve build metadata")
        metadata = json.loads(metadata_bytes.decode("utf-8"))
        if metadata.get("buildChannel") != "production" or metadata.get("transport") != "http":
            raise ExportAcceptanceError("clean export served a non-production build configuration")
        marker_files = list(frontend_info["appMarkerFiles"])
        if not marker_files:
            raise ExportAcceptanceError("compiled App marker list is empty")
        marker_statuses = {}
        for relative in marker_files:
            marker_status, _ = _fetch(f"{base_url}/{relative}")
            marker_statuses[str(relative)] = marker_status
            if marker_status != 200:
                raise ExportAcceptanceError(f"clean export could not serve App chunk {relative}")
        return {
            "status": "passed",
            "server": "python -I -m http.server",
            "homeIsolated": True,
            "rootMountServed": True,
            "productionMetadataServed": True,
            "appId": app_manifest["id"],
            "appChunkStatuses": marker_statuses,
            "providerCalls": 0,
            "installActionPerformed": False,
        }
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def _browser_smoke(
    extracted_root: Path,
    *,
    app_manifest: Mapping[str, object],
    frontend_info: Mapping[str, object],
    control_center_root: Path,
    clean_root: Path,
) -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        raise ExportAcceptanceError("--browser-smoke requires node")
    try:
        playwright_module = subprocess.run(
            [node, "-e", "process.stdout.write(require.resolve('@playwright/test'))"],
            cwd=control_center_root,
            env=_clean_environment(clean_root),
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ExportAcceptanceError(
            "--browser-smoke requires the installed @playwright/test dependency"
        ) from error
    if not playwright_module:
        raise ExportAcceptanceError("--browser-smoke could not resolve @playwright/test")
    web_root = _require_directory(extracted_root / "web", label="extracted exported web artifact")
    process, base_url = _start_static_server(web_root, clean_root)
    manifest = dict(app_manifest)
    slug = str(manifest["id"]).split(":", 1)[-1]
    marker_files = [str(value) for value in frontend_info["appMarkerFiles"]]
    node_script = f"""
import playwrightTest from {json.dumps(playwright_module)};
const {{ chromium }} = playwrightTest;
const baseUrl = {json.dumps(base_url)};
const manifest = {json.dumps(manifest, ensure_ascii=False)};
const slug = manifest.id.split(':', 2)[1];
const markerFiles = {json.dumps(marker_files)};
const bindingCapability = `pawos.extension.binding.${{manifest.bindingSha256.slice(0, 40)}}`;
const installation = {{
  id: manifest.packageId,
  packageId: manifest.packageId,
  version: manifest.version,
  installed: true,
  enabled: true,
  capabilities: [bindingCapability],
  extensionApp: {{
    id: manifest.id,
    packageId: manifest.packageId,
    version: manifest.version,
    bindingSha256: manifest.bindingSha256,
    bindingCapability,
    skillRef: manifest.skillRef,
    skillSha256: manifest.skillSha256,
    verticalSuiteId: manifest.verticalSuiteId,
    verticalSuiteRevision: manifest.verticalSuiteRevision,
    sandbox: manifest.sandbox,
  }},
}};
const browser = await chromium.launch({{ headless: true }});
const page = await browser.newPage({{ locale: 'zh-CN' }});
const failedAssets = [];
const pageErrors = [];
const requested = [];
page.on('request', request => requested.push(request.url()));
page.on('requestfailed', request => {{
  if (/\\.(?:js|css)(?:\\?|$)/u.test(request.url())) failedAssets.push(request.url());
}});
page.on('pageerror', error => pageErrors.push(String(error.message || error)));
await page.route('**/api/**', async route => {{
  const path = new URL(route.request().url()).pathname;
  let body = {{ ok: true }};
  if (path === '/api/agent/extensions') body = {{ ok: true, items: [installation] }};
  else if (path === '/api/agent/sessions') body = {{ ok: true, items: [] }};
  else if (path === '/api/agent/rooms') body = {{ ok: true, items: [] }};
  else if (path === '/api/agent/memory-maintenance') body = {{ ok: true, items: [] }};
  else if (path === '/api/agent/control/capabilities') body = {{ ok: true, features: {{}} }};
  await route.fulfill({{ status: 200, contentType: 'application/json', body: JSON.stringify(body) }});
}});
await page.goto(`${{baseUrl}}/#/extensions/${{slug}}`, {{ waitUntil: 'domcontentloaded' }});
await page.getByRole('heading', {{ name: manifest.label }}).waitFor({{ state: 'visible', timeout: 20_000 }});
await page.waitForTimeout(1_000);
const bodyText = await page.locator('body').innerText();
const tabCount = await page.getByRole('tab').count();
const appChunkRequested = markerFiles.some(file => requested.some(url => url.endsWith('/' + file)));
if (!appChunkRequested) throw new Error('real Extension App chunk was not requested by the clean runtime');
if (failedAssets.length) throw new Error(`clean runtime failed JS/CSS assets: ${{failedAssets.join(', ')}}`);
if (pageErrors.length) throw new Error(`clean runtime page error: ${{pageErrors.join('; ')}}`);
if (!bodyText.includes(manifest.label)) throw new Error('real App label is not visible after startup');
console.log(JSON.stringify({{
  status: 'passed',
  appHeadingVisible: true,
  appChunkRequested,
  tabCount,
  controlledSourceVisible: bodyText.includes(manifest.label + '受控数据'),
  pageErrors: 0,
  failedAssets: 0,
  providerCalls: 0,
  installActionPerformed: false,
}}));
await browser.close();
"""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            suffix=".mjs",
            prefix="paw-app-browser-smoke-",
            dir=clean_root,
            delete=False,
        ) as handle:
            script_path = Path(handle.name)
            handle.write(node_script)
        environment = _clean_environment(clean_root)
        browser_cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        if not browser_cache:
            candidate_cache = Path.home() / "Library" / "Caches" / "ms-playwright"
            if candidate_cache.exists():
                browser_cache = str(candidate_cache)
        if browser_cache and Path(browser_cache).exists():
            # Browser binaries are an acceptance-host dependency. Product
            # files still run from the extracted archive and HOME stays clean.
            environment["PLAYWRIGHT_BROWSERS_PATH"] = browser_cache
        completed = subprocess.run(
            [node, str(script_path)],
            cwd=control_center_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=90,
        )
        if completed.returncode != 0:
            detail = (completed.stdout + completed.stderr).strip()[-6000:]
            raise ExportAcceptanceError(f"browser smoke failed: {detail}")
        lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        if not lines:
            raise ExportAcceptanceError("browser smoke returned no receipt")
        receipt = json.loads(lines[-1])
        if receipt.get("status") != "passed":
            raise ExportAcceptanceError("browser smoke did not return a passed receipt")
        return receipt
    finally:
        try:
            script_path.unlink()
        except (NameError, OSError):
            pass
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def _exported_report_without_paths(report: Mapping[str, object]) -> dict[str, object]:
    value = json.loads(json.dumps(report, ensure_ascii=False))
    if isinstance(value, dict):
        artifact = value.get("artifact")
        if isinstance(artifact, dict):
            artifact.pop("path", None)
    return value


def run_paw_app_export_acceptance(
    app_directory: str | Path,
    output: str | Path,
    *,
    frontend_dist: str | Path | None = None,
    run_build: bool = True,
    run_ui_tests: bool = True,
    browser_smoke: bool = False,
    quick_validate: str | None = None,
) -> dict[str, object]:
    """Export and verify one real Extension App.

    ``frontend_dist`` and ``run_build=False`` are test seams for deterministic
    unit tests. Production acceptance leaves them unset so the current source
    is built with the exact Control Center package manager and lockfile.
    """

    from scripts.run_extension_app_candidate_eval import run_extension_app_candidate_eval
    from scripts.validate_extension_app import ExtensionAppValidationError, validate_extension_app

    app_root = _require_directory(app_directory, label="Extension App source")
    output_path = Path(output).expanduser().resolve(strict=False)
    if output_path.exists():
        raise ExportAcceptanceError(f"export output already exists: {output_path}")
    control_center_root = app_root.parents[1]
    if control_center_root.name != "control-center-web":
        raise ExportAcceptanceError(
            "Extension App must live under control-center-web/extension-apps/<slug>"
        )
    try:
        validation = validate_extension_app(app_root, quick_validate=quick_validate)
    except (ExtensionAppValidationError, OSError, ValueError) as error:
        raise ExportAcceptanceError(f"Extension App source validation failed: {error}") from error
    manifest_path = app_root / "pawos-app.json"
    app_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_git = _source_git_metadata()

    with tempfile.TemporaryDirectory(prefix="paw-app-export-", dir=output_path.parent) as temporary:
        work_root = Path(temporary)
        clean_root = work_root / "clean"
        clean_root.mkdir(mode=0o700)
        source_eval = run_extension_app_candidate_eval(
            app_root,
            work_root / "source-workspace",
            quick_validate=quick_validate,
        )
        if source_eval.get("status") != "sandbox_verified":
            raise ExportAcceptanceError("source Extension App candidate evaluation did not pass")
        source_ui = (
            _run_source_ui_tests(control_center_root, app_root, clean_root=clean_root)
            if run_ui_tests
            else {"status": "not_requested"}
        )
        frontend_root = (
            _require_directory(frontend_dist, label="frontend build output")
            if frontend_dist is not None
            else work_root / "frontend-dist"
        )
        build_info: dict[str, object]
        if run_build:
            build_info = _build_frontend(
                control_center_root,
                frontend_root,
                clean_root=clean_root,
                source_git=source_git,
            )
        else:
            build_info = {"mode": "injected-test-seam"}
        frontend_info = _read_build_metadata(frontend_root, app_manifest)
        package_json = _require_file(
            control_center_root / "package.json", label="Control Center package manifest"
        )
        lockfile = _require_file(
            control_center_root / "pnpm-lock.yaml", label="Control Center pnpm lockfile"
        )
        stage = work_root / EXPORT_ROOT_NAME
        stage.mkdir(mode=0o700)
        app_export_root = stage / "app" / app_root.name
        app_export_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for child in sorted(app_root.iterdir(), key=lambda item: item.name):
            if child.name == "pi-package":
                continue
            destination = app_export_root / child.name
            if child.is_dir():
                _copy_tree(child, destination)
            else:
                if child.is_symlink():
                    raise ExportAcceptanceError(f"Export does not include symlinked source: {child}")
                shutil.copy2(child, destination)
        _copy_tree(frontend_root, stage / "web")
        package_directory = app_root / "pi-package"
        _copy_tree(package_directory, app_export_root / "pi-package")
        dependencies = stage / "dependencies"
        dependencies.mkdir(mode=0o700)
        shutil.copy2(package_json, dependencies / "control-center-web.package.json")
        shutil.copy2(lockfile, dependencies / "pnpm-lock.yaml")
        config = stage / "config"
        config.mkdir(mode=0o700)
        shutil.copy2(manifest_path, config / "pawos-app.json")
        shutil.copy2(frontend_root / BUILD_METADATA_NAME, config / BUILD_METADATA_NAME)
        export_manifest = {
            "schemaVersion": "pawos.extension-app-export.v1",
            "exportRoot": EXPORT_ROOT_NAME,
            "appId": validation["appId"],
            "version": validation["version"],
            "packageId": validation["packageId"],
            "sourceTreeSha256": _tree_sha256(app_root),
            "piPackageTreeSha256": _tree_sha256(package_directory),
            "frontend": frontend_info,
            "build": build_info,
            "source": {
                "commit": source_git["commit"],
                "dirty": source_git["dirty"],
            },
            "dependencies": {
                "packageJsonSha256": _sha256_file(package_json),
                "pnpmLockSha256": _sha256_file(lockfile),
                "packageManager": json.loads(package_json.read_text(encoding="utf-8")).get("packageManager"),
            },
            "boundary": {
                "installActionPerformed": False,
                "providerCalls": 0,
                "foregroundAccepted": False,
                "cleanEnvironment": "isolated HOME + stdlib http.server",
            },
        }
        (stage / "export.json").write_text(
            json.dumps(export_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        provisional = work_root / "provisional.zip"
        _zip_directory(stage, provisional)
        extracted = work_root / "extracted"
        extracted.mkdir(mode=0o700)
        extracted_root = _safe_extract(provisional, extracted)
        clean_start = _clean_start_check(
            extracted_root,
            app_manifest=app_manifest,
            frontend_info=frontend_info,
            clean_root=clean_root,
        )
        browser_receipt = (
            _browser_smoke(
                extracted_root,
                app_manifest=app_manifest,
                frontend_info=frontend_info,
                control_center_root=control_center_root,
                clean_root=clean_root,
            )
            if browser_smoke
            else {"status": "not_requested", "providerCalls": 0, "installActionPerformed": False}
        )
        exported_app = extracted_root / "app" / app_root.name
        exported_eval = run_extension_app_candidate_eval(
            exported_app,
            work_root / "exported-workspace",
            quick_validate=quick_validate,
        )
        if exported_eval.get("status") != "sandbox_verified":
            raise ExportAcceptanceError("extracted Extension App candidate evaluation did not pass")
        source_metrics = source_eval.get("result", {}).get("metrics")
        exported_metrics = exported_eval.get("result", {}).get("metrics")
        parity = {
            "evaluation": {
                "metricsEqual": source_metrics == exported_metrics,
                "before": source_metrics,
                "after": exported_metrics,
                "providerCallsEqual": source_eval["result"]["providerCalls"] == exported_eval["result"]["providerCalls"],
                "traceVerifiedBefore": source_eval["result"]["traceVerified"],
                "traceVerifiedAfter": exported_eval["result"]["traceVerified"],
            },
            "functionality": {
                # Keep optional gates truthful: an omitted UI test must not be
                # promoted to a passing result merely because export/eval gates
                # passed.  The sibling receipt carries the explicit status.
                "sourceUiPassed": source_ui.get("status") == "passed",
                "exportedCleanStartPassed": clean_start.get("status") == "passed",
                "browserSmokePassed": browser_receipt.get("status") == "passed",
                "appIdPreserved": exported_eval["candidate"]["appId"] == source_eval["candidate"]["appId"],
            },
        }
        if not parity["evaluation"]["metricsEqual"] or not parity["evaluation"]["providerCallsEqual"]:
            raise ExportAcceptanceError("exported App evaluation did not preserve source metrics/provider-call parity")
        acceptance = {
            "sourceUi": source_ui,
            "cleanStart": clean_start,
            "browserSmoke": browser_receipt,
            "exportedCandidateEval": exported_eval,
            "parity": parity,
        }
        receipts = stage / "receipts"
        receipts.mkdir(mode=0o700)
        (receipts / "source-candidate-eval.json").write_text(
            json.dumps(source_eval, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (receipts / "exported-candidate-eval.json").write_text(
            json.dumps(exported_eval, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (receipts / "clean-start.json").write_text(
            json.dumps(clean_start, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (receipts / "browser-smoke.json").write_text(
            json.dumps(browser_receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        report: dict[str, object] = {
            "schemaVersion": "pawos.extension-app-export-acceptance.v1",
            "status": "passed",
            "artifact": {
                "path": str(output_path),
                "root": EXPORT_ROOT_NAME,
                "installActionPerformed": False,
            },
            "source": {
                "appId": validation["appId"],
                "version": validation["version"],
                "packageId": validation["packageId"],
                "sourceTreeSha256": export_manifest["sourceTreeSha256"],
                "piPackageTreeSha256": export_manifest["piPackageTreeSha256"],
                "candidateEval": source_eval,
                "ui": source_ui,
            },
            "export": {
                "frontend": frontend_info,
                "dependencies": export_manifest["dependencies"],
                "archiveContentsChecked": True,
            },
            "acceptance": acceptance,
            "boundary": {
                "providerCalls": 0,
                "installActionPerformed": False,
                "foregroundAccepted": False,
                "realBusinessData": False,
                "claimAllowed": "A real source-isolated Extension App, Pi Package, production HTTP frontend artifact, clean static startup, and offline evaluation parity were verified.",
                "claimForbidden": "Installed Package state, native macOS distribution, live Provider behavior, real business-data correctness, and foreground acceptance.",
            },
        }
        (stage / "export.json").write_text(
            json.dumps({**export_manifest, "acceptance": acceptance}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (receipts / "acceptance.json").write_text(
            json.dumps(_exported_report_without_paths(report), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _zip_directory(stage, output_path)
        report["artifact"] = {
            **report["artifact"],
            "sha256": _sha256_file(output_path),
            "bytes": output_path.stat().st_size,
        }
        return report


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-directory", type=Path, default=DEFAULT_APP)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frontend-dist", type=Path)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--skip-ui-tests", action="store_true")
    parser.add_argument("--browser-smoke", action="store_true")
    parser.add_argument("--quick-validate")
    args = parser.parse_args(argv)
    try:
        report = run_paw_app_export_acceptance(
            args.app_directory,
            args.output,
            frontend_dist=args.frontend_dist,
            run_build=not args.skip_build,
            run_ui_tests=not args.skip_ui_tests,
            browser_smoke=args.browser_smoke,
            quick_validate=args.quick_validate,
        )
    except (ExportAcceptanceError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"PAWOS App export acceptance failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
