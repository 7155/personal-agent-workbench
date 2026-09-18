export type EarthViewCommand = { requestId: string; runId?: string; action: 'focus' | 'layer' | 'panel' | 'feature'; center?: [number, number]; zoom?: number; layerId?: string; visible?: boolean; featureId?: string; panel?: 'split' | 'map' | 'code' | 'results' | 'sources' | 'knowledge' };

/** The read-only state projection written by the map host. It deliberately
 * contains only view and selection metadata; geometries remain in the
 * explicit composer context and result artifacts. */
export type EarthMapState = {
  schemaVersion: 'earth.map-state.v1';
  center: [number, number];
  zoom: number;
  bounds: [number, number, number, number];
  visibleLayerIds: string[];
  selectedFeatureIds: string[];
  updatedAt: string;
};

export function parseMapState(raw: unknown): EarthMapState {
  const x = raw as Partial<EarthMapState> | null;
  if (!x || x.schemaVersion !== 'earth.map-state.v1' || !Array.isArray(x.center) || x.center.length !== 2
    || !x.center.every(Number.isFinite) || Math.abs(x.center[0]) > 180 || Math.abs(x.center[1]) > 90
    || !Number.isFinite(x.zoom) || Number(x.zoom) < 1 || Number(x.zoom) > 24 || !Array.isArray(x.bounds) || x.bounds.length !== 4
    || !x.bounds.every(Number.isFinite) || !Array.isArray(x.visibleLayerIds) || !x.visibleLayerIds.every(value => typeof value === 'string')
    || !Array.isArray(x.selectedFeatureIds) || !x.selectedFeatureIds.every(value => typeof value === 'string') || typeof x.updatedAt !== 'string') {
    throw new Error('地图状态不完整。');
  }
  return x as EarthMapState;
}
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
    if (!['split', 'map', 'code', 'results', 'sources', 'knowledge'].includes(String(x.panel))) throw new Error('工作区面板无效。');
  } else throw new Error('不支持的视图操作。');
  return x;
}
