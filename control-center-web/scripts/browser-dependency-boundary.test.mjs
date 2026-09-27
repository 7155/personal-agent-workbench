import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import test from 'node:test';
import config from '../vite.config.ts';

const motionRequire = createRequire(import.meta.resolve('motion/package.json'));
const framerRoot = path.dirname(motionRequire.resolve('framer-motion/package.json'));
const sourcePath = path.join(framerRoot, 'dist/es/render/dom/utils/filter-props.mjs');
const source = await readFile(sourcePath, 'utf8');
const dependencyRequire = createRequire(sourcePath);
const motionDomPackagePath = path.resolve(path.dirname(dependencyRequire.resolve('motion-dom')), '../../package.json');
const motionDomPackage = JSON.parse(await readFile(motionDomPackagePath, 'utf8'));
const motionDomUrl = pathToFileURL(path.join(path.dirname(motionDomPackagePath), motionDomPackage.module)).href;
const validPropUrl = new URL('../../../motion/utils/valid-prop.mjs', pathToFileURL(sourcePath)).href;
const plugin = config.plugins.find((candidate) => candidate.name === 'rag-ime-browser-dependency-boundary');
const context = { error(message) { throw new Error(message); } };
let importSequence = 0;

function transform(code = source, id = sourcePath) {
  return plugin.transform.call(context, code, id);
}

async function browserModule(code) {
  // Use the actual installed dependencies, while evaluating the source as ESM
  // without Node's CommonJS require (the same fallback boundary as a browser).
  const linked = code.replace("from 'motion-dom'", `from ${JSON.stringify(motionDomUrl)}`)
    .replace("from '../../../motion/utils/valid-prop.mjs'", `from ${JSON.stringify(validPropUrl)}`);
  return import(`data:text/javascript;base64,${Buffer.from(linked).toString('base64')}#${importSequence++}`);
}

test('the installed Motion ESM optional Node loader is eliminated without relaxing CSP', () => {
  assert.match(source, /loadExternalIsValidProp\(require\(emotionPkg\)\.default\)/);
  const result = transform();
  assert.ok(result, 'the real installed module must pass through the build transform');
  assert.doesNotMatch(result.code, /\brequire\s*\(|\beval\s*\(|new Function|unsafe-eval/);
  assert.match(result.code, /export \{ filterProps, loadExternalIsValidProp \}/);
});

test('DOM attributes, native events, Motion props and Motion values retain browser fallback behavior', async () => {
  const original = await browserModule(source);
  const browser = await browserModule(transform().code);
  const { motionValue } = await import(motionDomUrl);
  const click = () => {};
  const drag = () => {};
  const props = {
    id: 'card', 'aria-label': 'Card', 'data-probe': 'yes', arbitrary: 'custom',
    onClick: click, draggable: true, onDrag: drag,
    animate: { opacity: 1 }, whileHover: { scale: 1.1 }, onTap: () => {},
    values: [1, 2], liveValue: motionValue(3),
  };
  const expected = { id: 'card', 'aria-label': 'Card', 'data-probe': 'yes', arbitrary: 'custom',
    onClick: click, draggable: true, onDrag: drag };
  assert.deepEqual(browser.filterProps(props, true, false), expected);
  assert.deepEqual(browser.filterProps(props, true, false), original.filterProps(props, true, false));
  assert.equal(browser.filterProps(props, true, true).animate, props.animate);
  assert.equal(browser.filterProps(props, true, true).onTap, props.onTap);
});

test('explicit external prop validation remains injectable and preserves component/native event rules', async () => {
  const browser = await browserModule(transform().code);
  browser.loadExternalIsValidProp((key) => key === 'id' || key.startsWith('data-'));
  const click = () => {};
  const props = { id: 'card', 'data-probe': 'yes', arbitrary: 'custom', onClick: click,
    onTap: () => {}, animate: { opacity: 1 } };
  assert.deepEqual(browser.filterProps(props, true, false), { id: 'card', 'data-probe': 'yes', onClick: click });
  assert.equal(browser.filterProps(props, false, false).arbitrary, 'custom');
  browser.loadExternalIsValidProp(undefined);
  assert.equal(browser.filterProps(props, true, false).arbitrary, undefined);
});

test('dependency drift fails visibly and unrelated require calls are not rewritten', () => {
  assert.throws(() => transform(source.replace('require(emotionPkg)', 'require(newDependency)')), /Motion.*shape changed/);
  assert.equal(transform(source, '/workspace/src/filter-props.mjs'), null);
  assert.equal(transform(source, '/deps/another-package/dist/es/render/dom/utils/filter-props.mjs'), null);
  assert.ok(transform(source, sourcePath.replaceAll('/', '\\')));
});
