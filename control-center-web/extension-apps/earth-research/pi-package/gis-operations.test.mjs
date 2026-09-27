import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import test from 'node:test';
import { GIS_CATALOG, connectSpatialSource, exportGISLayer, inspectGISPath, listGISFiles, prepareGISWorkspace, runGISOperation } from './gis-operations.mjs';

const python = process.env.PAW_EARTH_GIS_PYTHON || 'python3';
const hasGISRuntime = spawnSync(python, ['-c', 'import geopandas, rasterio'], { stdio: 'ignore' }).status === 0;

test('lists workspace-owned GIS files without following symlinks', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'paw-earth-gis-list-'));
  const external = fs.mkdtempSync(path.join(os.tmpdir(), 'paw-earth-gis-external-'));
  try {
    const data = path.join(root, 'data');
    fs.mkdirSync(path.join(data, 'nested'), { recursive: true });
    fs.mkdirSync(path.join(external, 'nested'), { recursive: true });
    fs.writeFileSync(path.join(data, 'local.geojson'), '{"local":true}');
    fs.writeFileSync(path.join(data, 'nested', 'inside.gpkg'), 'workspace');
    fs.writeFileSync(path.join(external, 'outside.geojson'), '{"outside":"private metadata"}');
    fs.writeFileSync(path.join(external, 'nested', 'hidden.geojson'), '{"outside":"hidden"}');
    fs.symlinkSync(path.join(external, 'outside.geojson'), path.join(data, 'external.geojson'), 'file');
    fs.symlinkSync(path.join(external, 'nested'), path.join(data, 'external-dir'), 'dir');

    const items = listGISFiles(root);
    assert.deepEqual(items.map(item => item.path), ['data/local.geojson', 'data/nested/inside.gpkg']);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
    fs.rmSync(external, { recursive: true, force: true });
  }
});

test('ignores dangling GIS file symlinks without failing the listing', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'paw-earth-gis-dangling-'));
  try {
    const data = path.join(root, 'data');
    fs.mkdirSync(path.join(data, 'nested'), { recursive: true });
    fs.writeFileSync(path.join(data, 'nested', 'valid.geojson'), '{}');
    fs.symlinkSync(path.join(root, 'missing.geojson'), path.join(data, 'dangling.geojson'), 'file');

    assert.deepEqual(listGISFiles(root).map(item => item.path), ['data/nested/valid.geojson']);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("exposes GISclaw 28 deterministic operations and runs CRS-aware buffer", { skip: !hasGISRuntime && !process.env.PAW_EARTH_GIS_PYTHON ? 'set PAW_EARTH_GIS_PYTHON to run the GeoPandas/Rasterio integration test' : false }, async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'paw-earth-gis-test-'));
  try {
    fs.mkdirSync(path.join(root, 'data'));
    fs.writeFileSync(path.join(root, 'data', 'roads.geojson'), JSON.stringify({
      type: 'FeatureCollection',
      features: [{ type: 'Feature', properties: { id: 'r1' }, geometry: { type: 'LineString', coordinates: [[120, 30], [120.01, 30.01]] } }],
    }));
    assert.equal(GIS_CATALOG.flatMap(group => group.ops).length, 28);
    const prepared = prepareGISWorkspace(root, { python });
    assert.equal(fs.existsSync(prepared.runtime), true);
    assert.deepEqual(listGISFiles(root).map(item => item.kind), ['vector']);
    const inspected = await inspectGISPath({ root, python, path: 'data/roads.geojson' });
    assert.equal(inspected.crs, 'EPSG:4326');
    const result = await runGISOperation({ root, python, request: {
      op: 'buffer', inputs: { layer: 'data/roads.geojson' }, params: { distance: 100 },
      output: 'roads_buffer', saveAs: 'pred_results/roads_buffer.geojson',
    } });
    assert.equal(result.status, 'completed');
    assert.equal(result.outputs?.[0]?.relativePath, 'pred_results/roads_buffer.geojson');
    const output = JSON.parse(fs.readFileSync(path.join(root, '.earth/gis/runs', result.runId, 'pred_results/roads_buffer.geojson'), 'utf8'));
    assert.equal(output.type, 'FeatureCollection');
    assert.equal(output.features[0].geometry.type, 'Polygon');
    assert.equal(JSON.parse(fs.readFileSync(path.join(root, '.earth/gis/workspace.json'), 'utf8')).runId, result.runId);
    const shp = await exportGISLayer({ root, python, request: { input: 'data/roads.geojson', format: 'shp', name: 'roads' } });
    assert.equal(shp.status, 'completed');
    assert.ok(shp.outputs.some(item => item.name === 'roads.zip'));
    assert.ok(shp.outputs.some(item => item.name === 'roads.shp'));
    const gpkg = await exportGISLayer({ root, python, request: { input: 'data/roads.geojson', format: 'gpkg', name: 'roads_db', layer: 'roads' } });
    assert.equal(gpkg.status, 'completed');
    const connected = await connectSpatialSource({ root, python, source: { name: 'project-gpkg', kind: 'geopackage', path: `.earth/gis/runs/${gpkg.runId}/pred_results/roads_db.gpkg` } });
    assert.equal(connected.status, 'ready');
    assert.deepEqual(connected.layers, ['roads']);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("runs the short power-grid siting acceptance task", { skip: !hasGISRuntime && !process.env.PAW_EARTH_GIS_PYTHON ? 'set PAW_EARTH_GIS_PYTHON to run the GeoPandas/Rasterio integration test' : false }, async () => {
  // Plain-language task: find substation sites, stay 200 m from the river, export SHP.
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'paw-earth-gis-acceptance-'));
  try {
    fs.mkdirSync(path.join(root, 'data'));
    const polygon = (id, west) => ({ type: 'Feature', properties: { id }, geometry: { type: 'Polygon', coordinates: [[[west, 30], [west + 0.02, 30], [west + 0.02, 30.02], [west, 30.02], [west, 30]]] } });
    fs.writeFileSync(path.join(root, 'data', 'candidate_parcels.geojson'), JSON.stringify({ type: 'FeatureCollection', features: [polygon('candidate-a', 120), polygon('candidate-b', 120.05)] }));
    fs.writeFileSync(path.join(root, 'data', 'river.geojson'), JSON.stringify({ type: 'FeatureCollection', features: [{ type: 'Feature', properties: { id: 'river-1' }, geometry: { type: 'LineString', coordinates: [[120.01, 29.99], [120.01, 30.03]] } }] }));

    const buffer = await runGISOperation({ root, python, request: {
      op: 'buffer', inputs: { layer: 'data/river.geojson' }, params: { distance: 200, dissolve: true },
      output: 'river_exclusion', saveAs: 'pred_results/river_exclusion.geojson',
    } });
    assert.equal(buffer.status, 'completed');
    const safe = await runGISOperation({ root, python, request: {
      op: 'difference', inputs: { layer: 'data/candidate_parcels.geojson', overlay: `.earth/gis/runs/${buffer.runId}/pred_results/river_exclusion.geojson` }, params: {},
      output: 'safe_sites', saveAs: 'pred_results/safe_sites.geojson',
    } });
    assert.equal(safe.status, 'completed');
    const safeOutput = JSON.parse(fs.readFileSync(path.join(root, '.earth/gis/runs', safe.runId, 'pred_results/safe_sites.geojson'), 'utf8'));
    assert.equal(safeOutput.type, 'FeatureCollection');
    assert.ok(safeOutput.features.length >= 1);
    const inspected = await inspectGISPath({ root, python, path: `.earth/gis/runs/${safe.runId}/pred_results/safe_sites.geojson` });
    assert.equal(inspected.crs, 'EPSG:4326');

    const shp = await exportGISLayer({ root, python, request: { input: `.earth/gis/runs/${safe.runId}/pred_results/safe_sites.geojson`, format: 'shp', name: 'safe_sites' } });
    assert.equal(shp.status, 'completed');
    assert.ok(shp.outputs.some(item => item.name === 'safe_sites.zip'));
    assert.ok(shp.outputs.some(item => item.name === 'safe_sites.shp'));
    const gpkg = await exportGISLayer({ root, python, request: { input: `.earth/gis/runs/${safe.runId}/pred_results/safe_sites.geojson`, format: 'gpkg', name: 'safe_sites', layer: 'safe_sites' } });
    assert.equal(gpkg.status, 'completed');
    const connected = await connectSpatialSource({ root, python, source: { name: 'acceptance-gpkg', kind: 'geopackage', path: `.earth/gis/runs/${gpkg.runId}/pred_results/safe_sites.gpkg` } });
    assert.equal(connected.status, 'ready');
    assert.deepEqual(connected.layers, ['safe_sites']);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
