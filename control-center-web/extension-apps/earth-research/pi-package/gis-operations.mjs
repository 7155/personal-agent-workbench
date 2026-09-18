import fs from 'node:fs';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';

const catalogPath = path.join(path.dirname(fileURLToPath(import.meta.url)), 'gis-catalog.json');
const catalog = JSON.parse(fs.readFileSync(catalogPath, 'utf8'));
const operationIds = new Set(catalog.flatMap(group => group.ops.map(operation => operation.op)));
const supportedExtensions = new Set(['.geojson', '.json', '.gpkg', '.shp', '.sqlite', '.kml', '.tif', '.tiff', '.img']);
const vectorExtensions = new Set(['.geojson', '.json', '.gpkg', '.shp', '.sqlite', '.kml']);
const rasterExtensions = new Set(['.tif', '.tiff', '.img']);

export const GIS_CATALOG = catalog;
export const GIS_OPERATION_IDS = operationIds;
export const GIS_SCHEMA_VERSION = 'earth.gis-workspace.v1';

function atomicWrite(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temporary = `${file}.${randomUUID()}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(value, null, 2), { mode: 0o600 });
  fs.renameSync(temporary, file);
}

function safeRoot(root) {
  if (typeof root !== 'string' || !path.isAbsolute(root) || root.includes('\0')) throw new TypeError('GIS workspace root must be an absolute path');
  return fs.realpathSync(root);
}

function safeRelative(root, value, { allowMissing = false } = {}) {
  if (typeof value !== 'string' || !value || value.includes('\0') || path.isAbsolute(value)) throw new TypeError('GIS path must be relative to the workspace');
  const target = path.resolve(root, value);
  const relative = path.relative(root, target);
  if (!relative || relative.startsWith(`..${path.sep}`) || relative === '..') throw new TypeError('GIS path escapes the workspace');
  if (!allowMissing && !fs.existsSync(target)) throw new Error(`GIS path does not exist: ${value}`);
  return target;
}

export function prepareGISWorkspace(root, { version = '0.8.0', python = '' } = {}) {
  const resolvedRoot = safeRoot(root);
  const gisRoot = safeRelative(resolvedRoot, '.earth/gis', { allowMissing: true });
  const adapterRoot = safeRelative(resolvedRoot, `.earth/gis-adapter/${version}`, { allowMissing: true });
  fs.mkdirSync(gisRoot, { recursive: true });
  fs.mkdirSync(adapterRoot, { recursive: true });
  for (const name of ['gis-runner.py', 'gis-catalog.json', 'vendor/gisclaw-geo-ops.py']) {
    const source = path.join(path.dirname(fileURLToPath(import.meta.url)), name);
    const target = path.join(adapterRoot, name);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    if (fs.existsSync(target) && fs.lstatSync(target).isSymbolicLink()) throw new Error(`GIS adapter file cannot be a symlink: ${name}`);
    fs.copyFileSync(source, target);
  }
  const configPath = path.join(gisRoot, 'runtime.json');
  const config = fs.existsSync(configPath) ? JSON.parse(fs.readFileSync(configPath, 'utf8')) : {};
  if (configPath && fs.existsSync(configPath) && fs.lstatSync(configPath).isSymbolicLink()) throw new Error('GIS runtime configuration cannot be a symlink');
  const selectedPython = String(config.python || python || process.env.PAW_EARTH_GIS_PYTHON || 'python3');
  const next = { schemaVersion: 'earth.gis-runtime.v1', ...config, python: selectedPython, runner: path.join(adapterRoot, 'gis-runner.py') };
  atomicWrite(configPath, next);
  return { root: resolvedRoot, gisRoot, adapter: adapterRoot, runtime: configPath, python: selectedPython, runner: next.runner, catalog: GIS_CATALOG };
}

export function listGISFiles(root, directory = 'data') {
  const resolvedRoot = safeRoot(root);
  const base = safeRelative(resolvedRoot, directory);
  const items = [];
  const walk = current => {
    for (const entry of fs.readdirSync(current, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
      if (entry.name.startsWith('.')) continue;
      const full = path.join(current, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (supportedExtensions.has(path.extname(entry.name).toLowerCase())) {
        const relativePath = path.relative(resolvedRoot, full);
        const extension = path.extname(entry.name).toLowerCase();
        items.push({ path: relativePath, name: entry.name, bytes: fs.statSync(full).size, kind: vectorExtensions.has(extension) ? 'vector' : rasterExtensions.has(extension) ? 'raster' : 'other' });
      }
    }
  };
  walk(base);
  return items;
}

function parseRunnerOutput(stdout) {
  const lines = String(stdout || '').trim().split(/\r?\n/).reverse();
  for (const line of lines) {
    if (!line.trim().startsWith('{')) continue;
    try { return JSON.parse(line); } catch { /* keep searching */ }
  }
  throw new Error('GIS runner returned no JSON receipt');
}

function executeRunner(python, runner, request, cwd) {
  return new Promise((resolve, reject) => {
    const child = execFile(python, [runner], { cwd, timeout: 300_000, maxBuffer: 32 * 1024 * 1024 }, (error, stdout, stderr) => {
      let parsed;
      try { parsed = parseRunnerOutput(stdout); } catch (parseError) {
        reject(error ?? parseError);
        return;
      }
      if (error && parsed.status !== 'failed') parsed = { ...parsed, status: 'failed', error: String(stderr || error.message || error) };
      resolve(parsed);
    });
    child.stdin.on('error', () => {});
    child.stdin.end(JSON.stringify(request));
  });
}

function relativeResultPaths(root, result) {
  const normalize = value => {
    if (typeof value !== 'string' || !path.isAbsolute(value)) return value;
    const relative = path.relative(root, value);
    if (relative.startsWith('..')) return undefined;
    return relative;
  };
  if (!result || typeof result !== 'object') return result;
  return {
    ...result,
    outputs: Array.isArray(result.outputs) ? result.outputs.map(output => ({
      ...output,
      path: normalize(output.path),
    })) : result.outputs,
  };
}

export async function runGISOperation({ root, python, request }) {
  const resolvedRoot = safeRoot(root);
  const prepared = prepareGISWorkspace(resolvedRoot, { python });
  const runId = randomUUID();
  const runRelative = `.earth/gis/runs/${runId}`;
  const runDir = safeRelative(resolvedRoot, runRelative, { allowMissing: true });
  fs.mkdirSync(runDir, { recursive: true });
  const base = { schemaVersion: GIS_SCHEMA_VERSION, runId, startedAt: new Date().toISOString(), status: 'running', op: request.op ?? null };
  atomicWrite(path.join(runDir, 'run.json'), base);
  let result;
  try {
    result = await executeRunner(prepared.python, prepared.runner, { operation: 'process', root: resolvedRoot, runDir, ...request }, resolvedRoot);
  } catch (error) {
    result = { status: 'failed', code: 'runner_failed', error: error instanceof Error ? error.message : String(error) };
  }
  const normalized = relativeResultPaths(resolvedRoot, { ...result, runId, startedAt: base.startedAt, updatedAt: new Date().toISOString() });
  atomicWrite(path.join(runDir, 'run.json'), normalized);
  atomicWrite(path.join(resolvedRoot, '.earth/gis/workspace.json'), normalized);
  return normalized;
}

export async function inspectGISPath({ root, python, path: inputPath }) {
  const resolvedRoot = safeRoot(root);
  const prepared = prepareGISWorkspace(resolvedRoot, { python });
  const result = await executeRunner(prepared.python, prepared.runner, { operation: 'inspect', root: resolvedRoot, path: inputPath }, resolvedRoot);
  if (result.status === 'failed') throw new Error(result.error || 'GIS inspect failed');
  return result.result;
}
