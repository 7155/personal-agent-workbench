import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { findQGISProcess, helpQGISAlgorithm, listQGISAlgorithms, qgisBackendStatus, runQGISAlgorithm } from './qgis.mjs';

function fakeProcess(root) {
  const executable = path.join(root, 'qgis process.mjs');
  fs.writeFileSync(executable, `#!${process.execPath}
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { input += chunk; });
process.stdin.on('end', () => {
  const args = process.argv.slice(2);
  if (args.includes('native:fail')) { process.stderr.write('algorithm failed'); process.exitCode = 1; }
  process.stdout.write('provider initialization\\n');
  process.stdout.write(JSON.stringify({ args, input: input ? JSON.parse(input) : null, cwd: process.cwd(), platform: process.env.QT_QPA_PLATFORM, results: { OUTPUT: 'result.geojson' } }, null, 2));
});
`, { mode: 0o755 });
  return executable;
}

test('QGIS discovery distinguishes an executable from a directory or nonexecutable file', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'paw-qgis-discovery-'));
  try {
    const textFile = path.join(root, 'text');
    fs.writeFileSync(textFile, 'not an executable', { mode: 0o600 });
    assert.equal(findQGISProcess({ candidates: [root, textFile] }), null);
    assert.deepEqual(qgisBackendStatus({ candidates: [] }).status, 'unavailable');
    const executable = fakeProcess(root);
    const status = qgisBackendStatus({ candidates: [textFile, executable] });
    assert.equal(status.available, true);
    assert.equal(status.nativeTested, false);
    assert.equal(status.status, 'installed_unverified');
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('QGIS list/help/run use real subprocess JSON I/O and the required stdin marker', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'paw-qgis-adapter-'));
  try {
    const executable = fakeProcess(root);
    const listing = await listQGISAlgorithms({ executable });
    assert.deepEqual(listing.result.args, ['--json', 'list']);
    assert.equal(listing.result.input, null);
    assert.equal(listing.result.platform, process.env.QT_QPA_PLATFORM || 'offscreen');
    const help = await helpQGISAlgorithm({ executable, algorithm: 'native:buffer' });
    assert.deepEqual(help.result.args, ['--json', 'help', 'native:buffer']);
    const inputs = { INPUT: 'data/中文源.geojson', DISTANCE: 200, OUTPUT: 'result.geojson' };
    const result = await runQGISAlgorithm({ executable, root, algorithm: 'native:buffer', inputs });
    assert.deepEqual(result.command, ['--json', 'run', 'native:buffer', '-']);
    assert.deepEqual(result.result.args, result.command);
    assert.deepEqual(result.result.input, { inputs });
    assert.equal(result.result.cwd, fs.realpathSync(root));
    assert.equal(result.result.results.OUTPUT, 'result.geojson');
    await assert.rejects(runQGISAlgorithm({ executable, algorithm: 'native:fail' }), /algorithm failed/);
    await assert.rejects(runQGISAlgorithm({ executable, algorithm: 'native:buffer; touch anything' }), error => error.code === 'qgis_invalid_algorithm');
    await assert.rejects(runQGISAlgorithm({ executable, algorithm: 'native:buffer', inputs: [] }), error => error.code === 'qgis_invalid_inputs');
    await assert.rejects(runQGISAlgorithm({ executable, algorithm: 'native:buffer', inputs: { DISTANCE: NaN } }), error => error.code === 'qgis_invalid_inputs');
    await assert.rejects(runQGISAlgorithm({ executable, algorithm: 'native:buffer', inputs: { INPUT: 'x'.repeat(256_001) } }), error => error.code === 'qgis_request_too_large');
    await assert.rejects(listQGISAlgorithms({ executable: path.join(root, 'missing') }), error => error.code === 'qgis_unavailable');
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

const nativeExecutable = findQGISProcess();
test('installed native QGIS lists algorithms, describes buffer, and buffers a real WGS84 vector', { skip: nativeExecutable ? false : 'qgis_process is absent; native acceptance is unverified' }, async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'paw-qgis-native-'));
  try {
    const listing = await listQGISAlgorithms({ executable: nativeExecutable });
    assert.equal(listing.status, 'completed');
    assert.match(JSON.stringify(listing.result), /native:buffer/);
    assert.equal((await helpQGISAlgorithm({ executable: nativeExecutable, algorithm: 'native:buffer' })).status, 'completed');
    const input = path.join(root, 'point.geojson');
    const output = path.join(root, 'buffer.geojson');
    fs.writeFileSync(input, JSON.stringify({ type: 'FeatureCollection', features: [{ type: 'Feature', properties: { id: 'site' }, geometry: { type: 'Point', coordinates: [1, 1] } }] }));
    const receipt = await runQGISAlgorithm({ executable: nativeExecutable, root, algorithm: 'native:buffer', inputs: { INPUT: 'point.geojson', DISTANCE: 0.01, SEGMENTS: 8, END_CAP_STYLE: 0, JOIN_STYLE: 0, MITER_LIMIT: 2, DISSOLVE: false, OUTPUT: 'buffer.geojson' } });
    assert.equal(receipt.status, 'completed');
    const readback = JSON.parse(fs.readFileSync(output, 'utf8'));
    assert.equal(readback.features.length, 1);
    assert.match(readback.features[0].geometry.type, /Polygon/);
    assert.equal(readback.features[0].properties.id, 'site');
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
