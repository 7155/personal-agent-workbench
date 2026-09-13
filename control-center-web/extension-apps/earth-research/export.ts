const xml = (value: unknown) => String(value ?? '').replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/g, '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;' })[char]!);
function positions(values: GeoJSON.Position[]) {
  return values.map(value => {
    if (value.length < 2 || !value.every(Number.isFinite) || Math.abs(value[0]) > 180 || Math.abs(value[1]) > 90) throw new Error('KML 需要有效的 WGS84 经纬度。');
    return `${value[0]},${value[1]},${value[2] ?? 0}`;
  }).join(' ');
}
function geometry(value: GeoJSON.Geometry): string {
  switch (value.type) {
    case 'Point': return `<Point><coordinates>${positions([value.coordinates])}</coordinates></Point>`;
    case 'LineString': return `<LineString><coordinates>${positions(value.coordinates)}</coordinates></LineString>`;
    case 'Polygon': return `<Polygon>${value.coordinates.map((ring, i) => `<${i ? 'inner' : 'outer'}BoundaryIs><LinearRing><coordinates>${positions(ring)}</coordinates></LinearRing></${i ? 'inner' : 'outer'}BoundaryIs>`).join('')}</Polygon>`;
    case 'MultiPoint': return `<MultiGeometry>${value.coordinates.map(coordinates => geometry({ type: 'Point', coordinates })).join('')}</MultiGeometry>`;
    case 'MultiLineString': return `<MultiGeometry>${value.coordinates.map(coordinates => geometry({ type: 'LineString', coordinates })).join('')}</MultiGeometry>`;
    case 'MultiPolygon': return `<MultiGeometry>${value.coordinates.map(coordinates => geometry({ type: 'Polygon', coordinates })).join('')}</MultiGeometry>`;
    case 'GeometryCollection': return `<MultiGeometry>${value.geometries.map(geometry).join('')}</MultiGeometry>`;
  }
}
export function toKml(value: GeoJSON.GeoJsonObject, title: string): string {
  const input = value as GeoJSON.FeatureCollection | GeoJSON.Feature;
  const features = input.type === 'FeatureCollection' ? input.features : input.type === 'Feature' ? [input] : [];
  if (features.length > 5000) throw new Error('此 KML 导出最多支持 5000 个要素，请使用 GeoJSON。');
  return `<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>${xml(title)}</name>${features.map((feature, index) => `<Placemark><name>${xml(feature.properties?.name ?? feature.properties?.id ?? `${index + 1}`)}</name>${feature.geometry ? geometry(feature.geometry) : ''}</Placemark>`).join('')}</Document></kml>`;
}
