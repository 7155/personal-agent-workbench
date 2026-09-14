import { expect, it } from 'vitest';
import {
  createGisState, featureCollectionContext, removeGeometry, selectGeometries,
  setView, upsertGeometry, type GisGeometry,
} from './gis-state';

const point = (id: string, x = 120): GisGeometry => ({
  type: 'Feature', id, properties: { name: id },
  geometry: { type: 'Point', coordinates: [x, 30] },
});

it('keeps add, toggle, remove and FeatureCollection context consistent', () => {
  let state = createGisState('/project', 'gee');
  state = upsertGeometry(state, point('a'));
  state = upsertGeometry(state, point('b', 121));
  expect(state.selectedIds).toEqual(['a', 'b']);
  state = selectGeometries(state, ['a'], 'toggle');
  expect(state.selectedIds).toEqual(['b']);
  expect(featureCollectionContext(state)).toMatchObject({
    workspaceRoot: '/project', projectId: 'gee', type: 'FeatureCollection',
    features: [point('b', 121)],
  });
  state = removeGeometry(state, 'b');
  expect(state.selectedIds).toEqual([]);
});

it('toggles an already-selected duplicate ID only once per request', () => {
  const state = upsertGeometry(createGisState(), point('a'));
  expect(selectGeometries(state, ['a', 'a'], 'toggle').selectedIds).toEqual([]);
  expect(state.selectedIds).toEqual(['a']);
});

it('selects an unselected duplicate ID once instead of cancelling it', () => {
  const state = selectGeometries(upsertGeometry(createGisState(), point('a')), []);
  expect(selectGeometries(state, ['a', 'a'], 'toggle').selectedIds).toEqual(['a']);
});

it('treats a mixed toggle request as a set without changing its input', () => {
  const state = upsertGeometry(createGisState(), point('a'));
  const ids = ['a', 'b', 'a', 'b'];
  expect(selectGeometries(state, ids, 'toggle').selectedIds).toEqual(['b']);
  expect(ids).toEqual(['a', 'b', 'a', 'b']);
});

it('preserves add, remove and replace set semantics', () => {
  const state = upsertGeometry(createGisState(), point('a'));
  expect(selectGeometries(state, ['a', 'b', 'b'], 'add').selectedIds).toEqual(['a', 'b']);
  expect(selectGeometries(state, ['a', 'a'], 'remove').selectedIds).toEqual([]);
  expect(selectGeometries(state, ['b', 'b', 'a']).selectedIds).toEqual(['b', 'a']);
});

it('supports empty requests without modifying selection except for replace', () => {
  const state = upsertGeometry(createGisState(), point('a'));
  for (const mode of ['add', 'remove', 'toggle'] as const) {
    expect(selectGeometries(state, [], mode).selectedIds).toEqual(['a']);
  }
  expect(selectGeometries(state, []).selectedIds).toEqual([]);
});

it('updates view through setView without losing map type or mutating the input', () => {
  const state = setView(createGisState(), { mapType: 'hybrid' });
  const next = setView(state, { zoom: 9 });
  expect(next.view).toEqual({ center: [110, 30], zoom: 9, mapType: 'hybrid' });
  expect(state.view).toEqual({ center: [110, 30], zoom: 4, mapType: 'hybrid' });
});
