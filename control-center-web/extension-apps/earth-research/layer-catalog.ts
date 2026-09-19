export type ProjectLayer = {
  id: string;
  name: string;
  path: string;
  format: 'geojson' | 'shp' | 'gpkg' | 'kml';
  featureCount: number;
  geometryTypes: string[];
  crs: string;
  updatedAt: string;
  revision?: number;
  history?: string[];
  visible: boolean;
  features: GeoJSON.Feature[];
};

export type SpatialSourceSummary = {
  id: string;
  name: string;
  kind: 'geopackage' | 'spatialite' | 'postgis';
  path: string;
  status: string;
  layers: string[];
  schema: string;
  table: string;
  updatedAt: string;
};

export type SpatialSourceDraft = {
  name: string;
  kind: SpatialSourceSummary['kind'];
  path?: string;
  secretReference?: string;
  schema?: string;
  table?: string;
  readOnly?: boolean;
};

/** A workspace entry projected into the GIS data dock. */
export type WorkspaceFileSummary = {
  path: string;
  name: string;
  kind: 'file' | 'directory' | 'symlink';
  byteSize?: number;
};

export function layerSlug(value: string): string {
  const normalized = String(value || '').trim().normalize('NFKC').replace(/[^A-Za-z0-9\u4e00-\u9fff_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 48);
  return normalized || 'layer';
}

export function featureCollection(features: GeoJSON.Feature[]): GeoJSON.FeatureCollection {
  return { type: 'FeatureCollection', features: features.map(feature => structuredClone(feature)) };
}

export function parseProjectLayerCatalog(value: unknown): Array<Omit<ProjectLayer, 'features'>> {
  if (!value || typeof value !== 'object') return [];
  const sources = (value as { layers?: unknown }).layers;
  if (!Array.isArray(sources)) return [];
  return sources.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object')).map((item, index) => ({
    id: typeof item.id === 'string' ? item.id : `layer:${index}`,
    name: typeof item.name === 'string' ? item.name : `图层 ${index + 1}`,
    path: typeof item.path === 'string' ? item.path : '',
    format: item.format === 'shp' || item.format === 'gpkg' || item.format === 'kml' ? item.format : 'geojson',
    featureCount: Number.isFinite(Number(item.featureCount)) ? Number(item.featureCount) : 0,
    geometryTypes: Array.isArray(item.geometryTypes) ? item.geometryTypes.filter((type): type is string => typeof type === 'string') : [],
    crs: typeof item.crs === 'string' ? item.crs : 'EPSG:4326',
    updatedAt: typeof item.updatedAt === 'string' ? item.updatedAt : '',
    revision: Number.isFinite(Number(item.revision)) ? Number(item.revision) : 1,
    history: Array.isArray(item.history) ? item.history.filter((entry): entry is string => typeof entry === 'string') : [],
    visible: item.visible !== false,
  }));
}

export function parseSpatialCatalog(value: unknown): SpatialSourceSummary[] {
  if (!value || typeof value !== 'object' || !Array.isArray((value as { sources?: unknown }).sources)) return [];
  return ((value as { sources: unknown[] }).sources).filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object')).map((item, index) => ({
    id: typeof item.id === 'string' ? item.id : `spatial:${index}`,
    name: typeof item.name === 'string' ? item.name : `数据源 ${index + 1}`,
    kind: item.kind === 'postgis' || item.kind === 'spatialite' ? item.kind : 'geopackage',
    path: typeof item.path === 'string' ? item.path : '',
    status: typeof item.status === 'string' ? item.status : 'unknown',
    layers: Array.isArray(item.layers) ? item.layers.filter((layer): layer is string => typeof layer === 'string') : [],
    schema: typeof item.schema === 'string' ? item.schema : '',
    table: typeof item.table === 'string' ? item.table : '',
    updatedAt: typeof item.updatedAt === 'string' ? item.updatedAt : '',
  }));
}

export function parseGeoJsonFeatures(value: unknown): GeoJSON.Feature[] {
  if (!value || typeof value !== 'object') return [];
  const data = value as Partial<GeoJSON.FeatureCollection> & Partial<GeoJSON.Feature>;
  if (data.type === 'FeatureCollection' && Array.isArray(data.features)) return data.features.filter(feature => feature?.type === 'Feature' && feature.geometry);
  if (data.type === 'Feature' && data.geometry) return [data as GeoJSON.Feature];
  return [];
}

export function bindLayerFeatures(layer: ProjectLayer): ProjectLayer {
  return {...layer, features:layer.features.map(feature=>({...feature,pawLayerId:layer.id,pawRevision:layer.revision}))};
}
export function selectedLayerFeatures(layer: ProjectLayer, selected: GeoJSON.Feature[]): GeoJSON.Feature[] {
  return layer.features.filter(item=>selected.some(feature=> {
    const owner=(feature as GeoJSON.Feature & {pawLayerId?:string}).pawLayerId;
    if (owner && owner!==layer.id) return false;
    if (owner) return item.id !== undefined && item.id === feature.id;
    return item.id !== undefined && item.id===feature.id && JSON.stringify(item.geometry)===JSON.stringify(feature.geometry);
  }));
}
