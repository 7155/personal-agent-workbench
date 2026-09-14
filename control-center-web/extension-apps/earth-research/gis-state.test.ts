import {expect,it} from 'vitest';
import {createGisState,featureCollectionContext,removeGeometry,selectGeometries,upsertGeometry,type GisGeometry} from './gis-state';
const p=(id:string,x:number):GisGeometry=>({type:'Feature',id,properties:{name:id},geometry:{type:'Point',coordinates:[x,30]}});
it('keeps one source of truth for add, toggle, remove and FeatureCollection context',()=>{let s=createGisState('/project','gee');s=upsertGeometry(s,p('a',120));s=upsertGeometry(s,p('b',121));expect(s.selectedIds).toEqual(['a','b']);s=selectGeometries(s,['a'],'toggle');expect(s.selectedIds).toEqual(['b']);expect(featureCollectionContext(s).features).toEqual([p('b',121)]);s=removeGeometry(s,'b');expect(s.selectedIds).toEqual([]);});
it('supports partial view updates without losing map type',()=>{let s=createGisState();s={...s,view:{...s.view,mapType:'hybrid'}};expect(s.view).toEqual({center:[110,30],zoom:4,mapType:'hybrid'});});
