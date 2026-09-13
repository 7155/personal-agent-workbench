import { expect, it } from 'vitest';
import { toKml } from './export';
it('round-trips line endpoints and preserves polygon holes with escaped names', () => {
  const kml = toKml({ type: 'FeatureCollection', features: [
    { type: 'Feature', properties: { name: 'A & <B>' }, geometry: { type: 'LineString', coordinates: [[120,30],[121,31]] } },
    { type: 'Feature', properties: {}, geometry: { type: 'Polygon', coordinates: [[[0,0],[1,0],[1,1],[0,0]], [[.1,.1],[.2,.1],[.2,.2],[.1,.1]]] } },
  ] } as GeoJSON.FeatureCollection, '路线');
  const document = new DOMParser().parseFromString(kml, 'application/xml');
  expect(document.querySelector('parsererror')).toBeNull();
  expect(document.querySelector('Placemark name')?.textContent).toBe('A & <B>');
  expect(document.querySelector('LineString coordinates')?.textContent).toBe('120,30,0 121,31,0');
  expect(document.querySelectorAll('innerBoundaryIs')).toHaveLength(1);
});
it('rejects projected coordinates rather than exporting a wrong geographic location', () => {
  expect(() => toKml({type:'Feature',properties:{},geometry:{type:'Point',coordinates:[400000,3000000]}} as GeoJSON.Feature,'x')).toThrow(/WGS84/);
});
