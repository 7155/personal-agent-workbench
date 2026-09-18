import fs from 'node:fs';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { randomUUID } from 'node:crypto';

const execFileAsync = promisify(execFile);

export async function runEarthScriptBatch({ root, scripts, project, python, dependencies, concurrency = 2 }) {
  if (!Array.isArray(scripts) || !scripts.length) throw new Error('Cloud batch requires at least one script.');
  const runtime = JSON.parse(fs.readFileSync(path.join(root, '.earth', 'runtime.json'), 'utf8'));
  const runner = runtime.runner;
  if (typeof runner !== 'string' || !fs.existsSync(runner)) throw new Error('Earth Engine runner is not configured.');
  const limit = Math.max(1, Math.min(3, Number(concurrency) || 2));
  const results = new Array(scripts.length); let cursor = 0;
  async function worker() {
    while (cursor < scripts.length) {
      const index = cursor++; const relative = scripts[index]; const id = randomUUID();
      try {
        if (typeof relative !== 'string' || relative.startsWith('/') || relative.includes('..')) throw new Error('Cloud batch scripts must stay inside the workspace.');
        const file = path.resolve(root, relative); if (!file.startsWith(`${path.resolve(root)}${path.sep}`) || !fs.existsSync(file)) throw new Error(`Missing batch script: ${relative}`);
        const { stdout, stderr } = await execFileAsync(process.execPath, [runner, '--root', root, '--script', file, '--project', project || runtime.project, '--dependencies', dependencies || runtime.dependencies, '--python', python || runtime.python], { cwd: root, timeout: 310000, maxBuffer: 4 * 1024 * 1024 });
        const lines = `${stdout}\n${stderr}`.trim().split(/\r?\n/).reverse(); const receipt = lines.map(line => { try { return JSON.parse(line); } catch { return null; } }).find(Boolean);
        results[index] = { index, batchItemId: id, script: relative, status: receipt?.status || 'unknown', runId: receipt?.runId || null, error: receipt?.error || null };
      } catch (error) { results[index] = { index, batchItemId: id, script: relative, status: 'failed', runId: null, error: String(error?.message || error) }; }
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, scripts.length) }, worker));
  return { schemaVersion: 'earth.script-batch.v1', status: results.every(item => item.status === 'completed') ? 'completed' : results.some(item => item.status === 'completed') ? 'partial' : 'failed', total: results.length, completed: results.filter(item => item.status === 'completed').length, failed: results.filter(item => item.status === 'failed').length, results };
}
