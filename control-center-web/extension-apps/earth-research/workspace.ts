export interface EarthLayer { id: string; name: string; shown: boolean; opacity: number; status: string; tileUrl?: string }
export interface EarthTask { id: string | null; kind: string; destination: string; status: string; submittedAt: string; error?: string }
export interface EarthArtifact { kind: string; path: string; bytes?: number; source?: string; createdAt?: string }
export interface EarthRun {
  schemaVersion: 'earth.run.v1'; runId: string; status: 'starting' | 'running' | 'completed' | 'failed' | 'cancelled';
  code: string; scriptPath: string; project: string; sourceHash: string; startedAt: string; updatedAt: string;
  layers: EarthLayer[]; console: Array<{ values: unknown[]; pending: boolean }>;
  tasks?: EarthTask[]; artifacts?: EarthArtifact[];
  sourceRefs?: Array<{ title: string; url: string; retrievedAt: string }>;
  view?: { center: [number, number]; zoom: number } | null; error?: string;
}
export function parseRun(value: unknown): EarthRun {
  const x = value as Partial<EarthRun> | null;
  if (!x || x.schemaVersion !== 'earth.run.v1' || typeof x.runId !== 'string' || typeof x.code !== 'string'
    || typeof x.scriptPath !== 'string' || typeof x.sourceHash !== 'string' || !Array.isArray(x.layers) || !Array.isArray(x.console)
    || !['starting', 'running', 'completed', 'failed', 'cancelled'].includes(String(x.status))) throw new Error('运行记录不完整，保留上一份结果。');
  if (!x.layers.every(layer => layer && typeof layer.id === 'string' && typeof layer.name === 'string'
    && (layer.tileUrl === undefined || safeTileUrl(layer.tileUrl)))) throw new Error('运行图层地址无效。');
  if (!x.console.every(row => row && Array.isArray(row.values))) throw new Error('控制台记录无效。');
  if (x.tasks !== undefined && (!Array.isArray(x.tasks) || !x.tasks.every(task => task && (task.id === null || typeof task.id === 'string') && typeof task.kind === 'string' && typeof task.destination === 'string' && typeof task.status === 'string' && typeof task.submittedAt === 'string'))) throw new Error('云端任务记录无效。');
  if (x.artifacts !== undefined && (!Array.isArray(x.artifacts) || !x.artifacts.every(artifact => artifact && typeof artifact.kind === 'string' && typeof artifact.path === 'string'))) throw new Error('下载成果记录无效。');
  if (x.sourceRefs !== undefined && (!Array.isArray(x.sourceRefs) || !x.sourceRefs.every(ref => ref && typeof ref.title === 'string' && typeof ref.url === 'string' && ref.url.startsWith('https://developers.google.com/earth-engine/') && typeof ref.retrievedAt === 'string'))) throw new Error('资料读取记录无效。');
  if (x.view && (!Array.isArray(x.view.center) || x.view.center.length !== 2 || !x.view.center.every(Number.isFinite)
    || Math.abs(x.view.center[0]) > 180 || Math.abs(x.view.center[1]) > 90 || !Number.isFinite(x.view.zoom))) throw new Error('地图视角无效。');
  return x as EarthRun;
}
export function safeTileUrl(value: string): boolean {
  try { const url = new URL(value); return url.protocol === 'https:' && url.hostname === 'earthengine.googleapis.com' && !url.username && !url.password; } catch { return false; }
}
export function geoJsonOutputs(run: EarthRun | null): Array<{ label: string; geojson: GeoJSON.GeoJsonObject }> {
  return (run?.console ?? []).flatMap((row, index) => row.values.flatMap(value => {
    const object = value as Record<string, unknown> | null;
    return object && ['FeatureCollection', 'Feature'].includes(String(object.type))
      ? [{ label: typeof row.values[0] === 'string' ? row.values[0] : `结果 ${index + 1}`, geojson: object as unknown as GeoJSON.GeoJsonObject }] : [];
  }));
}
export const runStatus = { starting: '正在连接 Google', running: '正在计算', completed: '计算已完成', failed: '运行失败', cancelled: '本地运行已停止' } as const;
