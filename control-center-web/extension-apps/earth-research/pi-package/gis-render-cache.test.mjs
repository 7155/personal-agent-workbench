import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { createGISBundle, verifyGISBundle } from './gis-delivery.mjs';

const python = process.env.PAW_EARTH_GIS_PYTHON || 'python3';
const available = spawnSync(python, ['-c', 'import geopandas, rasterio, matplotlib'], { stdio: 'ignore' }).status === 0;
const installer = fileURLToPath(new URL('../../../../scripts/install_earth_gis_runtime.sh', import.meta.url));
const quote = value => `'${value.replaceAll("'", "'\\''")}'`;

test('installer prewarms a persistent font cache reused by two real 100-polygon bundles', { skip: !available && !process.env.PAW_EARTH_GIS_PYTHON }, t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'paw-gis-render-cache-'));
  const previousSupport = process.env.RAG_IME_APP_SUPPORT_DIR;
  const previousPath = process.env.PATH;
  try {
    const support = path.join(root, 'app-support');
    const runtime = path.join(support, 'EarthGISRuntime');
    const executable = path.join(runtime, '.venv/bin/python');
    const calls = path.join(root, 'renderer-cache-paths.txt');
    const fontScan = path.join(root, 'unexpected-system-font-scan');
    const bin = path.join(root, 'bin');
    fs.mkdirSync(path.dirname(executable), { recursive: true });
    fs.mkdirSync(bin);
    fs.writeFileSync(executable, `#!/bin/sh\nprintf '%s\\n' "$MPLCONFIGDIR" >> ${quote(calls)}\nexec ${quote(python)} "$@"\n`, { mode: 0o700 });
    fs.writeFileSync(path.join(bin, 'system_profiler'), `#!/bin/sh\nprintf called > ${quote(fontScan)}\nexit 1\n`, { mode: 0o700 });
    fs.writeFileSync(path.join(bin, 'uv'), '#!/bin/sh\nexit 97\n', { mode: 0o700 });
    process.env.RAG_IME_APP_SUPPORT_DIR = support;
    process.env.PATH = `${bin}${path.delimiter}${previousPath || ''}`;
    const warmStarted = performance.now();
    const warmed = spawnSync('bash', [installer, '--warm-cache-only'], { encoding: 'utf8', timeout: 30_000, env: process.env });
    const warmMilliseconds = performance.now() - warmStarted;
    assert.equal(warmed.status, 0, warmed.stderr || warmed.error?.message);
    const cache = path.join(runtime, 'cache/matplotlib');
    const fontLists = fs.readdirSync(cache).filter(name => /^fontlist-.*\.json$/.test(name));
    assert.ok(fontLists.length > 0, 'installation must build the font cache before a delivery');
    const before = fontLists.map(name => [name, fs.statSync(path.join(cache, name)).mtimeMs, fs.readFileSync(path.join(cache, name), 'utf8')]);
    const runId = randomUUID();
    const runRelative = `.earth/gis/runs/${runId}`;
    const directory = path.join(root, runRelative);
    fs.mkdirSync(path.join(directory, 'pred_results'), { recursive: true });
    fs.writeFileSync(path.join(root, '.earth/gis/runtime.json'), JSON.stringify({ python: executable }));
    const collection = { type: 'FeatureCollection', features: Array.from({ length: 100 }, (_, index) => {
      const west = 120 + (index % 10) * 0.001, south = 30 + Math.floor(index / 10) * 0.001;
      return { type: 'Feature', id: `parcel-${index}`, properties: { id: `parcel-${index}` }, geometry: { type: 'Polygon', coordinates: [[[west, south], [west + 0.0005, south], [west + 0.0005, south + 0.0005], [west, south + 0.0005], [west, south]]] } };
    }) };
    const relativePath = 'pred_results/parcels.geojson';
    fs.writeFileSync(path.join(directory, relativePath), JSON.stringify(collection));
    fs.writeFileSync(path.join(directory, 'run.json'), JSON.stringify({ schemaVersion: 'earth.gis-run.v1', runId, status: 'completed', op: 'export', params: {}, startedAt: new Date().toISOString(), inputVersions: [], outputs: [{ path: `${runRelative}/${relativePath}`, relativePath, name: 'parcels.geojson', kind: 'vector', geojson: collection, summary: { featureCount: 100, crs: 'EPSG:4326' } }] }));
    const milliseconds = [];
    for (const version of [1, 2]) {
      const started = performance.now();
      const delivery = createGISBundle({ root, runId, name: 'hundred-parcels' });
      milliseconds.push(performance.now() - started);
      assert.equal(delivery.manifest.version, version);
      const destination = path.join(root, delivery.path);
      assert.equal(verifyGISBundle({ root, path: delivery.path }).runId, runId);
      assert.equal(JSON.parse(fs.readFileSync(path.join(destination, 'quality.json'), 'utf8')).checks[0].features, 100);
      assert.equal(fs.readFileSync(path.join(destination, 'map.pdf')).subarray(0, 5).toString(), '%PDF-');
      assert.ok(fs.statSync(path.join(destination, 'map.svg')).size > 0);
      assert.ok(delivery.manifest.files.every(item => !item.path.includes('matplotlib') && !item.path.includes('fontlist')));
    }
    assert.deepEqual(fs.readFileSync(calls, 'utf8').trim().split('\n'), [cache, cache, cache]);
    assert.equal(fs.existsSync(fontScan), false, 'renderer must use bundled fonts without scanning macOS system fonts');
    assert.deepEqual(fontLists.map(name => [name, fs.statSync(path.join(cache, name)).mtimeMs, fs.readFileSync(path.join(cache, name), 'utf8')]), before, 'delivery must reuse the prewarmed cache without deleting or rebuilding it');
    for (const elapsed of milliseconds) assert.ok(elapsed < 20_000, `render exceeded synchronous command latency budget: ${elapsed.toFixed(0)} ms`);
    t.diagnostic(JSON.stringify({ polygons: 100, warmMilliseconds: Math.round(warmMilliseconds), bundleMilliseconds: milliseconds.map(Math.round), fontCacheReused: true, systemFontsScanned: false }));
  } finally {
    if (previousSupport === undefined) delete process.env.RAG_IME_APP_SUPPORT_DIR;
    else process.env.RAG_IME_APP_SUPPORT_DIR = previousSupport;
    if (previousPath === undefined) delete process.env.PATH;
    else process.env.PATH = previousPath;
    fs.rmSync(root, { recursive: true, force: true });
  }
});
