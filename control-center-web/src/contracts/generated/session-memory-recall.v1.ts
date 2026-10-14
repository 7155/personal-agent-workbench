/* eslint-disable */
/**
 * This file is generated. Do not edit it by hand.
 * Source: rag_ime/contracts/json/session-memory-recall.v1.json
 */

export interface SessionMemoryRecallV1 {
  schemaVersion: 'rag-ime.session-memory-recall.v1';
  recallId: string;
  sessionId: string;
  project: string;
  roleId: string;
  generatedAtMs: number;
  trigger: 'first_user_prompt' | 'turn_start' | 'compaction' | 'room_task' | 'subagent_task';
  query: {
    preview: string;
    sha256: string;
    recentCompleteInputCount: number;
    recentCompleteInputUsedForRetrieval: boolean;
    retrievalContextUsed: boolean;
    recentConversationCount: number;
    timelineIntent: TimelineIntent;
  };
  retrieval: {
    strategy: 'hybrid_book_atom' | 'vcp_hybrid_book_atom';
    primaryQuery: string;
    matchedAliases: string[];
    activatedTags: string[];
    visibleOwners: {
      ownerKind: string;
      ownerId: string;
    }[];
    requestedEmbeddingProvider: string;
    embeddingProvider: string;
    embeddingFallback: boolean;
    temporalIntent: boolean;
    timelineIntent: TimelineIntent;
    activityTimelineIncluded: boolean;
    vectorFusion: {
      applied: boolean;
      queryWeight: number;
      contextWeight: number;
    };
  };
  /**
   * @maxItems 12
   */
  items:
    | []
    | [
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
      ]
    | [
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
      ]
    | [
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
      ]
    | [
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
      ]
    | [
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
      ]
    | [
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
      ]
    | [
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
      ]
    | [
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
      ]
    | [
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
      ]
    | [
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
      ]
    | [
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
      ]
    | [
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
        {
          rank: number;
          sourceType: 'memory_book' | 'memory_atom' | 'memory_timeline';
          sourceId: string;
          title: string;
          text: string;
          score: number;
          confidence: number;
          lanes: string[];
          rawScores: {
            [k: string]: number;
          };
          tags: string[];
          ownerKind: string;
          ownerId: string;
          evidenceEventIds: number[];
          project?: string;
          claimState?: string;
          validFromMs?: number;
          validToMs?: number;
          supersedesId?: string;
          sourceUpdatedAtMs?: number;
          sourceStartMs?: number;
          sourceEndMs?: number;
          corroborationOnly?: boolean;
          maySupportFacts?: boolean;
        },
      ];
  /**
   * @maxItems 8
   */
  recentConversation?:
    | []
    | [
        {
          role: 'user' | 'assistant';
          text: string;
        },
      ]
    | [
        {
          role: 'user' | 'assistant';
          text: string;
        },
        {
          role: 'user' | 'assistant';
          text: string;
        },
      ]
    | [
        {
          role: 'user' | 'assistant';
          text: string;
        },
        {
          role: 'user' | 'assistant';
          text: string;
        },
        {
          role: 'user' | 'assistant';
          text: string;
        },
      ]
    | [
        {
          role: 'user' | 'assistant';
          text: string;
        },
        {
          role: 'user' | 'assistant';
          text: string;
        },
        {
          role: 'user' | 'assistant';
          text: string;
        },
        {
          role: 'user' | 'assistant';
          text: string;
        },
      ]
    | [
        {
          role: 'user' | 'assistant';
          text: string;
        },
        {
          role: 'user' | 'assistant';
          text: string;
        },
        {
          role: 'user' | 'assistant';
          text: string;
        },
        {
          role: 'user' | 'assistant';
          text: string;
        },
        {
          role: 'user' | 'assistant';
          text: string;
        },
      ]
    | [
        {
          role: 'user' | 'assistant';
          text: string;
        },
        {
          role: 'user' | 'assistant';
          text: string;
        },
        {
          role: 'user' | 'assistant';
          text: string;
        },
        {
          role: 'user' | 'assistant';
          text: string;
        },
        {
          role: 'user' | 'assistant';
          text: string;
        },
        {
          role: 'user' | 'assistant';
          text: string;
        },
      ]
    | [
        {
          role: 'user' | 'assistant';
          text: string;
        },
        {
          role: 'user' | 'assistant';
          text: string;
        },
        {
          role: 'user' | 'assistant';
          text: string;
        },
        {
          role: 'user' | 'assistant';
          text: string;
        },
        {
          role: 'user' | 'assistant';
          text: string;
        },
        {
          role: 'user' | 'assistant';
          text: string;
        },
        {
          role: 'user' | 'assistant';
          text: string;
        },
      ]
    | [
        {
          role: 'user' | 'assistant';
          text: string;
        },
        {
          role: 'user' | 'assistant';
          text: string;
        },
        {
          role: 'user' | 'assistant';
          text: string;
        },
        {
          role: 'user' | 'assistant';
          text: string;
        },
        {
          role: 'user' | 'assistant';
          text: string;
        },
        {
          role: 'user' | 'assistant';
          text: string;
        },
        {
          role: 'user' | 'assistant';
          text: string;
        },
        {
          role: 'user' | 'assistant';
          text: string;
        },
      ];
  /**
   * @maxItems 8
   */
  todo?:
    | []
    | [
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
      ]
    | [
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
      ]
    | [
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
      ]
    | [
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
      ]
    | [
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
      ]
    | [
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
      ]
    | [
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
      ]
    | [
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
        {
          status: 'pending' | 'in_progress' | 'blocked';
          content: string;
        },
      ];
  task?: {
    kind?: string;
    objective?: string;
    expectedOutput?: string;
    state?: string;
    /**
     * @maxItems 8
     */
    acceptanceCriteria?:
      | []
      | [string]
      | [string, string]
      | [string, string, string]
      | [string, string, string, string]
      | [string, string, string, string, string]
      | [string, string, string, string, string, string]
      | [string, string, string, string, string, string, string]
      | [string, string, string, string, string, string, string, string];
    /**
     * @maxItems 4
     */
    originalRequirements?:
      | []
      | [string]
      | [string, string]
      | [string, string, string]
      | [string, string, string, string];
  };
  compactionRecovery?: {
    schemaVersion: 'rag-ime.agent-compaction-recovery.v2';
    summaryPresent: boolean;
    summarySha256: string;
    summaryChars: number;
    /**
     * @maxItems 32
     */
    skills: {
      name: string;
      contentRevision: string;
    }[];
    /**
     * @maxItems 32
     */
    tools: {
      name: string;
      schemaRevision: string;
    }[];
  };
  coverage?: {
    status: string;
    processedThroughAtMs: number;
    lastRunAtMs: number;
  };
  sourceIds: string[];
  budget: {
    maxItems: number;
    maxChars: number;
    usedChars: number;
    omittedCount: number;
  };
  policy: {
    priority: 'developer';
    lifecycle: 'session';
    evidenceOnly: true;
    currentUserMessageWins: true;
    rawRecentInputInjected: false;
    recentConversationInjected: boolean;
    detailLevel: 'compact' | 'balanced' | 'detailed';
  };
}
export interface TimelineIntent {
  requested: boolean;
  reason: 'none' | 'disabled' | 'exact_date' | 'explicit_timeline' | 'relative_time';
  /**
   * @maxItems 8
   */
  matched:
    | []
    | [string]
    | [string, string]
    | [string, string, string]
    | [string, string, string, string]
    | [string, string, string, string, string]
    | [string, string, string, string, string, string]
    | [string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string];
  range: string;
}
