import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { transformWithEsbuild, type Plugin, type ResolvedConfig } from 'vite';

/** Keep the existing standalone validators out of Rollup's application AST.
 * This is a local, immutable ESM asset, not a second validation runtime. Static
 * imports preserve synchronous validation and the browser's shared module instance.
 */
export function contractValidatorAsset(): Plugin {
  let config: ResolvedConfig;
  let sourceFile: string;
  let assetFileName: string | undefined;
  return {
    name: 'paw-contract-validator-asset',
    apply: 'build',
    enforce: 'pre',
    configResolved(resolved) {
      config = resolved;
      sourceFile = path.resolve(config.root, 'src/contracts/generated-validators.ts');
    },
    async buildStart() {
      this.addWatchFile(sourceFile);
      const source = await readFile(sourceFile, 'utf8');
      // Compile/minify this generated module once, before the application graph
      // exists. The source generator and all exported validators remain unchanged.
      const { code } = await transformWithEsbuild(source, sourceFile, {
        loader: 'ts', target: config.build.target || 'es2022',
        minify: true, sourcemap: false, legalComments: 'none',
      });
      const digest = createHash('sha256').update(code).digest('hex');
      assetFileName = path.posix.join(config.build.assetsDir, `contract-validators.${digest}.js`);
      this.emitFile({ type: 'asset', fileName: assetFileName, source: code });
    },
    resolveId: {
      // Run before CommonJS's delegating resolver: its second normalization
      // would turn an absolute import's relative external into an absolute one.
      order: 'pre',
      handler(source, importer) {
        if (!importer || source.includes('?') || source.startsWith('\0')) return null;
        const candidate = path.resolve(path.dirname(importer.split('?')[0]), source);
        if (candidate !== sourceFile && `${candidate}.ts` !== sourceFile) return null;
        return { id: sourceFile, external: 'relative', moduleSideEffects: false };
      },
    },
    outputOptions(options) {
      const previous = options.paths;
      return {
        ...options,
        paths: (id) => {
          if (id !== sourceFile) return typeof previous === 'function' ? previous(id) : previous?.[id] ?? '';
          if (!assetFileName) throw new Error('Contract validator asset is missing');
          // Stable external identity keeps cached watch importers valid. The
          // output mapping is renewed for each content hash, relative to outDir.
          return assetFileName;
        },
      };
    },
    generateBundle(_options, bundle) {
      for (const output of Object.values(bundle)) {
        if (output.type === 'chunk' && sourceFile in output.modules) {
          this.error('Generated validators unexpectedly entered the application bundle');
        }
      }
    },
  };
}
