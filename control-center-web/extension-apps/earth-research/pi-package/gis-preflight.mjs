import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { inspectGISPath } from './gis-operations.mjs';
import { workspacePath } from './workspace-path.mjs';
import { throwIfAborted } from './runner-process.mjs';

export function inputHash(file) {
  const hash = createHash('sha256'), data = Buffer.allocUnsafe(1024 * 1024), fd = fs.openSync(file, 'r');
  try { let count; while ((count = fs.readSync(fd, data, 0, data.length, null)) > 0) hash.update(data.subarray(0, count)); return hash.digest('hex'); }
  finally { fs.closeSync(fd); }
}

/** Hash all components of a Shapefile, not just its geometry. */
export function inputFileVersions(root, relative) {
  const file = workspacePath(root, relative);
  const files = path.extname(file).toLowerCase() === '.shp'
    ? fs.readdirSync(path.dirname(file)).filter(name => path.parse(name).name === path.parse(file).name &&
        ['.shp', '.shx', '.dbf', '.prj', '.cpg', '.qix'].includes(path.extname(name).toLowerCase()))
        .map(name => workspacePath(root, path.join(path.dirname(relative), name)))
    : [file];
  return files.map(file => ({ name: path.basename(file), sha256: inputHash(file) })).sort((a, b) => a.name.localeCompare(b.name));
}

/** A data check, not planning permission or a promise that a site is buildable. */
export async function preflightSiting({ root, python, parcels, avoidance, distance, signal }) {
  throwIfAborted(signal); root = fs.realpathSync(root);
  const issues = [], inputs = [];
  if (!Number.isFinite(distance) || distance <= 0 || distance > 100_000) issues.push({ level: 'error', code: 'invalid_distance', message: '避让距离必须大于 0 且不超过 100000 米。' });
  if (parcels === avoidance) issues.push({ level: 'error', code: 'same_input', message: '候选地块和避让图层不能是同一份文件。' });
  for (const [role, relative] of Object.entries({ parcels, avoidance })) {
    throwIfAborted(signal);
    try {
      const file = workspacePath(root, relative), sha256 = inputHash(file), files = inputFileVersions(root, relative);
      const summary = await inspectGISPath({ root, python, path: relative, signal });
      if (inputHash(file) !== sha256 || JSON.stringify(inputFileVersions(root, relative)) !== JSON.stringify(files)) throw new Error('检查期间输入发生变化，请重新检查。');
      // Omit sample attributes: preflight only needs spatial metadata.
      const { kind, rows, crs, bounds, geometryTypes, columns } = summary;
      inputs.push({ role, path: relative, sha256, files, kind, rows, crs, bounds, geometryTypes, columns });
      if (kind !== 'vector') issues.push({ level: 'error', code: 'not_vector', message: `${role} 需要矢量图层。` });
      if (!crs) issues.push({ level: 'error', code: 'unknown_crs', message: `${role} 缺少已知坐标系，不能推测或直接指定 EPSG:4326。` });
      if (!Number.isInteger(rows) || rows < 1) issues.push({ level: 'error', code: 'empty_input', message: `${role} 没有可分析要素。` });
      if (role === 'parcels' && (!Array.isArray(geometryTypes) || !geometryTypes.length || geometryTypes.some(type => !['Polygon', 'MultiPolygon'].includes(type)))) {
        issues.push({ level: 'error', code: 'non_polygon_parcels', message: '候选地块只能包含 Polygon / MultiPolygon。' });
      }
    } catch (error) {
      throwIfAborted(signal);
      issues.push({ level: 'error', code: 'input_unreadable', message: `${role}：${error instanceof Error ? error.message : String(error)}` });
    }
  }
  issues.push({ level: 'info', code: 'scope', message: '本流程只检查距指定要素的几何避让，不代表权属、坡度、通行时间或工程可建设性。' });
  return { schemaVersion: 'earth.gis-preflight.v1', status: 'completed', ready: !issues.some(item => item.level === 'error'),
    checkedAt: new Date().toISOString(), inputs, issues, distance, units: 'm',
    steps: ['读取并冻结输入', '在米制投影中计算缓冲', '从候选地块扣除避让区', '保存结果与输入版本'] };
}
