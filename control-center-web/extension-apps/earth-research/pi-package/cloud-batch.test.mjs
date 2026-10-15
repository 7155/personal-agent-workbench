import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { runEarthScriptBatch } from './cloud-batch.mjs';

function fixture(t, runner) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'earth-cloud-batch-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.mkdirSync(path.join(root, '.earth'));
  const entry = path.join(root, 'runner.mjs');
  fs.writeFileSync(entry, runner);
  fs.writeFileSync(path.join(root, '.earth', 'runtime.json'), JSON.stringify({ runner: entry, project: 'test-project', dependencies: root, python: 'python3' }));
  for (const name of ['first.js', 'second.js']) fs.writeFileSync(path.join(root, name), '');
  return root;
}

test('nonzero runner exit retains the failed run identity and artifact instead of a command dump', async t => {
  const root = fixture(t, `console.log(JSON.stringify({runId:'failed-run',status:'failed',artifact:'run.json',error:'Too many pixels'}));process.exitCode=1;`);
  const result = await runEarthScriptBatch({ root, scripts: ['first.js'] });
  assert.equal(result.status, 'failed');
  assert.equal(result.failed, 1);
  assert.equal(result.results[0].runId, 'failed-run');
  assert.equal(result.results[0].artifact, 'run.json');
  assert.equal(result.results[0].error, 'Too many pixels');
});

test('one failed script preserves its receipt while the next script completes', async t => {
  const root = fixture(t, `const script=process.argv[process.argv.indexOf('--script')+1];const bad=script.endsWith('first.js');console.log(JSON.stringify({runId:bad?'failed-run':'completed-run',status:bad?'failed':'completed',error:bad?'No data':null}));process.exitCode=bad?1:0;`);
  const result = await runEarthScriptBatch({ root, scripts: ['first.js', 'second.js'], concurrency: 1 });
  assert.equal(result.status, 'partial');
  assert.equal(result.completed, 1);
  assert.equal(result.failed, 1);
  assert.deepEqual(result.results.map(item => [item.status, item.runId]), [['failed', 'failed-run'], ['completed', 'completed-run']]);
});

test('an exit without a terminal receipt is a counted failure', async t => {
  const root = fixture(t, `console.log('ordinary output without a run receipt');`);
  const result = await runEarthScriptBatch({ root, scripts: ['first.js'] });
  assert.equal(result.status, 'failed');
  assert.equal(result.failed, 1);
  assert.equal(result.results[0].status, 'failed');
  assert.equal(result.results[0].runId, null);
});

test('a completed receipt cannot turn a nonzero process exit into success', async t => {
  const root = fixture(t, `console.log(JSON.stringify({runId:'uncertain-run',status:'completed'}));process.exitCode=1;`);
  const result = await runEarthScriptBatch({ root, scripts: ['first.js'] });
  assert.equal(result.results[0].runId, 'uncertain-run');
  assert.equal(result.results[0].status, 'failed');
  assert.equal(result.failed, 1);
});

test('Stop terminates the active child, retains its cancellation receipt and never starts the queued script', { timeout: 10_000 }, async t => {
  const root = fixture(t, `import fs from 'node:fs';import path from 'node:path';const script=path.basename(process.argv[process.argv.indexOf('--script')+1]);fs.writeFileSync(script+'.started','yes');process.on('SIGTERM',()=>{console.log(JSON.stringify({runId:'cancelled-run',status:'cancelled',artifact:'cancelled.json'}));process.exit(130);});setTimeout(()=>process.exit(2),2000);`);
  const controller = new AbortController();
  const running = runEarthScriptBatch({ root, scripts: ['first.js', 'second.js'], concurrency: 1, signal: controller.signal });
  for (let i = 0; !fs.existsSync(path.join(root, 'first.js.started')) && i < 200; i++) await new Promise(resolve => setTimeout(resolve, 10));
  assert.ok(fs.existsSync(path.join(root, 'first.js.started')), 'first child started');
  controller.abort();
  const result = await running;
  assert.equal(result.status, 'cancelled');
  assert.equal(result.cancelled, 2);
  assert.equal(result.results[0].runId, 'cancelled-run');
  assert.equal(result.results[0].remoteComputeMayContinue, true);
  assert.equal(result.results[1].runId, null);
  assert.equal(fs.existsSync(path.join(root, 'second.js.started')), false);
});

test('a batch stopped before dispatch starts no child', async t => {
  const root = fixture(t, `import fs from 'node:fs';fs.writeFileSync('unexpected-start','yes');`);
  const controller = new AbortController();
  controller.abort();
  const result = await runEarthScriptBatch({ root, scripts: ['first.js', 'second.js'], signal: controller.signal });
  assert.equal(result.status, 'cancelled');
  assert.equal(result.cancelled, 2);
  assert.equal(fs.existsSync(path.join(root, 'unexpected-start')), false);
});

test('the real script runner emits the same cancelled identity it persists', { timeout: 10_000 }, async t => {
  const root = fixture(t, '');
  const sdk = path.join(root, 'sdk');
  const module = path.join(sdk, 'node_modules', '@google', 'earthengine');
  fs.mkdirSync(module, { recursive: true });
  fs.writeFileSync(path.join(sdk, 'package.json'), '{}');
  // This SDK and token helper are inert fixtures: no Google or Provider call.
  fs.writeFileSync(path.join(module, 'index.js'), `module.exports={data:{setAuthToken(){}},initialize(){require('node:fs').writeFileSync('sdk-ready','yes');setTimeout(()=>{},5000);}};`);
  const python = path.join(root, 'fixture-python');
  fs.writeFileSync(python, "#!/bin/sh\nprintf 'fixture-token'\n", { mode: 0o700 });
  const child = execFile(process.execPath, [fileURLToPath(new URL('./run-script.mjs', import.meta.url)), '--root', root, '--script', 'first.js', '--project', 'test-project', '--dependencies', sdk, '--python', python], { cwd: root, env: { ...process.env, HTTPS_PROXY: '', https_proxy: '' } });
  t.after(() => child.kill('SIGKILL'));
  let stdout = '';
  child.stdout.on('data', chunk => { stdout += chunk; });
  const completed = new Promise(resolve => child.on('close', code => resolve(code)));
  for (let i = 0; !fs.existsSync(path.join(root, 'sdk-ready')) && i < 200; i++) await new Promise(resolve => setTimeout(resolve, 10));
  assert.ok(fs.existsSync(path.join(root, 'sdk-ready')));
  child.kill('SIGTERM');
  assert.equal(await completed, 130);
  const receipt = JSON.parse(stdout.trim());
  const persisted = JSON.parse(fs.readFileSync(receipt.artifact, 'utf8'));
  assert.equal(receipt.runId, persisted.runId);
  assert.equal(receipt.status, 'cancelled');
  assert.equal(persisted.status, 'cancelled');
  assert.equal(persisted.remoteComputeMayContinue, true);
});
