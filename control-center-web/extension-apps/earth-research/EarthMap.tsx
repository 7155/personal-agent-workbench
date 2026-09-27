import { useEffect, useMemo, useRef, useState } from 'react';
import { MapDrawingToolbar } from './MapDrawingToolbar';
import L from 'leaflet';
import { layerSymbol, MAP_SYMBOLS } from './map-symbols';
import 'leaflet/dist/leaflet.css';
import { geoJsonOutputs, type EarthRun } from './workspace';
import type { EarthMapState, EarthViewCommand } from './pi-package/view-contract';
import { drawingControls, type DrawingKind, type EditableGeometry } from './drawing-controls';
import { selectionKey, type SelectionMode } from './map-selection';
import type { ProjectLayer, SpatialSourceDraft, SpatialSourceSummary, WorkspaceFileSummary } from './layer-catalog';
import { EarthDataDock, type EarthDataDockProps } from './EarthDataDock';

declare global { interface Window { google?: { maps?: unknown } } }
const EMPTY_PROJECT_LAYERS: ProjectLayer[] = [];

type EarthMapProps = Partial<Omit<EarthDataDockProps,'workspaceRoot'|'selectedFeatures'|'onSelectFeature'>> & {
  run:EarthRun|null;
  onSelect:(feature:GeoJSON.Feature|null,mode?:SelectionMode)=>void;
  command?:EarthViewCommand; selection:GeoJSON.Feature[];workspaceKey:string;onActivity:()=>void;
  onMapState?:(state:EarthMapState)=>void;
  onCommandApplied?:(command:EarthViewCommand,status:'applied'|'rejected',reason?:string)=>void;
  localResult?:Record<string,any>|null;
  onUpdateFeatures?:(features:GeoJSON.Feature[],removed?:GeoJSON.Feature[])=>Promise<void>;
};
export function EarthMap({ dockView, onDockViewChange, onOpenCloud, run, onSelect, command, selection, workspaceKey, onActivity, onMapState, onCommandApplied, projectLayers = EMPTY_PROJECT_LAYERS, spatialSources = [], workspaceFiles = [], workspaceFilesIncomplete, workspaceFilesNotice, activeObjectLabel, onSaveLayer, onUpdateFeature, onUpdateFeatures, onCreateBundle, onExportLayer, onToggleLayer, onRemoveLayer, onConnectSource, onRefreshCatalog, onRefreshFiles, onOpenFile, localRuns,localResult,onLoadSourceLayer,onShowRun,onCompareRuns,onOpenLayerRevision }: EarthMapProps) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const [multiSelect,setMultiSelect]=useState(false);
  const callback=useRef<(feature:GeoJSON.Feature|null,mode?:SelectionMode|'select')=>void>(()=>{});
  callback.current=(feature,mode='select')=>onSelect(feature,mode==='select' ? multiSelect ? 'toggle' : 'replace' : mode);
  const rasterLayers = useRef(new Map<string, L.Layer>());
  const projectLayerRefs = useRef(new Map<string, L.Layer>());
  const initiallyFocusedWorkspace = useRef<string | null>(null);
  const baseLayers = useRef<Record<string, L.Layer>>({});
  const layerControl = useRef<L.Control.Layers | null>(null);
  const [tileError, setTileError] = useState(false);
  const [drawing, setDrawing] = useState(false);
  const [geometrySaving, setGeometrySaving] = useState(false);
  const [snapping,setSnapping]=useState(true);
  const [snapPixels,setSnapPixels]=useState(12);
  const [drawingKind, setDrawingKind] = useState<DrawingKind>();
  const [drawingVertexCount, setDrawingVertexCount] = useState(0);
  const [imports, setImports] = useState<EditableGeometry[]>([]);
  const [storageError,setStorageError] = useState('');
  const [pendingRemoved,setPendingRemoved]=useState<string[]>([]);
  const editingSelectionKeys=JSON.stringify(drawingKind==='edit'||drawingKind==='cut' ? selection.map(selectionKey) : []);
  const geometryTools=useRef<ReturnType<typeof drawingControls> | undefined>(undefined);
  const activity=useRef(onActivity);activity.current=onActivity;
  const drawingRef = useRef(false); drawingRef.current = drawing;
  const suppressClick = useRef(0);
  const stateTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const stateCallback = useRef(onMapState); stateCallback.current = onMapState;
  const commandApplied = useRef(onCommandApplied); commandApplied.current = onCommandApplied;
  const selected = useRef(selection); selected.current = selection;
  const projectData=useRef(projectLayers);projectData.current=projectLayers;
  const updateFeatures=useRef(onUpdateFeatures);updateFeatures.current=onUpdateFeatures;
  const updateFeature=useRef(onUpdateFeature);updateFeature.current=onUpdateFeature;
  useEffect(() => {
    if (!container.current) return;
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
    layerControl.current = L.control.layers(baseLayers.current, undefined, { collapsed: true, position: 'bottomright' }).addTo(instance);
    const layerToggle = layerControl.current.getContainer()?.querySelector<HTMLAnchorElement>('.leaflet-control-layers-toggle');
    if (layerToggle) { layerToggle.title = '底图与叠加图层'; layerToggle.setAttribute('aria-label', '底图与叠加图层'); }
    instance.on('click', (event: L.LeafletMouseEvent) => {
      if (Date.now() < suppressClick.current) return;
      if (!drawingRef.current && !event.originalEvent?.shiftKey) callback.current(null);
    });
    let resizeFrame = 0;
    const resize = new ResizeObserver(() => { cancelAnimationFrame(resizeFrame); resizeFrame = requestAnimationFrame(() => instance.invalidateSize({ pan: false, debounceMoveend: true })); }); resize.observe(container.current);
    const publish = () => {
      if (!stateCallback.current) return;
      clearTimeout(stateTimer.current);
      stateTimer.current = setTimeout(() => {
        const center = instance.getCenter(); const bounds = instance.getBounds();
        const visibleLayerIds = [...rasterLayers.current.entries()].filter(([, layer]) => instance.hasLayer(layer)).map(([id]) => id);
        stateCallback.current?.({ schemaVersion: 'earth.map-state.v1', center: [center.lng, center.lat], zoom: instance.getZoom(), bounds: [bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()], visibleLayerIds, selectedFeatureIds: selected.current.map(selectionKey), updatedAt: new Date().toISOString() });
      }, 120);
    };
    instance.on('moveend zoomend overlayadd overlayremove', publish);
    publish();
    return () => { clearTimeout(stateTimer.current); cancelAnimationFrame(resizeFrame); resize.disconnect(); layerControl.current?.remove(); layerControl.current = null; instance.remove(); map.current = null; };
  }, []);
  useEffect(() => {
    const instance=map.current;if(!instance)return;
    const storageKey=`paw-earth-geometries:${workspaceKey}`;
    let initial:EditableGeometry[]=[];
    try {const raw=JSON.parse(localStorage.getItem(storageKey)||'[]');if(Array.isArray(raw)&&raw.length<=200) initial=raw.filter(f=>f?.type==='Feature' && ['Point','LineString','Polygon'].includes(f.geometry?.type));}
    catch {setStorageError('本机几何未能恢复，原始记录没有被清除。');}
    setImports(initial);
    const tools=drawingControls(instance,initial,{
      select:(feature,mode)=>{
        suppressClick.current=Date.now()+250;
        const ownerId=(feature as EditableGeometry & {pawLayerId?:string}|null)?.pawLayerId;
        const saved=ownerId && mode==='upsert' ? projectData.current.find(layer=>layer.id===ownerId)?.features.find(item=>item.id===feature?.id) : undefined;
        // A successful project save may have advanced the revision while the
        // drawing adapter awaited it. Keep the authoritative bound feature.
        callback.current(saved && JSON.stringify(saved.geometry)===JSON.stringify(feature?.geometry) ? saved : feature,mode);
      },
      change:async (features,removed=[])=>{
        const deleted=removed.filter(feature=>(feature as any).pawLayerId);
        const local=features.filter(feature=>!(feature as any).pawLayerId);
        const changed=features.filter(feature=> {
          if(!(feature as any).pawLayerId)return false;
          const owner=projectData.current.find(layer=>layer.id===(feature as any).pawLayerId);
          const original=owner?.features.find(item=>item.id===feature.id);
          if(!original)throw new Error('项目要素已经变更，请重新载入后再编辑。');
          return JSON.stringify(original.geometry)!==JSON.stringify(feature.geometry);
        });
        if(changed.length || deleted.length) {
          if(deleted.length && !updateFeatures.current)throw new Error('当前项目没有可用的删除保存入口。');
          if(!updateFeatures.current&&!updateFeature.current)throw new Error('当前项目没有可用的几何保存入口。');
          await (updateFeatures.current ? updateFeatures.current(changed,...(deleted.length ? [deleted] : [])) : Promise.all(changed.map(feature=>updateFeature.current!(feature))));
        }
        try {localStorage.setItem(storageKey,JSON.stringify(local));setStorageError('');}
        catch {
          if(!changed.length && !deleted.length)throw new Error('本机草稿未保存，请保留当前窗口。');
          setStorageError('项目几何已保存，但本机草稿未写入，请保留当前窗口。');
        }
        setImports(local);
      },
      previewRemoved:setPendingRemoved,
      saving:pending=>{setGeometrySaving(pending);if(pending)setStorageError('');},
      error:setStorageError,
    active:(value,kind,vertices)=>{drawingRef.current=value;setDrawing(value);setDrawingKind(value ? kind : undefined);setDrawingVertexCount(value ? vertices ?? 0 : 0);if(value)activity.current();else suppressClick.current=Date.now()+250;},
    });geometryTools.current=tools;
    return()=>{tools.dispose();geometryTools.current=undefined;};
  },[workspaceKey]);
  useEffect(() => {
    const instance=map.current; if(!instance) return;
    const collection:GeoJSON.FeatureCollection={type:'FeatureCollection',features:selection.filter(feature=>!pendingRemoved.includes(selectionKey(feature)) && drawingKind!=='edit' && drawingKind!=='cut')};
    const halo=selection.length ? L.geoJSON(collection,{interactive:false,style:{color:'#fff',weight:9,fill:false},pointToLayer:(_f,point)=>L.circleMarker(point,{radius:11,color:'#fff',weight:3,fillOpacity:0,interactive:false})}).addTo(instance) : undefined;
    const highlight=selection.length ? L.geoJSON(collection,{interactive:false,style:{color:MAP_SYMBOLS.selected.color,weight:3,fillOpacity:0.2},pointToLayer:(_f,point)=>L.circleMarker(point,{radius:8,color:MAP_SYMBOLS.selected.color,fillOpacity:0.6,interactive:false})}).addTo(instance) : undefined;
    const timer = setTimeout(() => {
      const center = instance.getCenter(); const bounds = instance.getBounds();
      stateCallback.current?.({ schemaVersion: 'earth.map-state.v1', center: [center.lng, center.lat], zoom: instance.getZoom(), bounds: [bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()], visibleLayerIds: [...rasterLayers.current.entries()].filter(([, layer]) => instance.hasLayer(layer)).map(([id]) => id), selectedFeatureIds: selection.map(selectionKey), updatedAt: new Date().toISOString() });
    }, 0);
    return()=>{clearTimeout(timer);if (highlight) instance.removeLayer(highlight);if (halo) instance.removeLayer(halo);};
  },[selection,pendingRemoved,drawingKind]);
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
        style: { color: MAP_SYMBOLS.result.color, weight: 2.5, fillOpacity: 0.1, className: 'earth-vector' },
        pointToLayer: (_f, point) => L.circleMarker(point, { radius: 6, color: MAP_SYMBOLS.result.color, className: 'earth-vector' }),
        onEachFeature: (feature, item) => {
          L.Util.setOptions(item,{bubblingMouseEvents:true});
          const label = document.createElement('span'); label.textContent = String(feature.properties?.name ?? feature.properties?.id ?? output.label); item.bindTooltip(label);
          item.on('click', (event: L.LeafletMouseEvent) => {
            if(drawingRef.current)return;
            L.DomEvent.stopPropagation(event);
            if(Date.now() < suppressClick.current)return;
            callback.current(feature,event.originalEvent?.shiftKey ? 'toggle' : 'select');
          });
          item.on('add', () => {
            const element = item instanceof L.Path ? item.getElement() : undefined;
            if (!element) return;
            element.setAttribute('tabindex', '0'); element.setAttribute('role', 'button'); element.setAttribute('aria-label', label.textContent || output.label);
            element.addEventListener('keydown', event => { if (event instanceof KeyboardEvent && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); event.stopPropagation(); callback.current(feature,event.shiftKey ? 'toggle' : 'select'); } });
          });
        },
      }).addTo(instance);
      overlays.push(layer); choices[output.label] = layer;
    }
    Object.entries(choices).forEach(([label, layer]) => layerControl.current?.addOverlay(layer, label));
    return () => { overlays.forEach(layer => { layerControl.current?.removeLayer(layer); instance.removeLayer(layer); }); };
  }, [run?.runId, run?.updatedAt]);
  useEffect(() => {
    const instance = map.current;
    if (!instance) return;
    for (const layer of projectLayerRefs.current.values()) {
      layerControl.current?.removeLayer(layer);
      instance.removeLayer(layer);
    }
    projectLayerRefs.current.clear();
    const removed = new Set(pendingRemoved), editing = new Set<string>(JSON.parse(editingSelectionKeys));
    for (const projectLayer of projectLayers) {
      if (!projectLayer.features.length) continue;
      const symbol = layerSymbol(projectLayer.geometryTypes);
      const layer = L.geoJSON({ type: 'FeatureCollection', features: projectLayer.features.filter(feature=>!removed.has(selectionKey(feature)) && !editing.has(selectionKey(feature))) } as GeoJSON.GeoJsonObject, {
        style: { color: symbol.color, weight: symbol.shape === 'line' ? 3 : 1.8, fillColor: symbol.color, fillOpacity: 0.035, className: 'earth-vector' },
        pointToLayer: (_feature, point) => L.circleMarker(point, { radius: 6, color: '#fff', fillColor: symbol.color, fillOpacity: 1, weight: 2, className: 'earth-vector' }),
        onEachFeature: (feature, item) => {
          L.Util.setOptions(item,{bubblingMouseEvents:true});
          const label=document.createElement('span');label.textContent=`${projectLayer.name} · ${String(feature.properties?.name ?? feature.properties?.id ?? projectLayer.name)}`;
          item.bindTooltip(label);
          item.on('click', (event: L.LeafletMouseEvent) => {
            // During drawing Geoman owns map clicks, including clicks inside
            // existing features. Selection consumes the Leaflet event itself.
            if (drawingRef.current) return;
            L.DomEvent.stopPropagation(event);
            if (Date.now() < suppressClick.current) return;
            callback.current(feature,event.originalEvent?.shiftKey ? 'toggle' : 'select');
          });
        },
      });
      projectLayerRefs.current.set(projectLayer.id, layer);
      if (projectLayer.visible !== false) layer.addTo(instance);
      layerControl.current?.addOverlay(layer, `项目 · ${projectLayer.name}`);
    }
    return () => {
      for (const layer of projectLayerRefs.current.values()) {
        layerControl.current?.removeLayer(layer);
        instance.removeLayer(layer);
      }
      projectLayerRefs.current.clear();
    };
  }, [projectLayers,pendingRemoved,editingSelectionKeys]);
  useEffect(() => {
    const instance = map.current;
    if (!instance || initiallyFocusedWorkspace.current === workspaceKey) return;
    // A displayed analysis owns its view. Otherwise fit saved data once when
    // the project first loads, so later catalog refreshes never steal the map.
    if (run || localResult) { initiallyFocusedWorkspace.current = workspaceKey; return; }
    const visible = projectLayers.filter(layer => layer.visible !== false).flatMap(layer => {
      const mapLayer = projectLayerRefs.current.get(layer.id);
      return mapLayer ? [mapLayer] : [];
    });
    const bounds = L.featureGroup(visible).getBounds();
    if (!bounds.isValid()) return;
    initiallyFocusedWorkspace.current = workspaceKey;
    instance.fitBounds(bounds, { padding: [28, 28], maxZoom: 17 });
  }, [projectLayers, workspaceKey, run, localResult]);
  useEffect(()=> {
    const instance=map.current;if(!instance || !localResult)return;
    const overlays:L.Layer[]=[];
    for(const output of localResult.outputs ?? []) {
      if(!output.geojson)continue;
      const layer=L.geoJSON(output.geojson,{style:{color:MAP_SYMBOLS.result.color,weight:2.5,fillOpacity:.12,className:'earth-vector'},onEachFeature:(feature,item)=>{
        L.Util.setOptions(item,{bubblingMouseEvents:true});
        item.on('click',(event:L.LeafletMouseEvent)=>{
          if(drawingRef.current)return;
          L.DomEvent.stopPropagation(event);
          if(Date.now() < suppressClick.current)return;
          callback.current({...feature,pawLayerId:`run:${localResult.runId}:${output.relativePath}`} as GeoJSON.Feature,event.originalEvent?.shiftKey ? 'toggle' : 'select');
        });
      }}).addTo(instance);
      layerControl.current?.addOverlay(layer,`运行 ${localResult.runId.slice(0,8)} · ${output.name}`);overlays.push(layer);
    }
    if(localResult.preview?.dataUrl?.startsWith('data:image/png;base64,')&&localResult.preview.bounds?.length===4){
      const [west,south,east,north]=localResult.preview.bounds;
      const preview=L.imageOverlay(localResult.preview.dataUrl,[[south,west],[north,east]],{opacity:.8}).addTo(instance);
      overlays.push(preview);layerControl.current?.addOverlay(preview,`运行 ${localResult.runId.slice(0,8)} · 分类/指数预览`);
    }
    const bounds=L.featureGroup(overlays).getBounds();if(bounds.isValid())instance.fitBounds(bounds,{padding:[32,32],maxZoom:17});
    return()=>overlays.forEach(layer=>{layerControl.current?.removeLayer(layer);instance.removeLayer(layer);});
  },[localResult?.runId]);
  const center = run?.view?.center; const zoom = run?.view?.zoom;
  useEffect(() => {
    if (center && zoom !== undefined) map.current?.setView([center[1], center[0]], zoom);
  }, [run?.runId, center?.[0], center?.[1], zoom]);
  useEffect(() => {
    const instance = map.current;
    if (!instance || !command || command.action === 'panel') return;
    try {
      if (command.action === 'focus' && command.center) {
        instance.setView([command.center[1], command.center[0]], command.zoom, { animate: false });
      } else if (command.action === 'layer') {
        const layer = rasterLayers.current.get(command.layerId!);
        if (!layer) throw new Error('当前地图不存在该图层。');
        if (command.visible) layer.addTo(instance); else instance.removeLayer(layer);
      } else if (command.action === 'feature') {
        const feature = geoJsonOutputs(run).flatMap(output => {
          const data = output.geojson as GeoJSON.FeatureCollection | GeoJSON.Feature;
          return data.type === 'FeatureCollection' ? data.features : [data];
        }).find(item => String(item.id ?? item.properties?.id ?? '') === command.featureId);
        if (!feature) throw new Error('当前地图不存在该要素。');
        const bounds = L.geoJSON(feature).getBounds();
        if (!bounds.isValid()) throw new Error('要素没有可定位的有效范围。');
        instance.fitBounds(bounds, { padding: [24, 24], maxZoom: 16, animate: false });
        callback.current(feature, 'upsert');
      } else throw new Error('视图命令缺少有效参数。');
      commandApplied.current?.(command, 'applied');
    } catch (error) { commandApplied.current?.(command, 'rejected', error instanceof Error ? error.message : String(error)); }
  }, [command?.requestId]);

  const available=useMemo(()=>{
    const resultFeatures=geoJsonOutputs(run).flatMap(output=>{const value=output.geojson as GeoJSON.FeatureCollection|GeoJSON.Feature;return value.type==='FeatureCollection'?value.features:[value];});
    const savedFeatures = projectLayers.flatMap(layer => layer.features);
    return [...new Map([...imports,...savedFeatures,...resultFeatures,...selection].map(feature=>[selectionKey(feature),feature])).values()];
  },[imports,projectLayers,run,selection]);
  const selectedKeys=new Set(selection.map(selectionKey));
  // An empty selection means “nothing selected”, not “every feature”.  Bulk
  // operations must be explicit through the visible 全选 action below.
  const selectedFeatures = selection;
  const selectionNeedsProjectLayer = selection.some(feature => {
    const ownerId = (feature as GeoJSON.Feature & { pawLayerId?: string }).pawLayerId;
    return ownerId !== undefined && (ownerId.startsWith('run:') || !projectLayers.some(layer => layer.id === ownerId));
  });
  const readOnlySelectionHint = '所选要素需先保存为项目图层，才能编辑、删除或挖洞。请使用“将地图所选保存为新图层”。';
  function editSelection(kind: 'edit' | 'cut') {
    if (selectionNeedsProjectLayer || !selection.length) return;
    geometryTools.current?.start(kind,selection as EditableGeometry[]);
  }
  function selectTableFeature(feature: GeoJSON.Feature, layerId: string) {
    const boundFeature = { ...feature, pawLayerId: layerId };
    const wasSelected = selection.some(item => (item as GeoJSON.Feature & { pawLayerId?: string }).pawLayerId === layerId && item.id !== undefined && item.id === feature.id);
    // Table checkboxes always add/remove one row, independently of the map's
    // single-click selection mode. Preserve the typed ID and owning layer.
    callback.current(boundFeature, 'toggle');
    if (!wasSelected) {
      const bounds = L.geoJSON(feature).getBounds();
      if (bounds.isValid()) map.current?.fitBounds(bounds, { padding: [36, 36], maxZoom: 17 });
    }
  }
  const legendSymbols: Array<(typeof MAP_SYMBOLS)[keyof typeof MAP_SYMBOLS]> = [...new Set(projectLayers.filter(layer => layer.visible !== false).map(layer => layerSymbol(layer.geometryTypes)))];
  if (localResult?.outputs?.some((output: { geojson?: unknown }) => output.geojson) || geoJsonOutputs(run).length) legendSymbols.push(MAP_SYMBOLS.result);
  if (selection.length) legendSymbols.push(MAP_SYMBOLS.selected);
  return <div className="earth-map" data-drawing={drawing} data-dock-open={Boolean(dockView)} onKeyDown={event=>{
      const target=event.target as HTMLElement;
      if(!target.closest('.earth-map__canvas,.earth-map-tools') || target.closest('input,textarea,select,[contenteditable=true]'))return;
      if(event.key==='Escape'){event.preventDefault();geometryTools.current?.cancel();}
      else if(drawing && event.key==='Enter'){event.preventDefault();if(drawingKind==='polygon'||drawingKind==='polyline')geometryTools.current?.finish();else if(['edit','cut','remove'].includes(drawingKind ?? ''))void geometryTools.current?.save();}
      else if((drawingKind==='polygon'||drawingKind==='polyline') && (event.key==='Backspace'||event.key==='Delete')){event.preventDefault();geometryTools.current?.undoVertex();}
    }}><div ref={container} tabIndex={0} onPointerDown={event=>event.currentTarget.focus({preventScroll:true})} aria-label="地理分析地图" className="earth-map__canvas" />
    <div className="earth-map-tools">
      <MapDrawingToolbar drawing={drawing} kind={drawingKind} saving={geometrySaving} vertexCount={drawingVertexCount}
        selectedCount={selection.length} canEdit={!selectionNeedsProjectLayer} canCut={!selectionNeedsProjectLayer && selection.some(feature => ['Polygon', 'MultiPolygon'].includes(feature.geometry.type))}
        readOnlyHint={selectionNeedsProjectLayer ? readOnlySelectionHint : undefined} snapping={snapping} tolerance={snapPixels} multiSelect={multiSelect}
        onDraw={kind => { geometryTools.current?.start(kind); container.current?.focus({ preventScroll: true }); }}
        onEdit={kind => { editSelection(kind); container.current?.focus({ preventScroll: true }); }}
        onRemove={() => { geometryTools.current?.removeSelected(selection as EditableGeometry[]); container.current?.focus({ preventScroll: true }); }}
        onCancel={() => geometryTools.current?.cancel()} onSave={() => { void geometryTools.current?.save(); }} onFinish={() => geometryTools.current?.finish()}
        onUndo={() => geometryTools.current?.undo()} onRedo={() => geometryTools.current?.redo()} onUndoVertex={() => geometryTools.current?.undoVertex()}
        onSnapping={(enabled, pixels) => { setSnapping(enabled); setSnapPixels(pixels); geometryTools.current?.setSnapping(enabled, pixels); }} onMultiSelect={setMultiSelect} />
      <span role="status" hidden={!drawing && !geometrySaving && !selectionNeedsProjectLayer && !multiSelect}>{geometrySaving ? '正在保存几何，完成后可继续编辑' : drawingKind === 'edit' ? '拖动顶点后点击“保存”更新几何' : drawingKind === 'cut' ? '绘制内部范围，完成后保存；取消可还原' : drawingKind === 'remove' ? '所选对象已暂存删除 · 保存后生效，撤销或取消可恢复' : drawingKind === 'polygon' ? `面：已添加 ${drawingVertexCount} 个点 · 继续点击添加，双击或“完成”结束` : drawingKind === 'polyline' ? `线：已添加 ${drawingVertexCount} 个点 · 继续点击添加，双击或“完成”结束` : drawingKind === 'point' ? '单击放置点 · Esc 取消' : drawingKind === 'rectangle' ? '拖动绘制矩形 · Esc 取消' : drawing ? '绘图中 · Esc 取消' : selectionNeedsProjectLayer ? readOnlySelectionHint : selection.length ? `已选 ${selection.length} 个 · 可编辑或删除所选` : multiSelect ? '点击选择，再次点击取消选择' : '单击选择 · Shift 多选 · 空白处取消选择'}</span>
    </div>
    <EarthDataDock dockView={dockView} onDockViewChange={onDockViewChange} onOpenCloud={onOpenCloud} run={run} workspaceRoot={workspaceKey} projectLayers={projectLayers} spatialSources={spatialSources} workspaceFiles={workspaceFiles} workspaceFilesIncomplete={workspaceFilesIncomplete} workspaceFilesNotice={workspaceFilesNotice} activeObjectLabel={activeObjectLabel} selectedFeatures={selectedFeatures} onSaveLayer={onSaveLayer} onUpdateFeature={onUpdateFeature} onCreateBundle={onCreateBundle} localRuns={localRuns} onShowRun={onShowRun} onCompareRuns={onCompareRuns} onLoadSourceLayer={onLoadSourceLayer} onOpenLayerRevision={onOpenLayerRevision} onSelectFeature={selectTableFeature} onExportLayer={onExportLayer} onToggleLayer={onToggleLayer} onRemoveLayer={onRemoveLayer} onConnectSource={onConnectSource} onRefreshCatalog={onRefreshCatalog} onRefreshFiles={onRefreshFiles} onOpenFile={onOpenFile} />
    {legendSymbols.length ? <div className="earth-map-legend" aria-label="地图图例">{legendSymbols.map(symbol => <span key={symbol.label}><i aria-hidden="true" data-shape={symbol.shape} style={{ backgroundColor: symbol.color, borderColor: symbol.ink }} />{symbol.label}</span>)}</div> : null}
    <details className="earth-geometry-imports" hidden={!available.length}><summary>几何与选择 · {selection.length} 已选</summary>
      <div className="earth-geometry-actions"><button onClick={()=>available.forEach(feature=>callback.current(feature,'upsert'))}>全选</button><button disabled={!selection.length} onClick={()=>callback.current(null)}>清空选择</button></div>
      {available.map(feature=><div className="earth-geometry-row" key={selectionKey(feature)}><label><input type="checkbox" checked={selectedKeys.has(selectionKey(feature))} onChange={()=>callback.current(feature,'toggle')}/><span>{String(feature.properties?.name ?? feature.properties?.id ?? feature.id ?? '几何')}</span></label><details><summary>GEE 代码</summary><pre>{`var geometry = ee.Geometry(${JSON.stringify(feature.geometry)}, null, false);`}</pre></details></div>)}
      {!available.length ? <p>绘制点、线和区域后可多选，统一交给 Agent。</p> : null}
      <small>绘制几何保留在本机；分析结果来自当前运行。</small>
    </details>
    {storageError ? <p className="earth-map__save-error" role="alert">{storageError}</p> : null}
    {tileError && !storageError ? <p className="earth-map__notice" role="status">底图或结果图层暂未载入。可在右下角切换“道路地图（备用）”，或让 Agent 重新生成图层。</p> : null}
    {!run && !projectLayers.length && !imports.length && !storageError && !tileError ? <p className="earth-map__notice">在地图上选择地点，或导入项目图层开始分析。</p> : null}
  </div>;
}
