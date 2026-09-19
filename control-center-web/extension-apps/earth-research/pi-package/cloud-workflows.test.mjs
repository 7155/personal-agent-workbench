import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import { createHash } from 'node:crypto';
import test from 'node:test';
import { prepareCloudWorkflow } from './cloud-workflows.mjs';
import { executeScript } from './earth-execution.mjs';

const region = { type: 'Polygon', coordinates: [[[120, 30], [120.01, 30], [120.01, 30.01], [120, 30.01], [120, 30]]] };
const plan = { kind: 'ndvi', region, dateFrom: '2025-01-15', dateTo: '2025-04-01', scale: 20 };

test('prepares unique, hashed cloud scripts with explicit SCL, periods, scale and unverified authentication', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'earth-cloud-prepare-'));
  try {
    const first = prepareCloudWorkflow({ root, plan });
    const second = prepareCloudWorkflow({ root, plan });
    assert.notEqual(first.planId, second.planId);
    assert.notEqual(first.scriptPath, second.scriptPath);
    assert.equal(first.backend, 'gee');
    assert.equal(first.execution, 'gee');
    assert.equal(first.status, 'prepared');
    assert.equal(first.runnable, true);
    assert.equal(first.requirements.find(item => item.key === 'authentication').status, 'preparation_only');
    const saved = JSON.parse(fs.readFileSync(path.join(root, first.path), 'utf8'));
    assert.deepEqual(saved, first);
    assert.deepEqual(saved.plan.region, region);
    assert.equal(saved.plan.collection, 'COPERNICUS/S2_SR_HARMONIZED');
    assert.deepEqual(saved.plan.bands, { red: 'B4', nir: 'B8' });
    assert.deepEqual(saved.plan.periods.map(item => [item.start, item.end]), [['2025-01-15', '2025-02-01'], ['2025-02-01', '2025-03-01'], ['2025-03-01', '2025-04-01']]);
    const script = fs.readFileSync(path.join(root, first.scriptPath), 'utf8');
    assert.equal(first.scriptSha256, createHash('sha256').update(script).digest('hex'));
    new vm.Script(`(async () => {\n${script}\n})()`);
    assert.match(script, /select\('SCL'\)/);
    assert.match(script, /scl\.eq\(4\)\.or\(scl\.eq\(5\)\)\.or\(scl\.eq\(6\)\)/);
    assert.match(script, /ee\.Reducer\.count\(\)/);
    assert.match(script, /Earth\.evaluate/);
    assert.match(script, /Earth\.writeArtifact\('statistics\.json'/);
    assert.match(script, /Earth\.writeArtifact\('report\.html'/);
    assert.doesNotMatch(script, /ui\.Chart|eval\(|new Function/);
    assert.equal(fs.existsSync(path.join(root, 'analysis.js')), false);
    const annual = prepareCloudWorkflow({ root, plan: { ...plan, interval: 'annual', dateFrom: '2023-07-01', dateTo: '2025-04-01' } });
    assert.deepEqual(annual.plan.periods.map(item => item.label), ['2023', '2024', '2025']);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('prepares explicit change comparisons, real animation downloads and evidence-only research', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'earth-cloud-kinds-'));
  try {
    const change = prepareCloudWorkflow({ root, plan: { ...plan, kind: 'change', splitDate: '2025-02-15' } });
    assert.deepEqual(change.plan.periods.map(item => [item.label, item.start, item.end]), [['before', '2025-01-15', '2025-02-15'], ['after', '2025-02-15', '2025-04-01']]);
    const changeScript = fs.readFileSync(path.join(root, change.scriptPath), 'utf8');
    assert.match(changeScript, /after\.subtract\(before\)/);
    assert.match(changeScript, /commonValidPixelCount/);
    const animation = prepareCloudWorkflow({ root, plan: { ...plan, kind: 'animation', dimensions: 640, framesPerSecond: 3 } });
    const animationScript = fs.readFileSync(path.join(root, animation.scriptPath), 'utf8');
    assert.match(animationScript, /await Earth\.downloadAnimation/);
    assert.match(animationScript, /animation\.gif/);
    const question = 'Assess uncertainty; "); throw new Error("injected"); //';
    const research = prepareCloudWorkflow({ root, plan: { ...plan, kind: 'research', question } });
    assert.equal(research.runnable, false);
    const evidence = JSON.parse(fs.readFileSync(path.join(root, research.evidenceRequestPath), 'utf8'));
    assert.equal(evidence.question, question);
    assert.equal(evidence.sourcesQueried, false);
    assert.equal(evidence.status, 'awaiting_sources');
    assert.equal(evidence.report, null);
    assert.ok(evidence.requests.some(item => item.kind === 'scene_inventory'));
    for (const result of [change, animation, research]) new vm.Script(`(async () => {\n${fs.readFileSync(path.join(root, result.scriptPath), 'utf8')}\n})()`);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('rejects invalid dates, geographic coordinates, bands and writable paths before preparation', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'earth-cloud-reject-'));
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), 'earth-cloud-outside-'));
  try {
    for (const override of [{ dateFrom: '2025-02-30' }, { dateTo: plan.dateFrom }, { collection: 'arbitrary-script' }, { bands: { red: 'B4', nir: "B8'); print('injection" } }, { scale: 0 }, { scale: Infinity }, { interval: 'weekly' }, { region: { type: 'Point', coordinates: [120, 30] } }, { region: { type: 'Polygon', coordinates: [[[181, 0], [182, 0], [181, 1], [181, 0]]] } }, { script: 'print(42)' }]) {
      assert.throws(() => prepareCloudWorkflow({ root, plan: { ...plan, ...override } }));
    }
    fs.symlinkSync(outside, path.join(root, '.earth'));
    assert.throws(() => prepareCloudWorkflow({ root, plan }), /workspace/);
    assert.deepEqual(fs.readdirSync(outside), []);
  } finally { fs.rmSync(root, { recursive: true, force: true }); fs.rmSync(outside, { recursive: true, force: true }); }
});

function evaluated(value) { return { evaluate: callback => queueMicrotask(() => callback(value)) }; }

function fakeEarthEngine(rows, sceneCount = 3) {
  const calls = [];
  const chain = new Proxy({}, { get(_target, method) {
    if (method === 'getMap') return (_style, callback) => callback({ urlFormat: 'https://earthengine.googleapis.com/tiles/{z}/{x}/{y}' });
    if (method === 'centroid') return () => evaluated({ coordinates: [120, 30] });
    if (method === 'evaluate') return callback => queueMicrotask(() => callback(sceneCount));
    if (method === 'get') return () => evaluated(100);
    if (method === 'aggregate_array') return () => evaluated(['scene-a', 'scene-b', 'scene-c']);
    return (...args) => { calls.push({ method, args }); return chain; };
  } });
  const Image = Object.assign(() => chain, { constant: () => chain });
  const ee = { Geometry: () => chain, Image, ImageCollection: Object.assign(() => chain, { fromImages: () => chain }), Algorithms: { If: (_condition, yes) => yes }, Reducer: { mean: () => chain, minMax: () => chain, count: () => chain }, Feature: (_geometry, properties) => ({ properties }), FeatureCollection: () => evaluated({ type: 'FeatureCollection', features: rows.map(properties => ({ type: 'Feature', geometry: null, properties })) }) };
  return { ee, calls };
}

test('generated NDVI report uses returned reducer values and preserves missing periods as gaps', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'earth-cloud-report-'));
  try {
    const prepared = prepareCloudWorkflow({ root, plan });
    const rows = [
      { periodStart: '2025-01-15', periodEnd: '2025-02-01', label: '2025-01', imageCount: 2, meanNdvi: 0.25, minNdvi: 0.1, maxNdvi: 0.4, validPixelCount: 20 },
      { periodStart: '2025-02-01', periodEnd: '2025-03-01', label: '2025-02', imageCount: 0, meanNdvi: null, minNdvi: null, maxNdvi: null, validPixelCount: 0 },
      { periodStart: '2025-03-01', periodEnd: '2025-04-01', label: '2025-03', imageCount: 1, meanNdvi: 0.75, minNdvi: 0.5, maxNdvi: 1, validPixelCount: 50 },
    ];
    const { ee } = fakeEarthEngine(rows);
    const output = path.join(root, 'output');
    const result = await executeScript({ ee, script: fs.readFileSync(path.join(root, prepared.scriptPath), 'utf8'), downloadDir: output });
    assert.equal(result.status, 'completed', result.error);
    const statistics = JSON.parse(fs.readFileSync(path.join(output, 'statistics.json'), 'utf8'));
    assert.deepEqual(statistics.rows.map(item => item.meanNdvi), [0.25, null, 0.75]);
    assert.deepEqual(statistics.rows.map(item => item.validPixelCoverage), [0.2, 0, 0.5]);
    assert.deepEqual(statistics.sourceSceneIds, ['scene-a', 'scene-b', 'scene-c']);
    assert.equal(statistics.planId, prepared.planId);
    const report = fs.readFileSync(path.join(output, 'report.html'), 'utf8');
    assert.match(report, /<svg/);
    assert.match(report, /0\.2500/);
    assert.match(report, /0\.7500/);
    assert.match(report, /No valid pixels/);
    assert.equal(fs.readFileSync(path.join(output, 'statistics.csv'), 'utf8').split('\n')[2], '"2025-02","2025-02-01","2025-03-01","0","","","","0","0"');
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('empty source collections produce a no-data receipt and no fabricated chart or imagery', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'earth-cloud-no-data-'));
  try {
    const prepared = prepareCloudWorkflow({ root, plan });
    const { ee } = fakeEarthEngine([], 0);
    const output = path.join(root, 'output');
    const result = await executeScript({ ee, script: fs.readFileSync(path.join(root, prepared.scriptPath), 'utf8'), downloadDir: output });
    assert.equal(result.status, 'completed', result.error);
    const statistics = JSON.parse(fs.readFileSync(path.join(output, 'statistics.json'), 'utf8'));
    assert.equal(statistics.status, 'no_data');
    assert.deepEqual(statistics.rows, []);
    assert.equal(result.layers.length, 0);
    assert.equal(fs.existsSync(path.join(output, 'animation.gif')), false);
    assert.doesNotMatch(fs.readFileSync(path.join(output, 'report.html'), 'utf8'), /<svg/);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
