import { useState } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import L from 'leaflet';
import { afterEach, expect, it, vi } from 'vitest';
import { EarthMap } from './EarthMap';
import { updateSelection, type MapSelection } from './map-selection';
import type { ProjectLayer } from './layer-catalog';
const originalSvg=L.Browser.svg;
afterEach(()=>{cleanup();Reflect.set(L.Browser,'svg',originalSvg);vi.restoreAllMocks();vi.unstubAllGlobals();localStorage.clear();});
it.each((['project', 'cloud', 'local', 'draft'] as const).flatMap(source => (['polygon', 'polyline'] as const).map(kind => ({ source, kind }))))('routes real layer clicks through an existing $source polygon in $kind mode without duplicating ordinary selection', ({ source, kind }) => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  Reflect.set(L.Browser, 'svg', true);
  const feature: GeoJSON.Feature = { type: 'Feature', id: 'existing', properties: { name: '已有面' }, geometry: { type: 'Polygon', coordinates: [[[120, 30], [120.1, 30], [120.1, 30.1], [120, 30.1], [120, 30]]] } };
  const project: ProjectLayer = { id: 'project', name: '已有项目面', path: '.earth/layers/existing.geojson', format: 'geojson', featureCount: 1, geometryTypes: ['Polygon'], crs: 'EPSG:4326', updatedAt: '', visible: true, features: [feature] };
  const workspaceKey = `drawing-through-${source}-${kind}`;
  if (source === 'draft') localStorage.setItem(`paw-earth-geometries:${workspaceKey}`, JSON.stringify([feature]));
  const run = source === 'cloud' ? { schemaVersion: 'earth.run.v1' as const, runId: 'cloud', status: 'completed' as const, code: '', scriptPath: '', project: '', sourceHash: '', startedAt: '', updatedAt: '', layers: [], console: [{ values: [feature], pending: false }] } : null;
  const factory = vi.spyOn(L, 'map'), selected = vi.fn();
  render(<EarthMap run={run} localResult={source === 'local' ? { runId: 'local', outputs: [{ name: 'result', relativePath: 'result.geojson', geojson: feature }] } : null} selection={[]} projectLayers={source === 'project' ? [project] : []} workspaceKey={workspaceKey} onActivity={vi.fn()} onSelect={selected} />);
  const map = factory.mock.results[0].value as L.Map;
  Object.defineProperties(map.getContainer(), { clientWidth: { configurable: true, value: 1000 }, clientHeight: { configurable: true, value: 800 } });
  act(() => { map.invalidateSize(); map.setView([30.05, 120.05], 14, { animate: false }); });
  let layer: L.Polygon | undefined;
  map.eachLayer(item => { if (item instanceof L.Polygon && (item as any).feature?.id === 'existing') layer = item; });
  expect(layer).toBeDefined();
  const path = layer!.getElement()!, layerClicks = vi.fn(), mapClicks = vi.fn();
  layer!.on('click', layerClicks); map.on('click', mapClicks);
  const clickLayer = (position: L.LatLngTuple) => {
    const point = map.latLngToContainerPoint(position);
    // Dispatch to the real SVG target: Leaflet must produce layer.click with
    // originalEvent and decide whether that same click reaches the map.
    fireEvent.mouseMove(path, { clientX: point.x, clientY: point.y });
    fireEvent.click(path, { clientX: point.x, clientY: point.y });
  };
  clickLayer([30.05, 120.05]);
  expect(layerClicks).toHaveBeenCalledWith(expect.objectContaining({ originalEvent: expect.any(MouseEvent) }));
  expect(selected).toHaveBeenCalledTimes(1);
  expect(selected).toHaveBeenCalledWith(expect.objectContaining({ id: 'existing', geometry: feature.geometry }), 'replace');
  expect(mapClicks).not.toHaveBeenCalled();
  selected.mockClear(); layerClicks.mockClear();
  const positions: L.LatLngTuple[] = [[30.02, 120.02], [30.02, 120.08], [30.06, 120.09], [30.08, 120.05], [30.06, 120.02]];
  for (let index = 1; index < positions.length; index += 1) expect(map.latLngToContainerPoint(positions[index]).distanceTo(map.latLngToContainerPoint(positions[index - 1]))).toBeGreaterThan(50);
  fireEvent.click(screen.getByRole('button', { name: kind === 'polygon' ? '面' : '线' }));
  for (const position of positions) clickLayer(position);
  expect(layerClicks).toHaveBeenCalledTimes(5);
  expect(mapClicks).toHaveBeenCalledTimes(5);
  expect(screen.getByRole('status')).toHaveTextContent('5 个点');
  fireEvent.click(screen.getByRole('button',{name:'撤销顶点'}));
  expect(screen.getByRole('status')).toHaveTextContent('4 个点');
  act(()=>{map.fire('mousemove',{latlng:L.latLng(30.08,119.95),originalEvent:new MouseEvent('mousemove')});map.fire('click',{latlng:L.latLng(30.08,119.95),originalEvent:new MouseEvent('click')});});
  expect(selected).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '完成' }));
  expect(selected).toHaveBeenCalledTimes(1);
  const drawn = selected.mock.lastCall?.[0] as GeoJSON.Feature<GeoJSON.Polygon | GeoJSON.LineString>;
  expect(drawn.geometry.type).toBe(kind === 'polygon' ? 'Polygon' : 'LineString');
  const coordinates = drawn.geometry.type === 'Polygon' ? drawn.geometry.coordinates[0] : drawn.geometry.coordinates;
  expect(coordinates).toHaveLength(kind === 'polygon' ? 6 : 5);
  const vertices = drawn.geometry.type === 'Polygon' ? coordinates.slice(0, -1) : coordinates;
  expect(new Set(vertices.map(position => position.join(','))).size).toBe(5);
  const saved = JSON.parse(localStorage.getItem(`paw-earth-geometries:${workspaceKey}`)!);
  expect(saved.find((item: GeoJSON.Feature) => item.id === drawn.id).geometry).toEqual(drawn.geometry);
});

it('draws more than three vertices using the real Geoman handler and persists the closed geometry',()=>{
  vi.stubGlobal('ResizeObserver',class {observe(){}disconnect(){}});Reflect.set(L.Browser,'svg',true);
  const factory=vi.spyOn(L,'map'),selected=vi.fn();
  render(<EarthMap run={null} selection={[]} workspaceKey="polygon-test" onActivity={vi.fn()} onSelect={selected}/>);
  const map=factory.mock.results[0].value as L.Map;
  act(()=>{map.setView([30.05,120.05],14);});
  expect(screen.getByRole('button',{name:'面'})).toHaveAttribute('aria-pressed','false');
  fireEvent.click(screen.getByRole('button',{name:'面'}));
  expect(screen.getByRole('button',{name:'面'})).toHaveAttribute('aria-pressed','true');
  expect(screen.getByRole('button',{name:'线'})).toHaveAttribute('aria-pressed','false');
  for(const [lat,lng] of [[30,120],[30,120.1],[30.1,120.15],[30.15,120.05],[30.08,119.95]]) {
    act(()=>{map.fire('mousemove',{latlng:L.latLng(lat,lng),originalEvent:new MouseEvent('mousemove')});map.fire('click',{latlng:L.latLng(lat,lng),originalEvent:new MouseEvent('click')});});
  }
  expect(screen.getByRole('status')).toHaveTextContent('5 个点');
  fireEvent.click(screen.getByRole('button',{name:'撤销顶点'}));
  expect(screen.getByRole('status')).toHaveTextContent('4 个点');
  act(()=>{map.fire('mousemove',{latlng:L.latLng(30.08,119.95),originalEvent:new MouseEvent('mousemove')});map.fire('click',{latlng:L.latLng(30.08,119.95),originalEvent:new MouseEvent('click')});});
  fireEvent.click(screen.getByRole('button',{name:'完成'}));
  expect(screen.getByRole('button',{name:'面'})).toHaveAttribute('aria-pressed','false');
  const feature=selected.mock.lastCall?.[0];
  expect(feature.geometry.type).toBe('Polygon');expect(feature.geometry.coordinates[0]).toHaveLength(6);
  expect(JSON.parse(localStorage.getItem('paw-earth-geometries:polygon-test')!)[0].id).toBe(feature.id);
});
it('keeps edit and cut in a cancellable draft; saved cut retains identity and a real interior ring',()=>{
  vi.stubGlobal('ResizeObserver',class {observe(){}disconnect(){}});Reflect.set(L.Browser,'svg',true);
  const feature:GeoJSON.Feature={type:'Feature',id:'parcel',properties:{note:'unchanged'},geometry:{type:'Polygon',coordinates:[[[120,30],[120.1,30],[120.1,30.1],[120,30.1],[120,30]]]}};
  localStorage.setItem('paw-earth-geometries:cut-test',JSON.stringify([feature]));
  const factory=vi.spyOn(L,'map'),selected=vi.fn();
  render(<EarthMap run={null} selection={[feature]} workspaceKey="cut-test" onActivity={vi.fn()} onSelect={selected}/>);
  const map=factory.mock.results[0].value as L.Map;
  act(()=>{map.setView([30.05,120.05],14);});
  fireEvent.click(screen.getByRole('button',{name:'编辑所选'}));
  let owned:L.Polygon|undefined;map.eachLayer(layer=>{if(layer instanceof L.Polygon && (layer as any).feature?.id==='parcel')owned=layer;});
  act(()=>{owned!.setLatLngs([[30,120],[30,120.2],[30.1,120.1],[30.1,120]]);owned!.fire('pm:edit');});
  fireEvent.click(screen.getByRole('button',{name:'取消'}));
  expect(JSON.parse(localStorage.getItem('paw-earth-geometries:cut-test')!)[0]).toEqual(feature);
  fireEvent.click(screen.getByRole('button',{name:'挖洞'}));
  // Draw a real cut polygon through Geoman; it computes the polygon difference.
  for(const [lat,lng] of [[30.02,120.02],[30.02,120.04],[30.04,120.04],[30.04,120.02]])act(()=>{map.fire('mousemove',{latlng:L.latLng(lat,lng),originalEvent:new MouseEvent('mousemove')});map.fire('click',{latlng:L.latLng(lat,lng),originalEvent:new MouseEvent('click')});});
  act(()=>{(map.pm.Draw as any).Cut._finishShape();});
  fireEvent.click(screen.getByRole('button',{name:'保存'}));
  const saved=JSON.parse(localStorage.getItem('paw-earth-geometries:cut-test')!)[0];
  expect(saved.id).toBe('parcel');expect(saved.properties).toEqual(feature.properties);
  expect(saved.geometry.coordinates).toHaveLength(2);
});

it('starts with satellite imagery and keeps the lower-right basemap control collapsed until opened', () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  Reflect.set(L.Browser, 'svg', true);
  const tileLayer = vi.spyOn(L, 'tileLayer');
  render(<EarthMap run={null} selection={[]} workspaceKey="basemap-test" onActivity={vi.fn()} onSelect={vi.fn()} />);

  expect(tileLayer).toHaveBeenCalledWith(
    expect.stringContaining('mt1.google.com/vt/lyrs=s'),
    expect.objectContaining({ attribution: 'Google satellite imagery', maxZoom: 20 }),
  );
  expect(tileLayer).toHaveBeenCalledWith(
    expect.stringContaining('mt1.google.com/vt/lyrs=m'),
    expect.objectContaining({ attribution: 'Google road map', maxZoom: 20 }),
  );
  expect(screen.getByText('Google 卫星影像')).not.toBeVisible();
  const toggle = screen.getByRole('button', { name: '底图与叠加图层' });
  expect(toggle.closest('.leaflet-bottom.leaflet-right')).not.toBeNull();
  fireEvent.click(toggle);
  expect(screen.getByText('Google 卫星影像')).toBeVisible();
  expect(screen.getByText('道路地图（备用）')).toBeVisible();
});

it('manages project layers and submits a secret-safe spatial database connection', async () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  Reflect.set(L.Browser, 'svg', true);
  const feature: GeoJSON.Feature = { type: 'Feature', id: 'parcel-1', properties: { name: '候选地块' }, geometry: { type: 'Point', coordinates: [120, 30] } };
  const layer = { id: 'layer:roads', name: '道路候选', path: '.earth/layers/roads.geojson', format: 'geojson' as const, featureCount: 1, geometryTypes: ['Point'], crs: 'EPSG:4326', updatedAt: '2026-09-19T00:00:00Z', visible: true, features: [feature] };
  const onSaveLayer = vi.fn().mockResolvedValue(undefined);
  const onToggleLayer = vi.fn().mockResolvedValue(undefined);
  const onRemoveLayer = vi.fn().mockResolvedValue(undefined);
  const onConnectSource = vi.fn().mockResolvedValue(undefined);
  render(<EarthMap run={null} selection={[feature]} projectLayers={[layer]} workspaceKey="layer-test" onActivity={vi.fn()} onSelect={vi.fn()} onSaveLayer={onSaveLayer} onToggleLayer={onToggleLayer} onRemoveLayer={onRemoveLayer} onConnectSource={onConnectSource} />);
  expect(screen.getByText('道路候选')).toBeVisible();
  fireEvent.click(screen.getByRole('checkbox', { name: '道路候选 可见' }));
  expect(onToggleLayer).toHaveBeenCalledWith('layer:roads', false);
  await act(async()=>{});
  fireEvent.click(screen.getByText('版本与图层管理'));
  fireEvent.click(screen.getByRole('button', { name: '从目录移除 道路候选' }));
  expect(onRemoveLayer).toHaveBeenCalledWith('layer:roads');
  await act(async()=>{});
  fireEvent.click(screen.getByText('将地图所选保存为新图层'));
  fireEvent.click(screen.getByRole('button', { name: '保存图层' }));
  expect(onSaveLayer).toHaveBeenCalledWith('候选区域', [feature]);
  await act(async()=>{});
  fireEvent.click(screen.getByRole('tab',{name:'数据库'}));
  fireEvent.change(screen.getByRole('combobox', { name: '空间数据源类型' }), { target: { value: 'postgis' } });
  fireEvent.change(screen.getByRole('textbox', { name: 'PostGIS 密钥引用' }), { target: { value: 'PAW_POSTGIS_URL' } });
  fireEvent.click(screen.getByRole('button', { name: '连接并读取目录' }));
  expect(onConnectSource).toHaveBeenCalledWith({ name: '项目数据库', kind: 'postgis', secretReference: 'PAW_POSTGIS_URL' });
});

it('does not treat an empty selection as every available feature', () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  Reflect.set(L.Browser, 'svg', true);
  const feature: GeoJSON.Feature = { type: 'Feature', id: 'parcel-1', properties: { name: '候选地块' }, geometry: { type: 'Point', coordinates: [120, 30] } };
  const onSaveLayer = vi.fn();
  const layer = { id: 'layer:roads', name: '道路候选', path: '.earth/layers/roads.geojson', format: 'geojson' as const, featureCount: 1, geometryTypes: ['Point'], crs: 'EPSG:4326', updatedAt: '2026-09-19T00:00:00Z', visible: true, features: [feature] };
  render(<EarthMap run={null} selection={[]} projectLayers={[layer]} workspaceKey="empty-selection" onActivity={vi.fn()} onSelect={vi.fn()} onSaveLayer={onSaveLayer} />);
  expect(screen.getByRole('button', { name: '保存图层' })).toBeDisabled();
  expect(onSaveLayer).not.toHaveBeenCalled();
});

it.each(['run:analysis:result.geojson', 'layer:missing'])('requires a project copy before editing features owned by %s', async ownerId => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  Reflect.set(L.Browser, 'svg', true);
  const saved = { type: 'Feature', id: 'saved', pawLayerId: 'layer:saved', properties: { name: '项目地块' }, geometry: { type: 'Polygon', coordinates: [[[120, 30], [120.1, 30], [120.1, 30.1], [120, 30.1], [120, 30]]] } } as GeoJSON.Feature & { pawLayerId: string };
  const readOnly = { ...saved, id: 'result', pawLayerId: ownerId, properties: { name: '待保存结果' } };
  const layer: ProjectLayer = { id: 'layer:saved', name: '项目地块', path: '.earth/layers/saved.geojson', format: 'geojson', featureCount: 1, geometryTypes: ['Polygon'], crs: 'EPSG:4326', updatedAt: '', visible: true, features: [saved] };
  const onSaveLayer = vi.fn().mockResolvedValue(undefined), onUpdateFeatures = vi.fn().mockResolvedValue(undefined);
  const props = { run: null, workspaceKey: `readonly-${ownerId}`, projectLayers: [layer], onActivity: vi.fn(), onSelect: vi.fn(), onSaveLayer, onUpdateFeatures };
  // One read-only owner must also block a mixed selection of saved and result features.
  const view = render(<EarthMap {...props} selection={[saved, readOnly]} />);
  expect(screen.getByRole('button', { name: '编辑所选' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '挖洞' })).toBeDisabled();
  expect(screen.getByRole('status')).toHaveTextContent('需先保存为项目图层');
  fireEvent.click(screen.getByRole('button', { name: '编辑所选' }));
  fireEvent.click(screen.getByRole('button', { name: '挖洞' }));
  expect(screen.queryByRole('button', { name: '保存' })).not.toBeInTheDocument();
  expect(onUpdateFeatures).not.toHaveBeenCalled();
  expect(localStorage.getItem(`paw-earth-geometries:${props.workspaceKey}`)).toBeNull();

  fireEvent.click(screen.getByText('将地图所选保存为新图层'));
  expect(screen.getByRole('button', { name: '保存图层' })).toBeEnabled();
  fireEvent.click(screen.getByRole('button', { name: '保存图层' }));
  expect(onSaveLayer).toHaveBeenCalledWith('候选区域', [saved, readOnly]);
  await act(async () => {});

  view.rerender(<EarthMap {...props} selection={[saved]} />);
  expect(screen.getByRole('button', { name: '编辑所选' })).toBeEnabled();
  expect(screen.getByRole('button', { name: '挖洞' })).toBeEnabled();
  expect(screen.getByRole('status')).not.toHaveTextContent('需先保存为项目图层');
});

it('multi-selects table rows by typed ID and layer and does not move the map when unchecking', async () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  Reflect.set(L.Browser, 'svg', true);
  const point = (id: string | number, name: string, layerId: string): GeoJSON.Feature & { pawLayerId: string } => ({ type: 'Feature', id, pawLayerId: layerId, properties: { name }, geometry: { type: 'Point', coordinates: [120, 30] } });
  const numeric = point(1, '数字 ID 样本', 'layer:a');
  const text = point('1', '文本 ID 样本', 'layer:a');
  const foreign = point(1, '另一图层同 ID', 'layer:b');
  const layer: ProjectLayer = { id: 'layer:a', name: '训练样本', path: '.earth/layers/samples.geojson', format: 'geojson', featureCount: 2, geometryTypes: ['Point'], crs: 'EPSG:4326', updatedAt: '', revision: 1, visible: true, features: [numeric, text] };
  const selected = vi.fn();
  function Harness() {
    const [selection, setSelection] = useState<MapSelection | null>({ runId: null, features: [foreign] });
    return <EarthMap run={null} selection={selection?.features ?? []} projectLayers={[layer]} workspaceKey="table-selection" onActivity={vi.fn()} onSelect={(feature, mode) => { selected(feature, mode); setSelection(current => updateSelection(current, feature, mode ?? 'toggle', null)); }} />;
  }
  const factory = vi.spyOn(L, 'map');
  render(<Harness />);
  const map = factory.mock.results[0].value as L.Map;
  const fitBounds = vi.spyOn(map, 'fitBounds');
  // The table's checkboxes retain multi-selection even in single-click map mode.
  expect(screen.getByRole('button', { name: '多选关' })).toHaveAttribute('aria-pressed','false');
  fireEvent.click(screen.getByRole('tab', { name: '属性表' }));
  const numericCheckbox = () => within(screen.getByRole('row', { name: /数字 ID 样本/ })).getByRole('checkbox');
  const textCheckbox = () => within(screen.getByRole('row', { name: /文本 ID 样本/ })).getByRole('checkbox');
  expect(numericCheckbox()).not.toBeChecked();
  fireEvent.click(numericCheckbox());
  await waitFor(() => expect(numericCheckbox()).toBeEnabled());
  expect(numericCheckbox()).toBeChecked();
  fireEvent.click(textCheckbox());
  await waitFor(() => expect(textCheckbox()).toBeEnabled());
  expect(numericCheckbox()).toBeChecked();
  expect(textCheckbox()).toBeChecked();
  expect(screen.getByText('已选 2')).toBeVisible();
  expect(screen.getByText('几何与选择 · 3 已选')).toBeVisible();
  expect(selected).toHaveBeenNthCalledWith(1, expect.objectContaining({ id: 1, pawLayerId: 'layer:a' }), 'toggle');
  expect(selected).toHaveBeenNthCalledWith(2, expect.objectContaining({ id: '1', pawLayerId: 'layer:a' }), 'toggle');
  expect(fitBounds).toHaveBeenCalledTimes(2);
  fireEvent.click(numericCheckbox());
  await waitFor(() => expect(numericCheckbox()).toBeEnabled());
  expect(numericCheckbox()).not.toBeChecked();
  expect(textCheckbox()).toBeChecked();
  expect(screen.getByText('几何与选择 · 2 已选')).toBeVisible();
  expect(fitBounds).toHaveBeenCalledTimes(2);
});

it('retains a failed project geometry draft and only publishes selection after a successful retry', async () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  Reflect.set(L.Browser, 'svg', true);
  const feature = { type: 'Feature', id: 'parcel', pawLayerId: 'layer:parcels', pawRevision: 3, properties: { name: '地块' }, geometry: { type: 'Polygon', coordinates: [[[120, 30], [120.1, 30], [120.1, 30.1], [120, 30.1], [120, 30]]] } } as GeoJSON.Feature & { pawLayerId: string; pawRevision: number };
  const layer: ProjectLayer = { id: 'layer:parcels', name: '地块', path: '.earth/layers/parcels.geojson', format: 'geojson', featureCount: 1, geometryTypes: ['Polygon'], crs: 'EPSG:4326', updatedAt: '', revision: 3, visible: true, features: [feature] };
  let rejectSave!: (reason: Error) => void;
  const firstSave = new Promise<void>((_resolve, reject) => { rejectSave = reject; });
  const onUpdateFeatures = vi.fn().mockReturnValueOnce(firstSave).mockResolvedValue(undefined);
  const selected = vi.fn(), factory = vi.spyOn(L, 'map');
  render(<EarthMap run={null} selection={[feature]} projectLayers={[layer]} workspaceKey="save-recovery" onActivity={vi.fn()} onSelect={selected} onUpdateFeatures={onUpdateFeatures} />);
  const map = factory.mock.results[0].value as L.Map;
  fireEvent.click(screen.getByRole('button', { name: '编辑所选' }));
  let draft: L.Polygon | undefined;
  map.eachLayer(item => { if (item instanceof L.Polygon && (item as any).feature?.id === 'parcel' && (item as any).pm?.enabled()) draft = item; });
  expect(draft).toBeDefined();
  act(() => { draft!.setLatLngs([[30, 120], [30, 120.2], [30.1, 120.1], [30.1, 120]]); draft!.fire('pm:edit'); });
  const edited = (draft!.toGeoJSON() as GeoJSON.Feature).geometry;
  fireEvent.click(screen.getByRole('button', { name: '保存' }));
  expect(screen.getByRole('button', { name: '保存' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '取消' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '挖洞' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '保存' }));
  expect(onUpdateFeatures).toHaveBeenCalledTimes(1);
  expect(selected).not.toHaveBeenCalled();
  await act(async () => { rejectSave(new Error('版本冲突')); });
  expect(screen.getByRole('alert')).toHaveTextContent('草稿已保留');
  expect(screen.getByRole('alert')).toBeVisible();
  expect(screen.getByRole('button', { name: '保存' })).toBeEnabled();
  expect(map.hasLayer(draft!)).toBe(true);
  expect((draft!.toGeoJSON() as GeoJSON.Feature).geometry).toEqual(edited);
  expect(feature.geometry).not.toEqual(edited);
  expect(selected).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '保存' }));
  await waitFor(() => expect(screen.queryByRole('button', { name: '保存' })).not.toBeInTheDocument());
  expect(onUpdateFeatures).toHaveBeenCalledTimes(2);
  expect(onUpdateFeatures).toHaveBeenLastCalledWith([expect.objectContaining({ id: 'parcel', pawLayerId: 'layer:parcels', pawRevision: 3, geometry: edited })]);
  expect(selected).toHaveBeenCalledWith(expect.objectContaining({ id: 'parcel', geometry: edited }), 'upsert');
  expect(map.hasLayer(draft!)).toBe(false);
});

it('restores a saved project polygon when a real Geoman cut would remove it completely', async () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  Reflect.set(L.Browser, 'svg', true);
  const feature = { type: 'Feature', id: 'parcel', pawLayerId: 'layer:parcels', pawRevision: 2, properties: { note: '保留' }, geometry: { type: 'Polygon', coordinates: [[[120, 30], [120.1, 30], [120.1, 30.1], [120, 30.1], [120, 30]]] } } as GeoJSON.Feature & { pawLayerId: string; pawRevision: number };
  const layer: ProjectLayer = { id: 'layer:parcels', name: '项目地块', path: '.earth/layers/parcels.geojson', format: 'geojson', featureCount: 1, geometryTypes: ['Polygon'], crs: 'EPSG:4326', updatedAt: '', revision: 2, visible: true, features: [feature] };
  const onUpdateFeatures = vi.fn().mockResolvedValue(undefined), selected = vi.fn(), factory = vi.spyOn(L, 'map');
  render(<EarthMap run={null} selection={[feature]} projectLayers={[layer]} workspaceKey="full-cut" onActivity={vi.fn()} onSelect={selected} onUpdateFeatures={onUpdateFeatures} />);
  const map = factory.mock.results[0].value as L.Map;
  act(() => { map.setView([30.05, 120.05], 14); });
  fireEvent.click(screen.getByRole('button', { name: '挖洞' }));
  for (const [lat, lng] of [[29.99, 119.99], [29.99, 120.11], [30.11, 120.11], [30.11, 119.99]]) act(() => { map.fire('mousemove', { latlng: L.latLng(lat, lng), originalEvent: new MouseEvent('mousemove') }); map.fire('click', { latlng: L.latLng(lat, lng), originalEvent: new MouseEvent('click') }); });
  act(() => { (map.pm.Draw as any).Cut._finishShape(); });
  expect(screen.getByRole('alert')).toHaveTextContent('不能整块挖除已保存的项目要素');
  expect(screen.getByRole('alert')).toBeVisible();
  let restored = false;
  map.eachLayer(item => { if (item instanceof L.Polygon && (item as any).feature?.id === 'parcel' && JSON.stringify(item.toGeoJSON().geometry) === JSON.stringify(feature.geometry)) restored = true; });
  expect(restored).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: '保存' }));
  await waitFor(() => expect(screen.queryByRole('button', { name: '保存' })).not.toBeInTheDocument());
  expect(onUpdateFeatures).not.toHaveBeenCalled();
  expect(selected).not.toHaveBeenCalledWith(expect.anything(), 'remove');
  expect(selected).toHaveBeenCalledWith(expect.objectContaining({ id: 'parcel', geometry: feature.geometry }), 'upsert');
});

it('fits asynchronously loaded project data once and preserves later user navigation', () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  Reflect.set(L.Browser, 'svg', true);
  const features: GeoJSON.Feature[] = [{ type: 'Feature', id: 'a', properties: {}, geometry: { type: 'Point', coordinates: [120, 30] } }, { type: 'Feature', id: 'b', properties: {}, geometry: { type: 'Point', coordinates: [121, 31] } }];
  const layer: ProjectLayer = { id: 'first-data', name: '项目数据', path: '.earth/layers/data.geojson', format: 'geojson', featureCount: 2, geometryTypes: ['Point'], crs: 'EPSG:4326', updatedAt: '', revision: 1, visible: true, features };
  const props = { run: null, workspaceKey: 'first-focus', onActivity: vi.fn(), onSelect: vi.fn() };
  const factory = vi.spyOn(L, 'map');
  const view = render(<EarthMap {...props} selection={[]} projectLayers={[]} />);
  const map = factory.mock.results[0].value as L.Map, fitBounds = vi.spyOn(map, 'fitBounds');
  view.rerender(<EarthMap {...props} selection={[]} projectLayers={[layer]} />);
  expect(fitBounds).toHaveBeenCalledTimes(1);
  const fitted = fitBounds.mock.calls[0][0] as L.LatLngBounds;
  expect(fitted.getWest()).toBe(120); expect(fitted.getEast()).toBe(121);
  expect(fitted.getSouth()).toBe(30); expect(fitted.getNorth()).toBe(31);
  act(() => { map.setView([40, 80], 6, { animate: false }); });
  view.rerender(<EarthMap {...props} selection={[features[0]]} projectLayers={[{ ...layer, revision: 2, features: structuredClone(features) }]} />);
  expect(fitBounds).toHaveBeenCalledTimes(1);
  expect(map.getCenter().lat).toBe(40); expect(map.getCenter().lng).toBe(80);
  expect(map.getZoom()).toBe(6);
});

it.each(['cloud', 'local'] as const)('does not override the initial %s result view when project layers arrive', kind => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  Reflect.set(L.Browser, 'svg', true);
  const feature: GeoJSON.Feature = { type: 'Feature', id: 'a', properties: {}, geometry: { type: 'Point', coordinates: [120, 30] } };
  const layer: ProjectLayer = { id: 'data', name: '数据', path: '.earth/layers/data.geojson', format: 'geojson', featureCount: 1, geometryTypes: ['Point'], crs: 'EPSG:4326', updatedAt: '', visible: true, features: [feature] };
  const run = kind === 'cloud' ? { schemaVersion: 'earth.run.v1' as const, runId: 'cloud', status: 'completed' as const, code: '', scriptPath: '', project: '', sourceHash: '', startedAt: '', updatedAt: '', layers: [], console: [] } : null;
  const props = { workspaceKey: `result-focus-${kind}`, onActivity: vi.fn(), onSelect: vi.fn() };
  const factory = vi.spyOn(L, 'map');
  const view = render(<EarthMap {...props} run={run} localResult={kind === 'local' ? { runId: 'local', outputs: [] } : null} selection={[]} projectLayers={[]} />);
  const map = factory.mock.results[0].value as L.Map, fitBounds = vi.spyOn(map, 'fitBounds');
  act(() => { map.setView([40, 80], 6, { animate: false }); });
  view.rerender(<EarthMap {...props} run={null} localResult={null} selection={[]} projectLayers={[layer]} />);
  expect(fitBounds).not.toHaveBeenCalled();
  expect(map.getCenter().lat).toBe(40); expect(map.getCenter().lng).toBe(80);
});

it('keeps snapping settings discoverable and returns keyboard focus when dismissed', () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  render(<EarthMap run={null} selection={[]} workspaceKey="snap-settings" onActivity={vi.fn()} onSelect={vi.fn()} />);
  expect(screen.getByRole('button', {name:'矩形'})).toBeVisible();
  const trigger=screen.getByText('吸附开');
  fireEvent.click(trigger);
  expect(screen.getByRole('spinbutton', {name:'吸附容差（像素）'})).toBeVisible();
  fireEvent.keyDown(trigger, {key:'Escape'});
  expect(trigger.closest('details')).not.toHaveAttribute('open');
  expect(trigger).toHaveFocus();
});


it('stages deletion of a saved selection, supports undo and cancel, and persists only on save', async () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  Reflect.set(L.Browser, 'svg', true);
  const feature = { type:'Feature', id:'parcel', pawLayerId:'parcels', pawRevision:3, properties:{name:'地块'}, geometry:{type:'Point',coordinates:[120,30]} } as GeoJSON.Feature;
  const layer:ProjectLayer={id:'parcels',name:'地块',path:'parcels.geojson',format:'geojson',featureCount:1,geometryTypes:['Point'],crs:'EPSG:4326',updatedAt:'',revision:3,visible:true,features:[feature]};
  const save=vi.fn().mockResolvedValue(undefined), selected=vi.fn();
  render(<EarthMap run={null} selection={[feature]} projectLayers={[layer]} workspaceKey="delete-selection" onActivity={vi.fn()} onSelect={selected} onUpdateFeatures={save}/>);
  fireEvent.click(screen.getByRole('button',{name:'删除所选'}));
  expect(save).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button',{name:'撤销'}));
  fireEvent.click(screen.getByRole('button',{name:'保存'}));
  await waitFor(()=>expect(screen.queryByRole('button',{name:'保存'})).not.toBeInTheDocument());
  expect(save).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button',{name:'删除所选'}));
  fireEvent.click(screen.getByRole('button',{name:'取消'}));
  expect(save).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button',{name:'删除所选'}));
  fireEvent.click(screen.getByRole('button',{name:'保存'}));
  await waitFor(()=>expect(save).toHaveBeenCalledWith([],[expect.objectContaining({id:'parcel',pawLayerId:'parcels',pawRevision:3})]));
  expect(selected).toHaveBeenCalledWith(expect.objectContaining({id:'parcel'}),'remove');
});

it('enables editing only for selected drafts and does not select unrelated drafts on save', async () => {
  vi.stubGlobal('ResizeObserver',class {observe(){}disconnect(){}});Reflect.set(L.Browser,'svg',true);
  const features=['selected','unselected'].map((id,index)=>({type:'Feature',id,properties:{source:'user_drawing'},geometry:{type:'Point',coordinates:[120+index,30]}} as GeoJSON.Feature));
  localStorage.setItem('paw-earth-geometries:targeted-edit',JSON.stringify(features));
  const factory=vi.spyOn(L,'map'), selected=vi.fn();
  render(<EarthMap run={null} selection={[features[0]]} workspaceKey="targeted-edit" onActivity={vi.fn()} onSelect={selected}/>);
  fireEvent.click(screen.getByRole('button',{name:'编辑所选'}));
  const map=factory.mock.results[0].value as L.Map;
  const enabled:string[]=[];
  map.eachLayer(layer=>{if((layer as any).feature && (layer as any).pm?.enabled())enabled.push((layer as any).feature?.id);});
  expect(enabled).toEqual(['selected']);
  fireEvent.click(screen.getByRole('button',{name:'保存'}));
  await waitFor(()=>expect(screen.queryByRole('button',{name:'保存'})).not.toBeInTheDocument());
  expect(selected).not.toHaveBeenCalledWith(expect.objectContaining({id:'unselected'}),'upsert');
  expect(JSON.parse(localStorage.getItem('paw-earth-geometries:targeted-edit')!)).toEqual(features);
});


it('browses without creating points and cancels explicit drawing with Escape',()=>{
  vi.stubGlobal('ResizeObserver',class {observe(){}disconnect(){}});Reflect.set(L.Browser,'svg',true);
  const factory=vi.spyOn(L,'map'),selected=vi.fn();
  render(<EarthMap run={null} selection={[]} workspaceKey="browse-not-draw" onActivity={vi.fn()} onSelect={selected}/>);
  const map=factory.mock.results[0].value as L.Map;
  act(()=>{map.fire('click',{latlng:L.latLng(30,120),originalEvent:new MouseEvent('click')});});
  expect(selected).toHaveBeenCalledWith(null,'replace');
  expect(selected.mock.calls.some(call=>call[0]?.geometry?.type==='Point')).toBe(false);
  fireEvent.click(screen.getByRole('button',{name:'点'}));
  expect((map.pm.Draw.Marker as any).enabled()).toBe(true);
  fireEvent.keyDown(screen.getByLabelText('地理分析地图'),{key:'Escape'});
  expect((map.pm.Draw.Marker as any).enabled()).toBe(false);
  expect(screen.getByRole('button',{name:'选择'})).toHaveAttribute('aria-pressed','true');
});
