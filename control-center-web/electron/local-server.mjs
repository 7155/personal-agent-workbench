import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import {
  defaultPawHostPort,
  resolveHostMode,
  validateProductionFrontend,
} from './host-config.mjs';

const contentTypes = new Map([
  ['.css', 'text/css; charset=utf-8'],
  ['.html', 'text/html; charset=utf-8'],
  ['.ico', 'image/x-icon'],
  ['.jpeg', 'image/jpeg'],
  ['.jpg', 'image/jpeg'],
  ['.js', 'text/javascript; charset=utf-8'],
  ['.json', 'application/json; charset=utf-8'],
  ['.png', 'image/png'],
  ['.svg', 'image/svg+xml'],
  ['.webmanifest', 'application/manifest+json; charset=utf-8'],
  ['.woff2', 'font/woff2'],
]);

export async function startPawHostServer({
  browserBridge = null,
  fallbackToEphemeralPort = false,
  frontendEntry,
  controlOrigin = 'http://127.0.0.1:8768',
  hostMode = resolveHostMode(),
  port = defaultPawHostPort,
}) {
  if (hostMode !== 'development' && hostMode !== 'production') {
    throw new Error(`Unsupported PAW host mode: ${hostMode}`);
  }
  if (hostMode === 'production') validateProductionFrontend(frontendEntry);
  const frontendRoot = path.dirname(frontendEntry);
  const controlUrl = new URL(controlOrigin);
  let hostOrigin = '';
  const server = http.createServer((request, response) => {
    const requestUrl = new URL(request.url || '/', 'http://127.0.0.1');
    if (requestUrl.pathname.startsWith('/__paw_browser/')) {
      void handleBrowserBridgeRequest(request, response, requestUrl.pathname, browserBridge);
      return;
    }
    if (requestUrl.pathname.startsWith('/api/') || requestUrl.pathname.startsWith('/control/') || requestUrl.pathname === '/__paw_html_preview') {
      if (!isAllowedControlRequest(request, hostOrigin)) {
        response.writeHead(403, { 'Content-Type': 'application/json; charset=utf-8' });
        response.end('{"ok":false,"error":"PAW host origin rejected"}\n');
        return;
      }
      proxyControlRequest(request, response, controlUrl);
      return;
    }
    serveFrontend(requestUrl.pathname, response, frontendRoot, frontendEntry);
  });
  try {
    await listenLoopback(server, port);
  } catch (error) {
    if (!fallbackToEphemeralPort || error?.code !== 'EADDRINUSE' || Number(port) === 0) {
      throw error;
    }
    await listenLoopback(server, 0);
  }
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('PAWOS host did not bind loopback');
  hostOrigin = `http://127.0.0.1:${address.port}`;
  return {
    close: () => new Promise((resolve) => server.close(resolve)),
    hostMode,
    origin: hostOrigin,
    production: hostMode === 'production',
  };
}

function listenLoopback(server, port) {
  return new Promise((resolve, reject) => {
    const onError = (error) => {
      server.off('listening', onListening);
      reject(error);
    };
    const onListening = () => {
      server.off('error', onError);
      resolve();
    };
    server.once('error', onError);
    server.once('listening', onListening);
    server.listen(port, '127.0.0.1');
  });
}

function isAllowedControlRequest(request, hostOrigin) {
  const expectedHost = new URL(hostOrigin).host;
  if (request.headers.host !== expectedHost) return false;
  const origin = request.headers.origin;
  if (origin && origin !== hostOrigin) return false;
  const fetchSite = request.headers['sec-fetch-site'];
  if (fetchSite && fetchSite !== 'same-origin' && fetchSite !== 'none') return false;
  return ['GET', 'HEAD', 'OPTIONS'].includes(request.method || 'GET') || origin === hostOrigin;
}

async function handleBrowserBridgeRequest(request, response, pathname, bridge) {
  if (!bridge || request.method !== 'POST' || request.headers['x-paw-browser-token'] !== bridge.token) {
    response.writeHead(403).end();
    return;
  }
  let body = '';
  for await (const chunk of request) body += chunk;
  try {
    const input = JSON.parse(body || '{}');
    const result = pathname === '/__paw_browser/tabs'
      ? await bridge.createTab(String(input.url || 'about:blank'))
      : pathname === '/__paw_browser/activate'
        ? await bridge.activateTarget(String(input.targetId || ''))
        : pathname === '/__paw_browser/screenshot' && typeof bridge.captureScreenshot === 'function'
          ? await bridge.captureScreenshot(String(input.targetId || ''))
        : null;
    if (!result) {
      response.writeHead(404).end();
      return;
    }
    response.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
    response.end(`${JSON.stringify(result)}\n`);
  } catch (error) {
    response.writeHead(500, { 'Content-Type': 'application/json; charset=utf-8' });
    response.end(`${JSON.stringify({ ok: false, error: String(error?.message || error) })}\n`);
  }
}

function proxyControlRequest(request, response, controlUrl) {
  const headers = { ...request.headers };
  headers.host = controlUrl.host;
  if (headers.origin) headers.origin = controlUrl.origin;
  let upstreamBody;
  const upstream = http.request({
    hostname: controlUrl.hostname,
    port: controlUrl.port,
    method: request.method,
    path: request.url,
    headers,
  }, (upstreamResponse) => {
    upstreamBody = upstreamResponse;
    upstreamResponse.on('error', () => response.destroy());
    upstreamResponse.once('aborted', () => response.destroy());
    if (response.destroyed) {
      upstreamResponse.destroy();
      return;
    }
    response.writeHead(upstreamResponse.statusCode || 502, upstreamResponse.headers);
    upstreamResponse.pipe(response);
  });
  const cancelUpstream = () => {
    upstreamBody?.destroy();
    upstream.destroy();
  };
  // pipe() does not cancel its source when the destination disappears. SSE
  // subscriptions must release the Gateway socket when the renderer leaves.
  // IncomingMessage 'close' also fires for a normally completed GET, so bind
  // cancellation to the response lifetime and incomplete request bodies.
  response.once('close', () => {
    if (!response.writableFinished) cancelUpstream();
  });
  request.once('aborted', cancelUpstream);
  upstream.on('error', () => {
    if (response.destroyed) return;
    if (response.headersSent) return response.destroy();
    response.writeHead(502, { 'Content-Type': 'application/json; charset=utf-8' });
    response.end('{"ok":false,"error":"PAW local authority is unavailable"}\n');
  });
  request.pipe(upstream);
}

function serveFrontend(pathname, response, frontendRoot, frontendEntry) {
  let decoded;
  try {
    decoded = decodeURIComponent(pathname);
  } catch {
    response.writeHead(400).end();
    return;
  }
  const relative = decoded === '/' ? 'index.html' : decoded.replace(/^\/+/, '');
  let target = path.resolve(frontendRoot, relative);
  if (!target.startsWith(`${frontendRoot}${path.sep}`) || !fs.existsSync(target) || !fs.statSync(target).isFile()) {
    target = frontendEntry;
  }
  response.writeHead(200, {
    'Cache-Control': target === frontendEntry ? 'no-store' : 'public, max-age=31536000, immutable',
    'Content-Type': contentTypes.get(path.extname(target).toLowerCase()) || 'application/octet-stream',
    'X-Content-Type-Options': 'nosniff',
  });
  fs.createReadStream(target).pipe(response);
}
