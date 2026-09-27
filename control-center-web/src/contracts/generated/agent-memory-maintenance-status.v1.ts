/* eslint-disable */
/**
 * This file is generated. Do not edit it by hand.
 * Source: rag_ime/contracts/json/agent-memory-maintenance-status.v1.json
 */

export interface AgentMemoryMaintenanceStatusV1 {
  schemaVersion: 'rag-ime.agent-memory-maintenance-status.v1';
  ok: true;
  policy: 'review' | 'auto_governed' | 'disabled';
  autoApply: boolean;
  scheduledDraftOnly: boolean;
  due: boolean;
  dueReason:
    | 'pending_events'
    | 'idle'
    | 'daily'
    | 'owner_daily'
    | 'owner_scheduled'
    | 'draft_pending_review'
    | 'automatic_organization_disabled'
    | 'not_due';
  idleMs: number;
  compileState: {
    project: string;
    lastCompiledEventId: number;
    lastRunMs: number;
    pendingEventCount: number;
    lastBundleHash: string;
    [k: string]: unknown;
  };
  draftCoverage: {
    coveredThroughEventId: number;
    undraftedEventCount: number;
    coversAllPending: boolean;
    lastDraftRunId: string;
    [k: string]: unknown;
  };
  automation: {
    minimumNewEvents: number;
    idleThresholdMs: number;
    dailyIntervalMs: number;
    schedulerPollIntervalMs: number;
    enabled: boolean;
    model: string;
    thinkingLevel: string;
    runsPerDay: number;
    autoApply: boolean;
    curationProtocol: 'atom-first-v1';
    targetSourceCount: number;
    maximumSourceCount: number;
    maximumInputTokens: number;
    reservedContextTokens: number;
    [k: string]: unknown;
  };
  catalogConsolidation: {
    schemaVersion: 'rag-ime.memory-catalog-consolidation.v1';
    project: string;
    enabled: boolean;
    automaticOrganizationEnabled: boolean;
    cadenceDays: number;
    receiptId: string;
    state: 'never' | 'running' | 'completed' | 'failed';
    lastAttemptAtMs: number;
    lastCompletionAtMs: number;
    nextDueAtMs: number;
    catalogDigest: string;
    lastSuccessfulCatalogDigest: string;
    lastSuccessfulCatalogCommittedAtMs: number;
    attemptCatalogDigest: string;
    curationRunId: string;
    attemptCount: number;
    retryCount: number;
    result: {
      [k: string]: unknown;
    };
    error: string;
    createdAtMs: number;
    updatedAtMs: number;
    leaseExpiresAtMs: number;
    due: boolean;
    dueReason:
      | 'first_run'
      | 'scheduled'
      | 'retry_backoff'
      | 'running'
      | 'lease_expired'
      | 'catalog_consolidation_disabled'
      | 'automatic_organization_disabled'
      | 'not_due';
    observedAtMs: number;
    [k: string]: unknown;
  };
  pendingDraftCount?: number;
  ownerCuration: {
    schemaVersion: 'rag-ime.owner-memory-curation-status.v1';
    ok: true;
    project: string;
    policy: {
      [k: string]: unknown;
    };
    due: boolean;
    pendingSourceCount: number;
    needsReviewSourceCount: number;
    scopes: {
      [k: string]: unknown;
    }[];
    [k: string]: unknown;
  };
  modelCuration: {
    schemaVersion: 'rag-ime.memory-curation-model-status.v1';
    ok: true;
    profile: 'MEMORY_CURATION';
    requiredModel: string;
    requiredThinkingLevel: 'max';
    minimumContextTokens: number;
    stateCounts: {
      [k: string]: number;
    };
    runs: {
      [k: string]: unknown;
    }[];
    [k: string]: unknown;
  };
  bookProjection: {
    schemaVersion: 'rag-ime.personal-memory-book-projection-status.v1';
    ok: boolean;
    projectionOwner: string;
    currentAtomCount: number;
    desiredBookCount: number;
    activeBookCount: number;
    historicalBookCount: number;
    unbookedAtomCount: number;
    missingBookCount: number;
    staleBookCount: number;
    membershipMismatchCount: number;
    guardedBookCount: number;
    inSync: boolean;
    [k: string]: unknown;
  };
  projection: {
    schemaVersion: 'rag-ime.memory-projection-runtime.v1';
    ok: boolean;
    configured: boolean;
    owner: string;
    running: boolean;
    lastRunAtMs: number;
    lastError: string;
    freshness: {
      [k: string]: unknown;
    };
    disabledReason: string;
    [k: string]: unknown;
  };
  runs: {
    runId: string;
    createdAtMs: number;
    status: 'draft' | 'applied' | 'partial' | 'rolled_back' | 'superseded' | 'dismissed' | 'empty';
    summary: string;
    diffCount: number;
    bundleHash: string;
    sourceCursor: {
      [k: string]: unknown;
    };
    ownerKind: 'user' | 'shared' | 'agent' | 'session' | 'room';
    ownerId: string;
    runKind:
      'legacy' | 'daily_curation' | 'manual_curation' | 'dream_insight' | 'catalog_consolidation';
    [k: string]: unknown;
  }[];
  [k: string]: unknown;
}
