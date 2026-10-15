import fs from 'node:fs';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { randomUUID } from 'node:crypto';

function terminalReceipt(stdout) {
  for (const line of String(stdout || '').trim().split(/\r?\n/).reverse()) {
    try {
      const value = JSON.parse(line);
      if (value && typeof value.runId === 'string' && value.runId
        && ['completed', 'failed', 'cancelled'].includes(value.status)) return value;
    } catch { /* Ordinary runner output is not a terminal receipt. */ }
  }
  return null;
}

function executeRunner(args, root, signal) {
  return new Promise(resolve => {
    let stopped = false, timedOut = false, closed = false, forceKill;
    const child = execFile(process.execPath, args, { cwd: root, maxBuffer: 4 * 1024 * 1024 }, (error, stdout) => {
      closed = true;
      clearTimeout(deadline); clearTimeout(forceKill);
      signal?.removeEventListener('abort', stop);
      resolve({ error, stdout, stopped, timedOut });
    });
    function terminate() {
      if (closed) return;
      child.kill('SIGTERM');
      forceKill ??= setTimeout(() => { if (!closed) child.kill('SIGKILL'); }, 3000);
    }
    function stop() { stopped = true; terminate(); }
    const deadline = setTimeout(() => { timedOut = true; terminate(); }, 310_000);
    signal?.addEventListener('abort', stop, { once: true });
    if (signal?.aborted) stop();
  });
}

export async function runEarthScriptBatch({ root, scripts, project, python, dependencies, concurrency = 2, signal }) {
  if (!Array.isArray(scripts) || !scripts.length) throw new Error('Cloud batch requires at least one script.');
  const runtime = JSON.parse(fs.readFileSync(path.join(root, '.earth', 'runtime.json'), 'utf8'));
  const runner = runtime.runner;
  if (typeof runner !== 'string' || !fs.existsSync(runner)) throw new Error('Earth Engine runner is not configured.');
  const limit = Math.max(1, Math.min(3, Number(concurrency) || 2));
  const results = new Array(scripts.length); let cursor = 0;
  async function worker() {
    while (cursor < scripts.length) {
      const index = cursor++; const relative = scripts[index]; const id = randomUUID();
      if (signal?.aborted) {
        results[index] = { index, batchItemId: id, script: relative, status: 'cancelled', runId: null, artifact: null, error: null, remoteComputeMayContinue: false };
        continue;
      }
      try {
        if (typeof relative !== 'string' || relative.startsWith('/') || relative.includes('..')) throw new Error('Cloud batch scripts must stay inside the workspace.');
        const file = path.resolve(root, relative); if (!file.startsWith(`${path.resolve(root)}${path.sep}`) || !fs.existsSync(file)) throw new Error(`Missing batch script: ${relative}`);
        const execution = await executeRunner([runner, '--root', root, '--script', file, '--project', project || runtime.project, '--dependencies', dependencies || runtime.dependencies, '--python', python || runtime.python], root, signal);
        const receipt = terminalReceipt(execution.stdout);
        // A nonzero exit can still carry the real failed/cancelled run receipt.
        // Retain its identity instead of replacing it with execFile's command dump.
        const status = receipt?.status === 'completed' && !execution.error && !execution.timedOut ? 'completed'
          : execution.stopped ? 'cancelled' : receipt?.status === 'cancelled' && !execution.timedOut ? 'cancelled' : 'failed';
        const error = status === 'completed' ? null : execution.timedOut ? 'Earth Engine batch item exceeded its execution deadline; remote computation may continue.'
          : receipt?.error || (status === 'cancelled' ? null : receipt ? 'Runner exited unsuccessfully despite its terminal receipt.' : 'Runner returned no terminal run receipt.');
        results[index] = { index, batchItemId: id, script: relative, status, runId: receipt?.runId || null, artifact: typeof receipt?.artifact === 'string' ? receipt.artifact : null, error,
          ...(status === 'cancelled' ? { remoteComputeMayContinue: true } : {}) };
      } catch (error) { results[index] = { index, batchItemId: id, script: relative, status: 'failed', runId: null, artifact: null, error: String(error?.message || error) }; }
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, scripts.length) }, worker));
  return { schemaVersion: 'earth.script-batch.v1', status: results.every(item => item.status === 'completed') ? 'completed' : results.some(item => item.status === 'completed') ? 'partial' : results.every(item => item.status === 'cancelled') ? 'cancelled' : 'failed', total: results.length, completed: results.filter(item => item.status === 'completed').length, failed: results.filter(item => item.status === 'failed').length, cancelled: results.filter(item => item.status === 'cancelled').length, results };
}
