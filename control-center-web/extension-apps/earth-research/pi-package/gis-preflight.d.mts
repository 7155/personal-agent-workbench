export type SitingPreflight = {
  schemaVersion: 'earth.gis-preflight.v1'; status: 'completed'; ready: boolean; checkedAt: string;
  inputs: Array<{ role: string; path: string; sha256: string; files: Array<{name: string; sha256: string}>; crs?: string | null; rows?: number; geometryTypes?: string[] }>;
  issues: Array<{ level: 'error' | 'info'; code: string; message: string }>;
  steps: string[]; distance: number; units: 'm';
};
export function preflightSiting(input: { root: string; python?: string; parcels: string; avoidance: string; distance: number; signal?: AbortSignal }): Promise<SitingPreflight>;
export function inputHash(file: string): string;
export function inputFileVersions(root: string, relative: string): Array<{name: string; sha256: string}>;
