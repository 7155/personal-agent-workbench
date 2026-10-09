import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { build, type Plugin } from 'vite';
import { contractValidatorAsset } from '../../scripts/contract-validator-asset';
import * as original from './generated-validators';
import approvalFixture from '../../../tests/fixtures/agent/agent-approval.json';
import eventFixture from '../../../tests/fixtures/agent/agent-event.json';
import mediaFixture from '../../../tests/fixtures/agent/agent-media.json';
import maintenanceFixture from '../../../tests/fixtures/agent/agent-memory-maintenance-status.json';
import messageFixture from '../../../tests/fixtures/agent/agent-message.json';
import sessionFixture from '../../../tests/fixtures/agent/agent-session.json';

type Validator = ((value: unknown) => boolean) & { errors?: unknown };
type ValidatorModule = {
  contractValidators: Record<string, Validator>;
  tolerantAgentEventValidator: Validator;
  tolerantRoomEventValidator: Validator;
  tolerantAgentMessageValidator: Validator;
};

describe('generated contract ESM build asset', () => {
  const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
  const generated = path.join(webRoot, 'src/contracts/generated-validators.ts');
  let temporary: string;
  let assetFile: string;
  let assetCode: string;
  let entryCode: string;
  let built: ValidatorModule;
  let bundledModules: string[] = [];

  beforeAll(async () => {
    temporary = await mkdtemp(path.join(os.tmpdir(), 'paw-validator-asset-'));
    const output = path.join(temporary, 'dist');
    await writeFile(path.join(temporary, 'package.json'), '{"type":"module"}\n');
    const entry = path.join(temporary, 'entry.ts');
    await writeFile(entry, `export { contractValidators, tolerantAgentEventValidator, tolerantRoomEventValidator, tolerantAgentMessageValidator } from ${JSON.stringify(generated)};\n`);
    const capture: Plugin = {
      name: 'capture-validator-build',
      generateBundle(_options, bundle) {
        for (const value of Object.values(bundle)) {
          if (value.type === 'chunk') {
            bundledModules.push(...Object.keys(value.modules));
            if (value.isEntry) entryCode = value.code;
          } else if (value.fileName.includes('contract-validators.')) {
            assetFile = value.fileName;
            assetCode = typeof value.source === 'string' ? value.source : Buffer.from(value.source).toString();
          }
        }
      },
    };
    await build({
      configFile: false, root: webRoot, base: './', logLevel: 'silent',
      plugins: [contractValidatorAsset(), capture],
      build: {
        outDir: output, emptyOutDir: false, sourcemap: false,
        minify: false, reportCompressedSize: false, target: 'es2022',
        rollupOptions: { input: entry, preserveEntrySignatures: 'strict', output: { entryFileNames: 'nested/entry.js' } },
      },
    });
    // Import the emitted artifact through its real relative ESM dependency.
    built = await import(/* @vite-ignore */ pathToFileURL(path.join(output, 'nested/entry.js')).href) as ValidatorModule;
    expect(await readFile(path.join(output, assetFile), 'utf8')).toBe(assetCode);
  }, 60_000);

  afterAll(async () => { if (temporary) await rm(temporary, { recursive: true, force: true }); });

  it('keeps the large generated source outside the application graph with a local hashed asset', () => {
    expect(bundledModules).not.toContain(generated);
    expect(bundledModules.some(id => id.includes('generated-validators'))).toBe(false);
    const digest = createHash('sha256').update(assetCode).digest('hex');
    expect(assetFile).toBe(`assets/contract-validators.${digest}.js`);
    expect(entryCode).toContain('../assets/contract-validators.');
    expect(entryCode).not.toContain(temporary);
    expect(entryCode).not.toMatch(/https?:\/\//u);
    expect(assetCode).not.toMatch(/unsafe-eval|new Function|require\("|eval\(/u);
  });

  it('retains all 173 synchronous validators and identical errors', () => {
    const expected = original as unknown as ValidatorModule;
    const names = Object.keys(expected.contractValidators).sort();
    expect(names).toHaveLength(173);
    expect(Object.keys(built.contractValidators).sort()).toEqual(names);
    const inputs = [null, {}, [], '', 0, true, approvalFixture, eventFixture,
      mediaFixture, maintenanceFixture, messageFixture, sessionFixture,
      { schemaVersion: 'unknown', status: 'completed' }];
    for (const name of names) {
      for (const value of inputs) {
        const reference = expected.contractValidators[name];
        const actual = built.contractValidators[name];
        const valid = reference(value);
        const errors = JSON.stringify(reference.errors);
        expect(actual(value), name).toBe(valid);
        expect(JSON.stringify(actual.errors), name).toBe(errors);
      }
    }
  });

  it('accepts the same real application fixtures through the emitted asset', () => {
    for (const [name, value] of [
      ['agent-approval.v1', approvalFixture], ['agent-event.v1', eventFixture],
      ['agent-media.v1', mediaFixture], ['agent-memory-maintenance-status.v1', maintenanceFixture],
      ['agent-message.v1', messageFixture], ['agent-session.v1', sessionFixture],
    ] as const) expect(built.contractValidators[name](value), name).toBe(true);
  });

  it('preserves the three tolerant event/message validators', () => {
    const expected = original as unknown as ValidatorModule;
    const names = ['tolerantAgentEventValidator', 'tolerantRoomEventValidator', 'tolerantAgentMessageValidator'] as const;
    for (const name of names) {
      for (const value of [null, {}, [], eventFixture, messageFixture, { schemaVersion: 'unknown', type: 'future_event', payload: {} }]) {
        const valid = expected[name](value);
        const errors = JSON.stringify(expected[name].errors);
        expect(built[name](value)).toBe(valid);
        expect(JSON.stringify(built[name].errors)).toBe(errors);
      }
    }
    expect(built.tolerantAgentEventValidator({ ...eventFixture, eventType: 'future_agent_chart', payload: { chartId: 'one' } })).toBe(true);
    expect(built.tolerantRoomEventValidator({
      schemaVersion: 'rag-ime.agent-room-event.v1', eventId: 'room:1', roomId: 'room',
      sequence: 1, turnId: 'turn', eventType: 'future_room_vote', participantId: null,
      sourceSessionId: 'session', createdAtMs: 1, payload: {}, resumeToken: 'room:1',
    })).toBe(true);
    expect(built.tolerantAgentMessageValidator({
      ...messageFixture,
      blocks: [...messageFixture.blocks, { id: 'future', type: 'interactive_chart', status: 'completed', presentationKind: 'chart', data: { series: [1, 2] } }],
    })).toBe(true);
  });

  it('refreshes a watch-cached importer to the new asset hash without changing other external paths', async () => {
    const root = await mkdtemp(path.join(os.tmpdir(), 'paw-validator-watch-'));
    const output = path.join(root, 'dist');
    const source = path.join(root, 'src/contracts/generated-validators.ts');
    await mkdir(path.dirname(source), { recursive: true });
    await writeFile(path.join(root, 'package.json'), '{"type":"module"}\n');
    await writeFile(source, 'export const revision = 1 as const;\n');
    await writeFile(path.join(root, 'entry.ts'), "export { revision } from './src/contracts/generated-validators'; export { keep } from 'existing-external';\n");
    const watcher = await build({
      configFile: false, root, base: './', logLevel: 'silent',
      plugins: [contractValidatorAsset(), {
        name: 'existing-local-external',
        buildStart() { this.emitFile({ type: 'asset', fileName: 'existing-external.js', source: 'export const keep = 7;\n' }); },
      }],
      build: {
        outDir: output, emptyOutDir: false, minify: false, reportCompressedSize: false, watch: {},
        rollupOptions: {
          input: path.join(root, 'entry.ts'), preserveEntrySignatures: 'strict', external: ['existing-external'],
          output: { entryFileNames: 'entry.js', paths: id => id === 'existing-external' ? './existing-external.js' : '' },
        },
      },
    });
    if (!('on' in watcher)) throw new Error('Expected a Rollup watcher');
    const nextBundle = () => new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => { watcher.off('event', listener); reject(new Error('Watch rebuild did not finish')); }, 10_000);
      const listener = (event: { code: string; error?: unknown }) => {
        if (event.code !== 'BUNDLE_END' && event.code !== 'ERROR') return;
        clearTimeout(timer); watcher.off('event', listener);
        if (event.code === 'ERROR') reject(event.error); else resolve();
      };
      watcher.on('event', listener);
    });
    try {
      await nextBundle();
      const entry = path.join(output, 'entry.js');
      const firstCode = await readFile(entry, 'utf8');
      const first = await import(/* @vite-ignore */ `${pathToFileURL(entry).href}?revision=1`);
      expect(first.revision).toBe(1); expect(first.keep).toBe(7);
      const next = nextBundle();
      await writeFile(source, 'export const revision = 2 as const;\n');
      await next;
      const secondCode = await readFile(entry, 'utf8');
      const hash = /contract-validators\.([a-f0-9]{64})\.js/u;
      expect(firstCode.match(hash)?.[1]).toBeTruthy();
      expect(secondCode.match(hash)?.[1]).toBeTruthy();
      expect(secondCode.match(hash)?.[1]).not.toBe(firstCode.match(hash)?.[1]);
      const second = await import(/* @vite-ignore */ `${pathToFileURL(entry).href}?revision=2`);
      expect(second.revision).toBe(2); expect(second.keep).toBe(7);
    } finally {
      await watcher.close();
      await rm(root, { recursive: true, force: true });
    }
  }, 30_000);
});
