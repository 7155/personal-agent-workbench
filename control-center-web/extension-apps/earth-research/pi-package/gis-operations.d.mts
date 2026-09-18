export interface GISParameter { name: string; type: 'number' | 'text' | 'bool' | 'select' | 'crs'; default: unknown; required: boolean; choices?: string[] | null }
export interface GISOperation { op: string; desc: string; inputs: Array<{ role: string; kind: 'vector' | 'raster' }>; args: GISParameter[] }
export interface GISGroup { category: string; ops: GISOperation[] }
export interface GISFile { path: string; name: string; bytes: number; kind: 'vector' | 'raster' | 'other' }
export interface GISOutput { path: string; relativePath: string; name: string; kind: 'vector' | 'raster' | 'file'; bytes: number; geojson?: GeoJSON.GeoJsonObject }
export interface GISRun { schemaVersion?: string; runId: string; status: 'running' | 'completed' | 'failed'; op?: string; output?: string; inputs?: Record<string, unknown>; params?: Record<string, unknown>; outputs?: GISOutput[]; code?: string; error?: string; startedAt: string; updatedAt: string }
export interface GISRequest { op: string; inputs: Record<string, string>; params?: Record<string, unknown>; output?: string; saveAs?: string }
export const GIS_CATALOG: GISGroup[];
export const GIS_OPERATION_IDS: Set<string>;
export const GIS_SCHEMA_VERSION: string;
export function prepareGISWorkspace(root: string, options?: { version?: string; python?: string }): { root: string; gisRoot: string; adapter: string; runtime: string; python: string; runner: string; catalog: GISGroup[] };
export function listGISFiles(root: string, directory?: string): GISFile[];
export function runGISOperation(input: { root: string; python?: string; request: GISRequest }): Promise<GISRun>;
export function inspectGISPath(input: { root: string; python?: string; path: string }): Promise<Record<string, unknown>>;
