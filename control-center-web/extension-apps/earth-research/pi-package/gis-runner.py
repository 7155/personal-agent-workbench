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
import zipfile
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
SUPPORTED_EXPORTS = {"geojson", "shp", "gpkg", "kml"}


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


def require_known_crs(crs: Any) -> None:
    # GeoPackage may expose a WKT named "Undefined geographic SRS" rather
    # than None. That is not evidence that the coordinates are WGS84.
    if crs is None or str(getattr(crs, "name", "")).lower().startswith("undefined"):
        fail("Source CRS is unknown; assign its verified CRS before querying or loading it", code="unknown_crs")


def raster_band(request: dict[str, Any], count: int) -> int:
    value = request.get("band", 1)
    if value is None:
        value = 1
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        fail("Raster band must be an integer", code="invalid_params")
    if isinstance(value, bool) or not math.isfinite(numeric) or not numeric.is_integer() or not 1 <= numeric <= count:
        fail(f"Raster band must be an integer between 1 and {count}", code="invalid_params")
    return int(numeric)


def raster_pixel(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    """Read one raster cell at a WGS84 coordinate without creating a layer."""
    require_runtime()
    target = inside(root, request.get("path"))
    if kind_for(target) != "raster":
        fail("Pixel queries require a GeoTIFF/IMG raster", code="unsupported_format")
    try:
        longitude = float(request.get("longitude"))
        latitude = float(request.get("latitude"))
    except (TypeError, ValueError):
        fail("Pixel query requires numeric longitude and latitude", code="invalid_params")
    if isinstance(request.get("longitude"), bool) or isinstance(request.get("latitude"), bool) or not math.isfinite(longitude) or not math.isfinite(latitude) or abs(longitude) > 180 or abs(latitude) > 90:
        fail("Pixel query requires finite WGS84 longitude and latitude", code="invalid_params")
    with rasterio.open(target) as source:
        require_known_crs(source.crs)
        band = raster_band(request, source.count)
        x, y = longitude, latitude
        if str(source.crs).upper() not in {"EPSG:4326", "OGC:CRS84"}:
            from rasterio.warp import transform
            x, y = transform("EPSG:4326", source.crs, [longitude], [latitude])
            x, y = x[0], y[0]
        if not math.isfinite(x) or not math.isfinite(y):
            fail("Pixel coordinate cannot be transformed into the raster CRS", code="invalid_params")
        row, column = source.index(x, y)
        receipt = {"path": str(target.relative_to(root)), "longitude": longitude, "latitude": latitude, "band": band, "row": row, "column": column, "crs": str(source.crs)}
        if row < 0 or row >= source.height or column < 0 or column >= source.width:
            return {**receipt, "status": "outside", "value": None, "nodata": True}
        value = source.read(band, window=((row, row + 1), (column, column + 1)), masked=True)[0, 0]
        if np.iscomplexobj(value):
            fail("Pixel queries require a real-valued raster band", code="unsupported_format")
        nodata = bool(np.ma.is_masked(value) or not np.isfinite(value))
        return {**receipt, "status": "completed", "value": None if nodata else float(value), "nodata": nodata}


def raster_region(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    """Read only a polygon's raster window, masking holes and nodata cells."""
    require_runtime()
    from rasterio.features import geometry_mask, geometry_window
    from rasterio.warp import transform_geom
    from shapely.geometry import mapping, shape

    target = inside(root, request.get("path"))
    if kind_for(target) != "raster":
        fail("Region queries require a GeoTIFF/IMG raster", code="unsupported_format")
    geometry = request.get("geometry")
    if isinstance(geometry, dict) and geometry.get("type") == "Feature":
        geometry = geometry.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
        fail("Region query requires a WGS84 Polygon or MultiPolygon", code="invalid_geometry")
    try:
        polygon = shape(geometry)
        if polygon.is_empty or not polygon.is_valid:
            raise ValueError("polygon is empty or invalid")
        polygons = [polygon] if polygon.geom_type == "Polygon" else list(polygon.geoms)
        for part in polygons:
            for ring in [part.exterior, *part.interiors]:
                for longitude, latitude, *_ in ring.coords:
                    if not math.isfinite(longitude) or not math.isfinite(latitude) or abs(longitude) > 180 or abs(latitude) > 90:
                        raise ValueError("coordinates must be finite WGS84 longitude and latitude")
    except (TypeError, ValueError, KeyError) as exc:
        fail(f"Region query geometry is invalid: {exc}", code="invalid_geometry")
    all_touched = request.get("allTouched", False)
    if all_touched is None:
        all_touched = False
    if not isinstance(all_touched, bool):
        fail("allTouched must be a boolean", code="invalid_params")
    with rasterio.open(target) as source:
        require_known_crs(source.crs)
        band = raster_band(request, source.count)
        projected = transform_geom("EPSG:4326", source.crs, mapping(polygon))
        if not shape(projected).is_valid:
            fail("Region cannot be transformed into the raster CRS", code="invalid_geometry")
        # Keep the requested grid window, including cells outside the raster,
        # so validPixelCoverage does not hide missing coverage at its edges.
        window = geometry_window(source, [projected], boundless=True)
        width, height = int(window.width), int(window.height)
        if width < 1 or height < 1:
            fail("Region contains no raster cells", code="invalid_geometry")
        if width * height > 4_000_000:
            fail("Region window exceeds 4 million cells; query a smaller region", code="query_too_large")
        selected = geometry_mask([projected], out_shape=(height, width), transform=source.window_transform(window), all_touched=all_touched, invert=True)
        total = int(selected.sum())
        row_offset, column_offset = int(window.row_off), int(window.col_off)
        in_raster = ((np.arange(height) + row_offset >= 0) & (np.arange(height) + row_offset < source.height))[:, None] & ((np.arange(width) + column_offset >= 0) & (np.arange(width) + column_offset < source.width))[None, :]
        raster_pixels = int((selected & in_raster).sum())
        stats = {"min": None, "max": None, "mean": None, "sum": None, "stddev": None}
        valid_pixels = 0
        if raster_pixels:
            values = source.read(band, window=window, boundless=True, masked=True)
            if np.iscomplexobj(values):
                fail("Region queries require a real-valued raster band", code="unsupported_format")
            valid = selected & in_raster & ~np.ma.getmaskarray(values) & np.isfinite(values.data)
            samples = values.data[valid].astype("float64")
            valid_pixels = int(samples.size)
            if valid_pixels:
                stats = {"min": float(samples.min()), "max": float(samples.max()), "mean": float(samples.mean()), "sum": float(samples.sum()), "stddev": float(samples.std())}
        return {
            "status": "completed" if raster_pixels else "outside",
            "path": str(target.relative_to(root)), "band": band, "crs": str(source.crs),
            "geometry": mapping(polygon), "geometryCrs": "EPSG:4326", "allTouched": all_touched,
            "window": {"rowOffset": row_offset, "columnOffset": column_offset, "width": width, "height": height},
            "totalPixels": total, "rasterPixels": raster_pixels, "validPixels": valid_pixels,
            "nodataPixels": raster_pixels - valid_pixels, "outsidePixels": total - raster_pixels,
            "validPixelCoverage": valid_pixels / total if total else None,
            "coverageBasis": "selected raster grid cells, including cells outside the source extent",
            "stats": stats,
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
    for file in sorted(result_dir.rglob("*")):
        if not file.is_file() or file.name.startswith("."):
            continue
        suffix = file.suffix.lower()
        kind = "vector" if suffix in {".geojson", ".json", ".kml", ".shp", ".gpkg", ".sqlite"} else "raster" if suffix in {".tif", ".tiff"} else "file"
        item: dict[str, Any] = {"path": str(file), "relativePath": str(file.relative_to(run_dir)), "name": file.name, "kind": kind, "bytes": file.stat().st_size}
        if kind == "vector":
            frame = gpd.read_file(file)
            summary = {"featureCount": len(frame), "crs": str(frame.crs), "areaM2": None, "measurementCrs": None}
            if frame.crs and not frame.empty:
                metric = frame.estimate_utm_crs() if frame.crs.is_geographic else frame.crs
                # Only meter-based projections supply an area in square meters.
                if metric and all(axis.unit_name.lower() in {"metre", "meter"} for axis in metric.axis_info):
                    measured = frame.to_crs(metric)
                    polygons = measured.geometry.geom_type.isin(["Polygon", "MultiPolygon"])
                    summary.update(areaM2=float(measured.loc[polygons].geometry.area.sum()), measurementCrs=str(metric))
                if file.stat().st_size <= 2_000_000:
                    preview = json.loads(frame.to_crs("EPSG:4326").to_json())
                    if suffix in {".geojson", ".json"}:
                        original = json.loads(file.read_text(encoding="utf-8"))
                        features = original.get("features", []) if original.get("type") == "FeatureCollection" else [original]
                        # Keep the original id and properties; only the preview
                        # geometry changes when a source uses another CRS.
                        preview["features"] = [{**feature, "geometry": converted["geometry"]} for feature, converted in zip(features, preview["features"])]
                    elif suffix in {".gpkg", ".sqlite"}:
                        import fiona

                        with fiona.open(file) as collection:
                            tags = collection.tags()
                            rows = list(collection)
                        identity_json = tags.get("PAW_FEATURE_ID_JSON_FIELD")
                        identity_display = tags.get("PAW_FEATURE_ID_FIELD")
                        for feature, row in zip(preview["features"], rows):
                            properties = row["properties"]
                            feature_id = json.loads(properties[identity_json]) if identity_json else properties.get("id", row.id)
                            if isinstance(feature_id, str) or type(feature_id) in {int, float}:
                                feature["id"] = feature_id
                            else:
                                feature.pop("id", None)
                            for helper in [identity_json, identity_display]:
                                if helper:
                                    feature["properties"].pop(helper, None)
                    item["geojson"] = preview
            elif frame.empty:
                item["geojson"] = {"type": "FeatureCollection", "features": []}
            item["summary"] = summary
        files.append(item)
    return files


def safe_layer_name(value: Any) -> str:
    import hashlib
    import unicodedata

    if not isinstance(value, str) or not value.strip() or len(value) > 160 or re.search(r"[\x00-\x1f\x7f/\\]", value):
        fail("Layer display name is invalid", code="invalid_output")
    value = value.strip()
    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,62}", value) and value.lower() not in reserved:
        return value
    stem = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-_") or "layer"
    if not stem[0].isalpha() or stem.lower() in reserved:
        stem = f"layer-{stem}"
    return f"{stem[:50]}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:12]}"


def export_layer(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    require_runtime()
    source = inside(root, request.get("input"))
    if kind_for(source) != "vector":
        fail("Only vector layers can be exported as SHP, GeoPackage, GeoJSON or KML", code="unsupported_format")
    display_name = request.get("name") or source.stem
    output_name = safe_layer_name(display_name)
    export_format = str(request.get("format") or "geojson").lower()
    if export_format not in SUPPORTED_EXPORTS:
        fail(f"Unsupported export format: {export_format}", code="unsupported_format")
    scope = request.get("scope", "all")
    feature_ids = request.get("featureIds", [])
    if scope not in {"all", "selected"}:
        fail("Export scope must be all or selected", code="invalid_selection_scope")
    if scope == "selected" and (not isinstance(feature_ids, list) or not feature_ids):
        fail("Selected export requires a non-empty featureIds array", code="selected_features_required")
    if not isinstance(feature_ids, list) or (scope == "all" and feature_ids):
        fail("featureIds may only be used with selected export", code="invalid_selection_scope")

    def valid_id(value: Any) -> bool:
        return (isinstance(value, str) and bool(value.strip())) or (type(value) in {int, float} and math.isfinite(value))

    def id_key(value: Any) -> tuple[str, Any]:
        return ("string" if isinstance(value, str) else "number", value)

    if any(not valid_id(value) for value in feature_ids):
        fail("Feature IDs must be non-empty strings or finite numbers", code="invalid_feature_id")
    if len({id_key(value) for value in feature_ids}) != len(feature_ids):
        fail("Selected feature IDs must not contain duplicates", code="duplicate_feature_id")
    layer_id = request.get("layerId")
    revision = request.get("revision")
    if layer_id is not None and (not isinstance(layer_id, str) or not layer_id.strip()):
        fail("Layer ID must be a non-empty string", code="invalid_layer_id")
    if revision is not None and (type(revision) is not int or revision < 1):
        fail("Layer revision must be a positive integer", code="invalid_layer_revision")
    version_binding = None
    catalog_path = root / ".earth/layers/catalog.json"
    if layer_id is not None and catalog_path.exists():
        catalog = json.loads(inside(root, ".earth/layers/catalog.json").read_text(encoding="utf-8"))
        matches = [layer for layer in catalog.get("layers", []) if layer.get("id") == layer_id]
        if len(matches) != 1:
            fail("Export layer ID is absent or ambiguous in the project catalog", code="missing_project_layer")
        layer = matches[0]
        current_revision = layer.get("revision", 1)
        current_path = inside(root, layer.get("path"))
        if source == current_path and revision == current_revision:
            version_binding = "catalog-current"
        else:
            # Explicit older immutable paths can still be exported after the
            # current catalog advances; a stale current-path request cannot.
            history_match = False
            if revision is not None and revision < current_revision:
                for historical in layer.get("history", []):
                    version = re.search(r"(?:-v(\d+)-[0-9a-f]{8}|/v(\d+))\.geojson$", str(historical))
                    if version and int(version.group(1) or version.group(2)) == revision and inside(root, historical) == source:
                        history_match = True
                        break
            if not history_match:
                fail("Export source or revision does not match the project layer; reload it or bind an immutable historical version", code="stale_layer_revision")
            version_binding = "catalog-history"

    target_crs = request.get("targetCrs")
    source_layer = request.get("sourceLayer")
    source_features = None
    if source.suffix.lower() in {".geojson", ".json"}:
        document = json.loads(source.read_text(encoding="utf-8"))
        source_features = document.get("features") if document.get("type") == "FeatureCollection" else [document] if document.get("type") == "Feature" else None
        if not isinstance(source_features, list) or any(not isinstance(feature, dict) or feature.get("type") != "Feature" for feature in source_features):
            fail("Export source must contain GeoJSON features", code="invalid_source")
        source_crs = gpd.read_file(source).crs
        # GDAL can merge a top-level id into properties.id. Build the frame from
        # the original properties so the business field survives unchanged.
        frame = gpd.GeoDataFrame.from_features(source_features, crs=source_crs) if source_features else gpd.GeoDataFrame(geometry=[], crs=source_crs)
        canonical_ids = [feature.get("id") if "id" in feature else (feature.get("properties") or {}).get("id") for feature in source_features]
    else:
        import fiona

        layers = fiona.listlayers(source)
        if source_layer is not None and (not isinstance(source_layer, str) or source_layer not in layers):
            fail("The requested source layer does not exist", code="missing_source_layer")
        if source_layer is None and len(layers) != 1:
            fail("A multi-layer source requires sourceLayer", code="source_layer_required")
        source_layer = source_layer or layers[0]
        frame = gpd.read_file(source, layer=source_layer)
        with fiona.open(source, layer=source_layer) as collection:
            metadata = collection.tags()
            rows = list(collection)
        identity_json = metadata.get("PAW_FEATURE_ID_JSON_FIELD")
        if identity_json:
            if identity_json not in frame.columns:
                fail("The source identity field is missing", code="invalid_source_identity")
            canonical_ids = [json.loads(value) for value in frame[identity_json]]
        else:
            canonical_ids = [row["properties"].get("id") if "id" in row["properties"] else row.id for row in rows]
        if len(canonical_ids) != len(frame):
            fail("The source identity count does not match its features", code="invalid_source_identity")

    selected_positions = list(range(len(frame)))
    if scope == "selected":
        positions_by_id: dict[tuple[str, Any], list[int]] = {}
        for position, value in enumerate(canonical_ids):
            if valid_id(value):
                positions_by_id.setdefault(id_key(value), []).append(position)
        selected_positions = []
        for value in feature_ids:
            matches = positions_by_id.get(id_key(value), [])
            if not matches:
                fail(f"Selected feature ID was not found: {json.dumps(value, ensure_ascii=False)}", code="selected_feature_missing")
            if len(matches) != 1:
                fail(f"Selected feature ID is ambiguous: {json.dumps(value, ensure_ascii=False)}", code="ambiguous_feature_id")
            selected_positions.append(matches[0])
        frame = frame.iloc[selected_positions].copy()
    exported_ids = [canonical_ids[position] if valid_id(canonical_ids[position]) else None for position in selected_positions]
    if target_crs:
        if frame.crs is None:
            fail("A target CRS requires the source layer to have a CRS", code="missing_crs")
        frame = frame.to_crs(str(target_crs))

    identity_field = identity_json_field = None
    if export_format == "gpkg":
        # Dedicated fields preserve feature identity without overwriting any
        # source attributes; JSON also preserves mixed string/number IDs.
        def available_field(base: str) -> str:
            candidate, suffix = base, 1
            while candidate.lower() in {str(column).lower() for column in frame.columns}:
                candidate = f"{base}_{suffix}"
                suffix += 1
            return candidate

        identity_field = available_field("paw_feature_id")
        frame[identity_field] = [str(value) if value is not None else None for value in exported_ids]
        identity_json_field = available_field("paw_feature_id_json")
        frame[identity_json_field] = [json.dumps(value, ensure_ascii=False, allow_nan=False) for value in exported_ids]

    result_dir = inside(root, request.get("runDir"), allow_missing=True) / "pred_results"
    result_dir.mkdir(parents=True, exist_ok=True)
    if export_format == "shp":
        folder = result_dir / f"{output_name}_shp"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{output_name}.shp"
        frame.to_file(target, driver="ESRI Shapefile", index=False)
        archive = result_dir / f"{output_name}.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for sidecar in sorted(folder.iterdir()):
                if sidecar.is_file():
                    bundle.write(sidecar, arcname=sidecar.name)
        driver = "ESRI Shapefile"
    elif export_format == "geojson":
        target = result_dir / f"{output_name}.geojson"
        if source_features is not None:
            features = [dict(source_features[position]) for position in selected_positions]
            if target_crs:
                for feature, transformed in zip(features, json.loads(frame.to_json())["features"]):
                    feature["geometry"] = transformed["geometry"]
        else:
            features = json.loads(frame.to_json(drop_id=True))["features"]
            for feature, feature_id in zip(features, exported_ids):
                if feature_id is not None:
                    feature["id"] = feature_id
        output_document: dict[str, Any] = {"type": "FeatureCollection", "features": features}
        if frame.crs is not None and frame.crs.to_epsg() != 4326:
            output_document["crs"] = {"type": "name", "properties": {"name": str(frame.crs)}}
        target.write_text(json.dumps(output_document, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        driver = "GeoJSON"
    else:
        extension = ".geojson" if export_format == "geojson" else f".{export_format}"
        target = result_dir / f"{output_name}{extension}"
        kwargs: dict[str, Any] = {"driver": {"geojson": "GeoJSON", "gpkg": "GPKG", "kml": "KML"}[export_format], "index": False}
        if export_format == "gpkg":
            kwargs["layer"] = str(request.get("layer") or output_name)
            kwargs["metadata"] = {"PAW_FEATURE_ID_FIELD": identity_field, "PAW_FEATURE_ID_JSON_FIELD": identity_json_field}
        frame.to_file(target, **kwargs)
        driver = kwargs["driver"]
        if export_format == "gpkg":
            readback = gpd.read_file(target, layer=kwargs["layer"])
            restored_ids = [json.loads(value) for value in readback[identity_json_field]]
            if len(readback) != len(frame) or restored_ids != exported_ids:
                fail("Exported GeoPackage IDs do not match the requested features", code="export_identity_mismatch")
    outputs = output_files(inside(root, request.get("runDir"), allow_missing=True))
    if not outputs:
        fail("Layer export completed without a file output", code="empty_output")
    receipt = {
        "schemaVersion": "earth.gis-export.v1",
        "status": "completed",
        "input": str(source.relative_to(root)),
        "format": export_format,
        "driver": driver,
        "name": output_name,
        "displayName": display_name,
        "scope": scope,
        "selectedFeatureIds": feature_ids,
        "exportedFeatureIds": exported_ids,
        "layerId": layer_id,
        "revision": revision,
        "layerVersionBinding": version_binding,
        "sourceLayer": source_layer,
        "identityField": identity_field,
        "identityJsonField": identity_json_field,
        "targetCrs": str(frame.crs) if frame.crs is not None else None,
        "featureCount": int(len(frame)),
        "exportCount": int(len(frame)),
        "outputs": outputs,
    }
    (inside(root, request.get("runDir"), allow_missing=True) / "export.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return receipt


def catalog_source(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    require_runtime()
    source = inside(root, request.get("path"))
    if source.suffix.lower() not in {".gpkg", ".sqlite", ".db"}:
        fail("Spatial catalog currently accepts GeoPackage/SpatiaLite files", code="unsupported_format")
    try:
        import fiona
        layers = [str(name) for name in fiona.listlayers(source)]
    except Exception as exc:
        fail(f"Unable to list spatial database layers: {type(exc).__name__}: {exc}", code="catalog_failed")
    return {"status": "completed", "path": str(source.relative_to(root)), "kind": "geopackage" if source.suffix.lower() == ".gpkg" else "spatialite", "layers": layers}


def load_source(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    """Materialize the explicitly selected database layer for project editing."""
    import hashlib
    import uuid
    from datetime import datetime, timezone
    import fiona

    catalog = catalog_source(root, request)
    source = inside(root, request.get("path"))
    layer = request.get("layer")
    if not isinstance(layer, str) or not layer or "\x00" in layer:
        fail("A spatial database layer name is required", code="invalid_layer")
    if layer not in catalog["layers"]:
        fail(f"Spatial database layer does not exist: {layer}", code="layer_not_found")
    frame = gpd.read_file(source, layer=layer)
    require_known_crs(frame.crs)
    source_crs = str(frame.crs)
    # Read the feature-id codec written by PAW exports. Plain source layers
    # retain their business id, then the database feature id as a fallback.
    with fiona.open(source, layer=layer) as collection:
        metadata = collection.tags()
        row_ids = [row.id for row in collection]
    identity_json = metadata.get("PAW_FEATURE_ID_JSON_FIELD")
    identity_field = metadata.get("PAW_FEATURE_ID_FIELD")
    if identity_json and identity_json not in frame.columns:
        fail("The source identity field is missing", code="invalid_source_identity")
    projected = frame.to_crs("EPSG:4326")
    data = json.loads(projected.to_json(drop_id=True, na="null"))
    for index, feature in enumerate(data["features"]):
        properties = feature["properties"]
        if identity_json:
            try:
                identifier = json.loads(properties.get(identity_json))
            except (TypeError, ValueError):
                fail("The source identity codec is invalid", code="invalid_source_identity")
        elif identity_field and identity_field in properties:
            identifier = properties[identity_field]
        else:
            identifier = properties.get("id")
            if identifier is None:
                identifier = row_ids[index] if index < len(row_ids) else None
        if isinstance(identifier, str) and identifier or isinstance(identifier, (int, float)) and not isinstance(identifier, bool) and math.isfinite(identifier):
            feature["id"] = identifier
        for field in [identity_json, identity_field]:
            if field:
                properties.pop(field, None)
    bounds = [float(value) for value in projected.total_bounds] if len(projected) else None
    if bounds is not None and not all(math.isfinite(value) for value in bounds):
        fail("Layer bounds cannot be transformed into WGS84", code="invalid_geometry")
    with source.open("rb") as handle:
        source_sha256 = hashlib.file_digest(handle, "sha256").hexdigest()
    supplied_lineage = request.get("sourceLineage")
    lineage = {
        **(supplied_lineage if isinstance(supplied_lineage, dict) else {}),
        "sourcePath": str(source.relative_to(root)), "layer": layer,
        "sourceCrs": source_crs, "sourceSha256": source_sha256,
        "loadedAt": datetime.now(timezone.utc).isoformat(),
    }
    data.update(sourceId=lineage.get("sourceId"), sourceLineage=lineage)
    target = inside(root, request.get("output"), allow_missing=True)
    if target.suffix.lower() != ".geojson" or target == source:
        fail("Loaded layer output must be a separate GeoJSON path", code="invalid_output")
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
    temporary = target.with_name(f"{target.name}.{uuid.uuid4()}.tmp")
    try:
        temporary.write_text(encoded, encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    lineage_path = target.with_suffix(".lineage.json")
    receipt = {
        "schemaVersion": "earth.spatial-layer-load.v1", "status": "completed",
        "path": str(target.relative_to(root)), "lineagePath": str(lineage_path.relative_to(root)),
        "sourceId": lineage.get("sourceId"), "sourceLineage": lineage,
        "layer": layer, "sourceCrs": source_crs, "crs": "EPSG:4326",
        "featureCount": len(projected), "bounds": bounds,
        "geometryTypes": sorted({str(value) for value in projected.geometry.geom_type.dropna().unique()}),
    }
    temporary_receipt = lineage_path.with_name(f"{lineage_path.name}.{uuid.uuid4()}.tmp")
    try:
        temporary_receipt.write_text(json.dumps(receipt, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        os.replace(temporary_receipt, lineage_path)
    finally:
        temporary_receipt.unlink(missing_ok=True)
    if len(encoded.encode("utf-8")) <= 2_000_000:
        receipt["geojson"] = data
    return receipt


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
    if operation == "pixel":
        print(json.dumps(raster_pixel(root, request), ensure_ascii=False, default=str))
        return
    if operation == "region":
        print(json.dumps(raster_region(root, request), ensure_ascii=False, default=str))
        return
    if operation == "process":
        print(json.dumps(process(root, request), ensure_ascii=False, default=str))
        return
    if operation == "export":
        print(json.dumps(export_layer(root, request), ensure_ascii=False, default=str))
        return
    if operation == "catalog_source":
        print(json.dumps(catalog_source(root, request), ensure_ascii=False, default=str))
        return
    if operation == "load_source":
        print(json.dumps(load_source(root, request), ensure_ascii=False, default=str))
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
