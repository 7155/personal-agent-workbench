import { randomUUID } from 'node:crypto';
import { runGISOperation } from './gis-operations.mjs';
import { executionFailure } from './runner-process.mjs';

export async function runGISBatch({ root, python, requests, concurrency = 2, signal }) {
  if (!Array.isArray(requests) || !requests.length) throw new Error('Batch requires at least one GIS request.');
  if (requests.length > 100) throw new Error('Batch supports at most 100 GIS requests.');
  concurrency = Math.max(1, Math.min(4, Math.floor(Number(concurrency)) || 2));
  const results = new Array(requests.length);
  let cursor = 0;
  async function worker() {
    while (cursor < requests.length) {
      const index = cursor++, batchItemId = randomUUID();
      if (signal?.aborted) {
        results[index] = { index, batchItemId, status: 'cancelled', code: 'not_started', outputs: [] };
        continue;
      }
      try { results[index] = { index, batchItemId, ...(await runGISOperation({ root, python, request: requests[index], signal })) }; }
      catch (error) { results[index] = { index, batchItemId, ...executionFailure(error) }; }
    }
  }
  await Promise.all(Array.from({ length: Math.min(concurrency, requests.length) }, worker));
  const count = status => results.filter(item => item.status === status).length;
  const completed = count('completed'), failed = count('failed'), cancelled = count('cancelled');
  const status = completed === results.length ? 'completed' : completed ? 'partial' : cancelled === results.length ? 'cancelled' : 'failed';
  return { schemaVersion: 'earth.gis-batch.v1', status, total: results.length, completed, failed, cancelled, results };
}
