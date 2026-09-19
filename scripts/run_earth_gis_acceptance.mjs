#!/usr/bin/env node
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { connectSpatialSource, exportGISLayer, inspectGISPath, runGISOperation } from '../control-center-web/extension-apps/earth-research/pi-package/gis-operations.mjs';

const task = '找适合建变电站的地块，避开河流 200 米，并导出 SHP。';
const managedPython = path.join(os.homedir(), 'Library', 'Application Support', 'RagIme', 'EarthGISRuntime', '.venv', 'bin', 'python');
const python = process.env.PAW_EARTH_GIS_PYTHON || (fs.existsSync(managedPython) ? managedPython : 'python3');
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'paw-earth-gis-acceptance-'));

function polygon(id, west) {
  return { type: 'Feature', properties: { id }, geometry: { type: 'Polygon', coordinates: [[[west, 30], [west + 0.02, 30], [west + 0.02, 30.02], [west, 30.02], [west, 30]]] } };
}

try {
  fs.mkdirSync(path.join(root, 'data'));
  fs.writeFileSync(path.join(root, 'data', 'candidate_parcels.geojson'), JSON.stringify({ type: 'FeatureCollection', features: [polygon('candidate-a', 120), polygon('candidate-b', 120.05)] }));
  fs.writeFileSync(path.join(root, 'data', 'river.geojson'), JSON.stringify({ type: 'FeatureCollection', features: [{ type: 'Feature', properties: { id: 'river-1' }, geometry: { type: 'LineString', coordinates: [[120.01, 29.99], [120.01, 30.03]] } }] }));

  const buffer = await runGISOperation({ root, python, request: { op: 'buffer', inputs: { layer: 'data/river.geojson' }, params: { distance: 200, dissolve: true }, output: 'river_exclusion', saveAs: 'pred_results/river_exclusion.geojson' } });
  const safe = await runGISOperation({ root, python, request: { op: 'difference', inputs: { layer: 'data/candidate_parcels.geojson', overlay: `.earth/gis/runs/${buffer.runId}/pred_results/river_exclusion.geojson` }, params: {}, output: 'safe_sites', saveAs: 'pred_results/safe_sites.geojson' } });
  const safePath = `.earth/gis/runs/${safe.runId}/pred_results/safe_sites.geojson`;
  const safeGeoJson = JSON.parse(fs.readFileSync(path.join(root, safePath), 'utf8'));
  const inspected = await inspectGISPath({ root, python, path: safePath });
  const shp = await exportGISLayer({ root, python, request: { input: safePath, format: 'shp', name: 'safe_sites' } });
  const gpkg = await exportGISLayer({ root, python, request: { input: safePath, format: 'gpkg', name: 'safe_sites', layer: 'safe_sites' } });
  const database = await connectSpatialSource({ root, python, source: { name: 'acceptance-gpkg', kind: 'geopackage', path: `.earth/gis/runs/${gpkg.runId}/pred_results/safe_sites.gpkg` } });
  console.log(JSON.stringify({ task, status: safe.status === 'completed' && shp.status === 'completed' && gpkg.status === 'completed' && database.status === 'ready' ? 'completed' : 'failed', safeFeatureCount: safeGeoJson.features.length, crs: inspected.crs, shapefile: shp.outputs.filter(item => /safe_sites\.(zip|shp)$/.test(item.name)).map(item => item.name), geopackage: gpkg.outputs.filter(item => item.name === 'safe_sites.gpkg').map(item => item.name), databaseLayers: database.layers }, null, 2));
} finally {
  if (process.env.PAW_EARTH_GIS_KEEP !== '1') fs.rmSync(root, { recursive: true, force: true });
}
