import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFile } from 'node:child_process';

const DEFAULT_CANDIDATES = [
  '/opt/homebrew/bin/qgis_process',
  '/usr/local/bin/qgis_process',
  '/usr/bin/qgis_process',
  '/Applications/QGIS.app/Contents/MacOS/bin/qgis_process',
  path.join(os.homedir(), 'Applications/QGIS.app/Contents/MacOS/bin/qgis_process'),
].filter(Boolean);

const MAX_JSON_BYTES = 256_000;
const DEFAULT_TIMEOUT_MS = 30_000;

function executableFile(value) {
  if (typeof value !== 'string' || !value || value.includes('\0')) return false;
  try {
    fs.accessSync(value, fs.constants.X_OK);
    return fs.statSync(value).isFile();
  } catch {
    return false;
  }
}

export function findQGISProcess({ candidates } = {}) {
  const discovered = candidates ?? [process.env.PAW_QGIS_PROCESS, ...DEFAULT_CANDIDATES, ...(process.env.PATH || '').split(path.delimiter).filter(Boolean).map(directory => path.join(directory, 'qgis_process'))];
  const values = Array.isArray(discovered) ? discovered : [discovered];
  return values.find(executableFile) || null;
}

function qgisExecutable(executable) {
  const selected = executable || findQGISProcess();
  if (!selected) {
    const error = new Error('qgis_process is not installed or configured; set PAW_QGIS_PROCESS before using QGIS Processing.');
    error.code = 'qgis_unavailable';
    throw error;
  }
  if (!executableFile(selected)) {
    const error = new Error(`qgis_process is not executable: ${selected}`);
    error.code = 'qgis_unavailable';
    throw error;
  }
  return selected;
}

function parseJSONOutput(stdout) {
  const text = String(stdout || '').trim();
  try { return JSON.parse(text); } catch { /* Providers may print informational lines first. */ }
  const lines = text.split(/\r?\n/);
  for (let index = 0; index < lines.length; index += 1) {
    if (!lines[index].startsWith('{') && !lines[index].startsWith('[')) continue;
    try { return JSON.parse(lines.slice(index).join('\n')); } catch { /* Keep searching for the complete top-level JSON. */ }
  }
  const error = new Error('qgis_process returned no JSON result');
  error.code = 'qgis_invalid_json';
  throw error;
}

function runQGISProcess(executable, args, { input, root, timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
  let cwd;
  if (root !== undefined) {
    if (typeof root !== 'string' || !path.isAbsolute(root) || !fs.statSync(root).isDirectory()) {
      throw Object.assign(new TypeError('QGIS workspace root must be an existing absolute directory'), { code: 'qgis_invalid_root' });
    }
    cwd = fs.realpathSync(root);
  }
  const serialized = input === undefined ? undefined : JSON.stringify(input, (_key, value) => {
    if (typeof value === 'number' && !Number.isFinite(value) || ['bigint', 'function', 'symbol', 'undefined'].includes(typeof value)) {
      throw Object.assign(new TypeError('QGIS inputs must contain finite JSON values'), { code: 'qgis_invalid_inputs' });
    }
    return value;
  });
  if (serialized && Buffer.byteLength(serialized, 'utf8') > MAX_JSON_BYTES) {
    const error = new Error(`QGIS request exceeds ${MAX_JSON_BYTES} bytes`);
    error.code = 'qgis_request_too_large';
    return Promise.reject(error);
  }
  return new Promise((resolve, reject) => {
    const child = execFile(executable, args, {
      cwd,
      env: { ...process.env, QT_QPA_PLATFORM: process.env.QT_QPA_PLATFORM || 'offscreen' },
      timeout: Math.min(Math.max(Number(timeoutMs) || DEFAULT_TIMEOUT_MS, 1_000), 120_000),
      maxBuffer: 8 * 1024 * 1024,
      windowsHide: true,
    }, (error, stdout, stderr) => {
      let result;
      try { result = parseJSONOutput(stdout); } catch (parseError) {
        const detail = String(stderr || error?.message || parseError.message || parseError);
        const failure = new Error(`qgis_process failed: ${detail}`);
        failure.code = error?.code || parseError.code || 'qgis_failed';
        reject(failure);
        return;
      }
      if (error) {
        const failure = new Error(String(stderr || error.message || 'qgis_process failed'));
        failure.code = error.code || 'qgis_failed';
        failure.result = result;
        reject(failure);
        return;
      }
      resolve(result);
    });
    child.stdin.on('error', () => {});
    child.stdin.end(serialized);
  });
}

export async function listQGISAlgorithms({ executable, root, timeoutMs } = {}) {
  const selected = qgisExecutable(executable);
  const command = ['--json', 'list'];
  const result = await runQGISProcess(selected, command, { root, timeoutMs });
  return { status: 'completed', backend: 'qgis', executable: selected, command, result };
}

export async function helpQGISAlgorithm({ executable, root, algorithm, timeoutMs } = {}) {
  if (typeof algorithm !== 'string' || !/^[A-Za-z0-9_.:-]{1,160}$/.test(algorithm)) {
    const error = new TypeError('QGIS algorithm id is invalid');
    error.code = 'qgis_invalid_algorithm';
    throw error;
  }
  const selected = qgisExecutable(executable);
  const command = ['--json', 'help', algorithm];
  const result = await runQGISProcess(selected, command, { root, timeoutMs });
  return { status: 'completed', backend: 'qgis', executable: selected, command, algorithm, result };
}

export async function runQGISAlgorithm({ executable, root, algorithm, inputs = {}, timeoutMs } = {}) {
  if (typeof algorithm !== 'string' || !/^[A-Za-z0-9_.:-]{1,160}$/.test(algorithm)) {
    const error = new TypeError('QGIS algorithm id is invalid');
    error.code = 'qgis_invalid_algorithm';
    throw error;
  }
  if (!inputs || typeof inputs !== 'object' || Array.isArray(inputs)) {
    const error = new TypeError('QGIS algorithm inputs must be an object');
    error.code = 'qgis_invalid_inputs';
    throw error;
  }
  const selected = qgisExecutable(executable);
  const command = ['--json', 'run', algorithm, '-'];
  const result = await runQGISProcess(selected, command, { input: { inputs }, root, timeoutMs });
  return { status: 'completed', backend: 'qgis', executable: selected, command, algorithm, inputs, result };
}

export function qgisBackendStatus(options = {}) {
  const executable = findQGISProcess(options);
  return {
    id: 'qgis',
    available: Boolean(executable),
    executable,
    nativeTested: false,
    status: executable ? 'installed_unverified' : 'unavailable',
    role: 'optional QGIS Processing provider; configure PAW_QGIS_PROCESS before enabling',
  };
}
