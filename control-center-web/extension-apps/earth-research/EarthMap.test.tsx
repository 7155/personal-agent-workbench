import { act, cleanup, render, screen } from '@testing-library/react';
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
