import { randomUUID } from 'node:crypto';
import { runGISOperation } from './gis-operations.mjs';

export async function runGISBatch({ root, python, requests, concurrency = 2 }) {
  if (!Array.isArray(requests) || !requests.length) throw new Error('Batch requires at least one GIS request.');
  const limit = Math.max(1, Math.min(4, Number(concurrency) || 2));
  const results = new Array(requests.length);
  let cursor = 0;
  async function worker() {
    while (cursor < requests.length) {
      const index = cursor++;
      const request = requests[index];
      try { results[index] = { index, batchItemId: randomUUID(), ...(await runGISOperation({ root, python, request })) }; }
      catch (error) { results[index] = { index, batchItemId: randomUUID(), status: 'failed', error: String(error?.message || error) }; }
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, requests.length) }, worker));
  return { schemaVersion: 'earth.gis-batch.v1', status: results.every(item => item.status === 'completed') ? 'completed' : results.some(item => item.status === 'completed') ? 'partial' : 'failed', total: results.length, completed: results.filter(item => item.status === 'completed').length, failed: results.filter(item => item.status === 'failed').length, results };
}
