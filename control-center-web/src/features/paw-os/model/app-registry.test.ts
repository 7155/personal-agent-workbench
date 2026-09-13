import { describe, expect, it } from 'vitest';
import { routeRegistry } from '@/app/route-registry';
import {
  coreAppIds,
  pawOsApp,
  pawOsAppForRoute,
  pawOsAppRegistry,
  primaryDockAppIds,
  wayfinderRouteId,
} from './app-registry';

describe('pawOsAppRegistry', () => {
  it('keeps Project Field as the windowless Wayfinder home', () => {
    expect(wayfinderRouteId).toBe('project-field');
    expect(pawOsAppForRoute('project-field')).toBeNull();
  });

  it('groups legacy routes into the accepted PAWOS Apps', () => {
    expect(pawOsAppRegistry.map((app) => app.id)).toEqual([
      'schedules',
      'project-workbench',
      'agent',
      'agent-capsule',
      'memory',
      'knowledge',
      'input-studio',
      'app-center',
      'system-monitor',
      'trace-agent',
      'eval-lab',
      'system-settings',
      'files',
      'browser',
      'terminal',
    ]);
    expect(pawOsAppForRoute('planning')?.id).toBe('project-workbench');
    expect(pawOsAppForRoute('work-documents')?.id).toBe('project-workbench');
    expect(pawOsAppForRoute('rooms')?.id).toBe('agent');
    expect(pawOsAppForRoute('agent-capsule')?.id).toBe('agent-capsule');
    expect(pawOsAppForRoute('voice')?.id).toBe('input-studio');
    expect(pawOsAppForRoute('history')?.id).toBe('input-studio');
    expect(pawOsAppForRoute('diagnostics')?.id).toBe('system-monitor');
    expect(pawOsAppForRoute('trace-agent')?.id).toBe('trace-agent');
    expect(pawOsAppForRoute('evolution-report')).toBeNull();
    expect(pawOsAppForRoute('governance')?.id).toBe('system-settings');
  });

  it('maps every old route exactly once, except the Wayfinder home', () => {
    const appRoutes = pawOsAppRegistry.flatMap((app) => app.routeIds);
    const expected = routeRegistry
      .filter((route) => route.surface !== 'standalone')
      .map((route) => route.id)
      .filter((id) => id !== wayfinderRouteId);

    expect(appRoutes).toHaveLength(new Set(appRoutes).size);
    expect(appRoutes.sort()).toEqual(expected.sort());
  });

  it('keeps system utilities out of the focused work Dock', () => {
    expect(primaryDockAppIds).toEqual([
      'agent',
      'eval-lab',
      'project-workbench',
      'memory',
      'knowledge',
      'files',
      'browser',
      'terminal',
    ]);
    expect(primaryDockAppIds.length).toBeLessThan(pawOsAppRegistry.length);
  });

  it('exposes the six core PAWOS product entry points separately from utilities', () => {
    expect(coreAppIds).toEqual([
      'agent',
      'eval-lab',
      'project-workbench',
      'memory',
      'knowledge',
      'input-studio',
    ]);
    expect(new Set(coreAppIds).size).toBe(6);
    for (const appId of coreAppIds) expect(pawOsApp(appId).id).toBe(appId);
    expect(coreAppIds).not.toContain('system-monitor');
    expect(coreAppIds).not.toContain('files');
    expect(coreAppIds).not.toContain('browser');
    expect(coreAppIds).not.toContain('terminal');
  });
});
