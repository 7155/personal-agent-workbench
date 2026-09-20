import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createHash, randomUUID } from 'node:crypto';
import { snapshotGISInputs } from './gis-delivery.mjs';
export { createGISBundle, verifyGISBundle, compareGISRuns, listGISRuns, readGISRun } from './gis-delivery.mjs';
import { findQGISProcess, helpQGISAlgorithm, listQGISAlgorithms, qgisBackendStatus, runQGISAlgorithm } from './qgis.mjs';

const catalogPath = path.join(path.dirname(fileURLToPath(import.meta.url)), 'gis-catalog.json');
const catalog = JSON.parse(fs.readFileSync(catalogPath, 'utf8'));
const operationIds = new Set(catalog.flatMap(group => group.ops.map(operation => operation.op)));
const supportedExtensions = new Set(['.geojson', '.json', '.gpkg', '.shp', '.sqlite', '.kml', '.tif', '.tiff', '.img']);
const vectorExtensions = new Set(['.geojson', '.json', '.gpkg', '.shp', '.sqlite', '.kml']);
const rasterExtensions = new Set(['.tif', '.tiff', '.img']);
const spatialDatabaseExtensions = new Set(['.gpkg', '.sqlite', '.db']);
const spatialKinds = new Set(['geopackage', 'spatialite', 'postgis']);

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
  const managedPython = path.join(os.homedir(), 'Library', 'Application Support', 'RagIme', 'EarthGISRuntime', '.venv', 'bin', 'python');
  const selectedPython = String(config.python || python || process.env.PAW_EARTH_GIS_PYTHON || (fs.existsSync(managedPython) ? managedPython : 'python3'));
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

export function listGISBackends() {
  return { schemaVersion: 'earth.gis-backends.v1', default: 'geopandas', backends: [{ id: 'geopandas', available: true, nativeTested: false, status: 'runtime_unverified', role: 'default deterministic adapter; Python dependencies are checked when invoked' }, qgisBackendStatus()] };
}

function parseRunnerOutput(stdout) {
  const lines = String(stdout || '').trim().split(/\r?\n/).reverse();
  for (const line of lines) {
    if (!line.trim().startsWith('{')) continue;
    try { return JSON.parse(line); } catch { /* keep searching */ }
  }
  throw new Error('GIS runner returned no JSON receipt');
}

function executeRunner(python, runner, request, cwd, { timeout = 300_000 } = {}) {
  return new Promise((resolve, reject) => {
    const child = execFile(python, [runner], { cwd, timeout, maxBuffer: 32 * 1024 * 1024, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } }, (error, stdout, stderr) => {
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
  let inputVersions = [];
  try {
    const snapshot = snapshotGISInputs(resolvedRoot, runDir, request.inputs);
    inputVersions = snapshot.versions;
    result = await executeRunner(prepared.python, prepared.runner, { ...request, inputs: snapshot.bindings, operation: 'process', root: resolvedRoot, runDir }, resolvedRoot);
  } catch (error) {
    result = { status: 'failed', code: 'runner_failed', error: error instanceof Error ? error.message : String(error) };
  }
  const normalized = relativeResultPaths(resolvedRoot, { ...result, runId, inputVersions, params: request.params ?? {}, startedAt: base.startedAt, updatedAt: new Date().toISOString() });
  for (const output of normalized.outputs ?? []) {
    if (output.path) output.sha256 = createHash('sha256').update(fs.readFileSync(safeRelative(resolvedRoot, output.path))).digest('hex');
  }
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

export async function queryGISPixel({ root, python, path: inputPath, longitude, latitude, band }) {
  if (!Number.isFinite(longitude) || !Number.isFinite(latitude) || Math.abs(longitude) > 180 || Math.abs(latitude) > 90) throw new TypeError('Pixel query requires finite WGS84 longitude and latitude.');
  const resolvedRoot = safeRoot(root);
  const prepared = prepareGISWorkspace(resolvedRoot, { python });
  const result = await executeRunner(prepared.python, prepared.runner, { operation: 'pixel', root: resolvedRoot, path: inputPath, longitude, latitude, band }, resolvedRoot);
  if (result.status === 'failed') throw Object.assign(new Error(result.error || 'GIS pixel query failed'), { code: result.code });
  return result;
}

export async function queryGISRegion({ root, python, path: inputPath, geometry, band, allTouched }) {
  const resolvedRoot = safeRoot(root);
  const prepared = prepareGISWorkspace(resolvedRoot, { python });
  const result = await executeRunner(prepared.python, prepared.runner, { operation: 'region', root: resolvedRoot, path: inputPath, geometry, band, allTouched }, resolvedRoot);
  if (result.status === 'failed') throw Object.assign(new Error(result.error || 'GIS region query failed'), { code: result.code });
  return result;
}

function spatialCatalogPath(root) {
  const resolvedRoot = safeRoot(root);
  const directory = safeRelative(resolvedRoot, '.earth/gis', { allowMissing: true });
  fs.mkdirSync(directory, { recursive: true });
  const file = path.join(directory, 'databases.json');
  if (fs.existsSync(file) && fs.lstatSync(file).isSymbolicLink()) throw new Error('Spatial database catalog cannot be a symlink.');
  return file;
}

function readSpatialCatalog(root) {
  const file = spatialCatalogPath(root);
  if (!fs.existsSync(file)) return { schemaVersion: 'earth.spatial-catalog.v1', sources: [] };
  const value = JSON.parse(fs.readFileSync(file, 'utf8'));
  if (!value || value.schemaVersion !== 'earth.spatial-catalog.v1' || !Array.isArray(value.sources)) throw new Error('Spatial database catalog is invalid.');
  return value;
}

export function listSpatialSources({ root }) {
  return readSpatialCatalog(root);
}

function displayName(value) {
  const name = String(value || '').trim();
  if (!name || [...name].length > 80 || /[\u0000-\u001f\u007f\\/]/u.test(name)) throw new TypeError('Spatial source name is invalid.');
  return name;
}

function sourceIdentity({ kind, path: sourcePath, schema, table, secretReference }) {
  return [kind, sourcePath, schema, table, secretReference].map(value => String(value || '')).join('\u001f');
}

function generatedSourceId(identity) {
  return `spatial:${createHash('sha256').update(identity).digest('hex').slice(0, 32)}`;
}

function requestedSourceId(source) {
  const value = source?.sourceId ?? source?.id;
  if (value === undefined || value === null || String(value).trim() === '') return '';
  const id = String(value).trim();
  if (!/^[A-Za-z0-9:_-]{1,128}$/.test(id)) throw new TypeError('Spatial source id is invalid.');
  return id;
}

const postgisFailureStatuses = {
  postgis_secret_missing: 'missing_secret', postgis_dependency_missing: 'dependency_missing',
  postgis_connection_failed: 'connection_failed', postgis_authentication_failed: 'authentication_failed',
  postgis_permission_denied: 'permission_denied', postgis_extension_missing: 'postgis_missing',
  postgis_database_missing: 'database_missing', postgis_query_timeout: 'query_timeout',
  postgis_runner_failed: 'runtime_unavailable',
};

const postgisErrors = {
  postgis_secret_missing: 'The PostGIS connection environment variable is not configured in this runtime.',
  postgis_dependency_missing: 'PostGIS requires psycopg[binary] or psycopg2 in the configured Earth GIS Python runtime.',
  postgis_connection_failed: 'PostGIS connection failed. Check the referenced configuration and server availability.',
  postgis_authentication_failed: 'PostGIS authentication failed. Check the referenced connection credentials.',
  postgis_permission_denied: 'The PostGIS account cannot read the selected resource.',
  postgis_extension_missing: 'The selected database does not have the PostGIS extension enabled.',
  postgis_database_missing: 'The referenced PostgreSQL database does not exist.',
  postgis_query_timeout: 'PostGIS read exceeded the query time limit. Select a smaller table or view.',
  postgis_runner_failed: 'PostGIS runtime could not complete the read. Check the configured Python runtime and retry.',
  postgis_query_failed: 'PostGIS could not read the selected spatial resource. Check its geometry, CRS and read permissions.',
  postgis_invalid_identifier: 'PostGIS schema/table identifier is invalid.',
  postgis_invalid_secret_reference: 'PostGIS requires an environment secret reference.',
  postgis_feature_limit: 'PostGIS layer exceeds the 10,000 feature read limit. Select a smaller table or view.',
  postgis_byte_limit: 'PostGIS layer exceeds the 32 MiB read limit. Select a smaller table or view.',
  postgis_feature_byte_limit: 'A PostGIS feature exceeds the 2 MiB read limit. Select a view with fewer attributes or simpler geometry.',
  postgis_invalid_geometry: 'PostGIS returned geometry that cannot be represented as finite WGS84 GeoJSON.',
  postgis_mixed_srid: 'The PostGIS layer contains mixed SRIDs; use a view with one explicit CRS.',
  postgis_read_only_violation: 'The selected PostGIS resource attempted a write. Only read-only tables and views are supported.',
  layer_not_found: 'The selected PostGIS layer is absent or is not readable; reconnect to refresh its catalog.',
  unknown_crs: 'The PostGIS layer has no known source SRID.',
  invalid_output: 'Loaded PostGIS output must be a GeoJSON path.',
};

async function executePostGIS(prepared, request) {
  try {
    const result = await executeRunner(prepared.python, prepared.runner, { ...request, root: prepared.root }, prepared.root, { timeout: 60_000 });
    if (result.status === 'failed') {
      const code = Object.hasOwn(postgisErrors, result.code) ? result.code : 'postgis_runner_failed';
      return { status: 'failed', code, error: postgisErrors[code] };
    }
    return result;
  } catch {
    // Driver/child-process errors may embed a DSN, username or server address.
    // Only the runner's bounded, redacted receipt may cross this boundary.
    return { status: 'failed', code: 'postgis_runner_failed', error: postgisErrors.postgis_runner_failed };
  }
}

export async function connectSpatialSource({ root, python, source }) {
  const resolvedRoot = safeRoot(root);
  if (!source || typeof source !== 'object') throw new TypeError('Spatial source must be an object.');
  const name = displayName(source.name);
  const kind = String(source.kind || '').trim().toLowerCase();
  if (!spatialKinds.has(kind)) throw new TypeError(`Unsupported spatial source kind: ${kind}`);
  const now = new Date().toISOString();
  let status = 'ready';
  let layers = [];
  let probe = {};
  let relativePath = '';
  const schema = String(source.schema || '').trim();
  const table = String(source.table || '').trim();
  const secretReference = kind === 'postgis' ? String(source.secretReference || '').trim() : '';
  if (kind !== 'postgis') {
    relativePath = String(source.path || '');
    const target = safeRelative(resolvedRoot, relativePath);
    if (!spatialDatabaseExtensions.has(path.extname(target).toLowerCase())) throw new Error('GeoPackage/SpatiaLite source must be .gpkg, .sqlite or .db.');
    relativePath = path.relative(resolvedRoot, target);
    const prepared = prepareGISWorkspace(resolvedRoot, { python });
    const result = await executeRunner(prepared.python, prepared.runner, { operation: 'catalog_source', root: resolvedRoot, path: relativePath }, resolvedRoot);
    if (result.status === 'failed') throw new Error(result.error || 'Spatial source catalog failed.');
    layers = Array.isArray(result.layers) ? result.layers : [];
    status = 'ready';
  } else {
    if (!/^[A-Z][A-Z0-9_]{2,127}$/.test(secretReference)) throw new TypeError('PostGIS requires an environment secret reference such as PAW_POSTGIS_URL.');
    if (['url', 'connectionString', 'password', 'dsn', 'username', 'user', 'host', 'port', 'database'].some(key => source[key] !== undefined)) throw new Error('PostGIS credentials must stay in the referenced secret; do not send connection values in the workspace request.');
    if (source.readOnly === false) throw new Error('PostGIS connections support read-only access.');
    for (const identifier of [schema, table]) {
      if (identifier.includes('\0') || Buffer.byteLength(identifier, 'utf8') > 63) throw new TypeError('PostGIS schema/table identifier is invalid.');
    }
    const prepared = prepareGISWorkspace(resolvedRoot, { python });
    const result = await executePostGIS(prepared, { operation: 'catalog_postgis', secretReference, schema, table });
    if (result.status === 'failed') {
      status = postgisFailureStatuses[result.code] || 'query_failed';
      probe = { code: result.code, error: result.error };
    } else {
      layers = result.layers || [];
      probe = { layerDetails: result.layerDetails, postgisVersion: result.postgisVersion, catalogTruncated: result.catalogTruncated, featureLimit: result.featureLimit };
    }
  }
  const catalog = readSpatialCatalog(resolvedRoot);
  const identity = sourceIdentity({ kind, path: relativePath, schema, table, secretReference });
  const requestedId = requestedSourceId(source);
  const existing = catalog.sources.find(item => requestedId && item.id === requestedId)
    || catalog.sources.find(item => sourceIdentity(item) === identity);
  const record = {
    id: existing?.id || requestedId || generatedSourceId(identity),
    name,
    kind,
    path: relativePath,
    schema,
    table,
    secretReference,
    readOnly: source.readOnly !== false,
    status,
    layers,
    ...probe,
    updatedAt: now,
  };
  const next = { schemaVersion: 'earth.spatial-catalog.v1', updatedAt: now, sources: [...catalog.sources.filter(item => item.id !== record.id), record] };
  atomicWrite(spatialCatalogPath(resolvedRoot), next);
  return record;
}

function layerOutputPath(source, layer) {
  const layerHash = createHash('sha256').update(`${source.id}\u0000${layer}`).digest('hex').slice(0, 20);
  return `.earth/gis/layers/${encodeURIComponent(source.id)}/${layerHash}.geojson`;
}

export async function loadSpatialLayer({ root, python, sourceId, layer }) {
  const resolvedRoot = safeRoot(root);
  const id = String(sourceId || '').trim();
  if (!id || !/^[A-Za-z0-9:_-]{1,128}$/.test(id)) throw new TypeError('Spatial source id is invalid.');
  if (typeof layer !== 'string' || !layer || layer.includes('\0')) throw new TypeError('Spatial layer name is invalid.');
  const catalog = readSpatialCatalog(resolvedRoot);
  const source = catalog.sources.find(item => item.id === id);
  if (!source) throw new Error(`Spatial source not found: ${id}`);
  if (source.status !== 'ready') throw Object.assign(new Error(source.error || 'Spatial source is not ready; reconnect it before loading a layer.'), { code: source.code || 'source_not_ready' });
  const output = layerOutputPath(source, layer);
  const lineage = {
    sourceId: source.id,
    sourceName: source.name,
    sourceKind: source.kind,
    sourcePath: source.path,
    layer,
  };
  const prepared = prepareGISWorkspace(resolvedRoot, { python });
  const result = source.kind === 'postgis' ? await executePostGIS(prepared, {
    operation: 'load_postgis', secretReference: source.secretReference,
    schema: source.schema, table: source.table, layer, output, sourceLineage: lineage,
  }) : await executeRunner(prepared.python, prepared.runner, {
    operation: 'load_source',
    root: resolvedRoot,
    path: source.path,
    layer,
    output,
    sourceLineage: lineage,
  }, resolvedRoot);
  if (result.status === 'failed') throw Object.assign(new Error(result.error || 'Spatial layer load failed.'), { code: result.code });
  const relativePath = typeof result.path === 'string' && path.isAbsolute(result.path) ? path.relative(resolvedRoot, result.path) : result.path || output;
  return {
    ...result,
    status: 'completed',
    path: relativePath,
    layer,
    sourceId: source.id,
    sourceLineage: result.sourceLineage || lineage,
    crs: 'EPSG:4326',
  };
}

export { findQGISProcess, listQGISAlgorithms, helpQGISAlgorithm, runQGISAlgorithm };

export async function exportGISLayer({ root, python, request }) {
  const resolvedRoot = safeRoot(root);
  const prepared = prepareGISWorkspace(resolvedRoot, { python });
  const runId = randomUUID();
  const runRelative = `.earth/gis/runs/${runId}`;
  const runDir = safeRelative(resolvedRoot, runRelative, { allowMissing: true });
  fs.mkdirSync(runDir, { recursive: true });
  const context = {
    scope: request.scope ?? 'all',
    selectedFeatureIds: request.scope === 'selected' && Array.isArray(request.featureIds) ? request.featureIds : [],
    layerId: request.layerId ?? null,
    revision: request.revision ?? null,
  };
  const base = { schemaVersion: GIS_SCHEMA_VERSION, runId, startedAt: new Date().toISOString(), status: 'running', op: 'export', ...context };
  atomicWrite(path.join(runDir, 'run.json'), base);
  let result;
  try {
    result = await executeRunner(prepared.python, prepared.runner, { ...request, operation: 'export', root: resolvedRoot, runDir }, resolvedRoot);
  } catch (error) {
    result = { status: 'failed', code: 'runner_failed', error: error instanceof Error ? error.message : String(error) };
  }
  const normalized = relativeResultPaths(resolvedRoot, { ...context, ...result, op: 'export', runId, startedAt: base.startedAt, updatedAt: new Date().toISOString() });
  for (const output of normalized.outputs ?? []) {
    if (output.path) output.sha256 = createHash('sha256').update(fs.readFileSync(safeRelative(resolvedRoot, output.path))).digest('hex');
  }
  atomicWrite(path.join(runDir, 'run.json'), normalized);
  atomicWrite(path.join(resolvedRoot, '.earth/gis/workspace.json'), normalized);
  return normalized;
}
