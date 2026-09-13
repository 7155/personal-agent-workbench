import { describe, expect, it } from 'vitest';
import { geoJsonOutputs, parseRun, safeTileUrl } from './workspace';

export const exampleRun = {
  schemaVersion: 'earth.run.v1', runId: 'run-1', status: 'completed', code: "print('result')", scriptPath: '/work/analysis.js', project: 'test-project',
  sourceHash: 'abc', startedAt: '2026-09-13T00:00:00Z', updatedAt: '2026-09-13T00:00:01Z',
  layers: [], console: [{ values: ['Sites', { type: 'FeatureCollection', features: [] }], pending: false }], view: null,
};
describe('Earth result contracts', () => {
  it('rejects incomplete results and unexpected image destinations', () => {
    expect(() => parseRun({ ...exampleRun, status: 'success' })).toThrow();
    expect(() => parseRun({ ...exampleRun, layers: [{ id: 'x', name: 'x', tileUrl: 'https://example.com/tracking' }] })).toThrow();
    expect(safeTileUrl('https://earthengine.googleapis.com/v1/map/tiles/{z}/{x}/{y}')).toBe(true);
    expect(safeTileUrl('https://earthengine.googleapis.com@evil.test/tiles')).toBe(false);
    expect(() => parseRun({ ...exampleRun, sourceRefs: [{ title:'x', url:'javascript:alert(1)', retrievedAt:'now' }] })).toThrow();
  });
  it('exports the exact returned GeoJSON, without creating an extra candidate', () => {
    const parsed = parseRun(exampleRun); const results = geoJsonOutputs(parsed);
    expect(results).toHaveLength(1); expect(results[0].geojson).toBe(exampleRun.console[0].values[1]);
    expect(results[0].label).toBe('Sites');
  });
});
