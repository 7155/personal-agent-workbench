import { useEffect, useMemo, useRef, useState } from 'react';
import { EarthMap as LeafletEarthMap } from './EarthMap';
import { geoJsonOutputs, type EarthRun } from './workspace';
import { selectionKey, type SelectionMode } from './map-selection';
import type { EarthViewCommand } from './pi-package/view-contract';

type Props = {
  run: EarthRun | null;
  selection: GeoJSON.Feature[];
  onSelect: (feature: GeoJSON.Feature | null, mode?: SelectionMode) => void;
  workspaceKey: string;
  onActivity: () => void;
  command?: EarthViewCommand;
};

type GoogleMapsNamespace = { maps?: any };
type GoogleWindow = Window & { google?: GoogleMapsNamespace };

const googleMapsApiKey = String(import.meta.env.VITE_GOOGLE_MAPS_API_KEY ?? '').trim();
let googleMapsLoader: Promise<GoogleMapsNamespace> | undefined;

function googleWindow(): GoogleWindow {
  return window as GoogleWindow;
}

function loadGoogleMaps(): Promise<GoogleMapsNamespace> {
  if (googleWindow().google?.maps) return Promise.resolve(googleWindow().google!);
  if (!googleMapsApiKey) return Promise.reject(new Error('未配置 Google Maps API Key'));
  if (googleMapsLoader) return googleMapsLoader;
  googleMapsLoader = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(googleMapsApiKey)}&libraries=drawing,geometry&v=weekly`;
    script.async = true;
    script.defer = true;
    script.onload = () => {
      const loaded = googleWindow().google;
      if (loaded?.maps) resolve(loaded);
      else reject(new Error('Google Maps JavaScript API 未返回地图模块'));
    };
    script.onerror = () => reject(new Error('Google Maps JavaScript API 加载失败'));
    document.head.appendChild(script);
  });
  return googleMapsLoader;
}

/** Use Google Maps when configured and keep the existing GEE map as a fallback. */
export function GoogleEarthMap(props: Props) {
  const [renderer, setRenderer] = useState<'loading' | 'google' | 'fallback'>(
    googleMapsApiKey ? 'loading' : 'fallback',
  );

  useEffect(() => {
    if (!googleMapsApiKey) return;
    let active = true;
    void loadGoogleMaps()
      .then(() => {
        if (active) setRenderer('google');
      })
      .catch(() => {
        if (active) setRenderer('fallback');
      });
    return () => {
      active = false;
    };
  }, []);

  if (renderer !== 'google') {
    return (
      <div className="earth-google-map-fallback">
        <LeafletEarthMap {...props} />
        {renderer === 'loading' ? (
          <p className="earth-google-map-fallback__notice" role="status">
            正在连接 Google 地图，先显示 GEE 兼容底图…
          </p>
        ) : null}
      </div>
    );
  }
  return <GoogleEarthMapCanvas {...props} />;
}

function GoogleEarthMapCanvas({
  run,
  selection,
  onSelect,
  workspaceKey,
  onActivity,
  command,
}: Props) {
  const host = useRef<HTMLDivElement>(null);
  const map = useRef<any>(null);
  const data = useRef<any>(null);
  const drawing = useRef<any>(null);
  const styleRef = useRef<((feature: any) => Record<string, unknown>) | null>(null);
  const [status, setStatus] = useState('正在加载 Google 地图');
  const [mapReady, setMapReady] = useState(false);
  const [mode, setMode] = useState<'select' | 'point' | 'line' | 'polygon' | 'rectangle'>('select');
  const [multi, setMulti] = useState(true);
  const [available, setAvailable] = useState<GeoJSON.Feature[]>([]);
  const [drawn, setDrawn] = useState<GeoJSON.Feature[]>(() => {
    try {
      const raw = localStorage.getItem(`paw-earth-geometries:${workspaceKey}`)
        ?? localStorage.getItem(`earth-gis:${workspaceKey}`)
        ?? '[]';
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  });
  const modeRef = useRef(mode);
  modeRef.current = mode;
  const multiRef = useRef(multi);
  multiRef.current = multi;
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;
  const onActivityRef = useRef(onActivity);
  onActivityRef.current = onActivity;
  const selected = new Set(selection.map(selectionKey));
  const sync = (feature: GeoJSON.Feature | null, selectionMode: SelectionMode = 'toggle') => {
    onActivityRef.current();
    onSelectRef.current(feature, selectionMode);
  };

  useEffect(() => {
    let dead = false;
    void loadGoogleMaps()
      .then((g) => {
        if (dead || !host.current || !g.maps) return;
        const instance = new g.maps.Map(host.current, {
          center: { lat: 30, lng: 110 },
          zoom: 4,
          mapTypeId: 'satellite',
          mapTypeControl: true,
          mapTypeControlOptions: {
            mapTypeIds: ['satellite', 'hybrid', 'roadmap', 'terrain'],
            position: g.maps.ControlPosition.TOP_RIGHT,
          },
          streetViewControl: false,
          fullscreenControl: true,
        });
        map.current = instance;
        const layer = new g.maps.Data({ map: instance });
        data.current = layer;
        const style = (feature: any) => {
          const id = String(feature.getId?.() ?? feature.getProperty('id') ?? '');
          const isSelected = selected.has(id);
          return {
            strokeColor: isSelected ? '#ffffff' : '#d08a19',
            strokeWeight: isSelected ? 6 : 3,
            fillColor: '#d08a19',
            fillOpacity: isSelected ? 0.3 : 0.12,
            icon: isSelected
              ? {
                path: g.maps.SymbolPath.CIRCLE,
                scale: 8,
                fillColor: '#d08a19',
                fillOpacity: 1,
                strokeColor: '#fff',
                strokeWeight: 3,
              }
              : undefined,
          };
        };
        styleRef.current = style;
        layer.setStyle(style);
        setMapReady(true);
        layer.addListener('click', (event: any) => {
          const feature = event.feature;
          feature.toGeoJson((value: GeoJSON.GeoJsonObject) => sync(
            value as GeoJSON.Feature,
            multiRef.current ? 'toggle' : 'replace',
          ));
        });
        const manager = new g.maps.drawing.DrawingManager({
          drawingControl: false,
          polygonOptions: { strokeColor: '#d08a19', fillColor: '#d08a19', fillOpacity: 0.2 },
          polylineOptions: { strokeColor: '#d08a19', strokeWeight: 4 },
          rectangleOptions: { strokeColor: '#d08a19', fillColor: '#d08a19', fillOpacity: 0.2 },
        });
        manager.setMap(instance);
        drawing.current = manager;
        manager.addListener('overlaycomplete', (event: any) => {
          const overlay = event.overlay;
          let feature: GeoJSON.Feature;
          if (event.type === 'marker') {
            const position = overlay.getPosition();
            feature = {
              type: 'Feature',
              geometry: { type: 'Point', coordinates: [position.lng(), position.lat()] },
              properties: { source: 'user_drawing' },
            };
          } else if (event.type === 'rectangle') {
            const bounds = overlay.getBounds();
            feature = {
              type: 'Feature',
              geometry: {
                type: 'Polygon',
                coordinates: [[
                  [bounds.getWest(), bounds.getSouth()],
                  [bounds.getEast(), bounds.getSouth()],
                  [bounds.getEast(), bounds.getNorth()],
                  [bounds.getWest(), bounds.getNorth()],
                  [bounds.getWest(), bounds.getSouth()],
                ]],
              },
              properties: { source: 'user_drawing' },
            };
          } else {
            const points = overlay.getPath().getArray().map((point: any) => [point.lng(), point.lat()]);
            const coordinates = event.type === 'polygon' ? [...points, points[0]] : points;
            feature = {
              type: 'Feature',
              geometry: event.type === 'polygon'
                ? { type: 'Polygon', coordinates: [coordinates] }
                : { type: 'LineString', coordinates },
              properties: { source: 'user_drawing' },
            };
          }
          overlay.setMap(null);
          feature.id = crypto.randomUUID();
          setDrawn((current) => [...current, feature]);
          sync(feature, 'upsert');
          setMode('select');
          manager.setDrawingMode(null);
        });
        instance.addListener('click', (event: any) => {
          if (modeRef.current !== 'select') return;
          sync({
            type: 'Feature',
            geometry: { type: 'Point', coordinates: [event.latLng.lng(), event.latLng.lat()] },
            properties: { source: 'user_selection' },
          }, multiRef.current ? 'toggle' : 'replace');
        });
        setStatus('Google 卫星影像 · GEE 分析图层');
      })
      .catch((error: unknown) => {
        if (!dead) setStatus(error instanceof Error ? error.message : 'Google 地图加载失败');
      });
    return () => {
      dead = true;
      map.current = null;
      data.current = null;
      drawing.current = null;
    };
  }, []);

  useEffect(() => {
    try {
      localStorage.setItem(`paw-earth-geometries:${workspaceKey}`, JSON.stringify(drawn));
    } catch {
      // Keep current geometry in memory when local storage is unavailable.
    }
  }, [workspaceKey, drawn]);

  useEffect(() => {
    if (!mapReady || !data.current || !styleRef.current) return;
    data.current.setStyle(styleRef.current);
  }, [mapReady, selection]);

  useEffect(() => {
    const layer = data.current;
    if (!mapReady || !layer) return;
    layer.forEach((feature: any) => layer.remove(feature));
    const features = [
      ...geoJsonOutputs(run).flatMap((output) => {
        const value = output.geojson as GeoJSON.FeatureCollection | GeoJSON.Feature;
        return value.type === 'FeatureCollection' ? value.features : [value];
      }),
      ...drawn,
    ];
    setAvailable(features);
    features.forEach((feature, index) => {
      layer.addGeoJson(feature, { id: String(feature.id ?? feature.properties?.id ?? index) });
    });
    selection.forEach((feature) => {
      if (!features.some((candidate) => selectionKey(candidate) === selectionKey(feature))) {
        layer.addGeoJson(feature, { id: selectionKey(feature) });
      }
    });
  }, [mapReady, run?.runId, run?.updatedAt, selection, drawn]);

  useEffect(() => {
    const instance = map.current;
    if (!instance || !command) return;
    if (command.action === 'focus' && command.center) {
      instance.setCenter({ lng: command.center[0], lat: command.center[1] });
      if (command.zoom) instance.setZoom(command.zoom);
    }
  }, [command]);

  useEffect(() => {
    const manager = drawing.current;
    const maps = googleWindow().google?.maps;
    if (!manager || !maps?.drawing) return;
    const mapMode: Record<string, string> = {
      point: 'MARKER',
      line: 'POLYLINE',
      polygon: 'POLYGON',
      rectangle: 'RECTANGLE',
    };
    manager.setDrawingMode(mode === 'select' ? null : maps.drawing.OverlayType[mapMode[mode]]);
  }, [mode]);

  const names = useMemo(
    () => available.map((feature) => String(feature.properties?.name ?? feature.properties?.id ?? feature.id ?? '对象')),
    [available],
  );

  return (
    <div className="earth-map earth-google-map">
      <div ref={host} className="earth-map__canvas" aria-label="Google 地理分析地图" />
      <div className="earth-map-tools">
        <button
          className="earth-map-select-place"
          aria-label="选择地图地点"
          onClick={() => {
            setMode('point');
            if (!map.current) {
              sync({
                type: 'Feature',
                geometry: { type: 'Point', coordinates: [120.1, 30.2] },
                properties: { source: 'user_selection' },
              }, 'replace');
            }
          }}
        >选择地点</button>
        <div className="earth-gis-toolbar" aria-label="GEE 几何工具">
          <button onClick={() => setMode('point')}>点</button>
          <button onClick={() => setMode('line')}>线</button>
          <button onClick={() => setMode('polygon')}>面</button>
          <button onClick={() => setMode('rectangle')}>框选</button>
        </div>
        <button aria-pressed={multi} onClick={() => setMulti((value) => !value)}>多选{multi ? '开' : '关'}</button>
        <span role="status">
          {mode === 'select' ? status : `正在绘制${mode === 'point' ? '点' : mode === 'line' ? '线' : mode === 'polygon' ? '面' : '矩形'}，完成后加入选择`}
        </span>
      </div>
      <details className="earth-geometry-imports">
        <summary>选择对象 · {selection.length}</summary>
        <div className="earth-geometry-actions">
          <button onClick={() => available.forEach((feature) => sync(feature, 'upsert'))}>全选</button>
          <button disabled={!selection.length} onClick={() => sync(null, 'replace')}>清空选择</button>
        </div>
        {available.map((feature, index) => (
          <label className="earth-geometry-row" key={selectionKey(feature)}>
            <input
              type="checkbox"
              checked={selected.has(selectionKey(feature))}
              onChange={() => sync(feature, 'toggle')}
            />
            <span>{names[index]}</span>
          </label>
        ))}
      </details>
      <small className="earth-map-attribution">Google satellite imagery · Earth Engine results</small>
    </div>
  );
}
