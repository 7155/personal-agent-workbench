import { expect, it } from 'vitest';
import { rectangleFeature, updateSelection } from './map-selection';
it('normalizes reverse drags into a closed WGS84 rectangle', () => {
  const feature=rectangleFeature([120.2,30.2],[120.1,30.1]);
  expect(feature.bbox).toEqual([120.1,30.1,120.2,30.2]);
  expect(feature.geometry.coordinates[0]).toEqual([[120.1,30.1],[120.2,30.1],[120.2,30.2],[120.1,30.2],[120.1,30.1]]);
});
it('preserves multiple distinct objects, updates an edited object, and removes only its selected entry',()=>{
  const a:GeoJSON.Feature={type:'Feature',id:'a',properties:{},geometry:{type:'Point',coordinates:[120,30]}};
  const b:GeoJSON.Feature={...a,id:'b',geometry:{type:'Point',coordinates:[121,31]}};
  let selection=updateSelection(null,a,'toggle','run');
  selection=updateSelection(selection,b,'toggle','run');
  expect(selection?.features.map(f=>f.id)).toEqual(['a','b']);
  selection=updateSelection(selection,{...a,properties:{edited:true}},'upsert','run');
  expect(selection?.features).toHaveLength(2);
  expect(selection?.features.find(f=>f.id==='a')?.properties?.edited).toBe(true);
  selection=updateSelection(selection,b,'toggle','run');
  expect(selection?.features.map(f=>f.id)).toEqual(['a']);
  expect(updateSelection(selection,a,'remove','run')).toBeNull();
  expect(updateSelection(selection,b,'upsert','new-run')?.features.map(f=>f.id)).toEqual(['b']);
});
it('does not fabricate a region from a click or invalid coordinates', () => {
  expect(()=>rectangleFeature([120,30],[120,30])).toThrow();
  expect(()=>rectangleFeature([NaN,30],[120,31])).toThrow();
});
it('keeps Earth Engine objects from separate collections with reused indices',()=>{
  const a:GeoJSON.Feature={type:'Feature',id:'0',properties:{id:'A'},geometry:{type:'Point',coordinates:[120,30]}};
  const route:GeoJSON.Feature={...a,properties:{id:'route'},geometry:{type:'LineString',coordinates:[[120,30],[121,31]]}};
  const exclusion:GeoJSON.Feature={...a,properties:{name:'exclusion'},geometry:{type:'Point',coordinates:[122,32]}};
  let selection=updateSelection(null,a,'upsert','run');
  selection=updateSelection(selection,route,'upsert','run');
  selection=updateSelection(selection,exclusion,'upsert','run');
  expect(selection?.features).toEqual([a,route,exclusion]);
  expect(updateSelection(selection,route,'remove','run')?.features).toEqual([a,exclusion]);
});
