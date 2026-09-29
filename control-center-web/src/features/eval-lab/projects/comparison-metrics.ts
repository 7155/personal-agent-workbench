import type { LabWorkflowMetric, LabWorkflowNode } from './project-workflow-types';

export const metricIsRatio = (metric: LabWorkflowMetric) => metric.unit === 'ratio' || /Rate$/.test(metric.label);
export const metricIsCost = (metric: LabWorkflowMetric) => metric.unit === 'USD' || /cost|费用|成本/i.test(metric.label);
export const metricIsLatency = (metric: LabWorkflowMetric) => /latency|duration|耗时|延迟/i.test(metric.label);
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
export function pairedMetricVisual(metric: LabWorkflowMetric) {
  if (metric.value != null || !finite(metric.baseline) || !finite(metric.candidate)) return undefined;
  const before = metric.baseline; const after = metric.candidate;
  const difference = after - before;
  const round = (value: number) => Number(value.toFixed(2));
  const sign = difference > 0 ? '+' : difference < 0 ? '−' : '';
  const delta = difference === 0 ? '无变化' : metricIsRatio(metric)
    ? `${sign}${round(Math.abs(difference) * 100)} 个百分点`
    : (metricIsCost(metric) || metricIsLatency(metric)) && before > 0 && after >= 0
      ? `${sign}${round(Math.abs(difference) / before * 100)}%`
      : `${sign}${Number(Math.abs(difference).toFixed(6)).toLocaleString()}${metric.unit && metric.unit !== 'ratio' ? ` ${metric.unit}` : ''}`;
  const maximum = metricIsRatio(metric) ? 1 : Math.max(before, after);
  // Invalid ratios and negative values still have readable numbers, never misleading bars.
  const drawable = before >= 0 && after >= 0 && (!metricIsRatio(metric) || (before <= 1 && after <= 1));
  return { delta, beforeWidth: drawable ? maximum === 0 ? 0 : before / maximum * 100 : undefined, afterWidth: drawable ? maximum === 0 ? 0 : after / maximum * 100 : undefined };
}
export type DecisionGroup = 'baseline' | 'keep' | 'reject' | 'pending';
export function decisionGroup(node: LabWorkflowNode): DecisionGroup {
  if (node.status !== 'completed') return 'pending';
  const decision = node.decision?.toLowerCase();
  return decision === 'baseline' ? 'baseline' : decision === 'keep' || decision === 'improved' ? 'keep' : decision === 'reject' || decision === 'no_improvement' ? 'reject' : 'pending';
}
