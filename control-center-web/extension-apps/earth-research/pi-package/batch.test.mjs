import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { runGISBatch } from './batch.mjs';

test('keeps invalid local GIS batch items visible in an aggregate receipt', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'earth-batch-'));
  try {
    const result = await runGISBatch({ root, concurrency: 9, requests: [{ op: 'buffer', inputs: {} }] });
    assert.equal(result.schemaVersion, 'earth.gis-batch.v1');
    assert.equal(result.status, 'failed');
    assert.equal(result.total, 1);
    assert.equal(result.failed, 1);
    assert.equal(result.results[0].index, 0);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('rejects an empty batch before creating any run', async () => {
  await assert.rejects(() => runGISBatch({ root: os.tmpdir(), requests: [] }), /at least one GIS request/);
});
