import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, expect, it } from 'vitest';
import register from './index';
const roots: string[] = [];
afterEach(() => { roots.splice(0).forEach(root => fs.rmSync(root, { recursive: true, force: true })); });
function setup() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'earth-package-test-')); roots.push(root);
  const tools = new Map<string, any>(); const commands = new Map<string, any>();
  register({ registerTool: (tool: any) => tools.set(tool.name, tool), registerCommand: (name: string, command: any) => commands.set(name, command) });
  return { root, tools, commands };
}
it('prepares the installed adapter without losing existing Python and SDK configuration', async () => {
  const { root, tools } = setup(); fs.mkdirSync(path.join(root, '.earth'));
  fs.writeFileSync(path.join(root, '.earth/runtime.json'), JSON.stringify({ python: '/configured/python', dependencies: '/configured/sdk', project: 'test-project' }));
  await tools.get('earth_workspace').execute('id', {}, undefined, undefined, { cwd: root });
  const config = JSON.parse(fs.readFileSync(path.join(root, '.earth/runtime.json'), 'utf8'));
  expect(config).toMatchObject({ python: '/configured/python', dependencies: '/configured/sdk', project: 'test-project' });
  expect(config.runner.startsWith(fs.realpathSync(root) + '/.earth/adapter/')).toBe(true); expect(fs.existsSync(config.runner)).toBe(true);
  expect(tools.get('earth_run_script').executionMode).toBe('sequential');
});
it('rejects an adapter symlink before writing outside the bound workspace', async () => {
  const { root, tools } = setup(); const outside = fs.mkdtempSync(path.join(os.tmpdir(), 'earth-outside-')); roots.push(outside);
  fs.mkdirSync(path.join(root, '.earth')); fs.symlinkSync(outside, path.join(root, '.earth/adapter'));
  await expect(tools.get('earth_workspace').execute('id', {}, undefined, undefined, { cwd: root })).rejects.toThrow(/symlink/);
  expect(fs.readdirSync(outside)).toEqual([]);
});

it('registers the map state, GIS retrieval, spatial data, cloud task, Asset, batch and ML workflow tools', () => {
  const { tools, commands } = setup();
  for (const name of ['earth_map_state', 'earth_gis_search', 'earth_gis_export', 'earth_spatial_connect', 'earth_spatial_catalog', 'earth_gis_pixel', 'earth_gis_backends', 'earth_gis_bundle', 'earth_gis_batch', 'earth_run_batch', 'earth_task_status', 'earth_task_cancel', 'earth_asset_upload', 'earth_ml_catalog', 'earth_ml_template', 'earth_ml_prepare']) expect(tools.has(name)).toBe(true);
  expect(commands.has('earth-gis-export')).toBe(true);
  expect(commands.has('earth-spatial-connect')).toBe(true);
  expect(commands.has('earth-gis-bundle')).toBe(true);
});

it('returns a deterministic spatial connection receipt through the package command bridge', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'earth-package-command-test-')); roots.push(root); const entries: unknown[] = []; const commands = new Map<string, any>();
  register({ registerTool: () => {}, registerCommand: (name: string, command: any) => commands.set(name, command), appendEntry: (_type: string, data: unknown) => entries.push(data) });
  await commands.get('earth-spatial-connect').handler(JSON.stringify({ name: 'PostGIS-Test', kind: 'postgis', secretReference: 'PAW_POSTGIS_URL' }), { cwd: root });
  expect(entries[0]).toMatchObject({ schemaVersion: 'rag-ime.pi-package-command-result.v1', command: 'earth-spatial-connect', result: { status: 'missing_secret', secretReference: 'PAW_POSTGIS_URL' } });
});

it('reads a trusted published map state from the bound workspace', async () => {
  const { root, tools } = setup(); fs.mkdirSync(path.join(root, '.earth')); fs.writeFileSync(path.join(root, '.earth/map-state.json'), JSON.stringify({ schemaVersion: 'earth.map-state.v1', center: [120, 30], zoom: 10, bounds: [119, 29, 121, 31], visibleLayerIds: ['terrain'], selectedFeatureIds: ['A'], updatedAt: '2026-09-18T00:00:00Z' }));
  const result = await tools.get('earth_map_state').execute('id', {}, undefined, undefined, { cwd: root });
  expect(result.details.state.center).toEqual([120, 30]);
});
