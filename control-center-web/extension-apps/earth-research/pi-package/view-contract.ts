export type EarthViewCommand = { requestId: string; runId?: string; action: 'focus' | 'layer' | 'panel' | 'feature'; center?: [number, number]; zoom?: number; layerId?: string; visible?: boolean; featureId?: string; panel?: 'split' | 'map' | 'code' | 'results' | 'sources' };
export function parseViewCommand(raw: unknown): EarthViewCommand {
  const x = raw as EarthViewCommand | null;
  if (!x || typeof x.requestId !== 'string' || !x.requestId || (x.runId !== undefined && typeof x.runId !== 'string')) throw new Error('视图请求缺少标识。');
  if (x.action === 'focus') {
    if (!Array.isArray(x.center) || x.center.length !== 2 || !x.center.every(Number.isFinite) || Math.abs(x.center[0]) > 180 || Math.abs(x.center[1]) > 85 || !Number.isFinite(x.zoom) || x.zoom! < 1 || x.zoom! > 20) throw new Error('地图范围参数无效。');
  } else if (x.action === 'layer') {
    if (typeof x.layerId !== 'string' || !x.layerId || typeof x.visible !== 'boolean') throw new Error('图层请求无效。');
  } else if (x.action === 'feature') {
    if (typeof x.featureId !== 'string' || !x.featureId) throw new Error('需要实际要素 ID。');
  } else if (x.action === 'panel') {
    if (!['split', 'map', 'code', 'results', 'sources'].includes(String(x.panel))) throw new Error('工作区面板无效。');
  } else throw new Error('不支持的视图操作。');
  return x;
}
