import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import * as ts from 'typescript';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const { featureLoaded } = vi.hoisted(() => ({ featureLoaded: vi.fn() }));
vi.mock('@/features/eval-lab/projects/LabAppHost', () => {
  featureLoaded();
  return { default: () => null };
});

const appId = 'extension:lab-11111111111111111111111111111111';
const manifest = {
  schemaVersion: 'pawos.lab-app.v1', id: appId, version: '1.0.0',
  label: 'Test', shortLabel: 'Test', tagline: 'Test', packageId: appId,
  route: '/extensions/lab-11111111111111111111111111111111',
  presentation: 'workspace', bindingSha256: 'a'.repeat(64), accent: 'green',
  icon: { symbol: 'assistant', background: '#22876A' },
  hosting: { kind: 'lab-html', appId, projectId: 'project-1', version: 1 },
};
const payload = () => ({ ok: true, items: [{
  appId, projectId: 'project-1', activeVersion: 1, installation: manifest,
}] });

describe('extension registry host integration', () => {
  beforeEach(() => { vi.resetModules(); featureLoaded.mockClear(); });

  it('loads an activated Lab App through the injected host', async () => {
    const registry = await import('./registry');
    const module = { default: () => null };
    const loader = vi.fn(async () => module);
    registry.registerPawExtensionHost('lab-html', loader);
    expect(registry.registerLabExtensionApps(payload()).has(appId)).toBe(true);
    expect(loader).not.toHaveBeenCalled();
    expect(await registry.loadPawExtensionApp(appId)).toBe(module);
    expect(loader).toHaveBeenCalledTimes(1);
  });

  it('preserves pre-inventory layout restoration without inventing activation', async () => {
    const registry = await import('./registry');
    const module = { default: () => null };
    registry.registerPawExtensionHost('lab-html', async () => module);
    expect(registry.pawExtensionApp(appId).label).toBe('正在恢复应用');
    expect(registry.pawExtensionApps.some((app) => app.id === appId)).toBe(false);
    expect(await registry.loadPawExtensionApp(appId)).toBe(module);
  });

  it('does not treat a registered host as an installed App', async () => {
    const registry = await import('./registry');
    registry.registerPawExtensionHost('lab-html', async () => ({ default: () => null }));
    const before = [...registry.pawExtensionApps];
    expect(registry.registerLabExtensionApps({ ok: true, items: [] }).size).toBe(0);
    expect(registry.pawExtensionApps).toEqual(before);
  });

  it('retains manifest binding rejection and removes disabled inventory', async () => {
    const registry = await import('./registry');
    const invalid = payload();
    invalid.items[0]!.installation = { ...manifest, bindingSha256: 'invalid' };
    expect(registry.registerLabExtensionApps(invalid).size).toBe(0);
    expect(registry.registerLabExtensionApps(payload()).has(appId)).toBe(true);
    registry.registerLabExtensionApps({ ok: true, items: [] });
    expect(registry.pawExtensionApps.some((app) => app.id === appId)).toBe(false);
    expect(registry.pawExtensionApp(appId).version).toBe('0.0.0');
  });

  it('makes a missing product binding an explicit recoverable load error', async () => {
    const registry = await import('./registry');
    registry.registerLabExtensionApps(payload());
    await expect(registry.loadPawExtensionApp(appId)).rejects.toThrow('is not registered');
    registry.registerPawExtensionHost('lab-html', async () => ({ default: () => null }));
    await expect(registry.loadPawExtensionApp(appId)).resolves.toHaveProperty('default');
  });

  it('keeps unknown non-Lab App behavior unchanged', async () => {
    const registry = await import('./registry');
    await expect(registry.loadPawExtensionApp('extension:missing')).rejects.toThrow('Unknown PAWOS');
  });

  it.each([
    { major: 2 },
    { major: 1, features: ['grant-tools'] },
  ])('rejects unsupported chat requirements before returning a mountable App: %j', async (chatSurface) => {
    const registry = await import('./registry');
    const component = vi.fn(() => null);
    registry.registerPawExtensionHost('lab-html', async () => ({ default: component, chatSurface }));
    await expect(registry.loadPawExtensionApp(appId)).rejects.toThrow(/聊天/);
    expect(component).not.toHaveBeenCalled();
  });

  it('accepts an implemented chat requirement without eagerly mounting the App', async () => {
    const registry = await import('./registry');
    const module = { default: vi.fn(() => null), chatSurface: { major: 1, features: ['embedded'] } };
    registry.registerPawExtensionHost('lab-html', async () => module);
    expect(await registry.loadPawExtensionApp(appId)).toBe(module);
    expect(module.default).not.toHaveBeenCalled();
  });

  it('registers the product host without eagerly loading the Lab feature', async () => {
    const { registerProductExtensionHosts } = await import('@/app/register-extension-hosts');
    expect(() => registerProductExtensionHosts()).not.toThrow();
    expect(() => registerProductExtensionHosts()).not.toThrow();
    expect(featureLoaded).not.toHaveBeenCalled();
    const registry = await import('./registry');
    await registry.loadPawExtensionApp(appId);
    expect(featureLoaded).toHaveBeenCalledTimes(1);
  });
});

describe('OS registry direct dependency boundary', () => {
  const here = dirname(fileURLToPath(import.meta.url));
  const sourceRoot = resolve(here, '../..');

  it('keeps composition within the lazy mount startup recovery boundary', () => {
    const main = readFileSync(resolve(sourceRoot, 'main.tsx'), 'utf8');
    const mount = readFileSync(resolve(sourceRoot, 'app/mount-control-center.tsx'), 'utf8');
    expect(main).not.toContain('register-extension-hosts');
    const registration = mount.indexOf('registerProductExtensionHosts();');
    expect(registration).toBeGreaterThanOrEqual(0);
    expect(mount.indexOf('createRoot(root).render(')).toBeGreaterThan(registration);
  });

  it.each(['registry.ts', 'host-registry.ts'])('%s does not import feature implementations', (name) => {
    const filename = resolve(here, name);
    const source = ts.createSourceFile(filename, readFileSync(filename, 'utf8'), ts.ScriptTarget.Latest, true);
    const forbidden: string[] = [];
    function inspect(node: ts.Node): void {
      let specifier: ts.Node | undefined;
      if (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) {
        specifier = node.moduleSpecifier;
      } else if (ts.isCallExpression(node) && (node.expression.kind === ts.SyntaxKind.ImportKeyword
        || (ts.isIdentifier(node.expression) && node.expression.text === 'require'))) {
        specifier = node.arguments[0];
      }
      if (specifier && ts.isStringLiteralLike(specifier)) {
        const value = specifier.text;
        const target = value.startsWith('@/') ? resolve(sourceRoot, value.slice(2))
          : value.startsWith('.') ? resolve(dirname(filename), value) : '';
        if (target.startsWith(resolve(sourceRoot, 'features') + '/')) forbidden.push(value);
      }
      ts.forEachChild(node, inspect);
    }
    inspect(source);
    expect(forbidden).toEqual([]);
  });
});
