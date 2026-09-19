export interface CloudWorkflowPlan {
  kind: 'ndvi' | 'change' | 'animation' | 'research';
  region: GeoJSON.Polygon | GeoJSON.MultiPolygon | GeoJSON.Feature<GeoJSON.Polygon | GeoJSON.MultiPolygon>;
  dateFrom: string;
  dateTo: string;
  collection?: 'COPERNICUS/S2_SR_HARMONIZED';
  scale?: number;
  bands?: { red: 'B4'; nir: 'B8' | 'B8A' } | ['B4', 'B8' | 'B8A'];
  interval?: 'monthly' | 'annual';
  splitDate?: string;
  dimensions?: number;
  framesPerSecond?: number;
  question?: string;
}
export interface CloudWorkflowRequirement { key: string; label: string; status: 'provided' | 'missing' | 'preparation_only'; detail: string }
export interface PreparedCloudWorkflow {
  schemaVersion: 'earth.cloud-workflow-plan.v1';
  planId: string;
  backend: 'gee';
  execution: 'gee';
  status: 'prepared';
  runnable: boolean;
  createdAt: string;
  path: string;
  scriptPath: string;
  scriptSha256: string;
  plan: Omit<CloudWorkflowPlan, 'region' | 'bands'> & { region: GeoJSON.Polygon | GeoJSON.MultiPolygon; collection: 'COPERNICUS/S2_SR_HARMONIZED'; scale: number; bands: { red: 'B4'; nir: 'B8' | 'B8A' }; interval: 'monthly' | 'annual'; periods: Array<{ label: string; start: string; end: string }>; reductionCrs: 'EPSG:4326'; maxPixels: number; sclClasses: number[]; splitStrategy?: 'midpoint' | 'explicit' };
  requirements: CloudWorkflowRequirement[];
  references: Array<{ title: string; url: string }>;
  evidenceRequestPath?: string;
}
export function prepareCloudWorkflow(input: { root: string; plan: CloudWorkflowPlan }): PreparedCloudWorkflow;
