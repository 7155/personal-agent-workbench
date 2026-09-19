import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import L from 'leaflet';
import { afterEach, expect, it, vi } from 'vitest';
import { EarthMap } from './EarthMap';
const originalSvg=L.Browser.svg;
afterEach(()=>{cleanup();Reflect.set(L.Browser,'svg',originalSvg);vi.restoreAllMocks();vi.unstubAllGlobals();localStorage.clear();});
it('keeps created, edited and deleted geometry in sync with context and local imports',()=>{
  vi.stubGlobal('ResizeObserver',class {observe(){} disconnect(){}});Reflect.set(L.Browser,'svg',true);
  const factory=vi.spyOn(L,'map'),selected=vi.fn(),activity=vi.fn();
  render(<EarthMap run={null} selection={[]} workspaceKey="test-workspace" onActivity={activity} onSelect={selected}/>);
  const map=factory.mock.results[0].value as L.Map;
  expect(screen.getByTitle('绘制多边形')).toBeVisible();
  const polygon=L.polygon([[30,120],[30,120.1],[30.1,120.1]]);
  act(()=>{map.fire('draw:drawstart',{layerType:'polygon'});map.fire('draw:created',{layer:polygon,layerType:'polygon'});map.fire('draw:drawstop');});
  expect(activity).toHaveBeenCalledOnce();
  expect(selected.mock.lastCall?.[0].geometry.type).toBe('Polygon');
  const id=selected.mock.lastCall?.[0].id;
  expect(polygon.options.bubblingMouseEvents).toBe(false);
  expect(JSON.parse(localStorage.getItem('paw-earth-geometries:test-workspace')!)[0].id).toBe(id);
  polygon.setLatLngs([[30,120],[30,120.2],[30.1,120.2]]);
  act(()=>map.fire('draw:edited',{layers:L.featureGroup([polygon])}));
  expect(selected.mock.lastCall?.[0].id).toBe(id);
  expect(selected.mock.lastCall?.[0].geometry.coordinates[0][1]).toEqual([120.2,30]);
  // Leaflet.draw removes a deleted feature before publishing the delete receipt.
  let owner:L.FeatureGroup|undefined;map.eachLayer(layer=>{if(layer instanceof L.FeatureGroup&&layer.hasLayer(polygon))owner=layer;});
  owner!.removeLayer(polygon);
  act(()=>map.fire('draw:deleted',{layers:L.featureGroup([polygon])}));
  expect(selected.mock.lastCall?.[1]).toBe('remove');
  expect(selected.mock.lastCall?.[0].id).toBe(id);
  expect(JSON.parse(localStorage.getItem('paw-earth-geometries:test-workspace')!)).toEqual([]);
});

it('exposes an explicit save action for edit and delete modes',()=>{
  vi.stubGlobal('ResizeObserver',class {observe(){} disconnect(){}});Reflect.set(L.Browser,'svg',true);
  const feature: GeoJSON.Feature = { type:'Feature', id:'keep-me', properties:{name:'测试区域',source:'user_drawing'}, geometry:{type:'Polygon',coordinates:[[[120,30],[120.1,30],[120.1,30.1],[120,30.1],[120,30]]]}};
  localStorage.setItem('paw-earth-geometries:action-test',JSON.stringify([feature]));
  const factory=vi.spyOn(L,'map'),selected=vi.fn();
  render(<EarthMap run={null} selection={[]} workspaceKey="action-test" onActivity={vi.fn()} onSelect={selected}/>);
  const map=factory.mock.results[0].value as L.Map;
  const toolbarButtons=document.querySelectorAll<HTMLButtonElement>('.earth-gis-toolbar button');
  act(()=>fireEvent.click(toolbarButtons[4]!));
  expect(screen.getByRole('button',{name:'保存'})).toBeVisible();
  act(()=>fireEvent.click(screen.getByRole('button',{name:'取消'})));
  expect(screen.queryByRole('button',{name:'保存'})).toBeNull();
  act(()=>fireEvent.click(toolbarButtons[5]!));
  let group:L.FeatureGroup|undefined;map.eachLayer(layer=>{if(layer instanceof L.FeatureGroup&&layer.getLayers().length)group=layer;});
  expect(group).toBeDefined();
  act(()=>group!.getLayers()[0]!.fire('click'));
  expect(screen.getByRole('button',{name:'保存'})).toBeVisible();
  act(()=>fireEvent.click(screen.getByRole('button',{name:'保存'})));
  expect(selected.mock.lastCall?.[1]).toBe('remove');
  expect(JSON.parse(localStorage.getItem('paw-earth-geometries:action-test')!)).toEqual([]);
});

it('keeps polygon digitizing open for more than three vertices and exposes finish/cancel', () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  Reflect.set(L.Browser, 'svg', true);
  const factory = vi.spyOn(L, 'map');
  render(<EarthMap run={null} selection={[]} workspaceKey="polygon-test" onActivity={vi.fn()} onSelect={vi.fn()} />);
  const map = factory.mock.results[0].value as L.Map;
  act(() => map.fire('draw:drawstart', { layerType: 'polygon' }));
  const vertices = L.layerGroup([L.marker([30, 120]), L.marker([30, 120.1]), L.marker([30.1, 120.1]), L.marker([30.1, 120.05])]);
  act(() => map.fire('draw:drawvertex', { layers: vertices }));
  expect(screen.getByRole('status')).toHaveTextContent('已添加 4 个点');
  expect(screen.getByRole('button', { name: '完成' })).toBeVisible();
  expect(screen.getByRole('button', { name: '取消' })).toBeVisible();
  act(() => map.fire('draw:drawstop'));
  expect(screen.queryByRole('button', { name: '完成' })).toBeNull();
});

it('routes the explicit polygon completion button to leaflet-draw', () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  Reflect.set(L.Browser, 'svg', true);
  const complete = vi.spyOn((L.Draw.Polygon as unknown as { prototype: { completeShape: () => void } }).prototype, 'completeShape').mockImplementation(() => undefined);
  vi.spyOn(L, 'map');
  render(<EarthMap run={null} selection={[]} workspaceKey="polygon-finish-test" onActivity={vi.fn()} onSelect={vi.fn()} />);
  fireEvent.click(screen.getByRole('button', { name: '面' }));
  fireEvent.click(screen.getByRole('button', { name: '完成' }));
  expect(complete).toHaveBeenCalledOnce();
});

it('starts with a geography-ready satellite basemap and exposes a roads fallback', () => {
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
  expect(screen.getByText('Google 卫星影像')).toBeVisible();
  expect(screen.getByText('道路地图（备用）')).toBeVisible();
});

it('manages project layers and submits a secret-safe spatial database connection', () => {
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
  fireEvent.click(screen.getByRole('checkbox', { name: '道路候选' }));
  expect(onToggleLayer).toHaveBeenCalledWith('layer:roads', false);
  fireEvent.click(screen.getByRole('button', { name: '从目录移除 道路候选' }));
  expect(onRemoveLayer).toHaveBeenCalledWith('layer:roads');
  fireEvent.click(screen.getByRole('button', { name: '保存图层' }));
  expect(onSaveLayer).toHaveBeenCalledWith('候选区域', [feature]);
  fireEvent.click(screen.getByText('连接空间数据库'));
  fireEvent.change(screen.getByRole('combobox', { name: '空间数据源类型' }), { target: { value: 'postgis' } });
  fireEvent.change(screen.getByRole('textbox', { name: 'PostGIS 密钥引用' }), { target: { value: 'PAW_POSTGIS_URL' } });
  fireEvent.click(screen.getByRole('button', { name: '连接并登记' }));
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
