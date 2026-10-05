import type { SessionSummary } from '@/features/agent/types';
import { emptyPetCounts, type PetConversation, type PetConversationState, type PetStateValue } from '@/features/agent/desktop-pet-snapshot';
import { projectStellarAgents } from './stellar-agent-projection';

const priority: Record<PetConversationState, number> = { attention: 0, running: 1, paused: 2, idle: 3, unknown: 4, terminal: 5 };

/** One small read-only directory view; neither histories nor paths cross IPC. */
export function projectPetDirectory(sessions: readonly SessionSummary[], fresh: boolean, loaded: boolean): PetStateValue {
  const eligible = sessions.filter(session => session.status !== 'archived' && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/.test(session.id)).slice(0, 100);
  const records = new Map(eligible.map(session => [session.id, session]));
  const stellar = projectStellarAgents({ nowMs: 0, sessions: eligible, rooms: [], sessionStatusFresh: fresh, roomStatusFresh: false });
  const counts = emptyPetCounts();
  const conversations = stellar.planets.map((planet): PetConversation => {
    const record = records.get(planet.sessionId);
    const state: PetConversationState = !fresh ? 'unknown'
      : planet.status === 'idle' && (record?.goal?.status === 'paused' || planet.sourceStatus === 'paused') ? 'paused' : planet.status;
    counts[state] += 1;
    return { id: planet.sessionId, state, label: [...planet.title.replace(/[\x00-\x1f\x7f]/g, ' ').trim()].slice(0, 48).join('') || '未命名对话' };
  });
  // Priority decides inclusion, stable ids decide positions. Progress does not
  // reshuffle the same admitted list or change a conversation's identity.
  const visible = conversations.sort((a, b) => priority[a.state] - priority[b.state] || a.id.localeCompare(b.id))
    .slice(0, 8).sort((a, b) => a.id.localeCompare(b.id));
  return { freshness: fresh ? 'synced' : loaded ? 'recovering' : 'unavailable', counts, conversations: visible };
}
