import L from 'leaflet';
import 'leaflet-draw';
import 'leaflet-draw/dist/leaflet.draw.css';
import type { SelectionMode } from './map-selection';

export type EditableGeometry = GeoJSON.Feature<GeoJSON.Point | GeoJSON.LineString | GeoJSON.Polygon>;
export type DrawingKind = 'point'|'polyline'|'polygon'|'rectangle'|'edit'|'remove';
type EditableLayer = L.Layer & { feature?: EditableGeometry };
const markerIcon=L.divIcon({className:'earth-drawn-point',html:'',iconSize:[14,14]});
export function drawingControls(map:L.Map, initial:EditableGeometry[], events:{
  select:(feature:EditableGeometry|null,mode?:SelectionMode|'select')=>void;
  change:(features:EditableGeometry[])=>void;
  active:(active:boolean,kind?:DrawingKind)=>void;
}) {
  Object.assign(L.drawLocal.draw.toolbar.buttons,{polyline:'绘制折线',polygon:'绘制多边形',rectangle:'绘制矩形',marker:'绘制点'});
  Object.assign(L.drawLocal.draw.toolbar.actions,{title:'取消绘制',text:'取消'});
  Object.assign(L.drawLocal.draw.toolbar.finish,{title:'完成绘制',text:'完成'});
  Object.assign(L.drawLocal.draw.toolbar.undo,{title:'删除最后一个顶点',text:'撤销顶点'});
  Object.assign(L.drawLocal.edit.toolbar.buttons,{edit:'编辑几何',editDisabled:'先绘制几何再编辑',remove:'删除几何',removeDisabled:'没有可删除的几何'});
  Object.assign(L.drawLocal.edit.toolbar.actions.save,{title:'保存修改',text:'保存'});
  Object.assign(L.drawLocal.edit.toolbar.actions.cancel,{title:'放弃修改',text:'取消'});
  Object.assign(L.drawLocal.edit.toolbar.actions.clearAll,{title:'删除全部绘制几何',text:'全部删除'});
  L.drawLocal.draw.handlers.polyline.tooltip={start:'点击地图添加起点',cont:'点击添加顶点',end:'点击最后一个点或“完成”结束'};
  L.drawLocal.draw.handlers.polygon.tooltip={start:'点击地图添加第一个顶点',cont:'点击继续添加顶点',end:'点击起点或“完成”闭合多边形'};
  L.drawLocal.draw.handlers.rectangle.tooltip.start='按住并拖动绘制矩形';
  L.drawLocal.draw.handlers.marker.tooltip.start='点击地图放置点';
  L.drawLocal.edit.handlers.edit.tooltip={text:'拖动点或顶点调整几何',subtext:'保存后更新输入框；取消可撤销'};
  L.drawLocal.edit.handlers.remove.tooltip.text='点击要删除的几何，然后保存';
  const group=L.featureGroup().addTo(map);
  let active=false;
  let activeKind:DrawingKind|undefined;
  let activeHandler:L.Handler|undefined;
  function layerFeature(layer:L.Layer):EditableGeometry {
    const current=(layer as EditableLayer).feature;
    const next=(layer as L.Polyline).toGeoJSON() as EditableGeometry;
    if (!current) return next;
    return {
      ...next,
      id: current.id,
      properties: { ...current.properties },
    } as EditableGeometry;
  }
  function syncLayerFeature(layer:L.Layer):EditableGeometry {
    const feature=layerFeature(layer);
    (layer as EditableLayer).feature=feature;
    return feature;
  }
  function attach(layer:L.Layer,feature:EditableGeometry) {
    L.Util.setOptions(layer,{bubblingMouseEvents:false});
    Object.assign(layer,{feature:structuredClone(feature)});
    layer.on('click',(event:L.LeafletMouseEvent)=>{if(active)return;if(event.originalEvent)L.DomEvent.stopPropagation(event.originalEvent);events.select(layerFeature(layer),'select');});
    group.addLayer(layer);
  }
  for(const feature of initial) {
    L.geoJSON(feature,{pointToLayer:(_feature,point)=>L.marker(point,{icon:markerIcon})}).eachLayer(layer=>attach(layer,feature));
  }
  const control=new L.Control.Draw({position:'topleft',draw:{polyline:{shapeOptions:{color:'#d08a19'}},polygon:{allowIntersection:false,showArea:true,shapeOptions:{color:'#d08a19'}},rectangle:{shapeOptions:{color:'#d08a19'}},marker:{icon:markerIcon},circle:false,circlemarker:false},edit:{featureGroup:group}});
  map.addControl(control);
  const all=()=>group.getLayers().map(layer=>layerFeature(layer));
  const created=(event:L.LeafletEvent)=>{
    const {layer,layerType}=event as L.DrawEvents.Created;
    const feature=(layer as L.Polyline).toGeoJSON() as EditableGeometry;
    feature.id=crypto.randomUUID();feature.properties={...feature.properties,name:`${({marker:'点',polyline:'线',polygon:'多边形',rectangle:'矩形'} as Record<string,string>)[layerType] ?? '几何'} ${group.getLayers().length+1}`,source:'user_drawing'};
    attach(layer,feature);events.change(all());events.select(feature,'upsert');
  };
  const edited=(event:L.LeafletEvent)=>{const changed:EditableGeometry[]=[];(event as L.DrawEvents.Edited).layers.eachLayer(layer=>changed.push(syncLayerFeature(layer)));events.change(all());changed.forEach(feature=>events.select(feature,'upsert'));};
  const deleted=(event:L.LeafletEvent)=>{const removed:EditableGeometry[]=[];(event as L.DrawEvents.Deleted).layers.eachLayer(layer=>removed.push(layerFeature(layer)));events.change(all());removed.forEach(feature=>events.select(feature,'remove'));};
  const start=(event:L.LeafletEvent)=>{active=true;const detail=event as L.DrawEvents.Created & {handler?:string};activeKind=(detail.handler as DrawingKind|undefined) ?? ((detail.layerType==='marker' ? 'point' : detail.layerType) as DrawingKind|undefined);events.active(true,activeKind);};
  const stop=()=>{active=false;activeKind=undefined;activeHandler=undefined;events.active(false);};
  function cancelActive() {
    if (!activeHandler) return;
    const handler=activeHandler as L.Handler & {revertLayers?:()=>void};
    handler.revertLayers?.();
    handler.disable();
    activeHandler=undefined;
  }
  map.on('draw:created',created).on('draw:edited',edited).on('draw:deleted',deleted)
    .on('draw:drawstart draw:editstart draw:deletestart',start).on('draw:drawstop draw:editstop draw:deletestop',stop);
  return {start(kind:DrawingKind) {
      cancelActive();
      const drawMap = map as any;
      const handler = kind==='point' ? new L.Draw.Marker(drawMap,{icon:markerIcon}) : kind==='polyline' ? new L.Draw.Polyline(drawMap,{shapeOptions:{color:'#d08a19'}}) : kind==='polygon' ? new L.Draw.Polygon(drawMap,{shapeOptions:{color:'#d08a19'}}) : kind==='rectangle' ? new L.Draw.Rectangle(drawMap,{shapeOptions:{color:'#d08a19'}}) : kind==='edit' ? new L.EditToolbar.Edit(drawMap,{featureGroup:group}) : new L.EditToolbar.Delete(drawMap,{featureGroup:group});
      activeKind=kind;
      activeHandler=handler;
      handler.enable();
      if (!(handler as L.Handler & {_enabled?:boolean})._enabled) {
        activeHandler=undefined;
        activeKind=undefined;
        events.active(false);
      }
    },save(){
      if (!activeHandler || (activeKind!=='edit' && activeKind!=='remove')) return;
      const handler=activeHandler as L.Handler & {save?:()=>void};
      handler.save?.();
      handler.disable();
    },cancel(){cancelActive();},dispose(){cancelActive();map.off('draw:created',created).off('draw:edited',edited).off('draw:deleted',deleted)
    .off('draw:drawstart draw:editstart draw:deletestart',start).off('draw:drawstop draw:editstop draw:deletestop',stop);control.remove();group.remove();},
    focus(id:string){const feature=all().find(f=>f.id===id);if(feature){const bounds=L.geoJSON(feature).getBounds();if(bounds.isValid())map.fitBounds(bounds,{maxZoom:17,padding:[30,30]});events.select(feature,'select');}},
  };
}
