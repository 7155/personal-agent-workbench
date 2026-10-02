import type { PawOsWindowTarget } from '@/features/paw-os/model/desktop';

export type AgentWorkspaceKind = 'room' | 'session';

// Both intent warmup and React.lazy use these same module promises. A failed
// prefetch must not poison a later first render; actual render errors retain
// the App error boundary's existing reload recovery.
let sessionLoad: Promise<{ default: typeof import('./PawSessionWorkspace').PawSessionWorkspace }> | undefined;
let roomLoad: Promise<{ default: typeof import('./PawRoomWorkspace').PawRoomWorkspace }> | undefined;

export function loadSessionWorkspace() {
  const current = sessionLoad ??= import('./PawSessionWorkspace').then(module => ({ default: module.PawSessionWorkspace }));
  void current.catch(() => { if (sessionLoad === current) sessionLoad = undefined; });
  return current;
}

export function loadRoomWorkspace() {
  const current = roomLoad ??= import('./PawRoomWorkspace').then(module => ({ default: module.PawRoomWorkspace }));
  void current.catch(() => { if (roomLoad === current) roomLoad = undefined; });
  return current;
}

export function warmAgentWorkspace(kind: AgentWorkspaceKind): void {
  void (kind === 'room' ? loadRoomWorkspace() : loadSessionWorkspace()).catch(() => undefined);
}

/** Use the same route selection as Agent; satellite targets stay outside it. */
export function agentWorkspaceIntent({ appId, entityId, initialRoute = '', target }: {
  appId: string; entityId?: string; initialRoute?: string; target?: PawOsWindowTarget;
}): AgentWorkspaceKind | undefined {
  if (appId !== 'agent') return;
  if (target && (target.kind !== 'session' && target.kind !== 'room' || target.kind === 'room' && Boolean(target.panel))) return;
  const selection = initialAgentSelection(initialRoute, target?.kind ?? (entityId ? 'session' : undefined), target?.id ?? entityId);
  return selection.kind === 'new' ? undefined : selection.kind;
}

export type AgentSelection =
  | { kind: 'new'; draft?: string }
  | { kind: 'session'; id: string; draft?: string }
  | { kind: 'room'; id: string; draft?: string; error?: string };

export function initialAgentSelection(
  initialRoute: string,
  targetKind?: PawOsWindowTarget['kind'],
  targetId?: string,
  targetRoomId?: string,
): AgentSelection {
  const query = new URLSearchParams(initialRoute.split('?', 2)[1] ?? '');
  const routeDraft = query.get('draft');
  const draft = routeDraft?.trim() ? routeDraft : undefined;
  const draftSelection = draft === undefined ? {} : { draft };
  if (targetKind === 'session' && targetId) return { kind: 'session', id: targetId };
  if (targetKind === 'room' && targetId) return { kind: 'room', id: targetId, ...draftSelection };
  if (targetKind === 'participant' && targetRoomId) return { kind: 'room', id: targetRoomId, ...draftSelection };
  if (initialRoute.startsWith('/rooms')) {
    const roomId = query.get('room');
    return roomId ? { kind: 'room', id: roomId, ...draftSelection } : { kind: 'new' };
  }
  const roomId = query.get('room');
  if (roomId) return { kind: 'room', id: roomId, ...draftSelection };
  const sessionId = query.get('session') || query.get('sessionId');
  if (draft) return { kind: 'new', draft };
  return sessionId ? { kind: 'session', id: sessionId } : { kind: 'new' };
}
