#!/usr/bin/env python3
"""Bounded deterministic GIS runner for the Earth Agent.

The operation templates are vendored from GISclaw's geo_ops registry. This
runner owns only file validation, input loading, execution, and receipts; it
does not create an LLM loop or accept arbitrary user code.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any

VENDOR = Path(__file__).resolve().parent / "vendor"
sys.path.insert(0, str(VENDOR))

try:
    import geopandas as gpd
    import numpy as np
    import pandas as pd
    import rasterio
except Exception as exc:  # pragma: no cover - exercised by the missing-runtime path
    gpd = np = pd = rasterio = None
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
else:
    IMPORT_ERROR = ""

try:
    import importlib.util
    _spec = importlib.util.spec_from_file_location("gisclaw_geo_ops", VENDOR / "gisclaw-geo-ops.py")
    geo_ops = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(geo_ops)
except Exception as exc:  # pragma: no cover
    geo_ops = None
    GEO_OPS_ERROR = f"{type(exc).__name__}: {exc}"
else:
    GEO_OPS_ERROR = ""

MAX_REQUEST_BYTES = 256_000
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
SUPPORTED_VECTOR = {".geojson", ".json", ".gpkg", ".shp", ".sqlite", ".kml"}
SUPPORTED_RASTER = {".tif", ".tiff", ".img"}


def fail(message: str, *, code: str = "gis_error") -> None:
    print(json.dumps({"status": "failed", "code": code, "error": message}, ensure_ascii=False))
    raise SystemExit(1)


def read_request() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        fail("GIS request exceeds 256 KB", code="request_too_large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        fail(f"Invalid GIS request JSON: {exc}", code="invalid_request")
    if not isinstance(value, dict):
        fail("GIS request must be an object", code="invalid_request")
    return value


def root_path(value: Any) -> Path:
    if not isinstance(value, str) or not value.startswith("/"):
        fail("GIS root must be an absolute path", code="invalid_root")
    root = Path(value).resolve()
    if not root.is_dir():
        fail("GIS workspace does not exist", code="invalid_root")
    return root


def inside(root: Path, value: Any, *, allow_missing: bool = False) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        fail("GIS path is invalid", code="invalid_path")
    candidate = (root / value).resolve() if not value.startswith("/") else Path(value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        fail("GIS path escapes the bound workspace", code="path_escape")
    if not allow_missing and not candidate.exists():
        fail(f"GIS path does not exist: {value}", code="missing_path")
    return candidate


def require_runtime() -> None:
    if IMPORT_ERROR:
        fail(
            "GIS runtime is missing geopandas/rasterio dependencies. "
            "Install the Earth GIS runtime, then retry. " + IMPORT_ERROR,
            code="runtime_missing",
        )
    if GEO_OPS_ERROR or geo_ops is None:
        fail(f"GIS operation registry unavailable: {GEO_OPS_ERROR}", code="registry_missing")


def kind_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in SUPPORTED_VECTOR:
        return "vector"
    if suffix in SUPPORTED_RASTER:
        return "raster"
    return "other"


def vector_summary(path: Path) -> dict[str, Any]:
    require_runtime()
    frame = gpd.read_file(path)
    return {
        "kind": "vector",
        "path": str(path),
        "rows": int(len(frame)),
        "columns": [str(column) for column in frame.columns],
        "crs": str(frame.crs) if frame.crs is not None else None,
        "bounds": [float(value) for value in frame.total_bounds] if len(frame) else None,
        "geometryTypes": sorted({str(value) for value in frame.geometry.geom_type.dropna().unique()}),
        "sample": json.loads(frame.head(3).drop(columns="geometry", errors="ignore").to_json(orient="records", date_format="iso")),
    }


def raster_summary(path: Path) -> dict[str, Any]:
    require_runtime()
    with rasterio.open(path) as source:
        values = source.read(masked=True)
        compressed = values.compressed() if hasattr(values, "compressed") else np.asarray(values).reshape(-1)
        finite = compressed[np.isfinite(compressed)] if len(compressed) else compressed
        return {
            "kind": "raster",
            "path": str(path),
            "width": int(source.width),
            "height": int(source.height),
            "bands": int(source.count),
            "dtype": str(source.dtypes[0]),
            "crs": str(source.crs) if source.crs is not None else None,
            "bounds": [float(value) for value in source.bounds],
            "min": float(finite.min()) if len(finite) else None,
            "max": float(finite.max()) if len(finite) else None,
            "nodata": source.nodata,
        }


def inspect(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    target = inside(root, request.get("path"))
    kind = kind_for(target)
    if kind == "vector":
        return vector_summary(target)
    if kind == "raster":
        return raster_summary(target)
    fail(f"Unsupported GIS input format: {target.suffix or target.name}", code="unsupported_format")


def load_input(root: Path, path_value: Any, role: str, namespace: dict[str, Any]) -> dict[str, str]:
    path = inside(root, path_value)
    kind = kind_for(path)
    if kind == "vector":
        namespace[role] = gpd.read_file(path)
        return {"path": str(path), "kind": kind}
    if kind == "raster":
        with rasterio.open(path) as source:
            values = source.read().astype(float)
            namespace[role] = values[0] if source.count == 1 else values
            namespace[f"{role}_meta"] = {
                "crs": source.crs,
                "transform": source.transform,
                "width": source.width,
                "height": source.height,
                "nodata": source.nodata,
                "dtype": source.dtypes[0],
                "count": source.count,
                "bounds": source.bounds,
            }
        return {"path": str(path), "kind": kind}
    fail(f"Unsupported GIS input format: {path.suffix or path.name}", code="unsupported_format")


def output_files(run_dir: Path) -> list[dict[str, Any]]:
    result_dir = run_dir / "pred_results"
    if not result_dir.is_dir():
        return []
    files: list[dict[str, Any]] = []
    for file in sorted(result_dir.iterdir()):
        if not file.is_file() or file.name.startswith("."):
            continue
        suffix = file.suffix.lower()
        kind = "vector" if suffix in {".geojson", ".json", ".kml"} else "raster" if suffix in {".tif", ".tiff"} else "file"
        item: dict[str, Any] = {"path": str(file), "relativePath": str(file.relative_to(run_dir)), "name": file.name, "kind": kind, "bytes": file.stat().st_size}
        if kind == "vector" and file.stat().st_size <= 2_000_000:
            try:
                item["geojson"] = json.loads(file.read_text(encoding="utf-8"))
            except Exception:
                pass
        files.append(item)
    return files


def process(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    require_runtime()
    op = request.get("op")
    if not isinstance(op, str) or op not in geo_ops.REGISTRY:
        available = ", ".join(sorted(geo_ops.REGISTRY))
        fail(f"Unknown GIS operation '{op}'. Available: {available}", code="unknown_operation")
    output = request.get("output") or f"{op}_out"
    if not isinstance(output, str) or not SAFE_IDENTIFIER.fullmatch(output):
        fail("GIS output must be a safe Python identifier", code="invalid_output")
    run_dir = inside(root, request.get("runDir"), allow_missing=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "pred_results").mkdir(parents=True, exist_ok=True)
    save_as = request.get("saveAs")
    if save_as is not None:
        if not isinstance(save_as, str) or save_as.startswith("/") or ".." in Path(save_as).parts or not save_as.startswith("pred_results/"):
            fail("saveAs must stay inside pred_results/", code="invalid_output")
        save_target = run_dir / save_as
        save_target.parent.mkdir(parents=True, exist_ok=True)
    else:
        save_target = None

    raw_inputs = request.get("inputs") or {}
    if not isinstance(raw_inputs, dict):
        fail("GIS inputs must be an object", code="invalid_inputs")
    namespace: dict[str, Any] = {"__builtins__": __builtins__}
    bindings: dict[str, str] = {}
    loaded: dict[str, dict[str, str]] = {}
    for role, path_value in raw_inputs.items():
        if not isinstance(role, str) or not SAFE_IDENTIFIER.fullmatch(role):
            fail(f"Invalid GIS input role: {role}", code="invalid_inputs")
        variable = f"_input_{role}"
        loaded[role] = load_input(root, path_value, variable, namespace)
        bindings[role] = variable

    params = request.get("params") or {}
    if not isinstance(params, dict):
        fail("GIS params must be an object", code="invalid_params")
    code = geo_ops.build_code(op, bindings, params, output, str(save_target) if save_target else None)
    old_cwd = Path.cwd()
    os.chdir(run_dir)
    try:
        exec(code, namespace, namespace)
    finally:
        os.chdir(old_cwd)

    outputs = output_files(run_dir)
    if not outputs:
        fail("GIS operation completed without a file output", code="empty_output")
    receipt = {
        "schemaVersion": "earth.gis-run.v1",
        "status": "completed",
        "op": op,
        "output": output,
        "inputs": loaded,
        "params": params,
        "outputs": outputs,
        "code": code,
    }
    (run_dir / "result.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return receipt


def main() -> None:
    request = read_request()
    root = root_path(request.get("root"))
    operation = request.get("operation", "")
    if operation == "catalog":
        if geo_ops is None:
            fail(f"GIS operation registry unavailable: {GEO_OPS_ERROR}", code="registry_missing")
        print(json.dumps({"status": "completed", "catalog": geo_ops.catalog()}, ensure_ascii=False))
        return
    if operation == "inspect":
        print(json.dumps({"status": "completed", "result": inspect(root, request)}, ensure_ascii=False, default=str))
        return
    if operation == "process":
        print(json.dumps(process(root, request), ensure_ascii=False, default=str))
        return
    fail(f"Unknown GIS runner operation: {operation}", code="unknown_runner_operation")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        print(json.dumps({"status": "failed", "code": "execution_failed", "error": detail}, ensure_ascii=False))
        raise SystemExit(1)
