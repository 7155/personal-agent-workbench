import L from 'leaflet';
import '@geoman-io/leaflet-geoman-free';
import '@geoman-io/leaflet-geoman-free/dist/leaflet-geoman.css';
import {selectionKey,type SelectionMode} from './map-selection';
export type EditableGeometry=GeoJSON.Feature<GeoJSON.Point|GeoJSON.MultiPoint|GeoJSON.LineString|GeoJSON.MultiLineString|GeoJSON.Polygon|GeoJSON.MultiPolygon>;
export type DrawingKind='point'|'polyline'|'polygon'|'rectangle'|'edit'|'remove'|'cut';
type Events={select:(feature:EditableGeometry|null,mode?:SelectionMode|'select')=>void;change:(features:EditableGeometry[],removed?:EditableGeometry[])=>Promise<void>|void;active:(active:boolean,kind?:DrawingKind,vertices?:number)=>void;previewRemoved?:(keys:string[])=>void;saving?:(pending:boolean)=>void;error?:(message:string)=>void};
type DataLayer=L.Layer & {feature?:EditableGeometry;toGeoJSON?:()=>GeoJSON.Feature|GeoJSON.FeatureCollection;pm?:any};
const icon=L.divIcon({className:'earth-drawn-point',html:'',iconSize:[14,14]});
export function drawingControls(map:L.Map,initial:EditableGeometry[],events:Events) {
  const pm=map.pm,group=L.featureGroup().addTo(map);
  let kind:DrawingKind|undefined,before:EditableGeometry[]|undefined,snapping=true,tolerance=12,saving=false,disposed=false;
  let undo:EditableGeometry[][]=[],redo:EditableGeometry[][]=[];
  let targets:Set<string>|undefined;
  const targeted=(layer:DataLayer)=>!targets || !!layer.feature && targets.has(selectionKey(layer.feature));
  const enableEditing=()=>group.eachLayer(layer=>{if(targeted(layer as DataLayer))(layer as DataLayer).pm?.enable(options());});
  const shapeNames={point:'Marker',polyline:'Line',polygon:'Polygon',rectangle:'Rectangle'} as const;
  function featureOf(layer:DataLayer):EditableGeometry {
    const value=layer.toGeoJSON?.();
    if(!value)throw new Error('该对象没有可编辑几何。');
    let geometry:GeoJSON.Geometry;
    if(value.type==='FeatureCollection') {
      const parts=value.features.map(x=>x.geometry);
      if(parts.every(x=>x.type==='Polygon'||x.type==='MultiPolygon'))geometry={type:'MultiPolygon',coordinates:parts.flatMap(x=>x.type==='Polygon'?[x.coordinates]:(x as GeoJSON.MultiPolygon).coordinates)};
      else throw new Error('切割结果类型不一致。');
    }else geometry=value.geometry;
    return {...layer.feature,type:'Feature',geometry,properties:structuredClone(layer.feature?.properties ?? {})} as EditableGeometry;
  }
  const all=()=>group.getLayers().map(layer=>featureOf(layer as DataLayer));
  function attach(layer:DataLayer,feature:EditableGeometry) {
    layer.feature=structuredClone(feature);
    L.Util.setOptions(layer,{bubblingMouseEvents:true,pmIgnore:false});
    L.PM.reInitLayer(layer);
    layer.on('click',(event:L.LeafletMouseEvent)=>{
      // Draw and cut listen on the map; existing draft surfaces must not
      // absorb their vertex clicks. Selection/edit/remove stay layer-owned.
      if(!saving&&kind&&!['edit','remove'].includes(kind))return;
      L.DomEvent.stopPropagation(event);
      if(saving)return;
      if(kind==='remove'){
        if(!targeted(layer))return;
        if((layer.feature as EditableGeometry & {pawLayerId?:string})?.pawLayerId){events.error?.('不能删除已保存的项目要素；“删除草稿”只用于本机草稿。');return;}
        group.removeLayer(layer);checkpoint();return;
      }
      if(!kind)events.select(featureOf(layer),'select');
    });
    layer.on('pm:edit pm:dragend',()=>{if(kind==='edit')checkpoint();});
    group.addLayer(layer);
  }
  function add(feature:EditableGeometry) {
    if(saving||disposed)return;
    if(all().some(item=>selectionKey(item)===selectionKey(feature)))return;
    L.geoJSON(feature,{pointToLayer:(_feature,point)=>L.marker(point,{icon})}).eachLayer(layer=>attach(layer as DataLayer,feature));
  }
  function replace(features:EditableGeometry[]) {
    group.eachLayer(layer=>(layer as DataLayer).pm?.disable());group.clearLayers();features.forEach(add);
    if(kind==='edit')enableEditing();
    preview();
  }
  const options=()=>({snappable:snapping,snapDistance:tolerance,snapSegment:true,snapVertex:true,allowSelfIntersection:false,continueDrawing:false});
  function preview(){events.previewRemoved?.((before ?? []).filter(feature=>!all().some(item=>selectionKey(item)===selectionKey(feature))).map(selectionKey));}
  function checkpoint(){preview();const state=all();if(JSON.stringify(state)!==JSON.stringify(undo.at(-1))){undo.push(structuredClone(state));redo=[];}}
  function disableModes(){pm.disableDraw();pm.disableGlobalCutMode();group.eachLayer(layer=>(layer as DataLayer).pm?.disable());}
  function end(){events.previewRemoved?.([]);targets=undefined;kind=undefined;before=undefined;undo=[];redo=[];events.active(false);}
  function cleanupCopies(){group.getLayers().filter(layer=>(layer as DataLayer).feature && ((layer as DataLayer).feature as any).pawLayerId).forEach(layer=>group.removeLayer(layer));}
  function cancel(){if(saving||disposed)return;disableModes();kind=undefined;if(before)replace(before);cleanupCopies();end();}
  function start(next:DrawingKind,selected?:EditableGeometry[]){
    if(saving||disposed)return;
    if(kind)cancel();
    selected?.forEach(add);
    targets=selected ? new Set(selected.map(selectionKey)) : undefined;
    kind=next;before=structuredClone(all());undo=[structuredClone(before)];redo=[];
    events.active(true,next,0);
    if(next==='edit')enableEditing();
    else if(next==='cut'){const cutOptions={...options(),layersToCut:group.getLayers().filter(layer=>targeted(layer as DataLayer))};pm.enableGlobalCutMode(cutOptions);}
    else if(next!=='remove')pm.enableDraw(shapeNames[next],{...options(),markerStyle:{icon},pathOptions:{color:'#c08422'},finishOn:'dblclick'});
  }
  async function save(){
    if(saving||disposed||!kind||!['edit','cut','remove'].includes(kind))return;
    saving=true;events.saving?.(true);
    try {
      disableModes();const current=structuredClone(all());const prior=before ?? [];
      const removed=prior.filter(feature=>!current.some(x=>selectionKey(x)===selectionKey(feature)));
      await events.change(current,removed);
      if(disposed)return;
      prior.filter(feature=>!current.some(x=>selectionKey(x)===selectionKey(feature))).forEach(feature=>events.select(feature,'remove'));
      group.getLayers().filter(layer=>targeted(layer as DataLayer)).forEach(layer=>events.select(featureOf(layer as DataLayer),'upsert'));
      cleanupCopies();end();
    } catch(reason) {
      if(disposed)return;
      events.error?.(`几何保存失败：${reason instanceof Error?reason.message:String(reason)}。草稿已保留，可重试保存或取消。`);
      if(kind==='edit')enableEditing();
      else if(kind==='cut'){ const cutOptions = {...options(),layersToCut:group.getLayers().filter(layer=>targeted(layer as DataLayer))}; pm.enableGlobalCutMode(cutOptions); }
    } finally {
      saving=false;if(!disposed)events.saving?.(false);
    }
  }
  function created(event:any){
    if(!kind||kind==='cut')return;
    const layer=event.layer as DataLayer;
    const geometry=layer.toGeoJSON?.();if(!geometry||geometry.type!=='Feature')return;
    map.removeLayer(layer);
    const feature={...geometry,id:crypto.randomUUID(),properties:{name:`${geometry.geometry.type} ${group.getLayers().length+1}`,source:'user_drawing'}} as EditableGeometry;
    attach(layer,feature);
    try {Promise.resolve(events.change(all())).catch(reason=>{if(!disposed)events.error?.(`本机草稿未保存：${reason instanceof Error?reason.message:String(reason)}。新绘制对象仍保留在地图上。`);});}
    catch(reason){events.error?.(`本机草稿未保存：${reason instanceof Error?reason.message:String(reason)}。新绘制对象仍保留在地图上。`);}
    events.select(feature,'upsert');end();
  }
  function cut(event:any){
    if(saving||kind!=='cut'||!group.hasLayer(event.originalLayer))return;
    const original=featureOf(event.originalLayer);
    group.removeLayer(event.originalLayer);map.removeLayer(event.layer);
    const replacement=event.layer as DataLayer;replacement.feature=original;
    const feature=featureOf(replacement);
    if((feature.geometry.type==='MultiPolygon'||feature.geometry.type==='Polygon') && !feature.geometry.coordinates.length){
      if((original as EditableGeometry & {pawLayerId?:string}).pawLayerId){
        add(original);
        events.error?.('不能整块挖除已保存的项目要素，原几何已恢复。请保留部分几何；本机草稿可以删除。');
      }else checkpoint();
      return;
    }
    attach(replacement,feature);checkpoint();events.active(true,'cut');
  }
  function drawStart(event:any){event.workingLayer?.on('pm:vertexadded pm:vertexremoved',()=>{
    const points=event.workingLayer.getLatLngs?.();const count=Array.isArray(points?.[0])?points[0].length:points?.length ?? 0;
    events.active(true,kind,count);
  });}
  initial.forEach(add);pm.setGlobalOptions(options());
  map.on('pm:create',created).on('pm:cut',cut).on('pm:drawstart',drawStart);
  return {add,start,save,cancel,removeSelected(features:EditableGeometry[]){
    if(saving||disposed||!features.length)return;
    start('remove',features);
    group.getLayers().filter(layer=>targeted(layer as DataLayer)).forEach(layer=>group.removeLayer(layer));
    checkpoint();
  },finish(){if(!saving&&(kind==='polygon'||kind==='polyline')){const handler=(pm.Draw as any)[shapeNames[kind]];handler?._finishShape();}},
    setSnapping(enabled:boolean,pixels:number){if(saving)return;snapping=enabled;tolerance=Math.max(1,Math.min(80,pixels));pm.setGlobalOptions(options());group.eachLayer(layer=>(layer as DataLayer).pm?.setOptions(options()));},
    undo(){if(saving||undo.length<2)return;redo.push(undo.pop()!);replace(undo.at(-1)!);},redo(){if(saving)return;const next=redo.pop();if(next){undo.push(next);replace(next);}},
    focus(id:string){if(saving)return;const feature=all().find(f=>f.id===id);if(feature){const bounds=L.geoJSON(feature).getBounds();if(bounds.isValid())map.fitBounds(bounds,{padding:[30,30],maxZoom:17});events.select(feature,'select');}},
    dispose(){disposed=true;disableModes();map.off('pm:create',created).off('pm:cut',cut).off('pm:drawstart',drawStart);group.remove();},
  };
}
