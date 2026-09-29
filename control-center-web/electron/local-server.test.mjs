import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { once } from 'node:events';
import { setTimeout as delay } from 'node:timers/promises';
import test from 'node:test';
import { startPawHostServer } from './local-server.mjs';

async function proxyFixture(t, handler) {
  const upstream = http.createServer(handler);
  upstream.listen(0, '127.0.0.1');
  await once(upstream, 'listening');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'paw-proxy-lifecycle-'));
  const frontendEntry = path.join(root, 'index.html');
  fs.writeFileSync(frontendEntry, '<title>proxy test</title>');
  const host = await startPawHostServer({
    frontendEntry, hostMode: 'development', port: 0,
    controlOrigin: `http://127.0.0.1:${upstream.address().port}`,
  });
  t.after(async () => {
    upstream.closeAllConnections();
    await Promise.all([host.close(), new Promise(resolve => upstream.close(resolve))]);
    fs.rmSync(root, { recursive: true });
  });
  return host;
}

for (const sendHeaders of [true, false]) {
  test(`client disconnect releases the upstream subscription ${sendHeaders ? 'after' : 'before'} headers`, async (t) => {
    let closed = false;
    let accepted;
    const ready = new Promise(resolve => { accepted = resolve; });
    const host = await proxyFixture(t, (_request, response) => {
      response.on('close', () => { closed = true; });
      if (sendHeaders) {
        response.writeHead(200, { 'Content-Type': 'text/event-stream' });
        response.write(': connected\n\n');
      }
      accepted();
    });
    const client = http.get(`${host.origin}/api/events`);
    client.on('error', () => {});
    if (sendHeaders) {
      const [response] = await once(client, 'response');
      await once(response, 'data');
    } else {
      await ready;
    }
    client.destroy();
    for (let attempt = 0; attempt < 50 && !closed; attempt += 1) await delay(10);
    assert.equal(closed, true, 'abandoned SSE connection still occupies an upstream slot');
  });
}

test('upstream failure terminates the downstream stream so the client can reconnect', async (t) => {
  let peer;
  const host = await proxyFixture(t, (_request, response) => {
    peer = response;
    response.writeHead(200, { 'Content-Type': 'text/event-stream' });
    response.write(': connected\n\n');
  });
  const client = http.get(`${host.origin}/api/events`);
  client.on('error', () => {});
  const [response] = await once(client, 'response');
  let closed = false;
  response.on('error', () => {});
  response.on('close', () => { closed = true; });
  await once(response, 'data');
  peer.destroy();
  for (let attempt = 0; attempt < 50 && !closed; attempt += 1) await delay(10);
  client.destroy();
  assert.equal(closed, true, 'failed upstream left the client waiting forever');
});
