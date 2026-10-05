import { createExtensionHostRegistry } from './host-registry';
import { chatSurfaceCompatibility } from './chat-surface-contract';
import type {
  PawExtensionAppId,
  PawExtensionAppManifest,
  PawExtensionAppModule,
} from './types';
export type { PawExtensionAppId } from './types';

const extensionHosts = createExtensionHostRegistry<PawExtensionAppModule>();

/** Product bootstrap binds view implementations; inventory still owns activation. */
export const registerPawExtensionHost = extensionHosts.register;

const manifestModules = import.meta.glob('../../../extension-apps/*/pawos-app.json', {
  eager: true,
  import: 'default',
}) as Record<string, unknown>;

const entryModules = import.meta.glob('../../../extension-apps/*/App.tsx') as Record<
  string,
  () => Promise<PawExtensionAppModule>
>;

type RegisteredExtensionApp = {
  manifest: PawExtensionAppManifest;
  load: () => Promise<PawExtensionAppModule>;
  ownerDirectory: string;
};

const registeredApps = Object.entries(manifestModules).map(([manifestPath, value]) => {
  const ownerDirectory = ownerDirectoryFromPath(manifestPath, 'pawos-app.json');
  const manifest = requireManifest(value, ownerDirectory);
  const entry = Object.entries(entryModules).find(([entryPath]) => (
    ownerDirectoryFromPath(entryPath, 'App.tsx') === ownerDirectory
  ))?.[1];
  if (!entry) throw new Error(`Extension App ${manifest.id} has no App.tsx entry`);
  return { manifest, load: entry, ownerDirectory } satisfies RegisteredExtensionApp;
});

assertUnique(registeredApps, (app) => app.manifest.id, 'id');
assertUnique(registeredApps, (app) => app.manifest.route, 'route');
assertUnique(registeredApps, (app) => app.manifest.packageId, 'packageId');

const byId = new Map(registeredApps.map((app) => [app.manifest.id, app]));
const byPackage = new Map(registeredApps.map((app) => [app.manifest.packageId, app]));
const labEntries = new Map<PawExtensionAppId, RegisteredExtensionApp>();

export const pawExtensionApps: readonly PawExtensionAppManifest[] = registeredApps
  .map((app) => app.manifest)
  .sort((left, right) => left.id.localeCompare(right.id));

export function isPawExtensionAppId(value: string): value is PawExtensionAppId {
  return value.startsWith('extension:') && (byId.has(value as PawExtensionAppId) || isLabExtensionAppId(value));
}

export function isLabExtensionAppId(value: string): value is PawExtensionAppId {
  return /^extension:lab-[a-f0-9]{32}$/u.test(value);
}

/** Server-owned activation inventory supplies dynamic App identities. */
export function registerLabExtensionApps(payload: unknown): Set<PawExtensionAppId> {
  const value = isRecord(payload) ? payload : {}; const enabled = new Set<PawExtensionAppId>();
  const items = value.ok === true && Array.isArray(value.items) ? value.items : [];
  for (const raw of items) {
    if (!isRecord(raw) || !Number.isSafeInteger(raw.activeVersion) || Number(raw.activeVersion) < 1 || !isRecord(raw.installation)) continue;
    const manifest = raw.installation; const hosting = isRecord(manifest.hosting) ? manifest.hosting : {};
    if (manifest.schemaVersion !== 'pawos.lab-app.v1' || typeof manifest.id !== 'string' || !isLabExtensionAppId(manifest.id)
        || manifest.id !== raw.appId || hosting.appId !== raw.appId || hosting.projectId !== raw.projectId
        || hosting.version !== raw.activeVersion || hosting.kind !== 'lab-html'
        || manifest.route !== `/extensions/${manifest.id.slice('extension:'.length)}`
        || !['label', 'shortLabel', 'tagline', 'version', 'packageId'].every((key) => typeof manifest[key] === 'string')
        || typeof manifest.bindingSha256 !== 'string' || !/^[a-f0-9]{64}$/u.test(manifest.bindingSha256)
        || !['cyan', 'blue', 'violet', 'amber', 'green', 'rose', 'slate'].includes(String(manifest.accent))
        || !isRecord(manifest.icon) || !['analytics', 'assistant', 'document', 'commerce'].includes(String(manifest.icon.symbol))
        || typeof manifest.icon.background !== 'string' || !/^#[0-9a-fA-F]{6}$/u.test(manifest.icon.background)) continue;
    const id = manifest.id; const entry = { manifest: manifest as PawExtensionAppManifest,
      ownerDirectory: id.slice('extension:'.length), load: () => extensionHosts.load('lab-html') };
    labEntries.set(id, entry); byId.set(id, entry); enabled.add(id);
  }
  for (const id of labEntries.keys()) {
    if (!enabled.has(id)) { byId.delete(id); labEntries.delete(id); }
  }
  (pawExtensionApps as PawExtensionAppManifest[]).splice(0, pawExtensionApps.length,
    ...registeredApps.map((app) => app.manifest), ...[...labEntries.values()].map((entry) => entry.manifest));
  return enabled;
}

export function pawExtensionApp(id: PawExtensionAppId): PawExtensionAppManifest {
  const app = byId.get(id);
  if (!app && isLabExtensionAppId(id)) return {
    schemaVersion: 'pawos.lab-app.v1', id, version: '0.0.0', label: '正在恢复应用', shortLabel: '应用', tagline: '',
    route: `/extensions/${id.slice('extension:'.length)}`, presentation: 'workspace', accent: 'green', icon: { symbol: 'assistant', background: '#22876A' },
    packageId: id, bindingSha256: '', skillRef: '', skillSha256: '', verticalSuiteId: '', verticalSuiteRevision: '',
  };
  if (!app) throw new Error(`Unknown PAWOS Extension App: ${id}`);
  return app.manifest;
}

export function extensionAppForPackage(packageId: string): PawExtensionAppManifest | null {
  return byPackage.get(packageId)?.manifest ?? null;
}

export async function loadPawExtensionApp(id: PawExtensionAppId): Promise<PawExtensionAppModule> {
  const app = byId.get(id);
  if (!app && !isLabExtensionAppId(id)) throw new Error(`Unknown PAWOS Extension App: ${id}`);
  const module = await (app ? app.load() : extensionHosts.load('lab-html'));
  if (typeof module.default !== 'function') {
    throw new Error(`Extension App ${id} does not export a React component`);
  }
  if ('chatSurface' in module && module.chatSurface !== undefined) {
    const compatibility = chatSurfaceCompatibility(module.chatSurface);
    if (!compatibility.supported) throw new Error(`${compatibility.reason} 现有对话记录保持不变，请更新 App 或工作台后重试。`);
  }
  return module;
}

function requireManifest(value: unknown, ownerDirectory: string): PawExtensionAppManifest {
  if (!isRecord(value)) throw new Error(`Extension App ${ownerDirectory} manifest must be an object`);
  const expectedId = `extension:${ownerDirectory}`;
  const expectedRoute = `/extensions/${ownerDirectory}`;
  if (value.schemaVersion !== 'pawos.extension-app.v1') {
    throw new Error(`Extension App ${ownerDirectory} has an unsupported schemaVersion`);
  }
  if (value.id !== expectedId || value.route !== expectedRoute) {
    throw new Error(`Extension App ${ownerDirectory} id and route must match its owner directory`);
  }
  if (typeof value.version !== 'string' || !/^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(value.version)) {
    throw new Error(`Extension App ${ownerDirectory} version must be semantic`);
  }
  if (typeof value.bindingSha256 !== 'string' || !/^[0-9a-f]{64}$/.test(value.bindingSha256)) {
    throw new Error(`Extension App ${ownerDirectory} bindingSha256 must be a lowercase SHA-256`);
  }
  if (typeof value.skillSha256 !== 'string' || !/^[0-9a-f]{64}$/.test(value.skillSha256)) {
    throw new Error(`Extension App ${ownerDirectory} skillSha256 must be a lowercase SHA-256`);
  }
  for (const field of ['packageId', 'label', 'shortLabel', 'tagline', 'skillRef', 'verticalSuiteId', 'verticalSuiteRevision'] as const) {
    if (typeof value[field] !== 'string' || !value[field].trim()) {
      throw new Error(`Extension App ${ownerDirectory} requires ${field}`);
    }
  }
  if (!new Set(['workspace', 'conversation', 'library', 'studio', 'utility']).has(String(value.presentation))) {
    throw new Error(`Extension App ${ownerDirectory} presentation is invalid`);
  }
  if (!new Set(['cyan', 'blue', 'violet', 'amber', 'green', 'rose', 'slate']).has(String(value.accent))) {
    throw new Error(`Extension App ${ownerDirectory} accent is invalid`);
  }
  if (!isRecord(value.icon)
    || !new Set(['analytics', 'assistant', 'document', 'commerce']).has(String(value.icon.symbol))
    || typeof value.icon.background !== 'string'
    || !/^#[0-9A-Fa-f]{6}$/.test(value.icon.background)) {
    throw new Error(`Extension App ${ownerDirectory} icon is invalid`);
  }
  if (value.sandbox !== undefined && (
    !isRecord(value.sandbox)
    || !new Set(['required', 'optional', 'disabled']).has(String(value.sandbox.default))
    || value.sandbox.connectorPackageId !== 'vertical-agent-sandbox'
    || value.sandbox.policyId !== 'vertical-readonly-v1'
    || Object.keys(value.sandbox).some((key) => !new Set(['default', 'connectorPackageId', 'policyId']).has(key))
  )) {
    throw new Error(`Extension App ${ownerDirectory} sandbox contract is invalid`);
  }
  return value as PawExtensionAppManifest;
}

function ownerDirectoryFromPath(path: string, fileName: string): string {
  const normalized = path.replaceAll('\\', '/');
  const escapedFileName = fileName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = normalized.match(new RegExp(`/extension-apps/([^/]+)/${escapedFileName}$`));
  const ownerDirectory = match?.[1] ?? '';
  if (!/^[a-z0-9][a-z0-9-]{0,63}$/.test(ownerDirectory)) {
    throw new Error(`Invalid Extension App source path: ${path}`);
  }
  return ownerDirectory;
}

function assertUnique(
  apps: readonly RegisteredExtensionApp[],
  key: (app: RegisteredExtensionApp) => string,
  label: string,
): void {
  const values = new Set<string>();
  for (const app of apps) {
    const value = key(app);
    if (values.has(value)) throw new Error(`Duplicate Extension App ${label}: ${value}`);
    values.add(value);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
