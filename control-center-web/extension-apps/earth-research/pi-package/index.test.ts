import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, expect, it } from 'vitest';
import register from './index';
const roots: string[] = [];
afterEach(() => { roots.splice(0).forEach(root => fs.rmSync(root, { recursive: true, force: true })); });
function setup() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'earth-package-test-')); roots.push(root);
  const tools = new Map<string, any>(); register({ registerTool: (tool: any) => tools.set(tool.name, tool) });
  return { root, tools };
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
