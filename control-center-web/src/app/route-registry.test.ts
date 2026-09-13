import { describe, expect, it } from 'vitest';
import { routeRegistry } from '@/app/route-registry';

describe('route registry', () => {
  it('contains every control-center route exactly once', () => {
    expect(routeRegistry).toHaveLength(24);
    expect(new Set(routeRegistry.map((route) => route.id)).size).toBe(24);
    expect(new Set(routeRegistry.map((route) => route.path)).size).toBe(24);
    expect(routeRegistry).toContainEqual(
      expect.objectContaining({ id: 'schedules', path: '/schedules' }),
    );
    expect(routeRegistry).toContainEqual(
      expect.objectContaining({ id: 'trace-agent', path: '/trace-agent' }),
    );
    expect(routeRegistry).toContainEqual(
      expect.objectContaining({
        id: 'evolution-report',
        path: '/evolution-report',
        surface: 'standalone',
      }),
    );
    expect(routeRegistry).toContainEqual(
      expect.objectContaining({ id: 'eval-lab', path: '/eval-lab' }),
    );
    expect(routeRegistry).toContainEqual(
      expect.objectContaining({ id: 'agent-capsule', path: '/agent-capsule' }),
    );
  });
});
