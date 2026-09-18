import { expect, it } from 'vitest';
import { parseMapState, parseViewCommand } from './view-contract';

it('accepts real map and panel operations with stable request identity', () => {
  expect(parseViewCommand({requestId:'one',runId:'run',action:'focus',center:[120,30],zoom:14})).toMatchObject({requestId:'one',runId:'run'});
  expect(parseViewCommand({requestId:'two',action:'layer',layerId:'terrain',visible:false}).visible).toBe(false);
  expect(parseViewCommand({requestId:'three',action:'panel',panel:'sources'}).panel).toBe('sources');
});
it('rejects invalid geography and incomplete or unknown operations', () => {
  for (const command of [
    {action:'focus',center:[190,30],zoom:14},
    {action:'focus',center:[120,30],zoom:NaN},
    {action:'layer',layerId:'terrain'},
    {action:'feature',featureId:''},
    {action:'panel',panel:'delete'},
  ]) expect(() => parseViewCommand({requestId:'x',...command})).toThrow();
});

it('accepts a published map state and rejects state that cannot be trusted', () => {
  expect(parseMapState({ schemaVersion: 'earth.map-state.v1', center: [120, 30], zoom: 11, bounds: [119, 29, 121, 31], visibleLayerIds: ['terrain'], selectedFeatureIds: ['candidate_A'], updatedAt: '2026-09-18T00:00:00Z' }).selectedFeatureIds).toEqual(['candidate_A']);
  expect(() => parseMapState({ schemaVersion: 'earth.map-state.v1', center: [120, 30], zoom: 11, bounds: [119, 29, 121, 31], visibleLayerIds: [], selectedFeatureIds: [], updatedAt: 1 })).toThrow();
});
