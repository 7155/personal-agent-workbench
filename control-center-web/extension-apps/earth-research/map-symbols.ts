/** One rendering key for the map, its legend and the layer list. No persisted data changes. */
export const MAP_SYMBOLS = {
  polygon: { label: '面图层', color: '#67e8f9', ink: '#0e7490', shape: 'polygon' },
  line: { label: '线图层', color: '#93c5fd', ink: '#1d4ed8', shape: 'line' },
  point: { label: '点图层', color: '#fdba74', ink: '#9a3412', shape: 'point' },
  mixed: { label: '混合图层', color: '#cbd5e1', ink: '#475569', shape: 'polygon' },
  result: { label: '分析结果', color: '#c4b5fd', ink: '#6d28d9', shape: 'polygon' },
  selected: { label: '已选要素', color: '#fbbf24', ink: '#92400e', shape: 'polygon' },
} as const;
export function layerSymbol(types: readonly string[]) {
  if (types.length && types.every(type => type.includes('Polygon'))) return MAP_SYMBOLS.polygon;
  if (types.length && types.every(type => type.includes('LineString'))) return MAP_SYMBOLS.line;
  if (types.length && types.every(type => type.includes('Point'))) return MAP_SYMBOLS.point;
  return MAP_SYMBOLS.mixed;
}
