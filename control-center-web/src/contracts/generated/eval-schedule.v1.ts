/* eslint-disable */
/**
 * This file is generated. Do not edit it by hand.
 * Source: rag_ime/contracts/json/eval-schedule.v1.json
 */

export interface EvalScheduleV1 {
  id: string;
  suiteId: string;
  suiteRevision: string;
  recurrenceKind: 'daily' | 'weekly';
  recurrenceInterval: number;
  maxRuns: number;
  runCount: number;
  status: 'scheduled' | 'running' | 'completed' | 'failed' | 'paused' | 'cancelled';
  initialDueAtMs: number;
  nextDueAtMs: number;
  lastErrorCode: string;
  createdAtMs: number;
  updatedAtMs: number;
  latestRun: LatestRun;
}
export interface LatestRun {
  id?: string;
  scheduleId?: string;
  attempt?: number;
  state?: 'claimed' | 'succeeded' | 'failed';
  dueAtMs?: number;
  claimedAtMs?: number;
  finishedAtMs?: number;
  evalRunId?: string;
  errorCode?: string;
}
