import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import test from 'node:test';
import { prepareRemoteSensingWorkflow, runRemoteSensingWorkflow } from './remote-sensing.mjs';
import { saveProjectLayer } from './gis-project.mjs';

const managed = path.join(os.homedir(), 'Library', 'Application Support', 'RagIme', 'EarthGISRuntime', '.venv', 'bin', 'python');
const python = process.env.PAW_EARTH_GIS_PYTHON || (fs.existsSync(managed) ? managed : 'python3');
const hasGIS = spawnSync(python, ['-c', 'import numpy, geopandas, rasterio'], { stdio: 'ignore' }).status === 0;
const hasRF = hasGIS && spawnSync(python, ['-c', 'import sklearn, scipy'], { stdio: 'ignore' }).status === 0;
const gis = { skip: !hasGIS && !process.env.PAW_EARTH_GIS_PYTHON ? 'Configure PAW_EARTH_GIS_PYTHON for real raster tests.' : false };
const rf = { skip: !hasRF && !process.env.PAW_EARTH_GIS_PYTHON ? 'Install scikit-learn in PAW_EARTH_GIS_PYTHON for real RF tests.' : false };
const region = { type: 'Polygon', coordinates: [[[0, 0], [0.004, 0], [0.004, 0.004], [0, 0.004], [0, 0]]] };
const hash = file => createHash('sha256').update(fs.readFileSync(file)).digest('hex');

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'paw-remote-sensing-'));
  fs.mkdirSync(path.join(root, 'data'));
  return root;
}

function pythonJSON(code, ...args) {
  const result = spawnSync(python, ['-c', code, ...args], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr || result.stdout);
  return JSON.parse(result.stdout);
}

function raster(root, mode = 'ndvi') {
  const relative = 'data/image.tif';
  pythonJSON(`import json, sys, numpy as np, rasterio
from rasterio.transform import from_origin
array = np.empty((2, 40, 40), dtype="float32")
if sys.argv[2] == "classification":
    array[0, :, :20], array[0, :, 20:] = 2, 8
    array[1, :, :20], array[1, :, 20:] = 8, 2
else:
    array[0], array[1] = 2, 6
    array[:, 0, 0] = -9999
    array[:, 0, 1] = 0
with rasterio.open(sys.argv[1], "w", driver="GTiff", width=40, height=40, count=2, dtype="float32", crs="EPSG:4326", transform=from_origin(0, .004, .0001, .0001), nodata=-9999) as dst:
    dst.write(array)
    dst.set_band_description(1, "red")
    dst.set_band_description(2, "nir")
print(json.dumps({"created": True}))`, path.join(root, relative), mode);
  return relative;
}

function rectangle(id, value, west, south, site = id) {
  return { type: 'Feature', id, properties: { class: value, site }, geometry: { type: 'Polygon', coordinates: [[[west, south], [west + 0.0005, south], [west + 0.0005, south + 0.0005], [west, south + 0.0005], [west, south]]] } };
}

function labels(root, features = [rectangle('fissure-a', 'fissure', 0.0002, 0.0002), rectangle('fissure-b', 'fissure', 0.0002, 0.0028), rectangle('rock-a', 0, 0.0028, 0.0002), rectangle('rock-b', 0, 0.0028, 0.0028)]) {
  const relative = 'data/samples.geojson';
  fs.writeFileSync(path.join(root, relative), JSON.stringify({ type: 'FeatureCollection', features }));
  return relative;
}

test('preparation records exact missing imagery and labels and never substitutes a cloud collection for pixels', async () => {
  const root = fixture();
  try {
    const prepared = await prepareRemoteSensingWorkflow({ root, plan: { kind: 'classification', region, collection: 'example/collection' } });
    assert.equal(prepared.status, 'needs_input');
    assert.equal(prepared.runnable, false);
    assert.deepEqual(prepared.requirements.filter(item => item.status === 'missing').map(item => item.key), ['imagePath', 'samples', 'classField']);
    const result = await runRemoteSensingWorkflow({ root, planId: prepared.planId, python: '/not/a/python' });
    assert.equal(result.status, 'needs_input');
    assert.equal(result.code, 'missing_inputs');
    assert.deepEqual(result.outputs, []);
    assert.equal(fs.existsSync(path.join(root, prepared.path)), true);
    await assert.rejects(prepareRemoteSensingWorkflow({ root, plan: { kind: 'ndvi', region, imagePath: '../outside.tif', bands: [1, 2] } }), /escaped/);
    await assert.rejects(prepareRemoteSensingWorkflow({ root, plan: { kind: 'ndvi', region: { type: 'Point', coordinates: [0, 0] } } }), /Polygon/);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('NDVI executes on real red/NIR bands with nodata and zero-denominator masks and exact readback', gis, async () => {
  const root = fixture();
  try {
    const imagePath = raster(root);
    const original = hash(path.join(root, imagePath));
    const prepared = await prepareRemoteSensingWorkflow({ root, plan: { kind: 'ndvi', region, imagePath, bands: { red: 'red', nir: 'nir' } } });
    assert.equal(prepared.runnable, true);
    const result = await runRemoteSensingWorkflow({ root, planId: prepared.planId, python });
    assert.equal(result.status, 'completed', JSON.stringify(result));
    assert.equal(result.statistics.count, 1598);
    assert.equal(result.statistics.maskedPixelCount, 2);
    assert.equal(result.statistics.mean, 0.5);
    assert.equal(result.statistics.outsideUnitRangeCount, 0);
    const output = result.outputs.find(item => item.name === 'ndvi.tif');
    const readback = pythonJSON('import json, sys, rasterio; src = rasterio.open(sys.argv[1]); data = src.read(1, masked=True); print(json.dumps({"count": int(data.count()), "mean": float(data.mean()), "crs": str(src.crs), "masked": bool(data.mask[0,0]) and bool(data.mask[0,1])}))', path.join(root, output.path));
    assert.deepEqual(readback, { count: 1598, mean: 0.5, crs: 'EPSG:4326', masked: true });
    assert.equal(output.sha256, hash(path.join(root, output.path)));
    assert.equal(hash(path.join(root, imagePath)), original);
    assert.equal(JSON.parse(fs.readFileSync(path.join(root, '.earth/remote-sensing/workspace.json'), 'utf8')).runId, result.runId);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('random forest trains on feature groups, holds out every class, and writes real typed classes and metrics', rf, async () => {
  const root = fixture();
  try {
    const imagePath = raster(root, 'classification'), samplePath = labels(root);
    const sourceHashes = [hash(path.join(root, imagePath)), hash(path.join(root, samplePath))];
    const prepared = await prepareRemoteSensingWorkflow({ root, plan: { kind: 'classification', region, imagePath, samplePath, classField: 'class', groupField: 'site', bands: [1, 2] } });
    const result = await runRemoteSensingWorkflow({ root, planId: prepared.planId, python });
    assert.equal(result.status, 'completed', JSON.stringify(result));
    assert.deepEqual(result.classMapping.map(item => [item.value, item.valueType]), [['fissure', 'string'], [0, 'number']]);
    assert.ok(result.metrics.trainPixelCount > 0);
    assert.ok(result.metrics.validationPixelCount > 0);
    assert.equal(result.metrics.confusionMatrix.flat().reduce((sum, count) => sum + count, 0), result.metrics.validationPixelCount);
    assert.equal(result.metrics.confusionMatrix.reduce((sum, row, index) => sum + row[index], 0) / result.metrics.validationPixelCount, result.metrics.accuracy);
    const split = JSON.parse(fs.readFileSync(path.join(root, result.outputs.find(item => item.name === 'sample-split.json').path), 'utf8'));
    assert.equal(split.unit, 'groupField');
    assert.equal(split.training.filter(group => split.validation.some(other => other.group === group.group)).length, 0);
    assert.equal(split.validationBufferPixels, 1);
    const output = result.outputs.find(item => item.name === 'classification.tif');
    const readback = pythonJSON('import json, sys, rasterio, numpy as np; src = rasterio.open(sys.argv[1]); a = src.read(1); print(json.dumps({"codes": np.unique(a).tolist(), "left": int(a[20,5]), "right": int(a[20,30]), "nodata": src.nodata, "classes": json.loads(src.tags()["PAW_CLASS_MAPPING"])}))', path.join(root, output.path));
    assert.deepEqual(readback.codes, [1, 2]);
    assert.equal(readback.left, result.classMapping.find(item => item.value === 'fissure').code);
    assert.equal(readback.right, result.classMapping.find(item => item.value === 0).code);
    assert.equal(readback.nodata, 0);
    assert.deepEqual(readback.classes, result.classMapping);
    assert.deepEqual([hash(path.join(root, imagePath)), hash(path.join(root, samplePath))], sourceHashes);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('one polygon per class cannot manufacture a validation set by splitting its pixels', rf, async () => {
  const root = fixture();
  try {
    const imagePath = raster(root, 'classification');
    const samplePath = labels(root, [rectangle('one-a', 'fissure', 0.0002, 0.0002), rectangle('one-b', 'rock', 0.0028, 0.0002)]);
    const prepared = await prepareRemoteSensingWorkflow({ root, plan: { kind: 'classification', region, imagePath, samplePath, classField: 'class' } });
    const result = await runRemoteSensingWorkflow({ root, planId: prepared.planId, python });
    assert.equal(result.status, 'failed');
    assert.equal(result.code, 'insufficient_holdout_groups');
    assert.deepEqual(result.outputs, []);
    assert.equal(result.metrics, undefined);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('overlapping contradictory labels fail before fitting rather than replacing a class', rf, async () => {
  const root = fixture();
  try {
    const imagePath = raster(root, 'classification');
    const samplePath = labels(root, [rectangle('a', 'fissure', 0.0002, 0.0002), rectangle('b', 'rock', 0.0002, 0.0002)]);
    const prepared = await prepareRemoteSensingWorkflow({ root, plan: { kind: 'classification', region, imagePath, samplePath, classField: 'class' } });
    const result = await runRemoteSensingWorkflow({ root, planId: prepared.planId, python });
    assert.equal(result.status, 'failed');
    assert.equal(result.code, 'conflicting_labels');
    assert.deepEqual(result.outputs, []);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('saved layer plans bind immutable sample revision and reject changed input bytes before execution', gis, async () => {
  const root = fixture();
  try {
    const imagePath = raster(root);
    const layer = saveProjectLayer({ root, name: '训练样本', features: [rectangle('a', 'fissure', 0.0002, 0.0002)] }).layer;
    const samplePlan = await prepareRemoteSensingWorkflow({ root, plan: { kind: 'classification', region, imagePath, sampleLayerId: layer.id, classField: 'class' } });
    assert.equal(samplePlan.plan.samplePath, layer.path);
    assert.equal(samplePlan.plan.sampleRevision, layer.revision);
    assert.equal(samplePlan.inputVersions.find(item => item.role === 'samples').revision, layer.revision);
    const ndviPlan = await prepareRemoteSensingWorkflow({ root, plan: { kind: 'ndvi', region, imagePath, bands: [1, 2] } });
    fs.appendFileSync(path.join(root, imagePath), 'changed');
    const result = await runRemoteSensingWorkflow({ root, planId: ndviPlan.planId, python: '/not/a/python' });
    assert.equal(result.status, 'failed');
    assert.match(result.error, /changed after plan preparation/);
    assert.deepEqual(result.outputs, []);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('local adapter reports animation and change plans as preparation only', gis, async () => {
  const root = fixture();
  try {
    const imagePath = raster(root);
    for (const kind of ['change', 'animation', 'research']) {
      const prepared = await prepareRemoteSensingWorkflow({ root, plan: { kind, region, imagePath } });
      assert.equal(prepared.execution, 'preparation_only');
      assert.equal(prepared.runnable, false);
      const result = await runRemoteSensingWorkflow({ root, planId: prepared.planId, python: '/not/a/python' });
      assert.equal(result.code, 'workflow_preparation_only');
      assert.deepEqual(result.outputs, []);
    }
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
