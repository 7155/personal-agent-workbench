export interface GISParameter { name: string; type: 'number' | 'text' | 'bool' | 'select' | 'crs'; default: unknown; required: boolean; choices?: string[] | null }
export interface GISOperation { op: string; desc: string; inputs: Array<{ role: string; kind: 'vector' | 'raster' }>; args: GISParameter[] }
export interface GISGroup { category: string; ops: GISOperation[] }
export interface GISFile { path: string; name: string; bytes: number; kind: 'vector' | 'raster' | 'other' }
export interface GISOutput { path: string; relativePath: string; name: string; kind: 'vector' | 'raster' | 'file'; bytes: number; geojson?: GeoJSON.GeoJsonObject }
export interface GISRun { schemaVersion?: string; runId: string; status: 'running' | 'completed' | 'failed'; op?: string; output?: string; inputs?: Record<string, unknown>; params?: Record<string, unknown>; outputs?: GISOutput[]; code?: string; error?: string; startedAt: string; updatedAt: string }
export interface GISRequest { op: string; inputs: Record<string, string>; params?: Record<string, unknown>; output?: string; saveAs?: string }
export interface SpatialSource { id: string; name: string; kind: 'geopackage' | 'spatialite' | 'postgis'; path: string; schema: string; table: string; secretReference: string; readOnly: boolean; status: string; layers: string[]; updatedAt: string }
export interface SpatialLineage { sourceId: string; sourceName: string; sourceKind: string; sourcePath: string; layer: string; sourceCrs: string; sourceSha256: string; loadedAt: string }
export interface SpatialLayerResult { status: 'completed'; path: string; lineagePath: string; sourceId: string; sourceLineage: SpatialLineage; layer: string; sourceCrs: string; crs: 'EPSG:4326'; featureCount: number; bounds: number[] | null; geometryTypes: string[]; geojson?: GeoJSON.FeatureCollection }
export interface GISRegionResult { status: 'completed' | 'outside'; path: string; band: number; crs: string; geometry: GeoJSON.Polygon | GeoJSON.MultiPolygon; geometryCrs: 'EPSG:4326'; allTouched: boolean; window: { rowOffset: number; columnOffset: number; width: number; height: number }; totalPixels: number; rasterPixels: number; validPixels: number; nodataPixels: number; outsidePixels: number; validPixelCoverage: number | null; coverageBasis: string; stats: { min: number | null; max: number | null; mean: number | null; sum: number | null; stddev: number | null } }
export const GIS_CATALOG: GISGroup[];
export const GIS_OPERATION_IDS: Set<string>;
export const GIS_SCHEMA_VERSION: string;
export function prepareGISWorkspace(root: string, options?: { version?: string; python?: string }): { root: string; gisRoot: string; adapter: string; runtime: string; python: string; runner: string; catalog: GISGroup[] };
export function listGISFiles(root: string, directory?: string): GISFile[];
export function listGISBackends(): { schemaVersion: string; default: string; backends: Array<{ id: string; available: boolean; executable?: string | null; nativeTested: boolean; status: string; role: string }> };
export function runGISOperation(input: { root: string; python?: string; request: GISRequest }): Promise<GISRun>;
export function inspectGISPath(input: { root: string; python?: string; path: string }): Promise<Record<string, unknown>>;
export function queryGISPixel(input: { root: string; python?: string; path: string; longitude: number; latitude: number; band?: number }): Promise<Record<string, unknown>>;
export function queryGISRegion(input: { root: string; python?: string; path: string; geometry: GeoJSON.Polygon | GeoJSON.MultiPolygon | GeoJSON.Feature<GeoJSON.Polygon | GeoJSON.MultiPolygon>; band?: number; allTouched?: boolean }): Promise<GISRegionResult>;
export interface GISExportRequest { input: string; format: 'geojson' | 'shp' | 'gpkg' | 'kml'; name: string; layer?: string; sourceLayer?: string; targetCrs?: string; scope?: 'all' | 'selected'; featureIds?: Array<string | number>; layerId?: string; revision?: number }
export function exportGISLayer(input: { root: string; python?: string; request: GISExportRequest }): Promise<GISRun & { format?: string; driver?: string; featureCount?: number; exportCount?: number; scope: 'all' | 'selected'; selectedFeatureIds: Array<string | number>; exportedFeatureIds?: Array<string | number | null>; layerId: string | null; revision: number | null; layerVersionBinding?: 'catalog-current' | 'catalog-history' | null; displayName?: string; identityField?: string | null; identityJsonField?: string | null }>;
export function connectSpatialSource(input: { root: string; python?: string; source: { name: string; kind: 'geopackage' | 'spatialite' | 'postgis'; path?: string; schema?: string; table?: string; secretReference?: string; readOnly?: boolean; id?: string; sourceId?: string }}): Promise<SpatialSource>;
export function listSpatialSources(input: { root: string }): { schemaVersion: string; updatedAt?: string; sources: SpatialSource[] };
export function loadSpatialLayer(input: { root: string; python?: string; sourceId: string; layer: string }): Promise<SpatialLayerResult>;
export { findQGISProcess, listQGISAlgorithms, helpQGISAlgorithm, runQGISAlgorithm } from './qgis.mjs';
export function createGISBundle(input: { root: string; runId: string; name?: string; version?: number; include?: string[] }): { status: string; path: string; manifest: Record<string, unknown> };
