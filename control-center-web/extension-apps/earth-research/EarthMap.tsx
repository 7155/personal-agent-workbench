import { useEffect, useMemo, useRef, useState } from 'react';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import { geoJsonOutputs, type EarthRun } from './workspace';
import type { EarthViewCommand } from './pi-package/view-contract';
import { drawingControls, type EditableGeometry } from './drawing-controls';
import { selectionKey, type SelectionMode } from './map-selection';

declare global { interface Window { google?: { maps?: unknown } } }

const googleMapsKey = import.meta.env.VITE_GOOGLE_MAPS_API_KEY as string | undefined;
let googleMapsLoader: Promise<void> | undefined;
function loadGoogleMapsApi() {
  if (!googleMapsKey || typeof window === 'undefined') return Promise.resolve();
  if (window.google?.maps) return Promise.resolve();
  if (googleMapsLoader) return googleMapsLoader;
  googleMapsLoader = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(googleMapsKey)}&v=weekly`;
    script.async = true; script.defer = true;
    script.onload = () => resolve(); script.onerror = () => reject(new Error('Google Maps JavaScript API 加载失败'));
    document.head.appendChild(script);
  });
  return googleMapsLoader;
}

export function EarthMap({ run, onSelect, command, selection, workspaceKey, onActivity }: { run: EarthRun | null; onSelect: (feature: GeoJSON.Feature | null,mode?:SelectionMode) => void; command?: EarthViewCommand; selection: GeoJSON.Feature[]; workspaceKey: string; onActivity: () => void }) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const [multiSelect,setMultiSelect]=useState(true);
  const callback=useRef<(feature:GeoJSON.Feature|null,mode?:SelectionMode|'select')=>void>(()=>{});
  callback.current=(feature,mode='select')=>onSelect(feature,mode==='select' ? multiSelect ? 'toggle' : 'replace' : mode);
  const rasterLayers = useRef(new Map<string, L.Layer>());
  const baseLayers = useRef<Record<string, L.Layer>>({});
  const layerControl = useRef<L.Control.Layers | null>(null);
  const [tileError, setTileError] = useState(false);
  const [drawing, setDrawing] = useState(false);
  const [imports, setImports] = useState<EditableGeometry[]>([]);
  const [storageError,setStorageError] = useState('');
  const geometryTools=useRef<ReturnType<typeof drawingControls> | undefined>(undefined);
  const activity=useRef(onActivity);activity.current=onActivity;
  const drawingRef = useRef(false); drawingRef.current = drawing;
  const suppressClick = useRef(0);
  useEffect(() => {
    if (!container.current) return;
    void loadGoogleMapsApi().catch(() => undefined);
    const instance = L.map(container.current, { zoomControl: true }).setView([30, 110], 4);
    map.current = instance;
    // Google satellite is the product-facing basemap. Keep a same-provider
    // roads layer as the fallback so the map does not depend on a tile host
    // that rejects the gateway's privacy referrer policy.
    const satellite = L.tileLayer('https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}', { maxZoom: 20, attribution: 'Google satellite imagery' });
    const roads = L.tileLayer('https://mt1.google.com/vt/lyrs=m&x={x}&y={y}&z={z}', { maxZoom: 20, attribution: 'Google road map' });
    satellite.on('tileerror', () => setTileError(true));
    roads.on('tileerror', () => setTileError(true));
    satellite.addTo(instance);
    baseLayers.current = { 'Google 卫星影像': satellite, '道路地图（备用）': roads };
    layerControl.current = L.control.layers(baseLayers.current, undefined, { collapsed: false }).addTo(instance);
    instance.on('click', (event: L.LeafletMouseEvent) => {
      if (Date.now() < suppressClick.current) return;
      if (!drawingRef.current) callback.current({ type: 'Feature', geometry: { type: 'Point', coordinates: [event.latlng.lng, event.latlng.lat] }, properties: { source: 'user_selection' } });
    });
    const resize = new ResizeObserver(() => instance.invalidateSize()); resize.observe(container.current);
    return () => { resize.disconnect(); layerControl.current?.remove(); layerControl.current = null; instance.remove(); map.current = null; };
  }, []);
  useEffect(() => {
    const instance=map.current;if(!instance)return;
    const storageKey=`paw-earth-geometries:${workspaceKey}`;
    let initial:EditableGeometry[]=[];
    try {const raw=JSON.parse(localStorage.getItem(storageKey)||'[]');if(Array.isArray(raw)&&raw.length<=200) initial=raw.filter(f=>f?.type==='Feature' && ['Point','LineString','Polygon'].includes(f.geometry?.type));}
    catch {setStorageError('本机几何未能恢复，原始记录没有被清除。');}
    setImports(initial);
    const tools=drawingControls(instance,initial,{
      select:(feature,mode)=>{suppressClick.current=Date.now()+250;callback.current(feature,mode);},
      change:features=>{setImports(features);try {localStorage.setItem(storageKey,JSON.stringify(features));setStorageError('');}catch {setStorageError('未能保存本机几何，请先复制代码或保留当前窗口。');}},
      active:value=>{drawingRef.current=value;setDrawing(value);if(value)activity.current();else suppressClick.current=Date.now()+250;},
    });geometryTools.current=tools;
    return()=>{tools.dispose();geometryTools.current=undefined;};
  },[workspaceKey]);
  useEffect(() => {
    const instance=map.current; if(!instance || !selection.length) return;
    const collection:GeoJSON.FeatureCollection={type:'FeatureCollection',features:selection};
    const halo=L.geoJSON(collection,{interactive:false,style:{color:'#fff',weight:9,fill:false},pointToLayer:(_f,point)=>L.circleMarker(point,{radius:11,color:'#fff',weight:3,fillOpacity:0,interactive:false})}).addTo(instance);
    const highlight=L.geoJSON(collection,{interactive:false,style:{color:'#d08a19',weight:4,fillOpacity:0.16},pointToLayer:(_f,point)=>L.circleMarker(point,{radius:8,color:'#d08a19',fillOpacity:0.4,interactive:false})}).addTo(instance);
    return()=>{instance.removeLayer(highlight);instance.removeLayer(halo);};
  },[selection]);
  useEffect(() => {
    const instance = map.current; if (!instance || !run) return;
    rasterLayers.current.clear();
    const overlays: L.Layer[] = [];
    const choices: Record<string, L.Layer> = {};
    for (const layer of run.layers) {
      if (!layer.tileUrl) continue;
      const item = L.tileLayer(layer.tileUrl, { opacity: layer.opacity, attribution: 'Google Earth Engine', maxZoom: 20 });
      item.on('tileerror', () => setTileError(true));
      overlays.push(item); choices[layer.name] = item;
      rasterLayers.current.set(layer.id, item);
      if (layer.shown) item.addTo(instance);
    }
    for (const output of geoJsonOutputs(run)) {
      const layer = L.geoJSON(output.geojson, {
        style: { color: '#14795c', weight: 3, fillOpacity: 0.1 },
        pointToLayer: (_f, point) => L.circleMarker(point, { radius: 6, color: '#14795c' }),
        onEachFeature: (feature, item) => {
          L.Util.setOptions(item,{bubblingMouseEvents:false});
          const label = document.createElement('span'); label.textContent = String(feature.properties?.name ?? feature.properties?.id ?? output.label); item.bindTooltip(label);
          item.on('click', (event: L.LeafletMouseEvent) => { if(drawingRef.current || Date.now() < suppressClick.current) return; if (event.originalEvent) L.DomEvent.stopPropagation(event.originalEvent); callback.current(feature); });
          item.on('add', () => {
            const element = item instanceof L.Path ? item.getElement() : undefined;
            if (!element) return;
            element.setAttribute('tabindex', '0'); element.setAttribute('role', 'button'); element.setAttribute('aria-label', label.textContent || output.label);
            element.addEventListener('keydown', event => { if (event instanceof KeyboardEvent && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); event.stopPropagation(); callback.current(feature); } });
          });
        },
      }).addTo(instance);
      overlays.push(layer); choices[output.label] = layer;
    }
    Object.entries(choices).forEach(([label, layer]) => layerControl.current?.addOverlay(layer, label));
    return () => { overlays.forEach(layer => { layerControl.current?.removeLayer(layer); instance.removeLayer(layer); }); };
  }, [run?.runId, run?.updatedAt]);
  const center = run?.view?.center; const zoom = run?.view?.zoom;
  useEffect(() => {
    if (center && zoom !== undefined) map.current?.setView([center[1], center[0]], zoom);
  }, [run?.runId, center?.[0], center?.[1], zoom]);
  useEffect(() => {
    const instance = map.current; if (!instance || !command) return;
    if (command.action === 'focus' && command.center) instance.setView([command.center[1],command.center[0]],command.zoom);
    if (command.action === 'layer') { const layer = rasterLayers.current.get(command.layerId!); if(layer) { if(command.visible) layer.addTo(instance); else instance.removeLayer(layer); } }
    if (command.action === 'feature') {
      for (const output of geoJsonOutputs(run)) {
        const data = output.geojson as GeoJSON.FeatureCollection | GeoJSON.Feature;
        const feature = (data.type === 'FeatureCollection' ? data.features : data.type === 'Feature' ? [data] : []).find(item => String(item.id ?? item.properties?.id ?? '') === command.featureId);
        if (feature) { const bounds = L.geoJSON(feature).getBounds(); if(bounds.isValid()) instance.fitBounds(bounds, { padding:[24,24], maxZoom:16 }); callback.current(feature,'upsert'); break; }
      }
    }
  }, [command?.requestId]);
  const available=useMemo(()=>{
    const resultFeatures=geoJsonOutputs(run).flatMap(output=>{const value=output.geojson as GeoJSON.FeatureCollection|GeoJSON.Feature;return value.type==='FeatureCollection'?value.features:[value];});
    return [...new Map([...imports,...resultFeatures,...selection].map(feature=>[selectionKey(feature),feature])).values()];
  },[imports,run,selection]);
  const selectedKeys=new Set(selection.map(selectionKey));
  return <div className="earth-map" data-drawing={drawing}><div ref={container} aria-label="地理分析地图" className="earth-map__canvas" />
    <div className="earth-map-tools"><div className="earth-gis-toolbar" aria-label="GEE 几何工具"><button onClick={()=>geometryTools.current?.start('point')}>点</button><button onClick={()=>geometryTools.current?.start('polyline')}>线</button><button onClick={()=>geometryTools.current?.start('polygon')}>面</button><button onClick={()=>geometryTools.current?.start('rectangle')}>框选</button><span aria-hidden="true" className="earth-gis-toolbar__divider"/><button onClick={()=>geometryTools.current?.start('edit')}>编辑</button><button onClick={()=>geometryTools.current?.start('remove')}>删除</button></div><button aria-pressed={multiSelect} onClick={()=>setMultiSelect(value=>!value)}>多选{multiSelect ? '开' : '关'}</button><span role="status">{drawing ? '绘图／编辑中 · 完成或保存后更新选择' : multiSelect ? '点击可增选或取消 · 可在几何列表批量选择' : '点击选中一个对象'}</span></div>
    <details className="earth-geometry-imports"><summary>几何与选择 · {selection.length} 已选</summary>
      <div className="earth-geometry-actions"><button onClick={()=>available.forEach(feature=>callback.current(feature,'upsert'))}>全选</button><button disabled={!selection.length} onClick={()=>callback.current(null)}>清空选择</button></div>
      {available.map(feature=><div className="earth-geometry-row" key={selectionKey(feature)}><label><input type="checkbox" checked={selectedKeys.has(selectionKey(feature))} onChange={()=>callback.current(feature,'toggle')}/><span>{String(feature.properties?.name ?? feature.properties?.id ?? feature.id ?? '几何')}</span></label><details><summary>GEE 代码</summary><pre>{`var geometry = ee.Geometry(${JSON.stringify(feature.geometry)}, null, false);`}</pre></details></div>)}
      {!available.length ? <p>绘制点、线和区域后可多选，统一交给 Agent。</p> : null}
      <small>绘制几何保留在本机；分析结果来自当前运行。</small>
      {storageError ? <p role="alert">{storageError}</p> : null}
    </details>
    {tileError ? <p className="earth-map__notice" role="status">底图或结果图层暂未载入。可在右上角切换“道路地图（备用）”，或让 Agent 重新生成图层。</p> : null}
    {!run ? <p className="earth-map__notice">选择地点开始任务，运行后的真实图层会显示在这里。</p> : null}
  </div>;
}
