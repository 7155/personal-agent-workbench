/** Offline guard tests: every child installs an inert fetch before the preload. */
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import * as zlib from 'node:zlib';

const endpoint = 'https://chatgpt.com/backend-api/codex/responses';
const approvedBody = { model: 'gpt-6.1-sol', reasoning: { effort: 'xhigh' } };
const filename = fileURLToPath(import.meta.url);

async function child(scenario) {
  let calls = 0;
  let replacements = 0;
  let observedRedirect;
  let observedBody;
  const inertFetch = async (_input, init) => {
    calls += 1;
    observedRedirect = init?.redirect;
    observedBody = init?.body ? Buffer.from(init.body).toString('utf8') : undefined;
    if (scenario.transportFailure) throw new Error('synthetic_transport_failure');
    return new Response('synthetic offline response', {
      headers: { 'x-request-id': 'synthetic-private-response-header' },
    });
  };
  globalThis.fetch = inertFetch;
  globalThis.WebSocket = class { constructor() { throw new Error('unexpected websocket'); } };
  try {
    await import('./paw_acceptance_provider_guard.mjs');
    const hooked = globalThis.fetch;
    let nonconfigurable = false;
    if (scenario.replace) {
      globalThis.fetch = async (...args) => { replacements += 1; return inertFetch(...args); };
      globalThis.WebSocket = class {};
      globalThis.fetch = globalThis.fetch;
      try { Object.defineProperty(globalThis, 'fetch', { value: inertFetch }); }
      catch (error) { nonconfigurable = error instanceof TypeError; }
      assert.throws(() => { globalThis.fetch = null; }, /PAW_GUARD_FETCH_INVALID/);
      assert.equal(globalThis.fetch, hooked);
    }
    if (scenario.currentReservation) {
      writeFileSync(join(process.env.PAW_CALL_GUARD_DIR, 'reservation.json'),
        JSON.stringify(scenario.currentReservation));
    }
    if (scenario.waitForGo) {
      process.send({ ready: true });
      await new Promise((resolve) => process.once('message', resolve));
    }
    const results = await Promise.all(Array.from({ length: scenario.attempts ?? 1 }, async () => {
      try {
        const raw = scenario.rawBody ?? JSON.stringify(scenario.body ?? approvedBody);
        let body = raw;
        if (scenario.zstd) body = zlib.zstdCompressSync(Buffer.from(raw));
        if (scenario.typedArray) body = new Uint8Array(Buffer.from(raw));
        if (scenario.unsupportedBody) body = { not: 'a supported body' };
        const headers = {
          authorization: 'Bearer synthetic-private-auth',
          ...(scenario.encoding ? { 'content-encoding': scenario.encoding } : {}),
          ...(scenario.zstd ? { 'content-encoding': 'zstd' } : {}),
        };
        const response = await fetch(scenario.url ?? endpoint, {
          method: scenario.method ?? 'POST', headers, body,
          redirect: 'follow',
        });
        return { status: response.status };
      } catch (error) {
        return { error: error.message };
      }
    }));
    process.stdout.write(JSON.stringify({ calls, replacements, results, nonconfigurable,
      websocketDisabled: globalThis.WebSocket === undefined, observedRedirect, observedBody }));
  } catch (error) {
    process.stdout.write(JSON.stringify({ calls, startupError: error.message }));
  }
}

const reservation = (changes = {}) => ({
  reservedUsd: 1, priorKnownUsd: 0, maxProviderRequests: 1,
  maxRequestBytes: 4096, ...changes,
});

function fixture(t, changes = {}) {
  const root = mkdtempSync(join(tmpdir(), 'paw-provider-guard-offline-'));
  writeFileSync(join(root, 'reservation.json'), JSON.stringify(reservation(changes)));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  return root;
}

function run(root, scenario = {}, barrier) {
  return new Promise((resolve, reject) => {
    const processChild = spawn(process.execPath,
      [filename, '--offline-guard-child', JSON.stringify(scenario)], {
        env: { ...process.env, NODE_OPTIONS: '', PAW_CALL_GUARD_DIR: root },
        stdio: ['ignore', 'pipe', 'pipe', 'ipc'],
      });
    let stdout = '';
    let stderr = '';
    const timeout = setTimeout(() => {
      processChild.kill();
      reject(new Error('offline guard child timed out'));
    }, 20000);
    processChild.stdout.on('data', (chunk) => { stdout += chunk; });
    processChild.stderr.on('data', (chunk) => { stderr += chunk; });
    processChild.on('message', (message) => {
      if (message.ready) barrier(processChild);
    });
    processChild.once('error', (error) => { clearTimeout(timeout); reject(error); });
    processChild.once('close', (code) => {
      clearTimeout(timeout);
      if (code !== 0) return reject(new Error(`offline child exit ${code}: ${stderr}`));
      try { resolve(JSON.parse(stdout)); } catch (error) { reject(error); }
    });
  });
}

function claims(root) {
  return readdirSync(root).filter((name) => /^provider-call-\d+\.claim$/.test(name));
}

function evidence(root, name) {
  return readFileSync(join(root, name), 'utf8').trim().split('\n').map(JSON.parse);
}

if (process.argv[2] === '--offline-guard-child') {
  await child(JSON.parse(process.argv[3]));
} else {
  test('zero budget rejects before the inert transport, including SDK replacement', async (t) => {
    const root = fixture(t, { maxProviderRequests: 0 });
    const result = await run(root, { replace: true, attempts: 2 });
    assert.equal(result.calls, 0);
    assert.equal(result.replacements, 0);
    assert.equal(result.websocketDisabled, true);
    assert.equal(result.nonconfigurable, true);
    assert.ok(result.results.every((row) => row.error === 'PAW_GUARD_ZERO_REQUEST_PROOF'));
    assert.equal(claims(root).length, 0);
    assert.ok(evidence(root, 'denied.jsonl').every((row) =>
      row.event === 'budget_zero_rejected_before_network'));
  });

  test('SDK replacement remains guarded and admitted bytes use the replacement transport', async (t) => {
    const root = fixture(t);
    const result = await run(root, { replace: true, typedArray: true, attempts: 2 });
    assert.equal(result.calls, 1);
    assert.equal(result.replacements, 1);
    assert.equal(result.nonconfigurable, true);
    assert.equal(result.websocketDisabled, true);
    assert.equal(result.observedRedirect, 'error');
    assert.deepEqual(JSON.parse(result.observedBody), approvedBody);
    assert.equal(result.results.filter((row) => row.error === 'PAW_PROVIDER_CALL_LIMIT').length, 1);
    assert.equal(claims(root).length, 1);
    assert.deepEqual(evidence(root, 'http.jsonl'), [{ slot: 1, status: 200 }]);
  });

  test('unapproved endpoint, host, scheme, method and redirect targets never reach transport', async (t) => {
    const root = fixture(t);
    const scenarios = [
      { url: 'https://api.openai.com/v1/responses' },
      { url: 'https://chatgpt.com.example.invalid/backend-api/codex/responses' },
      { url: 'https://auth.openai.com/oauth/token' },
      { url: 'https://example.invalid/private/synthetic-secret', method: 'GET' },
      { url: 'http://chatgpt.com/backend-api/codex/responses' },
      { url: 'https://chatgpt.com:444/backend-api/codex/responses' },
      { url: 'https://chatgpt.com/other/responses' },
      { url: `${endpoint}?token=synthetic-secret` },
      { url: 'https://synthetic-secret@chatgpt.com/backend-api/codex/responses' },
      { method: 'GET' },
    ];
    for (const scenario of scenarios) {
      const result = await run(root, scenario);
      assert.equal(result.calls, 0, JSON.stringify(scenario));
      assert.equal(result.results[0].error, 'PAW_GUARD_ENDPOINT_REJECTED');
    }
    assert.equal(claims(root).length, 0);
  });

  test('unapproved model and reasoning effort reject before network', async (t) => {
    const root = fixture(t);
    for (const body of [
      { ...approvedBody, model: 'unapproved-model' },
      { ...approvedBody, reasoning: { effort: 'low' } }, {}, null,
    ]) {
      const result = await run(root, { rawBody: JSON.stringify(body) });
      assert.equal(result.calls, 0);
      assert.equal(result.results[0].error, 'PAW_GUARD_MODEL_REJECTED');
    }
    assert.equal(claims(root).length, 0);
  });

  test('malformed, oversized and unverified bodies fail closed', async (t) => {
    const root = fixture(t, { maxRequestBytes: 128 });
    for (const scenario of [
      { rawBody: '{broken' },
      { body: { ...approvedBody, input: 'x'.repeat(1000) } },
      { unsupportedBody: true },
      { encoding: 'gzip' },
    ]) {
      const result = await run(root, scenario);
      assert.equal(result.calls, 0);
      assert.match(result.results[0].error, /^PAW_GUARD_/);
    }
    assert.equal(claims(root).length, 0);
  });

  test('zstd payload is inspected before forwarding, with a decompressed size cap', {
    skip: typeof zlib.zstdCompressSync !== 'function',
  }, async (t) => {
    const root = fixture(t, { maxRequestBytes: 128 });
    const oversized = await run(root, {
      zstd: true, body: { ...approvedBody, input: 'x'.repeat(1000) },
    });
    assert.equal(oversized.calls, 0);
    const wrongModel = await run(root, {
      zstd: true, body: { ...approvedBody, model: 'unapproved-model' },
    });
    assert.equal(wrongModel.calls, 0);
    const allowed = await run(root, { zstd: true });
    assert.equal(allowed.calls, 1);
    assert.equal(claims(root).length, 1);
  });

  test('atomic cross-process claims enforce cap under synchronized concurrent Hosts', async (t) => {
    const root = fixture(t, { maxProviderRequests: 3 });
    const waiting = [];
    const barrier = (processChild) => {
      waiting.push(processChild);
      if (waiting.length === 8) for (const child of waiting) child.send({ go: true });
    };
    const results = await Promise.all(Array.from({ length: 8 }, () =>
      run(root, { waitForGo: true, attempts: 3 }, barrier)));
    assert.equal(results.reduce((total, row) => total + row.calls, 0), 3);
    assert.equal(claims(root).length, 3);
    assert.deepEqual(new Set(evidence(root, 'http.jsonl').map((row) => row.slot)), new Set([1, 2, 3]));
    assert.equal(results.flatMap((row) => row.results)
      .filter((row) => row.error === 'PAW_PROVIDER_CALL_LIMIT').length, 21);
    const restarted = await run(root);
    assert.equal(restarted.calls, 0);
    assert.equal(restarted.results[0].error, 'PAW_PROVIDER_CALL_LIMIT');
  });

  test('hard cap never exceeds 12 even when the reservation asks for more', async (t) => {
    const root = fixture(t, { maxProviderRequests: 99 });
    const result = await run(root, { attempts: 20 });
    assert.equal(result.calls, 12);
    assert.equal(claims(root).length, 12);
  });

  test('current reservation can tighten but cannot expand the preload cap', async (t) => {
    const root = fixture(t, { maxProviderRequests: 2 });
    const result = await run(root, {
      attempts: 4, currentReservation: reservation({ maxProviderRequests: 8 }),
    });
    assert.equal(result.calls, 2);
    const zeroRoot = fixture(t);
    const stopped = await run(zeroRoot, {
      currentReservation: reservation({ maxProviderRequests: 0 }),
    });
    assert.equal(stopped.calls, 0);
    assert.equal(stopped.results[0].error, 'PAW_GUARD_ZERO_REQUEST_PROOF');
  });

  test('transport failure keeps its claim and blocks retries across Host restart', async (t) => {
    const root = fixture(t);
    const first = await run(root, { transportFailure: true });
    assert.equal(first.calls, 1);
    assert.equal(first.results[0].error, 'synthetic_transport_failure');
    assert.equal(claims(root).length, 1);
    assert.ok(!readdirSync(root).includes('http.jsonl'));
    const second = await run(root);
    assert.equal(second.calls, 0);
    assert.equal(second.results[0].error, 'PAW_PROVIDER_CALL_LIMIT');
  });

  test('invalid reservation never installs an admitted provider transport', async (t) => {
    for (const change of [
      { reservedUsd: -1 }, { reservedUsd: 0 }, { priorKnownUsd: -1 },
      { reservedUsd: 51, priorKnownUsd: 50 }, { reservedUsd: '1' },
      { maxProviderRequests: -1 }, { maxProviderRequests: 1.5 },
      { maxRequestBytes: 0 }, { maxRequestBytes: '1024' },
      { provider: 'unapproved-provider' }, { model: 'unapproved-model' },
      { reasoningEffort: 'low' },
    ]) {
      const root = fixture(t, change);
      const result = await run(root);
      assert.equal(result.calls, 0);
      assert.equal(result.startupError, 'PAW_GUARD_RESERVATION_INVALID');
      assert.equal(claims(root).length, 0);
    }
  });

  test('evidence contains no private prompt, authorization, response header or URL', async (t) => {
    const root = fixture(t);
    await run(root, { body: { ...approvedBody, input: 'synthetic-private-prompt' } });
    await run(root, { url: 'https://example.invalid/synthetic-private-url?key=synthetic-private-key' });
    for (const name of readdirSync(root)) {
      assert.doesNotMatch(readFileSync(join(root, name), 'utf8'),
        /synthetic-private|authorization|Bearer|example\.invalid|backend-api/);
    }
  });

  test('loopback remains usable but cannot follow redirects to an external provider', async (t) => {
    const root = fixture(t, { maxProviderRequests: 0 });
    const result = await run(root, { url: 'http://127.0.0.1:54321/tool', method: 'GET' });
    assert.equal(result.calls, 1);
    assert.equal(result.observedRedirect, 'error');
    assert.equal(claims(root).length, 0);
  });
}
