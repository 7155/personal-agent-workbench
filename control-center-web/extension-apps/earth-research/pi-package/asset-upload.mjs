import fs from 'node:fs';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { saveCloudReceipt } from './cloud-tasks.mjs';

const execFileAsync = promisify(execFile);

function inside(root, value) {
  const target = path.resolve(root, value);
  if (!target.startsWith(`${path.resolve(root)}${path.sep}`) || !fs.existsSync(target)) throw new Error('Asset source must be an existing file inside the bound workspace.');
  return target;
}

function assetId(value) {
  if (typeof value !== 'string' || !/^(users|projects)\/[A-Za-z0-9._-]+(?:\/[A-Za-z0-9._-]+)+$/.test(value)) throw new Error('Asset ID must be users/... or projects/... with a valid path.');
  return value;
}

function requireWgs84Shapefile(source) {
  if (path.extname(source).toLowerCase() !== '.shp') return;
  const directory = path.dirname(source);
  const stem = path.basename(source, path.extname(source)).toLowerCase();
  const prjName = fs.readdirSync(directory).find(name => path.extname(name).toLowerCase() === '.prj' && path.basename(name, path.extname(name)).toLowerCase() === stem);
  if (!prjName) throw new Error('Shapefile upload requires a matching .prj sidecar in EPSG:4326/WGS84.');
  const wkt = fs.readFileSync(path.join(directory, prjName), 'utf8');
  if (!/(?:WGS[\s_]*84|WGS[\s_]*1984|EPSG[^0-9]{0,12}4326)/i.test(wkt)) throw new Error('Shapefile .prj must describe EPSG:4326/WGS84 before upload.');
}

export async function uploadTableAsset({ root, path: sourcePath, assetId: requestedAssetId, cli = '' }) {
  const source = inside(root, sourcePath);
  const id = assetId(requestedAssetId);
  if (!['.shp', '.zip', '.csv'].includes(path.extname(source).toLowerCase())) throw new Error('Table Asset upload currently accepts .shp, .zip or .csv. Reproject vectors to EPSG:4326 before upload.');
  requireWgs84Shapefile(source);
  const command = cli || process.env.PAW_EARTHENGINE_CLI || 'earthengine';
  let stdout = ''; let stderr = '';
  try {
    ({ stdout, stderr } = await execFileAsync(command, ['upload', 'table', `--asset_id=${id}`, source], { cwd: root, timeout: 300000, maxBuffer: 2 * 1024 * 1024 }));
  } catch (error) {
    const detail = `${error?.stderr || stderr || error?.message || error}`.slice(-2000);
    const receipt = { schemaVersion: 'earth.asset-upload.v1', status: 'failed', source: path.relative(root, source), assetId: id, error: detail, updatedAt: new Date().toISOString() };
    return { ...receipt, receipt: saveCloudReceipt(root, `asset-${id.replaceAll('/', '_')}`, receipt) };
  }
  const taskId = `${stdout}\n${stderr}`.match(/(?:task|operation)[^A-Za-z0-9_-]*([A-Za-z0-9_-]{8,})/i)?.[1] || null;
  const receipt = { schemaVersion: 'earth.asset-upload.v1', status: taskId ? 'submitted' : 'unknown', source: path.relative(root, source), assetId: id, taskId, output: `${stdout}\n${stderr}`.trim().slice(-4000), updatedAt: new Date().toISOString() };
  return { ...receipt, receipt: saveCloudReceipt(root, `asset-${id.replaceAll('/', '_')}`, receipt) };
}
