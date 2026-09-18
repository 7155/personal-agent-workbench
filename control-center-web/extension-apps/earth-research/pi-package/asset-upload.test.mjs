import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { uploadTableAsset } from './asset-upload.mjs';

test('requires a supported workspace-owned table before invoking the Earth Engine CLI', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'earth-asset-'));
  try {
    fs.writeFileSync(path.join(root, 'notes.txt'), 'not a table');
    fs.writeFileSync(path.join(root, 'data.csv'), 'id\n1\n');
    fs.writeFileSync(path.join(root, 'roads.shp'), 'placeholder');
    await assert.rejects(() => uploadTableAsset({ root, path: 'notes.txt', assetId: 'users/demo/table' }), /accepts .shp, .zip or .csv/);
    await assert.rejects(() => uploadTableAsset({ root, path: '../notes.txt', assetId: 'users/demo/table' }), /inside the bound workspace/);
    await assert.rejects(() => uploadTableAsset({ root, path: 'data.csv', assetId: 'https://example.invalid/table' }), /Asset ID must be/);
    await assert.rejects(() => uploadTableAsset({ root, path: 'roads.shp', assetId: 'users/demo/roads' }), /requires a matching .prj/);
    fs.writeFileSync(path.join(root, 'roads.prj'), 'LOCAL_CS["not geographic"]');
    await assert.rejects(() => uploadTableAsset({ root, path: 'roads.shp', assetId: 'users/demo/roads' }), /must describe EPSG:4326\/WGS84/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
