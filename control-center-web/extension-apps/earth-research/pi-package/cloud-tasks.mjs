import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const packageRoot = path.dirname(fileURLToPath(import.meta.url));

function configFor(root, input = {}) {
  const file = path.join(root, '.earth', 'runtime.json');
  const existing = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : {};
  const config = { ...existing };
  for (const [key, value] of Object.entries(input)) if (value !== undefined && value !== '') config[key] = value;
  if (!config.project) throw new Error('Set an authorized Earth Engine project in .earth/runtime.json.');
  if (!config.dependencies) throw new Error('Earth Engine SDK dependencies are not configured.');
  return { config, helper: config.authHelper || path.join(path.dirname(config.runner || packageRoot), 'auth-token.py') };
}

async function withEE(root, input, action) {
  const { config, helper } = configFor(root, input);
  const require = createRequire(path.join(path.resolve(config.dependencies), 'package.json'));
  const ee = require('@google/earthengine');
  const token = execFileSync(config.python || 'python3', [helper], { encoding: 'utf8', timeout: 30000, stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  ee.data.setAuthToken('', 'Bearer', token, 3600, [], undefined, false);
  await new Promise((resolve, reject) => ee.initialize(null, null, resolve, reject, null, config.project));
  return action(ee, config);
}

export async function earthTaskStatus({ root, taskIds = [], project, python, dependencies }) {
  return withEE(root, { project, python, dependencies }, async ee => {
    const ids = Array.isArray(taskIds) ? taskIds.filter(id => typeof id === 'string' && id) : [];
    if (!ids.length) return { status: 'completed', tasks: ee.data.getTaskListWithLimit(100)?.tasks ?? [] };
    return { status: 'completed', tasks: ee.data.getTaskStatus(ids) ?? [] };
  });
}

export async function earthTaskCancel({ root, taskIds = [], project, python, dependencies }) {
  return withEE(root, { project, python, dependencies }, async ee => {
    const ids = Array.isArray(taskIds) ? taskIds.filter(id => typeof id === 'string' && id) : [];
    for (const id of ids) ee.data.cancelTask(id);
    return { status: 'submitted', cancelledTaskIds: ids };
  });
}

export function saveCloudReceipt(root, name, value) {
  const directory = path.join(root, '.earth', 'cloud');
  fs.mkdirSync(directory, { recursive: true });
  const safe = String(name).replace(/[^A-Za-z0-9_.-]/g, '_');
  const file = path.join(directory, `${safe}.json`);
  fs.writeFileSync(file, JSON.stringify(value, null, 2), { mode: 0o600 });
  return path.relative(root, file);
}
