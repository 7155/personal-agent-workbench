import { describe, expect, it } from 'vitest';
import { adjustWeight, pointWeights, preferenceWeights, triangleVertices, weightObjective, weightPoint, weightPresets } from './optimization-weights';

describe('optimization preference weights', () => {
  it('recovers legacy and invalid preferences without NaN or negative weights', () => {
    expect(preferenceWeights('cost')).toEqual(weightPresets.cost);
    expect(preferenceWeights('quality', { quality: -1, cost: 60, latency: 41 })).toEqual(weightPresets.quality);
    expect(preferenceWeights('latency', { quality: NaN, cost: 10, latency: 90 })).toEqual(weightPresets.latency);
    expect(preferenceWeights('balanced', { quality: 1, cost: 1, latency: 1 })).toEqual(weightPresets.balanced);
  });
  it('keeps the changed slider exact and redistributes the other two, including a pure corner', () => {
    expect(adjustWeight(weightPresets.quality, 'cost', 60)).toEqual({ quality: 33, cost: 60, latency: 7 });
    expect(adjustWeight({ quality: 100, cost: 0, latency: 0 }, 'quality', 20)).toEqual({ quality: 20, cost: 40, latency: 40 });
    expect(weightObjective({ quality: 30, cost: 10, latency: 60 })).toBe('latency');
  });
  it('maps vertices, arbitrary positions and outside drags to a bounded total of 100', () => {
    triangleVertices.forEach((p, i) => expect(Object.values(pointWeights(p.x, p.y))).toEqual([0, 1, 2].map((axis) => axis === i ? 100 : 0)));
    expect(pointWeights(160, -100)).toEqual({ quality: 100, cost: 0, latency: 0 });
    expect(pointWeights(160, 300)).toEqual({ quality: 0, cost: 50, latency: 50 });
    for (let quality = 0; quality <= 100; quality += 5) for (let cost = 0; cost <= 100 - quality; cost += 5) {
      const weights = { quality, cost, latency: 100 - quality - cost }; const p = weightPoint(weights);
      expect(pointWeights(p.x, p.y)).toEqual(weights);
    }
    for (const [x, y] of [[-500, 100], [900, 800], [80, 120]]) {
      const values = Object.values(pointWeights(x!, y!));
      expect(values.reduce((a, b) => a + b, 0)).toBe(100);
      expect(values.every((value) => value >= 0 && value <= 100)).toBe(true);
    }
  });
});
