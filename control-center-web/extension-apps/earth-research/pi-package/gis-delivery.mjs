import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import { createHash, randomUUID } from 'node:crypto';

const hash = file => {
  const digest=createHash('sha256'),buffer=Buffer.allocUnsafe(1024*1024),fd=fs.openSync(file,'r');
  try {let count;while((count=fs.readSync(fd,buffer,0,buffer.length,null))>0)digest.update(buffer.subarray(0,count));return digest.digest('hex');} finally{fs.closeSync(fd);}
};
const json = file => JSON.parse(fs.readFileSync(file, 'utf8'));
const write = (file, value) => fs.writeFileSync(file, JSON.stringify(value, null, 2));
const escape = value => String(value).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
function inside(root, relative) {
  if (typeof relative !== 'string' || !relative || path.isAbsolute(relative)) throw new Error('Expected a workspace-relative path.');
  const target = path.resolve(root, relative);
  const rel = path.relative(root, target);
  if (rel === '..' || rel.startsWith('../') || !rel) throw new Error('Path escapes workspace.');
  let cursor = root;
  for (const segment of rel.split(path.sep)) {
    cursor = path.join(cursor, segment);
    if (fs.existsSync(cursor) && fs.lstatSync(cursor).isSymbolicLink()) throw new Error('GIS delivery paths cannot contain symlinks.');
  }
  return target;
}
function completedRun(root, runId) {
  if (!/^[a-f0-9-]{8,80}$/i.test(runId)) throw new Error('Invalid local GIS run ID.');
  const directory = inside(root, `.earth/gis/runs/${runId}`);
  const run = json(inside(root, `.earth/gis/runs/${runId}/run.json`));
  if (run.status !== 'completed' || run.runId !== runId) throw new Error('A matching completed local GIS run is required.');
  return { run, directory };
}

/** Freeze the actual bytes sent to the runner; later edits cannot rewrite an analysis input. */
export function snapshotGISInputs(root, runDir, inputs = {}) {
  const bindings = {}, versions = [];
  fs.mkdirSync(path.join(runDir, 'inputs'), { recursive: true });
  let catalog = [];
  try { catalog = json(inside(root, '.earth/layers/catalog.json')).layers ?? []; } catch { /* External files have hashes instead of catalog revisions. */ }
  for (const [role, relative] of Object.entries(inputs)) {
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(role)) throw new Error('Invalid input role.');
    const source = inside(root, relative);
    if (!fs.statSync(source).isFile()) throw new Error('GIS inputs must be files.');
    const folder = path.join(runDir, 'inputs', role); fs.mkdirSync(folder);
    const files = path.extname(source).toLowerCase() === '.shp'
      ? fs.readdirSync(path.dirname(source)).filter(name => path.parse(name).name === path.parse(source).name && ['.shp','.shx','.dbf','.prj','.cpg','.qix'].includes(path.extname(name).toLowerCase())).map(name => path.join(path.dirname(source), name))
      : [source];
    const copies = files.map(file => {
      inside(root, path.relative(root, file));
      const destination = path.join(folder, path.basename(file)); fs.copyFileSync(file, destination);
      return { path: path.relative(root, destination), sha256: hash(destination), bytes: fs.statSync(destination).size };
    });
    bindings[role] = path.relative(root, path.join(folder, path.basename(source)));
    const layer = catalog.find(item => item.path === relative);
    versions.push({ role, source: relative, layerId: layer?.id ?? null, revision: layer?.revision ?? null, snapshot: bindings[role], sha256: hash(path.join(root, bindings[role])), files: copies });
  }
  return { bindings, versions };
}

export function listGISRuns({ root }) {
  root = fs.realpathSync(root);
  const directory = inside(root, '.earth/gis/runs');
  if (!fs.existsSync(directory)) return { status: 'completed', runs: [] };
  const runs = [];
  for (const id of fs.readdirSync(directory)) {
    try {
      const run = json(inside(root, `.earth/gis/runs/${id}/run.json`));
      if (run.runId === id) runs.push({ ...run, preview:undefined, outputs: (run.outputs ?? []).map(({ geojson, ...item }) => item), code: undefined });
    } catch { /* A pending or damaged receipt is not a completed analysis. */ }
  }
  return { status: 'completed', runs: runs.sort((a,b) => String(b.startedAt).localeCompare(String(a.startedAt))).slice(0, 100) };
}
export function readGISRun({ root, runId }) {
  root = fs.realpathSync(root);
  return { ...completedRun(root, runId).run, status: 'completed' };
}
function statistics(run) {
  return { runId: run.runId, op: run.op, params: run.params ?? {}, inputVersions: run.inputVersions ?? [], outputs: (run.outputs ?? []).map(item => ({ path: item.relativePath, ...item.summary })) };
}
function report(run, stats, cartography, hasGeoPackage) {
  const rows = stats.outputs.map(item => `<tr><td>${escape(item.path)}</td><td>${escape(item.featureCount ?? '—')}</td><td>${escape(item.areaM2 ?? '—')}</td><td>${escape(item.crs ?? '—')}</td></tr>`).join('');
  const rasterLinks = run.outputs.filter(item => item.kind === 'raster').map(item => `<a href="${escape(`run/${item.relativePath || path.basename(item.path)}`)}">${escape(item.name || path.basename(item.path))}</a>`);
  const downloads = [hasGeoPackage ? '<a href="result.gpkg">GeoPackage</a>' : '', '<a href="map.pdf">地图 PDF</a>', '<a href="map.svg">矢量版 SVG</a>', '<a href="statistics.csv">统计 CSV</a>', ...rasterLinks].filter(Boolean).join(' · ');
  return `<!doctype html><html lang="zh"><meta charset="utf-8"><title>${escape(cartography.title)}</title><style>body{font:16px system-ui;max-width:960px;margin:40px auto;padding:24px;color:#213b34}table{border-collapse:collapse;width:100%}td,th{border:1px solid #cbd6d1;padding:10px;text-align:left}code,pre{overflow:auto;background:#f2f6f3;padding:12px;display:block}img{width:100%;height:auto;border:1px solid #cbd6d1}a{color:#24634b}</style><h1>${escape(cartography.title)}</h1><p>${escape(cartography.subtitle)}</p><p>运行 <code>${escape(run.runId)}</code></p><p>算子：${escape(run.op)} · 本地计算</p><p>${downloads}</p><a href="map.pdf"><img src="map.png" alt="成果地图预览"></a><p>${escape(cartography.paperSize)} · ${escape(cartography.orientation)} · ${escape(cartography.crs)}。图例、北向、比例尺与投影详情见 <a href="quality.json">制图检查</a>；栅格预览经过降采样，原始输出保存在 run/。</p><h2>参数</h2><pre>${escape(JSON.stringify(stats.params,null,2))}</pre><h2>成果统计</h2><table><tr><th>文件</th><th>要素数</th><th>面积 m²</th><th>坐标系</th></tr>${rows}</table><p>面积采用输出图层适用的投影坐标系计算，测量坐标系见 statistics.json。没有统计值时显示 —。</p><h2>输入版本</h2><pre>${escape(JSON.stringify(stats.inputVersions,null,2))}</pre><p>这是软件计算结果，不能替代工程审批。</p></html>`;
}

function mapConfiguration(value, run) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new TypeError('mapOptions must be an object.');
  const options = { title: `GIS analysis / ${run.op}`, subtitle: run.runId, paperSize: 'A4', orientation: 'landscape', legend: true, scaleBar: true, northArrow: true, ...value };
  for (const [key, limit] of [['title', 200], ['subtitle', 400], ['crs', 2048]]) {
    if (options[key] !== undefined && (typeof options[key] !== 'string' || options[key].length > limit || options[key].includes('\0'))) throw new TypeError(`mapOptions.${key} must be text of at most ${limit} characters.`);
  }
  if (!['A4', 'A3', 'Letter'].includes(options.paperSize)) throw new TypeError('mapOptions.paperSize must be A4, A3 or Letter.');
  if (!['landscape', 'portrait'].includes(options.orientation)) throw new TypeError('mapOptions.orientation must be landscape or portrait.');
  for (const key of ['legend', 'scaleBar', 'northArrow']) if (typeof options[key] !== 'boolean') throw new TypeError(`mapOptions.${key} must be a boolean.`);
  return options;
}

export function createGISBundle({ root, runId, name = 'earth-analysis', version, include = [], mapOptions = {} }) {
  root = fs.realpathSync(root);
  const { run, directory } = completedRun(root, runId);
  if (!run.outputs?.length) throw new Error('Run has no recorded outputs.');
  const layout = mapConfiguration(mapOptions, run);
  const safeName = String(name).replace(/[^A-Za-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0,64) || 'earth-analysis';
  const parent = inside(root, '.earth/deliverables'); fs.mkdirSync(parent, { recursive: true });
  if (version !== undefined && (!Number.isInteger(version) || version < 1)) throw new Error('Version must be a positive integer.');
  const existing = fs.readdirSync(parent).filter(item => item.startsWith(`${safeName}-v`)).map(item => Number(item.slice(safeName.length + 2))).filter(Number.isInteger);
  const nextVersion = version ?? Math.max(0,...existing) + 1;
  const relative = `.earth/deliverables/${safeName}-v${nextVersion}`;
  const target = inside(root, relative);
  if (fs.existsSync(target)) throw new Error('This deliverable version already exists.');
  const staging = path.join(parent, `.pending-${randomUUID()}`); fs.mkdirSync(staging);
  try {
    const entries = [];
    const copy = (source, destination) => {
      inside(root, path.relative(root, source));
      if (fs.statSync(source).isDirectory()) {
        fs.mkdirSync(destination, {recursive:true});
        for (const file of fs.readdirSync(source)) copy(path.join(source,file),path.join(destination,file));
      } else {
        fs.mkdirSync(path.dirname(destination),{recursive:true}); fs.copyFileSync(source,destination);
      }
    };
    for (const output of run.outputs) {
      const source = inside(root, output.path);
      if (!source.startsWith(directory + path.sep)) throw new Error('Output does not belong to this run.');
      if (output.sha256 && hash(source) !== output.sha256) throw new Error('Output changed since the run completed.');
    }
    copy(directory, path.join(staging,'run'));
    for (const file of include) copy(inside(root,file),path.join(staging,'attachments',file));
    const stats = statistics(run);
    write(path.join(staging,'statistics.json'),stats);
    const csvCell=value=>`"${String(value ?? '').replace(/"/g,'""')}"`;
    fs.writeFileSync(path.join(staging,'statistics.csv'),['run_id,file,feature_count,area_m2,crs',...stats.outputs.map(item=>[runId,item.path,item.featureCount,item.areaM2,item.crs].map(csvCell).join(','))].join('\n'));
    const configFile=inside(root,'.earth/gis/runtime.json');
    const config=fs.existsSync(configFile)?json(configFile):{};
    const managed=path.join(os.homedir(),'Library/Application Support/RagIme/EarthGISRuntime/.venv/bin/python');
    const python=config.python || process.env.PAW_EARTH_GIS_PYTHON || (fs.existsSync(managed)?managed:'python3');
    // Share the installer's versioned font cache across deliveries. A cache in
    // staging is deleted after every package and repeats macOS font discovery.
    const runtimeRoot=path.join(process.env.RAG_IME_APP_SUPPORT_DIR || path.join(os.homedir(),'Library/Application Support/RagIme'),'EarthGISRuntime');
    const fontCache=path.join(runtimeRoot,'cache','matplotlib');
    for(const directory of [path.join(runtimeRoot,'cache'),fontCache]) {
      if(fs.existsSync(directory)&&fs.lstatSync(directory).isSymbolicLink())throw new Error('GIS font cache must be a regular directory.');
      fs.mkdirSync(directory,{recursive:true,mode:0o700});
    }
    const rendered=spawnSync(python,[fileURLToPath(new URL('./gis-deliver.py',import.meta.url))],{input:JSON.stringify({root,run,target:staging,mapOptions:layout}),encoding:'utf8',timeout:120000,maxBuffer:4000000,env:{...process.env,MPLCONFIGDIR:fontCache,MPLBACKEND:'Agg',MPL_IGNORE_SYSTEM_FONTS:'1'}});
    if(rendered.status!==0)throw new Error(`GIS 制图失败：${rendered.stderr || rendered.error?.message || '请安装 GIS 运行环境中的 matplotlib'}`);
    const { cartography } = json(path.join(staging, 'quality.json'));
    fs.writeFileSync(path.join(staging,'report.html'),report(run,stats,cartography,fs.existsSync(path.join(staging,'result.gpkg'))));
    const walk = dir => {
      for (const name of fs.readdirSync(dir)) {
        const full = path.join(dir,name);
        if (fs.statSync(full).isDirectory()) walk(full);
        else entries.push({path:path.relative(staging,full),sha256:hash(full),bytes:fs.statSync(full).size});
      }
    };
    walk(staging);
    const manifest = { schemaVersion:'earth.gis-deliverable.v2', name:safeName, version:nextVersion, runId, op:run.op, params:run.params ?? {}, inputVersions:run.inputVersions ?? [], createdAt:new Date().toISOString(), files:entries.sort((a,b)=>a.path.localeCompare(b.path)), report:'report.html',statistics:'statistics.json',map:'map.geojson',preview:'map.png',cartography };
    write(path.join(staging,'run-manifest.json'),manifest);
    // Exclusive mkdir reserves a version before moving its content; never overwrite another delivery.
    fs.mkdirSync(target);
    try { for (const file of fs.readdirSync(staging)) fs.renameSync(path.join(staging,file),path.join(target,file)); }
    catch (error) { fs.rmSync(target,{recursive:true,force:true}); throw error; }
    return { status:'completed',path:relative,manifest,verification:verifyGISBundle({root,path:relative}) };
  } finally { fs.rmSync(staging,{recursive:true,force:true}); }
}
export function verifyGISBundle({ root, path:relative }) {
  root = fs.realpathSync(root);
  const base = inside(root,relative), manifest = json(inside(root,`${relative}/run-manifest.json`));
  if (manifest.schemaVersion !== 'earth.gis-deliverable.v2') throw new Error('Unsupported deliverable manifest.');
  for (const item of manifest.files) {
    const file = inside(base,item.path);
    if (fs.statSync(file).size !== item.bytes || hash(file) !== item.sha256) throw new Error(`Deliverable readback differs: ${item.path}`);
  }
  const run = json(path.join(base,'run/run.json')), stats = json(path.join(base,manifest.statistics));
  if (manifest.runId !== run.runId || run.runId !== stats.runId) throw new Error('Deliverable run IDs disagree.');
  return {status:'completed',runId:run.runId,verifiedFiles:manifest.files.length};
}
export function compareGISRuns({ root, firstRunId, secondRunId }) {
  root = fs.realpathSync(root);
  const first = completedRun(root,firstRunId).run, second = completedRun(root,secondRunId).run;
  const firstStats = statistics(first), secondStats = statistics(second);
  const sameInputs = JSON.stringify(firstStats.inputVersions.map(x=>[x.role,x.sha256])) === JSON.stringify(secondStats.inputVersions.map(x=>[x.role,x.sha256]));
  const sum = stats => stats.outputs.reduce((total,item)=> total+(typeof item.areaM2==='number' ? item.areaM2 : 0),0);
  const comparison = {status:'completed',firstRunId,secondRunId,sameInputs,first:firstStats,second:secondStats,areaDeltaM2:sum(secondStats)-sum(firstStats)};
  const directory = inside(root,`.earth/comparisons/${firstRunId}-${secondRunId}`); fs.mkdirSync(directory,{recursive:true});
  write(path.join(directory,'comparison.json'),comparison);
  fs.writeFileSync(path.join(directory,'report.html'),`<!doctype html><html lang="zh"><meta charset="utf-8"><title>两次运行对比</title><style>body{font:16px system-ui;margin:40px;max-width:1100px}pre{white-space:pre-wrap}</style><h1>两次 GIS 运行对比</h1><p>输入内容${sameInputs?'相同':'不同，请同时核对输入变化'} · 面积变化 ${escape(comparison.areaDeltaM2)} m²</p><pre>${escape(JSON.stringify(comparison,null,2))}</pre></html>`);
  return {...comparison,path:path.relative(root,directory),report:path.relative(root,path.join(directory,'report.html'))};
}
