import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import test from 'node:test';
import { createGISBundle, verifyGISBundle } from './gis-delivery.mjs';

const managed = path.join(os.homedir(), 'Library/Application Support/RagIme/EarthGISRuntime/.venv/bin/python');
const python = process.env.PAW_EARTH_GIS_PYTHON || (fs.existsSync(managed) ? managed : 'python3');
const available = spawnSync(python, ['-c', 'import geopandas, rasterio, matplotlib, pyproj'], { stdio: 'ignore' }).status === 0;
const integration = { skip: !available && !process.env.PAW_EARTH_GIS_PYTHON ? 'A GIS Python runtime is required for real map rendering' : false };

function fixture(root, { rasterOnly = false } = {}) {
  const runId = randomUUID();
  const relative = `.earth/gis/runs/${runId}`;
  const directory = path.join(root, relative);
  fs.mkdirSync(directory, { recursive: true });
  fs.writeFileSync(path.join(root, '.earth/gis/runtime.json'), JSON.stringify({ python }));
  const geojson = { type: 'FeatureCollection', features: [0, 1].map(index => ({ type: 'Feature', properties: { id: index }, geometry: { type: 'Polygon', coordinates: [[[120 + index * .01, 30], [120.006 + index * .01, 30], [120.006 + index * .01, 30.006], [120 + index * .01, 30.006], [120 + index * .01, 30]]] } })) };
  fs.writeFileSync(path.join(directory, 'parcels.geojson'), JSON.stringify(geojson));
  const raster = spawnSync(python, ['-c', `import numpy as np, rasterio, sys
from rasterio.transform import from_origin
with rasterio.open(sys.argv[1], 'w', driver='GTiff', width=8, height=8, count=1, dtype='float32', crs='EPSG:4326', transform=from_origin(119.99,30.02,.005,.005), nodata=-9999) as output:
 data=np.arange(64,dtype='float32').reshape((8,8)); data[0,0]=-9999; output.write(data,1)
`, path.join(directory, 'terrain.tif')], { encoding: 'utf8' });
  assert.equal(raster.status, 0, raster.stderr);
  const vectorOutput = { path: `${relative}/parcels.geojson`, relativePath: 'parcels.geojson', kind: 'vector', name: 'Parcels', geojson: { ...geojson, features: geojson.features.slice(0, 1) }, summary: { crs: 'EPSG:4326', featureCount: 2 } };
  const rasterOutput = { path: `${relative}/terrain.tif`, relativePath: 'terrain.tif', kind: 'raster', name: 'Terrain', summary: { crs: 'EPSG:4326' } };
  const run = { runId, status: 'completed', op: 'overlay', params: {}, inputVersions: [], startedAt: '2026-09-20T00:00:00Z', outputs: rasterOnly ? [rasterOutput] : [vectorOutput, rasterOutput] };
  fs.writeFileSync(path.join(directory, 'run.json'), JSON.stringify(run));
  return { runId, geojson };
}

test('mixed raster and vector delivery shares a CRS and exports a full cartographic page', integration, () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'paw-cartography-'));
  try {
    const { runId, geojson } = fixture(root);
    const delivery = createGISBundle({ root, runId, mapOptions: { title: '选址方案 / Site suitability', subtitle: 'Parcels over terrain', paperSize: 'A3', orientation: 'portrait', crs: 'EPSG:32651' } });
    const directory = path.join(root, delivery.path);
    const quality = JSON.parse(fs.readFileSync(path.join(directory, 'quality.json')));
    assert.equal(quality.checks.length, 2, 'a vector-first output list must not omit its raster');
    assert.ok(quality.checks.every(layer => layer.mapCrs === 'EPSG:32651'));
    const layout = quality.cartography;
    assert.equal(layout.title, '选址方案 / Site suitability');
    assert.deepEqual([layout.paperSize, layout.orientation, layout.widthMm, layout.heightMm], ['A3', 'portrait', 297, 420]);
    assert.deepEqual(layout.legendEntries.map(item => item.label), ['Parcels', 'Terrain']);
    assert.ok(layout.scaleBar.lengthMetres > 0);
    assert.equal(layout.scaleBar.method, 'geodesic at map center');
    assert.ok(Number.isFinite(layout.northArrow.angleDegrees));
    assert.ok(layout.extent.every(Number.isFinite));
    assert.equal(delivery.manifest.cartography.crs, 'EPSG:32651');
    assert.equal(delivery.manifest.preview, 'map.png');
    assert.ok(fs.statSync(path.join(directory, 'map.png')).size > 1000);
    assert.equal(fs.readFileSync(path.join(directory, 'map.pdf')).subarray(0, 5).toString(), '%PDF-');
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(directory, 'map.geojson'))).features, geojson.features);
    assert.equal(verifyGISBundle({ root, path: delivery.path }).runId, runId);
    assert.throws(() => createGISBundle({ root, runId, mapOptions: { paperSize: 'bad' } }), /paperSize/);
    const undecorated = createGISBundle({ root, runId, mapOptions: { legend: false, scaleBar: false, northArrow: false } });
    assert.deepEqual(undecorated.manifest.cartography.legendEntries, []);
    assert.equal(undecorated.manifest.cartography.scaleBar, null);
    assert.equal(undecorated.manifest.cartography.northArrow, null);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('geographic map coordinates still produce a metre-based scale', integration, () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'paw-geographic-map-'));
  try {
    const { runId } = fixture(root, { rasterOnly: true });
    const delivery = createGISBundle({ root, runId, mapOptions: { crs: 'EPSG:4326' } });
    const { scaleBar } = delivery.manifest.cartography;
    assert.equal(scaleBar.lengthMetres, 1000);
    assert.ok(scaleBar.mapUnits > .010 && scaleBar.mapUnits < .011, '1000 metres near 30 degrees north is about 0.01036 longitude degrees');
    assert.equal(scaleBar.label, '1 km');
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('raster-only maps get a metric scale and portable report links without a nonexistent GeoPackage', integration, () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'paw-raster-map-'));
  try {
    const { runId } = fixture(root, { rasterOnly: true });
    const delivery = createGISBundle({ root, runId, mapOptions: { paperSize: 'Letter', orientation: 'landscape' } });
    const directory = path.join(root, delivery.path);
    const quality = JSON.parse(fs.readFileSync(path.join(directory, 'quality.json')));
    assert.ok(quality.cartography.scaleBar.lengthMetres > 0);
    assert.ok(quality.cartography.crs.startsWith('EPSG:326'));
    assert.deepEqual([quality.cartography.widthMm, quality.cartography.heightMm], [279.4, 215.9]);
    const report = fs.readFileSync(path.join(directory, 'report.html'), 'utf8');
    assert.ok(report.includes('map.png'));
    assert.equal(report.includes('href="result.gpkg"'), false);
    assert.ok(report.includes('run/terrain.tif'));
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
