/* eslint-disable */
/**
 * This file is generated. Do not edit it by hand.
 * Source: rag_ime/contracts/json/eval-lab-run-list.v1.json
 */

export type Sha256 = string;

export interface EvalLabRunListV1 {
  schemaVersion: 'rag-ime.eval-lab-run-list.v1';
  ok: true;
  /**
   * @maxItems 500
   */
  items: Run[];
  total: number;
  /**
   * @maxItems 500
   */
  experiments: {
    schemaVersion: 'rag-ime.agent-lab-experiment.v1';
    experimentId: string;
    revisionSha256: Sha256;
    [k: string]: unknown;
  }[];
  experimentTotal: number;
  /**
   * Display bounds after complete evaluation-only pagination and run grouping. total remains the full run count; aggregate run statistics include all eligible tasks.
   */
  truncation?: {
    runLimit: 500;
    taskLimit: 500;
    omittedRunCount: number;
    /**
     * Task rows omitted only within returned runs; does not count tasks in omitted runs.
     */
    omittedTaskCount: number;
  };
  /**
   * @maxItems 32
   */
  pathSearches?: PathSearch[];
  pathSearchTotal?: number;
}
export interface Run {
  schemaVersion: 'rag-ime.eval-lab-run.v1';
  runId: string;
  title: string;
  suiteId: string;
  split: string;
  workflowProfile: string;
  status: 'completed';
  taskCount: number;
  taskSuccessCount: number;
  taskSuccessRate: number;
  verifierPassCount: number;
  verifierCount: number;
  verifierPassRate: number;
  toolCalls: number;
  failedToolCalls: number;
  latencyMs: number;
  sourceDatabaseSha256: Sha256;
  sourceReportSha256: Sha256;
  createdAtMs: number;
  updatedAtMs: number;
  /**
   * @minItems 1
   * @maxItems 500
   */
  tasks: [Task, ...Task[]];
}
export interface Task {
  sessionId: string;
  title: string;
  taskAlias: string;
  taskIndex: number;
  taskSucceeded: boolean;
  terminalEvent: string;
  verifierPassed: number;
  verifierTotal: number;
  toolCalls: number;
  failedToolCalls: number;
  latencyMs: number;
  explanation?: Explanation;
}
export interface Explanation {
  caseId: string;
  businessRequest: NormalizedText;
  agentOutcome: NormalizedSummary;
  acceptance: Acceptance;
}
export interface NormalizedText {
  normalizedText: string;
}
export interface NormalizedSummary {
  normalizedSummary: string;
}
export interface Acceptance {
  passed: number;
  total: number;
  /**
   * @maxItems 64
   */
  items: AcceptanceItem[];
}
export interface AcceptanceItem {
  id: string;
  label: string;
  status: 'pass' | 'fail' | 'partial' | 'unknown';
  failureOwner: null | 'prompt_context' | 'evaluator_gold' | 'agent' | 'unknown';
  explanation: string;
}
export interface PathSearch {
  schemaVersion: 'rag-ime.agent-lab-path-search.v1';
  searchId: string;
  title: string;
  objectiveSummary: string;
  metricSummary: string;
  frozenControlCount: number;
  selectedNodeId: string;
  /**
   * @minItems 1
   * @maxItems 64
   */
  selectedPath: [
    {
      nodeId: string;
      decision: string;
      reason: string;
    },
    ...{
      nodeId: string;
      decision: string;
      reason: string;
    }[],
  ];
  claimStatus: 'best_known' | 'blocked' | 'insufficient_evidence';
  claimSummary: string;
  /**
   * @maxItems 128
   */
  candidates: {
    nodeId: string;
    changedFactor: string;
    status: 'eligible' | 'rejected' | 'not_evaluated' | 'unknown';
    metrics: {
      [k: string]: number;
    };
    reason: string;
  }[];
  generatedAtMs: number;
}
