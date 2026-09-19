import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import test from 'node:test';
import { exportGISLayer } from './gis-operations.mjs';
import { saveProjectLayer } from './gis-project.mjs';

const managedPython = path.join(os.homedir(), 'Library', 'Application Support', 'RagIme', 'EarthGISRuntime', '.venv', 'bin', 'python');
const python = process.env.PAW_EARTH_GIS_PYTHON || (fs.existsSync(managedPython) ? managedPython : 'python3');
const hasGISRuntime = spawnSync(python, ['-c', 'import geopandas, rasterio, fiona'], { stdio: 'ignore' }).status === 0;
const integration = { skip: !hasGISRuntime && !process.env.PAW_EARTH_GIS_PYTHON ? 'set PAW_EARTH_GIS_PYTHON to run GIS export readback tests' : false };

function fixture(features) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'paw-gis-export-'));
  fs.mkdirSync(path.join(root, 'data'));
  const input = 'data/parcels.geojson';
  const file = path.join(root, input);
  fs.writeFileSync(file, JSON.stringify({ type: 'FeatureCollection', features }));
  return { root, input, file };
}

function parcel(id, index = 0, properties = {}) {
  const west = 120 + (index % 10) * 0.001;
  const south = 30 + Math.floor(index / 10) * 0.001;
  return {
    type: 'Feature',
    ...(id === undefined ? {} : { id }),
    properties: { id: `business-${index + 1}`, name: `地块 ${index + 1}`, ...properties },
    geometry: { type: 'Polygon', coordinates: [[[west, south], [west + 0.0005, south], [west + 0.0005, south + 0.0005], [west, south + 0.0005], [west, south]]] },
  };
}

function digest(file) { return createHash('sha256').update(fs.readFileSync(file)).digest('hex'); }

function pythonJSON(code, ...args) {
  const result = spawnSync(python, ['-c', code, ...args], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr || result.stdout);
  return JSON.parse(result.stdout);
}

function readGpkg(root, receipt) {
  const output = receipt.outputs.find(item => item.name.endsWith('.gpkg'));
  assert.ok(output, JSON.stringify(receipt));
  return pythonJSON(`import json, sys, geopandas as gpd, fiona
file, field, json_field = sys.argv[1:]
frame = gpd.read_file(file)
with fiona.open(file) as source:
    metadata = source.tags()
print(json.dumps({"count": len(frame), "ids": [json.loads(value) for value in frame[json_field]], "displayIds": frame[field].tolist(), "businessIds": frame["id"].tolist(), "columns": frame.columns.tolist(), "metadata": metadata}, ensure_ascii=False))`, path.join(root, output.path), receipt.identityField, receipt.identityJsonField);
}

test('100 persisted parcels export exactly the two selected canonical IDs to GPKG without changing source', integration, async () => {
  const { root, input, file } = fixture(Array.from({ length: 100 }, (_, index) => parcel(`parcel-${index + 1}`, index)));
  try {
    const sourceHash = digest(file);
    const sourceMtime = fs.statSync(file).mtimeMs;
    const selected = ['parcel-7', 'parcel-92'];
    const receipt = await exportGISLayer({ root, python, request: { input, format: 'gpkg', name: '选中的地块', scope: 'selected', featureIds: selected, layerId: 'project:parcels', revision: 4 } });
    assert.equal(receipt.status, 'completed', JSON.stringify(receipt));
    assert.equal(receipt.scope, 'selected');
    assert.equal(receipt.featureCount, 2);
    assert.equal(receipt.exportCount, 2);
    assert.deepEqual(receipt.selectedFeatureIds, selected);
    assert.deepEqual(receipt.exportedFeatureIds, selected);
    assert.equal(receipt.layerId, 'project:parcels');
    assert.equal(receipt.revision, 4);
    assert.equal(receipt.displayName, '选中的地块');
    assert.match(receipt.outputs.find(item => item.name.endsWith('.gpkg')).name, /^[A-Za-z][A-Za-z0-9_-]*\.gpkg$/);
    const actual = readGpkg(root, receipt);
    assert.equal(actual.count, 2);
    assert.deepEqual(actual.ids, selected);
    assert.deepEqual(actual.displayIds, selected);
    assert.deepEqual(actual.businessIds, ['business-7', 'business-92']);
    assert.equal(actual.metadata.PAW_FEATURE_ID_JSON_FIELD, receipt.identityJsonField);
    assert.deepEqual(receipt.outputs.find(item => item.name.endsWith('.gpkg')).geojson.features.map(feature => feature.id), selected);
    const saved = JSON.parse(fs.readFileSync(path.join(root, '.earth/gis/runs', receipt.runId, 'export.json'), 'utf8'));
    assert.deepEqual(saved.selectedFeatureIds, selected);
    assert.equal(saved.revision, 4);
    assert.equal(digest(file), sourceHash);
    assert.equal(fs.statSync(file).mtimeMs, sourceMtime);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('default all export remains compatible and GeoJSON keeps top-level IDs separate from business IDs', integration, async () => {
  const features = Array.from({ length: 100 }, (_, index) => parcel(`parcel-${index + 1}`, index));
  const { root, input } = fixture(features);
  try {
    const receipt = await exportGISLayer({ root, python, request: { input, format: 'geojson', name: 'parcels' } });
    assert.equal(receipt.status, 'completed', JSON.stringify(receipt));
    assert.equal(receipt.scope, 'all');
    assert.equal(receipt.featureCount, 100);
    assert.deepEqual(receipt.selectedFeatureIds, []);
    const output = JSON.parse(fs.readFileSync(path.join(root, receipt.outputs.find(item => item.name.endsWith('.geojson')).path), 'utf8'));
    assert.deepEqual(output.features, features);
    assert.deepEqual(receipt.outputs.find(item => item.name.endsWith('.geojson')).geojson.features.map(feature => feature.id), features.map(feature => feature.id));
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('numeric and string IDs remain distinct while properties.id is the fallback only when top-level id is absent', integration, async () => {
  const features = [parcel(7, 0), parcel('7', 1), parcel(undefined, 2, { id: 'fallback' })];
  const { root, input } = fixture(features);
  try {
    const receipt = await exportGISLayer({ root, python, request: { input, format: 'gpkg', name: 'typed_ids', scope: 'selected', featureIds: [7, '7', 'fallback'] } });
    assert.equal(receipt.status, 'completed', JSON.stringify(receipt));
    assert.deepEqual(readGpkg(root, receipt).ids, [7, '7', 'fallback']);
    const selected = await exportGISLayer({ root, python, request: { input, format: 'geojson', name: 'numeric_only', scope: 'selected', featureIds: [7] } });
    assert.equal(selected.status, 'completed', JSON.stringify(selected));
    const actual = JSON.parse(fs.readFileSync(path.join(root, selected.outputs[0].path), 'utf8'));
    assert.deepEqual(actual.features, [features[0]]);
    const businessId = await exportGISLayer({ root, python, request: { input, format: 'gpkg', name: 'business_id', scope: 'selected', featureIds: ['business-1'] } });
    assert.equal(businessId.status, 'failed');
    assert.equal(businessId.code, 'selected_feature_missing');
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('invalid or ambiguous selections fail without a vector output or source changes', integration, async () => {
  const { root, input, file } = fixture([parcel('duplicate', 0), parcel('duplicate', 1), parcel('unique', 2)]);
  try {
    const original = digest(file);
    const cases = [
      [{ scope: 'selected' }, 'selected_features_required'],
      [{ scope: 'selected', featureIds: [] }, 'selected_features_required'],
      [{ scope: 'selected', featureIds: ['absent'] }, 'selected_feature_missing'],
      [{ scope: 'selected', featureIds: ['duplicate'] }, 'ambiguous_feature_id'],
      [{ scope: 'selected', featureIds: ['unique', 'unique'] }, 'duplicate_feature_id'],
      [{ scope: 'selected', featureIds: [''] }, 'invalid_feature_id'],
      [{ scope: 'selected', featureIds: [true] }, 'invalid_feature_id'],
      [{ scope: 'all', featureIds: ['unique'] }, 'invalid_selection_scope'],
      [{ scope: 'selection', featureIds: ['unique'] }, 'invalid_selection_scope'],
    ];
    for (const [selection, code] of cases) {
      const receipt = await exportGISLayer({ root, python, request: { input, format: 'gpkg', name: 'invalid', ...selection } });
      assert.equal(receipt.status, 'failed', JSON.stringify(receipt));
      assert.equal(receipt.code, code, JSON.stringify(receipt));
      assert.equal(receipt.outputs?.length ?? 0, 0);
      assert.equal(fs.existsSync(path.join(root, '.earth/gis/runs', receipt.runId, 'pred_results')), false);
    }
    assert.equal(digest(file), original);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('GPKG identity fields avoid business-field collisions and support exact re-export', integration, async () => {
  const { root, input } = fixture([parcel('canonical-a', 0, { paw_feature_id: 'business-a', paw_feature_id_json: 'do not overwrite' }), parcel('canonical-b', 1, { paw_feature_id: 'business-b', paw_feature_id_json: 'also preserve' })]);
  try {
    const first = await exportGISLayer({ root, python, request: { input, format: 'gpkg', name: 'parcels', layer: '地块' } });
    assert.equal(first.status, 'completed', JSON.stringify(first));
    assert.notEqual(first.identityField, 'paw_feature_id');
    assert.notEqual(first.identityJsonField, 'paw_feature_id_json');
    assert.deepEqual(readGpkg(root, first).ids, ['canonical-a', 'canonical-b']);
    const source = first.outputs.find(item => item.name.endsWith('.gpkg')).path;
    const hash = digest(path.join(root, source));
    const second = await exportGISLayer({ root, python, request: { input: source, sourceLayer: '地块', format: 'gpkg', name: 'selected_again', scope: 'selected', featureIds: ['canonical-b'] } });
    assert.equal(second.status, 'completed', JSON.stringify(second));
    assert.deepEqual(readGpkg(root, second).ids, ['canonical-b']);
    const business = pythonJSON('import json, sys, geopandas as gpd; frame = gpd.read_file(sys.argv[1]); print(json.dumps(frame[["paw_feature_id", "paw_feature_id_json"]].to_dict("records")))', path.join(root, second.outputs.find(item => item.name.endsWith('.gpkg')).path));
    assert.deepEqual(business, [{ paw_feature_id: 'business-b', paw_feature_id_json: 'also preserve' }]);
    assert.equal(digest(path.join(root, source)), hash);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('multi-layer GPKG requires an exact input layer before selected export', integration, async () => {
  const { root } = fixture([]);
  try {
    const input = 'data/multiple.gpkg';
    pythonJSON(`import json, sys, geopandas as gpd
from shapely.geometry import Point
for name, value in [("west", "west-id"), ("east", "east-id")]:
    gpd.GeoDataFrame({"id": [value]}, geometry=[Point(120, 30)], crs="EPSG:4326").to_file(sys.argv[1], layer=name, driver="GPKG")
print(json.dumps({"created": True}))`, path.join(root, input));
    const ambiguous = await exportGISLayer({ root, python, request: { input, format: 'gpkg', name: 'ambiguous', scope: 'selected', featureIds: ['east-id'] } });
    assert.equal(ambiguous.status, 'failed');
    assert.equal(ambiguous.code, 'source_layer_required');
    const exact = await exportGISLayer({ root, python, request: { input, sourceLayer: 'east', format: 'gpkg', name: 'east', scope: 'selected', featureIds: ['east-id'] } });
    assert.equal(exact.status, 'completed', JSON.stringify(exact));
    assert.deepEqual(readGpkg(root, exact).ids, ['east-id']);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('catalog-bound export rejects stale or unrelated versions and accepts explicit immutable history', integration, async () => {
  const { root, input } = fixture([parcel('external')]);
  try {
    const first = saveProjectLayer({ root, name: '项目地块', features: [parcel('parcel-1')] }).layer;
    const second = saveProjectLayer({ root, layerId: first.id, expectedRevision: first.revision, name: first.name, features: [parcel('parcel-2')] }).layer;
    for (const request of [
      { input: second.path, layerId: second.id, revision: first.revision },
      { input, layerId: second.id, revision: second.revision },
      { input: second.path, layerId: second.id },
    ]) {
      const result = await exportGISLayer({ root, python, request: { ...request, format: 'geojson', name: 'stale' } });
      assert.equal(result.status, 'failed');
      assert.equal(result.code, 'stale_layer_revision');
    }
    const missing = await exportGISLayer({ root, python, request: { input, layerId: 'missing', revision: 1, format: 'geojson', name: 'missing' } });
    assert.equal(missing.code, 'missing_project_layer');
    for (const [layer, binding] of [[first, 'catalog-history'], [second, 'catalog-current']]) {
      const result = await exportGISLayer({ root, python, request: { input: layer.path, layerId: layer.id, revision: layer.revision, format: 'geojson', name: 'bound' } });
      assert.equal(result.status, 'completed', JSON.stringify(result));
      assert.equal(result.layerVersionBinding, binding);
      assert.deepEqual(result.exportedFeatureIds, layer.features.map(feature => feature.id));
    }
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
