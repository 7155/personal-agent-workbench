import { pawApp, pawAppForPath } from '../runtime/app-registry';
import type { PawDesktopStore, PawWindowNode } from '../runtime/desktop-store';

function agentIdentity(route: string) {
  const query = new URLSearchParams(route.split('?', 2)[1] ?? '');
  const room = query.get('room')?.trim();
  if (room) return { kind: 'room', id: room };
  const session = (query.get('session') || query.get('sessionId'))?.trim();
  return session ? { kind: 'session', id: session } : null;
}

function ownsRoute(node: PawWindowNode, route: string): boolean {
  if (pawAppForPath(route)?.id !== node.appId) return false;
  if (node.appId !== 'agent') return !node.target && !node.entityId;
  const target = node.target;
  if (target && !(target.kind === 'session' || target.kind === 'room' && !target.panel)) return false;
  const requested = agentIdentity(route);
  const bound = target ?? agentIdentity(node.initialRoute ?? '');
  // A generic Agent entry remains owned by its main window after Home binds
  // a conversation without rewriting the hash. Observers have entity IDs.
  return requested ? bound?.kind === requested.kind && bound.id === requested.id : !node.entityId;
}

function reloadRoute(node: PawWindowNode | undefined): string {
  if (!node) return '/project-field';
  const target = node.target;
  if (node.appId === 'agent' && target) {
    if (!(target.kind === 'session' || target.kind === 'room' && !target.panel)) return '/project-field';
    const initial = agentIdentity(node.initialRoute ?? '');
    if (initial?.kind === target.kind && initial.id === target.id && pawAppForPath(node.initialRoute!)?.id === 'agent') {
      return node.initialRoute!;
    }
    return `/agent?${target.kind === 'room' ? 'room' : 'session'}=${encodeURIComponent(target.id)}`;
  }
  if (target || node.entityId && !node.initialRoute) return '/project-field';
  return node.initialRoute && pawAppForPath(node.initialRoute)?.id === node.appId
    ? node.initialRoute : pawApp(node.appId).route;
}

/** Close is a UI composition action. Align only a removed reload-route owner;
 * never dispatch a route, start work, or change desktop persistence semantics. */
export function closePawWindows(api: PawDesktopStore, close: () => void): void {
  const route = window.location.hash.replace(/^#/, '') || '/project-field';
  const owners = Object.values(api.getState().windows).filter(node => ownsRoute(node, route));
  close();
  const state = api.getState();
  if (!owners.length || owners.some(node => state.windows[node.id])) return;
  const nextRoute = reloadRoute(state.activeWindowId ? state.windows[state.activeWindowId] : undefined);
  window.history.replaceState(window.history.state, '', `${window.location.search}#${nextRoute}`);
}
