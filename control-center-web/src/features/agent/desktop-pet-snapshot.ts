export type PetConversationState = 'running' | 'attention' | 'error' | 'paused' | 'idle' | 'terminal' | 'unknown';
export type PetTaskCounts = Record<PetConversationState, number>;
export type PetConversation = { id: string; label: string; state: PetConversationState };
export type PetStateValue = { freshness: 'synced' | 'recovering' | 'unavailable'; counts: PetTaskCounts; conversations: PetConversation[] };
export type PetSourceIdentity = { schemaVersion: 1; sourceId: 'work-directory'; scopeId: string };
export type PetSnapshot = PetStateValue & {
  schemaVersion: 1; producerEpoch: number; revision: number; sourceId: 'work-directory' | null; scopeId: string | null;
};
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
  // Attention is deliberately generic: the directory does not expose pending
  // approval/input facts. Never turn it into an error or a request for approval.
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

export interface PetStatePublisherBridge {
  begin(identity: PetSourceIdentity): Promise<{ producerEpoch: number }>;
  publish(value: PetStateValue & { schemaVersion: 1; producerEpoch: number; revision: number }): Promise<boolean>;
  release(value: { producerEpoch: number }): Promise<boolean>;
}
declare global { interface Window { pawDesktopPetState?: PetStatePublisherBridge } }
