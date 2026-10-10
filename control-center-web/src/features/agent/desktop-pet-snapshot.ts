import type { PlanetMotionMode, PlanetSignalState } from '@/features/rooms/sphere-avatar/sphere-avatar-protocol';

export type PetConversationState = 'running' | 'attention' | 'error' | 'paused' | 'idle' | 'terminal' | 'unknown';
export type PetTaskCounts = Record<PetConversationState, number>;
export type PetConversation = { id: string; label: string; state: PetConversationState };
export type PetDirectoryFact = {
  id: string; activeTurnId?: string;
  waiting?: { turnId: string; requestId: string; kind: 'input' | 'approval' | 'review' }[];
  terminal?: { eventId: string; turnId: string; sequence: number; outcome: 'completed' | 'aborted' | 'failed' };
};
export type PetNativeVisual = {
  signal: Exclude<PlanetSignalState, 'offline'>; motion: PlanetMotionMode; label: string; arrivalKey: string | null;
};
export type PetStateValue = { facts?: PetDirectoryFact[]; freshness: 'synced' | 'recovering' | 'unavailable'; counts: PetTaskCounts; conversations: PetConversation[] };
export type PetSourceIdentity = { schemaVersion: 1; sourceId: 'work-directory'; scopeId: string };
export type PetSnapshot = PetStateValue & {
  visual?: PetNativeVisual; schemaVersion: 1; producerEpoch: number; revision: number; sourceId: 'work-directory' | null; scopeId: string | null;
};
/** Keep an entire content-free fact or omit it; never shorten an identity. */
export function normalizePetFact(value: unknown): PetDirectoryFact | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return;
  const row = value as Record<string, unknown>;
  const opaque = (id: unknown, max: number) => typeof id === 'string' && id.length <= max && /^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(id);
  const exact = (row: Record<string, unknown>, keys: string[]) => Object.keys(row).length === keys.length && keys.every(key => Object.hasOwn(row, key));
  if (!opaque(row.id, 200) || Object.keys(row).some(key => !['id', 'activeTurnId', 'waiting', 'terminal'].includes(key))) return;
  const result: PetDirectoryFact = { id: row.id as string };
  if (Object.hasOwn(row, 'activeTurnId')) {
    if (row.activeTurnId !== '' && !opaque(row.activeTurnId, 240)) return;
    result.activeTurnId = row.activeTurnId as string;
  }
  if (Object.hasOwn(row, 'waiting')) {
    if (!Object.hasOwn(row, 'activeTurnId') || !Array.isArray(row.waiting) || row.waiting.length > 8) return;
    const waiting: NonNullable<PetDirectoryFact['waiting']> = [];
    for (const request of row.waiting) {
      if (!request || typeof request !== 'object' || Array.isArray(request)
        || !exact(request, ['turnId', 'requestId', 'kind']) || !opaque(request.turnId, 240)
        || request.turnId !== row.activeTurnId || !opaque(request.requestId, 240)
        || !['input', 'approval', 'review'].includes(request.kind)
        || waiting.some(item => item.requestId === request.requestId)) return;
      waiting.push({ turnId: request.turnId, requestId: request.requestId, kind: request.kind });
    }
    result.waiting = waiting;
  }
  if (Object.hasOwn(row, 'terminal')) {
    const terminal = row.terminal as Record<string, unknown> | undefined;
    if (!terminal || typeof terminal !== 'object' || Array.isArray(terminal)
      || !exact(terminal, ['eventId', 'turnId', 'sequence', 'outcome'])
      || !opaque(terminal.eventId, 512) || !opaque(terminal.turnId, 240)
      || !Number.isSafeInteger(terminal.sequence) || (terminal.sequence as number) < 1
      || !['completed', 'aborted', 'failed'].includes(terminal.outcome as string)) return;
    result.terminal = { eventId: terminal.eventId as string, turnId: terminal.turnId as string,
      sequence: terminal.sequence as number, outcome: terminal.outcome as 'completed' | 'aborted' | 'failed' };
  }
  return Object.keys(result).length > 1 ? result : undefined;
}

export type PetConversationTarget = Pick<PetSnapshot, 'producerEpoch' | 'sourceId' | 'scopeId'> & { id: string };
export const emptyPetCounts = (): PetTaskCounts => ({ running: 0, attention: 0, error: 0, paused: 0, idle: 0, terminal: 0, unknown: 0 });
export const unavailablePetSnapshot = (): PetSnapshot => ({ schemaVersion: 1, producerEpoch: 0, revision: 0,
  sourceId: null, scopeId: null, freshness: 'unavailable', counts: emptyPetCounts(), conversations: [] });

export function acceptPetSnapshot(previous: PetSnapshot, next: PetSnapshot): PetSnapshot {
  if (next.schemaVersion !== 1 || next.producerEpoch < previous.producerEpoch
    || (next.producerEpoch === previous.producerEpoch && (next.sourceId !== previous.sourceId || next.scopeId !== previous.scopeId))
    || (next.producerEpoch === previous.producerEpoch && next.revision <= previous.revision)) return previous;
  return next;
}

/** Signals describe retained directory facts, independently of the planet's face. */
export function petPresentation(snapshot: PetSnapshot): { state: PetConversationState; label: string } {
  if (snapshot.freshness === 'unavailable') return { state: 'unknown', label: '状态未同步' };
  if (snapshot.freshness === 'recovering') return { state: 'unknown', label: '正在重新同步' };
  const running = snapshot.counts.running ? ` · ${snapshot.counts.running} 个进行中` : '';
  // Legacy attention counts do not prove a pending input or approval.
  // Typed facts are used only by the capable native visual projection.
  if (snapshot.counts.attention) return { state: 'attention', label: `${snapshot.counts.attention} 个对话待查看${running}` };
  if (snapshot.counts.error) return { state: 'error', label: `${snapshot.counts.error} 个对话出错${running}` };
  if (snapshot.counts.running) return { state: 'running', label: `${snapshot.counts.running} 个对话进行中` };
  if (snapshot.counts.paused) return { state: 'paused', label: `${snapshot.counts.paused} 个对话已暂停` };
  if (snapshot.counts.unknown) return { state: 'unknown', label: '部分状态未同步' };
  // Historical completions are retained facts, never a new completion event.
  return { state: 'idle', label: '当前没有运行中的对话' };
}

export const petConversationLabel: Record<PetConversationState, string> = {
  running: '进行中', attention: '待查看', error: '出错', paused: '已暂停', idle: '空闲', terminal: '已结束', unknown: '未同步',
};

/** Directory diagnostics do not prove waiting, successful completion or offline. */
export function petVisualSignal(snapshot: PetSnapshot): { signal: PlanetSignalState; motion: PlanetMotionMode } {
  if (snapshot.visual && snapshot.freshness === 'synced') return { signal: snapshot.visual.signal, motion: snapshot.visual.motion };
  const { state } = petPresentation(snapshot);
  if (state === 'running') return { signal: 'working', motion: 'full' };
  if (state === 'error') return { signal: 'error', motion: 'full' };
  const historicalOnly = snapshot.counts.terminal > 0 && snapshot.counts.idle === 0;
  return { signal: 'idle', motion: state === 'idle' && !historicalOnly ? 'full' : 'static' };
}

export type PetVisualSnapshot = { snapshot: PetSnapshot; arrivalKey: string | null };
/** Accepted, continuous local transitions only; loading/recovery is a static seed. */
export function acceptPetVisualSnapshot(previous: PetVisualSnapshot, received: PetSnapshot): PetVisualSnapshot {
  const snapshot = acceptPetSnapshot(previous.snapshot, received);
  if (snapshot === previous.snapshot) return previous;
  const before = petPresentation(previous.snapshot).state;
  const after = petPresentation(snapshot).state;
  const continuous = previous.snapshot.freshness === 'synced' && snapshot.freshness === 'synced'
    && snapshot.producerEpoch === previous.snapshot.producerEpoch
    && snapshot.sourceId === previous.snapshot.sourceId && snapshot.scopeId === previous.snapshot.scopeId;
  const newError = continuous && before !== 'unknown' && before !== 'error' && after === 'error';
  const arrivalKey = snapshot.visual ? continuous ? snapshot.visual.arrivalKey : null : newError ? `${snapshot.producerEpoch}:${snapshot.scopeId}:${snapshot.revision}`
    : continuous && before === 'error' && after === 'error' ? previous.arrivalKey : null;
  return { snapshot, arrivalKey };
}

export interface PetStatePublisherBridge {
  begin(identity: PetSourceIdentity): Promise<{ producerEpoch: number; factsVersion?: 1 }>;
  publish(value: PetStateValue & { schemaVersion: 1; producerEpoch: number; revision: number }): Promise<boolean>;
  release(value: { producerEpoch: number }): Promise<boolean>;
}
declare global { interface Window { pawDesktopPetState?: PetStatePublisherBridge } }
