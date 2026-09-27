import type { OptimizationObjective } from './optimization-parameters';

export type OptimizationWeights = { quality: number; cost: number; latency: number };
export type WeightAxis = keyof OptimizationWeights;
export const weightAxes: WeightAxis[] = ['quality', 'cost', 'latency'];
export const weightPresets: Record<OptimizationObjective, OptimizationWeights> = {
  quality: { quality: 70, cost: 15, latency: 15 },
  cost: { quality: 20, cost: 65, latency: 15 },
  latency: { quality: 20, cost: 15, latency: 65 },
  balanced: { quality: 34, cost: 33, latency: 33 },
};

/** Integer percentages with a stable total, including rounding at triangle edges. */
function percentages(values: number[]): OptimizationWeights {
  const total = values.reduce((sum, value) => sum + value, 0);
  if (!Number.isFinite(total) || total <= 0) return { ...weightPresets.balanced };
  const scaled = values.map((value) => value / total * 100);
  const rounded = scaled.map(Math.floor);
  const order = scaled.map((value, index) => ({ index, remainder: value - rounded[index]! })).sort((a, b) => b.remainder - a.remainder);
  const remaining = 100 - rounded.reduce((sum, value) => sum + value, 0);
  for (let i = 0; i < remaining; i++) rounded[order[i]!.index]! += 1;
  return { quality: rounded[0]!, cost: rounded[1]!, latency: rounded[2]! };
}

export function preferenceWeights(objective: OptimizationObjective, value?: unknown): OptimizationWeights {
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    const numbers = weightAxes.map((axis) => record[axis]);
    if (numbers.every((number) => typeof number === 'number' && Number.isFinite(number) && number >= 0 && number <= 100) && (numbers as number[]).reduce((a, b) => a + b, 0) > 0) return percentages(numbers as number[]);
  }
  return { ...weightPresets[objective] };
}

export function adjustWeight(weights: OptimizationWeights, axis: WeightAxis, next: number): OptimizationWeights {
  const value = Math.round(Math.min(100, Math.max(0, next)));
  const others = weightAxes.filter((item) => item !== axis);
  const total = weights[others[0]!] + weights[others[1]!];
  const first = Math.round((100 - value) * (total ? weights[others[0]!] / total : .5));
  return { ...weights, [axis]: value, [others[0]!]: first, [others[1]!]: 100 - value - first };
}

export function weightObjective(weights: OptimizationWeights): OptimizationObjective {
  const sorted = [...weightAxes].sort((a, b) => weights[b] - weights[a]);
  return weights[sorted[0]!] - weights[sorted[1]!] <= 5 ? 'balanced' : sorted[0]!;
}

export const triangleVertices = [{ x: 160, y: 38 }, { x: 42, y: 224 }, { x: 278, y: 224 }] as const;
export function weightPoint(weights: OptimizationWeights) {
  return { x: weightAxes.reduce((sum, axis, i) => sum + weights[axis] * triangleVertices[i]!.x / 100, 0), y: weightAxes.reduce((sum, axis, i) => sum + weights[axis] * triangleVertices[i]!.y / 100, 0) };
}

/** Project outside drags to the nearest edge, then obtain barycentric weights. */
export function pointWeights(x: number, y: number): OptimizationWeights {
  const barycentric = (px: number, py: number) => {
    const quality = (224 - py) / 186;
    const latency = (px - 42 - 118 * quality) / 236;
    return [quality, 1 - quality - latency, latency];
  };
  let values = barycentric(x, y);
  if (values.some((value) => value < 0)) {
    let best = { x: 160, y: 38, distance: Infinity };
    for (let i = 0; i < 3; i++) {
      const a = triangleVertices[i]!; const b = triangleVertices[(i + 1) % 3]!;
      const dx = b.x - a.x; const dy = b.y - a.y;
      const t = Math.max(0, Math.min(1, ((x - a.x) * dx + (y - a.y) * dy) / (dx * dx + dy * dy)));
      const px = a.x + t * dx; const py = a.y + t * dy;
      const distance = (px - x) ** 2 + (py - y) ** 2;
      if (distance < best.distance) best = { x: px, y: py, distance };
    }
    values = barycentric(best.x, best.y);
  }
  return percentages(values.map((value) => Math.max(0, value)));
}
