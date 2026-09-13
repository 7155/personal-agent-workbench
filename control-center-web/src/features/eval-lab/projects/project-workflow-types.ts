import { isApplicationMethodComparison, type ApplicationMethodComparison } from '../golden/application-method';
/** Read-only projection of durable owners. The canvas never advances a node. */
export type LabWorkflowStatus = 'pending' | 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted' | 'unavailable';
export type LabWorkflowKind = 'materials' | 'corpus' | 'index' | 'dataset' | 'calibration' | 'experiment' | 'artifact' | 'application' | 'job' | 'step';
export type LabWorkflowMetric = { label: string; baseline: number | null; candidate: number | null; unit?: string; value?: number | null; sampleCount?: number };
export type LabWorkflowNode = {
  id: string; kind: LabWorkflowKind; title: string; status: LabWorkflowStatus; summary: string;
  dependencies: string[]; parentId?: string; ref: { kind: string; id: string; version?: string | number };
  decision?: string; metrics?: LabWorkflowMetric[]; children?: string[]; updatedAtMs?: number; source: 'runtime' | 'artifact';
  factors?: { name: string; before: string; after: string; reason: string }[];
  reasons?: string[]; evidenceRefs?: { kind: string; id: string; version?: number }[];
  optimization?: { scope: string; baselineModel?: string; candidateModel?: string; promptChanged?: boolean; selectedCandidateIndex?: number };
  applicationMethodComparison?: ApplicationMethodComparison;
};
export type LabProjectWorkflow = {
  schemaVersion: 'paw.lab-project-workflow.v1'; observedAtMs: number; nodes: LabWorkflowNode[];
  edges: { source: string; target: string }[]; counts: { running: number; queued: number; completed: number; failed: number };
  currentNodeId: string | null;
  complete?: boolean; unavailableOwners?: string[];
};
const record = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const text = (value: unknown): value is string => typeof value === 'string';
const strings = (value: unknown) => Array.isArray(value) && value.every(text);
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const natural = (value: unknown) => finite(value) && Number.isSafeInteger(value) && value >= 0;
export function isLabProjectWorkflow(value: unknown): value is LabProjectWorkflow {
  const item = record(value); const counts = record(item.counts);
  if (item.schemaVersion !== 'paw.lab-project-workflow.v1' || !natural(item.observedAtMs)
    || !Array.isArray(item.nodes) || !Array.isArray(item.edges)
    || !['running', 'queued', 'completed', 'failed'].every((key) => natural(counts[key]))
    || !(item.currentNodeId === null || text(item.currentNodeId))
    || !(item.complete === undefined || typeof item.complete === 'boolean')
    || !(item.unavailableOwners === undefined || strings(item.unavailableOwners))) return false;
  if (!item.nodes.every((raw) => {
    const node = record(raw); const ref = record(node.ref);
    return text(node.id) && !!node.id && text(node.title) && text(node.summary)
      && ['materials', 'corpus', 'index', 'dataset', 'calibration', 'experiment', 'artifact', 'application', 'job', 'step'].includes(String(node.kind))
      && ['pending', 'queued', 'running', 'completed', 'failed', 'cancelled', 'interrupted', 'unavailable'].includes(String(node.status))
      && ['runtime', 'artifact'].includes(String(node.source)) && strings(node.dependencies)
      && text(ref.kind) && text(ref.id) && (ref.version === undefined || text(ref.version) || finite(ref.version))
      && (node.parentId === undefined || text(node.parentId)) && (node.children === undefined || strings(node.children))
      && (node.updatedAtMs === undefined || natural(node.updatedAtMs)) && (node.decision === undefined || text(node.decision))
      && (node.reasons === undefined || strings(node.reasons))
      && (node.applicationMethodComparison === undefined || isApplicationMethodComparison(node.applicationMethodComparison))
      && (node.factors === undefined || (Array.isArray(node.factors) && node.factors.every((raw) => { const factor = record(raw); return ['name', 'before', 'after', 'reason'].every((key) => text(factor[key])); })))
      && (node.evidenceRefs === undefined || (Array.isArray(node.evidenceRefs) && node.evidenceRefs.every((raw) => { const evidence = record(raw); return text(evidence.kind) && text(evidence.id) && (evidence.version === undefined || natural(evidence.version)); })))
      && (node.optimization === undefined || (() => { const optimization = record(node.optimization); return text(optimization.scope)
        && ['baselineModel', 'candidateModel'].every((key) => optimization[key] === undefined || text(optimization[key]))
        && (optimization.promptChanged === undefined || typeof optimization.promptChanged === 'boolean')
        && (optimization.selectedCandidateIndex === undefined || natural(optimization.selectedCandidateIndex)); })())
      && (node.metrics === undefined || (Array.isArray(node.metrics) && node.metrics.every((rawMetric) => {
        const metric = record(rawMetric); return text(metric.label) && (metric.baseline === null || finite(metric.baseline))
          && (metric.candidate === null || finite(metric.candidate)) && (metric.unit === undefined || text(metric.unit))
          && (metric.value === undefined || metric.value === null || finite(metric.value)) && (metric.sampleCount === undefined || natural(metric.sampleCount));
      })));
  })) return false;
  const ids = new Set(item.nodes.map((node) => record(node).id));
  return ids.size === item.nodes.length && (item.currentNodeId === null || ids.has(item.currentNodeId))
    && item.edges.every((raw) => { const edge = record(raw); return text(edge.source) && text(edge.target) && ids.has(edge.source) && ids.has(edge.target); });
}
