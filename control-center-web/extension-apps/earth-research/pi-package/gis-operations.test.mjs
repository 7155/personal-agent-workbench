import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import test from 'node:test';
import { GIS_CATALOG, connectSpatialSource, exportGISLayer, inspectGISPath, listGISFiles, prepareGISWorkspace, runGISOperation } from './gis-operations.mjs';

const python = process.env.PAW_EARTH_GIS_PYTHON || 'python3';
const hasGISRuntime = spawnSync(python, ['-c', 'import geopandas, rasterio'], { stdio: 'ignore' }).status === 0;

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
