import { describe, expect, it } from 'vitest';
import { pairedMetricVisual, decisionGroup } from './comparison-metrics';
import type { LabWorkflowNode } from './project-workflow-types';

describe('paired comparison visuals', () => {
  it('uses percentage points for quality and relative change for resource cost', () => {
    expect(pairedMetricVisual({ label: '通过率', unit: 'ratio', baseline: .5, candidate: .8 })).toEqual({ delta: '+30 个百分点', beforeWidth: 50, afterWidth: 80 });
    expect(pairedMetricVisual({ label: '费用估算', unit: 'USD', baseline: 2, candidate: .1 })).toEqual({ delta: '−95%', beforeWidth: 100, afterWidth: 5 });
  });
  it('does not divide by zero, invent missing metrics, or chart negative and invalid ratios', () => {
    expect(pairedMetricVisual({ label: '费用', unit: 'USD', baseline: 0, candidate: .1 })?.delta).toBe('+0.1 USD');
    expect(pairedMetricVisual({ label: '耗时', baseline: 0, candidate: 0 })).toEqual({ delta: '无变化', beforeWidth: 0, afterWidth: 0 });
    expect(pairedMetricVisual({ label: 'MRR', baseline: null, candidate: null, value: .7 })).toBeUndefined();
    expect(pairedMetricVisual({ label: 'MRR', baseline: .2, candidate: null })).toBeUndefined();
    expect(pairedMetricVisual({ label: '评分', baseline: -1, candidate: 1 })?.beforeWidth).toBeUndefined();
    expect(pairedMetricVisual({ label: '通过率', unit: 'ratio', baseline: 1, candidate: 1.2 })?.afterWidth).toBeUndefined();
  });
  it('keeps running or uncertain records out of the accepted segment', () => {
    const node = { status: 'running', decision: 'keep' } as LabWorkflowNode;
    expect(decisionGroup(node)).toBe('pending');
    expect(decisionGroup({ ...node, status: 'completed', decision: 'no_improvement' })).toBe('reject');
    expect(decisionGroup({ ...node, status: 'completed', decision: 'inconclusive' })).toBe('pending');
    expect(decisionGroup({ ...node, status: 'completed', decision: 'baseline' })).toBe('baseline');
    expect(decisionGroup({ ...node, status: 'completed', decision: 'improved' })).toBe('keep');
    expect(decisionGroup({ ...node, decision: 'improved' })).toBe('pending');
  });
});
