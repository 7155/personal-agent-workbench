import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import test from 'node:test';
import { connectSpatialSource, listSpatialSources, loadSpatialLayer, queryGISPixel, queryGISRegion, runGISOperation } from './gis-operations.mjs';

const python = process.env.PAW_EARTH_GIS_PYTHON || 'python3';
const hasGISRuntime = spawnSync(python, ['-c', 'import geopandas, rasterio, fiona, shapely'], { stdio: 'ignore' }).status === 0;
const runtime = { skip: !hasGISRuntime && !process.env.PAW_EARTH_GIS_PYTHON ? 'set PAW_EARTH_GIS_PYTHON to run real spatial acceptance' : false };

function pythonJSON(root, script) {
  const result = spawnSync(python, ['-c', script, root], { encoding: 'utf8', maxBuffer: 4 * 1024 * 1024 });
  assert.equal(result.status, 0, result.stderr || result.stdout);
  return JSON.parse(result.stdout.trim().split(/\r?\n/).at(-1));
}

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'paw-earth-spatial-'));
  try {
    pythonJSON(root, `
import json, sys
from pathlib import Path
import geopandas as gpd
import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Point, box
root = Path(sys.argv[1]); data = root / 'data'; data.mkdir()
database = data / 'project.gpkg'
gpd.GeoDataFrame({'name': ['wrong layer']}, geometry=[Point(0, 0)], crs='EPSG:3857').to_file(database, layer='first_layer', driver='GPKG')
gpd.GeoDataFrame({'name': ['目标要素']}, geometry=[Point(111319.49079327357, 0)], crs='EPSG:3857').to_file(database, layer='目标图层', driver='GPKG')
gpd.GeoDataFrame({'name': ['numeric', 'string', 'missing'], 'canonical_json': ['7', '\\"7\\"', 'null'], 'canonical_text': ['7', '7', None], 'paw_feature_id': ['business-a', 'business-b', 'business-c']}, geometry=[Point(1, 1), Point(2, 2), Point(3, 3)], crs='EPSG:4326').to_file(database, layer='typed_ids', driver='GPKG', metadata={'PAW_FEATURE_ID_JSON_FIELD': 'canonical_json', 'PAW_FEATURE_ID_FIELD': 'canonical_text'})
gpd.GeoDataFrame({'name': ['unknown']}, geometry=[Point(1, 1)]).to_file(data / 'unknown.gpkg', layer='unknown', driver='GPKG')
values = np.array([[1, 2, 3, 4], [5, -9999, 7, 8], [9, 10, 11, 12], [13, 14, 15, np.nan]], dtype='float64')
for name, crs in [('known.tif', 'EPSG:4326'), ('unknown.tif', None)]:
    with rasterio.open(data / name, 'w', driver='GTiff', width=4, height=4, count=1, dtype='float64', transform=from_origin(0, 4, 1, 1), crs=crs, nodata=-9999) as dest:
        dest.write(values, 1)
for name, geom in [('parcel', box(500000, 0, 501000, 1000)), ('lake', box(500400, 400, 500600, 600))]:
    gpd.GeoDataFrame({'id': [name]}, geometry=[geom], crs='EPSG:32631').to_file(data / (name + '.geojson'), driver='GeoJSON')
print(json.dumps({'created': True}))
`);
    return root;
  } catch (error) {
    fs.rmSync(root, { recursive: true, force: true });
    throw error;
  }
}

test('reads the exact named GeoPackage layer, persists WGS84 and lineage, and keeps Chinese source identity across renames', runtime, async () => {
  const root = fixture();
  try {
    const connected = await connectSpatialSource({ root, python, source: { name: '项目数据库', kind: 'geopackage', path: 'data/project.gpkg' } });
    assert.deepEqual(connected.layers, ['first_layer', '目标图层', 'typed_ids']);
    const renamed = await connectSpatialSource({ root, python, source: { name: '改名后的数据库', kind: 'geopackage', path: 'data/./project.gpkg' } });
    assert.equal(renamed.id, connected.id);
    assert.equal(listSpatialSources({ root }).sources.length, 1);

    const loaded = await loadSpatialLayer({ root, python, sourceId: renamed.id, layer: '目标图层' });
    assert.equal(loaded.status, 'completed');
    assert.equal(loaded.featureCount, 1);
    assert.equal(loaded.crs, 'EPSG:4326');
    assert.equal(loaded.sourceCrs, 'EPSG:3857');
    assert.equal(loaded.sourceId, connected.id);
    const persisted = JSON.parse(fs.readFileSync(path.join(root, loaded.path), 'utf8'));
    assert.equal(persisted.features[0].properties.name, '目标要素');
    assert.ok(Math.abs(persisted.features[0].geometry.coordinates[0] - 1) < 1e-9);
    assert.ok(Math.abs(persisted.features[0].geometry.coordinates[1]) < 1e-9);
    assert.equal(persisted.sourceId, connected.id);
    assert.equal(persisted.sourceLineage.layer, '目标图层');
    assert.equal(persisted.sourceLineage.sourcePath, 'data/project.gpkg');
    assert.match(persisted.sourceLineage.sourceSha256, /^[a-f0-9]{64}$/);
    const lineage = JSON.parse(fs.readFileSync(path.join(root, loaded.lineagePath), 'utf8'));
    assert.equal(lineage.sourceId, connected.id);
    assert.equal(lineage.sourceCrs, 'EPSG:3857');
    await assert.rejects(loadSpatialLayer({ root, python, sourceId: connected.id, layer: 'missing' }), error => error.code === 'layer_not_found');

    const typed = await loadSpatialLayer({ root, python, sourceId: connected.id, layer: 'typed_ids' });
    assert.deepEqual(typed.geojson.features.map(feature => feature.id), [7, '7', undefined]);
    assert.equal(typed.geojson.features[0].properties.paw_feature_id, 'business-a');
    assert.equal('canonical_json' in typed.geojson.features[0].properties, false);
    assert.equal('canonical_text' in typed.geojson.features[0].properties, false);

    fs.copyFileSync(path.join(root, 'data/project.gpkg'), path.join(root, 'data/other.gpkg'));
    const sameDisplayName = await connectSpatialSource({ root, python, source: { name: '改名后的数据库', kind: 'geopackage', path: 'data/other.gpkg' } });
    assert.notEqual(sameDisplayName.id, connected.id);
    assert.equal(listSpatialSources({ root }).sources.length, 2);

    const unknown = await connectSpatialSource({ root, python, source: { name: '未知坐标系', kind: 'geopackage', path: 'data/unknown.gpkg' } });
    await assert.rejects(loadSpatialLayer({ root, python, sourceId: unknown.id, layer: 'unknown' }), error => error.code === 'unknown_crs');
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('queries real raster cells and polygon windows with finite values, nodata and coverage', runtime, async () => {
  const root = fixture();
  try {
    const pixel = await queryGISPixel({ root, python, path: 'data/known.tif', longitude: 0.5, latitude: 3.5 });
    assert.equal(pixel.value, 1);
    assert.equal(pixel.nodata, false);
    assert.deepEqual([pixel.row, pixel.column], [0, 0]);
    const nodata = await queryGISPixel({ root, python, path: 'data/known.tif', longitude: 1.5, latitude: 2.5 });
    assert.equal(nodata.value, null);
    assert.equal(nodata.nodata, true);
    const nonfinite = await queryGISPixel({ root, python, path: 'data/known.tif', longitude: 3.5, latitude: 0.5 });
    assert.equal(nonfinite.value, null);
    assert.equal(nonfinite.nodata, true);
    assert.equal((await queryGISPixel({ root, python, path: 'data/known.tif', longitude: 8, latitude: 8 })).status, 'outside');
    for (const longitude of [NaN, Infinity, -Infinity, 181]) {
      await assert.rejects(queryGISPixel({ root, python, path: 'data/known.tif', longitude, latitude: 0 }), /finite|longitude|latitude/i);
    }
    await assert.rejects(queryGISPixel({ root, python, path: 'data/unknown.tif', longitude: 1, latitude: 1 }), error => error.code === 'unknown_crs');
    await assert.rejects(queryGISPixel({ root, python, path: 'data/known.tif', longitude: 1, latitude: 1, band: 0 }), /band/i);

    // L-shaped polygon excludes values 7 and 11 that its bounding box contains.
    const geometry = { type: 'Polygon', coordinates: [[[0, 4], [3, 4], [3, 3], [2, 3], [2, 1], [0, 1], [0, 4]]] };
    const region = await queryGISRegion({ root, python, path: 'data/known.tif', geometry });
    assert.equal(region.status, 'completed');
    assert.deepEqual(region.window, { rowOffset: 0, columnOffset: 0, width: 3, height: 3 });
    assert.equal(region.totalPixels, 7);
    assert.equal(region.validPixels, 6);
    assert.equal(region.nodataPixels, 1);
    assert.equal(region.outsidePixels, 0);
    assert.equal(region.validPixelCoverage, 6 / 7);
    assert.deepEqual({ ...region.stats, stddev: undefined }, { min: 1, max: 10, sum: 30, mean: 5, stddev: undefined });
    assert.ok(Math.abs(region.stats.stddev - Math.sqrt(70 / 6)) < 1e-9);

    const withHole = { type: 'Polygon', coordinates: [[[0, 4], [3, 4], [3, 1], [0, 1], [0, 4]], [[1, 3], [2, 3], [2, 2], [1, 2], [1, 3]]] };
    const hole = await queryGISRegion({ root, python, path: 'data/known.tif', geometry: withHole });
    assert.equal(hole.totalPixels, 8);
    assert.equal(hole.validPixels, 8);
    assert.equal(hole.nodataPixels, 0);
    assert.equal(hole.stats.mean, 6);

    const partial = await queryGISRegion({ root, python, path: 'data/known.tif', geometry: { type: 'Polygon', coordinates: [[[-1, 4], [1, 4], [1, 3], [-1, 3], [-1, 4]]] } });
    assert.equal(partial.totalPixels, 2);
    assert.equal(partial.validPixels, 1);
    assert.equal(partial.outsidePixels, 1);
    assert.equal(partial.validPixelCoverage, 0.5);
    const empty = await queryGISRegion({ root, python, path: 'data/known.tif', geometry: { type: 'Polygon', coordinates: [[[1, 3], [2, 3], [2, 2], [1, 2], [1, 3]]] } });
    assert.equal(empty.status, 'completed');
    assert.equal(empty.totalPixels, 1);
    assert.equal(empty.validPixels, 0);
    assert.equal(empty.nodataPixels, 1);
    assert.equal(empty.validPixelCoverage, 0);
    assert.deepEqual(empty.stats, { min: null, max: null, mean: null, sum: null, stddev: null });
    const outside = await queryGISRegion({ root, python, path: 'data/known.tif', geometry: { type: 'Polygon', coordinates: [[[8, 9], [9, 9], [9, 8], [8, 8], [8, 9]]] } });
    assert.equal(outside.status, 'outside');
    assert.equal(outside.outsidePixels, 1);
    assert.equal(outside.nodataPixels, 0);
    assert.equal(outside.validPixelCoverage, 0);
    await assert.rejects(queryGISRegion({ root, python, path: 'data/unknown.tif', geometry }), error => error.code === 'unknown_crs');
    await assert.rejects(queryGISRegion({ root, python, path: 'data/known.tif', geometry: { type: 'Point', coordinates: [1, 1] } }), /polygon/i);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('subtracts an internal lake and persists valid geometry with the expected metric area', runtime, async () => {
  const root = fixture();
  try {
    const result = await runGISOperation({ root, python, request: { op: 'difference', inputs: { layer: 'data/parcel.geojson', overlay: 'data/lake.geojson' }, output: 'parcel_without_lake', saveAs: 'pred_results/parcel_without_lake.geojson' } });
    assert.equal(result.status, 'completed', result.error);
    const output = result.outputs.find(item => item.name === 'parcel_without_lake.geojson');
    assert.ok(output);
    const check = pythonJSON(root, `
import json, sys
from pathlib import Path
import geopandas as gpd
root = Path(sys.argv[1])
result = gpd.read_file(root / ${JSON.stringify(output.path)}).to_crs('EPSG:32631')
lake = gpd.read_file(root / 'data/lake.geojson').to_crs('EPSG:32631')
geometry = result.geometry.iloc[0]
print(json.dumps({'valid': bool(geometry.is_valid), 'holes': len(geometry.interiors), 'areaM2': float(geometry.area), 'lakeOverlapM2': float(geometry.intersection(lake.geometry.iloc[0]).area), 'id': result.iloc[0]['id']}))
`);
    assert.equal(check.valid, true);
    assert.equal(check.holes, 1);
    assert.equal(check.id, 'parcel');
    assert.ok(Math.abs(check.areaM2 - 960_000) < 0.01, JSON.stringify(check));
    assert.ok(check.lakeOverlapM2 < 0.01);
    assert.ok(Math.abs(output.summary.areaM2 - 960_000) < 0.1);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
