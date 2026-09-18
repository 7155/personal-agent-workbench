import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { listMLTemplates, mlTemplate, prepareMLScript } from './ml-templates.mjs';

test('lists bounded ML workflows and renders a workspace script with explicit unresolved inputs', () => {
  assert.deepEqual(listMLTemplates().map(item => item.id), ['random_forest', 'kmeans', 'change_detection']);
  assert.match(mlTemplate('random_forest').script, /smileRandomForest/);
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'earth-ml-'));
  try {
    const result = prepareMLScript(root, {
      workflow: 'random_forest',
      saveAs: 'workflows/site.js',
      replacements: { DATASET_ID: 'LANDSAT/LC08/C02/T1_L2', TRAINING_ASSET: 'users/demo/samples' },
    });
    assert.equal(result.path, 'workflows/site.js');
    assert.deepEqual(result.unresolved, ['BANDS', 'CLASS_PROPERTY', 'SCALE']);
    assert.equal(fs.existsSync(path.join(root, result.path)), true);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('does not let ML preparation escape the bound workspace', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'earth-ml-'));
  try {
    assert.throws(() => prepareMLScript(root, { workflow: 'kmeans', saveAs: '../outside.js' }), /workspace-relative/);
    assert.throws(() => mlTemplate('unknown'), /Unknown Earth ML workflow/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
