import assert from 'node:assert/strict';
import { test } from 'node:test';
import { executeScript } from './earth-execution.mjs';

test('waits for real asynchronous evaluation before completing, preserving code and layers', async () => {
  const pending = [];
  const image = { getMap: (_style, cb) => pending.push(() => cb({ urlFormat: 'https://earthengine.googleapis.com/v1/tiles/{z}/{x}/{y}' })) };
  const ee = { Image: () => image, Number: () => ({ evaluate: cb => pending.push(() => cb(42)) }) };
  const snapshots = [];
  const run = executeScript({ ee, script: "Map.addLayer(ee.Image(), {}, '地形'); print('数量', ee.Number());", onChange: x => snapshots.push(structuredClone(x)) });
  assert.equal(snapshots.at(-1).status, 'running');
  pending.forEach(fn => fn());
  const result = await run;
  assert.equal(result.status, 'completed');
  assert.equal(result.layers[0].name, '地形');
  assert.equal(result.console[0].values[1], 42);
});

test('API failure retains earlier output and never becomes a successful map', async () => {
  const ee = { Image: () => ({ getMap: (_s, cb) => cb(undefined, 'Earth Engine permission denied') }) };
  const result = await executeScript({ ee, script: "print('开始'); Map.addLayer(ee.Image(), {}, '坡度');" });
  assert.equal(result.status, 'failed');
  assert.match(result.error, /permission denied/);
  assert.equal(result.console[0].values[0], '开始');
  assert.equal(result.layers.length, 0);
});

test('script errors have a filename and line; unsupported Code Editor APIs fail explicitly', async () => {
  const result = await executeScript({ ee: {}, script: 'var x = 1;\nui.Panel();', filename: 'analysis.js' });
  assert.equal(result.status, 'failed');
  assert.match(result.error, /analysis.js:2/);
});

test('captures GeoJSON, map center and output order rather than callback completion order', async () => {
  let finish;
  const feature = { evaluate: cb => { finish = cb; } };
  const promise = executeScript({ ee: { FeatureCollection: () => feature }, script: "print(ee.FeatureCollection()); print('第二条'); Map.setCenter(120, 30, 11);" });
  finish({ type: 'FeatureCollection', features: [] });
  const result = await promise;
  assert.equal(result.console[0].values[0].type, 'FeatureCollection');
  assert.equal(result.console[1].values[0], '第二条');
  assert.deepEqual(result.view, { center: [120, 30], zoom: 11 });
});

test('awaited Earth evaluation supports data-dependent algorithms and waits for later outputs', async () => {
  const ee = { Number: () => ({ evaluate: cb => queueMicrotask(() => cb(21)) }) };
  const result = await executeScript({ ee, script: 'var x = await Earth.evaluate(ee.Number()); print(x * 2);' });
  assert.equal(result.status, 'completed'); assert.equal(result.console[0].values[0], 42);
});

test('starts supported export tasks and records task identity without claiming completion', async () => {
  const task = { id: null, start() { this.id = 'task-123'; } };
  const ee = { Image: () => ({ getMap: (_style, cb) => cb({ urlFormat: 'https://earthengine.googleapis.com/v1/tiles/{z}/{x}/{y}' }) }), batch: { Export: { image: { toAsset: () => task } } } };
  const result = await executeScript({ ee, script: "Export.image.toAsset(ee.Image(), 'demo', 'projects/test/assets/demo');" });
  assert.equal(result.status, 'completed');
  assert.deepEqual(result.tasks[0], { id: 'task-123', kind: 'image', destination: 'toAsset', status: 'submitted', submittedAt: result.tasks[0].submittedAt });
});
