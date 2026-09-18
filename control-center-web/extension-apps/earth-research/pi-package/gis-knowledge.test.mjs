import assert from 'node:assert/strict';
import { test } from 'node:test';
import { searchGISKnowledge } from './gis-knowledge.mjs';

test('retrieves CRS and local/cloud guidance with bounded, source-indexed hits', () => {
  const result = searchGISKnowledge('buffer distance CRS meter local cloud');
  assert.equal(result.index, 'earth-gis-knowledge.v1');
  assert.ok(result.hits.some(hit => hit.id === 'crs-buffer'));
  assert.ok(result.hits.length <= 8);
});

test('empty GIS knowledge queries remain explicit instead of returning arbitrary context', () => {
  assert.deepEqual(searchGISKnowledge(''), { query: '', hits: [], total: 0, index: 'earth-gis-knowledge.v1' });
});
