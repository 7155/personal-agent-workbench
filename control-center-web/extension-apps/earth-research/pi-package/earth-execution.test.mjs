import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import https from 'node:https';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
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

test('writes bounded report artifacts and refuses overwrites or escaped filenames', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'earth-artifact-'));
  try {
    const result = await executeScript({ ee: {}, downloadDir: root, script: "await Earth.writeArtifact('statistics.json', JSON.stringify({ meanNdvi: 0.42, validPixelCount: 10 })); await Earth.writeArtifact('report.html', '<!doctype html><p>Observed values</p>');" });
    assert.equal(result.status, 'completed');
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(root, 'statistics.json'), 'utf8')), { meanNdvi: 0.42, validPixelCount: 10 });
    assert.deepEqual(result.artifacts.map(item => item.kind), ['json', 'html']);
    assert.match(result.artifacts[0].sha256, /^[a-f0-9]{64}$/);
    const repeat = await executeScript({ ee: {}, downloadDir: root, script: "await Earth.writeArtifact('statistics.json', 'overwritten');" });
    assert.equal(repeat.status, 'failed');
    assert.equal(JSON.parse(fs.readFileSync(path.join(root, 'statistics.json'), 'utf8')).meanNdvi, 0.42);
    const unawaited = await executeScript({ ee: {}, downloadDir: root, script: "Earth.writeArtifact('summary.md', 'Observed result');" });
    assert.equal(unawaited.status, 'completed');
    assert.equal(fs.readFileSync(path.join(root, 'summary.md'), 'utf8'), 'Observed result');
    for (const filename of ['../outside.json', '/tmp/outside.json', 'script.js', '.hidden.json']) {
      const invalid = await executeScript({ ee: {}, downloadDir: root, script: `await Earth.writeArtifact(${JSON.stringify(filename)}, 'invalid');` });
      assert.equal(invalid.status, 'failed');
      assert.match(invalid.error, /filename/);
    }
    assert.equal((await executeScript({ ee: {}, script: "await Earth.writeArtifact('stats.json', '{}');" })).status, 'failed');
    const oversized = await executeScript({ ee: {}, downloadDir: root, script: "await Earth.writeArtifact('large.json', 'x'.repeat(4 * 1024 * 1024 + 1));" });
    assert.equal(oversized.status, 'failed');
    assert.equal(fs.existsSync(path.join(root, 'large.json')), false);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('downloads animation only from the official HTTPS host and verifies actual GIF bytes', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'earth-animation-'));
  const gif = Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64');
  let body = gif, requested = 0, contentLength;
  t.mock.method(https, 'get', (_url, callback) => {
    requested += 1;
    const request = new EventEmitter();
    request.setTimeout = () => request;
    request.destroy = error => { if (error) queueMicrotask(() => request.emit('error', error)); return request; };
    queueMicrotask(() => {
      const response = new PassThrough();
      response.statusCode = 200;
      response.headers = { ...(contentLength ? { 'content-length': String(contentLength) } : {}), 'content-type': 'image/gif' };
      callback(response);
      response.end(body);
    });
    return request;
  });
  let url = 'https://earthengine.googleapis.com/v1/projects/test/videoThumbnails/real:getPixels';
  const collection = { getVideoThumbURL: (_params, callback) => callback(url) };
  const execute = filename => executeScript({ ee: { ImageCollection: () => collection }, downloadDir: root, script: `await Earth.downloadAnimation(ee.ImageCollection(), { region: {type:'Polygon',coordinates:[[[0,0],[1,0],[1,1],[0,0]]]}, dimensions: 512, framesPerSecond: 2 }, ${JSON.stringify(filename)});` });
  try {
    const result = await execute('animation.gif');
    assert.equal(result.status, 'completed', result.error);
    assert.equal(result.artifacts[0].kind, 'gif');
    assert.deepEqual(fs.readFileSync(path.join(root, 'animation.gif')), gif);
    assert.equal(requested, 1);
    url = 'https://earthengine.googleapis.com.attacker.invalid/animation.gif';
    const rejectedHost = await execute('rejected.gif');
    assert.equal(rejectedHost.status, 'failed');
    assert.match(rejectedHost.error, /host/);
    assert.equal(requested, 1);
    url = 'https://earthengine.googleapis.com/v1/projects/test/videoThumbnails/real:getPixels';
    body = Buffer.from('<html>not an animation</html>');
    const rejectedBytes = await execute('not-gif.gif');
    assert.equal(rejectedBytes.status, 'failed');
    assert.match(rejectedBytes.error, /GIF/);
    assert.equal(fs.existsSync(path.join(root, 'not-gif.gif')), false);
    body = gif;
    contentLength = 64 * 1024 * 1024 + 1;
    const tooLarge = await execute('too-large.gif');
    assert.equal(tooLarge.status, 'failed');
    assert.match(tooLarge.error, /64 MiB/);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
