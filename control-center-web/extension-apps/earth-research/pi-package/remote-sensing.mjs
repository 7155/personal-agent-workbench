import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const kinds = new Set(['classification', 'ndvi', 'change', 'animation', 'research']);
const localKinds = new Set(['classification', 'ndvi']);
const runner = path.join(path.dirname(fileURLToPath(import.meta.url)), 'remote-sensing-runner.py');
const idPattern = /^[a-f0-9-]{36}$/;

function workspace(root) {
  if (typeof root !== 'string' || !path.isAbsolute(root)) throw new TypeError('An absolute workspace root is required.');
  return fs.realpathSync(root);
}

function inside(root, relative, allowMissing = false) {
  if (typeof relative !== 'string' || !relative || path.isAbsolute(relative) || relative.includes('\0')) throw new TypeError('Remote-sensing paths must be workspace-relative.');
  const target = path.resolve(root, relative);
  if (!target.startsWith(root + path.sep)) throw new Error('Remote-sensing path escaped the workspace.');
  let ancestor = target;
  while (!fs.existsSync(ancestor)) {
    if (!allowMissing) throw new Error(`Workspace input is missing: ${relative}`);
    ancestor = path.dirname(ancestor);
  }
  const actual = fs.realpathSync(ancestor);
  if (actual !== root && !actual.startsWith(root + path.sep)) throw new Error('Remote-sensing path resolves outside the workspace.');
  return target;
}

function write(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temporary = `${file}.${randomUUID()}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(value, null, 2), { flag: 'wx', mode: 0o600 });
  fs.renameSync(temporary, file);
}

function hashFile(file) {
  const hash = createHash('sha256'), buffer = Buffer.allocUnsafe(1024 * 1024), descriptor = fs.openSync(file, 'r');
  try {
    let count;
    while ((count = fs.readSync(descriptor, buffer, 0, buffer.length, null)) > 0) hash.update(buffer.subarray(0, count));
    return hash.digest('hex');
  } finally { fs.closeSync(descriptor); }
}

function validateRegion(region) {
  if (!region || !['Polygon', 'MultiPolygon'].includes(region.type) || !Array.isArray(region.coordinates)) throw new TypeError('Choose a Polygon or MultiPolygon region in WGS84.');
  const polygons = region.type === 'Polygon' ? [region.coordinates] : region.coordinates;
  let count = 0;
  if (!polygons.length) throw new TypeError('Region must contain a polygon.');
  for (const polygon of polygons) {
    if (!Array.isArray(polygon) || !polygon.length) throw new TypeError('Region polygon is empty.');
    for (const ring of polygon) {
      if (!Array.isArray(ring) || ring.length < 4) throw new TypeError('Region rings need at least four positions.');
      for (const position of ring) {
        if (!Array.isArray(position) || position.length < 2 || !position.every(Number.isFinite) || Math.abs(position[0]) > 180 || Math.abs(position[1]) > 90) throw new TypeError('Region coordinates must be valid WGS84 positions.');
        if (++count > 20_000) throw new TypeError('Region exceeds 20,000 vertices.');
      }
      if (ring[0][0] !== ring.at(-1)[0] || ring[0][1] !== ring.at(-1)[1]) throw new TypeError('Region rings must be closed.');
    }
  }
}

function bandReference(value) {
  return (Number.isInteger(value) && value > 0 && value <= 256) || (typeof value === 'string' && value.trim().length > 0 && value.length < 128);
}

export async function prepareRemoteSensingWorkflow({ root, plan }) {
  root = workspace(root);
  if (!plan || !kinds.has(plan.kind)) throw new TypeError('Unsupported remote-sensing workflow kind.');
  validateRegion(plan.region);
  const normalized = structuredClone(plan);
  for (const field of ['classField', 'groupField', 'collection', 'sampleLayerId', 'sampleLayer']) {
    if (normalized[field] !== undefined && (typeof normalized[field] !== 'string' || !normalized[field].trim() || normalized[field].length > 300)) throw new TypeError(`Invalid ${field}.`);
  }
  for (const field of ['dateFrom', 'dateTo']) {
    if (normalized[field] !== undefined && (!/^\d{4}-\d{2}-\d{2}$/.test(normalized[field]) || !Number.isFinite(Date.parse(normalized[field])))) throw new TypeError(`Invalid ${field}.`);
  }
  if (normalized.dateFrom && normalized.dateTo && normalized.dateFrom > normalized.dateTo) throw new TypeError('dateFrom must not follow dateTo.');
  if (normalized.scale !== undefined && (!Number.isFinite(normalized.scale) || normalized.scale <= 0)) throw new TypeError('Scale must be positive.');
  if (normalized.bands !== undefined) {
    const references = Array.isArray(normalized.bands) ? normalized.bands : normalized.bands && typeof normalized.bands === 'object' ? [normalized.bands.red, normalized.bands.nir] : [];
    if (!references.length || references.length > 32 || !references.every(bandReference)) throw new TypeError('Bands must be one-based indexes or exact raster band descriptions.');
    if (plan.kind === 'classification' && !Array.isArray(normalized.bands)) throw new TypeError('Classification bands must be an array.');
    if (plan.kind === 'ndvi' && references.length !== 2) throw new TypeError('NDVI requires exactly red and near-infrared bands.');
  }
  const requirements = [], inputVersions = [];
  const require = (key, label, provided, detail) => {
    const existing = requirements.find(item => item.key === key);
    if (!existing) requirements.push({ key, label, status: provided ? 'provided' : 'missing', detail });
  };
  if (normalized.sampleLayerId) {
    const catalogPath = inside(root, '.earth/layers/catalog.json', true);
    const catalog = fs.existsSync(catalogPath) ? JSON.parse(fs.readFileSync(catalogPath, 'utf8')) : { layers: [] };
    const matches = catalog.layers?.filter(layer => layer.id === normalized.sampleLayerId) ?? [];
    if (matches.length !== 1) require('sampleLayerId', 'Saved sample layer', false, 'The sample layer must exist uniquely in the project catalog.');
    else {
      if (normalized.samplePath && normalized.samplePath !== matches[0].path) throw new Error('samplePath does not match the selected saved layer.');
      normalized.samplePath = matches[0].path;
      normalized.sampleRevision = matches[0].revision ?? 1;
    }
  }
  for (const [field, role] of [['imagePath', 'image'], ['samplePath', 'samples']]) {
    if (!normalized[field]) continue;
    const file = inside(root, normalized[field], true);
    const exists = fs.existsSync(file) && fs.statSync(file).isFile();
    if (!exists) require(field, role === 'image' ? 'Local multiband GeoTIFF' : 'Persisted labeled samples', false, `Missing workspace input: ${normalized[field]}`);
    else {
      const allowed = role === 'image' ? ['.tif', '.tiff'] : ['.geojson', '.json', '.gpkg'];
      if (!allowed.includes(path.extname(file).toLowerCase())) throw new TypeError(`Unsupported ${role} format.`);
      inputVersions.push({ role, path: path.relative(root, file), bytes: fs.statSync(file).size, sha256: hashFile(file), ...(role === 'samples' ? { layerId: normalized.sampleLayerId ?? null, revision: normalized.sampleRevision ?? null } : {}) });
    }
  }
  require('imagePath', 'Local multiband GeoTIFF', inputVersions.some(item => item.role === 'image'), normalized.collection ? 'A collection identifier is context only; download a real raster before local execution.' : 'No imagery is downloaded or synthesized by preparation.');
  if (plan.kind === 'classification') {
    require('samples', 'Labeled polygon or point samples', inputVersions.some(item => item.role === 'samples'), 'At least two classes and separate held-out feature/groups per class are checked during execution.');
    require('classField', 'Class attribute', Boolean(normalized.classField), 'Class values may be strings or numbers and are kept in a typed mapping.');
  }
  if (plan.kind === 'ndvi') require('bands', 'Red and near-infrared bands', Boolean(normalized.bands), 'Use {red, nir} or [red, nir] with one-based band indexes or exact raster descriptions.');
  if (normalized.scale && localKinds.has(plan.kind)) require('nativeGrid', 'Native raster resolution', true, 'Local execution clips the raster on its native grid and does not resample to the optional cloud scale value.');
  if (!localKinds.has(plan.kind)) requirements.push({ key: 'execution', label: 'Execution adapter', status: 'preparation_only', detail: `${plan.kind} is recorded for planning. This adapter only executes local classification and NDVI; no change raster, animation, or research result has been generated.` });
  const runnable = localKinds.has(plan.kind) && requirements.every(item => item.status === 'provided');
  const planId = randomUUID(), relative = `.earth/remote-sensing/plans/${planId}/plan.json`;
  const record = { schemaVersion: 'earth.remote-sensing-plan.v1', planId, createdAt: new Date().toISOString(), status: requirements.some(item => item.status === 'missing') ? 'needs_input' : 'prepared', execution: localKinds.has(plan.kind) ? 'local' : 'preparation_only', runnable, plan: normalized, requirements, inputVersions, path: relative };
  write(inside(root, relative, true), record);
  return record;
}

function execute(python, request, root) {
  return new Promise((resolve, reject) => {
    const child = execFile(python, [runner], { cwd: root, timeout: 300_000, maxBuffer: 4 * 1024 * 1024 }, (error, stdout, stderr) => {
      let value;
      try { value = JSON.parse(String(stdout).trim().split(/\r?\n/).at(-1)); } catch { reject(new Error(String(stderr || error?.message || 'Remote-sensing runner returned no receipt.'))); return; }
      if (error && value.status !== 'failed') reject(new Error(String(stderr || error.message)));
      else resolve(value);
    });
    child.stdin.on('error', () => {});
    child.stdin.end(JSON.stringify(request));
  });
}

export async function runRemoteSensingWorkflow({ root, planId, python }) {
  root = workspace(root);
  if (!idPattern.test(String(planId))) throw new TypeError('Invalid remote-sensing plan ID.');
  const saved = JSON.parse(fs.readFileSync(inside(root, `.earth/remote-sensing/plans/${planId}/plan.json`), 'utf8'));
  if (saved.schemaVersion !== 'earth.remote-sensing-plan.v1' || saved.planId !== planId) throw new Error('Invalid saved remote-sensing plan.');
  const runId = randomUUID(), runRelative = `.earth/gis/runs/${runId}`, runDir = inside(root, runRelative, true);
  fs.mkdirSync(runDir, { recursive: true });
  const base = { schemaVersion: 'earth.remote-sensing-run.v1', runId, planId, kind: saved.plan.kind, op:`remote-${saved.plan.kind}`,backend:'local',params:saved.plan, startedAt: new Date().toISOString(), status: 'running', inputVersions: saved.inputVersions, outputs: [] };
  write(path.join(runDir, 'run.json'), base);
  let result;
  try {
    if (!saved.runnable) result = { status: 'needs_input', code: saved.execution === 'preparation_only' ? 'workflow_preparation_only' : 'missing_inputs', requirements: saved.requirements, error: 'Complete the listed requirements and prepare a new plan before execution.' };
    else {
      for (const input of saved.inputVersions) {
        if (hashFile(inside(root, input.path)) !== input.sha256) throw new Error(`Input changed after plan preparation: ${input.path}. Prepare a new plan.`);
      }
      const managed = path.join(os.homedir(), 'Library', 'Application Support', 'RagIme', 'EarthGISRuntime', '.venv', 'bin', 'python');
      const configFile = inside(root, '.earth/gis/runtime.json', true);
      const configured = fs.existsSync(configFile) ? JSON.parse(fs.readFileSync(configFile, 'utf8')).python : undefined;
      const executable = python || configured || process.env.PAW_EARTH_GIS_PYTHON || (fs.existsSync(managed) ? managed : 'python3');
      result = await execute(executable, { root, runDir, plan: saved.plan }, root);
      for (const input of saved.inputVersions) {
        if (hashFile(inside(root, input.path)) !== input.sha256) throw new Error(`Input changed during execution: ${input.path}. No result was accepted.`);
      }
      if(result.status==='completed') {
        const escape=value=>String(value).replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
        const reportPath=path.join(runDir,'report.html');
        const metrics=result.metrics ?? result.statistics ?? {};
        const matrix=result.metrics?.confusionMatrix;
        const table=matrix?`<h2>验证混淆矩阵</h2><table>${matrix.map(row=>`<tr>${row.map(value=>`<td>${escape(value)}</td>`).join('')}</tr>`).join('')}</table>`:'';
        fs.writeFileSync(reportPath,`<!doctype html><html lang="zh"><meta charset="utf-8"><title>遥感计算报告</title><style>body{font:16px system-ui;max-width:920px;margin:40px auto;padding:24px;color:#203c32}img{max-width:100%}pre{white-space:pre-wrap;background:#f2f6f3;padding:16px}td{padding:12px;border:1px solid #c5d6cc}</style><h1>${saved.plan.kind==='classification'?'随机森林地物分类':'NDVI 区域统计'}</h1><p>运行 ${escape(runId)} · 计划 ${escape(planId)}</p>${result.preview?`<img alt="计算结果预览" src="${result.preview.dataUrl}">`:''}${table}<h2>实际计算指标</h2><pre>${escape(JSON.stringify(metrics,null,2))}</pre><h2>类别与数据版本</h2><pre>${escape(JSON.stringify({classes:result.classMapping,inputs:saved.inputVersions},null,2))}</pre><p>地图预览经过降采样；GeoTIFF 保留原始分析网格。样本分组验证不等同于跨地区或跨日期验证。</p></html>`);
        result.outputs ??=[];result.outputs.push({path:path.relative(root,reportPath),name:'report.html',kind:'file'});
      }
      for (const output of result.outputs ?? []) {
        const file = inside(root, output.path);
        if (!file.startsWith(runDir + path.sep)) throw new Error('Remote-sensing output does not belong to this run.');
        output.relativePath=path.relative(runDir,file);
        output.sha256 = hashFile(file);
        output.bytes = fs.statSync(file).size;
      }
    }
  } catch (error) { result = { status: 'failed', code: 'workflow_failed', error: error instanceof Error ? error.message : String(error), outputs: [] }; }
  const receipt = { ...base, ...result, updatedAt: new Date().toISOString() };
  write(path.join(runDir, 'run.json'), receipt);
  write(inside(root, '.earth/remote-sensing/workspace.json', true), receipt);
  return receipt;
}
