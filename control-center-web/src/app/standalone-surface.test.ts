import { describe, expect, it } from 'vitest';
import {
  EVOLUTION_REPORT_PATH,
  standaloneSurfaceForPath,
} from './standalone-surface';

describe('standaloneSurfaceForPath', () => {
  it('owns only the exact desktop pet surface route', () => {
    expect(standaloneSurfaceForPath('/desktop-pet')).toBe('desktop-pet');
    expect(standaloneSurfaceForPath('/desktop-pet/')).toBe('desktop-pet');
    expect(standaloneSurfaceForPath('/desktop-pet-other')).toBeNull();
  });
  it('opens a compact screen conversation without creating another desktop', () => {
    expect(standaloneSurfaceForPath('/screen-assistant')).toBe('screen-assistant');
    expect(standaloneSurfaceForPath('/screen-assistant/')).toBe('screen-assistant');
    expect(standaloneSurfaceForPath('/screen-assistant-extra')).toBeNull();
  });
  it('keeps the capture conversation under the managed Agent Capsule App', () => {
    expect(standaloneSurfaceForPath('/agent-capsule')).toBeNull();
    expect(standaloneSurfaceForPath('/agent-capsule', '?surface=capture')).toBe('agent-capsule');
    expect(standaloneSurfaceForPath('/agent-capsule/', '?surface=capture')).toBe('agent-capsule');
    expect(standaloneSurfaceForPath('/agent-capsule', '?surface=launcher')).toBeNull();
  });
  it('owns the Evolution Report as a document path outside the PAWOS desktop', () => {
    expect(EVOLUTION_REPORT_PATH).toBe('/evolution-report');
    expect(standaloneSurfaceForPath('/evolution-report')).toBe('evolution-report');
    expect(standaloneSurfaceForPath('/evolution-report/')).toBe('evolution-report');
  });

  it('does not steal ordinary PAWOS application routes', () => {
    expect(standaloneSurfaceForPath('/')).toBeNull();
    expect(standaloneSurfaceForPath('/observability')).toBeNull();
    expect(standaloneSurfaceForPath('/evolution-report-extra')).toBeNull();
  });
});
