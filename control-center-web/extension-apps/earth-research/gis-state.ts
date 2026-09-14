export type GisLayerKind = 'basemap' | 'gee-raster' | 'gee-vector' | 'drawing';
export type GisGeometry = GeoJSON.Feature<GeoJSON.Point | GeoJSON.LineString | GeoJSON.Polygon>;
export type GisLayer = { id:string; name:string; kind:GisLayerKind; visible:boolean; source?:string; featureIds?:string[] };
export type GisView = { center:[number,number]; zoom:number; mapType:'satellite'|'hybrid'|'roadmap'|'terrain' };
export type GisState = { workspaceRoot:string; projectId:string; view:GisView; layers:GisLayer[]; geometries:GisGeometry[]; selectedIds:string[]; question:string; connection:'connecting'|'ready'|'recovering'|'failed' };
export const geometryId=(f:GeoJSON.Feature)=>String(f.id ?? f.properties?.id ?? JSON.stringify(f.geometry));
export function createGisState(workspaceRoot='',projectId=''):GisState{return {workspaceRoot,projectId,view:{center:[110,30],zoom:4,mapType:'satellite'},layers:[],geometries:[],selectedIds:[],question:'',connection:'connecting'};}
export function selectGeometries(
  state: GisState,
  ids: string[],
  mode: 'replace' | 'add' | 'remove' | 'toggle' = 'replace',
): GisState {
  // A selection is a set. A duplicate ID in one request must not toggle the
  // same geometry twice, cancelling the requested change.
  const requested = new Set(ids);
  if (mode === 'replace') return { ...state, selectedIds: [...requested] };
  const current = new Set(state.selectedIds);
  for (const id of requested) {
    if (mode === 'remove' || (mode === 'toggle' && current.has(id))) current.delete(id);
    else current.add(id);
  }
  return { ...state, selectedIds: [...current] };
}
export function selectedGeometries(state:GisState):GisGeometry[]{const ids=new Set(state.selectedIds);return state.geometries.filter(f=>ids.has(geometryId(f)));}
export function upsertGeometry(state:GisState,f:GisGeometry):GisState {const id=geometryId(f);return {...state,geometries:[...state.geometries.filter(x=>geometryId(x)!==id),f],selectedIds:state.selectedIds.includes(id)?state.selectedIds:[...state.selectedIds,id]};}
export function removeGeometry(state:GisState,id:string):GisState{return {...state,geometries:state.geometries.filter(f=>geometryId(f)!==id),selectedIds:state.selectedIds.filter(x=>x!==id)};}
export function setView(state:GisState,view:Partial<GisView>):GisState{return {...state,view:{...state.view,...view}};}
export function featureCollectionContext(state:GisState){return {workspaceRoot:state.workspaceRoot,projectId:state.projectId,type:'FeatureCollection',features:selectedGeometries(state)};}
