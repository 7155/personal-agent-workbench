from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import plistlib
import stat
import subprocess
import tarfile
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping


SCHEMA_VERSION = "rag-ime.release-candidate-staging.v1"
CONTROL_APP_NAME = "RagImeControlElectron.app"
CONTROL_BUNDLE_ID = "com.rag-ime.control"
CONTROL_DIST_MARKER = "rag-ime-control-web-build.json"
SOURCE_EXCLUDED_PARTS = {".git", "__pycache__", "build", "download", "Frameworks", "lib", "node_modules"}
PROJECT_EXCLUDED_PREFIXES = (
    "docs/",
    ".github/",
    "macos/RagImeMac/",
    "scripts/build_macos_frontend.sh",
    "scripts/doctor_macos_frontend.sh",
    "scripts/install_macos_frontend.sh",
    "scripts/install_system_macos_frontend.sh",
    "scripts/refresh_macos_input_sources.sh",
    "scripts/reset_macos_ragime_registration.sh",
)


def prepare_release_candidate(
    root: str | Path,
    *,
    release_id: str,
    output_root: str | Path,
    squirrel_source: str | Path,
    apps: Mapping[str, str | Path],
    project_files: Iterable[str] | None = None,
) -> dict[str, object]:
    """Create deterministic unsigned staging archives without release claims."""

    repo_root = Path(root).resolve()
    output = Path(output_root).resolve()
    if not release_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in release_id):
        raise ValueError("release_id must contain only letters, digits, dot, underscore, or hyphen")
    source_root = Path(squirrel_source).resolve()
    if not source_root.is_dir() or not (source_root / "LICENSE.txt").is_file():
        raise ValueError("squirrel_source must be a prepared Squirrel checkout with LICENSE.txt")
    upstream_commit = _git_value(source_root, "rev-parse", "HEAD")
    if len(upstream_commit) != 40:
        raise ValueError("squirrel_source must have a readable Git HEAD")

    project_commit = _git_value(repo_root, "rev-parse", "HEAD")
    if len(project_commit) != 40:
        raise ValueError("project root must have a readable Git HEAD")

    normalized_apps = _validate_apps(apps)
    control_provenance: dict[str, object] | None = None
    if control_app := normalized_apps.get("control"):
        control_provenance = _validate_web_control_app(
            control_app,
            expected_commit=project_commit,
        )
    if desktop_bridge_app := normalized_apps.get("desktopBridge"):
        _validate_desktop_bridge_app(desktop_bridge_app)
    files = sorted(set(project_files if project_files is not None else _candidate_files(repo_root)))
    source_files = [
        relative
        for relative in files
        if not relative.startswith(PROJECT_EXCLUDED_PREFIXES)
        and (repo_root / relative).is_file()
    ]
    if not source_files:
        raise ValueError("project source file set is empty")

    stage = output / release_id
    stage.mkdir(parents=True, exist_ok=True)
    source_archive = stage / f"RAG-IME-{release_id}-corresponding-source.tar.gz"
    app_archive = stage / f"RAG-IME-{release_id}-macos-unsigned.tar.gz"
    manifest_path = stage / "staging-manifest.json"

    project_snapshot = _snapshot_files(repo_root, source_files)
    squirrel_snapshot = _tree_snapshot(source_root, excluded_parts=SOURCE_EXCLUDED_PARTS)
    source_metadata = {
        "schemaVersion": SCHEMA_VERSION,
        "releaseId": release_id,
        "projectCommit": project_commit,
        "projectDirty": bool(_git_value(repo_root, "status", "--porcelain")),
        "projectSnapshotSha256": _snapshot_digest(project_snapshot),
        "projectFileCount": len(project_snapshot),
        "squirrelUpstreamCommit": upstream_commit,
        "squirrelDirty": bool(_git_value(source_root, "status", "--porcelain")),
        "squirrelTreeSha256": _snapshot_digest(squirrel_snapshot),
        "excludedSquirrelParts": sorted(SOURCE_EXCLUDED_PARTS),
    }
    with _deterministic_tar_gz(source_archive) as archive:
        for relative in source_files:
            _add_path(archive, repo_root / relative, PurePosixPath("rag-ime-project") / relative)
        _add_tree(archive, source_root, PurePosixPath("patched-squirrel-source"))
        _add_bytes(
            archive,
            "SOURCE-SNAPSHOT.json",
            json.dumps(source_metadata, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n",
        )
    if _snapshot_files(repo_root, source_files) != project_snapshot:
        source_archive.unlink(missing_ok=True)
        raise RuntimeError("project source changed during release staging")
    if _tree_snapshot(source_root, excluded_parts=SOURCE_EXCLUDED_PARTS) != squirrel_snapshot:
        source_archive.unlink(missing_ok=True)
        raise RuntimeError("Squirrel source changed during release staging")

    app_records: list[dict[str, object]] = []
    app_snapshots = {label: _tree_snapshot(path) for label, path in normalized_apps.items()}
    with _deterministic_tar_gz(app_archive) as archive:
        _add_bytes(
            archive,
            "INSTALL-NOT-READY.txt",
            (
                "This is an unsigned engineering staging archive.\n"
                "Do not distribute or install it as a public release.\n"
                "Developer ID signing, notarization, stapling, foreground acceptance, and the final release manifest are pending.\n"
            ).encode("utf-8"),
        )
        for label, app_path in normalized_apps.items():
            _add_path(archive, app_path, PurePosixPath("apps") / app_path.name)
            record = {
                "label": label,
                "bundle": app_path.name,
                "bundleIdentifier": _bundle_identifier(app_path),
                "treeSha256": _snapshot_digest(app_snapshots[label]),
            }
            if label == "control" and control_provenance is not None:
                record["provenance"] = control_provenance
            app_records.append(record)
    for label, app_path in normalized_apps.items():
        if _tree_snapshot(app_path) != app_snapshots[label]:
            app_archive.unlink(missing_ok=True)
            raise RuntimeError(f"app bundle changed during release staging: {label}")

    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "releaseId": release_id,
        "status": "unsigned_staging_only",
        "releaseEligible": False,
        "trainingOrRuntimeStateModified": False,
        "source": source_metadata,
        "artifacts": [
            _artifact_record(source_archive, kind="corresponding_source_staging"),
            _artifact_record(app_archive, kind="macos_unsigned_staging"),
        ],
        "apps": app_records,
        "pendingGates": [
            "top_level_project_license",
            "clean_source_commit",
            "foreground_acceptance",
            "developer_id_codesign",
            "notarization",
            "stapling",
            "gatekeeper_clean_machine",
            "final_release_manifest",
        ],
        "notes": [
            "This is not rag-ime.release-manifest.v1 and cannot satisfy the public release gate.",
            "Archives use normalized ordering, ownership, permissions, and timestamps for reproducibility.",
            "Squirrel build/download products are excluded; full edited source and initialized source subtrees are included.",
        ],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"ok": True, "manifest": str(manifest_path), **manifest}


def _candidate_files(root: Path) -> list[str]:
    tracked = _git_lines(root, "ls-files")
    untracked = _git_lines(root, "ls-files", "--others", "--exclude-standard")
    return sorted(set(tracked + untracked))


def _validate_apps(apps: Mapping[str, str | Path]) -> dict[str, Path]:
    if not apps:
        raise ValueError("at least one app bundle is required")
    result: dict[str, Path] = {}
    seen_names: set[str] = set()
    for label, raw_path in sorted(apps.items()):
        path = Path(raw_path).resolve()
        if not label or not path.is_dir() or path.suffix != ".app" or not (path / "Contents" / "Info.plist").is_file():
            raise ValueError(f"invalid app bundle for {label!r}: {raw_path}")
        if path.name in seen_names:
            raise ValueError(f"duplicate app bundle name: {path.name}")
        seen_names.add(path.name)
        result[label] = path
    return result


def _validate_web_control_app(
    app: Path,
    *,
    expected_commit: str,
) -> dict[str, object]:
    if app.name != CONTROL_APP_NAME:
        raise ValueError(
            f"control app must use the canonical Electron bundle {CONTROL_APP_NAME}"
        )
    marker_path = app / "Contents" / "Resources" / "rag-ime-control-web-build-marker.json"
    resources = app / "Contents" / "Resources"
    dist = resources / "app" / "dist"
    frontend_marker_path = dist / CONTROL_DIST_MARKER
    legacy_frontend = resources / "control-center-web"
    if legacy_frontend.exists():
        raise ValueError("control app contains the removed native/WebKit layout")
    if not dist.is_dir():
        raise ValueError("control app must contain the canonical Electron app/dist layout")
    if not (dist / "index.html").is_file() or not (dist / "manifest.webmanifest").is_file() or not (dist / "assets").is_dir():
        raise ValueError("canonical Electron app/dist layout is incomplete")
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        frontend_marker = json.loads(frontend_marker_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise ValueError("control app must be a verified Web Control Center release") from exc
    provenance = marker.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("control app is missing content-addressed provenance")
    required_provenance = {
        "sourceCommit": expected_commit,
        "sourceDirty": False,
        "frontendProduct": "paw-os",
        "bundleId": CONTROL_BUNDLE_ID,
        "frontendTransport": "http",
        "browserHost": "electron-webview",
        "browserControl": "ego-browser",
        "browserTransport": "cdp",
        "browserPartition": "persist:paw-browser",
        "sameOriginControlProxy": True,
    }
    if any(provenance.get(key) != value for key, value in required_provenance.items()):
        raise ValueError("control app provenance does not describe the Electron PAW OS host")
    if any(marker.get(key) != value for key, value in required_provenance.items()):
        raise ValueError("control app marker does not match its Electron PAW OS provenance")
    dist_digest = control_center_dist_digest(dist)
    if (
        not isinstance(provenance.get("distTreeDigest"), str)
        or provenance.get("distTreeDigest") != dist_digest
        or marker.get("distTreeDigest") != dist_digest
    ):
        raise ValueError("control app dist tree digest does not match its content")
    if (
        marker.get("schemaVersion") != "rag-ime.control-build-marker.v1"
        or marker.get("ui") != "control-center-web"
        or marker.get("channel") != "release"
        or marker.get("frontendBuildChannel") != "production"
        or marker.get("forbiddenTransportModulesExcluded") is not True
        or "swiftFallback" in marker
        or frontend_marker.get("schemaVersion") != "rag-ime.control-web-build.v1"
        or frontend_marker.get("buildChannel") != "production"
        or frontend_marker.get("transport") != "http"
        or frontend_marker.get("httpOnly") is not True
        or frontend_marker.get("frontendProduct") != "paw-os"
        or frontend_marker.get("forbiddenTransportModulesExcluded") is not True
        or frontend_marker.get("previewFixturesExcluded") is not True
        or frontend_marker.get("sourceCommit") != expected_commit
        or frontend_marker.get("distTreeDigest") != dist_digest
    ):
        raise ValueError("control app is not a verified Web Control Center release")
    if _bundle_identifier(app) != CONTROL_BUNDLE_ID:
        raise ValueError("control app has the wrong bundle identifier")
    return dict(provenance)


def _validate_desktop_bridge_app(app: Path) -> None:
    marker_path = (
        app
        / "Contents"
        / "Resources"
        / "rag-ime-desktop-bridge-build-marker.json"
    )
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise ValueError("desktop bridge app must contain a verified build marker") from exc
    capabilities = marker.get("capabilities")
    if (
        marker.get("schemaVersion") != "rag-ime.desktop-bridge-build-marker.v1"
        or marker.get("gitDirty") is True
        or not isinstance(capabilities, dict)
        or capabilities.get("accessibilitySemantics") is not True
        or capabilities.get("treeDiff") is not True
        or capabilities.get("semanticActions") is not True
        or capabilities.get("liveStateRevalidation") is not True
        or capabilities.get("modelSuppliedCoordinates") is not False
        or capabilities.get("screenCapture") is not False
    ):
        raise ValueError("desktop bridge app is not Accessibility-only or provenance-clean")


def _deterministic_tar_gz(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = path.open("wb")
    compressed = gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0)
    archive = tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT)

    class _Context:
        def __enter__(self):
            return archive

        def __exit__(self, exc_type, exc, traceback):
            archive.close()
            compressed.close()
            raw.close()

    return _Context()


def _add_tree(archive: tarfile.TarFile, root: Path, destination: PurePosixPath) -> None:
    _add_path(archive, root, destination, excluded_parts=SOURCE_EXCLUDED_PARTS)


def _add_path(
    archive: tarfile.TarFile,
    source: Path,
    destination: PurePosixPath,
    *,
    excluded_parts: set[str] | None = None,
) -> None:
    excluded = excluded_parts or set()
    if source.name in excluded:
        return
    info = source.lstat()
    if stat.S_ISDIR(info.st_mode):
        _add_directory(archive, str(destination))
        for child in sorted(source.iterdir(), key=lambda item: item.name):
            _add_path(archive, child, destination / child.name, excluded_parts=excluded)
        return
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(source)
        _validate_symlink_target(target)
        tar_info = _tar_info(str(destination), mode=0o777, size=0)
        tar_info.type = tarfile.SYMTYPE
        tar_info.linkname = target
        archive.addfile(tar_info)
        return
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"unsupported staging file type: {source}")
    mode = 0o755 if info.st_mode & stat.S_IXUSR else 0o644
    tar_info = _tar_info(str(destination), mode=mode, size=info.st_size)
    with source.open("rb") as handle:
        archive.addfile(tar_info, handle)


def _add_directory(archive: tarfile.TarFile, name: str) -> None:
    info = _tar_info(name.rstrip("/") + "/", mode=0o755, size=0)
    info.type = tarfile.DIRTYPE
    archive.addfile(info)


def _add_bytes(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    archive.addfile(_tar_info(name, mode=0o644, size=len(payload)), io.BytesIO(payload))


def _tar_info(name: str, *, mode: int, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name=name)
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.mode = mode
    info.size = size
    return info


def _validate_symlink_target(target: str) -> None:
    path = PurePosixPath(target)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe symlink target in release staging: {target}")


def _bundle_identifier(app: Path) -> str:
    with (app / "Contents" / "Info.plist").open("rb") as handle:
        payload = plistlib.load(handle)
    return str(payload.get("CFBundleIdentifier") or "")


def _snapshot_files(root: Path, files: Iterable[str]) -> list[dict[str, object]]:
    return [
        {"path": relative, "sha256": _sha256_file(root / relative), "sizeBytes": (root / relative).stat().st_size}
        for relative in files
    ]


def _snapshot_digest(records: list[dict[str, object]]) -> str:
    payload = json.dumps(records, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def content_tree_digest(
    root: str | Path,
    *,
    excluded_paths: Iterable[str] = (),
    excluded_file_names: Iterable[str] = (),
) -> str:
    """Return the stable bare-hex digest used by the frontend dist marker."""

    tree_root = Path(root).resolve()
    excluded = {PurePosixPath(path).as_posix() for path in excluded_paths}
    excluded_names = set(excluded_file_names)
    if not tree_root.is_dir() or tree_root.is_symlink():
        raise ValueError(f"invalid content tree: {root}")
    digest = hashlib.sha256()
    for path in sorted(tree_root.rglob("*"), key=lambda item: item.relative_to(tree_root).as_posix()):
        relative = path.relative_to(tree_root).as_posix()
        if relative in excluded:
            continue
        if path.is_symlink():
            raise ValueError(f"content tree contains a symlink: {relative}")
        if not path.is_file() or path.name in excluded_names:
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def control_center_dist_digest(root: str | Path) -> str:
    """Bind frontend code while allowing Finder to update directory metadata."""
    if Path(root).is_symlink():
        raise ValueError(f"invalid control-center dist tree: {root}")
    return content_tree_digest(
        root, excluded_paths=(CONTROL_DIST_MARKER,),
        excluded_file_names=(".DS_Store",),
    )


def _tree_snapshot(root: Path, *, excluded_parts: set[str] | None = None) -> list[dict[str, object]]:
    excluded = excluded_parts or set()
    records = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if set(relative.parts) & excluded:
            continue
        if path.is_file() and not path.is_symlink():
            records.append({"path": relative.as_posix(), "sha256": _sha256_file(path)})
        elif path.is_symlink():
            records.append({"path": relative.as_posix(), "symlink": os.readlink(path)})
    return records


def _artifact_record(path: Path, *, kind: str) -> dict[str, object]:
    return {"kind": kind, "path": str(path), "sha256": _sha256_file(path), "sizeBytes": path.stat().st_size}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_lines(root: Path, *args: str) -> list[str]:
    completed = subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)
    return [line for line in completed.stdout.splitlines() if line]


def _git_value(root: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=root, check=False, capture_output=True, text=True)
    return completed.stdout.strip() if completed.returncode == 0 else ""
