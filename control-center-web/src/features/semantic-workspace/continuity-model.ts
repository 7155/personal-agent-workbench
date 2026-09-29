export interface ContextSource { kind: string; id: string; revision: string; label?: string }
export interface ContinuityDecision { id: string; text: string; status: 'current' | 'needs_review' | 'superseded'; source: unknown }
export interface SpaceFacts {
  key: string; title: string; revision: string; observedAtMs: number; running: boolean | null;
  goal: { configured: boolean; objective: string; status: string; successCriteria: string } | null;
  lastReply?: { text: string; source: ContextSource } | null; constraints?: string[];
  candidates: { id: string; text: string; workItemId: string; source: ContextSource }[];
  requests: { text: string; source: ContextSource; createdAtMs?: number }[];
  decisions: ContinuityDecision[]; blockers: string[]; sources: ContextSource[]; missing: string[];
  pendingDecisions: { id: string; text: string; source: ContextSource }[];
  deliveries: { id: string; title: string; data: Record<string, unknown>; source: ContextSource;
    availability?: 'available' | 'changed' | 'unavailable' | 'unknown'; fileRevision?: string; generated: boolean; verified: string; adopted: string; review?: Record<string, unknown> }[];
  organization: { pinned: boolean; placement: 'desk' | 'shelf' };
  executionAllowed: boolean; contextPack: Record<string, unknown>;
}
export interface ResumeProposal { origin?: 'jev' | 'user'; id: string; spaceKey: string; text: string; revision: string; expiresAtMs: number; model: string }
export interface ResumeIntent { spaceKey: string; proposalId: string; commandId: string }
