import type { ControlTransport } from './transport';

const scopes = new WeakMap<ControlTransport, string>();
let nextScope = 0;

/** In-memory ownership follows the live transport instance. The durable
 * connectionIdentity is for restored drafts, not cross-connection caches. */
export function controlTransportScopeKey(transport: ControlTransport): string {
  let scope = scopes.get(transport);
  if (!scope) {
    scope = `transport:${++nextScope}`;
    scopes.set(transport, scope);
  }
  return scope;
}
