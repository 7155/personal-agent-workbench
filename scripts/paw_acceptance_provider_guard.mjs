/**
 * Opt-in acceptance-test preload, not a production transport or a sandbox.
 *
 * NODE_OPTIONS="--import=/explicit/path/paw_acceptance_provider_guard.mjs"
 * PAW_CALL_GUARD_DIR=/explicit/fresh/run-directory
 *
 * The caller creates reservation.json before starting any Host. Required fields:
 * reservedUsd, priorKnownUsd, maxProviderRequests, maxRequestBytes. The accepted
 * provider/model/effort are deliberately fixed to the verified bounded batch:
 * openai-codex / gpt-6.1-sol / xhigh. At most 12 HTTP attempts share one directory.
 * Reservations are accounting bounds supplied by the runner, not price quotes.
 *
 * Claim files are never released, including on transport failure or Host restart.
 * Keep the directory private and do not remove claims while a run is active.
 * Only metadata is recorded: never prompts, response bodies, headers or URLs.
 * Direct node:http, imported fetch functions and arbitrary subprocess networking
 * are outside this test injection's scope. The prepared runtime must use fetch.
 */
import { appendFileSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import * as zlib from 'node:zlib';

const directory = process.env.PAW_CALL_GUARD_DIR;
if (!directory) throw new Error('PAW_GUARD_DIRECTORY_REQUIRED');
const root = resolve(directory);
if (!statSync(root).isDirectory()) throw new Error('PAW_GUARD_DIRECTORY_REQUIRED');
const reservationPath = join(root, 'reservation.json');
const model = 'gpt-6.1-sol';
const effort = 'xhigh';
const hardRequestCap = 12;
const NativeHeaders = globalThis.Headers;

function log(file, entry) {
  appendFileSync(join(root, file), `${JSON.stringify(entry)}\n`, { mode: 0o600 });
}

function deny(code, event) {
  log('denied.jsonl', { event, pid: process.pid });
  throw new Error(code);
}

function readReservation() {
  let value;
  try {
    value = JSON.parse(readFileSync(reservationPath, 'utf8'));
  } catch {
    throw new Error('PAW_GUARD_RESERVATION_INVALID');
  }
  if (!value || typeof value !== 'object'
    || !Number.isFinite(value.reservedUsd) || value.reservedUsd < 0
    || !Number.isFinite(value.priorKnownUsd) || value.priorKnownUsd < 0
    || value.reservedUsd + value.priorKnownUsd > 100
    || !Number.isSafeInteger(value.maxProviderRequests) || value.maxProviderRequests < 0
    || (value.maxProviderRequests > 0 && value.reservedUsd === 0)
    || !Number.isSafeInteger(value.maxRequestBytes) || value.maxRequestBytes < 1
    || (value.provider !== undefined && value.provider !== 'openai-codex')
    || (value.model !== undefined && value.model !== model)
    || (value.reasoningEffort !== undefined && value.reasoningEffort !== effort)) {
    throw new Error('PAW_GUARD_RESERVATION_INVALID');
  }
  return value;
}

const initialReservation = readReservation();
let transportFetch = globalThis.fetch;
if (typeof transportFetch !== 'function') throw new Error('PAW_GUARD_FETCH_UNAVAILABLE');

// The verified adapter falls back to HTTP/SSE before sending a model request
// when WebSocket is unavailable. In particular, undici.install() cannot restore
// WebSocket or replace the guard with its newly installed fetch implementation.
Object.defineProperty(globalThis, 'WebSocket', {
  configurable: false,
  get: () => undefined,
  set: () => log('guard-loaded.jsonl', {
    event: 'websocket_install_blocked', pid: process.pid,
  }),
});

function requestBytes(body) {
  if (typeof body === 'string') return Buffer.from(body, 'utf8');
  if (body instanceof ArrayBuffer) return Buffer.from(body).subarray();
  if (ArrayBuffer.isView(body)) {
    return Buffer.from(body.buffer, body.byteOffset, body.byteLength);
  }
  // Streams and Request-only bodies have not been verified for this adapter.
  // Fail closed instead of consuming one body and forwarding a different one.
  deny('PAW_GUARD_BODY_UNSUPPORTED', 'request_body_rejected_before_network');
}

const guardedFetch = async (input, init) => {
  let url;
  try {
    url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url);
  } catch {
    deny('PAW_GUARD_URL_REJECTED', 'url_rejected_before_network');
  }
  const options = { ...init };
  const method = String(options.method || input?.method || 'GET').toUpperCase();
  const loopback = ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname);
  if (loopback && ['http:', 'https:'].includes(url.protocol)
    && !url.username && !url.password) {
    // Refuse redirects so an allowed loopback request cannot silently become an
    // unreserved external request. This preload never starts a login flow.
    return transportFetch(input, { ...options, redirect: 'error' });
  }
  if (url.origin !== 'https://chatgpt.com'
    || url.pathname !== '/backend-api/codex/responses'
    || url.username || url.password || url.search || url.hash || method !== 'POST') {
    deny('PAW_GUARD_ENDPOINT_REJECTED', 'endpoint_rejected_before_network');
  }

  const currentReservation = readReservation();
  const limit = Math.min(hardRequestCap, initialReservation.maxProviderRequests,
    currentReservation.maxProviderRequests);
  if (limit === 0) {
    deny('PAW_GUARD_ZERO_REQUEST_PROOF', 'budget_zero_rejected_before_network');
  }
  const byteLimit = Math.min(initialReservation.maxRequestBytes,
    currentReservation.maxRequestBytes);
  const headers = new NativeHeaders(options.headers ?? input?.headers);
  // Copy the bytes once. The validated body and the forwarded body are the same
  // immutable snapshot even when a caller supplied a mutable typed array.
  const wireBytes = Buffer.from(requestBytes(options.body));
  let decodedBytes = wireBytes;
  const encoding = headers.get('content-encoding');
  if (encoding && encoding !== 'identity') {
    if (encoding !== 'zstd' || typeof zlib.zstdDecompressSync !== 'function') {
      deny('PAW_GUARD_ENCODING_REJECTED', 'encoding_rejected_before_network');
    }
    try {
      decodedBytes = zlib.zstdDecompressSync(wireBytes, { maxOutputLength: byteLimit });
    } catch {
      deny('PAW_GUARD_BODY_REJECTED', 'request_body_rejected_before_network');
    }
  }
  if (decodedBytes.byteLength > byteLimit) {
    deny('PAW_GUARD_REQUEST_TOO_LARGE', 'request_size_rejected_before_network');
  }
  let body;
  try {
    body = JSON.parse(decodedBytes.toString('utf8'));
  } catch {
    deny('PAW_GUARD_BODY_REJECTED', 'request_body_rejected_before_network');
  }
  if (body?.model !== model || body.reasoning?.effort !== effort) {
    deny('PAW_GUARD_MODEL_REJECTED', 'model_rejected_before_network');
  }

  // O_EXCL claims are shared across concurrent child Hosts and Host restarts.
  // A failed/unknown network attempt still owns its slot and reservation.
  let slot = 0;
  for (let index = 1; index <= limit; index += 1) {
    try {
      writeFileSync(join(root, `provider-call-${index}.claim`), JSON.stringify({
        at: new Date().toISOString(), model, bytes: decodedBytes.byteLength,
        slot: index, reservedUsd: initialReservation.reservedUsd,
      }), { flag: 'wx', mode: 0o600 });
      slot = index;
      break;
    } catch (error) {
      if (error.code !== 'EEXIST') throw error;
    }
  }
  if (!slot) deny('PAW_PROVIDER_CALL_LIMIT', 'request_cap_rejected_before_network');
  try {
    writeFileSync(join(root, 'provider-request.once'), JSON.stringify({ firstSlot: slot }),
      { flag: 'wx', mode: 0o600 });
  } catch (error) {
    if (error.code !== 'EEXIST') throw error;
  }
  const response = await transportFetch(input, {
    ...options, headers, body: wireBytes, redirect: 'error',
  });
  // Only the status is needed for reconciliation; omit arbitrary provider
  // header values so tokens or private data cannot enter acceptance evidence.
  log('http.jsonl', { slot, status: response.status });
  return response;
};

Object.defineProperty(globalThis, 'fetch', {
  configurable: false,
  get: () => guardedFetch,
  set: (replacement) => {
    if (replacement === guardedFetch) return;
    if (typeof replacement !== 'function') throw new Error('PAW_GUARD_FETCH_INVALID');
    transportFetch = replacement;
    log('guard-loaded.jsonl', {
      event: 'fetch_transport_replaced_guard_preserved', pid: process.pid,
    });
  },
});
log('guard-loaded.jsonl', { pid: process.pid, at: new Date().toISOString() });
