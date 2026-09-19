#!/usr/bin/env python3
"""Local, bounded raster workflows. No model, network, or cloud credential calls."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

MAX_PIXELS = 4_000_000
MAX_ARRAY_BYTES = 256 * 1024 * 1024
MAX_FEATURES = 2_000
MAX_SAMPLES = 200_000
SAMPLES_PER_GROUP_CLASS = 2_000
SEED = 42


class WorkflowError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def fail(code: str, message: str) -> None:
    raise WorkflowError(code, message)


def inside(root: Path, value: Any) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        fail("invalid_path", "A workspace-relative input path is required.")
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        fail("path_escape", "Input or output escaped the bound workspace.")
    return candidate


def imports() -> None:
    global np, gpd, rasterio, shape, mapping, geometry_mask, transform_geom, geometry_window, Window
    try:
        import numpy as np
        import geopandas as gpd
        import rasterio
        from rasterio.features import geometry_mask, geometry_window
        from rasterio.warp import transform_geom
        from rasterio.windows import Window
        from shapely.geometry import mapping, shape
    except ImportError as exc:
        fail("runtime_missing", f"The local GIS runtime requires numpy, geopandas, rasterio and shapely: {exc}")


def scalar(value: Any) -> str | int | float:
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, str) and value.strip():
        return value
    if type(value) in {int, float} and math.isfinite(value):
        return value
    fail("invalid_label", "Class and group attributes must be non-empty strings or finite numbers.")


def typed_key(value: Any) -> str:
    value = scalar(value)
    return json.dumps(["string" if isinstance(value, str) else "number", value], ensure_ascii=False, allow_nan=False)


def bands_for(dataset, plan: dict[str, Any]) -> list[int]:
    references = plan.get("bands")
    if plan["kind"] == "ndvi":
        references = [references.get("red"), references.get("nir")] if isinstance(references, dict) else references
        if not isinstance(references, list) or len(references) != 2:
            fail("missing_bands", "NDVI requires explicit [red, nir] bands or a {red, nir} mapping.")
    elif references is None:
        references = list(range(1, dataset.count + 1))
    if not isinstance(references, list) or not references or len(references) > 32:
        fail("invalid_bands", "Select between 1 and 32 raster bands.")
    indexes = []
    for reference in references:
        if type(reference) is int and 1 <= reference <= dataset.count:
            index = reference
        elif isinstance(reference, str):
            matches = [index + 1 for index, description in enumerate(dataset.descriptions) if description == reference]
            if len(matches) != 1:
                fail("invalid_bands", f"Raster band description is absent or ambiguous: {reference}")
            index = matches[0]
        else:
            fail("invalid_bands", f"Band index is outside the raster: {reference}")
        indexes.append(index)
    if len(set(indexes)) != len(indexes):
        fail("invalid_bands", "Each selected raster band must be distinct.")
    return indexes


def read_region(root: Path, plan: dict[str, Any]):
    source = inside(root, plan.get("imagePath"))
    if source.suffix.lower() not in {".tif", ".tiff"} or not source.is_file():
        fail("missing_image", "Provide a real local GeoTIFF imagePath.")
    region = plan.get("region")
    if not isinstance(region, dict) or region.get("type") not in {"Polygon", "MultiPolygon"}:
        fail("invalid_region", "A WGS84 Polygon or MultiPolygon region is required.")
    region_shape = shape(region)
    if region_shape.is_empty or not region_shape.is_valid:
        fail("invalid_region", "The analysis region must be non-empty and geometrically valid.")
    west, south, east, north = region_shape.bounds
    if not all(math.isfinite(value) for value in region_shape.bounds) or west < -180 or east > 180 or south < -90 or north > 90:
        fail("invalid_region", "Region coordinates must be WGS84 longitude and latitude.")
    with rasterio.open(source) as dataset:
        if dataset.crs is None:
            fail("missing_crs", "The image needs a CRS before sample or region alignment.")
        indexes = bands_for(dataset, plan)
        projected = transform_geom("EPSG:4326", dataset.crs, region)
        try:
            window = geometry_window(dataset, [projected]).intersection(Window(0, 0, dataset.width, dataset.height))
        except rasterio.errors.WindowError:
            fail("region_outside_image", "The selected region does not overlap the image.")
        width, height = int(window.width), int(window.height)
        pixels = width * height
        if pixels <= 0 or pixels > MAX_PIXELS or pixels * len(indexes) * 4 > MAX_ARRAY_BYTES:
            fail("region_too_large", f"Local execution supports at most {MAX_PIXELS:,} window pixels and 256 MiB of band data; choose a smaller region.")
        if any(np.issubdtype(np.dtype(dataset.dtypes[index - 1]), np.complexfloating) for index in indexes):
            fail("unsupported_dtype", "Complex rasters need an explicit preprocessing step.")
        cube = dataset.read(indexes, window=window, masked=True).astype("float32")
        transform = dataset.window_transform(window)
        roi_mask = geometry_mask([projected], out_shape=(height, width), transform=transform, invert=True)
        valid = roi_mask & ~np.any(np.ma.getmaskarray(cube), axis=0) & np.all(np.isfinite(cube.data), axis=0)
        if not valid.any():
            fail("no_valid_pixels", "The region contains no valid pixels in all required bands.")
        grid = {"crs": dataset.crs, "transform": transform, "width": width, "height": height, "bands": indexes, "bandDescriptions": [dataset.descriptions[index - 1] for index in indexes], "scales": [float(dataset.scales[index - 1]) for index in indexes], "offsets": [float(dataset.offsets[index - 1]) for index in indexes], "region": projected, "roiPixelCount": int(roi_mask.sum()), "validPixelCount": int(valid.sum())}
    return cube.data, valid, grid


def grid_receipt(grid: dict[str, Any]) -> dict[str, Any]:
    transform = grid["transform"]
    return {"crs": str(grid["crs"]), "width": grid["width"], "height": grid["height"], "transform": list(transform)[:6], "resolution": [math.hypot(transform.a, transform.d), math.hypot(transform.b, transform.e)], "bands": grid["bands"], "bandDescriptions": grid["bandDescriptions"], "scales": grid["scales"], "offsets": grid["offsets"], "roiPixelCount": grid["roiPixelCount"], "validPixelCount": grid["validPixelCount"], "resampling": "none; native raster grid"}


def write_json(file: Path, value: Any) -> None:
    file.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")


def write_raster(file: Path, values, grid, nodata, tags=None) -> None:
    with rasterio.open(file, "w", driver="GTiff", height=grid["height"], width=grid["width"], count=1, dtype=values.dtype, crs=grid["crs"], transform=grid["transform"], nodata=nodata, compress="deflate") as target:
        target.write(values, 1)
        if tags:
            target.update_tags(**tags)
    with rasterio.open(file) as readback:
        if readback.crs != grid["crs"] or readback.transform != grid["transform"] or not np.array_equal(readback.read(1), values, equal_nan=True):
            fail("raster_readback_failed", "Raster readback differs from the computed result.")


def raster_preview(root: Path, directory: Path, file: Path, categorical=False):
    import base64
    import io
    from PIL import Image
    from rasterio.warp import calculate_default_transform, reproject, Resampling, transform_bounds
    with rasterio.open(file) as src:
        target_transform, width, height = calculate_default_transform(src.crs, "EPSG:4326", src.width, src.height, *src.bounds)
        ratio = min(1.0, 512 / max(width, height))
        width, height = max(1, int(width * ratio)), max(1, int(height * ratio))
        bounds = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
        target_transform = rasterio.transform.from_bounds(*bounds, width, height)
        values = np.full((height, width), np.nan, dtype="float32")
        reproject(rasterio.band(src, 1), values, src_transform=src.transform, src_crs=src.crs, src_nodata=src.nodata, dst_transform=target_transform, dst_crs="EPSG:4326", dst_nodata=np.nan, resampling=Resampling.nearest if categorical else Resampling.bilinear)
    valid=np.isfinite(values)
    rgba=np.zeros((height,width,4),dtype="uint8")
    if categorical:
        palette=np.array([[38,139,86],[207,127,38],[99,91,186],[45,139,168],[185,66,99],[127,150,61]],dtype="uint8")
        rgba[valid,:3]=palette[(np.nan_to_num(values[valid]).astype(int)-1)%len(palette)]
    else:
        scaled=np.clip((np.nan_to_num(values)+1)/2,0,1)
        rgba[:,:,0]=(220*(1-scaled)).astype('uint8');rgba[:,:,1]=(60+130*scaled).astype('uint8');rgba[:,:,2]=(70*(1-scaled)).astype('uint8')
    rgba[:,:,3]=valid.astype('uint8')*210
    buffer=io.BytesIO();Image.fromarray(rgba).save(buffer,format='PNG')
    (directory/'preview.png').write_bytes(buffer.getvalue())
    return {"dataUrl":"data:image/png;base64,"+base64.b64encode(buffer.getvalue()).decode(),"bounds":[bounds[0],bounds[1],bounds[2],bounds[3]],"width":width,"height":height,"displayCrs":"EPSG:4326","analysisCrs":str(src.crs)}


def outputs(root: Path, directory: Path) -> list[dict[str, Any]]:
    return [{"path": str(file.relative_to(root)), "name": file.name, "kind": "raster" if file.suffix == ".tif" else "file", "bytes": file.stat().st_size} for file in sorted(directory.iterdir()) if file.is_file()]


def ndvi(root: Path, directory: Path, plan: dict[str, Any]) -> dict[str, Any]:
    cube, valid, grid = read_region(root, plan)
    red = cube[0].astype("float64") * grid["scales"][0] + grid["offsets"][0]
    nir = cube[1].astype("float64") * grid["scales"][1] + grid["offsets"][1]
    denominator = nir + red
    valid &= np.isfinite(red) & np.isfinite(nir) & (np.abs(denominator) > 1e-12)
    with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
        calculated = (nir - red) / denominator
    valid &= np.isfinite(calculated) & (np.abs(calculated) <= np.finfo("float32").max)
    if not valid.any():
        fail("no_valid_pixels", "No valid NDVI pixels remain after nodata and zero-denominator masking.")
    result = np.full(valid.shape, np.nan, dtype="float32")
    result[valid] = calculated[valid]
    values = result[valid].astype("float64")
    statistics = {"formula": "(nir - red) / (nir + red)", "count": int(values.size), "maskedPixelCount": int(grid["roiPixelCount"] - values.size), "min": float(values.min()), "max": float(values.max()), "mean": float(values.mean()), "standardDeviation": float(values.std()), "median": float(np.median(values)), "outsideUnitRangeCount": int(((values < -1) | (values > 1)).sum()), "scaling": "GeoTIFF band scale and offset metadata applied; no assumed sensor calibration", "grid": grid_receipt(grid)}
    directory.mkdir(parents=True, exist_ok=True)
    write_raster(directory / "ndvi.tif", result, grid, float("nan"), {"PAW_WORKFLOW": "ndvi", "PAW_BANDS": json.dumps(grid["bands"])})
    write_json(directory / "statistics.json", statistics)
    return {"status": "completed", "execution": "local", "kind": "ndvi", "statistics": statistics, "preview": raster_preview(root,directory,directory / "ndvi.tif"), "outputs": outputs(root, directory)}


def sample_pixels(geometry, transform, valid):
    height, width = valid.shape
    inverse = ~transform
    if geometry.geom_type in {"Point", "MultiPoint"}:
        points = [geometry] if geometry.geom_type == "Point" else list(geometry.geoms)
        positions = []
        for point in points:
            col, row = inverse * (point.x, point.y)
            col, row = math.floor(col), math.floor(row)
            if 0 <= col < width and 0 <= row < height and valid[row, col]:
                positions.append(row * width + col)
        return np.array(sorted(set(positions)), dtype="int64")
    if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
        fail("invalid_sample_geometry", "Training samples must be points or polygons.")
    west, south, east, north = geometry.bounds
    corners = [inverse * point for point in [(west, south), (west, north), (east, south), (east, north)]]
    col0 = max(0, math.floor(min(point[0] for point in corners)))
    col1 = min(width, math.ceil(max(point[0] for point in corners)))
    row0 = max(0, math.floor(min(point[1] for point in corners)))
    row1 = min(height, math.ceil(max(point[1] for point in corners)))
    if col0 >= col1 or row0 >= row1:
        return np.empty(0, dtype="int64")
    local_transform = transform * rasterio.Affine.translation(col0, row0)
    mask = geometry_mask([mapping(geometry)], out_shape=(row1 - row0, col1 - col0), transform=local_transform, invert=True)
    rows, cols = np.nonzero(mask & valid[row0:row1, col0:col1])
    return (rows + row0) * width + cols + col0


def labeled_pixels(root: Path, plan, valid, grid):
    source = inside(root, plan.get("samplePath"))
    if not source.is_file():
        fail("missing_samples", "Persist a labeled point or polygon sample layer before classification.")
    class_field = plan.get("classField")
    if not isinstance(class_field, str) or not class_field:
        fail("missing_class_field", "Choose the class attribute in the sample layer.")
    kwargs = {}
    if source.suffix.lower() == ".gpkg":
        import fiona

        layers = fiona.listlayers(source)
        layer = plan.get("sampleLayer")
        if layer not in layers and (layer is not None or len(layers) != 1):
            fail("missing_sample_layer", "A multi-layer GeoPackage needs an exact sampleLayer.")
        kwargs["layer"] = layer or layers[0]
    if source.suffix.lower() in {".geojson", ".json"} and source.stat().st_size > 32 * 1024 * 1024:
        fail("samples_too_large", "Use a sample layer below 32 MiB for the bounded local workflow.")
    frame = gpd.read_file(source, rows=MAX_FEATURES + 1, **kwargs)
    if len(frame) > MAX_FEATURES or frame.empty:
        fail("invalid_samples", f"Provide between 1 and {MAX_FEATURES} labeled features.")
    if frame.crs is None:
        fail("missing_crs", "Sample coordinates require a CRS.")
    feature_ids = None
    if source.suffix.lower() in {".geojson", ".json"}:
        document = json.loads(source.read_text(encoding="utf-8"))
        features = document.get("features", []) if document.get("type") == "FeatureCollection" else [document]
        feature_ids = [feature.get("id", (feature.get("properties") or {}).get("id", f"feature:{index}")) for index, feature in enumerate(features)]
        frame = gpd.GeoDataFrame.from_features(features, crs=frame.crs)
    if class_field not in frame.columns:
        fail("missing_class_field", f"Sample layer does not contain class field: {class_field}")
    group_field = plan.get("groupField")
    if group_field and group_field not in frame.columns:
        fail("missing_group_field", f"Sample layer does not contain group field: {group_field}")
    frame = frame.to_crs(grid["crs"])
    owner = np.full(valid.size, -1, dtype="int32")
    classes_grid = np.zeros(valid.size, dtype="uint16")
    class_records, class_codes, groups, group_codes, feature_records = [], {}, [], {}, []
    region = shape(grid["region"])
    for position, (_, row) in enumerate(frame.iterrows()):
        value = scalar(row[class_field])
        class_key = typed_key(value)
        if class_key not in class_codes:
            if len(class_codes) >= 256:
                fail("too_many_classes", "Local classification supports at most 256 classes.")
            class_codes[class_key] = len(class_codes) + 1
            class_records.append({"code": class_codes[class_key], "value": value, "valueType": "string" if isinstance(value, str) else "number"})
        code = class_codes[class_key]
        feature_id = feature_ids[position] if feature_ids else f"feature:{position}"
        group_value = scalar(row[group_field]) if group_field else scalar(feature_id)
        group_key = typed_key(group_value)
        if group_key not in group_codes:
            group_codes[group_key] = len(groups)
            groups.append({"group": group_value, "valueType": "string" if isinstance(group_value, str) else "number"})
        group = group_codes[group_key]
        geometry = row.geometry
        if geometry is None or geometry.is_empty or not geometry.is_valid:
            fail("invalid_sample_geometry", "Sample geometries must be non-empty and valid.")
        if geometry.geom_type not in {"Point", "MultiPoint", "Polygon", "MultiPolygon"}:
            fail("invalid_sample_geometry", "Training samples must be points or polygons.")
        if not geometry.intersects(region):
            indexes = np.empty(0, dtype="int64")
        else:
            indexes = sample_pixels(geometry, grid["transform"], valid)
        if indexes.size:
            if ((classes_grid[indexes] != 0) & (classes_grid[indexes] != code)).any():
                fail("conflicting_labels", "Overlapping sample pixels carry different classes; correct the labels before training.")
            if ((owner[indexes] != -1) & (owner[indexes] != group)).any():
                fail("overlapping_groups", "Sample groups overlap in raster pixels. Merge correlated features using groupField or remove overlaps.")
            owner[indexes], classes_grid[indexes] = group, code
        feature_records.append({"featureId": feature_id, "group": group_value, "classCode": code, "validPixelCount": int(indexes.size)})
    if len(class_records) < 2:
        fail("insufficient_classes", "Random forest requires at least two labeled classes.")
    labelled = np.flatnonzero(owner >= 0)
    if not labelled.size:
        fail("no_valid_samples", "No labeled pixels overlap valid image pixels inside the region.")
    combinations = owner[labelled].astype("int64") * (len(class_records) + 1) + classes_grid[labelled]
    order = np.argsort(combinations, kind="stable")
    sorted_indexes = labelled[order]
    boundaries = np.flatnonzero(np.diff(combinations[order])) + 1
    partitions = np.split(sorted_indexes, boundaries)
    cap = min(SAMPLES_PER_GROUP_CLASS, MAX_SAMPLES // len(partitions))
    if cap < 1:
        fail("too_many_groups", "Too many class/group combinations for the local sample budget.")
    random = np.random.default_rng(SEED)
    sampled = np.concatenate([random.choice(part, cap, replace=False) if part.size > cap else part for part in partitions])
    for record in class_records:
        group_count = len(np.unique(owner[sampled[classes_grid[sampled] == record["code"]]]))
        record.update(samplePixels=int((classes_grid[sampled] == record["code"]).sum()), groupCount=group_count)
        if group_count < 2:
            fail("insufficient_holdout_groups", f"Class {json.dumps(record['value'], ensure_ascii=False)} needs valid samples in at least two independent features/groups.")
    return sampled, owner, classes_grid, class_records, groups, feature_records


def classify(root: Path, directory: Path, plan: dict[str, Any]) -> dict[str, Any]:
    try:
        import sklearn
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.metrics import accuracy_score, balanced_accuracy_score, classification_report, confusion_matrix
        from sklearn.model_selection import GroupShuffleSplit
        from scipy.ndimage import binary_dilation
    except ImportError as exc:
        fail("runtime_missing", f"Local random forest requires scikit-learn and scipy in the Earth GIS runtime: {exc}")
    cube, valid, grid = read_region(root, plan)
    sampled, owner, labels, class_mapping, groups, feature_records = labeled_pixels(root, plan, valid, grid)
    x = cube.reshape(cube.shape[0], -1)[:, sampled].T
    y, group_ids = labels[sampled], owner[sampled]
    codes = [record["code"] for record in class_mapping]
    unique_groups = np.unique(group_ids)
    # One pixel exclusion around every held-out group's footprint supplements
    # the feature/group split; individual pixels never choose their own split.
    test_groups_count = min(len(unique_groups) - 1, max(len(codes), math.ceil(len(unique_groups) * 0.25)))
    splitter = GroupShuffleSplit(n_splits=64, test_size=test_groups_count, random_state=SEED)
    selected_split = None
    for train, test in splitter.split(x, y, group_ids):
        test_groups = np.unique(group_ids[test])
        heldout_mask = np.isin(owner, test_groups).reshape(valid.shape)
        exclusion = binary_dilation(heldout_mask, structure=np.ones((3, 3), dtype=bool), iterations=1).ravel()
        train = train[~exclusion[sampled[train]]]
        if set(np.unique(y[train])) == set(codes) and set(np.unique(y[test])) == set(codes):
            selected_split = train, test
            break
    if selected_split is None:
        fail("insufficient_holdout_groups", "No group split retains every class in training and held-out validation after the one-pixel exclusion buffer. Add independent labeled groups.")
    train, test = selected_split
    model = RandomForestClassifier(n_estimators=100, max_depth=24, class_weight="balanced_subsample", random_state=SEED, n_jobs=1)
    model.fit(x[train], y[train])
    predicted = model.predict(x[test])
    train_groups = set(group_ids[train].tolist())
    test_groups = set(group_ids[test].tolist())
    if train_groups & test_groups:
        fail("split_leakage", "A feature/group appeared in both training and validation.")
    metrics = {"evaluation": "held-out feature/group validation", "accuracy": float(accuracy_score(y[test], predicted)), "balancedAccuracy": float(balanced_accuracy_score(y[test], predicted)), "confusionMatrix": confusion_matrix(y[test], predicted, labels=codes).tolist(), "classCodes": codes, "perClass": classification_report(y[test], predicted, labels=codes, output_dict=True, zero_division=0), "trainPixelCount": int(len(train)), "validationPixelCount": int(len(test)), "trainGroupCount": len(train_groups), "validationGroupCount": len(test_groups), "validationBufferPixels": 1, "seed": SEED, "nEstimators": 100, "maxDepth": 24, "sklearnVersion": sklearn.__version__, "limits": "Feature/group holdout with a one-pixel training exclusion is not an independent geographic or temporal validation; labels and class definitions remain user-supplied.", "grid": grid_receipt(grid)}
    classification = np.zeros(valid.size, dtype="uint16")
    valid_indexes = np.flatnonzero(valid.ravel())
    flattened = cube.reshape(cube.shape[0], -1)
    for offset in range(0, valid_indexes.size, 65_536):
        indexes = valid_indexes[offset:offset + 65_536]
        classification[indexes] = model.predict(flattened[:, indexes].T)
    directory.mkdir(parents=True, exist_ok=True)
    write_raster(directory / "classification.tif", classification.reshape(valid.shape), grid, 0, {"PAW_CLASS_MAPPING": json.dumps(class_mapping, ensure_ascii=False), "PAW_WORKFLOW": "random_forest"})
    write_json(directory / "class-mapping.json", {"nodataCode": 0, "classes": class_mapping})
    write_json(directory / "metrics.json", metrics)
    split = {"unit": "groupField" if plan.get("groupField") else "feature", "groupField": plan.get("groupField"), "training": [groups[index] for index in sorted(train_groups)], "validation": [groups[index] for index in sorted(test_groups)], "features": feature_records, "validationBufferPixels": 1, "sampleCapPerGroupClass": SAMPLES_PER_GROUP_CLASS, "maximumSamples": MAX_SAMPLES}
    write_json(directory / "sample-split.json", split)
    return {"status": "completed", "execution": "local", "kind": "classification", "metrics": metrics, "classMapping": class_mapping, "preview": raster_preview(root,directory,directory / "classification.tif",True), "outputs": outputs(root, directory)}


def main() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(2_000_001)
    if len(raw) > 2_000_000:
        fail("request_too_large", "Remote-sensing request exceeds 2 MB.")
    request = json.loads(raw)
    root = Path(request["root"]).resolve()
    directory = inside(root, request["runDir"]) / "outputs"
    plan = request.get("plan")
    if not isinstance(plan, dict) or plan.get("kind") not in {"classification", "ndvi"}:
        fail("workflow_preparation_only", "This runner executes local classification and NDVI only.")
    imports()
    return classify(root, directory, plan) if plan["kind"] == "classification" else ndvi(root, directory, plan)


if __name__ == "__main__":
    try:
        print(json.dumps(main(), ensure_ascii=False, allow_nan=False))
    except Exception as exc:
        print(json.dumps({"status": "failed", "code": exc.code if isinstance(exc, WorkflowError) else "execution_failed", "error": str(exc), "outputs": []}, ensure_ascii=False))
        sys.exit(1)
