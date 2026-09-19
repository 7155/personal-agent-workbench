export interface GISParameter { name: string; type: 'number' | 'text' | 'bool' | 'select' | 'crs'; default: unknown; required: boolean; choices?: string[] | null }
export interface GISOperation { op: string; desc: string; inputs: Array<{ role: string; kind: 'vector' | 'raster' }>; args: GISParameter[] }
export interface GISGroup { category: string; ops: GISOperation[] }
export interface GISFile { path: string; name: string; bytes: number; kind: 'vector' | 'raster' | 'other' }
export interface GISOutput { path: string; relativePath: string; name: string; kind: 'vector' | 'raster' | 'file'; bytes: number; geojson?: GeoJSON.GeoJsonObject }
export interface GISRun { schemaVersion?: string; runId: string; status: 'running' | 'completed' | 'failed'; op?: string; output?: string; inputs?: Record<string, unknown>; params?: Record<string, unknown>; outputs?: GISOutput[]; code?: string; error?: string; startedAt: string; updatedAt: string }
export interface GISRequest { op: string; inputs: Record<string, string>; params?: Record<string, unknown>; output?: string; saveAs?: string }
export interface SpatialSource { id: string; name: string; kind: 'geopackage' | 'spatialite' | 'postgis'; path: string; schema: string; table: string; secretReference: string; readOnly: boolean; status: string; layers: string[]; updatedAt: string }
export const GIS_CATALOG: GISGroup[];
export const GIS_OPERATION_IDS: Set<string>;
export const GIS_SCHEMA_VERSION: string;
export function prepareGISWorkspace(root: string, options?: { version?: string; python?: string }): { root: string; gisRoot: string; adapter: string; runtime: string; python: string; runner: string; catalog: GISGroup[] };
export function listGISFiles(root: string, directory?: string): GISFile[];
export function listGISBackends(): { schemaVersion: string; default: string; backends: Array<{ id: string; available: boolean; executable?: string | null; role: string }> };
export function runGISOperation(input: { root: string; python?: string; request: GISRequest }): Promise<GISRun>;
export function inspectGISPath(input: { root: string; python?: string; path: string }): Promise<Record<string, unknown>>;
export function queryGISPixel(input: { root: string; python?: string; path: string; longitude: number; latitude: number; band?: number }): Promise<Record<string, unknown>>;
export function exportGISLayer(input: { root: string; python?: string; request: { input: string; format: 'geojson' | 'shp' | 'gpkg' | 'kml'; name: string; layer?: string; targetCrs?: string } }): Promise<GISRun & { format?: string; driver?: string; featureCount?: number }>;
export function connectSpatialSource(input: { root: string; python?: string; source: { name: string; kind: 'geopackage' | 'spatialite' | 'postgis'; path?: string; schema?: string; table?: string; secretReference?: string; readOnly?: boolean }}): Promise<SpatialSource>;
export function listSpatialSources(input: { root: string }): { schemaVersion: string; updatedAt?: string; sources: SpatialSource[] };
export function createGISBundle(input: { root: string; runId: string; name?: string; version?: number; include?: string[] }): { status: string; path: string; manifest: Record<string, unknown> };
