import type { SessionSummary } from '@/features/agent/types';
import { emptyPetCounts, normalizePetFact, type PetConversation, type PetConversationState, type PetStateValue } from '@/features/agent/desktop-pet-snapshot';
import { projectStellarAgents } from './stellar-agent-projection';

const priority: Record<PetConversationState, number> = { attention: 0, error: 1, running: 2, paused: 3, idle: 4, unknown: 5, terminal: 6 };

/** One small read-only directory view; neither histories nor paths cross IPC. */
export function projectPetDirectory(sessions: readonly SessionSummary[], fresh: boolean, loaded: boolean): PetStateValue {
  const eligible = sessions.filter(session => session.status !== 'archived' && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/.test(session.id)).slice(0, 100);
  const records = new Map(eligible.map(session => [session.id, session]));
  const stellar = projectStellarAgents({ nowMs: 0, sessions: eligible, rooms: [], sessionStatusFresh: fresh, roomStatusFresh: false });
  const counts = emptyPetCounts();
  const conversations = stellar.planets.map((planet): PetConversation => {
    const record = records.get(planet.sessionId);
    // Only a real faulted directory record proves failure. Generic attention
    // does not prove approval/input, and a busy Session stays running even if
    // its goal was paused or a local stop request has not yet settled.
    const state: PetConversationState = !fresh ? 'unknown'
      : planet.sourceStatus === 'faulted' ? 'error'
      : planet.status === 'idle' && (record?.goal?.status === 'paused' || planet.sourceStatus === 'paused') ? 'paused' : planet.status;
    counts[state] += 1;
    return { id: planet.sessionId, state, label: [...planet.title.replace(/[\x00-\x1f\x7f]/g, ' ').trim()].slice(0, 48).join('') || '未命名对话' };
  });
  // Priority decides inclusion, stable ids decide positions. Progress does not
  // reshuffle the same admitted list or change a conversation's identity.
  const visible = conversations.sort((a, b) => priority[a.state] - priority[b.state] || a.id.localeCompare(b.id))
    .slice(0, 8).sort((a, b) => a.id.localeCompare(b.id));
  const facts = fresh ? visible.flatMap(item => {
    const source = records.get(item.id)?.presentationFacts;
    if (!source || Object.hasOwn(source, 'id')) return [];
    const fact = normalizePetFact({ id: item.id, ...source });
    return fact ? [fact] : [];
  }) : [];
  return { ...(facts.length ? { facts } : {}), freshness: fresh ? 'synced' : loaded ? 'recovering' : 'unavailable', counts, conversations: visible };
}
