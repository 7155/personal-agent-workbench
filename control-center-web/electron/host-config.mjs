import fs from 'node:fs';
import crypto from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const electronDirectory = path.dirname(fileURLToPath(import.meta.url));
const repositoryRoot = path.resolve(electronDirectory, '..', '..');
const packagedApplication = path.resolve(electronDirectory, '..', 'dist')
  !== path.resolve(repositoryRoot, 'control-center-web', 'dist');
const packagedPreview = packagedApplication
  && [process.execPath, process.resourcesPath]
    .filter((value) => typeof value === 'string')
    .some((value) => /preview/i.test(value));

export const browserPartition = 'persist:paw-browser';
export const defaultPawHostPort = 8770;
const frontendBuildMarkerName = 'rag-ime-control-web-build.json';

export function resolveHostMode(env = process.env) {
  const explicitMode = String(env.PAW_HOST_MODE || '').trim().toLowerCase();
  if (explicitMode) {
    if (['dev', 'development', 'preview', 'test'].includes(explicitMode)) return 'development';
    if (['prod', 'production', 'release'].includes(explicitMode)) return 'production';
    throw new Error(`Unsupported PAW_HOST_MODE: ${explicitMode}`);
  }

  const channel = String(env.PAW_ELECTRON_CHANNEL || '').trim().toLowerCase();
  if (['prod', 'production', 'release'].includes(channel)) return 'production';
  if (['dev', 'development', 'preview', 'test'].includes(channel)) return 'development';

  // A packaged app keeps the host under Resources/app, while source execution
  // resolves both entries to control-center-web/dist. Keep the explicit mode
  // visible for dev/test and do not rely on an environment override in a
  // packaged production host.
  return (packagedApplication && !packagedPreview)
    || (!packagedPreview && process.defaultApp === false)
    ? 'production'
    : 'development';
}

export function resolveHostPaths(env = process.env) {
  const appSupportRoot = path.resolve(
    env.RAG_IME_APP_SUPPORT_DIR
      || path.join(env.HOME || '', 'Library', 'Application Support', 'RagIme'),
  );
  const profilePath = path.resolve(
    env.PAW_BROWSER_PROFILE_DIR
      || path.join(appSupportRoot, 'Browser', 'runtime-profile'),
  );
  const packagedEntry = path.join(electronDirectory, '..', 'dist', 'index.html');
  const sourceEntry = path.join(repositoryRoot, 'control-center-web', 'dist', 'index.html');
  const hostMode = resolveHostMode(env);
  if (hostMode === 'production' && env.PAW_FRONTEND_ENTRY) {
    throw new Error('PAW_FRONTEND_ENTRY is not allowed for production Electron hosts');
  }
  const frontendEntry = env.PAW_FRONTEND_ENTRY
    ? path.resolve(env.PAW_FRONTEND_ENTRY)
    : hostMode === 'production'
      ? packagedEntry
      : fs.existsSync(packagedEntry)
        ? packagedEntry
        : sourceEntry;
  if (hostMode === 'production') validateProductionFrontend(frontendEntry);
  return {
    frontendEntry,
    hostMode,
    hostPidFile: path.join(profilePath, 'PAWBrowserHost.pid'),
    browserHistoryFile: path.join(profilePath, 'PAWBrowserHost.history.json'),
    browserBookmarksFile: path.join(profilePath, 'PAWBrowserHost.bookmarks.json'),
    browserDownloadsFile: path.join(profilePath, 'PAWBrowserHost.downloads.json'),
    browserSettingsFile: path.join(profilePath, 'PAWBrowserHost.settings.json'),
    browserExtensionsDir: path.join(profilePath, 'Extensions'),
    preloadEntry: path.join(electronDirectory, 'preload.cjs'),
    production: hostMode === 'production',
    profilePath,
    repositoryRoot,
  };
}

export function computeFrontendDistDigest(frontendRoot) {
  const root = path.resolve(frontendRoot);
  const stat = fs.statSync(root, { throwIfNoEntry: false });
  if (!stat?.isDirectory()) throw new Error(`frontend dist root is missing: ${root}`);

  const digest = crypto.createHash('sha256');
  const visit = (directory, relativeDirectory) => {
    const entries = fs.readdirSync(directory, { withFileTypes: true })
      .sort((left, right) => left.name < right.name ? -1 : left.name > right.name ? 1 : 0);
    for (const entry of entries) {
      const relative = relativeDirectory
        ? `${relativeDirectory}/${entry.name}`
        : entry.name;
      if (relative === frontendBuildMarkerName) continue;
      const target = path.join(directory, entry.name);
      if (entry.isSymbolicLink()) {
        throw new Error(`frontend dist contains a symlink: ${relative}`);
      }
      if (entry.isDirectory()) {
        visit(target, relative);
        continue;
      }
      if (!entry.isFile()) continue;
      // Match the build/install digest owner: Finder may update this regular
      // metadata file after installation. Symlinks and directories were checked
      // above, so this exception cannot hide frontend code under the same name.
      if (entry.name === '.DS_Store') continue;
      digest.update(relative);
      digest.update('\0');
      digest.update(fs.readFileSync(target));
      digest.update('\0');
    }
  };
  visit(root, '');
  return digest.digest('hex');
}

export function validateProductionFrontend(frontendEntry) {
  const entry = path.resolve(frontendEntry);
  const entryStat = fs.statSync(entry, { throwIfNoEntry: false });
  if (!entryStat?.isFile()) throw new Error(`production frontend entry is missing: ${entry}`);
  const root = path.dirname(entry);
  const markerPath = path.join(root, frontendBuildMarkerName);
  if (!fs.existsSync(markerPath)) {
    throw new Error(`production frontend build marker is missing: ${markerPath}`);
  }

  let marker;
  try {
    marker = JSON.parse(fs.readFileSync(markerPath, 'utf8'));
  } catch (error) {
    throw new Error(`production frontend build marker is invalid: ${error.message}`);
  }
  if (marker?.schemaVersion !== 'rag-ime.control-web-build.v1') {
    throw new Error('production frontend build marker has the wrong schema');
  }
  if (marker.buildChannel !== 'production') {
    throw new Error('production frontend build marker is stale: non-production build');
  }
  if (marker.frontendProduct === 'legacy') {
    throw new Error('production frontend marker contains legacy frontend product');
  }
  if (marker.frontendProduct !== 'paw-os') {
    throw new Error('production frontend marker has no PAWOS frontend product');
  }
  if (typeof marker.sourceCommit !== 'string' || !marker.sourceCommit.trim()) {
    throw new Error('production frontend marker is stale: missing source commit');
  }
  if (!/^[0-9a-f]{64}$/.test(String(marker.distTreeDigest || ''))) {
    throw new Error('production frontend marker is stale: missing dist tree digest');
  }
  const actualDigest = computeFrontendDistDigest(root);
  if (actualDigest !== marker.distTreeDigest) {
    throw new Error(
      `production frontend marker is stale: dist tree digest ${marker.distTreeDigest} != ${actualDigest}`,
    );
  }
  return marker;
}

export function isBrowserGuestUrl(rawUrl) {
  try {
    const url = new URL(rawUrl);
    return ['http:', 'https:'].includes(url.protocol) || rawUrl === 'about:blank';
  } catch {
    return false;
  }
}
