import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import react from '@vitejs/plugin-react';
import type { Plugin } from 'vite';
import { defineConfig } from 'vitest/config';

const rootDirectory = path.dirname(fileURLToPath(import.meta.url));
const controlTransport = process.env.VITE_CONTROL_TRANSPORT ?? 'auto';
const buildChannel = process.env.VITE_BUILD_CHANNEL ?? 'preview';
const controlProxyTarget = normalizeControlProxyTarget(
  process.env.VITE_CONTROL_PROXY_TARGET ?? 'http://127.0.0.1:8768',
);
const nativeOnlyBuild = controlTransport === 'native';
const httpOnlyBuild = controlTransport === 'http';
const isolatedTransportBuild = nativeOnlyBuild || httpOnlyBuild;
const sourceCommit = execFileSync('git', ['rev-parse', 'HEAD'], {
  cwd: path.resolve(rootDirectory, '..'),
  encoding: 'utf8',
}).trim();

if (!new Set(['auto', 'mock', 'http', 'native']).has(controlTransport)) {
  throw new Error(`Unsupported VITE_CONTROL_TRANSPORT: ${controlTransport}`);
}
if (!new Set(['preview', 'production']).has(buildChannel)) {
  throw new Error(`Unsupported VITE_BUILD_CHANNEL: ${buildChannel}`);
}
if (buildChannel === 'production' && !isolatedTransportBuild) {
  throw new Error('Production control-center builds require native or http transport');
}

const nativeTransportEntry = path.resolve(
  rootDirectory,
  'src/app/control-transport.native.tsx',
);
const httpTransportEntry = path.resolve(
  rootDirectory,
  'src/app/control-transport.http.tsx',
);
const productionDataEntry = path.resolve(
  rootDirectory,
  'src/features/agent/production-data.ts',
);
const forbiddenNativeBundleModules = [
  '/src/app/control-transport.tsx',
  '/src/app/control-transport.http.tsx',
  '/src/platform/http-transport.ts',
  '/src/test/mock-transport.ts',
  '/src/features/agent/preview-data.ts',
];
const forbiddenHttpBundleModules = [
  '/src/app/control-transport.tsx',
  '/src/app/control-transport.native.tsx',
  '/src/platform/native-transport.ts',
  '/src/test/mock-transport.ts',
  '/src/features/agent/preview-data.ts',
];

function controlTransportBoundary(): Plugin {
  return {
    name: 'rag-ime-control-transport-boundary',
    transformIndexHtml(html) {
      if (!isolatedTransportBuild) return html;
      const isolated = html.replace(
        "connect-src 'self' http://127.0.0.1:8766 http://127.0.0.1:8768 ws://127.0.0.1:*;",
        "connect-src 'self';",
      );
      return httpOnlyBuild
        ? isolated.replace(
            /(img-src[^;]*) http:\/\/127\.0\.0\.1:8766(?=[ ;])/,
            '$1',
          )
        : isolated;
    },
    generateBundle(_options, bundle) {
      const bundledModules = Object.values(bundle)
        .filter((output) => output.type === 'chunk')
        .flatMap((output) => Object.keys(output.modules))
        .map((moduleId) => moduleId.replaceAll('\\', '/'));
      const forbiddenModules = nativeOnlyBuild
        ? bundledModules.filter((moduleId) =>
            forbiddenNativeBundleModules.some((suffix) => moduleId.includes(suffix)))
        : httpOnlyBuild
          ? bundledModules.filter((moduleId) =>
              forbiddenHttpBundleModules.some((suffix) => moduleId.includes(suffix)))
        : [];
      if (forbiddenModules.length > 0) {
        this.error(
          `Isolated control-center bundle includes forbidden transport modules: ${forbiddenModules.join(', ')}`,
        );
      }
      this.emitFile({
        type: 'asset',
        fileName: 'rag-ime-control-web-build.json',
        source: `${JSON.stringify({
          schemaVersion: 'rag-ime.control-web-build.v1',
          buildChannel,
          transport: controlTransport,
          nativeOnly: nativeOnlyBuild,
          httpOnly: httpOnlyBuild,
          forbiddenTransportModulesExcluded: isolatedTransportBuild && forbiddenModules.length === 0,
          previewFixturesExcluded: buildChannel === 'production' && forbiddenModules.length === 0,
          sourceCommit,
        }, null, 2)}\n`,
      });
    },
  };
}

function browserDependencyBoundary(): Plugin {
  return {
    name: 'rag-ime-browser-dependency-boundary',
    enforce: 'pre',
    transform(code, id) {
      if (id.replaceAll('\\', '/').endsWith('/@geoman-io/leaflet-geoman-free/dist/leaflet-geoman.js')) {
        // Geoman ships an already bundled Lodash, so the individual _nodeUtil
        // transform below cannot see it. Use its browser paths explicitly;
        // never carry Node's util loader or the dynamic global fallback into CSP.
        if (!code.includes('freeModule.require("util").types') || !code.includes('Function("return this")()')) {
          this.error('Geoman browser dependency shape changed; review its bundled Lodash before upgrading.');
        }
        return {
          code: code.replaceAll('freeModule.require("util").types', 'undefined')
            .replaceAll('Function("return this")()', 'globalThis'),
          map: null,
        };
      }
      if (!id.replaceAll('\\', '/').endsWith('/lodash/_nodeUtil.js')) return null;
      const browserCode = code.replace(
        /freeModule\.require\((['"])util\1\)\.types/g,
        'undefined',
      );
      return browserCode === code ? null : { code: browserCode, map: null };
    },
  };
}

function normalizeControlProxyTarget(value: string | undefined): string | undefined {
  if (!value) return undefined;
  const url = new URL(value);
  if (
    url.protocol !== 'http:' ||
    !new Set(['127.0.0.1', 'localhost', '[::1]']).has(url.hostname) ||
    url.username ||
    url.password ||
    url.pathname !== '/' ||
    url.search ||
    url.hash
  ) {
    throw new Error('VITE_CONTROL_PROXY_TARGET must be a loopback HTTP origin');
  }
  return url.origin;
}

export default defineConfig({
  base: './',
  define: {
    __CONTROL_PREVIEW__: JSON.stringify(buildChannel !== 'production'),
    // Local Vitest/dev renders deliberately identify themselves as `dev`.
    // Host builds pass the exact package version and source commit.
    __PAW_PRODUCT_VERSION__: JSON.stringify(
      process.env.VITE_PAW_PRODUCT_VERSION?.trim() || '0.1.0',
    ),
    __PAW_BUILD_COMMIT__: JSON.stringify(
      process.env.VITE_PAW_BUILD_COMMIT?.trim() || 'dev',
    ),
    __PAW_BUILD_NUMBER__: JSON.stringify(
      process.env.VITE_PAW_BUILD_NUMBER?.trim() || 'dev',
    ),
    __PAW_SOURCE_DIRTY__: JSON.stringify(
      process.env.VITE_PAW_SOURCE_DIRTY?.trim().toLowerCase() === 'true',
    ),
  },
  plugins: [react(), browserDependencyBoundary(), controlTransportBoundary()],
  resolve: {
    dedupe: ['react', 'react-dom', '@testing-library/react', '@testing-library/user-event'],
    alias: [
      ...(nativeOnlyBuild
        ? [{ find: /^@\/app\/control-transport$/, replacement: nativeTransportEntry }]
        : httpOnlyBuild
          ? [{ find: /^@\/app\/control-transport$/, replacement: httpTransportEntry }]
        : []),
      ...(buildChannel === 'production'
        ? [{ find: /^@\/features\/agent\/preview-data$/, replacement: productionDataEntry }]
        : []),
      { find: '@', replacement: path.resolve(rootDirectory, 'src') },
    ],
  },
  build: {
    sourcemap: false,
    target: 'es2022',
  },
  server: {
    fs: { allow: [path.resolve(rootDirectory, '..')] },
    ...(controlProxyTarget
      ? {
          proxy: {
            '/__paw_html_preview': {
              target: controlProxyTarget,
            },
            '/api': {
              target: controlProxyTarget,
            },
          },
        }
      : {}),
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
    css: true,
    testTimeout: 15_000,
    include: [
      'src/**/*.test.{ts,tsx}',
      'extension-apps/**/*.test.{ts,tsx}',
      '../integrations/pi/skills/pawos-app-builder/assets/frontend-template/**/*.test.{ts,tsx}',
    ],
  },
});
