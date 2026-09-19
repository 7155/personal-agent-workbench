export type RemoteSensingKind = 'classification' | 'ndvi' | 'change' | 'animation' | 'research';
export type RasterBandReference = number | string;
export interface RemoteSensingPlan {
  kind: RemoteSensingKind;
  region: GeoJSON.Polygon | GeoJSON.MultiPolygon;
  sampleLayerId?: string;
  samplePath?: string;
  sampleLayer?: string;
  sampleRevision?: number;
  classField?: string;
  groupField?: string;
  imagePath?: string;
  collection?: string;
  dateFrom?: string;
  dateTo?: string;
  scale?: number;
  bands?: RasterBandReference[] | { red: RasterBandReference; nir: RasterBandReference };
}
export interface RemoteSensingRequirement { key: string; label: string; status: 'provided' | 'missing' | 'preparation_only'; detail: string }
export interface RemoteSensingInputVersion { role: 'image' | 'samples'; path: string; bytes: number; sha256: string; layerId?: string | null; revision?: number | null }
export interface PreparedRemoteSensingWorkflow {
  schemaVersion: 'earth.remote-sensing-plan.v1';
  planId: string;
  createdAt: string;
  status: 'prepared' | 'needs_input';
  execution: 'local' | 'preparation_only';
  runnable: boolean;
  plan: RemoteSensingPlan;
  requirements: RemoteSensingRequirement[];
  inputVersions: RemoteSensingInputVersion[];
  path: string;
}
export interface RemoteSensingRun {
  schemaVersion: 'earth.remote-sensing-run.v1';
  runId: string;
  planId: string;
  kind: RemoteSensingKind;
  status: 'completed' | 'failed' | 'needs_input';
  op?: string;
  backend?: 'local';
  params?: RemoteSensingPlan;
  startedAt: string;
  updatedAt: string;
  code?: string;
  error?: string;
  execution?: 'local';
  inputVersions: RemoteSensingInputVersion[];
  outputs: Array<{ path: string; relativePath?: string; name: string; kind: 'raster' | 'file'; bytes: number; sha256: string }>;
  preview?: { dataUrl: string; bounds: [number, number, number, number] };
  requirements?: RemoteSensingRequirement[];
  metrics?: Record<string, unknown>;
  statistics?: Record<string, unknown>;
  classMapping?: Array<{ code: number; value: string | number; valueType: 'string' | 'number'; samplePixels: number; groupCount: number }>;
}
export function prepareRemoteSensingWorkflow(input: { root: string; plan: RemoteSensingPlan }): Promise<PreparedRemoteSensingWorkflow>;
export function runRemoteSensingWorkflow(input: { root: string; planId: string; python?: string }): Promise<RemoteSensingRun>;
