#!/usr/bin/env python3
"""Check that the PAWOS real-frontend map still follows production selectors.

This deliberately proves only source ownership and build wiring. It does not
run, install, or foreground the product.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any


DEFAULT_MANIFEST = Path(
    "control-center-web/docs/handoffs/PAWOS_REAL_FRONTEND_SOURCE_MAP.v1.json"
)
REGISTRY = Path("control-center-web/src/features/paw-os/model/app-registry.ts")
CLOUD_ENTRY = Path("control-center-web/CLOUD_MODEL.md")
EXPECTED_KIND = "paw.real-frontend-source-map"
EXPECTED_VERSION = 1
EXPECTED_APP_COUNT = 15
OWNER_KEYS = {"renderOwners", "styleOwners", "supportingOwners"}
FORBIDDEN_OWNER_FRAGMENTS = (
    "/docs/",
    "/dist/",
    "/node_modules/",
    "/fixtures/",
    "/references/",
    ".test.",
    "/preview-",
    "/contracts/generated/",
    "/prototype-data.",
)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("manifest root must be a JSON object")
    return value


def _registry_app_ids(repo_root: Path) -> list[str]:
    source = (repo_root / REGISTRY).read_text(encoding="utf-8")
    try:
        registry = source.split("export const pawOsAppRegistry", 1)[1]
        registry = registry.split("export const primaryDockAppIds", 1)[0]
    except IndexError as error:
        raise ValueError("could not isolate pawOsAppRegistry") from error
    return re.findall(r"\bid:\s*'([^']+)'", registry)


def _walk(value: Any) -> Iterator[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _evidence_items(manifest: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for value in _walk(manifest):
        if isinstance(value, dict) and "file" in value and "contains" in value:
            yield value


def _owner_paths(manifest: dict[str, Any]) -> Iterator[tuple[str, str]]:
    for value in _walk(manifest):
        if not isinstance(value, dict):
            continue
        for key in OWNER_KEYS:
            owners = value.get(key)
            if isinstance(owners, list):
                for owner in owners:
                    if isinstance(owner, str):
                        yield key, owner


def _duplicates(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    duplicate: set[str] = set()
    for value in values:
        if value in seen:
            duplicate.add(value)
        seen.add(value)
    return sorted(duplicate)


def check_manifest(repo_root: Path, manifest: dict[str, Any]) -> list[str]:
    """Return stable, user-readable errors; an empty list means the map aligns."""

    errors: list[str] = []
    if manifest.get("kind") != EXPECTED_KIND:
        errors.append(f"kind must be {EXPECTED_KIND!r}")
    if manifest.get("schemaVersion") != EXPECTED_VERSION:
        errors.append(f"schemaVersion must be {EXPECTED_VERSION}")
    if not isinstance(manifest.get("proofBoundary"), str):
        errors.append("proofBoundary must be present")
    instructions = manifest.get("modelInstructions")
    if not isinstance(instructions, list) or len(instructions) < 4:
        errors.append("modelInstructions must contain at least four rules")

    apps = manifest.get("apps")
    if not isinstance(apps, list):
        errors.append("apps must be a list")
        apps = []
    manifest_ids = [
        app.get("id")
        for app in apps
        if isinstance(app, dict) and isinstance(app.get("id"), str)
    ]
    try:
        registry_ids = _registry_app_ids(repo_root)
    except (OSError, ValueError) as error:
        errors.append(f"cannot read registry: {error}")
        registry_ids = []

    if len(registry_ids) != EXPECTED_APP_COUNT:
        errors.append(
            f"registry must expose {EXPECTED_APP_COUNT} apps, found {len(registry_ids)}"
        )
    if manifest_ids != registry_ids:
        errors.append(
            "manifest app order/identity differs from pawOsAppRegistry: "
            f"manifest={manifest_ids!r}, registry={registry_ids!r}"
        )
    duplicates = _duplicates(manifest_ids)
    if duplicates:
        errors.append(f"duplicate app ids: {duplicates!r}")

    for app in apps:
        if not isinstance(app, dict):
            errors.append("each app must be an object")
            continue
        app_id = app.get("id", "<missing>")
        if app.get("authority") != "production-selected":
            errors.append(f"app {app_id}: authority must be production-selected")
        if not app.get("selectionChain"):
            errors.append(f"app {app_id}: selectionChain is empty")
        if not app.get("renderOwners"):
            errors.append(f"app {app_id}: renderOwners is empty")

    evidence_seen: set[tuple[str, tuple[str, ...]]] = set()
    for item in _evidence_items(manifest):
        relative = item.get("file")
        markers = item.get("contains")
        if not isinstance(relative, str) or not relative:
            errors.append("evidence file must be a non-empty string")
            continue
        if not isinstance(markers, list) or not markers or not all(
            isinstance(marker, str) and marker for marker in markers
        ):
            errors.append(f"{relative}: evidence contains must be non-empty strings")
            continue
        key = (relative, tuple(markers))
        if key in evidence_seen:
            continue
        evidence_seen.add(key)
        path = repo_root / relative
        if not path.is_file():
            errors.append(f"evidence file does not exist: {relative}")
            continue
        source = path.read_text(encoding="utf-8")
        for marker in markers:
            if marker not in source:
                errors.append(f"evidence marker missing from {relative}: {marker!r}")

    owner_seen: set[str] = set()
    for key, relative in _owner_paths(manifest):
        if relative in owner_seen:
            continue
        owner_seen.add(relative)
        normalized = f"/{relative.lstrip('/')}"
        if any(fragment in normalized for fragment in FORBIDDEN_OWNER_FRAGMENTS):
            errors.append(f"{key} points into a non-authoritative area: {relative}")
        if not (repo_root / relative).is_file():
            errors.append(f"{key} file does not exist: {relative}")

    human_guide = manifest.get("humanGuide")
    if not isinstance(human_guide, str) or not (repo_root / human_guide).is_file():
        errors.append(f"humanGuide does not exist: {human_guide!r}")
    else:
        guide = (repo_root / human_guide).read_text(encoding="utf-8")
        if "PAWOS_REAL_FRONTEND_SOURCE_MAP.v1.json" not in guide:
            errors.append("humanGuide does not link the JSON manifest")
        if "python3 scripts/check_pawos_frontend_source_map.py" not in guide:
            errors.append("humanGuide does not publish the checker command")

    cloud_path = repo_root / CLOUD_ENTRY
    if not cloud_path.is_file():
        errors.append(f"cloud entry does not exist: {CLOUD_ENTRY}")
    else:
        cloud = cloud_path.read_text(encoding="utf-8")
        if "docs/handoffs/PAWOS_REAL_FRONTEND_SOURCE_MAP.md" not in cloud:
            errors.append("CLOUD_MODEL.md does not route models to the source map")

    gaps = manifest.get("knownPackagingGaps", [])
    if not isinstance(gaps, list):
        errors.append("knownPackagingGaps must be a list")
    else:
        electron_gap = any(
            isinstance(gap, dict)
            and gap.get("id") == "electron-desktop-build-script-missing"
            for gap in gaps
        )
        electron_script = repo_root / "scripts/build_paw_os_electron_host.sh"
        if electron_gap and electron_script.exists():
            errors.append(
                "electron desktop build script now exists; remove the stale knownPackagingGap"
            )
        if not electron_gap and not electron_script.exists():
            errors.append(
                "electron desktop build script is absent but knownPackagingGaps does not record it"
            )

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="manifest path relative to --root (or an absolute path)",
    )
    args = parser.parse_args(argv)
    repo_root = args.root.resolve()
    manifest_path = args.manifest
    if not manifest_path.is_absolute():
        manifest_path = repo_root / manifest_path

    try:
        manifest = _read_json(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"PAWOS frontend source map: FAIL\n- cannot load manifest: {error}")
        return 1

    errors = check_manifest(repo_root, manifest)
    if errors:
        print("PAWOS frontend source map: FAIL")
        for error in errors:
            print(f"- {error}")
        return 1

    print(
        "PAWOS frontend source map: OK "
        f"({len(manifest['apps'])} apps, "
        f"{len(manifest.get('nativeSurfaces', []))} native surfaces; "
        "source/build wiring only)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
