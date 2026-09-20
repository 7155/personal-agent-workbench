import fs from 'node:fs';
import https from 'node:https';
import path from 'node:path';
import { createRequire } from 'node:module';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
const execFileAsync = promisify(execFile);
import { fileURLToPath } from 'node:url';

const packageRoot = path.dirname(fileURLToPath(import.meta.url));

function configFor(root, input = {}) {
  const file = path.join(root, '.earth', 'runtime.json');
  const existing = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : {};
  const config = { ...existing };
  for (const [key, value] of Object.entries(input)) if (value !== undefined && value !== '') config[key] = value;
  if (!config.project) throw new Error('Set an authorized Earth Engine project in .earth/runtime.json.');
  if (!config.dependencies) throw new Error('Earth Engine SDK dependencies are not configured.');
  return { config, helper: config.authHelper || path.join(config.runner ? path.dirname(config.runner) : packageRoot, 'auth-token.py') };
}

async function withEE(root, input, action) {
  const { config, helper } = configFor(root, input);
  const require = createRequire(path.join(path.resolve(config.dependencies), 'package.json'));
  if (process.env.HTTPS_PROXY || process.env.https_proxy) {
    const { HttpsProxyAgent } = require('https-proxy-agent');
    const agent = new HttpsProxyAgent(process.env.HTTPS_PROXY || process.env.https_proxy);
    const original = https.request.bind(https);
    https.request = (options, callback) => original({ ...options, agent }, callback);
  }
  const ee = require('@google/earthengine');
  let token;
  try {
    const result = await execFileAsync(config.python || 'python3', [helper], { encoding: 'utf8', timeout: 30000, maxBuffer: 65536 });
    token = result.stdout.trim();
  } catch { throw new Error('Earth Engine 授权不可用，请检查当前项目的授权与 Python 环境。'); }
  if (!token) throw new Error('Earth Engine 授权未返回令牌。');
  ee.data.setAuthToken('', 'Bearer', token, 3600, [], undefined, false);
  await request(callback => ee.initialize(null, null, () => callback(true), error => callback(null, error), null, config.project));
  return action(ee, config);
}

function request(action) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Earth Engine 请求超时，请刷新任务状态。')), 30000);
    try { action((result, error) => { clearTimeout(timer); error ? reject(new Error(String(error))) : resolve(result); }); }
    catch (error) { clearTimeout(timer); reject(error); }
  });
}

function taskIds(value) {
  if (!Array.isArray(value) || value.length > 100 || value.some(id => typeof id !== 'string' || !/^[A-Za-z0-9_/-]+$/.test(id))) {
    throw new Error('任务 ID 必须是最多 100 个非空标识符。');
  }
  return [...new Set(value)];
}

async function queryStatus({ root, taskIds: requested = [], project, python, dependencies }) {
  const ids = taskIds(requested);
  return withEE(root, { project, python, dependencies }, async (ee, config) => {
    const response = ids.length
      ? await request(callback => ee.data.getTaskStatus(ids, callback))
      : await request(callback => ee.data.getTaskListWithLimit(100, callback));
    const tasks = ids.length ? response : response?.tasks;
    if (!Array.isArray(tasks)) throw new Error('Earth Engine 返回了无效的任务列表。');
    return { status: 'completed', project: config.project, checkedAt: new Date().toISOString(), tasks };
  });
}

async function cancelTasks({ root, taskIds: requested = [], project, python, dependencies }) {
  const ids = taskIds(requested);
  if (!ids.length) throw new Error('请选择要取消的云端任务。');
  return withEE(root, { project, python, dependencies }, async ee => {
    for (const id of ids) await request(callback => ee.data.cancelTask(id, callback));
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

// The SDK keeps global authentication and XMLHttpRequest proxy state. Isolate
// each query in a bounded child so two projects cannot replace each other's auth.
function inWorker(mode, input) {
  return new Promise((resolve, reject) => {
    const child = execFile(process.execPath, [fileURLToPath(import.meta.url), '--cloud-task-worker'],
      { timeout: 75000, maxBuffer: 2 * 1024 * 1024 }, (error, stdout) => {
        let receipt;
        try { receipt = JSON.parse(stdout); } catch { /* Never expose private child stdout/stderr on auth failures. */ }
        if (error || receipt?.error) return reject(new Error(receipt?.error || '云端任务请求未完成，请检查网络与授权后刷新。'));
        resolve(receipt);
      });
    child.stdin.on('error', () => {});
    child.stdin.end(JSON.stringify({mode,input}));
  });
}
export async function earthTaskStatus(input) {
  taskIds(input.taskIds ?? []);
  configFor(input.root, input);
  return inWorker('status', input);
}
export async function earthTaskCancel(input) {
  if (!taskIds(input.taskIds ?? []).length) throw new Error('请选择要取消的云端任务。');
  configFor(input.root, input);
  return inWorker('cancel', input);
}
if (process.argv[1] === fileURLToPath(import.meta.url) && process.argv[2] === '--cloud-task-worker') {
  try {
    const {mode,input} = JSON.parse(fs.readFileSync(0,'utf8'));
    const result = await (mode === 'cancel' ? cancelTasks(input) : queryStatus(input));
    process.stdout.write(JSON.stringify(result));
    process.exit(0);
  } catch (error) {
    process.stdout.write(JSON.stringify({error:String(error?.message || 'Cloud task request failed').slice(0,2000)}));
    process.exit(1);
  }
}
